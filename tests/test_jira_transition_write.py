"""Moving one Jira issue to another status: the second bounded write Solercia Connect performs.

Decision: docs/decisions/2026-10-25-escritura-transiciones-jira.md. Every call goes through the
FakeTransport; nothing here reaches a real Jira site.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import solercia_connect.application as application
import solercia_connect.cli as cli
from solercia_connect.auth import BasicAuthProvider
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
    IssueTransitionRequest,
    IssueTransitionResult,
    IssueTransitionsQuery,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    StepOutcome,
)
from solercia_connect.envelope import ResultEnvelope
from solercia_connect.errors import ConnectorFailure
from solercia_connect.matrix import CAPABILITY_MATRIX
from solercia_connect.oauth import OAuthTokenSet
from solercia_connect.ports import TransportResponse
from solercia_connect.providers.jira import JiraAdapter
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import BROWSER_FALLBACK_CAPABILITIES, ConnectorRuntime
from solercia_connect.secret_store import KeyringSecretStore
from solercia_connect.secrets import SecretValue
from solercia_connect.service import create_app
from tests.fakes import NOW, FakeAdapter, FakeTransport, json_response
from tests.test_jira_transitions import (
    _CLOSE,
    _REVIEW,
    _START,
    ACCOUNT,
    BASIC,
    ISSUE,
    ISSUE_STATUS,
    MYSELF,
    SITE,
    TOKEN,
    TRANSITIONS,
    MemoryKeyring,
    _connected_api_token,
    _issue,
    _myself,
    _no_registry,
    _route_runs_to,
    _transition,
    _transitions,
    _values,
)

POST = f"{SITE}/rest/api/3/issue/{ISSUE}/transitions"
CLOUD_ID = "11223344-a1b2-3b33-c444-def123456789"
_HEADERS = {"X-Solercia-Connect-Key": "local-api-key"}

runner = CliRunner()


def _no_content() -> TransportResponse:
    return TransportResponse(204, {}, b"")


def _writer(transport: FakeTransport, *, grant_can_write: bool = True) -> JiraAdapter:
    return JiraAdapter(
        transport,
        BasicAuthProvider(ACCOUNT, SecretValue(TOKEN)),
        base_url=SITE,
        expected_account_id="acct-1",
        expected_account=ACCOUNT,
        grant_can_write=grant_can_write,
        clock=lambda: NOW,
    )


def _request(to: str = "En curso", *, authorized: bool = True) -> IssueTransitionRequest:
    return IssueTransitionRequest(ISSUE, to, ACCOUNT, authorized)


def _posts(transport: FakeTransport) -> list[Any]:
    return [request for request in transport.requests if request.method == "POST"]


# --- Domain ------------------------------------------------------------------------------------


def test_a_transition_request_names_one_issue_and_a_trimmed_target_status() -> None:
    request = IssueTransitionRequest(ISSUE, "  En curso ")

    assert request.issue_key == ISSUE
    assert request.to_status == "En curso"
    assert request.write_authorized is False
    with pytest.raises(ValueError, match="exact Jira issue key"):
        IssueTransitionRequest("CP-1356,CP-1357", "En curso")
    with pytest.raises(ValueError, match="target status"):
        IssueTransitionRequest(ISSUE, "   ")


def test_the_transition_is_a_write_and_the_listing_is_not() -> None:
    assert Capability.EXTERNAL_ISSUES_TRANSITION in WRITE_CAPABILITIES
    assert Capability.EXTERNAL_ISSUES_TRANSITIONS_LIST not in WRITE_CAPABILITIES
    assert Capability.MAIL_CATEGORIES_SET in WRITE_CAPABILITIES


# --- Official adapter: the write ---------------------------------------------------------------


def test_a_transition_reads_writes_one_transition_id_and_reads_the_status_back() -> None:
    transport = FakeTransport(
        _myself(),
        _issue("Por hacer"),
        _transitions(_START, _REVIEW, _CLOSE),
        _no_content(),
        _issue("En curso", "indeterminate"),
    )

    (record,) = _writer(transport).transition_issue(_request("  en CURSO "))

    assert [(request.method, request.url) for request in transport.requests] == [
        ("GET", MYSELF),
        ("GET", ISSUE_STATUS),
        ("GET", TRANSITIONS),
        ("POST", POST),
        ("GET", ISSUE_STATUS),
    ]
    assert _posts(transport)[0].json_body == {"transition": {"id": "11"}}
    assert all(
        request.json_body is None for request in transport.requests if request.method == "GET"
    )
    assert all(
        request.headers and request.headers["Authorization"] == BASIC
        for request in transport.requests
    )
    assert isinstance(record, IssueTransitionResult)
    assert (
        record.issue_key,
        record.previous_status,
        record.status,
        record.transition_id,
        record.transition_name,
        record.changed,
    ) == (ISSUE, "Por hacer", "En curso", "11", "Iniciar", True)
    assert record.provenance.provider_id == "jira.cloud"
    assert record.provenance.route is RouteKind.OFFICIAL
    assert record.provenance.account_id == "acct-1"
    assert record.provenance.source_id == ISSUE


def test_a_transition_with_only_optional_fields_still_writes_nothing_but_its_id() -> None:
    transport = FakeTransport(
        _myself(),
        _issue("Por hacer"),
        _transitions(_REVIEW),
        _no_content(),
        _issue("En revisión"),
    )

    (record,) = _writer(transport).transition_issue(_request("En revisión"))

    assert record.changed is True
    assert _posts(transport)[0].json_body == {"transition": {"id": "21"}}


def test_an_issue_already_in_the_target_status_is_not_written() -> None:
    transport = FakeTransport(_myself(), _issue("En curso", "indeterminate"))

    (record,) = _writer(transport).transition_issue(_request(" EN CURSO"))

    assert [request.url for request in transport.requests] == [MYSELF, ISSUE_STATUS]
    assert (record.previous_status, record.status, record.changed) == (
        "En curso",
        "En curso",
        False,
    )
    assert (record.transition_id, record.transition_name) == (None, None)


def test_no_transition_to_the_target_fails_closed_and_names_the_available_targets() -> None:
    transport = FakeTransport(_myself(), _issue(), _transitions(_START, _REVIEW, _CLOSE))

    with pytest.raises(ConnectorFailure) as captured:
        _writer(transport).transition_issue(_request("Bloqueado"))

    assert captured.value.reason is ReasonCode.TRANSITION_UNAVAILABLE
    for name in ("En curso", "En revisión", "Hecho"):
        assert name in captured.value.safe_message
    assert _posts(transport) == []


def test_an_issue_without_available_transitions_says_so() -> None:
    transport = FakeTransport(_myself(), _issue(), _transitions())

    with pytest.raises(ConnectorFailure) as captured:
        _writer(transport).transition_issue(_request("En curso"))

    assert captured.value.reason is ReasonCode.TRANSITION_UNAVAILABLE
    assert "no transition" in captured.value.safe_message.casefold()
    assert _posts(transport) == []


def test_several_transitions_to_the_target_fail_closed() -> None:
    other_start = _transition("12", "Retomar", "en curso")
    transport = FakeTransport(_myself(), _issue(), _transitions(_START, other_start))

    with pytest.raises(ConnectorFailure) as captured:
        _writer(transport).transition_issue(_request("En curso"))

    assert captured.value.reason is ReasonCode.TRANSITION_AMBIGUOUS
    assert _posts(transport) == []


def test_a_transition_that_requires_fields_fails_closed() -> None:
    transport = FakeTransport(_myself(), _issue(), _transitions(_START, _CLOSE))

    with pytest.raises(ConnectorFailure) as captured:
        _writer(transport).transition_issue(_request("Hecho"))

    assert captured.value.reason is ReasonCode.TRANSITION_REQUIRES_FIELDS
    assert _posts(transport) == []


def test_the_adapter_refuses_an_unauthorized_write_before_any_provider_call() -> None:
    transport = FakeTransport()

    with pytest.raises(ConnectorFailure) as captured:
        _writer(transport).transition_issue(_request(authorized=False))

    assert captured.value.reason is ReasonCode.WRITE_NOT_AUTHORIZED
    assert transport.requests == []


def test_an_oauth_grant_without_write_scope_fails_closed_before_any_provider_call() -> None:
    transport = FakeTransport()

    with pytest.raises(ConnectorFailure) as captured:
        _writer(transport, grant_can_write=False).transition_issue(_request())

    assert captured.value.reason is ReasonCode.WRITE_SCOPE_MISSING
    assert "write:jira-work" in captured.value.safe_message
    assert "API token" in captured.value.safe_message
    assert transport.requests == []


def test_a_status_that_does_not_match_after_the_write_fails_as_unverified() -> None:
    transport = FakeTransport(
        _myself(), _issue(), _transitions(_START), _no_content(), _issue("Por hacer")
    )

    with pytest.raises(ConnectorFailure) as captured:
        _writer(transport).transition_issue(_request())

    assert captured.value.reason is ReasonCode.WRITE_UNVERIFIED
    assert "may have applied" in captured.value.safe_message
    assert len(_posts(transport)) == 1


def test_a_read_back_that_fails_after_the_write_is_also_unverified() -> None:
    transport = FakeTransport(
        _myself(),
        _issue(),
        _transitions(_START),
        _no_content(),
        json_response(503, {"errorMessages": ["down"]}),
    )

    with pytest.raises(ConnectorFailure) as captured:
        _writer(transport).transition_issue(_request())

    assert captured.value.reason is ReasonCode.WRITE_UNVERIFIED
    assert "may have applied" in captured.value.safe_message
    assert len(_posts(transport)) == 1


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (404, ReasonCode.ISSUE_NOT_FOUND),
        (401, ReasonCode.INVALID_CREDENTIALS),
        (403, ReasonCode.AUTHORIZATION_DENIED),
        (409, ReasonCode.WRITE_CONFLICT),
        (400, ReasonCode.REQUEST_REJECTED),
    ],
)
def test_a_rejected_write_is_typed_and_sent_once(status: int, reason: ReasonCode) -> None:
    transport = FakeTransport(
        _myself(),
        _issue(),
        _transitions(_START),
        json_response(status, {"errorMessages": ["raw provider text"]}),
    )

    with pytest.raises(ConnectorFailure) as captured:
        _writer(transport).transition_issue(_request())

    assert captured.value.reason is reason
    assert "raw provider text" not in captured.value.safe_message
    assert len(_posts(transport)) == 1


def test_a_missing_issue_is_typed_before_any_write() -> None:
    transport = FakeTransport(_myself(), json_response(404, {"errorMessages": ["missing"]}))

    with pytest.raises(ConnectorFailure) as captured:
        _writer(transport).transition_issue(_request())

    assert captured.value.reason is ReasonCode.ISSUE_NOT_FOUND
    assert _posts(transport) == []


def test_another_jira_identity_is_never_written() -> None:
    transport = FakeTransport(_myself(account_id="acct-other"))

    with pytest.raises(ConnectorFailure) as captured:
        _writer(transport).transition_issue(_request())

    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert [request.url for request in transport.requests] == [MYSELF]


# --- Runtime -----------------------------------------------------------------------------------


def _write_request(*, authorized: bool = True) -> ConnectorRequest:
    return ConnectorRequest.create(
        "jira.cloud", Capability.EXTERNAL_ISSUES_TRANSITION, _request(authorized=authorized)
    )


def _no_sleep(delay: float) -> None:
    raise AssertionError(f"A write must never schedule a retry (delay {delay})")


def test_a_provider_error_on_the_write_is_never_retried() -> None:
    transport = FakeTransport(
        _myself(), _issue(), _transitions(_START), json_response(503, {"errorMessages": ["x"]})
    )
    registry = ProviderRegistry()
    registry.register(_writer(transport))

    outcome = ConnectorRuntime(registry, sleeper=_no_sleep).execute(_write_request())

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.PROVIDER_UNAVAILABLE
    assert len(_posts(transport)) == 1
    assert [step.outcome for step in outcome.receipt.steps if step.route is RouteKind.OFFICIAL] == [
        StepOutcome.FAILURE
    ]


def test_the_runtime_refuses_an_unauthorized_write_without_any_provider_call() -> None:
    transport = FakeTransport()
    registry = ProviderRegistry()
    registry.register(_writer(transport))

    outcome = ConnectorRuntime(registry).execute(_write_request(authorized=False))

    assert outcome.receipt.final_reason is ReasonCode.WRITE_NOT_AUTHORIZED
    assert transport.requests == []


@pytest.mark.parametrize(
    "capability",
    [Capability.EXTERNAL_ISSUES_TRANSITION, Capability.EXTERNAL_ISSUES_TRANSITIONS_LIST],
)
def test_no_browser_route_serves_jira_transitions_even_when_fallback_is_allowed(
    capability: Capability,
) -> None:
    official = FakeAdapter(
        "jira.cloud",
        ProviderFamily.JIRA,
        RouteKind.OFFICIAL,
        frozenset({capability}),
        ConnectorFailure(ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE, "official route unavailable"),
    )
    browser = FakeAdapter(
        "jira.browser",
        ProviderFamily.JIRA,
        RouteKind.BROWSER,
        frozenset({capability, Capability.EXTERNAL_ISSUES_SEARCH}),
    )
    registry = ProviderRegistry()
    registry.register(official)
    registry.register(browser)
    payload = (
        _request()
        if capability is Capability.EXTERNAL_ISSUES_TRANSITION
        else IssueTransitionsQuery(ISSUE, ACCOUNT)
    )
    request = ConnectorRequest.create("jira.cloud", capability, payload)

    outcome = ConnectorRuntime(registry, sleeper=lambda _: None).execute(
        request, browser_fallback_enabled=True
    )

    assert outcome.ok is False
    assert browser.calls == 0
    assert outcome.receipt.steps[-1].reason is ReasonCode.BROWSER_FALLBACK_DENIED
    assert capability not in BROWSER_FALLBACK_CAPABILITIES[ProviderFamily.JIRA]


# --- Application, CLI and HTTP -----------------------------------------------------------------


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "jira",
        "account": ACCOUNT,
        "capability": "external.issues.transition",
        "issue": ISSUE,
        "to": "En curso",
        "allow_write": True,
    }
    payload.update(overrides)
    return payload


def test_an_authorized_run_transitions_the_issue_over_the_official_route_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _connected_api_token(tmp_path)
    transport = FakeTransport(
        _myself(), _issue(), _transitions(_START, _CLOSE), _no_content(), _issue("En curso")
    )
    _route_runs_to(monkeypatch, tmp_path, keyring, transport)

    result = application.run_application(_payload(allow_browser_fallback=True))

    assert result.ok is True, result.error
    assert result.route == "official"
    (record,) = result.to_dict()["data"]["records"]
    assert record["schema"] == "solercia.connect/record/v1"
    assert (record["previous_status"], record["status"], record["changed"]) == (
        "Por hacer",
        "En curso",
        True,
    )
    assert (record["transition_id"], record["transition_name"]) == ("11", "Iniciar")
    assert [request.url for request in transport.requests] == [
        MYSELF,
        ISSUE_STATUS,
        TRANSITIONS,
        POST,
        ISSUE_STATUS,
    ]
    assert result.to_dict()["receipt"]["capability"] == "external.issues.transition"
    assert TOKEN not in str(result.to_dict())


def test_a_failed_run_explains_itself_with_the_available_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _connected_api_token(tmp_path)
    transport = FakeTransport(_myself(), _issue(), _transitions(_START, _REVIEW))
    _route_runs_to(monkeypatch, tmp_path, keyring, transport)

    result = application.run_application(_payload(to="Bloqueado"))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "transition_unavailable"
    assert "En curso" in result.error.message and "En revisión" in result.error.message
    assert _posts(transport) == []


class _StoredOAuthGrant:
    """An Atlassian grant that is present; the write must refuse it before using it."""

    def load(self, key: str) -> OAuthTokenSet | None:
        return OAuthTokenSet(
            SecretValue("atlassian-access-token-value"),
            SecretValue("atlassian-refresh-token-value"),
            datetime.now(UTC) + timedelta(hours=1),
            ("read:jira-work", "read:jira-user", "read:me", "offline_access"),
        )

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        raise AssertionError(f"The OAuth grant must not be written for {key}")

    def delete(self, key: str) -> None:
        raise AssertionError(f"The OAuth grant must not be deleted for {key}")


def test_an_oauth_connection_cannot_write_and_is_told_to_use_an_api_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        **_values(tmp_path),
        "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID": "atlassian-client-id",
        "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET": "atlassian-client-secret-value",
    }
    ProfileStore.from_settings(load_settings(environ=values)).save(
        ConnectionProfile.create(
            "jira",
            ACCOUNT,
            site=SITE,
            provider_account_id="acct-1",
            token_kind="oauth",
            cloud_id=CLOUD_ID,
        )
    )
    transport = FakeTransport()

    def build(**kwargs: Any) -> Any:
        return registry_from_environment(
            **kwargs,
            environ=values,
            token_store=_StoredOAuthGrant(),
            secret_store=KeyringSecretStore(backend=MemoryKeyring()),
            transport=transport,
        )

    monkeypatch.setattr(application, "registry_from_environment", build)

    result = application.run_application(_payload())

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "write_scope_missing"
    assert "write:jira-work" in result.error.message
    assert any("--token-env" in step for step in result.error.remediation)
    assert transport.requests == []


def test_without_allow_write_the_run_fails_before_any_provider_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(allow_write=False))

    assert result.status == "invalid_input"
    assert result.error is not None
    assert result.error.code == "write_not_authorized"
    assert "external.issues.transition" in result.error.message
    assert "--allow-write" in result.error.message
    (command,) = result.error.remediation
    assert "--capability external.issues.transition" in command
    assert "--issue" in command and "--to" in command and "--allow-write" in command
    assert result.receipt is None


def test_the_application_never_transitions_for_a_caller_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    for browser_via_http in (False, True):
        result = application.run_application(
            _payload(), interactive=False, browser_via_http=browser_via_http
        )

        assert result.status == "forbidden"
        assert result.error is not None
        assert result.error.code == "write_not_available_over_http"
        assert "external.issues.transition" in result.error.message


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"to": None}, "to is required"),
        ({"issue": None}, "issue is required"),
        ({"issue": "CP-1356 CP-1357"}, "exact Jira issue key"),
        ({"issue": "project = CP AND status = Abierto"}, "exact Jira issue key"),
        ({"account": None}, "account is required for external.issues.transition"),
        ({"allow_write": "yes"}, "allow_write must be a boolean"),
        ({"add": ["Resuelto"]}, "mail.categories.set"),
        ({"capability": "external.issues.transitions.list"}, "external.issues.transition"),
        (
            {"capability": "external.issues.transitions.list", "allow_write": False},
            "to applies only to external.issues.transition",
        ),
    ],
)
def test_malformed_transition_requests_are_invalid_input(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], message: str
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(**overrides))

    assert result.status == "invalid_input"
    assert result.error is not None and message in result.error.message


def _cli_args(*extra: str) -> list[str]:
    return [
        "run",
        "--provider",
        "jira.cloud",
        "--capability",
        "external.issues.transition",
        "--account",
        ACCOUNT,
        "--issue",
        ISSUE,
        "--to",
        "En curso",
        *extra,
    ]


def test_the_cli_passes_the_issue_target_and_write_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_run(payload: dict[str, Any]) -> Any:
        seen.append(payload)
        return ResultEnvelope.success(operation="run", data={"records": []})

    monkeypatch.setattr(cli, "run_application", fake_run)

    result = runner.invoke(cli.app, _cli_args("--allow-write"), env={})

    assert result.exit_code == 0, result.stdout
    (payload,) = seen
    assert (payload["type"], payload["issue"], payload["to"]) == ("jira", ISSUE, "En curso")
    assert payload["allow_write"] is True
    assert payload["allow_browser_fallback"] is False


def test_the_cli_without_allow_write_exits_with_input_error_and_no_provider_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = runner.invoke(cli.app, _cli_args(), env={})

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "write_not_authorized"
    assert payload["receipt"] is None


@pytest.mark.parametrize("interactive", [False, True])
def test_http_rejects_the_transition_before_the_application_runs(
    tmp_path: Path, interactive: bool
) -> None:
    def run_handler(payload: dict[str, Any], *, browser_via_http: bool = False) -> Any:
        raise AssertionError("HTTP must not reach the application for a write")

    values = {**_values(tmp_path), "SOLERCIA_CONNECT_API_KEY": "local-api-key"}
    if interactive:
        values["SOLERCIA_CONNECT_BROWSER_FALLBACK"] = "true"
        values["SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN"] = "true"
    client = TestClient(create_app(settings=load_settings(environ=values), run_handler=run_handler))

    plain = client.post(
        "/v1/run",
        headers=_HEADERS,
        json={"type": "jira", "account": ACCOUNT, "capability": "external.issues.transition"},
    )
    with_write_fields = client.post(
        "/v1/run",
        headers=_HEADERS,
        json={
            "type": "jira",
            "account": ACCOUNT,
            "capability": "external.issues.transition",
            "issue": ISSUE,
            "to": "En curso",
        },
    )

    assert plain.status_code == 403
    assert plain.json()["error"]["code"] == "write_not_available_over_http"
    assert "external.issues.transition" in plain.json()["error"]["message"]
    assert with_write_fields.status_code == 422


# --- Contract surface --------------------------------------------------------------------------


def test_the_jira_target_offers_the_write_on_the_official_route_only() -> None:
    capability = Capability.EXTERNAL_ISSUES_TRANSITION

    assert capability.value in target_spec(ConnectionType.JIRA).capabilities
    assert capability in JiraAdapter.descriptor.capabilities
    assert BROWSER_FALLBACK_CAPABILITIES[ProviderFamily.JIRA] == frozenset(
        {Capability.EXTERNAL_ISSUES_SEARCH}
    )
    rows = [row for row in CAPABILITY_MATRIX if row.capability == capability.value]
    assert [(row.provider, row.channel, row.v0_1_status) for row in rows] == [
        ("Jira Cloud REST v3", "official", "implemented-offline")
    ]


def test_the_jira_adapter_has_exactly_one_write_body() -> None:
    source = (Path(__file__).parents[1] / "src/solercia_connect/providers/jira.py").read_text()

    assert source.count('json_body={"transition": {"id": ') == 1
    assert '"PUT"' not in source and '"DELETE"' not in source
    assert source.count('"POST"') == 2  # the JQL search and the one transition


def test_the_legacy_query_helper_refuses_to_build_the_transition() -> None:
    with pytest.raises(cli._InputError, match=r"external\.issues\.transition is a write"):
        cli._query_payload(
            Capability.EXTERNAL_ISSUES_TRANSITION,
            start=None,
            end=None,
            account=ACCOUNT,
            calendar_id="primary",
            assignee=None,
            jql=None,
            meeting_id=None,
            issue=ISSUE,
            limit=50,
        )


def test_docs_declare_the_jira_transition_and_its_flag() -> None:
    root = Path(__file__).parents[1]
    assert (root / "docs/decisions/2026-10-25-escritura-transiciones-jira.md").is_file()
    for relative in (
        "README.md",
        "docs/runbooks/jira.md",
        "docs/USO-CON-AGENTES.md",
        "agents/skills/solercia-connect/SKILL.md",
    ):
        text = (root / relative).read_text()
        assert re.search(r"external\.issues\.transition(?![s.])", text), relative
        assert "external.issues.transitions.list" in text, relative
        assert "--allow-write" in text, relative
        assert "--issue" in text and "--to" in text, relative
