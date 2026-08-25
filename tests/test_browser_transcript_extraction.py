"""Meeting transcript reads over the supervised SharePoint session.

A transcript is not a Teams resource on this route. It hangs off the recording, in whichever
drive stores it, which is what makes a meeting the account merely attended readable at all.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest

from solercia_connect.config import ConnectionType
from solercia_connect.domain import (
    Capability,
    DateWindow,
    ReasonCode,
    RouteKind,
    TranscriptQuery,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.m365_browser import driver as driver_module
from solercia_connect.providers.m365_browser.driver import PlaywrightBrowserDriver
from solercia_connect.providers.m365_browser.selectors import SELECTORS
from solercia_connect.serialization import to_json

_TOKEN = "opaque-sharepoint-bearer-that-must-never-escape"
_ORIGIN = "https://contoso-my.sharepoint.com"
_API = f"{_ORIGIN}/_api/v2.0"
_OWN_DRIVE = "b!own-drive"
_SHARED_DRIVE = "b!organizer-drive"
_WINDOW = DateWindow(
    datetime(2026, 8, 1, tzinfo=UTC),
    datetime(2026, 9, 1, tzinfo=UTC),
)


def _own_recording() -> dict[str, Any]:
    return {
        "id": "01OWNITEM",
        "name": "Revisión de historia de usuario-20260804-Grabación de la reunión.mp4",
        "lastModifiedDateTime": "2026-08-04T17:39:22Z",
        "parentReference": {"driveId": _OWN_DRIVE},
        "createdBy": {"user": {"displayName": "Camilo Calderón"}},
    }


def _shared_recording() -> dict[str, Any]:
    """One entry of the shared collection, whose real identifiers are nested."""
    return {
        "id": "01LINKINMYDRIVE",
        "name": "PRUEBAS PANDORA-20260820-Grabación de la reunión.mp4",
        "lastModifiedDateTime": "2026-08-20T20:56:16Z",
        "remoteItem": {
            "id": "01ORGANIZERITEM",
            "parentReference": {"driveId": _SHARED_DRIVE},
            "shared": {"sharedBy": {"user": {"displayName": "Claudia Herrera"}}},
        },
    }


def _manifest(transcript_id: str, display_name: str) -> dict[str, Any]:
    return {
        "value": [
            {
                "id": transcript_id,
                "displayName": display_name,
                "size": 144_425,
                "languageTag": "es-MX",
                "transcriptType": "subtitle",
            }
        ]
    }


def _stream(*turns: tuple[str, str, str, str]) -> dict[str, Any]:
    return {
        "$schema": "stream-transcript",
        "version": "1.0",
        "type": "Transcript",
        "entries": [
            {
                "id": f"cue-{index}",
                "text": text,
                "speakerDisplayName": speaker,
                "startOffset": start,
                "endOffset": end,
            }
            for index, (speaker, text, start, end) in enumerate(turns, start=1)
        ],
        "events": [],
    }


class _FakeRequest:
    def __init__(self, url: str, authorization: object) -> None:
        self.url = url
        self.headers = {"authorization": authorization} if authorization is not None else {}


class _FakeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    def json(self) -> object:
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
        params: dict[str, str] | None = None,
        timeout: int,
        fail_on_status_code: bool,
    ) -> _FakeResponse:
        del fail_on_status_code, timeout
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

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


class _FakePage:
    url = f"{_ORIGIN}/personal/user/_layouts/15/onedrive.aspx"

    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}
        self.goto_calls: list[str] = []

    def on(self, event: str, callback: Any) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def emit_request(self, url: str, authorization: object) -> None:
        for callback in self.handlers.get("request", ()):
            callback(_FakeRequest(url, authorization))

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        del wait_until, timeout
        self.goto_calls.append(url)
        self.emit_request(f"{_API}/me/drive", f"Bearer {_TOKEN}")

    def wait_for_timeout(self, milliseconds: int) -> None:
        del milliseconds

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
) -> tuple[PlaywrightBrowserDriver, _FakeRequestContext]:
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
    page.emit_request(f"{_API}/me/drive/root", f"Bearer {_TOKEN}")
    return driver, request


def _read(
    driver: PlaywrightBrowserDriver,
    query: TranscriptQuery,
    capability: Capability = Capability.MEETING_TRANSCRIPT_GET,
) -> tuple[Any, ...]:
    return driver.read_records(capability, query, SELECTORS, timeout_ms=9_000)


def test_period_read_covers_own_and_shared_recordings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, {"value": [_own_recording()]}),
        # Teams names the folder in one language; the other name is a 404, not an empty list.
        _FakeResponse(404, {"error": {"code": "itemNotFound"}}),
        _FakeResponse(200, {"value": [_shared_recording()]}),
        _FakeResponse(200, _manifest("t-shared", "PRUEBAS PANDORA.json")),
        _FakeResponse(
            200,
            _stream(
                ("Claudia Herrera", "Camilo, buenas tardes.", "00:00:04.7977753", "00:00:09.100"),
                ("Camilo Calderón", "Seguimos con el caso.", "00:00:09.100", "00:00:15.500"),
            ),
        ),
        _FakeResponse(200, _manifest("t-own", "Revisión de historia de usuario.json")),
        _FakeResponse(
            200,
            _stream(("Camilo Calderón", "Arrancamos.", "00:00:01.000", "00:00:04.000")),
        ),
    )

    transcripts = _read(driver, TranscriptQuery(window=_WINDOW))

    assert [item.id for item in transcripts] == ["t-shared", "t-own"]
    shared, own = transcripts

    # The shared entry's own id addresses a link in this account's drive. Reading the transcript
    # from it would ask the wrong drive for the wrong item, so the nested pair is what is used.
    assert shared.drive_id == _SHARED_DRIVE and shared.item_id == "01ORGANIZERITEM"
    assert shared.organizer == "Claudia Herrera"
    assert own.drive_id == _OWN_DRIVE and own.item_id == "01OWNITEM"
    # `createdBy` on an own recording is the Teams service that wrote the file, so there is no
    # organizer to report rather than a service name dressed up as a person.
    assert own.organizer is None

    assert (
        f"{_API}/drives/{quote(_SHARED_DRIVE, safe='')}/items/01ORGANIZERITEM/media/transcripts"
        in request.urls
    )
    # The account-relative form that serves file reads answers 404 for this collection.
    assert not any("me/drive/items/" in url for url in request.urls)
    driver.close()


def test_transcript_content_is_speaker_named_vtt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, _manifest("t-1", "Sesión de pruebas.json")),
        _FakeResponse(
            200,
            _stream(
                ("Carmen Torres", "Hicimos las pruebas anoche.", "00:00:04.7977753", "00:00:20.4"),
                ("Camilo Calderón", "Perfecto, reviso el caso.", "00:00:20.5", "00:01:04.25"),
                ("Carmen Torres", "Quedamos así entonces.", "00:01:04.25", "00:01:30.000"),
            ),
        ),
    )

    (transcript,) = _read(
        driver,
        TranscriptQuery(drive_id=_OWN_DRIVE, item_id="01OWNITEM"),
    )

    # WebVTT without speaker markers collapses consecutive turns into one unattributed block,
    # which is what the default rendition returns, so the JSON rendition is the only one asked for.
    stream_call = next(call for call in request.calls if "streamContent" in call["url"])
    assert stream_call["params"] == {"format": "json"}

    assert transcript.content_format == "text/vtt"
    assert transcript.content.startswith("WEBVTT")
    assert "<v Carmen Torres>Hicimos las pruebas anoche." in transcript.content
    assert "<v Camilo Calderón>Perfecto, reviso el caso." in transcript.content
    assert "00:00:04.798 --> 00:00:20.400" in transcript.content

    assert transcript.speakers == ("Carmen Torres", "Camilo Calderón")
    assert transcript.speaker_count == 2
    assert transcript.last_cue_seconds == pytest.approx(90.0)
    assert transcript.provenance.route is RouteKind.BROWSER
    driver.close()


def test_named_recording_skips_discovery_and_listing_omits_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, _manifest("t-1", "Sesión de pruebas.json")),
    )

    (transcript,) = _read(
        driver,
        TranscriptQuery(drive_id=_SHARED_DRIVE, item_id="01ORGANIZERITEM"),
        capability=Capability.MEETING_TRANSCRIPTS_LIST,
    )

    assert not any("children" in url or "sharedWithMe" in url for url in request.urls)
    assert not any("streamContent" in url for url in request.urls)
    assert transcript.content == "WEBVTT\n"
    assert transcript.speakers == ()
    driver.close()


def test_absent_transcript_is_not_a_failure_but_a_rejected_session_is(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, _ = _driver(
        tmp_path,
        monkeypatch,
        # A meeting recorded without transcription has no collection to read.
        _FakeResponse(404, {"error": {"code": "itemNotFound"}}),
    )
    assert _read(driver, TranscriptQuery(drive_id=_OWN_DRIVE, item_id="01OWNITEM")) == ()
    driver.close()

    rejected, _ = _driver(
        tmp_path / "second",
        monkeypatch,
        _FakeResponse(403, {"error": {"code": "accessDenied"}}),
    )
    with pytest.raises(ConnectorFailure) as failure:
        _read(rejected, TranscriptQuery(drive_id=_OWN_DRIVE, item_id="01OWNITEM"))
    assert failure.value.reason is ReasonCode.BROWSER_IDENTITY_UNAVAILABLE
    rejected.close()


def test_recordings_outside_the_window_are_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _own_recording() | {"lastModifiedDateTime": "2026-07-04T17:39:22Z"}
    driver, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, {"value": [stale]}),
        _FakeResponse(404, {"error": {"code": "itemNotFound"}}),
        _FakeResponse(200, {"value": []}),
    )

    assert _read(driver, TranscriptQuery(window=_WINDOW)) == ()
    assert not any("media/transcripts" in url for url in request.urls)
    driver.close()


def test_shared_recordings_can_be_excluded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, {"value": []}),
        _FakeResponse(404, {"error": {"code": "itemNotFound"}}),
    )

    assert _read(driver, TranscriptQuery(window=_WINDOW, include_shared=False)) == ()
    assert not any("sharedWithMe" in url for url in request.urls)
    driver.close()


def test_transcript_records_never_carry_the_session_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, request = _driver(
        tmp_path,
        monkeypatch,
        _FakeResponse(200, _manifest("t-1", "Sesión de pruebas.json")),
        _FakeResponse(200, _stream(("Camilo Calderón", "Arrancamos.", "00:00:01.0", "00:00:04.0"))),
    )

    transcripts = _read(driver, TranscriptQuery(drive_id=_OWN_DRIVE, item_id="01OWNITEM"))

    assert all(call["authorized"] for call in request.calls)
    assert _TOKEN not in to_json({"records": [transcripts[0].__class__.__name__]})
    assert _TOKEN not in transcripts[0].content
    driver.close()
