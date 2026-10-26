"""Jira open issues: the read-only list of what the connected account still has to finish.

Every call goes through the FakeTransport; nothing here reaches a real Jira site.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta, timezone
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
    WRITE_CAPABILITIES,
    Capability,
    ConnectorRequest,
    DateWindow,
    IssueQuery,
    OpenIssue,
    OpenIssueQuery,
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
MYSELF = f"{SITE}/rest/api/3/myself"
SEARCH = f"{SITE}/rest/api/3/search/jql"
FIELDS = ["summary", "status", "issuetype", "priority", "updated", "assignee"]
DEFAULT_JQL = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
CAPABILITY = "external.issues.open.list"

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


def _issue(key: str = "CP-10", issue_id: str = "10010", **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": "Conectar el informe de pago",
        "status": {
            "name": "En curso",
            "statusCategory": {"key": "indeterminate", "name": "En curso"},
        },
        "issuetype": {"name": "Historia"},
        "priority": {"name": "Media"},
        "updated": "2026-10-20T15:30:00.000-0500",
        "assignee": {"accountId": "acct-1", "displayName": "User"},
    }
    fields.update(overrides)
    return {"id": issue_id, "key": key, "fields": fields}


def _page(*issues: dict[str, Any], token: str | None = None) -> Any:
    payload: dict[str, Any] = {"issues": list(issues), "isLast": token is None}
    if token is not None:
        payload["nextPageToken"] = token
    return json_response(200, payload)


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


def test_the_open_issue_query_defaults_to_the_connected_account_without_filters() -> None:
    query = OpenIssueQuery()

    assert (query.project, query.current_sprint, query.jql, query.account_id, query.limit) == (
        None,
        False,
        None,
        None,
        100,
    )


@pytest.mark.parametrize("project", ["CP", "AB_9", "PANDORA2", "X1"])
def test_a_jira_project_key_is_accepted(project: str) -> None:
    assert OpenIssueQuery(project=project).project == project


@pytest.mark.parametrize(
    "project",
    [
        "",
        "C",
        "cp",
        "Cp",
        "1CP",
        "_CP",
        "CP-1",
        " CP",
        "CP ",
        "CP\n",
        "CP,AB",
        'CP" OR project = "AB',
        "CP OR assignee is EMPTY",
    ],
)
def test_anything_but_one_jira_project_key_is_refused(project: str) -> None:
    with pytest.raises(ValueError, match="Jira project key"):
        OpenIssueQuery(project=project)


@pytest.mark.parametrize("jql", ["", " ", "\n\t "])
def test_a_blank_jql_is_refused(jql: str) -> None:
    with pytest.raises(ValueError, match="jql cannot be blank"):
        OpenIssueQuery(jql=jql)


def test_the_jql_is_kept_without_its_outer_whitespace() -> None:
    assert OpenIssueQuery(jql="  project = CP  ").jql == "project = CP"


@pytest.mark.parametrize(
    "extra",
    [{"project": "CP"}, {"current_sprint": True}, {"project": "CP", "current_sprint": True}],
)
def test_a_jql_replaces_the_default_filter_so_it_refuses_project_or_sprint(
    extra: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="replaces the default filter"):
        OpenIssueQuery(jql="project = CP", **extra)


@pytest.mark.parametrize("limit", [0, 101, -1])
def test_the_open_issue_limit_stays_between_one_and_one_hundred(limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        OpenIssueQuery(limit=limit)


# --- Official adapter --------------------------------------------------------------------------


def test_listing_proves_identity_then_reads_open_issues_with_the_default_jql() -> None:
    transport = FakeTransport(_myself(), _page(_issue()))

    records = _adapter(transport).list_open_issues(OpenIssueQuery(account_id=ACCOUNT))

    assert [(request.method, request.url) for request in transport.requests] == [
        ("GET", MYSELF),
        ("POST", SEARCH),
    ]
    assert all(
        request.headers and request.headers["Authorization"] == BASIC
        for request in transport.requests
    )
    assert transport.requests[1].json_body == {
        "jql": DEFAULT_JQL,
        "fields": FIELDS,
        "maxResults": 100,
    }
    (record,) = records
    assert isinstance(record, OpenIssue)
    assert (
        record.key,
        record.summary,
        record.status,
        record.status_category,
        record.issue_type,
        record.priority,
        record.assignee_account_id,
    ) == (
        "CP-10",
        "Conectar el informe de pago",
        "En curso",
        "indeterminate",
        "Historia",
        "Media",
        "acct-1",
    )
    assert record.updated_at == datetime(2026, 10, 20, 15, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert record.updated_at == datetime(2026, 10, 20, 20, 30, tzinfo=UTC)
    assert record.provenance.provider_id == "jira.cloud"
    assert record.provenance.family is ProviderFamily.JIRA
    assert record.provenance.route is RouteKind.OFFICIAL
    assert record.provenance.source_id == "10010"
    assert record.provenance.account_id == "acct-1"
    assert record.provenance.endpoint == "/rest/api/3/search/jql"
    assert record.provenance.fetched_at == NOW


@pytest.mark.parametrize(
    ("query", "jql"),
    [
        (
            OpenIssueQuery(project="CP"),
            'assignee = currentUser() AND statusCategory != Done AND project = "CP" '
            "ORDER BY updated DESC",
        ),
        (
            OpenIssueQuery(current_sprint=True),
            "assignee = currentUser() AND statusCategory != Done AND sprint in openSprints() "
            "ORDER BY updated DESC",
        ),
        (
            OpenIssueQuery(project="CP", current_sprint=True),
            'assignee = currentUser() AND statusCategory != Done AND project = "CP" '
            "AND sprint in openSprints() ORDER BY updated DESC",
        ),
        (OpenIssueQuery(jql="  project = CP ORDER BY rank  "), "project = CP ORDER BY rank"),
    ],
)
def test_the_jql_is_deterministic_for_each_filter(query: OpenIssueQuery, jql: str) -> None:
    transport = FakeTransport(_myself(), _page())

    assert _adapter(transport).list_open_issues(query) == ()
    assert transport.requests[1].json_body is not None
    assert transport.requests[1].json_body["jql"] == jql


def test_listing_follows_the_page_token_until_the_limit() -> None:
    transport = FakeTransport(
        _myself(),
        _page(_issue("CP-1", "1"), _issue("CP-2", "2"), token="page-2"),
        _page(_issue("CP-3", "3"), _issue("CP-4", "4"), token="page-3"),
    )

    records = _adapter(transport).list_open_issues(OpenIssueQuery(limit=3))

    assert [record.key for record in records] == ["CP-1", "CP-2", "CP-3"]
    bodies = [request.json_body for request in transport.requests[1:]]
    assert bodies == [
        {"jql": DEFAULT_JQL, "fields": FIELDS, "maxResults": 3},
        {"jql": DEFAULT_JQL, "fields": FIELDS, "maxResults": 1, "nextPageToken": "page-2"},
    ]


def test_listing_stops_on_the_last_page() -> None:
    transport = FakeTransport(
        _myself(),
        _page(_issue("CP-1", "1"), token="page-2"),
        _page(_issue("CP-2", "2")),
    )

    records = _adapter(transport).list_open_issues(OpenIssueQuery())

    assert [record.key for record in records] == ["CP-1", "CP-2"]
    assert len(transport.requests) == 3


def test_listing_refuses_to_follow_more_pages_than_the_fixed_bound() -> None:
    pages = [_page(token=f"page-{index}") for index in range(2, 22)]
    transport = FakeTransport(_myself(), *pages)

    with pytest.raises(ConnectorFailure) as captured:
        _adapter(transport).list_open_issues(OpenIssueQuery())

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA
    assert "page bound" in captured.value.safe_message
    assert len(transport.requests) == 21


def test_another_jira_identity_reads_no_issues() -> None:
    transport = FakeTransport(_myself(account_id="acct-other"))

    with pytest.raises(ConnectorFailure) as captured:
        _adapter(transport).list_open_issues(OpenIssueQuery())

    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert [request.url for request in transport.requests] == [MYSELF]


def test_a_visible_email_of_another_account_reads_no_issues() -> None:
    transport = FakeTransport(_myself(email="other@example.com"))

    with pytest.raises(ConnectorFailure) as captured:
        _adapter(transport).list_open_issues(OpenIssueQuery())

    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert [request.url for request in transport.requests] == [MYSELF]


def test_optional_issue_fields_may_be_absent() -> None:
    sparse = {
        "id": "10011",
        "key": "CP-11",
        "fields": {"summary": None, "status": None, "assignee": None, "updated": None},
    }
    transport = FakeTransport(_myself(), _page(sparse))

    (record,) = _adapter(transport).list_open_issues(OpenIssueQuery(jql="key = CP-11"))

    assert (
        record.summary,
        record.status,
        record.status_category,
        record.issue_type,
        record.priority,
        record.updated_at,
        record.assignee_account_id,
    ) == ("", None, None, None, None, None, None)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"issues": "not-a-list"},
        {"issues": ["not-an-object"]},
        {"issues": [{"key": "CP-1", "fields": {}}]},
        {"issues": [{"id": "1", "fields": {}}]},
        {"issues": [{"id": "1", "key": "CP-1"}]},
        {"issues": [{"id": "1", "key": "CP-1", "fields": []}]},
        {"issues": [{"id": "1", "key": "CP-1", "fields": {"updated": "yesterday"}}]},
    ],
)
def test_malformed_search_data_fails_closed(payload: Any) -> None:
    transport = FakeTransport(_myself(), json_response(200, payload))

    with pytest.raises(ConnectorFailure) as captured:
        _adapter(transport).list_open_issues(OpenIssueQuery())

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, ReasonCode.INVALID_CREDENTIALS), (403, ReasonCode.AUTHORIZATION_DENIED)],
)
def test_rejected_credentials_keep_their_existing_reasons(status: int, reason: ReasonCode) -> None:
    transport = FakeTransport(_myself(), json_response(status, {"errorMessages": ["no"]}))

    with pytest.raises(ConnectorFailure) as captured:
        _adapter(transport).list_open_issues(OpenIssueQuery())

    assert captured.value.reason is reason
    assert "no" not in captured.value.safe_message.split()


def test_the_adapter_routes_the_capability_to_the_open_issue_listing() -> None:
    transport = FakeTransport(_myself(), _page(_issue()))
    request = ConnectorRequest.create(
        "jira.cloud", Capability.EXTERNAL_ISSUES_OPEN_LIST, OpenIssueQuery(project="CP")
    )

    records = _adapter(transport).execute(request)

    assert [record.key for record in records if isinstance(record, OpenIssue)] == ["CP-10"]
    assert Capability.EXTERNAL_ISSUES_OPEN_LIST in JiraAdapter.descriptor.capabilities


def test_the_search_still_keeps_only_issues_resolved_inside_its_window() -> None:
    """The open-issue read shares the search pagination; the search filter must not change."""
    inside = _issue("CP-1", "1", resolutiondate="2026-08-10T09:00:00.000+0000")
    after = _issue("CP-2", "2", resolutiondate="2026-09-01T00:00:00.000+0000")
    unresolved = _issue("CP-3", "3")
    transport = FakeTransport(_myself(), _page(after, unresolved, inside))
    window = DateWindow(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))

    issues = _adapter(transport).search_issues(IssueQuery(window, limit=2))

    assert [issue.key for issue in issues] == ["CP-1"]
    assert transport.requests[1].json_body is not None
    assert transport.requests[1].json_body["maxResults"] == 2


# --- Contract surface --------------------------------------------------------------------------


def test_the_open_issue_listing_is_a_read_on_the_official_route_only() -> None:
    capability = Capability.EXTERNAL_ISSUES_OPEN_LIST

    assert capability.value == CAPABILITY
    assert capability.value in target_spec(ConnectionType.JIRA).capabilities
    assert capability not in WRITE_CAPABILITIES
    assert capability not in BROWSER_FALLBACK_CAPABILITIES[ProviderFamily.JIRA]
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


def test_run_lists_open_issues_without_a_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _connected_api_token(tmp_path)
    transport = FakeTransport(_myself(), _page(_issue()))
    _route_runs_to(monkeypatch, tmp_path, keyring, transport)

    result = application.run_application(
        {"type": "jira", "account": ACCOUNT, "capability": CAPABILITY}
    )

    assert result.ok is True, result.error
    assert result.route == "official"
    (record,) = result.to_dict()["data"]["records"]
    assert set(record) == {
        "schema",
        "key",
        "summary",
        "status",
        "status_category",
        "issue_type",
        "priority",
        "updated_at",
        "assignee_account_id",
        "provenance",
    }
    assert record["schema"] == "solercia.connect/record/v1"
    assert (record["key"], record["status_category"], record["assignee_account_id"]) == (
        "CP-10",
        "indeterminate",
        "acct-1",
    )
    assert record["updated_at"] == "2026-10-20T15:30:00-05:00"
    assert record["provenance"]["endpoint"] == "/rest/api/3/search/jql"
    assert [request.url for request in transport.requests] == [MYSELF, SEARCH]
    assert transport.requests[1].json_body == {
        "jql": DEFAULT_JQL,
        "fields": FIELDS,
        "maxResults": 50,
    }
    assert TOKEN not in str(result.to_dict())


@pytest.mark.parametrize(
    ("extra", "jql", "max_results"),
    [
        (
            {"project": "CP", "sprint": "current", "limit": 500},
            'assignee = currentUser() AND statusCategory != Done AND project = "CP" '
            "AND sprint in openSprints() ORDER BY updated DESC",
            100,
        ),
        ({"jql": " project = CP ORDER BY rank ", "limit": 7}, "project = CP ORDER BY rank", 7),
    ],
)
def test_run_maps_the_filters_and_caps_the_limit_at_one_hundred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: dict[str, Any],
    jql: str,
    max_results: int,
) -> None:
    keyring = _connected_api_token(tmp_path)
    transport = FakeTransport(_myself(), _page())
    _route_runs_to(monkeypatch, tmp_path, keyring, transport)

    result = application.run_application(
        {"type": "jira", "account": ACCOUNT, "capability": CAPABILITY, **extra}
    )

    assert result.ok is True, result.error
    assert transport.requests[1].json_body == {
        "jql": jql,
        "fields": FIELDS,
        "maxResults": max_results,
    }


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"project": "cp"}, "Jira project key"),
        ({"project": ""}, "Jira project key"),
        ({"project": 'CP" OR project = "AB'}, "Jira project key"),
        ({"project": 7}, "Jira project key"),
        ({"sprint": "next"}, "sprint accepts only current"),
        ({"sprint": ""}, "sprint accepts only current"),
        ({"jql": ""}, "jql cannot be blank"),
        ({"jql": "  \t"}, "jql cannot be blank"),
        ({"jql": "project = CP", "project": "CP"}, "replaces the default filter"),
        ({"jql": "project = CP", "sprint": "current"}, "replaces the default filter"),
        (
            {"start": "2026-08-01T00:00:00+00:00", "end": "2026-09-01T00:00:00+00:00"},
            "no date window",
        ),
        ({"assignee": "other@example.com"}, "assignee does not apply"),
        ({"limit": 0}, "limit must be an integer"),
        ({"limit": True}, "limit must be an integer"),
        ({"issue": "CP-1"}, "issue applies only"),
    ],
)
def test_run_refuses_invalid_open_issue_input_before_provider_work(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any], message: str
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(
        {"type": "jira", "account": ACCOUNT, "capability": CAPABILITY, **extra}
    )

    assert result.status == "invalid_input"
    assert result.error is not None and message in result.error.message


@pytest.mark.parametrize(("name", "value"), [("project", "CP"), ("sprint", "current")])
def test_project_and_sprint_apply_only_to_the_open_issue_listing(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(
        {
            "type": "jira",
            "account": ACCOUNT,
            "capability": "external.issues.search",
            "start": "2026-08-01T00:00:00+00:00",
            "end": "2026-09-01T00:00:00+00:00",
            name: value,
        }
    )

    assert result.status == "invalid_input"
    assert result.error is not None
    assert "project and sprint apply only to external.issues.open.list" in result.error.message


def _captured_cli_payload(monkeypatch: pytest.MonkeyPatch, *options: str) -> dict[str, Any]:
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
            CAPABILITY,
            "--account",
            ACCOUNT,
            *options,
        ],
        env={},
    )
    assert result.exit_code == 0, result.stdout
    (payload,) = seen
    return payload


def test_the_cli_passes_project_sprint_and_limit_to_the_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _captured_cli_payload(
        monkeypatch, "--project", "CP", "--sprint", "current", "--limit", "20"
    )

    assert payload["type"] == "jira"
    assert (payload["project"], payload["sprint"], payload["limit"]) == ("CP", "current", 20)
    assert (payload["jql"], payload["start"], payload["end"]) == (None, None, None)
    assert payload["allow_write"] is False


def test_the_cli_sends_project_and_sprint_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _captured_cli_payload(monkeypatch, "--jql", "project = CP")

    assert payload["jql"] == "project = CP"
    assert "project" not in payload
    assert "sprint" not in payload


def test_the_legacy_query_helper_builds_the_open_issue_listing() -> None:
    common: dict[str, Any] = {
        "start": None,
        "end": None,
        "account": ACCOUNT,
        "calendar_id": "primary",
        "assignee": None,
        "jql": None,
        "meeting_id": None,
        "limit": 500,
    }

    query = cli._query_payload(
        Capability.EXTERNAL_ISSUES_OPEN_LIST, **common, project="CP", sprint="current"
    )

    assert query == OpenIssueQuery("CP", True, None, ACCOUNT, 100)
    assert cli._query_payload(Capability.EXTERNAL_ISSUES_OPEN_LIST, **common) == OpenIssueQuery(
        account_id=ACCOUNT, limit=100
    )
    with pytest.raises(cli._InputError, match="--sprint"):
        cli._query_payload(Capability.EXTERNAL_ISSUES_OPEN_LIST, **common, sprint="next")
