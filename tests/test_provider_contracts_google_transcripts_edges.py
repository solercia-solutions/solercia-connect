from __future__ import annotations

import pytest

from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    ReasonCode,
    TranscriptQuery,
    record_matches_capability,
)
from solercia_connect.envelope import RECORD_SCHEMA, versioned_record
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import TransportResponse
from tests.fakes import json_response
from tests.test_provider_contracts_google_transcripts import (
    WINDOW,
    _adapter,
    _attachment,
    _event,
    _page,
)


def test_window_paginates_filters_and_orders_transcripts() -> None:
    ignored = _event(
        "ignored",
        "2026-08-21T10:00:00Z",
        [
            _attachment("recording", "Recording", "video/mp4"),
            _attachment("chat", "Chat", "text/plain"),
            _attachment("notes", "Notas de Gemini"),
        ],
    )
    adapter, transport = _adapter(
        json_response(200, {"id": "user@example.com"}),
        _page(
            _event("late", "2026-08-20T10:00:00Z", [_attachment("late-file", "Late Transcript")]),
            ignored,
            token="page-2",
        ),
        TransportResponse(200, {}, b"Late: text"),
        _page(
            _event(
                "early", "2026-08-10T10:00:00Z", [_attachment("early-file", "Early - TRANSCRIPT")]
            )
        ),
        TransportResponse(200, {}, b"Early: text"),
    )
    records = adapter.list_transcripts(
        TranscriptQuery(window=WINDOW, account_id="user@example.com")
    )

    assert [record.meeting_id for record in records] == ["early", "late"]
    assert len(transport.requests) == 5 and transport.requests[3].params["pageToken"] == "page-2"
    params = transport.requests[1].params
    assert (
        params["timeMin"] == "2026-08-01T00:00:00+00:00"
        and params["timeMax"] == "2026-09-01T00:00:00+00:00"
    )
    assert "attachments(fileId,title,mimeType)" in str(params["fields"])


def test_transcript_get_has_record_contract_shape() -> None:
    adapter, _ = _adapter(
        json_response(200, {"id": "user@example.com"}),
        _page(_event("event-1", "2026-08-10T10:00:00Z", [_attachment("file-1", "Transcript")])),
        TransportResponse(200, {}, b"Alice: hello"),
    )
    records = adapter.execute(
        ConnectorRequest.create(
            "google.meet", Capability.MEETING_TRANSCRIPT_GET, TranscriptQuery(window=WINDOW)
        )
    )
    assert len(records) == 1 and record_matches_capability(
        records[0], Capability.MEETING_TRANSCRIPT_GET
    )
    assert versioned_record(records[0])["schema"] == RECORD_SCHEMA


def test_drive_selectors_fail_closed_before_reads() -> None:
    adapter, transport = _adapter()
    with pytest.raises(ConnectorFailure) as captured:
        adapter.list_transcripts(TranscriptQuery(drive_id="drive", item_id="item"))
    assert captured.value.reason is ReasonCode.INVALID_INPUT and not transport.requests


def test_identity_mismatch_happens_before_calendar_data() -> None:
    adapter, transport = _adapter(json_response(200, {"id": "other@example.com"}))
    with pytest.raises(ConnectorFailure) as captured:
        adapter.list_transcripts(TranscriptQuery(meeting_id="event", account_id="user@example.com"))
    assert (
        captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
        and len(transport.requests) == 1
    )


def test_drive_permission_failure_is_typed_and_safe() -> None:
    adapter, _ = _adapter(
        json_response(200, {"id": "user@example.com"}),
        json_response(
            200, _event("event", "2026-08-10T10:00:00Z", [_attachment("file", "Transcript")])
        ),
        TransportResponse(403, {}, b"raw provider body bearer google-secret"),
    )
    with pytest.raises(ConnectorFailure) as captured:
        adapter.list_transcripts(TranscriptQuery(meeting_id="event"))
    assert captured.value.reason is ReasonCode.AUTHORIZATION_DENIED
    assert (
        "raw provider body" not in captured.value.safe_message
        and "google-secret" not in captured.value.safe_message
    )


@pytest.mark.parametrize("content", [b"\xff", b"x" * (10 * 1024 * 1024 + 1)])
def test_malformed_export_is_typed(content: bytes) -> None:
    adapter, _ = _adapter(
        json_response(200, {"id": "user@example.com"}),
        json_response(
            200, _event("event", "2026-08-10T10:00:00Z", [_attachment("file", "Transcript")])
        ),
        TransportResponse(200, {}, content),
    )
    with pytest.raises(ConnectorFailure) as captured:
        adapter.list_transcripts(TranscriptQuery(meeting_id="event"))
    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


def test_window_without_transcripts_stays_empty() -> None:
    adapter, _ = _adapter(
        json_response(200, {"id": "user@example.com"}),
        _page(_event("event", "2026-08-10T10:00:00Z", [])),
    )
    assert adapter.list_transcripts(TranscriptQuery(window=WINDOW)) == ()
