"""Gmail inbox read, `mail.inbox.list`, over the official route only (T9.1, decision D5).

The read mirrors the sent listing: one identity proof, one deterministic `q` for the inbox and the
end-exclusive window, bounded paging, and `messages.get` in full format. The record is the same
normalized mail record the Microsoft inbox returns, with the received instant taken from Gmail's
`internalDate`.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from solercia_connect import cli
from solercia_connect.auth import BearerTokenProvider
from solercia_connect.config import ConnectionType, target_spec
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    DateWindow,
    MailQuery,
    ProviderFamily,
    ReasonCode,
)
from solercia_connect.envelope import versioned_record
from solercia_connect.errors import ConnectorFailure
from solercia_connect.matrix import CAPABILITY_MATRIX
from solercia_connect.ports import TransportResponse
from solercia_connect.providers.google import GoogleGmailAdapter
from solercia_connect.providers.microsoft_graph import MicrosoftGraphAdapter
from solercia_connect.runtime import BROWSER_FALLBACK_CAPABILITIES
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport, json_response

ACCOUNT = "user@example.com"
BASE = "https://gmail.googleapis.com/gmail/v1"
START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 9, 1, tzinfo=UTC)
WINDOW = DateWindow(START, END)


def _ms(value: datetime) -> str:
    return str(int(value.timestamp() * 1000))


def _message(
    message_id: str,
    received: datetime,
    *,
    date_header: str | None = "Mon, 10 Aug 2026 09:31:00 +0000",
    message_header_id: str | None = "<inbox-1@example.net>",
) -> dict[str, Any]:
    body = base64.urlsafe_b64encode(b"Cuerpo completo").decode().rstrip("=")
    headers = [
        {"name": "Subject", "value": "Entrada"},
        {"name": "From", "value": "Remitente <remitente@example.net>"},
        {"name": "To", "value": "User <user@example.com>"},
        {"name": "Cc", "value": "copia@example.net"},
    ]
    if date_header is not None:
        headers.append({"name": "Date", "value": date_header})
    if message_header_id is not None:
        headers.append({"name": "Message-ID", "value": message_header_id})
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "internalDate": _ms(received),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "body": {},
            "parts": [{"mimeType": "text/plain", "filename": "", "body": {"data": body}}],
        },
    }


def _adapter(*responses: TransportResponse | Exception) -> tuple[GoogleGmailAdapter, FakeTransport]:
    transport = FakeTransport(*responses)
    adapter = GoogleGmailAdapter(
        transport,
        BearerTokenProvider(SecretValue("google-secret")),
        expected_account=ACCOUNT,
        clock=lambda: NOW,
    )
    return adapter, transport


def _profile() -> TransportResponse:
    return json_response(200, {"emailAddress": ACCOUNT})


def test_gmail_inbox_lists_with_the_exact_query_then_gets_each_full_message() -> None:
    received = datetime(2026, 8, 10, 9, 35, tzinfo=UTC)
    adapter, transport = _adapter(
        _profile(),
        json_response(200, {"messages": [{"id": "g-1", "threadId": "thread-g-1"}]}),
        json_response(200, _message("g-1", received)),
    )

    messages = adapter.list_inbox_mail(MailQuery(WINDOW, ACCOUNT, limit=250))

    assert [request.url for request in transport.requests] == [
        f"{BASE}/users/me/profile",
        f"{BASE}/users/me/messages",
        f"{BASE}/users/me/messages/g-1",
    ]
    after = int(START.timestamp()) - 1
    before = int(END.timestamp())
    assert transport.requests[1].params == {
        "q": f"in:inbox after:{after} before:{before}",
        "labelIds": "INBOX",
        "maxResults": 100,
    }
    assert transport.requests[2].params == {"format": "full"}
    assert {request.method for request in transport.requests} == {"GET"}
    assert len(messages) == 1
    message = messages[0]
    assert message.folder == "inbox"
    assert message.received_at == received
    assert message.sent_at == datetime(2026, 8, 10, 9, 31, tzinfo=UTC)
    assert message.internet_message_id == "<inbox-1@example.net>"
    assert message.subject == "Entrada"
    assert message.body == "Cuerpo completo"
    assert message.sender is not None and message.sender.address == "remitente@example.net"
    assert [address.address for address in message.cc] == ["copia@example.net"]
    assert message.conversation_id == "thread-g-1"
    assert message.provenance.account_id == ACCOUNT
    assert message.provenance.endpoint == "/users/me/messages/{id}"


def test_gmail_inbox_keeps_the_window_strict_and_end_exclusive() -> None:
    adapter, _ = _adapter(
        _profile(),
        json_response(
            200,
            {"messages": [{"id": "at-end"}, {"id": "at-start"}, {"id": "before"}]},
        ),
        json_response(200, _message("at-end", END)),
        json_response(200, _message("at-start", START)),
        json_response(200, _message("before", START - timedelta(milliseconds=1))),
    )

    messages = adapter.list_inbox_mail(MailQuery(WINDOW, ACCOUNT))

    assert [message.id for message in messages] == ["at-start"]


def test_gmail_inbox_orders_by_received_instant_ascending_like_the_microsoft_inbox() -> None:
    later = datetime(2026, 8, 20, tzinfo=UTC)
    earlier = datetime(2026, 8, 5, tzinfo=UTC)
    adapter, _ = _adapter(
        _profile(),
        json_response(200, {"messages": [{"id": "later"}, {"id": "earlier"}]}),
        json_response(200, _message("later", later)),
        json_response(200, _message("earlier", earlier)),
    )

    messages = adapter.list_inbox_mail(MailQuery(WINDOW, ACCOUNT))

    assert [message.id for message in messages] == ["earlier", "later"]


def test_gmail_inbox_falls_back_to_internal_date_when_the_date_header_is_unusable() -> None:
    received = datetime(2026, 8, 12, 8, tzinfo=UTC)
    adapter, _ = _adapter(
        _profile(),
        json_response(200, {"messages": [{"id": "no-date"}, {"id": "bad"}, {"id": "naive"}]}),
        json_response(200, _message("no-date", received, date_header=None)),
        json_response(200, _message("bad", received, date_header="not a date")),
        # RFC 2822 reads -0000 as an unknown zone, which parses to a naive datetime.
        json_response(
            200, _message("naive", received, date_header="Wed, 12 Aug 2026 03:00:00 -0000")
        ),
    )

    messages = adapter.list_inbox_mail(MailQuery(WINDOW, ACCOUNT))

    assert [message.sent_at for message in messages] == [received, received, received]
    assert all(message.received_at == received for message in messages)


def test_gmail_inbox_pages_with_the_token_and_stops_at_the_limit() -> None:
    received = datetime(2026, 8, 10, tzinfo=UTC)
    adapter, transport = _adapter(
        _profile(),
        json_response(200, {"messages": [{"id": "p1"}], "nextPageToken": "page-2"}),
        json_response(200, {"messages": [{"id": "p2"}, {"id": "p3"}], "nextPageToken": "page-3"}),
        json_response(200, _message("p1", received)),
        json_response(200, _message("p2", received + timedelta(hours=1))),
    )

    messages = adapter.list_inbox_mail(MailQuery(WINDOW, ACCOUNT, limit=2))

    assert transport.requests[1].params is not None
    assert "pageToken" not in transport.requests[1].params
    assert transport.requests[2].params is not None
    assert transport.requests[2].params["pageToken"] == "page-2"
    assert transport.requests[2].params["maxResults"] == 2
    assert [message.id for message in messages] == ["p1", "p2"]
    assert len(transport.requests) == 5


def test_gmail_inbox_fails_closed_past_the_fixed_page_bound() -> None:
    pages = [
        json_response(200, {"messages": [], "nextPageToken": f"page-{index}"})
        for index in range(20)
    ]
    adapter, transport = _adapter(_profile(), *pages)

    with pytest.raises(ConnectorFailure) as raised:
        adapter.list_inbox_mail(MailQuery(WINDOW, ACCOUNT))

    assert raised.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA
    assert len(transport.requests) == 21


@pytest.mark.parametrize(
    ("observed", "requested"),
    [("other@example.com", ACCOUNT), (ACCOUNT, "other@example.com")],
    ids=["profile-mismatch", "requested-mismatch"],
)
def test_gmail_inbox_refuses_another_identity_before_any_listing(
    observed: str, requested: str
) -> None:
    adapter, transport = _adapter(json_response(200, {"emailAddress": observed}))

    with pytest.raises(ConnectorFailure) as raised:
        adapter.list_inbox_mail(MailQuery(WINDOW, requested))

    assert raised.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert len(transport.requests) == 1


def test_gmail_inbox_reuses_the_google_policy_refiner() -> None:
    blocked = json_response(
        403,
        {
            "error": {
                "code": 403,
                "message": "sensitive-provider-detail",
                "errors": [{"reason": "domainPolicy", "message": "sensitive-provider-detail"}],
            }
        },
    )
    adapter, _ = _adapter(_profile(), blocked)

    with pytest.raises(ConnectorFailure) as raised:
        adapter.list_inbox_mail(MailQuery(WINDOW, ACCOUNT))

    assert raised.value.reason is ReasonCode.ADMIN_CONSENT_REQUIRED
    assert "sensitive-provider-detail" not in raised.value.safe_message


def test_gmail_inbox_record_has_the_shape_of_the_microsoft_inbox_record() -> None:
    gmail, _ = _adapter(
        _profile(),
        json_response(200, {"messages": [{"id": "g-1"}]}),
        json_response(
            200,
            _message(
                "g-1",
                datetime(2026, 8, 14, 9, 35, tzinfo=UTC),
                date_header="Fri, 14 Aug 2026 09:31:00 +0000",
            ),
        ),
    )
    graph = MicrosoftGraphAdapter(
        FakeTransport(
            json_response(200, {"id": "user-1", "mail": ACCOUNT, "userPrincipalName": ACCOUNT}),
            json_response(
                200,
                {
                    "value": [
                        {
                            "id": "m-1",
                            "subject": "Entrada",
                            "sentDateTime": "2026-08-14T09:31:00Z",
                            "receivedDateTime": "2026-08-14T09:35:00Z",
                            "from": {"emailAddress": {"address": "remitente@example.net"}},
                            "toRecipients": [],
                            "ccRecipients": [],
                            "body": {"contentType": "text", "content": "cuerpo"},
                            "conversationId": "c-1",
                            "hasAttachments": False,
                            "internetMessageId": "<m-1@example.net>",
                        }
                    ]
                },
            ),
        ),
        BearerTokenProvider(SecretValue("graph-secret")),
        expected_account=ACCOUNT,
        clock=lambda: NOW,
    )

    gmail_record = versioned_record(gmail.list_inbox_mail(MailQuery(WINDOW, ACCOUNT))[0])
    graph_record = versioned_record(graph.list_inbox_mail(MailQuery(WINDOW, ACCOUNT))[0])

    assert gmail_record.keys() == graph_record.keys()
    assert gmail_record["provenance"].keys() == graph_record["provenance"].keys()
    for field in ("folder", "subject", "sent_at", "received_at", "categories", "attachments"):
        assert gmail_record[field] == graph_record[field], field
    assert gmail_record["internet_message_id"] == "<inbox-1@example.net>"


def test_the_gmail_target_routes_the_inbox_over_the_official_route_only() -> None:
    assert GoogleGmailAdapter.descriptor.capabilities == frozenset(
        {Capability.MAIL_SENT_LIST, Capability.MAIL_INBOX_LIST}
    )
    assert target_spec(ConnectionType.GMAIL).capabilities == ("mail.sent.list", "mail.inbox.list")
    assert (
        cli._target_for_provider("google.gmail", Capability.MAIL_INBOX_LIST) is ConnectionType.GMAIL
    )
    assert Capability.MAIL_INBOX_LIST not in BROWSER_FALLBACK_CAPABILITIES[ProviderFamily.GOOGLE]
    rows = [
        row
        for row in CAPABILITY_MATRIX
        if row.provider == "Gmail API" and row.capability == "mail.inbox.list"
    ]
    assert [(row.channel, row.v0_1_status) for row in rows] == [("official", "implemented-offline")]


def test_the_gmail_adapter_executes_the_inbox_capability() -> None:
    adapter, _ = _adapter(
        _profile(),
        json_response(200, {"messages": []}),
    )

    records = adapter.execute(
        ConnectorRequest.create("google.gmail", Capability.MAIL_INBOX_LIST, MailQuery(WINDOW))
    )

    assert records == ()
