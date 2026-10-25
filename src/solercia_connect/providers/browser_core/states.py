from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BrowserState(StrEnum):
    NEW = "new"
    OPENING = "opening"
    LOGIN_REQUIRED = "login_required"
    MFA_REQUIRED = "mfa_required"
    AUTHENTICATED = "authenticated"
    ACCOUNT_MISMATCH = "account_mismatch"
    READY = "ready"
    READING = "reading"
    COMPLETE = "complete"
    FAILED = "failed"
    CLOSED = "closed"


_ALLOWED: dict[BrowserState, frozenset[BrowserState]] = {
    BrowserState.NEW: frozenset({BrowserState.OPENING}),
    BrowserState.OPENING: frozenset(
        {
            BrowserState.LOGIN_REQUIRED,
            BrowserState.MFA_REQUIRED,
            BrowserState.AUTHENTICATED,
            BrowserState.FAILED,
        }
    ),
    BrowserState.LOGIN_REQUIRED: frozenset(
        {
            BrowserState.LOGIN_REQUIRED,
            BrowserState.MFA_REQUIRED,
            BrowserState.AUTHENTICATED,
            BrowserState.FAILED,
        }
    ),
    BrowserState.MFA_REQUIRED: frozenset(
        {
            BrowserState.MFA_REQUIRED,
            BrowserState.LOGIN_REQUIRED,
            BrowserState.AUTHENTICATED,
            BrowserState.FAILED,
        }
    ),
    BrowserState.AUTHENTICATED: frozenset(
        {BrowserState.ACCOUNT_MISMATCH, BrowserState.READY, BrowserState.FAILED}
    ),
    BrowserState.ACCOUNT_MISMATCH: frozenset({BrowserState.FAILED, BrowserState.CLOSED}),
    BrowserState.READY: frozenset({BrowserState.READING, BrowserState.CLOSED}),
    BrowserState.READING: frozenset({BrowserState.COMPLETE, BrowserState.FAILED}),
    BrowserState.COMPLETE: frozenset({BrowserState.READY, BrowserState.CLOSED}),
    BrowserState.FAILED: frozenset({BrowserState.CLOSED}),
    BrowserState.CLOSED: frozenset(),
}


@dataclass(slots=True)
class BrowserStateMachine:
    state: BrowserState = BrowserState.NEW
    history: list[BrowserState] = field(default_factory=lambda: [BrowserState.NEW])

    def advance(self, next_state: BrowserState) -> None:
        if next_state not in _ALLOWED[self.state]:
            raise RuntimeError(
                f"Invalid browser state transition: {self.state.value} -> {next_state.value}"
            )
        self.state = next_state
        self.history.append(next_state)
