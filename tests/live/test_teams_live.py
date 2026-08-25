from __future__ import annotations

import pytest

from solercia_connect.application import run_application
from solercia_connect.config import ConnectionType
from tests.live.conftest import require_live

pytestmark = pytest.mark.live


def test_teams_known_meeting_transcript_contract_live() -> None:
    context = require_live(
        ConnectionType.TEAMS,
        requires_window=False,
        requires_meeting_id=True,
    )
    outcome = run_application(
        {
            "type": context.type.value,
            "account": context.account,
            "capability": "meeting.transcript.get",
            "meeting_id": context.meeting_id,
            "limit": 1,
            "allow_browser_fallback": False,
        }
    )

    assert outcome.ok, outcome.to_dict()
    assert outcome.route == "official"
