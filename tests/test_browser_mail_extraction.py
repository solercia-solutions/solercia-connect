"""Sent-mail extraction over the supervised Microsoft 365 browser session."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from solercia_connect.config import ConnectionType
from solercia_connect.domain import Capability, DateWindow, MailQuery, ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.m365_browser import driver as driver_module
from solercia_connect.providers.m365_browser.driver import PlaywrightBrowserDriver
from solercia_connect.providers.m365_browser.selectors import ENDPOINTS, SELECTORS
from solercia_connect.serialization import to_json

_TOKEN = "opaque-browser-bearer-that-must-never-escape"
_SENT = ENDPOINTS.outlook_sent_messages
_WINDOW = DateWindow(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))


def _message(index: int, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Id": f"AAMk{index}",
        "Subject": f"Solicitud {index}",
        "SentDateTime": f"2026-08-{index:02d}T14:23:11Z",
        "From": {"EmailAddress": {"Name": "Camilo", "Address": "USER@Example.com"}},
        "ToRecipients": [{"EmailAddress": {"Name": "Mesa", "Address": "mesa@example.com"}}],
        "CcRecipients": [{"EmailAddress": {"Address": "jefe@example.com"}}],
        "Body": {"ContentType": "HTML", "Content": f"<p>cuerpo {index}</p>"},
        "ConversationId": f"conv-{index}",
        "HasAttachments": index % 2 == 0,
    }
    payload.update(overrides)
    return payload


class _FakeRequest:
    def __init__(self, url: str, authorization: object) -> None:
        self.url = url
        self.headers = {"authorization": authorization} if authorization is not None else {}


class _FakeResponse:
    def __init__(self, status: int, payload: object, *, malformed: bool = False) -> None:
        self.status = status
        self._payload = payload
        self._malformed = malformed

    def json(self) -> object:
        if self._malformed:
            raise ValueError("not json")
        return self._payload

    def dispose(self) -> None:
        return None


class _FakeRequestContext:
    def __init__(self, *responses: _FakeResponse) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None,
        timeout: int,
        fail_on_status_code: bool,
    ) -> _FakeResponse:
        del fail_on_status_code
        self.calls.append(
            {
                "url": url,
                "params": params,
                "timeout": timeout,
                "authorized": headers.get("Authorization", "").startswith("Bearer "),
            }
        )
        if not self.responses:
            raise AssertionError("Unexpected Outlook REST call")
        return self.responses.popleft()


class _FakePage:
    url = "https://outlook.cloud.microsoft/mail/sentitems/"

    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}
        self.goto_calls: list[str] = []
        self.wait_calls: list[int] = []
        self.emit_on_goto = True

    def on(self, event: str, callback: Any) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def emit_request(self, url: str, authorization: object) -> None:
        for callback in self.handlers.get("request", ()):
            callback(_FakeRequest(url, authorization))

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        del wait_until, timeout
        self.goto_calls.append(url)
        if self.emit_on_goto:
            self.emit_request(
                "https://outlook.cloud.microsoft/owa/service.svc?action=FindItem",
                f"Bearer {_TOKEN}",
            )

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.wait_calls.append(milliseconds)

    def title(self) -> str:
        return "Outlook"


class _FakeContext:
    def __init__(self, page: _FakePage, request: _FakeRequestContext) -> None:
        self.pages = [page]
        self.request = request
        self.handlers: dict[str, list[Any]] = {}
        self.closed = False

    def on(self, event: str, callback: Any) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def close(self) -> None:
        self.closed = True


def _driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *responses: _FakeResponse,
) -> tuple[PlaywrightBrowserDriver, _FakePage, _FakeRequestContext]:
    page = _FakePage()
    request = _FakeRequestContext(*responses)
    context = _FakeContext(page, request)
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
    return driver, page, request


def _read(driver: PlaywrightBrowserDriver, limit: int = 50) -> tuple[Any, ...]:
    return driver.read_records(
        Capability.MAIL_SENT_LIST,
        MailQuery(_WINDOW, limit=limit),
        SELECTORS,
        timeout_ms=9_000,
    )


def test_sent_mail_is_normalized_from_the_documented_rest_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, page, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, {"value": [_message(4), _message(5)]}),
    )
    page.emit_request("https://outlook.office.com/owa/first", f"Bearer {_TOKEN}")

    records = _read(driver)
    driver.close()

    assert len(records) == 2
    first = records[0]
    assert first.id == "AAMk4"
    assert first.subject == "Solicitud 4"
    assert first.sent_at == datetime(2026, 8, 4, 14, 23, 11, tzinfo=UTC)
    assert first.sender is not None and first.sender.address == "user@example.com"
    assert [address.address for address in first.to] == ["mesa@example.com"]
    assert [address.address for address in first.cc] == ["jefe@example.com"]
    assert first.body == "<p>cuerpo 4</p>"
    assert first.body_format == "html"
    assert first.conversation_id == "conv-4"
    assert first.has_attachments is True
    assert records[1].has_attachments is False
    assert request.calls[0]["url"] == _SENT
    assert request.calls[0]["authorized"] is True


def test_sent_mail_query_is_end_exclusive_and_asks_only_for_needed_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, page, request = _driver(tmp_path, monkeypatch, _FakeResponse(200, {"value": []}))
    page.emit_request("https://outlook.office.com/owa/first", f"Bearer {_TOKEN}")

    assert _read(driver) == ()
    driver.close()

    params = request.calls[0]["params"]
    assert params is not None
    assert params["$filter"] == (
        "SentDateTime ge 2026-08-01T00:00:00Z and SentDateTime lt 2026-09-01T00:00:00Z"
    )
    assert params["$orderby"] == "SentDateTime asc"
    assert set(params["$select"].split(",")) == {
        "Id",
        "Subject",
        "SentDateTime",
        "From",
        "ToRecipients",
        "CcRecipients",
        "Body",
        "ConversationId",
        "HasAttachments",
    }


def test_sent_mail_follows_the_provider_next_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    next_link = f"{_SENT}?%24skip=50"
    driver, page, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, {"value": [_message(1)], "@odata.nextLink": next_link}),
        _FakeResponse(200, {"value": [_message(2)]}),
    )
    page.emit_request("https://outlook.office.com/owa/first", f"Bearer {_TOKEN}")

    records = _read(driver)
    driver.close()

    assert [record.id for record in records] == ["AAMk1", "AAMk2"]
    assert [call["url"] for call in request.calls] == [_SENT, next_link]
    assert request.calls[1]["params"] is None
    assert all(call["authorized"] for call in request.calls)


def test_sent_mail_stops_at_the_requested_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, page, _ = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, {"value": [_message(1), _message(2), _message(3)]}),
    )
    page.emit_request("https://outlook.office.com/owa/first", f"Bearer {_TOKEN}")

    records = _read(driver, limit=2)
    driver.close()

    assert [record.id for record in records] == ["AAMk1", "AAMk2"]


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_authorization_is_typed_as_identity_unavailable(
    status: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, page, _ = _driver(tmp_path, monkeypatch, _FakeResponse(status, None))
    page.emit_request("https://outlook.office.com/owa/first", f"Bearer {_TOKEN}")

    with pytest.raises(ConnectorFailure) as captured:
        _read(driver)
    driver.close()

    assert captured.value.reason is ReasonCode.BROWSER_IDENTITY_UNAVAILABLE


@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(200, {"value": "not-a-list"}),
        _FakeResponse(200, None, malformed=True),
        _FakeResponse(500, None),
    ],
    ids=["wrong-shape", "unparsable", "server-error"],
)
def test_unusable_provider_payloads_are_typed_as_extraction_failures(
    response: _FakeResponse,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, page, _ = _driver(tmp_path, monkeypatch, response)
    page.emit_request("https://outlook.office.com/owa/first", f"Bearer {_TOKEN}")

    with pytest.raises(ConnectorFailure) as captured:
        _read(driver)
    driver.close()

    assert captured.value.reason is ReasonCode.BROWSER_EXTRACTION_FAILED


def test_a_session_that_never_authorizes_fails_within_a_bounded_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, page, request = _driver(tmp_path, monkeypatch)
    page.emit_on_goto = False

    with pytest.raises(ConnectorFailure) as captured:
        _read(driver)
    driver.close()

    assert captured.value.reason is ReasonCode.BROWSER_IDENTITY_UNAVAILABLE
    assert request.calls == []
    assert len(page.wait_calls) == driver_module._READ_BEARER_MAX_CYCLES


def test_extracted_records_never_carry_the_session_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver, page, _ = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, {"value": [_message(7)]}),
    )
    page.emit_request("https://outlook.office.com/owa/first", f"Bearer {_TOKEN}")

    records = _read(driver)
    driver.close()

    rendered = to_json(records)
    captured = capsys.readouterr()
    assert _TOKEN not in rendered
    assert "authorization" not in rendered.lower()
    assert _TOKEN not in captured.out
    assert _TOKEN not in captured.err
    assert driver._outlook_bearer is None
