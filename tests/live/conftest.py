from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime

import pytest

from solercia_connect.config import (
    ConnectionType,
    ProfileStore,
    load_settings,
    normalize_account,
    target_spec,
)
from solercia_connect.oauth import KeyringOAuthTokenStore


@dataclass(frozen=True, slots=True)
class LiveContext:
    type: ConnectionType
    account: str
    start: str | None
    end: str | None
    meeting_id: str | None


def require_live(
    expected_type: ConnectionType,
    *,
    requires_window: bool,
    requires_meeting_id: bool = False,
) -> LiveContext:
    if os.getenv("SOLERCIA_CONNECT_LIVE") != "1":
        pytest.skip(_diagnostic("live_opt_in_required", ("SOLERCIA_CONNECT_LIVE",)))
    selected = os.getenv("SOLERCIA_CONNECT_LIVE_TYPE", "").strip()
    if selected != expected_type.value:
        pytest.skip(
            _diagnostic(
                "different_live_provider_selected",
                ("SOLERCIA_CONNECT_LIVE_TYPE",),
            )
        )
    raw_account = os.getenv("SOLERCIA_CONNECT_LIVE_ACCOUNT", "").strip()
    missing = [
        name
        for name in target_spec(expected_type).required_configuration
        if not os.getenv(name, "").strip()
    ]
    if not raw_account:
        missing.append("SOLERCIA_CONNECT_LIVE_ACCOUNT")
    start = os.getenv("SOLERCIA_CONNECT_LIVE_START", "").strip() or None
    end = os.getenv("SOLERCIA_CONNECT_LIVE_END", "").strip() or None
    if requires_window:
        if start is None:
            missing.append("SOLERCIA_CONNECT_LIVE_START")
        if end is None:
            missing.append("SOLERCIA_CONNECT_LIVE_END")
    meeting_id = os.getenv("SOLERCIA_CONNECT_LIVE_MEETING_ID", "").strip() or None
    if requires_meeting_id and meeting_id is None:
        missing.append("SOLERCIA_CONNECT_LIVE_MEETING_ID")
    if missing:
        pytest.skip(_diagnostic("live_prerequisites_missing", tuple(sorted(set(missing)))))
    account = normalize_account(raw_account)
    if start is not None and end is not None:
        start_value = datetime.fromisoformat(start)
        end_value = datetime.fromisoformat(end)
        if start_value.tzinfo is None or end_value.tzinfo is None or start_value >= end_value:
            pytest.skip(
                _diagnostic(
                    "live_range_invalid",
                    ("SOLERCIA_CONNECT_LIVE_START", "SOLERCIA_CONNECT_LIVE_END"),
                )
            )
    settings = load_settings()
    try:
        profile = ProfileStore.from_settings(settings).get(expected_type, account)
    except ValueError:
        pytest.skip(_diagnostic("live_connection_profile_unsafe", ("local_connection_profile",)))
    if profile is None or profile.route != "official":
        pytest.skip(_diagnostic("live_connection_profile_missing", ("local_connection_profile",)))
    if not _token_store_ready(expected_type, account):
        pytest.skip(_diagnostic("live_token_store_unavailable", ("secure_oauth_token_store",)))
    return LiveContext(expected_type, account, start, end, meeting_id)


def _token_store_ready(type_value: ConnectionType, account: str) -> bool:
    try:
        tokens = KeyringOAuthTokenStore().load(f"{type_value.value}:{account}")
    except Exception:
        return False
    if tokens is None:
        return False
    required = set(target_spec(type_value).oauth_scopes)
    return required.issubset(tokens.scopes)


def _diagnostic(code: str, missing: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "schema": "solercia.connect/live-skip/v1",
            "code": code,
            "missing_configuration": list(missing),
        },
        sort_keys=True,
    )
