from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from solercia_connect.auth import AuthorizationProvider
from solercia_connect.domain import (
    CalendarEvent,
    CalendarQuery,
    Capability,
    ConnectorRequest,
    DateWindow,
    DriveFile,
    EmailAddress,
    FileQuery,
    HealthReport,
    HealthState,
    MailMessage,
    MailQuery,
    MeetingTranscript,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    TranscriptQuery,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import HTTPTransport, ProviderDescriptor
from solercia_connect.providers._microsoft import (
    microsoft_provenance,
    normalize_drive_item,
    normalize_mail_attachment,
    normalize_mail_message,
)
from solercia_connect.providers._shared import (
    as_string,
    ensure_official_https_base_url,
    lower_domain,
    parse_datetime,
    require_list,
    require_mapping,
    require_same_origin_https_url,
    require_string,
)
from solercia_connect.transport import request_bytes, request_json

_MICROSOFT_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class MicrosoftGraphAdapter:
    descriptor = ProviderDescriptor(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset(
            {
                Capability.MAIL_SENT_LIST,
                Capability.CALENDAR_EVENTS_LIST,
                Capability.MEETING_TRANSCRIPTS_LIST,
                Capability.MEETING_TRANSCRIPT_GET,
                Capability.FILES_SEARCH,
                Capability.FILES_GET,
            }
        ),
    )

    def __init__(
        self,
        transport: HTTPTransport,
        auth: AuthorizationProvider,
        *,
        expected_account: str | None = None,
        base_url: str = _MICROSOFT_GRAPH_BASE_URL,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._transport = transport
        self._auth = auth
        self._expected_account = expected_account.lower() if expected_account else None
        self._base_url = ensure_official_https_base_url(
            base_url,
            "Microsoft Graph base URL",
            _MICROSOFT_GRAPH_BASE_URL,
        )
        self._clock = clock

    def health(self) -> HealthReport:
        account_id = self._identity()
        return HealthReport(
            self.descriptor.provider_id,
            HealthState.READY,
            ReasonCode.SUCCESS,
            tuple(sorted(self.descriptor.capabilities, key=str)),
            account_id,
            "Microsoft Graph identity and read capability are reachable",
        )

    def execute(
        self, request: ConnectorRequest
    ) -> tuple[MailMessage | CalendarEvent | MeetingTranscript | DriveFile, ...]:
        if request.capability is Capability.MAIL_SENT_LIST and isinstance(
            request.payload, MailQuery
        ):
            return self.list_sent_mail(request.payload)
        if request.capability is Capability.CALENDAR_EVENTS_LIST and isinstance(
            request.payload, CalendarQuery
        ):
            return self.list_events(request.payload)
        if request.capability in {
            Capability.MEETING_TRANSCRIPTS_LIST,
            Capability.MEETING_TRANSCRIPT_GET,
        } and isinstance(request.payload, TranscriptQuery):
            return self.list_transcripts(
                request.payload,
                first_only=request.capability is Capability.MEETING_TRANSCRIPT_GET,
            )
        if request.capability in {
            Capability.FILES_SEARCH,
            Capability.FILES_GET,
        } and isinstance(request.payload, FileQuery):
            return self.list_files(
                request.payload,
                with_content=request.capability is Capability.FILES_GET,
            )
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "Microsoft Graph request does not match an implemented capability contract",
        )

    def list_sent_mail(self, query: MailQuery) -> tuple[MailMessage, ...]:
        account_id = self._identity()
        if query.account_id and query.account_id.lower() != account_id.lower():
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Requested mailbox does not match the authenticated Microsoft account",
            )
        params: dict[str, Any] = {
            "$filter": (
                f"sentDateTime ge {_graph_datetime(query.window.start)} "
                f"and sentDateTime lt {_graph_datetime(query.window.end)}"
            ),
            "$select": (
                "id,subject,sentDateTime,from,sender,toRecipients,ccRecipients,body,"
                "conversationId,hasAttachments"
            ),
            "$orderby": "sentDateTime desc",
            "$top": min(query.limit, 100),
        }
        items = self._paged_values(
            f"{self._base_url}/me/mailFolders/sentitems/messages", params, query.limit
        )
        messages = [self._normalize_message(item, account_id) for item in items]
        if query.include_attachments:
            messages = [self._message_with_attachments(message, query) for message in messages]
        return tuple(messages)

    def _message_with_attachments(self, message: MailMessage, query: MailQuery) -> MailMessage:
        """Attach the message's own files, dropping content that exceeds the byte budget."""
        if not message.has_attachments:
            return message
        payload = request_json(
            self._transport,
            "GET",
            f"{self._base_url}/me/messages/{quote(message.id, safe='')}/attachments",
            headers=self._headers(),
            # contentBytes belongs to a subtype, so any projection drops the very content the
            # caller asked for. The collection is requested whole.
            params=None,
        )
        data = require_mapping(payload, "microsoft.attachments")
        attachments = []
        for raw in require_list(data.get("value"), "microsoft.attachment.value"):
            if not isinstance(raw, dict):
                continue
            attachment = normalize_mail_attachment(raw)
            if attachment is None:
                continue
            if (
                attachment.size_bytes is not None
                and attachment.size_bytes > query.max_attachment_bytes
            ):
                attachment = replace(attachment, content_base64=None)
            attachments.append(attachment)
        return replace(message, attachments=tuple(attachments))

    def list_files(self, query: FileQuery, *, with_content: bool = False) -> tuple[DriveFile, ...]:
        """List, search or fetch drive items from OneDrive or a SharePoint document library."""
        account_id = self._identity()
        if query.account_id and query.account_id.lower() != account_id.lower():
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Requested drive does not match the authenticated Microsoft account",
            )
        drive_url = self._drive_base_url(query.site)
        select = (
            "id,name,size,lastModifiedDateTime,createdDateTime,file,folder,webUrl,parentReference"
        )

        if query.item_id is not None:
            payload = request_json(
                self._transport,
                "GET",
                f"{drive_url}/items/{quote(query.item_id.strip(), safe='')}",
                headers=self._headers(),
                params={"$select": select},
            )
            items = [require_mapping(payload, "microsoft.driveItem")]
        else:
            url, params = self._drive_listing(drive_url, query, select)
            items = self._paged_values(url, params, query.limit)

        files = [self._normalize_drive_item(item, account_id) for item in items]
        files = [item for item in files if _within_window(item, query.window)]
        if with_content:
            files = [self._with_content(item, drive_url, query.max_content_bytes) for item in files]
        return tuple(files[: query.limit])

    def _drive_base_url(self, site: str | None) -> str:
        if site is None:
            return f"{self._base_url}/me/drive"
        cleaned = site.strip().strip("/")
        return f"{self._base_url}/sites/{quote(cleaned, safe=':/')}/drive"

    def _drive_listing(
        self,
        drive_url: str,
        query: FileQuery,
        select: str,
    ) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {"$select": select, "$top": min(query.limit, 200)}
        if query.text is not None:
            escaped = query.text.strip().replace("'", "''")
            return f"{drive_url}/root/search(q='{quote(escaped, safe='')}')", params
        if query.folder_path is not None:
            path = query.folder_path.strip().strip("/")
            return f"{drive_url}/root:/{quote(path, safe='/')}:/children", params
        return f"{drive_url}/root/children", params

    def _with_content(self, item: DriveFile, drive_url: str, max_bytes: int) -> DriveFile:
        if item.is_folder:
            return item
        if item.size_bytes is not None and item.size_bytes > max_bytes:
            raise ConnectorFailure(
                ReasonCode.INVALID_INPUT,
                "Drive item exceeds the requested maximum content size",
            )
        response = request_bytes(
            self._transport,
            "GET",
            f"{drive_url}/items/{quote(item.id, safe='')}/content",
            headers=self._headers(),
        )
        content = response.content
        if len(content) > max_bytes:
            raise ConnectorFailure(
                ReasonCode.INVALID_INPUT,
                "Drive item exceeds the requested maximum content size",
            )
        return replace(item, content_base64=base64.b64encode(content).decode("ascii"))

    def _normalize_drive_item(self, item: dict[str, Any], account_id: str) -> DriveFile:
        """Normalize through the shared Microsoft layer, exactly like the browser route."""
        source_id = require_string(item.get("id"), "driveItem.id")
        return normalize_drive_item(
            item,
            provenance=microsoft_provenance(
                provider_id=self.descriptor.provider_id,
                route=self.descriptor.route,
                source_id=source_id,
                fetched_at=self._clock(),
                account_id=account_id,
                endpoint="graph-drive-v1",
            ),
        )

    def list_events(self, query: CalendarQuery) -> tuple[CalendarEvent, ...]:
        account_id = self._identity()
        if query.account_id and query.account_id.lower() != account_id.lower():
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Requested calendar does not match the authenticated Microsoft account",
            )
        if query.calendar_id not in {"primary", "default"}:
            endpoint = f"{self._base_url}/me/calendars/{query.calendar_id}/calendarView"
        else:
            endpoint = f"{self._base_url}/me/calendar/calendarView"
        params: dict[str, Any] = {
            "startDateTime": query.window.start.isoformat(),
            "endDateTime": query.window.end.isoformat(),
            "$top": min(query.limit, 1000),
            "$select": (
                "id,subject,start,end,organizer,attendees,responseStatus,isOnlineMeeting,"
                "isCancelled,isOrganizer"
            ),
        }
        items = self._paged_values(endpoint, params, query.limit)
        normalized = [self._normalize_event(item, account_id) for item in items]
        excluded_domains = {domain.lower() for domain in query.excluded_organizer_domains}
        return tuple(
            event
            for event in normalized
            if not event.is_cancelled
            and (not query.accepted_only or event.response_status in {"accepted", "organizer"})
            and lower_domain(event.organizer.address if event.organizer else None)
            not in excluded_domains
        )

    def list_transcripts(
        self,
        query: TranscriptQuery,
        *,
        first_only: bool = False,
    ) -> tuple[MeetingTranscript, ...]:
        if not query.meeting_id or not query.meeting_id.strip():
            # The browser route selects a transcript by the recording that carries it, which the
            # Graph transcript collection cannot address; this route needs the meeting itself.
            raise ConnectorFailure(
                ReasonCode.INVALID_INPUT,
                "A known Microsoft Teams online meeting ID is required by the official route",
            )
        meeting_id = query.meeting_id.strip()
        account_id = self._identity()
        if query.account_id and query.account_id.lower() != account_id.lower():
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Requested transcript account does not match the authenticated Microsoft account",
            )
        meeting_path = quote(meeting_id, safe="")
        collection_url = f"{self._base_url}/me/onlineMeetings/{meeting_path}/transcripts"
        try:
            items = self._paged_values(
                collection_url,
                {"$top": min(1 if first_only else query.limit, 50)},
                1 if first_only else min(query.limit, 50),
            )
        except ConnectorFailure as failure:
            if failure.reason is ReasonCode.AUTHORIZATION_DENIED:
                raise ConnectorFailure(
                    ReasonCode.TENANT_POLICY_BLOCKED,
                    "Microsoft Teams transcript access is disabled, not consented, or blocked "
                    "by tenant policy",
                ) from failure
            raise

        records: list[MeetingTranscript] = []
        for item in items:
            transcript_id = require_string(item.get("id"), "transcript.id")
            content_url = f"{collection_url}/{quote(transcript_id, safe='')}/content"
            try:
                response = request_bytes(
                    self._transport,
                    "GET",
                    content_url,
                    headers={**self._headers(), "Accept": "text/vtt"},
                )
            except ConnectorFailure as failure:
                if failure.reason is ReasonCode.AUTHORIZATION_DENIED:
                    raise ConnectorFailure(
                        ReasonCode.TENANT_POLICY_BLOCKED,
                        "Microsoft Teams transcript download is disabled or blocked by tenant "
                        "policy",
                    ) from failure
                raise
            if len(response.content) > 10 * 1024 * 1024:
                raise ConnectorFailure(
                    ReasonCode.MALFORMED_PROVIDER_DATA,
                    "Microsoft Teams transcript exceeds the fixed offline processing bound",
                )
            try:
                content = response.content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ConnectorFailure(
                    ReasonCode.MALFORMED_PROVIDER_DATA,
                    "Microsoft Teams transcript content is not UTF-8",
                ) from exc
            content_type = next(
                (
                    value.split(";", 1)[0].strip().casefold()
                    for key, value in response.headers.items()
                    if key.casefold() == "content-type"
                ),
                "text/vtt",
            )
            records.append(
                MeetingTranscript(
                    transcript_id,
                    meeting_id,
                    content,
                    content_type,
                    _vtt_speaker_count(content),
                    _vtt_last_cue_seconds(content),
                    Provenance(
                        self.descriptor.provider_id,
                        self.descriptor.family,
                        self.descriptor.route,
                        transcript_id,
                        self._clock(),
                        account_id,
                        "/me/onlineMeetings/{onlineMeetingId}/transcripts/{id}/content",
                    ),
                    speakers=_vtt_speakers(content),
                )
            )
        return tuple(records)

    def _identity(self) -> str:
        payload = request_json(
            self._transport,
            "GET",
            f"{self._base_url}/me",
            headers=self._headers(),
            params={"$select": "id,mail,userPrincipalName"},
        )
        data = require_mapping(payload, "microsoft.me")
        account = as_string(data.get("mail")) or as_string(data.get("userPrincipalName"))
        if not account:
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Microsoft Graph identity response has no account identifier",
            )
        if self._expected_account and account.lower() != self._expected_account:
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Authenticated Microsoft account does not match configured account",
            )
        return account

    def _paged_values(self, url: str, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params: dict[str, Any] | None = params
        pages = 0
        while next_url and len(items) < limit:
            pages += 1
            if pages > 20:
                raise ConnectorFailure(
                    ReasonCode.MALFORMED_PROVIDER_DATA,
                    "Microsoft Graph pagination exceeded the fixed page bound",
                )
            trusted_url = require_same_origin_https_url(
                next_url,
                self._base_url,
                "Microsoft Graph page URL",
            )
            payload = request_json(
                self._transport,
                "GET",
                trusted_url,
                headers=self._headers(),
                params=next_params,
            )
            data = require_mapping(payload, "microsoft.collection")
            for raw in require_list(data.get("value"), "microsoft.value"):
                items.append(require_mapping(raw, "microsoft.item"))
                if len(items) >= limit:
                    break
            raw_next = data.get("@odata.nextLink")
            next_url = raw_next if isinstance(raw_next, str) and raw_next else None
            next_params = None
        return items

    def _normalize_message(self, item: dict[str, Any], account_id: str) -> MailMessage:
        """Normalize through the shared Microsoft layer, exactly like the browser route."""
        source_id = require_string(item.get("id"), "message.id")
        return normalize_mail_message(
            item,
            provenance=microsoft_provenance(
                provider_id=self.descriptor.provider_id,
                route=self.descriptor.route,
                source_id=source_id,
                fetched_at=self._clock(),
                account_id=account_id,
                endpoint="/me/mailFolders/sentitems/messages",
            ),
        )

    def _normalize_event(self, item: dict[str, Any], account_id: str) -> CalendarEvent:
        source_id = require_string(item.get("id"), "event.id")
        start = require_mapping(item.get("start"), "event.start")
        end = require_mapping(item.get("end"), "event.end")
        response = require_mapping(item.get("responseStatus") or {}, "event.responseStatus")
        status = "organizer" if item.get("isOrganizer") else as_string(response.get("response"))
        return CalendarEvent(
            source_id,
            str(item.get("subject") or ""),
            parse_datetime(start.get("dateTime"), "event.start.dateTime"),
            parse_datetime(end.get("dateTime"), "event.end.dateTime"),
            _graph_address(item.get("organizer")),
            _graph_addresses(item.get("attendees") or []),
            status,
            bool(item.get("isOnlineMeeting")),
            bool(item.get("isCancelled")),
            Provenance(
                self.descriptor.provider_id,
                self.descriptor.family,
                self.descriptor.route,
                source_id,
                self._clock(),
                account_id,
                "/me/calendar/calendarView",
            ),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": self._auth.authorization_header(),
            "Prefer": 'outlook.body-content-type="text", outlook.timezone="UTC"',
        }


def _graph_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _graph_address(value: Any) -> EmailAddress | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("emailAddress")
    if not isinstance(raw, dict):
        return None
    address = as_string(raw.get("address"))
    if not address:
        return None
    return EmailAddress(address, as_string(raw.get("name")))


def _graph_addresses(value: Any) -> tuple[EmailAddress, ...]:
    if not isinstance(value, list):
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Microsoft recipient collection is malformed",
        )
    result: list[EmailAddress] = []
    for raw in value:
        parsed = _graph_address(raw)
        if parsed:
            result.append(parsed)
    return tuple(result)


def _vtt_speakers(content: str) -> tuple[str, ...]:
    """Name each speaker once, in the order the transcript first hears them."""
    ordered: list[str] = []
    seen: set[str] = set()
    for match in re.findall(r"<v\s+([^>]+)>", content, flags=re.IGNORECASE):
        speaker = match.strip()
        if speaker and speaker.casefold() not in seen:
            seen.add(speaker.casefold())
            ordered.append(speaker)
    return tuple(ordered)


def _vtt_speaker_count(content: str) -> int | None:
    speakers = _vtt_speakers(content)
    return len(speakers) if speakers else None


def _vtt_last_cue_seconds(content: str) -> float | None:
    values: list[float] = []
    for hours, minutes, seconds in re.findall(
        r"-->\s*(\d{2,}):(\d{2}):(\d{2}(?:\.\d{1,3})?)",
        content,
    ):
        values.append(int(hours) * 3600 + int(minutes) * 60 + float(seconds))
    return max(values) if values else None


def _within_window(item: DriveFile, window: DateWindow | None) -> bool:
    if window is None:
        return True
    if item.modified_at is None:
        return False
    return window.start <= item.modified_at < window.end
