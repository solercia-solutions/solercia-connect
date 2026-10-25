"""Vendor-neutral browser core: the discipline every family's browser route shares."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from solercia_connect.config import ConnectionType
from solercia_connect.domain import Capability, ProviderFamily, ReasonCode, RouteKind
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import BrowserProviderAdapter
from solercia_connect.providers import browser_core
from solercia_connect.providers.browser_core import session as session_module
from solercia_connect.providers.browser_core.login import (
    SessionPhase,
    close_session,
    poll_until_ready,
)
from solercia_connect.providers.browser_core.session import (
    MAX_BEARER_BYTES,
    PlaywrightSession,
    TransientBearer,
    authorization_header,
    is_allowed_request_url,
    launch_failure_message,
    parse_bearer,
)
from solercia_connect.providers.browser_core.states import BrowserState, BrowserStateMachine
from solercia_connect.providers.m365_browser import Microsoft365BrowserAdapter
from solercia_connect.providers.m365_browser import driver as m365_driver
from solercia_connect.providers.m365_browser import states as m365_states
from tests.fakes import FakeAdapter, FakeBrowserDriver

_SUFFIXES = ("example.com",)


# --- Host allowlist ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/rest",
        "https://api.example.com:8443/rest",
        "https://api.example.com:80/rest",
        "https://evil.test/rest",
        "https://example.com.evil.test/rest",
        "https://notexample.com/rest",
        "https://user:pass@api.example.com/rest",
        "https://api.example.com@evil.test/rest",
        "https://api.example.com:notaport/rest",
        "file:///etc/passwd",
    ],
)
def test_allowlist_rejects_other_hosts_non_https_and_non_default_ports(url: str) -> None:
    assert is_allowed_request_url(url, _SUFFIXES) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/rest",
        "https://api.example.com/rest",
        "https://API.Example.COM:443/rest",
        "https://api.example.com./rest",
    ],
)
def test_allowlist_accepts_https_hosts_on_or_below_a_suffix(url: str) -> None:
    assert is_allowed_request_url(url, _SUFFIXES) is True


def test_allowlist_without_suffixes_accepts_nothing() -> None:
    assert is_allowed_request_url("https://api.example.com/rest", ()) is False


# --- Transient bearer ----------------------------------------------------------------------


def test_transient_bearer_is_never_printable_and_is_zeroed_on_clear() -> None:
    bearer = TransientBearer("opaque-core-token")

    assert "opaque-core-token" not in repr(bearer)
    assert bearer.authorization_header() == "Bearer opaque-core-token"

    bearer.clear()

    assert bearer.cleared is True
    with pytest.raises(RuntimeError):
        bearer.authorization_header()


def test_bearer_parser_accepts_a_token_at_the_byte_cap() -> None:
    token = "a" * MAX_BEARER_BYTES

    captured = parse_bearer(f"Bearer {token}")

    assert captured is not None
    assert captured.authorization_header() == f"Bearer {token}"


def test_bearer_parser_refuses_a_token_over_the_byte_cap() -> None:
    assert parse_bearer(f"Bearer {'a' * (MAX_BEARER_BYTES + 1)}") is None


def test_bearer_parser_honours_an_explicit_cap() -> None:
    assert parse_bearer("Bearer abcdef", max_bytes=6) is not None
    assert parse_bearer("Bearer abcdefg", max_bytes=6) is None


@pytest.mark.parametrize(
    "value",
    [None, 42, "Basic dXNlcjpwYXNz", "Bearer ", "Bearer tok en", "Bearer tökén"],
)
def test_bearer_parser_refuses_anything_but_one_ascii_bearer(value: object) -> None:
    assert parse_bearer(value) is None


def test_authorization_header_is_read_case_insensitively_from_all_headers() -> None:
    request = SimpleNamespace(all_headers=lambda: {"AUTHORIZATION": "Bearer abc"})

    assert authorization_header(request) == "Bearer abc"


def test_authorization_header_falls_back_to_plain_headers_and_ignores_non_mappings() -> None:
    assert authorization_header(SimpleNamespace(headers={"authorization": "x"})) == "x"
    assert authorization_header(SimpleNamespace(headers=[("authorization", "x")])) is None
    assert authorization_header(SimpleNamespace(headers={"accept": "x"})) is None


# --- Launch, page acquisition and close ----------------------------------------------------


class _Emitter:
    def __init__(self) -> None:
        self.listeners: list[tuple[str, object]] = []

    def on(self, event: str, listener: object) -> None:
        self.listeners.append((event, listener))


class _Page(_Emitter):
    def __init__(self) -> None:
        super().__init__()
        self.goto_calls: list[tuple[str, str, int]] = []
        self.waits: list[int] = []

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


class _Response:
    def __init__(self, status: int, payload: object = None, *, malformed: bool = False) -> None:
        self.status = status
        self._payload = payload
        self._malformed = malformed
        self.disposed = False

    def json(self) -> object:
        if self._malformed:
            raise ValueError("not JSON")
        return self._payload

    def dispose(self) -> None:
        self.disposed = True


class _RequestAPI:
    def __init__(self, session: PlaywrightSession, *responses: _Response | Exception) -> None:
        self._session = session
        self.responses: deque[_Response | Exception] = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None,
        timeout: int,
        fail_on_status_code: bool,
    ) -> _Response:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout,
                "fail_on_status_code": fail_on_status_code,
                "issuing": self._session._issuing_session_request,
            }
        )
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


class _Context(_Emitter):
    def __init__(self, pages: list[_Page], *, request: object = None) -> None:
        super().__init__()
        self.pages = pages
        self.request = request
        self.close_calls = 0
        self.created_pages: list[_Page] = []

    def new_page(self) -> _Page:
        page = _Page()
        self.created_pages.append(page)
        return page

    def close(self) -> None:
        self.close_calls += 1


class _Chromium:
    def __init__(self, outcome: _Context | Exception) -> None:
        self._outcome = outcome
        self.launch_calls: list[dict[str, object]] = []

    def launch_persistent_context(self, **kwargs: object) -> _Context:
        self.launch_calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _Playwright:
    def __init__(self, outcome: _Context | Exception) -> None:
        self.chromium = _Chromium(outcome)
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


def _inject_playwright(monkeypatch: pytest.MonkeyPatch, playwright: _Playwright) -> None:
    starter = SimpleNamespace(start=lambda: playwright)
    monkeypatch.setattr(
        session_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(sync_playwright=lambda: starter),
    )


def test_launch_uses_the_audited_profile_refuses_downloads_and_reuses_the_open_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page()
    context = _Context([page])
    playwright = _Playwright(context)
    _inject_playwright(monkeypatch, playwright)
    session = PlaywrightSession()
    ready: list[object] = []

    session._open_persistent_context(
        tmp_path / "profile",
        headless=True,
        on_page=lambda: ready.append(session._page),
    )

    launch = playwright.chromium.launch_calls[0]
    assert launch == {
        "user_data_dir": str(tmp_path / "profile"),
        "headless": True,
        "accept_downloads": False,
    }
    assert (tmp_path / "profile").is_dir()
    assert ready == [page]
    assert context.created_pages == []


def test_launch_opens_a_page_when_the_context_has_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context([])
    _inject_playwright(monkeypatch, _Playwright(context))
    session = PlaywrightSession()

    session._open_persistent_context(tmp_path / "profile", headless=False)

    assert session._page is context.created_pages[0]


def test_launch_failure_stops_playwright_and_names_the_obstacle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playwright = _Playwright(RuntimeError("ProcessSingleton: SingletonLock is held"))
    _inject_playwright(monkeypatch, playwright)
    session = PlaywrightSession()

    with pytest.raises(ConnectorFailure) as raised:
        session._open_persistent_context(tmp_path / "profile", headless=True)

    assert raised.value.reason is ReasonCode.CONFIGURATION_INVALID
    assert "already in use" in raised.value.safe_message
    assert playwright.stop_calls == 1
    assert session._playwright is None


def test_missing_playwright_is_a_typed_configuration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(session_module.importlib, "import_module", missing)

    with pytest.raises(ConnectorFailure) as raised:
        PlaywrightSession()._open_persistent_context(tmp_path / "profile", headless=True)

    assert raised.value.reason is ReasonCode.CONFIGURATION_INVALID
    assert raised.value.safe_message == "Playwright optional dependency is not installed"


def test_page_setup_failure_closes_the_browser_before_failing_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context([_Page()])
    playwright = _Playwright(context)
    _inject_playwright(monkeypatch, playwright)
    session = PlaywrightSession()

    def broken() -> None:
        raise RuntimeError("observer could not attach")

    with pytest.raises(ConnectorFailure) as raised:
        session._open_persistent_context(tmp_path / "profile", headless=True, on_page=broken)

    assert raised.value.safe_message == "Playwright Chromium context could not initialize"
    assert (context.close_calls, playwright.stop_calls) == (1, 1)
    assert session._context is None


def test_launch_failure_message_separates_a_held_profile_from_a_missing_runtime() -> None:
    assert "already in use" in launch_failure_message(RuntimeError("profile lock"))
    assert "install the browser runtime" in launch_failure_message(RuntimeError("no chromium"))


def test_close_is_idempotent_and_releases_the_browser_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context([_Page()])
    playwright = _Playwright(context)
    _inject_playwright(monkeypatch, playwright)
    session = PlaywrightSession()
    session._open_persistent_context(tmp_path / "profile", headless=True)

    session.close()
    session.close()

    assert (context.close_calls, playwright.stop_calls) == (1, 1)
    assert (session._page, session._context, session._playwright) == (None, None, None)


def test_close_on_a_never_opened_session_does_nothing() -> None:
    PlaywrightSession().close()


class _DeafPage:
    def on(self, event: str, listener: object) -> None:
        raise KeyError(event)


def test_request_observer_is_installed_on_context_and_page_and_tolerates_failures() -> None:
    session = PlaywrightSession()
    context = _Context([])
    page = _Page()
    session._context = context
    session._page = page

    def observer(request: object) -> None:
        del request

    session._install_request_observer(observer)
    assert context.listeners == [("request", observer)]
    assert page.listeners == [("request", observer)]

    session._page = _DeafPage()
    session._install_request_observer(observer)
    assert len(context.listeners) == 2


def test_navigation_and_waits_need_an_open_page() -> None:
    session = PlaywrightSession()
    with pytest.raises(ConnectorFailure) as raised:
        session.navigate("https://example.com/", timeout_ms=10)
    assert raised.value.reason is ReasonCode.CONFIGURATION_INVALID

    page = _Page()
    session._page = page
    session.navigate("https://example.com/", timeout_ms=10)
    session.wait(5)

    assert page.goto_calls == [("https://example.com/", "domcontentloaded", 10)]
    assert page.waits == [5]


# --- Same-context REST read ----------------------------------------------------------------


def _session_with(*responses: _Response | Exception) -> tuple[PlaywrightSession, _RequestAPI]:
    session = PlaywrightSession()
    api = _RequestAPI(session, *responses)
    session._context = _Context([_Page()], request=api)
    return session, api


def _allowed(url: str) -> bool:
    return is_allowed_request_url(url, _SUFFIXES)


def test_rest_get_refuses_a_host_outside_the_allowlist_without_issuing_a_request() -> None:
    session, api = _session_with()

    outcome = session._rest_get(
        "https://evil.test/rest",
        TransientBearer("tok"),
        params=None,
        timeout_ms=10,
        allowed=_allowed,
    )

    assert outcome == {"kind": "unavailable"}
    assert api.calls == []


def test_rest_get_refuses_every_host_when_the_allowlist_is_empty() -> None:
    session, api = _session_with()

    outcome = session._rest_get(
        "https://api.example.com/rest",
        TransientBearer("tok"),
        params=None,
        timeout_ms=10,
        allowed=lambda url: is_allowed_request_url(url, ()),
    )

    assert outcome == {"kind": "unavailable"}
    assert api.calls == []


def test_rest_get_refuses_when_no_context_is_open() -> None:
    outcome = PlaywrightSession()._rest_get(
        "https://api.example.com/rest",
        TransientBearer("tok"),
        params=None,
        timeout_ms=10,
        allowed=_allowed,
    )

    assert outcome == {"kind": "unavailable"}


def test_rest_get_reads_through_the_context_with_a_transient_bearer() -> None:
    response = _Response(200, {"value": []})
    session, api = _session_with(response)

    outcome = session._rest_get(
        "https://api.example.com/rest",
        TransientBearer("tok"),
        params={"q": "1"},
        timeout_ms=10,
        allowed=_allowed,
        extra_headers={"Prefer": "x"},
    )

    assert outcome == {"kind": "ok", "payload": {"value": []}}
    assert api.calls == [
        {
            "url": "https://api.example.com/rest",
            "headers": {
                "Authorization": "Bearer tok",
                "Accept": "application/json",
                "Prefer": "x",
            },
            "params": {"q": "1"},
            "timeout": 10,
            "fail_on_status_code": False,
            "issuing": True,
        }
    ]
    assert session._issuing_session_request is False
    assert response.disposed is True


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_Response(403), {"kind": "http_error", "status": 403}),
        (_Response(200, malformed=True), {"kind": "malformed"}),
        (_Response(200, ["not", "a", "mapping"]), {"kind": "malformed"}),
        (RuntimeError("socket closed"), {"kind": "unavailable"}),
    ],
)
def test_rest_get_maps_every_other_outcome_to_a_typed_kind(
    response: _Response | Exception,
    expected: dict[str, object],
) -> None:
    session, _ = _session_with(response)

    outcome = session._rest_get(
        "https://api.example.com/rest",
        TransientBearer("tok"),
        params=None,
        timeout_ms=10,
        allowed=_allowed,
    )

    assert outcome == expected
    assert session._issuing_session_request is False


# --- Login/MFA poll loop -------------------------------------------------------------------


class _Probe:
    """Each observation is the phase itself; identity proof returns a fixed account."""

    def __init__(self, *phases: SessionPhase, account: str = "user@example.com") -> None:
        self.phases = deque(phases)
        self.account = account
        self.waits: list[int] = []
        self.attested = 0

    def observe(self) -> SessionPhase:
        return self.phases.popleft() if len(self.phases) > 1 else self.phases[0]

    def phase(self, snapshot: SessionPhase) -> SessionPhase:
        return snapshot

    def attest(self, machine: BrowserStateMachine, snapshot: SessionPhase) -> str:
        assert machine.state is BrowserState.AUTHENTICATED
        assert snapshot is SessionPhase.AUTHENTICATED
        self.attested += 1
        return self.account

    def wait(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


def _opened() -> BrowserStateMachine:
    machine = BrowserStateMachine()
    machine.advance(BrowserState.OPENING)
    return machine


def _poll(
    probe: _Probe,
    machine: BrowserStateMachine,
    *,
    interactive: bool = False,
    boundaries: tuple[str | None, ...] = (None,),
    cycles: int = 5,
) -> str:
    return poll_until_ready(
        machine,
        probe,
        interactive=interactive,
        product="Vendor Suite",
        boundaries=boundaries,
        max_poll_cycles=cycles,
        state_poll_ms=7,
    )


def test_poll_returns_the_attested_account_once_authenticated() -> None:
    machine = _opened()
    probe = _Probe(SessionPhase.PENDING, SessionPhase.AUTHENTICATED)

    account = _poll(probe, machine, boundaries=("user@example.com", None))

    assert account == "user@example.com"
    assert machine.state is BrowserState.READY
    assert probe.waits == [7]
    assert probe.attested == 1


@pytest.mark.parametrize(
    ("phase", "state", "reason", "message"),
    [
        (
            SessionPhase.LOGIN_REQUIRED,
            BrowserState.LOGIN_REQUIRED,
            ReasonCode.BROWSER_LOGIN_REQUIRED,
            "Vendor Suite browser session requires interactive login",
        ),
        (
            SessionPhase.MFA_REQUIRED,
            BrowserState.MFA_REQUIRED,
            ReasonCode.BROWSER_MFA_REQUIRED,
            "Vendor Suite browser session requires user MFA",
        ),
    ],
)
def test_headless_poll_fails_closed_when_the_user_must_act(
    phase: SessionPhase,
    state: BrowserState,
    reason: ReasonCode,
    message: str,
) -> None:
    machine = _opened()

    with pytest.raises(ConnectorFailure) as raised:
        _poll(_Probe(phase), machine)

    assert (raised.value.reason, raised.value.safe_message) == (reason, message)
    assert machine.state is state


def test_interactive_poll_waits_through_login_and_mfa() -> None:
    machine = _opened()
    probe = _Probe(
        SessionPhase.LOGIN_REQUIRED,
        SessionPhase.LOGIN_REQUIRED,
        SessionPhase.MFA_REQUIRED,
        SessionPhase.AUTHENTICATED,
    )

    assert _poll(probe, machine, interactive=True) == "user@example.com"
    assert machine.history == [
        BrowserState.NEW,
        BrowserState.OPENING,
        BrowserState.LOGIN_REQUIRED,
        BrowserState.MFA_REQUIRED,
        BrowserState.AUTHENTICATED,
        BrowserState.READY,
    ]


def test_poll_refuses_an_account_outside_any_boundary() -> None:
    machine = _opened()

    with pytest.raises(ConnectorFailure) as raised:
        _poll(
            _Probe(SessionPhase.AUTHENTICATED, account="other@example.com"),
            machine,
            boundaries=(None, "user@example.com"),
        )

    assert raised.value.reason is ReasonCode.BROWSER_ACCOUNT_MISMATCH
    assert raised.value.safe_message == (
        "Authenticated Vendor Suite account does not match the configured or requested account"
    )
    assert machine.state is BrowserState.ACCOUNT_MISMATCH


@pytest.mark.parametrize(
    ("phase", "reason"),
    [
        (SessionPhase.PENDING, ReasonCode.BROWSER_TIMEOUT),
        (SessionPhase.LOGIN_REQUIRED, ReasonCode.BROWSER_LOGIN_REQUIRED),
        (SessionPhase.MFA_REQUIRED, ReasonCode.BROWSER_MFA_REQUIRED),
    ],
)
def test_bounded_poll_ends_with_the_last_observed_obstacle(
    phase: SessionPhase,
    reason: ReasonCode,
) -> None:
    machine = _opened()
    probe = _Probe(phase)

    with pytest.raises(ConnectorFailure) as raised:
        _poll(probe, machine, interactive=True, cycles=3)

    assert raised.value.reason is reason
    assert raised.value.safe_message == "Vendor Suite browser state did not become ready in time"
    assert machine.state is BrowserState.FAILED
    assert probe.waits == [7, 7, 7]


@pytest.mark.parametrize(
    ("path", "history_tail"),
    [
        ((BrowserState.OPENING,), [BrowserState.FAILED, BrowserState.CLOSED]),
        (
            (BrowserState.OPENING, BrowserState.AUTHENTICATED),
            [BrowserState.FAILED, BrowserState.CLOSED],
        ),
        (
            (BrowserState.OPENING, BrowserState.AUTHENTICATED, BrowserState.READY),
            [BrowserState.READY, BrowserState.CLOSED],
        ),
    ],
)
def test_close_session_always_ends_closed(
    path: tuple[BrowserState, ...],
    history_tail: list[BrowserState],
) -> None:
    machine = BrowserStateMachine()
    for state in path:
        machine.advance(state)
    closed: list[bool] = []

    close_session(machine, lambda: closed.append(True))
    close_session(machine, lambda: closed.append(True))

    assert machine.history[-2:] == history_tail
    assert closed == [True, True]


def test_close_session_ends_closed_even_when_the_driver_close_fails() -> None:
    machine = _opened()

    def failing() -> None:
        raise RuntimeError("browser already gone")

    with pytest.raises(RuntimeError):
        close_session(machine, failing)

    assert machine.state is BrowserState.CLOSED


# --- Seams: Microsoft keeps its names, and the port fits it ----------------------------------


def test_microsoft_modules_re_export_the_core_objects_under_their_old_names() -> None:
    assert m365_states.BrowserState is BrowserState
    assert m365_states.BrowserStateMachine is BrowserStateMachine
    assert m365_driver._TransientBearer is TransientBearer
    assert m365_driver._parse_bearer is parse_bearer
    assert m365_driver._is_allowed_request_url is is_allowed_request_url
    assert m365_driver._authorization_header is authorization_header
    assert m365_driver._launch_failure_message is launch_failure_message
    assert m365_driver._MAX_OUTLOOK_BEARER_BYTES == MAX_BEARER_BYTES
    assert issubclass(m365_driver.PlaywrightBrowserDriver, PlaywrightSession)
    assert browser_core.PlaywrightSession is PlaywrightSession


def test_microsoft_browser_adapter_satisfies_the_vendor_neutral_browser_port(
    tmp_path: Path,
) -> None:
    adapter = Microsoft365BrowserAdapter(
        FakeBrowserDriver([]),
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=tmp_path,
        expected_account=None,
    )
    official = FakeAdapter(
        "vendor.official",
        ProviderFamily.GOOGLE,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
    )

    assert isinstance(adapter, BrowserProviderAdapter)
    assert not isinstance(official, BrowserProviderAdapter)
