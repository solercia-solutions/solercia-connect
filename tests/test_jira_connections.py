from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import solercia_connect.cli as cli
from solercia_connect.application import run_application
from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.config import (
    PROFILE_SCHEMA,
    ConnectionProfile,
    ProfileStore,
    load_settings,
    normalize_jira_site,
)
from solercia_connect.connections import ConnectionService
from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import OAuthTokenSet
from solercia_connect.secret_store import API_TOKEN_SERVICE, KeyringSecretStore
from solercia_connect.secrets import SecretValue
from solercia_connect.service import create_app
from tests.fakes import NOW, FakeTransport, json_response

ACCOUNT = "user@example.com"
SITE = "https://example.atlassian.net"
TOKEN = "jira-api-token-value"
KEY = (API_TOKEN_SERVICE, f"jira:{ACCOUNT}")
BASIC = "Basic " + base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()
MYSELF = f"{SITE}/rest/api/3/myself"

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
    """Jira API tokens must never reach OAuth token state."""

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
    tmp_path: Path,
    *responses: Any,
    keyring: MemoryKeyring | None = None,
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


def _myself(account_id: Any = "acct-1", email: Any = ACCOUNT) -> Any:
    payload: dict[str, Any] = {"accountId": account_id, "displayName": "User"}
    if email is not None:
        payload["emailAddress"] = email
    return json_response(200, payload)


def _connected(tmp_path: Path, *later: Any) -> tuple[Any, ProfileStore, MemoryKeyring, Any]:
    service, profiles, keyring, transport = _service(tmp_path, _myself(), *later)
    assert service.connect_jira(ACCOUNT, site=SITE, token=SecretValue(TOKEN)).ok
    return service, profiles, keyring, transport


def test_jira_site_is_normalized_and_plain_http_is_rejected() -> None:
    assert normalize_jira_site(" HTTPS://Example.Atlassian.NET/jira/browse/ ") == SITE
    assert normalize_jira_site("https://example.atlassian.net") == SITE
    for invalid in (
        "http://example.atlassian.net",
        "https://user:secret@example.atlassian.net",
        "https://example.atlassian.net/?a=1",
        "https://:443",
        "example.atlassian.net",
    ):
        with pytest.raises(ValueError):
            normalize_jira_site(invalid)


def test_profiles_keep_reading_files_written_before_the_site_field(tmp_path: Path) -> None:
    store = ProfileStore.from_settings(load_settings(environ=_values(tmp_path)))
    legacy = ConnectionProfile.create("gmail", ACCOUNT, now=NOW)
    store.save(legacy)

    loaded = store.get("gmail", ACCOUNT)
    raw = json.loads(store.path_for("gmail", ACCOUNT).read_text(encoding="utf-8"))

    assert loaded == legacy
    assert loaded is not None and loaded.site is None and loaded.provider_account_id is None
    assert raw["schema"] == PROFILE_SCHEMA
    assert "site" not in raw and "provider_account_id" not in raw

    jira = ConnectionProfile.create(
        "jira", ACCOUNT, now=NOW, site="https://Example.atlassian.net/", provider_account_id="a-1"
    )
    assert ConnectionProfile.from_dict(jira.to_dict()) == jira
    assert jira.site == SITE
    for field, value in (("site", "http://example.atlassian.net"), ("provider_account_id", 7)):
        with pytest.raises(ValueError):
            ConnectionProfile.from_dict({**jira.to_dict(), field: value})


def test_keyring_secret_store_is_separate_from_oauth_state_and_never_leaks() -> None:
    keyring = MemoryKeyring()
    store = KeyringSecretStore(backend=keyring)

    store.save(f"jira:{ACCOUNT}", SecretValue(TOKEN))
    loaded = store.load(f"jira:{ACCOUNT}")

    assert keyring.values == {KEY: TOKEN}
    assert API_TOKEN_SERVICE != "solercia-connect"
    assert loaded is not None and loaded.reveal() == TOKEN
    assert store.delete(f"jira:{ACCOUNT}") is True
    assert store.delete(f"jira:{ACCOUNT}") is False
    assert store.load(f"jira:{ACCOUNT}") is None

    with pytest.raises(ConnectorFailure) as captured:
        KeyringSecretStore(backend=BrokenKeyring()).save(f"jira:{ACCOUNT}", SecretValue(TOKEN))
    assert captured.value.reason is ReasonCode.CONFIGURATION_INVALID
    assert TOKEN not in str(captured.value)


def test_jira_connect_binds_the_account_id_and_keeps_the_token_in_the_keyring_only(
    tmp_path: Path,
) -> None:
    service, profiles, keyring, transport = _service(tmp_path, _myself(email="User@Example.com"))

    result = service.connect_jira(
        "User@Example.com", site="https://Example.atlassian.net/", token=SecretValue(TOKEN)
    )

    assert result.ok is True
    assert result.status == "connected_official"
    assert result.route == "official"
    assert result.data["site"] == SITE
    assert result.data["provider_account_id"] == "acct-1"
    assert result.data["email_source"] == "myself"
    profile = profiles.get("jira", ACCOUNT)
    assert profile is not None and profile.route == "official"
    assert profile.site == SITE and profile.provider_account_id == "acct-1"
    assert keyring.values == {KEY: TOKEN}
    assert [request.url for request in transport.requests] == [MYSELF]
    assert transport.requests[0].headers is not None
    assert transport.requests[0].headers["Authorization"] == BASIC
    assert TOKEN not in profiles.path_for("jira", ACCOUNT).read_text(encoding="utf-8")
    assert TOKEN not in json.dumps(result.to_dict())


def test_jira_connect_accepts_a_hidden_email_and_records_authentication_as_its_source(
    tmp_path: Path,
) -> None:
    for index, email in enumerate((None, "", "   ")):
        service, profiles, keyring, _ = _service(tmp_path / str(index), _myself(email=email))

        result = service.connect_jira(ACCOUNT, site=SITE, token=SecretValue(TOKEN))

        assert result.status == "connected_official"
        assert result.data["email_source"] == "authentication"
        assert profiles.get("jira", ACCOUNT) is not None
        assert keyring.values == {KEY: TOKEN}


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_myself(email="other@example.com"), "authorization_boundary_violation"),
        (_myself(account_id="unknown"), "authorization_boundary_violation"),
        (_myself(account_id=""), "malformed_provider_data"),
        (_myself(account_id=None), "malformed_provider_data"),
        (json_response(401, {"errorMessages": ["nope"]}), "invalid_credentials"),
        (json_response(403, {"errorMessages": ["nope"]}), "authorization_denied"),
    ],
)
def test_jira_connect_fails_closed_and_stores_nothing(
    tmp_path: Path, response: Any, code: str
) -> None:
    service, profiles, keyring, _ = _service(tmp_path, response)

    result = service.connect_jira(ACCOUNT, site=SITE, token=SecretValue(TOKEN))

    assert result.ok is False
    assert result.error is not None and result.error.code == code
    assert profiles.get("jira", ACCOUNT) is None
    assert keyring.values == {}
    assert TOKEN not in json.dumps(result.to_dict())
    if code in {"invalid_credentials", "authorization_denied"}:
        assert result.error.remediation[-1] == (
            f"solercia-connect connect jira {ACCOUNT} --site {SITE}"
        )


def test_jira_connect_rejects_a_plain_http_site_before_any_request(tmp_path: Path) -> None:
    service, profiles, keyring, transport = _service(tmp_path)

    result = service.connect_jira(
        ACCOUNT, site="http://example.atlassian.net", token=SecretValue(TOKEN)
    )
    bad_account = service.connect_jira("not-an-account", site=SITE, token=SecretValue(TOKEN))

    assert result.status == "invalid_input"
    assert bad_account.status == "invalid_input"
    assert transport.requests == []
    assert profiles.get("jira", ACCOUNT) is None and keyring.values == {}


def test_jira_profile_write_failure_rolls_back_the_stored_token(tmp_path: Path) -> None:
    service, profiles, keyring, _ = _service(tmp_path, _myself())
    (profiles.root / "jira").mkdir(parents=True, mode=0o755)

    result = service.connect_jira(ACCOUNT, site=SITE, token=SecretValue(TOKEN))

    assert result.error is not None and result.error.code == "configuration_invalid"
    assert keyring.values == {}
    assert TOKEN not in json.dumps(result.to_dict())


def test_jira_connect_without_site_or_token_points_to_the_cli_and_glpi_is_unchanged(
    tmp_path: Path,
) -> None:
    service, _, _, transport = _service(tmp_path)

    jira = service.connect("jira", ACCOUNT)
    glpi = service.connect("glpi", ACCOUNT)

    assert jira.status == "configuration_error"
    assert jira.error is not None and jira.error.code == "configuration_invalid"
    assert jira.error.remediation == (f"solercia-connect connect jira {ACCOUNT} --site <site>",)
    assert glpi.error is not None and glpi.error.code == "capability_not_implemented"
    assert transport.requests == []


def test_jira_secret_save_failure_leaves_no_profile(tmp_path: Path) -> None:
    service, profiles, _, _ = _service(tmp_path, _myself(), keyring=BrokenKeyring())

    result = service.connect_jira(ACCOUNT, site=SITE, token=SecretValue(TOKEN))

    assert result.error is not None and result.error.code == "configuration_invalid"
    assert profiles.get("jira", ACCOUNT) is None
    assert TOKEN not in json.dumps(result.to_dict())


def test_jira_status_reverifies_the_bound_account_id(tmp_path: Path) -> None:
    service, _, _, transport = _connected(tmp_path, _myself(), _myself(account_id="acct-2"))

    healthy = service.status("jira", ACCOUNT)
    changed = service.status("jira", ACCOUNT)

    assert healthy.status == "connected_official"
    assert healthy.data["identity_verified"] is True
    assert healthy.data["site"] == SITE
    assert changed.ok is False
    assert changed.error is not None
    assert changed.error.code == "authorization_boundary_violation"
    assert [request.url for request in transport.requests] == [MYSELF, MYSELF, MYSELF]
    assert all(r.headers and r.headers["Authorization"] == BASIC for r in transport.requests)


def test_jira_status_without_profile_or_secret_requires_the_exact_connect_command(
    tmp_path: Path,
) -> None:
    service, _, keyring, transport = _connected(tmp_path)

    keyring.values.clear()
    missing_secret = service.status("jira", ACCOUNT)
    missing_profile = service.status("jira", "other@example.com")

    assert missing_secret.status == "action_required"
    assert missing_secret.error is not None
    assert missing_secret.error.remediation == (
        f"solercia-connect connect jira {ACCOUNT} --site {SITE}",
    )
    assert missing_profile.status == "action_required"
    assert missing_profile.error is not None
    assert missing_profile.error.remediation == (
        "solercia-connect connect jira other@example.com --site <site>",
    )
    assert len(transport.requests) == 1


def test_jira_status_with_a_revoked_token_asks_to_reconnect_the_same_site(tmp_path: Path) -> None:
    service, _, _, _ = _connected(tmp_path, json_response(401, {}))

    result = service.status("jira", ACCOUNT)

    assert result.status == "action_required"
    assert result.error is not None
    assert result.error.remediation == (f"solercia-connect connect jira {ACCOUNT} --site {SITE}",)


def test_jira_disconnect_deletes_profile_and_keyring_secret(tmp_path: Path) -> None:
    service, profiles, keyring, _ = _connected(tmp_path)

    result = service.disconnect("jira", ACCOUNT)
    again = service.disconnect("jira", ACCOUNT)

    assert result.ok is True
    assert result.data["local_profile_removed"] is True
    assert result.data["local_token_removed"] is True
    assert profiles.get("jira", ACCOUNT) is None
    assert keyring.values == {}
    assert again.ok is True and again.data["local_profile_removed"] is False


def _env_jira(tmp_path: Path) -> dict[str, str]:
    return _values(
        tmp_path,
        JIRA_BASE_URL="https://env-site.atlassian.net",
        JIRA_EMAIL="env@example.com",
        JIRA_API_TOKEN="env-token",
        JIRA_EXPECTED_ACCOUNT_ID="env-acct",
    )


def test_run_with_an_account_uses_that_profile_and_its_keyring_token(tmp_path: Path) -> None:
    values = _env_jira(tmp_path)
    _, _, keyring, _ = _connected(tmp_path)
    transport = FakeTransport(_myself(), _myself(account_id="acct-2"))

    registry = registry_from_environment(
        connection_type="jira",
        account=ACCOUNT,
        environ=values,
        token_store=UnusedOAuthStore(),
        secret_store=KeyringSecretStore(backend=keyring),
        transport=transport,
    )
    report = registry.find("jira.cloud").health()

    assert report.account_id == "acct-1"
    assert transport.requests[0].url == MYSELF
    assert transport.requests[0].headers is not None
    assert transport.requests[0].headers["Authorization"] == BASIC
    with pytest.raises(ConnectorFailure) as captured:
        registry.find("jira.cloud").health()
    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION


def test_run_without_an_account_keeps_the_environment_credentials(tmp_path: Path) -> None:
    transport = FakeTransport(json_response(200, {"accountId": "env-acct"}))

    registry = registry_from_environment(
        connection_type="jira",
        environ=_env_jira(tmp_path),
        token_store=UnusedOAuthStore(),
        secret_store=KeyringSecretStore(backend=MemoryKeyring()),
        transport=transport,
    )

    assert registry.find("jira.cloud").health().account_id == "env-acct"
    assert transport.requests[0].url == "https://env-site.atlassian.net/rest/api/3/myself"
    expected = "Basic " + base64.b64encode(b"env@example.com:env-token").decode()
    assert transport.requests[0].headers is not None
    assert transport.requests[0].headers["Authorization"] == expected


def test_run_for_an_account_without_a_profile_never_falls_back_to_environment_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _env_jira(tmp_path)
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="solercia-connect connect jira"):
        registry_from_environment(
            connection_type="jira",
            account="env@example.com",
            environ=values,
            token_store=UnusedOAuthStore(),
            secret_store=KeyringSecretStore(backend=MemoryKeyring()),
            transport=FakeTransport(),
        )
    result = run_application(
        {
            "type": "jira",
            "account": "env@example.com",
            "capability": "external.issues.search",
            "start": "2026-08-01T00:00:00+00:00",
            "end": "2026-09-01T00:00:00+00:00",
        },
        settings=load_settings(environ=values),
    )

    assert result.status == "configuration_error"
    assert result.error is not None and result.error.code == "configuration_invalid"
    assert "env-token" not in json.dumps(result.to_dict())


def test_run_for_an_account_whose_keyring_token_is_gone_requires_reconnection(
    tmp_path: Path,
) -> None:
    _, _, keyring, _ = _connected(tmp_path)
    keyring.values.clear()

    with pytest.raises(ConnectorFailure) as captured:
        registry_from_environment(
            connection_type="jira",
            account=ACCOUNT,
            environ=_values(tmp_path),
            token_store=UnusedOAuthStore(),
            secret_store=KeyringSecretStore(backend=keyring),
            transport=FakeTransport(),
        )

    assert captured.value.reason is ReasonCode.INVALID_CREDENTIALS


def _cli_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *responses: Any
) -> tuple[ProfileStore, MemoryKeyring, FakeTransport]:
    service, profiles, keyring, transport = _service(tmp_path, *responses)
    monkeypatch.setattr(cli, "connection_service_from_environment", lambda **_: service)
    return profiles, keyring, transport


def test_cli_reads_the_jira_token_from_a_hidden_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles, keyring, transport = _cli_service(tmp_path, monkeypatch, _myself())

    result = runner.invoke(
        cli.app, ["connect", "jira", ACCOUNT, "--site", SITE], input=f"{TOKEN}\n"
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "connected_official"
    assert TOKEN not in result.output
    assert keyring.values == {KEY: TOKEN}
    assert transport.requests[0].headers is not None
    assert transport.requests[0].headers["Authorization"] == BASIC
    assert profiles.get("jira", ACCOUNT) is not None


def test_cli_reads_one_jira_token_line_from_stdin_for_automation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, keyring, _ = _cli_service(tmp_path, monkeypatch, _myself())

    result = runner.invoke(
        cli.app,
        ["connect", "jira", ACCOUNT, "--site", SITE, "--token-stdin"],
        input=f"{TOKEN}\nignored-second-line\n",
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "connected_official"
    assert TOKEN not in result.output
    assert keyring.values == {KEY: TOKEN}


def test_cli_token_stdin_refuses_an_interactive_terminal_that_would_echo_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Terminal(f"{TOKEN}\n"))

    with pytest.raises(ValueError, match="hidden prompt"):
        cli._jira_token(from_stdin=True)


def test_cli_jira_input_errors_never_prompt_or_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles, keyring, transport = _cli_service(tmp_path, monkeypatch)

    no_site = runner.invoke(cli.app, ["connect", "jira", ACCOUNT])
    http_site = runner.invoke(
        cli.app, ["connect", "jira", ACCOUNT, "--site", "http://example.atlassian.net"]
    )
    empty = runner.invoke(
        cli.app, ["connect", "jira", ACCOUNT, "--site", SITE, "--token-stdin"], input="\n"
    )
    cancelled = runner.invoke(cli.app, ["connect", "jira", ACCOUNT, "--site", SITE])
    misplaced = runner.invoke(cli.app, ["connect", "gmail", ACCOUNT, "--site", SITE])

    assert json.loads(no_site.stdout)["status"] == "configuration_error"
    for result in (http_site, empty, cancelled, misplaced):
        assert json.loads(result.stdout)["status"] == "invalid_input"
    assert transport.requests == []
    assert keyring.values == {} and profiles.get("jira", ACCOUNT) is None


def test_http_service_never_accepts_jira_tokens(tmp_path: Path) -> None:
    settings = load_settings(environ=_values(tmp_path, SOLERCIA_CONNECT_API_KEY="local-api-key"))
    client = TestClient(create_app(settings=settings, token_store=UnusedOAuthStore()))
    headers = {"X-Solercia-Connect-Key": "local-api-key"}

    with_token = client.post(
        "/v1/connections/connect",
        headers=headers,
        json={"type": "jira", "account": ACCOUNT, "api_token": TOKEN, "site": SITE},
    )
    without_token = client.post(
        "/v1/connections/connect", headers=headers, json={"type": "jira", "account": ACCOUNT}
    )

    assert with_token.status_code == 422
    assert TOKEN not in with_token.text
    assert without_token.status_code == 422
    assert without_token.json()["status"] == "configuration_error"
    assert without_token.json()["error"]["code"] == "configuration_invalid"
    assert without_token.json()["error"]["remediation"] == [
        f"solercia-connect connect jira {ACCOUNT} --site <site>"
    ]
