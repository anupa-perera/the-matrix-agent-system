from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.onboarding import OnboardingService
from thematrix.schemas import (
    AuthMode,
    FileChangeConsent,
    OnboardingProfile,
    PrivacyMode,
    ProviderConfig,
)


def test_onboarding_stores_preferences_without_secret_values(tmp_path) -> None:
    vault = MemoryVault(tmp_path / "vault")
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    vault.initialize()
    store.initialize()

    provider_config = ProviderConfig(
        provider_id="openrouter",
        selected_model="openai/gpt-5-mini",
        auth_mode=AuthMode.API_KEY,
        secret_ref="keyring:provider:openrouter:api_key",
        file_change_consent=FileChangeConsent.ASK_EACH_TIME,
    )
    profile = OnboardingProfile(
        default_provider_id="openrouter",
        default_model="openai/gpt-5-mini",
        auth_mode=AuthMode.API_KEY,
        privacy_mode=PrivacyMode.ASK_EACH_TIME,
        file_change_consent=FileChangeConsent.ASK_EACH_TIME,
        guarded_shell_enabled=True,
        vault_path=str(tmp_path / "vault"),
        secret_configured=True,
    )

    OnboardingService(store, vault).complete(profile, provider_config)

    current = store.get_default_provider_config()
    assert current is not None
    assert current.provider_id == "openrouter"
    assert store.get_preference("onboarding_complete") is True
    assert store.get_preference("default_privacy_mode") == "ask_each_time"
    log_text = (tmp_path / "vault" / "log.md").read_text(encoding="utf-8")
    assert "Onboarding completed" in log_text
    assert "sk-" not in log_text

