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
from solercia_connect.connections import connect_command
from solercia_connect.domain import (
    CalendarQuery,
    Capability,
    ConnectorRequest,
    DateWindow,
    FileQuery,
    IssueQuery,
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
) -> ResultEnvelope:
    """Execute a validated connector request for CLI or HTTP adapters."""

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
        query = _query(type_value, capability, account, payload)
        fallback = payload.get("allow_browser_fallback", False)
        if not isinstance(fallback, bool):
            raise ValueError("allow_browser_fallback must be a boolean")
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

    if not interactive and fallback:
        assert account is not None
        return _interactive_action(type_value, account, browser_fallback=True)

    active_settings = settings
    if not interactive and type_value in _OAUTH_TYPES:
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
            include_browser=interactive and fallback,
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
            return _interactive_action(type_value, account, receipt=outcome.receipt)
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
            return _interactive_action(type_value, account)
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
) -> ResultEnvelope:
    return ResultEnvelope.failure(
        operation="run",
        status="action_required",
        provider=type_value.value,
        account=account,
        receipt=receipt,
        data={"interactive_flow_started": False},
        error=ErrorDetail(
            "action_required",
            "Interactive authorization must run in the canonical CLI",
            True,
            remediation=(
                connect_command(
                    type_value,
                    account,
                    allow_browser_fallback=browser_fallback,
                ),
            ),
            cause_category="interactive_authorization",
        ),
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
    limit = payload.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    optional_window_capabilities = {
        Capability.MEETING_TRANSCRIPT_GET,
        Capability.FILES_SEARCH,
        Capability.FILES_GET,
    }
    window = (
        None
        if capability in optional_window_capabilities
        else _window(payload.get("start"), payload.get("end"))
    )
    if capability in {Capability.FILES_SEARCH, Capability.FILES_GET}:
        window = _window(payload.get("start"), payload.get("end"), optional=True)
    if capability is Capability.MAIL_SENT_LIST:
        return MailQuery(_required_window(window), account, min(limit, 100))
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
        meeting_id = _required_string(payload, "meeting_id")
        return TranscriptQuery(meeting_id, window, account, min(limit, 50))
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
