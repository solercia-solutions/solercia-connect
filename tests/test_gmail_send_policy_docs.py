"""The Gmail two-step send is declared before it exists in code (decision D5).

Decision: docs/decisions/2026-10-25-envio-correo-gmail-dos-pasos.md. These checks keep the policy
documents and the agent skill naming the third write, its separate connection, and the human
review between the draft and the send.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
ADR = ROOT / "docs/decisions/2026-10-25-envio-correo-gmail-dos-pasos.md"


def test_the_adr_records_the_decision_and_cites_the_official_endpoints() -> None:
    text = ADR.read_text(encoding="utf-8")

    assert "- **Estado:** Aceptado" in text
    for token in (
        "mail.draft.create",
        "mail.draft.send",
        "gmail-compose",
        "https://www.googleapis.com/auth/gmail.compose",
        "--allow-write",
        "--allow-duplicate",
        "duplicate_suspected",
        "write_unverified",
        "write_scope_missing",
        "users.drafts/create",
        "users.drafts/get",
        "users.drafts/send",
        "users.messages/list",
        "Restricted scopes",
    ):
        assert token in text, token
    for voseo in ("revisá", "podés", "tenés", "enviá", "usá"):
        assert voseo not in text, voseo


def test_policy_documents_name_three_writes_and_the_separate_connection() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    platform = (ROOT / "docs/decisions/2026-08-23-solercia-platform-decisions.md").read_text(
        encoding="utf-8"
    )

    assert "exactly three bounded writes" in agents
    assert "exactly two bounded writes" not in agents
    assert "exactly\nthree bounded writes" in security
    assert "## Write exception: Gmail draft and send" in security
    for text in (agents, security, platform):
        assert "mail.draft.create" in text
        assert "mail.draft.send" in text
        assert "gmail-compose" in text
        assert "2026-10-25-envio-correo-gmail-dos-pasos.md" in text


def test_the_skill_states_the_two_step_agent_workflow() -> None:
    skill = (ROOT / "agents/skills/solercia-connect/SKILL.md").read_text(encoding="utf-8")

    assert "There are exactly three writes" in skill
    for token in (
        "solercia-connect connect gmail-compose <account>",
        "Confirm with the user which account sends the email",
        "--capability mail.draft.create",
        "--body-file",
        "--body-stdin",
        "Gmail > Drafts",
        "explicitly approves that draft id",
        "--capability mail.draft.send",
        "--draft-id",
        "never invent or guess recipients",
        "never pass\n   `--allow-duplicate` without the user's explicit approval",
    ):
        assert token in skill, token
    assert "@idartes" not in skill
