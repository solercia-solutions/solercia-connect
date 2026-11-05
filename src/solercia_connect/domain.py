from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit


class Capability(StrEnum):
    MAIL_SENT_LIST = "mail.sent.list"
    CALENDAR_EVENTS_LIST = "calendar.events.list"
    MEETING_TRANSCRIPTS_LIST = "meeting.transcripts.list"
    MEETING_TRANSCRIPT_GET = "meeting.transcript.get"
    FILES_SEARCH = "files.search"
    FILES_GET = "files.get"
    EXTERNAL_ISSUES_SEARCH = "external.issues.search"
    TICKETS_SEARCH = "tickets.search"
    SEARCH_ANALYTICS_QUERY = "search.analytics.query"
    SEARCH_SITEMAPS_LIST = "search.sitemaps.list"
    SEARCH_URLS_INSPECT = "search.urls.inspect"


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
        if not 1 <= self.limit <= 100:
            raise ValueError("MailQuery limit must be between 1 and 100")
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


SEARCH_ANALYTICS_DIMENSIONS = frozenset(
    {"country", "date", "device", "hour", "page", "query", "searchAppearance"}
)
SEARCH_ANALYTICS_TYPES = frozenset({"discover", "googleNews", "image", "news", "video", "web"})
SEARCH_ANALYTICS_DATA_STATES = frozenset({"all", "final"})
SEARCH_ANALYTICS_MAX_ROWS = 500_000

_DOMAIN_PROPERTY = re.compile(r"sc-domain:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+\Z")


def validate_search_property(value: str) -> str:
    """Accept a property exactly as Search Console names it.

    A Domain property is ``sc-domain:example.com``. A URL-prefix property is an absolute URL with
    a trailing slash, and without the slash it names a property that does not exist.
    """
    if _DOMAIN_PROPERTY.fullmatch(value):
        return value
    parsed = urlsplit(value)
    if (
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.path.endswith("/")
        and not parsed.query
        and not parsed.fragment
    ):
        return value
    raise ValueError(
        "site_url must be sc-domain:example.com or a URL-prefix property ending in a slash"
    )


def url_belongs_to_search_property(url: str, site_url: str) -> bool:
    """Tell whether a page falls under a property, which URL Inspection requires."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if site_url.startswith("sc-domain:"):
        domain = site_url.removeprefix("sc-domain:")
        host = parsed.hostname.casefold()
        return host == domain or host.endswith(f".{domain}")
    return url.startswith(site_url)


@dataclass(frozen=True, slots=True)
class SearchAnalyticsQuery:
    """Read Search Console performance for exact calendar dates.

    Search Console dates are calendar days in America/Los_Angeles, inclusive at both ends. They
    are not instants, so this contract keeps the provider's own meaning instead of an
    end-exclusive window that would shift each day by a time zone.
    """

    site_url: str
    start_date: date
    end_date: date
    dimensions: tuple[str, ...] = ("date",)
    search_type: str = "web"
    data_state: str = "final"
    row_limit: int = 25_000
    account_id: str | None = None

    def __post_init__(self) -> None:
        validate_search_property(self.site_url)
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        unknown = set(self.dimensions) - SEARCH_ANALYTICS_DIMENSIONS
        if unknown:
            raise ValueError(f"Unsupported dimensions: {', '.join(sorted(unknown))}")
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError("dimensions cannot repeat")
        if self.search_type not in SEARCH_ANALYTICS_TYPES:
            raise ValueError("search_type is not a Search Console type")
        if self.data_state not in SEARCH_ANALYTICS_DATA_STATES:
            raise ValueError("data_state must be final or all")
        if not 1 <= self.row_limit <= SEARCH_ANALYTICS_MAX_ROWS:
            raise ValueError(f"row_limit must be between 1 and {SEARCH_ANALYTICS_MAX_ROWS}")


@dataclass(frozen=True, slots=True)
class SearchSitemapsQuery:
    site_url: str
    account_id: str | None = None

    def __post_init__(self) -> None:
        validate_search_property(self.site_url)


@dataclass(frozen=True, slots=True)
class SearchUrlInspectionQuery:
    site_url: str
    inspection_url: str
    language_code: str = "es-CO"
    account_id: str | None = None

    def __post_init__(self) -> None:
        validate_search_property(self.site_url)
        if not url_belongs_to_search_property(self.inspection_url, self.site_url):
            raise ValueError("inspection_url must be a full URL under site_url")
        if not self.language_code.strip():
            raise ValueError("language_code cannot be blank")


type QueryPayload = (
    MailQuery
    | CalendarQuery
    | TranscriptQuery
    | IssueQuery
    | TicketQuery
    | FileQuery
    | SearchAnalyticsQuery
    | SearchSitemapsQuery
    | SearchUrlInspectionQuery
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


@dataclass(frozen=True, slots=True)
class SearchAnalyticsRow:
    """One grouped row; ``keys`` follows the order of the report's ``dimensions``."""

    keys: tuple[str, ...]
    clicks: float
    impressions: float
    ctr: float
    position: float


@dataclass(frozen=True, slots=True)
class SearchAnalyticsReport:
    """Everything one query answered, including an honest statement of what it covers.

    A query always yields one report, so "no rows" is an observed answer and not an absent one.
    ``first_day_with_data`` and ``last_day_with_data`` come only from returned rows and stay unset
    when the query did not group by date. Search Console omits days without data, so neither value
    says data began or ended beyond the requested range. ``truncated`` means the row limit was
    reached and more rows may exist.
    """

    id: str
    site_url: str
    start_date: date
    end_date: date
    dimensions: tuple[str, ...]
    search_type: str
    data_state: str
    rows: tuple[SearchAnalyticsRow, ...]
    first_day_with_data: date | None
    last_day_with_data: date | None
    first_incomplete_date: date | None
    aggregation_type: str | None
    truncated: bool
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class SearchSitemapContent:
    type: str
    submitted: int | None


@dataclass(frozen=True, slots=True)
class SearchSitemap:
    id: str
    site_url: str
    path: str
    is_pending: bool
    is_index: bool
    type: str | None
    last_submitted: datetime | None
    last_downloaded: datetime | None
    errors: int
    warnings: int
    contents: tuple[SearchSitemapContent, ...]
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class SearchUrlInspection:
    """Google's indexed view of one URL; fields it did not report stay unset."""

    id: str
    site_url: str
    inspection_url: str
    verdict: str | None
    coverage_state: str | None
    robots_txt_state: str | None
    indexing_state: str | None
    page_fetch_state: str | None
    last_crawl_time: datetime | None
    google_canonical: str | None
    user_canonical: str | None
    crawled_as: str | None
    sitemaps: tuple[str, ...]
    referring_urls: tuple[str, ...]
    inspection_link: str | None
    provenance: Provenance


type DomainRecord = (
    MailMessage
    | CalendarEvent
    | MeetingTranscript
    | ExternalIssue
    | TicketCase
    | DriveFile
    | SearchAnalyticsReport
    | SearchSitemap
    | SearchUrlInspection
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
    Capability.CALENDAR_EVENTS_LIST: CalendarEvent,
    Capability.MEETING_TRANSCRIPTS_LIST: MeetingTranscript,
    Capability.MEETING_TRANSCRIPT_GET: MeetingTranscript,
    Capability.FILES_SEARCH: DriveFile,
    Capability.FILES_GET: DriveFile,
    Capability.EXTERNAL_ISSUES_SEARCH: ExternalIssue,
    Capability.TICKETS_SEARCH: TicketCase,
    Capability.SEARCH_ANALYTICS_QUERY: SearchAnalyticsReport,
    Capability.SEARCH_SITEMAPS_LIST: SearchSitemap,
    Capability.SEARCH_URLS_INSPECT: SearchUrlInspection,
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
