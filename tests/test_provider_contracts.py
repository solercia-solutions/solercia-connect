from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from solercia_connect.auth import (
    BasicAuthProvider,
    BearerTokenProvider,
    GlpiCredentials,
)
from solercia_connect.domain import (
    CalendarQuery,
    Capability,
    ConnectorRequest,
    DateWindow,
    IssueQuery,
    MailQuery,
    ReasonCode,
    TicketQuery,
    TranscriptQuery,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import TransportResponse
from solercia_connect.providers.glpi import GlpiAdapter
from solercia_connect.providers.google import GoogleCalendarAdapter, GoogleGmailAdapter
from solercia_connect.providers.jira import JiraAdapter
from solercia_connect.providers.microsoft_graph import MicrosoftGraphAdapter
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import ConnectorRuntime
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport, json_response


def utc_window() -> DateWindow:
    return DateWindow(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
    )


def test_microsoft_graph_normalizes_full_sent_message() -> None:
    transport = FakeTransport(
        json_response(
            200,
            {"id": "user-1", "mail": "user@example.com", "userPrincipalName": "user@example.com"},
        ),
        json_response(
            200,
            {
                "value": [
                    {
                        "id": "m-1",
                        "subject": "Full body",
                        "sentDateTime": "2026-08-10T15:30:00Z",
                        "from": {"emailAddress": {"name": "User", "address": "user@example.com"}},
                        "toRecipients": [
                            {
                                "emailAddress": {
                                    "name": "Recipient",
                                    "address": "recipient@example.com",
                                }
                            }
                        ],
                        "ccRecipients": [],
                        "body": {"contentType": "text", "content": "Complete message body"},
                        "conversationId": "c-1",
                        "hasAttachments": False,
                    }
                ]
            },
        ),
    )
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("graph-secret")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )

    messages = adapter.list_sent_mail(MailQuery(utc_window(), "user@example.com"))

    assert len(messages) == 1
    assert messages[0].body == "Complete message body"
    assert messages[0].to[0].address == "recipient@example.com"
    assert messages[0].provenance.source_id == "m-1"
    assert messages[0].folder == "sent"
    assert messages[0].received_at is None
    assert transport.requests[1].params is not None
    assert "sentDateTime lt 2026-09-01T00:00:00Z" in str(transport.requests[1].params["$filter"])
    assert transport.requests[1].params["$orderby"] == "sentDateTime desc"
    select = str(transport.requests[1].params["$select"])
    assert "internetMessageId" in select
    assert "receivedDateTime" not in select


def test_microsoft_graph_inbox_reads_the_declared_inbox_folder_collection() -> None:
    transport = FakeTransport(
        json_response(
            200,
            {"id": "user-1", "mail": "user@example.com", "userPrincipalName": "user@example.com"},
        ),
        json_response(200, {"value": []}),
    )
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("graph-secret")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )

    adapter.list_inbox_mail(MailQuery(utc_window(), "user@example.com", limit=500))

    assert transport.requests[1].url == (
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
    )
    params = transport.requests[1].params
    assert params is not None
    assert params["$filter"] == (
        "receivedDateTime ge 2026-08-01T00:00:00Z and receivedDateTime lt 2026-09-01T00:00:00Z"
    )
    assert params["$orderby"] == "receivedDateTime asc"
    assert params["$top"] == 100
    assert params["$select"] == (
        "id,subject,sentDateTime,from,sender,toRecipients,ccRecipients,body,"
        "conversationId,hasAttachments,internetMessageId,receivedDateTime"
    )


def test_microsoft_graph_inbox_record_carries_folder_message_id_and_received_instant() -> None:
    transport = FakeTransport(
        json_response(
            200,
            {"id": "user-1", "mail": "user@example.com", "userPrincipalName": "user@example.com"},
        ),
        json_response(
            200,
            {
                "value": [
                    {
                        "id": "m-inbox-1",
                        "subject": "Entrada",
                        "sentDateTime": "2026-08-14T09:31:00Z",
                        "receivedDateTime": "2026-08-14T09:35:00Z",
                        "from": {
                            "emailAddress": {
                                "name": "Remitente",
                                "address": "remitente@example.com",
                            }
                        },
                        "toRecipients": [],
                        "ccRecipients": [],
                        "body": {"contentType": "text", "content": "cuerpo"},
                        "conversationId": "conv-inbox-1",
                        "hasAttachments": False,
                        "internetMessageId": "<inbox-1@contoso.com>",
                    }
                ]
            },
        ),
    )
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("graph-secret")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )

    messages = adapter.list_inbox_mail(MailQuery(utc_window(), "user@example.com"))

    assert len(messages) == 1
    message = messages[0]
    assert message.folder == "inbox"
    assert message.internet_message_id == "<inbox-1@contoso.com>"
    assert message.received_at is not None
    assert message.received_at != message.sent_at
    assert message.provenance.endpoint == "/me/mailFolders/inbox/messages"


def test_microsoft_graph_inbox_refuses_a_mailbox_that_is_not_the_authenticated_account() -> None:
    transport = FakeTransport(
        json_response(200, {"id": "user-1", "mail": "user@example.com"}),
    )
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("graph-secret")),
        expected_account="user@example.com",
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.list_inbox_mail(MailQuery(utc_window(), "otro@example.com"))

    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert len(transport.requests) == 1


def test_microsoft_graph_inbox_without_sent_instant_fails_closed() -> None:
    transport = FakeTransport(
        json_response(
            200,
            {"id": "user-1", "mail": "user@example.com", "userPrincipalName": "user@example.com"},
        ),
        json_response(
            200,
            {
                "value": [
                    {
                        "id": "m-inbox-2",
                        "subject": "Sin instante de envío",
                        "receivedDateTime": "2026-08-14T09:35:00Z",
                        "from": {"emailAddress": {"address": "remitente@example.com"}},
                        "toRecipients": [],
                        "ccRecipients": [],
                        "body": {"contentType": "text", "content": "cuerpo"},
                        "conversationId": "conv-inbox-2",
                        "hasAttachments": False,
                        "internetMessageId": "<inbox-2@contoso.com>",
                    }
                ]
            },
        ),
    )
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("graph-secret")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.list_inbox_mail(MailQuery(utc_window(), "user@example.com"))

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


def test_microsoft_graph_calendar_filters_cancelled_and_unaccepted() -> None:
    transport = FakeTransport(
        json_response(200, {"id": "u", "mail": "user@example.com"}),
        json_response(
            200,
            {
                "value": [
                    _graph_event("accepted", False, "accepted"),
                    _graph_event("declined", False, "declined"),
                    _graph_event("cancelled", True, "accepted"),
                ]
            },
        ),
    )
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("token")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )

    events = adapter.list_events(CalendarQuery(utc_window(), account_id="user@example.com"))

    assert [event.id for event in events] == ["accepted"]
    assert events[0].response_status == "accepted"


def test_microsoft_graph_lists_and_downloads_known_meeting_transcripts() -> None:
    vtt = (
        b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<v Alice>Hello</v>\n\n"
        b"00:00:02.000 --> 00:00:03.500\n<v Bob>World</v>\n"
    )
    transport = FakeTransport(
        json_response(200, {"id": "u", "mail": "user@example.com"}),
        json_response(200, {"value": [{"id": "transcript-1"}]}),
        TransportResponse(200, {"Content-Type": "text/vtt"}, vtt),
    )
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("token")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )

    records = adapter.execute(
        ConnectorRequest.create(
            "microsoft.graph",
            Capability.MEETING_TRANSCRIPTS_LIST,
            TranscriptQuery("meeting/id", account_id="user@example.com"),
        )
    )

    assert Capability.MEETING_TRANSCRIPTS_LIST in adapter.descriptor.capabilities
    assert len(records) == 1
    assert records[0].meeting_id == "meeting/id"
    assert records[0].content_format == "text/vtt"
    assert records[0].speaker_count == 2
    assert records[0].last_cue_seconds == 3.5
    assert transport.requests[1].url.endswith("/me/onlineMeetings/meeting%2Fid/transcripts")
    assert transport.requests[2].url.endswith(
        "/me/onlineMeetings/meeting%2Fid/transcripts/transcript-1/content"
    )


def test_teams_transcript_input_and_tenant_policy_fail_closed_without_raw_body() -> None:
    adapter = MicrosoftGraphAdapter(
        FakeTransport(),
        BearerTokenProvider(SecretValue("token")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )
    with pytest.raises(ConnectorFailure) as invalid:
        adapter.execute(
            ConnectorRequest.create(
                "microsoft.graph",
                Capability.MEETING_TRANSCRIPTS_LIST,
                TranscriptQuery(None, account_id="user@example.com"),
            )
        )
    assert invalid.value.reason is ReasonCode.INVALID_INPUT

    transport = FakeTransport(
        json_response(200, {"id": "u", "mail": "user@example.com"}),
        TransportResponse(
            403,
            {"Content-Type": "application/json"},
            b'{"error":{"code":"Forbidden","message":"raw tenant policy body"}}',
        ),
    )
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("token")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )
    with pytest.raises(ConnectorFailure) as blocked:
        adapter.execute(
            ConnectorRequest.create(
                "microsoft.graph",
                Capability.MEETING_TRANSCRIPTS_LIST,
                TranscriptQuery("meeting-1", account_id="user@example.com"),
            )
        )

    assert blocked.value.reason is ReasonCode.TENANT_POLICY_BLOCKED
    assert "raw tenant policy body" not in blocked.value.safe_message


def test_gmail_lists_then_gets_full_mime_message() -> None:
    body = base64.urlsafe_b64encode(b"Complete Gmail body").decode().rstrip("=")
    sent_ms = str(int(datetime(2026, 8, 10, 15, tzinfo=UTC).timestamp() * 1000))
    transport = FakeTransport(
        json_response(200, {"emailAddress": "user@example.com"}),
        json_response(200, {"messages": [{"id": "g-1", "threadId": "t-1"}]}),
        json_response(
            200,
            {
                "id": "g-1",
                "threadId": "t-1",
                "internalDate": sent_ms,
                "payload": {
                    "mimeType": "multipart/alternative",
                    "headers": [
                        {"name": "Subject", "value": "Gmail subject"},
                        {"name": "From", "value": "User <user@example.com>"},
                        {"name": "To", "value": "Recipient <recipient@example.com>"},
                    ],
                    "body": {},
                    "parts": [{"mimeType": "text/plain", "filename": "", "body": {"data": body}}],
                },
            },
        ),
    )
    adapter = GoogleGmailAdapter(
        transport,
        BearerTokenProvider(SecretValue("google-secret")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )

    messages = adapter.list_sent_mail(MailQuery(utc_window(), "user@example.com"))

    assert len(messages) == 1
    assert messages[0].body == "Complete Gmail body"
    assert messages[0].body_format == "text"
    assert messages[0].has_attachments is False
    assert transport.requests[1].params is not None
    assert str(transport.requests[1].params["q"]).startswith("from:me after:")
    assert transport.requests[1].params["labelIds"] == "SENT"
    assert transport.requests[2].params == {"format": "full"}


def test_google_calendar_normalizes_and_filters_self_response() -> None:
    transport = FakeTransport(
        json_response(200, {"id": "USER@Example.COM"}),
        json_response(
            200,
            {
                "items": [
                    _google_event("accepted", "accepted"),
                    _google_event("pending", "needsAction"),
                ]
            },
        ),
    )
    adapter = GoogleCalendarAdapter(
        transport,
        BearerTokenProvider(SecretValue("google-secret")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )

    events = adapter.list_events(
        CalendarQuery(utc_window(), "user@example.com", "User@example.com")
    )

    assert [event.id for event in events] == ["accepted"]
    assert events[0].is_online is True
    assert events[0].provenance.account_id == "user@example.com"
    assert transport.requests[1].params is not None
    assert transport.requests[1].params["singleEvents"] == "true"


@pytest.mark.parametrize(
    "identity_payload",
    [
        {},
        {"id": "user@example.com and other@example.com"},
        {"id": "other@example.com"},
    ],
    ids=["missing", "ambiguous", "mismatched"],
)
def test_google_calendar_rejects_unverified_identity_before_event_read(
    identity_payload: dict[str, object],
) -> None:
    transport = FakeTransport(
        json_response(200, identity_payload),
        json_response(200, {"items": [_google_event("must-not-read", "accepted")]}),
    )
    adapter = GoogleCalendarAdapter(
        transport,
        BearerTokenProvider(SecretValue("google-secret")),
        expected_account=None,
        clock=lambda: NOW,
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.list_events(CalendarQuery(utc_window(), "primary", account_id="user@example.com"))

    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert len(transport.requests) == 1
    assert transport.requests[0].url.endswith("/users/me/calendarList/primary")


def test_jira_uses_enhanced_jql_and_normalizes_completed_issue() -> None:
    transport = FakeTransport(
        json_response(200, {"accountId": "acct-1", "displayName": "User"}),
        json_response(
            200,
            {
                "isLast": True,
                "issues": [
                    {
                        "id": "10001",
                        "key": "CP-10",
                        "fields": {
                            "summary": "Fix connector",
                            "status": {
                                "name": "Done",
                                "statusCategory": {"name": "Done"},
                            },
                            "issuetype": {"name": "Bug"},
                            "assignee": {"displayName": "User"},
                            "reporter": {"displayName": "Reporter"},
                            "priority": {"name": "High"},
                            "project": {"name": "Connect"},
                            "created": "2026-08-01T09:00:00.000+0000",
                            "updated": "2026-08-10T09:00:00.000+0000",
                            "resolutiondate": "2026-08-10T09:00:00.000+0000",
                        },
                    }
                ],
            },
        ),
    )
    adapter = JiraAdapter(
        transport,
        BasicAuthProvider("user@example.com", SecretValue("jira-secret")),
        base_url="https://example.atlassian.net",
        expected_account_id="acct-1",
        clock=lambda: NOW,
    )

    issues = adapter.search_issues(IssueQuery(utc_window(), assignee="acct-1"))

    assert len(issues) == 1
    assert issues[0].key == "CP-10"
    assert issues[0].status_category == "Done"
    search_request = transport.requests[1]
    assert search_request.url.endswith("/rest/api/3/search/jql")
    assert search_request.json_body is not None
    assert 'resolved < "2026-09-01"' in str(search_request.json_body["jql"])


def test_glpi_validates_fields_normalizes_ticket_and_closes_session() -> None:
    options = {key: {"name": f"Field {key}"} for key in ("1", "2", "7", "12", "15", "16")}
    transport = FakeTransport(
        json_response(200, {"session_token": "session-secret"}),
        json_response(200, options),
        json_response(
            200,
            {
                "data": [
                    {
                        "2": 42,
                        "1": "<b>Printer issue</b>",
                        "15": "2026-08-10 09:00:00",
                        "16": "2026-08-10 10:00:00",
                        "12": 5,
                        "7": "Hardware",
                    }
                ]
            },
        ),
        json_response(200, {}),
    )
    adapter = GlpiAdapter(
        transport,
        GlpiCredentials(SecretValue("app-secret"), SecretValue("user-secret")),
        base_url="https://glpi.example.com/apirest.php",
        clock=lambda: NOW,
    )
    window = DateWindow(
        datetime.fromisoformat("2026-08-01T00:00:00-05:00"),
        datetime.fromisoformat("2026-09-01T00:00:00-05:00"),
    )

    tickets = adapter.search_tickets(TicketQuery(window))

    assert len(tickets) == 1
    assert tickets[0].id == "42"
    assert tickets[0].title == "Printer issue"
    assert tickets[0].status == "resolved"
    assert transport.requests[-1].url.endswith("/killSession")
    search_request = transport.requests[2]
    assert search_request.params is not None
    forced = [search_request.params[f"forcedisplay[{index}]"] for index in range(6)]
    assert forced == ["2", "1", "15", "16", "12", "7"]


@pytest.mark.parametrize(
    ("adapter_kind", "base_url"),
    [
        ("microsoft", "http://graph.microsoft.com/v1.0"),
        ("microsoft", "https://graph.microsoft.com.evil/v1.0"),
        ("microsoft", "https://graph.microsoft.com/beta"),
        ("microsoft", "https://attacker@graph.microsoft.com/v1.0"),
        ("microsoft", "https://graph.microsoft.com:444/v1.0"),
        ("gmail", "http://gmail.googleapis.com/gmail/v1"),
        ("gmail", "https://gmail.googleapis.com.evil/gmail/v1"),
        ("gmail", "https://gmail.googleapis.com/drive/v3"),
        ("calendar", "http://www.googleapis.com/calendar/v3"),
        ("calendar", "https://www.googleapis.com.evil/calendar/v3"),
        ("calendar", "https://www.googleapis.com/drive/v3"),
    ],
)
def test_official_adapters_reject_untrusted_base_urls(
    adapter_kind: str,
    base_url: str,
) -> None:
    transport = FakeTransport()
    auth = BearerTokenProvider(SecretValue("not-sent"))

    with pytest.raises(
        ValueError,
        match=r"official endpoint|absolute HTTPS|credentials|default HTTPS port",
    ):
        if adapter_kind == "microsoft":
            MicrosoftGraphAdapter(transport, auth, base_url=base_url)
        elif adapter_kind == "gmail":
            GoogleGmailAdapter(transport, auth, base_url=base_url)
        else:
            GoogleCalendarAdapter(transport, auth, base_url=base_url)

    assert transport.requests == []


@pytest.mark.parametrize(
    "next_link",
    [
        "http://graph.microsoft.com/v1.0/me/messages?$skiptoken=unsafe",
        "https://graph.microsoft.com.evil/v1.0/me/messages?$skiptoken=unsafe",
        "https://evil.example/messages?$skiptoken=unsafe",
        "https://attacker@graph.microsoft.com/v1.0/me/messages?$skiptoken=unsafe",
    ],
)
def test_microsoft_graph_rejects_untrusted_next_link_before_sending_credentials(
    next_link: str,
) -> None:
    transport = FakeTransport(
        json_response(200, {"mail": "user@example.com"}),
        json_response(200, {"value": [], "@odata.nextLink": next_link}),
    )
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("graph-secret")),
        expected_account="user@example.com",
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.list_sent_mail(MailQuery(utc_window(), "user@example.com", limit=2))

    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert len(transport.requests) == 2
    assert all(
        request.url.startswith("https://graph.microsoft.com/") for request in transport.requests
    )


def test_microsoft_graph_follows_same_origin_next_link() -> None:
    next_link = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=safe"
    transport = FakeTransport(
        json_response(200, {"mail": "user@example.com"}),
        json_response(200, {"value": [], "@odata.nextLink": next_link}),
        json_response(200, {"value": [_graph_message("page-2")]}),
    )
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("graph-secret")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )

    messages = adapter.list_sent_mail(MailQuery(utc_window(), "user@example.com", limit=1))

    assert [message.id for message in messages] == ["page-2"]
    assert transport.requests[2].url == next_link
    assert transport.requests[2].params is None


def test_glpi_teardown_failure_after_success_produces_failure_receipt() -> None:
    options = {key: {} for key in ("1", "2", "7", "12", "15", "16")}
    transport = FakeTransport(
        json_response(200, {"session_token": "session-secret"}),
        json_response(200, options),
        json_response(200, {"data": []}),
        json_response(401, {}),
    )
    adapter = GlpiAdapter(
        transport,
        GlpiCredentials(SecretValue("app-secret"), SecretValue("user-secret")),
        base_url="https://glpi.example.com/apirest.php",
        clock=lambda: NOW,
    )
    registry = ProviderRegistry()
    registry.register(adapter)
    request = ConnectorRequest.create(
        "glpi.rest",
        Capability.TICKETS_SEARCH,
        TicketQuery(utc_window()),
    )

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(request)

    assert outcome.ok is False
    assert outcome.records == ()
    assert outcome.receipt.final_reason is ReasonCode.INVALID_CREDENTIALS
    assert all(step.reason is not ReasonCode.SUCCESS for step in outcome.receipt.steps)


def test_glpi_preserves_primary_failure_when_teardown_also_fails() -> None:
    transport = FakeTransport(
        json_response(200, {"session_token": "session-secret"}),
        json_response(200, {}),
        json_response(401, {}),
    )
    adapter = GlpiAdapter(
        transport,
        GlpiCredentials(SecretValue("app-secret"), SecretValue("user-secret")),
        base_url="https://glpi.example.com/apirest.php",
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.health()

    assert captured.value.reason is ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE
    assert isinstance(captured.value.__cause__, ConnectorFailure)
    assert captured.value.__cause__.reason is ReasonCode.INVALID_CREDENTIALS
    assert captured.value.__notes__ == ["GLPI session teardown also failed (invalid_credentials)"]


def test_glpi_unknown_teardown_failure_is_typed_after_success() -> None:
    options = {key: {} for key in ("1", "2", "7", "12", "15", "16")}
    transport = FakeTransport(
        json_response(200, {"session_token": "session-secret"}),
        json_response(200, options),
        json_response(200, {"data": []}),
        RuntimeError("unsafe teardown detail"),
    )
    adapter = GlpiAdapter(
        transport,
        GlpiCredentials(SecretValue("app-secret"), SecretValue("user-secret")),
        base_url="https://glpi.example.com/apirest.php",
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.search_tickets(TicketQuery(utc_window()))

    assert captured.value.reason is ReasonCode.UNKNOWN_ERROR
    assert "unsafe teardown detail" not in str(captured.value)


def _graph_event(event_id: str, cancelled: bool, response: str) -> dict[str, object]:
    return {
        "id": event_id,
        "subject": event_id,
        "start": {"dateTime": "2026-08-10T10:00:00Z"},
        "end": {"dateTime": "2026-08-10T11:00:00Z"},
        "organizer": {"emailAddress": {"address": "organizer@example.com"}},
        "attendees": [],
        "responseStatus": {"response": response},
        "isOnlineMeeting": True,
        "isCancelled": cancelled,
        "isOrganizer": False,
    }


def _graph_message(message_id: str) -> dict[str, object]:
    return {
        "id": message_id,
        "subject": message_id,
        "sentDateTime": "2026-08-10T15:30:00Z",
        "from": {"emailAddress": {"address": "user@example.com"}},
        "toRecipients": [],
        "ccRecipients": [],
        "body": {"contentType": "text", "content": "Complete message body"},
        "conversationId": "conversation-1",
        "hasAttachments": False,
    }


def _google_event(event_id: str, response: str) -> dict[str, object]:
    return {
        "id": event_id,
        "summary": event_id,
        "start": {"dateTime": "2026-08-10T10:00:00Z"},
        "end": {"dateTime": "2026-08-10T11:00:00Z"},
        "organizer": {"email": "organizer@example.com"},
        "attendees": [{"email": "user@example.com", "self": True, "responseStatus": response}],
        "hangoutLink": "https://meet.google.com/example",
        "status": "confirmed",
    }
