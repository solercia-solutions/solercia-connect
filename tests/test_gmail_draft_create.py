"""Gmail draft creation, `mail.draft.create`, on the separate `gmail-compose` target (T9.2).

Decision: docs/decisions/2026-10-25-envio-correo-gmail-dos-pasos.md. Every provider call goes
through FakeTransport; nothing here reaches Google, and no draft is created anywhere.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import solercia_connect.application as application
import solercia_connect.cli as cli
from solercia_connect.auth import BearerTokenProvider
from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.browser_routes import has_browser_route
from solercia_connect.config import (
    GMAIL_COMPOSE_SCOPE,
    ConnectionProfile,
    ConnectionType,
    ProfileStore,
    load_settings,
    target_spec,
)
from solercia_connect.connections import ConnectionService
from solercia_connect.doctor import DoctorService
from solercia_connect.domain import (
    MAX_DRAFT_BODY_BYTES,
    MAX_DRAFT_RECIPIENTS,
    WRITE_CAPABILITIES,
    Capability,
    ConnectorRequest,
    MailDraft,
    MailDraftRequest,
    ReasonCode,
    RouteKind,
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

ACCOUNT = "user@example.com"
BASE = "https://gmail.googleapis.com/gmail/v1"
DRAFTS_URL = f"{BASE}/users/me/drafts"
PROFILE_URL = f"{BASE}/users/me/profile"
_HEADERS = {"X-Solercia-Connect-Key": "local-api-key"}

runner = CliRunner()


class MemoryTokenStore:
    def __init__(self, tokens: dict[str, OAuthTokenSet] | None = None) -> None:
        self.values = dict(tokens or {})
        self.deleted: list[str] = []

    def load(self, key: str) -> OAuthTokenSet | None:
        return self.values.get(key)

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        self.values[key] = tokens

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.values.pop(key, None)


def tokens(*scopes: str) -> OAuthTokenSet:
    return OAuthTokenSet(
        access_token=SecretValue("access"),
        refresh_token=SecretValue("refresh"),
        expires_at=NOW + timedelta(hours=1),
        scopes=scopes,
    )


def compose_scopes() -> tuple[str, ...]:
    return ("openid", "https://www.googleapis.com/auth/userinfo.email", GMAIL_COMPOSE_SCOPE)


def profile(account: str = ACCOUNT) -> TransportResponse:
    return json_response(200, {"emailAddress": account})


def created(draft_id: str = "r-1", message_id: str = "m-1") -> TransportResponse:
    return json_response(
        200,
        {"id": draft_id, "message": {"id": message_id, "threadId": "t-1", "labelIds": ["DRAFT"]}},
    )


def draft_request(**overrides: Any) -> MailDraftRequest:
    values: dict[str, Any] = {
        "to": ("recipient@example.net",),
        "subject": "Informe semanal",
        "body": "Cuerpo del correo.\n",
        "cc": ("copy@example.net",),
        "bcc": ("hidden@example.net",),
        "account_id": ACCOUNT,
        "write_authorized": True,
        **overrides,
    }
    return MailDraftRequest(**values)


def compose_adapter(
    *responses: TransportResponse | Exception, grant_can_write: bool = True
) -> tuple[GmailComposeAdapter, FakeTransport]:
    transport = FakeTransport(*responses)
    adapter = GmailComposeAdapter(
        transport,
        BearerTokenProvider(SecretValue("compose-secret")),
        expected_account=ACCOUNT,
        grant_can_write=grant_can_write,
        clock=lambda: NOW,
    )
    return adapter, transport


def decoded_raw(json_body: Any) -> EmailMessage:
    raw = json_body["message"]["raw"]
    message = email.message_from_bytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=email.policy.default
    )
    assert isinstance(message, EmailMessage)
    return message


def posts(transport: FakeTransport) -> list[Any]:
    return [request for request in transport.requests if request.method == "POST"]


# --- Adapter -------------------------------------------------------------------------------


def test_the_draft_is_one_post_of_a_plain_text_message_from_the_connected_account() -> None:
    adapter, transport = compose_adapter(profile(), created())

    records = adapter.create_draft(
        draft_request(subject="Informe de gestión — octubre", body="Línea uno\nLínea dos\n")
    )

    assert [(request.method, request.url) for request in transport.requests] == [
        ("GET", PROFILE_URL),
        ("POST", DRAFTS_URL),
    ]
    body = transport.requests[1].json_body
    assert body is not None and set(body) == {"message"} and set(body["message"]) == {"raw"}
    message = decoded_raw(body)
    assert message["From"] == ACCOUNT
    assert message["To"] == "recipient@example.net"
    assert message["Cc"] == "copy@example.net"
    assert message["Bcc"] == "hidden@example.net"
    assert message["Subject"] == "Informe de gestión — octubre"
    assert message.get_content_type() == "text/plain"
    assert message.get_content_charset() == "utf-8"
    assert message.get_content().replace("\r\n", "\n") == "Línea uno\nLínea dos\n"
    raw = body["message"]["raw"]
    assert base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).isascii()
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, MailDraft)
    assert (record.draft_id, record.message_id, record.thread_id) == ("r-1", "m-1", "t-1")
    assert record.sender.address == ACCOUNT
    assert [item.address for item in record.to] == ["recipient@example.net"]
    assert [item.address for item in record.cc] == ["copy@example.net"]
    assert [item.address for item in record.bcc] == ["hidden@example.net"]
    assert record.subject == "Informe de gestión — octubre"
    assert record.provenance.account_id == ACCOUNT
    assert record.provenance.route is RouteKind.OFFICIAL
    assert record.provenance.endpoint == "/users/me/drafts"


def test_the_record_carries_only_a_short_preview_of_the_body() -> None:
    adapter, _ = compose_adapter(profile(), created())
    body = "a" * 150 + "b" * 350

    record = adapter.create_draft(draft_request(body=body))[0]

    assert record.body_preview == body[:200]
    assert "b" * 51 not in record.body_preview


def test_several_recipients_share_one_header_and_optional_fields_are_omitted() -> None:
    adapter, transport = compose_adapter(profile(), created())

    adapter.create_draft(
        draft_request(to=("one@example.net", "Two@Example.NET"), cc=(), bcc=()),
    )

    message = decoded_raw(transport.requests[1].json_body)
    assert message["To"] == "one@example.net, two@example.net"
    assert message["Cc"] is None and message["Bcc"] is None


def test_another_identity_produces_no_draft() -> None:
    adapter, transport = compose_adapter(profile("other@example.com"))

    with pytest.raises(ConnectorFailure) as raised:
        adapter.create_draft(draft_request())

    assert raised.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert posts(transport) == []


def test_a_request_for_another_account_produces_no_draft() -> None:
    adapter, transport = compose_adapter(profile())

    with pytest.raises(ConnectorFailure) as raised:
        adapter.create_draft(draft_request(account_id="other@example.com"))

    assert raised.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert posts(transport) == []


def test_a_grant_without_gmail_compose_fails_before_any_provider_call() -> None:
    adapter, transport = compose_adapter(grant_can_write=False)

    with pytest.raises(ConnectorFailure) as raised:
        adapter.create_draft(draft_request())

    assert raised.value.reason is ReasonCode.WRITE_SCOPE_MISSING
    assert transport.requests == []


def test_the_adapter_refuses_an_unauthorized_request_on_its_own() -> None:
    adapter, transport = compose_adapter()

    with pytest.raises(ConnectorFailure) as raised:
        adapter.create_draft(draft_request(write_authorized=False))

    assert raised.value.reason is ReasonCode.WRITE_NOT_AUTHORIZED
    assert transport.requests == []


@pytest.mark.parametrize(
    "failure",
    [
        json_response(503, {"error": {"message": "sensitive-provider-detail"}}),
        TransportTimeoutError("timed out"),
        TransportResponse(200, {}, b"not json"),
        json_response(200, {"message": {"id": "m-1"}}),
        RuntimeError("socket closed"),
    ],
    ids=["5xx", "timeout", "unreadable", "no-draft-id", "unknown"],
)
def test_an_unconfirmed_post_is_reported_as_unverified_and_sent_once(
    failure: TransportResponse | Exception,
) -> None:
    adapter, transport = compose_adapter(profile(), failure)
    registry = ProviderRegistry()
    registry.register(adapter)

    outcome = ConnectorRuntime(registry, sleeper=lambda _: None).execute(
        ConnectorRequest.create("google.gmail", Capability.MAIL_DRAFT_CREATE, draft_request())
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.WRITE_UNVERIFIED
    assert len(posts(transport)) == 1
    detail = next(
        step.detail for step in outcome.receipt.steps if step.reason is ReasonCode.WRITE_UNVERIFIED
    )
    assert "may" in detail and "Drafts" in detail
    assert "sensitive-provider-detail" not in detail
    assert "Cuerpo" not in detail


@pytest.mark.parametrize(
    ("status", "reason"),
    [(400, ReasonCode.REQUEST_REJECTED), (401, ReasonCode.INVALID_CREDENTIALS)],
)
def test_a_refused_post_keeps_its_typed_reason(status: int, reason: ReasonCode) -> None:
    adapter, transport = compose_adapter(profile(), json_response(status, {}))

    with pytest.raises(ConnectorFailure) as raised:
        adapter.create_draft(draft_request())

    assert raised.value.reason is reason
    assert len(posts(transport)) == 1


def test_the_adapter_health_proves_the_identity() -> None:
    adapter, transport = compose_adapter(profile("USER@example.com"))

    report = adapter.health()

    assert report.account_id == "user@example.com"
    assert Capability.MAIL_DRAFT_CREATE in report.capabilities
    assert [request.url for request in transport.requests] == [PROFILE_URL]


def test_the_adapter_refuses_a_request_outside_its_contract() -> None:
    adapter, transport = compose_adapter()

    with pytest.raises(ConnectorFailure) as raised:
        adapter.execute(
            ConnectorRequest.create("google.gmail", Capability.MAIL_SENT_LIST, draft_request())
        )

    assert raised.value.reason is ReasonCode.CAPABILITY_NOT_IMPLEMENTED
    assert transport.requests == []


# --- Payload validation --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"to": ()}, "at least one --to"),
        ({"to": ("not-an-email",)}, "not a valid email address"),
        ({"to": ("a,b@example.net",)}, "not a valid email address"),
        ({"to": ("Name <a@example.net>",)}, "not a valid email address"),
        ({"cc": ("x@localhost",)}, "not a valid email address"),
        ({"bcc": ("x@example.net\r\nBcc: y@example.net",)}, "not a valid email address"),
        ({"cc": ("Recipient@example.net",)}, "more than once"),
        ({"subject": "   "}, "subject"),
        ({"subject": "Uno\r\nBcc: otro@example.net"}, "one line"),
        ({"subject": "x" * 256}, "255"),
        ({"body": " \n "}, "body"),
        ({"body": "x" * (MAX_DRAFT_BODY_BYTES + 1)}, "200 KB"),
        ({"body": "ñ" * (MAX_DRAFT_BODY_BYTES // 2 + 1)}, "200 KB"),
    ],
)
def test_the_draft_payload_is_validated_before_any_call(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        draft_request(**overrides)


def test_the_recipient_cap_counts_every_field() -> None:
    to = tuple(f"to{index}@example.net" for index in range(MAX_DRAFT_RECIPIENTS - 1))

    accepted = draft_request(to=to, cc=("cc@example.net",), bcc=())
    with pytest.raises(ValueError, match="20 recipients"):
        draft_request(to=to, cc=("cc@example.net",), bcc=("bcc@example.net",))

    assert len(accepted.to) + len(accepted.cc) == MAX_DRAFT_RECIPIENTS
    assert MAX_DRAFT_RECIPIENTS == 20


def test_a_body_at_the_cap_is_accepted_and_addresses_are_normalized() -> None:
    request = draft_request(
        to=(" Recipient@Example.NET ",), cc=(), bcc=(), body="x" * MAX_DRAFT_BODY_BYTES
    )

    assert request.to == ("recipient@example.net",)
    assert MAX_DRAFT_BODY_BYTES == 200 * 1024
    assert "Cuerpo" not in repr(draft_request())


# --- Runtime, application, CLI and HTTP guards ---------------------------------------------


def test_the_runtime_refuses_an_unauthorized_draft_before_any_adapter() -> None:
    adapter, transport = compose_adapter()
    registry = ProviderRegistry()
    registry.register(adapter)

    outcome = ConnectorRuntime(registry).execute(
        ConnectorRequest.create(
            "google.gmail",
            Capability.MAIL_DRAFT_CREATE,
            draft_request(write_authorized=False),
        )
    )

    assert outcome.receipt.final_reason is ReasonCode.WRITE_NOT_AUTHORIZED
    assert transport.requests == []


def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "type": "gmail-compose",
        "account": ACCOUNT,
        "capability": "mail.draft.create",
        "to": ["recipient@example.net"],
        "subject": "Informe semanal",
        "body": "Cuerpo del correo.\n",
        "allow_write": True,
        **overrides,
    }


def _no_registry(**_: Any) -> Any:
    raise AssertionError("A refused request must not build the provider registry")


def test_the_application_requires_allow_write_and_calls_nothing_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(allow_write=False))

    assert result.status == "invalid_input"
    assert result.error is not None and result.error.code == "write_not_authorized"
    assert "mail.draft.create" in result.error.message
    assert "--to <recipient>" in result.error.remediation[0]


def test_the_application_refuses_the_draft_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(), interactive=False)

    assert result.status == "forbidden"
    assert result.error is not None and result.error.code == "write_not_available_over_http"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"account": None}, "account is required for mail.draft.create"),
        ({"to": None}, "at least one --to"),
        ({"to": "recipient@example.net"}, "to must be a list of email addresses"),
        ({"cc": [1]}, "cc must be a list of email addresses"),
        ({"subject": None}, "subject is required"),
        ({"body": None}, "body is required"),
        ({"allow_write": "yes"}, "allow_write must be a boolean"),
        ({"allow_browser_fallback": True}, "official Gmail API"),
        ({"type": "gmail"}, "not available for gmail"),
        ({"capability": "mail.sent.list"}, "not available for gmail-compose"),
        (
            {"type": "gmail", "capability": "mail.sent.list", "allow_write": False},
            "subject and body apply only to mail.draft.create",
        ),
        (
            {
                "type": "gmail",
                "capability": "mail.sent.list",
                "subject": None,
                "body": None,
                "allow_write": False,
            },
            "to applies only to external.issues.transition and mail.draft.create",
        ),
    ],
)
def test_malformed_draft_requests_are_invalid_input(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], message: str
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(**overrides))

    assert result.status == "invalid_input"
    assert result.error is not None and message in result.error.message


def test_a_missing_compose_scope_points_at_the_compose_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, transport = compose_adapter(grant_can_write=False)
    registry = ProviderRegistry()
    registry.register(adapter)
    monkeypatch.setattr(application, "registry_from_environment", lambda **_: registry)

    result = application.run_application(_payload())

    assert result.error is not None and result.error.code == "write_scope_missing"
    assert result.error.remediation == (f"solercia-connect connect gmail-compose {ACCOUNT}",)
    assert transport.requests == []


def test_the_application_runs_the_draft_through_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, transport = compose_adapter(profile(), created())
    registry = ProviderRegistry()
    registry.register(adapter)
    calls: list[dict[str, Any]] = []

    def build(**kwargs: Any) -> ProviderRegistry:
        calls.append(kwargs)
        return registry

    monkeypatch.setattr(application, "registry_from_environment", build)

    result = application.run_application(_payload(cc=["copy@example.net"]))

    assert result.ok is True
    assert calls[0]["connection_type"] is ConnectionType.GMAIL_COMPOSE
    assert calls[0]["include_browser"] is False
    record = result.data["records"][0] if result.data else {}
    assert record["draft_id"] == "r-1"
    assert record["body_preview"] == "Cuerpo del correo.\n"
    assert [item["address"] for item in record["cc"]] == ["copy@example.net"]
    assert len(posts(transport)) == 1


def test_the_read_only_gmail_target_cannot_create_a_draft() -> None:
    read_adapter = GoogleGmailAdapter(
        FakeTransport(), BearerTokenProvider(SecretValue("read-secret")), expected_account=ACCOUNT
    )
    registry = ProviderRegistry()
    registry.register(read_adapter)

    outcome = ConnectorRuntime(registry).execute(
        ConnectorRequest.create("google.gmail", Capability.MAIL_DRAFT_CREATE, draft_request())
    )

    assert Capability.MAIL_DRAFT_CREATE not in GoogleGmailAdapter.descriptor.capabilities
    assert "mail.draft.create" not in target_spec(ConnectionType.GMAIL).capabilities
    assert outcome.receipt.final_reason is ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE


@pytest.mark.parametrize("interactive", [False, True])
def test_http_rejects_the_draft_before_the_application_runs(
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
        json={"type": "gmail-compose", "account": ACCOUNT, "capability": "mail.draft.create"},
    )
    with_fields = client.post(
        "/v1/run",
        headers=_HEADERS,
        json={
            "type": "gmail-compose",
            "account": ACCOUNT,
            "capability": "mail.draft.create",
            "subject": "Informe",
        },
    )

    assert plain.status_code == 403
    assert plain.json()["error"]["code"] == "write_not_available_over_http"
    assert "mail.draft.create" in plain.json()["error"]["message"]
    assert with_fields.status_code == 422


def _cli_args(*extra: str) -> list[str]:
    return [
        "run",
        "--provider",
        "google.gmail",
        "--capability",
        "mail.draft.create",
        "--account",
        ACCOUNT,
        "--to",
        "recipient@example.net",
        "--subject",
        "Informe semanal",
        *extra,
    ]


def _capture_payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def run(payload: dict[str, Any], **_: Any) -> Any:
        captured.update(payload)
        return ResultEnvelope.success(operation="run", data={"records": []})

    monkeypatch.setattr(cli, "run_application", run)
    return captured


def test_the_cli_reads_the_body_file_and_repeats_recipients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = tmp_path / "body.txt"
    body.write_text("Hola,\nlínea dos\n", encoding="utf-8")
    captured = _capture_payload(monkeypatch)

    result = runner.invoke(
        cli.app,
        _cli_args(
            "--to",
            "second@example.net",
            "--cc",
            "copy@example.net",
            "--bcc",
            "hidden@example.net",
            "--body-file",
            str(body),
            "--allow-write",
        ),
        env={},
    )

    assert result.exit_code == 0, result.stdout
    assert captured["type"] == "gmail-compose"
    assert captured["to"] == ["recipient@example.net", "second@example.net"]
    assert captured["cc"] == ["copy@example.net"]
    assert captured["bcc"] == ["hidden@example.net"]
    assert captured["subject"] == "Informe semanal"
    assert captured["body"] == "Hola,\nlínea dos\n"
    assert captured["allow_write"] is True


def test_the_cli_reads_the_body_from_piped_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_payload(monkeypatch)

    result = runner.invoke(
        cli.app,
        _cli_args("--body-stdin", "--allow-write"),
        input="Cuerpo por la entrada estándar\n",
        env={},
    )

    assert result.exit_code == 0, result.stdout
    assert captured["body"] == "Cuerpo por la entrada estándar\n"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--body-file", "{missing}"), "--body-file could not be read"),
        (("--body-file", "{latin1}"), "--body-file must be UTF-8 text"),
        (("--body-file", "{large}"), "--body-file exceeds"),
        (("--body-file", "{ok}", "--body-stdin"), "mutually exclusive"),
        ((), "needs --body-file or --body-stdin"),
    ],
    ids=["missing", "not-utf8", "too-large", "both", "neither"],
)
def test_the_cli_refuses_an_unusable_body_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: tuple[str, ...],
    message: str,
) -> None:
    (tmp_path / "latin1.txt").write_bytes("cañón".encode("latin-1"))
    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_DRAFT_BODY_BYTES + 1))
    (tmp_path / "ok.txt").write_text("ok", encoding="utf-8")
    paths = {name: str(tmp_path / f"{name}.txt") for name in ("missing", "latin1", "large", "ok")}
    monkeypatch.setattr(cli, "run_application", _no_registry)

    result = runner.invoke(
        cli.app,
        _cli_args(*(item.format(**paths) for item in extra), "--allow-write"),
        env={},
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "invalid_input"
    assert message in payload["error"]["message"]


def test_the_cli_refuses_body_options_on_another_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = tmp_path / "body.txt"
    body.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cli, "run_application", _no_registry)

    result = runner.invoke(
        cli.app,
        [
            "run",
            "--provider",
            "google.gmail",
            "--capability",
            "mail.sent.list",
            "--account",
            ACCOUNT,
            "--body-file",
            str(body),
        ],
        env={},
    )

    assert result.exit_code == 2
    assert "apply only to mail.draft.create" in json.loads(result.stdout)["error"]["message"]


def test_the_cli_without_allow_write_exits_with_input_error_and_no_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = tmp_path / "body.txt"
    body.write_text("x", encoding="utf-8")
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = runner.invoke(cli.app, _cli_args("--body-file", str(body)), env={})

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "write_not_authorized"
    assert payload["receipt"] is None


@pytest.mark.parametrize("values", [(), ("Done", "Closed")], ids=["none", "two"])
def test_the_jira_transition_still_takes_exactly_one_to(
    monkeypatch: pytest.MonkeyPatch, values: tuple[str, ...]
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)
    to_args = [arg for value in values for arg in ("--to", value)]

    result = runner.invoke(
        cli.app,
        [
            "run",
            "--provider",
            "jira.cloud",
            "--capability",
            "external.issues.transition",
            "--account",
            ACCOUNT,
            "--issue",
            "CP-1356",
            *to_args,
            "--allow-write",
        ],
        env={},
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "invalid_input"
    assert "to" in payload["error"]["message"]


def test_the_legacy_query_helper_refuses_the_draft_write() -> None:
    with pytest.raises(cli._InputError, match=r"mail\.draft\.create is a write"):
        cli._query_payload(
            Capability.MAIL_DRAFT_CREATE,
            start=None,
            end=None,
            account=ACCOUNT,
            calendar_id="primary",
            assignee=None,
            jql=None,
            meeting_id=None,
            limit=50,
        )


# --- Connection target ---------------------------------------------------------------------


def _settings(tmp_path: Path) -> Any:
    return load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
        }
    )


def test_gmail_compose_is_a_separate_least_privilege_target() -> None:
    spec = target_spec("gmail-compose")

    assert ConnectionType.parse("GMAIL_COMPOSE") is ConnectionType.GMAIL_COMPOSE
    assert spec.provider_id == "google.gmail"
    assert "mail.draft.create" in spec.capabilities
    assert not {"mail.sent.list", "mail.inbox.list"} & set(spec.capabilities)
    assert spec.oauth_scopes == ("openid", "email", GMAIL_COMPOSE_SCOPE)
    assert GMAIL_COMPOSE_SCOPE == "https://www.googleapis.com/auth/gmail.compose"
    assert target_spec("gmail").oauth_scopes == (
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.readonly",
    )
    assert has_browser_route(ConnectionType.GMAIL_COMPOSE) is False
    assert Capability.MAIL_DRAFT_CREATE in WRITE_CAPABILITIES
    assert all(
        Capability.MAIL_DRAFT_CREATE not in allowed
        for allowed in BROWSER_FALLBACK_CAPABILITIES.values()
    )
    rows = [row for row in CAPABILITY_MATRIX if row.capability == "mail.draft.create"]
    assert [(row.channel, row.v0_1_status) for row in rows] == [("official", "implemented-offline")]


def test_connect_status_and_disconnect_use_the_compose_token_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    store = MemoryTokenStore()
    flows: list[object] = []

    def callback_flow(oauth: object, open_browser: bool) -> None:
        flows.append(oauth)
        store.save(f"gmail-compose:{ACCOUNT}", tokens(*compose_scopes()))

    service = ConnectionService(
        settings,
        profiles,
        store,
        oauth_builder=lambda type, account: BearerTokenProvider(SecretValue("compose")),
        google_callback_flow=callback_flow,
        transport=FakeTransport(profile(), profile()),
        clock=lambda: NOW,
    )

    connected = service.connect("gmail-compose", ACCOUNT, open_browser=False)
    status = service.status("gmail-compose", ACCOUNT)
    disconnected = service.disconnect("gmail-compose", ACCOUNT)

    assert connected.status == "connected_official"
    assert connected.data is not None and connected.data["capabilities"] == list(
        target_spec("gmail-compose").capabilities
    )
    assert len(flows) == 1
    assert status.status == "connected_official"
    assert disconnected.status == "disconnected"
    assert store.deleted == [f"gmail-compose:{ACCOUNT}"]
    assert profiles.get(ConnectionType.GMAIL_COMPOSE, ACCOUNT) is None


def test_status_reports_a_compose_grant_without_gmail_compose(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profiles = ProfileStore.from_settings(settings)
    profiles.save(ConnectionProfile.create("gmail-compose", ACCOUNT, now=NOW))
    store = MemoryTokenStore({f"gmail-compose:{ACCOUNT}": tokens("openid", "email")})
    service = ConnectionService(
        settings,
        profiles,
        store,
        oauth_builder=lambda type, account: BearerTokenProvider(SecretValue("compose")),
        transport=FakeTransport(),
        clock=lambda: NOW,
    )

    result = service.status("gmail-compose", ACCOUNT)

    assert result.status != "connected_official"
    assert result.error is not None
    assert f"solercia-connect connect gmail-compose {ACCOUNT}" in result.error.remediation


def test_connect_refuses_a_compose_grant_for_another_identity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = MemoryTokenStore()
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        store,
        oauth_builder=lambda type, account: BearerTokenProvider(SecretValue("compose")),
        google_callback_flow=lambda oauth, open_browser: None,
        transport=FakeTransport(profile("other@example.com")),
        clock=lambda: NOW,
    )

    result = service.connect("gmail-compose", ACCOUNT, open_browser=False)

    assert result.ok is False
    assert ProfileStore.from_settings(settings).get(ConnectionType.GMAIL_COMPOSE, ACCOUNT) is None


def test_doctor_treats_gmail_compose_as_an_oauth_target(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    service = DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=lambda name: True,
        keyring_probe=lambda: (True, "test-backend"),
        browser_probe=lambda: (False, "not applicable"),
    )

    report = service.inspect("gmail-compose", ACCOUNT).to_dict()

    checks = {check["name"]: check["status"] for check in report["checks"]}
    assert checks.get("keyring_backend") == "ready"
    assert report["ready"] is True


@pytest.mark.parametrize(
    ("granted", "can_write"),
    [(compose_scopes(), True), (("openid", "email"), False)],
    ids=["compose-grant", "no-compose-scope"],
)
def test_bootstrap_builds_the_compose_adapter_from_its_own_connection(
    tmp_path: Path, granted: tuple[str, ...], can_write: bool
) -> None:
    settings = _settings(tmp_path)
    ProfileStore.from_settings(settings).save(
        ConnectionProfile.create("gmail-compose", ACCOUNT, now=NOW)
    )
    store = MemoryTokenStore({f"gmail-compose:{ACCOUNT}": tokens(*granted)})

    registry = registry_from_environment(
        connection_type="gmail-compose",
        account=ACCOUNT,
        capability=Capability.MAIL_DRAFT_CREATE,
        token_store=store,
        environ={"GOOGLE_ACCESS_TOKEN": "an-env-token-never-used-for-writes"},
        include_browser=False,
        settings_override=settings,
        transport=FakeTransport(),
    )

    adapter = registry.official("google.gmail")
    assert isinstance(adapter, GmailComposeAdapter)
    if can_write:
        assert Capability.MAIL_DRAFT_CREATE in adapter.descriptor.capabilities
    else:
        with pytest.raises(ConnectorFailure) as raised:
            adapter.create_draft(draft_request())
        assert raised.value.reason is ReasonCode.WRITE_SCOPE_MISSING


def test_bootstrap_without_a_compose_connection_names_the_connect_command(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="solercia-connect connect gmail-compose"):
        registry_from_environment(
            connection_type="gmail-compose",
            account=ACCOUNT,
            token_store=MemoryTokenStore(),
            environ={},
            include_browser=False,
            settings_override=_settings(tmp_path),
            transport=FakeTransport(),
        )


def test_bootstrap_without_a_stored_compose_grant_asks_to_reconnect(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ProfileStore.from_settings(settings).save(
        ConnectionProfile.create("gmail-compose", ACCOUNT, now=NOW)
    )

    with pytest.raises(ConnectorFailure) as raised:
        registry_from_environment(
            connection_type="gmail-compose",
            account=ACCOUNT,
            token_store=MemoryTokenStore(),
            environ={},
            include_browser=False,
            settings_override=settings,
            transport=FakeTransport(),
        )

    assert raised.value.reason is ReasonCode.INVALID_CREDENTIALS
