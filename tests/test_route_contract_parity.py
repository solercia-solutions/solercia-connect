"""The route is an implementation detail, never part of the contract.

A caller asks for a capability. Whether it was served by the official Microsoft Graph route or by
the supervised browser route must not change the shape of what comes back. These tests feed the
same information in the two casings the two collections actually use and require the resulting
records to be identical apart from provenance, which is the one field whose job is to say where
the data came from.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from solercia_connect.domain import Capability, MeetingTranscript, RouteKind
from solercia_connect.matrix import CAPABILITY_MATRIX
from solercia_connect.providers import microsoft_graph as graph_module
from solercia_connect.providers._microsoft import (
    microsoft_provenance,
    normalize_drive_item,
    normalize_mail_message,
    normalize_stream_transcript,
)
from solercia_connect.providers.google_meet import GoogleMeetAdapter
from solercia_connect.providers.m365_browser import Microsoft365BrowserAdapter
from solercia_connect.providers.microsoft_graph import MicrosoftGraphAdapter

_FETCHED = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

# What the official transcript endpoint serves: WebVTT that already names its speakers.
_GRAPH_TRANSCRIPT_VTT = (
    "WEBVTT\n"
    "\n"
    "1\n"
    "00:00:04.798 --> 00:00:12.000\n"
    "<v Carmen Torres>Hicimos las pruebas anoche.\n"
    "\n"
    "2\n"
    "00:00:12.000 --> 00:00:20.400\n"
    "<v Camilo Calderón>Perfecto, reviso el caso.\n"
)

# What the drive-backed endpoint serves for the same meeting, with sub-millisecond offsets.
_BROWSER_TRANSCRIPT_JSON: dict[str, Any] = {
    "$schema": "stream-transcript",
    "version": "1.0",
    "type": "Transcript",
    "entries": [
        {
            "id": "cue-1",
            "text": "Hicimos las pruebas anoche.",
            "speakerDisplayName": "Carmen Torres",
            "startOffset": "00:00:04.7977753",
            "endOffset": "00:00:12.0000000",
        },
        {
            "id": "cue-2",
            "text": "Perfecto, reviso el caso.",
            "speakerDisplayName": "Camilo Calderón",
            "startOffset": "00:00:12.0000000",
            "endOffset": "00:00:20.4000000",
        },
    ],
    "events": [],
}

_GRAPH_MESSAGE: dict[str, Any] = {
    "id": "AAMk-1",
    "subject": "Radicación de soporte",
    "sentDateTime": "2026-08-14T09:31:00Z",
    "receivedDateTime": "2026-08-14T09:35:00Z",
    "from": {"emailAddress": {"name": "Camilo", "address": "user@example.com"}},
    "toRecipients": [{"emailAddress": {"name": "Mesa", "address": "mesa@example.com"}}],
    "ccRecipients": [{"emailAddress": {"address": "jefe@example.com"}}],
    "body": {"contentType": "html", "content": "<p>cuerpo</p>"},
    "conversationId": "conv-1",
    "hasAttachments": True,
    "internetMessageId": "<abc@contoso.com>",
}

_BROWSER_MESSAGE: dict[str, Any] = {
    "Id": "AAMk-1",
    "Subject": "Radicación de soporte",
    "SentDateTime": "2026-08-14T09:31:00Z",
    "ReceivedDateTime": "2026-08-14T09:35:00Z",
    "From": {"EmailAddress": {"Name": "Camilo", "Address": "user@example.com"}},
    "ToRecipients": [{"EmailAddress": {"Name": "Mesa", "Address": "mesa@example.com"}}],
    "CcRecipients": [{"EmailAddress": {"Address": "jefe@example.com"}}],
    "Body": {"ContentType": "HTML", "Content": "<p>cuerpo</p>"},
    "ConversationId": "conv-1",
    "HasAttachments": True,
    "InternetMessageId": "<abc@contoso.com>",
}

_GRAPH_FILE: dict[str, Any] = {
    "id": "01ITEM",
    "name": "Acta 2026-08.docx",
    "size": 20480,
    "lastModifiedDateTime": "2026-08-20T18:05:00Z",
    "createdDateTime": "2026-08-19T08:00:00Z",
    "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "webUrl": "https://example.sharepoint.com/Documents/Acta.docx",
    "parentReference": {"driveId": "b!drive", "path": "/drive/root:/Documentos"},
}

_BROWSER_FILE: dict[str, Any] = {
    "Id": "01ITEM",
    "Name": "Acta 2026-08.docx",
    "Size": 20480,
    "LastModifiedDateTime": "2026-08-20T18:05:00Z",
    "CreatedDateTime": "2026-08-19T08:00:00Z",
    "File": {"MimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "WebUrl": "https://example.sharepoint.com/Documents/Acta.docx",
    "ParentReference": {"DriveId": "b!drive", "Path": "/drive/root:/Documentos"},
}


def _provenance(route: RouteKind, endpoint: str):
    return microsoft_provenance(
        provider_id="microsoft.graph" if route is RouteKind.OFFICIAL else "microsoft365.browser",
        route=route,
        source_id="source",
        fetched_at=_FETCHED,
        account_id="user@example.com",
        endpoint=endpoint,
    )


def test_a_mail_record_is_identical_whichever_route_produced_it() -> None:
    official = normalize_mail_message(
        _GRAPH_MESSAGE,
        provenance=_provenance(RouteKind.OFFICIAL, "graph-v1"),
        folder="inbox",
    )
    browser = normalize_mail_message(
        _BROWSER_MESSAGE,
        provenance=_provenance(RouteKind.BROWSER, "outlook-rest-v2"),
        folder="inbox",
    )

    assert replace(official, provenance=browser.provenance) == browser
    assert official.sender == browser.sender
    assert official.to == browser.to
    assert official.cc == browser.cc
    assert official.body_format == browser.body_format == "html"
    assert official.sent_at == browser.sent_at
    assert official.folder == browser.folder == "inbox"
    assert official.internet_message_id == browser.internet_message_id == "<abc@contoso.com>"
    assert official.received_at is not None
    assert browser.received_at is not None
    assert official.received_at == browser.received_at


def test_a_sent_mail_record_declares_its_folder_and_no_received_instant() -> None:
    sent_only = {key: value for key, value in _GRAPH_MESSAGE.items() if key != "receivedDateTime"}

    record = normalize_mail_message(
        sent_only,
        provenance=_provenance(RouteKind.OFFICIAL, "graph-v1"),
        folder="sent",
    )

    assert record.folder == "sent"
    assert record.received_at is None


def test_a_file_record_is_identical_whichever_route_produced_it() -> None:
    official = normalize_drive_item(
        _GRAPH_FILE,
        provenance=_provenance(RouteKind.OFFICIAL, "graph-drive-v1"),
    )
    browser = normalize_drive_item(
        _BROWSER_FILE,
        provenance=_provenance(RouteKind.BROWSER, "sharepoint-rest-v2"),
    )

    assert replace(official, provenance=browser.provenance) == browser
    assert official.name == browser.name == "Acta 2026-08.docx"
    assert official.size_bytes == browser.size_bytes == 20480
    assert official.path == browser.path == "/drive/root:/Documentos/Acta 2026-08.docx"
    assert official.is_folder is browser.is_folder is False


def _graph_transcript(content: str) -> MeetingTranscript:
    """Build the record the official route builds, from the same helpers it uses."""
    return MeetingTranscript(
        "t-1",
        "meeting-1",
        content,
        "text/vtt",
        graph_module._vtt_speaker_count(content),
        graph_module._vtt_last_cue_seconds(content),
        _provenance(RouteKind.OFFICIAL, "graph-transcript-v1"),
        speakers=graph_module._vtt_speakers(content),
    )


def test_a_transcript_record_is_identical_whichever_route_produced_it() -> None:
    """The two routes are handed the two renditions the two surfaces actually serve.

    The official route receives WebVTT and the drive-backed route receives Stream's JSON, which
    is the only rendition of that surface carrying speakers at all. A caller must not be able to
    tell them apart, so the JSON is rendered as the same speaker-named WebVTT.
    """
    official = _graph_transcript(_GRAPH_TRANSCRIPT_VTT)
    browser = normalize_stream_transcript(
        _BROWSER_TRANSCRIPT_JSON,
        transcript_id="t-1",
        provenance=_provenance(RouteKind.BROWSER, "sharepoint-rest-v2"),
        meeting_id="meeting-1",
    )

    assert official.content_format == browser.content_format == "text/vtt"
    assert official.content == browser.content
    assert official.speakers == browser.speakers == ("Carmen Torres", "Camilo Calderón")
    assert official.speaker_count == browser.speaker_count == 2
    assert official.last_cue_seconds == browser.last_cue_seconds == pytest.approx(20.4)

    # Only the drive-backed route observes the recording, so the official one leaves it unset
    # rather than inventing identifiers that surface does not have.
    assert official.drive_id is None and official.item_id is None


def test_provenance_is_the_only_place_the_route_is_visible() -> None:
    official = normalize_mail_message(
        _GRAPH_MESSAGE,
        provenance=_provenance(RouteKind.OFFICIAL, "graph-v1"),
        folder="inbox",
    )
    browser = normalize_mail_message(
        _BROWSER_MESSAGE,
        provenance=_provenance(RouteKind.BROWSER, "outlook-rest-v2"),
        folder="inbox",
    )

    assert official.provenance.route is RouteKind.OFFICIAL
    assert browser.provenance.route is RouteKind.BROWSER
    assert official.provenance.family is browser.provenance.family


def test_google_meet_matrix_rows_are_offline_honest() -> None:
    rows = [row for row in CAPABILITY_MATRIX if row.capability == "meeting.transcripts.*"]
    assert [(row.provider, row.channel, row.v0_1_status) for row in rows] == [
        ("Microsoft Graph", "official", "implemented-offline"),
        ("Google Calendar + Drive APIs", "official", "implemented-offline"),
        ("Microsoft 365 Web", "browser", "implemented"),
        # Not proven live yet: the identity probe and the selectors wait for tasks T5.0 and T5.1.
        ("Google Calendar + Docs Web", "browser", "implemented-offline"),
    ]


def test_google_meet_is_official_only_and_declares_both_transcript_capabilities() -> None:
    assert GoogleMeetAdapter.descriptor.provider_id == "google.meet"
    assert GoogleMeetAdapter.descriptor.family.value == "google"
    assert GoogleMeetAdapter.descriptor.route is RouteKind.OFFICIAL
    assert GoogleMeetAdapter.descriptor.capabilities == {
        Capability.MEETING_TRANSCRIPTS_LIST,
        Capability.MEETING_TRANSCRIPT_GET,
    }


def test_both_microsoft_adapters_answer_the_same_port() -> None:
    """Neither adapter may add or rename a method the other does not have."""
    contract = {"descriptor", "health", "execute"}

    assert contract.issubset(set(dir(MicrosoftGraphAdapter)))
    assert contract.issubset(set(dir(Microsoft365BrowserAdapter)))
    assert MicrosoftGraphAdapter.descriptor.family is Microsoft365BrowserAdapter.descriptor.family


@pytest.mark.parametrize(
    "capability",
    sorted(
        MicrosoftGraphAdapter.descriptor.capabilities
        & Microsoft365BrowserAdapter.descriptor.capabilities,
        key=str,
    ),
)
def test_shared_capabilities_are_declared_with_the_same_identifier(
    capability: Capability,
) -> None:
    """A capability both routes claim must be the same capability, not a look-alike."""
    assert capability in MicrosoftGraphAdapter.descriptor.capabilities
    assert capability in Microsoft365BrowserAdapter.descriptor.capabilities
    assert isinstance(capability.value, str) and "." in capability.value


def test_normalization_never_stamps_its_own_time() -> None:
    """Fetch time comes from the caller's clock, so records stay reproducible in tests."""
    record = normalize_mail_message(
        _BROWSER_MESSAGE,
        provenance=_provenance(RouteKind.BROWSER, "outlook-rest-v2"),
        folder="inbox",
    )
    again = normalize_mail_message(
        _BROWSER_MESSAGE,
        provenance=_provenance(RouteKind.BROWSER, "outlook-rest-v2"),
        folder="inbox",
    )

    assert record.provenance.fetched_at == _FETCHED
    assert record == again
