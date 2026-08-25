from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from typer import _click
from typer.core import TyperGroup

from solercia_connect import __version__
from solercia_connect.application import run_application
from solercia_connect.bootstrap import (
    browser_adapter_from_environment,
    registry_from_environment,
)
from solercia_connect.config import (
    TARGET_SPECS,
    ConnectionType,
    ProfileStore,
    load_settings,
    normalize_account,
)
from solercia_connect.connections import ConnectionService
from solercia_connect.doctor import DoctorService
from solercia_connect.domain import (
    CalendarQuery,
    Capability,
    DateWindow,
    IssueQuery,
    MailQuery,
    QueryPayload,
    ReasonCode,
    TicketQuery,
    TranscriptQuery,
)
from solercia_connect.envelope import ErrorDetail, ResultEnvelope
from solercia_connect.errors import ConnectorFailure
from solercia_connect.matrix import CAPABILITY_MATRIX
from solercia_connect.oauth import KeyringOAuthTokenStore
from solercia_connect.output import SecureOutputError, write_secure_json
from solercia_connect.providers.m365_browser import browser_extraction_report
from solercia_connect.serialization import to_json, to_primitive


class JsonTyperGroup(TyperGroup):
    def invoke(self, ctx: _click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except _click.exceptions.UsageError as failure:
            _emit_usage_failure(failure)
            raise _click.exceptions.Exit(2) from None


app = typer.Typer(
    cls=JsonTyperGroup,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
    help="Deterministic, official-first provider connectors.",
)
m365_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode=None,
    help="Microsoft 365 browser profile compatibility operations.",
)
config_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None, help="Configuration checks.")
profile_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode=None,
    help="Non-secret local connection profiles.",
)
app.add_typer(m365_app, name="m365")
app.add_typer(config_app, name="config")
app.add_typer(profile_app, name="profile")


@app.command()
def version() -> None:
    """Print package version metadata."""

    _finish(
        ResultEnvelope.success(
            operation="version",
            data={"version": __version__},
        )
    )


@app.command()
def providers(
    output: Annotated[Path | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Print provider targets, capabilities, and configured adapters."""

    try:
        registry = registry_from_environment()
        data = {
            "configured": [descriptor.provider_id for descriptor in registry.descriptors()],
            "targets": [
                {
                    "type": spec.type.value,
                    "provider_id": spec.provider_id,
                    "capabilities": list(spec.capabilities),
                }
                for spec in TARGET_SPECS
            ],
            "matrix": [to_primitive(row) for row in CAPABILITY_MATRIX],
        }
        envelope = ResultEnvelope.success(operation="providers", data=data)
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope("providers", failure)
    except Exception:
        envelope = _unknown_envelope("providers")
    _finish(envelope, output=output, force=force)


@app.command()
def health(
    provider: str,
    account: Annotated[str | None, typer.Option("--account")] = None,
    allow_browser_fallback: Annotated[
        bool,
        typer.Option("--allow-browser-fallback"),
    ] = False,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Validate one configured adapter without printing credentials."""

    if provider == "microsoft365.browser" and not allow_browser_fallback:
        _finish(
            ResultEnvelope.failure(
                operation="health",
                status="action_required",
                provider=provider,
                route="browser",
                data={"interactive_flow_started": False},
                error=ErrorDetail(
                    "action_required",
                    "Browser health inspection requires explicit authorization",
                    True,
                    remediation=(
                        "solercia-connect health microsoft365.browser --allow-browser-fallback",
                    ),
                    cause_category="interactive_authorization",
                ),
            ),
            output=output,
            force=force,
        )
        return
    try:
        target = _target_for_provider(provider, None)
        normalized = normalize_account(account) if account else None
        registry = registry_from_environment(
            connection_type=target,
            account=normalized,
            include_browser=allow_browser_fallback,
        )
        report = registry.find(provider).health()
        envelope = ResultEnvelope.success(
            operation="health",
            status="ready",
            provider=provider,
            account=report.account_id,
            route=registry.find(provider).descriptor.route.value,
            data=report,
        )
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope("health", failure, provider=provider, account=account)
    except Exception:
        envelope = _unknown_envelope("health", provider=provider, account=account)
    _finish(envelope, output=output, force=force)


@app.command(name="run")
def run_capability(
    provider: Annotated[str, typer.Option("--provider")],
    capability_value: Annotated[str, typer.Option("--capability")],
    start: Annotated[str | None, typer.Option(help="Timezone-aware ISO 8601 start")] = None,
    end: Annotated[
        str | None,
        typer.Option(help="Timezone-aware ISO 8601 end (exclusive)"),
    ] = None,
    account: Annotated[str | None, typer.Option("--account")] = None,
    calendar_id: Annotated[str, typer.Option("--calendar-id")] = "primary",
    assignee: Annotated[str | None, typer.Option("--assignee")] = None,
    jql: Annotated[str | None, typer.Option("--jql")] = None,
    meeting_id: Annotated[str | None, typer.Option("--meeting-id")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 50,
    browser_fallback: Annotated[
        bool,
        typer.Option("--allow-browser-fallback", "--browser-fallback"),
    ] = False,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Execute one typed capability and print normalized records and a receipt."""

    normalized_account: str | None = None
    try:
        try:
            capability = Capability(capability_value)
        except ValueError as exc:
            raise _InputError(
                "Invalid capability; expected one of: "
                + ", ".join(item.value for item in Capability)
            ) from exc
        target = _target_for_provider(provider, capability)
        if target is None:
            raise _InputError("Unsupported provider ID")
        payload: dict[str, Any] = {
            "type": target.value,
            "account": account,
            "capability": capability.value,
            "start": start,
            "end": end,
            "calendar_id": calendar_id,
            "assignee": assignee,
            "jql": jql,
            "meeting_id": meeting_id,
            "limit": limit,
        }
        payload["allow_browser_fallback"] = browser_fallback
        envelope = run_application(payload)
        normalized_account = envelope.account
    except _InputError as failure:
        envelope = _input_envelope("run", str(failure), provider, normalized_account)
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope(
            "run", failure, provider=provider, account=normalized_account
        )
    except Exception:
        envelope = _unknown_envelope("run", provider=provider, account=normalized_account)
    _finish(envelope, output=output, force=force)


@app.command()
def connect(
    type_value: Annotated[str, typer.Argument(metavar="TYPE")],
    account: Annotated[str, typer.Argument(metavar="ACCOUNT")],
    allow_browser_fallback: Annotated[
        bool,
        typer.Option("--allow-browser-fallback"),
    ] = False,
    no_open_browser: Annotated[bool, typer.Option("--no-open-browser")] = False,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Connect one provider target and account using official OAuth first."""

    try:
        service = connection_service_from_environment(action_sink=_interactive_action)
        envelope = service.connect(
            type_value,
            account,
            allow_browser_fallback=allow_browser_fallback,
            open_browser=not no_open_browser,
        )
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope("connect", failure, provider=type_value, account=account)
    except Exception:
        envelope = _unknown_envelope("connect", provider=type_value, account=account)
    _finish(envelope, output=output, force=force)


@app.command()
def status(
    type_value: Annotated[str, typer.Argument(metavar="TYPE")],
    account: Annotated[str, typer.Argument(metavar="ACCOUNT")],
    allow_browser_fallback: Annotated[
        bool,
        typer.Option("--allow-browser-fallback"),
    ] = False,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Check local token/profile state and the observed provider identity."""

    try:
        envelope = connection_service_from_environment().status(
            type_value,
            account,
            allow_browser_fallback=allow_browser_fallback,
        )
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope("status", failure, provider=type_value, account=account)
    except Exception:
        envelope = _unknown_envelope("status", provider=type_value, account=account)
    _finish(envelope, output=output, force=force)


@app.command()
def disconnect(
    type_value: Annotated[str, typer.Argument(metavar="TYPE")],
    account: Annotated[str, typer.Argument(metavar="ACCOUNT")],
    output: Annotated[Path | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Remove local profile/token state without claiming remote revocation."""

    try:
        envelope = connection_service_from_environment().disconnect(type_value, account)
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope("disconnect", failure, provider=type_value, account=account)
    except Exception:
        envelope = _unknown_envelope("disconnect", provider=type_value, account=account)
    _finish(envelope, output=output, force=force)


@app.command()
def doctor(
    type_value: Annotated[str | None, typer.Argument(metavar="TYPE")] = None,
    account: Annotated[str | None, typer.Argument(metavar="ACCOUNT")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Inspect configuration and readiness without printing or testing secrets."""

    try:
        settings = load_settings()
        service = DoctorService(settings, ProfileStore.from_settings(settings))
        if type_value is None and account is not None:
            raise _InputError("ACCOUNT requires TYPE")
        reports = (
            [service.inspect(type_value, account)]
            if type_value is not None
            else [service.inspect(spec.type) for spec in TARGET_SPECS]
        )
        ready = all(report.ready for report in reports)
        missing = tuple(
            dict.fromkeys(name for report in reports for name in report.missing_configuration)
        )
        data: Any = (
            reports[0].to_dict()
            if len(reports) == 1
            else {"reports": [report.to_dict() for report in reports]}
        )
        if ready:
            envelope = ResultEnvelope.success(
                operation="doctor",
                status="ready",
                provider=reports[0].type.value if len(reports) == 1 else None,
                account=reports[0].account if len(reports) == 1 else None,
                data=data,
            )
        else:
            envelope = ResultEnvelope.failure(
                operation="doctor",
                status="not_ready",
                provider=reports[0].type.value if len(reports) == 1 else None,
                account=reports[0].account if len(reports) == 1 else None,
                data=data,
                error=ErrorDetail(
                    "not_ready",
                    "One or more required readiness checks failed",
                    False,
                    missing,
                    ("Apply the remediation steps in the doctor checks.",),
                    "configuration",
                ),
            )
    except _InputError as failure:
        envelope = _input_envelope("doctor", str(failure), type_value, account)
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope("doctor", failure, provider=type_value, account=account)
    except Exception:
        envelope = _unknown_envelope("doctor", provider=type_value, account=account)
    _finish(envelope, output=output, force=force)


@config_app.command("validate")
def config_validate(
    output: Annotated[Path | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Validate explicit settings syntax and report configured names only."""

    try:
        settings = load_settings()
        configured = set(settings.configured_names)
        data = {
            "env_file": str(settings.env_file) if settings.env_file else None,
            "configured_names": sorted(
                name
                for name in configured
                if name.startswith("SOLERCIA_CONNECT_")
                or name.startswith(("JIRA_", "GLPI_", "MS_GRAPH_", "GOOGLE_"))
            ),
            "targets": [
                {
                    "type": spec.type.value,
                    "missing_configuration": [
                        name for name in spec.required_configuration if name not in configured
                    ],
                }
                for spec in TARGET_SPECS
            ],
            "api": {"host": settings.api_host, "port": settings.api_port},
        }
        envelope = ResultEnvelope.success(
            operation="config.validate",
            status="valid",
            data=data,
        )
    except ValueError as failure:
        envelope = _exception_envelope("config.validate", failure)
    except Exception:
        envelope = _unknown_envelope("config.validate")
    _finish(envelope, output=output, force=force)


@profile_app.command("list")
def profile_list(
    type_value: Annotated[str | None, typer.Option("--type")] = None,
) -> None:
    """List non-secret local profiles."""

    try:
        settings = load_settings()
        profiles = ProfileStore.from_settings(settings).list(type_value)
        envelope = ResultEnvelope.success(
            operation="profile.list",
            data={"profiles": [profile.to_dict() for profile in profiles]},
        )
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope("profile.list", failure, provider=type_value)
    except Exception:
        envelope = _unknown_envelope("profile.list", provider=type_value)
    _finish(envelope)


@profile_app.command("show")
def profile_show(type_value: str, account: str) -> None:
    """Show one non-secret local profile."""

    try:
        settings = load_settings()
        profile = ProfileStore.from_settings(settings).get(type_value, account)
        if profile is None:
            envelope = ResultEnvelope.failure(
                operation="profile.show",
                status="not_found",
                provider=type_value,
                account=account,
                error=ErrorDetail(
                    "profile_not_found",
                    "Connection profile does not exist",
                    False,
                    remediation=("Run the canonical connect command.",),
                    cause_category="configuration",
                ),
            )
        else:
            envelope = ResultEnvelope.success(
                operation="profile.show",
                provider=profile.type.value,
                account=profile.account,
                route=profile.route,
                data=profile.to_dict(),
            )
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope(
            "profile.show", failure, provider=type_value, account=account
        )
    except Exception:
        envelope = _unknown_envelope("profile.show", provider=type_value, account=account)
    _finish(envelope)


@profile_app.command("delete")
def profile_delete(type_value: str, account: str) -> None:
    """Delete one local profile without revoking provider-side authorization."""

    try:
        settings = load_settings()
        normalized = normalize_account(account)
        removed = ProfileStore.from_settings(settings).delete(type_value, normalized)
        envelope = ResultEnvelope.success(
            operation="profile.delete",
            status="deleted" if removed else "not_found",
            provider=ConnectionType.parse(type_value).value,
            account=normalized,
            data={"local_profile_removed": removed, "remote_revocation": "not_performed"},
        )
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope(
            "profile.delete", failure, provider=type_value, account=account
        )
    except Exception:
        envelope = _unknown_envelope("profile.delete", provider=type_value, account=account)
    _finish(envelope)


@m365_app.command("login")
def m365_login(
    allow_browser_fallback: Annotated[
        bool,
        typer.Option("--allow-browser-fallback"),
    ] = False,
) -> None:
    """Compatibility alias for opening the dedicated Microsoft profile."""

    if not allow_browser_fallback:
        _finish(_m365_browser_action("login"))
        return
    try:
        report = browser_adapter_from_environment(
            connection_type=ConnectionType.TEAMS
        ).interactive_login()
        envelope = ResultEnvelope.success(
            operation="m365.login",
            status="connected_browser",
            provider="microsoft365.browser",
            account=report.account_id,
            route="browser",
            data=report,
        )
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope("m365.login", failure, provider="microsoft365.browser")
    except Exception:
        envelope = _unknown_envelope("m365.login", provider="microsoft365.browser")
    _finish(envelope)


@m365_app.command("status")
def m365_status(
    allow_browser_fallback: Annotated[
        bool,
        typer.Option("--allow-browser-fallback"),
    ] = False,
) -> None:
    """Compatibility alias for checking the dedicated Microsoft profile."""

    if not allow_browser_fallback:
        _finish(_m365_browser_action("status"))
        return
    try:
        report = browser_adapter_from_environment(connection_type=ConnectionType.TEAMS).health()
        envelope = ResultEnvelope.success(
            operation="m365.status",
            status="connected_browser",
            provider="microsoft365.browser",
            account=report.account_id,
            route="browser",
            data={"health": report, **browser_extraction_report(ConnectionType.TEAMS)},
        )
    except (ValueError, ConnectorFailure) as failure:
        envelope = _exception_envelope("m365.status", failure, provider="microsoft365.browser")
    except Exception:
        envelope = _unknown_envelope("m365.status", provider="microsoft365.browser")
    _finish(envelope)


def _m365_browser_action(operation: str) -> ResultEnvelope:
    return ResultEnvelope.failure(
        operation=f"m365.{operation}",
        status="action_required",
        provider="microsoft365.browser",
        route="browser",
        data={"interactive_flow_started": False},
        error=ErrorDetail(
            "action_required",
            "Microsoft browser compatibility operations require explicit authorization",
            True,
            remediation=(f"solercia-connect m365 {operation} --allow-browser-fallback",),
            cause_category="interactive_authorization",
        ),
    )


def connection_service_from_environment(
    *,
    action_sink: Any = None,
) -> ConnectionService:
    settings = load_settings()
    return ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        KeyringOAuthTokenStore(),
        action_sink=action_sink,
    )


class _InputError(ValueError):
    pass


def _query_payload(
    capability: Capability,
    *,
    start: str | None,
    end: str | None,
    account: str | None,
    calendar_id: str,
    assignee: str | None,
    jql: str | None,
    meeting_id: str | None,
    limit: int,
) -> QueryPayload:
    window = _window(start, end) if capability is not Capability.MEETING_TRANSCRIPT_GET else None
    if capability is Capability.MAIL_SENT_LIST:
        return MailQuery(_require_window(window), account, min(limit, 100))
    if capability is Capability.CALENDAR_EVENTS_LIST:
        return CalendarQuery(
            _require_window(window),
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
        if not meeting_id:
            raise _InputError("--meeting-id is required for Teams transcript capabilities")
        return TranscriptQuery(meeting_id, window, account, min(limit, 50))
    if capability is Capability.EXTERNAL_ISSUES_SEARCH:
        return IssueQuery(_require_window(window), assignee, jql, min(limit, 100))
    return TicketQuery(_require_window(window), min(limit, 1000))


def _window(start: str | None, end: str | None) -> DateWindow:
    if not start or not end:
        raise _InputError("start and end are required for this capability")
    try:
        return DateWindow(datetime.fromisoformat(start), datetime.fromisoformat(end))
    except ValueError as exc:
        raise _InputError(f"Invalid end-exclusive ISO 8601 window: {exc}") from exc


def _require_window(window: DateWindow | None) -> DateWindow:
    if window is None:
        raise _InputError("Capability requires a date window")
    return window


def _target_for_provider(
    provider: str,
    capability: Capability | None,
) -> ConnectionType | None:
    if provider == "microsoft.graph":
        return (
            ConnectionType.TEAMS
            if capability
            in {Capability.MEETING_TRANSCRIPTS_LIST, Capability.MEETING_TRANSCRIPT_GET}
            else ConnectionType.OUTLOOK
        )
    if provider == "google.gmail":
        return ConnectionType.GMAIL
    if provider == "google.calendar":
        return ConnectionType.GOOGLE_CALENDAR
    if provider == "jira.cloud":
        return ConnectionType.JIRA
    if provider == "glpi.rest":
        return ConnectionType.GLPI
    return None


def _interactive_action(envelope: ResultEnvelope) -> None:
    typer.echo(to_json(envelope.to_dict()), err=True)


def _finish(
    envelope: ResultEnvelope,
    *,
    output: Path | None = None,
    force: bool = False,
) -> None:
    if output is not None:
        try:
            write_secure_json(output, envelope, overwrite=force)
        except SecureOutputError as failure:
            envelope = ResultEnvelope.failure(
                operation=envelope.operation,
                status="output_error",
                provider=envelope.provider,
                account=envelope.account,
                error=ErrorDetail(
                    "output_error",
                    str(failure),
                    False,
                    remediation=("Choose a new path or pass --force explicitly.",),
                    cause_category="output",
                ),
            )
    typer.echo(to_json(envelope.to_dict()))
    if not envelope.ok:
        raise typer.Exit(2 if envelope.status == "invalid_input" else 1)


def _input_envelope(
    operation: str,
    message: str,
    provider: str | None = None,
    account: str | None = None,
) -> ResultEnvelope:
    return ResultEnvelope.failure(
        operation=operation,
        status="invalid_input",
        provider=provider,
        account=account,
        error=ErrorDetail(
            "invalid_input",
            message,
            False,
            remediation=("Run the command with --help for the accepted input contract.",),
            cause_category="input",
        ),
    )


def _exception_envelope(
    operation: str,
    failure: Exception,
    *,
    provider: str | None = None,
    account: str | None = None,
) -> ResultEnvelope:
    if isinstance(failure, ConnectorFailure):
        if failure.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH and operation in {
            "m365.login",
            "m365.status",
        }:
            error = ErrorDetail(
                failure.reason.value,
                failure.safe_message,
                True,
                remediation=(
                    "solercia-connect m365 login --allow-browser-fallback",
                    "solercia-connect m365 status --allow-browser-fallback",
                ),
                cause_category="runtime",
            )
        else:
            error = _reason_error(failure.reason, failure.safe_message)
    else:
        error = ErrorDetail(
            "configuration_invalid",
            str(failure),
            False,
            remediation=("Run solercia-connect config validate and doctor.",),
            cause_category="configuration",
        )
    return ResultEnvelope.failure(
        operation=operation,
        status="failed",
        provider=provider,
        account=account,
        error=error,
    )


def _unknown_envelope(
    operation: str,
    *,
    provider: str | None = None,
    account: str | None = None,
) -> ResultEnvelope:
    return ResultEnvelope.failure(
        operation=operation,
        status="failed",
        provider=provider,
        account=account,
        error=ErrorDetail(
            "unknown_error",
            "Operation failed with an unknown error",
            False,
            remediation=("Run doctor and retry with sanitized diagnostics.",),
            cause_category="unknown",
        ),
    )


def _reason_error(reason: ReasonCode, message: str) -> ErrorDetail:
    retryable = reason in {
        ReasonCode.NETWORK_TIMEOUT,
        ReasonCode.NETWORK_UNAVAILABLE,
        ReasonCode.PROVIDER_UNAVAILABLE,
        ReasonCode.RATE_LIMITED,
    }
    category = (
        "configuration"
        if reason in {ReasonCode.CONFIGURATION_INVALID, ReasonCode.CAPABILITY_NOT_IMPLEMENTED}
        else "authorization"
        if reason
        in {
            ReasonCode.INVALID_CREDENTIALS,
            ReasonCode.AUTHORIZATION_DENIED,
            ReasonCode.ADMIN_CONSENT_REQUIRED,
            ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
            ReasonCode.TENANT_POLICY_BLOCKED,
        }
        else "network"
        if retryable
        else "runtime"
    )
    return ErrorDetail(
        reason.value,
        message,
        retryable,
        remediation=("Run solercia-connect doctor for structured remediation.",),
        cause_category=category,
    )


def _emit_usage_failure(failure: _click.exceptions.UsageError) -> None:
    envelope = _input_envelope("cli", failure.format_message())
    _click.echo(to_json(envelope.to_dict()))
