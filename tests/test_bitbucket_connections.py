"""Bitbucket Cloud per-account connection: B-D1 storage and the B-D2 identity proof (B1)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import solercia_connect.cli as cli
from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.browser_routes import has_browser_route, target_family
from solercia_connect.config import (
    ConnectionProfile,
    ConnectionType,
    ProfileStore,
    load_settings,
    normalize_bitbucket_workspace,
    target_spec,
)
from solercia_connect.connections import ConnectionService
from solercia_connect.domain import ProviderFamily, ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import OAuthTokenSet
from solercia_connect.project_home import prepare_project_home
from solercia_connect.project_secrets import oauth_token_store_from_settings
from solercia_connect.providers.bitbucket import BITBUCKET_API_BASE_URL, BitbucketAdapter
from solercia_connect.secret_store import API_TOKEN_SERVICE, KeyringSecretStore
from solercia_connect.secrets import SecretValue
from solercia_connect.service import create_app
from tests.fakes import NOW, FakeTransport, json_response

ACCOUNT = "user@example.com"
WORKSPACE = "idartes-tecnologia"
TOKEN = "bitbucket-api-token-value"
ACCOUNT_ID = "557058:acct-1"
API = "https://api.bitbucket.org/2.0"
USER = f"{API}/user"
EMAILS = f"{API}/user/emails"
WORKSPACE_URL = f"{API}/workspaces/{WORKSPACE}"
KEY = (API_TOKEN_SERVICE, f"bitbucket:{ACCOUNT}")
BASIC = "Basic " + base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()
CONNECT = f"solercia-connect connect bitbucket {ACCOUNT} --workspace {WORKSPACE}"

runner = CliRunner()


class MemoryKeyring:
    """In-memory stand-in for the OS keyring; deleting a missing entry fails like keyring does."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        if (service_name, username) not in self.values:
            raise RuntimeError("password not found")
        del self.values[(service_name, username)]


class BrokenKeyring(MemoryKeyring):
    def set_password(self, service_name: str, username: str, password: str) -> None:
        raise RuntimeError(f"backend refused {password}")


class UnusedOAuthStore:
    """Bitbucket API tokens must never reach OAuth token state."""

    def load(self, key: str) -> OAuthTokenSet | None:
        raise AssertionError(f"OAuth token store must not be read for {key}")

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        raise AssertionError(f"OAuth token store must not be written for {key}")

    def delete(self, key: str) -> None:
        raise AssertionError(f"OAuth token store must not be deleted for {key}")


def _values(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        **extra,
    }


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


def _user(account_id: Any = ACCOUNT_ID) -> Any:
    payload: dict[str, Any] = {"display_name": "User", "uuid": "{u-1}", "nickname": "user"}
    if account_id is not None:
        payload["account_id"] = account_id
    return json_response(200, payload)


def _email(email: Any = ACCOUNT, confirmed: Any = True) -> dict[str, Any]:
    return {"type": "email", "email": email, "is_primary": True, "is_confirmed": confirmed}


def _emails(*entries: dict[str, Any], next_url: str | None = None) -> Any:
    payload: dict[str, Any] = {"pagelen": 10, "values": list(entries)}
    if next_url is not None:
        payload["next"] = next_url
    return json_response(200, payload)


def _workspace() -> Any:
    return json_response(200, {"type": "workspace", "slug": WORKSPACE, "uuid": "{w-1}"})


def _identity(account_id: str = ACCOUNT_ID) -> tuple[Any, Any, Any]:
    return _user(account_id), _emails(_email()), _workspace()


def _connected(tmp_path: Path, *later: Any) -> tuple[Any, ProfileStore, MemoryKeyring, Any]:
    service, profiles, keyring, transport = _service(tmp_path, *_identity(), *later)
    assert service.connect_bitbucket(ACCOUNT, workspace=WORKSPACE, token=SecretValue(TOKEN)).ok
    return service, profiles, keyring, transport


def _scope_denial() -> Any:
    return json_response(
        403,
        {
            "type": "error",
            "error": {
                "message": "Your credentials lack one or more required privilege scopes.",
                "detail": {
                    "required": ["read:workspace:bitbucket", "<script>alert(1)</script>"],
                    "granted": ["read:user:bitbucket"],
                },
            },
        },
    )


# --- Workspace slug, target and profile ------------------------------------------------------


def test_a_workspace_slug_is_normalized_and_anything_that_could_reshape_a_path_is_refused() -> None:
    assert normalize_bitbucket_workspace(" IDARTES-Tecnologia ") == WORKSPACE
    assert normalize_bitbucket_workspace("team_1") == "team_1"
    assert normalize_bitbucket_workspace("a" * 62) == "a" * 62
    for invalid in ("", "   ", "-team", "_team", "a/b", "a b", "../x", "a" * 63, "ws?x", "añb"):
        with pytest.raises(ValueError, match="workspace"):
            normalize_bitbucket_workspace(invalid)


def test_the_bitbucket_target_is_read_only_official_and_needs_no_environment() -> None:
    spec = target_spec("bitbucket")

    assert spec.type is ConnectionType.BITBUCKET
    assert spec.provider_id == "bitbucket.cloud"
    assert spec.required_configuration == ()
    assert spec.oauth_scopes == ()
    assert target_family(ConnectionType.BITBUCKET) is ProviderFamily.BITBUCKET
    assert has_browser_route(ConnectionType.BITBUCKET) is False
    assert BITBUCKET_API_BASE_URL == API
    assert BitbucketAdapter.descriptor.family is ProviderFamily.BITBUCKET


def test_the_profile_keeps_the_workspace_only_when_set_so_older_files_still_load(
    tmp_path: Path,
) -> None:
    store = ProfileStore.from_settings(load_settings(environ=_values(tmp_path)))
    legacy = ConnectionProfile.create("jira", ACCOUNT, now=NOW, site="https://x.atlassian.net")
    store.save(legacy)
    raw = json.loads(store.path_for("jira", ACCOUNT).read_text(encoding="utf-8"))

    assert "workspace" not in raw
    assert store.get("jira", ACCOUNT) == legacy

    bitbucket = ConnectionProfile.create(
        "bitbucket", ACCOUNT, now=NOW, workspace="IDARTES-Tecnologia", provider_account_id="a-1"
    )
    assert bitbucket.workspace == WORKSPACE
    assert bitbucket.to_dict()["workspace"] == WORKSPACE
    assert ConnectionProfile.from_dict(bitbucket.to_dict()) == bitbucket
    for value in ("IDARTES-Tecnologia", "a/b", 7):
        with pytest.raises(ValueError):
            ConnectionProfile.from_dict({**bitbucket.to_dict(), "workspace": value})
    with pytest.raises(ValueError, match="workspace"):
        ConnectionProfile.create("jira", ACCOUNT, now=NOW, workspace=WORKSPACE)
    with pytest.raises(ValueError):
        ConnectionProfile.from_dict({**legacy.to_dict(), "workspace": WORKSPACE})


# --- Connect --------------------------------------------------------------------------------


def test_connect_proves_the_identity_in_three_steps_and_keeps_the_token_in_the_keyring_only(
    tmp_path: Path,
) -> None:
    service, profiles, keyring, transport = _service(
        tmp_path,
        _user(),
        _emails(_email("other@example.com"), _email("User@Example.com")),
        _workspace(),
    )

    result = service.connect_bitbucket(
        "User@Example.com", workspace="IDARTES-Tecnologia", token=SecretValue(TOKEN)
    )

    assert result.ok is True
    assert result.status == "connected_official"
    assert result.route == "official"
    assert result.data["provider_id"] == "bitbucket.cloud"
    assert result.data["workspace"] == WORKSPACE
    assert result.data["provider_account_id"] == ACCOUNT_ID
    assert result.data["identity_verified"] is True
    assert [request.url for request in transport.requests] == [USER, EMAILS, WORKSPACE_URL]
    assert all(r.method == "GET" for r in transport.requests)
    assert all(r.headers and r.headers["Authorization"] == BASIC for r in transport.requests)
    profile = profiles.get("bitbucket", ACCOUNT)
    assert profile is not None and profile.route == "official"
    assert profile.workspace == WORKSPACE and profile.provider_account_id == ACCOUNT_ID
    assert keyring.values == {KEY: TOKEN}
    assert TOKEN not in profiles.path_for("bitbucket", ACCOUNT).read_text(encoding="utf-8")
    assert TOKEN not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    ("responses", "code"),
    [
        ((json_response(401, {"type": "error"}),), "invalid_credentials"),
        ((_user(""),), "malformed_provider_data"),
        ((_user(None),), "malformed_provider_data"),
        ((json_response(200, []),), "malformed_provider_data"),
        ((_user(), _emails(_email("other@example.com"))), "authorization_boundary_violation"),
        ((_user(), _emails()), "authorization_boundary_violation"),
        ((_user(), _emails(_email(confirmed=False))), "authorization_boundary_violation"),
        ((_user(), _emails(_email(confirmed="yes"))), "malformed_provider_data"),
        ((_user(), _emails(_email(email=None))), "malformed_provider_data"),
        ((_user(), json_response(200, {"values": "nope"})), "malformed_provider_data"),
        ((_user(), json_response(200, {"size": 1})), "malformed_provider_data"),
        ((_user(), _emails(_email()), json_response(404, {})), "workspace_not_found"),
        ((_user(), _emails(_email()), json_response(403, {})), "authorization_denied"),
        ((_user(), _scope_denial()), "authorization_denied"),
        ((json_response(429, {}),), "rate_limited"),
    ],
)
def test_connect_fails_closed_and_stores_nothing(
    tmp_path: Path, responses: tuple[Any, ...], code: str
) -> None:
    service, profiles, keyring, _ = _service(tmp_path, *responses)

    result = service.connect_bitbucket(ACCOUNT, workspace=WORKSPACE, token=SecretValue(TOKEN))

    assert result.ok is False
    assert result.error is not None and result.error.code == code
    assert profiles.get("bitbucket", ACCOUNT) is None
    assert keyring.values == {}
    assert TOKEN not in json.dumps(result.to_dict())
    if code in {"invalid_credentials", "authorization_denied", "workspace_not_found"}:
        assert result.error.remediation[-1] == CONNECT


def test_a_rejected_token_explains_that_bitbucket_needs_its_own_scoped_token(
    tmp_path: Path,
) -> None:
    service, _, _, _ = _service(tmp_path, json_response(401, {"error": {"message": TOKEN}}))

    result = service.connect_bitbucket(ACCOUNT, workspace=WORKSPACE, token=SecretValue(TOKEN))

    assert result.error is not None and result.error.code == "invalid_credentials"
    guidance = " ".join((result.error.message, *result.error.remediation))
    assert "API token with scopes" in guidance
    assert "Jira" in guidance
    for scope in (
        "read:user:bitbucket",
        "read:workspace:bitbucket",
        "read:repository:bitbucket",
        "read:pullrequest:bitbucket",
    ):
        assert scope in guidance
    assert TOKEN not in json.dumps(result.to_dict())


def test_a_scope_denial_names_only_the_scope_names_the_body_provides(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path, _user(), _scope_denial())

    result = service.connect_bitbucket(ACCOUNT, workspace=WORKSPACE, token=SecretValue(TOKEN))

    assert result.error is not None and result.error.code == "authorization_denied"
    assert "read:workspace:bitbucket" in result.error.message
    assert "read:user:bitbucket" not in result.error.message
    assert "<script>" not in result.error.message
    assert "privilege scopes" not in result.error.message


def test_the_email_list_follows_next_on_the_same_collection_only(tmp_path: Path) -> None:
    second = f"{EMAILS}?page=2"
    service, _, keyring, transport = _service(
        tmp_path,
        _user(),
        _emails(_email("other@example.com"), next_url=second),
        _emails(_email()),
        _workspace(),
    )

    result = service.connect_bitbucket(ACCOUNT, workspace=WORKSPACE, token=SecretValue(TOKEN))

    assert result.status == "connected_official"
    assert [request.url for request in transport.requests] == [USER, EMAILS, second, WORKSPACE_URL]
    assert transport.requests[2].params is None
    assert keyring.values == {KEY: TOKEN}


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example.com/2.0/user/emails?page=2",
        "http://api.bitbucket.org/2.0/user/emails?page=2",
        f"{API}/repositories/{WORKSPACE}?page=2",
        7,
    ],
)
def test_a_next_link_off_the_collection_fails_closed_before_the_token_travels(
    tmp_path: Path, next_url: Any
) -> None:
    payload = {"values": [_email("other@example.com")], "next": next_url}
    service, profiles, keyring, transport = _service(tmp_path, _user(), json_response(200, payload))

    result = service.connect_bitbucket(ACCOUNT, workspace=WORKSPACE, token=SecretValue(TOKEN))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code in {"authorization_boundary_violation", "malformed_provider_data"}
    assert [request.url for request in transport.requests] == [USER, EMAILS]
    assert profiles.get("bitbucket", ACCOUNT) is None and keyring.values == {}


def test_the_email_list_has_a_fixed_page_bound(tmp_path: Path) -> None:
    pages = [
        _emails(_email("other@example.com"), next_url=f"{EMAILS}?page={index + 2}")
        for index in range(5)
    ]
    service, _, keyring, transport = _service(tmp_path, _user(), *pages)

    result = service.connect_bitbucket(ACCOUNT, workspace=WORKSPACE, token=SecretValue(TOKEN))

    assert result.error is not None and result.error.code == "malformed_provider_data"
    assert len(transport.requests) == 6
    assert keyring.values == {}


def test_invalid_input_is_refused_before_any_request(tmp_path: Path) -> None:
    service, profiles, keyring, transport = _service(tmp_path)

    bad_workspace = service.connect_bitbucket(ACCOUNT, workspace="a/b", token=SecretValue(TOKEN))
    bad_account = service.connect_bitbucket(
        "not-an-account", workspace=WORKSPACE, token=SecretValue(TOKEN)
    )

    assert bad_workspace.status == "invalid_input"
    assert bad_workspace.error is not None and "workspace" in bad_workspace.error.message
    assert bad_account.status == "invalid_input"
    assert transport.requests == []
    assert profiles.get("bitbucket", ACCOUNT) is None and keyring.values == {}


def test_a_profile_write_failure_rolls_back_the_stored_token(tmp_path: Path) -> None:
    service, profiles, keyring, _ = _service(tmp_path, *_identity())
    (profiles.root / "bitbucket").mkdir(parents=True, mode=0o755)

    result = service.connect_bitbucket(ACCOUNT, workspace=WORKSPACE, token=SecretValue(TOKEN))

    assert result.error is not None and result.error.code == "configuration_invalid"
    assert keyring.values == {}
    assert TOKEN not in json.dumps(result.to_dict())


def test_a_secret_save_failure_leaves_no_profile(tmp_path: Path) -> None:
    service, profiles, _, _ = _service(tmp_path, *_identity(), keyring=BrokenKeyring())

    result = service.connect_bitbucket(ACCOUNT, workspace=WORKSPACE, token=SecretValue(TOKEN))

    assert result.error is not None and result.error.code == "configuration_invalid"
    assert profiles.get("bitbucket", ACCOUNT) is None
    assert TOKEN not in json.dumps(result.to_dict())


def test_the_generic_connect_and_the_http_service_point_to_the_cli(tmp_path: Path) -> None:
    service, _, _, transport = _service(tmp_path)
    settings = load_settings(environ=_values(tmp_path, SOLERCIA_CONNECT_API_KEY="local-api-key"))
    client = TestClient(create_app(settings=settings, token_store=UnusedOAuthStore()))

    headers = {"X-Solercia-Connect-Key": "local-api-key"}

    generic = service.connect("bitbucket", ACCOUNT)
    with_token = client.post(
        "/v1/connections/connect",
        headers=headers,
        json={"type": "bitbucket", "account": ACCOUNT, "api_token": TOKEN},
    )
    over_http = client.post(
        "/v1/connections/connect",
        headers=headers,
        json={"type": "bitbucket", "account": ACCOUNT},
    )

    expected = f"solercia-connect connect bitbucket {ACCOUNT} --workspace <workspace>"
    assert generic.status == "configuration_error"
    assert generic.error is not None and generic.error.remediation == (expected,)
    assert with_token.status_code == 422
    assert TOKEN not in with_token.text
    assert over_http.status_code == 422
    assert over_http.json()["status"] == "configuration_error"
    assert over_http.json()["error"]["remediation"] == [expected]
    assert transport.requests == []


# --- Status and disconnect ------------------------------------------------------------------


def test_status_repeats_the_proof_against_the_stored_account_id(tmp_path: Path) -> None:
    service, _, _, transport = _connected(
        tmp_path, *_identity(), *_identity(account_id="557058:acct-2")
    )

    healthy = service.status("bitbucket", ACCOUNT)
    changed = service.status("bitbucket", ACCOUNT)

    assert healthy.status == "connected_official"
    assert healthy.data["identity_verified"] is True
    assert healthy.data["workspace"] == WORKSPACE
    assert healthy.data["provider_account_id"] == ACCOUNT_ID
    assert changed.ok is False
    assert changed.error is not None and changed.error.code == "authorization_boundary_violation"
    assert [request.url for request in transport.requests][3:6] == [USER, EMAILS, WORKSPACE_URL]
    assert all(r.headers and r.headers["Authorization"] == BASIC for r in transport.requests)


def test_status_fails_when_the_workspace_is_no_longer_accessible(tmp_path: Path) -> None:
    service, _, _, _ = _connected(tmp_path, _user(), _emails(_email()), json_response(404, {}))

    result = service.status("bitbucket", ACCOUNT)

    assert result.error is not None and result.error.code == "workspace_not_found"
    assert result.error.remediation[-1] == CONNECT


def test_status_without_profile_or_secret_requires_the_exact_connect_command(
    tmp_path: Path,
) -> None:
    service, _, keyring, transport = _connected(tmp_path)

    keyring.values.clear()
    missing_secret = service.status("bitbucket", ACCOUNT)
    missing_profile = service.status("bitbucket", "other@example.com")

    assert missing_secret.status == "action_required"
    assert missing_secret.error is not None and missing_secret.error.remediation == (CONNECT,)
    assert missing_profile.status == "action_required"
    assert missing_profile.error is not None
    assert missing_profile.error.remediation == (
        "solercia-connect connect bitbucket other@example.com --workspace <workspace>",
    )
    assert len(transport.requests) == 3


def test_status_with_an_expired_token_asks_to_reconnect_the_same_workspace(
    tmp_path: Path,
) -> None:
    service, _, _, _ = _connected(tmp_path, json_response(401, {}))

    result = service.status("bitbucket", ACCOUNT)

    assert result.status == "action_required"
    assert result.error is not None and result.error.remediation == (CONNECT,)


def test_disconnect_deletes_the_profile_and_only_the_bitbucket_secret(tmp_path: Path) -> None:
    service, profiles, keyring, _ = _connected(tmp_path)
    jira_key = (API_TOKEN_SERVICE, f"jira:{ACCOUNT}")
    keyring.values[jira_key] = "jira-token"

    result = service.disconnect("bitbucket", ACCOUNT)
    again = service.disconnect("bitbucket", ACCOUNT)

    assert result.ok is True
    assert result.data["local_profile_removed"] is True
    assert result.data["local_token_removed"] is True
    assert profiles.get("bitbucket", ACCOUNT) is None
    assert keyring.values == {jira_key: "jira-token"}
    assert again.ok is True and again.data["local_profile_removed"] is False


# --- Runs: the adapter is built from the account's own connection ---------------------------


def test_a_run_builds_the_adapter_from_the_profile_and_its_stored_token(tmp_path: Path) -> None:
    _, _, keyring, _ = _connected(tmp_path)
    transport = FakeTransport(*_identity(), *_identity(account_id="557058:acct-2"))

    registry = registry_from_environment(
        connection_type="bitbucket",
        account=ACCOUNT,
        environ=_values(tmp_path),
        token_store=UnusedOAuthStore(),
        secret_store=KeyringSecretStore(backend=keyring),
        transport=transport,
    )
    report = registry.find("bitbucket.cloud").health()

    assert report.account_id == ACCOUNT_ID
    assert [request.url for request in transport.requests] == [USER, EMAILS, WORKSPACE_URL]
    assert all(r.headers and r.headers["Authorization"] == BASIC for r in transport.requests)
    with pytest.raises(ConnectorFailure) as captured:
        registry.find("bitbucket.cloud").health()
    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION


def test_a_run_needs_an_account_with_a_profile_and_a_stored_token(tmp_path: Path) -> None:
    _, _, keyring, _ = _connected(tmp_path)
    common: dict[str, Any] = {
        "connection_type": "bitbucket",
        "environ": _values(tmp_path),
        "token_store": UnusedOAuthStore(),
        "transport": FakeTransport(),
    }

    with pytest.raises(ValueError, match="account"):
        registry_from_environment(secret_store=KeyringSecretStore(backend=keyring), **common)
    with pytest.raises(ValueError, match="solercia-connect connect bitbucket"):
        registry_from_environment(
            account="other@example.com", secret_store=KeyringSecretStore(backend=keyring), **common
        )
    keyring.values.clear()
    with pytest.raises(ConnectorFailure) as captured:
        registry_from_environment(
            account=ACCOUNT, secret_store=KeyringSecretStore(backend=keyring), **common
        )
    assert captured.value.reason is ReasonCode.INVALID_CREDENTIALS


# --- CLI ------------------------------------------------------------------------------------


def _cli_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *responses: Any
) -> tuple[ProfileStore, MemoryKeyring, FakeTransport]:
    service, profiles, keyring, transport = _service(tmp_path, *responses)
    monkeypatch.setattr(cli, "connection_service_from_environment", lambda **_: service)
    return profiles, keyring, transport


def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SOLERCIA_CONNECT_ENV_FILE", "SOLERCIA_CONNECT_HOME", "BITBUCKET_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _connect(*options: str) -> list[str]:
    return ["connect", "bitbucket", ACCOUNT, "--workspace", WORKSPACE, *options]


def test_cli_reads_the_token_from_a_hidden_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch)
    profiles, keyring, transport = _cli_service(tmp_path, monkeypatch, *_identity())

    result = runner.invoke(cli.app, _connect(), input=f"{TOKEN}\n")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "connected_official"
    assert "Bitbucket API token" in result.output
    assert TOKEN not in result.output
    assert keyring.values == {KEY: TOKEN}
    assert transport.requests[0].headers is not None
    assert transport.requests[0].headers["Authorization"] == BASIC
    assert profiles.get("bitbucket", ACCOUNT) is not None


def test_cli_reads_the_token_from_the_named_variable_or_one_stdin_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch)
    monkeypatch.setenv("BITBUCKET_API_TOKEN", f"  {TOKEN}  ")
    _, keyring, _ = _cli_service(tmp_path / "env", monkeypatch, *_identity())

    from_env = runner.invoke(
        cli.app, _connect("--token-env", "BITBUCKET_API_TOKEN"), input="prompt-token\n"
    )

    assert json.loads(from_env.stdout)["status"] == "connected_official"
    assert "Bitbucket API token" not in from_env.output
    assert keyring.values == {KEY: TOKEN}

    _, keyring, _ = _cli_service(tmp_path / "stdin", monkeypatch, *_identity())
    from_stdin = runner.invoke(
        cli.app, _connect("--token-stdin"), input=f"{TOKEN}\nignored-second-line\n"
    )

    assert json.loads(from_stdin.stdout)["status"] == "connected_official"
    assert TOKEN not in from_stdin.output
    assert keyring.values == {KEY: TOKEN}


def test_cli_input_errors_name_the_option_and_never_prompt_or_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch)
    profiles, keyring, transport = _cli_service(tmp_path, monkeypatch)
    cases = {
        "--site": _connect("--site", "https://example.atlassian.net"),
        "--token-kind": _connect("--token-kind", "scoped"),
        "--cloud-id": _connect("--cloud-id", "11223344-a1b2-3b33-c444-def123456789"),
        "--allow-browser-fallback": _connect("--allow-browser-fallback"),
        "workspace": ["connect", "bitbucket", ACCOUNT, "--workspace", "a/b"],
        "--workspace applies only to bitbucket": [
            "connect",
            "jira",
            ACCOUNT,
            "--site",
            "https://example.atlassian.net",
            "--workspace",
            WORKSPACE,
        ],
        "BITBUCKET_API_TOKEN": _connect("--token-env", "BITBUCKET_API_TOKEN"),
        "mutually exclusive": _connect("--token-env", "BITBUCKET_API_TOKEN", "--token-stdin"),
        "apply only to jira": ["connect", "gmail", ACCOUNT, "--token-env", "BITBUCKET_API_TOKEN"],
    }

    for expected, arguments in cases.items():
        result = runner.invoke(cli.app, arguments, input=f"{TOKEN}\n")
        payload = json.loads(result.stdout)
        assert payload["status"] == "invalid_input", expected
        assert expected in payload["error"]["message"], expected
        assert TOKEN not in result.output
    no_workspace = runner.invoke(cli.app, ["connect", "bitbucket", ACCOUNT])
    assert json.loads(no_workspace.stdout)["status"] == "configuration_error"
    assert transport.requests == []
    assert keyring.values == {} and profiles.get("bitbucket", ACCOUNT) is None


# --- Project mode ---------------------------------------------------------------------------


def test_project_mode_stores_the_token_in_the_project_folder_through_the_existing_factories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    home = project / ".solercia-connect"
    prepare_project_home(home)
    env_file = home / "connect.env"
    env_file.write_text(f"BITBUCKET_API_TOKEN={TOKEN}\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("SOLERCIA_CONNECT_HOME", str(home))
    settings = load_settings()
    transport = FakeTransport(*_identity())
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        oauth_token_store_from_settings(settings),
        transport=transport,
        clock=lambda: NOW,
    )
    monkeypatch.setattr(cli, "connection_service_from_environment", lambda **_: service)

    result = runner.invoke(cli.app, _connect("--token-env", "BITBUCKET_API_TOKEN"))

    assert json.loads(result.stdout)["status"] == "connected_official", result.output
    assert TOKEN not in result.output
    secrets = [path for path in (home / "secrets").iterdir() if path.is_file()]
    assert len(secrets) == 1 and secrets[0].read_text(encoding="utf-8") == TOKEN
    profile = ProfileStore.from_settings(settings).get("bitbucket", ACCOUNT)
    assert profile is not None and profile.workspace == WORKSPACE

    run_transport = FakeTransport(*_identity())
    registry = registry_from_environment(
        connection_type="bitbucket", account=ACCOUNT, transport=run_transport
    )

    assert registry.find("bitbucket.cloud").health().account_id == ACCOUNT_ID
    assert run_transport.requests[0].headers is not None
    assert run_transport.requests[0].headers["Authorization"] == BASIC
