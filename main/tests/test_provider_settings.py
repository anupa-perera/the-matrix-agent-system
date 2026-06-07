from thematrix.memory import RuntimeStore
from thematrix.providers import provider_catalog
from thematrix.schemas import AuthMode, FileChangeConsent, ProviderConfig, ReasoningEffort


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
        reasoning_effort=ReasoningEffort.HIGH,
        file_change_consent=FileChangeConsent.ASK_EACH_TIME,
    )

    store.configure_provider(config)
    current = store.get_default_provider_config()

    assert current is not None
    assert current.provider_id == "openrouter"
    assert current.selected_model == "openai/gpt-5-mini"
    assert current.base_url == "https://openrouter.ai/api/v1"
    assert current.reasoning_effort == ReasoningEffort.HIGH
    assert current.secret_ref == "keyring:provider:openrouter:api_key"
    assert "sk-" not in current.model_dump_json()


def test_runtime_store_uses_wal_and_busy_timeout(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()

    with store.connect() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode == "wal"
    assert busy_timeout == 5000


def test_configure_provider_default_change_is_atomic(tmp_path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.initialize()

    first = ProviderConfig(
        provider_id="ollama",
        selected_model="llama3.2",
        auth_mode=AuthMode.NONE,
    )
    second = ProviderConfig(
        provider_id="openrouter",
        selected_model="openai/gpt-5-mini",
        auth_mode=AuthMode.API_KEY,
        secret_ref="keyring:provider:openrouter:api_key",
    )

    store.configure_provider(first)
    store.configure_provider(second)

    configs = store.list_provider_configs()
    assert [config.provider_id for config in configs if config.is_default] == ["openrouter"]
