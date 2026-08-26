from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import solercia_connect.application as application
from solercia_connect.config import ConnectionType
from solercia_connect.domain import (
    CalendarQuery,
    Capability,
    DateWindow,
    ExecutionOutcome,
    ExecutionReceipt,
    IssueQuery,
    MailQuery,
    ReasonCode,
    ReceiptStatus,
    RouteKind,
    StepOutcome,
    TicketQuery,
    TraceStep,
    TranscriptQuery,
)
from solercia_connect.errors import ConnectorFailure
from tests.fakes import NOW, mail_record


def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "type": "outlook",
        "account": "user@example.com",
        "capability": "mail.sent.list",
        "start": "2026-08-01T00:00:00+00:00",
        "end": "2026-09-01T00:00:00+00:00",
        "limit": 50,
        **overrides,
    }


def _receipt(
    *,
    ok: bool,
    reason: ReasonCode,
    route: RouteKind | None,
) -> ExecutionReceipt:
    return ExecutionReceipt(
        "receipt-1",
        "request-1",
        "microsoft.graph",
        Capability.MAIL_SENT_LIST,
        ReceiptStatus.SUCCESS if ok else ReceiptStatus.FAILURE,
        route,
        reason,
        NOW,
        (
            TraceStep(
                1,
                route or RouteKind.OFFICIAL,
                "microsoft.graph",
                1,
                StepOutcome.SUCCESS if ok else StepOutcome.FAILURE,
                reason,
                "safe step detail",
            ),
        ),
    )


def _runtime_for(outcome: ExecutionOutcome) -> type[Any]:
    class FakeRuntime:
        def __init__(self, registry: object) -> None:
            assert registry == "registry"

        def execute(self, request: object, *, browser_fallback_enabled: bool) -> ExecutionOutcome:
            assert request is not None
            assert isinstance(browser_fallback_enabled, bool)
            return outcome

    return FakeRuntime


def _capturing_runtime(outcome: ExecutionOutcome, captured: dict[str, Any]) -> type[Any]:
    """Fake runtime that records the built request so a test can inspect its payload."""

    class CapturingRuntime:
        def __init__(self, registry: object) -> None:
            assert registry == "registry"

        def execute(self, request: object, *, browser_fallback_enabled: bool) -> ExecutionOutcome:
            captured["request"] = request
            assert isinstance(browser_fallback_enabled, bool)
            return outcome

    return CapturingRuntime


def _inbox_receipt() -> ExecutionReceipt:
    return ExecutionReceipt(
        "receipt-inbox",
        "request-inbox",
        "microsoft.graph",
        Capability.MAIL_INBOX_LIST,
        ReceiptStatus.SUCCESS,
        RouteKind.OFFICIAL,
        ReasonCode.SUCCESS,
        NOW,
        (
            TraceStep(
                1,
                RouteKind.OFFICIAL,
                "microsoft.graph",
                1,
                StepOutcome.SUCCESS,
                ReasonCode.SUCCESS,
                "Official adapter completed",
            ),
        ),
    )


def test_run_application_returns_versioned_records_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = ExecutionOutcome(
        True,
        (mail_record(),),
        _receipt(ok=True, reason=ReasonCode.SUCCESS, route=RouteKind.OFFICIAL),
    )
    monkeypatch.setattr(application, "registry_from_environment", lambda **_: "registry")
    monkeypatch.setattr(application, "ConnectorRuntime", _runtime_for(outcome))

    result = application.run_application(_payload())

    assert result.ok is True
    assert result.route == "official"
    assert result.data["records"][0]["schema"] == "solercia.connect/record/v1"
    assert result.receipt == outcome.receipt


def test_run_application_maps_runtime_and_connector_failures_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = ExecutionOutcome(
        False,
        (),
        _receipt(ok=False, reason=ReasonCode.NETWORK_TIMEOUT, route=None),
    )
    monkeypatch.setattr(application, "registry_from_environment", lambda **_: "registry")
    monkeypatch.setattr(application, "ConnectorRuntime", _runtime_for(failed))

    result = application.run_application(_payload(allow_browser_fallback=False))

    assert result.ok is False
    assert result.error is not None and result.error.retryable is True
    assert result.error.message == "safe step detail"

    def denied(**_: object) -> object:
        raise ConnectorFailure(ReasonCode.AUTHORIZATION_DENIED, "Authorization denied")

    monkeypatch.setattr(application, "registry_from_environment", denied)
    denied_result = application.run_application(_payload())
    assert denied_result.error is not None
    assert denied_result.error.code == "authorization_denied"
    assert denied_result.error.cause_category == "runtime"


def test_run_application_maps_unknown_failures_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(**_: object) -> object:
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr(application, "registry_from_environment", broken)
    result = application.run_application(_payload())

    assert result.error is not None and result.error.code == "unknown_error"
    assert "sensitive" not in result.error.message


@pytest.mark.parametrize(
    "payload",
    [
        {},
        _payload(type="unknown"),
        _payload(capability="unknown"),
        _payload(type="gmail", capability="calendar.events.list"),
        _payload(limit=True),
        _payload(limit=1001),
        _payload(allow_browser_fallback="yes"),
        _payload(start=None),
    ],
)
def test_run_application_rejects_invalid_typed_requests_before_runtime(
    payload: dict[str, Any],
) -> None:
    result = application.run_application(payload)

    assert result.status == "invalid_input"
    assert result.error is not None and result.error.cause_category == "input"


def test_inbox_mail_request_for_gmail_is_rejected_before_any_capability_dispatch() -> None:
    result = application.run_application(_payload(type="gmail", capability="mail.inbox.list"))

    assert result.status == "invalid_input"
    assert result.error is not None and result.error.cause_category == "input"


def test_inbox_mail_runs_end_to_end_over_the_official_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    outcome = ExecutionOutcome(
        True,
        (mail_record(folder="inbox", internet_message_id="<abc@contoso.com>", received_at=NOW),),
        _inbox_receipt(),
    )
    monkeypatch.setattr(application, "registry_from_environment", lambda **_: "registry")
    monkeypatch.setattr(application, "ConnectorRuntime", _capturing_runtime(outcome, captured))

    result = application.run_application(_payload(capability="mail.inbox.list", limit=1000))

    assert result.ok is True
    assert result.receipt is not None
    assert result.receipt.capability == Capability.MAIL_INBOX_LIST
    assert result.data is not None
    assert result.data["records"][0]["folder"] == "inbox"
    # The old mail clamp silently reduced any request above 100 to 100; the request built here
    # must now carry the full 1000 the payload asked for, not the stale lower ceiling.
    assert captured["request"].payload.limit == 1000


def test_inbox_mail_over_an_empty_window_reports_zero_records_without_widening_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    outcome = ExecutionOutcome(True, (), _inbox_receipt())
    monkeypatch.setattr(application, "registry_from_environment", lambda **_: "registry")
    monkeypatch.setattr(application, "ConnectorRuntime", _capturing_runtime(outcome, captured))

    result = application.run_application(
        _payload(
            capability="mail.inbox.list",
            start="2026-08-01T00:00:00+00:00",
            end="2026-09-01T00:00:00+00:00",
        )
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["records"] == []
    window = captured["request"].payload.window
    assert window.start.isoformat() == "2026-08-01T00:00:00+00:00"
    assert window.end.isoformat() == "2026-09-01T00:00:00+00:00"


def test_mail_query_accepts_the_thousand_record_limit_and_rejects_more() -> None:
    window = DateWindow(
        datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
        datetime.fromisoformat("2026-09-01T00:00:00+00:00"),
    )

    assert MailQuery(window, limit=1000).limit == 1000
    with pytest.raises(ValueError, match="MailQuery limit must be between 1 and 1000"):
        MailQuery(window, limit=1001)
    with pytest.raises(ValueError, match="MailQuery limit must be between 1 and 1000"):
        MailQuery(window, limit=0)


def test_query_builder_covers_every_provider_contract() -> None:
    window = {
        "start": "2026-08-01T00:00:00+00:00",
        "end": "2026-09-01T00:00:00+00:00",
        "limit": 500,
    }

    mail = application._query(
        ConnectionType.OUTLOOK,
        Capability.MAIL_SENT_LIST,
        "user@example.com",
        window,
    )
    inbox = application._query(
        ConnectionType.OUTLOOK,
        Capability.MAIL_INBOX_LIST,
        "user@example.com",
        window,
    )
    calendar = application._query(
        ConnectionType.GOOGLE_CALENDAR,
        Capability.CALENDAR_EVENTS_LIST,
        "user@example.com",
        {**window, "calendar_id": " team "},
    )
    transcript_list = application._query(
        ConnectionType.TEAMS,
        Capability.MEETING_TRANSCRIPTS_LIST,
        "user@example.com",
        {**window, "meeting_id": "meeting-1"},
    )
    transcript_get = application._query(
        ConnectionType.TEAMS,
        Capability.MEETING_TRANSCRIPT_GET,
        "user@example.com",
        {"meeting_id": "meeting-1", "limit": 50},
    )
    issue = application._query(
        ConnectionType.JIRA,
        Capability.EXTERNAL_ISSUES_SEARCH,
        None,
        {**window, "assignee": "  ", "jql": "status = Done"},
    )
    ticket = application._query(
        ConnectionType.GLPI,
        Capability.TICKETS_SEARCH,
        None,
        window,
    )

    assert isinstance(mail, MailQuery) and mail.limit == 500
    assert isinstance(inbox, MailQuery) and inbox.limit == 500
    assert isinstance(calendar, CalendarQuery) and calendar.calendar_id == " team "
    assert isinstance(transcript_list, TranscriptQuery) and transcript_list.window is not None
    assert isinstance(transcript_get, TranscriptQuery) and transcript_get.window is None
    assert isinstance(issue, IssueQuery) and issue.assignee is None
    assert isinstance(ticket, TicketQuery) and ticket.limit == 500
    with pytest.raises(ValueError, match="MailQuery limit must be between 1 and 1000"):
        MailQuery(mail.window, limit=1001)


def test_query_builder_rejects_wrong_target_missing_text_and_invalid_optionals() -> None:
    base = {
        "start": "2026-08-01T00:00:00+00:00",
        "end": "2026-09-01T00:00:00+00:00",
    }
    with pytest.raises(ValueError, match="GLPI"):
        application._query(
            ConnectionType.GMAIL,
            Capability.TICKETS_SEARCH,
            None,
            base,
        )
    with pytest.raises(ValueError, match="meeting_id"):
        application._query(
            ConnectionType.TEAMS,
            Capability.MEETING_TRANSCRIPT_GET,
            None,
            {},
        )
    with pytest.raises(ValueError, match="Optional text"):
        application._query(
            ConnectionType.JIRA,
            Capability.EXTERNAL_ISSUES_SEARCH,
            None,
            {**base, "assignee": 7},
        )
