from __future__ import annotations

import os

import pytest

from solercia_connect.application import run_application
from solercia_connect.config import ConnectionType
from tests.live.conftest import require_live

pytestmark = pytest.mark.live


def test_search_console_sitemaps_contract_live() -> None:
    context = require_live(ConnectionType.SEARCH_CONSOLE)
    site_url = os.environ.get("SOLERCIA_CONNECT_LIVE_SITE_URL", "").strip()
    if not site_url:
        pytest.skip("SOLERCIA_CONNECT_LIVE_SITE_URL is required")
    outcome = run_application(
        {
            "type": context.type.value,
            "account": context.account,
            "capability": "search.sitemaps.list",
            "site_url": site_url,
        }
    )

    assert outcome.ok, outcome.to_dict()
    assert outcome.route == "official"
