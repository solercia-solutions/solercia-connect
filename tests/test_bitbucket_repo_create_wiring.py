"""Repository creation close (R3): runbook, capability matrix, README, skill and agent guide."""

from __future__ import annotations

from pathlib import Path

from solercia_connect.matrix import CAPABILITY_MATRIX

ROOT = Path(__file__).parents[1]
ADR_NAME = "2026-10-25-escritura-creacion-repositorios-bitbucket.md"
ADMIN_SCOPES = (
    "read:user:bitbucket",
    "read:workspace:bitbucket",
    "read:repository:bitbucket",
    "read:project:bitbucket",
    "admin:repository:bitbucket",
)
PUSH = (
    "git remote add origin "
    "https://x-bitbucket-api-token-auth@bitbucket.org/idartes-tecnologia/pandora-front.git"
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_the_matrix_lists_the_creation_on_its_own_connection() -> None:
    rows = [row for row in CAPABILITY_MATRIX if row.capability == "scm.repositories.create"]

    assert [(row.provider, row.channel, row.v0_1_status) for row in rows] == [
        ("Bitbucket Cloud REST 2.0 (bitbucket-admin)", "official", "implemented-offline")
    ]
    assert "--allow-write" in rows[0].notes


def test_the_runbook_covers_the_admin_token_the_creation_and_the_push() -> None:
    runbook = _read("docs/runbooks/bitbucket.md")

    for token in (
        "## Create a repository",
        "BITBUCKET_ADMIN_API_TOKEN",
        "solercia-connect connect bitbucket-admin user@example.com --workspace idartes-tecnologia",
        "--token-env BITBUCKET_ADMIN_API_TOKEN",
        "solercia-connect status bitbucket-admin user@example.com",
        "solercia-connect disconnect bitbucket-admin user@example.com",
        "--capability scm.repositories.create",
        "--repo pandora-front --project PAN --allow-write",
        "bitbucket-admin:<email>",
        PUSH,
        "git push -u origin main",
        "repository_exists",
        "project_not_found",
        "request_rejected",
        "write_unverified",
        "clone_https",
        ADR_NAME,
    ):
        assert token in runbook, token
    for scope in ADMIN_SCOPES:
        assert scope in runbook, scope


def test_the_readme_and_the_agent_guide_name_the_fourth_write() -> None:
    readme = _read("README.md")
    guide = _read("docs/USO-CON-AGENTES.md")
    outlook = _read("docs/runbooks/outlook.md")

    for text in (readme, guide):
        assert "scm.repositories.create" in text
        assert "bitbucket-admin" in text
        assert "--allow-write" in text
    assert "one of the four exceptions" in readme
    assert "one of the three exceptions" not in readme
    assert "one of the four" in outlook
    assert "## 5. Las cuatro escrituras" in guide
    assert "git push -u origin" in guide


def test_the_skill_shows_the_admin_connect_the_creation_and_the_push() -> None:
    skill = _read("agents/skills/solercia-connect/SKILL.md")

    for token in (
        "| `scm.repositories.create` (Bitbucket write) | "
        "yes, `bitbucket-admin`, with `--allow-write`",
        "solercia-connect connect bitbucket-admin <account> --workspace <workspace> "
        "--token-env BITBUCKET_ADMIN_API_TOKEN",
        "--capability scm.repositories.create",
        "--repo <slug> --project <KEY> --allow-write",
        "git remote add origin https://x-bitbucket-api-token-auth@bitbucket.org/<workspace>/<slug>.git",
        "git push -u origin <branch>",
        "| Bitbucket repository creation |",
    ):
        assert token in skill, token
