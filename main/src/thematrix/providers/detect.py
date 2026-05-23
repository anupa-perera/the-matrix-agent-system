from __future__ import annotations

from dataclasses import dataclass
import json
import shutil
from urllib.error import URLError
from urllib.request import urlopen


@dataclass(frozen=True)
class ProviderDetection:
    provider_id: str
    display_name: str
    reachable: bool
    base_url: str
    models: list[str]
    message: str


def detect_local_providers(timeout_seconds: float = 1.0) -> list[ProviderDetection]:
    return [
        _detect_ollama(timeout_seconds),
        _detect_lm_studio(timeout_seconds),
        _detect_codex_cli(),
    ]


def _detect_ollama(timeout_seconds: float) -> ProviderDetection:
    api_url = "http://localhost:11434/api/tags"
    base_url = "http://localhost:11434/v1"
    try:
        payload = _get_json(api_url, timeout_seconds)
    except Exception as exc:
        return ProviderDetection(
            provider_id="ollama",
            display_name="Ollama",
            reachable=False,
            base_url=base_url,
            models=[],
            message=f"Not reachable: {exc}",
        )
    models = [
        str(model.get("name"))
        for model in payload.get("models", [])
        if isinstance(model, dict) and model.get("name")
    ]
    return ProviderDetection(
        provider_id="ollama",
        display_name="Ollama",
        reachable=True,
        base_url=base_url,
        models=models,
        message="Ollama is reachable.",
    )


def _detect_lm_studio(timeout_seconds: float) -> ProviderDetection:
    api_url = "http://localhost:1234/v1/models"
    base_url = "http://localhost:1234/v1"
    try:
        payload = _get_json(api_url, timeout_seconds)
    except Exception as exc:
        return ProviderDetection(
            provider_id="lmstudio",
            display_name="LM Studio",
            reachable=False,
            base_url=base_url,
            models=[],
            message=f"Not reachable: {exc}",
        )
    models = [
        str(model.get("id"))
        for model in payload.get("data", [])
        if isinstance(model, dict) and model.get("id")
    ]
    return ProviderDetection(
        provider_id="lmstudio",
        display_name="LM Studio",
        reachable=True,
        base_url=base_url,
        models=models,
        message="LM Studio is reachable.",
    )


def _detect_codex_cli() -> ProviderDetection:
    command = shutil.which("codex")
    reachable = command is not None
    return ProviderDetection(
        provider_id="openai-codex",
        display_name="OpenAI Codex",
        reachable=reachable,
        base_url="",
        models=["auto"] if reachable else [],
        message=(
            "Codex CLI is installed. Sign-in is handled by the official Codex app."
            if reachable
            else "Codex CLI is not installed."
        ),
    )


def _get_json(url: str, timeout_seconds: float) -> dict:
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid JSON") from exc
