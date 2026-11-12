"""Gmail draft send, `mail.draft.send`, after a duplicate check over the read connection (T9.3).

Decision: docs/decisions/2026-10-25-envio-correo-gmail-dos-pasos.md. Every provider call goes
through FakeTransport; nothing here reaches Google, and no message is ever sent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import solercia_connect.application as application
import solercia_connect.cli as cli
from solercia_connect.auth import BearerTokenProvider
from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.config import (
    ConnectionProfile,
    ConnectionType,
    ProfileStore,
    load_settings,
    target_spec,
)
from solercia_connect.domain import (
    WRITE_CAPABILITIES,
    Capability,
    ConnectorRequest,
    MailDraftSendRequest,
    MailSendResult,
    ReasonCode,
)
from solercia_connect.envelope import ResultEnvelope
from solercia_connect.errors import ConnectorFailure, TransportTimeoutError
from solercia_connect.matrix import CAPABILITY_MATRIX
from solercia_connect.oauth import OAuthTokenSet
from solercia_connect.ports import TransportResponse
from solercia_connect.providers.gmail_compose import GmailComposeAdapter
from solercia_connect.providers.google import GoogleGmailAdapter
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import BROWSER_FALLBACK_CAPABILITIES, ConnectorRuntime
from solercia_connect.secrets import SecretValue
from solercia_connect.service import create_app
from tests.fakes import NOW, FakeTransport, json_response
from tests.test_gmail_draft_create import (
    ACCOUNT,
    BASE,
    PROFILE_URL,
    MemoryTokenStore,
    compose_scopes,
    posts,
    profile,
)

DRAFT_URL = f"{BASE}/users/me/drafts/r-1"
SEND_URL = f"{BASE}/users/me/drafts/send"
MESSAGES_URL = f"{BASE}/users/me/messages"
COMPOSE_AUTH = "Bearer compose-secret"
READER_AUTH = "Bearer read-secret"
_HEADERS = {"X-Solercia-Connect-Key": "local-api-key"}
SENT_MS = str(int(datetime(2026, 8, 23, 12, 0, 5, tzinfo=UTC).timestamp() * 1000))
AFTER = int((NOW - timedelta(days=30)).timestamp())

runner = CliRunner()


def headers(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"name": name, "value": value} for name, value in pairs]


def draft(
    *,
    sender: str | None = f"User <{ACCOUNT}>",
    to: str = "Recipient <recipient@example.net>",
    cc: str | None = "copy@example.net",
    bcc: str | None = "hidden@example.net",
    subject: str | None = "Informe semanal",
) -> TransportResponse:
    pairs = [("To", to)]
    for name, value in (("From", sender), ("Cc", cc), ("Bcc", bcc), ("Subject", subject)):
        if value is not None:
            pairs.append((name, value))
    return json_response(
        200,
        {
            "id": "r-1",
            "message": {
                "id": "m-draft",
                "threadId": "t-1",
                "labelIds": ["DRAFT"],
                "payload": {"headers": headers(*pairs)},
            },
        },
    )


def listing(*ids: str, next_page: str | None = None) -> TransportResponse:
    body: dict[str, Any] = {"messages": [{"id": item, "threadId": "x"} for item in ids]}
    if next_page is not None:
        body["nextPageToken"] = next_page
    return json_response(200, body)


def sent_message(
    message_id: str,
    *,
    subject: str = "Informe semanal",
    to: str = "recipient@example.net",
    cc: str | None = "copy@example.net",
    bcc: str | None = "hidden@example.net",
    sent: datetime = datetime(2026, 8, 20, 15, 30, tzinfo=UTC),
) -> TransportResponse:
    pairs = [("Subject", subject), ("To", to)]
    for name, value in (("Cc", cc), ("Bcc", bcc)):
        if value is not None:
            pairs.append((name, value))
    return json_response(
        200,
        {
            "id": message_id,
            "internalDate": str(int(sent.timestamp() * 1000)),
            "snippet": "sensitive-sent-body",
            "payload": {"headers": headers(*pairs)},
        },
    )


def sent_ok(*, internal_date: str | None = SENT_MS) -> TransportResponse:
    body: dict[str, Any] = {"id": "m-sent", "threadId": "t-1", "labelIds": ["SENT"]}
    if internal_date is not None:
        body["internalDate"] = internal_date
    return json_response(200, body)


def send_request(**overrides: Any) -> MailDraftSendRequest:
    values: dict[str, Any] = {
        "draft_id": "r-1",
        "account_id": ACCOUNT,
        "write_authorized": True,
        **overrides,
    }
    return MailDraftSendRequest(**values)


def send_adapter(
    *responses: TransportResponse | Exception,
    reader: bool = True,
    grant_can_write: bool = True,
) -> tuple[GmailComposeAdapter, FakeTransport]:
    transport = FakeTransport(*responses)
    adapter = GmailComposeAdapter(
        transport,
        BearerTokenProvider(SecretValue("compose-secret")),
        expected_account=ACCOUNT,
        grant_can_write=grant_can_write,
        sent_reader=BearerTokenProvider(SecretValue("read-secret")) if reader else None,
        clock=lambda: NOW,
    )
    return adapter, transport


def auth_of(request: Any) -> str:
    return str(request.headers["Authorization"])


# --- Adapter -------------------------------------------------------------------------------


def test_the_send_reads_the_draft_checks_sent_and_posts_exactly_once() -> None:
    adapter, transport = send_adapter(profile(), draft(), profile(), listing(), sent_ok())

    records = adapter.send_draft(send_request())

    assert [(request.method, request.url) for request in transport.requests] == [
        ("GET", PROFILE_URL),
        ("GET", DRAFT_URL),
        ("GET", PROFILE_URL),
        ("GET", MESSAGES_URL),
        ("POST", SEND_URL),
    ]
    assert transport.requests[1].params == {"format": "metadata"}
    assert transport.requests[3].params == {
        "q": f'in:sent subject:"Informe semanal" after:{AFTER}',
        "maxResults": 50,
    }
    assert [auth_of(request) for request in transport.requests] == [
        COMPOSE_AUTH,
        COMPOSE_AUTH,
        READER_AUTH,
        READER_AUTH,
        COMPOSE_AUTH,
    ]
    assert transport.requests[4].json_body == {"id": "r-1"}
    assert len(posts(transport)) == 1
    record = records[0]
    assert isinstance(record, MailSendResult)
    assert (record.draft_id, record.message_id, record.thread_id) == ("r-1", "m-sent", "t-1")
    assert record.sender.address == ACCOUNT
    assert [item.address for item in record.to] == ["recipient@example.net"]
    assert [item.address for item in record.cc] == ["copy@example.net"]
    assert [item.address for item in record.bcc] == ["hidden@example.net"]
    assert record.subject == "Informe semanal"
    assert record.sent_at == datetime(2026, 8, 23, 12, 0, 5, tzinfo=UTC)
    assert record.provenance.account_id == ACCOUNT
    assert record.provenance.endpoint == "/users/me/drafts/send"


def test_a_sent_message_with_the_same_subject_and_recipients_blocks_the_send() -> None:
    adapter, transport = send_adapter(
        profile(),
        draft(),
        profile(),
        listing("other-recipients", "same"),
        sent_message("other-recipients", to="someone@example.net"),
        sent_message(
            "same",
            subject="  informe   SEMANAL ",
            to="Recipient@Example.NET",
            cc="HIDDEN@example.net",
            bcc="copy@example.net",
        ),
    )

    with pytest.raises(ConnectorFailure) as raised:
        adapter.send_draft(send_request())

    assert raised.value.reason is ReasonCode.DUPLICATE_SUSPECTED
    assert "2026-08-20T15:30:00+00:00" in raised.value.safe_message
    assert "sensitive-sent-body" not in raised.value.safe_message
    assert posts(transport) == []
    metadata = transport.requests[4].params
    assert metadata == {
        "format": "metadata",
        "metadataHeaders": ["Subject", "To", "Cc", "Bcc"],
    }


def test_the_same_subject_to_other_recipients_is_not_a_duplicate() -> None:
    adapter, transport = send_adapter(
        profile(),
        draft(),
        profile(),
        listing("s-1"),
        sent_message("s-1", bcc=None),
        sent_ok(),
    )

    adapter.send_draft(send_request())

    assert len(posts(transport)) == 1


def test_allow_duplicate_skips_the_check_and_the_read_connection() -> None:
    adapter, transport = send_adapter(profile(), draft(), sent_ok(), reader=False)

    adapter.send_draft(send_request(allow_duplicate=True))

    assert [request.url for request in transport.requests] == [PROFILE_URL, DRAFT_URL, SEND_URL]
    assert READER_AUTH not in [auth_of(request) for request in transport.requests]


def test_too_many_sent_candidates_block_the_send() -> None:
    adapter, transport = send_adapter(
        profile(),
        draft(),
        profile(),
        listing(next_page="page-2"),
        listing(next_page="page-3"),
    )

    with pytest.raises(ConnectorFailure) as raised:
        adapter.send_draft(send_request())

    assert raised.value.reason is ReasonCode.DUPLICATE_SUSPECTED
    assert transport.requests[4].params is not None
    assert transport.requests[4].params["pageToken"] == "page-2"
    assert posts(transport) == []


@pytest.mark.parametrize(
    "reader_responses",
    [
        (),
        (profile("other@example.com"),),
        (json_response(401, {}),),
        (profile(), json_response(403, {})),
        (profile(), TransportTimeoutError("timed out")),
    ],
    ids=["no-read-connection", "other-identity", "reader-401", "list-403", "list-timeout"],
)
def test_a_missing_or_failing_read_connection_blocks_the_send(
    reader_responses: tuple[TransportResponse | Exception, ...],
) -> None:
    adapter, transport = send_adapter(
        profile(), draft(), *reader_responses, reader=bool(reader_responses)
    )

    with pytest.raises(ConnectorFailure) as raised:
        adapter.send_draft(send_request())

    assert raised.value.reason is ReasonCode.DUPLICATE_CHECK_UNAVAILABLE
    assert "solercia-connect connect gmail" in raised.value.safe_message
    assert posts(transport) == []


@pytest.mark.parametrize(
    ("draft_response", "reason"),
    [
        (draft(sender="other@example.com"), ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION),
        (draft(sender=None), ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION),
        (
            draft(sender=f"{ACCOUNT}, other@example.com"),
            ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
        ),
        (draft(subject=None), ReasonCode.INVALID_INPUT),
        (draft(to="", cc=None, bcc=None), ReasonCode.INVALID_INPUT),
        (
            draft(to=", ".join(f"r{index}@example.net" for index in range(21)), cc=None, bcc=None),
            ReasonCode.INVALID_INPUT,
        ),
        (json_response(404, {"error": {"message": "sensitive"}}), ReasonCode.DRAFT_NOT_FOUND),
        (
            json_response(200, {"id": "r-2", "message": {"payload": {"headers": []}}}),
            ReasonCode.MALFORMED_PROVIDER_DATA,
        ),
    ],
    ids=[
        "from-mismatch",
        "from-missing",
        "from-several",
        "no-subject",
        "no-recipient",
        "too-many-recipients",
        "draft-not-found",
        "another-draft",
    ],
)
def test_an_unsendable_draft_fails_closed_before_any_post(
    draft_response: TransportResponse, reason: ReasonCode
) -> None:
    adapter, transport = send_adapter(profile(), draft_response)

    with pytest.raises(ConnectorFailure) as raised:
        adapter.send_draft(send_request())

    assert raised.value.reason is reason
    assert posts(transport) == []
    assert "sensitive" not in raised.value.safe_message


def test_another_identity_reads_no_draft() -> None:
    adapter, transport = send_adapter(profile("other@example.com"))

    with pytest.raises(ConnectorFailure) as raised:
        adapter.send_draft(send_request())

    assert raised.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert [request.url for request in transport.requests] == [PROFILE_URL]


def test_a_grant_without_gmail_compose_cannot_send() -> None:
    adapter, transport = send_adapter(grant_can_write=False)

    with pytest.raises(ConnectorFailure) as raised:
        adapter.send_draft(send_request())

    assert raised.value.reason is ReasonCode.WRITE_SCOPE_MISSING
    assert transport.requests == []


def test_a_draft_gone_at_send_time_is_reported_as_not_found() -> None:
    adapter, transport = send_adapter(profile(), draft(), json_response(404, {}), reader=False)

    with pytest.raises(ConnectorFailure) as raised:
        adapter.send_draft(send_request(allow_duplicate=True))

    assert raised.value.reason is ReasonCode.DRAFT_NOT_FOUND
    assert len(posts(transport)) == 1


@pytest.mark.parametrize(
    "failure",
    [
        json_response(503, {"error": {"message": "sensitive-provider-detail"}}),
        json_response(500, {}),
        TransportTimeoutError("timed out"),
        json_response(200, {"threadId": "t-1"}),
    ],
    ids=["503", "500", "timeout", "no-message-id"],
)
def test_an_unconfirmed_send_is_sent_once_and_may_have_happened(
    failure: TransportResponse | Exception,
) -> None:
    adapter, transport = send_adapter(profile(), draft(), failure, reader=False)
    registry = ProviderRegistry()
    registry.register(adapter)

    outcome = ConnectorRuntime(registry, sleeper=lambda _: None).execute(
        ConnectorRequest.create(
            "google.gmail", Capability.MAIL_DRAFT_SEND, send_request(allow_duplicate=True)
        )
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.WRITE_UNVERIFIED
    assert len(posts(transport)) == 1
    detail = next(
        step.detail for step in outcome.receipt.steps if step.reason is ReasonCode.WRITE_UNVERIFIED
    )
    assert "may have been sent" in detail and "Sent" in detail
    assert "not sent" not in detail
    assert "sensitive-provider-detail" not in detail


def test_the_sent_instant_falls_back_to_the_confirmation_time() -> None:
    adapter, _ = send_adapter(profile(), draft(), sent_ok(internal_date=None), reader=False)

    record = adapter.send_draft(send_request(allow_duplicate=True))[0]

    assert record.sent_at == NOW


def test_the_draft_id_is_one_plain_gmail_identifier() -> None:
    for value in ("", "  ", "r-1/../../profile", "r 1", "r-1?format=raw"):
        with pytest.raises(ValueError, match="draft id"):
            send_request(draft_id=value)
    assert send_request(draft_id=" r-123_ABC ").draft_id == "r-123_ABC"


# --- Runtime, application, CLI and HTTP guards ---------------------------------------------


def test_the_runtime_refuses_an_unauthorized_send_before_any_adapter() -> None:
    adapter, transport = send_adapter()
    registry = ProviderRegistry()
    registry.register(adapter)

    outcome = ConnectorRuntime(registry).execute(
        ConnectorRequest.create(
            "google.gmail", Capability.MAIL_DRAFT_SEND, send_request(write_authorized=False)
        )
    )

    assert outcome.receipt.final_reason is ReasonCode.WRITE_NOT_AUTHORIZED
    assert transport.requests == []


def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "type": "gmail-compose",
        "account": ACCOUNT,
        "capability": "mail.draft.send",
        "draft_id": "r-1",
        "allow_write": True,
        **overrides,
    }


def _no_registry(**_: Any) -> Any:
    raise AssertionError("A refused request must not build the provider registry")


def test_the_application_requires_allow_write_for_the_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(allow_write=False))

    assert result.error is not None and result.error.code == "write_not_authorized"
    assert "mail.draft.send" in result.error.message
    assert "--draft-id <id>" in result.error.remediation[0]


def test_the_application_refuses_the_send_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(), interactive=False)

    assert result.status == "forbidden"
    assert result.error is not None and result.error.code == "write_not_available_over_http"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"draft_id": None}, "draft_id is required"),
        ({"account": None}, "account is required for mail.draft.send"),
        ({"allow_duplicate": "yes"}, "allow_duplicate must be a boolean"),
        ({"type": "gmail"}, "not available for gmail"),
        (
            {"capability": "mail.draft.create", "to": ["recipient@example.net"]},
            "draft_id and allow_duplicate apply only to mail.draft.send",
        ),
        (
            {"type": "gmail", "capability": "mail.sent.list", "allow_write": False},
            "draft_id and allow_duplicate apply only to mail.draft.send",
        ),
        ({"allow_browser_fallback": True}, "official Gmail API"),
    ],
)
def test_malformed_send_requests_are_invalid_input(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], message: str
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(**overrides))

    assert result.status == "invalid_input"
    assert result.error is not None and message in result.error.message


@pytest.mark.parametrize(
    ("reason", "remediation"),
    [
        (ReasonCode.DUPLICATE_CHECK_UNAVAILABLE, f"solercia-connect connect gmail {ACCOUNT}"),
        (ReasonCode.DUPLICATE_SUSPECTED, "--allow-duplicate only after the user"),
        (ReasonCode.WRITE_UNVERIFIED, "Gmail > Sent"),
    ],
)
def test_send_failures_carry_their_remediation(
    monkeypatch: pytest.MonkeyPatch, reason: ReasonCode, remediation: str
) -> None:
    class Failing:
        descriptor = GmailComposeAdapter.descriptor

        def health(self) -> Any:
            raise AssertionError("unused")

        def execute(self, request: ConnectorRequest) -> Any:
            raise ConnectorFailure(reason, "refused")

    registry = ProviderRegistry()
    registry.register(Failing())
    monkeypatch.setattr(application, "registry_from_environment", lambda **_: registry)

    result = application.run_application(_payload())

    assert result.error is not None and result.error.code == reason.value
    assert result.error.retryable is False
    assert any(remediation in item for item in result.error.remediation)


def test_the_read_only_gmail_target_cannot_send() -> None:
    registry = ProviderRegistry()
    registry.register(
        GoogleGmailAdapter(
            FakeTransport(),
            BearerTokenProvider(SecretValue("read-secret")),
            expected_account=ACCOUNT,
        )
    )

    outcome = ConnectorRuntime(registry).execute(
        ConnectorRequest.create("google.gmail", Capability.MAIL_DRAFT_SEND, send_request())
    )

    assert outcome.receipt.final_reason is ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE
    assert "mail.draft.send" not in target_spec(ConnectionType.GMAIL).capabilities
    assert "mail.draft.send" in target_spec(ConnectionType.GMAIL_COMPOSE).capabilities
    assert Capability.MAIL_DRAFT_SEND in WRITE_CAPABILITIES
    assert all(
        Capability.MAIL_DRAFT_SEND not in allowed
        for allowed in BROWSER_FALLBACK_CAPABILITIES.values()
    )
    rows = [row for row in CAPABILITY_MATRIX if row.capability == "mail.draft.send"]
    assert [(row.channel, row.v0_1_status) for row in rows] == [("official", "implemented-offline")]


@pytest.mark.parametrize("interactive", [False, True])
def test_http_rejects_the_send_before_the_application_runs(
    tmp_path: Path, interactive: bool
) -> None:
    def run_handler(payload: dict[str, Any], *, browser_via_http: bool = False) -> Any:
        raise AssertionError("HTTP must not reach the application for a write")

    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "SOLERCIA_CONNECT_API_KEY": "local-api-key",
    }
    if interactive:
        values["SOLERCIA_CONNECT_BROWSER_FALLBACK"] = "true"
        values["SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN"] = "true"
    client = TestClient(create_app(settings=load_settings(environ=values), run_handler=run_handler))

    plain = client.post(
        "/v1/run",
        headers=_HEADERS,
        json={"type": "gmail-compose", "account": ACCOUNT, "capability": "mail.draft.send"},
    )

    assert plain.status_code == 403
    assert plain.json()["error"]["code"] == "write_not_available_over_http"
    assert "mail.draft.send" in plain.json()["error"]["message"]


def _cli_args(*extra: str) -> list[str]:
    return [
        "run",
        "--provider",
        "google.gmail",
        "--capability",
        "mail.draft.send",
        "--account",
        ACCOUNT,
        "--draft-id",
        "r-1",
        *extra,
    ]


def test_the_cli_passes_the_draft_id_and_the_duplicate_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def run(payload: dict[str, Any], **_: Any) -> Any:
        captured.update(payload)
        return ResultEnvelope.success(operation="run", data={"records": []})

    monkeypatch.setattr(cli, "run_application", run)

    result = runner.invoke(cli.app, _cli_args("--allow-write", "--allow-duplicate"), env={})

    assert result.exit_code == 0, result.stdout
    assert captured["type"] == "gmail-compose"
    assert captured["draft_id"] == "r-1"
    assert captured["allow_duplicate"] is True
    assert captured["allow_write"] is True
    assert "body" not in captured


def test_the_cli_without_allow_write_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = runner.invoke(cli.app, _cli_args(), env={})

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "write_not_authorized"


def test_the_legacy_query_helper_refuses_the_send() -> None:
    with pytest.raises(cli._InputError, match=r"mail\.draft\.send is a write"):
        cli._query_payload(
            Capability.MAIL_DRAFT_SEND,
            start=None,
            end=None,
            account=ACCOUNT,
            calendar_id="primary",
            assignee=None,
            jql=None,
            meeting_id=None,
            limit=50,
        )


# --- Wiring through the stored connections -------------------------------------------------


def _lasting_tokens(access: str, *scopes: str) -> OAuthTokenSet:
    return OAuthTokenSet(
        access_token=SecretValue(access),
        refresh_token=SecretValue("refresh"),
        expires_at=datetime(2100, 1, 1, tzinfo=UTC),
        scopes=scopes,
    )


def _settings(tmp_path: Path) -> Any:
    return load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
        }
    )


def _registry(tmp_path: Path, transport: FakeTransport, *, with_gmail: bool) -> ProviderRegistry:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    profiles.save(ConnectionProfile.create("gmail-compose", ACCOUNT, now=NOW))
    store = MemoryTokenStore(
        {f"gmail-compose:{ACCOUNT}": _lasting_tokens("compose-access", *compose_scopes())}
    )
    if with_gmail:
        profiles.save(ConnectionProfile.create("gmail", ACCOUNT, now=NOW))
        store.save(
            f"gmail:{ACCOUNT}",
            _lasting_tokens(
                "read-access",
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
            ),
        )
    return registry_from_environment(
        connection_type="gmail-compose",
        account=ACCOUNT,
        capability=Capability.MAIL_DRAFT_SEND,
        token_store=store,
        environ={},
        include_browser=False,
        settings_override=settings,
        transport=transport,
    )


def test_the_duplicate_check_uses_the_accounts_read_only_gmail_connection(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(profile(), draft(), profile(), listing(), sent_ok())
    registry = _registry(tmp_path, transport, with_gmail=True)

    outcome = ConnectorRuntime(registry).execute(
        ConnectorRequest.create("google.gmail", Capability.MAIL_DRAFT_SEND, send_request())
    )

    assert outcome.ok is True
    assert [auth_of(request) for request in transport.requests] == [
        "Bearer compose-access",
        "Bearer compose-access",
        "Bearer read-access",
        "Bearer read-access",
        "Bearer compose-access",
    ]


def test_without_a_gmail_connection_only_an_approved_duplicate_can_send(tmp_path: Path) -> None:
    blocked_transport = FakeTransport(profile(), draft())
    blocked = ConnectorRuntime(_registry(tmp_path, blocked_transport, with_gmail=False)).execute(
        ConnectorRequest.create("google.gmail", Capability.MAIL_DRAFT_SEND, send_request())
    )
    approved_transport = FakeTransport(profile(), draft(), sent_ok())
    approved = ConnectorRuntime(_registry(tmp_path, approved_transport, with_gmail=False)).execute(
        ConnectorRequest.create(
            "google.gmail", Capability.MAIL_DRAFT_SEND, send_request(allow_duplicate=True)
        )
    )

    assert blocked.receipt.final_reason is ReasonCode.DUPLICATE_CHECK_UNAVAILABLE
    assert posts(blocked_transport) == []
    assert approved.ok is True
    assert len(posts(approved_transport)) == 1


# --- Documentation -------------------------------------------------------------------------


def test_docs_declare_the_two_step_gmail_send() -> None:
    root = Path(__file__).parents[1]
    for relative in (
        "README.md",
        "docs/runbooks/gmail.md",
        "docs/USO-CON-AGENTES.md",
        "agents/skills/solercia-connect/SKILL.md",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        for token in (
            "gmail-compose",
            "mail.draft.create",
            "mail.draft.send",
            "--draft-id",
            "--allow-write",
            "--allow-duplicate",
            "duplicate_suspected",
        ):
            assert token in text, (relative, token)
        assert "@idartes" not in text, relative
