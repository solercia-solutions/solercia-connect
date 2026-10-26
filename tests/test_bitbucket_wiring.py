"""Bitbucket wiring close (B5): doctor, capability matrix, project template, runbook and docs."""

from __future__ import annotations

from pathlib import Path

import pytest

from solercia_connect.cli import _project_next_steps
from solercia_connect.config import ConnectionProfile, ProfileStore, load_settings
from solercia_connect.doctor import DoctorService
from solercia_connect.matrix import CAPABILITY_MATRIX
from solercia_connect.project_home import connect_env_template, prepare_project_home
from tests.fakes import NOW

ROOT = Path(__file__).parents[1]
ACCOUNT = "user@example.com"
WORKSPACE = "idartes-tecnologia"
SCM_CAPABILITIES = ("scm.repositories.list", "scm.pullrequests.list", "scm.commits.list")
READ_SCOPES = (
    "read:user:bitbucket",
    "read:workspace:bitbucket",
    "read:repository:bitbucket",
    "read:pullrequest:bitbucket",
)


def _doctor(
    tmp_path: Path, *, keyring_ready: bool = True, home: Path | None = None
) -> tuple[DoctorService, ProfileStore]:
    environ = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")}
    if home is not None:
        environ["SOLERCIA_CONNECT_HOME"] = str(home)
    settings = load_settings(environ=environ)
    profiles = ProfileStore.from_settings(settings)
    service = DoctorService(
        settings,
        profiles,
        module_available=lambda name: name == "keyring",
        keyring_probe=lambda: (keyring_ready, "test-backend"),
        browser_probe=lambda: pytest.fail("Bitbucket must not probe Playwright"),
    )
    return service, profiles


def test_doctor_reports_a_connected_bitbucket_account_without_touching_its_token(
    tmp_path: Path,
) -> None:
    service, profiles = _doctor(tmp_path)
    profiles.save(
        ConnectionProfile.create(
            "bitbucket", ACCOUNT, now=NOW, workspace=WORKSPACE, provider_account_id="a-1"
        )
    )

    report = service.inspect("bitbucket", ACCOUNT)
    checks = {check.name: check for check in report.checks}

    assert report.ready is True
    assert report.capabilities == SCM_CAPABILITIES
    assert report.missing_configuration == () and report.oauth_scopes == ()
    for scope in READ_SCOPES:
        assert any(scope in item for item in report.external_prerequisites), scope
    assert "browser_profile" not in checks and "microsoft_browser_route" not in checks
    assert checks["keyring_package"].status == "ready"
    assert checks["keyring_backend"].status == "ready"
    assert checks["bitbucket_connection"].status == "present"
    assert WORKSPACE in checks["bitbucket_connection"].detail
    assert ACCOUNT not in str(report.to_dict()["checks"])


def test_doctor_requires_secure_storage_and_names_the_exact_connect_command(
    tmp_path: Path,
) -> None:
    service, _ = _doctor(tmp_path, keyring_ready=False)

    report = service.inspect("bitbucket", ACCOUNT)
    checks = {check.name: check for check in report.checks}

    assert report.ready is False
    assert checks["keyring_backend"].status == "unavailable"
    assert checks["keyring_backend"].required is True
    assert checks["bitbucket_connection"].status == "absent"
    assert checks["bitbucket_connection"].remediation[0] == (
        "solercia-connect connect bitbucket <account> --workspace <workspace> "
        "--token-env BITBUCKET_API_TOKEN"
    )
    assert "docs/runbooks/bitbucket.md" in " ".join(checks["bitbucket_connection"].remediation)


def test_doctor_in_project_mode_keeps_the_token_in_the_project_folder(tmp_path: Path) -> None:
    home = tmp_path / "project" / ".solercia-connect"
    (tmp_path / "project").mkdir()
    prepare_project_home(home)
    service, _ = _doctor(tmp_path, keyring_ready=False, home=home)

    report = service.inspect("bitbucket", ACCOUNT)
    checks = {check.name: check for check in report.checks}

    assert report.ready is True
    assert checks["keyring_backend"].status == "not_required"
    assert checks["project_mode"].status == "ready"


def test_the_matrix_lists_the_three_reads_as_implemented_offline() -> None:
    rows = [row for row in CAPABILITY_MATRIX if row.provider == "Bitbucket Cloud REST 2.0"]

    assert [row.capability for row in rows] == list(SCM_CAPABILITIES)
    assert {row.channel for row in rows} == {"official"}
    assert {row.v0_1_status for row in rows} == {"implemented-offline"}


def test_project_init_offers_the_bitbucket_token_variable_and_connect_step() -> None:
    template = connect_env_template()
    steps = _project_next_steps(
        Path("/tmp/project/.solercia-connect"),
        discoverable=True,
        discovery=True,
        explicit_home=None,
    )

    assert "# BITBUCKET_API_TOKEN=" in template.splitlines()
    assert "Bitbucket" in template
    assert any(
        "solercia-connect connect bitbucket <account> --workspace <workspace>" in step
        and "--token-env BITBUCKET_API_TOKEN" in step
        for step in steps
    )


def test_the_runbook_covers_the_token_connection_reads_and_errors() -> None:
    runbook = (ROOT / "docs/runbooks/bitbucket.md").read_text(encoding="utf-8")

    for token in (
        "id.atlassian.com",
        "Security",
        "Create API token with scopes",
        "expiry",
        "solercia-connect project init",
        "BITBUCKET_API_TOKEN",
        "solercia-connect connect bitbucket user@example.com --workspace idartes-tecnologia",
        "--token-env BITBUCKET_API_TOKEN",
        "solercia-connect status bitbucket user@example.com",
        "solercia-connect disconnect bitbucket user@example.com",
        "--capability scm.repositories.list",
        "--capability scm.pullrequests.list",
        "--capability scm.commits.list",
        "invalid_credentials",
        "authorization_denied",
        "authorization_boundary_violation",
        "workspace_not_found",
        "repository_not_found",
        "result_incomplete",
        "rate_limited",
        "2026-10-26-conector-bitbucket.md",
    ):
        assert token in runbook, token
    for scope in READ_SCOPES:
        assert scope in runbook, scope


def test_the_readme_skill_and_runbook_index_name_the_bitbucket_reads() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    index = (ROOT / "docs/runbooks/README.md").read_text(encoding="utf-8")
    skill = (ROOT / "agents/skills/solercia-connect/SKILL.md").read_text(encoding="utf-8")

    assert "[Bitbucket](bitbucket.md)" in index
    assert "docs/runbooks/bitbucket.md" in readme
    for capability in SCM_CAPABILITIES:
        assert f"`{capability}`" in readme, capability
        assert f"`{capability}` (Bitbucket)" in skill, capability
