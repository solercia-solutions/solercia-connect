from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from solercia_connect.config import ConnectSettings, normalize_account, normalize_jira_site
from solercia_connect.domain import ProviderFamily, ReasonCode
from solercia_connect.errors import ConnectorFailure

# Each family keeps its dedicated profiles under its own root, so a browser session is never
# shared across families. The Microsoft name predates the others and must not change: existing
# signed-in profiles live there.
_PROFILE_ROOT_NAMES: dict[ProviderFamily, str] = {
    ProviderFamily.MICROSOFT: "m365",
    ProviderFamily.GOOGLE: "google",
    ProviderFamily.JIRA: "jira",
}


@dataclass(frozen=True, slots=True)
class BrowserProfilePathAudit:
    path: Path
    exists: bool


def browser_profile_root(settings: ConnectSettings, family: ProviderFamily) -> Path:
    """Return one family's dedicated profile root: its explicit setting or ``data_dir/<name>``.

    The Google and Jira roots must stay apart from every other family's root, neither equal to
    it nor nested on either side, or no profile path is returned for them. The Microsoft root is
    resolved exactly as before.
    """

    root = _explicit_or_default_root(settings, family)
    if family is not ProviderFamily.MICROSOFT:
        candidate = _absolute(root)
        for other in _PROFILE_ROOT_NAMES:
            if other is family:
                continue
            other_root = _absolute(_explicit_or_default_root(settings, other))
            if candidate.is_relative_to(other_root) or other_root.is_relative_to(candidate):
                raise _unsafe(
                    f"The {family.value} browser profile root must not equal or nest within "
                    f"the {other.value} browser profile root"
                )
    return root


def browser_profile_path(
    settings: ConnectSettings,
    family: ProviderFamily,
    account: str,
    *,
    site: str | None = None,
) -> Path:
    """Return one isolated profile path below the family's dedicated root.

    A Jira session belongs to one site, so a Jira profile is keyed by the site's origin as well
    as the account; the other families are keyed by the account alone.
    """

    key = normalize_account(account)
    if family is ProviderFamily.JIRA:
        if site is None:
            raise ValueError("A Jira browser profile belongs to one site; name the site")
        key = f"{normalize_jira_site(site)}\n{key}"
    elif site is not None:
        raise ValueError("Only a Jira browser profile is keyed by a site")
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return browser_profile_root(settings, family) / digest


def microsoft_browser_profile_root(settings: ConnectSettings) -> Path:
    """Return the dedicated Microsoft profile root used by every runtime surface."""

    return browser_profile_root(settings, ProviderFamily.MICROSOFT)


def microsoft_browser_profile_path(settings: ConnectSettings, account: str) -> Path:
    """Return one account-isolated profile path below the dedicated root."""

    return browser_profile_path(settings, ProviderFamily.MICROSOFT, account)


def _explicit_or_default_root(settings: ConnectSettings, family: ProviderFamily) -> Path:
    name = _PROFILE_ROOT_NAMES.get(family)
    if name is None:
        raise ValueError(f"No dedicated browser profile root exists for family: {family.value}")
    explicit = {
        ProviderFamily.MICROSOFT: settings.m365_profile_dir,
        ProviderFamily.GOOGLE: settings.google_profile_dir,
        ProviderFamily.JIRA: settings.jira_profile_dir,
    }[family]
    return explicit or (settings.data_dir / name)


def audit_browser_profile_path(
    profile_dir: Path,
    *,
    private_root: Path | None = None,
) -> BrowserProfilePathAudit:
    """Validate one browser profile path without following any symlink component."""

    path = _absolute(profile_dir)
    controlled = _absolute(private_root or path.parent)
    if path != controlled and not path.is_relative_to(controlled):
        raise _unsafe("Microsoft browser profile is outside its controlled private root")
    _reject_symlink_components(path)
    _reject_personal_browser_profile(path)
    for component in _controlled_components(controlled, path):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise _unsafe("Microsoft browser profile path must contain only directories")
        if _wrong_owner(metadata.st_uid):
            raise _unsafe("Microsoft browser profile ownership does not match the current user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise _unsafe("Microsoft browser profile permissions are not private")
    return BrowserProfilePathAudit(path, path.exists())


def ensure_browser_profile_path(profile_dir: Path) -> Path:
    """Create a dedicated profile tree privately after a no-symlink audit."""

    path = _absolute(profile_dir)
    private_root = path.parent
    audit_browser_profile_path(path, private_root=private_root)
    try:
        _mkdir_missing_private(private_root)
        if not path.exists():
            os.mkdir(path, 0o700)
    except OSError as exc:
        raise _unsafe("Microsoft browser profile directory could not be created safely") from exc
    return audit_browser_profile_path(path, private_root=private_root).path


def _mkdir_missing_private(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while True:
        try:
            current.lstat()
            break
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent
    for directory in reversed(missing):
        with suppress(FileExistsError):
            os.mkdir(directory, 0o700)
        _reject_symlink_components(directory)
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or _wrong_owner(metadata.st_uid):
            raise _unsafe("Microsoft browser profile directory is not privately controlled")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise _unsafe("Microsoft browser profile permissions are not private")


def _controlled_components(private_root: Path, path: Path) -> tuple[Path, ...]:
    relative = path.relative_to(private_root)
    components = [private_root]
    current = private_root
    for part in relative.parts:
        current /= part
        components.append(current)
    return tuple(components)


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise _unsafe("Microsoft browser profile path contains a symbolic link")


def _reject_personal_browser_profile(path: Path) -> None:
    home = _absolute(Path.home())
    forbidden_roots = (
        home / "Library/Application Support/Google/Chrome",
        home / "Library/Application Support/Microsoft Edge",
        home / "Library/Application Support/BraveSoftware/Brave-Browser",
        home / ".config/google-chrome",
        home / ".config/chromium",
        home / ".config/microsoft-edge",
    )
    if any(path == root or path.is_relative_to(root) for root in forbidden_roots):
        raise _unsafe(
            "Microsoft 365 fallback requires a dedicated profile, not a personal browser profile"
        )


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _wrong_owner(owner: int) -> bool:
    return hasattr(os, "getuid") and owner != os.getuid()


def _unsafe(message: str) -> ConnectorFailure:
    return ConnectorFailure(ReasonCode.CONFIGURATION_INVALID, message)
