from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from solercia_connect.config import ConnectionType
from solercia_connect.domain import (
    CalendarQuery,
    Capability,
    ConnectorRequest,
    DomainRecord,
    HealthReport,
    HealthState,
    MailQuery,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    TranscriptQuery,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import ProviderDescriptor
from solercia_connect.providers._shared import (
    canonical_account_identities,
    unique_canonical_account_identity,
)
from solercia_connect.providers.m365_browser.driver import BrowserDriver, BrowserSnapshot
from solercia_connect.providers.m365_browser.selectors import ENDPOINTS, SELECTORS
from solercia_connect.providers.m365_browser.states import BrowserState, BrowserStateMachine

_OUTLOOK_IDENTITY_PROBE_ATTEMPTS = 2


class Microsoft365BrowserAdapter:
    descriptor = ProviderDescriptor(
        "microsoft365.browser",
        ProviderFamily.MICROSOFT,
        RouteKind.BROWSER,
        frozenset(
            {
                Capability.MAIL_SENT_LIST,
                Capability.CALENDAR_EVENTS_LIST,
                Capability.MEETING_TRANSCRIPTS_LIST,
                Capability.MEETING_TRANSCRIPT_GET,
            }
        ),
    )

    def __init__(
        self,
        driver: BrowserDriver,
        *,
        connection_type: str | ConnectionType,
        profile_dir: Path,
        expected_account: str | None,
        navigation_timeout_ms: int = 30_000,
        state_poll_ms: int = 1_000,
        max_poll_cycles: int = 120,
        # Bounded post-shell cycles: Outlook token observation or Teams DOM re-observation.
        identity_attestation_cycles: int = 30,
        read_timeout_ms: int = 30_000,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if (
            min(
                navigation_timeout_ms,
                state_poll_ms,
                max_poll_cycles,
                identity_attestation_cycles,
                read_timeout_ms,
            )
            <= 0
        ):
            raise ValueError("Browser timeouts and poll bounds must be positive")
        parsed_type = ConnectionType.parse(connection_type)
        if parsed_type not in {ConnectionType.OUTLOOK, ConnectionType.TEAMS}:
            raise ValueError("Microsoft browser fallback supports only Outlook and Teams")
        self._driver = driver
        self._connection_type = parsed_type
        self._profile_dir = profile_dir.expanduser()
        self._expected_account = (
            _canonical_expected_account(expected_account) if expected_account else None
        )
        self._navigation_timeout_ms = navigation_timeout_ms
        self._state_poll_ms = state_poll_ms
        self._max_poll_cycles = max_poll_cycles
        self._identity_attestation_cycles = identity_attestation_cycles
        self._read_timeout_ms = read_timeout_ms
        self._clock = clock

    def health(self) -> HealthReport:
        machine = BrowserStateMachine()
        try:
            account = self._prepare(machine, interactive=False, headless=True)
            return HealthReport(
                self.descriptor.provider_id,
                HealthState.READY,
                ReasonCode.SUCCESS,
                tuple(sorted(self.descriptor.capabilities, key=str)),
                account,
                (
                    "Microsoft 365 browser session is ready; identity=outlook-rest-v2-me"
                    if self._connection_type is ConnectionType.OUTLOOK
                    else f"Microsoft 365 browser session is ready; selectors={SELECTORS.version}"
                ),
            )
        finally:
            self._close(machine)

    def interactive_login(self) -> HealthReport:
        headed = BrowserStateMachine()
        try:
            account = self._prepare(headed, interactive=True, headless=False)
        finally:
            self._close(headed)

        verified = BrowserStateMachine()
        try:
            persisted_account = self._prepare(
                verified,
                interactive=False,
                headless=True,
                requested_account=account,
            )
            return HealthReport(
                self.descriptor.provider_id,
                HealthState.READY,
                ReasonCode.SUCCESS,
                tuple(sorted(self.descriptor.capabilities, key=str)),
                persisted_account,
                "Interactive login/MFA was persisted and verified headlessly in the dedicated "
                "profile",
            )
        finally:
            self._close(verified)

    def execute(self, request: ConnectorRequest) -> tuple[DomainRecord, ...]:
        requested_account = _requested_account(request)
        machine = BrowserStateMachine()
        try:
            account = self._prepare(
                machine,
                interactive=False,
                headless=True,
                requested_account=requested_account,
            )
            machine.advance(BrowserState.READING)
            records = self._driver.read_records(
                request.capability,
                request.payload,
                SELECTORS,
                timeout_ms=self._read_timeout_ms,
            )
            machine.advance(BrowserState.COMPLETE)
            return tuple(self._bind_provenance(record, account) for record in records)
        except ConnectorFailure:
            if machine.state not in {BrowserState.FAILED, BrowserState.CLOSED}:
                machine.advance(BrowserState.FAILED)
            raise
        finally:
            self._close(machine)

    def _prepare(
        self,
        machine: BrowserStateMachine,
        *,
        interactive: bool,
        headless: bool,
        requested_account: str | None = None,
    ) -> str:
        machine.advance(BrowserState.OPENING)
        self._driver.open_persistent(
            self._profile_dir,
            headless=headless,
            connection_type=self._connection_type,
        )
        self._driver.navigate(
            ENDPOINTS.home_for(self._connection_type),
            timeout_ms=self._navigation_timeout_ms,
        )
        last_snapshot: BrowserSnapshot | None = None
        for _ in range(self._max_poll_cycles):
            snapshot = self._driver.snapshot(
                SELECTORS,
                connection_type=self._connection_type,
            )
            last_snapshot = snapshot
            if snapshot.mfa_visible:
                if machine.state is not BrowserState.MFA_REQUIRED:
                    machine.advance(BrowserState.MFA_REQUIRED)
                if not interactive:
                    raise ConnectorFailure(
                        ReasonCode.BROWSER_MFA_REQUIRED,
                        "Microsoft 365 browser session requires user MFA",
                    )
            elif snapshot.login_visible:
                if machine.state is not BrowserState.LOGIN_REQUIRED:
                    machine.advance(BrowserState.LOGIN_REQUIRED)
                if not interactive:
                    raise ConnectorFailure(
                        ReasonCode.BROWSER_LOGIN_REQUIRED,
                        "Microsoft 365 browser session requires interactive login",
                    )
            elif snapshot.authenticated_shell_visible:
                machine.advance(BrowserState.AUTHENTICATED)
                account = (
                    self._outlook_account(machine)
                    if self._connection_type is ConnectionType.OUTLOOK
                    else self._attest_teams_account(machine, snapshot)
                )
                if any(
                    boundary is not None and account != boundary
                    for boundary in (self._expected_account, requested_account)
                ):
                    machine.advance(BrowserState.ACCOUNT_MISMATCH)
                    raise ConnectorFailure(
                        ReasonCode.BROWSER_ACCOUNT_MISMATCH,
                        "Authenticated Microsoft 365 account does not match the configured or "
                        "requested account",
                    )
                machine.advance(BrowserState.READY)
                return account
            self._driver.wait(self._state_poll_ms)

        if last_snapshot and last_snapshot.mfa_visible:
            reason = ReasonCode.BROWSER_MFA_REQUIRED
        elif last_snapshot and last_snapshot.login_visible:
            reason = ReasonCode.BROWSER_LOGIN_REQUIRED
        else:
            reason = ReasonCode.BROWSER_TIMEOUT
        machine.advance(BrowserState.FAILED)
        raise ConnectorFailure(reason, "Microsoft 365 browser state did not become ready in time")

    def _outlook_account(self, machine: BrowserStateMachine) -> str:
        observed = self._driver.outlook_session_identity(
            wait_interval_ms=self._state_poll_ms,
            max_wait_cycles=self._identity_attestation_cycles,
            max_probe_attempts=_OUTLOOK_IDENTITY_PROBE_ATTEMPTS,
            probe_timeout_ms=self._read_timeout_ms,
        )
        identity = unique_canonical_account_identity(observed, exact=True)
        if identity is None:
            machine.advance(BrowserState.FAILED)
            raise ConnectorFailure(
                ReasonCode.BROWSER_IDENTITY_UNAVAILABLE,
                "Outlook identity endpoint did not return one canonical EmailAddress",
            )
        return identity

    def _attest_teams_account(
        self,
        machine: BrowserStateMachine,
        initial_snapshot: BrowserSnapshot,
    ) -> str:
        snapshot = initial_snapshot
        identity_menu_materialized = False
        for cycle in range(self._identity_attestation_cycles + 1):
            identities = _identities_from_snapshot(snapshot)
            if len(identities) > 1:
                machine.advance(BrowserState.FAILED)
                raise ConnectorFailure(
                    ReasonCode.BROWSER_SELECTOR_MISMATCH,
                    _identity_diagnostic(
                        "Authenticated account identity is ambiguous",
                        snapshot,
                    ),
                )
            if len(identities) == 1:
                return next(iter(identities))
            if cycle == self._identity_attestation_cycles:
                break
            if not identity_menu_materialized:
                identity_menu_materialized = self._driver.reveal_identity_menu(
                    SELECTORS,
                    timeout_ms=self._state_poll_ms,
                )
            self._driver.wait(self._state_poll_ms)
            snapshot = self._driver.snapshot(
                SELECTORS,
                connection_type=ConnectionType.TEAMS,
            )
            if snapshot.mfa_visible:
                machine.advance(BrowserState.FAILED)
                raise ConnectorFailure(
                    ReasonCode.BROWSER_MFA_REQUIRED,
                    "Microsoft 365 browser persistence verification requires user MFA",
                )
            if snapshot.login_visible:
                machine.advance(BrowserState.FAILED)
                raise ConnectorFailure(
                    ReasonCode.BROWSER_LOGIN_REQUIRED,
                    "Microsoft 365 browser persistence verification requires interactive login",
                )

        machine.advance(BrowserState.FAILED)
        raise ConnectorFailure(
            ReasonCode.BROWSER_SELECTOR_MISMATCH,
            _identity_diagnostic(
                "Authenticated account identity was not observable after bounded attestation",
                snapshot,
            ),
        )

    def _bind_provenance(self, record: DomainRecord, account: str) -> DomainRecord:
        identity_evidence = (
            "outlook-rest-v2-me"
            if self._connection_type is ConnectionType.OUTLOOK
            else f"web-ui:{SELECTORS.version}"
        )
        provenance = Provenance(
            self.descriptor.provider_id,
            self.descriptor.family,
            self.descriptor.route,
            record.provenance.source_id,
            self._clock(),
            account,
            identity_evidence,
        )
        return replace(record, provenance=provenance)

    def _close(self, machine: BrowserStateMachine) -> None:
        try:
            self._driver.close()
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


def _identities_from_snapshot(snapshot: BrowserSnapshot) -> frozenset[str]:
    return frozenset(
        identity
        for hint in snapshot.account_hints
        for identity in canonical_account_identities(hint)
    )


def _identity_diagnostic(message: str, snapshot: BrowserSnapshot) -> str:
    signal_count = len(tuple(dict.fromkeys(snapshot.account_hints)))
    return (
        f"{message}; selectors={SELECTORS.version}; signals={signal_count}; "
        f"host={_safe_host(snapshot.url)}"
    )


def _safe_host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return "unknown"
    if not host or re.fullmatch(r"[a-z0-9.-]{1,255}", host) is None:
        return "unknown"
    return host


def _canonical_expected_account(value: str) -> str:
    identity = unique_canonical_account_identity(value, exact=True)
    if identity is None:
        raise ValueError("Expected Microsoft 365 account must be one unambiguous email identity")
    return identity


def _requested_account(request: ConnectorRequest) -> str | None:
    payload = request.payload
    if not isinstance(payload, (MailQuery, CalendarQuery, TranscriptQuery)):
        return None
    if payload.account_id is None:
        return None
    identity = unique_canonical_account_identity(payload.account_id, exact=True)
    if identity is None:
        raise ConnectorFailure(
            ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
            "Requested Microsoft 365 account is not one unambiguous account identity",
        )
    return identity
