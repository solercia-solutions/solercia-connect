from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import solercia_connect.providers.m365_browser.driver as driver_module
from solercia_connect.config import ConnectionProfile, ConnectionType, ProfileStore, load_settings
from solercia_connect.connections import ConnectionService
from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.m365_browser import Microsoft365BrowserAdapter
from solercia_connect.providers.m365_browser.driver import (
    BrowserSnapshot,
    PlaywrightBrowserDriver,
)
from solercia_connect.providers.m365_browser.selectors import ENDPOINTS, SELECTORS
from tests.fakes import NOW, FakeBrowserDriver, authenticated_snapshot

_TOKEN = "opaque-browser-bearer-that-must-never-escape"
_OUTLOOK_ME = "https://outlook.office.com/api/v2.0/me"


class _MemoryTokenStore:
    def load(self, key: str) -> None:
        del key
        return None

    def save(self, key: str, tokens: object) -> None:
        del key, tokens

    def delete(self, key: str) -> None:
        del key


def _settings(tmp_path: Path, *, browser_enabled: bool = True):
    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    if browser_enabled:
        values["SOLERCIA_CONNECT_BROWSER_FALLBACK"] = "true"
    return load_settings(environ=values)


def _outlook_shell() -> BrowserSnapshot:
    return BrowserSnapshot(
        "https://outlook.cloud.microsoft/mail/sentitems/",
        "Outlook",
        (),
        False,
        False,
        True,
    )


def _connect_with_driver(tmp_path: Path, driver: FakeBrowserDriver):
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    profile_dir = tmp_path / "browser-profile"
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=profile_dir,
        expected_account="user@example.com",
        state_poll_ms=7,
        identity_attestation_cycles=2,
    )
    service = ConnectionService(
        settings,
        profiles,
        _MemoryTokenStore(),
        browser_connector=lambda type_value, account: adapter.interactive_login().account_id or "",
        clock=lambda: NOW,
    )
    result = service.connect(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )
    return result, profiles, profile_dir


def test_live_shape_zero_dom_signals_with_rest_me_connects_and_returns(tmp_path: Path) -> None:
    driver = FakeBrowserDriver(
        [_outlook_shell(), _outlook_shell()],
        outlook_identity_outcomes=["USER@Example.com", "user@example.com"],
    )

    result, profiles, profile_dir = _connect_with_driver(tmp_path, driver)

    assert result.ok is True
    assert result.status == "connected_browser"
    assert result.data == {
        "identity_verified": True,
        "session_status": "connected_browser",
        "extraction_status": "available",
        "capabilities": ["mail.sent.list"],
    }
    assert driver.open_calls == [(profile_dir, False), (profile_dir, True)]
    assert driver.open_connection_types == [ConnectionType.OUTLOOK, ConnectionType.OUTLOOK]
    assert driver.outlook_identity_calls == [
        (7, 2, 2, 30_000),
        (7, 2, 2, 30_000),
    ]
    assert driver.reveal_calls == []
    assert driver.close_calls == 2
    assert profiles.get("outlook", "user@example.com") is not None


def test_outlook_never_uses_dom_identity_attestation(tmp_path: Path) -> None:
    driver = FakeBrowserDriver(
        [_outlook_shell()],
        reveal_result=False,
        outlook_identity_outcomes=["user@example.com"],
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
        state_poll_ms=3,
        identity_attestation_cycles=4,
    )

    report = adapter.health()

    assert report.account_id == "user@example.com"
    assert driver.outlook_identity_calls == [(3, 4, 2, 30_000)]
    assert driver.snapshot_connection_types == [ConnectionType.OUTLOOK]
    assert driver.reveal_calls == []


def test_mismatched_rest_me_fails_closed_without_profile_save(tmp_path: Path) -> None:
    driver = FakeBrowserDriver(
        [_outlook_shell()],
        outlook_identity_outcomes=["other@example.com"],
    )

    result, profiles, _ = _connect_with_driver(tmp_path, driver)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "browser_account_mismatch"
    assert profiles.get("outlook", "user@example.com") is None
    assert driver.close_calls == 1


@pytest.mark.parametrize(
    "identity",
    [None, "", "Display Name", "first@example.com and second@example.com"],
    ids=["missing", "empty", "malformed", "ambiguous"],
)
def test_invalid_rest_me_email_is_typed_identity_unavailable_and_not_saved(
    identity: object,
    tmp_path: Path,
) -> None:
    driver = FakeBrowserDriver(
        [_outlook_shell()],
        outlook_identity_outcomes=[identity],
    )

    result, profiles, _ = _connect_with_driver(tmp_path, driver)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "browser_identity_unavailable"
    assert result.error.code != "browser_selector_mismatch"
    assert result.error.remediation == (
        "solercia-connect connect outlook user@example.com --allow-browser-fallback",
        "solercia-connect status outlook user@example.com --allow-browser-fallback",
    )
    assert profiles.get("outlook", "user@example.com") is None


def test_status_identity_unavailable_names_executable_connect_and_status_paths(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    profiles.save(ConnectionProfile.create("outlook", "user@example.com", route="browser", now=NOW))

    def unavailable(type_value: ConnectionType, account: str) -> str:
        del type_value, account
        raise ConnectorFailure(
            ReasonCode.BROWSER_IDENTITY_UNAVAILABLE,
            "Outlook session authorization was not observed within the bounded wait",
        )

    service = ConnectionService(
        settings,
        profiles,
        _MemoryTokenStore(),
        browser_status=unavailable,
        clock=lambda: NOW,
    )

    result = service.status(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.error is not None
    assert result.error.code == "browser_identity_unavailable"
    assert result.error.remediation == (
        "solercia-connect connect outlook user@example.com --allow-browser-fallback",
        "solercia-connect status outlook user@example.com --allow-browser-fallback",
    )


def test_teams_keeps_dom_identity_and_never_calls_outlook_probe(tmp_path: Path) -> None:
    driver = FakeBrowserDriver(
        [
            BrowserSnapshot(
                ENDPOINTS.teams_home,
                "Teams",
                (),
                False,
                False,
                True,
            ),
            authenticated_snapshot("USER@Example.com"),
        ],
        outlook_identity_outcomes=[AssertionError("Outlook probe must not run for Teams")],
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
        state_poll_ms=5,
        identity_attestation_cycles=1,
    )

    report = adapter.health()

    assert report.account_id == "user@example.com"
    assert driver.outlook_identity_calls == []
    assert driver.open_connection_types == [ConnectionType.TEAMS]
    assert driver.snapshot_connection_types == [ConnectionType.TEAMS, ConnectionType.TEAMS]
    assert driver.reveal_calls == [5]


class _FakeRequest:
    def __init__(self, url: str, authorization: object) -> None:
        self.url = url
        self._authorization = authorization

    def all_headers(self) -> dict[str, object]:
        return {"Authorization": self._authorization}


class _EventEmitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

    def on(self, event: str, callback: Any) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def emit_request(self, url: str, authorization: object) -> None:
        request = _FakeRequest(url, authorization)
        for callback in self.handlers.get("request", ()):
            callback(request)


class _ProbeControl:
    def __init__(self, *, visible: bool) -> None:
        self._visible = visible

    def count(self) -> int:
        return int(self._visible)

    def is_visible(self) -> bool:
        return self._visible


class _ProbeLocator:
    def __init__(self, *, visible: bool) -> None:
        self.first = _ProbeControl(visible=visible)


class _ProbePage(_EventEmitter):
    url = "https://outlook.cloud.microsoft/mail/sentitems/"

    def __init__(self, *results: object) -> None:
        super().__init__()
        self.results = deque(results)
        self.goto_calls: list[tuple[str, str, int]] = []
        self.wait_calls: list[int] = []
        self.probe_calls: list[dict[str, object]] = []
        self.probe_scripts: list[str] = []
        self.probe_params: list[dict[str, str] | None] = []
        self.probe_authorization_present: list[bool] = []
        self.requests_on_wait: deque[tuple[str, object]] = deque()
        self.request_on_goto: tuple[str, object] | None = None
        self.selector_calls: list[str] = []

    def title(self) -> str:
        return "Outlook"

    def locator(self, selector: str) -> _ProbeLocator:
        self.selector_calls.append(selector)
        return _ProbeLocator(visible=selector == SELECTORS.authenticated_shells[0])

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        if self.request_on_goto is not None:
            request_url, authorization = self.request_on_goto
            self.emit_request(request_url, authorization)

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.wait_calls.append(milliseconds)
        if self.requests_on_wait:
            url, authorization = self.requests_on_wait.popleft()
            self.emit_request(url, authorization)

    def evaluate(self, script: str, payload: dict[str, object]) -> object:
        raise AssertionError("Identity must not be probed from inside the page")


class _FakeResponse:
    def __init__(self, status: int, payload: object, *, malformed: bool = False) -> None:
        self.status = status
        self._payload = payload
        self._malformed = malformed
        self.disposed = False

    def json(self) -> object:
        if self._malformed:
            raise ValueError("response is not JSON")
        return self._payload

    def dispose(self) -> None:
        self.disposed = True


class _FakeAPIRequestContext:
    """Stands in for Playwright's request context, which is not bound by page CORS."""

    def __init__(self, page: _ProbePage) -> None:
        self._page = page

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None,
        timeout: int,
        fail_on_status_code: bool,
    ) -> _FakeResponse:
        assert fail_on_status_code is False
        authorization = headers.get("Authorization")
        self._page.probe_authorization_present.append(
            isinstance(authorization, str) and authorization.startswith("Bearer ")
        )
        self._page.probe_calls.append({"endpoint": url, "method": "GET", "timeoutMs": timeout})
        self._page.probe_params.append(params)
        if not self._page.results:
            raise AssertionError("Unexpected Outlook identity probe")
        result = self._page.results.popleft()
        assert isinstance(result, dict)
        kind = result.get("kind")
        if kind == "unavailable":
            raise RuntimeError("request could not be issued")
        if kind == "http_error":
            status = result.get("status")
            assert isinstance(status, int)
            return _FakeResponse(status, None)
        if kind == "malformed":
            return _FakeResponse(200, None, malformed=True)
        payload: dict[str, object] = {}
        if "emailAddress" in result:
            payload["EmailAddress"] = result["emailAddress"]
        return _FakeResponse(200, payload)


class _FakeContext(_EventEmitter):
    def __init__(self, page: _ProbePage) -> None:
        super().__init__()
        self.pages = [page]
        self.closed = False
        self.request = _FakeAPIRequestContext(page)

    def new_page(self) -> _ProbePage:
        return self.pages[0]

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, context: _FakeContext) -> None:
        self.context = context
        self.launch_calls: list[dict[str, object]] = []

    def launch_persistent_context(self, **kwargs: object) -> _FakeContext:
        self.launch_calls.append(kwargs)
        return self.context


class _FakePlaywright:
    def __init__(self, context: _FakeContext) -> None:
        self.chromium = _FakeChromium(context)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _PlaywrightSequence:
    def __init__(self, *contexts: _FakeContext) -> None:
        self.contexts = deque(contexts)
        self.instances: list[_FakePlaywright] = []

    def start(self) -> _FakePlaywright:
        if not self.contexts:
            raise AssertionError("Unexpected Playwright start")
        instance = _FakePlaywright(self.contexts.popleft())
        self.instances.append(instance)
        return instance


def _open_injected_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *probe_results: object,
) -> tuple[PlaywrightBrowserDriver, _ProbePage, _FakeContext, _FakePlaywright]:
    page = _ProbePage(*probe_results)
    context = _FakeContext(page)
    playwright = _FakePlaywright(context)
    starter = SimpleNamespace(start=lambda: playwright)
    monkeypatch.setattr(
        driver_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(sync_playwright=lambda: starter),
    )
    driver = PlaywrightBrowserDriver()
    driver.open_persistent(
        tmp_path / "profile",
        headless=True,
        connection_type=ConnectionType.OUTLOOK,
    )
    return driver, page, context, playwright


def test_live_shape_allowed_bearer_rest_me_reaches_connected_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        _ProbePage({"kind": "ok", "emailAddress": "USER@Example.com"}),
        _ProbePage({"kind": "ok", "emailAddress": "user@example.com"}),
    ]
    for page in pages:
        page.request_on_goto = (
            "https://outlook.cloud.microsoft/owa/service.svc?action=FindItem",
            f"Bearer {_TOKEN}",
        )
    contexts = [_FakeContext(page) for page in pages]
    sequence = _PlaywrightSequence(*contexts)
    monkeypatch.setattr(
        driver_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(sync_playwright=lambda: sequence),
    )
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    profile_dir = tmp_path / "browser-profile"
    adapter = Microsoft365BrowserAdapter(
        PlaywrightBrowserDriver(),
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=profile_dir,
        expected_account="user@example.com",
        state_poll_ms=1,
        identity_attestation_cycles=1,
        read_timeout_ms=11,
    )
    service = ConnectionService(
        settings,
        profiles,
        _MemoryTokenStore(),
        browser_connector=lambda type_value, account: adapter.interactive_login().account_id or "",
        clock=lambda: NOW,
    )

    result = service.connect(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert result.status == "connected_browser"
    assert profiles.get("outlook", "user@example.com") is not None
    assert [page.probe_calls for page in pages] == [
        [{"endpoint": _OUTLOOK_ME, "method": "GET", "timeoutMs": 11}],
        [{"endpoint": _OUTLOOK_ME, "method": "GET", "timeoutMs": 11}],
    ]
    assert all(
        not set(page.selector_calls).intersection(SELECTORS.identity_controls) for page in pages
    )
    assert all(context.closed for context in contexts)
    assert len(sequence.instances) == 2
    assert [
        instance.chromium.launch_calls[0]["user_data_dir"] for instance in sequence.instances
    ] == [str(profile_dir), str(profile_dir)]
    assert [instance.chromium.launch_calls[0]["headless"] for instance in sequence.instances] == [
        False,
        True,
    ]
    assert all(instance.stopped for instance in sequence.instances)


def test_request_hooks_exist_before_navigation_and_probe_exact_get_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver, page, context, playwright = _open_injected_driver(
        tmp_path,
        monkeypatch,
        {"kind": "ok", "emailAddress": "USER@Example.com"},
    )
    assert context.handlers["request"]
    assert page.handlers["request"]
    driver.navigate(ENDPOINTS.outlook_sent, timeout_ms=17)
    page.emit_request(
        "https://outlook.cloud.microsoft/owa/service.svc?action=FindItem",
        f"Bearer {_TOKEN}",
    )
    transient = driver._outlook_bearer

    identity = driver.outlook_session_identity(
        wait_interval_ms=5,
        max_wait_cycles=2,
        max_probe_attempts=1,
        probe_timeout_ms=19,
    )
    driver.close()

    assert identity == "user@example.com"
    assert page.probe_calls == [
        {
            "endpoint": _OUTLOOK_ME,
            "method": "GET",
            "timeoutMs": 19,
        }
    ]
    assert page.probe_authorization_present == [True]
    assert page.probe_params == [{"$select": "EmailAddress"}]
    assert transient is not None and transient.cleared is True
    assert context.closed is True
    assert playwright.stopped is True
    serialized_profile = json.dumps(
        ConnectionProfile.create("outlook", "user@example.com", route="browser").to_dict()
    )
    captured = capsys.readouterr()
    assert _TOKEN not in repr(driver)
    assert _TOKEN not in repr(transient)
    assert _TOKEN not in serialized_profile
    assert _TOKEN not in captured.out
    assert _TOKEN not in captured.err


def test_close_stops_playwright_even_when_context_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, _, context, playwright = _open_injected_driver(tmp_path, monkeypatch)

    def fail_close() -> None:
        context.closed = True
        raise RuntimeError("synthetic close failure")

    monkeypatch.setattr(context, "close", fail_close)

    with pytest.raises(RuntimeError, match="synthetic close failure"):
        driver.close()

    assert context.closed is True
    assert playwright.stopped is True
    assert driver._context is None
    assert driver._page is None
    assert driver._playwright is None


@pytest.mark.parametrize(
    "url",
    [
        "https://outlook.cloud.microsoft/owa/service.svc",
        "https://edge.outlook.cloud.microsoft/owa/service.svc",
        "https://outlook.office.com/owa/service.svc",
        "https://mail.outlook.office.com/owa/service.svc",
        "https://substrate.office.com/ows/v1.0/lightning/items/unseen",
        "https://api.substrate.office.com/owa/notificationchannel",
    ],
)
def test_authorization_capture_accepts_only_allowlisted_https_host_families(
    url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, _, context, _ = _open_injected_driver(tmp_path, monkeypatch)

    context.emit_request(url, f"bEaReR {_TOKEN}")

    assert driver._outlook_bearer is not None
    driver.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://outlook.office.com/owa/service.svc",
        "https://outlook.office.com.evil.example/owa/service.svc",
        "https://eviloutlook.office.com/owa/service.svc",
        "https://substrate.office.com.evil.example/owa/service.svc",
        "https://login.microsoftonline.com/common/oauth2/authorize",
        "https://outlook.cloud.microsoft@evil.example/owa/service.svc",
        "https://outlook.office.com:444/owa/service.svc",
    ],
)
def test_authorization_capture_rejects_http_lookalikes_login_and_nondefault_ports(
    url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, _, context, _ = _open_injected_driver(tmp_path, monkeypatch)

    context.emit_request(url, f"Bearer {_TOKEN}")

    assert driver._outlook_bearer is None
    driver.close()


@pytest.mark.parametrize(
    "authorization",
    [
        "Basic abc",
        "Bearer",
        "Bearer abc def",
        "Bearer abc\nX-Evil: value",
        "Token abc",
        "Bearer " + ("a" * 8193),
        123,
    ],
    ids=["basic", "missing", "space", "newline", "wrong-scheme", "oversized", "non-string"],
)
def test_authorization_capture_requires_bounded_well_formed_bearer(
    authorization: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, _, context, _ = _open_injected_driver(tmp_path, monkeypatch)

    context.emit_request("https://outlook.office.com/owa/service.svc", authorization)

    assert driver._outlook_bearer is None
    driver.close()


def test_token_wait_is_bounded_and_absence_is_identity_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, page, _, playwright = _open_injected_driver(tmp_path, monkeypatch)

    with pytest.raises(ConnectorFailure) as captured:
        driver.outlook_session_identity(
            wait_interval_ms=4,
            max_wait_cycles=2,
            max_probe_attempts=1,
            probe_timeout_ms=9,
        )
    driver.close()

    assert captured.value.reason.value == "browser_identity_unavailable"
    assert captured.value.reason is not ReasonCode.BROWSER_SELECTOR_MISMATCH
    assert page.wait_calls == [4, 4]
    assert page.probe_calls == []
    assert playwright.stopped is True


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_unauthorized_or_retired_me_endpoint_is_typed_and_token_is_cleared(
    status: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, page, context, _ = _open_injected_driver(
        tmp_path,
        monkeypatch,
        {"kind": "http_error", "status": status},
    )
    context.emit_request(
        "https://substrate.office.com/owa/notificationchannel",
        f"Bearer {_TOKEN}",
    )
    transient = driver._outlook_bearer

    with pytest.raises(ConnectorFailure) as captured:
        driver.outlook_session_identity(
            wait_interval_ms=2,
            max_wait_cycles=1,
            max_probe_attempts=1,
            probe_timeout_ms=9,
        )

    assert captured.value.reason.value == "browser_identity_unavailable"
    assert captured.value.reason is not ReasonCode.BROWSER_SELECTOR_MISMATCH
    assert transient is not None and transient.cleared is True
    assert len(page.probe_calls) == 1
    driver.close()


@pytest.mark.parametrize(
    "response",
    [
        {"kind": "ok"},
        {"kind": "ok", "emailAddress": None},
        {"kind": "ok", "emailAddress": "Display Name"},
        {"kind": "ok", "emailAddress": "first@example.com second@example.com"},
        {"kind": "malformed"},
    ],
    ids=["missing", "null", "malformed", "ambiguous", "non-object"],
)
def test_me_response_requires_one_pascal_case_canonical_email_address(
    response: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, _, context, _ = _open_injected_driver(tmp_path, monkeypatch, response)
    context.emit_request("https://outlook.office.com/owa/service.svc", f"Bearer {_TOKEN}")

    with pytest.raises(ConnectorFailure) as captured:
        driver.outlook_session_identity(
            wait_interval_ms=1,
            max_wait_cycles=1,
            max_probe_attempts=1,
            probe_timeout_ms=9,
        )

    assert captured.value.reason.value == "browser_identity_unavailable"
    assert captured.value.reason is not ReasonCode.BROWSER_SELECTOR_MISMATCH
    driver.close()


def test_probe_attempts_are_bounded_across_distinct_captured_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, page, context, _ = _open_injected_driver(
        tmp_path,
        monkeypatch,
        {"kind": "http_error", "status": 401},
        {"kind": "http_error", "status": 401},
    )
    context.emit_request("https://outlook.office.com/owa/first", "Bearer first-token")
    page.requests_on_wait.append(
        ("https://api.substrate.office.com/owa/second", "Bearer second-token")
    )

    with pytest.raises(ConnectorFailure) as captured:
        driver.outlook_session_identity(
            wait_interval_ms=1,
            max_wait_cycles=3,
            max_probe_attempts=2,
            probe_timeout_ms=9,
        )

    assert captured.value.reason.value == "browser_identity_unavailable"
    assert len(page.probe_calls) == 2
    assert page.wait_calls == [1]
    driver.close()


def test_both_browser_gates_and_noninteractive_http_boundary_remain_intact(
    tmp_path: Path,
) -> None:
    browser_calls: list[str] = []
    globally_enabled = _settings(tmp_path / "enabled")
    enabled_profiles = ProfileStore.from_settings(globally_enabled)
    enabled = ConnectionService(
        globally_enabled,
        enabled_profiles,
        _MemoryTokenStore(),
        browser_connector=lambda type_value, account: browser_calls.append(account) or account,
        browser_status=lambda type_value, account: browser_calls.append(account) or account,
        clock=lambda: NOW,
    )

    missing_invocation_gate = enabled.connect("outlook", "user@example.com")
    disabled = ConnectionService(
        _settings(tmp_path / "disabled", browser_enabled=False),
        ProfileStore.from_settings(_settings(tmp_path / "disabled", browser_enabled=False)),
        _MemoryTokenStore(),
        browser_connector=lambda type_value, account: browser_calls.append(account) or account,
        clock=lambda: NOW,
    )
    missing_global_gate = disabled.connect(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )
    enabled_profiles.save(
        ConnectionProfile.create("outlook", "user@example.com", route="browser", now=NOW)
    )
    http_result = enabled.test_connection(
        "outlook",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert missing_invocation_gate.ok is False
    assert missing_global_gate.ok is False
    assert http_result.status == "action_required"
    assert http_result.data["interactive_flow_started"] is False
    assert browser_calls == []
