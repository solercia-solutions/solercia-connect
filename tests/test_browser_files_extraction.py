"""Drive reads over the supervised SharePoint session."""

from __future__ import annotations

import base64
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from solercia_connect.config import ConnectionType
from solercia_connect.domain import (
    Capability,
    DateWindow,
    FileQuery,
    ReasonCode,
    RouteKind,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.m365_browser import driver as driver_module
from solercia_connect.providers.m365_browser.driver import PlaywrightBrowserDriver
from solercia_connect.providers.m365_browser.selectors import SELECTORS
from solercia_connect.serialization import to_json

_TOKEN = "opaque-sharepoint-bearer-that-must-never-escape"
_ORIGIN = "https://contoso-my.sharepoint.com"
_DRIVE = f"{_ORIGIN}/_api/v2.0/me/drive"


def _item(name: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"01{name.upper()}",
        "name": name,
        "size": 2048,
        "lastModifiedDateTime": "2026-08-15T10:00:00Z",
        "createdDateTime": "2026-08-01T10:00:00Z",
        "file": {"mimeType": "application/pdf"},
        "webUrl": f"{_ORIGIN}/Documents/{name}",
        "parentReference": {"driveId": "b!drive", "path": "/drive/root:/Documentos"},
    }
    payload.update(overrides)
    return payload


class _FakeRequest:
    def __init__(self, url: str, authorization: object) -> None:
        self.url = url
        self.headers = {"authorization": authorization} if authorization is not None else {}


class _FakeResponse:
    def __init__(self, status: int, payload: object, *, body: bytes = b"") -> None:
        self.status = status
        self._payload = payload
        self._body = body

    def json(self) -> object:
        return self._payload

    def body(self) -> bytes:
        return self._body

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
        params: dict[str, str] | None = None,
        timeout: int,
        fail_on_status_code: bool,
    ) -> _FakeResponse:
        del fail_on_status_code
        self.calls.append(
            {
                "url": url,
                "params": params,
                "authorized": headers.get("Authorization", "").startswith("Bearer "),
            }
        )
        if not self.responses:
            raise AssertionError(f"Unexpected SharePoint call: {url}")
        return self.responses.popleft()


class _FakePage:
    url = f"{_ORIGIN}/personal/user/_layouts/15/onedrive.aspx"

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
            self.emit_request(f"{_ORIGIN}/_api/v2.0/me/drive", f"Bearer {_TOKEN}")

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.wait_calls.append(milliseconds)

    def title(self) -> str:
        return "OneDrive"


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
        connection_type=ConnectionType.ONEDRIVE,
    )
    page.emit_request(f"{_ORIGIN}/_api/v2.0/me/drive/root", f"Bearer {_TOKEN}")
    return driver, page, request


def _read(
    driver: PlaywrightBrowserDriver,
    query: FileQuery,
    capability: Capability = Capability.FILES_SEARCH,
) -> tuple[Any, ...]:
    return driver.read_records(capability, query, SELECTORS, timeout_ms=9_000)


def test_root_listing_uses_the_observed_drive_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, _, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, {"value": [_item("acta.pdf")]}),
    )

    files = _read(driver, FileQuery())
    driver.close()

    assert len(files) == 1
    assert files[0].name == "acta.pdf"
    assert files[0].path == "/drive/root:/Documentos/acta.pdf"
    assert files[0].provenance.route is RouteKind.BROWSER
    assert files[0].provenance.endpoint == "sharepoint-rest-v2"
    assert request.calls[0]["url"] == f"{_DRIVE}/root/children"
    assert request.calls[0]["authorized"] is True


def test_folder_and_search_target_the_documented_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, _, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, {"value": []}),
    )
    _read(driver, FileQuery(folder_path="/Documentos/2026"))
    driver.close()

    assert request.calls[0]["url"] == f"{_DRIVE}/root:/Documentos/2026:/children"


def test_a_site_replaces_the_personal_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, _, request = _driver(tmp_path, monkeypatch, _FakeResponse(200, {"value": []}))

    _read(driver, FileQuery(site="sites/calidad"))
    driver.close()

    assert request.calls[0]["url"] == f"{_ORIGIN}/_api/v2.0/sites/sites/calidad/drive/root/children"


def test_a_window_filters_by_last_modification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, _, _ = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(
            200,
            {
                "value": [
                    _item("dentro.pdf"),
                    _item("fuera.pdf", lastModifiedDateTime="2026-06-01T10:00:00Z"),
                ]
            },
        ),
    )

    files = _read(
        driver,
        FileQuery(
            window=DateWindow(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
        ),
    )
    driver.close()

    assert [item.name for item in files] == ["dentro.pdf"]


def test_files_get_downloads_content_within_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"%PDF-1.7 minimal"
    driver, _, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, _item("acta.pdf")),
        _FakeResponse(200, None, body=content),
    )

    files = _read(driver, FileQuery(item_id="01ACTA.PDF"), Capability.FILES_GET)
    driver.close()

    assert files[0].content_base64 == base64.b64encode(content).decode("ascii")
    assert request.calls[-1]["url"].endswith("/content")


def test_content_over_the_budget_is_refused_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, _, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, _item("grande.pdf", size=90_000)),
    )

    with pytest.raises(ConnectorFailure) as captured:
        _read(driver, FileQuery(item_id="01G", max_content_bytes=1_000), Capability.FILES_GET)
    driver.close()

    assert captured.value.reason is ReasonCode.INVALID_INPUT
    assert not any(call["url"].endswith("/content") for call in request.calls)


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_authorization_is_typed_as_identity_unavailable(
    status: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, _, _ = _driver(tmp_path, monkeypatch, _FakeResponse(status, None))

    with pytest.raises(ConnectorFailure) as captured:
        _read(driver, FileQuery())
    driver.close()

    assert captured.value.reason is ReasonCode.BROWSER_IDENTITY_UNAVAILABLE


def test_the_mail_authorization_is_never_reused_for_drive_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Outlook host must not seed the drive authorization; the audiences differ."""
    page = _FakePage()
    request = _FakeRequestContext()
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
        tmp_path / "profile", headless=True, connection_type=ConnectionType.ONEDRIVE
    )
    page.emit_on_goto = False
    page.emit_request("https://outlook.office.com/api/v2.0/me", f"Bearer {_TOKEN}")

    with pytest.raises(ConnectorFailure) as captured:
        _read(driver, FileQuery())
    driver.close()

    assert captured.value.reason is ReasonCode.BROWSER_IDENTITY_UNAVAILABLE
    assert request.calls == []


def test_drive_records_never_carry_the_session_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, _, _ = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, {"value": [_item("acta.pdf")]}),
    )

    files = _read(driver, FileQuery())
    driver.close()

    rendered = to_json(files)
    assert _TOKEN not in rendered
    assert "authorization" not in rendered.lower()
    assert driver._files_bearer is None
