from __future__ import annotations

from datetime import datetime
from typing import Any

from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.config import (
    ConnectionType,
    ConnectSettings,
    ProfileStore,
    load_settings,
    normalize_account,
    target_spec,
)
from solercia_connect.connections import connect_command, http_connect_hint
from solercia_connect.domain import (
    WRITE_CAPABILITIES,
    CalendarQuery,
    Capability,
    ConnectorRequest,
    DateWindow,
    FileQuery,
    IssueQuery,
    MailCategoriesUpdate,
    MailQuery,
    QueryPayload,
    ReasonCode,
    TicketQuery,
    TranscriptQuery,
)
from solercia_connect.envelope import ErrorDetail, ResultEnvelope, versioned_record
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import KeyringOAuthTokenStore, OAuthTokenStore
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
        if not writes and (
            payload.get("allow_write", False) is not False
            or any(payload.get(name) is not None for name in ("add", "remove", "replace"))
        ):
            raise ValueError(
                "allow_write, add, remove and replace apply only to mail.categories.set"
            )
        if writes and account is None:
            raise ValueError("account is required for mail.categories.set")
        query = _query(type_value, capability, account, payload)
        fallback = payload.get("allow_browser_fallback", False)
        if not isinstance(fallback, bool):
            raise ValueError("allow_browser_fallback must be a boolean")
        if fallback and type_value is ConnectionType.GOOGLE_MEET:
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

    if isinstance(query, MailCategoriesUpdate):
        assert account is not None
        if not interactive:
            return http_write_rejection(type_value, account)
        if not query.write_authorized:
            return _write_not_authorized(type_value, account)

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
            tokens = (token_store or KeyringOAuthTokenStore()).load(f"{type_value.value}:{account}")
        except ConnectorFailure as failure:
            return _configuration_failure(type_value, account, failure.safe_message)
        if tokens is None or not set(target_spec(type_value).oauth_scopes).issubset(tokens.scopes):
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
        detail = outcome.receipt.steps[-1].detail if outcome.receipt.steps else "Run failed"
        return ResultEnvelope.failure(
            operation="run",
            status="failed",
            provider=type_value.value,
            account=account,
            receipt=outcome.receipt,
            error=_reason_error(outcome.receipt.final_reason, detail),
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


_OAUTH_TYPES = frozenset(
    {
        ConnectionType.OUTLOOK,
        ConnectionType.TEAMS,
        ConnectionType.ONEDRIVE,
        ConnectionType.GMAIL,
        ConnectionType.GOOGLE_CALENDAR,
        ConnectionType.GOOGLE_MEET,
    }
)
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


def http_write_rejection(type_value: ConnectionType, account: str | None) -> ResultEnvelope:
    """Refuse a write for a caller without a terminal; the write exists only in the CLI."""
    return ResultEnvelope.failure(
        operation="run",
        status="forbidden",
        provider=type_value.value,
        account=account,
        error=ErrorDetail(
            "write_not_available_over_http",
            "mail.categories.set changes the mailbox and runs only in the canonical CLI",
            False,
            remediation=(_write_command(type_value, account),),
            cause_category="policy",
        ),
    )


def _write_not_authorized(type_value: ConnectionType, account: str) -> ResultEnvelope:
    return ResultEnvelope.failure(
        operation="run",
        status="invalid_input",
        provider=type_value.value,
        account=account,
        error=ErrorDetail(
            "write_not_authorized",
            "mail.categories.set changes the mailbox and needs --allow-write on this invocation; "
            "nothing was sent to the provider",
            False,
            remediation=(_write_command(type_value, account),),
            cause_category="input",
        ),
    )


def _write_command(type_value: ConnectionType, account: str | None) -> str:
    return (
        f"solercia-connect run --provider {target_spec(type_value).provider_id} "
        f"--capability mail.categories.set --account {account or '<account>'} "
        "--message-id <id> --add <category> --allow-write --allow-browser-fallback"
    )


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
    limit = payload.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
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


def _category_names(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of category names")
    return tuple(value)


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


def _reason_error(reason: ReasonCode, message: str) -> ErrorDetail:
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
        remediation=("Run solercia-connect doctor for structured remediation.",),
        cause_category=("network" if retryable else "runtime"),
    )
