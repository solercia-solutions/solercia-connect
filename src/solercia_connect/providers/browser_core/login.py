"""Vendor-neutral supervised sign-in: the bounded login/MFA poll and the close discipline.

A family supplies only what differs between vendors, through a small probe: how to observe its
session, how to classify one observation, and how to prove the authenticated identity. The loop,
its bounds, its typed failures and its state transitions are the same for every family.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.browser_core.states import BrowserState, BrowserStateMachine


class SessionPhase(StrEnum):
    """What one observation of a session shows, in the order it is checked."""

    MFA_REQUIRED = "mfa_required"
    LOGIN_REQUIRED = "login_required"
    AUTHENTICATED = "authenticated"
    PENDING = "pending"


class SessionProbe[SnapshotT](Protocol):
    def observe(self) -> SnapshotT: ...

    def phase(self, snapshot: SnapshotT) -> SessionPhase: ...

    def attest(self, machine: BrowserStateMachine, snapshot: SnapshotT) -> str:
        """Return the canonical account the session proves, or fail closed."""
        ...

    def wait(self, milliseconds: int) -> None: ...


def poll_until_ready[SnapshotT](
    machine: BrowserStateMachine,
    probe: SessionProbe[SnapshotT],
    *,
    interactive: bool,
    product: str,
    boundaries: tuple[str | None, ...],
    max_poll_cycles: int,
    state_poll_ms: int,
) -> str:
    """Poll an opened session until it is ready, and return the account it proved.

    A headless run never waits for the user: a visible login or MFA prompt fails closed at once.
    An interactive run waits for the user within ``max_poll_cycles``. An authenticated session
    must prove an account inside every non-empty boundary, or it ends in an account mismatch.
    ``product`` names the vendor in the user-facing failure messages.
    """
    last_phase: SessionPhase | None = None
    for _ in range(max_poll_cycles):
        snapshot = probe.observe()
        phase = probe.phase(snapshot)
        last_phase = phase
        if phase is SessionPhase.MFA_REQUIRED:
            if machine.state is not BrowserState.MFA_REQUIRED:
                machine.advance(BrowserState.MFA_REQUIRED)
            if not interactive:
                raise ConnectorFailure(
                    ReasonCode.BROWSER_MFA_REQUIRED,
                    f"{product} browser session requires user MFA",
                )
        elif phase is SessionPhase.LOGIN_REQUIRED:
            if machine.state is not BrowserState.LOGIN_REQUIRED:
                machine.advance(BrowserState.LOGIN_REQUIRED)
            if not interactive:
                raise ConnectorFailure(
                    ReasonCode.BROWSER_LOGIN_REQUIRED,
                    f"{product} browser session requires interactive login",
                )
        elif phase is SessionPhase.AUTHENTICATED:
            machine.advance(BrowserState.AUTHENTICATED)
            account = probe.attest(machine, snapshot)
            if any(boundary is not None and account != boundary for boundary in boundaries):
                machine.advance(BrowserState.ACCOUNT_MISMATCH)
                raise ConnectorFailure(
                    ReasonCode.BROWSER_ACCOUNT_MISMATCH,
                    f"Authenticated {product} account does not match the configured or "
                    "requested account",
                )
            machine.advance(BrowserState.READY)
            return account
        probe.wait(state_poll_ms)

    if last_phase is SessionPhase.MFA_REQUIRED:
        reason = ReasonCode.BROWSER_MFA_REQUIRED
    elif last_phase is SessionPhase.LOGIN_REQUIRED:
        reason = ReasonCode.BROWSER_LOGIN_REQUIRED
    else:
        reason = ReasonCode.BROWSER_TIMEOUT
    machine.advance(BrowserState.FAILED)
    raise ConnectorFailure(reason, f"{product} browser state did not become ready in time")


def close_session(machine: BrowserStateMachine, close: Callable[[], None]) -> None:
    """Close the browser, then end the machine in CLOSED, failing any unfinished opening."""
    try:
        close()
    finally:
        if machine.state is not BrowserState.CLOSED:
            if machine.state not in {
                BrowserState.READY,
                BrowserState.COMPLETE,
                BrowserState.FAILED,
                BrowserState.ACCOUNT_MISMATCH,
            } and machine.state in {
                BrowserState.OPENING,
                BrowserState.LOGIN_REQUIRED,
                BrowserState.MFA_REQUIRED,
                BrowserState.AUTHENTICATED,
            }:
                machine.advance(BrowserState.FAILED)
            machine.advance(BrowserState.CLOSED)
