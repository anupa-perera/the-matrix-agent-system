from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from thematrix.schemas import (
    AuthMode,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderHealth,
    ProviderAdapterKind,
    ProviderConfig,
    ProviderProfile,
)


class ProviderAdapterError(RuntimeError):
    """Raised when a provider adapter cannot complete a request."""


class JsonTransport(Protocol):
    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CodexExecResult:
    stdout: str
    stderr: str
    returncode: int


class CodexExecRunner(Protocol):
    def run(
        self,
        args: list[str],
        prompt: str,
        timeout_seconds: int,
        cwd: Path,
    ) -> CodexExecResult: ...


class UrlLibJsonTransport:
    """Small standard-library JSON transport for provider adapters."""

    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProviderAdapterError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ProviderAdapterError(str(exc.reason)) from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderAdapterError("Provider returned invalid JSON.") from exc


@dataclass
class SubprocessCodexExecRunner:
    command: str | None = None

    def run(
        self,
        args: list[str],
        prompt: str,
        timeout_seconds: int,
        cwd: Path,
    ) -> CodexExecResult:
        command = self.command or shutil.which("codex")
        if not command:
            raise ProviderAdapterError(
                "OpenAI Codex CLI is not installed. Install Codex, run `codex`, "
                "and choose Sign in with ChatGPT."
            )
        try:
            completed = subprocess.run(
                [command, *args],
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(cwd),
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderAdapterError("Codex CLI timed out.") from exc
        except OSError as exc:
            raise ProviderAdapterError(f"Codex CLI could not start: {exc}") from exc
        return CodexExecResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )


@dataclass
class OpenAICompatibleAdapter:
    transport: JsonTransport
    timeout_seconds: int = 60

    def generate(
        self,
        request: ModelRequest,
        profile: ProviderProfile,
        config: ProviderConfig,
        credential: str | None,
    ) -> ModelResponse:
        base_url = _base_url(config, profile)
        headers = {"Content-Type": "application/json"}
        if config.auth_mode != AuthMode.NONE and credential:
            headers["Authorization"] = f"Bearer {credential}"
        payload = {
            "model": config.selected_model,
            "messages": [message.model_dump() for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        raw = self.transport.post_json(
            url=f"{base_url}/chat/completions",
            headers=headers,
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        text = _extract_openai_compatible_text(raw)
        return ModelResponse(
            provider_id=profile.provider_id,
            model=config.selected_model,
            text=text,
            raw=raw,
            usage=raw.get("usage", {}),
        )


@dataclass
class CodexExecAdapter:
    runner: CodexExecRunner
    timeout_seconds: int = 180
    status_timeout_seconds: int = 30

    def health_check(
        self,
        profile: ProviderProfile,
        config: ProviderConfig,
        credential: str | None,
    ) -> ProviderHealth:
        if credential:
            return ProviderHealth(
                provider_id=profile.provider_id,
                ok=False,
                message="OpenAI Codex sign-in is managed by the Codex CLI.",
            )
        cwd = Path.cwd()
        try:
            version = self.runner.run(
                args=["--version"],
                prompt="",
                timeout_seconds=self.status_timeout_seconds,
                cwd=cwd,
            )
            status = self.runner.run(
                args=["login", "status"],
                prompt="",
                timeout_seconds=self.status_timeout_seconds,
                cwd=cwd,
            )
        except ProviderAdapterError as exc:
            return ProviderHealth(provider_id=profile.provider_id, ok=False, message=str(exc))
        if version.returncode != 0:
            return ProviderHealth(
                provider_id=profile.provider_id,
                ok=False,
                message=_codex_error_message(version.stderr, version.stdout),
            )
        if status.returncode != 0:
            return ProviderHealth(
                provider_id=profile.provider_id,
                ok=False,
                message=(
                    "Codex CLI is installed, but it is not signed in. Run `codex login` "
                    "and choose Sign in with ChatGPT."
                ),
            )
        status_text = f"{status.stdout}\n{status.stderr}".lower()
        if "logged in" not in status_text:
            return ProviderHealth(
                provider_id=profile.provider_id,
                ok=False,
                message=(
                    "Codex CLI did not report an active sign-in. Run `codex login` "
                    "and choose Sign in with ChatGPT."
                ),
            )
        version_text = _first_non_empty_line(version.stdout, version.stderr) or "Codex CLI"
        return ProviderHealth(
            provider_id=profile.provider_id,
            ok=True,
            message=f"{version_text} is installed and signed in.",
        )

    def generate(
        self,
        request: ModelRequest,
        profile: ProviderProfile,
        config: ProviderConfig,
        credential: str | None,
    ) -> ModelResponse:
        if credential:
            raise ProviderAdapterError("OpenAI Codex sign-in is managed by the Codex CLI.")
        prompt = _codex_prompt(request.messages)
        args = [
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
        ]
        selected_model = config.selected_model.strip()
        if selected_model and selected_model not in {"auto", "default"}:
            args.extend(["--model", selected_model])
        with tempfile.TemporaryDirectory(prefix="thematrix-codex-") as workdir:
            args.extend(["--cd", workdir, "-"])
            result = self.runner.run(
                args=args,
                prompt=prompt,
                timeout_seconds=self.timeout_seconds,
                cwd=Path(workdir),
            )
        if result.returncode != 0:
            raise ProviderAdapterError(_codex_error_message(result.stderr, result.stdout))
        text = result.stdout.strip()
        if not text:
            raise ProviderAdapterError("Codex CLI returned an empty response.")
        return ModelResponse(
            provider_id=profile.provider_id,
            model=config.selected_model,
            text=text,
            raw={"stderr_tail": result.stderr[-4000:]},
            usage={},
        )


@dataclass
class AnthropicMessagesAdapter:
    transport: JsonTransport
    timeout_seconds: int = 60

    def generate(
        self,
        request: ModelRequest,
        profile: ProviderProfile,
        config: ProviderConfig,
        credential: str | None,
    ) -> ModelResponse:
        if not credential:
            raise ProviderAdapterError("Anthropic requires an API key.")
        base_url = _base_url(config, profile)
        headers = {
            "Content-Type": "application/json",
            "x-api-key": credential,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": config.selected_model,
            "messages": _anthropic_messages(request.messages),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        raw = self.transport.post_json(
            url=f"{base_url}/messages",
            headers=headers,
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        text = _extract_anthropic_text(raw)
        usage = raw.get("usage", {})
        return ModelResponse(
            provider_id=profile.provider_id,
            model=config.selected_model,
            text=text,
            raw=raw,
            usage=usage,
        )


@dataclass
class GeminiGenerateContentAdapter:
    transport: JsonTransport
    timeout_seconds: int = 60

    def generate(
        self,
        request: ModelRequest,
        profile: ProviderProfile,
        config: ProviderConfig,
        credential: str | None,
    ) -> ModelResponse:
        if not credential:
            raise ProviderAdapterError("Gemini requires an API key or OAuth token.")
        base_url = _base_url(config, profile)
        headers = {"Content-Type": "application/json"}
        url = f"{base_url}/models/{quote(config.selected_model, safe='')}:generateContent"
        if config.auth_mode == AuthMode.OAUTH:
            headers["Authorization"] = f"Bearer {credential}"
        else:
            url = f"{url}?{urlencode({'key': credential})}"

        payload = {
            "contents": [_gemini_content(message) for message in request.messages],
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }
        raw = self.transport.post_json(
            url=url,
            headers=headers,
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        text = _extract_gemini_text(raw)
        return ModelResponse(
            provider_id=profile.provider_id,
            model=config.selected_model,
            text=text,
            raw=raw,
            usage=raw.get("usageMetadata", {}),
        )


@dataclass
class ProviderAdapterRegistry:
    adapters: dict[ProviderAdapterKind, object]

    @classmethod
    def default(cls) -> "ProviderAdapterRegistry":
        transport = UrlLibJsonTransport()
        return cls(
            adapters={
                ProviderAdapterKind.OPENAI_COMPATIBLE: OpenAICompatibleAdapter(transport),
                ProviderAdapterKind.ANTHROPIC_MESSAGES: AnthropicMessagesAdapter(transport),
                ProviderAdapterKind.GEMINI_GENERATE_CONTENT: GeminiGenerateContentAdapter(
                    transport
                ),
                ProviderAdapterKind.CODEX_EXEC: CodexExecAdapter(SubprocessCodexExecRunner()),
            }
        )

    def for_profile(self, profile: ProviderProfile):
        adapter = self.adapters.get(profile.adapter_kind)
        if adapter is None:
            raise ProviderAdapterError(f"No adapter registered for {profile.adapter_kind.value}.")
        return adapter


def _base_url(config: ProviderConfig, profile: ProviderProfile) -> str:
    base_url = config.base_url or profile.default_base_url
    if not base_url:
        raise ProviderAdapterError(f"Provider `{profile.provider_id}` needs a base URL.")
    return base_url.rstrip("/")


def _extract_openai_compatible_text(raw: dict[str, Any]) -> str:
    try:
        return str(raw["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderAdapterError("OpenAI-compatible response did not contain message text.") from exc


def _extract_anthropic_text(raw: dict[str, Any]) -> str:
    try:
        parts = raw["content"]
        texts = [part.get("text", "") for part in parts if part.get("type") == "text"]
    except (KeyError, TypeError) as exc:
        raise ProviderAdapterError("Anthropic response did not contain text content.") from exc
    text = "".join(texts).strip()
    if not text:
        raise ProviderAdapterError("Anthropic response text was empty.")
    return text


def _extract_gemini_text(raw: dict[str, Any]) -> str:
    try:
        parts = raw["candidates"][0]["content"]["parts"]
        texts = [part.get("text", "") for part in parts]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderAdapterError("Gemini response did not contain text content.") from exc
    text = "".join(texts).strip()
    if not text:
        raise ProviderAdapterError("Gemini response text was empty.")
    return text


def _anthropic_messages(messages: list[ModelMessage]) -> list[dict[str, str]]:
    converted: list[dict[str, str]] = []
    for message in messages:
        role = "assistant" if message.role == "assistant" else "user"
        converted.append({"role": role, "content": message.content})
    return converted


def _gemini_content(message: ModelMessage) -> dict[str, Any]:
    role = "model" if message.role == "assistant" else "user"
    return {"role": role, "parts": [{"text": message.content}]}


def _codex_prompt(messages: list[ModelMessage]) -> str:
    blocks = [
        "You are serving as the model layer for The Matrix agent system.",
        "Answer only from the messages below. Do not inspect files, run commands, or make changes.",
    ]
    for message in messages:
        role = message.role.upper()
        blocks.append(f"{role}:\n{message.content}")
    return "\n\n".join(blocks)


def _codex_error_message(stderr: str, stdout: str) -> str:
    detail = "\n".join(value for value in [stderr, stdout] if value.strip()).strip()
    if not detail:
        return "Codex CLI failed without returning details."
    lowered = detail.lower()
    if "requires a newer version of codex" in lowered:
        return (
            "Codex CLI is too old for its configured model. Update the Codex app or CLI, "
            "then try again."
        )
    if "not supported when using codex with a chatgpt account" in lowered:
        return (
            "The configured Codex model is not available for this ChatGPT sign-in. "
            "Open Codex and choose a supported model, then try again."
        )
    important_lines = [
        line.strip()
        for line in detail.splitlines()
        if line.strip().startswith("ERROR") or "AuthRequired" in line
    ]
    if important_lines:
        return f"Codex CLI failed: {' '.join(important_lines)[-800:]}"
    return f"Codex CLI failed: {detail[-800:]}"


def _first_non_empty_line(*values: str) -> str:
    for value in values:
        for line in value.splitlines():
            text = line.strip()
            if text:
                return text
    return ""
