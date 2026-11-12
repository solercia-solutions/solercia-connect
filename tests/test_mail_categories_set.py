"""Assigning Outlook categories, the only write Solercia Connect performs.

A support product records a case's state as an Outlook category. The write is the single exception
to the read-only rule, so every test here pins one of its limits: an explicit per-invocation
authorization, one conditional PATCH that carries nothing but the category collection, no write
when nothing changes, no retry, and no HTTP surface at all.
"""

from __future__ import annotations

import inspect
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import solercia_connect.application as application
import solercia_connect.cli as cli
from solercia_connect.config import ConnectionType, load_settings, target_spec
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    ExecutionOutcome,
    ExecutionReceipt,
    MailCategoriesUpdate,
    MailCategoryAssignment,
    Provenance,
    ProviderFamily,
    ReasonCode,
    ReceiptStatus,
    RouteKind,
    StepOutcome,
    TraceStep,
)
from solercia_connect.envelope import ResultEnvelope
from solercia_connect.errors import ConnectorFailure
from solercia_connect.matrix import CAPABILITY_MATRIX
from solercia_connect.providers.m365_browser import driver as driver_module
from solercia_connect.providers.m365_browser.adapter import Microsoft365BrowserAdapter
from solercia_connect.providers.m365_browser.driver import BrowserSnapshot, PlaywrightBrowserDriver
from solercia_connect.providers.m365_browser.selectors import ENDPOINTS
from solercia_connect.providers.microsoft_graph import MicrosoftGraphAdapter
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import ConnectorRuntime, RoutePolicy
from solercia_connect.serialization import to_json
from solercia_connect.service import create_app
from tests.fakes import NOW, FakeAdapter, FakeBrowserDriver, FakeTransport, authenticated_snapshot

_ACCOUNT = "pandora@example.com"
_TOKEN = "opaque-browser-bearer-that-must-never-escape"
_MESSAGE_ID = "AAMk+soporte/1=="
_MESSAGE_URL = f"{ENDPOINTS.outlook_messages}/AAMk%2Bsoporte%2F1%3D%3D"
_VERSION = 'W/"CQAAABYAAAAmP1Ln1wcHRariNdTMGAO9AAAS0AIP"'
_TEST_CATEGORY = "Prueba soporte-kb"
_HEADERS = {"X-Solercia-Connect-Key": "local-api-key"}


def _update(**overrides: Any) -> MailCategoriesUpdate:
    values: dict[str, Any] = {
        "message_id": _MESSAGE_ID,
        "add": (_TEST_CATEGORY,),
        "account_id": _ACCOUNT,
        "write_authorized": True,
    }
    values.update(overrides)
    return MailCategoriesUpdate(**values)


def _assignment(
    *,
    account: str | None = _ACCOUNT,
    route: RouteKind = RouteKind.BROWSER,
    provider_id: str = "microsoft365.browser",
) -> MailCategoryAssignment:
    return MailCategoryAssignment(
        _MESSAGE_ID,
        ("SOPORTE/NIVEL 1", _TEST_CATEGORY),
        ("SOPORTE/NIVEL 1",),
        True,
        Provenance(provider_id, ProviderFamily.MICROSOFT, route, _MESSAGE_ID, NOW, account),
    )


# --- The request itself ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"message_id": "  "}, "message_id"),
        ({"add": ()}, "at least one"),
        ({"add": (), "replace": ()}, "replace"),
        ({"replace": ("Resuelto",)}, "replace"),
        ({"add": (), "remove": ("Pendiente",), "replace": ("Resuelto",)}, "replace"),
        ({"add": ("Resuelto",), "remove": ("resuelto",)}, "both"),
        ({"add": ("Resuelto", "   ")}, "blank"),
    ],
)
def test_an_ambiguous_or_empty_category_request_is_refused(
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _update(**overrides)


def test_category_names_are_trimmed_and_repeated_names_collapse() -> None:
    update = _update(add=("  Resuelto ", "resuelto", "SOPORTE/NIVEL 1"), remove=(" Pendiente",))

    assert update.add == ("Resuelto", "SOPORTE/NIVEL 1")
    assert update.remove == ("Pendiente",)
    assert update.message_id == _MESSAGE_ID


@pytest.mark.parametrize(
    ("overrides", "current", "expected"),
    [
        ({"add": ("Resuelto",)}, ("SOPORTE/NIVEL 1",), ("SOPORTE/NIVEL 1", "Resuelto")),
        ({"add": ("resuelto",)}, ("Resuelto",), ("Resuelto",)),
        ({"add": (), "remove": ("pendiente",)}, ("Pendiente", "Otro"), ("Otro",)),
        (
            {"add": ("Resuelto",), "remove": ("Pendiente",)},
            ("SOPORTE/NIVEL 1", "Pendiente"),
            ("SOPORTE/NIVEL 1", "Resuelto"),
        ),
        ({"add": (), "replace": ("Resuelto",)}, ("Pendiente", "Otro"), ("Resuelto",)),
    ],
)
def test_the_final_list_is_computed_from_the_current_one(
    overrides: dict[str, Any],
    current: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    assert _update(**overrides).resolve(current) == expected


# --- The browser driver: one conditional PATCH of the category collection ----------------------


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    def json(self) -> object:
        return self._payload

    def dispose(self) -> None:
        return None


class _RequestContext:
    def __init__(self, *responses: _Response | Exception) -> None:
        self.responses: deque[_Response | Exception] = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def _answer(self, call: dict[str, Any]) -> _Response:
        self.calls.append(call)
        if not self.responses:
            raise AssertionError(f"Unexpected Outlook REST {call['method']}")
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    @staticmethod
    def _visible_headers(headers: dict[str, str]) -> dict[str, str]:
        return {key: value for key, value in headers.items() if key != "Authorization"}

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None,
        timeout: int,
        fail_on_status_code: bool,
    ) -> _Response:
        del timeout, fail_on_status_code
        return self._answer(
            {
                "method": "GET",
                "url": url,
                "params": params,
                "headers": self._visible_headers(headers),
                "authorized": headers.get("Authorization", "").startswith("Bearer "),
            }
        )

    def patch(
        self,
        url: str,
        *,
        headers: dict[str, str],
        data: object,
        timeout: int,
        fail_on_status_code: bool,
        max_redirects: int,
    ) -> _Response:
        del timeout, fail_on_status_code
        return self._answer(
            {
                "method": "PATCH",
                "url": url,
                "data": data,
                "headers": self._visible_headers(headers),
                "authorized": headers.get("Authorization", "").startswith("Bearer "),
                "max_redirects": max_redirects,
            }
        )


class _Page:
    url = "https://outlook.cloud.microsoft/mail/inbox/"

    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

    def on(self, event: str, callback: Any) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def emit_request(self) -> None:
        request = SimpleNamespace(
            url="https://outlook.office.com/owa/service.svc?action=FindItem",
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
        for callback in self.handlers.get("request", ()):
            callback(request)

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        del url, wait_until, timeout
        self.emit_request()

    def wait_for_timeout(self, milliseconds: int) -> None:
        del milliseconds

    def title(self) -> str:
        return "Outlook"


class _Context:
    def __init__(self, page: _Page, request: _RequestContext) -> None:
        self.pages = [page]
        self.request = request

    def on(self, event: str, callback: Any) -> None:
        del event, callback

    def close(self) -> None:
        return None


def _driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *responses: _Response | Exception,
) -> tuple[PlaywrightBrowserDriver, _RequestContext]:
    page = _Page()
    request = _RequestContext(*responses)
    context = _Context(page, request)
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=lambda **kwargs: context),
        stop=lambda: None,
    )
    monkeypatch.setattr(
        driver_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(
            sync_playwright=lambda: SimpleNamespace(start=lambda: playwright)
        ),
    )
    driver = PlaywrightBrowserDriver()
    driver.open_persistent(
        tmp_path / "profile",
        headless=True,
        connection_type=ConnectionType.OUTLOOK,
    )
    page.emit_request()
    return driver, request


def _state(*categories: str, version: str | None = _VERSION) -> _Response:
    payload: dict[str, Any] = {"Id": _MESSAGE_ID, "Categories": list(categories)}
    if version is not None:
        payload["@odata.etag"] = version
    return _Response(200, payload)


def _written(*categories: str) -> _Response:
    return _Response(
        200,
        {"@odata.etag": 'W/"next"', "Id": _MESSAGE_ID, "Categories": list(categories)},
    )


def test_adding_a_category_writes_only_the_collection_conditioned_on_the_read_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(
        tmp_path,
        monkeypatch,
        _state("SOPORTE/NIVEL 1"),
        _written("SOPORTE/NIVEL 1", _TEST_CATEGORY),
    )

    result = driver.set_mail_categories(_update(), timeout_ms=9_000)
    driver.close()

    read, write = request.calls
    assert read["method"] == "GET"
    assert read["url"] == _MESSAGE_URL
    assert read["params"] == {"$select": "Id,Categories"}
    assert read["authorized"] is True
    assert write["method"] == "PATCH"
    assert write["url"] == _MESSAGE_URL
    assert write["data"] == {"Categories": ["SOPORTE/NIVEL 1", _TEST_CATEGORY]}
    assert write["headers"]["If-Match"] == _VERSION
    assert write["headers"]["Content-Type"] == "application/json"
    assert write["authorized"] is True
    assert write["max_redirects"] == 0
    assert result.message_id == _MESSAGE_ID
    assert result.categories == ("SOPORTE/NIVEL 1", _TEST_CATEGORY)
    assert result.previous_categories == ("SOPORTE/NIVEL 1",)
    assert result.changed is True
    assert result.provenance.route is RouteKind.BROWSER
    assert _TOKEN not in to_json(result)
    assert driver._outlook_bearer is None


def test_assigning_a_category_the_message_already_has_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(tmp_path, monkeypatch, _state("Resuelto"))

    result = driver.set_mail_categories(_update(add=("resuelto",)), timeout_ms=9_000)

    assert [call["method"] for call in request.calls] == ["GET"]
    assert result.changed is False
    assert result.categories == result.previous_categories == ("Resuelto",)


def test_a_replacement_with_the_same_names_in_another_order_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(tmp_path, monkeypatch, _state("A", "B"))

    result = driver.set_mail_categories(
        _update(add=(), replace=("b", "a")),
        timeout_ms=9_000,
    )

    assert [call["method"] for call in request.calls] == ["GET"]
    assert result.changed is False
    assert result.categories == ("A", "B")


def test_a_state_change_removes_and_adds_in_one_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(
        tmp_path,
        monkeypatch,
        _state("SOPORTE/NIVEL 1", "Pendiente"),
        _written("SOPORTE/NIVEL 1", "Resuelto"),
    )

    result = driver.set_mail_categories(
        _update(add=("Resuelto",), remove=("pendiente",)),
        timeout_ms=9_000,
    )

    assert request.calls[1]["data"] == {"Categories": ["SOPORTE/NIVEL 1", "Resuelto"]}
    assert result.previous_categories == ("SOPORTE/NIVEL 1", "Pendiente")
    assert result.categories == ("SOPORTE/NIVEL 1", "Resuelto")


@pytest.mark.parametrize("status", [409, 412])
def test_a_concurrent_edit_fails_as_a_conflict_without_a_second_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    driver, request = _driver(
        tmp_path,
        monkeypatch,
        _state("Pendiente"),
        _Response(status, {"error": {"code": "ErrorIrresolvableConflict"}}),
    )

    with pytest.raises(ConnectorFailure) as captured:
        driver.set_mail_categories(_update(), timeout_ms=9_000)

    assert captured.value.reason is ReasonCode.WRITE_CONFLICT
    assert [call["method"] for call in request.calls] == ["GET", "PATCH"]


@pytest.mark.parametrize("status", [400, 404])
def test_an_unknown_or_moved_message_is_invalid_input_and_is_never_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    driver, request = _driver(tmp_path, monkeypatch, _Response(status, {}))

    with pytest.raises(ConnectorFailure) as captured:
        driver.set_mail_categories(_update(), timeout_ms=9_000)

    assert captured.value.reason is ReasonCode.INVALID_INPUT
    assert [call["method"] for call in request.calls] == ["GET"]


def test_a_message_without_a_version_is_never_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(tmp_path, monkeypatch, _state("Pendiente", version=None))

    with pytest.raises(ConnectorFailure) as captured:
        driver.set_mail_categories(_update(), timeout_ms=9_000)

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA
    assert [call["method"] for call in request.calls] == ["GET"]


def test_malformed_current_categories_are_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(
        tmp_path,
        monkeypatch,
        _Response(200, {"@odata.etag": _VERSION, "Categories": "Pendiente"}),
    )

    with pytest.raises(ConnectorFailure) as captured:
        driver.set_mail_categories(_update(), timeout_ms=9_000)

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA
    assert [call["method"] for call in request.calls] == ["GET"]


def test_a_write_the_provider_does_not_confirm_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, _ = _driver(tmp_path, monkeypatch, _state("Pendiente"), _written("Pendiente"))

    with pytest.raises(ConnectorFailure) as captured:
        driver.set_mail_categories(_update(), timeout_ms=9_000)

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (_Response(401, {}), ReasonCode.BROWSER_IDENTITY_UNAVAILABLE),
        (_Response(403, {}), ReasonCode.AUTHORIZATION_DENIED),
        (_Response(404, {}), ReasonCode.INVALID_INPUT),
        (_Response(500, {}), ReasonCode.BROWSER_EXTRACTION_FAILED),
        (_Response(200, ["not", "an", "object"]), ReasonCode.BROWSER_EXTRACTION_FAILED),
        (RuntimeError("connection reset"), ReasonCode.BROWSER_EXTRACTION_FAILED),
    ],
)
def test_a_rejected_or_interrupted_write_is_typed_and_never_reported_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: _Response | Exception,
    reason: ReasonCode,
) -> None:
    driver, _ = _driver(tmp_path, monkeypatch, _state("Pendiente"), outcome)

    with pytest.raises(ConnectorFailure) as captured:
        driver.set_mail_categories(_update(), timeout_ms=9_000)

    assert captured.value.reason is reason
    assert _TOKEN not in captured.value.safe_message
    assert driver._outlook_bearer is None


def test_the_driver_refuses_an_unauthorized_write_before_any_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(tmp_path, monkeypatch)

    with pytest.raises(ConnectorFailure) as captured:
        driver.set_mail_categories(_update(write_authorized=False), timeout_ms=9_000)

    assert captured.value.reason is ReasonCode.WRITE_NOT_AUTHORIZED
    assert request.calls == []


def test_the_driver_has_exactly_one_write_call_site() -> None:
    source = inspect.getsource(PlaywrightBrowserDriver)

    assert source.count("request.patch(") == 1
    for method in ("request.post(", "request.put(", "request.delete(", "request.fetch("):
        assert method not in source


# --- The browser adapter ----------------------------------------------------------------------


def _outlook_shell() -> BrowserSnapshot:
    return BrowserSnapshot(
        "https://outlook.cloud.microsoft/mail/", "Outlook", (), False, False, True
    )


def _outlook_adapter(tmp_path: Path, driver: FakeBrowserDriver) -> Microsoft365BrowserAdapter:
    return Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=tmp_path / "profile",
        expected_account=_ACCOUNT,
        state_poll_ms=1,
        identity_attestation_cycles=1,
    )


def test_the_browser_adapter_writes_through_the_driver_and_binds_the_verified_account(
    tmp_path: Path,
) -> None:
    driver = FakeBrowserDriver([_outlook_shell()], outlook_identity_outcomes=[_ACCOUNT])
    driver.write_result = _assignment(account=None)
    adapter = _outlook_adapter(tmp_path, driver)

    records = adapter.execute(
        ConnectorRequest.create("microsoft.graph", Capability.MAIL_CATEGORIES_SET, _update())
    )

    assert driver.write_calls == [_update()]
    assert driver.read_calls == 0
    assert driver.close_calls == 1
    (record,) = records
    assert isinstance(record, MailCategoryAssignment)
    assert record.provenance.account_id == _ACCOUNT
    assert record.provenance.provider_id == "microsoft365.browser"


def test_the_browser_adapter_never_writes_into_a_mailbox_it_did_not_verify(
    tmp_path: Path,
) -> None:
    driver = FakeBrowserDriver([_outlook_shell()], outlook_identity_outcomes=[_ACCOUNT])
    driver.write_result = _assignment()
    adapter = _outlook_adapter(tmp_path, driver)

    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(
            ConnectorRequest.create(
                "microsoft.graph",
                Capability.MAIL_CATEGORIES_SET,
                _update(account_id="otra@example.com"),
            )
        )

    assert captured.value.reason is ReasonCode.BROWSER_ACCOUNT_MISMATCH
    assert driver.write_calls == []


def test_only_the_outlook_session_can_write_categories(tmp_path: Path) -> None:
    driver = FakeBrowserDriver([authenticated_snapshot(_ACCOUNT)])
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account=_ACCOUNT,
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(
            ConnectorRequest.create("microsoft.graph", Capability.MAIL_CATEGORIES_SET, _update())
        )

    assert captured.value.reason is ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED
    assert driver.open_calls == []
    assert driver.write_calls == []


# --- Runtime routing ---------------------------------------------------------------------------


def _browser_fake(*outcomes: Any) -> FakeAdapter:
    return FakeAdapter(
        "microsoft365.browser",
        ProviderFamily.MICROSOFT,
        RouteKind.BROWSER,
        frozenset({Capability.MAIL_CATEGORIES_SET}),
        *outcomes,
    )


def test_the_runtime_refuses_an_unauthorized_write_without_touching_any_adapter() -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_CATEGORIES_SET}),
        (_assignment(route=RouteKind.OFFICIAL, provider_id="microsoft.graph"),),
    )
    browser = _browser_fake((_assignment(),))
    registry = ProviderRegistry()
    registry.register(official)
    registry.register(browser)

    outcome = ConnectorRuntime(registry, sleeper=lambda _: None).execute(
        ConnectorRequest.create(
            "microsoft.graph",
            Capability.MAIL_CATEGORIES_SET,
            _update(write_authorized=False),
        ),
        browser_fallback_enabled=True,
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.WRITE_NOT_AUTHORIZED
    assert official.calls == browser.calls == 0


def test_only_the_explicit_browser_route_serves_the_write() -> None:
    official = MicrosoftGraphAdapter(FakeTransport(), _NoAuthorization())
    request = ConnectorRequest.create(
        "microsoft.graph",
        Capability.MAIL_CATEGORIES_SET,
        _update(),
    )

    closed = ProviderRegistry()
    closed.register(official)
    closed_browser = _browser_fake((_assignment(),))
    closed.register(closed_browser)
    refused = ConnectorRuntime(closed).execute(request, browser_fallback_enabled=False)

    opened = ProviderRegistry()
    opened.register(MicrosoftGraphAdapter(FakeTransport(), _NoAuthorization()))
    opened_browser = _browser_fake((_assignment(),))
    opened.register(opened_browser)
    served = ConnectorRuntime(opened).execute(request, browser_fallback_enabled=True)

    assert Capability.MAIL_CATEGORIES_SET not in official.descriptor.capabilities
    assert refused.ok is False
    assert refused.receipt.final_reason is ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE
    assert closed_browser.calls == 0
    assert served.ok is True
    assert served.receipt.selected_route is RouteKind.BROWSER
    assert opened_browser.calls == 1
    assert Capability.MAIL_CATEGORIES_SET in RoutePolicy().browser_fallback_capabilities


def test_a_write_is_never_retried_automatically() -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_CATEGORIES_SET}),
        ConnectorFailure(ReasonCode.RATE_LIMITED, "Provider rate limit reached"),
        (_assignment(route=RouteKind.OFFICIAL, provider_id="microsoft.graph"),),
    )
    registry = ProviderRegistry()
    registry.register(official)

    outcome = ConnectorRuntime(registry, sleeper=lambda _: None).execute(
        ConnectorRequest.create("microsoft.graph", Capability.MAIL_CATEGORIES_SET, _update())
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.RATE_LIMITED
    assert official.calls == 1


def test_a_write_answered_from_another_mailbox_is_refused() -> None:
    registry = ProviderRegistry()
    registry.register(_browser_fake((_assignment(account="otra@example.com"),)))

    outcome = ConnectorRuntime(registry).execute(
        ConnectorRequest.create("microsoft.graph", Capability.MAIL_CATEGORIES_SET, _update()),
        browser_fallback_enabled=True,
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION


class _NoAuthorization:
    def authorization_header(self) -> str:
        raise AssertionError("The official route must not authorize a category write")


# --- Application, CLI and HTTP -----------------------------------------------------------------


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "outlook",
        "account": _ACCOUNT,
        "capability": "mail.categories.set",
        "message_id": _MESSAGE_ID,
        "add": [_TEST_CATEGORY],
        "allow_write": True,
        "allow_browser_fallback": True,
    }
    payload.update(overrides)
    return payload


def _no_registry(**_: Any) -> Any:
    raise AssertionError("A refused write must not build the provider registry")


def test_without_allow_write_the_command_fails_clearly_before_any_provider_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(allow_write=False))

    assert result.ok is False
    assert result.status == "invalid_input"
    assert result.error is not None
    assert result.error.code == "write_not_authorized"
    assert "--allow-write" in result.error.message
    assert result.receipt is None


def test_the_application_never_writes_for_a_non_interactive_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    for browser_via_http in (False, True):
        result = application.run_application(
            _payload(),
            interactive=False,
            browser_via_http=browser_via_http,
        )

        assert result.status == "forbidden"
        assert result.error is not None
        assert result.error.code == "write_not_available_over_http"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"message_id": None}, "message_id"),
        ({"add": None}, "at least one"),
        ({"replace": ["Resuelto"]}, "replace"),
        ({"add": "Resuelto"}, "list"),
        ({"account": None}, "account"),
        ({"allow_write": "yes"}, "allow_write"),
        ({"capability": "mail.inbox.list", "start": None, "end": None}, "mail.categories.set"),
    ],
)
def test_malformed_category_requests_are_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    message: str,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(**overrides))

    assert result.status == "invalid_input"
    assert result.error is not None
    assert message in result.error.message


def test_an_authorized_write_reaches_the_runtime_and_returns_one_versioned_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    receipt = ExecutionReceipt(
        "rcpt-1",
        "req-1",
        "microsoft.graph",
        Capability.MAIL_CATEGORIES_SET,
        ReceiptStatus.SUCCESS,
        RouteKind.BROWSER,
        ReasonCode.SUCCESS,
        NOW,
        (
            TraceStep(
                1,
                RouteKind.BROWSER,
                "microsoft365.browser",
                1,
                StepOutcome.SUCCESS,
                ReasonCode.SUCCESS,
                "Browser fallback completed",
            ),
        ),
    )

    class _Runtime:
        def __init__(self, registry: object) -> None:
            assert registry == "registry"

        def execute(self, request: ConnectorRequest, *, browser_fallback_enabled: bool) -> Any:
            captured["request"] = request
            captured["fallback"] = browser_fallback_enabled
            return ExecutionOutcome(True, (_assignment(),), receipt)

    monkeypatch.setattr(application, "registry_from_environment", lambda **_: "registry")
    monkeypatch.setattr(application, "ConnectorRuntime", _Runtime)

    result = application.run_application(_payload(remove=["Pendiente"]))

    request = captured["request"]
    assert request.capability is Capability.MAIL_CATEGORIES_SET
    assert request.payload == _update(remove=("Pendiente",))
    assert captured["fallback"] is True
    assert result.ok is True
    record = result.to_dict()["data"]["records"][0]
    assert record["schema"] == "solercia.connect/record/v1"
    assert record["message_id"] == _MESSAGE_ID
    assert record["categories"] == ["SOPORTE/NIVEL 1", _TEST_CATEGORY]
    assert record["previous_categories"] == ["SOPORTE/NIVEL 1"]
    assert record["changed"] is True
    assert result.to_dict()["receipt"]["receipt_id"] == "rcpt-1"


runner = CliRunner()


def _cli_args(*extra: str) -> list[str]:
    return [
        "run",
        "--provider",
        "microsoft.graph",
        "--capability",
        "mail.categories.set",
        "--account",
        _ACCOUNT,
        "--message-id",
        _MESSAGE_ID,
        *extra,
    ]


def test_the_cli_without_allow_write_exits_with_input_error_and_no_provider_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = runner.invoke(
        cli.app,
        _cli_args("--add", _TEST_CATEGORY, "--allow-browser-fallback"),
        env={},
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "write_not_authorized"
    assert payload["receipt"] is None


def test_the_cli_passes_repeatable_category_options_and_the_write_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_run(payload: dict[str, Any]) -> Any:
        seen.append(payload)
        return ResultEnvelope.success(operation="run", data={"records": []})

    monkeypatch.setattr(cli, "run_application", fake_run)

    result = runner.invoke(
        cli.app,
        _cli_args(
            "--add",
            "Resuelto",
            "--add",
            "SOPORTE/NIVEL 1",
            "--remove",
            "Pendiente",
            "--allow-write",
            "--allow-browser-fallback",
        ),
        env={},
    )

    assert result.exit_code == 0, result.stdout
    (payload,) = seen
    assert payload["message_id"] == _MESSAGE_ID
    assert payload["add"] == ["Resuelto", "SOPORTE/NIVEL 1"]
    assert payload["remove"] == ["Pendiente"]
    assert payload["replace"] is None
    assert payload["allow_write"] is True
    assert payload["allow_browser_fallback"] is True


def _service_settings(tmp_path: Path, *, interactive: bool) -> Any:
    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "SOLERCIA_CONNECT_API_KEY": "local-api-key",
    }
    if interactive:
        values["SOLERCIA_CONNECT_BROWSER_FALLBACK"] = "true"
        values["SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN"] = "true"
    return load_settings(environ=values)


@pytest.mark.parametrize("interactive", [False, True])
def test_http_rejects_the_write_before_the_application_runs(
    tmp_path: Path,
    interactive: bool,
) -> None:
    calls: list[dict[str, Any]] = []

    def run_handler(payload: dict[str, Any], *, browser_via_http: bool = False) -> Any:
        del browser_via_http
        calls.append(payload)
        raise AssertionError("HTTP must not reach the application for a write")

    client = TestClient(
        create_app(
            settings=_service_settings(tmp_path, interactive=interactive),
            run_handler=run_handler,
        )
    )

    plain = client.post(
        "/v1/run",
        headers=_HEADERS,
        json={
            "type": "outlook",
            "account": _ACCOUNT,
            "capability": "mail.categories.set",
            "allow_browser_fallback": interactive,
        },
    )
    with_write_fields = client.post(
        "/v1/run",
        headers=_HEADERS,
        json={
            "type": "outlook",
            "account": _ACCOUNT,
            "capability": "mail.categories.set",
            "message_id": _MESSAGE_ID,
            "add": [_TEST_CATEGORY],
            "allow_write": True,
        },
    )

    assert plain.status_code == 403
    assert plain.json()["error"]["code"] == "write_not_available_over_http"
    assert with_write_fields.status_code == 422
    assert calls == []


# --- Contract surface --------------------------------------------------------------------------


def test_the_outlook_target_offers_the_write_without_requesting_a_write_scope() -> None:
    outlook = target_spec(ConnectionType.OUTLOOK)

    assert "mail.categories.set" in outlook.capabilities
    assert outlook.oauth_scopes == ("openid", "profile", "User.Read", "Mail.Read", "Calendars.Read")
    assert Capability.MAIL_CATEGORIES_SET not in MicrosoftGraphAdapter.descriptor.capabilities
    assert Capability.MAIL_CATEGORIES_SET in Microsoft365BrowserAdapter.descriptor.capabilities


def test_the_capability_matrix_states_both_routes_honestly() -> None:
    rows = {
        row.channel: row.v0_1_status
        for row in CAPABILITY_MATRIX
        if row.capability == "mail.categories.set"
    }

    assert rows == {"official": "not-implemented", "browser": "implemented-offline"}


def test_the_legacy_query_helper_refuses_to_build_a_write() -> None:
    with pytest.raises(cli._InputError, match=r"mail\.categories\.set is a write"):
        cli._query_payload(
            Capability.MAIL_CATEGORIES_SET,
            start=None,
            end=None,
            account=_ACCOUNT,
            calendar_id="primary",
            assignee=None,
            jql=None,
            meeting_id=None,
            limit=50,
        )


def test_docs_declare_the_category_write_and_its_flag() -> None:
    root = Path(__file__).parents[1]
    for relative in (
        "README.md",
        "docs/runbooks/outlook.md",
        "docs/USO-CON-AGENTES.md",
        "agents/skills/solercia-connect/SKILL.md",
    ):
        text = (root / relative).read_text()
        assert "mail.categories.set" in text, relative
        assert "--allow-write" in text, relative
