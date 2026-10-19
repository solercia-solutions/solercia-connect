from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any


class Capability(StrEnum):
    MAIL_SENT_LIST = "mail.sent.list"
    MAIL_INBOX_LIST = "mail.inbox.list"
    CALENDAR_EVENTS_LIST = "calendar.events.list"
    MEETING_TRANSCRIPTS_LIST = "meeting.transcripts.list"
    MEETING_TRANSCRIPT_GET = "meeting.transcript.get"
    FILES_SEARCH = "files.search"
    FILES_GET = "files.get"
    EXTERNAL_ISSUES_SEARCH = "external.issues.search"
    TICKETS_SEARCH = "tickets.search"


class ProviderFamily(StrEnum):
    MICROSOFT = "microsoft"
    GOOGLE = "google"
    JIRA = "jira"
    GLPI = "glpi"


class RouteKind(StrEnum):
    OFFICIAL = "official"
    BROWSER = "browser"


class ReasonCode(StrEnum):
    SUCCESS = "success"
    INVALID_INPUT = "invalid_input"
    CONFIGURATION_INVALID = "configuration_invalid"
    CAPABILITY_NOT_IMPLEMENTED = "capability_not_implemented"
    OFFICIAL_CAPABILITY_UNAVAILABLE = "official_capability_unavailable"
    INVALID_CREDENTIALS = "invalid_credentials"
    AUTHORIZATION_DENIED = "authorization_denied"
    AUTHORIZATION_BOUNDARY_VIOLATION = "authorization_boundary_violation"
    ADMIN_CONSENT_REQUIRED = "admin_consent_required"
    MALFORMED_PROVIDER_DATA = "malformed_provider_data"
    RATE_LIMITED = "rate_limited"
    NETWORK_TIMEOUT = "network_timeout"
    NETWORK_UNAVAILABLE = "network_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    REQUEST_REJECTED = "request_rejected"
    UNKNOWN_ERROR = "unknown_error"
    BROWSER_FALLBACK_DISABLED = "browser_fallback_disabled"
    BROWSER_FALLBACK_DENIED = "browser_fallback_denied"
    BROWSER_LOGIN_REQUIRED = "browser_login_required"
    BROWSER_MFA_REQUIRED = "browser_mfa_required"
    BROWSER_ACCOUNT_MISMATCH = "browser_account_mismatch"
    BROWSER_IDENTITY_UNAVAILABLE = "browser_identity_unavailable"
    BROWSER_SELECTOR_MISMATCH = "browser_selector_mismatch"
    BROWSER_TIMEOUT = "browser_timeout"
    BROWSER_CAPABILITY_NOT_IMPLEMENTED = "browser_capability_not_implemented"
    BROWSER_EXTRACTION_FAILED = "browser_extraction_failed"
    OAUTH_CALLBACK_INVALID = "oauth_callback_invalid"
    OAUTH_STATE_MISMATCH = "oauth_state_mismatch"
    OAUTH_CALLBACK_TIMEOUT = "oauth_callback_timeout"
    PERSONAL_ACCOUNT_UNSUPPORTED = "personal_account_unsupported"
    TENANT_POLICY_BLOCKED = "tenant_policy_blocked"
    API_AUTHENTICATION_REQUIRED = "api_authentication_required"


class StepOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    RETRY = "retry"
    SKIPPED = "skipped"


class ReceiptStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class HealthState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DateWindow:
    """A timezone-aware, end-exclusive time range."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("DateWindow values must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("DateWindow start must be before end")


@dataclass(frozen=True, slots=True)
class MailQuery:
    window: DateWindow
    account_id: str | None = None
    limit: int = 50
    include_attachments: bool = False
    max_attachment_bytes: int = 10_485_760

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 1000:
            raise ValueError("MailQuery limit must be between 1 and 1000")
        if self.max_attachment_bytes <= 0:
            raise ValueError("MailQuery max_attachment_bytes must be positive")


@dataclass(frozen=True, slots=True)
class CalendarQuery:
    window: DateWindow
    calendar_id: str = "primary"
    account_id: str | None = None
    accepted_only: bool = True
    excluded_organizer_domains: tuple[str, ...] = ()
    limit: int = 250

    def __post_init__(self) -> None:
        if not self.calendar_id:
            raise ValueError("CalendarQuery calendar_id cannot be empty")
        if not 1 <= self.limit <= 1000:
            raise ValueError("CalendarQuery limit must be between 1 and 1000")


@dataclass(frozen=True, slots=True)
class TranscriptQuery:
    """Select meeting transcripts by meeting, by recording, or by period.

    The two routes address the same transcript by different names. The official route knows an
    online meeting id; the supervised browser route reads the recording that carries the
    transcript, which is a drive item. A caller that supplies neither selector asks for every
    recording in the window, which is how a period is reported without knowing ids in advance.
    """

    meeting_id: str | None = None
    drive_id: str | None = None
    item_id: str | None = None
    window: DateWindow | None = None
    account_id: str | None = None
    include_shared: bool = True
    limit: int = 50

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 500:
            raise ValueError("TranscriptQuery limit must be between 1 and 500")
        for name in ("meeting_id", "drive_id", "item_id"):
            value: str | None = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"TranscriptQuery {name} cannot be blank")
        if (self.drive_id is None) != (self.item_id is None):
            raise ValueError("TranscriptQuery drive_id and item_id must be given together")


@dataclass(frozen=True, slots=True)
class IssueQuery:
    window: DateWindow
    assignee: str | None = None
    jql: str | None = None
    limit: int = 100

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 100:
            raise ValueError("IssueQuery limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class TicketQuery:
    window: DateWindow
    limit: int = 200

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 1000:
            raise ValueError("TicketQuery limit must be between 1 and 1000")


@dataclass(frozen=True, slots=True)
class FileQuery:
    """Locate files in a personal drive or a SharePoint site."""

    text: str | None = None
    folder_path: str | None = None
    item_id: str | None = None
    site: str | None = None
    window: DateWindow | None = None
    account_id: str | None = None
    include_content: bool = False
    max_content_bytes: int = 10_485_760
    limit: int = 50

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 500:
            raise ValueError("FileQuery limit must be between 1 and 500")
        if self.max_content_bytes <= 0:
            raise ValueError("FileQuery max_content_bytes must be positive")
        if self.item_id is not None and not self.item_id.strip():
            raise ValueError("FileQuery item_id cannot be blank")
        if self.folder_path is not None and not self.folder_path.strip():
            raise ValueError("FileQuery folder_path cannot be blank")
        if self.site is not None and not self.site.strip():
            raise ValueError("FileQuery site cannot be blank")


type QueryPayload = (
    MailQuery | CalendarQuery | TranscriptQuery | IssueQuery | TicketQuery | FileQuery
)


@dataclass(frozen=True, slots=True)
class EmailAddress:
    address: str
    name: str | None = None

    def __post_init__(self) -> None:
        if not self.address.strip():
            raise ValueError("EmailAddress cannot be empty")
        object.__setattr__(self, "address", self.address.strip().lower())


@dataclass(frozen=True, slots=True)
class Provenance:
    provider_id: str
    family: ProviderFamily
    route: RouteKind
    source_id: str
    fetched_at: datetime
    account_id: str | None = None
    endpoint: str | None = None


@dataclass(frozen=True, slots=True)
class MailAttachment:
    """One file carried by a message, with content present only when it was requested."""

    id: str
    name: str
    content_type: str | None
    size_bytes: int | None
    is_inline: bool
    content_base64: str | None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("MailAttachment id cannot be empty")
        if not self.name.strip():
            raise ValueError("MailAttachment name cannot be empty")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("MailAttachment size_bytes cannot be negative")


@dataclass(frozen=True, slots=True)
class MailMessage:
    id: str
    subject: str
    sent_at: datetime
    sender: EmailAddress | None
    to: tuple[EmailAddress, ...]
    cc: tuple[EmailAddress, ...]
    body: str
    body_format: str
    conversation_id: str | None
    has_attachments: bool
    provenance: Provenance
    attachments: tuple[MailAttachment, ...] = ()
    internet_message_id: str | None = None
    folder: str | None = None
    received_at: datetime | None = None
    # Outlook category names in provider order; empty when the message carries none.
    categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    id: str
    title: str
    start: datetime
    end: datetime
    organizer: EmailAddress | None
    attendees: tuple[EmailAddress, ...]
    response_status: str | None
    is_online: bool
    is_cancelled: bool
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class MeetingTranscript:
    """One meeting transcript, however it was reached.

    Both routes fill the leading fields. The trailing fields describe the recording a transcript
    was read from, which only the drive-backed route observes; the official route leaves them
    unset rather than inventing them.
    """

    id: str
    meeting_id: str | None
    content: str
    content_format: str
    speaker_count: int | None
    last_cue_seconds: float | None
    provenance: Provenance
    display_name: str | None = None
    recorded_at: datetime | None = None
    drive_id: str | None = None
    item_id: str | None = None
    organizer: str | None = None
    speakers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("MeetingTranscript id cannot be empty")
        if self.speaker_count is not None and self.speaker_count < 0:
            raise ValueError("MeetingTranscript speaker_count cannot be negative")


@dataclass(frozen=True, slots=True)
class ExternalIssue:
    id: str
    key: str
    title: str
    issue_type: str | None
    status: str | None
    status_category: str | None
    project: str | None
    assignee: str | None
    reporter: str | None
    priority: str | None
    created_at: datetime | None
    updated_at: datetime | None
    resolved_at: datetime | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class TicketCase:
    id: str
    title: str
    status: str | None
    category: str | None
    opened_at: datetime | None
    closed_at: datetime | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class DriveFile:
    """One file or folder observed in a personal drive or SharePoint document library."""

    id: str
    name: str
    path: str | None
    size_bytes: int | None
    modified_at: datetime | None
    created_at: datetime | None
    mime_type: str | None
    web_url: str | None
    drive_id: str | None
    is_folder: bool
    content_base64: str | None
    provenance: Provenance

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("DriveFile id cannot be empty")
        if not self.name.strip():
            raise ValueError("DriveFile name cannot be empty")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("DriveFile size_bytes cannot be negative")


type DomainRecord = (
    MailMessage | CalendarEvent | MeetingTranscript | ExternalIssue | TicketCase | DriveFile
)


@dataclass(frozen=True, slots=True)
class ConnectorRequest:
    request_id: str
    provider_id: str
    capability: Capability
    payload: QueryPayload

    @classmethod
    def create(
        cls,
        provider_id: str,
        capability: Capability,
        payload: QueryPayload,
        request_id: str | None = None,
    ) -> ConnectorRequest:
        if request_id is None:
            source = json.dumps(
                {
                    "provider_id": provider_id,
                    "capability": capability.value,
                    "payload": _stable_value(payload),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            request_id = f"req-{hashlib.sha256(source.encode()).hexdigest()[:16]}"
        return cls(request_id, provider_id, capability, payload)


@dataclass(frozen=True, slots=True)
class TraceStep:
    index: int
    route: RouteKind
    provider_id: str
    attempt: int
    outcome: StepOutcome
    reason: ReasonCode
    detail: str


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    receipt_id: str
    request_id: str
    provider_id: str
    capability: Capability
    status: ReceiptStatus
    selected_route: RouteKind | None
    final_reason: ReasonCode
    completed_at: datetime
    steps: tuple[TraceStep, ...]


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    ok: bool
    records: tuple[DomainRecord, ...]
    receipt: ExecutionReceipt


@dataclass(frozen=True, slots=True)
class HealthReport:
    provider_id: str
    state: HealthState
    reason: ReasonCode
    capabilities: tuple[Capability, ...]
    account_id: str | None = None
    detail: str = ""


_CAPABILITY_RECORD_TYPES: dict[Capability, type[object]] = {
    Capability.MAIL_SENT_LIST: MailMessage,
    Capability.MAIL_INBOX_LIST: MailMessage,
    Capability.CALENDAR_EVENTS_LIST: CalendarEvent,
    Capability.MEETING_TRANSCRIPTS_LIST: MeetingTranscript,
    Capability.MEETING_TRANSCRIPT_GET: MeetingTranscript,
    Capability.FILES_SEARCH: DriveFile,
    Capability.FILES_GET: DriveFile,
    Capability.EXTERNAL_ISSUES_SEARCH: ExternalIssue,
    Capability.TICKETS_SEARCH: TicketCase,
}


def record_matches_capability(record: DomainRecord, capability: Capability) -> bool:
    """Check a record against the type its capability promises.

    An unmapped capability used to raise a bare KeyError that surfaced to the caller as an
    unknown error, hiding a one-line omission behind the least useful message available.
    """
    expected = _CAPABILITY_RECORD_TYPES.get(capability)
    if expected is None:
        raise ValueError(f"Capability {capability.value} declares no record type")
    return isinstance(record, expected)


def _stable_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return {field.name: _stable_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _stable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_stable_value(item) for item in value]
    return value


def receipt_fingerprint(
    request: ConnectorRequest,
    status: ReceiptStatus,
    selected_route: RouteKind | None,
    final_reason: ReasonCode,
    steps: tuple[TraceStep, ...],
) -> str:
    payload = {
        "request_id": request.request_id,
        "provider_id": request.provider_id,
        "capability": request.capability.value,
        "status": status.value,
        "selected_route": selected_route.value if selected_route else None,
        "final_reason": final_reason.value,
        "steps": [asdict(step) for step in steps],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"rcpt-{hashlib.sha256(encoded.encode()).hexdigest()[:20]}"


def utc_now() -> datetime:
    return datetime.now(UTC)
