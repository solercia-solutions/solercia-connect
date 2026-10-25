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
from typing import Any
from urllib.parse import urlparse

from solercia_connect.providers._shared import ensure_https_base_url
from solercia_connect.secrets import SecretValue

PROFILE_SCHEMA = "solercia.connect/profile/v1"

_ACCOUNT_PATTERN = re.compile(
    r"(?=.{3,320}\Z)[^\s<>@]+@[^\s<>@.]+(?:\.[^\s<>@.]+)+\Z",
    re.ASCII,
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


class ConnectionType(StrEnum):
    OUTLOOK = "outlook"
    TEAMS = "teams"
    ONEDRIVE = "onedrive"
    GMAIL = "gmail"
    GOOGLE_CALENDAR = "google-calendar"
    GOOGLE_MEET = "google-meet"
    JIRA = "jira"
    GLPI = "glpi"

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
        ("mail.sent.list",),
        ("SOLERCIA_CONNECT_GOOGLE_CLIENT_ID",),
        ("openid", "email", "https://www.googleapis.com/auth/gmail.readonly"),
        ("Google OAuth Desktop app client", "Workspace admin approval when policy requires it"),
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
        ("external.issues.search",),
        ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"),
    ),
    TargetSpec(
        ConnectionType.GLPI,
        "glpi.rest",
        ("tickets.search",),
        ("GLPI_BASE_URL", "GLPI_APP_TOKEN", "GLPI_USER_TOKEN"),
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


def normalize_jira_site(value: str) -> str:
    """Reduce a Jira site URL to its HTTPS origin, the non-secret half of a Jira connection."""

    parsed = urlparse(ensure_https_base_url(value.strip(), "Jira site"))
    if parsed.hostname is None:
        raise ValueError("Jira site must name a host")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"https://{parsed.hostname.casefold()}{port}"


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
) -> ConnectSettings:
    source = dict(os.environ if environ is None else environ)
    raw_explicit = (
        str(env_file) if env_file is not None else source.get("SOLERCIA_CONNECT_ENV_FILE")
    )
    explicit = Path(raw_explicit).expanduser() if raw_explicit and raw_explicit.strip() else None
    file_values = _read_env_file(explicit) if explicit is not None else {}
    values = {**file_values, **{key: value for key, value in source.items() if value != ""}}

    config_home = Path(values.get("XDG_CONFIG_HOME") or (Path.home() / ".config")).expanduser()
    data_home = Path(values.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")).expanduser()
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
        config_dir=config_home / "solercia-connect",
        data_dir=data_home / "solercia-connect",
        configured_names=tuple(sorted(name for name, value in values.items() if value.strip())),
        env_file=explicit,
        http_interactive_login=_bool_value(
            values.get("SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN", ""),
            "SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN",
        ),
    )


def effective_environment(
    *,
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> dict[str, str]:
    source = dict(os.environ if environ is None else environ)
    raw_explicit = (
        str(env_file) if env_file is not None else source.get("SOLERCIA_CONNECT_ENV_FILE")
    )
    explicit = Path(raw_explicit).expanduser() if raw_explicit and raw_explicit.strip() else None
    file_values = _read_env_file(explicit) if explicit is not None else {}
    return {**file_values, **{key: value for key, value in source.items() if value != ""}}


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
        )


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
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
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


def _reject_symlink_components(path: Path, label: str) -> None:
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
            raise ValueError(f"{label} contains a symbolic link")
