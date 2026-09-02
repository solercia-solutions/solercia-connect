from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from solercia_connect.auth import AuthorizationProvider
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    HealthReport,
    HealthState,
    MeetingTranscript,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    TranscriptQuery,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import HTTPTransport, ProviderDescriptor, TransportResponse
from solercia_connect.providers._shared import (
    as_string,
    ensure_official_https_base_url,
    require_list,
    require_mapping,
    require_string,
    unique_canonical_account_identity,
)
from solercia_connect.providers.google import (
    _configured_google_account,
    _google_address,
    _google_event_datetime,
    _requested_google_account,
)
from solercia_connect.transport import request_bytes, request_json

_CALENDAR_BASE_URL = "https://www.googleapis.com/calendar/v3"
_DRIVE_BASE_URL = "https://www.googleapis.com/drive/v3"
_TRANSCRIPT_MIME = "application/vnd.google-apps.document"
_MAX_EXPORT_BYTES = 10 * 1024 * 1024
_TURN_LINE = re.compile(r"^([^:\n]{2,80}):\s+", re.MULTILINE)
_FIELDS = "id,summary,organizer,start,attachments(fileId,title,mimeType)"
_WINDOW_FIELDS = f"items({_FIELDS}),nextPageToken".replace(" ", "")


class GoogleMeetAdapter:
    descriptor = ProviderDescriptor(
        "google.meet",
        ProviderFamily.GOOGLE,
        RouteKind.OFFICIAL,
        frozenset({Capability.MEETING_TRANSCRIPTS_LIST, Capability.MEETING_TRANSCRIPT_GET}),
    )

    def __init__(
        self,
        transport: HTTPTransport,
        auth: AuthorizationProvider,
        *,
        expected_account: str | None = None,
        calendar_base_url: str = _CALENDAR_BASE_URL,
        drive_base_url: str = _DRIVE_BASE_URL,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._transport, self._auth, self._clock = transport, auth, clock
        self._expected_account = _configured_google_account(expected_account)
        self._calendar_base_url = ensure_official_https_base_url(
            calendar_base_url, "Google Calendar base URL", _CALENDAR_BASE_URL
        )
        self._drive_base_url = ensure_official_https_base_url(
            drive_base_url, "Google Drive base URL", _DRIVE_BASE_URL
        )

    def health(self) -> HealthReport:
        account = self._verified_identity(None)
        return HealthReport(
            self.descriptor.provider_id,
            HealthState.READY,
            ReasonCode.SUCCESS,
            tuple(sorted(self.descriptor.capabilities, key=str)),
            account,
            "Google Meet Calendar and Drive transcript capabilities are reachable",
        )

    def execute(self, request: ConnectorRequest) -> tuple[MeetingTranscript, ...]:
        if request.capability in {
            Capability.MEETING_TRANSCRIPTS_LIST,
            Capability.MEETING_TRANSCRIPT_GET,
        } and isinstance(request.payload, TranscriptQuery):
            return self.list_transcripts(
                request.payload,
                first_only=request.capability is Capability.MEETING_TRANSCRIPT_GET,
            )
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "Google Meet request does not match the transcript capability contract",
        )

    def list_transcripts(
        self, query: TranscriptQuery, *, first_only: bool = False
    ) -> tuple[MeetingTranscript, ...]:
        self._validate_selector(query)
        account = self._verified_identity(query.account_id)
        limit = 1 if first_only else query.limit
        records = (
            self._transcript_records(self._get_event(account, query.meeting_id.strip()), account)
            if query.meeting_id is not None
            else self._window_records(query, account, limit)
        )
        records.sort(key=lambda r: (r.recorded_at or datetime.min.replace(tzinfo=UTC), r.id))
        return tuple(records[:limit])

    @staticmethod
    def _validate_selector(query: TranscriptQuery) -> None:
        if query.drive_id is not None or query.item_id is not None:
            raise ConnectorFailure(
                ReasonCode.INVALID_INPUT,
                "Google Meet transcripts se direccionan por meeting_id o window; los selectores "
                "de drive item son exclusivos de la ruta Microsoft",
            )
        if (query.meeting_id is None) == (query.window is None):
            raise ConnectorFailure(
                ReasonCode.INVALID_INPUT,
                "Google Meet transcripts requieren exactamente meeting_id o window",
            )

    def _verified_identity(self, requested_account: str | None) -> str:
        data = require_mapping(
            self._json(f"{self._calendar_base_url}/users/me/calendarList/primary"),
            "meet.identity",
        )
        observed = unique_canonical_account_identity(data.get("id"), exact=True)
        requested = _requested_google_account(requested_account)
        if observed is None or any(
            value is not None and value != observed for value in (self._expected_account, requested)
        ):
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Google Meet did not expose the configured authenticated account identity",
            )
        return observed

    def _get_event(self, account: str, event_id: str) -> dict[str, Any]:
        return require_mapping(
            self._json(
                f"{self._calendar_base_url}/calendars/{quote(account, safe='')}/events/"
                f"{quote(event_id, safe='')}",
                {"fields": _FIELDS},
            ),
            "meet.event",
        )

    def _window_records(
        self, query: TranscriptQuery, account: str, limit: int
    ) -> list[MeetingTranscript]:
        assert query.window is not None
        params: dict[str, Any] = {
            "timeMin": query.window.start.isoformat(),
            "timeMax": query.window.end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": min(limit, 2500),
            "fields": _WINDOW_FIELDS,
        }
        records: list[MeetingTranscript] = []
        token: str | None = None
        for _ in range(20):
            page_params = {**params, **({"pageToken": token} if token else {})}
            data = require_mapping(
                self._json(
                    f"{self._calendar_base_url}/calendars/{quote(account, safe='')}/events",
                    page_params,
                ),
                "meet.events",
            )
            for raw in require_list(data.get("items") or [], "meet.events.items"):
                event = require_mapping(raw, "meet.event")
                if _event_in_window(event, query):
                    records.extend(self._transcript_records(event, account))
                if len(records) >= limit:
                    return records[:limit]
            token = (
                data.get("nextPageToken") if isinstance(data.get("nextPageToken"), str) else None
            )
            if not token:
                return records
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Google Calendar pagination exceeded the fixed page bound",
        )

    def _transcript_records(self, event: dict[str, Any], account: str) -> list[MeetingTranscript]:
        event_id = require_string(event.get("id"), "meet.event.id")
        recorded_at = _google_event_datetime(event.get("start"), "meet.event.start")
        organizer = _google_address(event.get("organizer"))
        result: list[MeetingTranscript] = []
        for raw in require_list(event.get("attachments") or [], "meet.event.attachments"):
            attachment = require_mapping(raw, "meet.event.attachment")
            title = as_string(attachment.get("title")) or ""
            if (
                "transcript" not in title.casefold()
                or (as_string(attachment.get("mimeType")) or "").casefold() != _TRANSCRIPT_MIME
            ):
                continue
            file_id = require_string(attachment.get("fileId"), "meet.attachment.fileId")
            content = self._content(self._drive_export(file_id))
            speakers = _speakers(content)
            source_id = f"meet:{event_id}:{file_id}"
            result.append(
                MeetingTranscript(
                    source_id,
                    event_id,
                    content,
                    "text/plain",
                    len(speakers),
                    None,
                    Provenance(
                        self.descriptor.provider_id,
                        self.descriptor.family,
                        self.descriptor.route,
                        source_id,
                        self._clock(),
                        account,
                        "calendar→drive",
                    ),
                    display_name=title,
                    recorded_at=recorded_at,
                    item_id=file_id,
                    organizer=organizer.address if organizer else None,
                    speakers=speakers,
                )
            )
        return result

    @staticmethod
    def _content(response: TransportResponse) -> str:
        if len(response.content) > _MAX_EXPORT_BYTES:
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Google Meet transcript exceeds the fixed offline processing bound",
            )
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Google Meet transcript content is not UTF-8",
            ) from exc

    def _json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
        try:
            return request_json(self._transport, "GET", url, headers=self._headers(), params=params)
        except ConnectorFailure as failure:
            if failure.reason is ReasonCode.INVALID_CREDENTIALS:
                raise ConnectorFailure(
                    ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                    "Google Meet authenticated account could not be verified",
                ) from failure
            raise

    def _drive_export(self, file_id: str) -> TransportResponse:
        try:
            return request_bytes(
                self._transport,
                "GET",
                f"{self._drive_base_url}/files/{quote(file_id, safe='')}/export",
                headers={**self._headers(), "Accept": "text/plain"},
                params={"mimeType": "text/plain"},
            )
        except ConnectorFailure as failure:
            if failure.reason is ReasonCode.INVALID_CREDENTIALS:
                raise ConnectorFailure(
                    ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                    "Google Meet authenticated account could not access the transcript",
                ) from failure
            raise

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Authorization": self._auth.authorization_header()}


def _event_in_window(event: dict[str, Any], query: TranscriptQuery) -> bool:
    assert query.window is not None
    start = _google_event_datetime(event.get("start"), "meet.event.start")
    return query.window.start <= start < query.window.end


def _speakers(content: str) -> tuple[str, ...]:
    speakers: dict[str, str] = {}
    for match in _TURN_LINE.finditer(content):
        name = match.group(1).strip()
        if not name.casefold().startswith(("asistentes", "transcripción")):
            speakers.setdefault(name.casefold(), name)
    return tuple(speakers.values())
