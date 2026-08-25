from __future__ import annotations

import hashlib
from pathlib import Path

from solercia_connect.bootstrap import (
    browser_adapter_from_environment,
    browser_fallback_enabled_from_environment,
    registry_from_environment,
)
from solercia_connect.config import ConnectionProfile, ConnectionType, ProfileStore, load_settings
from solercia_connect.oauth import OAuthTokenSet


class MemoryTokenStore:
    def load(self, key: str) -> OAuthTokenSet | None:
        del key
        return None

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        del key, tokens

    def delete(self, key: str) -> None:
        del key


def _base(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


def test_bootstrap_registers_every_legacy_token_provider_without_exposing_secrets(
    tmp_path: Path,
) -> None:
    values = {
        **_base(tmp_path),
        "MS_GRAPH_ACCESS_TOKEN": "microsoft-bootstrap-token",
        "MS_GRAPH_EXPECTED_ACCOUNT": "user@example.com",
        "GOOGLE_ACCESS_TOKEN": "google-bootstrap-token",
        "GOOGLE_EXPECTED_ACCOUNT": "user@example.com",
        "GOOGLE_CALENDAR_ID": "calendar-1",
        "JIRA_BASE_URL": "https://jira.example",
        "JIRA_EMAIL": "user@example.com",
        "JIRA_API_TOKEN": "jira-token",
        "JIRA_EXPECTED_ACCOUNT_ID": "account-1",
        "GLPI_BASE_URL": "https://glpi.example",
        "GLPI_APP_TOKEN": "app-token",
        "GLPI_USER_TOKEN": "user-token",
    }

    registry = registry_from_environment(environ=values, token_store=MemoryTokenStore())
    configured = [descriptor.provider_id for descriptor in registry.descriptors()]

    assert configured == [
        "glpi.rest",
        "google.calendar",
        "google.gmail",
        "jira.cloud",
        "microsoft.graph",
    ]
    assert "microsoft365.browser" not in configured
    assert "token" not in repr(registry.descriptors()).casefold()


def test_bootstrap_registers_official_oauth_profiles_by_selected_target(tmp_path: Path) -> None:
    values = {
        **_base(tmp_path),
        "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID": "microsoft-client",
        "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
    }
    settings = load_settings(environ=values)
    profiles = ProfileStore.from_settings(settings)
    profiles.save(ConnectionProfile.create("outlook", "user@example.com"))
    profiles.save(ConnectionProfile.create("gmail", "user@example.com"))

    microsoft = registry_from_environment(
        connection_type="outlook",
        account="user@example.com",
        token_store=MemoryTokenStore(),
        environ=values,
    )
    gmail = registry_from_environment(
        connection_type="gmail",
        account="user@example.com",
        token_store=MemoryTokenStore(),
        environ=values,
    )

    assert [item.provider_id for item in microsoft.descriptors()] == ["microsoft.graph"]
    assert [item.provider_id for item in gmail.descriptors()] == ["google.gmail"]


def test_browser_registration_requires_real_profile_or_explicit_ready_directory(
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "m365"
    values = {
        **_base(tmp_path),
        "SOLERCIA_CONNECT_MICROSOFT_ACCESS_TOKEN": "bootstrap-token",
        "SOLERCIA_CONNECT_BROWSER_FALLBACK": "true",
        "SOLERCIA_CONNECT_M365_PROFILE_DIR": str(profile_root),
    }

    absent = registry_from_environment(
        connection_type="outlook",
        account="user@example.com",
        token_store=MemoryTokenStore(),
        environ=values,
    )
    profile_root.mkdir()
    ready = registry_from_environment(
        connection_type="outlook",
        account="user@example.com",
        token_store=MemoryTokenStore(),
        environ=values,
    )

    assert [item.provider_id for item in absent.descriptors()] == ["microsoft.graph"]
    assert [item.provider_id for item in ready.descriptors()] == [
        "microsoft.graph",
        "microsoft365.browser",
    ]


def test_browser_profile_route_is_registered_without_claiming_official_configuration(
    tmp_path: Path,
) -> None:
    values = {
        **_base(tmp_path),
        "SOLERCIA_CONNECT_BROWSER_FALLBACK": "true",
    }
    settings = load_settings(environ=values)
    ProfileStore.from_settings(settings).save(
        ConnectionProfile.create(
            "teams",
            "user@example.com",
            route="browser",
        )
    )

    registry = registry_from_environment(
        connection_type="teams",
        account="user@example.com",
        token_store=MemoryTokenStore(),
        environ=values,
    )

    assert [item.provider_id for item in registry.descriptors()] == ["microsoft365.browser"]


def test_browser_profile_route_is_not_registered_when_global_fallback_is_disabled(
    tmp_path: Path,
) -> None:
    values = _base(tmp_path)
    settings = load_settings(environ=values)
    ProfileStore.from_settings(settings).save(
        ConnectionProfile.create(
            "outlook",
            "user@example.com",
            route="browser",
        )
    )

    registry = registry_from_environment(
        connection_type="outlook",
        account="user@example.com",
        token_store=MemoryTokenStore(),
        environ=values,
    )

    assert registry.descriptors() == ()


def test_browser_adapter_uses_account_isolated_profile_and_legacy_fallback(tmp_path: Path) -> None:
    root = tmp_path / "m365"
    values = {
        **_base(tmp_path),
        "SOLERCIA_CONNECT_M365_PROFILE_DIR": str(root),
        "SOLERCIA_CONNECT_BROWSER_FALLBACK": "yes",
    }

    isolated = browser_adapter_from_environment(
        account="User@Example.com",
        connection_type=ConnectionType.OUTLOOK,
        environ=values,
    )
    legacy = browser_adapter_from_environment(
        connection_type=ConnectionType.TEAMS,
        environ=values,
    )
    digest = hashlib.sha256(b"user@example.com").hexdigest()[:16]

    assert isolated._profile_dir == root / digest
    assert isolated._expected_account == "user@example.com"
    assert isolated._connection_type is ConnectionType.OUTLOOK
    assert legacy._profile_dir == root / "legacy"
    assert legacy._expected_account is None
    assert legacy._connection_type is ConnectionType.TEAMS
    assert browser_fallback_enabled_from_environment(environ=values) is True
