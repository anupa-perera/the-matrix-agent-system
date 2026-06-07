import pytest

from thematrix.security import EnvironmentSecretStore, InMemorySecretStore, Keymaker, SecretStoreError


def test_keymaker_stores_secret_by_reference_only() -> None:
    keymaker = Keymaker(InMemorySecretStore())

    record = keymaker.store_api_key("openrouter", "sk-test")

    assert record.secret_ref == "keyring:provider:openrouter:api_key"
    assert keymaker.resolve_api_key(record.secret_ref) == "sk-test"


def test_environment_store_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THE_MATRIX_OPENAI_API_KEY", "sk-env")
    keymaker = Keymaker(EnvironmentSecretStore())

    assert keymaker.resolve_api_key("env:THE_MATRIX_OPENAI_API_KEY") == "sk-env"
    with pytest.raises(SecretStoreError):
        keymaker.store_api_key("openai", "sk-test")


def test_keymaker_wraps_unexpected_store_errors() -> None:
    class BrokenStore:
        backend_name = "broken"
        can_write = True

        def get_secret(self, secret_ref: str) -> str | None:
            raise RuntimeError("locked")

        def set_secret(self, secret_ref: str, value: str) -> None:
            raise RuntimeError("locked")

        def delete_secret(self, secret_ref: str) -> None:
            raise RuntimeError("locked")

    keymaker = Keymaker(BrokenStore())

    with pytest.raises(SecretStoreError, match="could not save"):
        keymaker.store_api_key("openai", "sk-test")
    with pytest.raises(SecretStoreError, match="could not read"):
        keymaker.resolve_api_key("broken:provider:openai:api_key")
