from __future__ import annotations

from thematrix.schemas import (
    AuthMode,
    ProviderAdapterKind,
    ProviderKind,
    ProviderProfile,
    ReasoningEffort,
)


def provider_catalog() -> list[ProviderProfile]:
    return [
        ProviderProfile(
            provider_id="openrouter",
            display_name="OpenRouter",
            kind=ProviderKind.CLOUD,
            adapter_kind=ProviderAdapterKind.OPENAI_COMPATIBLE,
            auth_modes=[AuthMode.API_KEY, AuthMode.OAUTH],
            default_base_url="https://openrouter.ai/api/v1",
            suggested_models=[
                "openai/gpt-5-mini",
                "anthropic/claude-sonnet-4.5",
                "google/gemini-2.5-pro",
            ],
            supports_model_selection=True,
            supports_provider_routing=True,
            supports_structured_output=True,
            data_boundary="cloud: requests pass through OpenRouter to the selected model provider",
            setup_hint="Choose OpenRouter if you want one account for many cloud models.",
        ),
        ProviderProfile(
            provider_id="openai",
            display_name="OpenAI",
            kind=ProviderKind.CLOUD,
            adapter_kind=ProviderAdapterKind.OPENAI_COMPATIBLE,
            auth_modes=[AuthMode.API_KEY],
            default_base_url="https://api.openai.com/v1",
            suggested_models=["gpt-5-mini", "gpt-5.1", "gpt-4.1"],
            supports_model_selection=True,
            supports_tools=True,
            supports_structured_output=True,
            supports_prompt_cache=True,
            data_boundary="cloud: requests are sent to OpenAI",
            setup_hint=(
                "Use an OpenAI API key. ChatGPT/Codex subscription sign-in is available "
                "through the separate OpenAI Codex provider."
            ),
        ),
        ProviderProfile(
            provider_id="openai-codex",
            display_name="OpenAI Codex",
            kind=ProviderKind.CLOUD,
            adapter_kind=ProviderAdapterKind.CODEX_EXEC,
            auth_modes=[AuthMode.EXTERNAL],
            suggested_models=["auto", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
            supports_model_selection=True,
            supports_tools=False,
            supports_structured_output=False,
            supports_prompt_cache=False,
            supported_reasoning_efforts=[
                ReasoningEffort.LOW,
                ReasoningEffort.MEDIUM,
                ReasoningEffort.HIGH,
                ReasoningEffort.XHIGH,
            ],
            data_boundary=(
                "cloud: requests are sent through the official Codex client using the "
                "user's Codex sign-in"
            ),
            setup_hint=(
                "Uses the official Codex CLI. Install Codex, run it once, and choose "
                "Sign in with ChatGPT to use an eligible ChatGPT/Codex subscription."
            ),
        ),
        ProviderProfile(
            provider_id="anthropic",
            display_name="Anthropic",
            kind=ProviderKind.CLOUD,
            adapter_kind=ProviderAdapterKind.ANTHROPIC_MESSAGES,
            auth_modes=[AuthMode.API_KEY],
            default_base_url="https://api.anthropic.com/v1",
            suggested_models=["claude-sonnet-4.5", "claude-opus-4.1"],
            supports_model_selection=True,
            supports_structured_output=True,
            data_boundary="cloud: requests are sent to Anthropic",
            setup_hint="Use an Anthropic API key.",
        ),
        ProviderProfile(
            provider_id="gemini",
            display_name="Gemini",
            kind=ProviderKind.CLOUD,
            adapter_kind=ProviderAdapterKind.GEMINI_GENERATE_CONTENT,
            auth_modes=[AuthMode.API_KEY, AuthMode.OAUTH],
            default_base_url="https://generativelanguage.googleapis.com/v1beta",
            suggested_models=["gemini-2.5-pro", "gemini-2.5-flash"],
            supports_model_selection=True,
            supports_structured_output=True,
            data_boundary="cloud: requests are sent to Google Gemini",
            setup_hint="Use a Gemini API key or OAuth where supported.",
        ),
        ProviderProfile(
            provider_id="mistral",
            display_name="Mistral",
            kind=ProviderKind.CLOUD,
            adapter_kind=ProviderAdapterKind.OPENAI_COMPATIBLE,
            auth_modes=[AuthMode.API_KEY],
            default_base_url="https://api.mistral.ai/v1",
            suggested_models=["mistral-large-latest", "codestral-latest"],
            supports_model_selection=True,
            data_boundary="cloud: requests are sent to Mistral",
            setup_hint="Use a Mistral API key.",
        ),
        ProviderProfile(
            provider_id="ollama",
            display_name="Ollama",
            kind=ProviderKind.LOCAL,
            adapter_kind=ProviderAdapterKind.OPENAI_COMPATIBLE,
            auth_modes=[AuthMode.NONE],
            default_base_url="http://localhost:11434/v1",
            suggested_models=["llama3.2", "qwen2.5-coder", "mistral"],
            supports_model_selection=True,
            data_boundary="local: requests stay on this PC if Ollama is bound to localhost",
            setup_hint="Install Ollama separately and choose a local model.",
        ),
        ProviderProfile(
            provider_id="lmstudio",
            display_name="LM Studio",
            kind=ProviderKind.LOCAL,
            adapter_kind=ProviderAdapterKind.OPENAI_COMPATIBLE,
            auth_modes=[AuthMode.NONE],
            default_base_url="http://localhost:1234/v1",
            suggested_models=["local-model"],
            supports_model_selection=True,
            data_boundary="local: requests stay on this PC if LM Studio is bound to localhost",
            setup_hint="Run the LM Studio local server and choose a downloaded model.",
        ),
        ProviderProfile(
            provider_id="custom-openai-compatible",
            display_name="Custom OpenAI-compatible endpoint",
            kind=ProviderKind.CLOUD,
            adapter_kind=ProviderAdapterKind.OPENAI_COMPATIBLE,
            auth_modes=[AuthMode.API_KEY, AuthMode.LOCAL_TOKEN, AuthMode.NONE],
            suggested_models=["custom-model"],
            supports_model_selection=True,
            data_boundary="custom: depends on the endpoint configured by the user",
            setup_hint="Use this for compatible local or cloud model servers.",
        ),
    ]
