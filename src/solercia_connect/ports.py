from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from solercia_connect.domain import (
    CalendarEvent,
    CalendarQuery,
    Capability,
    ConnectorRequest,
    DomainRecord,
    ExternalIssue,
    HealthReport,
    IssueQuery,
    MailMessage,
    MailQuery,
    MeetingTranscript,
    ProviderFamily,
    RouteKind,
    TicketCase,
    TicketQuery,
    TranscriptQuery,
)


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider_id: str
    family: ProviderFamily
    route: RouteKind
    capabilities: frozenset[Capability]


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes


class HTTPTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> TransportResponse: ...


class ProviderAdapter(Protocol):
    descriptor: ProviderDescriptor

    def health(self) -> HealthReport: ...

    def execute(self, request: ConnectorRequest) -> tuple[DomainRecord, ...]: ...


@runtime_checkable
class BrowserProviderAdapter(ProviderAdapter, Protocol):
    """What a family's supervised-browser adapter offers beyond any provider adapter.

    ``health`` reports, as ``account_id``, the account the dedicated profile's session proves
    headlessly. ``interactive_login`` lets the user sign in visibly in that profile, then proves
    the same identity again headlessly before reporting it, so a saved browser connection always
    rests on a session that survives without the user.
    """

    def interactive_login(self) -> HealthReport: ...


class MailProviderPort(Protocol):
    def list_sent_mail(self, query: MailQuery) -> tuple[MailMessage, ...]: ...


class InboxMailProviderPort(Protocol):
    def list_inbox_mail(self, query: MailQuery) -> tuple[MailMessage, ...]: ...


class CalendarProviderPort(Protocol):
    def list_events(self, query: CalendarQuery) -> tuple[CalendarEvent, ...]: ...


class MeetingTranscriptProviderPort(Protocol):
    def list_transcripts(self, query: TranscriptQuery) -> tuple[MeetingTranscript, ...]: ...


class ExternalIssueProviderPort(Protocol):
    def search_issues(self, query: IssueQuery) -> tuple[ExternalIssue, ...]: ...


class TicketProviderPort(Protocol):
    def search_tickets(self, query: TicketQuery) -> tuple[TicketCase, ...]: ...
