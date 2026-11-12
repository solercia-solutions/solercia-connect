"""The separate bitbucket-admin connection: its own token, the B-D2 identity proof (R1).

Decision: docs/decisions/2026-10-25-escritura-creacion-repositorios-bitbucket.md, R-D1. The admin
token lives under bitbucket-admin:<email>, apart from the read token of the bitbucket target.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import solercia_connect.cli as cli
from solercia_connect.browser_routes import has_browser_route
from solercia_connect.config import (
    BITBUCKET_TARGETS,
    ConnectionProfile,
    ConnectionType,
    ProfileStore,
    load_settings,
    target_spec,
)
from solercia_connect.connections import ConnectionService
from solercia_connect.doctor import DoctorService
from solercia_connect.project_home import connect_env_template, prepare_project_home
from solercia_connect.project_secrets import oauth_token_store_from_settings
from solercia_connect.secret_store import API_TOKEN_SERVICE, KeyringSecretStore
from solercia_connect.secrets import SecretValue
from solercia_connect.service import create_app
from tests.fakes import NOW, FakeTransport, json_response
from tests.test_bitbucket_connections import (
    MemoryKeyring,
    UnusedOAuthStore,
    _email,
    _emails,
    _scope_denial,
    _user,
    _workspace,
)

ACCOUNT = "user@example.com"
WORKSPACE = "idartes-tecnologia"
TOKEN = "bitbucket-admin-token-value"
READ_TOKEN = "bitbucket-read-token-value"
ACCOUNT_ID = "557058:acct-1"
API = "https://api.bitbucket.org/2.0"
USER = f"{API}/user"
EMAILS = f"{API}/user/emails"
WORKSPACE_URL = f"{API}/workspaces/{WORKSPACE}"
ADMIN_KEY = (API_TOKEN_SERVICE, f"bitbucket-admin:{ACCOUNT}")
READ_KEY = (API_TOKEN_SERVICE, f"bitbucket:{ACCOUNT}")
BASIC = "Basic " + base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()
CONNECT = f"solercia-connect connect bitbucket-admin {ACCOUNT} --workspace {WORKSPACE}"
ADMIN_SCOPES = (
    "read:user:bitbucket",
    "read:workspace:bitbucket",
    "read:repository:bitbucket",
    "read:project:bitbucket",
    "admin:repository:bitbucket",
)

runner = CliRunner()


def _values(tmp_path: Path) -> dict[str, str]:
    return {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")}


def _service(
    tmp_path: Path, *responses: Any, keyring: MemoryKeyring | None = None
) -> tuple[ConnectionService, ProfileStore, MemoryKeyring, FakeTransport]:
    settings = load_settings(environ=_values(tmp_path))
    profiles = ProfileStore.from_settings(settings)
    backend = keyring if keyring is not None else MemoryKeyring()
    transport = FakeTransport(*responses)
    service = ConnectionService(
        settings,
        profiles,
        UnusedOAuthStore(),
        secret_store=KeyringSecretStore(backend=backend),
        transport=transport,
        clock=lambda: NOW,
    )
    return service, profiles, backend, transport


def _identity(account_id: str = ACCOUNT_ID) -> tuple[Any, Any, Any]:
    return _user(account_id), _emails(_email()), _workspace()


def _connect_admin(service: ConnectionService) -> Any:
    return service.connect_bitbucket(
        ACCOUNT,
        workspace=WORKSPACE,
        token=SecretValue(TOKEN),
        connection_type=ConnectionType.BITBUCKET_ADMIN,
    )


def _connected(tmp_path: Path, *later: Any) -> tuple[Any, ProfileStore, MemoryKeyring, Any]:
    service, profiles, keyring, transport = _service(tmp_path, *_identity(), *later)
    assert _connect_admin(service).ok
    return service, profiles, keyring, transport


# --- Target ---------------------------------------------------------------------------------


def test_the_admin_target_is_separate_official_and_leaves_the_read_target_unchanged() -> None:
    admin = target_spec("bitbucket-admin")
    read = target_spec("bitbucket")

    assert ConnectionType.parse("BITBUCKET_ADMIN") is ConnectionType.BITBUCKET_ADMIN
    assert {ConnectionType.BITBUCKET, ConnectionType.BITBUCKET_ADMIN} == BITBUCKET_TARGETS
    assert admin.provider_id == read.provider_id == "bitbucket.cloud"
    assert admin.required_configuration == () and admin.oauth_scopes == ()
    for scope in ADMIN_SCOPES:
        assert scope in admin.external_prerequisites[0], scope
    assert "admin:repository:bitbucket" not in " ".join(read.external_prerequisites)
    assert read.capabilities == (
        "scm.repositories.list",
        "scm.pullrequests.list",
        "scm.commits.list",
    )
    assert has_browser_route(ConnectionType.BITBUCKET_ADMIN) is False


def test_only_the_bitbucket_targets_carry_a_workspace(tmp_path: Path) -> None:
    profile = ConnectionProfile.create(
        "bitbucket-admin", ACCOUNT, now=NOW, workspace="IDARTES-Tecnologia", provider_account_id="a"
    )
    store = ProfileStore.from_settings(load_settings(environ=_values(tmp_path)))
    store.save(profile)

    assert profile.workspace == WORKSPACE
    assert store.get("bitbucket-admin", ACCOUNT) == profile
    assert store.get("bitbucket", ACCOUNT) is None
    with pytest.raises(ValueError, match="workspace"):
        ConnectionProfile.create("jira", ACCOUNT, now=NOW, workspace=WORKSPACE)


# --- Connect ----------------------------------------------------------------------------------


def test_connect_proves_the_identity_and_keeps_the_admin_token_apart_from_the_read_token(
    tmp_path: Path,
) -> None:
    keyring = MemoryKeyring()
    keyring.values[READ_KEY] = READ_TOKEN
    service, profiles, _, transport = _service(tmp_path, *_identity(), keyring=keyring)

    result = _connect_admin(service)

    assert result.ok is True and result.status == "connected_official"
    assert result.provider == "bitbucket-admin"
    assert result.data["workspace"] == WORKSPACE
    assert result.data["provider_account_id"] == ACCOUNT_ID
    assert [request.url for request in transport.requests] == [USER, EMAILS, WORKSPACE_URL]
    assert all(request.method == "GET" for request in transport.requests)
    assert all(r.headers and r.headers["Authorization"] == BASIC for r in transport.requests)
    assert keyring.values == {READ_KEY: READ_TOKEN, ADMIN_KEY: TOKEN}
    profile = profiles.get("bitbucket-admin", ACCOUNT)
    assert profile is not None and profile.workspace == WORKSPACE
    assert profile.provider_account_id == ACCOUNT_ID
    assert profiles.get("bitbucket", ACCOUNT) is None
    stored = profiles.path_for("bitbucket-admin", ACCOUNT).read_text(encoding="utf-8")
    assert TOKEN not in stored
    assert TOKEN not in json.dumps(result.to_dict())


def test_a_failed_connect_stores_nothing_and_names_the_five_scopes(tmp_path: Path) -> None:
    rejected, profiles, keyring, _ = _service(tmp_path / "401", json_response(401, {}))
    denied, _, _, _ = _service(tmp_path / "403", _scope_denial())

    unauthorized = _connect_admin(rejected)
    forbidden = _connect_admin(denied)

    assert unauthorized.error is not None and unauthorized.error.code == "invalid_credentials"
    assert unauthorized.error.remediation[-1] == CONNECT
    for scope in ADMIN_SCOPES:
        assert scope in unauthorized.error.remediation[0], scope
    assert forbidden.error is not None and forbidden.error.code == "authorization_denied"
    assert "five scopes" in forbidden.error.remediation[0]
    assert forbidden.error.remediation[-1] == CONNECT
    assert keyring.values == {} and profiles.get("bitbucket-admin", ACCOUNT) is None


def test_an_invalid_workspace_is_refused_before_any_request(tmp_path: Path) -> None:
    service, _, keyring, transport = _service(tmp_path)

    result = service.connect_bitbucket(
        ACCOUNT,
        workspace="a/b",
        token=SecretValue(TOKEN),
        connection_type=ConnectionType.BITBUCKET_ADMIN,
    )

    assert result.status == "invalid_input" and result.provider == "bitbucket-admin"
    assert result.error is not None
    assert result.error.remediation == (
        f"solercia-connect connect bitbucket-admin {ACCOUNT} --workspace <workspace>",
    )
    assert transport.requests == [] and keyring.values == {}


def test_connect_bitbucket_refuses_a_target_that_is_not_bitbucket(tmp_path: Path) -> None:
    service, _, keyring, transport = _service(tmp_path)

    with pytest.raises(ValueError, match="bitbucket"):
        service.connect_bitbucket(
            ACCOUNT, workspace=WORKSPACE, token=SecretValue(TOKEN), connection_type="jira"
        )
    assert transport.requests == [] and keyring.values == {}


def test_the_generic_connect_and_the_http_service_point_to_the_cli(tmp_path: Path) -> None:
    service, _, _, transport = _service(tmp_path)
    settings = load_settings(
        environ={**_values(tmp_path), "SOLERCIA_CONNECT_API_KEY": "local-api-key"}
    )
    client = TestClient(create_app(settings=settings, token_store=UnusedOAuthStore()))

    generic = service.connect("bitbucket-admin", ACCOUNT)
    over_http = client.post(
        "/v1/connections/connect",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json={"type": "bitbucket-admin", "account": ACCOUNT},
    )

    expected = f"solercia-connect connect bitbucket-admin {ACCOUNT} --workspace <workspace>"
    assert generic.status == "configuration_error" and generic.provider == "bitbucket-admin"
    assert generic.error is not None and generic.error.remediation == (expected,)
    assert over_http.status_code == 422
    assert over_http.json()["provider"] == "bitbucket-admin"
    assert over_http.json()["error"]["remediation"] == [expected]
    assert transport.requests == []


# --- Status and disconnect ------------------------------------------------------------------


def test_status_repeats_the_proof_against_the_stored_account_id(tmp_path: Path) -> None:
    service, _, _, transport = _connected(
        tmp_path, *_identity(), *_identity(account_id="557058:acct-2")
    )

    healthy = service.status("bitbucket-admin", ACCOUNT)
    changed = service.status("bitbucket-admin", ACCOUNT)

    assert healthy.status == "connected_official" and healthy.provider == "bitbucket-admin"
    assert healthy.data["workspace"] == WORKSPACE
    assert changed.error is not None and changed.error.code == "authorization_boundary_violation"
    assert [request.url for request in transport.requests][3:6] == [USER, EMAILS, WORKSPACE_URL]
    assert all(r.headers and r.headers["Authorization"] == BASIC for r in transport.requests)


def test_status_without_the_admin_token_asks_for_the_admin_connect(tmp_path: Path) -> None:
    service, _, keyring, _ = _connected(tmp_path, json_response(401, {}))

    expired = service.status("bitbucket-admin", ACCOUNT)
    keyring.values.clear()
    missing = service.status("bitbucket-admin", ACCOUNT)

    for result in (expired, missing):
        assert result.status == "action_required"
        assert result.error is not None and result.error.remediation == (CONNECT,)


def test_disconnect_removes_only_the_admin_profile_and_token(tmp_path: Path) -> None:
    keyring = MemoryKeyring()
    service, profiles, _, _ = _service(tmp_path, *_identity(), *_identity(), keyring=keyring)
    assert service.connect_bitbucket(ACCOUNT, workspace=WORKSPACE, token=SecretValue(READ_TOKEN)).ok
    assert _connect_admin(service).ok

    result = service.disconnect("bitbucket-admin", ACCOUNT)

    assert result.ok is True and result.data["local_profile_removed"] is True
    assert profiles.get("bitbucket-admin", ACCOUNT) is None
    assert profiles.get("bitbucket", ACCOUNT) is not None
    assert keyring.values == {READ_KEY: READ_TOKEN}


# --- Doctor and project template ------------------------------------------------------------


def _doctor(tmp_path: Path, *, keyring_ready: bool = True) -> tuple[DoctorService, ProfileStore]:
    settings = load_settings(environ=_values(tmp_path))
    profiles = ProfileStore.from_settings(settings)
    service = DoctorService(
        settings,
        profiles,
        module_available=lambda name: name == "keyring",
        keyring_probe=lambda: (keyring_ready, "test-backend"),
        browser_probe=lambda: pytest.fail("Bitbucket must not probe Playwright"),
    )
    return service, profiles


def test_doctor_reports_the_admin_connection_and_names_its_connect_command(
    tmp_path: Path,
) -> None:
    service, profiles = _doctor(tmp_path / "present")
    profiles.save(
        ConnectionProfile.create(
            "bitbucket-admin", ACCOUNT, now=NOW, workspace=WORKSPACE, provider_account_id="a-1"
        )
    )
    absent_service, _ = _doctor(tmp_path / "absent", keyring_ready=False)

    present = service.inspect("bitbucket-admin", ACCOUNT)
    absent = absent_service.inspect("bitbucket-admin", ACCOUNT)
    checks = {check.name: check for check in present.checks}
    missing = {check.name: check for check in absent.checks}

    assert present.ready is True
    assert checks["bitbucket_admin_connection"].status == "present"
    assert WORKSPACE in checks["bitbucket_admin_connection"].detail
    assert "bitbucket_connection" not in checks
    for scope in ADMIN_SCOPES:
        assert any(scope in item for item in present.external_prerequisites), scope
    assert absent.ready is False
    assert missing["keyring_backend"].required is True
    assert missing["bitbucket_admin_connection"].status == "absent"
    assert missing["bitbucket_admin_connection"].remediation[0] == (
        "solercia-connect connect bitbucket-admin <account> --workspace <workspace> "
        "--token-env BITBUCKET_ADMIN_API_TOKEN"
    )


def test_project_init_offers_the_admin_token_variable() -> None:
    lines = connect_env_template().splitlines()

    assert "# BITBUCKET_ADMIN_API_TOKEN=" in lines
    assert "# BITBUCKET_API_TOKEN=" in lines


# --- CLI and project mode -------------------------------------------------------------------


def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SOLERCIA_CONNECT_ENV_FILE",
        "SOLERCIA_CONNECT_HOME",
        "BITBUCKET_API_TOKEN",
        "BITBUCKET_ADMIN_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def _cli_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *responses: Any
) -> tuple[ProfileStore, MemoryKeyring, FakeTransport]:
    service, profiles, keyring, transport = _service(tmp_path, *responses)
    monkeypatch.setattr(cli, "connection_service_from_environment", lambda **_: service)
    return profiles, keyring, transport


def _connect(*options: str) -> list[str]:
    return ["connect", "bitbucket-admin", ACCOUNT, "--workspace", WORKSPACE, *options]


def test_cli_reads_the_admin_token_from_the_prompt_the_variable_or_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch)
    _, keyring, _ = _cli_service(tmp_path / "prompt", monkeypatch, *_identity())
    prompted = runner.invoke(cli.app, _connect(), input=f"{TOKEN}\n")
    assert json.loads(prompted.stdout)["status"] == "connected_official", prompted.output
    assert "Bitbucket admin API token" in prompted.output
    assert keyring.values == {ADMIN_KEY: TOKEN}

    monkeypatch.setenv("BITBUCKET_ADMIN_API_TOKEN", f"  {TOKEN}  ")
    _, keyring, _ = _cli_service(tmp_path / "env", monkeypatch, *_identity())
    from_env = runner.invoke(cli.app, _connect("--token-env", "BITBUCKET_ADMIN_API_TOKEN"))
    assert json.loads(from_env.stdout)["provider"] == "bitbucket-admin"
    assert keyring.values == {ADMIN_KEY: TOKEN}

    _, keyring, _ = _cli_service(tmp_path / "stdin", monkeypatch, *_identity())
    from_stdin = runner.invoke(cli.app, _connect("--token-stdin"), input=f"{TOKEN}\n")
    assert json.loads(from_stdin.stdout)["status"] == "connected_official"
    assert keyring.values == {ADMIN_KEY: TOKEN}
    for result in (prompted, from_env, from_stdin):
        assert TOKEN not in result.output


def test_cli_refuses_the_jira_and_browser_options_for_the_admin_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch)
    profiles, keyring, transport = _cli_service(tmp_path, monkeypatch)
    cases = {
        "--site does not apply to bitbucket-admin": _connect(
            "--site", "https://example.atlassian.net"
        ),
        "--token-kind does not apply to bitbucket-admin": _connect("--token-kind", "scoped"),
        "--allow-browser-fallback does not apply to bitbucket-admin": _connect(
            "--allow-browser-fallback"
        ),
        "workspace": ["connect", "bitbucket-admin", ACCOUNT, "--workspace", "a/b"],
    }

    for expected, arguments in cases.items():
        payload = json.loads(runner.invoke(cli.app, arguments, input=f"{TOKEN}\n").stdout)
        assert payload["status"] == "invalid_input", expected
        assert expected in payload["error"]["message"], expected
    no_workspace = runner.invoke(cli.app, ["connect", "bitbucket-admin", ACCOUNT])
    assert json.loads(no_workspace.stdout)["status"] == "configuration_error"
    assert transport.requests == []
    assert keyring.values == {} and profiles.get("bitbucket-admin", ACCOUNT) is None


def test_project_mode_reads_the_admin_token_from_connect_env_into_the_project_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    home = project / ".solercia-connect"
    prepare_project_home(home)
    env_file = home / "connect.env"
    env_file.write_text(f"BITBUCKET_ADMIN_API_TOKEN={TOKEN}\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("SOLERCIA_CONNECT_HOME", str(home))
    settings = load_settings()
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        oauth_token_store_from_settings(settings),
        transport=FakeTransport(*_identity()),
        clock=lambda: NOW,
    )
    monkeypatch.setattr(cli, "connection_service_from_environment", lambda **_: service)

    result = runner.invoke(cli.app, _connect("--token-env", "BITBUCKET_ADMIN_API_TOKEN"))

    assert json.loads(result.stdout)["status"] == "connected_official", result.output
    assert TOKEN not in result.output
    secrets = [path for path in (home / "secrets").iterdir() if path.is_file()]
    assert len(secrets) == 1 and secrets[0].read_text(encoding="utf-8") == TOKEN
    profile = ProfileStore.from_settings(settings).get("bitbucket-admin", ACCOUNT)
    assert profile is not None and profile.workspace == WORKSPACE
