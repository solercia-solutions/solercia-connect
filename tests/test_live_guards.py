from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import tests.conftest as root_conftest
from solercia_connect.config import ConnectionProfile, ConnectionType, ProfileStore, load_settings
from tests.live.conftest import require_live


def test_default_secure_store_isolation_is_not_applied_to_explicit_live_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLERCIA_CONNECT_LIVE", "1")
    monkeypatch.delenv("PYTHON_KEYRING_BACKEND", raising=False)

    root_conftest.isolate_secure_store.__wrapped__(monkeypatch)

    assert "PYTHON_KEYRING_BACKEND" not in os.environ


def test_live_opt_in_with_profile_but_missing_token_store_skips_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {
        "SOLERCIA_CONNECT_LIVE": "1",
        "SOLERCIA_CONNECT_LIVE_TYPE": "outlook",
        "SOLERCIA_CONNECT_LIVE_ACCOUNT": "user@example.com",
        "SOLERCIA_CONNECT_LIVE_START": "2026-08-01T00:00:00+00:00",
        "SOLERCIA_CONNECT_LIVE_END": "2026-09-01T00:00:00+00:00",
        "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID": "microsoft-client",
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings = load_settings(environ=env)
    ProfileStore.from_settings(settings).save(
        ConnectionProfile.create("outlook", "user@example.com")
    )

    with pytest.raises(pytest.skip.Exception) as captured:
        require_live(ConnectionType.OUTLOOK, requires_window=True)

    diagnostic = json.loads(str(captured.value))
    assert diagnostic["schema"] == "solercia.connect/live-skip/v1"
    assert diagnostic["code"] == "live_token_store_unavailable"
    assert diagnostic["missing_configuration"] == ["secure_oauth_token_store"]
    assert "microsoft-client" not in str(captured.value)
