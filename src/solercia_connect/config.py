from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from solercia_connect.domain import EMAIL_ADDRESS_PATTERN

# The workspace slug rule lives in the domain, next to the queries it validates; it is importable
# from here for the CLI and the connection service, like normalize_jira_site.
from solercia_connect.domain import normalize_bitbucket_workspace as normalize_bitbucket_workspace

# normalize_jira_site moved next to the Jira adapter, which matches accessible resources with it;
# it stays importable from here for the CLI, the connection service and existing callers.
from solercia_connect.providers.jira import (
    jira_site_publishes_tenant_info,
    normalize_jira_cloud_id,
)
from solercia_connect.providers.jira import normalize_jira_site as normalize_jira_site
from solercia_connect.secrets import SecretValue

PROFILE_SCHEMA = "solercia.connect/profile/v1"
# Atlassian requires the redirect_uri to match the callback URL registered for the app, so the
# loopback listener takes this fixed port instead of a random one.
DEFAULT_ATLASSIAN_REDIRECT_URI = "http://127.0.0.1:8766/oauth/callback"
ATLASSIAN_OAUTH_CONFIGURATION = (
    "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID",
    "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET",
)
# Opt-in switch (D3.1): find <git root>/.solercia-connect when SOLERCIA_CONNECT_HOME is unset.
PROJECT_DISCOVERY_VARIABLE = "SOLERCIA_CONNECT_PROJECT_DISCOVERY"
ProjectHomeSource = Literal["explicit", "discovered"]

_ACCOUNT_PATTERN = EMAIL_ADDRESS_PATTERN
# The only write scope Connect requests, held by the gmail-compose target alone (decision D5).
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


class ConnectionType(StrEnum):
    OUTLOOK = "outlook"
    TEAMS = "teams"
    ONEDRIVE = "onedrive"
    GMAIL = "gmail"
    GMAIL_COMPOSE = "gmail-compose"
    GOOGLE_CALENDAR = "google-calendar"
    GOOGLE_MEET = "google-meet"
    JIRA = "jira"
    GLPI = "glpi"
    BITBUCKET = "bitbucket"

    @classmethod
    def parse(cls, value: str | ConnectionType) -> ConnectionType:
        if isinstance(value, cls):
            return value
        candidate = value.strip().casefold().replace("_", "-")
        aliases = {
            "microsoft-outlook": cls.OUTLOOK,
            "microsoft-teams": cls.TEAMS,
            "microsoft-onedrive": cls.ONEDRIVE,
            "sharepoint": cls.ONEDRIVE,
            "microsoft-sharepoint": cls.ONEDRIVE,
            "google-calendar": cls.GOOGLE_CALENDAR,
            "calendar": cls.GOOGLE_CALENDAR,
            "meet": cls.GOOGLE_MEET,
        }
        try:
            return aliases[candidate] if candidate in aliases else cls(candidate)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(f"Unsupported provider type; expected one of: {allowed}") from exc


@dataclass(frozen=True, slots=True)
class TargetSpec:
    type: ConnectionType
    provider_id: str
    capabilities: tuple[str, ...]
    required_configuration: tuple[str, ...]
    oauth_scopes: tuple[str, ...] = ()
    external_prerequisites: tuple[str, ...] = ()


# Every target served by the Microsoft identity stack. Deciding this inline at each call site
# meant a new Microsoft target silently fell into another provider's branch.
MICROSOFT_TARGETS: frozenset[ConnectionType] = frozenset(
    {
        ConnectionType.OUTLOOK,
        ConnectionType.TEAMS,
        ConnectionType.ONEDRIVE,
    }
)
# Every target served by Google OAuth, the writing gmail-compose included.
GOOGLE_TARGETS: frozenset[ConnectionType] = frozenset(
    {
        ConnectionType.GMAIL,
        ConnectionType.GMAIL_COMPOSE,
        ConnectionType.GOOGLE_CALENDAR,
        ConnectionType.GOOGLE_MEET,
    }
)


TARGET_SPECS: tuple[TargetSpec, ...] = (
    TargetSpec(
        ConnectionType.OUTLOOK,
        "microsoft.graph",
        # mail.categories.set is served by the browser route only: its official route needs
        # Mail.ReadWrite, and adding it here would invalidate every stored Outlook grant.
        ("mail.sent.list", "mail.inbox.list", "calendar.events.list", "mail.categories.set"),
        ("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID",),
        ("openid", "profile", "User.Read", "Mail.Read", "Calendars.Read"),
        ("Microsoft work or school account", "Tenant consent for delegated read scopes"),
    ),
    TargetSpec(
        ConnectionType.TEAMS,
        "microsoft.graph",
        ("meeting.transcripts.list", "meeting.transcript.get"),
        ("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID",),
        ("openid", "profile", "User.Read", "OnlineMeetingTranscript.Read.All"),
        (
            "Microsoft work or school account (personal accounts are unsupported)",
            "Tenant admin must allow Graph transcript access and grant the delegated scope",
            "A known online meeting ID is required by the official route",
            "The supervised browser route instead reads the recording that carries the "
            "transcript, selected by drive item or by period",
        ),
    ),
    TargetSpec(
        ConnectionType.ONEDRIVE,
        "microsoft.graph",
        ("files.search", "files.get"),
        ("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID",),
        ("openid", "profile", "User.Read", "Files.Read.All", "Sites.Read.All"),
        (
            "Microsoft work or school account",
            "Tenant consent for delegated file and site read scopes",
        ),
    ),
    TargetSpec(
        ConnectionType.GMAIL,
        "google.gmail",
        # Read-only: the inbox is served by the official route alone, never by the browser.
        ("mail.sent.list", "mail.inbox.list"),
        ("SOLERCIA_CONNECT_GOOGLE_CLIENT_ID",),
        ("openid", "email", "https://www.googleapis.com/auth/gmail.readonly"),
        ("Google OAuth Desktop app client", "Workspace admin approval when policy requires it"),
    ),
    TargetSpec(
        ConnectionType.GMAIL_COMPOSE,
        "google.gmail",
        # Writes only, official route only (decision D5, the Gmail send in two steps). A separate
        # grant keeps gmail.compose off the read-only gmail target.
        ("mail.draft.create", "mail.draft.send"),
        ("SOLERCIA_CONNECT_GOOGLE_CLIENT_ID",),
        ("openid", "email", GMAIL_COMPOSE_SCOPE),
        (
            "Google OAuth Desktop app client with the gmail.compose scope",
            "Workspace admin approval when policy requires it",
        ),
    ),
    TargetSpec(
        ConnectionType.GOOGLE_CALENDAR,
        "google.calendar",
        ("calendar.events.list",),
        ("SOLERCIA_CONNECT_GOOGLE_CLIENT_ID",),
        (
            "openid",
            "email",
            "https://www.googleapis.com/auth/calendar.events.readonly",
        ),
        ("Google OAuth Desktop app client", "Workspace admin approval when policy requires it"),
    ),
    TargetSpec(
        ConnectionType.GOOGLE_MEET,
        "google.meet",
        ("meeting.transcripts.list", "meeting.transcript.get"),
        ("SOLERCIA_CONNECT_GOOGLE_CLIENT_ID",),
        (
            "openid",
            "email",
            "https://www.googleapis.com/auth/calendar.events.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ),
        (
            "Google OAuth Desktop app client",
            "Google Workspace account with Google Meet transcription enabled",
            "Workspace admin approval when policy requires it",
        ),
    ),
    TargetSpec(
        ConnectionType.JIRA,
        "jira.cloud",
        (
            "external.issues.search",
            "external.issues.open.list",
            "external.issues.transitions.list",
            "external.issues.transition",
        ),
        ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"),
    ),
    TargetSpec(
        ConnectionType.GLPI,
        "glpi.rest",
        ("tickets.search",),
        ("GLPI_BASE_URL", "GLPI_APP_TOKEN", "GLPI_USER_TOKEN"),
    ),
    TargetSpec(
        ConnectionType.BITBUCKET,
        "bitbucket.cloud",
        # Read-only and official-only (docs/decisions/2026-10-26-conector-bitbucket.md). The account
        # connects with an API token, so no environment configuration and no OAuth scope apply.
        ("scm.repositories.list", "scm.pullrequests.list", "scm.commits.list"),
        (),
        (),
        (
            "Atlassian API token created with scopes for the Bitbucket app: read:user:bitbucket, "
            "read:workspace:bitbucket, read:repository:bitbucket and read:pullrequest:bitbucket",
            "Membership in the Bitbucket workspace that is read",
        ),
    ),
)


def target_spec(value: str | ConnectionType) -> TargetSpec:
    parsed = ConnectionType.parse(value)
    return next(spec for spec in TARGET_SPECS if spec.type is parsed)


def normalize_account(value: str) -> str:
    candidate = value.strip().casefold()
    if not _ACCOUNT_PATTERN.fullmatch(candidate):
        raise ValueError("Connection account must be one valid email address")
    return candidate


class JiraTokenKind(StrEnum):
    """How a Jira connection authenticates. The caller chooses; it is never detected.

    ``classic`` and ``scoped`` are API tokens; ``oauth`` is an Atlassian OAuth 2.0 (3LO) grant
    whose cloud ID comes from the grant's accessible resources.
    """

    CLASSIC = "classic"
    SCOPED = "scoped"
    OAUTH = "oauth"


def normalize_jira_token_binding(
    site: str, token_kind: str | None, cloud_id: str | None
) -> tuple[JiraTokenKind, str | None]:
    """Validate the token kind and cloud ID of a normalized site before any token is read.

    A scoped token without a cloud ID is accepted only where tenant_info can resolve it.
    """

    try:
        kind = JiraTokenKind((token_kind or JiraTokenKind.CLASSIC).strip().casefold())
    except ValueError:
        raise ValueError("--token-kind must be classic, scoped or oauth") from None
    if kind is not JiraTokenKind.SCOPED:
        if cloud_id is not None:
            raise ValueError("--cloud-id applies only to --token-kind scoped")
        return kind, None
    if cloud_id is None:
        if not jira_site_publishes_tenant_info(site):
            raise ValueError("A scoped token on a site outside atlassian.net needs --cloud-id")
        return kind, None
    try:
        return kind, normalize_jira_cloud_id(cloud_id)
    except ValueError:
        raise ValueError("--cloud-id must be the site cloud ID, a UUID") from None


@dataclass(frozen=True, slots=True)
class ConnectSettings:
    microsoft_client_id: str | None
    microsoft_tenant: str
    google_client_id: str | None
    google_client_secret: SecretValue | None
    browser_fallback_enabled: bool
    m365_profile_dir: Path | None
    api_host: str
    api_port: int
    api_key: SecretValue | None
    output_dir: Path | None
    config_dir: Path
    data_dir: Path
    configured_names: tuple[str, ...]
    env_file: Path | None = None
    http_interactive_login: bool = False
    atlassian_client_id: str | None = None
    atlassian_client_secret: SecretValue | None = None
    atlassian_redirect_uri: str = DEFAULT_ATLASSIAN_REDIRECT_URI
    google_profile_dir: Path | None = None
    jira_profile_dir: Path | None = None
    # Project mode: set by SOLERCIA_CONNECT_HOME or, when SOLERCIA_CONNECT_PROJECT_DISCOVERY is on,
    # found at the git root. None keeps the XDG and keyring defaults.
    project_home: Path | None = None
    project_home_source: ProjectHomeSource | None = None


class ProjectHomeError(ValueError):
    """Project mode (``SOLERCIA_CONNECT_HOME`` or the discovered folder) is misconfigured or unsafe.

    It is a ``ValueError``, so every existing configuration handler reports it; its message names
    paths and remediation, never a secret value.
    """


@dataclass(frozen=True, slots=True)
class ProjectLayout:
    """Where project mode keeps each kind of local state, all below one ``home`` folder."""

    home: Path

    @property
    def config_dir(self) -> Path:
        return self.home / "config"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def secrets_dir(self) -> Path:
        return self.home / "secrets"

    @property
    def env_file(self) -> Path:
        return self.home / "connect.env"

    @property
    def gitignore(self) -> Path:
        return self.home / ".gitignore"


def missing_atlassian_configuration(settings: ConnectSettings) -> tuple[str, ...]:
    """Name every Atlassian OAuth setting that is absent; the client secret is never optional."""

    values = (settings.atlassian_client_id, settings.atlassian_client_secret)
    return tuple(
        name for name, value in zip(ATLASSIAN_OAUTH_CONFIGURATION, values, strict=True) if not value
    )


def http_interactive_allowed(settings: ConnectSettings) -> bool:
    """Effective gate for starting the supervised browser from the loopback service.

    The switch alone is not enough: the browser route must be globally available and the
    service must already require its local API key, so an unauthenticated loopback client can
    never open a headed window. ``load_settings`` already forces the bind host to loopback.
    """

    return (
        settings.http_interactive_login
        and settings.api_key is not None
        and settings.browser_fallback_enabled
    )


def load_settings(
    *,
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    cwd: Path | None = None,
) -> ConnectSettings:
    """Resolve the settings; ``cwd`` matters only to discovery and defaults to the process cwd."""

    values, explicit, project_home, project_home_source = _layered_environment(
        environ, env_file, cwd
    )
    if project_home is not None:
        # Project mode keeps every path below the one home; XDG no longer applies to Connect.
        layout = ProjectLayout(project_home)
        config_dir, data_dir = layout.config_dir, layout.data_dir
    else:
        config_home = Path(values.get("XDG_CONFIG_HOME") or (Path.home() / ".config")).expanduser()
        data_home = Path(
            values.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        ).expanduser()
        config_dir = config_home / "solercia-connect"
        data_dir = data_home / "solercia-connect"
    host = _value(values, "SOLERCIA_CONNECT_API_HOST") or "127.0.0.1"
    if host.casefold() not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("SOLERCIA_CONNECT_API_HOST must be a loopback host")
    raw_port = _value(values, "SOLERCIA_CONNECT_API_PORT") or "8765"
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("SOLERCIA_CONNECT_API_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SOLERCIA_CONNECT_API_PORT must be between 1 and 65535")

    secret = _value(values, "SOLERCIA_CONNECT_GOOGLE_CLIENT_SECRET")
    atlassian_secret = _value(values, "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET")
    api_key = _value(values, "SOLERCIA_CONNECT_API_KEY")
    profile = _value(values, "SOLERCIA_CONNECT_M365_PROFILE_DIR")
    output = _value(values, "SOLERCIA_CONNECT_OUTPUT_DIR")
    return ConnectSettings(
        microsoft_client_id=_value(values, "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID"),
        microsoft_tenant=_value(values, "SOLERCIA_CONNECT_MICROSOFT_TENANT") or "organizations",
        google_client_id=_value(values, "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID"),
        google_client_secret=SecretValue(secret) if secret else None,
        browser_fallback_enabled=_bool_value(
            values.get("SOLERCIA_CONNECT_BROWSER_FALLBACK", ""),
            "SOLERCIA_CONNECT_BROWSER_FALLBACK",
        ),
        m365_profile_dir=Path(profile).expanduser() if profile else None,
        api_host=host,
        api_port=port,
        api_key=SecretValue(api_key) if api_key else None,
        output_dir=Path(output).expanduser() if output else None,
        config_dir=config_dir,
        data_dir=data_dir,
        configured_names=tuple(sorted(name for name, value in values.items() if value.strip())),
        env_file=explicit,
        http_interactive_login=_bool_value(
            values.get("SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN", ""),
            "SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN",
        ),
        atlassian_client_id=_value(values, "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID"),
        atlassian_client_secret=SecretValue(atlassian_secret) if atlassian_secret else None,
        atlassian_redirect_uri=_atlassian_redirect_uri(
            _value(values, "SOLERCIA_CONNECT_ATLASSIAN_REDIRECT_URI")
        ),
        google_profile_dir=_optional_path(values, "SOLERCIA_CONNECT_GOOGLE_PROFILE_DIR"),
        jira_profile_dir=_optional_path(values, "SOLERCIA_CONNECT_JIRA_PROFILE_DIR"),
        project_home=project_home,
        project_home_source=project_home_source,
    )


def _project_home(raw: str | None) -> Path | None:
    """Accept only an absolute home without symlinks, so it never depends on the cwd."""

    if raw is None:
        return None
    try:
        candidate = Path(raw).expanduser()
    except RuntimeError:
        raise ProjectHomeError(
            "SOLERCIA_CONNECT_HOME names a user directory that does not exist"
        ) from None
    if not candidate.is_absolute():
        raise ProjectHomeError(
            'SOLERCIA_CONNECT_HOME must be an absolute path, such as "$PWD/.solercia-connect"'
        )
    home = Path(os.path.abspath(candidate))
    _reject_symlink_components(home, "SOLERCIA_CONNECT_HOME", error=ProjectHomeError)
    return home


def _optional_path(values: Mapping[str, str], name: str) -> Path | None:
    value = _value(values, name)
    return Path(value).expanduser() if value else None


def _atlassian_redirect_uri(value: str | None) -> str:
    """Accept only the loopback callback the listener can serve on a fixed, unprivileged port."""

    if value is None:
        return DEFAULT_ATLASSIAN_REDIRECT_URI
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or port is None
        or not 1024 <= port <= 65535
        or parsed.path != "/oauth/callback"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "SOLERCIA_CONNECT_ATLASSIAN_REDIRECT_URI must be "
            "http://127.0.0.1:<port>/oauth/callback or http://localhost:<port>/oauth/callback "
            "with a port from 1024 to 65535"
        )
    return value


def effective_environment(
    *,
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, str]:
    return _layered_environment(environ, env_file, cwd)[0]


def project_selection(
    *,
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> tuple[bool, Path | None]:
    """Return whether discovery is on and the explicit home, without reading any connect.env."""

    values, _ = _base_environment(environ, env_file)
    return _discovery_enabled(values), _project_home(_value(values, "SOLERCIA_CONNECT_HOME"))


# connect.env lives inside the home and is read only after the home is resolved, so it never names
# the home, the env file, or the switch that finds the home.
_PROJECT_ENV_FORBIDDEN = (
    "SOLERCIA_CONNECT_HOME",
    "SOLERCIA_CONNECT_ENV_FILE",
    PROJECT_DISCOVERY_VARIABLE,
)


def _base_environment(
    environ: Mapping[str, str] | None, env_file: Path | None
) -> tuple[dict[str, str], Path | None]:
    """Merge the process environment over SOLERCIA_CONNECT_ENV_FILE; empty values hide nothing."""

    source = dict(os.environ if environ is None else environ)
    raw_explicit = (
        str(env_file) if env_file is not None else source.get("SOLERCIA_CONNECT_ENV_FILE")
    )
    explicit = Path(raw_explicit).expanduser() if raw_explicit and raw_explicit.strip() else None
    file_values = _read_env_file(explicit) if explicit is not None else {}
    return {**file_values, **{key: value for key, value in source.items() if value != ""}}, explicit


def _layered_environment(
    environ: Mapping[str, str] | None, env_file: Path | None, cwd: Path | None
) -> tuple[dict[str, str], Path | None, Path | None, ProjectHomeSource | None]:
    """Merge the process environment over SOLERCIA_CONNECT_ENV_FILE over ``<home>/connect.env``.

    ``connect.env`` exists only in project mode and is the lowest layer. Returns the values, the
    explicit env file, the project home and how that home was resolved.
    """

    values, explicit = _base_environment(environ, env_file)
    project_home, source = _resolve_project_home(values, cwd)
    if project_home is not None:
        values = {**_read_project_env_file(ProjectLayout(project_home).env_file), **values}
    return values, explicit, project_home, source


def _discovery_enabled(values: Mapping[str, str]) -> bool:
    return _bool_value(values.get(PROJECT_DISCOVERY_VARIABLE, ""), PROJECT_DISCOVERY_VARIABLE)


def _resolve_project_home(
    values: Mapping[str, str], cwd: Path | None
) -> tuple[Path | None, ProjectHomeSource | None]:
    """An explicit SOLERCIA_CONNECT_HOME wins; otherwise the opt-in switch searches the git root.

    The switch comes only from the process or the explicit env file, never from connect.env, and
    is validated even when the explicit home makes it moot.
    """

    discovery = _discovery_enabled(values)
    explicit_home = _project_home(_value(values, "SOLERCIA_CONNECT_HOME"))
    if explicit_home is not None:
        return explicit_home, "explicit"
    if not discovery:
        return None, None
    # project_home imports this module for its shared guards, so it is imported where used.
    from solercia_connect.project_home import discover_project_home

    discovered = discover_project_home(_discovery_start(cwd))
    return (discovered, "discovered") if discovered is not None else (None, None)


def _discovery_start(cwd: Path | None) -> Path:
    if cwd is not None:
        return cwd
    try:
        return Path.cwd()
    except OSError:
        raise ProjectHomeError(
            f"{PROJECT_DISCOVERY_VARIABLE} is on, but the current directory cannot be read"
        ) from None


def _read_project_env_file(path: Path) -> dict[str, str]:
    """Read ``<home>/connect.env`` only as a private regular file owned by the user."""

    label = "SOLERCIA_CONNECT_HOME/connect.env"
    descriptor = -1
    try:
        try:
            before = path.lstat()
        except FileNotFoundError:
            return {}
        _validate_project_env_metadata(before, label)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        _validate_project_env_metadata(opened, label)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ProjectHomeError(f"{label} changed during its safety check")
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            text = stream.read()
    except (OSError, UnicodeDecodeError):
        raise ProjectHomeError(f"{label} could not be read safely") from None
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
    try:
        values = _parse_env_text(text)
    except ValueError as exc:
        raise ProjectHomeError(f"{label}: {exc}") from None
    for name in _PROJECT_ENV_FORBIDDEN:
        if name in values:
            raise ProjectHomeError(f"{label} cannot set {name}; set it in the process environment")
    return values


def _validate_project_env_metadata(metadata: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise ProjectHomeError(f"{label} is a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise ProjectHomeError(f"{label} is not a regular file")
    if _wrong_owner(metadata.st_uid):
        raise ProjectHomeError(f"{label} is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ProjectHomeError(f'{label} is not private; run chmod 600 "${label}"')


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    schema: str
    type: ConnectionType
    account: str
    route: str
    capabilities: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    browser_extraction_implemented: bool = False
    # Non-secret Jira binding: the site origin and the opaque accountId observed at connect.
    # Both are optional so v1 files written before they existed keep loading unchanged.
    site: str | None = None
    provider_account_id: str | None = None
    # Absent on files written before scoped tokens; a Jira profile without it is classic.
    token_kind: JiraTokenKind | None = None
    cloud_id: str | None = None
    # The default Bitbucket workspace of a bitbucket profile; never set on another type.
    workspace: str | None = None

    @classmethod
    def create(
        cls,
        type: str | ConnectionType,
        account: str,
        *,
        route: str = "official",
        now: datetime | None = None,
        site: str | None = None,
        provider_account_id: str | None = None,
        token_kind: str | None = None,
        cloud_id: str | None = None,
        workspace: str | None = None,
    ) -> ConnectionProfile:
        parsed = ConnectionType.parse(type)
        if route not in {"official", "browser"}:
            raise ValueError("Connection profile route must be official or browser")
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("Connection profile timestamps must be timezone-aware")
        return cls(
            PROFILE_SCHEMA,
            parsed,
            normalize_account(account),
            route,
            target_spec(parsed).capabilities,
            timestamp,
            timestamp,
            False,
            normalize_jira_site(site) if site is not None else None,
            _provider_account_id(provider_account_id),
            *_jira_token_fields(token_kind, cloud_id),
            _profile_workspace(parsed, workspace),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "type": self.type.value,
            "account": self.account,
            "route": self.route,
            "capabilities": list(self.capabilities),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "browser_extraction_implemented": self.browser_extraction_implemented,
        }
        if self.site is not None:
            payload["site"] = self.site
        if self.provider_account_id is not None:
            payload["provider_account_id"] = self.provider_account_id
        if self.token_kind is not None:
            payload["token_kind"] = self.token_kind.value
        if self.cloud_id is not None:
            payload["cloud_id"] = self.cloud_id
        if self.workspace is not None:
            payload["workspace"] = self.workspace
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ConnectionProfile:
        try:
            schema = str(payload["schema"])
            if schema != PROFILE_SCHEMA:
                raise ValueError("Unsupported connection profile schema")
            parsed_type = ConnectionType.parse(str(payload["type"]))
            account = normalize_account(str(payload["account"]))
            route = str(payload["route"])
            if route not in {"official", "browser"}:
                raise ValueError("Connection profile route is invalid")
            raw_capabilities = payload["capabilities"]
            if not isinstance(raw_capabilities, list) or not all(
                isinstance(item, str) and item for item in raw_capabilities
            ):
                raise ValueError("Connection profile capabilities are malformed")
            created_at = datetime.fromisoformat(str(payload["created_at"]))
            updated_at = datetime.fromisoformat(str(payload["updated_at"]))
            if created_at.tzinfo is None or updated_at.tzinfo is None:
                raise ValueError("Connection profile timestamps must be timezone-aware")
            browser_implemented = payload.get("browser_extraction_implemented", False)
            if not isinstance(browser_implemented, bool):
                raise ValueError("Connection profile browser capability flag is malformed")
            raw_site = payload.get("site")
            if raw_site is not None and normalize_jira_site(str(raw_site)) != raw_site:
                raise ValueError("Connection profile site is not a normalized HTTPS origin")
            provider_account_id = _provider_account_id(payload.get("provider_account_id"))
            raw_kind = payload.get("token_kind")
            raw_cloud_id = payload.get("cloud_id")
            token_kind, cloud_id = _jira_token_fields(raw_kind, raw_cloud_id)
            if (raw_kind, raw_cloud_id) != (token_kind, cloud_id):
                raise ValueError("Connection profile Jira token fields are not normalized")
            raw_workspace = payload.get("workspace")
            workspace = _profile_workspace(parsed_type, raw_workspace)
            if workspace != raw_workspace:
                raise ValueError("Connection profile workspace is not a normalized slug")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Connection profile is malformed") from exc
        return cls(
            schema,
            parsed_type,
            account,
            route,
            tuple(raw_capabilities),
            created_at,
            updated_at,
            browser_implemented,
            raw_site,
            provider_account_id,
            token_kind,
            cloud_id,
            workspace,
        )


def _profile_workspace(type: ConnectionType, workspace: object) -> str | None:
    """Only a Bitbucket profile carries a workspace, always as a normalized slug."""

    if workspace is None:
        return None
    if type is not ConnectionType.BITBUCKET:
        raise ValueError("Only a Bitbucket connection profile has a workspace")
    return normalize_bitbucket_workspace(workspace)


def _jira_token_fields(
    token_kind: object, cloud_id: object
) -> tuple[JiraTokenKind | None, str | None]:
    """A cloud ID exists exactly when the gateway is used: a scoped token or an OAuth grant.

    No kind at all means classic.
    """

    if token_kind is None and cloud_id is None:
        return None, None
    kind = JiraTokenKind(str(token_kind))
    gateway = kind in {JiraTokenKind.SCOPED, JiraTokenKind.OAUTH}
    if gateway != (cloud_id is not None):
        raise ValueError(
            "Connection profile cloud ID must accompany a scoped or OAuth Jira binding"
        )
    return kind, normalize_jira_cloud_id(str(cloud_id)) if cloud_id is not None else None


def _provider_account_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Connection profile provider account ID is malformed")
    return value


class ProfileStore:
    """Private, non-secret, atomic connection profile storage."""

    def __init__(self, root: Path, *, controlled_root: Path | None = None) -> None:
        self.root = Path(os.path.abspath(root.expanduser()))
        self.controlled_root = Path(os.path.abspath((controlled_root or self.root).expanduser()))
        if self.root != self.controlled_root and not self.root.is_relative_to(self.controlled_root):
            raise ValueError("Connection profile root is outside the controlled config directory")

    @classmethod
    def from_settings(cls, settings: ConnectSettings) -> ProfileStore:
        return cls(settings.config_dir / "profiles", controlled_root=settings.config_dir)

    def path_for(self, type: str | ConnectionType, account: str) -> Path:
        parsed = ConnectionType.parse(type)
        normalized = normalize_account(account)
        digest = hashlib.sha256(f"{parsed.value}\0{normalized}".encode()).hexdigest()[:24]
        return self.root / parsed.value / f"profile-{digest}.json"

    def save(self, profile: ConnectionProfile) -> None:
        path = self.path_for(profile.type, profile.account)
        self._prepare_directory(path.parent)
        if _lexists(path):
            _validate_private_profile_file(path)
        content = json.dumps(profile.to_dict(), indent=2, sort_keys=True) + "\n"
        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
            temporary_path = Path(temporary)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if _lexists(path):
                _validate_private_profile_file(path)
            os.replace(temporary_path, path)
            temporary_path = None
        except OSError as exc:
            raise ValueError("Connection profile could not be written safely") from exc
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)

    def get(self, type: str | ConnectionType, account: str) -> ConnectionProfile | None:
        path = self.path_for(type, account)
        if not self._validate_directory_tree(path.parent, allow_missing=True):
            return None
        if not _lexists(path):
            return None
        profile = _load_private_profile(path)
        if profile.type is not ConnectionType.parse(type) or profile.account != normalize_account(
            account
        ):
            raise ValueError("Connection profile identity does not match its storage key")
        return profile

    def list(self, type: str | ConnectionType | None = None) -> tuple[ConnectionProfile, ...]:
        if not self._validate_directory_tree(self.root, allow_missing=True):
            return ()
        types = (ConnectionType.parse(type),) if type is not None else tuple(ConnectionType)
        profiles: list[ConnectionProfile] = []
        for parsed in types:
            directory = self.root / parsed.value
            if not self._validate_directory_tree(directory, allow_missing=True):
                continue
            for path in sorted(directory.glob("profile-*.json")):
                profile = _load_private_profile(path)
                if profile.type is not parsed:
                    raise ValueError("Connection profile identity does not match its directory")
                profiles.append(profile)
        return tuple(
            sorted(profiles, key=lambda item: (list(ConnectionType).index(item.type), item.account))
        )

    def delete(self, type: str | ConnectionType, account: str) -> bool:
        path = self.path_for(type, account)
        if not self._validate_directory_tree(path.parent, allow_missing=True):
            return False
        if not _lexists(path):
            return False
        _validate_private_profile_file(path)
        path.unlink()
        return True

    def _prepare_directory(self, directory: Path) -> None:
        _reject_symlink_components(directory, "Connection profile directory")
        missing: list[Path] = []
        current = directory
        while not _lexists(current):
            missing.append(current)
            if current.parent == current:
                break
            current = current.parent
        try:
            for path in reversed(missing):
                with suppress(FileExistsError):
                    os.mkdir(path, 0o700)
                _reject_symlink_components(path, "Connection profile directory")
        except OSError as exc:
            raise ValueError("Connection profile directory could not be created safely") from exc
        self._validate_directory_tree(directory, allow_missing=False)

    def _validate_directory_tree(self, directory: Path, *, allow_missing: bool) -> bool:
        _reject_symlink_components(directory, "Connection profile directory")
        if directory != self.controlled_root and not directory.is_relative_to(self.controlled_root):
            raise ValueError("Connection profile directory is outside the controlled config root")
        current = self.controlled_root
        components = [current]
        for part in directory.relative_to(self.controlled_root).parts:
            current /= part
            components.append(current)
        for component in components:
            try:
                metadata = component.lstat()
            except FileNotFoundError:
                if allow_missing:
                    return False
                raise ValueError("Connection profile directory does not exist") from None
            _validate_private_directory_metadata(metadata)
        return True


def _load_private_profile(path: Path) -> ConnectionProfile:
    before = _validate_private_profile_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        _validate_private_profile_metadata(opened)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Connection profile changed during its safety check")
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            payload = json.load(stream)
    except json.JSONDecodeError as exc:
        raise ValueError("Connection profile is malformed") from exc
    except OSError as exc:
        raise ValueError("Connection profile could not be read safely") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
    if not isinstance(payload, dict):
        raise ValueError("Connection profile is malformed")
    return ConnectionProfile.from_dict(payload)


def _validate_private_profile_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError("Connection profile could not be inspected safely") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("Connection profile path is a symbolic link")
    _validate_private_profile_metadata(metadata)
    return metadata


def _validate_private_profile_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Connection profile path is not a regular file")
    if _wrong_owner(metadata.st_uid):
        raise ValueError("Connection profile file ownership does not match the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("Connection profile file permissions are not private")


def _validate_private_directory_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Connection profile directory is not a regular directory")
    if _wrong_owner(metadata.st_uid):
        raise ValueError("Connection profile directory ownership does not match the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("Connection profile directory permissions are not private")


def _wrong_owner(owner: int) -> bool:
    return hasattr(os, "getuid") and owner != os.getuid()


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _read_env_file(path: Path) -> dict[str, str]:
    path = Path(os.path.abspath(path.expanduser()))
    _reject_symlink_components(path, "SOLERCIA_CONNECT_ENV_FILE")
    if path.is_symlink():
        raise ValueError("SOLERCIA_CONNECT_ENV_FILE cannot be a symbolic link")
    if not path.is_file():
        raise ValueError("SOLERCIA_CONNECT_ENV_FILE does not exist or is not a file")
    return _parse_env_text(path.read_text(encoding="utf-8"))


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse ``NAME=value`` lines; errors name the line number, never the value."""

    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError(f"Invalid environment file assignment on line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ValueError(f"Invalid environment variable name on line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        result[key] = value
    return result


def _value(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name, "").strip()
    return value or None


def _bool_value(value: str, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _reject_symlink_components(
    path: Path, label: str, *, error: type[ValueError] = ValueError
) -> None:
    """Reject every existing symlink component without resolving the requested path."""

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise error(f"{label} contains a symbolic link")
