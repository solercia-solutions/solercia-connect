"""The Bitbucket ADR is accepted and every policy document names the read-only family (B0)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
ADR = ROOT / "docs/decisions/2026-10-26-conector-bitbucket.md"
ADR_NAME = "2026-10-26-conector-bitbucket.md"
READ_SCOPES = (
    "read:user:bitbucket",
    "read:workspace:bitbucket",
    "read:repository:bitbucket",
    "read:pullrequest:bitbucket",
)


def test_the_adr_records_the_three_decisions_with_official_sources() -> None:
    text = ADR.read_text(encoding="utf-8")

    for token in (
        "- **Estado:** Aceptado",
        "## Contexto",
        "## Decisión",
        "## Alternativas descartadas o diferidas",
        "## Riesgos",
        "## Plan de reversión",
        "## Referencias oficiales",
        "B-D0",
        "B-D1",
        "B-D2",
        "idartes-tecnologia",
        "bitbucket:<email>",
        "result_incomplete",
        "https://developer.atlassian.com/cloud/bitbucket/rest/intro/",
        "https://support.atlassian.com/bitbucket-cloud/docs/create-an-api-token/",
        "https://support.atlassian.com/bitbucket-cloud/docs/api-request-limits/",
        "https://support.atlassian.com/bitbucket-cloud/docs/workspace-access-tokens/",
        "https://support.atlassian.com/bitbucket-cloud/docs/use-oauth-on-bitbucket-cloud/",
        "https://dac-static.atlassian.com/cloud/bitbucket/swagger.v3.json",
    ):
        assert token in text, token
    for scope in READ_SCOPES:
        assert scope in text, scope
    for voseo in ("hacé", "podés", "tenés", "usá", "verificá", "ingresá", "seleccioná"):
        assert voseo not in text, voseo


def test_policy_documents_add_bitbucket_without_a_fourth_write() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    platform = (ROOT / "docs/decisions/2026-08-23-solercia-platform-decisions.md").read_text(
        encoding="utf-8"
    )
    skill = (ROOT / "agents/skills/solercia-connect/SKILL.md").read_text(encoding="utf-8")

    for text in (agents, security, platform, skill):
        assert "Bitbucket" in text
    for text in (agents, security, platform):
        assert ADR_NAME in text
    assert "exactly three bounded writes" in agents
    assert "exactly\nthree bounded writes" in security
    assert "There are exactly three writes" in skill
    for scope in READ_SCOPES:
        assert scope in security, scope
        assert scope in skill, scope
    assert "bitbucket:<email>" in security


def test_the_skill_documents_the_bitbucket_connect_and_reads() -> None:
    skill = (ROOT / "agents/skills/solercia-connect/SKILL.md").read_text(encoding="utf-8")

    for token in (
        "solercia-connect connect bitbucket <account> --workspace <workspace>",
        "--token-env BITBUCKET_API_TOKEN",
        "--capability scm.repositories.list",
        "--capability scm.pullrequests.list",
        "--capability scm.commits.list",
        "result_incomplete",
    ):
        assert token in skill, token
