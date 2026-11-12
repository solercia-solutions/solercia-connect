from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import timedelta

import pytest

from solercia_connect.envelope import ResultEnvelope
from solercia_connect.interactive_sessions import InteractiveSessionRegistry
from tests.fakes import NOW


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not met in time")
        time.sleep(0.005)


def _connected() -> ResultEnvelope:
    return ResultEnvelope.success(
        operation="connections.connect",
        status="connected_browser",
        provider="outlook",
        account="user@example.com",
        route="browser",
    )


def test_registry_runs_one_worker_at_a_time_and_forgets_finished_sessions_after_retention() -> None:
    now = {"value": NOW}
    ids = iter(["session-1", "session-2"])
    registry = InteractiveSessionRegistry(
        clock=lambda: now["value"],
        retention_seconds=60,
        id_factory=lambda: next(ids),
    )
    started = threading.Event()
    release = threading.Event()

    def worker() -> ResultEnvelope:
        started.set()
        release.wait(timeout=5)
        return _connected()

    first = registry.start("outlook", "user@example.com", worker)
    assert first == "session-1"
    assert started.wait(timeout=5)
    assert registry.start("teams", "user@example.com", worker) is None
    running = registry.get("session-1")
    assert running is not None and running.running is True
    assert running.started_at == NOW
    active = registry.active()
    assert active is not None and active.session_id == "session-1"

    release.set()
    _wait_until(lambda: not registry.get("session-1").running)  # type: ignore[union-attr]
    finished = registry.get("session-1")
    assert finished is not None and finished.result is not None
    assert finished.result.status == "connected_browser"
    assert finished.finished_at == NOW
    assert registry.active() is None
    assert registry.start("outlook", "user@example.com", _connected) == "session-2"

    now["value"] = NOW + timedelta(seconds=59)
    assert registry.get("session-1") is not None
    now["value"] = NOW + timedelta(seconds=60)
    assert registry.get("session-1") is None
    assert registry.get("unknown") is None


def test_registry_turns_a_leaked_worker_exception_into_a_safe_failure() -> None:
    registry = InteractiveSessionRegistry(clock=lambda: NOW, id_factory=lambda: "session-1")

    def broken() -> ResultEnvelope:
        raise RuntimeError("sensitive playwright detail")

    assert registry.start("outlook", "user@example.com", broken) == "session-1"
    _wait_until(lambda: not registry.get("session-1").running)  # type: ignore[union-attr]

    session = registry.get("session-1")
    assert session is not None and session.result is not None
    assert session.result.ok is False
    assert session.result.error is not None
    assert session.result.error.code == "unknown_error"
    assert "sensitive" not in session.result.error.message
    assert session.result.provider == "outlook"
    assert session.result.route == "browser"


def test_registry_rejects_a_non_positive_retention() -> None:
    with pytest.raises(ValueError, match="retention"):
        InteractiveSessionRegistry(retention_seconds=0)
    assert InteractiveSessionRegistry().retention_seconds == 3600
