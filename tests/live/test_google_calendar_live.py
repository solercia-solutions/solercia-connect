from __future__ import annotations

import pytest

from solercia_connect.application import run_application
from solercia_connect.config import ConnectionType
from tests.live.conftest import require_live

pytestmark = pytest.mark.live


def test_google_calendar_events_contract_live() -> None:
    context = require_live(ConnectionType.GOOGLE_CALENDAR, requires_window=True)
    outcome = run_application(
        {
            "type": context.type.value,
            "account": context.account,
            "capability": "calendar.events.list",
            "start": context.start,
            "end": context.end,
            "limit": 5,
        }
    )

    assert outcome.ok, outcome.to_dict()
    assert outcome.route == "official"
