from __future__ import annotations

from thematrix.memory import MemoryVault, RuntimeStore
from thematrix.schemas import OnboardingProfile, ProviderConfig


class OnboardingService:
    """Stores first-run setup without leaking secrets into memory."""

    def __init__(self, store: RuntimeStore, vault: MemoryVault):
        self.store = store
        self.vault = vault

    def complete(self, profile: OnboardingProfile, provider_config: ProviderConfig) -> None:
        self.store.configure_provider(provider_config)
        self.store.set_preference("onboarding_complete", True)
        self.store.set_preference("default_provider_id", profile.default_provider_id)
        self.store.set_preference("default_privacy_mode", profile.privacy_mode.value)
        self.store.set_preference("file_change_consent", profile.file_change_consent.value)
        self.store.set_preference("guarded_shell_enabled", profile.guarded_shell_enabled)
        self.store.set_preference("onboarding_profile", profile.model_dump(mode="json"))
        self.vault.append_log(
            title="Onboarding completed",
            body=(
                f"Provider: {profile.default_provider_id}\n\n"
                f"Model: {profile.default_model}\n\n"
                f"Base URL: {profile.base_url or 'provider default'}\n\n"
                f"Auth mode: {profile.auth_mode.value}\n\n"
                f"Privacy mode: {profile.privacy_mode.value}\n\n"
                f"File-change consent: {profile.file_change_consent.value}\n\n"
                f"Guarded shell enabled: {profile.guarded_shell_enabled}\n\n"
                f"Vault: {profile.vault_path}\n\n"
                "Secret configured: "
                f"{'yes' if profile.secret_configured else 'not required or skipped'}\n\n"
                "No secret values were written to the vault."
            ),
        )
