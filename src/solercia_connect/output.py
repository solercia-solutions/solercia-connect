from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from solercia_connect.envelope import ResultEnvelope
from solercia_connect.serialization import to_primitive


class SecureOutputError(ValueError):
    pass


def write_secure_json(
    path: Path,
    payload: ResultEnvelope | Any,
    *,
    overwrite: bool = False,
) -> Path:
    target = Path(os.path.abspath(path.expanduser()))
    descriptor = -1
    temporary_path: Path | None = None
    try:
        _prepare_parent(target.parent)
        if target.is_symlink():
            raise SecureOutputError("Output path is a symbolic link")
        if target.exists() and not overwrite:
            raise SecureOutputError("Output path already exists; use explicit overwrite")
        value = payload.to_dict() if isinstance(payload, ResultEnvelope) else to_primitive(payload)
        encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()

        descriptor, temporary = tempfile.mkstemp(prefix=".solercia-connect-", dir=target.parent)
        temporary_path = Path(temporary)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_symlink():
            raise SecureOutputError("Output path is a symbolic link")
        if overwrite:
            os.replace(temporary_path, target)
            temporary_path = None
        else:
            try:
                os.link(temporary_path, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise SecureOutputError(
                    "Output path already exists; use explicit overwrite"
                ) from exc
            with suppress(OSError):
                temporary_path.unlink()
        return target
    except SecureOutputError:
        raise
    except OSError as exc:
        raise SecureOutputError("Output could not be written safely") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def _prepare_parent(parent: Path) -> None:
    try:
        _reject_symlink_components(parent)
        if parent.is_symlink():
            raise SecureOutputError("Output directory is a symbolic link")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _reject_symlink_components(parent)
        if parent.is_symlink() or not parent.is_dir():
            raise SecureOutputError("Output directory is invalid")
    except SecureOutputError:
        raise
    except OSError as exc:
        raise SecureOutputError("Output directory could not be prepared safely") from exc


def _reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise SecureOutputError("Output directory contains a symbolic link")
