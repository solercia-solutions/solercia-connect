"""Opt-in discovery of the project home at the git root (D3.1) and ``project init``."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import solercia_connect.cli as cli
from solercia_connect.config import (
    ProfileStore,
    ProjectHomeError,
    effective_environment,
    load_settings,
)
from solercia_connect.doctor import DoctorService
from solercia_connect.project_home import ProjectHomeTrackedError, prepare_project_home

DISCOVERY = "SOLERCIA_CONNECT_PROJECT_DISCOVERY"
CLIENT = "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID"
FOLDER = ".solercia-connect"
ACCOUNT = "user@example.com"
TEMPLATE_NAMES = {
    "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID",
    "SOLERCIA_CONNECT_GOOGLE_CLIENT_SECRET",
    "JIRA_API_TOKEN",
    "BITBUCKET_API_TOKEN",
    "BITBUCKET_ADMIN_API_TOKEN",
    "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID",
    "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET",
    "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID",
    "SOLERCIA_CONNECT_MICROSOFT_TENANT",
}

runner = CliRunner()


def _git(cwd: Path, *args: str) -> None:
    """Run git in a throwaway repository, isolated from the developer's git configuration."""

    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    identity = ("-c", "user.name=Test", "-c", "user.email=test@example.com")
    subprocess.run(
        ["git", *identity, "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=environment,
    )


def _repository(tmp_path: Path, name: str = "repo") -> Path:
    project = tmp_path / name
    project.mkdir()
    _git(project, "init", "-q")
    return project


def _home(root: Path, client_id: str | None = None) -> Path:
    home = root / FOLDER
    prepare_project_home(home)
    if client_id is not None:
        env_file = home / "connect.env"
        env_file.write_text(f"{CLIENT}={client_id}\n", encoding="utf-8")
        env_file.chmod(0o600)
    return home


def _environ(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        **extra,
    }


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _without_git_ancestor(directory: Path) -> Path:
    assert not any((parent / ".git").exists() for parent in (directory, *directory.parents))
    return directory


# --- Resolution order -------------------------------------------------------------------------


def test_without_the_switch_a_folder_at_the_git_root_is_never_read(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _home(repo, "client-from-the-project")

    settings = load_settings(environ=_environ(tmp_path), cwd=repo)

    assert settings.project_home is None
    assert settings.project_home_source is None
    assert settings.google_client_id is None
    assert settings.config_dir == tmp_path / "xdg-config" / "solercia-connect"
    assert CLIENT not in effective_environment(environ=_environ(tmp_path), cwd=repo)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_discovery_uses_the_folder_at_the_git_root_from_any_subfolder(
    tmp_path: Path, value: str
) -> None:
    repo = _repository(tmp_path)
    home = _home(repo, "client-from-the-project")
    deep = repo / "src" / "deep"
    deep.mkdir(parents=True)
    environ = _environ(tmp_path, **{DISCOVERY: value})

    settings = load_settings(environ=environ, cwd=deep)

    assert settings.project_home == home
    assert settings.project_home_source == "discovered"
    assert settings.google_client_id == "client-from-the-project"
    assert settings.config_dir == home / "config"
    assert settings.data_dir == home / "data"
    assert effective_environment(environ=environ, cwd=deep)[CLIENT] == "client-from-the-project"


def test_an_explicit_home_always_wins_over_discovery(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _home(repo, "client-from-the-git-root")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    explicit = _home(elsewhere, "client-from-the-explicit-home")
    environ = _environ(tmp_path, **{DISCOVERY: "1", "SOLERCIA_CONNECT_HOME": str(explicit)})

    settings = load_settings(environ=environ, cwd=repo)

    assert settings.project_home == explicit
    assert settings.project_home_source == "explicit"
    assert settings.google_client_id == "client-from-the-explicit-home"


def test_a_worktree_root_marked_by_a_git_file_counts_as_the_git_root(tmp_path: Path) -> None:
    main = _repository(tmp_path, "main")
    _git(main, "commit", "-q", "--allow-empty", "-m", "start")
    worktree = tmp_path / "worktree"
    _git(main, "worktree", "add", "-q", str(worktree))
    assert (worktree / ".git").is_file()
    home = _home(worktree, "client-from-the-worktree")
    (worktree / "pkg").mkdir()

    settings = load_settings(environ=_environ(tmp_path, **{DISCOVERY: "1"}), cwd=worktree / "pkg")

    assert settings.project_home == home
    assert settings.google_client_id == "client-from-the-worktree"


def test_a_folder_below_the_git_root_is_ignored_never_taken_from_the_bare_cwd(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    nested = repo / "nested"
    nested.mkdir()
    _home(nested, "client-from-a-subfolder")

    settings = load_settings(environ=_environ(tmp_path, **{DISCOVERY: "1"}), cwd=nested)

    assert settings.project_home is None
    assert settings.project_home_source is None
    assert settings.google_client_id is None


def test_without_a_git_root_discovery_is_global_mode_even_with_a_folder_in_the_cwd(
    tmp_path: Path,
) -> None:
    plain = _without_git_ancestor(tmp_path / "plain")
    plain.mkdir()
    _home(plain, "client-from-a-plain-folder")

    settings = load_settings(environ=_environ(tmp_path, **{DISCOVERY: "1"}), cwd=plain)

    assert settings.project_home is None
    assert settings.google_client_id is None
    assert settings.config_dir == tmp_path / "xdg-config" / "solercia-connect"


def test_a_git_root_without_the_folder_is_global_mode(tmp_path: Path) -> None:
    repo = _repository(tmp_path)

    settings = load_settings(environ=_environ(tmp_path, **{DISCOVERY: "1"}), cwd=repo)

    assert settings.project_home is None
    assert settings.project_home_source is None


def test_a_symlinked_or_non_directory_folder_at_the_git_root_fails_closed(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    real = _home(target, "client-behind-a-link")
    (repo / FOLDER).symlink_to(real)
    environ = _environ(tmp_path, **{DISCOVERY: "1"})

    with pytest.raises(ProjectHomeError, match="symbolic link") as linked:
        load_settings(environ=environ, cwd=repo)
    assert "client-behind-a-link" not in str(linked.value)

    (repo / FOLDER).unlink()
    (repo / FOLDER).write_text(f"{CLIENT}=client-in-a-file\n", encoding="utf-8")
    with pytest.raises(ProjectHomeError, match="not a directory"):
        load_settings(environ=environ, cwd=repo)


def test_a_public_discovered_folder_fails_closed_and_is_never_repaired(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    home = _home(repo, "client-in-a-public-folder")
    home.chmod(0o755)

    with pytest.raises(ProjectHomeError, match="not private"):
        load_settings(environ=_environ(tmp_path, **{DISCOVERY: "1"}), cwd=repo)

    assert _mode(home) == 0o755


def test_the_switch_can_come_from_the_explicit_env_file(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    home = _home(repo, "client-from-the-project")
    env_file = tmp_path / "connect.env"
    env_file.write_text(f"{DISCOVERY}=true\n", encoding="utf-8")

    settings = load_settings(
        environ=_environ(tmp_path, SOLERCIA_CONNECT_ENV_FILE=str(env_file)), cwd=repo
    )

    assert settings.project_home == home
    assert settings.project_home_source == "discovered"


def test_connect_env_can_never_turn_discovery_on(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    home = _home(elsewhere)
    (home / "connect.env").write_text(f"{DISCOVERY}=1\n", encoding="utf-8")
    (home / "connect.env").chmod(0o600)

    with pytest.raises(ProjectHomeError, match=DISCOVERY):
        load_settings(environ=_environ(tmp_path, SOLERCIA_CONNECT_HOME=str(home)))


def test_the_switch_must_be_a_boolean(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=DISCOVERY):
        load_settings(environ=_environ(tmp_path, **{DISCOVERY: "sometimes"}), cwd=tmp_path)


# --- A folder shipped inside a cloned repository ----------------------------------------------

SHIPPED_CLIENT = "client-id-shipped-in-a-cloned-repository"


def _clone_with_a_committed_folder(tmp_path: Path) -> Path:
    upstream = _repository(tmp_path, "upstream")
    shipped = upstream / FOLDER
    shipped.mkdir()
    (shipped / ".gitignore").write_text("*\n", encoding="utf-8")
    (shipped / "connect.env").write_text(f"{CLIENT}={SHIPPED_CLIENT}\n", encoding="utf-8")
    _git(upstream, "add", "-f", FOLDER)
    _git(upstream, "commit", "-q", "-m", "ship a project folder")
    _git(tmp_path, "clone", "-q", str(upstream), "clone")
    clone = tmp_path / "clone"
    assert (clone / FOLDER / "connect.env").is_file()
    return clone


@pytest.mark.parametrize("looks_private", [True, False])
def test_a_folder_committed_to_a_cloned_repository_is_never_used(
    tmp_path: Path, looks_private: bool
) -> None:
    clone = _clone_with_a_committed_folder(tmp_path)
    if looks_private:
        # The worst case: permissions alone would let it through, so only the git guard stops it.
        (clone / FOLDER).chmod(0o700)
        (clone / FOLDER / "connect.env").chmod(0o600)
    environ = _environ(tmp_path, **{DISCOVERY: "1"})

    with pytest.raises(ProjectHomeTrackedError) as captured:
        load_settings(environ=environ, cwd=clone)
    with pytest.raises(ProjectHomeTrackedError):
        effective_environment(environ=environ, cwd=clone)

    assert f"{FOLDER}/connect.env" in captured.value.tracked_paths
    assert SHIPPED_CLIENT not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)


def test_the_cli_reports_a_committed_folder_as_a_typed_failure_without_its_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _clone_with_a_committed_folder(tmp_path)
    (clone / FOLDER).chmod(0o700)
    (clone / FOLDER / "connect.env").chmod(0o600)
    for name in ("SOLERCIA_CONNECT_HOME", "SOLERCIA_CONNECT_ENV_FILE", CLIENT):
        monkeypatch.delenv(name, raising=False)
    for name, value in _environ(tmp_path, **{DISCOVERY: "1"}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(clone)

    for command in (["config", "validate"], ["doctor", "gmail", ACCOUNT]):
        result = runner.invoke(cli.app, command)

        payload = json.loads(result.stdout)
        assert result.exit_code == 1, command
        assert payload["ok"] is False
        assert payload["error"]["code"] == "configuration_invalid"
        assert "Git tracks files" in payload["error"]["message"]
        assert payload["data"] == {}
        assert SHIPPED_CLIENT not in result.output


# --- Entry points and doctor ------------------------------------------------------------------


def test_a_discovered_home_is_prepared_by_the_entry_points_like_an_explicit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    home = repo / FOLDER
    home.mkdir(mode=0o700)
    for name in ("SOLERCIA_CONNECT_HOME", "SOLERCIA_CONNECT_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in _environ(tmp_path, **{DISCOVERY: "1"}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(repo)

    result = runner.invoke(cli.app, ["providers"])

    assert result.exit_code == 0, result.output
    assert (home / ".gitignore").read_text(encoding="utf-8") == "*\n"
    for folder in ("config", "data", "secrets"):
        assert _mode(home / folder) == 0o700


def _doctor_report(settings: Any) -> dict[str, Any]:
    service = DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=lambda name: True,
        keyring_probe=lambda: (True, "test-backend"),
        browser_probe=lambda: (False, "not installed"),
    )
    report: dict[str, Any] = service.inspect("gmail", ACCOUNT).to_dict()
    return report


def _project_check(report: dict[str, Any]) -> dict[str, Any]:
    return next(check for check in report["checks"] if check["name"] == "project_mode")


def test_doctor_reports_how_the_project_home_was_resolved(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    home = _home(repo)
    base = _environ(tmp_path, SOLERCIA_CONNECT_GOOGLE_CLIENT_ID="google-client")

    discovered = _doctor_report(load_settings(environ={**base, DISCOVERY: "1"}, cwd=repo))
    explicit = _doctor_report(
        load_settings(environ={**base, "SOLERCIA_CONNECT_HOME": str(home)}, cwd=repo)
    )
    global_mode = _doctor_report(load_settings(environ=base, cwd=repo))

    assert discovered["project_home_source"] == "discovered"
    assert discovered["project_mode"]["home"] == str(home)
    assert "discovered at the git root" in _project_check(discovered)["detail"]
    assert explicit["project_home_source"] == "explicit"
    assert _project_check(explicit)["detail"].startswith("SOLERCIA_CONNECT_HOME")
    assert "project_home_source" not in global_mode
    assert "project_mode" not in global_mode


# --- project init -----------------------------------------------------------------------------


def _clean_cli_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SOLERCIA_CONNECT_HOME", "SOLERCIA_CONNECT_ENV_FILE", DISCOVERY):
        monkeypatch.delenv(name, raising=False)
    for name, value in _environ(tmp_path).items():
        monkeypatch.setenv(name, value)


def _init(*arguments: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(cli.app, ["project", "init", *arguments])
    payload: dict[str, Any] = json.loads(result.stdout)
    return result.exit_code, payload


def test_project_init_creates_the_home_at_the_git_root_with_a_value_free_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    (repo / "src").mkdir()
    _clean_cli_environment(tmp_path, monkeypatch)
    monkeypatch.chdir(repo / "src")

    code, payload = _init()

    home = repo / FOLDER
    assert code == 0
    assert payload["ok"] is True
    assert payload["operation"] == "project.init"
    assert payload["status"] == "initialized"
    data = payload["data"]
    assert data["home"] == str(home)
    assert data["created"] == ["home", "config", "data", "secrets", ".gitignore", "connect.env"]
    assert data["discoverable"] is True
    assert data["discovery_enabled"] is False
    assert data["next_steps"][0] == f"export {DISCOVERY}=1"
    assert "solercia-connect connect gmail <account>" in data["next_steps"]
    assert any("--token-env JIRA_API_TOKEN" in step for step in data["next_steps"])
    assert any("--token-env BITBUCKET_API_TOKEN" in step for step in data["next_steps"])
    for folder in (home, home / "config", home / "data", home / "secrets"):
        assert _mode(folder) == 0o700
    assert (home / ".gitignore").read_text(encoding="utf-8") == "*\n"
    env_file = home / "connect.env"
    assert _mode(env_file) == 0o600
    lines = [line for line in env_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all(line.startswith("#") for line in lines)
    names = {match.group(1) for line in lines if (match := re.fullmatch(r"# ([A-Z0-9_]+)=", line))}
    assert names == TEMPLATE_NAMES
    loaded = effective_environment(environ={"SOLERCIA_CONNECT_HOME": str(home)})
    assert not TEMPLATE_NAMES & set(loaded)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


def test_project_init_is_idempotent_and_never_overwrites_connect_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    _clean_cli_environment(tmp_path, monkeypatch)
    monkeypatch.chdir(repo)
    assert _init()[0] == 0
    env_file = repo / FOLDER / "connect.env"
    env_file.write_text(f"{CLIENT}=filled-by-the-user\n", encoding="utf-8")

    code, payload = _init()

    assert code == 0
    assert payload["status"] == "ready"
    assert payload["data"]["created"] == []
    assert env_file.read_text(encoding="utf-8") == f"{CLIENT}=filled-by-the-user\n"
    assert "filled-by-the-user" not in json.dumps(payload)


def test_project_init_with_discovery_active_needs_no_export_and_is_then_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    _clean_cli_environment(tmp_path, monkeypatch)
    monkeypatch.setenv(DISCOVERY, "1")
    monkeypatch.chdir(repo)

    code, payload = _init()

    assert code == 0
    assert payload["data"]["discovery_enabled"] is True
    assert not any(step.startswith("export ") for step in payload["data"]["next_steps"])
    settings = load_settings()
    assert settings.project_home == repo / FOLDER
    assert settings.project_home_source == "discovered"


def test_project_init_warns_when_an_explicit_home_takes_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    _clean_cli_environment(tmp_path, monkeypatch)
    other = tmp_path / "other" / FOLDER
    monkeypatch.setenv(DISCOVERY, "1")
    monkeypatch.setenv("SOLERCIA_CONNECT_HOME", str(other))
    monkeypatch.chdir(repo)

    code, payload = _init()

    assert code == 0
    assert payload["data"]["next_steps"][0] == "unset SOLERCIA_CONNECT_HOME"
    assert not other.exists()


def test_project_init_outside_a_git_repository_requires_an_absolute_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = _without_git_ancestor(tmp_path / "plain")
    plain.mkdir()
    _clean_cli_environment(tmp_path, monkeypatch)
    monkeypatch.chdir(plain)

    for arguments in ((), ("--path", "relative/dir"), ("--path", str(tmp_path / "missing"))):
        code, payload = _init(*arguments)
        assert code == 2, arguments
        assert payload["status"] == "invalid_input"
        assert "--path" in payload["error"]["message"]
    assert not (plain / FOLDER).exists()

    code, payload = _init("--path", str(plain))

    home = plain / FOLDER
    assert code == 0
    assert payload["data"]["home"] == str(home)
    assert payload["data"]["discoverable"] is False
    assert payload["data"]["next_steps"][0] == f'export SOLERCIA_CONNECT_HOME="{home}"'
    assert _mode(home / "connect.env") == 0o600


def test_project_init_refuses_a_tracked_folder_and_writes_no_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _clone_with_a_committed_folder(tmp_path)
    (clone / FOLDER / "connect.env").unlink()
    _clean_cli_environment(tmp_path, monkeypatch)
    monkeypatch.chdir(clone)

    code, payload = _init()

    assert code == 1
    assert payload["error"]["code"] == "configuration_invalid"
    assert "Git tracks files" in payload["error"]["message"]
    assert not (clone / FOLDER / "connect.env").exists()
    assert not (clone / FOLDER / "secrets").exists()


def test_project_init_fails_closed_on_an_unsafe_existing_connect_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    home = _home(repo, "client-in-a-readable-file")
    (home / "connect.env").chmod(0o644)
    _clean_cli_environment(tmp_path, monkeypatch)
    monkeypatch.chdir(repo)

    code, payload = _init()

    assert code == 1
    assert "chmod 600" in payload["error"]["message"]
    assert "client-in-a-readable-file" not in json.dumps(payload)
    assert _mode(home / "connect.env") == 0o644
