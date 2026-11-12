"""Doctor reports project mode, and the library entry point honors it through the environment."""

from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import solercia_connect.bootstrap as bootstrap_module
import solercia_connect.cli as cli
import solercia_connect.connections as connections_module
from solercia_connect.application import run_application
from solercia_connect.config import ProfileStore, load_settings
from solercia_connect.doctor import DoctorService
from solercia_connect.project_home import prepare_project_home
from tests.fakes import FakeTransport, json_response

ACCOUNT = "user@example.com"
SITE = "https://example.atlassian.net"
TOKEN = "jira-api-token-for-the-library"
BASIC = "Basic " + base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()

runner = CliRunner()


def _no_git(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    raise AssertionError(f"git must not run without a git ancestor: {list(args)}")


def _doctor(home: Path, **runner_kwargs: Any) -> DoctorService:
    settings = load_settings(
        environ={
            "SOLERCIA_CONNECT_HOME": str(home),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
        }
    )
    return DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=lambda name: True,
        keyring_probe=lambda: (False, "no keyring in this harness"),
        browser_probe=lambda: (False, "not installed"),
        **runner_kwargs,
    )


def _check(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(check for check in report["checks"] if check["name"] == name)


def test_doctor_without_project_home_reports_nothing_about_it(tmp_path: Path) -> None:
    settings = load_settings(environ={"XDG_CONFIG_HOME": str(tmp_path / "config")})
    service = DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=lambda name: True,
        keyring_probe=lambda: (True, "test-backend"),
        browser_probe=lambda: (False, "not installed"),
        git_runner=_no_git,
    )

    report = service.inspect("gmail", ACCOUNT).to_dict()

    assert "project_mode" not in report
    assert all(check["name"] != "project_mode" for check in report["checks"])


def test_doctor_reports_a_fresh_project_home_as_ready_without_creating_it(tmp_path: Path) -> None:
    home = tmp_path / "project" / ".solercia-connect"

    report = _doctor(home, git_runner=_no_git).inspect("gmail", ACCOUNT).to_dict()

    assert report["project_mode"] == {
        "home": str(home),
        "layout": {
            "home": "absent",
            "config": "absent",
            "data": "absent",
            "secrets": "absent",
            "connect.env": "absent",
        },
        "gitignore": "absent",
        "git_guard": "not_applicable",
        "tracked_paths": [],
        "ready": True,
    }
    assert _check(report, "project_mode")["status"] == "ready"
    assert _check(report, "keyring_backend")["status"] == "not_required"
    assert report["ready"] is True
    assert not home.exists()


def test_doctor_reports_a_prepared_home_with_its_private_layout(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = project / ".solercia-connect"
    prepare_project_home(home)
    (home / "connect.env").write_text("PANDORA_JIRA_TOKEN=never-shown\n", encoding="utf-8")
    (home / "connect.env").chmod(0o600)

    report = _doctor(home).inspect("gmail", ACCOUNT).to_dict()

    assert report["project_mode"]["layout"] == {
        "home": "private",
        "config": "private",
        "data": "private",
        "secrets": "private",
        "connect.env": "private",
    }
    assert report["project_mode"]["gitignore"] == "ignores_all"
    assert "never-shown" not in json.dumps(report)


def test_doctor_flags_an_unsafe_folder_and_never_repairs_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = project / ".solercia-connect"
    prepare_project_home(home)
    (home / "secrets").chmod(0o755)
    (home / ".gitignore").chmod(0o600)
    (home / ".gitignore").write_text("secrets/\n", encoding="utf-8")

    report = _doctor(home).inspect("gmail", ACCOUNT).to_dict()

    assert report["project_mode"]["layout"]["secrets"] == "unsafe"
    assert report["project_mode"]["gitignore"] == "conflict"
    assert report["project_mode"]["ready"] is False
    check = _check(report, "project_mode")
    assert check["status"] == "unsafe" and check["required"] is True
    assert any("chmod 700" in step for step in check["remediation"])
    assert report["ready"] is False
    assert (home / "secrets").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize(
    ("returncode", "stdout", "guard", "paths"),
    [
        (0, b"", "clean", []),
        (0, b".solercia-connect/config/x.json\0", "tracked", [".solercia-connect/config/x.json"]),
        (128, b"", "unavailable", []),
    ],
)
def test_doctor_reports_the_git_guard_result(
    tmp_path: Path, returncode: int, stdout: bytes, guard: str, paths: list[str]
) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(list(args), returncode, stdout, b"")

    report = _doctor(project / ".solercia-connect", git_runner=runner).inspect("gmail").to_dict()

    assert report["project_mode"]["git_guard"] == guard
    assert report["project_mode"]["tracked_paths"] == paths
    assert report["project_mode"]["ready"] is (guard == "clean")


def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    home = project / ".solercia-connect"
    prepare_project_home(home)
    (home / "connect.env").write_text(f"PANDORA_JIRA_TOKEN={TOKEN}\n", encoding="utf-8")
    (home / "connect.env").chmod(0o600)
    monkeypatch.delenv("SOLERCIA_CONNECT_ENV_FILE", raising=False)
    monkeypatch.delenv("PANDORA_JIRA_TOKEN", raising=False)
    monkeypatch.setenv("SOLERCIA_CONNECT_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    return home


def test_cli_doctor_reports_project_mode_without_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _environment(tmp_path, monkeypatch)

    result = runner.invoke(cli.app, ["doctor", "jira"])

    payload = json.loads(result.stdout)
    assert payload["data"]["project_mode"]["home"] == str(home)
    assert TOKEN not in result.output


def test_the_library_entry_point_honors_project_mode_through_the_environment_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _environment(tmp_path, monkeypatch)
    myself = json_response(200, {"accountId": "acct-1", "emailAddress": ACCOUNT})
    connect_transport = FakeTransport(myself)
    monkeypatch.setattr(connections_module, "HttpxTransport", lambda: connect_transport)
    connected = runner.invoke(
        cli.app,
        ["connect", "jira", ACCOUNT, "--site", SITE, "--token-env", "PANDORA_JIRA_TOKEN"],
    )
    assert json.loads(connected.stdout)["status"] == "connected_official"
    run_transport = FakeTransport(
        json_response(200, {"accountId": "acct-1"}),
        json_response(200, {"issues": [], "isLast": True}),
    )
    monkeypatch.setattr(bootstrap_module, "HttpxTransport", lambda: run_transport)

    result = run_application(
        {
            "type": "jira",
            "account": ACCOUNT,
            "capability": "external.issues.search",
            "start": "2026-08-01T00:00:00+00:00",
            "end": "2026-09-01T00:00:00+00:00",
        }
    )

    assert result.ok is True, result.to_dict()
    assert result.data == {"records": []}
    assert all(
        request.headers is not None and request.headers["Authorization"] == BASIC
        for request in run_transport.requests
    )
    written = sorted(path for path in tmp_path.rglob("*") if path.is_file())
    assert all(path.is_relative_to(home) for path in written)
    assert TOKEN not in json.dumps(result.to_dict())
