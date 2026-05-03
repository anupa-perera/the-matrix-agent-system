from thematrix.memory import RuntimeStore
from thematrix.providers import provider_catalog
from thematrix.schemas import AuthMode, FileChangeConsent, ProviderConfig


def test_provider_config_stores_metadata_without_secret_value(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()
    for profile in provider_catalog():
        store.upsert_provider(profile)

    config = ProviderConfig(
        provider_id="openrouter",
        selected_model="openai/gpt-5-mini",
        auth_mode=AuthMode.API_KEY,
        secret_ref="keyring:provider:openrouter:api_key",
        base_url="https://openrouter.ai/api/v1",
        file_change_consent=FileChangeConsent.ASK_EACH_TIME,
    )

    store.configure_provider(config)
    current = store.get_default_provider_config()

    assert current is not None
    assert current.provider_id == "openrouter"
    assert current.selected_model == "openai/gpt-5-mini"
    assert current.base_url == "https://openrouter.ai/api/v1"
    assert current.secret_ref == "keyring:provider:openrouter:api_key"
    assert "sk-" not in current.model_dump_json()
