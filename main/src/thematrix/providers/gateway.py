from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from thematrix.memory import RuntimeStore
from thematrix.providers.adapters import ProviderAdapterRegistry
from thematrix.schemas import (
    AuthMode,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    ProviderHealth,
    ProviderProfile,
)
from thematrix.security import Keymaker


class ModelGatewayError(RuntimeError):
    """Raised when a model call cannot be safely prepared or completed."""


class ProviderAdapter(Protocol):
    def generate(
        self,
        request: ModelRequest,
        profile: ProviderProfile,
        config: ProviderConfig,
        credential: str | None,
    ) -> ModelResponse: ...


@dataclass
class ModelGateway:
    """Single model-call boundary used by Oracle, Architect, Neo, and agents."""

    store: RuntimeStore
    keymaker: Keymaker
    adapters: ProviderAdapterRegistry

    def generate(
        self,
        request: ModelRequest,
        config: ProviderConfig | None = None,
    ) -> ModelResponse:
        provider_config = config or self.store.get_default_provider_config()
        if provider_config is None:
            raise ModelGatewayError("No provider is configured. Run: the-matrix setup")

        profile = self.store.get_provider_profile(provider_config.provider_id)
        if profile is None:
            raise ModelGatewayError(f"Unknown provider: {provider_config.provider_id}")

        credential = self._resolve_credential(provider_config)
        adapter = self.adapters.for_profile(profile)
        started = perf_counter()
        try:
            response = adapter.generate(request, profile, provider_config, credential)
        except Exception as exc:
            self.store.record_model_call(
                provider_id=provider_config.provider_id,
                model=provider_config.selected_model,
                ok=False,
                latency_ms=int((perf_counter() - started) * 1000),
                request_chars=sum(len(message.content) for message in request.messages),
                response_chars=0,
                error_type=type(exc).__name__,
            )
            if isinstance(exc, ModelGatewayError):
                raise
            raise ModelGatewayError(str(exc)) from exc

        self.store.record_model_call(
            provider_id=provider_config.provider_id,
            model=provider_config.selected_model,
            ok=True,
            latency_ms=int((perf_counter() - started) * 1000),
            request_chars=sum(len(message.content) for message in request.messages),
            response_chars=len(response.text),
        )
        return response

    def health_check(self, config: ProviderConfig | None = None) -> ProviderHealth:
        provider_config = config or self.store.get_default_provider_config()
        provider_id = provider_config.provider_id if provider_config else "unconfigured"
        try:
            response = self.generate(
                ModelRequest.from_prompt("Reply with one short sentence saying the provider is ready."),
                config=provider_config,
            )
        except Exception as exc:
            return ProviderHealth(provider_id=provider_id, ok=False, message=str(exc))
        return ProviderHealth(provider_id=provider_id, ok=True, message=response.text)

    def _resolve_credential(self, config: ProviderConfig) -> str | None:
        if config.auth_mode in {AuthMode.NONE, AuthMode.EXTERNAL}:
            return None
        if config.secret_ref is None:
            raise ModelGatewayError(
                f"Provider `{config.provider_id}` requires {config.auth_mode.value}, "
                "but no credential is configured."
            )
        credential = self.keymaker.resolve_api_key(config.secret_ref)
        if not credential:
            raise ModelGatewayError(
                f"Provider `{config.provider_id}` credential could not be resolved."
            )
        return credential


def default_model_gateway(store: RuntimeStore) -> ModelGateway:
    return ModelGateway(
        store=store,
        keymaker=Keymaker(),
        adapters=ProviderAdapterRegistry.default(),
    )
