"""Atlassian OAuth 2.0 (3LO) for Jira with the user's own app (task T2.3).

Every request goes to a FakeTransport; the token store and the API-token store share one
in-memory keyring so the tests can prove the two never collide.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import solercia_connect.cli as cli
import solercia_connect.connections as connections_module
from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.config import JiraTokenKind, ProfileStore, load_settings
from solercia_connect.connections import (
    ConnectionService,
    atlassian_oauth_provider_from_settings,
)
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    DateWindow,
    IssueQuery,
    ReasonCode,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import AtlassianOAuthProvider, KeyringOAuthTokenStore, OAuthTokenSet
from solercia_connect.secret_store import API_TOKEN_SERVICE, KeyringSecretStore
from solercia_connect.secrets import SecretValue
from solercia_connect.service import create_app
from tests.fakes import NOW, FakeTransport, json_response

ACCOUNT = "user@example.com"
SITE = "https://example.atlassian.net"
CLOUD_ID = "11223344-a1b2-3b33-c444-def123456789"
OTHER_CLOUD_ID = "99887766-a1b2-3b33-c444-def123456789"
GATEWAY = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}"
MYSELF = f"{GATEWAY}/rest/api/3/myself"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ME = "https://api.atlassian.com/me"
RESOURCES = "https://api.atlassian.com/oauth/token/accessible-resources"
CLIENT_ID = "atlassian-client-id"
CLIENT_SECRET = "atlassian-client-secret-value"
REDIRECT = "http://127.0.0.1:8766/oauth/callback"
CODE = "atlassian-authorization-code"
ACCESS = "atlassian-access-token-value"
REFRESH = "atlassian-refresh-token-value"
NEW_ACCESS = "atlassian-rotated-access-value"
NEW_REFRESH = "atlassian-rotated-refresh-value"
SCOPES = ("read:jira-work", "read:jira-user", "read:me", "offline_access")
OAUTH_SERVICE = "solercia-connect"
OAUTH_KEY = (OAUTH_SERVICE, f"jira:{ACCOUNT}")
RECONNECT = f"solercia-connect connect jira {ACCOUNT} --site {SITE} --token-kind oauth"
SECRETS = (CLIENT_SECRET, CODE, ACCESS, REFRESH, NEW_ACCESS, NEW_REFRESH)

runner = CliRunner()


class MemoryKeyring:
    """In-memory OS keyring; deleting a missing entry fails like the real keyring does."""

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


class MutableClock:
    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        return self.current


def _values(tmp_path: Path, *, configured: bool = True, **extra: str) -> dict[str, str]:
    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    if configured:
        values["SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID"] = CLIENT_ID
        values["SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET"] = CLIENT_SECRET
    return {**values, **extra}


def _token(access: str = ACCESS, refresh: str | None = REFRESH) -> Any:
    # Atlassian documents no token_type in these responses; the tokens are bearer tokens.
    payload: dict[str, Any] = {
        "access_token": access,
        "expires_in": 3600,
        "scope": " ".join(SCOPES),
    }
    if refresh is not None:
        payload["refresh_token"] = refresh
    return json_response(200, payload)


def _me(account_id: str = "acct-1", email: str | None = ACCOUNT) -> Any:
    payload: dict[str, Any] = {"account_id": account_id, "name": "User"}
    if email is not None:
        payload["email"] = email
    return json_response(200, payload)


def _site(cloud_id: str = CLOUD_ID, url: str = SITE) -> dict[str, Any]:
    return {"id": cloud_id, "name": "Site", "url": url, "scopes": list(SCOPES[:2])}


def _resources(*sites: dict[str, Any]) -> Any:
    return json_response(200, list(sites))


def _myself(account_id: str = "acct-1", email: str | None = ACCOUNT) -> Any:
    payload: dict[str, Any] = {"accountId": account_id, "displayName": "User"}
    if email is not None:
        payload["emailAddress"] = email
    return json_response(200, payload)


def _exchange(oauth: AtlassianOAuthProvider, open_browser: bool) -> None:
    del open_browser
    oauth.exchange_authorization_code(SecretValue(CODE), redirect_uri=REDIRECT)


def _service(
    tmp_path: Path,
    *responses: Any,
    configured: bool = True,
    flow: Any = _exchange,
    clock: Any = None,
    keyring: MemoryKeyring | None = None,
) -> tuple[ConnectionService, ProfileStore, MemoryKeyring, FakeTransport]:
    settings = load_settings(environ=_values(tmp_path, configured=configured))
    profiles = ProfileStore.from_settings(settings)
    backend = keyring if keyring is not None else MemoryKeyring()
    transport = FakeTransport(*responses)
    service = ConnectionService(
        settings,
        profiles,
        KeyringOAuthTokenStore(backend=backend),
        secret_store=KeyringSecretStore(backend=backend),
        transport=transport,
        atlassian_callback_flow=flow,
        clock=clock or (lambda: NOW),
    )
    return service, profiles, backend, transport


def _connected(
    tmp_path: Path, *later: Any, clock: Any = None
) -> tuple[ConnectionService, ProfileStore, MemoryKeyring, FakeTransport]:
    service, profiles, keyring, transport = _service(
        tmp_path, _token(), _me(), _resources(_site()), _myself(), *later, clock=clock
    )
    assert service.connect_jira_oauth(ACCOUNT, site=SITE).status == "connected_official"
    return service, profiles, keyring, transport


def _provider(transport: FakeTransport, store: Any, clock: Any = None) -> AtlassianOAuthProvider:
    return AtlassianOAuthProvider(
        transport,
        client_id=CLIENT_ID,
        client_secret=SecretValue(CLIENT_SECRET),
        store=store,
        store_key=f"jira:{ACCOUNT}",
        clock=clock or (lambda: NOW),
    )


def _stored(keyring: MemoryKeyring) -> OAuthTokenSet | None:
    return KeyringOAuthTokenStore(backend=keyring).load(f"jira:{ACCOUNT}")


def _assert_no_secret(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


# --- Settings ---------------------------------------------------------------------------------


def test_atlassian_settings_come_from_the_environment_with_a_fixed_loopback_redirect(
    tmp_path: Path,
) -> None:
    settings = load_settings(environ=_values(tmp_path))
    custom = load_settings(
        environ=_values(
            tmp_path, SOLERCIA_CONNECT_ATLASSIAN_REDIRECT_URI="http://localhost:9123/oauth/callback"
        )
    )

    assert settings.atlassian_client_id == CLIENT_ID
    assert settings.atlassian_client_secret is not None
    assert settings.atlassian_client_secret.reveal() == CLIENT_SECRET
    assert CLIENT_SECRET not in repr(settings)
    assert settings.atlassian_redirect_uri == REDIRECT
    assert custom.atlassian_redirect_uri == "http://localhost:9123/oauth/callback"
    for invalid in (
        "https://example.com/oauth/callback",
        "http://10.0.0.5:9123/oauth/callback",
        "http://127.0.0.1/oauth/callback",
        "http://127.0.0.1:80/oauth/callback",
        "http://127.0.0.1:9123/callback",
        "http://127.0.0.1:9123/oauth/callback?x=1",
        "http://user@127.0.0.1:9123/oauth/callback",
        "http://127.0.0.1:99999/oauth/callback",
    ):
        with pytest.raises(ValueError, match="SOLERCIA_CONNECT_ATLASSIAN_REDIRECT_URI"):
            load_settings(
                environ=_values(tmp_path, SOLERCIA_CONNECT_ATLASSIAN_REDIRECT_URI=invalid)
            )


# --- Provider ---------------------------------------------------------------------------------


def test_authorization_url_carries_the_documented_parameters_and_no_pkce() -> None:
    provider = _provider(FakeTransport(), KeyringOAuthTokenStore(backend=MemoryKeyring()))

    url = provider.authorization_url(redirect_uri=REDIRECT, state="state-value-123")
    parsed = urlsplit(url)

    assert (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://auth.atlassian.com/authorize"
    )
    assert parse_qs(parsed.query) == {
        "audience": ["api.atlassian.com"],
        "client_id": [CLIENT_ID],
        "scope": [" ".join(SCOPES)],
        "redirect_uri": [REDIRECT],
        "state": ["state-value-123"],
        "response_type": ["code"],
        "prompt": ["consent"],
    }
    assert "code_challenge" not in url and CLIENT_SECRET not in url
    with pytest.raises(ValueError):
        provider.authorization_url(redirect_uri=REDIRECT, state=" ")


def test_code_exchange_sends_the_client_secret_and_never_a_code_verifier() -> None:
    keyring = MemoryKeyring()
    transport = FakeTransport(_token())
    provider = _provider(transport, KeyringOAuthTokenStore(backend=keyring))

    tokens = provider.exchange_authorization_code(SecretValue(CODE), redirect_uri=REDIRECT)

    request = transport.requests[0]
    assert (request.method, request.url, request.form_body) == ("POST", TOKEN_URL, None)
    assert request.json_body == {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": CODE,
        "redirect_uri": REDIRECT,
    }
    assert tokens.token_type == "Bearer" and tokens.scopes == SCOPES
    stored = _stored(keyring)
    assert stored is not None and stored.refresh_token is not None
    assert stored.refresh_token.reveal() == REFRESH
    with pytest.raises(ValueError, match="PKCE"):
        provider.exchange_authorization_code(
            SecretValue(CODE), redirect_uri=REDIRECT, code_verifier=SecretValue("verifier")
        )
    assert len(transport.requests) == 1


def test_refresh_rotation_replaces_the_stored_refresh_token() -> None:
    keyring = MemoryKeyring()
    clock = MutableClock()
    transport = FakeTransport(_token(), _token(NEW_ACCESS, NEW_REFRESH), _token(NEW_ACCESS, None))
    provider = _provider(transport, KeyringOAuthTokenStore(backend=keyring), clock)
    provider.exchange_authorization_code(SecretValue(CODE), redirect_uri=REDIRECT)
    clock.current = NOW + timedelta(hours=2)

    header = provider.authorization_header()

    assert header == f"Bearer {NEW_ACCESS}"
    assert transport.requests[1].json_body == {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH,
    }
    stored = _stored(keyring)
    assert stored is not None and stored.refresh_token is not None
    assert stored.refresh_token.reveal() == NEW_REFRESH
    # The used refresh token is disabled by Atlassian, so a refresh without a new one fails
    # closed and keeps the rotated token in place instead of reviving the old one.
    clock.current = NOW + timedelta(hours=4)
    with pytest.raises(ConnectorFailure) as captured:
        provider.refresh()
    assert captured.value.reason is ReasonCode.INVALID_CREDENTIALS
    after = _stored(keyring)
    assert after is not None and after.refresh_token is not None
    assert after.refresh_token.reveal() == NEW_REFRESH


def test_invalid_grant_on_refresh_is_invalid_credentials_without_the_provider_body() -> None:
    keyring = MemoryKeyring()
    clock = MutableClock()
    body = {"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."}
    transport = FakeTransport(_token(), json_response(403, body))
    provider = _provider(transport, KeyringOAuthTokenStore(backend=keyring), clock)
    provider.exchange_authorization_code(SecretValue(CODE), redirect_uri=REDIRECT)
    clock.current = NOW + timedelta(hours=2)

    with pytest.raises(ConnectorFailure) as captured:
        provider.authorization_header()

    assert captured.value.reason is ReasonCode.INVALID_CREDENTIALS
    assert "Unknown or invalid" not in captured.value.safe_message


# --- connect ----------------------------------------------------------------------------------


def test_connect_binds_account_id_and_cloud_id_and_keeps_tokens_in_the_oauth_store_only(
    tmp_path: Path,
) -> None:
    # A resource whose URL is not an HTTPS origin can never equal the site, so it is skipped.
    plain_http = _site(OTHER_CLOUD_ID, "http://example.atlassian.net")
    service, profiles, keyring, transport = _service(
        tmp_path, _token(), _me(), _resources(plain_http, _site()), _myself()
    )

    result = service.connect_jira_oauth(ACCOUNT, site="https://Example.atlassian.net/jira")

    assert result.status == "connected_official"
    assert result.data["token_kind"] == "oauth" and result.data["cloud_id"] == CLOUD_ID
    assert result.data["provider_account_id"] == "acct-1" and result.data["site"] == SITE
    assert result.data["email_source"] == "me"
    assert [request.url for request in transport.requests] == [TOKEN_URL, ME, RESOURCES, MYSELF]
    assert all(
        request.headers is not None and request.headers["Authorization"] == f"Bearer {ACCESS}"
        for request in transport.requests[1:]
    )
    profile = profiles.get("jira", ACCOUNT)
    assert profile is not None and profile.token_kind is JiraTokenKind.OAUTH
    assert profile.site == SITE and profile.cloud_id == CLOUD_ID
    assert profile.provider_account_id == "acct-1"
    # Same key convention as other OAuth targets, in a keyring service of its own: no collision
    # with the API-token store, which uses the same key under another service.
    assert set(keyring.values) == {OAUTH_KEY}
    assert OAUTH_SERVICE != API_TOKEN_SERVICE
    _assert_no_secret(
        json.dumps(result.to_dict()),
        profiles.path_for("jira", ACCOUNT).read_text(encoding="utf-8"),
    )


@pytest.mark.parametrize(
    ("responses", "code", "urls"),
    [
        pytest.param(
            (_me(email="other@example.com"),),
            "authorization_boundary_violation",
            [TOKEN_URL, ME],
            id="me-email-mismatch",
        ),
        pytest.param(
            (_me(), _resources(_site(url="https://other.atlassian.net"))),
            "authorization_boundary_violation",
            [TOKEN_URL, ME, RESOURCES],
            id="no-site-match",
        ),
        pytest.param(
            (_me(), _resources(_site(), _site(OTHER_CLOUD_ID, f"{SITE}/"))),
            "authorization_boundary_violation",
            [TOKEN_URL, ME, RESOURCES],
            id="duplicate-site-match",
        ),
        pytest.param(
            (_me(), _resources(_site("not-a-uuid"))),
            "malformed_provider_data",
            [TOKEN_URL, ME, RESOURCES],
            id="malformed-cloud-id",
        ),
        pytest.param(
            (_me(), _resources(_site()), _myself(account_id="acct-2")),
            "authorization_boundary_violation",
            [TOKEN_URL, ME, RESOURCES, MYSELF],
            id="myself-account-mismatch",
        ),
        pytest.param(
            (_me(email=None), _resources(_site()), _myself(email=None)),
            "authorization_boundary_violation",
            [TOKEN_URL, ME, RESOURCES, MYSELF],
            id="no-visible-email",
        ),
    ],
)
def test_connect_fails_closed_and_stores_nothing(
    tmp_path: Path, responses: tuple[Any, ...], code: str, urls: list[str]
) -> None:
    service, profiles, keyring, transport = _service(tmp_path, _token(), *responses)

    result = service.connect_jira_oauth(ACCOUNT, site=SITE)

    assert result.ok is False
    assert result.error is not None and result.error.code == code
    assert [request.url for request in transport.requests] == urls
    assert profiles.get("jira", ACCOUNT) is None and keyring.values == {}
    _assert_no_secret(json.dumps(result.to_dict()))


def test_profile_write_failure_discards_the_exchanged_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, profiles, keyring, _ = _service(
        tmp_path, _token(), _me(), _resources(_site()), _myself()
    )

    def refuse(profile: Any) -> None:
        raise ValueError("Connection profile could not be written safely")

    monkeypatch.setattr(profiles, "save", refuse)

    result = service.connect_jira_oauth(ACCOUNT, site=SITE)

    assert result.status == "failed"
    assert result.error is not None and result.error.code == "configuration_invalid"
    assert keyring.values == {}


def test_connect_accepts_an_email_proven_by_myself_when_me_hides_it(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path, _token(), _me(email=None), _resources(_site()), _myself())

    result = service.connect_jira_oauth(ACCOUNT, site=SITE)

    assert result.status == "connected_official" and result.data["email_source"] == "myself"


def test_connect_maps_invalid_grant_to_reconnect_and_callback_denial_stays_denied(
    tmp_path: Path,
) -> None:
    body = {"error": "invalid_grant", "error_description": "raw provider detail"}
    rejected, profiles, keyring, _ = _service(tmp_path, json_response(403, body))

    def deny(oauth: AtlassianOAuthProvider, open_browser: bool) -> None:
        raise ConnectorFailure(ReasonCode.AUTHORIZATION_DENIED, "OAuth authorization was denied")

    denied, _, _, denied_transport = _service(tmp_path, flow=deny)

    invalid = rejected.connect_jira_oauth(ACCOUNT, site=SITE)
    refused = denied.connect_jira_oauth(ACCOUNT, site=SITE)

    assert invalid.error is not None and invalid.error.code == "invalid_credentials"
    assert invalid.error.remediation == (
        "Run the connect command again to renew the Atlassian authorization.",
        RECONNECT,
    )
    assert "raw provider detail" not in json.dumps(invalid.to_dict())
    assert refused.error is not None and refused.error.code == "authorization_denied"
    assert refused.error.remediation == (RECONNECT,)
    assert denied_transport.requests == []
    assert profiles.get("jira", ACCOUNT) is None and keyring.values == {}


def test_missing_client_settings_are_a_typed_configuration_error_before_any_request(
    tmp_path: Path,
) -> None:
    flows: list[bool] = []
    service, profiles, keyring, transport = _service(
        tmp_path, configured=False, flow=lambda oauth, open_browser: flows.append(open_browser)
    )

    result = service.connect_jira_oauth(ACCOUNT, site=SITE)

    assert result.status == "configuration_error"
    assert result.error is not None and result.error.code == "configuration_invalid"
    assert result.error.missing_configuration == (
        "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID",
        "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET",
    )
    assert REDIRECT in result.error.remediation[-1]
    assert flows == [] and transport.requests == []
    assert profiles.get("jira", ACCOUNT) is None and keyring.values == {}
    with pytest.raises(ValueError, match="SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET"):
        atlassian_oauth_provider_from_settings(
            load_settings(
                environ=_values(
                    tmp_path, configured=False, SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID=CLIENT_ID
                )
            ),
            KeyringOAuthTokenStore(backend=MemoryKeyring()),
            ACCOUNT,
        )


def test_connect_rejects_an_invalid_site_before_any_request(tmp_path: Path) -> None:
    service, _, keyring, transport = _service(tmp_path)

    result = service.connect_jira_oauth(ACCOUNT, site="http://example.atlassian.net")

    assert result.status == "invalid_input"
    assert transport.requests == [] and keyring.values == {}


def test_default_callback_binds_the_registered_port_and_exchanges_without_pkce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered = "http://localhost:9123/oauth/callback"
    actions: list[dict[str, Any]] = []
    servers: list[tuple[str, int]] = []
    opened: list[str] = []

    class Callback:
        def __init__(self, state: str, *, port: int) -> None:
            servers.append((state, port))

        def __enter__(self) -> Callback:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def wait(self) -> SimpleNamespace:
            return SimpleNamespace(code=SecretValue(CODE))

    pair = SimpleNamespace(state="state-value", challenge="unused", verifier=SecretValue("unused"))
    monkeypatch.setattr(connections_module, "build_pkce_pair", lambda: pair)
    monkeypatch.setattr(connections_module, "LoopbackCallbackServer", Callback)
    monkeypatch.setattr(
        connections_module.webbrowser, "open", lambda url, **_: opened.append(url) or True
    )
    settings = load_settings(
        environ=_values(tmp_path, SOLERCIA_CONNECT_ATLASSIAN_REDIRECT_URI=registered)
    )
    keyring = MemoryKeyring()
    transport = FakeTransport(_token())
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        KeyringOAuthTokenStore(backend=keyring),
        transport=transport,
        action_sink=lambda envelope: actions.append(envelope.to_dict()),
        clock=lambda: NOW,
    )
    oauth = atlassian_oauth_provider_from_settings(
        settings, KeyringOAuthTokenStore(backend=keyring), ACCOUNT, transport=transport
    )

    service._run_atlassian_callback(oauth, True)

    assert servers == [("state-value", 9123)]
    url = actions[0]["data"]["authorization_url"]
    query = parse_qs(urlsplit(url).query)
    assert query["redirect_uri"] == [registered] and query["state"] == ["state-value"]
    assert opened == [url]
    body = transport.requests[0].json_body
    assert body is not None and body["redirect_uri"] == registered
    assert "code_verifier" not in body
    _assert_no_secret(json.dumps(actions))


# --- status, run and disconnect ---------------------------------------------------------------


def test_status_reverifies_the_account_and_the_bound_cloud_id(tmp_path: Path) -> None:
    service, _, _, transport = _connected(tmp_path, _me(), _resources(_site()), _myself())

    result = service.status("jira", ACCOUNT)

    assert result.status == "connected_official"
    assert result.data["identity_verified"] is True
    assert result.data["token_kind"] == "oauth" and result.data["cloud_id"] == CLOUD_ID
    assert [request.url for request in transport.requests[4:]] == [ME, RESOURCES, MYSELF]
    _assert_no_secret(json.dumps(result.to_dict()))


def test_status_refreshes_an_expired_grant_and_persists_the_rotated_refresh_token(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    service, _, keyring, transport = _connected(
        tmp_path,
        _token(NEW_ACCESS, NEW_REFRESH),
        _me(),
        _resources(_site()),
        _myself(),
        clock=clock,
    )
    clock.current = NOW + timedelta(hours=2)

    result = service.status("jira", ACCOUNT)

    assert result.status == "connected_official"
    assert [request.url for request in transport.requests[4:]] == [TOKEN_URL, ME, RESOURCES, MYSELF]
    stored = _stored(keyring)
    assert stored is not None and stored.refresh_token is not None
    assert stored.refresh_token.reveal() == NEW_REFRESH
    assert transport.requests[-1].headers is not None
    assert transport.requests[-1].headers["Authorization"] == f"Bearer {NEW_ACCESS}"


@pytest.mark.parametrize(
    ("responses", "urls"),
    [
        pytest.param((_me(), _resources(_site(OTHER_CLOUD_ID))), [ME, RESOURCES], id="cloud-id"),
        pytest.param((_me(account_id="acct-2"),), [ME], id="account-id"),
    ],
)
def test_status_fails_closed_when_the_cloud_id_or_account_id_changed(
    tmp_path: Path, responses: tuple[Any, ...], urls: list[str]
) -> None:
    service, _, _, transport = _connected(tmp_path, *responses)

    result = service.status("jira", ACCOUNT)

    assert result.status == "failed"
    assert result.error is not None and result.error.code == "authorization_boundary_violation"
    assert [request.url for request in transport.requests[4:]] == urls


def test_status_asks_to_reconnect_on_invalid_grant_or_a_missing_grant(tmp_path: Path) -> None:
    clock = MutableClock()
    service, _, keyring, _ = _connected(
        tmp_path, json_response(403, {"error": "invalid_grant"}), clock=clock
    )
    clock.current = NOW + timedelta(hours=2)

    expired = service.status("jira", ACCOUNT)
    keyring.values.clear()
    missing = service.status("jira", ACCOUNT)

    for result in (expired, missing):
        assert result.status == "action_required"
        assert result.error is not None and result.error.remediation == (RECONNECT,)


def test_run_uses_bearer_against_the_stored_cloud_id_gateway(tmp_path: Path) -> None:
    # Runs read the wall clock, so the grant is issued now and needs no refresh.
    _, _, keyring, _ = _connected(tmp_path, clock=lambda: datetime.now(UTC))
    run_transport = FakeTransport(_myself(), json_response(200, {"issues": [], "isLast": True}))

    registry = registry_from_environment(
        connection_type="jira",
        account=ACCOUNT,
        environ=_values(tmp_path),
        token_store=KeyringOAuthTokenStore(backend=keyring),
        secret_store=KeyringSecretStore(backend=keyring),
        transport=run_transport,
    )
    window = DateWindow(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
    issues = registry.find("jira.cloud").execute(
        ConnectorRequest.create("jira.cloud", Capability.EXTERNAL_ISSUES_SEARCH, IssueQuery(window))
    )

    assert issues == ()
    assert [request.url for request in run_transport.requests] == [
        MYSELF,
        f"{GATEWAY}/rest/api/3/search/jql",
    ]
    assert all(
        request.headers is not None and request.headers["Authorization"] == f"Bearer {ACCESS}"
        for request in run_transport.requests
    )


def test_status_and_run_without_client_settings_or_stored_grant_fail_closed(
    tmp_path: Path,
) -> None:
    _, _, keyring, _ = _connected(tmp_path)
    unconfigured, _, _, transport = _service(tmp_path, configured=False, keyring=keyring)

    status = unconfigured.status("jira", ACCOUNT)

    assert status.status == "configuration_error" and transport.requests == []
    assert status.error is not None
    assert status.error.missing_configuration == (
        "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID",
        "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET",
    )

    def build(**values: str) -> Any:
        return registry_from_environment(
            connection_type="jira",
            account=ACCOUNT,
            environ=values,
            token_store=KeyringOAuthTokenStore(backend=keyring),
            secret_store=KeyringSecretStore(backend=keyring),
            transport=FakeTransport(),
        )

    with pytest.raises(ValueError, match="SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID"):
        build(**_values(tmp_path, configured=False))
    keyring.values.clear()
    with pytest.raises(ConnectorFailure) as captured:
        build(**_values(tmp_path))
    assert captured.value.reason is ReasonCode.INVALID_CREDENTIALS


def test_disconnect_removes_the_profile_and_every_stored_credential(tmp_path: Path) -> None:
    service, profiles, keyring, _ = _connected(tmp_path)
    keyring.values[(API_TOKEN_SERVICE, f"jira:{ACCOUNT}")] = "stale-api-token"

    result = service.disconnect("jira", ACCOUNT)
    again = service.disconnect("jira", ACCOUNT)

    assert result.ok is True and result.data["local_profile_removed"] is True
    assert profiles.get("jira", ACCOUNT) is None
    assert keyring.values == {}
    assert again.ok is True and again.data["local_profile_removed"] is False


# --- CLI and HTTP -----------------------------------------------------------------------------


def test_cli_connects_with_oauth_without_prompting_for_an_api_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flows: list[bool] = []

    def flow(oauth: AtlassianOAuthProvider, open_browser: bool) -> None:
        flows.append(open_browser)
        _exchange(oauth, open_browser)

    service, profiles, keyring, _ = _service(
        tmp_path, _token(), _me(), _resources(_site()), _myself(), flow=flow
    )
    monkeypatch.setattr(cli, "connection_service_from_environment", lambda **_: service)

    # No input: a token prompt would abort and turn the result into invalid_input.
    result = runner.invoke(
        cli.app,
        ["connect", "jira", ACCOUNT, "--site", SITE, "--token-kind", "oauth", "--no-open-browser"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "connected_official" and payload["data"]["token_kind"] == "oauth"
    assert flows == [False]
    assert set(keyring.values) == {OAUTH_KEY}
    assert profiles.get("jira", ACCOUNT) is not None
    _assert_no_secret(result.output)


def test_cli_oauth_rejects_api_token_options_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, keyring, transport = _service(tmp_path)
    monkeypatch.setattr(cli, "connection_service_from_environment", lambda **_: service)
    base = ["connect", "jira", ACCOUNT, "--site", SITE, "--token-kind", "oauth"]

    stdin = runner.invoke(cli.app, [*base, "--token-stdin"], input="token\n")
    cloud = runner.invoke(cli.app, [*base, "--cloud-id", CLOUD_ID])

    for result, option in ((stdin, "--token-stdin"), (cloud, "--cloud-id")):
        payload = json.loads(result.stdout)
        assert payload["status"] == "invalid_input" and option in payload["error"]["message"]
    assert transport.requests == [] and keyring.values == {}


def test_api_token_connect_refuses_the_oauth_kind_and_http_still_refuses_jira(
    tmp_path: Path,
) -> None:
    service, _, keyring, transport = _service(tmp_path)
    settings = load_settings(environ=_values(tmp_path, SOLERCIA_CONNECT_API_KEY="local-api-key"))
    client = TestClient(create_app(settings=settings))

    api_token = service.connect_jira(
        ACCOUNT, site=SITE, token=SecretValue("api-token"), token_kind="oauth"
    )
    http = client.post(
        "/v1/connections/connect",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json={"type": "jira", "account": ACCOUNT},
    )

    assert api_token.status == "invalid_input"
    assert api_token.error is not None and "--token-kind" in api_token.error.message
    assert transport.requests == [] and keyring.values == {}
    assert http.status_code == 422 and http.json()["status"] == "configuration_error"
