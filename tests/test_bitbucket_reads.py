"""Bitbucket Cloud reads over the official API: repositories (B2), pull requests and commits.

Decision: docs/decisions/2026-10-26-conector-bitbucket.md. Every call goes through the
FakeTransport; nothing here reaches Bitbucket.
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
    Capability,
    ConnectorRequest,
    DateWindow,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    ScmCommit,
    ScmCommitQuery,
    ScmPullRequest,
    ScmPullRequestQuery,
    ScmRepository,
    ScmRepositoryQuery,
)
from solercia_connect.envelope import ResultEnvelope
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import OAuthTokenSet
from solercia_connect.providers.bitbucket import BitbucketAdapter
from solercia_connect.runtime import BROWSER_FALLBACK_CAPABILITIES, RoutePolicy
from solercia_connect.secret_store import KeyringSecretStore
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport, json_response

ACCOUNT = "user@example.com"
WORKSPACE = "idartes-tecnologia"
TOKEN = "bitbucket-api-token-value"
ACCOUNT_ID = "557058:acct-1"
API = "https://api.bitbucket.org/2.0"
USER = f"{API}/user"
EMAILS = f"{API}/user/emails"
WORKSPACE_URL = f"{API}/workspaces/{WORKSPACE}"
REPOSITORIES = f"{API}/repositories/{WORKSPACE}"
BASIC = "Basic " + base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()
IDENTITY = [USER, EMAILS, WORKSPACE_URL]

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


def _values(tmp_path: Path) -> dict[str, str]:
    return {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")}


def _identity(workspace: str = WORKSPACE, account_id: str = ACCOUNT_ID) -> list[Any]:
    return [
        json_response(200, {"account_id": account_id, "display_name": "User"}),
        json_response(200, {"values": [{"email": ACCOUNT, "is_confirmed": True}]}),
        json_response(200, {"slug": workspace}),
    ]


def _page(*values: dict[str, Any], next_url: str | None = None) -> Any:
    payload: dict[str, Any] = {"pagelen": len(values), "values": list(values)}
    if next_url is not None:
        payload["next"] = next_url
    return json_response(200, payload)


def _adapter(*responses: Any) -> tuple[BitbucketAdapter, FakeTransport]:
    transport = FakeTransport(*responses)
    adapter = BitbucketAdapter(
        transport,
        BasicAuthProvider(ACCOUNT, SecretValue(TOKEN)),
        account=ACCOUNT,
        workspace=WORKSPACE,
        expected_account_id=ACCOUNT_ID,
        clock=lambda: NOW,
    )
    return adapter, transport


def _connected(tmp_path: Path) -> MemoryKeyring:
    """Connect ACCOUNT as `connect bitbucket` does, against fakes only."""
    settings = load_settings(environ=_values(tmp_path))
    keyring = MemoryKeyring()
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        UnusedOAuthStore(),
        secret_store=KeyringSecretStore(backend=keyring),
        transport=FakeTransport(*_identity()),
        clock=lambda: NOW,
    )
    assert service.connect_bitbucket(ACCOUNT, workspace=WORKSPACE, token=SecretValue(TOKEN)).ok
    return keyring


def _route_runs_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, keyring: MemoryKeyring, transport: Any
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


# --- scm.repositories.list (B2) -------------------------------------------------------------


def _repository(
    slug: str,
    updated_on: str,
    *,
    workspace: str = WORKSPACE,
    private: Any = True,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": "repository",
        "slug": slug,
        "name": slug.replace("_", " ").title(),
        "full_name": f"{workspace}/{slug}",
        "is_private": private,
        "mainbranch": {"type": "branch", "name": "develop"},
        "project": {"type": "project", "key": "PAN"},
        "updated_on": updated_on,
        "links": {"html": {"href": f"https://bitbucket.org/{workspace}/{slug}"}},
        **extra,
    }


def test_the_repository_query_names_one_workspace_slug_or_the_connected_default() -> None:
    assert ScmRepositoryQuery().workspace is None
    assert ScmRepositoryQuery(" IDARTES-Tecnologia ").workspace == WORKSPACE
    for invalid in ("a/b", "../x", ""):
        with pytest.raises(ValueError, match="workspace"):
            ScmRepositoryQuery(invalid)


def test_repositories_are_read_after_the_identity_and_follow_next_on_the_same_collection() -> None:
    second = f"{REPOSITORIES}?role=member&sort=-updated_on&pagelen=100&page=2"
    adapter, transport = _adapter(
        *_identity(),
        _page(
            _repository("pandora_proxy", "2026-10-20T15:00:00.123456+00:00"),
            _repository("zeta", "2026-10-21T10:00:00+00:00", private=False, mainbranch=None),
            next_url=second,
        ),
        _page(
            _repository("alpha", "2026-10-20T15:00:00.123456+00:00", project=None),
            _repository("legacy", "2026-01-01T00:00:00Z", links={}),
        ),
    )

    records = adapter.execute(
        ConnectorRequest.create(
            "bitbucket.cloud", Capability.SCM_REPOSITORIES_LIST, ScmRepositoryQuery()
        )
    )

    assert [request.url for request in transport.requests] == [*IDENTITY, REPOSITORIES, second]
    assert transport.requests[3].params == {"role": "member", "sort": "-updated_on", "pagelen": 100}
    assert transport.requests[4].params is None
    assert all(r.headers and r.headers["Authorization"] == BASIC for r in transport.requests)
    assert [record.slug for record in records] == ["zeta", "alpha", "pandora_proxy", "legacy"]
    first = records[2]
    assert isinstance(first, ScmRepository)
    assert first.workspace == WORKSPACE
    assert first.full_name == f"{WORKSPACE}/pandora_proxy"
    assert first.name == "Pandora Proxy"
    assert first.is_private is True
    assert first.main_branch == "develop"
    assert first.project_key == "PAN"
    assert first.updated_at == datetime(2026, 10, 20, 15, 0, 0, 123456, tzinfo=UTC)
    assert first.web_url == f"https://bitbucket.org/{WORKSPACE}/pandora_proxy"
    assert records[0].main_branch is None and records[0].is_private is False
    assert records[1].project_key is None and records[3].web_url is None
    assert records[3].updated_at.tzinfo is not None
    provenance = first.provenance
    assert provenance.provider_id == "bitbucket.cloud"
    assert provenance.family is ProviderFamily.BITBUCKET
    assert provenance.route is RouteKind.OFFICIAL
    assert provenance.account_id == ACCOUNT_ID
    assert provenance.source_id == f"{WORKSPACE}/pandora_proxy"
    assert provenance.endpoint == "/2.0/repositories/{workspace}"
    assert provenance.fetched_at == NOW


def test_a_named_workspace_is_proven_and_read_instead_of_the_default() -> None:
    adapter, transport = _adapter(
        *_identity("other-team"),
        _page(_repository("x", "2026-10-20T00:00:00Z", workspace="other-team")),
    )

    records = adapter.execute(
        ConnectorRequest.create(
            "bitbucket.cloud", Capability.SCM_REPOSITORIES_LIST, ScmRepositoryQuery("Other-Team")
        )
    )

    assert [request.url for request in transport.requests] == [
        USER,
        EMAILS,
        f"{API}/workspaces/other-team",
        f"{API}/repositories/other-team",
    ]
    assert [record.workspace for record in records] == ["other-team"]


def test_a_failed_identity_reads_no_repository() -> None:
    adapter, transport = _adapter(*_identity(account_id="557058:someone-else"))

    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(
            ConnectorRequest.create(
                "bitbucket.cloud", Capability.SCM_REPOSITORIES_LIST, ScmRepositoryQuery()
            )
        )

    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert [request.url for request in transport.requests] == [USER]


def test_the_page_bound_fails_with_result_incomplete_instead_of_truncating() -> None:
    pages = [
        _page(
            _repository(f"repo-{index}", "2026-10-20T00:00:00Z"),
            next_url=f"{REPOSITORIES}?page={index + 2}",
        )
        for index in range(20)
    ]
    adapter, transport = _adapter(*_identity(), *pages)

    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(
            ConnectorRequest.create(
                "bitbucket.cloud", Capability.SCM_REPOSITORIES_LIST, ScmRepositoryQuery()
            )
        )

    assert captured.value.reason is ReasonCode.RESULT_INCOMPLETE
    assert "20 pages" in captured.value.safe_message
    assert len(transport.requests) == 3 + 20


@pytest.mark.parametrize(
    "page",
    [
        _page(_repository("x", "2026-10-20T00:00:00Z", workspace="another-team")),
        _page(_repository("x", "2026-10-20T00:00:00Z", private="yes")),
        _page(_repository("x", "not-a-date")),
        _page({**_repository("x", "2026-10-20T00:00:00Z"), "full_name": "no-slash"}),
        _page({**_repository("x", "2026-10-20T00:00:00Z"), "name": None}),
        json_response(200, {"values": {"slug": "x"}}),
        json_response(200, ["x"]),
    ],
)
def test_malformed_repositories_fail_closed(page: Any) -> None:
    adapter, _ = _adapter(*_identity(), page)

    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(
            ConnectorRequest.create(
                "bitbucket.cloud", Capability.SCM_REPOSITORIES_LIST, ScmRepositoryQuery()
            )
        )

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


def test_the_target_declares_the_read_and_no_browser_route_serves_it() -> None:
    assert "scm.repositories.list" in target_spec(ConnectionType.BITBUCKET).capabilities
    assert Capability.SCM_REPOSITORIES_LIST in BitbucketAdapter.descriptor.capabilities
    assert ProviderFamily.BITBUCKET not in BROWSER_FALLBACK_CAPABILITIES
    assert ProviderFamily.BITBUCKET not in RoutePolicy().browser_fallback_families


def test_run_lists_the_repositories_of_the_connected_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _connected(tmp_path)
    transport = FakeTransport(
        *_identity(), _page(_repository("pandora_proxy", "2026-10-20T00:00:00Z"))
    )
    _route_runs_to(monkeypatch, tmp_path, keyring, transport)

    result = application.run_application(
        {"type": "bitbucket", "account": ACCOUNT, "capability": "scm.repositories.list"}
    )

    assert result.ok is True, result.error
    assert result.route == "official"
    records = result.to_dict()["data"]["records"]
    assert [record["slug"] for record in records] == ["pandora_proxy"]
    assert records[0]["schema"] == "solercia.connect/record/v1"
    assert records[0]["updated_at"] == "2026-10-20T00:00:00+00:00"
    assert [request.url for request in transport.requests] == [*IDENTITY, REPOSITORIES]
    assert TOKEN not in str(result.to_dict())


def test_run_reports_the_page_bound_as_complete_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _connected(tmp_path)
    pages = [
        _page(
            _repository(f"r{index}", "2026-10-20T00:00:00Z"), next_url=f"{REPOSITORIES}?p={index}"
        )
        for index in range(20)
    ]
    _route_runs_to(monkeypatch, tmp_path, keyring, FakeTransport(*_identity(), *pages))

    result = application.run_application(
        {"type": "bitbucket", "account": ACCOUNT, "capability": "scm.repositories.list"}
    )

    assert result.ok is False
    assert result.error is not None and result.error.code == "result_incomplete"
    assert result.error.retryable is False
    assert result.to_dict()["data"] == {"complete": False}
    assert "narrow" in " ".join(result.error.remediation)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "account is required"),
        ({"account": ACCOUNT, "allow_browser_fallback": True}, "browser fallback"),
        ({"account": ACCOUNT, "workspace": "a/b"}, "workspace"),
        (
            {
                "account": ACCOUNT,
                "start": "2026-10-01T00:00:00+00:00",
                "end": "2026-11-01T00:00:00+00:00",
            },
            "window",
        ),
    ],
)
def test_run_refuses_invalid_repository_requests_before_provider_work(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], message: str
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(
        {"type": "bitbucket", "capability": "scm.repositories.list", **payload}
    )

    assert result.status == "invalid_input"
    assert result.error is not None and message in result.error.message


def test_a_workspace_applies_only_to_the_bitbucket_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(
        {
            "type": "jira",
            "account": ACCOUNT,
            "capability": "external.issues.search",
            "start": "2026-08-01T00:00:00+00:00",
            "end": "2026-09-01T00:00:00+00:00",
            "workspace": WORKSPACE,
        }
    )

    assert result.status == "invalid_input"
    assert result.error is not None and "workspace" in result.error.message


def test_run_without_a_connection_is_a_configuration_error(tmp_path: Path) -> None:
    result = application.run_application(
        {"type": "bitbucket", "account": ACCOUNT, "capability": "scm.repositories.list"},
        settings=load_settings(environ=_values(tmp_path)),
    )

    assert result.status == "configuration_error"
    assert result.error is not None and "connect bitbucket" in result.error.message


def test_a_rejected_token_during_a_read_points_to_a_scoped_bitbucket_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _connected(tmp_path)
    _route_runs_to(monkeypatch, tmp_path, keyring, FakeTransport(json_response(401, {})))

    result = application.run_application(
        {"type": "bitbucket", "account": ACCOUNT, "capability": "scm.repositories.list"}
    )

    assert result.error is not None and result.error.code == "invalid_credentials"
    remediation = " ".join(result.error.remediation)
    assert "read:repository:bitbucket" in remediation
    assert f"solercia-connect connect bitbucket {ACCOUNT} --workspace" in remediation


def test_the_cli_passes_the_workspace_to_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_run(payload: dict[str, Any]) -> Any:
        seen.append(payload)
        return ResultEnvelope.success(operation="run", data={"records": []})

    monkeypatch.setattr(cli, "run_application", fake_run)

    with_workspace = runner.invoke(
        cli.app,
        [
            "run",
            "--provider",
            "bitbucket.cloud",
            "--capability",
            "scm.repositories.list",
            "--account",
            ACCOUNT,
            "--workspace",
            WORKSPACE,
        ],
    )
    default = runner.invoke(
        cli.app,
        ["run", "--provider", "bitbucket.cloud", "--capability", "scm.repositories.list"],
    )

    assert with_workspace.exit_code == 0 and default.exit_code == 0
    assert seen[0]["type"] == "bitbucket" and seen[0]["workspace"] == WORKSPACE
    assert "workspace" not in seen[1]


# --- scm.pullrequests.list (B3) -------------------------------------------------------------

REPO = "pandora_proxy"
PULLREQUESTS = f"{API}/repositories/{WORKSPACE}/{REPO}/pullrequests"
BOGOTA = timezone(timedelta(hours=-5))
WINDOW = DateWindow(datetime(2026, 10, 1, tzinfo=BOGOTA), datetime(2026, 11, 1, tzinfo=BOGOTA))
ALL_STATES = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")


def _pull_request(number: Any, updated_on: str, *, state: str = "MERGED", **extra: Any) -> dict:
    return {
        "type": "pullrequest",
        "id": number,
        "title": f"Change {number}",
        "state": state,
        "draft": False,
        "author": {"display_name": "Dev One", "account_id": "557058:dev-1"},
        "source": {"branch": {"name": f"feature/{number}"}},
        "destination": {"branch": {"name": "develop"}},
        "created_on": "2026-09-30T12:00:00+00:00",
        "updated_on": updated_on,
        "merge_commit": {"hash": "abc1234"} if state == "MERGED" else None,
        "links": {
            "html": {"href": f"https://bitbucket.org/{WORKSPACE}/{REPO}/pull-requests/{number}"}
        },
        **extra,
    }


def _pull_requests(query: ScmPullRequestQuery, *responses: Any) -> tuple[tuple[Any, ...], Any]:
    adapter, transport = _adapter(*_identity(), *responses)
    records = adapter.execute(
        ConnectorRequest.create("bitbucket.cloud", Capability.SCM_PULLREQUESTS_LIST, query)
    )
    return records, transport


def test_the_pull_request_query_validates_the_repository_states_and_window() -> None:
    query = ScmPullRequestQuery(" Pandora_Proxy ", WINDOW)

    assert query.repository == REPO
    assert query.states == ALL_STATES
    assert query.workspace is None
    assert ScmPullRequestQuery(REPO, WINDOW, ("merged", "OPEN", "MERGED")).states == (
        "OPEN",
        "MERGED",
    )
    assert ScmPullRequestQuery(REPO, WINDOW, workspace="Other").workspace == "other"
    for invalid in ("", "a/b", "../x", ".hidden", "-x", "a b", "a?b", "x" * 129):
        with pytest.raises(ValueError, match="repository"):
            ScmPullRequestQuery(invalid, WINDOW)
    for states in ((), ("QUEUED",), ("OPEN", "")):
        with pytest.raises(ValueError, match="state"):
            ScmPullRequestQuery(REPO, WINDOW, states)


def test_pull_requests_are_filtered_by_updated_on_in_the_server_and_again_in_the_client() -> None:
    second = f"{PULLREQUESTS}?page=2"
    records, transport = _pull_requests(
        ScmPullRequestQuery(REPO, WINDOW),
        _page(
            _pull_request(7, "2026-10-20T10:00:00+00:00"),
            _pull_request(9, "2026-10-20T10:00:00+00:00", state="OPEN", merge_commit=None),
            _pull_request(3, "2026-11-01T05:00:00+00:00"),
            next_url=second,
        ),
        _page(
            _pull_request(7, "2026-10-20T10:00:00+00:00"),
            _pull_request(2, "2026-10-01T05:00:00+00:00", state="DECLINED", draft=True),
            _pull_request(1, "2026-10-01T04:59:59+00:00"),
        ),
    )

    assert [request.url for request in transport.requests] == [*IDENTITY, PULLREQUESTS, second]
    assert transport.requests[3].params == {
        "state": list(ALL_STATES),
        "q": "updated_on >= 2026-10-01T05:00:00+00:00 AND updated_on < 2026-11-01T05:00:00+00:00",
        "sort": "-updated_on",
        "pagelen": 50,
    }
    assert transport.requests[4].params is None
    assert [record.id for record in records] == [7, 9, 2]
    first = records[0]
    assert isinstance(first, ScmPullRequest)
    assert first.title == "Change 7"
    assert first.state == "MERGED" and first.draft is False
    assert first.author_account_id == "557058:dev-1" and first.author_name == "Dev One"
    assert first.source_branch == "feature/7" and first.destination_branch == "develop"
    assert first.created_at == datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert first.updated_at == datetime(2026, 10, 20, 10, tzinfo=UTC)
    assert first.merge_commit == "abc1234"
    assert first.web_url == f"https://bitbucket.org/{WORKSPACE}/{REPO}/pull-requests/7"
    assert first.repository == REPO and first.workspace == WORKSPACE
    assert first.provenance.source_id == f"{WORKSPACE}/{REPO}#7"
    assert first.provenance.endpoint == "/2.0/repositories/{workspace}/{repo_slug}/pullrequests"
    assert first.provenance.account_id == ACCOUNT_ID
    assert records[1].merge_commit is None and records[2].draft is True


def test_the_server_query_rounds_the_window_outward_and_the_client_keeps_it_exact() -> None:
    window = DateWindow(
        datetime(2026, 10, 1, 0, 0, 0, 500, tzinfo=UTC),
        datetime(2026, 10, 2, 0, 0, 0, 1, tzinfo=UTC),
    )
    records, transport = _pull_requests(
        ScmPullRequestQuery(REPO, window, ("OPEN",)),
        _page(
            _pull_request(5, "2026-10-02T00:00:00.000001+00:00", state="OPEN"),
            _pull_request(4, "2026-10-02T00:00:00+00:00", state="OPEN"),
            _pull_request(3, "2026-10-01T00:00:00+00:00", state="OPEN"),
            _pull_request(8, "2026-10-01T12:00:00+00:00", state="MERGED"),
        ),
    )

    assert transport.requests[3].params is not None
    assert transport.requests[3].params["state"] == ["OPEN"]
    assert transport.requests[3].params["q"] == (
        "updated_on >= 2026-10-01T00:00:00+00:00 AND updated_on < 2026-10-02T00:00:01+00:00"
    )
    assert [record.id for record in records] == [4]


def test_an_unknown_repository_and_the_page_bound_fail_with_typed_reasons() -> None:
    adapter, _ = _adapter(*_identity(), json_response(404, {"error": {"message": "x"}}))
    with pytest.raises(ConnectorFailure) as missing:
        adapter.execute(
            ConnectorRequest.create(
                "bitbucket.cloud",
                Capability.SCM_PULLREQUESTS_LIST,
                ScmPullRequestQuery(REPO, WINDOW),
            )
        )
    assert missing.value.reason is ReasonCode.REPOSITORY_NOT_FOUND
    assert f"{WORKSPACE}/{REPO}" in missing.value.safe_message

    pages = [
        _page(
            _pull_request(index + 1, "2026-10-20T00:00:00Z"), next_url=f"{PULLREQUESTS}?p={index}"
        )
        for index in range(20)
    ]
    with pytest.raises(ConnectorFailure) as bounded:
        _pull_requests(ScmPullRequestQuery(REPO, WINDOW), *pages)
    assert bounded.value.reason is ReasonCode.RESULT_INCOMPLETE


@pytest.mark.parametrize(
    "item",
    [
        _pull_request("7", "2026-10-20T00:00:00Z"),
        _pull_request(True, "2026-10-20T00:00:00Z"),
        _pull_request(0, "2026-10-20T00:00:00Z"),
        _pull_request(7, "2026-10-20T00:00:00Z", state="QUEUED"),
        _pull_request(7, "2026-10-20T00:00:00Z", draft="no"),
        _pull_request(7, "2026-10-20T00:00:00Z", title=None),
        _pull_request(7, "2026-10-20T00:00:00Z", created_on=None),
        _pull_request(7, "yesterday"),
    ],
)
def test_malformed_pull_requests_fail_closed(item: dict[str, Any]) -> None:
    with pytest.raises(ConnectorFailure) as captured:
        _pull_requests(ScmPullRequestQuery(REPO, WINDOW), _page(item))

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


def test_run_lists_pull_requests_with_repeated_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _connected(tmp_path)
    transport = FakeTransport(*_identity(), _page(_pull_request(7, "2026-10-20T00:00:00Z")))
    _route_runs_to(monkeypatch, tmp_path, keyring, transport)

    result = application.run_application(
        {
            "type": "bitbucket",
            "account": ACCOUNT,
            "capability": "scm.pullrequests.list",
            "repo": REPO,
            "state": ["MERGED", "OPEN"],
            "start": "2026-10-01T00:00:00-05:00",
            "end": "2026-11-01T00:00:00-05:00",
        }
    )

    assert result.ok is True, result.error
    records = result.to_dict()["data"]["records"]
    assert [record["id"] for record in records] == [7]
    assert records[0]["updated_at"] == "2026-10-20T00:00:00+00:00"
    assert transport.requests[3].url == PULLREQUESTS
    assert transport.requests[3].params is not None
    assert transport.requests[3].params["state"] == ["OPEN", "MERGED"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"start": "2026-10-01T00:00:00+00:00", "end": "2026-11-01T00:00:00+00:00"}, "repo"),
        ({"repo": REPO}, "start and end"),
        (
            {
                "repo": "a/b",
                "start": "2026-10-01T00:00:00+00:00",
                "end": "2026-11-01T00:00:00+00:00",
            },
            "repository",
        ),
        (
            {
                "repo": REPO,
                "state": ["CLOSED"],
                "start": "2026-10-01T00:00:00+00:00",
                "end": "2026-11-01T00:00:00+00:00",
            },
            "state",
        ),
        (
            {
                "repo": REPO,
                "state": "OPEN",
                "start": "2026-10-01T00:00:00+00:00",
                "end": "2026-11-01T00:00:00+00:00",
            },
            "state",
        ),
    ],
)
def test_run_refuses_invalid_pull_request_requests_before_provider_work(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], message: str
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(
        {"type": "bitbucket", "account": ACCOUNT, "capability": "scm.pullrequests.list", **payload}
    )

    assert result.status == "invalid_input"
    assert result.error is not None and message in result.error.message


def test_repository_and_state_options_do_not_apply_to_the_repository_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    for name, value in (("repo", REPO), ("state", ["OPEN"])):
        result = application.run_application(
            {
                "type": "bitbucket",
                "account": ACCOUNT,
                "capability": "scm.repositories.list",
                name: value,
            }
        )
        assert result.status == "invalid_input"
        assert result.error is not None and name in result.error.message


def test_a_rejected_repository_points_to_the_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _connected(tmp_path)
    _route_runs_to(
        monkeypatch, tmp_path, keyring, FakeTransport(*_identity(), json_response(404, {}))
    )

    result = application.run_application(
        {
            "type": "bitbucket",
            "account": ACCOUNT,
            "capability": "scm.pullrequests.list",
            "repo": "missing",
            "start": "2026-10-01T00:00:00+00:00",
            "end": "2026-11-01T00:00:00+00:00",
        }
    )

    assert result.error is not None and result.error.code == "repository_not_found"
    assert "scm.repositories.list" in " ".join(result.error.remediation)


def test_the_cli_passes_the_repository_and_repeated_states(monkeypatch: pytest.MonkeyPatch) -> None:
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
            "bitbucket.cloud",
            "--capability",
            "scm.pullrequests.list",
            "--account",
            ACCOUNT,
            "--repo",
            REPO,
            "--start",
            "2026-10-01T00:00:00-05:00",
            "--end",
            "2026-11-01T00:00:00-05:00",
            "--state",
            "OPEN",
            "--state",
            "MERGED",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen[0]["repo"] == REPO
    assert seen[0]["state"] == ["OPEN", "MERGED"]
    assert seen[0]["type"] == "bitbucket"


# --- scm.commits.list (B4) ------------------------------------------------------------------

COMMITS = f"{API}/repositories/{WORKSPACE}/{REPO}/commits/develop"
REPOSITORY = f"{API}/repositories/{WORKSPACE}/{REPO}"
COMMIT_WINDOW = DateWindow(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 15, tzinfo=UTC))


def _commit(
    sha: str, when: str, *, message: Any = "Fix the proxy\n\nLonger body", **extra: Any
) -> dict:
    return {
        "type": "commit",
        "hash": sha,
        "date": when,
        "message": message,
        "author": {
            "raw": "Dev One <dev@example.com>",
            "user": {"display_name": "Dev One", "account_id": "557058:dev-1"},
        },
        "parents": [{"hash": "0" * 40}],
        **extra,
    }


def _sha(index: int) -> str:
    return f"{index:040x}"


def _commits(query: ScmCommitQuery, *responses: Any) -> tuple[tuple[Any, ...], Any]:
    adapter, transport = _adapter(*_identity(), *responses)
    records = adapter.execute(
        ConnectorRequest.create("bitbucket.cloud", Capability.SCM_COMMITS_LIST, query)
    )
    return records, transport


def test_the_commit_query_validates_the_branch_as_one_ref_name() -> None:
    assert ScmCommitQuery(REPO, COMMIT_WINDOW).branch is None
    for valid in ("develop", "feature/ABC-1_fix", "release/1.2"):
        assert ScmCommitQuery(REPO, COMMIT_WINDOW, f" {valid} ").branch == valid
    for invalid in ("", "../x", "a..b", "-x", "/x", "x/", "x.lock", "a b", "a?b", "a~b", "a//b"):
        with pytest.raises(ValueError, match="branch"):
            ScmCommitQuery(REPO, COMMIT_WINDOW, invalid)
    with pytest.raises(ValueError, match="repository"):
        ScmCommitQuery("a/b", COMMIT_WINDOW)


def test_commits_are_filtered_in_the_client_and_stop_after_a_page_older_than_the_margin() -> None:
    second = f"{COMMITS}?page=2"
    third = f"{COMMITS}?page=3"
    records, transport = _commits(
        ScmCommitQuery(REPO, COMMIT_WINDOW, "develop"),
        _page(
            _commit(_sha(9), "2026-10-20T00:00:00+00:00"),
            _commit(_sha(8), "2026-10-14T23:59:59+00:00"),
            _commit(_sha(7), "2026-10-15T00:00:00+00:00"),
            next_url=second,
        ),
        _page(
            # Topological order: an old commit does not end the read while its page is mixed.
            _commit(_sha(6), "2026-09-01T00:00:00+00:00"),
            _commit(_sha(5), "2026-10-01T00:00:00+00:00", message="Merge\n"),
            _commit(_sha(8), "2026-10-14T23:59:59+00:00"),
            next_url=third,
        ),
        _page(
            _commit(_sha(4), "2026-09-23T00:00:00+00:00"),
            _commit(_sha(3), "2026-09-30T23:59:59+00:00"),
            next_url=f"{COMMITS}?page=4",
        ),
        _page(
            _commit(_sha(2), "2026-10-02T00:00:00+00:00"),
            _commit(_sha(1), "2026-09-01T00:00:00+00:00"),
            next_url=f"{COMMITS}?page=5",
        ),
        _page(
            _commit(_sha(0), "2026-08-01T00:00:00+00:00"),
            _commit("a" * 40, "2026-08-02T00:00:00+00:00"),
            next_url=f"{COMMITS}?page=6",
        ),
    )

    assert [request.url for request in transport.requests] == [
        *IDENTITY,
        COMMITS,
        second,
        third,
        f"{COMMITS}?page=4",
        f"{COMMITS}?page=5",
    ]
    assert transport.requests[3].params == {"pagelen": 100}
    assert [record.hash for record in records] == [_sha(8), _sha(2), _sha(5)]
    first = records[0]
    assert isinstance(first, ScmCommit)
    assert first.date == datetime(2026, 10, 14, 23, 59, 59, tzinfo=UTC)
    assert first.author == "Dev One <dev@example.com>"
    assert first.author_account_id == "557058:dev-1"
    assert first.summary == "Fix the proxy"
    assert first.parent_count == 1
    assert first.branch == "develop" and first.repository == REPO and first.workspace == WORKSPACE
    assert first.provenance.source_id == f"{WORKSPACE}/{REPO}@{_sha(8)}"
    assert (
        first.provenance.endpoint == "/2.0/repositories/{workspace}/{repo_slug}/commits/{revision}"
    )
    assert records[2].summary == "Merge"


def test_without_a_branch_the_main_branch_of_the_repository_is_read() -> None:
    records, transport = _commits(
        ScmCommitQuery(REPO, COMMIT_WINDOW),
        json_response(200, {"slug": REPO, "mainbranch": {"name": "develop"}}),
        _page(
            _commit(
                _sha(1), "2026-10-02T00:00:00Z", author={"raw": "bot"}, parents=[], message=None
            )
        ),
    )

    assert [request.url for request in transport.requests] == [*IDENTITY, REPOSITORY, COMMITS]
    assert records[0].branch == "develop"
    assert records[0].author == "bot" and records[0].author_account_id is None
    assert records[0].parent_count == 0 and records[0].summary == ""


def test_a_branch_with_a_slash_is_one_encoded_path_segment() -> None:
    collection = f"{API}/repositories/{WORKSPACE}/{REPO}/commits/feature%2Fx"
    records, transport = _commits(
        ScmCommitQuery(REPO, COMMIT_WINDOW, "feature/x"),
        _page(_commit(_sha(2), "2026-10-02T00:00:00Z"), next_url=f"{collection}?page=2"),
        _page(_commit(_sha(1), "2026-09-01T00:00:00Z")),
    )

    assert [request.url for request in transport.requests][3:] == [
        collection,
        f"{collection}?page=2",
    ]
    assert [record.branch for record in records] == ["feature/x"]


def test_the_page_bound_fails_with_result_incomplete_unless_the_last_page_ends_the_read() -> None:
    recent = [
        _page(_commit(_sha(index), "2026-10-02T00:00:00Z"), next_url=f"{COMMITS}?page={index + 2}")
        for index in range(29)
    ]

    with pytest.raises(ConnectorFailure) as captured:
        _commits(
            ScmCommitQuery(REPO, COMMIT_WINDOW, "develop"),
            *recent,
            _page(_commit(_sha(99), "2026-10-02T00:00:00Z"), next_url=f"{COMMITS}?page=31"),
        )
    assert captured.value.reason is ReasonCode.RESULT_INCOMPLETE
    assert "30 pages" in captured.value.safe_message

    records, transport = _commits(
        ScmCommitQuery(REPO, COMMIT_WINDOW, "develop"),
        *recent,
        _page(_commit(_sha(99), "2026-08-01T00:00:00Z"), next_url=f"{COMMITS}?page=31"),
    )
    assert len(records) == 29 and len(transport.requests) == 3 + 30


def test_an_unknown_repository_or_branch_and_a_missing_main_branch_fail_closed() -> None:
    adapter, _ = _adapter(*_identity(), json_response(404, {}))
    with pytest.raises(ConnectorFailure) as missing:
        adapter.execute(
            ConnectorRequest.create(
                "bitbucket.cloud",
                Capability.SCM_COMMITS_LIST,
                ScmCommitQuery(REPO, COMMIT_WINDOW, "nope"),
            )
        )
    assert missing.value.reason is ReasonCode.REPOSITORY_NOT_FOUND
    assert "nope" in missing.value.safe_message

    with pytest.raises(ConnectorFailure) as no_main:
        _commits(ScmCommitQuery(REPO, COMMIT_WINDOW), json_response(200, {"mainbranch": None}))
    assert no_main.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA
    assert "--branch" in no_main.value.safe_message


@pytest.mark.parametrize(
    "item",
    [
        _commit("not-a-hash", "2026-10-02T00:00:00Z"),
        _commit("ABCDEF1", "2026-10-02T00:00:00Z"),
        _commit(_sha(1), "2026-10-02T00:00:00Z", date=None),
        _commit(_sha(1), "2026-10-02T00:00:00Z", parents=None),
        _commit(_sha(1), "2026-10-02T00:00:00Z", message=["x"]),
    ],
)
def test_malformed_commits_fail_closed(item: dict[str, Any]) -> None:
    with pytest.raises(ConnectorFailure) as captured:
        _commits(ScmCommitQuery(REPO, COMMIT_WINDOW, "develop"), _page(item))

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


def test_run_lists_the_commits_of_a_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = _connected(tmp_path)
    transport = FakeTransport(*_identity(), _page(_commit(_sha(1), "2026-10-02T00:00:00Z")))
    _route_runs_to(monkeypatch, tmp_path, keyring, transport)

    result = application.run_application(
        {
            "type": "bitbucket",
            "account": ACCOUNT,
            "capability": "scm.commits.list",
            "repo": REPO,
            "branch": "develop",
            "start": "2026-10-01T00:00:00+00:00",
            "end": "2026-10-15T00:00:00+00:00",
        }
    )

    assert result.ok is True, result.error
    records = result.to_dict()["data"]["records"]
    assert [record["hash"] for record in records] == [_sha(1)]
    assert records[0]["date"] == "2026-10-02T00:00:00+00:00"
    assert transport.requests[3].url == COMMITS


@pytest.mark.parametrize(
    ("capability", "payload", "message"),
    [
        ("scm.commits.list", {"branch": "develop"}, "repo"),
        ("scm.commits.list", {"repo": REPO}, "start and end"),
        ("scm.commits.list", {"repo": REPO, "state": ["OPEN"]}, "state"),
        ("scm.pullrequests.list", {"repo": REPO, "branch": "develop"}, "branch"),
        ("scm.repositories.list", {"branch": "develop"}, "branch"),
    ],
)
def test_run_refuses_invalid_commit_requests_before_provider_work(
    monkeypatch: pytest.MonkeyPatch, capability: str, payload: dict[str, Any], message: str
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)
    window = {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-15T00:00:00+00:00"}
    request = {**(window if message != "start and end" else {}), **payload}

    result = application.run_application(
        {"type": "bitbucket", "account": ACCOUNT, "capability": capability, **request}
    )

    assert result.status == "invalid_input"
    assert result.error is not None and message in result.error.message


def test_the_cli_passes_the_branch(monkeypatch: pytest.MonkeyPatch) -> None:
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
            "bitbucket.cloud",
            "--capability",
            "scm.commits.list",
            "--account",
            ACCOUNT,
            "--repo",
            REPO,
            "--branch",
            "develop",
            "--start",
            "2026-10-01T00:00:00-05:00",
            "--end",
            "2026-10-15T00:00:00-05:00",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen[0]["branch"] == "develop" and seen[0]["repo"] == REPO
