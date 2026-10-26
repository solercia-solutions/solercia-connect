"""scm.repositories.create: one private, empty repository over bitbucket-admin (R2).

Decision: docs/decisions/2026-10-25-escritura-creacion-repositorios-bitbucket.md, R-D2. Every call
goes through the FakeTransport; nothing here reaches Bitbucket.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
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
    ProviderFamily,
    ReasonCode,
    RouteKind,
    ScmRepositoryCreateRequest,
    ScmRepositoryCreation,
    record_matches_capability,
)
from solercia_connect.envelope import ResultEnvelope
from solercia_connect.errors import ConnectorFailure, TransportNetworkError, TransportTimeoutError
from solercia_connect.ports import TransportResponse
from solercia_connect.providers.bitbucket import BitbucketAdapter
from solercia_connect.providers.bitbucket_admin import BitbucketAdminAdapter
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import BROWSER_FALLBACK_CAPABILITIES, ConnectorRuntime
from solercia_connect.secret_store import KeyringSecretStore
from solercia_connect.secrets import SecretValue
from solercia_connect.service import create_app
from tests.fakes import NOW, FakeTransport, json_response
from tests.test_bitbucket_connections import MemoryKeyring, UnusedOAuthStore

ACCOUNT = "user@example.com"
WORKSPACE = "idartes-tecnologia"
SLUG = "pandora-front"
PROJECT = "PAN"
TOKEN = "bitbucket-admin-token-value"
ACCOUNT_ID = "557058:acct-1"
API = "https://api.bitbucket.org/2.0"
USER = f"{API}/user"
EMAILS = f"{API}/user/emails"
WORKSPACE_URL = f"{API}/workspaces/{WORKSPACE}"
REPOSITORY_URL = f"{API}/repositories/{WORKSPACE}/{SLUG}"
PROJECT_URL = f"{API}/workspaces/{WORKSPACE}/projects/{PROJECT}"
BASIC = "Basic " + base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()
BODY = {
    "scm": "git",
    "is_private": True,
    "fork_policy": "no_public_forks",
    "project": {"key": PROJECT},
}
CREATED = "2026-10-25T15:04:05.123456+00:00"
RAW_BODY_SECRET = "raw provider detail that must never be echoed"

runner = CliRunner()


def _identity() -> list[Any]:
    return [
        json_response(200, {"account_id": ACCOUNT_ID, "display_name": "User"}),
        json_response(200, {"values": [{"email": ACCOUNT, "is_confirmed": True}]}),
        json_response(200, {"slug": WORKSPACE}),
    ]


def _absent() -> Any:
    return json_response(404, {"type": "error", "error": {"message": RAW_BODY_SECRET}})


def _project(key: str = PROJECT) -> Any:
    return json_response(200, {"type": "project", "key": key, "name": "Pandora"})


def _created(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "repository",
        "slug": SLUG,
        "name": SLUG,
        "full_name": f"{WORKSPACE}/{SLUG}",
        "is_private": True,
        "scm": "git",
        "fork_policy": "no_public_forks",
        "project": {"type": "project", "key": PROJECT},
        "workspace": {"type": "workspace", "slug": WORKSPACE},
        "created_on": CREATED,
        "links": {
            "html": {"href": f"https://bitbucket.org/{WORKSPACE}/{SLUG}"},
            "clone": [
                {"name": "https", "href": f"https://user@bitbucket.org/{WORKSPACE}/{SLUG}.git"},
                {"name": "ssh", "href": f"git@bitbucket.org:{WORKSPACE}/{SLUG}.git"},
            ],
        },
    }
    payload.update(overrides)
    return payload


def _ready(*after: Any) -> list[Any]:
    """Identity, an absent repository and an existing project, then whatever follows."""
    return [*_identity(), _absent(), _project(), *after]


def _adapter(*responses: Any) -> tuple[BitbucketAdminAdapter, FakeTransport]:
    transport = FakeTransport(*responses)
    adapter = BitbucketAdminAdapter(
        transport,
        BasicAuthProvider(ACCOUNT, SecretValue(TOKEN)),
        account=ACCOUNT,
        workspace=WORKSPACE,
        expected_account_id=ACCOUNT_ID,
        clock=lambda: NOW,
    )
    return adapter, transport


def _request(authorized: bool = True, **overrides: Any) -> ConnectorRequest:
    payload = ScmRepositoryCreateRequest(
        overrides.pop("repository", SLUG),
        overrides.pop("project_key", PROJECT),
        write_authorized=authorized,
        **overrides,
    )
    return ConnectorRequest.create("bitbucket.cloud", Capability.SCM_REPOSITORIES_CREATE, payload)


def _create(*responses: Any) -> tuple[Any, FakeTransport]:
    adapter, transport = _adapter(*responses)
    return adapter.execute(_request()), transport


def _failure(*responses: Any) -> tuple[ConnectorFailure, FakeTransport]:
    adapter, transport = _adapter(*responses)
    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(_request())
    return captured.value, transport


def _posts(transport: FakeTransport) -> list[Any]:
    return [request for request in transport.requests if request.method == "POST"]


# --- Request contract -------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["pandora-front", "app_v2", "a.b", "x", "0", "-", "a" * 300])
def test_a_slug_of_lower_case_letters_digits_dot_dash_or_underscore_is_kept_as_given(
    slug: str,
) -> None:
    assert ScmRepositoryCreateRequest(slug, PROJECT).repository == slug


@pytest.mark.parametrize(
    "slug",
    ["", "Pandora", "PANDORA", "a b", " pandora", "pandora ", "a/b", ".", "..", "{8a1f-uuid}"],
)
def test_a_slug_is_never_lowered_or_trimmed_and_nothing_else_is_accepted(slug: str) -> None:
    with pytest.raises(ValueError, match="repository slug"):
        ScmRepositoryCreateRequest(slug, PROJECT)


@pytest.mark.parametrize("key", ["", "P AN", "P/A", "{ba516952-992a}", " PAN", "PAN?"])
def test_the_project_is_one_key_never_a_uuid_or_a_default(key: str) -> None:
    with pytest.raises(ValueError, match="project key"):
        ScmRepositoryCreateRequest(SLUG, key)


def test_the_workspace_is_one_slug_and_the_write_flag_must_be_a_real_boolean() -> None:
    request = ScmRepositoryCreateRequest(SLUG, "MIG_2", workspace="IDARTES-Tecnologia")

    assert request.workspace == WORKSPACE and request.project_key == "MIG_2"
    assert request.write_authorized is False
    with pytest.raises(ValueError, match="workspace"):
        ScmRepositoryCreateRequest(SLUG, PROJECT, workspace="a/b")


def test_the_creation_is_a_write_of_the_admin_target_alone() -> None:
    capability = Capability.SCM_REPOSITORIES_CREATE

    assert capability.value == "scm.repositories.create"
    assert capability in WRITE_CAPABILITIES
    assert target_spec(ConnectionType.BITBUCKET_ADMIN).capabilities == (capability.value,)
    assert capability.value not in target_spec(ConnectionType.BITBUCKET).capabilities
    assert BitbucketAdminAdapter.descriptor.capabilities == frozenset({capability})
    assert BitbucketAdminAdapter.descriptor.provider_id == "bitbucket.cloud"
    assert BitbucketAdminAdapter.descriptor.route is RouteKind.OFFICIAL
    assert capability not in BitbucketAdapter.descriptor.capabilities
    assert ProviderFamily.BITBUCKET not in BROWSER_FALLBACK_CAPABILITIES


# --- Adapter: order, body and record ----------------------------------------------------------


def test_the_creation_proves_identity_checks_both_absences_and_posts_the_fixed_body_once() -> None:
    records, transport = _create(*_ready(json_response(200, _created())))

    assert [(request.method, request.url) for request in transport.requests] == [
        ("GET", USER),
        ("GET", EMAILS),
        ("GET", WORKSPACE_URL),
        ("GET", REPOSITORY_URL),
        ("GET", PROJECT_URL),
        ("POST", REPOSITORY_URL),
    ]
    post = _posts(transport)[0]
    assert post.json_body == BODY
    assert post.headers is not None and post.headers["Content-Type"] == "application/json"
    assert all(r.headers and r.headers["Authorization"] == BASIC for r in transport.requests)
    (record,) = records
    assert isinstance(record, ScmRepositoryCreation)
    assert record_matches_capability(record, Capability.SCM_REPOSITORIES_CREATE)
    assert (record.workspace, record.slug, record.project_key) == (WORKSPACE, SLUG, PROJECT)
    assert record.full_name == f"{WORKSPACE}/{SLUG}"
    assert record.is_private is True
    assert record.web_url == f"https://bitbucket.org/{WORKSPACE}/{SLUG}"
    assert record.clone_https == f"https://bitbucket.org/{WORKSPACE}/{SLUG}.git"
    assert record.created_at == datetime(2026, 10, 25, 15, 4, 5, 123456, tzinfo=UTC)
    assert record.provenance.provider_id == "bitbucket.cloud"
    assert record.provenance.account_id == ACCOUNT_ID
    assert record.provenance.source_id == f"{WORKSPACE}/{SLUG}"
    assert record.provenance.endpoint == "/2.0/repositories/{workspace}/{repo_slug}"


def test_a_named_workspace_is_proven_and_used_instead_of_the_connected_one() -> None:
    other = "otro-workspace"
    adapter, transport = _adapter(
        json_response(200, {"account_id": ACCOUNT_ID}),
        json_response(200, {"values": [{"email": ACCOUNT, "is_confirmed": True}]}),
        json_response(200, {"slug": other}),
        _absent(),
        _project(),
        json_response(
            200,
            _created(full_name=f"{other}/{SLUG}", workspace={"slug": other}, links={}),
        ),
    )

    (record,) = adapter.execute(_request(workspace=other))

    assert record.workspace == other
    assert record.web_url is None and record.clone_https is None
    assert transport.requests[2].url == f"{API}/workspaces/{other}"
    assert _posts(transport)[0].url == f"{API}/repositories/{other}/{SLUG}"


@pytest.mark.parametrize(
    "links",
    [
        {"clone": [{"name": "https", "href": f"http://bitbucket.org/{WORKSPACE}/{SLUG}.git"}]},
        {"clone": [{"name": "https", "href": f"https://evil.example/{WORKSPACE}/{SLUG}.git"}]},
        {"clone": [{"name": "https", "href": None}], "html": {"href": None}},
        {"clone": ["https", {"name": "ssh", "href": "git@bitbucket.org:x/y.git"}]},
        {"clone": "https://bitbucket.org/x/y.git", "html": "https://bitbucket.org/x/y"},
    ],
)
def test_a_link_that_is_not_https_on_bitbucket_org_is_dropped(links: Any) -> None:
    (record,), _ = _create(*_ready(json_response(200, _created(links=links))))

    assert record.clone_https is None and record.web_url is None


def test_links_are_rebuilt_without_any_user_name_or_password() -> None:
    links = {
        "html": {"href": f"https://user:secret@bitbucket.org/{WORKSPACE}/{SLUG}"},
        "clone": [
            {"name": "https", "href": f"https://user:secret@bitbucket.org/{WORKSPACE}/{SLUG}.git"}
        ],
    }

    (record,), _ = _create(*_ready(json_response(200, _created(links=links))))

    assert record.web_url == f"https://bitbucket.org/{WORKSPACE}/{SLUG}"
    assert record.clone_https == f"https://bitbucket.org/{WORKSPACE}/{SLUG}.git"


def test_an_unauthorized_write_makes_no_request_at_all() -> None:
    adapter, transport = _adapter()

    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(_request(authorized=False))

    assert captured.value.reason is ReasonCode.WRITE_NOT_AUTHORIZED
    assert transport.requests == []


def test_another_request_shape_is_refused_without_any_request() -> None:
    adapter, transport = _adapter()
    listing = ConnectorRequest.create(
        "bitbucket.cloud", Capability.SCM_REPOSITORIES_LIST, ScmRepositoryCreateRequest(SLUG, "P")
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(listing)

    assert captured.value.reason is ReasonCode.CAPABILITY_NOT_IMPLEMENTED
    assert transport.requests == []


def test_a_failed_identity_checks_and_writes_nothing() -> None:
    failure, transport = _failure(json_response(401, {}))

    assert failure.reason is ReasonCode.INVALID_CREDENTIALS
    assert len(transport.requests) == 1


# --- Adapter: refusals before the write -------------------------------------------------------


def test_an_existing_repository_is_never_touched() -> None:
    failure, transport = _failure(*_identity(), json_response(200, _created()))

    assert failure.reason is ReasonCode.REPOSITORY_EXISTS
    assert f"{WORKSPACE}/{SLUG}" in failure.safe_message
    assert "nothing was changed" in failure.safe_message
    assert _posts(transport) == [] and len(transport.requests) == 4


def test_an_unavailable_repository_check_stops_the_write() -> None:
    failure, transport = _failure(*_identity(), json_response(503, {}))

    assert failure.reason is ReasonCode.PROVIDER_UNAVAILABLE
    assert _posts(transport) == []


def test_a_private_repository_the_account_cannot_see_also_stops_the_write() -> None:
    failure, transport = _failure(*_identity(), json_response(403, {"error": {}}))

    assert failure.reason is ReasonCode.AUTHORIZATION_DENIED
    assert "nothing was created" in failure.safe_message
    assert _posts(transport) == []


def test_a_missing_project_stops_the_write() -> None:
    failure, transport = _failure(*_identity(), _absent(), _absent())

    assert failure.reason is ReasonCode.PROJECT_NOT_FOUND
    assert PROJECT in failure.safe_message and RAW_BODY_SECRET not in failure.safe_message
    assert _posts(transport) == []


def test_a_project_key_bitbucket_spells_otherwise_stops_the_write() -> None:
    failure, transport = _failure(*_identity(), _absent(), _project("Pan"))

    assert failure.reason is ReasonCode.INVALID_INPUT
    assert "--project Pan" in failure.safe_message
    assert _posts(transport) == []


@pytest.mark.parametrize("project", [json_response(200, {"name": "x"}), _project("P/A")])
def test_a_malformed_project_answer_stops_the_write(project: Any) -> None:
    failure, transport = _failure(*_identity(), _absent(), project)

    assert failure.reason is ReasonCode.MALFORMED_PROVIDER_DATA
    assert _posts(transport) == []


def test_a_denied_project_read_names_only_the_scope_names_it_carries() -> None:
    denial = json_response(
        403,
        {"error": {"message": RAW_BODY_SECRET, "detail": {"required": ["read:project:bitbucket"]}}},
    )

    failure, transport = _failure(*_identity(), _absent(), denial)

    assert failure.reason is ReasonCode.AUTHORIZATION_DENIED
    assert "read:project:bitbucket" in failure.safe_message
    assert RAW_BODY_SECRET not in failure.safe_message
    assert _posts(transport) == []


# --- Adapter: the one POST --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (400, ReasonCode.REQUEST_REJECTED),
        (401, ReasonCode.INVALID_CREDENTIALS),
        (403, ReasonCode.AUTHORIZATION_DENIED),
        (404, ReasonCode.WORKSPACE_NOT_FOUND),
        (409, ReasonCode.REQUEST_REJECTED),
        (429, ReasonCode.RATE_LIMITED),
    ],
)
def test_a_refused_post_is_typed_sanitized_and_sent_once(status: int, reason: ReasonCode) -> None:
    refusal = json_response(status, {"type": "error", "error": {"message": RAW_BODY_SECRET}})

    failure, transport = _failure(*_ready(refusal))

    assert failure.reason is reason
    assert RAW_BODY_SECRET not in failure.safe_message
    assert len(_posts(transport)) == 1


@pytest.mark.parametrize(
    "answer",
    [
        json_response(500, {"error": {"message": RAW_BODY_SECRET}}),
        json_response(503, {}),
        TransportTimeoutError("timed out"),
        TransportNetworkError("reset"),
        RuntimeError("socket closed"),
        TransportResponse(200, {"Content-Type": "application/json"}, b"<html>"),
    ],
)
def test_an_unconfirmed_post_may_have_created_the_repository(answer: Any) -> None:
    failure, transport = _failure(*_ready(answer))

    assert failure.reason is ReasonCode.WRITE_UNVERIFIED
    assert "may have been created" in failure.safe_message
    assert "Bitbucket" in failure.safe_message
    assert RAW_BODY_SECRET not in failure.safe_message
    assert len(_posts(transport)) == 1


@pytest.mark.parametrize(
    "answer",
    [
        _created(is_private=False),
        _created(slug="other"),
        _created(full_name=f"otro/{SLUG}"),
        _created(project={"key": "OTHER"}),
        _created(workspace={"slug": "otro"}),
        _created(project=None),
        _created(created_on="yesterday"),
        ["not", "an", "object"],
    ],
)
def test_an_answer_that_does_not_match_says_the_repository_was_created(answer: Any) -> None:
    failure, transport = _failure(*_ready(json_response(200, answer)))

    assert failure.reason is ReasonCode.WRITE_UNVERIFIED
    assert "was created" in failure.safe_message
    assert "review it in Bitbucket" in failure.safe_message
    assert len(_posts(transport)) == 1


def test_the_runtime_never_retries_the_post() -> None:
    adapter, transport = _adapter(*_ready(json_response(503, {})))
    registry = ProviderRegistry()
    registry.register(adapter)

    outcome = ConnectorRuntime(registry, sleeper=lambda _: None).execute(_request())

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.WRITE_UNVERIFIED
    assert len(_posts(transport)) == 1


def test_the_runtime_refuses_an_unauthorized_write_without_any_provider_call() -> None:
    adapter, transport = _adapter()
    registry = ProviderRegistry()
    registry.register(adapter)

    outcome = ConnectorRuntime(registry).execute(_request(authorized=False))

    assert outcome.receipt.final_reason is ReasonCode.WRITE_NOT_AUTHORIZED
    assert transport.requests == []


def test_the_admin_adapter_has_exactly_one_write_call() -> None:
    root = Path(__file__).parents[1] / "src/solercia_connect/providers"
    source = (root / "bitbucket_admin.py").read_text(encoding="utf-8")

    assert source.count('"POST"') == 1
    for verb in ('"PUT"', '"PATCH"', '"DELETE"'):
        assert verb not in source
        assert verb not in (root / "bitbucket.py").read_text(encoding="utf-8")


# --- Application, CLI and HTTP ------------------------------------------------------------------


def _values(tmp_path: Path) -> dict[str, str]:
    return {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")}


def _connected_admin(tmp_path: Path) -> MemoryKeyring:
    """Connect ACCOUNT as `connect bitbucket-admin` does, against fakes only."""
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
    assert service.connect_bitbucket(
        ACCOUNT,
        workspace=WORKSPACE,
        token=SecretValue(TOKEN),
        connection_type=ConnectionType.BITBUCKET_ADMIN,
    ).ok
    return keyring


def _route_runs_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, keyring: MemoryKeyring, transport: Any
) -> None:
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


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "bitbucket-admin",
        "account": ACCOUNT,
        "capability": "scm.repositories.create",
        "repo": SLUG,
        "project": PROJECT,
        "allow_write": True,
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


def test_an_authorized_run_creates_the_repository_through_the_admin_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = _connected_admin(tmp_path)
    transport = FakeTransport(*_ready(json_response(200, _created())))
    _route_runs_to(monkeypatch, tmp_path, keyring, transport)

    result = application.run_application(_payload())

    assert result.ok is True, result.error
    assert result.provider == "bitbucket-admin" and result.route == "official"
    (record,) = result.to_dict()["data"]["records"]
    assert record["clone_https"] == f"https://bitbucket.org/{WORKSPACE}/{SLUG}.git"
    assert record["is_private"] is True and record["project_key"] == PROJECT
    assert result.receipt is not None
    assert len(_posts(transport)) == 1 and _posts(transport)[0].json_body == BODY
    assert all(r.headers and r.headers["Authorization"] == BASIC for r in transport.requests)
    assert TOKEN not in json.dumps(result.to_dict())


def test_a_run_without_the_admin_connection_is_a_configuration_error(tmp_path: Path) -> None:
    result = application.run_application(
        _payload(), settings=load_settings(environ=_values(tmp_path))
    )

    assert result.status == "configuration_error"
    assert result.error is not None and "connect bitbucket-admin" in result.error.message


def test_the_read_connection_cannot_create_and_the_cli_routes_to_the_admin_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)
    seen: list[dict[str, Any]] = []

    def fake_run(payload: dict[str, Any]) -> Any:
        seen.append(payload)
        return ResultEnvelope.success(operation="run", data={"records": []})

    with_read = application.run_application(_payload(type="bitbucket"))
    monkeypatch.setattr(cli, "run_application", fake_run)
    result = runner.invoke(
        cli.app,
        [
            "run",
            "--provider",
            "bitbucket.cloud",
            "--capability",
            "scm.repositories.create",
            "--account",
            ACCOUNT,
            "--repo",
            SLUG,
            "--project",
            PROJECT,
            "--workspace",
            WORKSPACE,
            "--allow-write",
        ],
    )

    assert with_read.status == "invalid_input"
    assert with_read.error is not None
    assert "not available for bitbucket" in with_read.error.message
    assert result.exit_code == 0, result.output
    (payload,) = seen
    assert payload["type"] == "bitbucket-admin"
    assert (payload["repo"], payload["project"], payload["workspace"]) == (SLUG, PROJECT, WORKSPACE)
    assert payload["allow_write"] is True and payload["allow_browser_fallback"] is False


def test_without_allow_write_the_cli_fails_before_any_provider_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = runner.invoke(
        cli.app,
        [
            "run",
            "--provider",
            "bitbucket.cloud",
            "--capability",
            "scm.repositories.create",
            "--account",
            ACCOUNT,
            "--repo",
            SLUG,
            "--project",
            PROJECT,
        ],
        env={},
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "write_not_authorized"
    assert "creates a Bitbucket repository" in payload["error"]["message"]
    assert payload["error"]["remediation"] == [
        f"solercia-connect run --provider bitbucket.cloud --capability scm.repositories.create "
        f"--account {ACCOUNT} --repo <slug> --project <KEY> --allow-write"
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"repo": None}, "repo is required"),
        ({"project": None}, "project is required"),
        ({"repo": "Pandora"}, "repository slug"),
        ({"project": "{uuid}"}, "project key"),
        ({"account": None}, "account is required"),
        ({"allow_write": "yes"}, "allow_write must be a boolean"),
        ({"allow_browser_fallback": True}, "browser fallback"),
        ({"workspace": "a/b"}, "workspace"),
        ({"state": ["OPEN"]}, "state does not apply to scm.repositories.create"),
        ({"branch": "main"}, "branch does not apply to scm.repositories.create"),
        ({"start": "2026-10-01T00:00:00+00:00", "end": "2026-11-01T00:00:00+00:00"}, "window"),
        ({"sprint": "current"}, "sprint"),
    ],
)
def test_malformed_creation_requests_are_invalid_input(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], message: str
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(**overrides))

    assert result.status == "invalid_input"
    assert result.error is not None and message in result.error.message


def test_a_project_still_applies_to_no_read_of_bitbucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(
        {
            "type": "bitbucket",
            "account": ACCOUNT,
            "capability": "scm.repositories.list",
            "project": PROJECT,
        }
    )

    assert result.status == "invalid_input"
    assert result.error is not None
    assert "project and sprint apply only to external.issues.open.list" in result.error.message
    assert "scm.repositories.create" in result.error.message


def test_the_application_never_creates_for_a_caller_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application, "registry_from_environment", _no_registry)

    result = application.run_application(_payload(), interactive=False)

    assert result.status == "forbidden"
    assert result.error is not None and result.error.code == "write_not_available_over_http"
    assert "scm.repositories.create" in result.error.message


def test_http_rejects_the_creation_before_the_application_runs(tmp_path: Path) -> None:
    def run_handler(payload: dict[str, Any], *, browser_via_http: bool = False) -> Any:
        raise AssertionError("HTTP must not reach the application for a write")

    values = {**_values(tmp_path), "SOLERCIA_CONNECT_API_KEY": "local-api-key"}
    client = TestClient(create_app(settings=load_settings(environ=values), run_handler=run_handler))

    response = client.post(
        "/v1/run",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json={
            "type": "bitbucket-admin",
            "account": ACCOUNT,
            "capability": "scm.repositories.create",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "write_not_available_over_http"


@pytest.mark.parametrize(
    ("answer", "code", "hint"),
    [
        (json_response(200, _created()), "repository_exists", "nothing was changed"),
        (None, "project_not_found", "project key"),
        (json_response(503, {}), "write_unverified", "before any new attempt"),
        (json_response(401, {}), "invalid_credentials", "admin:repository:bitbucket"),
    ],
)
def test_each_failure_names_its_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Any, code: str, hint: str
) -> None:
    keyring = _connected_admin(tmp_path)
    if code == "repository_exists":
        responses = [*_identity(), answer]
    elif code == "project_not_found":
        responses = [*_identity(), _absent(), _absent()]
    elif code == "invalid_credentials":
        responses = [answer]
    else:
        responses = _ready(answer)
    _route_runs_to(monkeypatch, tmp_path, keyring, FakeTransport(*responses))

    result = application.run_application(_payload())

    assert result.error is not None and result.error.code == code
    assert hint in " ".join(result.error.remediation)


def test_the_legacy_query_helper_refuses_to_build_the_creation() -> None:
    with pytest.raises(cli._InputError, match=r"scm\.repositories\.create is a write"):
        cli._query_payload(
            Capability.SCM_REPOSITORIES_CREATE,
            start=None,
            end=None,
            account=ACCOUNT,
            calendar_id="primary",
            assignee=None,
            jql=None,
            meeting_id=None,
            limit=50,
        )


def test_the_cli_help_names_both_uses_of_project() -> None:
    result = runner.invoke(cli.app, ["run", "--help"], env={"COLUMNS": "200"})

    assert "scm.repositories.create" in result.output
    assert "external.issues.open.list" in result.output
