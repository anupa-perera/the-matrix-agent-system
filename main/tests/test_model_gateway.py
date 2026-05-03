from __future__ import annotations

from typing import Any

import pytest

from thematrix.memory import RuntimeStore
from thematrix.providers.adapters import (
    AnthropicMessagesAdapter,
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


def _profile(provider_id: str):
    return next(profile for profile in provider_catalog() if profile.provider_id == provider_id)

