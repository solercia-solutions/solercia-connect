from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
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
from solercia_connect.browser_routes import BROWSER_PROVIDER_IDS
from solercia_connect.config import (
    PROJECT_DISCOVERY_VARIABLE,
    TARGET_SPECS,
    ConnectionType,
    JiraTokenKind,
    ProfileStore,
    effective_environment,
    load_settings,
    normalize_account,
    normalize_bitbucket_workspace,
    normalize_jira_site,
    normalize_jira_token_binding,
    project_selection,
)
from solercia_connect.connections import ConnectionService
from solercia_connect.doctor import DoctorService
from solercia_connect.domain import (
    MAX_DRAFT_BODY_BYTES,
    CalendarQuery,
    Capability,
    DateWindow,
    FileQuery,
    IssueQuery,
    IssueTransitionsQuery,
    MailQuery,
    QueryPayload,
    ReasonCode,
    TicketQuery,
    TranscriptQuery,
)
from solercia_connect.envelope import ErrorDetail, ResultEnvelope
from solercia_connect.errors import ConnectorFailure
from solercia_connect.matrix import CAPABILITY_MATRIX
from solercia_connect.output import SecureOutputError, write_secure_json
from solercia_connect.project_home import find_git_root, init_project_home
from solercia_connect.project_secrets import (
    api_token_store_from_settings,
    oauth_token_store_from_settings,
)
from solercia_connect.providers.m365_browser import browser_extraction_report
from solercia_connect.secrets import SecretValue
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
project_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode=None,
    help="Project mode: one private .solercia-connect folder per project.",
)
app.add_typer(m365_app, name="m365")
app.add_typer(config_app, name="config")
app.add_typer(profile_app, name="profile")
app.add_typer(project_app, name="project")


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

    if provider in BROWSER_PROVIDER_IDS and not allow_browser_fallback:
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
                    remediation=(f"solercia-connect health {provider} --allow-browser-fallback",),
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
    search: Annotated[
        str | None,
        typer.Option("--search", help="Provider-side file search text"),
    ] = None,
    folder_path: Annotated[
        str | None,
        typer.Option("--folder", help="Drive folder path to list, relative to the drive root"),
    ] = None,
    item_id: Annotated[
        str | None,
        typer.Option("--item-id", help="Drive item ID, required by files.get"),
    ] = None,
    drive_id: Annotated[
        str | None,
        typer.Option("--drive-id", help="Drive ID holding a recording; pair with --item-id"),
    ] = None,
    site: Annotated[
        str | None,
        typer.Option("--site", help="SharePoint site ID or host:/sites/name; omit for OneDrive"),
    ] = None,
    with_attachments: Annotated[
        bool,
        typer.Option("--with-attachments", help="Include message attachments with their content"),
    ] = False,
    max_attachment_bytes: Annotated[
        int,
        typer.Option("--max-attachment-bytes", min=1, help="Drop attachment content above this"),
    ] = 10_485_760,
    with_content: Annotated[
        bool | None,
        typer.Option("--with-content/--without-content", help="Include file content, base64"),
    ] = None,
    max_content_bytes: Annotated[
        int,
        typer.Option("--max-content-bytes", min=1, help="Reject files larger than this"),
    ] = 10_485_760,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 50,
    message_id: Annotated[
        str | None,
        typer.Option(
            "--message-id",
            help="Provider message id: the `id` field of mail.inbox.list or mail.sent.list records",
        ),
    ] = None,
    add: Annotated[
        list[str] | None,
        typer.Option("--add", help="Category to add; repeatable (mail.categories.set)"),
    ] = None,
    remove: Annotated[
        list[str] | None,
        typer.Option("--remove", help="Category to remove; repeatable (mail.categories.set)"),
    ] = None,
    replace: Annotated[
        list[str] | None,
        typer.Option(
            "--replace", help="Set the whole category list; repeatable, excludes the rest"
        ),
    ] = None,
    issue: Annotated[
        str | None,
        typer.Option("--issue", help="One exact Jira issue key, such as CP-1356"),
    ] = None,
    to_values: Annotated[
        list[str] | None,
        typer.Option(
            "--to",
            help="Target status name (external.issues.transition, exactly one) or a recipient "
            "(mail.draft.create, repeatable)",
        ),
    ] = None,
    cc: Annotated[
        list[str] | None,
        typer.Option("--cc", help="Copy recipient; repeatable (mail.draft.create)"),
    ] = None,
    bcc: Annotated[
        list[str] | None,
        typer.Option("--bcc", help="Blind-copy recipient; repeatable (mail.draft.create)"),
    ] = None,
    subject: Annotated[
        str | None,
        typer.Option("--subject", help="One-line subject (mail.draft.create)"),
    ] = None,
    body_file: Annotated[
        Path | None,
        typer.Option("--body-file", help="UTF-8 plain-text body, up to 200 KB (mail.draft.create)"),
    ] = None,
    body_stdin: Annotated[
        bool,
        typer.Option("--body-stdin", help="Read the plain-text body from piped stdin"),
    ] = False,
    draft_id: Annotated[
        str | None,
        typer.Option(
            "--draft-id", help="The draft to send, from mail.draft.create (mail.draft.send)"
        ),
    ] = None,
    allow_duplicate: Annotated[
        bool,
        typer.Option(
            "--allow-duplicate",
            help="Skip the duplicate check of mail.draft.send; only with the user's approval",
        ),
    ] = False,
    allow_write: Annotated[
        bool,
        typer.Option("--allow-write", help="Authorize one write for this invocation only"),
    ] = False,
    workspace: Annotated[
        str | None,
        typer.Option(
            "--workspace",
            help="Bitbucket workspace slug; defaults to the connected one (scm capabilities)",
        ),
    ] = None,
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo", help="Bitbucket repository slug (scm.pullrequests.list, scm.commits.list)"
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help="Bitbucket branch; defaults to the repository's main branch (scm.commits.list)",
        ),
    ] = None,
    state: Annotated[
        list[str] | None,
        typer.Option(
            "--state",
            help="Pull request state, repeatable: OPEN, MERGED, DECLINED or SUPERSEDED; all four "
            "by default (scm.pullrequests.list)",
        ),
    ] = None,
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
            "search": search,
            "folder_path": folder_path,
            "item_id": item_id,
            "drive_id": drive_id,
            "site": site,
            "max_content_bytes": max_content_bytes,
            "include_attachments": with_attachments,
            "max_attachment_bytes": max_attachment_bytes,
            "limit": limit,
            "message_id": message_id,
            "add": list(add) if add else None,
            "remove": list(remove) if remove else None,
            "replace": list(replace) if replace else None,
            "allow_write": allow_write,
        }
        if with_content is not None:
            payload["include_content"] = with_content
        # Issue-scoped options travel only when given, so other capabilities keep their payload.
        if issue is not None:
            payload["issue"] = issue
        if to_values is not None:
            payload["to"] = _to_option(capability, to_values)
        # Draft options travel only when given, and the body is read only for the draft.
        for name, value in (("cc", cc), ("bcc", bcc)):
            if value is not None:
                payload[name] = list(value)
        if subject is not None:
            payload["subject"] = subject
        if draft_id is not None:
            payload["draft_id"] = draft_id
        if allow_duplicate:
            payload["allow_duplicate"] = True
        # Bitbucket options travel only when given, so other capabilities keep their payload.
        if workspace is not None:
            payload["workspace"] = workspace
        if repo is not None:
            payload["repo"] = repo
        if branch is not None:
            payload["branch"] = branch
        if state is not None:
            payload["state"] = list(state)
        if capability is Capability.MAIL_DRAFT_CREATE:
            payload["body"] = _draft_body(body_file, from_stdin=body_stdin)
        elif body_file is not None or body_stdin:
            raise _InputError("--body-file and --body-stdin apply only to mail.draft.create")
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
    site: Annotated[
        str | None,
        typer.Option("--site", help="Jira site URL, such as https://your-site.atlassian.net"),
    ] = None,
    workspace: Annotated[
        str | None,
        typer.Option(
            "--workspace",
            help="Bitbucket workspace slug, as in bitbucket.org/<workspace>/<repository>",
        ),
    ] = None,
    token_stdin: Annotated[
        bool,
        typer.Option(
            "--token-stdin", help="Read the Jira or Bitbucket API token from one stdin line"
        ),
    ] = False,
    token_env: Annotated[
        str | None,
        typer.Option(
            "--token-env",
            metavar="NAME",
            help="Read the Jira or Bitbucket API token from variable NAME of the process "
            "environment, SOLERCIA_CONNECT_ENV_FILE or SOLERCIA_CONNECT_HOME/connect.env",
        ),
    ] = None,
    token_kind: Annotated[
        str | None,
        typer.Option(
            "--token-kind",
            help="Jira credential: classic API token (default) calls the site; scoped API "
            "token calls api.atlassian.com/ex/jira/{cloudId}; oauth authorizes the "
            "SOLERCIA_CONNECT_ATLASSIAN_* app in the browser and takes no token",
        ),
    ] = None,
    cloud_id: Annotated[
        str | None,
        typer.Option(
            "--cloud-id",
            help="Jira cloud ID (UUID) for a scoped token; an atlassian.net site may omit it",
        ),
    ] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Connect one provider target and account using official OAuth first.

    Jira takes --site and reads its API token from a hidden prompt, from one stdin line with
    --token-stdin, or from the variable named by --token-env; the token itself is never accepted
    as an argument and never printed. A scoped token needs --token-kind scoped, plus --cloud-id
    unless the site is on atlassian.net.
    --token-kind oauth uses Atlassian OAuth 2.0 (3LO) with the app configured in the environment
    and prompts for nothing.

    Bitbucket takes --workspace and reads an API token with Bitbucket scopes the same three ways;
    it is read-only and official-only.
    """

    try:
        service = connection_service_from_environment(action_sink=_interactive_action)
        parsed_type = ConnectionType.parse(type_value)
        jira = parsed_type is ConnectionType.JIRA
        bitbucket = parsed_type is ConnectionType.BITBUCKET
        if bitbucket:
            _refuse_jira_options_for_bitbucket(
                site, token_kind, cloud_id, allow_browser_fallback=allow_browser_fallback
            )
        elif workspace is not None:
            raise _InputError("--workspace applies only to bitbucket")
        jira_options = (site, token_kind, cloud_id, token_env)
        if not (jira or bitbucket) and (
            token_stdin or any(option is not None for option in jira_options)
        ):
            raise _InputError(
                "--site, --token-kind and --cloud-id apply only to jira; --token-stdin and "
                "--token-env apply only to jira and bitbucket"
            )
        if token_stdin and token_env is not None:
            raise _InputError("--token-env and --token-stdin are mutually exclusive")
        if bitbucket and workspace is not None:
            normalized_account = _valid_input(normalize_account, account)
            slug = _valid_input(normalize_bitbucket_workspace, workspace)
            envelope = service.connect_bitbucket(
                normalized_account,
                workspace=slug,
                token=(
                    _jira_token_from_environment(token_env)
                    if token_env is not None
                    else _jira_token(from_stdin=token_stdin, label="Bitbucket API token")
                ),
            )
        elif jira and site is not None:
            normalized_account = _valid_input(normalize_account, account)
            origin = _valid_input(normalize_jira_site, site)
            try:
                kind, scoped_cloud_id = normalize_jira_token_binding(origin, token_kind, cloud_id)
            except ValueError as failure:
                raise _InputError(str(failure)) from None
            if kind is JiraTokenKind.OAUTH:
                if token_stdin:
                    raise _InputError("--token-stdin applies only to API tokens, not oauth")
                if token_env is not None:
                    raise _InputError("--token-env applies only to API tokens, not oauth")
                envelope = service.connect_jira_oauth(
                    normalized_account, site=origin, open_browser=not no_open_browser
                )
            else:
                envelope = service.connect_jira(
                    normalized_account,
                    site=origin,
                    token=(
                        _jira_token_from_environment(token_env)
                        if token_env is not None
                        else _jira_token(from_stdin=token_stdin)
                    ),
                    token_kind=kind,
                    cloud_id=scoped_cloud_id,
                )
        else:
            envelope = service.connect(
                type_value,
                account,
                allow_browser_fallback=allow_browser_fallback,
                open_browser=not no_open_browser,
            )
    except _InputError as failure:
        envelope = _input_envelope("connect", str(failure), type_value, account)
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
                or name.startswith(("JIRA_", "BITBUCKET_", "GLPI_", "MS_GRAPH_", "GOOGLE_"))
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


@project_app.command("init")
def project_init(
    path: Annotated[
        Path | None,
        typer.Option(
            "--path",
            help="Absolute project folder; defaults to the git root of the current directory",
        ),
    ] = None,
) -> None:
    """Create the private .solercia-connect folder and a connect.env template without values.

    It never overwrites connect.env and works whether or not project discovery is on.
    """

    try:
        discovery, explicit_home = project_selection()
        root = _project_root(path)
        result = init_project_home(root)
        discoverable = find_git_root(root) == root
        envelope = ResultEnvelope.success(
            operation="project.init",
            status="initialized" if result.created else "ready",
            data={
                "home": str(result.home),
                "created": list(result.created),
                "discoverable": discoverable,
                "discovery_enabled": discovery,
                "next_steps": _project_next_steps(
                    result.home,
                    discoverable=discoverable,
                    discovery=discovery,
                    explicit_home=explicit_home,
                ),
            },
        )
    except _InputError as failure:
        envelope = _input_envelope("project.init", str(failure))
    except ValueError as failure:
        envelope = _exception_envelope("project.init", failure)
    except Exception:
        envelope = _unknown_envelope("project.init")
    _finish(envelope)


def _project_root(path: Path | None) -> Path:
    if path is None:
        root = find_git_root(Path.cwd())
        if root is None:
            raise _InputError(
                "The current directory is not inside a git repository; "
                "pass --path with the absolute project folder"
            )
        return root
    if not path.is_absolute() or not path.is_dir():
        raise _InputError("--path must be the absolute path of an existing directory")
    return Path(os.path.abspath(path))


def _project_next_steps(
    home: Path, *, discoverable: bool, discovery: bool, explicit_home: Path | None
) -> list[str]:
    """Activation first, only when still needed, then the setup commands as templates."""

    steps: list[str] = []
    if explicit_home == home:
        pass
    elif not discoverable:
        steps.append(f'export SOLERCIA_CONNECT_HOME="{home}"')
    else:
        if explicit_home is not None:
            steps.append("unset SOLERCIA_CONNECT_HOME")
        if not discovery:
            steps.append(f"export {PROJECT_DISCOVERY_VARIABLE}=1")
    return [
        *steps,
        f"Fill in {home / 'connect.env'}: uncomment only the variables you use",
        "solercia-connect connect gmail <account>",
        "solercia-connect connect jira <account> --site https://<site>.atlassian.net "
        "--token-env JIRA_API_TOKEN",
        "solercia-connect connect bitbucket <account> --workspace <workspace> "
        "--token-env BITBUCKET_API_TOKEN",
        "solercia-connect doctor <type> <account>",
    ]


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
        oauth_token_store_from_settings(settings),
        action_sink=action_sink,
        secret_store=api_token_store_from_settings(settings),
    )


class _InputError(ValueError):
    pass


_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _valid_input(normalize: Callable[[str], str], value: str) -> str:
    """Validate Jira input before the token prompt, so a typo never costs a token entry."""

    try:
        return normalize(value)
    except ValueError as failure:
        raise _InputError(str(failure)) from None


def _refuse_jira_options_for_bitbucket(
    site: str | None,
    token_kind: str | None,
    cloud_id: str | None,
    *,
    allow_browser_fallback: bool,
) -> None:
    """Name the first option that does not apply to a Bitbucket connect."""

    for option, value in (("--site", site), ("--token-kind", token_kind), ("--cloud-id", cloud_id)):
        if value is not None:
            raise _InputError(
                f"{option} does not apply to bitbucket; it connects with --workspace and an API "
                "token with Bitbucket scopes"
            )
    if allow_browser_fallback:
        raise _InputError(
            "--allow-browser-fallback does not apply to bitbucket; it connects only over the "
            "official API"
        )


def _jira_token_from_environment(name: str) -> SecretValue:
    """Read the API token from one variable of the effective environment; only its name shows."""

    if not _VARIABLE_NAME.fullmatch(name):
        raise _InputError("--token-env must name an environment variable, such as JIRA_API_TOKEN")
    token = effective_environment().get(name, "").strip()
    if not token:
        raise _InputError(f"The variable {name} named by --token-env is missing or empty")
    return SecretValue(token)


def _to_option(capability: Capability, values: list[str]) -> str | list[str]:
    """Read --to as one target status for the Jira transition and as recipients otherwise."""
    if capability is Capability.EXTERNAL_ISSUES_TRANSITION:
        if len(values) != 1:
            raise _InputError("--to takes exactly one target status for external.issues.transition")
        return values[0]
    return list(values)


def _draft_body(path: Path | None, *, from_stdin: bool) -> str:
    """Read the draft body from one file or from piped stdin, never from an argument."""
    if path is not None and from_stdin:
        raise _InputError("--body-file and --body-stdin are mutually exclusive")
    if path is None and not from_stdin:
        raise _InputError("mail.draft.create needs --body-file or --body-stdin")
    if path is not None:
        try:
            if not path.is_file():
                raise OSError("not a regular file")
            if path.stat().st_size > MAX_DRAFT_BODY_BYTES:
                raise _InputError(f"--body-file exceeds {MAX_DRAFT_BODY_BYTES // 1024} KB")
            data = path.read_bytes()
        except OSError:
            raise _InputError("--body-file could not be read") from None
        source = "--body-file"
    else:
        if sys.stdin.isatty():
            raise _InputError("--body-stdin needs piped input")
        stream = getattr(sys.stdin, "buffer", None)
        data = (
            stream.read(MAX_DRAFT_BODY_BYTES + 1)
            if stream is not None
            else sys.stdin.read(MAX_DRAFT_BODY_BYTES + 1).encode("utf-8")
        )
        source = "--body-stdin"
    if len(data) > MAX_DRAFT_BODY_BYTES:
        raise _InputError(f"{source} exceeds {MAX_DRAFT_BODY_BYTES // 1024} KB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _InputError(f"{source} must be UTF-8 text") from None


def _jira_token(*, from_stdin: bool, label: str = "Jira API token") -> SecretValue:
    """Read one API token from piped stdin or the hidden prompt; Bitbucket reuses it by label."""

    if from_stdin and sys.stdin.isatty():
        # A terminal would echo the token as it is typed; only piped input skips the prompt.
        raise _InputError("--token-stdin needs piped input; omit it to use the hidden prompt")
    try:
        raw = (
            sys.stdin.readline()
            if from_stdin
            else str(typer.prompt(label, hide_input=True, err=True))
        )
    except _click.exceptions.Abort:
        raise _InputError(f"{label} entry was cancelled") from None
    token = raw.strip()
    if not token:
        raise _InputError(f"The {label} is empty")
    return SecretValue(token)


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
    drive_id: str | None = None,
    item_id: str | None = None,
    issue: str | None = None,
    limit: int,
) -> QueryPayload:
    if capability is Capability.MAIL_CATEGORIES_SET:
        raise _InputError(
            "mail.categories.set is a write, not a query; run it with --message-id and "
            "--allow-write"
        )
    if capability is Capability.EXTERNAL_ISSUES_TRANSITION:
        raise _InputError(
            "external.issues.transition is a write, not a query; run it with --issue, --to and "
            "--allow-write"
        )
    if capability is Capability.MAIL_DRAFT_CREATE:
        raise _InputError(
            "mail.draft.create is a write, not a query; run it with --to, --subject, --body-file "
            "and --allow-write"
        )
    if capability is Capability.MAIL_DRAFT_SEND:
        raise _InputError(
            "mail.draft.send is a write, not a query; run it with --draft-id and --allow-write"
        )
    if capability is Capability.EXTERNAL_ISSUES_TRANSITIONS_LIST:
        if issue is None:
            raise _InputError("external.issues.transitions.list needs --issue with one Jira key")
        return IssueTransitionsQuery(issue, account)
    transcripts = {Capability.MEETING_TRANSCRIPTS_LIST, Capability.MEETING_TRANSCRIPT_GET}
    if capability in transcripts:
        # A transcript is selected by meeting, by recording, or by period, so a window is one
        # option among three rather than a requirement.
        window = _optional_window(start, end)
    elif capability in {Capability.FILES_SEARCH, Capability.FILES_GET}:
        window = None
    else:
        window = _window(start, end)
    if capability in {Capability.MAIL_SENT_LIST, Capability.MAIL_INBOX_LIST}:
        return MailQuery(_require_window(window), account, min(limit, 1000))
    if capability is Capability.CALENDAR_EVENTS_LIST:
        return CalendarQuery(
            _require_window(window),
            calendar_id,
            account,
            True,
            (),
            min(limit, 1000),
        )
    if capability in transcripts:
        if (drive_id is None) != (item_id is None):
            raise _InputError("--drive-id and --item-id must be given together")
        if not meeting_id and drive_id is None and window is None:
            raise _InputError(
                "transcript capabilities need one of: --meeting-id, "
                "--drive-id with --item-id, or --start with --end"
            )
        return TranscriptQuery(
            meeting_id,
            drive_id,
            item_id,
            window,
            account,
            True,
            min(limit, 500),
        )
    if capability in {Capability.FILES_SEARCH, Capability.FILES_GET}:
        return FileQuery(
            None,
            None,
            None,
            None,
            window,
            account,
            capability is Capability.FILES_GET,
            10_485_760,
            min(limit, 500),
        )
    if capability is Capability.EXTERNAL_ISSUES_SEARCH:
        return IssueQuery(_require_window(window), assignee, jql, min(limit, 100))
    if capability is Capability.TICKETS_SEARCH:
        return TicketQuery(_require_window(window), min(limit, 1000))
    # Every capability the enum can hold has an explicit branch above. Falling through here means
    # a new capability was declared without teaching this legacy test helper its query shape, so
    # it fails closed instead of silently handing back a query built for a different capability.
    raise _InputError(f"Unsupported capability: {capability}")


def _optional_window(start: str | None, end: str | None) -> DateWindow | None:
    """Parse a window that the capability treats as one selector among several."""
    if not start and not end:
        return None
    return _window(start, end)


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
    """Resolve the target from the declared specs, not from a hand-maintained mapping.

    Several targets share one provider ID, so the capability decides between them. Deriving this
    from TARGET_SPECS means a new capability is routable as soon as it is declared.
    """
    matches = [spec for spec in TARGET_SPECS if spec.provider_id == provider]
    if not matches:
        return None
    if capability is None:
        return matches[0].type
    for spec in matches:
        if capability.value in spec.capabilities:
            return spec.type
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
