"""Project mode guards for the one private folder named by ``SOLERCIA_CONNECT_HOME``.

The folder and its subfolders are ``0700`` and owned by the user, a ``.gitignore`` holding ``*``
keeps git away from it, and git must not track anything inside it. Every check fails closed and
never repairs what it finds; see docs/decisions/2026-10-25-modo-proyecto-secretos-en-archivo.md.
Nothing here runs unless the variable is set.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

from solercia_connect.config import (
    ProjectHomeError,
    ProjectLayout,
    _lexists,
    _reject_symlink_components,
    _wrong_owner,
)

HOME_VARIABLE = "SOLERCIA_CONNECT_HOME"
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

    if not any(_holds_git_marker(ancestor) for ancestor in home.parents):
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
    path = layout.gitignore
    label = _label(layout, path)
    try:
        if not _lexists(path):
            write_private_file(path, GITIGNORE_CONTENT.encode(), replace=False)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ProjectHomeError(f"{label} must be a regular file, not a link or a folder")
        content = path.read_bytes()
    except OSError:
        raise ProjectHomeError(f"{label} could not be written or read safely") from None
    if content != GITIGNORE_CONTENT.encode():
        raise ProjectHomeError(
            f"{label} must contain exactly '*' so git ignores the whole folder. Connect never "
            "overwrites it; set that content yourself."
        )


def _holds_git_marker(directory: Path) -> bool:
    try:
        (directory / ".git").lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        raise ProjectHomeError(
            f"{HOME_VARIABLE} sits below a folder that cannot be inspected for git"
        ) from None
    return True


def _label(layout: ProjectLayout, path: Path) -> str:
    if path == layout.home:
        return HOME_VARIABLE
    return f"{HOME_VARIABLE}/{path.relative_to(layout.home).as_posix()}"
