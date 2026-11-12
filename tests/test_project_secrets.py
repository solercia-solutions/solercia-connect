"""Project mode stores secrets in private files behind the same KeyringBackend protocol."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

import solercia_connect.cli as cli
import solercia_connect.connections as connections_module
from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.config import ProfileStore, ProjectHomeError, load_settings
from solercia_connect.connections import ConnectionService
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import KeyringOAuthTokenStore, OAuthTokenSet
from solercia_connect.project_home import ProjectHomeTrackedError
from solercia_connect.project_secrets import (
    FileSecretBackend,
    SecretEntryNotFoundError,
    api_token_store_from_settings,
    oauth_token_store_from_settings,
)
from solercia_connect.secret_store import API_TOKEN_SERVICE, KeyringSecretStore
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport, json_response

ACCOUNT = "user@example.com"
SITE = "https://example.atlassian.net"
TOKEN = "jira-api-token-value"
BASIC = "Basic " + base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()

runner = CliRunner()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _no_git(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    raise AssertionError(f"git must not run without a git ancestor: {list(args)}")


def _home(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project / ".solercia-connect"


def _secret_path(home: Path, service: str, username: str) -> Path:
    digest = hashlib.sha256(f"{service}\0{username}".encode()).hexdigest()
    return home / "secrets" / digest


def _tokens() -> OAuthTokenSet:
    return OAuthTokenSet(
        SecretValue("access-value"),
        SecretValue("refresh-value"),
        NOW + timedelta(hours=1),
        ("openid", "email"),
    )


def _files_under(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_oauth_tokens_round_trip_through_the_file_backend(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = KeyringOAuthTokenStore(backend=FileSecretBackend(home, runner=_no_git))

    store.save("gmail:user@example.com", _tokens())
    loaded = store.load("gmail:user@example.com")

    assert loaded is not None
    assert loaded.access_token.reveal() == "access-value"
    assert loaded.refresh_token is not None
    assert loaded.refresh_token.reveal() == "refresh-value"
    assert loaded.scopes == ("openid", "email")
    store.delete("gmail:user@example.com")
    assert store.load("gmail:user@example.com") is None


def test_api_tokens_round_trip_through_the_file_backend(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = KeyringSecretStore(backend=FileSecretBackend(home, runner=_no_git))

    store.save("jira:user@example.com", SecretValue(TOKEN))
    loaded = store.load("jira:user@example.com")

    assert loaded is not None
    assert loaded.reveal() == TOKEN
    assert store.delete("jira:user@example.com") is True
    assert store.delete("jira:user@example.com") is False
    assert store.load("jira:user@example.com") is None


def test_each_secret_is_one_private_file_named_by_a_digest(tmp_path: Path) -> None:
    home = _home(tmp_path)
    backend = FileSecretBackend(home, runner=_no_git)

    backend.set_password(API_TOKEN_SERVICE, "jira:user@example.com", TOKEN)

    path = _secret_path(home, API_TOKEN_SERVICE, "jira:user@example.com")
    assert [item.name for item in _files_under(home / "secrets")] == [path.name]
    assert _mode(path) == 0o600
    assert _mode(home / "secrets") == 0o700
    assert (home / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert "user@example.com" not in path.name


def test_a_write_replaces_the_file_atomically_and_leaves_no_temporary_file(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    backend = FileSecretBackend(home, runner=_no_git)
    backend.set_password("service", "user", "first")
    path = _secret_path(home, "service", "user")
    first_inode = path.stat().st_ino

    backend.set_password("service", "user", "second")

    assert backend.get_password("service", "user") == "second"
    assert path.stat().st_ino != first_inode
    assert [item.name for item in (home / "secrets").iterdir()] == [path.name]


def test_a_missing_entry_reads_as_none_and_deleting_it_fails_like_keyring(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    backend = FileSecretBackend(home, runner=_no_git)

    assert backend.get_password("service", "user") is None
    assert not home.exists()
    with pytest.raises(SecretEntryNotFoundError):
        backend.delete_password("service", "user")

    backend.set_password("service", "other", "value")

    assert backend.get_password("service", "user") is None
    with pytest.raises(SecretEntryNotFoundError):
        backend.delete_password("service", "user")
    with pytest.raises(ConnectorFailure):
        KeyringOAuthTokenStore(backend=backend).delete("gmail:user@example.com")


def test_symlinked_secret_files_are_rejected_on_read_write_and_delete(tmp_path: Path) -> None:
    home = _home(tmp_path)
    backend = FileSecretBackend(home, runner=_no_git)
    backend.set_password("service", "other", "value")
    outside = tmp_path / "outside"
    outside.write_text("planted", encoding="utf-8")
    outside.chmod(0o600)
    _secret_path(home, "service", "user").symlink_to(outside)

    for operation in (
        lambda: backend.get_password("service", "user"),
        lambda: backend.set_password("service", "user", "new"),
        lambda: backend.delete_password("service", "user"),
    ):
        with pytest.raises(ProjectHomeError, match="symbolic link"):
            operation()

    assert outside.read_text(encoding="utf-8") == "planted"


def test_public_or_foreign_owned_secret_files_are_rejected_and_never_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    backend = FileSecretBackend(home, runner=_no_git)
    backend.set_password("service", "user", "value")
    path = _secret_path(home, "service", "user")
    path.chmod(0o644)

    with pytest.raises(ProjectHomeError, match="chmod 600"):
        backend.get_password("service", "user")
    assert _mode(path) == 0o644

    path.chmod(0o600)
    owner = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: owner + 1)
    with pytest.raises(ProjectHomeError, match="owned by the current user"):
        backend.get_password("service", "user")


def test_a_folder_in_place_of_a_secret_file_is_rejected(tmp_path: Path) -> None:
    home = _home(tmp_path)
    backend = FileSecretBackend(home, runner=_no_git)
    backend.set_password("service", "other", "value")
    _secret_path(home, "service", "user").mkdir(mode=0o700)

    with pytest.raises(ProjectHomeError, match="not a regular file"):
        backend.get_password("service", "user")


def test_the_stores_hide_backend_failures_behind_their_safe_messages(tmp_path: Path) -> None:
    home = _home(tmp_path)
    backend = FileSecretBackend(home, runner=_no_git)
    backend.set_password(API_TOKEN_SERVICE, "jira:user@example.com", TOKEN)
    _secret_path(home, API_TOKEN_SERVICE, "jira:user@example.com").chmod(0o640)

    with pytest.raises(ConnectorFailure) as captured:
        KeyringSecretStore(backend=backend).load("jira:user@example.com")

    assert TOKEN not in str(captured.value)


def test_the_git_guard_runs_once_before_the_first_write(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 0, b"", b"")

    backend = FileSecretBackend(project / ".solercia-connect", runner=runner)
    assert backend.get_password("service", "user") is None
    assert calls == []

    backend.set_password("service", "user", "first")
    backend.set_password("service", "user", "second")

    assert len(calls) == 1


def test_a_tracked_home_blocks_the_first_secret_write(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(list(args), 0, b".solercia-connect/x\0", b"")

    home = project / ".solercia-connect"
    backend = FileSecretBackend(home, runner=runner)

    with pytest.raises(ProjectHomeTrackedError):
        backend.set_password("service", "user", "value")

    assert _files_under(home / "secrets") == []


def test_factories_keep_the_os_keyring_without_project_home(tmp_path: Path) -> None:
    import keyring

    settings = load_settings(environ={"XDG_CONFIG_HOME": str(tmp_path / "config")})

    oauth_store = oauth_token_store_from_settings(settings)
    api_store = api_token_store_from_settings(settings)

    assert isinstance(oauth_store, KeyringOAuthTokenStore)
    assert isinstance(api_store, KeyringSecretStore)
    assert oauth_store._backend() is keyring
    assert api_store._backend() is keyring


def test_factories_inject_the_file_backend_in_project_mode(tmp_path: Path) -> None:
    settings = load_settings(environ={"SOLERCIA_CONNECT_HOME": str(_home(tmp_path))})

    assert isinstance(oauth_token_store_from_settings(settings)._backend(), FileSecretBackend)
    assert isinstance(api_token_store_from_settings(settings)._backend(), FileSecretBackend)


def _project_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = _home(tmp_path)
    monkeypatch.delenv("SOLERCIA_CONNECT_ENV_FILE", raising=False)
    monkeypatch.setenv("SOLERCIA_CONNECT_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    return home


def test_cli_jira_connect_in_project_mode_writes_only_under_the_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _project_environment(tmp_path, monkeypatch)
    transport = FakeTransport(json_response(200, {"accountId": "acct-1", "emailAddress": ACCOUNT}))
    monkeypatch.setattr(connections_module, "HttpxTransport", lambda: transport)

    result = runner.invoke(
        cli.app,
        ["connect", "jira", ACCOUNT, "--site", SITE, "--token-stdin"],
        input=f"{TOKEN}\n",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "connected_official"
    assert TOKEN not in result.output
    assert transport.requests[0].headers is not None
    assert transport.requests[0].headers["Authorization"] == BASIC
    written = _files_under(tmp_path)
    assert written and all(path.is_relative_to(home) for path in written)
    secret = _secret_path(home, API_TOKEN_SERVICE, f"jira:{ACCOUNT}")
    assert secret.read_text(encoding="utf-8") == TOKEN
    assert [path for path in written if TOKEN in path.read_text(encoding="utf-8")] == [secret]

    settings = load_settings()
    registry = registry_from_environment(
        connection_type="jira",
        account=ACCOUNT,
        transport=FakeTransport(json_response(200, {"accountId": "acct-1"})),
    )
    assert registry.find("jira.cloud").health().account_id == "acct-1"
    assert settings.project_home == home


def test_google_oauth_connect_in_project_mode_keeps_the_grant_in_a_file(tmp_path: Path) -> None:
    home = _home(tmp_path)
    settings = load_settings(
        environ={
            "SOLERCIA_CONNECT_HOME": str(home),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
        }
    )
    transport = FakeTransport(
        json_response(
            200,
            {
                "access_token": "google-access",
                "refresh_token": "google-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
            },
        ),
        json_response(200, {"emailAddress": ACCOUNT}),
    )

    def callback(oauth: object, open_browser: bool) -> None:
        oauth.exchange_authorization_code(  # type: ignore[attr-defined]
            SecretValue("authorization-code"),
            redirect_uri="http://127.0.0.1:8765/oauth/callback",
            code_verifier=SecretValue("pkce-verifier"),
        )

    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        oauth_token_store_from_settings(settings),
        google_callback_flow=callback,
        transport=transport,
        clock=lambda: NOW,
    )

    result = service.connect("gmail", ACCOUNT, open_browser=False)

    assert result.status == "connected_official"
    assert transport.requests[1].headers is not None
    assert transport.requests[1].headers["Authorization"] == "Bearer google-access"
    secret = _secret_path(home, "solercia-connect", f"gmail:{ACCOUNT}")
    assert json.loads(secret.read_text(encoding="utf-8"))["refresh_token"] == "google-refresh"
    assert _mode(secret) == 0o600
    assert all(path.is_relative_to(home) for path in _files_under(tmp_path))
