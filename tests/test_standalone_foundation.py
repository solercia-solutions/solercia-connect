from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from solercia_connect.config import (
    ConnectionProfile,
    ConnectionType,
    ProfileStore,
    load_settings,
    normalize_account,
)
from solercia_connect.envelope import ErrorDetail, ResultEnvelope
from solercia_connect.output import SecureOutputError, write_secure_json


def test_connection_types_are_ordered_and_accounts_are_validated_before_use() -> None:
    assert [item.value for item in ConnectionType] == [
        "outlook",
        "teams",
        "gmail",
        "google-calendar",
        "jira",
        "glpi",
    ]
    assert ConnectionType.parse("GOOGLE_CALENDAR") is ConnectionType.GOOGLE_CALENDAR
    assert normalize_account(" User@Example.com ") == "user@example.com"

    with pytest.raises(ValueError, match="account"):
        normalize_account("not-an-account")
    with pytest.raises(ValueError, match="provider type"):
        ConnectionType.parse("sharepoint")


def test_settings_only_load_an_explicit_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    implicit = tmp_path / ".env"
    implicit.write_text("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID=implicit\n")
    monkeypatch.chdir(tmp_path)

    assert load_settings(environ={}).microsoft_client_id is None

    explicit = tmp_path / "connect.env"
    explicit.write_text(
        "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID=file-client\n"
        "SOLERCIA_CONNECT_MICROSOFT_TENANT=organizations\n"
        "SOLERCIA_CONNECT_BROWSER_FALLBACK=true\n"
    )
    settings = load_settings(
        environ={
            "SOLERCIA_CONNECT_ENV_FILE": str(explicit),
            "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID": "environment-client",
        }
    )

    assert settings.microsoft_client_id == "environment-client"
    assert settings.microsoft_tenant == "organizations"
    assert settings.browser_fallback_enabled is True
    assert settings.env_file == explicit


def test_profile_store_is_private_atomic_and_contains_no_secrets(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "config")
    profile = ConnectionProfile.create(ConnectionType.OUTLOOK, "User@Example.com")

    store.save(profile)
    loaded = store.get(ConnectionType.OUTLOOK, "user@example.com")

    assert loaded == profile
    assert store.list() == (profile,)
    profile_path = store.path_for(ConnectionType.OUTLOOK, "user@example.com")
    assert profile_path.stat().st_mode & 0o777 == 0o600
    assert profile_path.parent.stat().st_mode & 0o777 == 0o700
    payload = profile_path.read_text()
    for forbidden in ("access_token", "refresh_token", "client_secret", "device_code"):
        assert forbidden not in payload

    assert store.delete(ConnectionType.OUTLOOK, "user@example.com") is True
    assert store.get(ConnectionType.OUTLOOK, "user@example.com") is None


def test_profile_store_refuses_symlink_targets(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "config")
    profile = ConnectionProfile.create(ConnectionType.GMAIL, "user@example.com")
    path = store.path_for(profile.type, profile.account)
    store.root.mkdir(mode=0o700)
    path.parent.mkdir(mode=0o700)
    target = tmp_path / "outside.json"
    target.write_text("keep")
    path.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic link"):
        store.save(profile)
    assert target.read_text() == "keep"


def test_profile_store_and_output_refuse_symlinked_parent_components(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    store = ProfileStore(alias / "profiles")
    profile = ConnectionProfile.create(ConnectionType.OUTLOOK, "user@example.com")

    with pytest.raises(ValueError, match="contains a symbolic link"):
        store.save(profile)
    with pytest.raises(SecureOutputError, match="contains a symbolic link"):
        write_secure_json(alias / "output" / "result.json", {"ok": True})

    assert list(outside.iterdir()) == []


def test_shared_envelope_has_stable_fields_and_safe_structured_error() -> None:
    envelope = ResultEnvelope.failure(
        operation="connect",
        status="configuration_error",
        provider="outlook",
        account="user@example.com",
        error=ErrorDetail(
            code="configuration_invalid",
            message="Microsoft client ID is missing",
            retryable=False,
            missing_configuration=("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID",),
            remediation=("Set SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID and run the command again.",),
            cause_category="configuration",
        ),
    )
    payload = envelope.to_dict()

    assert list(payload) == [
        "schema",
        "ok",
        "operation",
        "status",
        "provider",
        "account",
        "route",
        "data",
        "receipt",
        "error",
    ]
    assert payload["schema"] == "solercia.connect/v1"
    assert payload["error"]["missing_configuration"] == ["SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID"]


def test_secure_output_is_mode_0600_atomic_and_never_overwrites_by_default(
    tmp_path: Path,
) -> None:
    target = tmp_path / "result.json"
    write_secure_json(target, {"ok": True})

    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text()) == {"ok": True}
    with pytest.raises(SecureOutputError, match="already exists"):
        write_secure_json(target, {"ok": False})

    write_secure_json(target, {"ok": False}, overwrite=True)
    assert json.loads(target.read_text()) == {"ok": False}

    target.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    os.symlink(outside, target)
    with pytest.raises(SecureOutputError, match="symbolic link"):
        write_secure_json(target, {"ok": True}, overwrite=True)
    assert outside.read_text() == "keep"
