from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from email.utils import getaddresses
from typing import Any, cast
from urllib.parse import quote

from solercia_connect.auth import AuthorizationProvider
from solercia_connect.domain import (
    CalendarEvent,
    CalendarQuery,
    Capability,
    ConnectorRequest,
    EmailAddress,
    HealthReport,
    HealthState,
    MailMessage,
    MailQuery,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import HTTPTransport, ProviderDescriptor
from solercia_connect.providers._shared import (
    as_string,
    ensure_official_https_base_url,
    lower_domain,
    parse_datetime,
    require_list,
    require_mapping,
    require_string,
    unique_canonical_account_identity,
)
from solercia_connect.transport import request_json

_GMAIL_BASE_URL = "https://gmail.googleapis.com/gmail/v1"
_GOOGLE_CALENDAR_BASE_URL = "https://www.googleapis.com/calendar/v3"
# Structured 403 reasons Google documents as a domain administrator block (decision D0.1). The
# Gmail and Drive error guides both describe `domainPolicy` as the domain policy not allowing the
# app; no other documented reason means an organization block.
_GOOGLE_POLICY_REASONS = frozenset({"domainPolicy"})
_MAX_ERROR_BODY_BYTES = 64 * 1024


def google_policy_failure(status_code: int, content: bytes) -> ConnectorFailure | None:
    """Classify a 403 whose structured reasons are all a documented domain policy block.

    Only ``error.errors[].reason`` is read. Anything else, including an unparseable body, keeps
    the generic status mapping, and the body never reaches the message.
    """

    if status_code != 403:
        return None
    reasons = _google_error_reasons(content)
    if not reasons or not reasons <= _GOOGLE_POLICY_REASONS:
        return None
    return ConnectorFailure(
        ReasonCode.ADMIN_CONSENT_REQUIRED,
        "The Google Workspace domain policy does not allow this app to read the data",
    )


def _google_error_reasons(content: bytes) -> frozenset[str]:
    if len(content) > _MAX_ERROR_BODY_BYTES:
        return frozenset()
    try:
        payload = json.loads(content.decode("utf-8"))
    except (ValueError, RecursionError):
        return frozenset()
    error = payload.get("error") if isinstance(payload, dict) else None
    errors = error.get("errors") if isinstance(error, dict) else None
    if not isinstance(errors, list):
        return frozenset()
    reasons = [item.get("reason") if isinstance(item, dict) else None for item in errors]
    if not all(isinstance(reason, str) for reason in reasons):
        return frozenset()
    return frozenset(cast(list[str], reasons))


class GoogleGmailAdapter:
    descriptor = ProviderDescriptor(
        "google.gmail",
        ProviderFamily.GOOGLE,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
    )

    def __init__(
        self,
        transport: HTTPTransport,
        auth: AuthorizationProvider,
        *,
        expected_account: str | None = None,
        base_url: str = _GMAIL_BASE_URL,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._transport = transport
        self._auth = auth
        self._expected_account = expected_account.lower() if expected_account else None
        self._base_url = ensure_official_https_base_url(
            base_url,
            "Gmail base URL",
            _GMAIL_BASE_URL,
        )
        self._clock = clock

    def health(self) -> HealthReport:
        account = self._identity()
        return HealthReport(
            self.descriptor.provider_id,
            HealthState.READY,
            ReasonCode.SUCCESS,
            tuple(self.descriptor.capabilities),
            account,
            "Gmail profile and read capability are reachable",
        )

    def execute(self, request: ConnectorRequest) -> tuple[MailMessage, ...]:
        if request.capability is Capability.MAIL_SENT_LIST and isinstance(
            request.payload, MailQuery
        ):
            return self.list_sent_mail(request.payload)
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "Gmail request does not match the mail capability contract",
        )

    def list_sent_mail(self, query: MailQuery) -> tuple[MailMessage, ...]:
        account = self._identity()
        if query.account_id and query.account_id.lower() != account.lower():
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Requested mailbox does not match the authenticated Google account",
            )
        after_epoch = int(query.window.start.timestamp()) - 1
        before_epoch = int(query.window.end.timestamp())
        params: dict[str, Any] = {
            "q": f"from:me after:{after_epoch} before:{before_epoch}",
            "labelIds": "SENT",
            "maxResults": min(query.limit, 100),
        }
        ids = self._list_message_ids(params, query.limit)
        messages: list[MailMessage] = []
        for message_id in ids:
            payload = request_json(
                self._transport,
                "GET",
                f"{self._base_url}/users/me/messages/{quote(message_id, safe='')}",
                headers=self._headers(),
                params={"format": "full"},
                failure_refiner=google_policy_failure,
            )
            message = self._normalize_message(require_mapping(payload, "gmail.message"), account)
            if query.window.start <= message.sent_at < query.window.end:
                messages.append(message)
        messages.sort(key=lambda item: item.sent_at, reverse=True)
        return tuple(messages[: query.limit])

    def _identity(self) -> str:
        payload = request_json(
            self._transport,
            "GET",
            f"{self._base_url}/users/me/profile",
            headers=self._headers(),
            failure_refiner=google_policy_failure,
        )
        data = require_mapping(payload, "gmail.profile")
        account = require_string(data.get("emailAddress"), "gmail.profile.emailAddress")
        if self._expected_account and account.lower() != self._expected_account:
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Authenticated Google account does not match configured account",
            )
        return account

    def _list_message_ids(self, params: dict[str, Any], limit: int) -> list[str]:
        result: list[str] = []
        page_token: str | None = None
        pages = 0
        while len(result) < limit:
            pages += 1
            if pages > 20:
                raise ConnectorFailure(
                    ReasonCode.MALFORMED_PROVIDER_DATA,
                    "Gmail pagination exceeded the fixed page bound",
                )
            page_params = dict(params)
            if page_token:
                page_params["pageToken"] = page_token
            payload = request_json(
                self._transport,
                "GET",
                f"{self._base_url}/users/me/messages",
                headers=self._headers(),
                params=page_params,
                failure_refiner=google_policy_failure,
            )
            data = require_mapping(payload, "gmail.messages")
            raw_messages = data.get("messages") or []
            for raw in require_list(raw_messages, "gmail.messages.items"):
                item = require_mapping(raw, "gmail.message.reference")
                result.append(require_string(item.get("id"), "gmail.message.id"))
                if len(result) >= limit:
                    break
            raw_token = data.get("nextPageToken")
            page_token = raw_token if isinstance(raw_token, str) and raw_token else None
            if not page_token:
                break
        return result

    def _normalize_message(self, item: dict[str, Any], account: str) -> MailMessage:
        source_id = require_string(item.get("id"), "gmail.message.id")
        payload = require_mapping(item.get("payload"), "gmail.message.payload")
        headers = _gmail_headers(payload.get("headers"))
        internal_date = require_string(item.get("internalDate"), "gmail.message.internalDate")
        try:
            sent_at = datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC)
        except ValueError as exc:
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Gmail internalDate is malformed",
            ) from exc
        body, body_format = _gmail_body(payload)
        sender_values = _addresses_from_header(headers.get("from"))
        return MailMessage(
            source_id,
            headers.get("subject", ""),
            sent_at,
            sender_values[0] if sender_values else None,
            _addresses_from_header(headers.get("to")),
            _addresses_from_header(headers.get("cc")),
            body,
            body_format,
            as_string(item.get("threadId")),
            _gmail_has_attachments(payload),
            Provenance(
                self.descriptor.provider_id,
                self.descriptor.family,
                self.descriptor.route,
                source_id,
                self._clock(),
                account,
                "/users/me/messages/{id}",
            ),
            folder="sent",
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": self._auth.authorization_header(),
        }


class GoogleCalendarAdapter:
    descriptor = ProviderDescriptor(
        "google.calendar",
        ProviderFamily.GOOGLE,
        RouteKind.OFFICIAL,
        frozenset({Capability.CALENDAR_EVENTS_LIST}),
    )

    def __init__(
        self,
        transport: HTTPTransport,
        auth: AuthorizationProvider,
        *,
        expected_account: str | None = None,
        health_calendar_id: str = "primary",
        base_url: str = _GOOGLE_CALENDAR_BASE_URL,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._transport = transport
        self._auth = auth
        self._expected_account = _configured_google_account(expected_account)
        self._health_calendar_id = health_calendar_id
        self._base_url = ensure_official_https_base_url(
            base_url,
            "Google Calendar base URL",
            _GOOGLE_CALENDAR_BASE_URL,
        )
        self._clock = clock

    def health(self) -> HealthReport:
        account = self._verified_identity(None)
        if self._health_calendar_id.casefold() != "primary":
            payload = request_json(
                self._transport,
                "GET",
                f"{self._base_url}/users/me/calendarList/"
                f"{quote(self._health_calendar_id, safe='')}",
                headers=self._headers(),
                failure_refiner=google_policy_failure,
            )
            require_mapping(payload, "calendar.health")
        return HealthReport(
            self.descriptor.provider_id,
            HealthState.READY,
            ReasonCode.SUCCESS,
            tuple(self.descriptor.capabilities),
            account,
            "Google Calendar read capability is reachable",
        )

    def _verified_identity(self, requested_account: str | None) -> str:
        payload = request_json(
            self._transport,
            "GET",
            f"{self._base_url}/users/me/calendarList/primary",
            headers=self._headers(),
            failure_refiner=google_policy_failure,
        )
        data = require_mapping(payload, "calendar.identity")
        observed_account = unique_canonical_account_identity(data.get("id"), exact=True)
        if observed_account is None:
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Google Calendar did not expose one unambiguous authenticated account identity",
            )
        requested_boundary = _requested_google_account(requested_account)
        if any(
            boundary is not None and observed_account != boundary
            for boundary in (self._expected_account, requested_boundary)
        ):
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Authenticated Google account does not match the configured or requested account",
            )
        return observed_account

    def execute(self, request: ConnectorRequest) -> tuple[CalendarEvent, ...]:
        if request.capability is Capability.CALENDAR_EVENTS_LIST and isinstance(
            request.payload, CalendarQuery
        ):
            return self.list_events(request.payload)
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "Google Calendar request does not match the calendar capability contract",
        )

    def list_events(self, query: CalendarQuery) -> tuple[CalendarEvent, ...]:
        account_id = self._verified_identity(query.account_id)
        params: dict[str, Any] = {
            "timeMin": query.window.start.isoformat(),
            "timeMax": query.window.end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": min(query.limit, 2500),
        }
        raw_items = self._paged_events(query.calendar_id, params, query.limit)
        items = [self._normalize_event(item, account_id) for item in raw_items]
        excluded_domains = {domain.lower() for domain in query.excluded_organizer_domains}
        return tuple(
            item
            for item in items
            if not item.is_cancelled
            and (not query.accepted_only or item.response_status in {"accepted", "organizer"})
            and lower_domain(item.organizer.address if item.organizer else None)
            not in excluded_domains
        )

    def _paged_events(
        self, calendar_id: str, params: dict[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page_token: str | None = None
        pages = 0
        while len(result) < limit:
            pages += 1
            if pages > 20:
                raise ConnectorFailure(
                    ReasonCode.MALFORMED_PROVIDER_DATA,
                    "Google Calendar pagination exceeded the fixed page bound",
                )
            page_params = dict(params)
            if page_token:
                page_params["pageToken"] = page_token
            payload = request_json(
                self._transport,
                "GET",
                f"{self._base_url}/calendars/{quote(calendar_id, safe='')}/events",
                headers=self._headers(),
                params=page_params,
                failure_refiner=google_policy_failure,
            )
            data = require_mapping(payload, "calendar.events")
            for raw in require_list(data.get("items") or [], "calendar.events.items"):
                result.append(require_mapping(raw, "calendar.event"))
                if len(result) >= limit:
                    break
            raw_token = data.get("nextPageToken")
            page_token = raw_token if isinstance(raw_token, str) and raw_token else None
            if not page_token:
                break
        return result

    def _normalize_event(self, item: dict[str, Any], account_id: str) -> CalendarEvent:
        source_id = require_string(item.get("id"), "calendar.event.id")
        organizer_raw = item.get("organizer") or {}
        organizer = _google_address(organizer_raw)
        attendees_raw = require_list(item.get("attendees") or [], "calendar.event.attendees")
        status: str | None = (
            "organizer" if (isinstance(organizer_raw, dict) and organizer_raw.get("self")) else None
        )
        attendees: list[EmailAddress] = []
        for attendee_raw in attendees_raw:
            attendee_map = require_mapping(attendee_raw, "calendar.event.attendee")
            address = _google_address(attendee_map)
            if address:
                attendees.append(address)
            if attendee_map.get("self"):
                status = as_string(attendee_map.get("responseStatus"))
        return CalendarEvent(
            source_id,
            str(item.get("summary") or ""),
            _google_event_datetime(item.get("start"), "calendar.event.start"),
            _google_event_datetime(item.get("end"), "calendar.event.end"),
            organizer,
            tuple(attendees),
            status,
            bool(item.get("hangoutLink") or item.get("conferenceData")),
            item.get("status") == "cancelled",
            Provenance(
                self.descriptor.provider_id,
                self.descriptor.family,
                self.descriptor.route,
                source_id,
                self._clock(),
                account_id,
                "/calendars/{calendarId}/events",
            ),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": self._auth.authorization_header(),
        }


def _configured_google_account(value: str | None) -> str | None:
    if value is None:
        return None
    identity = unique_canonical_account_identity(value, exact=True)
    if identity is None:
        raise ValueError("Expected Google account must be one unambiguous email identity")
    return identity


def _requested_google_account(value: str | None) -> str | None:
    if value is None:
        return None
    identity = unique_canonical_account_identity(value, exact=True)
    if identity is None:
        raise ConnectorFailure(
            ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
            "Requested Google account is not one unambiguous account identity",
        )
    return identity


def _gmail_headers(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in require_list(value or [], "gmail.message.headers"):
        item = require_mapping(raw, "gmail.message.header")
        name = as_string(item.get("name"))
        header_value = as_string(item.get("value"))
        if name and header_value:
            result[name.lower()] = header_value
    return result


def _addresses_from_header(value: str | None) -> tuple[EmailAddress, ...]:
    if not value:
        return ()
    return tuple(
        EmailAddress(address, name or None) for name, address in getaddresses([value]) if address
    )


def _gmail_body(payload: dict[str, Any]) -> tuple[str, str]:
    plain: list[str] = []
    html_parts: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        mime = str(part.get("mimeType") or "").lower()
        body = require_mapping(part.get("body") or {}, "gmail.message.part.body")
        data = body.get("data")
        if isinstance(data, str) and data:
            decoded = _decode_base64url(data)
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        for raw_child in require_list(part.get("parts") or [], "gmail.message.parts"):
            visit(require_mapping(raw_child, "gmail.message.part"))

    visit(payload)
    if plain:
        return "\n".join(plain), "text"
    if html_parts:
        return "\n".join(html_parts), "html"
    return "", "unknown"


def _gmail_has_attachments(payload: dict[str, Any]) -> bool:
    body = require_mapping(payload.get("body") or {}, "gmail.message.body")
    if body.get("attachmentId") or payload.get("filename"):
        return True
    for raw_child in require_list(payload.get("parts") or [], "gmail.message.parts"):
        if _gmail_has_attachments(require_mapping(raw_child, "gmail.message.part")):
            return True
    return False


def _decode_base64url(value: str) -> str:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(f"{value}{padding}").decode("utf-8", errors="replace")
    except (ValueError, TypeError) as exc:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Gmail message body is not valid base64url data",
        ) from exc


def _google_address(value: Any) -> EmailAddress | None:
    if not isinstance(value, dict):
        return None
    address = as_string(value.get("email"))
    if not address:
        return None
    return EmailAddress(address, as_string(value.get("displayName")))


def _google_event_datetime(value: Any, label: str) -> datetime:
    data = require_mapping(value, label)
    if data.get("dateTime"):
        return parse_datetime(data.get("dateTime"), f"{label}.dateTime")
    raw_date = require_string(data.get("date"), f"{label}.date")
    try:
        return datetime.combine(date.fromisoformat(raw_date), time.min, tzinfo=UTC)
    except ValueError as exc:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            f"Provider date is malformed: {label}.date",
        ) from exc
