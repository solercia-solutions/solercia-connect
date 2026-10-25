"""Every surface offers the browser route only to a family whose browser adapter exists.

Today that is Microsoft alone, so Google and Jira targets must keep the exact refusals they had
before the route policy learned their families.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import solercia_connect.connections as connections_module
from solercia_connect.application import run_application
from solercia_connect.browser_routes import (
    BROWSER_PROVIDER_IDS,
    BROWSER_ROUTE_FAMILIES,
    has_browser_route,
    target_family,
)
from solercia_connect.cli import app
from solercia_connect.config import (
    MICROSOFT_TARGETS,
    ConnectionType,
    ConnectSettings,
    ProfileStore,
    load_settings,
)
from solercia_connect.connections import ConnectionService
from solercia_connect.doctor import DoctorService
from solercia_connect.domain import ProviderFamily, ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import CONNECT_FALLBACK_REASONS, RUNTIME_FALLBACK_REASONS, RoutePolicy
from solercia_connect.service import ConnectionConnectRequest, RunRequest
from tests.fakes import NOW
from tests.test_connections import MemoryTokenStore

_ACCOUNT = "user@example.com"
_NON_BROWSER_TARGETS = (
    ConnectionType.GMAIL,
    ConnectionType.GOOGLE_CALENDAR,
    ConnectionType.GOOGLE_MEET,
    ConnectionType.JIRA,
)


def _settings(tmp_path: Path, **extra: str) -> ConnectSettings:
    return load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
            "SOLERCIA_CONNECT_BROWSER_FALLBACK": "true",
            **extra,
        }
    )


def _never_called(*args: object) -> str:
    raise AssertionError(f"The browser route must not start: {args!r}")


def _service(settings: ConnectSettings, **overrides: Any) -> ConnectionService:
    return ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        browser_connector=_never_called,
        browser_status=_never_called,
        clock=lambda: NOW,
        **overrides,
    )


# --- One source for the policy and for which families have a browser route -----------------


def test_route_policy_covers_the_microsoft_google_and_jira_families() -> None:
    policy = RoutePolicy()

    assert policy.browser_fallback_families == {
        ProviderFamily.MICROSOFT,
        ProviderFamily.GOOGLE,
        ProviderFamily.JIRA,
    }
    # The per-family capability lists are pinned in test_browser_families.py.
    assert policy.browser_fallback_reasons is RUNTIME_FALLBACK_REASONS


def test_fallback_reasons_come_from_one_module() -> None:
    assert {
        ReasonCode.ADMIN_CONSENT_REQUIRED,
        ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE,
        ReasonCode.CONFIGURATION_INVALID,
    } == RUNTIME_FALLBACK_REASONS
    assert RUNTIME_FALLBACK_REASONS - {ReasonCode.CONFIGURATION_INVALID} == CONNECT_FALLBACK_REASONS
    assert connections_module.CONNECT_FALLBACK_REASONS is CONNECT_FALLBACK_REASONS


def test_only_microsoft_targets_have_a_browser_route_today() -> None:
    assert {ProviderFamily.MICROSOFT} == BROWSER_ROUTE_FAMILIES
    assert {"microsoft365.browser"} == BROWSER_PROVIDER_IDS
    assert {target for target in ConnectionType if has_browser_route(target)} == MICROSOFT_TARGETS


@pytest.mark.parametrize(
    ("target", "family"),
    [
        (ConnectionType.OUTLOOK, ProviderFamily.MICROSOFT),
        (ConnectionType.TEAMS, ProviderFamily.MICROSOFT),
        (ConnectionType.ONEDRIVE, ProviderFamily.MICROSOFT),
        (ConnectionType.GMAIL, ProviderFamily.GOOGLE),
        (ConnectionType.GOOGLE_CALENDAR, ProviderFamily.GOOGLE),
        (ConnectionType.GOOGLE_MEET, ProviderFamily.GOOGLE),
        (ConnectionType.JIRA, ProviderFamily.JIRA),
        (ConnectionType.GLPI, ProviderFamily.GLPI),
    ],
)
def test_every_target_belongs_to_one_family(
    target: ConnectionType,
    family: ProviderFamily,
) -> None:
    assert target_family(target) is family


@pytest.mark.parametrize("target", [ConnectionType.GMAIL, ConnectionType.JIRA])
def test_no_browser_adapter_is_built_for_a_family_without_one(
    tmp_path: Path,
    target: ConnectionType,
) -> None:
    with pytest.raises(ConnectorFailure) as raised:
        _service(_settings(tmp_path))._browser_adapter(target, _ACCOUNT)

    assert raised.value.reason is ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED


@pytest.mark.parametrize("provider", sorted(BROWSER_PROVIDER_IDS))
def test_health_of_every_available_browser_provider_needs_the_explicit_flag(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    def registry_sentinel(**_: object) -> ProviderRegistry:
        raise AssertionError("No registry may be built without the explicit flag")

    monkeypatch.setattr("solercia_connect.cli.registry_from_environment", registry_sentinel)

    result = CliRunner().invoke(app, ["health", provider])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "action_required"
    assert f"health {provider} --allow-browser-fallback" in result.stdout


# --- Today's refusals for Google and Jira are unchanged -------------------------------------


@pytest.mark.parametrize("target", _NON_BROWSER_TARGETS)
def test_supervised_browser_login_still_refuses_google_and_jira_targets(
    tmp_path: Path,
    target: ConnectionType,
) -> None:
    result = _service(_settings(tmp_path)).connect_browser(target, _ACCOUNT)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == ReasonCode.CAPABILITY_NOT_IMPLEMENTED.value
    assert result.error.message == "Browser login is available only for Microsoft targets"


@pytest.mark.parametrize("target", [ConnectionType.GMAIL, ConnectionType.GOOGLE_CALENDAR])
def test_a_blocked_google_connect_never_enters_a_browser_route(
    tmp_path: Path,
    target: ConnectionType,
) -> None:
    def blocked(oauth: object, open_browser: bool) -> None:
        del oauth, open_browser
        raise ConnectorFailure(ReasonCode.ADMIN_CONSENT_REQUIRED, "admin_policy_enforced")

    service = _service(
        _settings(tmp_path),
        oauth_builder=lambda type_value, account: object(),
        google_callback_flow=blocked,
    )

    result = service.connect(target, _ACCOUNT, allow_browser_fallback=True)

    assert result.ok is False
    assert result.status != "connected_browser"
    assert result.error is not None
    assert result.error.code == ReasonCode.ADMIN_CONSENT_REQUIRED.value


@pytest.mark.parametrize("target", _NON_BROWSER_TARGETS)
def test_http_connect_and_run_models_still_refuse_browser_fallback_for_google_and_jira(
    target: ConnectionType,
) -> None:
    with pytest.raises(ValidationError, match="available only for Microsoft targets"):
        ConnectionConnectRequest(type=target, account=_ACCOUNT, allow_browser_fallback=True)
    capability = "external.issues.search" if target is ConnectionType.JIRA else "mail.sent.list"
    with pytest.raises(ValidationError, match="available only for Microsoft targets"):
        RunRequest(
            type=target,
            account=_ACCOUNT,
            capability=capability,
            allow_browser_fallback=True,
        )


def test_a_google_meet_run_with_browser_fallback_is_still_invalid_input(tmp_path: Path) -> None:
    result = run_application(
        {
            "type": "google-meet",
            "account": _ACCOUNT,
            "capability": "meeting.transcripts.list",
            "meeting_id": "abc-defg-hij",
            "allow_browser_fallback": True,
        },
        settings=_settings(tmp_path),
    )

    assert result.status == "invalid_input"
    assert result.error is not None
    assert result.error.message == "Browser fallback is not available for Google Meet"


@pytest.mark.parametrize("target", ["gmail", "google-calendar", "jira"])
def test_doctor_offers_no_browser_checks_for_google_and_jira_targets(
    tmp_path: Path,
    target: str,
) -> None:
    report = DoctorService(
        _settings(tmp_path),
        ProfileStore.from_settings(_settings(tmp_path)),
        module_available=lambda name: name == "keyring",
        keyring_probe=lambda: (True, "test-backend"),
        browser_probe=lambda: pytest.fail("A family without a browser route must not probe it"),
    ).inspect(target, _ACCOUNT)
    names = {check.name for check in report.checks}

    assert "browser_profile" not in names
    assert "microsoft_browser_route" not in names


@pytest.mark.parametrize("provider", ["google.browser", "jira.browser"])
def test_health_of_a_browser_provider_that_does_not_exist_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    built: list[object] = []
    monkeypatch.setattr(
        "solercia_connect.cli.registry_from_environment",
        lambda **kwargs: built.append(kwargs) or ProviderRegistry(),
    )

    result = CliRunner().invoke(app, ["health", provider])
    envelope = json.loads(result.stdout)

    assert result.exit_code == 1
    assert envelope["status"] != "action_required"
    assert envelope["error"]["code"] == ReasonCode.CONFIGURATION_INVALID.value
    assert len(built) == 1
