"""The global skill and the agent guide describe the transparent project mode (D3.1)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "agents/skills/solercia-connect/SKILL.md"
GUIDE = ROOT / "docs/USO-CON-AGENTES.md"


def test_the_skill_sets_up_a_project_through_the_global_cli() -> None:
    skill = SKILL.read_text(encoding="utf-8")

    for token in (
        "## Project mode",
        "solercia-connect project init",
        "SOLERCIA_CONNECT_PROJECT_DISCOVERY",
        "SOLERCIA_CONNECT_HOME",
        "project_home_source",
        "per-project launcher",
    ):
        assert token in skill, token
    assert "/Users/" not in skill
    assert "solercia-connect.sh" not in skill


def test_the_skill_keeps_secrets_and_logins_with_the_user() -> None:
    skill = SKILL.read_text(encoding="utf-8")

    assert "Never read, print, or copy `connect.env`, `secrets/`, or `data/`" in skill
    assert "never ask for passwords or tokens in chat" in skill
    action_required = next(line for line in skill.splitlines() if "`action_required`" in line)
    assert "ask the user to run the exact `connect` command" in action_required


def test_the_skill_documents_the_jira_connect_options_and_gmail_scope() -> None:
    skill = SKILL.read_text(encoding="utf-8")

    for token in ("--site", "--token-kind classic|scoped|oauth", "--token-env"):
        assert token in skill, token
    assert "Gmail currently offers `mail.sent.list` only" in skill


def test_the_guide_says_rerunning_the_global_install_updates_cli_and_skill() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    assert "uv tool install --reinstall ." in guide
    assert 'ln -sfn "$(pwd)/agents/skills/solercia-connect" ~/.claude/skills/solercia-connect' in (
        guide
    )
    assert "project init" in guide.split("### Opción A", 1)[1].split("### Opción B", 1)[0]
