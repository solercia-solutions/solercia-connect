from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import solercia_connect.connections as connections_module
from solercia_connect.config import ConnectionProfile, ConnectionType, ProfileStore, load_settings
from solercia_connect.connections import ConnectionService, oauth_provider_from_settings
from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import DeviceAuthorization, GoogleOAuthProvider, OAuthTokenSet
from solercia_connect.providers.m365_browser import Microsoft365BrowserAdapter
from solercia_connect.providers.m365_browser.driver import BrowserSnapshot
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeBrowserDriver, authenticated_snapshot


class MemoryTokenStore:
    def __init__(self) -> None:
        self.values: dict[str, OAuthTokenSet] = {}
        self.deleted: list[str] = []

    def load(self, key: str) -> OAuthTokenSet | None:
        return self.values.get(key)

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        self.values[key] = tokens

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.values.pop(key, None)


class FakeMicrosoftOAuth:
    def __init__(self, store: MemoryTokenStore, key: str, failure: ConnectorFailure | None = None):
        self.store = store
        self.key = key
        self.failure = failure
        self.completed = 0

    def start_device_authorization(self) -> DeviceAuthorization:
        if self.failure:
            raise self.failure
        return DeviceAuthorization(
            SecretValue("never-print-device-code"),
            "ABCD-EFGH",
            "https://microsoft.com/devicelogin",
            NOW + timedelta(minutes=10),
            5,
        )

    def complete_device_authorization(
        self,
        authorization: DeviceAuthorization,
        *,
        max_attempts: int = 120,
        sleeper: Any = None,
    ) -> OAuthTokenSet:
        del authorization, max_attempts, sleeper
        self.completed += 1
        tokens = OAuthTokenSet(
            SecretValue("access"),
            SecretValue("refresh"),
            NOW + timedelta(hours=1),
            ("offline_access", "User.Read", "Mail.Read", "Calendars.Read"),
        )
        self.store.save(self.key, tokens)
        return tokens

    def authorization_header(self) -> str:
        return "Bearer access"


class FakeGoogleOAuth:
    def authorization_header(self) -> str:
        return "Bearer access"


def _settings(tmp_path: Path, **extra: str):
    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID": "microsoft-client",
        "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
        **extra,
    }
    return load_settings(environ=values)


def test_microsoft_connect_emits_safe_device_action_verifies_identity_and_saves_profile(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    tokens = MemoryTokenStore()
    actions: list[dict[str, Any]] = []
    key = "outlook:user@example.com"
    oauth = FakeMicrosoftOAuth(tokens, key)
    service = ConnectionService(
        settings,
        profiles,
        tokens,
        oauth_builder=lambda type, account: oauth,
        identity_verifier=lambda type, account, auth: "user@example.com",
        action_sink=lambda envelope: actions.append(envelope.to_dict()),
        clock=lambda: NOW,
    )

    result = service.connect("outlook", "User@Example.com")

    assert result.ok is True
    assert result.status == "connected_official"
    assert result.route == "official"
    assert profiles.get("outlook", "user@example.com") is not None
    assert actions[0]["status"] == "action_required"
    assert actions[0]["data"]["user_code"] == "ABCD-EFGH"
    assert "never-print-device-code" not in str(actions)


def test_invalid_account_fails_before_any_oauth_side_effect(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    calls: list[str] = []
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        oauth_builder=lambda type, account: calls.append(account),
        identity_verifier=lambda type, account, auth: account,
        clock=lambda: NOW,
    )

    result = service.connect("outlook", "not-an-account")

    assert result.ok is False
    assert result.status == "invalid_input"
    assert result.error is not None and result.error.code == "invalid_input"
    assert calls == []


@pytest.mark.parametrize(
    ("type_value", "expected_type"),
    [
        ("outlook", ConnectionType.OUTLOOK),
        ("teams", ConnectionType.TEAMS),
    ],
)
def test_connect_missing_microsoft_client_id_uses_browser_when_both_fallback_gates_are_true(
    type_value: str,
    expected_type: ConnectionType,
    tmp_path: Path,
) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_BROWSER_FALLBACK": "true",
        }
    )
    oauth_calls: list[tuple[ConnectionType, str]] = []
    browser_calls: list[tuple[ConnectionType, str]] = []

    def build_oauth(type_value: ConnectionType, account: str) -> FakeMicrosoftOAuth:
        oauth_calls.append((type_value, account))
        raise AssertionError("Official OAuth must be skipped when it is not configured")

    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        oauth_builder=build_oauth,
        browser_connector=lambda type_value, account: (
            browser_calls.append((type_value, account)) or account
        ),
        clock=lambda: NOW,
    )

    result = service.connect(
        type_value,
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.ok is True
    assert result.status == "connected_browser"
    assert result.route == "browser"
    assert oauth_calls == []
    assert browser_calls == [(expected_type, "user@example.com")]


def test_connection_service_preserves_type_when_constructing_browser_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    captured: dict[str, object] = {}

    monkeypatch.setattr(connections_module, "PlaywrightBrowserDriver", lambda: object())

    def construct_adapter(driver: object, **kwargs: object) -> object:
        captured.update({"driver": driver, **kwargs})
        return object()

    monkeypatch.setattr(connections_module, "Microsoft365BrowserAdapter", construct_adapter)
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        clock=lambda: NOW,
    )

    service._browser_adapter(ConnectionType.OUTLOOK, "user@example.com")

    assert captured["connection_type"] is ConnectionType.OUTLOOK


def test_failed_headless_persistence_check_prevents_connected_profile_save(tmp_path: Path) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_BROWSER_FALLBACK": "true",
        }
    )
    profiles = ProfileStore.from_settings(settings)
    browser_profile = tmp_path / "browser-profile"
    browser_profile.mkdir(mode=0o700)
    retained_marker = browser_profile / "persisted-session-marker"
    retained_marker.write_text("retained", encoding="utf-8")
    driver = FakeBrowserDriver(
        [
            authenticated_snapshot("user@example.com"),
            BrowserSnapshot(
                "https://login.microsoftonline.com/",
                "Sign in",
                (),
                True,
                False,
                False,
            ),
        ],
        outlook_identity_outcomes=["user@example.com"],
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=browser_profile,
        expected_account="user@example.com",
    )
    service = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        browser_connector=lambda type_value, account: adapter.interactive_login().account_id or "",
        clock=lambda: NOW,
    )

    result = service.connect(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.ok is False
    assert result.error is not None and result.error.code == "browser_login_required"
    assert result.error.remediation == (
        "solercia-connect connect outlook user@example.com --allow-browser-fallback",
    )
    assert profiles.get(ConnectionType.OUTLOOK, "user@example.com") is None
    assert retained_marker.read_text(encoding="utf-8") == "retained"


def test_browser_selector_mismatch_has_safe_executable_retry_and_status_paths(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_BROWSER_FALLBACK": "true",
        }
    )
    profiles = ProfileStore.from_settings(settings)
    raw_hint = "Accounts: user@example.com and unexpected-account@example.com"
    driver = FakeBrowserDriver(
        [
            BrowserSnapshot(
                "https://teams.microsoft.com/v2/",
                "Teams",
                (raw_hint,),
                False,
                False,
                True,
            )
        ]
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "browser-profile",
        expected_account="user@example.com",
    )
    service = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        browser_connector=lambda type_value, account: adapter.interactive_login().account_id or "",
        clock=lambda: NOW,
    )

    result = service.connect(
        "teams",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "browser_selector_mismatch"
    assert result.error.retryable is True
    assert result.error.remediation == (
        "solercia-connect connect teams user@example.com --allow-browser-fallback",
        "solercia-connect status teams user@example.com --allow-browser-fallback",
    )
    assert raw_hint not in str(result.to_dict())
    assert "unexpected-account@example.com" not in str(result.to_dict())
    assert profiles.get(ConnectionType.TEAMS, "user@example.com") is None


def test_browser_status_reopens_saved_profile_headless_and_closes(tmp_path: Path) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    profiles = ProfileStore.from_settings(settings)
    profiles.save(
        ConnectionProfile.create(
            ConnectionType.OUTLOOK,
            "user@example.com",
            route="browser",
            now=NOW,
        )
    )
    browser_profile = tmp_path / "browser-profile"
    driver = FakeBrowserDriver(
        [authenticated_snapshot("user@example.com")],
        outlook_identity_outcomes=["user@example.com"],
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=browser_profile,
        expected_account="user@example.com",
    )
    service = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        browser_status=lambda type_value, account: adapter.health().account_id or "",
        clock=lambda: NOW,
    )

    result = service.status(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.status == "connected_browser"
    assert driver.open_calls == [(browser_profile, True)]
    assert driver.close_calls == 1


def test_browser_status_login_required_names_executable_browser_reconnect(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    profiles = ProfileStore.from_settings(settings)
    profiles.save(
        ConnectionProfile.create(
            ConnectionType.OUTLOOK,
            "user@example.com",
            route="browser",
            now=NOW,
        )
    )
    service = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        browser_status=lambda type_value, account: (_ for _ in ()).throw(
            ConnectorFailure(
                ReasonCode.BROWSER_LOGIN_REQUIRED,
                "Microsoft 365 browser session requires interactive login",
            )
        ),
        clock=lambda: NOW,
    )

    result = service.status(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.status == "action_required"
    assert result.error is not None
    assert result.error.remediation == (
        "solercia-connect connect outlook user@example.com --allow-browser-fallback",
    )


def test_connect_missing_microsoft_client_id_with_global_fallback_only_has_zero_side_effects(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_BROWSER_FALLBACK": "true",
        }
    )
    oauth_calls: list[str] = []
    browser_calls: list[str] = []
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        oauth_builder=lambda type_value, account: oauth_calls.append(account),
        browser_connector=lambda type_value, account: browser_calls.append(account) or account,
        clock=lambda: NOW,
    )

    result = service.connect("outlook", "user@example.com")

    assert result.status == "action_required"
    assert result.error is not None
    assert result.error.code == "action_required"
    assert result.error.retryable is True
    assert result.error.missing_configuration == ("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID",)
    assert result.error.remediation == (
        "solercia-connect connect outlook user@example.com --allow-browser-fallback",
    )
    assert oauth_calls == []
    assert browser_calls == []


def test_connect_missing_microsoft_client_id_with_invocation_gate_only_has_zero_side_effects(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        }
    )
    oauth_calls: list[str] = []
    browser_calls: list[str] = []
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        oauth_builder=lambda type_value, account: oauth_calls.append(account),
        browser_connector=lambda type_value, account: browser_calls.append(account) or account,
        clock=lambda: NOW,
    )

    result = service.connect(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.status == "configuration_error"
    assert result.error is not None
    assert result.error.code == "configuration_invalid"
    assert result.error.remediation == (
        "SOLERCIA_CONNECT_BROWSER_FALLBACK=true solercia-connect connect outlook "
        "user@example.com --allow-browser-fallback",
    )
    assert oauth_calls == []
    assert browser_calls == []


def test_connect_configured_microsoft_route_stays_official_first(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    tokens = MemoryTokenStore()
    oauth = FakeMicrosoftOAuth(tokens, "outlook:user@example.com")
    oauth_calls: list[tuple[ConnectionType, str]] = []

    def build_oauth(type_value: ConnectionType, account: str) -> FakeMicrosoftOAuth:
        oauth_calls.append((type_value, account))
        return oauth

    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        tokens,
        oauth_builder=build_oauth,
        identity_verifier=lambda type_value, account, auth: account,
        browser_connector=lambda type_value, account: pytest.fail(
            "Browser fallback must not preempt a configured successful official route"
        ),
        clock=lambda: NOW,
    )

    result = service.connect(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.status == "connected_official"
    assert result.route == "official"
    assert oauth_calls == [(ConnectionType.OUTLOOK, "user@example.com")]


@pytest.mark.parametrize(
    "reason",
    [
        ReasonCode.INVALID_CREDENTIALS,
        ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
        ReasonCode.TENANT_POLICY_BLOCKED,
        ReasonCode.AUTHORIZATION_DENIED,
        ReasonCode.UNKNOWN_ERROR,
    ],
)
def test_connect_non_allowlisted_official_failures_never_use_browser(
    reason: ReasonCode,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    tokens = MemoryTokenStore()
    oauth = FakeMicrosoftOAuth(
        tokens,
        "outlook:user@example.com",
        ConnectorFailure(reason, "Safe official failure"),
    )
    browser_calls: list[str] = []
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        tokens,
        oauth_builder=lambda type_value, account: oauth,
        browser_connector=lambda type_value, account: browser_calls.append(account) or account,
        clock=lambda: NOW,
    )

    result = service.connect(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == reason.value
    assert browser_calls == []


def test_browser_fallback_is_explicit_allowlisted_and_declares_its_capabilities(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    tokens = MemoryTokenStore()
    oauth = FakeMicrosoftOAuth(
        tokens,
        "outlook:user@example.com",
        ConnectorFailure(ReasonCode.ADMIN_CONSENT_REQUIRED, "Administrator consent is required"),
    )
    browser_calls: list[tuple[str, str]] = []
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        tokens,
        oauth_builder=lambda type, account: oauth,
        identity_verifier=lambda type, account, auth: account,
        browser_connector=lambda type, account: (
            browser_calls.append((type.value, account)) or account
        ),
        clock=lambda: NOW,
    )

    denied = service.connect("outlook", "user@example.com")
    connected = service.connect("outlook", "user@example.com", allow_browser_fallback=True)

    assert denied.ok is False
    assert browser_calls == [("outlook", "user@example.com")]
    assert connected.status == "connected_browser"
    assert connected.data["extraction_status"] == "available"
    assert connected.data["capabilities"] == [
        "mail.sent.list",
        "mail.inbox.list",
        "calendar.events.list",
    ]


def test_teams_personal_audience_is_blocked_without_fallback(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        SOLERCIA_CONNECT_MICROSOFT_TENANT="consumers",
        SOLERCIA_CONNECT_BROWSER_FALLBACK="true",
    )
    browser_calls: list[str] = []
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        oauth_builder=lambda type, account: pytest.fail("OAuth must not start"),
        identity_verifier=lambda type, account, auth: account,
        browser_connector=lambda type, account: browser_calls.append(account) or account,
        clock=lambda: NOW,
    )

    result = service.connect("teams", "user@example.com", allow_browser_fallback=True)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "personal_account_unsupported"
    assert browser_calls == []


def test_google_connect_uses_injected_bounded_callback_flow(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    tokens = MemoryTokenStore()
    callback_calls: list[bool] = []
    service = ConnectionService(
        settings,
        profiles,
        tokens,
        oauth_builder=lambda type, account: FakeGoogleOAuth(),
        identity_verifier=lambda type, account, auth: account,
        google_callback_flow=lambda oauth, open_browser: callback_calls.append(open_browser),
        clock=lambda: NOW,
    )

    result = service.connect("gmail", "user@example.com", open_browser=False)

    assert result.status == "connected_official"
    assert callback_calls == [False]
    assert profiles.get("gmail", "user@example.com") is not None


def test_status_action_and_disconnect_have_exact_local_lifecycle(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    tokens = MemoryTokenStore()
    service = ConnectionService(
        settings,
        profiles,
        tokens,
        oauth_builder=lambda type, account: FakeGoogleOAuth(),
        identity_verifier=lambda type, account, auth: account,
        clock=lambda: NOW,
    )

    status = service.status("google-calendar", "user@example.com")
    assert status.status == "action_required"
    assert status.error is not None
    assert status.error.remediation == (
        "solercia-connect connect google-calendar user@example.com",
    )

    profiles.save(
        __import__(
            "solercia_connect.config", fromlist=["ConnectionProfile"]
        ).ConnectionProfile.create("google-calendar", "user@example.com", now=NOW)
    )
    disconnected = service.disconnect("google-calendar", "user@example.com")

    assert disconnected.ok is True
    assert disconnected.data["remote_revocation"] == "not_performed"
    assert tokens.deleted == ["google-calendar:user@example.com"]
    assert profiles.get("google-calendar", "user@example.com") is None


def test_connect_reports_non_oauth_targets_and_missing_configuration(tmp_path: Path) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        }
    )
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        oauth_builder=lambda type, account: pytest.fail("OAuth must not start"),
        clock=lambda: NOW,
    )

    jira = service.connect("jira", "user@example.com")
    outlook = service.connect("outlook", "user@example.com")

    assert jira.error is not None and jira.error.code == "capability_not_implemented"
    assert outlook.status == "configuration_error"
    assert outlook.error is not None
    assert outlook.error.missing_configuration == ("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID",)


def test_connect_removes_mismatched_token_and_maps_safe_failure_classes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    tokens = MemoryTokenStore()
    oauth = FakeMicrosoftOAuth(tokens, "outlook:user@example.com")
    service = ConnectionService(
        settings,
        profiles,
        tokens,
        oauth_builder=lambda type, account: oauth,
        identity_verifier=lambda type, account, auth: "other@example.com",
        clock=lambda: NOW,
    )

    mismatch = service.connect("outlook", "user@example.com")

    assert mismatch.error is not None
    assert mismatch.error.code == "authorization_boundary_violation"
    assert tokens.deleted == ["outlook:user@example.com"]
    assert profiles.get("outlook", "user@example.com") is None

    invalid_service = ConnectionService(
        settings,
        profiles,
        tokens,
        oauth_builder=lambda type, account: (_ for _ in ()).throw(ValueError("bad config")),
        clock=lambda: NOW,
    )
    unknown_service = ConnectionService(
        settings,
        profiles,
        tokens,
        oauth_builder=lambda type, account: (_ for _ in ()).throw(RuntimeError("private")),
        clock=lambda: NOW,
    )
    invalid = invalid_service.connect("gmail", "user@example.com", open_browser=False)
    unknown = unknown_service.connect("gmail", "user@example.com", open_browser=False)
    assert invalid.error is not None and invalid.error.code == "configuration_invalid"
    assert unknown.error is not None and unknown.error.code == "unknown_error"
    assert "private" not in unknown.error.message


def test_browser_connection_status_and_mismatch_are_identity_bound(tmp_path: Path) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    profiles = ProfileStore.from_settings(settings)
    tokens = MemoryTokenStore()
    profiles.save(
        ConnectionProfile.create(
            ConnectionType.TEAMS,
            "user@example.com",
            route="browser",
            now=NOW,
        )
    )
    service = ConnectionService(
        settings,
        profiles,
        tokens,
        browser_status=lambda type, account: account,
        clock=lambda: NOW,
    )

    connected = service.status(
        "teams",
        "user@example.com",
        allow_browser_fallback=True,
    )
    assert connected.status == "connected_browser"
    # The Teams client extracts nothing over this route. Transcripts are readable, but from the
    # drive that stores the recording, which is the onedrive target's surface.
    assert connected.data["extraction_status"] == "not_implemented"

    mismatch_service = ConnectionService(
        settings,
        profiles,
        tokens,
        browser_status=lambda type, account: "other@example.com",
        clock=lambda: NOW,
    )
    mismatch = mismatch_service.status(
        "teams",
        "user@example.com",
        allow_browser_fallback=True,
    )
    assert mismatch.error is not None and mismatch.error.code == "browser_account_mismatch"


def test_browser_status_invocation_gate_cannot_bypass_disabled_global_fallback(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    profiles.save(
        ConnectionProfile.create(
            ConnectionType.OUTLOOK,
            "user@example.com",
            route="browser",
            now=NOW,
        )
    )
    browser_calls: list[str] = []
    service = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        browser_status=lambda type_value, account: browser_calls.append(account) or account,
        clock=lambda: NOW,
    )

    result = service.status(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.status == "configuration_error"
    assert result.error is not None
    assert result.error.code == "configuration_invalid"
    assert browser_calls == []


def test_official_status_checks_scopes_refresh_identity_and_connection_test(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    tokens = MemoryTokenStore()
    profiles.save(ConnectionProfile.create("gmail", "user@example.com", now=NOW))
    tokens.values["gmail:user@example.com"] = OAuthTokenSet(
        SecretValue("access"),
        SecretValue("refresh"),
        NOW + timedelta(hours=1),
        (
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.readonly",
        ),
    )
    service = ConnectionService(
        settings,
        profiles,
        tokens,
        oauth_builder=lambda type, account: FakeGoogleOAuth(),
        identity_verifier=lambda type, account, auth: account,
        clock=lambda: NOW,
    )

    status = service.status("gmail", "user@example.com")
    tested = service.test_connection("gmail", "user@example.com")

    assert status.status == "connected_official"
    assert status.data["token_refresh_checked"] is True
    assert tested.ok is True and tested.operation == "connections.test"

    tokens.values["gmail:user@example.com"] = OAuthTokenSet(
        SecretValue("access"),
        None,
        NOW + timedelta(hours=1),
        ("openid",),
    )
    missing_scope = service.status("gmail", "user@example.com")
    assert missing_scope.error is not None
    assert missing_scope.error.code == "invalid_credentials"


def test_status_maps_refresh_action_configuration_and_unknown_failures(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    tokens = MemoryTokenStore()
    profiles.save(ConnectionProfile.create("outlook", "user@example.com", now=NOW))
    tokens.values["outlook:user@example.com"] = OAuthTokenSet(
        SecretValue("access"),
        SecretValue("refresh"),
        NOW + timedelta(hours=1),
        ("openid", "profile", "User.Read", "Mail.Read", "Calendars.Read"),
    )

    class BrokenOAuth:
        def authorization_header(self) -> str:
            raise ConnectorFailure(ReasonCode.INVALID_CREDENTIALS, "Refresh is required")

    action = ConnectionService(
        settings,
        profiles,
        tokens,
        oauth_builder=lambda type, account: BrokenOAuth(),
        clock=lambda: NOW,
    ).status("outlook", "user@example.com")
    invalid = ConnectionService(
        settings,
        profiles,
        tokens,
        oauth_builder=lambda type, account: (_ for _ in ()).throw(ValueError("bad settings")),
        clock=lambda: NOW,
    ).status("outlook", "user@example.com")
    unknown = ConnectionService(
        settings,
        profiles,
        tokens,
        oauth_builder=lambda type, account: (_ for _ in ()).throw(RuntimeError("private")),
        clock=lambda: NOW,
    ).status("outlook", "user@example.com")

    assert action.status == "action_required"
    assert invalid.error is not None and invalid.error.code == "configuration_invalid"
    assert unknown.error is not None and unknown.error.code == "unknown_error"


def test_noninteractive_connection_test_requires_cli_for_absent_or_browser_profile(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    service = ConnectionService(settings, profiles, MemoryTokenStore(), clock=lambda: NOW)

    absent = service.test_connection(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )
    profiles.save(
        ConnectionProfile.create(
            "outlook",
            "user@example.com",
            route="browser",
            now=NOW,
        )
    )
    browser = service.test_connection("outlook", "user@example.com")
    invalid = service.test_connection("unknown", "user@example.com")

    assert absent.status == "action_required"
    assert absent.error is not None
    assert absent.error.remediation[0].endswith("--allow-browser-fallback")
    assert browser.route == "browser"
    assert browser.data["interactive_flow_started"] is False
    assert invalid.status == "invalid_input"


def test_browser_fallback_identity_mismatch_never_saves_profile(tmp_path: Path) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    profiles = ProfileStore.from_settings(settings)
    failure = ConnectorFailure(
        ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE,
        "Official route cannot proceed",
    )
    oauth = FakeMicrosoftOAuth(MemoryTokenStore(), "outlook:user@example.com", failure)
    service = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        oauth_builder=lambda type, account: oauth,
        browser_connector=lambda type, account: "other@example.com",
        clock=lambda: NOW,
    )

    result = service.connect(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.error is not None and result.error.code == "browser_account_mismatch"
    assert profiles.get("outlook", "user@example.com") is None


def test_google_callback_flow_emits_action_and_exchanges_exact_correlated_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    actions: list[dict[str, Any]] = []
    calls: list[tuple[str, str, str]] = []
    pair = SimpleNamespace(
        state="state-value",
        challenge="challenge-value",
        verifier=SecretValue("verifier-value"),
    )

    class Callback:
        redirect_uri = "http://127.0.0.1:8765/oauth/callback"

        def __init__(self, state: str) -> None:
            assert state == "state-value"

        def __enter__(self) -> Callback:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def wait(self) -> SimpleNamespace:
            return SimpleNamespace(code=SecretValue("authorization-code"))

    class GoogleFlowOAuth:
        def authorization_url(self, **values: str) -> str:
            assert values == {
                "redirect_uri": Callback.redirect_uri,
                "state": "state-value",
                "code_challenge": "challenge-value",
            }
            return "https://accounts.example/authorize"

        def exchange_authorization_code(
            self,
            code: SecretValue,
            *,
            redirect_uri: str,
            code_verifier: SecretValue,
        ) -> None:
            calls.append((code.reveal(), redirect_uri, code_verifier.reveal()))

    monkeypatch.setattr(connections_module, "build_pkce_pair", lambda: pair)
    monkeypatch.setattr(connections_module, "LoopbackCallbackServer", Callback)
    monkeypatch.setattr(
        connections_module.webbrowser,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ignored")),
    )
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        action_sink=lambda envelope: actions.append(envelope.to_dict()),
        clock=lambda: NOW,
    )

    service._run_google_callback(GoogleFlowOAuth(), True)  # type: ignore[arg-type]

    assert actions[0]["status"] == "action_required"
    assert actions[0]["data"]["authorization_url"] == "https://accounts.example/authorize"
    assert calls == [
        (
            "authorization-code",
            "http://127.0.0.1:8765/oauth/callback",
            "verifier-value",
        )
    ]


def test_oauth_factory_enforces_provider_specific_configuration_and_scopes(
    tmp_path: Path,
) -> None:
    store = MemoryTokenStore()
    settings = _settings(tmp_path)

    microsoft = oauth_provider_from_settings(
        settings,
        store,
        ConnectionType.TEAMS,
        "user@example.com",
    )
    google = oauth_provider_from_settings(
        settings,
        store,
        ConnectionType.GOOGLE_CALENDAR,
        "user@example.com",
    )
    meet = oauth_provider_from_settings(
        settings,
        store,
        ConnectionType.GOOGLE_MEET,
        "user@example.com",
    )

    assert microsoft is not None and google is not None and meet is not None
    assert isinstance(meet, GoogleOAuthProvider)
    assert meet._store_key == "google-meet:user@example.com"
    assert meet._store_key != google._store_key
    assert meet._scopes == (
        "openid",
        "email",
        "https://www.googleapis.com/auth/calendar.events.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    )
    with pytest.raises(ValueError, match="does not use"):
        oauth_provider_from_settings(settings, store, ConnectionType.JIRA, "user@example.com")

    missing = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "missing-config"),
            "XDG_DATA_HOME": str(tmp_path / "missing-data"),
        }
    )
    with pytest.raises(ValueError, match="Microsoft client"):
        oauth_provider_from_settings(missing, store, ConnectionType.OUTLOOK, "user@example.com")
    with pytest.raises(ValueError, match="Google client"):
        oauth_provider_from_settings(missing, store, ConnectionType.GMAIL, "user@example.com")


def test_connect_browser_goes_straight_to_the_supervised_browser_and_saves_the_profile(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    profiles = ProfileStore.from_settings(settings)
    browser_calls: list[tuple[ConnectionType, str]] = []
    service = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        oauth_builder=lambda type, account: pytest.fail(
            "HTTP browser login never starts device-code OAuth"
        ),
        browser_connector=lambda type, account: browser_calls.append((type, account)) or account,
        clock=lambda: NOW,
    )

    result = service.connect_browser("outlook", "User@Example.com")

    assert result.ok is True
    assert result.operation == "connections.connect"
    assert result.status == "connected_browser"
    assert result.route == "browser"
    assert result.data["identity_verified"] is True
    assert browser_calls == [(ConnectionType.OUTLOOK, "user@example.com")]
    profile = profiles.get("outlook", "user@example.com")
    assert profile is not None and profile.route == "browser"


def test_connect_browser_rejects_non_microsoft_targets_and_a_disabled_global_gate(
    tmp_path: Path,
) -> None:
    enabled = _settings(tmp_path / "enabled", SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    disabled = _settings(tmp_path / "disabled")
    browser_calls: list[str] = []

    def service_for(settings: Any) -> ConnectionService:
        return ConnectionService(
            settings,
            ProfileStore.from_settings(settings),
            MemoryTokenStore(),
            browser_connector=lambda type, account: browser_calls.append(account) or account,
            clock=lambda: NOW,
        )

    gmail = service_for(enabled).connect_browser("gmail", "user@example.com")
    jira = service_for(enabled).connect_browser("jira", "user@example.com")
    invalid = service_for(enabled).connect_browser("outlook", "not-an-account")
    off = service_for(disabled).connect_browser("outlook", "user@example.com")

    assert gmail.ok is False
    assert gmail.error is not None and gmail.error.code == "capability_not_implemented"
    assert jira.error is not None and jira.error.code == "capability_not_implemented"
    assert invalid.status == "invalid_input"
    assert off.status == "configuration_error"
    assert off.error is not None and off.error.code == "configuration_invalid"
    assert off.error.missing_configuration == ("SOLERCIA_CONNECT_BROWSER_FALLBACK",)
    assert off.data["interactive_flow_started"] is False
    assert browser_calls == []
    assert ProfileStore.from_settings(disabled).get("outlook", "user@example.com") is None


def test_connect_browser_identity_mismatch_and_unexpected_errors_stay_safe(tmp_path: Path) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    profiles = ProfileStore.from_settings(settings)

    def broken(type: ConnectionType, account: str) -> str:
        raise RuntimeError("sensitive playwright detail")

    mismatch = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        browser_connector=lambda type, account: "other@example.com",
        clock=lambda: NOW,
    ).connect_browser("outlook", "user@example.com")
    unknown = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        browser_connector=broken,
        clock=lambda: NOW,
    ).connect_browser("outlook", "user@example.com")

    assert mismatch.operation == "connections.connect"
    assert mismatch.error is not None and mismatch.error.code == "browser_account_mismatch"
    assert unknown.operation == "connections.connect"
    assert unknown.error is not None and unknown.error.code == "unknown_error"
    assert "sensitive" not in unknown.error.message
    assert profiles.get("outlook", "user@example.com") is None


def test_connection_test_verifies_a_browser_profile_headless_when_both_gates_allow_it(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    profiles = ProfileStore.from_settings(settings)
    status_calls: list[tuple[ConnectionType, str]] = []
    service = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        browser_connector=lambda type, account: pytest.fail(
            "A connection test never opens a headed browser"
        ),
        browser_status=lambda type, account: status_calls.append((type, account)) or account,
        clock=lambda: NOW,
    )

    absent = service.test_connection("outlook", "user@example.com", allow_browser_fallback=True)
    profiles.save(ConnectionProfile.create("outlook", "user@example.com", route="browser", now=NOW))
    without_gate = service.test_connection("outlook", "user@example.com")
    calls_before_verification = list(status_calls)
    verified = service.test_connection("outlook", "user@example.com", allow_browser_fallback=True)

    assert absent.status == "action_required"
    assert absent.error is not None
    assert absent.error.remediation == (
        "solercia-connect connect outlook user@example.com --allow-browser-fallback",
        'POST /v1/connections/connect {"type":"outlook","account":"user@example.com",'
        '"allow_browser_fallback":true}',
    )
    assert without_gate.status == "action_required"
    assert without_gate.error is not None
    assert without_gate.error.remediation == ("solercia-connect connect outlook user@example.com",)
    assert calls_before_verification == []
    assert verified.ok is True
    assert verified.operation == "connections.test"
    assert verified.status == "connected_browser"
    assert verified.route == "browser"
    assert verified.data["identity_verified"] is True
    assert status_calls == [(ConnectionType.OUTLOOK, "user@example.com")]


def test_connection_test_browser_profile_maps_expired_login_to_action_required(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    profiles = ProfileStore.from_settings(settings)
    profiles.save(ConnectionProfile.create("outlook", "user@example.com", route="browser", now=NOW))

    def expired(type: ConnectionType, account: str) -> str:
        raise ConnectorFailure(ReasonCode.BROWSER_LOGIN_REQUIRED, "Browser session expired")

    service = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        browser_status=expired,
        clock=lambda: NOW,
    )

    result = service.test_connection("outlook", "user@example.com", allow_browser_fallback=True)

    assert result.ok is False
    assert result.operation == "connections.test"
    assert result.status == "action_required"
    assert result.error is not None and result.error.code == "action_required"
    assert result.error.remediation == (
        "solercia-connect connect outlook user@example.com --allow-browser-fallback",
    )
