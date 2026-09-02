from __future__ import annotations

from datetime import UTC, datetime

from solercia_connect.auth import BearerTokenProvider
from solercia_connect.domain import DateWindow, TranscriptQuery
from solercia_connect.ports import TransportResponse
from solercia_connect.providers.google_meet import GoogleMeetAdapter
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport, json_response

WINDOW = DateWindow(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))


def _event(event_id: str, start: str, attachments: list[dict[str, str]]) -> dict[str, object]:
    return {
        "id": event_id,
        "summary": event_id,
        "organizer": {"email": "organizer@example.com"},
        "start": {"dateTime": start},
        "attachments": attachments,
    }


def _attachment(
    file_id: str, title: str, mime: str = "application/vnd.google-apps.document"
) -> dict[str, str]:
    return {"fileId": file_id, "title": title, "mimeType": mime}


def _page(*events: dict[str, object], token: str | None = None) -> TransportResponse:
    payload: dict[str, object] = {"items": list(events)}
    if token:
        payload["nextPageToken"] = token
    return json_response(200, payload)


def _adapter(*responses: TransportResponse) -> tuple[GoogleMeetAdapter, FakeTransport]:
    transport = FakeTransport(*responses)
    return GoogleMeetAdapter(
        transport,
        BearerTokenProvider(SecretValue("google-secret")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    ), transport


def test_meeting_id_discovers_and_exports_transcript_as_plain_text() -> None:
    adapter, transport = _adapter(
        json_response(200, {"id": "user@example.com"}),
        json_response(
            200,
            _event(
                "event/1", "2026-08-10T15:00:00Z", [_attachment("file/1", "Event - Transcript")]
            ),
        ),
        TransportResponse(
            200, {"Content-Type": "text/plain"}, "Transcripción\nAlice: Hola\nBob: Adiós\n".encode()
        ),
    )
    records = adapter.list_transcripts(
        TranscriptQuery(meeting_id="event/1", account_id="user@example.com")
    )

    record = records[0]
    assert (record.id, record.meeting_id, record.content_format) == (
        "meet:event/1:file/1",
        "event/1",
        "text/plain",
    )
    assert (record.speakers, record.speaker_count, record.last_cue_seconds) == (
        ("Alice", "Bob"),
        2,
        None,
    )
    assert record.recorded_at == datetime(2026, 8, 10, 15, tzinfo=UTC)
    assert (
        record.organizer == "organizer@example.com"
        and record.provenance.endpoint == "calendar→drive"
    )
    assert transport.requests[1].url.endswith("/calendars/user%40example.com/events/event%2F1")
    assert transport.requests[1].params == {
        "fields": "id,summary,organizer,start,attachments(fileId,title,mimeType)"
    }
    assert transport.requests[2].url.endswith("/files/file%2F1/export")
    assert transport.requests[2].params == {"mimeType": "text/plain"}
