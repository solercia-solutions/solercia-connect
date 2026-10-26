"""The Bitbucket repository creation is declared before it exists in code (R0).

Decision: docs/decisions/2026-10-25-escritura-creacion-repositorios-bitbucket.md. These checks keep
the policy documents and the agent skill naming the fourth write, its separate bitbucket-admin
connection, and the read-only bitbucket target.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
ADR_NAME = "2026-10-25-escritura-creacion-repositorios-bitbucket.md"
ADR = ROOT / "docs/decisions" / ADR_NAME
ADMIN_SCOPES = (
    "read:user:bitbucket",
    "read:workspace:bitbucket",
    "read:repository:bitbucket",
    "read:project:bitbucket",
    "admin:repository:bitbucket",
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_the_adr_records_the_four_decisions_and_cites_the_official_specification() -> None:
    text = ADR.read_text(encoding="utf-8")

    for token in (
        "- **Estado:** Aceptado",
        "## Contexto",
        "## Decisión",
        "## Riesgos",
        "## Plan de reversión",
        "## Alternativas descartadas",
        "## Referencias oficiales",
        "R-D0",
        "R-D1",
        "R-D2",
        "R-D3",
        "scm.repositories.create",
        "bitbucket-admin",
        "bitbucket-admin:<email>",
        "--allow-write",
        "is_private",
        "no_public_forks",
        "repository_exists",
        "project_not_found",
        "write_unverified",
        "x-bitbucket-api-token-auth",
        "POST /repositories/{workspace}/{repo_slug}",
        "GET /workspaces/{workspace}/projects/{project_key}",
        "the repository is automatically assigned to the oldest project in the workspace",
        "https://dac-static.atlassian.com/cloud/bitbucket/swagger.v3.json",
        "https://developer.atlassian.com/cloud/bitbucket/rest/api-group-repositories/",
        "https://developer.atlassian.com/cloud/bitbucket/rest/api-group-projects/",
    ):
        assert token in text, token
    for scope in ADMIN_SCOPES:
        assert scope in text, scope
    for voseo in ("hacé", "podés", "tenés", "usá", "verificá", "ingresá", "revisá", "creá"):
        assert voseo not in text, voseo


def test_policy_documents_name_four_writes_and_keep_the_bitbucket_target_read_only() -> None:
    agents = _read("AGENTS.md")
    security = _read("SECURITY.md")
    platform = _read("docs/decisions/2026-08-23-solercia-platform-decisions.md")
    skill = _read("agents/skills/solercia-connect/SKILL.md")

    assert "exactly four bounded writes" in agents
    assert "exactly three bounded writes" not in agents
    assert "exactly\nfour bounded writes" in security
    assert "## Write exception: Bitbucket repository creation" in security
    assert "There are exactly four writes" in skill
    for text in (agents, security, platform, skill):
        assert "scm.repositories.create" in text
        assert "bitbucket-admin" in text
    for text in (agents, security, platform):
        assert ADR_NAME in text
    assert "The `bitbucket` target stays read-only" in agents
    assert "bitbucket-admin:<email>" in security
    for scope in ADMIN_SCOPES:
        assert scope in security, scope


def test_the_skill_keeps_the_creation_bounded_and_the_push_in_git() -> None:
    skill = _read("agents/skills/solercia-connect/SKILL.md")

    for token in (
        "Create a Bitbucket repository only when the user explicitly asks for it",
        "never pass `--allow-write` or\n  `--allow-browser-fallback` to the `bitbucket` reads",
        "`repository_exists`",
        "Solercia Connect never pushes",
    ):
        assert token in skill, token
