from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any


class Capability(StrEnum):
    MAIL_SENT_LIST = "mail.sent.list"
    MAIL_INBOX_LIST = "mail.inbox.list"
    # The Outlook category write: see docs/decisions/2026-10-19-escritura-categorias-outlook.md.
    MAIL_CATEGORIES_SET = "mail.categories.set"
    CALENDAR_EVENTS_LIST = "calendar.events.list"
    MEETING_TRANSCRIPTS_LIST = "meeting.transcripts.list"
    MEETING_TRANSCRIPT_GET = "meeting.transcript.get"
    FILES_SEARCH = "files.search"
    FILES_GET = "files.get"
    EXTERNAL_ISSUES_SEARCH = "external.issues.search"
    # Official route only: see docs/decisions/2026-10-25-escritura-transiciones-jira.md. The
    # transition is the second write, after the Outlook categories.
    EXTERNAL_ISSUES_TRANSITIONS_LIST = "external.issues.transitions.list"
    EXTERNAL_ISSUES_TRANSITION = "external.issues.transition"
    TICKETS_SEARCH = "tickets.search"
    # The Gmail send in two steps, on the gmail-compose target, official route only: see
    # docs/decisions/2026-10-25-envio-correo-gmail-dos-pasos.md. It is the third write.
    MAIL_DRAFT_CREATE = "mail.draft.create"
    MAIL_DRAFT_SEND = "mail.draft.send"
    # Bitbucket Cloud reads, official route only: docs/decisions/2026-10-26-conector-bitbucket.md.
    SCM_REPOSITORIES_LIST = "scm.repositories.list"
    SCM_PULLREQUESTS_LIST = "scm.pullrequests.list"


class ProviderFamily(StrEnum):
    MICROSOFT = "microsoft"
    GOOGLE = "google"
    JIRA = "jira"
    GLPI = "glpi"
    # Read-only and official-only: docs/decisions/2026-10-26-conector-bitbucket.md.
    BITBUCKET = "bitbucket"


# The family each official provider belongs to. A request names its official provider even when
# that adapter was never configured, so the runtime reads the target's family here and picks the
# browser route by family, never by which browser adapter happened to be registered first.
_OFFICIAL_PROVIDER_FAMILIES: dict[str, ProviderFamily] = {
    "microsoft.graph": ProviderFamily.MICROSOFT,
    "google.gmail": ProviderFamily.GOOGLE,
    "google.calendar": ProviderFamily.GOOGLE,
    "google.meet": ProviderFamily.GOOGLE,
    "jira.cloud": ProviderFamily.JIRA,
    "glpi.rest": ProviderFamily.GLPI,
    "bitbucket.cloud": ProviderFamily.BITBUCKET,
}


def official_provider_family(provider_id: str) -> ProviderFamily | None:
    """Return the family of a known official provider ID, or ``None`` for any other ID."""
    return _OFFICIAL_PROVIDER_FAMILIES.get(provider_id)


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
    WRITE_NOT_AUTHORIZED = "write_not_authorized"
    WRITE_CONFLICT = "write_conflict"
    ISSUE_NOT_FOUND = "issue_not_found"
    TRANSITION_UNAVAILABLE = "transition_unavailable"
    TRANSITION_AMBIGUOUS = "transition_ambiguous"
    TRANSITION_REQUIRES_FIELDS = "transition_requires_fields"
    WRITE_SCOPE_MISSING = "write_scope_missing"
    WRITE_UNVERIFIED = "write_unverified"
    DRAFT_NOT_FOUND = "draft_not_found"
    DUPLICATE_SUSPECTED = "duplicate_suspected"
    DUPLICATE_CHECK_UNAVAILABLE = "duplicate_check_unavailable"
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    # A paged read reached its fixed page bound: nothing is returned rather than a partial list.
    RESULT_INCOMPLETE = "result_incomplete"
    REPOSITORY_NOT_FOUND = "repository_not_found"


# Capabilities that change provider state. Each one needs explicit authorization per invocation,
# never runs over HTTP, and is never retried automatically.
WRITE_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.MAIL_CATEGORIES_SET,
        Capability.EXTERNAL_ISSUES_TRANSITION,
        Capability.MAIL_DRAFT_CREATE,
        Capability.MAIL_DRAFT_SEND,
    }
)


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


# One Jira issue key, exactly: a project key, a hyphen and a positive number without leading zeros.
# Lists, ranges and JQL never match, so an issue-scoped capability reaches one issue at most.
_JIRA_ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]+-[1-9][0-9]*")


def require_jira_issue_key(value: object) -> str:
    """Return the key unchanged when it is one exact Jira issue key; refuse anything else."""
    if not isinstance(value, str) or _JIRA_ISSUE_KEY.fullmatch(value) is None:
        raise ValueError(
            "issue must be one exact Jira issue key such as CP-1356; lists, ranges and JQL are "
            "not accepted"
        )
    return value


@dataclass(frozen=True, slots=True)
class IssueTransitionsQuery:
    """List the workflow transitions available to one Jira issue, named by its exact key."""

    issue_key: str
    account_id: str | None = None

    def __post_init__(self) -> None:
        require_jira_issue_key(self.issue_key)


@dataclass(frozen=True, slots=True)
class IssueTransitionRequest:
    """Move one Jira issue, named by its exact key, to the status called ``to_status``.

    The status name is trimmed and compared without case. ``write_authorized`` must be set
    explicitly per invocation; the runtime refuses the write otherwise.
    """

    issue_key: str
    to_status: str
    account_id: str | None = None
    write_authorized: bool = False

    def __post_init__(self) -> None:
        require_jira_issue_key(self.issue_key)
        if not isinstance(self.to_status, str) or not self.to_status.strip():
            raise ValueError("A Jira transition needs a target status name")
        object.__setattr__(self, "to_status", self.to_status.strip())


# One Bitbucket workspace slug, as it appears in bitbucket.org/<workspace>/<repository>. Nothing
# that could reshape the URL path it is joined to ever matches.
_BITBUCKET_WORKSPACE = re.compile(r"[a-z0-9][a-z0-9_-]{0,61}")


def normalize_bitbucket_workspace(value: object) -> str:
    """Return the lower-case workspace slug, or refuse anything that is not one slug."""
    # lower(), not casefold(): casefold would turn a non-ASCII letter such as ß into a valid slug.
    candidate = value.strip().lower() if isinstance(value, str) else ""
    if _BITBUCKET_WORKSPACE.fullmatch(candidate) is None:
        raise ValueError(
            "The Bitbucket workspace must be one slug of lower-case letters, digits, '-' or '_', "
            "such as idartes-tecnologia"
        )
    return candidate


# One Bitbucket repository slug, as in bitbucket.org/<workspace>/<repository>. No slash, space or
# query character can match, so a slug never reshapes the URL path it is joined to.
_BITBUCKET_REPOSITORY = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
# The pull request states Bitbucket documents, in the order a query lists them.
PULL_REQUEST_STATES: tuple[str, ...] = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")


def normalize_bitbucket_repository(value: object) -> str:
    """Return the lower-case repository slug, or refuse anything that is not one slug."""
    candidate = value.strip().lower() if isinstance(value, str) else ""
    if _BITBUCKET_REPOSITORY.fullmatch(candidate) is None:
        raise ValueError(
            "The Bitbucket repository must be one slug of lower-case letters, digits, '.', '-' "
            "or '_', such as pandora_proxy"
        )
    return candidate


@dataclass(frozen=True, slots=True)
class ScmRepositoryQuery:
    """List the repositories of one Bitbucket workspace the account is a member of.

    Without a workspace the adapter reads the one stored with the connection.
    """

    workspace: str | None = None
    account_id: str | None = None

    def __post_init__(self) -> None:
        if self.workspace is not None:
            object.__setattr__(self, "workspace", normalize_bitbucket_workspace(self.workspace))


@dataclass(frozen=True, slots=True)
class ScmPullRequestQuery:
    """List the pull requests of one repository updated inside an end-exclusive window.

    ``states`` keeps the documented order and drops repeats; it defaults to every state.
    """

    repository: str
    window: DateWindow
    states: tuple[str, ...] = PULL_REQUEST_STATES
    workspace: str | None = None
    account_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", normalize_bitbucket_repository(self.repository))
        if self.workspace is not None:
            object.__setattr__(self, "workspace", normalize_bitbucket_workspace(self.workspace))
        requested: set[str] = set()
        for state in self.states:
            name = state.strip().upper() if isinstance(state, str) else ""
            if name not in PULL_REQUEST_STATES:
                raise ValueError(
                    "Each pull request state must be one of " + ", ".join(PULL_REQUEST_STATES)
                )
            requested.add(name)
        if not requested:
            raise ValueError("A pull request query needs at least one state")
        object.__setattr__(
            self, "states", tuple(state for state in PULL_REQUEST_STATES if state in requested)
        )


def same_status_name(first: str, second: str) -> bool:
    """Tell whether two Jira status names are the same, ignoring case and outer whitespace."""
    return first.strip().casefold() == second.strip().casefold()


# One email address: the rule that also validates connection accounts (config.normalize_account).
EMAIL_ADDRESS_PATTERN = re.compile(
    r"(?=.{3,320}\Z)[^\s<>@]+@[^\s<>@.]+(?:\.[^\s<>@.]+)+\Z",
    re.ASCII,
)
# A recipient written into a mail header must also be a plain dot-atom address, so no comma,
# quote, parenthesis or angle bracket can add a second recipient or a comment to the header.
_HEADER_SAFE_ADDRESS = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
MAX_DRAFT_RECIPIENTS = 20
MAX_DRAFT_BODY_BYTES = 200 * 1024
MAX_DRAFT_SUBJECT_CHARS = 255
DRAFT_PREVIEW_CHARS = 200


def normalize_mail_recipient(value: object, label: str) -> str:
    """Return one recipient address in lower case, or refuse anything else."""
    if isinstance(value, str):
        candidate = value.strip().casefold()
        if EMAIL_ADDRESS_PATTERN.fullmatch(candidate) and _HEADER_SAFE_ADDRESS.fullmatch(candidate):
            return candidate
    raise ValueError(f"A --{label} value is not a valid email address")


def require_draft_subject(value: object) -> str:
    """Return the trimmed subject when it is one non-empty, bounded line."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A Gmail draft needs a non-empty subject")
    subject = value.strip()
    if len(subject.splitlines()) != 1 or any(
        ord(char) < 32 or ord(char) == 127 for char in subject
    ):
        raise ValueError("The subject must be one line without control characters")
    if len(subject) > MAX_DRAFT_SUBJECT_CHARS:
        raise ValueError(f"The subject is longer than {MAX_DRAFT_SUBJECT_CHARS} characters")
    return subject


@dataclass(frozen=True, slots=True)
class MailDraftRequest:
    """One plain-text Gmail draft from the connected account, with explicit recipients.

    Every recipient is validated and appears once across ``to``, ``cc`` and ``bcc``, at most
    ``MAX_DRAFT_RECIPIENTS`` in total. The body is UTF-8 text of at most ``MAX_DRAFT_BODY_BYTES``
    and never appears in a representation. ``write_authorized`` must be set explicitly per
    invocation; the runtime refuses the write otherwise.
    """

    to: tuple[str, ...]
    subject: str
    body: str = field(repr=False)
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    account_id: str | None = None
    write_authorized: bool = False

    def __post_init__(self) -> None:
        to = _draft_recipients(self.to, "to")
        cc = _draft_recipients(self.cc, "cc")
        bcc = _draft_recipients(self.bcc, "bcc")
        if not to:
            raise ValueError("A Gmail draft needs at least one --to recipient")
        everyone = [*to, *cc, *bcc]
        if len(set(everyone)) != len(everyone):
            raise ValueError("A recipient appears more than once across --to, --cc and --bcc")
        if len(everyone) > MAX_DRAFT_RECIPIENTS:
            raise ValueError(
                f"A Gmail draft takes at most {MAX_DRAFT_RECIPIENTS} recipients across --to, --cc "
                "and --bcc"
            )
        object.__setattr__(self, "to", to)
        object.__setattr__(self, "cc", cc)
        object.__setattr__(self, "bcc", bcc)
        object.__setattr__(self, "subject", require_draft_subject(self.subject))
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("A Gmail draft needs a non-empty body")
        try:
            size = len(self.body.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("The body is not valid UTF-8 text") from None
        if size > MAX_DRAFT_BODY_BYTES:
            raise ValueError(f"The body is larger than {MAX_DRAFT_BODY_BYTES // 1024} KB")


# A Gmail draft id as Gmail issues it; nothing that could reshape the URL path it is joined to.
_GMAIL_DRAFT_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")


@dataclass(frozen=True, slots=True)
class MailDraftSendRequest:
    """Send one existing Gmail draft, named by its id, after the user approved that draft.

    Unless ``allow_duplicate`` is set, the send first refuses a draft whose subject and recipients
    match a message already in Sent. ``write_authorized`` must be set explicitly per invocation.
    """

    draft_id: str
    account_id: str | None = None
    write_authorized: bool = False
    allow_duplicate: bool = False

    def __post_init__(self) -> None:
        candidate = self.draft_id.strip() if isinstance(self.draft_id, str) else ""
        if _GMAIL_DRAFT_ID.fullmatch(candidate) is None:
            raise ValueError("The draft id must be one Gmail draft identifier, such as r-123")
        object.__setattr__(self, "draft_id", candidate)


def _draft_recipients(values: object, label: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{label} must be a list of email addresses")
    return tuple(normalize_mail_recipient(value, label) for value in values)


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


@dataclass(frozen=True, slots=True)
class MailCategoriesUpdate:
    """Assign Outlook categories to one message, declared as a change to its current list.

    ``message_id`` is the provider id carried by the ``id`` field of mail records. ``add`` and
    ``remove`` may be combined so a state changes in one write; ``replace`` sets the whole list and
    excludes both. Names compare without case, so asking for a name the message already carries
    in another case changes nothing. ``write_authorized`` must be set explicitly per invocation.
    """

    message_id: str
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    replace: tuple[str, ...] | None = None
    account_id: str | None = None
    write_authorized: bool = False

    def __post_init__(self) -> None:
        if not self.message_id.strip():
            raise ValueError("MailCategoriesUpdate message_id cannot be blank")
        object.__setattr__(self, "message_id", self.message_id.strip())
        object.__setattr__(self, "add", _category_names(self.add, "add"))
        object.__setattr__(self, "remove", _category_names(self.remove, "remove"))
        if self.replace is not None:
            if self.add or self.remove:
                raise ValueError("Category replace excludes add and remove")
            replace = _category_names(self.replace, "replace")
            if not replace:
                raise ValueError("Category replace needs at least one name")
            object.__setattr__(self, "replace", replace)
        elif not self.add and not self.remove:
            raise ValueError("A category update needs at least one name to add, remove or replace")
        overlap = {name.casefold() for name in self.add} & {name.casefold() for name in self.remove}
        if overlap:
            raise ValueError("A category cannot be both added and removed in one update")

    def resolve(self, current: tuple[str, ...]) -> tuple[str, ...]:
        """Compute the list the message should carry, preserving the order already there."""
        if self.replace is not None:
            return self.replace
        removed = {name.casefold() for name in self.remove}
        result = [name for name in current if name.casefold() not in removed]
        present = {name.casefold() for name in result}
        for name in self.add:
            if name.casefold() not in present:
                result.append(name)
                present.add(name.casefold())
        return tuple(result)


def same_categories(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Tell whether two category lists carry the same names, ignoring case and order."""
    return sorted(name.casefold() for name in first) == sorted(name.casefold() for name in second)


def _category_names(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Category {label} cannot contain a blank name")
        name = value.strip()
        if name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return tuple(names)


type QueryPayload = (
    MailQuery
    | CalendarQuery
    | TranscriptQuery
    | IssueQuery
    | IssueTransitionsQuery
    | IssueTransitionRequest
    | TicketQuery
    | FileQuery
    | MailCategoriesUpdate
    | MailDraftRequest
    | MailDraftSendRequest
    | ScmRepositoryQuery
    | ScmPullRequestQuery
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
class IssueTransitionOption:
    """One workflow transition a Jira issue can take now, with the status it would reach.

    ``requires_fields`` is true when the transition screen declares a required field; the write
    refuses such a transition because it never sets fields. ``current_status`` repeats on every
    option so a caller sees where the issue stands without a second read.
    """

    issue_key: str
    transition_id: str
    transition_name: str
    to_status: str
    to_status_category: str | None
    requires_fields: bool
    current_status: str
    current_status_category: str | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class IssueTransitionResult:
    """The outcome of one Jira transition: the status read before and after the write.

    When the issue already had the requested status nothing is written, ``changed`` is false and
    the transition fields stay unset.
    """

    issue_key: str
    previous_status: str
    status: str
    transition_id: str | None
    transition_name: str | None
    changed: bool
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
class MailCategoryAssignment:
    """The outcome of one category write: the list the message now carries and what it had."""

    message_id: str
    categories: tuple[str, ...]
    previous_categories: tuple[str, ...]
    changed: bool
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class MailDraft:
    """A Gmail draft created for human review.

    ``body_preview`` holds only the first ``DRAFT_PREVIEW_CHARS`` characters of the body: it is
    what an agent shows the user before asking to approve the send, never the whole body.
    """

    draft_id: str
    message_id: str
    thread_id: str | None
    sender: EmailAddress
    to: tuple[EmailAddress, ...]
    cc: tuple[EmailAddress, ...]
    bcc: tuple[EmailAddress, ...]
    subject: str
    body_preview: str
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class MailSendResult:
    """The outcome of one Gmail draft send: the sent message and who it went to.

    ``sent_at`` is Gmail's ``internalDate`` when the send answer carries it, and otherwise the
    moment Connect received Gmail's confirmation.
    """

    draft_id: str
    message_id: str
    thread_id: str | None
    sender: EmailAddress
    to: tuple[EmailAddress, ...]
    cc: tuple[EmailAddress, ...]
    bcc: tuple[EmailAddress, ...]
    subject: str
    sent_at: datetime
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ScmRepository:
    """One Bitbucket repository, with the time of its last update as Bitbucket reports it."""

    workspace: str
    slug: str
    name: str
    full_name: str
    is_private: bool
    main_branch: str | None
    project_key: str | None
    updated_at: datetime
    web_url: str | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ScmPullRequest:
    """One Bitbucket pull request; ``merge_commit`` is set only once Bitbucket merged it."""

    id: int
    title: str
    state: str
    draft: bool
    author_account_id: str | None
    author_name: str | None
    source_branch: str | None
    destination_branch: str | None
    created_at: datetime
    updated_at: datetime
    merge_commit: str | None
    web_url: str | None
    repository: str
    workspace: str
    provenance: Provenance


type DomainRecord = (
    MailMessage
    | CalendarEvent
    | MeetingTranscript
    | ExternalIssue
    | IssueTransitionOption
    | IssueTransitionResult
    | TicketCase
    | DriveFile
    | MailCategoryAssignment
    | MailDraft
    | MailSendResult
    | ScmRepository
    | ScmPullRequest
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
    Capability.MAIL_CATEGORIES_SET: MailCategoryAssignment,
    Capability.CALENDAR_EVENTS_LIST: CalendarEvent,
    Capability.MEETING_TRANSCRIPTS_LIST: MeetingTranscript,
    Capability.MEETING_TRANSCRIPT_GET: MeetingTranscript,
    Capability.FILES_SEARCH: DriveFile,
    Capability.FILES_GET: DriveFile,
    Capability.EXTERNAL_ISSUES_SEARCH: ExternalIssue,
    Capability.EXTERNAL_ISSUES_TRANSITIONS_LIST: IssueTransitionOption,
    Capability.EXTERNAL_ISSUES_TRANSITION: IssueTransitionResult,
    Capability.TICKETS_SEARCH: TicketCase,
    Capability.MAIL_DRAFT_CREATE: MailDraft,
    Capability.MAIL_DRAFT_SEND: MailSendResult,
    Capability.SCM_REPOSITORIES_LIST: ScmRepository,
    Capability.SCM_PULLREQUESTS_LIST: ScmPullRequest,
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
