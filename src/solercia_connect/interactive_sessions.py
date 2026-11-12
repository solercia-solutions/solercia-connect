from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from solercia_connect.domain import utc_now
from solercia_connect.envelope import ErrorDetail, ResultEnvelope

# A finished session stays readable for this long so a client that polls late still gets the
# outcome; after that the process forgets it. Nothing is written to disk.
SESSION_RETENTION_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class InteractiveSession:
    """Snapshot of one supervised browser login started from the loopback service."""

    session_id: str
    type_value: str
    account: str
    started_at: datetime
    finished_at: datetime | None = None
    result: ResultEnvelope | None = None

    @property
    def running(self) -> bool:
        return self.result is None


class _Entry:
    __slots__ = ("account", "finished_at", "result", "session_id", "started_at", "type_value")

    def __init__(self, session_id: str, type_value: str, account: str, started_at: datetime):
        self.session_id = session_id
        self.type_value = type_value
        self.account = account
        self.started_at = started_at
        self.finished_at: datetime | None = None
        self.result: ResultEnvelope | None = None

    def snapshot(self) -> InteractiveSession:
        return InteractiveSession(
            self.session_id,
            self.type_value,
            self.account,
            self.started_at,
            self.finished_at,
            self.result,
        )


class InteractiveSessionRegistry:
    """Process-local registry of headed browser logins; one runs at a time.

    A single headed Chromium at a time keeps the operator's attention on one Microsoft page and
    avoids two logins racing for the same persistent profile. The worker runs in a daemon
    thread; any exception it leaks becomes an ``unknown_error`` envelope so a session can never
    stay ``running`` forever.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = utc_now,
        retention_seconds: int = SESSION_RETENTION_SECONDS,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        if retention_seconds <= 0:
            raise ValueError("Session retention must be a positive number of seconds")
        self._clock = clock
        self._retention = timedelta(seconds=retention_seconds)
        self._id_factory = id_factory
        self._lock = threading.Lock()
        self._sessions: dict[str, _Entry] = {}

    @property
    def retention_seconds(self) -> int:
        return int(self._retention.total_seconds())

    def start(
        self,
        type_value: str,
        account: str,
        worker: Callable[[], ResultEnvelope],
    ) -> str | None:
        """Start ``worker`` in a daemon thread; return its session id, or None when one runs."""

        with self._lock:
            self._purge_locked()
            if any(entry.result is None for entry in self._sessions.values()):
                return None
            entry = _Entry(self._id_factory(), type_value, account, self._clock())
            self._sessions[entry.session_id] = entry
        thread = threading.Thread(
            target=self._run,
            args=(entry, worker),
            name=f"solercia-connect-interactive-{entry.session_id}",
            daemon=True,
        )
        thread.start()
        return entry.session_id

    def get(self, session_id: str) -> InteractiveSession | None:
        with self._lock:
            self._purge_locked()
            entry = self._sessions.get(session_id)
            return entry.snapshot() if entry is not None else None

    def active(self) -> InteractiveSession | None:
        """The session still running, if any."""

        with self._lock:
            for entry in self._sessions.values():
                if entry.result is None:
                    return entry.snapshot()
            return None

    def _run(self, entry: _Entry, worker: Callable[[], ResultEnvelope]) -> None:
        try:
            result = worker()
        except Exception:
            result = ResultEnvelope.failure(
                operation="connections.connect",
                status="failed",
                provider=entry.type_value,
                account=entry.account,
                route="browser",
                data={"interactive_flow_started": True},
                error=ErrorDetail(
                    "unknown_error",
                    "Interactive browser login failed with an unknown error",
                    False,
                    remediation=("Run solercia-connect doctor and start the login again.",),
                    cause_category="unknown",
                ),
            )
        with self._lock:
            entry.result = result
            entry.finished_at = self._clock()

    def _purge_locked(self) -> None:
        now = self._clock()
        expired = [
            session_id
            for session_id, entry in self._sessions.items()
            if entry.finished_at is not None and entry.finished_at + self._retention <= now
        ]
        for session_id in expired:
            del self._sessions[session_id]
