from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solercia_connect.browser_profile import (
    audit_browser_profile_path,
    browser_profile_path,
    browser_profile_root,
)
from solercia_connect.browser_routes import has_browser_route, target_family
from solercia_connect.config import (
    MICROSOFT_TARGETS,
    ConnectionType,
    ConnectSettings,
    ProfileStore,
    normalize_account,
    target_spec,
)
from solercia_connect.domain import ProviderFamily
from solercia_connect.errors import ConnectorFailure
from solercia_connect.project_home import GitRunner, ProjectHomeInspection, inspect_project_home


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    required: bool
    remediation: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "required": self.required,
            "remediation": list(self.remediation),
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    type: ConnectionType
    account: str | None
    ready: bool
    missing_configuration: tuple[str, ...]
    capabilities: tuple[str, ...]
    oauth_scopes: tuple[str, ...]
    external_prerequisites: tuple[str, ...]
    checks: tuple[DoctorCheck, ...]
    service_host: str
    service_port: int
    api_key_configured: bool
    # Present only in project mode, so the report is unchanged without SOLERCIA_CONNECT_HOME.
    project_mode: ProjectHomeInspection | None = None
    # "explicit" (SOLERCIA_CONNECT_HOME) or "discovered" (SOLERCIA_CONNECT_PROJECT_DISCOVERY).
    project_home_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "type": self.type.value,
            "account": self.account,
            "ready": self.ready,
            "missing_configuration": list(self.missing_configuration),
            "capabilities": list(self.capabilities),
            "oauth_scopes": list(self.oauth_scopes),
            "external_prerequisites": list(self.external_prerequisites),
            "checks": [check.to_dict() for check in self.checks],
            "service": {
                "host": self.service_host,
                "port": self.service_port,
                "api_key_configured": self.api_key_configured,
            },
        }
        if self.project_mode is not None:
            payload["project_mode"] = self.project_mode.to_dict()
            payload["project_home_source"] = self.project_home_source
        return payload


class DoctorService:
    def __init__(
        self,
        settings: ConnectSettings,
        profiles: ProfileStore,
        *,
        module_available: Callable[[str], bool] | None = None,
        keyring_probe: Callable[[], tuple[bool, str]] | None = None,
        browser_probe: Callable[[], tuple[bool, str]] | None = None,
        git_runner: GitRunner | None = None,
    ) -> None:
        self._settings = settings
        self._profiles = profiles
        self._module_available = module_available or _module_available
        self._keyring_probe = keyring_probe or _keyring_backend_probe
        self._browser_probe = browser_probe or _playwright_browser_probe
        self._git_runner = git_runner
        self._project_inspection: ProjectHomeInspection | None = None

    def inspect(
        self,
        type: str | ConnectionType,
        account: str | None = None,
    ) -> DoctorReport:
        spec = target_spec(type)
        normalized_account = normalize_account(account) if account is not None else None
        configured_names = _configured_names(self._settings)
        missing = tuple(
            name for name in spec.required_configuration if name not in configured_names
        )
        microsoft_target = spec.type in MICROSOFT_TARGETS
        browser_target = has_browser_route(spec.type)
        oauth_target = spec.type in {
            ConnectionType.OUTLOOK,
            ConnectionType.TEAMS,
            ConnectionType.ONEDRIVE,
            ConnectionType.GMAIL,
            ConnectionType.GOOGLE_CALENDAR,
            ConnectionType.GOOGLE_MEET,
        }
        browser_profile_status = "not_applicable"
        browser_profile_detail = "Microsoft browser profiles do not apply to this target"
        browser_route_status = "not_applicable"
        browser_route_detail = "Microsoft browser fallback does not apply to this target"
        browser_route_ready = False
        if browser_target:
            browser_profile_status, browser_profile_detail = self._browser_profile_check(
                target_family(spec.type),
                normalized_account,
            )
            if not self._settings.browser_fallback_enabled:
                browser_route_status = "disabled"
                browser_route_detail = (
                    "Microsoft browser fallback is globally unavailable; doctor did not authorize "
                    "or launch it"
                )
            elif browser_profile_status == "unsafe":
                browser_route_status = "unsafe"
                browser_route_detail = browser_profile_detail
            else:
                browser_runtime_ready, browser_runtime_detail = self._browser_probe()
                browser_route_ready = browser_runtime_ready
                browser_route_status = "ready" if browser_runtime_ready else "not_ready"
                browser_route_detail = browser_runtime_detail

        official_configuration_ready = not missing
        project = self._project_mode()
        keyring_ready = not oauth_target
        keyring_checks: list[DoctorCheck] = []
        if oauth_target and project is not None:
            # Project mode keeps secrets in files, so the folder's safety replaces the keyring.
            keyring_ready = project.ready
            keyring_checks.extend(
                (
                    DoctorCheck(
                        "keyring_package",
                        "not_required",
                        "Project mode stores secrets in files under SOLERCIA_CONNECT_HOME/secrets",
                        False,
                    ),
                    DoctorCheck(
                        "keyring_backend",
                        "not_required",
                        "Project mode does not use the OS keyring",
                        False,
                    ),
                )
            )
        elif oauth_target:
            if official_configuration_ready:
                package_available = self._module_available("keyring")
                keyring_checks.append(
                    DoctorCheck(
                        "keyring_package",
                        "ready" if package_available else "missing",
                        (
                            "Keyring package is importable"
                            if package_available
                            else "Keyring package is not installed"
                        ),
                        True,
                        (() if package_available else ("Install the oauth dependency extra.",)),
                    )
                )
                backend_ready, backend_detail = (
                    self._keyring_probe()
                    if package_available
                    else (False, "No backend can be probed")
                )
                keyring_ready = package_available and backend_ready
                keyring_checks.append(
                    DoctorCheck(
                        "keyring_backend",
                        "ready" if keyring_ready else "unavailable",
                        backend_detail,
                        True,
                        (
                            ()
                            if keyring_ready
                            else ("Configure an OS keyring backend before connecting.",)
                        ),
                    )
                )
            else:
                keyring_checks.extend(
                    (
                        DoctorCheck(
                            "keyring_package",
                            "not_required",
                            "Keyring is required only by a configured official OAuth route",
                            False,
                        ),
                        DoctorCheck(
                            "keyring_backend",
                            "not_required",
                            "Keyring is not required until an official OAuth route is configured",
                            False,
                        ),
                    )
                )

        official_route_ready = official_configuration_ready and keyring_ready
        configuration_required = not (browser_target and browser_route_ready)
        checks: list[DoctorCheck] = [
            DoctorCheck(
                "configuration",
                "ready" if not missing else "missing",
                (
                    "Required non-secret configuration names are present"
                    if not missing
                    else "Configuration is missing only for the official provider route"
                    if browser_target
                    else "Required configuration names are missing"
                ),
                configuration_required,
                (
                    ()
                    if browser_target and browser_route_ready
                    else tuple(f"Set {name}." for name in missing)
                ),
            )
        ]
        checks.extend(keyring_checks)
        if project is not None:
            subject = (
                "The project home discovered at the git root"
                if self._settings.project_home_source == "discovered"
                else "SOLERCIA_CONNECT_HOME"
            )
            checks.append(
                DoctorCheck(
                    "project_mode",
                    "ready" if project.ready else "unsafe",
                    (
                        f"{subject} is private and git tracks nothing inside it"
                        if project.ready
                        else f"{subject} is not safe for project mode"
                    ),
                    True,
                    project.problems,
                )
            )

        if microsoft_target:
            checks.append(
                DoctorCheck(
                    "microsoft_official_route",
                    (
                        "ready"
                        if official_route_ready
                        else "unavailable"
                        if missing
                        else "not_ready"
                    ),
                    (
                        "Microsoft official OAuth and secure token storage are ready"
                        if official_route_ready
                        else (
                            "Microsoft official OAuth is unavailable because no client ID is "
                            "configured"
                        )
                        if missing
                        else (
                            "Microsoft official OAuth is configured but secure token storage is "
                            "not ready"
                        )
                    ),
                    not browser_route_ready,
                )
            )
        if browser_target:
            profile_remediation = (
                ()
                if browser_profile_status == "ready"
                else (
                    "Use a dedicated private Microsoft profile root under the Solercia data "
                    "directory.",
                )
            )
            checks.append(
                DoctorCheck(
                    "browser_profile",
                    browser_profile_status,
                    browser_profile_detail,
                    self._settings.browser_fallback_enabled and not official_route_ready,
                    profile_remediation,
                )
            )
            browser_remediation: tuple[str, ...] = ()
            if browser_route_status == "disabled":
                browser_remediation = (
                    "Enable SOLERCIA_CONNECT_BROWSER_FALLBACK, then rerun connect with "
                    "--allow-browser-fallback.",
                )
            elif browser_route_status == "not_ready":
                browser_remediation = ("Run make setup-browser.",)
            elif browser_route_status == "unsafe":
                browser_remediation = profile_remediation
            checks.append(
                DoctorCheck(
                    "microsoft_browser_route",
                    browser_route_status,
                    browser_route_detail,
                    not official_route_ready,
                    browser_remediation,
                )
            )

        profile = (
            self._profiles.get(spec.type, normalized_account)
            if normalized_account is not None
            else None
        )
        checks.append(
            DoctorCheck(
                "connection_profile",
                "present" if profile is not None else "absent",
                (
                    "A non-secret local connection profile exists"
                    if profile is not None
                    else "No local connection profile exists"
                ),
                False,
                (
                    ()
                    if profile is not None or normalized_account is None
                    else (
                        "Run solercia-connect connect "
                        f"{spec.type.value} with the inspected account.",
                    )
                ),
            )
        )

        for module in ("fastapi", "uvicorn", "playwright"):
            available = self._module_available(module)
            checks.append(
                DoctorCheck(
                    f"optional_package_{module}",
                    "available" if available else "not_installed",
                    f"Optional package {module} is {'available' if available else 'not installed'}",
                    False,
                )
            )

        checks.append(
            DoctorCheck(
                "api_security",
                "ready" if self._settings.api_key is not None else "missing",
                (
                    "Local API key authentication is configured"
                    if self._settings.api_key is not None
                    else "Local extraction POST endpoints are disabled until an API key is set"
                ),
                False,
                (
                    ()
                    if self._settings.api_key is not None
                    else ("Set SOLERCIA_CONNECT_API_KEY before using POST /v1/run.",)
                ),
            )
        )

        personal_account_blocked = (
            spec.type is ConnectionType.TEAMS
            and self._settings.microsoft_tenant.casefold() == "consumers"
        )
        if personal_account_blocked:
            checks.append(
                DoctorCheck(
                    "teams_account_audience",
                    "unsupported",
                    "Teams transcript APIs do not support personal Microsoft accounts",
                    True,
                    ("Use a Microsoft work or school tenant account.",),
                )
            )

        return DoctorReport(
            spec.type,
            normalized_account,
            (
                (official_route_ready or browser_route_ready)
                if browser_target
                else official_route_ready
            )
            and not personal_account_blocked
            and (project is None or project.ready),
            missing,
            spec.capabilities,
            spec.oauth_scopes,
            spec.external_prerequisites,
            tuple(checks),
            self._settings.api_host,
            self._settings.api_port,
            self._settings.api_key is not None,
            project,
            self._settings.project_home_source,
        )

    def _project_mode(self) -> ProjectHomeInspection | None:
        home = self._settings.project_home
        if home is None:
            return None
        if self._project_inspection is None:
            self._project_inspection = inspect_project_home(home, runner=self._git_runner)
        return self._project_inspection

    def _browser_profile_check(
        self,
        family: ProviderFamily,
        account: str | None,
    ) -> tuple[str, str]:
        root = browser_profile_root(self._settings, family)
        profile = (
            browser_profile_path(self._settings, family, account)
            if account is not None
            else root / "legacy"
        )
        try:
            audit = audit_browser_profile_path(profile, private_root=root)
        except ConnectorFailure as failure:
            return "unsafe", failure.safe_message
        return (
            "ready",
            "Dedicated Microsoft browser profile is private"
            if audit.exists
            else "Dedicated Microsoft browser profile can be created privately when authorized",
        )


def _configured_names(settings: ConnectSettings) -> frozenset[str]:
    return frozenset(settings.configured_names)


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _keyring_backend_probe() -> tuple[bool, str]:
    try:
        import keyring

        backend = keyring.get_keyring()
        backend_name = f"{type(backend).__module__}.{type(backend).__name__}"
        priority = getattr(backend, "priority", 0)
        usable = bool(
            priority
            and "fail" not in backend_name.casefold()
            and "null" not in backend_name.casefold()
        )
        return usable, (
            f"Keyring backend is available ({backend_name})"
            if usable
            else "Keyring backend is null, failing, or has no usable priority"
        )
    except Exception:
        return False, "Keyring backend could not be initialized safely"


def _playwright_browser_probe() -> tuple[bool, str]:
    import os

    if not _module_available("playwright"):
        return False, "Playwright package is not installed"
    configured = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    roots = (
        [Path(configured).expanduser()]
        if configured
        else [
            Path.home() / "Library" / "Caches" / "ms-playwright",
            Path.home() / ".cache" / "ms-playwright",
        ]
    )
    if any(
        root.is_dir() and any(root.glob(pattern))
        for root in roots
        for pattern in ("chromium-*", "chromium_headless_shell-*")
    ):
        return True, "A Playwright Chromium installation directory is present"
    return False, "Playwright Chromium is not installed"
