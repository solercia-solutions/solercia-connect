"""Project mode: SOLERCIA_CONNECT_HOME keeps Connect's local state in one private project folder."""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from solercia_connect.browser_profile import browser_profile_root
from solercia_connect.config import (
    ProfileStore,
    ProjectHomeError,
    ProjectLayout,
    load_settings,
)
from solercia_connect.connections import ConnectionService
from solercia_connect.domain import ProviderFamily
from solercia_connect.oauth import OAuthTokenSet
from solercia_connect.project_home import (
    GITIGNORE_CONTENT,
    ProjectHomeTrackedError,
    prepare_project_home,
    write_private_file,
)
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport

ACCOUNT = "user@example.com"
SITE = "https://example.atlassian.net"
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[bytes]]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _git(cwd: Path, *args: str) -> None:
    clean = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=clean)


def _repository(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init", "-q")
    return project


def _recording_runner(calls: list[list[str]], returncode: int = 0, stdout: bytes = b"") -> Runner:
    def run(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), returncode, stdout, b"")

    return run


def _unexpected_git(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    raise AssertionError(f"git must not run here: {list(args)}")


class MemoryTokenStore:
    def __init__(self) -> None:
        self.values: dict[str, OAuthTokenSet] = {}

    def load(self, key: str) -> OAuthTokenSet | None:
        return self.values.get(key)

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        self.values[key] = tokens

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class UntouchedSecretStore:
    def load(self, key: str) -> SecretValue | None:
        raise AssertionError(f"secret store must not be read for {key}")

    def save(self, key: str, secret: SecretValue) -> None:
        raise AssertionError(f"secret store must not be written for {key}")

    def delete(self, key: str) -> bool:
        raise AssertionError(f"secret store must not be deleted for {key}")


class FakeGoogleOAuth:
    def authorization_header(self) -> str:
        return "Bearer access"


def _service(settings: Any, *, git_runner: Runner | None = None) -> ConnectionService:
    return ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        oauth_builder=lambda type, account: FakeGoogleOAuth(),
        identity_verifier=lambda type, account, auth: account,
        google_callback_flow=lambda oauth, open_browser: None,
        browser_connector=lambda type, account: account,
        secret_store=UntouchedSecretStore(),
        transport=FakeTransport(),
        git_runner=git_runner,
        clock=lambda: NOW,
    )


def test_project_home_holds_config_data_secrets_and_env_in_one_folder(tmp_path: Path) -> None:
    home = tmp_path / "project" / ".solercia-connect"

    settings = load_settings(
        environ={
            "SOLERCIA_CONNECT_HOME": str(home),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        }
    )
    layout = ProjectLayout(home)

    assert settings.project_home == home
    assert settings.config_dir == layout.config_dir == home / "config"
    assert settings.data_dir == layout.data_dir == home / "data"
    assert layout.secrets_dir == home / "secrets"
    assert layout.env_file == home / "connect.env"
    assert layout.gitignore == home / ".gitignore"
    assert ProfileStore.from_settings(settings).root == home / "config" / "profiles"
    assert browser_profile_root(settings, ProviderFamily.MICROSOFT) == home / "data" / "m365"
    assert browser_profile_root(settings, ProviderFamily.GOOGLE) == home / "data" / "google"
    assert browser_profile_root(settings, ProviderFamily.JIRA) == home / "data" / "jira"


def test_explicit_profile_dir_overrides_still_win_in_project_mode(tmp_path: Path) -> None:
    settings = load_settings(
        environ={
            "SOLERCIA_CONNECT_HOME": str(tmp_path / ".solercia-connect"),
            "SOLERCIA_CONNECT_JIRA_PROFILE_DIR": str(tmp_path / "jira-profiles"),
        }
    )

    assert browser_profile_root(settings, ProviderFamily.JIRA) == tmp_path / "jira-profiles"


def test_project_home_expands_the_user_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = load_settings(environ={"SOLERCIA_CONNECT_HOME": "~/project/.solercia-connect"})

    assert settings.project_home == tmp_path / "project" / ".solercia-connect"


@pytest.mark.parametrize("value", ["relative/.solercia-connect", ".solercia-connect", "./x"])
def test_a_relative_project_home_is_rejected_so_it_never_depends_on_the_cwd(value: str) -> None:
    with pytest.raises(ProjectHomeError, match="absolute path"):
        load_settings(environ={"SOLERCIA_CONNECT_HOME": value})


def test_a_project_home_below_a_symbolic_link_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ProjectHomeError, match="symbolic link"):
        load_settings(environ={"SOLERCIA_CONNECT_HOME": str(link / ".solercia-connect")})


@pytest.mark.parametrize(
    "extra", [{}, {"SOLERCIA_CONNECT_HOME": ""}, {"SOLERCIA_CONNECT_HOME": " "}]
)
def test_without_project_home_paths_stay_exactly_as_before(
    tmp_path: Path, extra: dict[str, str]
) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            **extra,
        }
    )

    assert settings.project_home is None
    assert settings.config_dir == tmp_path / "xdg-config" / "solercia-connect"
    assert settings.data_dir == tmp_path / "xdg-data" / "solercia-connect"


def test_prepare_creates_private_folders_and_a_gitignore_that_ignores_everything(
    tmp_path: Path,
) -> None:
    home = tmp_path / "project" / ".solercia-connect"
    home.parent.mkdir()

    layout = prepare_project_home(home, runner=_unexpected_git)

    for directory in (home, layout.config_dir, layout.data_dir, layout.secrets_dir):
        assert directory.is_dir()
        assert _mode(directory) == 0o700
    assert layout.gitignore.read_text(encoding="utf-8") == GITIGNORE_CONTENT == "*\n"
    assert _mode(layout.gitignore) == 0o600
    assert sorted(path.name for path in home.iterdir()) == [
        ".gitignore",
        "config",
        "data",
        "secrets",
    ]

    prepare_project_home(home, runner=_unexpected_git)

    assert layout.gitignore.read_text(encoding="utf-8") == "*\n"


def test_prepare_never_overwrites_a_gitignore_with_other_content(tmp_path: Path) -> None:
    home = tmp_path / ".solercia-connect"
    home.mkdir(mode=0o700)
    (home / ".gitignore").write_text("secrets/\n", encoding="utf-8")

    with pytest.raises(ProjectHomeError, match=r"\.gitignore must contain exactly"):
        prepare_project_home(home, runner=_unexpected_git)

    assert (home / ".gitignore").read_text(encoding="utf-8") == "secrets/\n"


def test_prepare_rejects_a_gitignore_that_is_a_symbolic_link(tmp_path: Path) -> None:
    home = tmp_path / ".solercia-connect"
    home.mkdir(mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_text("*\n", encoding="utf-8")
    (home / ".gitignore").symlink_to(target)

    with pytest.raises(ProjectHomeError, match="regular file"):
        prepare_project_home(home, runner=_unexpected_git)


def test_prepare_fails_closed_on_a_public_folder_and_never_repairs_it(tmp_path: Path) -> None:
    home = tmp_path / ".solercia-connect"
    home.mkdir()
    home.chmod(0o755)

    with pytest.raises(ProjectHomeError, match="chmod 700"):
        prepare_project_home(home, runner=_unexpected_git)

    assert _mode(home) == 0o755


def test_prepare_fails_closed_on_a_subfolder_that_is_a_symbolic_link(tmp_path: Path) -> None:
    home = tmp_path / ".solercia-connect"
    home.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (home / "secrets").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectHomeError, match="symbolic link"):
        prepare_project_home(home, runner=_unexpected_git)


def test_prepare_fails_closed_on_a_folder_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".solercia-connect"
    home.mkdir(mode=0o700)
    owner = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: owner + 1)

    with pytest.raises(ProjectHomeError, match="owned by the current user"):
        prepare_project_home(home, runner=_unexpected_git)


def test_prepare_needs_the_project_folder_to_exist(tmp_path: Path) -> None:
    with pytest.raises(ProjectHomeError, match="could not be created"):
        prepare_project_home(tmp_path / "missing" / ".solercia-connect", runner=_unexpected_git)


def test_prepare_rejects_a_home_that_is_a_file(tmp_path: Path) -> None:
    home = tmp_path / ".solercia-connect"
    home.write_text("not a folder", encoding="utf-8")

    with pytest.raises(ProjectHomeError, match="is not a directory"):
        prepare_project_home(home, runner=_unexpected_git)


def test_private_writes_are_atomic_0600_and_create_only_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "file"

    assert write_private_file(target, b"first", replace=False) is True
    assert write_private_file(target, b"second", replace=False) is False
    assert target.read_bytes() == b"first"
    assert write_private_file(target, b"third", replace=True) is True
    assert target.read_bytes() == b"third"
    assert _mode(target) == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == ["file"]


def test_git_guard_passes_while_the_home_is_ignored_and_fails_once_a_path_is_tracked(
    tmp_path: Path,
) -> None:
    project = _repository(tmp_path)
    home = project / ".solercia-connect"

    prepare_project_home(home)
    (home / "config" / "note.txt").write_text("not secret", encoding="utf-8")
    prepare_project_home(home)
    _git(project, "add", "-f", ".solercia-connect/config/note.txt")

    with pytest.raises(ProjectHomeTrackedError) as captured:
        prepare_project_home(home)

    assert captured.value.tracked_paths == (".solercia-connect/config/note.txt",)
    assert ".solercia-connect/config/note.txt" in str(captured.value)
    assert str(tmp_path) not in str(captured.value)
    assert isinstance(captured.value, ProjectHomeError)


def test_git_guard_runs_a_bounded_literal_ls_files_for_a_worktree_git_file(
    tmp_path: Path,
) -> None:
    project = tmp_path / "worktree"
    project.mkdir()
    (project / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
    home = project / ".solercia-connect"
    calls: list[list[str]] = []

    prepare_project_home(home, runner=_recording_runner(calls))

    assert calls == [
        [
            "git",
            "--literal-pathspecs",
            "-C",
            str(project),
            "ls-files",
            "-z",
            "--",
            str(home),
        ]
    ]


def test_git_guard_is_skipped_without_a_git_ancestor(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    prepare_project_home(tmp_path / ".solercia-connect", runner=_recording_runner(calls))

    assert calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        FileNotFoundError("git"),
        PermissionError("git"),
        subprocess.TimeoutExpired("git", 10),
        subprocess.CompletedProcess(["git"], 128, b"", b"fatal: not a git repository"),
    ],
)
def test_git_guard_fails_closed_when_git_is_unavailable_or_errors(
    tmp_path: Path, outcome: Any
) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[no-any-return]

    with pytest.raises(ProjectHomeError, match="git could not confirm") as captured:
        prepare_project_home(project / ".solercia-connect", runner=runner)

    assert "fatal" not in str(captured.value)


def test_the_default_git_runner_fails_closed_when_git_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    with pytest.raises(ProjectHomeError, match="git could not confirm"):
        prepare_project_home(project / ".solercia-connect")


def _connect_calls() -> dict[str, Callable[[ConnectionService], Any]]:
    return {
        "oauth": lambda service: service.connect("gmail", ACCOUNT, open_browser=False),
        "jira_token": lambda service: service.connect_jira(
            ACCOUNT, site=SITE, token=SecretValue("jira-token-value")
        ),
        "jira_oauth": lambda service: service.connect_jira_oauth(
            ACCOUNT, site=SITE, open_browser=False
        ),
        "browser": lambda service: service.connect_browser("outlook", ACCOUNT),
    }


@pytest.mark.parametrize("entry_point", sorted(_connect_calls()))
def test_every_connect_entry_point_fails_closed_before_any_write_when_git_tracks_the_home(
    tmp_path: Path, entry_point: str
) -> None:
    project = _repository(tmp_path)
    home = project / ".solercia-connect"
    prepare_project_home(home)
    (home / "config" / "leak.txt").write_text("x", encoding="utf-8")
    _git(project, "add", "-f", ".solercia-connect/config/leak.txt")
    settings = load_settings(
        environ={
            "SOLERCIA_CONNECT_HOME": str(home),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
            "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID": "atlassian-client",
            "SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET": "atlassian-secret",
        }
    )

    result = _connect_calls()[entry_point](_service(settings))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "configuration_invalid"
    assert ".solercia-connect/config/leak.txt" in result.error.message
    assert not (home / "config" / "profiles").exists()


def test_connect_in_project_mode_prepares_the_folder_and_keeps_profiles_inside_it(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = project / ".solercia-connect"
    settings = load_settings(
        environ={
            "SOLERCIA_CONNECT_HOME": str(home),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
        }
    )

    result = _service(settings, git_runner=_unexpected_git).connect(
        "gmail", ACCOUNT, open_browser=False
    )

    assert result.status == "connected_official"
    assert (home / ".gitignore").read_text(encoding="utf-8") == "*\n"
    profile = ProfileStore.from_settings(settings).path_for("gmail", ACCOUNT)
    assert profile.is_relative_to(home / "config")
    assert sorted(path.name for path in project.iterdir()) == [".solercia-connect"]


def test_without_project_home_connect_never_calls_git_nor_writes_a_gitignore(
    tmp_path: Path,
) -> None:
    project = _repository(tmp_path)
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(project / "xdg-config"),
            "XDG_DATA_HOME": str(project / "xdg-data"),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
        }
    )

    result = _service(settings, git_runner=_unexpected_git).connect(
        "gmail", ACCOUNT, open_browser=False
    )

    assert result.status == "connected_official"
    assert settings.project_home is None
    assert list(project.rglob(".gitignore")) == []
