"""Jira issue transitions: the read that lists them and the one bounded write that performs one.

Decision: docs/decisions/2026-10-25-escritura-transiciones-jira.md. Every call goes through the
FakeTransport; nothing here reaches a real Jira site.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import solercia_connect.application as application
import solercia_connect.cli as cli
from solercia_connect.auth import BasicAuthProvider
from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.config import ConnectionType, ProfileStore, load_settings, target_spec
from solercia_connect.connections import ConnectionService
from solercia_connect.domain import (
    Capability,
    IssueTransitionOption,
    IssueTransitionsQuery,
    ProviderFamily,
    ReasonCode,
    RouteKind,
)
from solercia_connect.envelope import ResultEnvelope
from solercia_connect.errors import ConnectorFailure
from solercia_connect.matrix import CAPABILITY_MATRIX
from solercia_connect.oauth import OAuthTokenSet
from solercia_connect.providers.jira import JiraAdapter
from solercia_connect.runtime import BROWSER_FALLBACK_CAPABILITIES
from solercia_connect.secret_store import KeyringSecretStore
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport, json_response

ACCOUNT = "user@example.com"
SITE = "https://example.atlassian.net"
TOKEN = "jira-api-token-value"
BASIC = "Basic " + base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()
ISSUE = "CP-1356"
MYSELF = f"{SITE}/rest/api/3/myself"
ISSUE_STATUS = f"{SITE}/rest/api/3/issue/{ISSUE}?fields=status"
TRANSITIONS = f"{SITE}/rest/api/3/issue/{ISSUE}/transitions?expand=transitions.fields"

runner = CliRunner()


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        del self.values[(service_name, username)]


class UnusedOAuthStore:
    """An API-token connection must never touch OAuth token state."""

    def load(self, key: str) -> OAuthTokenSet | None:
        raise AssertionError(f"OAuth token store must not be read for {key}")

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        raise AssertionError(f"OAuth token store must not be written for {key}")

    def delete(self, key: str) -> None:
        raise AssertionError(f"OAuth token store must not be deleted for {key}")


def _myself(account_id: str = "acct-1", email: str | None = ACCOUNT) -> Any:
    payload: dict[str, Any] = {"accountId": account_id, "displayName": "User"}
    if email is not None:
        payload["emailAddress"] = email
    return json_response(200, payload)


def _issue(status: str = "Por hacer", category: str = "new", key: str = ISSUE) -> Any:
    return json_response(
        200,
        {
            "id": "10001",
            "key": key,
            "fields": {"status": {"name": status, "statusCategory": {"key": category}}},
        },
    )


def _transition(
    transition_id: str,
    name: str,
    to: str,
    category: str = "indeterminate",
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": transition_id,
        "name": name,
        "hasScreen": bool(fields),
        "to": {"id": f"status-{transition_id}", "name": to, "statusCategory": {"key": category}},
        "fields": fields or {},
    }


def _transitions(*items: dict[str, Any]) -> Any:
    return json_response(200, {"expand": "transitions", "transitions": list(items)})


def _field(required: Any) -> dict[str, Any]:
    return {"key": "resolution", "name": "Resolución", "required": required, "operations": ["set"]}


_START = _transition("11", "Iniciar", "En curso")
_CLOSE = _transition("31", "Cerrar", "Hecho", "done", {"resolution": _field(True)})
_REVIEW = _transition("21", "Revisar", "En revisión", fields={"comment": _field(False)})


def _adapter(transport: FakeTransport) -> JiraAdapter:
    return JiraAdapter(
        transport,
        BasicAuthProvider(ACCOUNT, SecretValue(TOKEN)),
        base_url=SITE,
        expected_account_id="acct-1",
        expected_account=ACCOUNT,
        clock=lambda: NOW,
    )


# --- Domain ------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["CP-1356", "CP-1", "AB_9-42", "PANDORA2-7"])
def test_an_exact_jira_key_is_accepted(key: str) -> None:
    assert IssueTransitionsQuery(key).issue_key == key


@pytest.mark.parametrize(
    "key",
    [
        "",
        "cp-1356",
        "C-1356",
        "CP-0",
        "CP-01",
        "CP1356",
        "1CP-1356",
        "CP-1356\n",
        " CP-1356",
        "CP-1356,CP-1357",
        "CP-1356..CP-1360",
        "project = CP",
        "CP-1356/transitions",
    ],
)
def test_anything_but_one_exact_jira_key_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="exact Jira issue key"):
        IssueTransitionsQuery(key)


# --- Official adapter: listing transitions -----------------------------------------------------


def test_listing_proves_identity_then_reads_the_status_and_the_transitions() -> None:
    transport = FakeTransport(_myself(), _issue(), _transitions(_START, _REVIEW, _CLOSE))

    records = _adapter(transport).list_transitions(IssueTransitionsQuery(ISSUE, ACCOUNT))

    assert [(request.method, request.url) for request in transport.requests] == [
        ("GET", MYSELF),
        ("GET", ISSUE_STATUS),
        ("GET", TRANSITIONS),
    ]
    assert all(request.json_body is None for request in transport.requests)
    assert all(
        request.headers and request.headers["Authorization"] == BASIC
        for request in transport.requests
    )
    assert [
        (
            record.transition_id,
            record.transition_name,
            record.to_status,
            record.to_status_category,
            record.requires_fields,
        )
        for record in records
    ] == [
        ("11", "Iniciar", "En curso", "indeterminate", False),
        ("21", "Revisar", "En revisión", "indeterminate", False),
        ("31", "Cerrar", "Hecho", "done", True),
    ]
    first = records[0]
    assert isinstance(first, IssueTransitionOption)
    assert first.issue_key == ISSUE
    assert (first.current_status, first.current_status_category) == ("Por hacer", "new")
    assert first.provenance.provider_id == "jira.cloud"
    assert first.provenance.family is ProviderFamily.JIRA
    assert first.provenance.route is RouteKind.OFFICIAL
    assert first.provenance.account_id == "acct-1"
    assert first.provenance.source_id == f"{ISSUE}:11"
    assert first.provenance.fetched_at == NOW


def test_an_issue_without_available_transitions_lists_nothing() -> None:
    transport = FakeTransport(_myself(), _issue(), _transitions())

    assert _adapter(transport).list_transitions(IssueTransitionsQuery(ISSUE)) == ()


def test_another_jira_identity_reads_no_issue_data() -> None:
    transport = FakeTransport(_myself(account_id="acct-other"))

    with pytest.raises(ConnectorFailure) as captured:
        _adapter(transport).list_transitions(IssueTransitionsQuery(ISSUE))

    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert [request.url for request in transport.requests] == [MYSELF]


@pytest.mark.parametrize("failing_call", ["status", "transitions"])
def test_a_missing_or_hidden_issue_is_typed_as_not_found(failing_call: str) -> None:
    not_found = json_response(404, {"errorMessages": ["Issue does not exist"]})
    responses = (
        (_myself(), not_found) if failing_call == "status" else (_myself(), _issue(), not_found)
    )
    transport = FakeTransport(*responses)

    with pytest.raises(ConnectorFailure) as captured:
        _adapter(transport).list_transitions(IssueTransitionsQuery(ISSUE))

    assert captured.value.reason is ReasonCode.ISSUE_NOT_FOUND
    assert "Issue does not exist" not in captured.value.safe_message


@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, ReasonCode.INVALID_CREDENTIALS), (403, ReasonCode.AUTHORIZATION_DENIED)],
)
def test_rejected_credentials_keep_their_existing_reasons(status: int, reason: ReasonCode) -> None:
    transport = FakeTransport(_myself(), json_response(status, {"errorMessages": ["no"]}))

    with pytest.raises(ConnectorFailure) as captured:
        _adapter(transport).list_transitions(IssueTransitionsQuery(ISSUE))

    assert captured.value.reason is reason


def test_a_key_jira_resolves_to_another_issue_fails_closed_before_reading_transitions() -> None:
    transport = FakeTransport(_myself(), _issue(key="CP-9999"))

    with pytest.raises(ConnectorFailure) as captured:
        _adapter(transport).list_transitions(IssueTransitionsQuery(ISSUE))

    assert captured.value.reason is ReasonCode.ISSUE_NOT_FOUND
    assert [request.url for request in transport.requests] == [MYSELF, ISSUE_STATUS]


@pytest.mark.parametrize(
    "issue_payload",
    [
        {"key": ISSUE},
        {"key": ISSUE, "fields": {}},
        {"key": ISSUE, "fields": {"status": {"statusCategory": {"key": "new"}}}},
        {"fields": {"status": {"name": "Por hacer"}}},
        [],
    ],
)
def test_a_malformed_issue_status_fails_closed(issue_payload: Any) -> None:
    transport = FakeTransport(_myself(), json_response(200, issue_payload))

    with pytest.raises(ConnectorFailure) as captured:
        _adapter(transport).list_transitions(IssueTransitionsQuery(ISSUE))

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


@pytest.mark.parametrize(
    "transitions_payload",
    [
        {},
        {"transitions": None},
        {"transitions": ["not-an-object"]},
        {"transitions": [{"name": "Iniciar", "to": {"name": "En curso"}}]},
        {"transitions": [{"id": "11", "to": {"name": "En curso"}}]},
        {"transitions": [{"id": "11", "name": "Iniciar"}]},
        {"transitions": [{"id": "11", "name": "Iniciar", "to": {"id": "3"}}]},
        {
            "transitions": [
                {"id": "11", "name": "Iniciar", "to": {"name": "En curso"}, "fields": []}
            ]
        },
        {
            "transitions": [
                {
                    "id": "11",
                    "name": "Iniciar",
                    "to": {"name": "En curso"},
                    "fields": {"resolution": {"required": "yes"}},
                }
            ]
        },
    ],
)
def test_malformed_transitions_fail_closed(transitions_payload: Any) -> None:
    transport = FakeTransport(_myself(), _issue(), json_response(200, transitions_payload))

    with pytest.raises(ConnectorFailure) as captured:
        _adapter(transport).list_transitions(IssueTransitionsQuery(ISSUE))

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


# --- Contract surface --------------------------------------------------------------------------


def test_the_listing_is_official_only() -> None:
    capability = Capability.EXTERNAL_ISSUES_TRANSITIONS_LIST

    assert capability.value in target_spec(ConnectionType.JIRA).capabilities
    assert capability in JiraAdapter.descriptor.capabilities
    assert BROWSER_FALLBACK_CAPABILITIES[ProviderFamily.JIRA] == frozenset(
        {Capability.EXTERNAL_ISSUES_SEARCH}
    )
    rows = [row for row in CAPABILITY_MATRIX if row.capability == capability.value]
    assert [(row.provider, row.channel, row.v0_1_status) for row in rows] == [
        ("Jira Cloud REST v3", "official", "implemented-offline")
    ]


# --- Application and CLI -----------------------------------------------------------------------


def _values(tmp_path: Path) -> dict[str, str]:
    return {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")}


def _connected_api_token(tmp_path: Path) -> MemoryKeyring:
    """Connect ACCOUNT with a classic API token, as `connect jira` does, against fakes only."""
    settings = load_settings(environ=_values(tmp_path))
    keyring = MemoryKeyring()
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        UnusedOAuthStore(),
        secret_store=KeyringSecretStore(backend=keyring),
        transport=FakeTransport(_myself()),
        clock=lambda: NOW,
    )
    assert service.connect_jira(ACCOUNT, site=SITE, token=SecretValue(TOKEN)).ok
    return keyring


def _route_runs_to(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    keyring: MemoryKeyring,
    transport: FakeTransport,
) -> None:
    """Build the real registry for each run, bound to the fake transport and in-memory stores."""

    def build(**kwargs: Any) -> Any:
        return registry_from_environment(
            **kwargs,
            environ=_values(tmp_path),
            token_store=UnusedOAuthStore(),
            secret_store=KeyringSecretStore(backend=keyring),
            transport=transport,
        )

    monkeypatch.setattr(application, "registry_from_environment", build)


def _no_registry(**_: Any) -> Any:
    raise AssertionError("A refused request must not build the provider registry")


def test_run_lists_transitions_for_one_issue_without_a_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _connected_api_token(tmp_path)
    transport = FakeTransport(_myself(), _issue(), _transitions(_START, _CLOSE))
    _route_runs_to(monkeypatch, tmp_path, keyring, transport)

    result = application.run_application(
        {
            "type": "jira",
            "account": ACCOUNT,
            "capability": "external.issues.transitions.list",
            "issue": ISSUE,
        }
    )

    assert result.ok is True, result.error
    assert result.route == "official"
    records = result.to_dict()["data"]["records"]
    assert [record["to_status"] for record in records] == ["En curso", "Hecho"]
    assert records[1]["requires_fields"] is True
    assert records[0]["current_status"] == "Por hacer"
    assert records[0]["schema"] == "solercia.connect/record/v1"
    assert [request.url for request in transport.requests] == [MYSELF, ISSUE_STATUS, TRANSITIONS]
    assert TOKEN not in str(result.to_dict())


@pytest.mark.parametrize(
    ("issue", "message"),
    [
        (None, "issue is required"),
        ("cp-1356", "exact Jira issue key"),
        ("CP-1356,CP-1357", "exact Jira issue key"),
        ("project = CP", "exact Jira issue key"),
    ],
)
def test_run_refuses_anything_but_one_exact_issue_key_before_provider_work(
    monkeypatch: pytest.MonkeyPatch, issue: str | None, message: str
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(
        {
            "type": "jira",
            "account": ACCOUNT,
            "capability": "external.issues.transitions.list",
            "issue": issue,
        }
    )

    assert result.status == "invalid_input"
    assert result.error is not None and message in result.error.message


def test_an_issue_key_applies_only_to_the_transition_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(
        {
            "type": "jira",
            "account": ACCOUNT,
            "capability": "external.issues.search",
            "start": "2026-08-01T00:00:00+00:00",
            "end": "2026-09-01T00:00:00+00:00",
            "issue": ISSUE,
        }
    )

    assert result.status == "invalid_input"
    assert result.error is not None and "issue" in result.error.message


def test_the_cli_passes_the_issue_key_to_the_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_run(payload: dict[str, Any]) -> Any:
        seen.append(payload)
        return ResultEnvelope.success(operation="run", data={"records": []})

    monkeypatch.setattr(cli, "run_application", fake_run)

    result = runner.invoke(
        cli.app,
        [
            "run",
            "--provider",
            "jira.cloud",
            "--capability",
            "external.issues.transitions.list",
            "--account",
            ACCOUNT,
            "--issue",
            ISSUE,
        ],
        env={},
    )

    assert result.exit_code == 0, result.stdout
    (payload,) = seen
    assert payload["type"] == "jira"
    assert payload["issue"] == ISSUE
    assert payload["allow_write"] is False


def test_the_legacy_query_helper_builds_the_listing_only_with_an_issue_key() -> None:
    common: dict[str, Any] = {
        "start": None,
        "end": None,
        "account": ACCOUNT,
        "calendar_id": "primary",
        "assignee": None,
        "jql": None,
        "meeting_id": None,
        "limit": 50,
    }

    query = cli._query_payload(Capability.EXTERNAL_ISSUES_TRANSITIONS_LIST, **common, issue=ISSUE)

    assert query == IssueTransitionsQuery(ISSUE, ACCOUNT)
    with pytest.raises(cli._InputError, match="--issue"):
        cli._query_payload(Capability.EXTERNAL_ISSUES_TRANSITIONS_LIST, **common)
