"""Project mode guards for the one private folder named by ``SOLERCIA_CONNECT_HOME``.

The folder and its subfolders are ``0700`` and owned by the user, a ``.gitignore`` holding ``*``
keeps git away from it, and git must not track anything inside it. Every check fails closed and
never repairs what it finds; see docs/decisions/2026-10-25-modo-proyecto-secretos-en-archivo.md.
Nothing here runs unless the variable is set, ``SOLERCIA_CONNECT_PROJECT_DISCOVERY`` is on, or
``project init`` is called.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from solercia_connect.config import (
    ATLASSIAN_OAUTH_CONFIGURATION,
    PROJECT_DISCOVERY_VARIABLE,
    ProjectHomeError,
    ProjectLayout,
    _lexists,
    _reject_symlink_components,
    _validate_project_env_metadata,
    _wrong_owner,
)

HOME_VARIABLE = "SOLERCIA_CONNECT_HOME"
PROJECT_FOLDER = ".solercia-connect"
GITIGNORE_CONTENT = "*\n"
GIT_TIMEOUT_SECONDS = 10.0
_LISTED_TRACKED_PATHS = 10

GitRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[bytes]]


class ProjectHomeTrackedError(ProjectHomeError):
    """Git tracks paths inside the project home; they are listed relative to its parent."""

    def __init__(self, tracked_paths: tuple[str, ...]) -> None:
        self.tracked_paths = tracked_paths
        listed = ", ".join(tracked_paths[:_LISTED_TRACKED_PATHS])
        hidden = len(tracked_paths) - _LISTED_TRACKED_PATHS
        more = f" and {hidden} more" if hidden > 0 else ""
        super().__init__(
            f"Git tracks files inside {HOME_VARIABLE}: {listed}{more}. Remove them from the "
            "index with git rm --cached and rotate any secret they held."
        )


def run_git(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """Run git with a bounded timeout and no repository override inherited from ``GIT_*``."""

    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        list(args),
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        env=environment,
    )


def prepare_project_home(home: Path, *, runner: GitRunner | None = None) -> ProjectLayout:
    """Create the private layout and its ``.gitignore``, then prove git tracks nothing inside."""

    layout = ProjectLayout(home)
    _reject_symlink_components(layout.home, HOME_VARIABLE, error=ProjectHomeError)
    for directory in (layout.home, layout.config_dir, layout.data_dir, layout.secrets_dir):
        _ensure_private_directory(layout, directory)
    _ensure_gitignore(layout)
    guard_git_tracking(layout.home, runner=runner)
    return layout


def check_private_directory(layout: ProjectLayout, directory: Path) -> None:
    """Fail closed unless ``directory`` is a real, private directory owned by the user."""

    label = _label(layout, directory)
    try:
        metadata = directory.lstat()
    except OSError:
        raise ProjectHomeError(f"{label} could not be inspected safely") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise ProjectHomeError(f"{label} is a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProjectHomeError(f"{label} is not a directory")
    if _wrong_owner(metadata.st_uid):
        raise ProjectHomeError(f"{label} is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ProjectHomeError(f'{label} is not private; run chmod 700 "${label}"')


def guard_git_tracking(home: Path, *, runner: GitRunner | None = None) -> None:
    """Fail closed if git tracks any path inside ``home``; skip when no ancestor holds ``.git``.

    A ``.git`` file counts as well as a directory, so linked worktrees are covered. Git being
    missing, slow or failing is never read as "nothing tracked".
    """

    if not _inside_git(home):
        return
    command = [
        "git",
        "--literal-pathspecs",
        "-C",
        str(home.parent),
        "ls-files",
        "-z",
        "--",
        str(home),
    ]
    unconfirmed = ProjectHomeError(
        f"{HOME_VARIABLE} is inside a git repository, but git could not confirm that nothing "
        "inside it is tracked. Install git, or move the folder out of the repository."
    )
    try:
        completed = (runner or run_git)(command)
    except (OSError, subprocess.SubprocessError):
        raise unconfirmed from None
    if completed.returncode != 0:
        raise unconfirmed
    tracked = tuple(
        path for path in completed.stdout.decode("utf-8", "replace").split("\0") if path
    )
    if tracked:
        raise ProjectHomeTrackedError(tracked)


def write_private_file(path: Path, content: bytes, *, replace: bool) -> bool:
    """Write ``0600`` content atomically: a temporary file in the same folder, fsync, then rename.

    With ``replace`` the rename is ``os.replace``; without it the file is linked into place only
    when absent, so an existing file is never overwritten and ``False`` reports that case.
    """

    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary_path, path)
            return True
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            return False
        return True
    finally:
        with suppress(OSError):
            temporary_path.unlink()


def _ensure_private_directory(layout: ProjectLayout, directory: Path) -> None:
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    except OSError:
        raise ProjectHomeError(
            f"{_label(layout, directory)} could not be created; create the project folder first"
        ) from None
    check_private_directory(layout, directory)


def _ensure_gitignore(layout: ProjectLayout) -> None:
    try:
        if not _lexists(layout.gitignore):
            write_private_file(layout.gitignore, GITIGNORE_CONTENT.encode(), replace=False)
    except OSError:
        raise ProjectHomeError(
            f"{_label(layout, layout.gitignore)} could not be written safely"
        ) from None
    _, problem = _gitignore_state(layout)
    if problem is not None:
        raise ProjectHomeError(problem)


def _gitignore_state(layout: ProjectLayout) -> tuple[str, str | None]:
    """Return the ``.gitignore`` status and, when it is not usable, the reason why."""

    path = layout.gitignore
    label = _label(layout, path)
    try:
        if not _lexists(path):
            return "absent", None
        if not stat.S_ISREG(path.lstat().st_mode):
            return "unsafe", f"{label} must be a regular file, not a link or a folder"
        content = path.read_bytes()
    except OSError:
        return "unsafe", f"{label} could not be read safely"
    if content != GITIGNORE_CONTENT.encode():
        return "conflict", (
            f"{label} must contain exactly '*' so git ignores the whole folder. Connect never "
            "overwrites it; set that content yourself."
        )
    return "ignores_all", None


def _inside_git(home: Path) -> bool:
    return any(_holds_git_marker(ancestor) for ancestor in home.parents)


def _holds_git_marker(directory: Path, *, label: str = HOME_VARIABLE) -> bool:
    try:
        (directory / ".git").lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        raise ProjectHomeError(
            f"{label} sits below a folder that cannot be inspected for git"
        ) from None
    return True


def find_git_root(start: Path) -> Path | None:
    """Return the first of ``start`` and its ancestors holding a ``.git`` folder or file.

    A ``.git`` file marks a linked worktree, so it counts. Only the file system is read.
    """

    current = Path(os.path.abspath(start))
    for directory in (current, *current.parents):
        if _holds_git_marker(directory, label="The project folder"):
            return directory
    return None


def discover_project_home(cwd: Path, *, runner: GitRunner | None = None) -> Path | None:
    """Return ``<git root>/.solercia-connect`` once it is proven safe to read, else ``None``.

    No git root, or no folder there, means global mode; the bare ``cwd`` is never used. A link or
    a non-directory fails closed. Git tracking is checked before anything inside is read, so a
    folder shipped in a cloned repository is refused before its ``connect.env`` can load.
    """

    root = find_git_root(cwd)
    if root is None:
        return None
    home = root / PROJECT_FOLDER
    label = f"The {PROJECT_FOLDER} folder found by {PROJECT_DISCOVERY_VARIABLE} at the git root"
    try:
        metadata = home.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ProjectHomeError(f"{label} could not be inspected safely") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise ProjectHomeError(f"{label} is a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProjectHomeError(f"{label} is not a directory")
    _reject_symlink_components(home, label, error=ProjectHomeError)
    guard_git_tracking(home, runner=runner)
    check_private_directory(ProjectLayout(home), home)
    return home


def _label(layout: ProjectLayout, path: Path) -> str:
    if path == layout.home:
        return HOME_VARIABLE
    return f"{HOME_VARIABLE}/{path.relative_to(layout.home).as_posix()}"


@dataclass(frozen=True, slots=True)
class ProjectHomeInspection:
    """Read-only state of the project home for doctor; it never holds a secret value."""

    home: Path
    layout: tuple[tuple[str, str], ...]
    gitignore: str
    git_guard: str
    tracked_paths: tuple[str, ...]
    problems: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "home": str(self.home),
            "layout": dict(self.layout),
            "gitignore": self.gitignore,
            "git_guard": self.git_guard,
            "tracked_paths": list(self.tracked_paths),
            "ready": self.ready,
        }


def inspect_project_home(home: Path, *, runner: GitRunner | None = None) -> ProjectHomeInspection:
    """Report the layout, its permissions, the ``.gitignore`` and the git guard; write nothing.

    A missing folder or file is ``absent``, which is fine: connect creates it. Anything unsafe,
    a conflicting ``.gitignore``, a tracked path, or an unconfirmed git check is a problem.
    """

    layout = ProjectLayout(home)
    problems: list[str] = []
    entries = [
        (name, _entry_status(path, partial(check_private_directory, layout, path), problems))
        for name, path in (
            ("home", layout.home),
            ("config", layout.config_dir),
            ("data", layout.data_dir),
            ("secrets", layout.secrets_dir),
        )
    ]
    entries.append(
        (
            "connect.env",
            _entry_status(layout.env_file, partial(_check_env_file, layout), problems),
        )
    )
    gitignore, gitignore_problem = _gitignore_state(layout)
    if gitignore_problem is not None:
        problems.append(gitignore_problem)
    git_guard = "clean"
    tracked: tuple[str, ...] = ()
    try:
        if not _inside_git(layout.home):
            git_guard = "not_applicable"
        else:
            guard_git_tracking(layout.home, runner=runner)
    except ProjectHomeTrackedError as failure:
        git_guard, tracked = "tracked", failure.tracked_paths
        problems.append(str(failure))
    except ProjectHomeError as failure:
        git_guard = "unavailable"
        problems.append(str(failure))
    return ProjectHomeInspection(
        layout.home, tuple(entries), gitignore, git_guard, tracked, tuple(problems)
    )


def _entry_status(path: Path, check: Callable[[], None], problems: list[str]) -> str:
    if not _lexists(path):
        return "absent"
    try:
        check()
    except ProjectHomeError as failure:
        problems.append(str(failure))
        return "unsafe"
    return "private"


def _check_env_file(layout: ProjectLayout) -> None:
    label = _label(layout, layout.env_file)
    try:
        metadata = layout.env_file.lstat()
    except OSError:
        raise ProjectHomeError(f"{label} could not be inspected safely") from None
    _validate_project_env_metadata(metadata, label)


# The connect.env template names each known setting, commented out and without a value.
CONNECT_ENV_TEMPLATE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Google (Gmail, Calendar, Meet): OAuth Desktop app client",
        ("SOLERCIA_CONNECT_GOOGLE_CLIENT_ID", "SOLERCIA_CONNECT_GOOGLE_CLIENT_SECRET"),
    ),
    ("Jira API token, read by: connect jira ... --token-env JIRA_API_TOKEN", ("JIRA_API_TOKEN",)),
    (
        "Bitbucket API token with Bitbucket scopes, read by: connect bitbucket ... "
        "--token-env BITBUCKET_API_TOKEN",
        ("BITBUCKET_API_TOKEN",),
    ),
    (
        "Jira over Atlassian OAuth: connect jira ... --token-kind oauth",
        ATLASSIAN_OAUTH_CONFIGURATION,
    ),
    (
        "Microsoft 365 official route (Outlook, Teams, OneDrive)",
        ("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID", "SOLERCIA_CONNECT_MICROSOFT_TENANT"),
    ),
)


def connect_env_template() -> str:
    forbidden = f"{HOME_VARIABLE}, SOLERCIA_CONNECT_ENV_FILE or {PROJECT_DISCOVERY_VARIABLE}"
    lines = [
        "# Solercia Connect settings for this project: uncomment only what you use, NAME=value.",
        "# Keep this file private (chmod 600); git ignores the whole folder. Never share it.",
        f"# It cannot set {forbidden}.",
    ]
    for title, names in CONNECT_ENV_TEMPLATE_GROUPS:
        lines.extend(("", f"# {title}", *(f"# {name}=" for name in names)))
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class ProjectInitResult:
    home: Path
    created: tuple[str, ...]


def init_project_home(root: Path, *, runner: GitRunner | None = None) -> ProjectInitResult:
    """Create ``<root>/.solercia-connect`` with the same guards as connect, plus a template.

    An existing folder is git-checked before anything is added to it. ``connect.env`` is written
    ``0600`` only when absent and is never overwritten; an existing one must already be private.
    """

    layout = ProjectLayout(root / PROJECT_FOLDER)
    _reject_symlink_components(layout.home, HOME_VARIABLE, error=ProjectHomeError)
    entries = (
        ("home", layout.home),
        ("config", layout.config_dir),
        ("data", layout.data_dir),
        ("secrets", layout.secrets_dir),
        (".gitignore", layout.gitignore),
        ("connect.env", layout.env_file),
    )
    existing = {name for name, path in entries if _lexists(path)}
    if "home" in existing:
        guard_git_tracking(layout.home, runner=runner)
    prepare_project_home(layout.home, runner=runner)
    template = connect_env_template().encode()
    if "connect.env" not in existing:
        try:
            if not write_private_file(layout.env_file, template, replace=False):
                existing.add("connect.env")
        except OSError:
            raise ProjectHomeError(
                f"{_label(layout, layout.env_file)} could not be written safely"
            ) from None
    _check_env_file(layout)
    return ProjectInitResult(
        layout.home, tuple(name for name, _ in entries if name not in existing)
    )
