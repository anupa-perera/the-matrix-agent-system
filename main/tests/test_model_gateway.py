from __future__ import annotations

from typing import Any

import pytest

from thematrix.memory import RuntimeStore
from thematrix.providers.adapters import (
    AnthropicMessagesAdapter,
    CodexExecAdapter,
    CodexExecResult,
    GeminiGenerateContentAdapter,
    OpenAICompatibleAdapter,
    ProviderAdapterRegistry,
)
from thematrix.providers.gateway import ModelGateway, ModelGatewayError
from thematrix.providers.models import provider_catalog
from thematrix.schemas import AuthMode, ModelRequest, ProviderAdapterKind, ProviderConfig
from thematrix.security import InMemorySecretStore, Keymaker


class FakeTransport:
    def __init__(self, response: dict[str, Any]):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.response


class FakeCodexRunner:
    def __init__(self, result: CodexExecResult):
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        args: list[str],
        prompt: str,
        timeout_seconds: int,
        cwd,
    ) -> CodexExecResult:
        self.calls.append(
            {
                "args": args,
                "prompt": prompt,
                "timeout_seconds": timeout_seconds,
                "cwd": cwd,
            }
        )
        return self.result


def test_openai_compatible_adapter_translates_request() -> None:
    transport = FakeTransport({"choices": [{"message": {"content": "ready"}}]})
    profile = _profile("openrouter")
    config = ProviderConfig(
        provider_id="openrouter",
        selected_model="openai/gpt-5-mini",
        auth_mode=AuthMode.API_KEY,
    )

    response = OpenAICompatibleAdapter(transport).generate(
        ModelRequest.from_prompt("test"),
        profile,
        config,
        "secret",
    )

    assert response.text == "ready"
    assert transport.calls[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert transport.calls[0]["payload"]["model"] == "openai/gpt-5-mini"


def test_anthropic_adapter_translates_request() -> None:
    transport = FakeTransport({"content": [{"type": "text", "text": "ready"}]})
    profile = _profile("anthropic")
    config = ProviderConfig(
        provider_id="anthropic",
        selected_model="claude-sonnet-4.5",
        auth_mode=AuthMode.API_KEY,
    )

    response = AnthropicMessagesAdapter(transport).generate(
        ModelRequest.from_prompt("test"),
        profile,
        config,
        "secret",
    )

    assert response.text == "ready"
    assert transport.calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert transport.calls[0]["headers"]["x-api-key"] == "secret"
    assert transport.calls[0]["headers"]["anthropic-version"] == "2023-06-01"


def test_gemini_adapter_translates_api_key_request() -> None:
    transport = FakeTransport(
        {"candidates": [{"content": {"parts": [{"text": "ready"}]}}]}
    )
    profile = _profile("gemini")
    config = ProviderConfig(
        provider_id="gemini",
        selected_model="gemini-2.5-flash",
        auth_mode=AuthMode.API_KEY,
    )

    response = GeminiGenerateContentAdapter(transport).generate(
        ModelRequest.from_prompt("test"),
        profile,
        config,
        "secret",
    )

    assert response.text == "ready"
    assert "models/gemini-2.5-flash:generateContent" in transport.calls[0]["url"]
    assert "key=secret" in transport.calls[0]["url"]
    assert transport.calls[0]["payload"]["contents"][0]["parts"][0]["text"] == "test"


def test_codex_exec_adapter_runs_read_only_ephemeral_without_secret() -> None:
    runner = FakeCodexRunner(CodexExecResult(stdout="ready\n", stderr="progress", returncode=0))
    profile = _profile("openai-codex")
    config = ProviderConfig(
        provider_id="openai-codex",
        selected_model="auto",
        auth_mode=AuthMode.EXTERNAL,
    )

    response = CodexExecAdapter(runner).generate(
        ModelRequest.from_prompt("test"),
        profile,
        config,
        None,
    )

    call = runner.calls[0]
    assert response.text == "ready"
    assert call["args"][:5] == [
        "exec",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--skip-git-repo-check",
    ]
    assert "--model" not in call["args"]
    assert call["args"][-1] == "-"
    assert "Do not inspect files" in call["prompt"]
    assert "USER:\ntest" in call["prompt"]


def test_codex_health_check_uses_cli_status_instead_of_exec() -> None:
    class StatusRunner(FakeCodexRunner):
        def run(self, args, prompt, timeout_seconds, cwd) -> CodexExecResult:
            self.calls.append(
                {
                    "args": args,
                    "prompt": prompt,
                    "timeout_seconds": timeout_seconds,
                    "cwd": cwd,
                }
            )
            if args == ["--version"]:
                return CodexExecResult(stdout="codex-cli 0.121.0\n", stderr="", returncode=0)
            if args == ["login", "status"]:
                return CodexExecResult(stdout="Logged in using ChatGPT\n", stderr="", returncode=0)
            raise AssertionError(f"unexpected args: {args}")

    runner = StatusRunner(CodexExecResult(stdout="", stderr="", returncode=0))
    profile = _profile("openai-codex")
    config = ProviderConfig(
        provider_id="openai-codex",
        selected_model="auto",
        auth_mode=AuthMode.EXTERNAL,
    )

    health = CodexExecAdapter(runner).health_check(profile, config, None)

    assert health.ok
    assert health.message == "codex-cli 0.121.0 is installed and signed in."
    assert [call["args"] for call in runner.calls] == [
        ["--version"],
        ["login", "status"],
    ]


def test_codex_error_message_hides_prompt_noise(tmp_path) -> None:
    runner = FakeCodexRunner(
        CodexExecResult(
            stdout=(
                "USER:\n"
                "Reply with one short sentence saying the provider is ready.\n"
                "ERROR: {\"error\":{\"message\":\"The 'gpt-5.5' model requires a "
                "newer version of Codex. Please upgrade to the latest app or CLI.\"}}\n"
            ),
            stderr=(
                "AuthRequired(AuthRequiredError { www_authenticate_header: "
                '"Bearer realm=\\"MCP Server\\"" })'
            ),
            returncode=1,
        )
    )
    config = ProviderConfig(
        provider_id="openai-codex",
        selected_model="auto",
        auth_mode=AuthMode.EXTERNAL,
    )
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    for profile in provider_catalog():
        store.upsert_provider(profile)
    store.configure_provider(config)

    with pytest.raises(ModelGatewayError) as exc_info:
        gateway = ModelGateway(
            store=store,
            keymaker=Keymaker(InMemorySecretStore()),
            adapters=ProviderAdapterRegistry(
                adapters={ProviderAdapterKind.CODEX_EXEC: CodexExecAdapter(runner)}
            ),
        )
        gateway.generate(ModelRequest.from_prompt("test"))

    message = str(exc_info.value)
    assert "too old" in message
    assert "Reply with one short sentence" not in message


def test_gateway_uses_registered_adapter_and_records_metadata(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    for profile in provider_catalog():
        store.upsert_provider(profile)
    keymaker = Keymaker(InMemorySecretStore())
    secret_ref = keymaker.store_api_key("openrouter", "secret").secret_ref
    store.configure_provider(
        ProviderConfig(
            provider_id="openrouter",
            selected_model="openai/gpt-5-mini",
            auth_mode=AuthMode.API_KEY,
            secret_ref=secret_ref,
        )
    )
    transport = FakeTransport({"choices": [{"message": {"content": "ready"}}]})
    gateway = ModelGateway(
        store=store,
        keymaker=keymaker,
        adapters=ProviderAdapterRegistry(
            adapters={
                ProviderAdapterKind.OPENAI_COMPATIBLE: OpenAICompatibleAdapter(transport),
            }
        ),
    )

    response = gateway.generate(ModelRequest.from_prompt("test"))

    assert response.text == "ready"
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM model_calls").fetchone()
    assert row["provider_id"] == "openrouter"
    assert row["ok"] == 1
    assert row["request_chars"] == 4
    assert row["response_chars"] == 5


def test_gateway_rejects_missing_required_secret(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    for profile in provider_catalog():
        store.upsert_provider(profile)
    store.configure_provider(
        ProviderConfig(
            provider_id="openai",
            selected_model="gpt-5-mini",
            auth_mode=AuthMode.API_KEY,
        )
    )
    gateway = ModelGateway(
        store=store,
        keymaker=Keymaker(InMemorySecretStore()),
        adapters=ProviderAdapterRegistry.default(),
    )

    with pytest.raises(ModelGatewayError, match="no credential"):
        gateway.generate(ModelRequest.from_prompt("test"))


def test_gateway_allows_external_auth_without_stored_secret(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    for profile in provider_catalog():
        store.upsert_provider(profile)
    store.configure_provider(
        ProviderConfig(
            provider_id="openai-codex",
            selected_model="auto",
            auth_mode=AuthMode.EXTERNAL,
        )
    )
    runner = FakeCodexRunner(CodexExecResult(stdout="ready", stderr="", returncode=0))
    gateway = ModelGateway(
        store=store,
        keymaker=Keymaker(InMemorySecretStore()),
        adapters=ProviderAdapterRegistry(
            adapters={
                ProviderAdapterKind.CODEX_EXEC: CodexExecAdapter(runner),
            }
        ),
    )

    response = gateway.generate(ModelRequest.from_prompt("test"))

    assert response.provider_id == "openai-codex"
    assert response.text == "ready"


def test_gateway_uses_adapter_specific_health_check_for_codex(tmp_path) -> None:
    class StatusRunner(FakeCodexRunner):
        def run(self, args, prompt, timeout_seconds, cwd) -> CodexExecResult:
            self.calls.append({"args": args, "prompt": prompt})
            if args == ["--version"]:
                return CodexExecResult(stdout="codex-cli 0.121.0", stderr="", returncode=0)
            if args == ["login", "status"]:
                return CodexExecResult(stdout="Logged in using ChatGPT", stderr="", returncode=0)
            raise AssertionError(f"unexpected args: {args}")

    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    for profile in provider_catalog():
        store.upsert_provider(profile)
    store.configure_provider(
        ProviderConfig(
            provider_id="openai-codex",
            selected_model="auto",
            auth_mode=AuthMode.EXTERNAL,
        )
    )
    runner = StatusRunner(CodexExecResult(stdout="", stderr="", returncode=0))
    gateway = ModelGateway(
        store=store,
        keymaker=Keymaker(InMemorySecretStore()),
        adapters=ProviderAdapterRegistry(
            adapters={ProviderAdapterKind.CODEX_EXEC: CodexExecAdapter(runner)}
        ),
    )

    health = gateway.health_check()

    assert health.ok
    assert [call["args"] for call in runner.calls] == [
        ["--version"],
        ["login", "status"],
    ]
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM model_calls").fetchall()
    assert rows == []


def _profile(provider_id: str):
    return next(profile for profile in provider_catalog() if profile.provider_id == provider_id)
