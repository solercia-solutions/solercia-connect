from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from solercia_connect import __version__
from solercia_connect.cli import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "solercia.connect/v1"
    assert payload["operation"] == "version"
    # Reported version must match the installed package and stay a valid release number,
    # so a release bump does not need this test edited to keep passing.
    assert payload["data"]["version"] == __version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", payload["data"]["version"])


def test_providers_command_reports_matrix_without_credentials(monkeypatch: object) -> None:
    del monkeypatch
    result = runner.invoke(app, ["providers"], env={})

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["operation"] == "providers"
    assert "microsoft365.browser" not in payload["data"]["configured"]
    assert [item["type"] for item in payload["data"]["targets"][:4]] == [
        "outlook",
        "teams",
        "onedrive",
        "gmail",
    ]
    assert '"mail.sent.list"' in result.stdout
    assert "ACCESS_TOKEN" not in result.stdout


def test_unconfigured_provider_returns_typed_failure() -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--provider",
            "microsoft.graph",
            "--capability",
            "mail.sent.list",
            "--start",
            "2026-08-01T00:00:00+00:00",
            "--end",
            "2026-09-01T00:00:00+00:00",
        ],
        env={},
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["schema"] == "solercia.connect/v1"
    assert payload["error"]["code"] == "configuration_invalid"


def test_doctor_and_config_validate_are_structured_and_never_print_secret_values(
    tmp_path: Path,
) -> None:
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "SOLERCIA_CONNECT_API_KEY": "must-not-print",
    }

    doctor = runner.invoke(app, ["doctor", "outlook", "user@example.com"], env=env)
    validate = runner.invoke(app, ["config", "validate"], env=env)

    assert doctor.exit_code == 1
    doctor_payload = json.loads(doctor.stdout)
    assert doctor_payload["operation"] == "doctor"
    assert doctor_payload["error"]["missing_configuration"] == [
        "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID"
    ]
    assert validate.exit_code == 0
    assert json.loads(validate.stdout)["status"] == "valid"
    assert "must-not-print" not in doctor.stdout + validate.stdout


def test_typer_usage_failures_are_uniform_json_with_exit_code_2() -> None:
    missing = runner.invoke(app, ["connect", "outlook"], env={})
    unknown = runner.invoke(app, ["not-a-command"], env={})

    for result in (missing, unknown):
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["schema"] == "solercia.connect/v1"
        assert payload["status"] == "invalid_input"
        assert payload["error"]["code"] == "invalid_input"
        assert "Traceback" not in result.stdout


def test_cli_output_path_is_private_and_requires_explicit_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "providers.json"

    first = runner.invoke(app, ["providers", "--output", str(target)], env={})
    second = runner.invoke(app, ["providers", "--output", str(target)], env={})
    forced = runner.invoke(
        app,
        ["providers", "--output", str(target), "--force"],
        env={},
    )

    assert first.exit_code == 0
    assert target.stat().st_mode & 0o777 == 0o600
    assert second.exit_code == 1
    assert json.loads(second.stdout)["error"]["code"] == "output_error"
    assert forced.exit_code == 0


def test_roadmap_names_every_requested_provider_and_has_honest_open_work() -> None:
    roadmap = (Path(__file__).parents[1] / "docs/ROADMAP.md").read_text()

    for provider in ("Outlook", "Teams", "Gmail", "Google Calendar", "Jira", "GLPI"):
        assert provider in roadmap
    assert "- [ ]" in roadmap
    assert "real tenant" in roadmap.lower()


def test_readme_documents_the_no_client_id_browser_fallback_make_path() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text()

    assert "make connect TYPE=outlook ACCOUNT=user@example.com ALLOW_BROWSER_FALLBACK=1" in readme
    assert "`connected_browser`" in readme
    assert '"route": "browser"' in readme


def test_canonical_agent_symlinks_are_valid() -> None:
    root = Path(__file__).parents[1]

    assert (root / "CLAUDE.md").is_symlink()
    assert (root / "CLAUDE.md").resolve() == (root / "AGENTS.md").resolve()
    assert (root / ".agents/skills/solercia-connect").resolve() == (
        root / "agents/skills/solercia-connect"
    ).resolve()
    assert (root / ".claude/skills/solercia-connect").resolve() == (
        root / "agents/skills/solercia-connect"
    ).resolve()


def test_skill_frontmatter_description_is_one_physical_line() -> None:
    skill = (Path(__file__).parents[1] / "agents/skills/solercia-connect/SKILL.md").read_text()
    frontmatter = skill.split("---", 2)[1]
    descriptions = [line for line in frontmatter.splitlines() if line.startswith("description:")]

    assert len(descriptions) == 1
    assert len(descriptions[0]) <= 265
    assert re.search(r"Trigger:.*Outlook.*GLPI", descriptions[0])
