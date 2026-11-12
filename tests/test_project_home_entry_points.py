"""Project mode guards every entry point, not only connect: run, status and the HTTP service."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from solercia_connect.application import run_application
from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.config import ConnectionType, ProfileStore, ProjectHomeError, load_settings
from solercia_connect.connections import ConnectionService
from solercia_connect.oauth import OAuthTokenSet
from solercia_connect.project_home import GITIGNORE_CONTENT, prepare_project_home
from solercia_connect.service import create_app
from tests.fakes import NOW, FakeTransport

ACCOUNT = "user@example.com"


def _git(cwd: Path, *args: str) -> None:
    clean = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=clean)


def _project(tmp_path: Path) -> tuple[Path, Path, Any]:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init", "-q")
    home = project / ".solercia-connect"
    settings = load_settings(
        environ={
            "SOLERCIA_CONNECT_HOME": str(home),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
        }
    )
    return project, home, settings


def _track_a_file_inside(project: Path, home: Path) -> None:
    prepare_project_home(home)
    (home / "config" / "note.txt").write_text("not secret", encoding="utf-8")
    _git(project, "add", "-f", ".solercia-connect/config/note.txt")


def _gmail_run() -> dict[str, Any]:
    return {
        "type": "gmail",
        "account": ACCOUNT,
        "capability": "mail.sent.list",
        "start": "2026-08-01T00:00:00+00:00",
        "end": "2026-09-01T00:00:00+00:00",
        "limit": 50,
    }


class MemoryTokenStore:
    def load(self, key: str) -> OAuthTokenSet | None:
        return None

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        raise AssertionError(f"no token may be written for {key}")

    def delete(self, key: str) -> None:
        return None


def _connections(settings: Any) -> ConnectionService:
    return ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        transport=FakeTransport(),
        clock=lambda: NOW,
    )


def test_run_prepares_the_project_home_before_building_any_route(tmp_path: Path) -> None:
    _, home, settings = _project(tmp_path)

    run_application(_gmail_run(), interactive=True, settings=settings)

    assert (home / ".gitignore").read_text(encoding="utf-8") == GITIGNORE_CONTENT


def test_run_fails_closed_when_git_tracks_the_project_home(tmp_path: Path) -> None:
    project, home, settings = _project(tmp_path)
    _track_a_file_inside(project, home)

    result = run_application(_gmail_run(), interactive=True, settings=settings)

    assert result.ok is False
    assert result.error is not None
    assert ".solercia-connect/config/note.txt" in result.error.message
    assert str(tmp_path) not in result.error.message


def test_building_a_registry_prepares_the_project_home_even_without_token_reads(
    tmp_path: Path,
) -> None:
    _, home, settings = _project(tmp_path)

    registry_from_environment(connection_type=ConnectionType.GLPI, settings_override=settings)

    assert (home / ".gitignore").read_text(encoding="utf-8") == GITIGNORE_CONTENT


def test_building_a_registry_fails_closed_when_git_tracks_the_project_home(
    tmp_path: Path,
) -> None:
    project, home, settings = _project(tmp_path)
    _track_a_file_inside(project, home)

    with pytest.raises(ProjectHomeError):
        registry_from_environment(connection_type=ConnectionType.GLPI, settings_override=settings)


def test_status_prepares_the_project_home(tmp_path: Path) -> None:
    _, home, settings = _project(tmp_path)

    result = _connections(settings).status("gmail", ACCOUNT)

    assert result.status == "action_required"
    assert (home / ".gitignore").read_text(encoding="utf-8") == GITIGNORE_CONTENT


def test_status_fails_closed_when_git_tracks_the_project_home(tmp_path: Path) -> None:
    project, home, settings = _project(tmp_path)
    _track_a_file_inside(project, home)

    result = _connections(settings).status("gmail", ACCOUNT)

    assert result.ok is False
    assert result.error is not None
    assert ".solercia-connect/config/note.txt" in result.error.message


def test_service_startup_fails_closed_when_git_tracks_the_project_home(tmp_path: Path) -> None:
    project, home, settings = _project(tmp_path)
    _track_a_file_inside(project, home)

    app = create_app(settings=settings)

    assert isinstance(app.state.startup_error, ProjectHomeError)


def test_service_startup_prepares_the_project_home(tmp_path: Path) -> None:
    _, home, settings = _project(tmp_path)

    app = create_app(settings=settings)

    assert app.state.startup_error is None
    assert (home / ".gitignore").read_text(encoding="utf-8") == GITIGNORE_CONTENT
