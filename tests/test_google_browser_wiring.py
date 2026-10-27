"""Google Meet's browser route as every surface reaches it: connect, registry and runtime.

The adapter itself is proven in ``test_google_browser_meet``; these tests prove that the route is
offered where decision 2026-10-25 (D1, made precise on 2026-10-26) opens it, and nowhere else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.config import ConnectionProfile, ConnectionType, ProfileStore, load_settings
from solercia_connect.connections import ConnectionService
from solercia_connect.domain import (
    Capability,
    MeetingTranscript,
    ProviderFamily,
    ReasonCode,
    RouteKind,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.google_browser import GoogleBrowserAdapter
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import ConnectorRuntime
from tests.fakes import NOW
from tests.test_connections import MemoryTokenStore
from tests.test_google_browser_meet import ACCOUNT, _adapter, _meeting_driver, _request


def _environ(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "SOLERCIA_CONNECT_BROWSER_FALLBACK": "true",
        **extra,
    }


def _service(tmp_path: Path, **overrides: Any) -> ConnectionService:
    settings = load_settings(environ=_environ(tmp_path))
    return ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        clock=lambda: NOW,
        **overrides,
    )


def test_connect_without_a_google_client_takes_the_authorized_browser_route(
    tmp_path: Path,
) -> None:
    started: list[tuple[ConnectionType, str]] = []

    def connector(parsed: ConnectionType, account: str) -> str:
        started.append((parsed, account))
        return account

    service = _service(tmp_path, browser_connector=connector)

    result = service.connect("google-meet", ACCOUNT, allow_browser_fallback=True)

    assert result.ok is True
    assert result.status == "connected_browser"
    assert started == [(ConnectionType.GOOGLE_MEET, ACCOUNT)]


def test_connect_without_a_google_client_and_without_the_flag_stays_a_configuration_error(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, browser_connector=lambda *args: pytest.fail("no browser"))

    result = service.connect("google-meet", ACCOUNT)

    assert result.status == "configuration_error"


def test_a_blocked_google_meet_connect_enters_the_browser_route(tmp_path: Path) -> None:
    def blocked(oauth: object, open_browser: bool) -> None:
        raise ConnectorFailure(ReasonCode.ADMIN_CONSENT_REQUIRED, "admin_policy_enforced")

    settings = load_settings(
        environ=_environ(tmp_path, SOLERCIA_CONNECT_GOOGLE_CLIENT_ID="google-client")
    )
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        oauth_builder=lambda type_value, account: object(),  # type: ignore[arg-type,return-value]
        google_callback_flow=blocked,
        browser_connector=lambda parsed, account: account,
        clock=lambda: NOW,
    )

    result = service.connect("google-meet", ACCOUNT, allow_browser_fallback=True)

    assert result.status == "connected_browser"


def test_the_connection_service_builds_the_google_adapter_on_the_google_profile_root(
    tmp_path: Path,
) -> None:
    adapter = _service(tmp_path)._browser_adapter(ConnectionType.GOOGLE_MEET, ACCOUNT)

    assert isinstance(adapter, GoogleBrowserAdapter)
    assert "google" in adapter._profile_dir.parts


def test_the_registry_serves_a_browser_connected_meet_account_with_the_google_adapter(
    tmp_path: Path,
) -> None:
    environ = _environ(tmp_path)
    settings = load_settings(environ=environ)
    ProfileStore.from_settings(settings).save(
        ConnectionProfile.create(ConnectionType.GOOGLE_MEET, ACCOUNT, route="browser", now=NOW)
    )

    def build(include_browser: bool) -> ProviderRegistry:
        return registry_from_environment(
            connection_type=ConnectionType.GOOGLE_MEET,
            account=ACCOUNT,
            capability=Capability.MEETING_TRANSCRIPTS_LIST,
            environ=environ,
            include_browser=include_browser,
            settings_override=settings,
            token_store=MemoryTokenStore(),
        )

    assert isinstance(build(True).browser(ProviderFamily.GOOGLE), GoogleBrowserAdapter)
    assert build(False).browser(ProviderFamily.GOOGLE) is None


def test_the_runtime_serves_meet_over_the_browser_when_no_official_route_exists() -> None:
    registry = ProviderRegistry()
    registry.register(_adapter(_meeting_driver()))

    outcome = ConnectorRuntime(registry).execute(_request(), browser_fallback_enabled=True)

    assert outcome.ok is True
    assert outcome.receipt.selected_route is RouteKind.BROWSER
    assert all(isinstance(record, MeetingTranscript) for record in outcome.records)
    assert {record.provenance.provider_id for record in outcome.records} == {"google.browser"}


def test_the_runtime_never_opens_the_browser_without_the_flag() -> None:
    driver = _meeting_driver()
    registry = ProviderRegistry()
    registry.register(_adapter(driver))

    outcome = ConnectorRuntime(registry).execute(_request())

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.CONFIGURATION_INVALID
    assert driver.open_calls == []
