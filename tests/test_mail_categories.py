"""Outlook categories travel on every mail record, whichever route read the message.

Support teams mark a case's state with Outlook categories, so a consumer needs to see them on the
record itself. The field is additive: records that carry no categories keep working, and a mailbox
that never used categories reports an empty list rather than an invented one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from solercia_connect.auth import BearerTokenProvider
from solercia_connect.domain import (
    DateWindow,
    EmailAddress,
    MailMessage,
    MailQuery,
    ReasonCode,
    RouteKind,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers._microsoft import microsoft_provenance, normalize_mail_message
from solercia_connect.providers.microsoft_graph import MicrosoftGraphAdapter
from solercia_connect.secrets import SecretValue
from solercia_connect.serialization import to_primitive
from tests.fakes import NOW, FakeTransport, json_response

_FETCHED = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
_WINDOW = DateWindow(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 18, tzinfo=UTC))
_CATEGORIES = ["SOPORTE/PENDIENTE", "SOPORTE/NIVEL 1"]


def _graph_message(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "AAMk-soporte-1",
        "subject": "Caso de soporte",
        "sentDateTime": "2026-10-14T09:31:00Z",
        "receivedDateTime": "2026-10-14T09:35:00Z",
        "from": {"emailAddress": {"name": "Usuaria", "address": "usuaria@example.com"}},
        "toRecipients": [{"emailAddress": {"name": "Mesa", "address": "mesa@example.com"}}],
        "ccRecipients": [],
        "body": {"contentType": "html", "content": "<p>cuerpo</p>"},
        "conversationId": "conv-soporte-1",
        "hasAttachments": False,
        "internetMessageId": "<soporte-1@example.com>",
        "categories": list(_CATEGORIES),
    }
    payload.update(overrides)
    return payload


def _browser_message(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Id": "AAMk-soporte-1",
        "Subject": "Caso de soporte",
        "SentDateTime": "2026-10-14T09:31:00Z",
        "ReceivedDateTime": "2026-10-14T09:35:00Z",
        "From": {"EmailAddress": {"Name": "Usuaria", "Address": "usuaria@example.com"}},
        "ToRecipients": [{"EmailAddress": {"Name": "Mesa", "Address": "mesa@example.com"}}],
        "CcRecipients": [],
        "Body": {"ContentType": "HTML", "Content": "<p>cuerpo</p>"},
        "ConversationId": "conv-soporte-1",
        "HasAttachments": False,
        "InternetMessageId": "<soporte-1@example.com>",
        "Categories": list(_CATEGORIES),
    }
    payload.update(overrides)
    return payload


def _normalize(item: dict[str, Any], route: RouteKind = RouteKind.OFFICIAL) -> MailMessage:
    official = route is RouteKind.OFFICIAL
    return normalize_mail_message(
        item,
        provenance=microsoft_provenance(
            provider_id="microsoft.graph" if official else "microsoft365.browser",
            route=route,
            source_id="source",
            fetched_at=_FETCHED,
            account_id="pandora@example.com",
            endpoint="graph-v1" if official else "outlook-rest-v2",
        ),
        folder="inbox",
    )


def test_categories_are_identical_whichever_route_produced_the_record() -> None:
    official = _normalize(_graph_message(), RouteKind.OFFICIAL)
    browser = _normalize(_browser_message(), RouteKind.BROWSER)

    assert official.categories == browser.categories == ("SOPORTE/PENDIENTE", "SOPORTE/NIVEL 1")
    assert replace(official, provenance=browser.provenance) == browser


def test_a_message_without_categories_reports_an_empty_list() -> None:
    absent = {key: value for key, value in _graph_message().items() if key != "categories"}

    assert _normalize(absent).categories == ()
    assert _normalize(_graph_message(categories=[])).categories == ()
    assert _normalize(_graph_message(categories=None)).categories == ()


def test_category_names_keep_their_order_without_duplicates_or_padding() -> None:
    record = _normalize(
        _browser_message(
            Categories=["  SOPORTE/EN ESPERA ", "SOPORTE/NIVEL 2", "SOPORTE/EN ESPERA"],
        ),
        RouteKind.BROWSER,
    )

    assert record.categories == ("SOPORTE/EN ESPERA", "SOPORTE/NIVEL 2")


@pytest.mark.parametrize(
    "malformed",
    [
        "SOPORTE/RESUELTO",
        {"name": "SOPORTE/RESUELTO"},
        ["SOPORTE/RESUELTO", 7],
        ["SOPORTE/RESUELTO", "   "],
    ],
)
def test_malformed_categories_fail_closed_instead_of_hiding_a_case_state(malformed: object) -> None:
    with pytest.raises(ConnectorFailure) as captured:
        _normalize(_graph_message(categories=malformed))

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


def test_existing_positional_construction_defaults_to_no_categories() -> None:
    provenance = microsoft_provenance(
        provider_id="google.gmail",
        route=RouteKind.OFFICIAL,
        source_id="g-1",
        fetched_at=_FETCHED,
        account_id="user@example.com",
        endpoint="gmail",
    )
    record = MailMessage(
        "g-1",
        "Asunto",
        _FETCHED,
        EmailAddress("user@example.com"),
        (),
        (),
        "cuerpo",
        "text",
        None,
        False,
        provenance,
    )

    assert record.categories == ()


def test_categories_serialize_as_a_list_without_changing_the_record_schema() -> None:
    primitive = to_primitive(_normalize(_graph_message()))

    assert primitive["categories"] == ["SOPORTE/PENDIENTE", "SOPORTE/NIVEL 1"]
    assert list(primitive).index("categories") > list(primitive).index("received_at")


@pytest.mark.parametrize(
    ("reader", "expected_url"),
    [
        ("list_inbox_mail", "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"),
        ("list_sent_mail", "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages"),
    ],
)
def test_graph_projects_and_returns_categories_for_inbox_and_sent(
    reader: str,
    expected_url: str,
) -> None:
    transport = FakeTransport(
        json_response(
            200,
            {
                "id": "user-1",
                "mail": "pandora@example.com",
                "userPrincipalName": "pandora@example.com",
            },
        ),
        json_response(200, {"value": [_graph_message()]}),
    )
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("graph-secret")),
        expected_account="pandora@example.com",
        clock=lambda: NOW,
    )

    messages = getattr(adapter, reader)(MailQuery(_WINDOW, "pandora@example.com"))

    assert transport.requests[1].url == expected_url
    params = transport.requests[1].params
    assert params is not None
    assert "categories" in str(params["$select"]).split(",")
    assert messages[0].categories == ("SOPORTE/PENDIENTE", "SOPORTE/NIVEL 1")


def test_docs_declare_categories_on_mail_records() -> None:
    root = Path(__file__).parents[1]
    for relative in (
        "README.md",
        "docs/runbooks/outlook.md",
        "docs/USO-CON-AGENTES.md",
        "agents/skills/solercia-connect/SKILL.md",
        "openspec/specs/mail-record-contract/spec.md",
    ):
        assert "categories" in (root / relative).read_text(), relative
