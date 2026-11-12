from __future__ import annotations

from datetime import date, datetime
from typing import Any

from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.browser_routes import has_browser_route
from solercia_connect.config import (
    BITBUCKET_TARGETS,
    GOOGLE_TARGETS,
    MICROSOFT_TARGETS,
    ConnectionType,
    ConnectSettings,
    ProfileStore,
    load_settings,
    normalize_account,
    target_spec,
)
from solercia_connect.connections import (
    bitbucket_failure_remediation,
    connect_command,
    http_connect_hint,
)
from solercia_connect.domain import (
    PULL_REQUEST_STATES,
    WRITE_CAPABILITIES,
    CalendarQuery,
    Capability,
    ConnectorRequest,
    DateWindow,
    FileQuery,
    IssueQuery,
    IssueTransitionRequest,
    IssueTransitionsQuery,
    MailCategoriesUpdate,
    MailDraftRequest,
    MailDraftSendRequest,
    MailQuery,
    OpenIssueQuery,
    QueryPayload,
    ReasonCode,
    ScmCommitQuery,
    ScmPullRequestQuery,
    ScmRepositoryCreateRequest,
    ScmRepositoryQuery,
    SearchAnalyticsQuery,
    SearchSitemapsQuery,
    SearchUrlInspectionQuery,
    TicketQuery,
    TranscriptQuery,
)
from solercia_connect.envelope import ErrorDetail, ResultEnvelope, versioned_record
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import OAuthTokenStore, grant_covers
from solercia_connect.project_secrets import oauth_token_store_from_settings
from solercia_connect.runtime import ConnectorRuntime


def run_application(
    payload: dict[str, Any],
    *,
    interactive: bool = True,
    settings: ConnectSettings | None = None,
    token_store: OAuthTokenStore | None = None,
    browser_via_http: bool = False,
) -> ResultEnvelope:
    """Execute a validated connector request for CLI or HTTP adapters.

    ``browser_via_http`` is the loopback service's opt-in: the browser adapter may serve the
    read (headless, over the persistent profile) even though nobody is at a terminal. An
    interactive prerequisite still comes back as ``action_required`` so the caller can tell
    "needs login" from "failed".
    """

    provider: str | None = None
    account: str | None = None
    try:
        type_value = ConnectionType.parse(_required_string(payload, "type"))
        spec = target_spec(type_value)
        provider = type_value.value
        raw_account = _optional_string(payload.get("account"))
        account = normalize_account(raw_account) if raw_account is not None else None
        capability = Capability(_required_string(payload, "capability"))
        if capability.value not in spec.capabilities:
            raise ValueError(
                f"Capability {capability.value} is not available for {type_value.value}"
            )
        writes = capability in WRITE_CAPABILITIES
        if not writes and payload.get("allow_write", False) is not False:
            raise ValueError(
                "allow_write applies only to the writes "
                + ", ".join(sorted(item.value for item in WRITE_CAPABILITIES))
            )
        if capability is not Capability.MAIL_CATEGORIES_SET and any(
            payload.get(name) is not None for name in ("add", "remove", "replace")
        ):
            raise ValueError("add, remove and replace apply only to mail.categories.set")
        if capability not in _ISSUE_CAPABILITIES and payload.get("issue") is not None:
            raise ValueError("issue applies only to the Jira transition capabilities")
        if (
            capability is not Capability.EXTERNAL_ISSUES_OPEN_LIST
            and payload.get("sprint") is not None
        ) or (capability not in _PROJECT_CAPABILITIES and payload.get("project") is not None):
            raise ValueError(
                "project and sprint apply only to external.issues.open.list; project also "
                "applies to scm.repositories.create"
            )
        if capability is not Capability.MAIL_DRAFT_CREATE and any(
            payload.get(name) is not None for name in ("cc", "bcc", "subject", "body")
        ):
            raise ValueError("cc, bcc, subject and body apply only to mail.draft.create")
        if capability is not Capability.MAIL_DRAFT_SEND and (
            payload.get("draft_id") is not None
            or payload.get("allow_duplicate", False) is not False
        ):
            raise ValueError("draft_id and allow_duplicate apply only to mail.draft.send")
        if capability not in _TO_CAPABILITIES and payload.get("to") is not None:
            raise ValueError("to applies only to external.issues.transition and mail.draft.create")
        for name in _SCM_OPTIONS:
            if payload.get(name) is not None and name not in _SCM_OPTIONS_BY_CAPABILITY.get(
                capability, ()
            ):
                raise ValueError(f"{name} does not apply to {capability.value}")
        if type_value in BITBUCKET_TARGETS and account is None:
            raise ValueError(
                f"account is required for {type_value.value} runs; connect it first with "
                + connect_command(type_value, "<account>")
            )
        if writes and account is None:
            raise ValueError(f"account is required for {capability.value}")
        query = _query(type_value, capability, account, payload)
        fallback = payload.get("allow_browser_fallback", False)
        if not isinstance(fallback, bool):
            raise ValueError("allow_browser_fallback must be a boolean")
        if fallback and type_value is ConnectionType.GMAIL_COMPOSE:
            raise ValueError(
                "gmail-compose runs only over the official Gmail API; browser fallback does not "
                "apply"
            )
        if fallback and type_value in BITBUCKET_TARGETS:
            raise ValueError(
                f"{type_value.value} runs only over the official Bitbucket Cloud API; browser "
                "fallback does not apply"
            )
        if (
            fallback
            and type_value is ConnectionType.GOOGLE_MEET
            and not has_browser_route(type_value)
        ):
            raise ValueError("Browser fallback is not available for Google Meet")
        if not interactive and type_value in _OAUTH_TYPES and account is None:
            raise ValueError("account is required for OAuth provider runs")
    except (KeyError, TypeError, ValueError) as failure:
        return ResultEnvelope.failure(
            operation="run",
            status="invalid_input",
            provider=provider,
            account=account,
            error=ErrorDetail(
                "invalid_input",
                str(failure),
                False,
                remediation=("Correct the typed run request and retry.",),
                cause_category="input",
            ),
        )

    if isinstance(
        query,
        (
            MailCategoriesUpdate,
            IssueTransitionRequest,
            MailDraftRequest,
            MailDraftSendRequest,
            ScmRepositoryCreateRequest,
        ),
    ):
        assert account is not None
        if not interactive:
            return http_write_rejection(type_value, account, capability)
        if not query.write_authorized:
            return _write_not_authorized(type_value, account, capability)

    if not interactive and fallback and not browser_via_http:
        assert account is not None
        return _interactive_action(type_value, account, browser_fallback=True)

    active_settings = settings
    if not interactive and not browser_via_http and type_value in _OAUTH_TYPES:
        assert account is not None
        try:
            active_settings = active_settings or load_settings()
            missing = _missing_oauth_configuration(type_value, active_settings)
            if missing:
                return _configuration_failure(
                    type_value,
                    account,
                    "Required OAuth configuration is missing",
                    missing,
                )
            profile = ProfileStore.from_settings(active_settings).get(type_value, account)
        except (TypeError, ValueError) as failure:
            return _configuration_failure(type_value, account, str(failure))
        if profile is None or profile.route == "browser":
            return _interactive_action(type_value, account)
        try:
            store = token_store or oauth_token_store_from_settings(active_settings)
            tokens = store.load(f"{type_value.value}:{account}")
        except ConnectorFailure as failure:
            return _configuration_failure(type_value, account, failure.safe_message)
        if tokens is None or not grant_covers(target_spec(type_value).oauth_scopes, tokens.scopes):
            return _interactive_action(type_value, account)

    try:
        registry = registry_from_environment(
            connection_type=type_value,
            account=account,
            capability=capability,
            include_browser=(interactive or browser_via_http) and fallback,
            settings_override=active_settings,
        )
        outcome = ConnectorRuntime(registry).execute(
            ConnectorRequest.create(spec.provider_id, capability, query),
            browser_fallback_enabled=fallback,
        )
        if outcome.ok:
            return ResultEnvelope.success(
                operation="run",
                provider=type_value.value,
                account=account,
                route=(
                    outcome.receipt.selected_route.value
                    if outcome.receipt.selected_route is not None
                    else None
                ),
                data={"records": [versioned_record(record) for record in outcome.records]},
                receipt=outcome.receipt,
            )
        if not interactive and outcome.receipt.final_reason in _INTERACTIVE_REASONS:
            assert account is not None
            return _interactive_action(
                type_value,
                account,
                browser_fallback=fallback,
                receipt=outcome.receipt,
                via_http=browser_via_http,
            )
        final_reason = outcome.receipt.final_reason
        # The step that produced the final reason explains it; a trailing browser-policy step
        # (fallback disabled or denied) only records that no browser attempt followed.
        detail = next(
            (
                step.detail
                for step in reversed(outcome.receipt.steps)
                if step.reason is final_reason
            ),
            outcome.receipt.steps[-1].detail if outcome.receipt.steps else "Run failed",
        )
        return ResultEnvelope.failure(
            operation="run",
            status="failed",
            provider=type_value.value,
            account=account,
            receipt=outcome.receipt,
            # A read that hit its page bound reports nothing, and says so explicitly.
            data={"complete": False} if final_reason is ReasonCode.RESULT_INCOMPLETE else None,
            error=_reason_error(
                final_reason,
                detail,
                remediation=_failure_remediation(final_reason, type_value, account),
            ),
        )
    except (TypeError, ValueError) as failure:
        assert provider is not None
        return _configuration_failure(type_value, account, str(failure))
    except ConnectorFailure as failure:
        if not interactive and failure.reason in _INTERACTIVE_REASONS:
            assert account is not None
            return _interactive_action(
                type_value,
                account,
                browser_fallback=fallback,
                via_http=browser_via_http,
            )
        return ResultEnvelope.failure(
            operation="run",
            status="failed",
            provider=provider,
            account=account,
            error=_reason_error(failure.reason, failure.safe_message),
        )
    except Exception:
        return ResultEnvelope.failure(
            operation="run",
            status="failed",
            provider=provider,
            account=account,
            error=ErrorDetail(
                "unknown_error",
                "Connector run failed with an unknown error",
                False,
                remediation=("Run doctor and retry with sanitized diagnostics.",),
                cause_category="unknown",
            ),
        )


_OAUTH_TYPES = MICROSOFT_TARGETS | GOOGLE_TARGETS
# Capabilities addressed by one exact Jira issue key instead of a window.
_ISSUE_CAPABILITIES = frozenset(
    {Capability.EXTERNAL_ISSUES_TRANSITIONS_LIST, Capability.EXTERNAL_ISSUES_TRANSITION}
)
# The Bitbucket reads and the options that apply to them alone.
_SCM_OPTIONS = ("workspace", "repo", "state", "branch")
_SCM_OPTIONS_BY_CAPABILITY: dict[Capability, tuple[str, ...]] = {
    Capability.SCM_REPOSITORIES_LIST: ("workspace",),
    Capability.SCM_PULLREQUESTS_LIST: ("workspace", "repo", "state"),
    Capability.SCM_COMMITS_LIST: ("workspace", "repo", "branch"),
    Capability.SCM_REPOSITORIES_CREATE: ("workspace", "repo"),
}
# `project` is the Jira project of the open-issue listing and the Bitbucket project of a creation.
_PROJECT_CAPABILITIES = frozenset(
    {Capability.EXTERNAL_ISSUES_OPEN_LIST, Capability.SCM_REPOSITORIES_CREATE}
)
# `to` is the target status of a Jira transition and the recipients of a Gmail draft.
_TO_CAPABILITIES = frozenset({Capability.EXTERNAL_ISSUES_TRANSITION, Capability.MAIL_DRAFT_CREATE})
# What each write changes, named in the refusals of a write that cannot run.
_WRITE_EFFECTS = {
    Capability.MAIL_CATEGORIES_SET: "changes the mailbox",
    Capability.EXTERNAL_ISSUES_TRANSITION: "changes the status of a Jira issue",
    Capability.MAIL_DRAFT_CREATE: "creates a Gmail draft",
    Capability.MAIL_DRAFT_SEND: "sends a Gmail draft",
    Capability.SCM_REPOSITORIES_CREATE: "creates a Bitbucket repository",
}
_INTERACTIVE_REASONS = frozenset(
    {
        ReasonCode.INVALID_CREDENTIALS,
        ReasonCode.BROWSER_LOGIN_REQUIRED,
        ReasonCode.BROWSER_MFA_REQUIRED,
    }
)


def _interactive_action(
    type_value: ConnectionType,
    account: str,
    *,
    browser_fallback: bool = False,
    receipt: Any = None,
    via_http: bool = False,
) -> ResultEnvelope:
    remediation: tuple[str, ...] = (
        connect_command(
            type_value,
            account,
            allow_browser_fallback=browser_fallback,
        ),
    )
    if via_http:
        remediation += (http_connect_hint(type_value, account),)
    return ResultEnvelope.failure(
        operation="run",
        status="action_required",
        provider=type_value.value,
        account=account,
        receipt=receipt,
        data={"interactive_flow_started": False},
        error=ErrorDetail(
            "action_required",
            (
                "Interactive browser login is required before this read can run"
                if via_http
                else "Interactive authorization must run in the canonical CLI"
            ),
            True,
            remediation=remediation,
            cause_category="interactive_authorization",
        ),
    )


def http_write_rejection(
    type_value: ConnectionType,
    account: str | None,
    capability: Capability = Capability.MAIL_CATEGORIES_SET,
) -> ResultEnvelope:
    """Refuse a write for a caller without a terminal; the write exists only in the CLI."""
    return ResultEnvelope.failure(
        operation="run",
        status="forbidden",
        provider=type_value.value,
        account=account,
        error=ErrorDetail(
            "write_not_available_over_http",
            f"{capability.value} {_WRITE_EFFECTS[capability]} and runs only in the canonical CLI",
            False,
            remediation=(_write_command(type_value, account, capability),),
            cause_category="policy",
        ),
    )


def _write_not_authorized(
    type_value: ConnectionType, account: str, capability: Capability
) -> ResultEnvelope:
    return ResultEnvelope.failure(
        operation="run",
        status="invalid_input",
        provider=type_value.value,
        account=account,
        error=ErrorDetail(
            "write_not_authorized",
            f"{capability.value} {_WRITE_EFFECTS[capability]} and needs --allow-write on this "
            "invocation; nothing was sent to the provider",
            False,
            remediation=(_write_command(type_value, account, capability),),
            cause_category="input",
        ),
    )


def _write_command(type_value: ConnectionType, account: str | None, capability: Capability) -> str:
    prefix = (
        f"solercia-connect run --provider {target_spec(type_value).provider_id} "
        f"--capability {capability.value} --account {account or '<account>'} "
    )
    if capability is Capability.EXTERNAL_ISSUES_TRANSITION:
        return prefix + "--issue <KEY> --to '<status>' --allow-write"
    if capability is Capability.MAIL_DRAFT_CREATE:
        return prefix + "--to <recipient> --subject '<subject>' --body-file <path> --allow-write"
    if capability is Capability.MAIL_DRAFT_SEND:
        return prefix + "--draft-id <id> --allow-write"
    if capability is Capability.SCM_REPOSITORIES_CREATE:
        return prefix + "--repo <slug> --project <KEY> --allow-write"
    return prefix + "--message-id <id> --add <category> --allow-write --allow-browser-fallback"


def _failure_remediation(
    reason: ReasonCode, type_value: ConnectionType, account: str | None
) -> tuple[str, ...] | None:
    """Name the next step of a failed run; ``None`` keeps the generic doctor remediation."""
    if reason is ReasonCode.RESULT_INCOMPLETE:
        return (
            "The read stopped at its fixed page bound and returned nothing rather than a partial "
            "list; narrow the request, such as a shorter --start/--end window, and report it.",
        )
    if type_value is ConnectionType.BITBUCKET and reason is ReasonCode.REPOSITORY_NOT_FOUND:
        return (
            "Check the repository slug; list the slugs this account reaches with "
            "--capability scm.repositories.list.",
        )
    if type_value is ConnectionType.BITBUCKET_ADMIN and reason in _CREATION_REMEDIATION:
        return (_CREATION_REMEDIATION[reason],)
    if type_value in BITBUCKET_TARGETS:
        return bitbucket_failure_remediation(
            reason, account or "<account>", connection_type=type_value
        )
    if reason is ReasonCode.WRITE_SCOPE_MISSING:
        return _write_scope_remediation(type_value, account)
    if reason is ReasonCode.DUPLICATE_CHECK_UNAVAILABLE:
        return (
            connect_command(ConnectionType.GMAIL, account or "<account>"),
            "Or pass --allow-duplicate only after the user explicitly approves sending without "
            "the duplicate check.",
        )
    if reason is ReasonCode.DUPLICATE_SUSPECTED:
        return (
            "Review Gmail > Sent with the user; rerun with --allow-duplicate only after the user "
            "explicitly approves sending it again.",
        )
    if reason is ReasonCode.WRITE_UNVERIFIED and type_value is ConnectionType.GMAIL_COMPOSE:
        return (
            "Check Gmail > Sent and Gmail > Drafts with the user before any new attempt; never "
            "resend on your own.",
        )
    return None


# The next step of each repository creation failure that is not about the connection itself.
_CREATION_REMEDIATION = {
    ReasonCode.REPOSITORY_EXISTS: (
        "The repository already exists and nothing was changed; report it to the user and never "
        "pick another slug on your own."
    ),
    ReasonCode.PROJECT_NOT_FOUND: (
        "Check the project key in the workspace's Projects page with the user; the project must "
        "exist before the repository."
    ),
    ReasonCode.WRITE_UNVERIFIED: (
        "Review the repository in Bitbucket with the user before any new attempt; never retry "
        "the creation on your own."
    ),
    ReasonCode.REQUEST_REJECTED: (
        "Check the slug, the project key and that this account may create repositories in the "
        "workspace; nothing was created."
    ),
}


def _write_scope_remediation(type_value: ConnectionType, account: str | None) -> tuple[str, ...]:
    """Point a connection without its write scope at the connect that grants it.

    A gmail-compose grant reconnects as itself; an OAuth Jira connection reconnects with an API
    token, the only Jira credential that can write.
    """
    command = connect_command(type_value, account or "<account>")
    if type_value is ConnectionType.GMAIL_COMPOSE:
        return (command,)
    return (f"{command} --token-env <VARIABLE>",)


def _configuration_failure(
    type_value: ConnectionType,
    account: str | None,
    message: str,
    missing: tuple[str, ...] = (),
) -> ResultEnvelope:
    return ResultEnvelope.failure(
        operation="run",
        status="configuration_error",
        provider=type_value.value,
        account=account,
        error=ErrorDetail(
            "configuration_invalid",
            message,
            False,
            missing,
            tuple(f"Set {name}." for name in missing)
            or ("Run solercia-connect config validate and doctor.",),
            "configuration",
        ),
    )


def _missing_oauth_configuration(
    type_value: ConnectionType,
    settings: ConnectSettings,
) -> tuple[str, ...]:
    configured = {
        "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID": settings.microsoft_client_id,
        "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": settings.google_client_id,
    }
    return tuple(
        name
        for name in target_spec(type_value).required_configuration
        if name in configured and not configured[name]
    )


def _query(
    type_value: ConnectionType,
    capability: Capability,
    account: str | None,
    payload: dict[str, Any],
) -> QueryPayload:
    if capability is Capability.MAIL_CATEGORIES_SET:
        return _categories_update(account, payload)
    if capability is Capability.EXTERNAL_ISSUES_TRANSITIONS_LIST:
        # One issue, named by its exact key; a window, a limit or JQL do not apply.
        return IssueTransitionsQuery(_required_string(payload, "issue"), account)
    if capability is Capability.EXTERNAL_ISSUES_TRANSITION:
        return _issue_transition(account, payload)
    if capability is Capability.MAIL_DRAFT_CREATE:
        return _draft_request(account, payload)
    if capability is Capability.MAIL_DRAFT_SEND:
        return _draft_send(account, payload)
    if capability is Capability.SCM_REPOSITORIES_LIST:
        # The whole workspace listing, bounded by pages; a window or a limit does not apply.
        if payload.get("start") is not None or payload.get("end") is not None:
            raise ValueError("scm.repositories.list takes no --start/--end window")
        return ScmRepositoryQuery(_optional_string(payload.get("workspace")), account)
    if capability is Capability.SCM_PULLREQUESTS_LIST:
        return ScmPullRequestQuery(
            _required_string(payload, "repo"),
            _required_window(_window(payload.get("start"), payload.get("end"))),
            _pull_request_states(payload.get("state")),
            _optional_string(payload.get("workspace")),
            account,
        )
    if capability is Capability.SCM_COMMITS_LIST:
        return ScmCommitQuery(
            _required_string(payload, "repo"),
            _required_window(_window(payload.get("start"), payload.get("end"))),
            _optional_string(payload.get("branch")),
            _optional_string(payload.get("workspace")),
            account,
        )
    if capability is Capability.SCM_REPOSITORIES_CREATE:
        return _repository_create(account, payload)
    limit = payload.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    if capability is Capability.EXTERNAL_ISSUES_OPEN_LIST:
        return _open_issues(account, payload, limit)
    if capability in {
        Capability.SEARCH_ANALYTICS_QUERY,
        Capability.SEARCH_SITEMAPS_LIST,
        Capability.SEARCH_URLS_INSPECT,
    }:
        return _search_console_query(capability, account, payload)
    optional_window_capabilities = {
        Capability.MEETING_TRANSCRIPTS_LIST,
        Capability.MEETING_TRANSCRIPT_GET,
        Capability.FILES_SEARCH,
        Capability.FILES_GET,
    }
    window = (
        None
        if capability in optional_window_capabilities
        else _window(payload.get("start"), payload.get("end"))
    )
    if capability in optional_window_capabilities:
        window = _window(payload.get("start"), payload.get("end"), optional=True)
    if capability in {Capability.MAIL_SENT_LIST, Capability.MAIL_INBOX_LIST}:
        include_attachments = payload.get("include_attachments", False)
        if not isinstance(include_attachments, bool):
            raise ValueError("include_attachments must be a boolean")
        max_attachment_bytes = payload.get("max_attachment_bytes", 10_485_760)
        if (
            isinstance(max_attachment_bytes, bool)
            or not isinstance(max_attachment_bytes, int)
            or max_attachment_bytes <= 0
        ):
            raise ValueError("max_attachment_bytes must be a positive integer")
        return MailQuery(
            _required_window(window),
            account,
            min(limit, 1000),
            include_attachments,
            max_attachment_bytes,
        )
    if capability is Capability.CALENDAR_EVENTS_LIST:
        calendar_id = str(payload.get("calendar_id") or "primary")
        return CalendarQuery(
            _required_window(window),
            calendar_id,
            account,
            True,
            (),
            min(limit, 1000),
        )
    if capability in {
        Capability.MEETING_TRANSCRIPTS_LIST,
        Capability.MEETING_TRANSCRIPT_GET,
    }:
        # The two routes name the same transcript differently: the official one by online
        # meeting, the drive-backed one by the recording that carries it. A window alone is a
        # third selector, which is how a whole period is read without knowing ids in advance.
        meeting_id = _optional_string(payload.get("meeting_id"))
        drive_id = _optional_string(payload.get("drive_id"))
        item_id = _optional_string(payload.get("item_id"))
        if (drive_id is None) != (item_id is None):
            raise ValueError("drive_id and item_id must be given together")
        if meeting_id is None and drive_id is None and window is None:
            raise ValueError(
                "transcript capabilities need meeting_id, drive_id with item_id, or a window"
            )
        include_shared = payload.get("include_shared", True)
        if not isinstance(include_shared, bool):
            raise ValueError("include_shared must be a boolean")
        return TranscriptQuery(
            meeting_id,
            drive_id,
            item_id,
            window,
            account,
            include_shared,
            min(limit, 500),
        )
    if capability in {Capability.FILES_SEARCH, Capability.FILES_GET}:
        include_content = payload.get("include_content", capability is Capability.FILES_GET)
        if not isinstance(include_content, bool):
            raise ValueError("include_content must be a boolean")
        max_content_bytes = payload.get("max_content_bytes", 10_485_760)
        if (
            isinstance(max_content_bytes, bool)
            or not isinstance(max_content_bytes, int)
            or max_content_bytes <= 0
        ):
            raise ValueError("max_content_bytes must be a positive integer")
        item_id = _optional_string(payload.get("item_id"))
        if capability is Capability.FILES_GET and item_id is None:
            raise ValueError("item_id is required for files.get")
        return FileQuery(
            _optional_string(payload.get("search")),
            _optional_string(payload.get("folder_path")),
            item_id,
            _optional_string(payload.get("site")),
            window,
            account,
            include_content,
            max_content_bytes,
            min(limit, 500),
        )
    if capability is Capability.EXTERNAL_ISSUES_SEARCH:
        assignee = _optional_string(payload.get("assignee"))
        jql = _optional_string(payload.get("jql"))
        return IssueQuery(_required_window(window), assignee, jql, min(limit, 100))
    if type_value is not ConnectionType.GLPI:
        raise ValueError("Ticket search requires the GLPI target")
    return TicketQuery(_required_window(window), min(limit, 1000))


def _categories_update(account: str | None, payload: dict[str, Any]) -> MailCategoriesUpdate:
    """Build the category write; the domain type refuses an empty or ambiguous request."""
    allow_write = payload.get("allow_write", False)
    if not isinstance(allow_write, bool):
        raise ValueError("allow_write must be a boolean")
    raw_replace = payload.get("replace")
    return MailCategoriesUpdate(
        _required_string(payload, "message_id"),
        _category_names(payload.get("add"), "add"),
        _category_names(payload.get("remove"), "remove"),
        None if raw_replace is None else _category_names(raw_replace, "replace"),
        account,
        allow_write,
    )


def _open_issues(account: str | None, payload: dict[str, Any], limit: int) -> OpenIssueQuery:
    """Build the open-issue read; the domain type validates the project key and the JQL."""
    if payload.get("start") is not None or payload.get("end") is not None:
        raise ValueError("external.issues.open.list takes no date window")
    if payload.get("assignee") is not None:
        raise ValueError(
            "assignee does not apply to external.issues.open.list; it reads the connected account"
        )
    sprint = payload.get("sprint")
    if sprint is not None and sprint != "current":
        raise ValueError("sprint accepts only current")
    return OpenIssueQuery(
        payload.get("project"),
        sprint == "current",
        payload.get("jql"),
        account,
        min(limit, 100),
    )


def _issue_transition(account: str | None, payload: dict[str, Any]) -> IssueTransitionRequest:
    """Build the Jira transition; the domain type refuses anything but one exact issue key."""
    allow_write = payload.get("allow_write", False)
    if not isinstance(allow_write, bool):
        raise ValueError("allow_write must be a boolean")
    return IssueTransitionRequest(
        _required_string(payload, "issue"),
        _required_string(payload, "to"),
        account,
        allow_write,
    )


def _repository_create(account: str | None, payload: dict[str, Any]) -> ScmRepositoryCreateRequest:
    """Build the creation; the domain type refuses any slug or key it would have to change."""
    if payload.get("start") is not None or payload.get("end") is not None:
        raise ValueError("scm.repositories.create takes no --start/--end window")
    allow_write = payload.get("allow_write", False)
    if not isinstance(allow_write, bool):
        raise ValueError("allow_write must be a boolean")
    return ScmRepositoryCreateRequest(
        _required_string(payload, "repo"),
        _required_string(payload, "project"),
        _optional_string(payload.get("workspace")),
        account,
        allow_write,
    )


def _draft_request(account: str | None, payload: dict[str, Any]) -> MailDraftRequest:
    """Build the Gmail draft; the domain type validates recipients, subject and body."""
    allow_write = payload.get("allow_write", False)
    if not isinstance(allow_write, bool):
        raise ValueError("allow_write must be a boolean")
    body = payload.get("body")
    if not isinstance(body, str):
        raise ValueError("body is required")
    return MailDraftRequest(
        _address_list(payload.get("to"), "to"),
        _required_string(payload, "subject"),
        body,
        _address_list(payload.get("cc"), "cc"),
        _address_list(payload.get("bcc"), "bcc"),
        account,
        allow_write,
    )


def _draft_send(account: str | None, payload: dict[str, Any]) -> MailDraftSendRequest:
    """Build the send of one named draft; the domain type refuses anything but one draft id."""
    allow_write = payload.get("allow_write", False)
    if not isinstance(allow_write, bool):
        raise ValueError("allow_write must be a boolean")
    allow_duplicate = payload.get("allow_duplicate", False)
    if not isinstance(allow_duplicate, bool):
        raise ValueError("allow_duplicate must be a boolean")
    return MailDraftSendRequest(
        _required_string(payload, "draft_id"), account, allow_write, allow_duplicate
    )


def _pull_request_states(value: Any) -> tuple[str, ...]:
    """Read the repeatable state option; without it every documented state is listed."""
    if value is None:
        return PULL_REQUEST_STATES
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError("state must be a list of pull request states")
    return tuple(value)


def _address_list(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of email addresses")
    return tuple(value)


def _category_names(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of category names")
    return tuple(value)


def _search_console_query(
    capability: Capability,
    account: str | None,
    payload: dict[str, Any],
) -> SearchAnalyticsQuery | SearchSitemapsQuery | SearchUrlInspectionQuery:
    # Search Console takes calendar days, not instants, so this reads exact dates rather than the
    # timezone-aware window the other capabilities share.
    site_url = _required_string(payload, "site_url")
    if capability is Capability.SEARCH_SITEMAPS_LIST:
        return SearchSitemapsQuery(site_url, account)
    if capability is Capability.SEARCH_URLS_INSPECT:
        language = _optional_string(payload.get("language_code")) or "es-CO"
        return SearchUrlInspectionQuery(
            site_url,
            _required_string(payload, "inspection_url"),
            language,
            account,
        )
    raw_dimensions = payload.get("dimensions") or ["date"]
    if not isinstance(raw_dimensions, (list, tuple)) or not all(
        isinstance(item, str) for item in raw_dimensions
    ):
        raise ValueError("dimensions must be a list of names")
    row_limit = payload.get("row_limit", 25_000)
    if isinstance(row_limit, bool) or not isinstance(row_limit, int):
        raise ValueError("row_limit must be an integer")
    return SearchAnalyticsQuery(
        site_url,
        date.fromisoformat(_required_string(payload, "start_date")),
        date.fromisoformat(_required_string(payload, "end_date")),
        tuple(raw_dimensions),
        _optional_string(payload.get("search_type")) or "web",
        _optional_string(payload.get("data_state")) or "final",
        row_limit,
        account,
    )


def _window(start: Any, end: Any, *, optional: bool = False) -> DateWindow | None:
    if start is None and end is None and optional:
        return None
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("start and end are required timezone-aware ISO 8601 values")
    return DateWindow(datetime.fromisoformat(start), datetime.fromisoformat(end))


def _required_window(value: DateWindow | None) -> DateWindow:
    if value is None:
        raise ValueError("Capability requires an end-exclusive date window")
    return value


def _required_string(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Optional text values must be strings")
    return value.strip() or None


def _reason_error(
    reason: ReasonCode, message: str, *, remediation: tuple[str, ...] | None = None
) -> ErrorDetail:
    retryable = reason in {
        ReasonCode.NETWORK_TIMEOUT,
        ReasonCode.NETWORK_UNAVAILABLE,
        ReasonCode.PROVIDER_UNAVAILABLE,
        ReasonCode.RATE_LIMITED,
    }
    return ErrorDetail(
        reason.value,
        message,
        retryable,
        remediation=remediation or ("Run solercia-connect doctor for structured remediation.",),
        cause_category=("network" if retryable else "runtime"),
    )
