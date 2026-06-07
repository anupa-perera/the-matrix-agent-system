from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


class SecretStoreError(RuntimeError):
    """Raised when Keymaker cannot safely read or write a secret."""


class SecretStore(Protocol):
    backend_name: str
    can_write: bool

    def get_secret(self, secret_ref: str) -> str | None: ...

    def set_secret(self, secret_ref: str, value: str) -> None: ...

    def delete_secret(self, secret_ref: str) -> None: ...


@dataclass(frozen=True)
class SecretRecord:
    secret_ref: str
    backend_name: str


class KeyringSecretStore:
    """OS credential store backend, loaded only when the optional package exists."""

    backend_name = "keyring"
    can_write = True

    def __init__(self, service_name: str = "the-matrix-agent-system"):
        self.service_name = service_name
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SecretStoreError(
                "The keyring package is not installed. "
                "Install the project dependencies to store API keys securely."
            ) from exc
        self._keyring = keyring

    def get_secret(self, secret_ref: str) -> str | None:
        username = _strip_ref_prefix(secret_ref, "keyring")
        try:
            return self._keyring.get_password(self.service_name, username)
        except Exception as exc:
            raise SecretStoreError(f"Keyring could not read `{username}`: {exc}") from exc

    def set_secret(self, secret_ref: str, value: str) -> None:
        username = _strip_ref_prefix(secret_ref, "keyring")
        try:
            self._keyring.set_password(self.service_name, username, value)
        except Exception as exc:
            raise SecretStoreError(f"Keyring could not store `{username}`: {exc}") from exc

    def delete_secret(self, secret_ref: str) -> None:
        username = _strip_ref_prefix(secret_ref, "keyring")
        try:
            self._keyring.delete_password(self.service_name, username)
        except self._keyring.errors.PasswordDeleteError:
            return
        except Exception as exc:
            raise SecretStoreError(f"Keyring could not delete `{username}`: {exc}") from exc


class EnvironmentSecretStore:
    """Read-only fallback for users who prefer environment variables."""

    backend_name = "environment"
    can_write = False

    def get_secret(self, secret_ref: str) -> str | None:
        env_name = _strip_ref_prefix(secret_ref, "env")
        return os.getenv(env_name)

    def set_secret(self, secret_ref: str, value: str) -> None:
        raise SecretStoreError(
            "Environment variables are read-only for Keymaker. "
            "Set the variable in your shell or install the secrets extra."
        )

    def delete_secret(self, secret_ref: str) -> None:
        raise SecretStoreError("Keymaker cannot delete environment variables.")


class InMemorySecretStore:
    """Test backend. It is intentionally not selected by default."""

    backend_name = "memory"
    can_write = True

    def __init__(self):
        self._values: dict[str, str] = {}

    def get_secret(self, secret_ref: str) -> str | None:
        return self._values.get(secret_ref)

    def set_secret(self, secret_ref: str, value: str) -> None:
        self._values[secret_ref] = value

    def delete_secret(self, secret_ref: str) -> None:
        self._values.pop(secret_ref, None)


class Keymaker:
    """Stable internal boundary for provider credentials."""

    def __init__(self, store: SecretStore | None = None):
        self.store = store or self.default_store()

    @classmethod
    def default_store(cls) -> SecretStore:
        try:
            return KeyringSecretStore()
        except SecretStoreError:
            return EnvironmentSecretStore()

    @property
    def backend_name(self) -> str:
        return self.store.backend_name

    @property
    def can_write(self) -> bool:
        return self.store.can_write

    def api_key_ref(self, provider_id: str) -> str:
        if self.store.backend_name == "environment":
            return f"env:{self.env_var_name(provider_id)}"
        return f"keyring:provider:{provider_id}:api_key"

    def store_api_key(self, provider_id: str, api_key: str) -> SecretRecord:
        if not api_key.strip():
            raise SecretStoreError("API key cannot be empty.")
        secret_ref = self.api_key_ref(provider_id)
        try:
            self.store.set_secret(secret_ref, api_key.strip())
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(f"Secret store could not save the API key: {exc}") from exc
        return SecretRecord(secret_ref=secret_ref, backend_name=self.store.backend_name)

    def resolve_api_key(self, secret_ref: str | None) -> str | None:
        if secret_ref is None:
            return None
        try:
            return self.store.get_secret(secret_ref)
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(f"Secret store could not read the API key: {exc}") from exc

    @staticmethod
    def env_var_name(provider_id: str) -> str:
        normalized = provider_id.upper().replace("-", "_")
        return f"THE_MATRIX_{normalized}_API_KEY"


def _strip_ref_prefix(secret_ref: str, expected_prefix: str) -> str:
    prefix = f"{expected_prefix}:"
    if not secret_ref.startswith(prefix):
        raise SecretStoreError(f"Secret reference must start with {prefix!r}.")
    return secret_ref[len(prefix) :]
