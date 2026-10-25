from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import KeyringBackend
from solercia_connect.secrets import SecretValue

# A keyring service of its own: an API token stored under ``jira:<email>`` can never collide
# with OAuth state, which lives under the same kind of key in the ``solercia-connect`` service.
API_TOKEN_SERVICE = "solercia-connect.api-token"


class SecretStore(Protocol):
    """Port for single provider secrets, such as a Jira API token, keyed by ``type:account``."""

    def load(self, key: str) -> SecretValue | None: ...

    def save(self, key: str, secret: SecretValue) -> None: ...

    def delete(self, key: str) -> bool: ...


class KeyringSecretStore:
    """Keep one plain provider secret in the OS keyring; it never reaches files or envelopes."""

    def __init__(
        self,
        service_name: str = API_TOKEN_SERVICE,
        *,
        backend: KeyringBackend | None = None,
    ) -> None:
        if not service_name.strip():
            raise ValueError("Keyring service name cannot be empty")
        self._service = service_name.strip()
        self._configured_backend = backend

    def load(self, key: str) -> SecretValue | None:
        username = _key(key)
        raw = self._call(lambda backend: backend.get_password(self._service, username))
        return SecretValue(raw) if raw else None

    def save(self, key: str, secret: SecretValue) -> None:
        username = _key(key)
        self._call(lambda backend: backend.set_password(self._service, username, secret.reveal()))

    def delete(self, key: str) -> bool:
        """Remove the secret; deleting a missing one is a no-op, not a keyring error."""

        username = _key(key)
        if self.load(username) is None:
            return False
        self._call(lambda backend: backend.delete_password(self._service, username))
        return True

    def _call[T](self, operation: Callable[[KeyringBackend], T]) -> T:
        backend = self._backend()
        try:
            return operation(backend)
        except Exception:
            # A backend error may quote the secret, so it is dropped instead of chained.
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Secure secret storage is unavailable",
            ) from None

    def _backend(self) -> KeyringBackend:
        if self._configured_backend is not None:
            return self._configured_backend
        try:
            import keyring
        except ImportError as exc:
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Install the oauth extra to use secure secret storage",
            ) from exc
        return cast(KeyringBackend, keyring)


def _key(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("Secret store key cannot be empty")
    return candidate
