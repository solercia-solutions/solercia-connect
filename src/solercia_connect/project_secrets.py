"""Secret storage selection: the OS keyring by default, private files only in project mode.

``FileSecretBackend`` implements the ``KeyringBackend`` protocol, so ``KeyringOAuthTokenStore``
and ``KeyringSecretStore`` use it unchanged. Its files hold plaintext secrets, the risk accepted in
docs/decisions/2026-10-25-modo-proyecto-secretos-en-archivo.md; the folder guards of
``project_home`` keep them private and out of git.
"""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from pathlib import Path

from solercia_connect.config import (
    ConnectSettings,
    ProjectHomeError,
    ProjectLayout,
    _lexists,
    _wrong_owner,
)
from solercia_connect.oauth import KeyringOAuthTokenStore
from solercia_connect.project_home import (
    GitRunner,
    check_private_directory,
    prepare_project_home,
    write_private_file,
)
from solercia_connect.secret_store import KeyringSecretStore


class SecretEntryNotFoundError(LookupError):
    """Deleting a missing entry fails, as the OS keyring's ``PasswordDeleteError`` does."""


class FileSecretBackend:
    """``KeyringBackend`` on private plaintext files under ``<home>/secrets``; project mode only.

    Each (service, username) pair is one ``0600`` file named by the SHA-256 of
    ``service\\0username`` and written atomically. Symlinked, foreign-owned, or non-private files
    fail closed and are never repaired. The folder is prepared, and git is checked, before the
    first write of each backend.
    """

    def __init__(self, home: Path, *, runner: GitRunner | None = None) -> None:
        self._layout = ProjectLayout(home)
        self._runner = runner
        self._prepared = False

    def get_password(self, service_name: str, username: str) -> str | None:
        path = self._path(service_name, username)
        if not self._existing_folders() or not _lexists(path):
            return None
        return self._read(path)

    def set_password(self, service_name: str, username: str, password: str) -> None:
        path = self._path(service_name, username)
        if not self._prepared:
            prepare_project_home(self._layout.home, runner=self._runner)
            self._prepared = True
        check_private_directory(self._layout, self._layout.secrets_dir)
        if _lexists(path):
            self._check_file(path)
        try:
            write_private_file(path, password.encode("utf-8"), replace=True)
        except OSError:
            raise ProjectHomeError("A project secret could not be written safely") from None

    def delete_password(self, service_name: str, username: str) -> None:
        path = self._path(service_name, username)
        if not self._existing_folders() or not _lexists(path):
            raise SecretEntryNotFoundError("No project secret is stored under that key")
        self._check_file(path)
        try:
            path.unlink()
        except OSError:
            raise ProjectHomeError("A project secret could not be deleted safely") from None

    def _path(self, service_name: str, username: str) -> Path:
        digest = hashlib.sha256(f"{service_name}\0{username}".encode()).hexdigest()
        return self._layout.secrets_dir / digest

    def _existing_folders(self) -> bool:
        for directory in (self._layout.home, self._layout.secrets_dir):
            if not _lexists(directory):
                return False
            check_private_directory(self._layout, directory)
        return True

    def _read(self, path: Path) -> str:
        before = self._check_file(path)
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            opened = os.fstat(descriptor)
            self._check_metadata(path, opened)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ProjectHomeError("A project secret changed during its safety check")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read()
            return content.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise ProjectHomeError("A project secret could not be read safely") from None
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)

    def _check_file(self, path: Path) -> os.stat_result:
        try:
            metadata = path.lstat()
        except OSError:
            raise ProjectHomeError("A project secret could not be inspected safely") from None
        self._check_metadata(path, metadata)
        return metadata

    def _check_metadata(self, path: Path, metadata: os.stat_result) -> None:
        label = f"SOLERCIA_CONNECT_HOME/secrets/{path.name}"
        if stat.S_ISLNK(metadata.st_mode):
            raise ProjectHomeError(f"{label} is a symbolic link")
        if not stat.S_ISREG(metadata.st_mode):
            raise ProjectHomeError(f"{label} is not a regular file")
        if _wrong_owner(metadata.st_uid):
            raise ProjectHomeError(f"{label} is not owned by the current user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ProjectHomeError(f'{label} is not private; run chmod 600 "${label}"')


def oauth_token_store_from_settings(settings: ConnectSettings) -> KeyringOAuthTokenStore:
    """OAuth state in the OS keyring, or in project files when ``SOLERCIA_CONNECT_HOME`` is set."""

    if settings.project_home is None:
        return KeyringOAuthTokenStore()
    return KeyringOAuthTokenStore(backend=FileSecretBackend(settings.project_home))


def api_token_store_from_settings(settings: ConnectSettings) -> KeyringSecretStore:
    """API tokens in the OS keyring, or in project files when ``SOLERCIA_CONNECT_HOME`` is set."""

    if settings.project_home is None:
        return KeyringSecretStore()
    return KeyringSecretStore(backend=FileSecretBackend(settings.project_home))
