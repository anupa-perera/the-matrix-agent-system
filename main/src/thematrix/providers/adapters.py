from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from thematrix.schemas import (
    AuthMode,
    ModelMessage,
    ModelRequest,
    ModelResponse,
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

