"""Shared Microsoft 365 normalization.

Both Microsoft routes return the same records. The official route reads Microsoft Graph and the
supervised browser route reads the equivalent Outlook and SharePoint REST collections. Those
collections carry the same information under different key casing, so normalization lives here
once instead of drifting apart in two adapters. A caller cannot tell the routes apart from the
records, which is the point: the route is an implementation detail, not part of the contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from solercia_connect.domain import (
    CalendarEvent,
    DriveFile,
    EmailAddress,
    MailAttachment,
    MailMessage,
    MeetingTranscript,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
)
from solercia_connect.errors import ConnectorFailure


def field(item: Mapping[str, Any], *names: str) -> Any:
    """Read a field regardless of the casing the Microsoft collection used."""
    lowered = {key.casefold(): value for key, value in item.items() if isinstance(key, str)}
    for name in names:
        if name in item:
            return item[name]
        candidate = lowered.get(name.casefold())
        if candidate is not None:
            return candidate
    return None


def microsoft_datetime(value: object, label: str) -> datetime:
    parsed = optional_microsoft_datetime(value)
    if parsed is None:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            f"Microsoft record carried an unusable {label}",
        )
    return parsed


def optional_microsoft_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def microsoft_address(value: object) -> EmailAddress | None:
    if not isinstance(value, Mapping):
        return None
    inner = field(value, "emailAddress", "EmailAddress")
    if not isinstance(inner, Mapping):
        inner = value
    address = field(inner, "address", "Address")
    if not isinstance(address, str) or not address.strip():
        return None
    name = field(inner, "name", "Name")
    return EmailAddress(address, name.strip() if isinstance(name, str) and name.strip() else None)


def microsoft_addresses(value: object) -> tuple[EmailAddress, ...]:
    if not isinstance(value, list):
        return ()
    resolved = (microsoft_address(item) for item in value)
    return tuple(address for address in resolved if address is not None)


def normalize_mail_message(
    item: Mapping[str, Any],
    *,
    provenance: Provenance,
) -> MailMessage:
    identifier = field(item, "id", "Id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Microsoft message is missing a usable identifier",
        )
    body = field(item, "body", "Body")
    content = ""
    body_format = "text"
    if isinstance(body, Mapping):
        raw_content = field(body, "content", "Content")
        content = raw_content if isinstance(raw_content, str) else ""
        raw_format = field(body, "contentType", "ContentType")
        if isinstance(raw_format, str) and raw_format.strip():
            body_format = raw_format.strip().casefold()
    subject = field(item, "subject", "Subject")
    conversation = field(item, "conversationId", "ConversationId")
    sender = microsoft_address(field(item, "from", "From")) or microsoft_address(
        field(item, "sender", "Sender")
    )
    return MailMessage(
        identifier,
        subject if isinstance(subject, str) else "",
        microsoft_datetime(field(item, "sentDateTime", "SentDateTime"), "sentDateTime"),
        sender,
        microsoft_addresses(field(item, "toRecipients", "ToRecipients")),
        microsoft_addresses(field(item, "ccRecipients", "CcRecipients")),
        content,
        body_format,
        conversation if isinstance(conversation, str) and conversation.strip() else None,
        bool(field(item, "hasAttachments", "HasAttachments")),
        provenance,
    )


def microsoft_event_datetime(value: object, label: str) -> datetime:
    """Read a Microsoft date-time pair, treating a naive value as the zone it declares."""
    if not isinstance(value, Mapping):
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            f"Microsoft event is missing a usable {label}",
        )
    raw = field(value, "dateTime", "DateTime")
    parsed = optional_microsoft_datetime(raw)
    if parsed is None:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            f"Microsoft event carried an unparsable {label}",
        )
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _attendee_addresses(value: object) -> tuple[EmailAddress, ...]:
    if not isinstance(value, list):
        return ()
    resolved = (microsoft_address(item) for item in value)
    return tuple(address for address in resolved if address is not None)


def normalize_calendar_event(
    item: Mapping[str, Any],
    *,
    provenance: Provenance,
) -> CalendarEvent:
    identifier = field(item, "id", "Id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Microsoft event is missing a usable identifier",
        )
    subject = field(item, "subject", "Subject")
    response = field(item, "responseStatus", "ResponseStatus")
    response_value = (
        field(response, "response", "Response") if isinstance(response, Mapping) else None
    )
    return CalendarEvent(
        identifier,
        subject if isinstance(subject, str) else "",
        microsoft_event_datetime(field(item, "start", "Start"), "start"),
        microsoft_event_datetime(field(item, "end", "End"), "end"),
        microsoft_address(field(item, "organizer", "Organizer")),
        _attendee_addresses(field(item, "attendees", "Attendees")),
        response_value if isinstance(response_value, str) and response_value.strip() else None,
        bool(field(item, "isOnlineMeeting", "IsOnlineMeeting")),
        bool(field(item, "isCancelled", "IsCancelled")),
        provenance,
    )


def normalize_mail_attachment(item: Mapping[str, Any]) -> MailAttachment | None:
    """Normalize one attachment; unsupported kinds without a name are skipped, not invented."""
    identifier = field(item, "id", "Id")
    name = field(item, "name", "Name")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    size = field(item, "size", "Size")
    content_type = field(item, "contentType", "ContentType")
    content = field(item, "contentBytes", "ContentBytes")
    return MailAttachment(
        identifier,
        name,
        content_type if isinstance(content_type, str) and content_type.strip() else None,
        int(size) if isinstance(size, int) else None,
        bool(field(item, "isInline", "IsInline")),
        content if isinstance(content, str) and content else None,
    )


def normalize_drive_item(
    item: Mapping[str, Any],
    *,
    provenance: Provenance,
    content_base64: str | None = None,
) -> DriveFile:
    identifier = field(item, "id", "Id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Microsoft drive item is missing a usable identifier",
        )
    name = field(item, "name", "Name")
    if not isinstance(name, str) or not name.strip():
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Microsoft drive item is missing a usable name",
        )
    parent = field(item, "parentReference", "ParentReference")
    parent_mapping = parent if isinstance(parent, Mapping) else {}
    parent_path = field(parent_mapping, "path", "Path")
    file_facet = field(item, "file", "File")
    size = field(item, "size", "Size")
    drive_id = field(parent_mapping, "driveId", "DriveId")
    web_url = field(item, "webUrl", "WebUrl")
    mime_type = (
        field(file_facet, "mimeType", "MimeType") if isinstance(file_facet, Mapping) else None
    )
    return DriveFile(
        identifier,
        name,
        f"{parent_path}/{name}" if isinstance(parent_path, str) and parent_path else None,
        int(size) if isinstance(size, int) else None,
        optional_microsoft_datetime(field(item, "lastModifiedDateTime", "LastModifiedDateTime")),
        optional_microsoft_datetime(field(item, "createdDateTime", "CreatedDateTime")),
        mime_type if isinstance(mime_type, str) else None,
        web_url if isinstance(web_url, str) else None,
        drive_id if isinstance(drive_id, str) else None,
        field(item, "folder", "Folder") is not None,
        content_base64,
        provenance,
    )


def microsoft_provenance(
    *,
    provider_id: str,
    route: RouteKind,
    source_id: str,
    fetched_at: datetime,
    account_id: str | None,
    endpoint: str,
) -> Provenance:
    return Provenance(
        provider_id,
        ProviderFamily.MICROSOFT,
        route,
        source_id,
        fetched_at,
        account_id,
        endpoint,
    )


def transcript_offset_seconds(value: object) -> float | None:
    """Read one Stream offset, which is ``HH:MM:SS.fffffff`` rather than a number."""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _vtt_timestamp(seconds: float) -> str:
    hours, rest = divmod(max(seconds, 0.0), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def stream_transcript_to_vtt(
    payload: Mapping[str, Any],
) -> tuple[str, tuple[str, ...], float | None]:
    """Render Stream's transcript JSON as WebVTT that names each speaker.

    The same endpoint also serves WebVTT directly, but that rendition carries no speaker at all,
    so consecutive turns collapse into a single unattributed block. The JSON rendition is the only
    one that names who spoke, and every route is expected to answer with the same cue format, so
    the naming is restored here rather than left for each caller to reinvent.
    """
    raw_entries = payload.get("entries")
    entries: list[Mapping[str, Any]] = (
        [entry for entry in raw_entries if isinstance(entry, Mapping)]
        if isinstance(raw_entries, list)
        else []
    )

    lines = ["WEBVTT", ""]
    speakers: list[str] = []
    last_cue: float | None = None

    for index, entry in enumerate(entries, start=1):
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        start = transcript_offset_seconds(entry.get("startOffset"))
        end = transcript_offset_seconds(entry.get("endOffset"))
        if start is None or end is None:
            continue
        last_cue = end if last_cue is None else max(last_cue, end)

        raw_speaker = entry.get("speakerDisplayName")
        speaker = raw_speaker.strip() if isinstance(raw_speaker, str) else ""
        if speaker and speaker not in speakers:
            speakers.append(speaker)

        lines.append(str(index))
        lines.append(f"{_vtt_timestamp(start)} --> {_vtt_timestamp(end)}")
        lines.append(f"<v {speaker}>{text.strip()}" if speaker else text.strip())
        lines.append("")

    return "\n".join(lines), tuple(speakers), last_cue


def normalize_stream_transcript(
    payload: Mapping[str, Any],
    *,
    transcript_id: str,
    provenance: Provenance,
    display_name: str | None = None,
    recorded_at: datetime | None = None,
    drive_id: str | None = None,
    item_id: str | None = None,
    organizer: str | None = None,
    meeting_id: str | None = None,
) -> MeetingTranscript:
    """Build the shared transcript record from Stream's JSON rendition."""
    if not transcript_id.strip():
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Microsoft transcript is missing a usable identifier",
        )
    content, speakers, last_cue = stream_transcript_to_vtt(payload)
    return MeetingTranscript(
        transcript_id,
        meeting_id,
        content,
        "text/vtt",
        len(speakers) or None,
        last_cue,
        provenance,
        display_name=display_name,
        recorded_at=recorded_at,
        drive_id=drive_id,
        item_id=item_id,
        organizer=organizer,
        speakers=speakers,
    )
