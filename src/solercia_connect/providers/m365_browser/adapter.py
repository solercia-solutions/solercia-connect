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
    MailCategoriesUpdate,
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
from solercia_connect.providers.browser_core.login import (
    SessionPhase,
    close_session,
    poll_until_ready,
)
from solercia_connect.providers.browser_core.states import BrowserState, BrowserStateMachine
from solercia_connect.providers.m365_browser.driver import BrowserDriver, BrowserSnapshot
from solercia_connect.providers.m365_browser.selectors import ENDPOINTS, SELECTORS

_OUTLOOK_IDENTITY_PROBE_ATTEMPTS = 2
# Names the vendor in the user-facing messages of the shared sign-in loop.
_PRODUCT = "Microsoft 365"

# Single source of truth for what the browser route can actually extract today.
_BROWSER_EXTRACTION_CAPABILITIES: dict[ConnectionType, tuple[Capability, ...]] = {
    ConnectionType.OUTLOOK: (
        Capability.MAIL_SENT_LIST,
        Capability.MAIL_INBOX_LIST,
        Capability.CALENDAR_EVENTS_LIST,
    ),
    # The Teams client itself extracts nothing over this route: it degrades under automation and
    # its transcripts are not stored there anyway.
    ConnectionType.TEAMS: (),
    # A meeting transcript is a drive item, hanging off the recording, so it is read from the
    # same SharePoint session that serves files rather than from the Teams surface.
    ConnectionType.ONEDRIVE: (
        Capability.FILES_SEARCH,
        Capability.FILES_GET,
        Capability.MEETING_TRANSCRIPTS_LIST,
        Capability.MEETING_TRANSCRIPT_GET,
    ),
}


def browser_surface_for_capability(capability: Capability) -> ConnectionType | None:
    """Return which session surface actually serves a capability over the browser route.

    A capability's target names where it belongs to the caller, which is not always where its
    records live. A meeting transcript belongs to Teams and is stored in the drive, so asking for
    one has to open the drive session, not the Teams client.
    """
    for connection_type, capabilities in _BROWSER_EXTRACTION_CAPABILITIES.items():
        if capability in capabilities:
            return connection_type
    return None


def browser_extraction_report(connection_type: str | ConnectionType) -> dict[str, object]:
    """Describe the browser route's extraction surface for a connection type."""
    parsed = ConnectionType.parse(connection_type)
    capabilities = _BROWSER_EXTRACTION_CAPABILITIES.get(parsed, ())
    return {
        "extraction_status": "available" if capabilities else "not_implemented",
        "capabilities": [capability.value for capability in capabilities],
    }


class Microsoft365BrowserAdapter:
    descriptor = ProviderDescriptor(
        "microsoft365.browser",
        ProviderFamily.MICROSOFT,
        RouteKind.BROWSER,
        frozenset(
            {
                Capability.MAIL_SENT_LIST,
                Capability.MAIL_INBOX_LIST,
                Capability.MAIL_CATEGORIES_SET,
                Capability.CALENDAR_EVENTS_LIST,
                Capability.MEETING_TRANSCRIPTS_LIST,
                Capability.MEETING_TRANSCRIPT_GET,
                Capability.FILES_SEARCH,
                Capability.FILES_GET,
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
        if parsed_type not in {
            ConnectionType.OUTLOOK,
            ConnectionType.TEAMS,
            ConnectionType.ONEDRIVE,
        }:
            raise ValueError("Microsoft browser fallback supports only Outlook, Teams and OneDrive")
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
        write = _category_write(request, self._connection_type)
        requested_account = _requested_account(request)
        machine = BrowserStateMachine()
        try:
            account = self._prepare(
                machine,
                interactive=False,
                headless=True,
                requested_account=requested_account,
            )
            # One operating state serves the reads and the single category write alike.
            machine.advance(BrowserState.READING)
            records: tuple[DomainRecord, ...]
            if write is not None:
                records = (
                    self._driver.set_mail_categories(write, timeout_ms=self._read_timeout_ms),
                )
            else:
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
        return poll_until_ready(
            machine,
            _MicrosoftSessionProbe(self._driver, self._connection_type, self._attested_account),
            interactive=interactive,
            product=_PRODUCT,
            boundaries=(self._expected_account, requested_account),
            max_poll_cycles=self._max_poll_cycles,
            state_poll_ms=self._state_poll_ms,
        )

    def _attested_account(
        self,
        machine: BrowserStateMachine,
        snapshot: BrowserSnapshot,
    ) -> str:
        """Prove identity with the evidence the connection type's own session exposes."""
        if self._connection_type is ConnectionType.OUTLOOK:
            return self._outlook_account(machine)
        if self._connection_type is ConnectionType.ONEDRIVE:
            return self._onedrive_account(machine)
        return self._attest_teams_account(machine, snapshot)

    def _onedrive_account(self, machine: BrowserStateMachine) -> str:
        try:
            return self._driver.onedrive_session_identity(timeout_ms=self._read_timeout_ms)
        except ConnectorFailure:
            machine.advance(BrowserState.FAILED)
            raise

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
        if self._connection_type is ConnectionType.OUTLOOK:
            identity_evidence = "outlook-rest-v2-me"
        elif self._connection_type is ConnectionType.ONEDRIVE:
            identity_evidence = "sharepoint-rest-v2-me"
        else:
            identity_evidence = f"web-ui:{SELECTORS.version}"
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
        close_session(machine, self._driver.close)


class _MicrosoftSessionProbe:
    """How the Microsoft surfaces show login, MFA and an authenticated session to the core loop.

    A drive surface exposes no stable shell selector, so traffic the session itself authorized
    also counts as authenticated there; it is asked only when no prompt or shell is visible.
    """

    def __init__(
        self,
        driver: BrowserDriver,
        connection_type: ConnectionType,
        attest: Callable[[BrowserStateMachine, BrowserSnapshot], str],
    ) -> None:
        self._driver = driver
        self._connection_type = connection_type
        self._attest = attest

    def observe(self) -> BrowserSnapshot:
        return self._driver.snapshot(SELECTORS, connection_type=self._connection_type)

    def phase(self, snapshot: BrowserSnapshot) -> SessionPhase:
        if snapshot.mfa_visible:
            return SessionPhase.MFA_REQUIRED
        if snapshot.login_visible:
            return SessionPhase.LOGIN_REQUIRED
        if snapshot.authenticated_shell_visible or (
            self._connection_type is ConnectionType.ONEDRIVE
            and self._driver.observed_session_authorization()
        ):
            return SessionPhase.AUTHENTICATED
        return SessionPhase.PENDING

    def attest(self, machine: BrowserStateMachine, snapshot: BrowserSnapshot) -> str:
        return self._attest(machine, snapshot)

    def wait(self, milliseconds: int) -> None:
        self._driver.wait(milliseconds)


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


def _category_write(
    request: ConnectorRequest,
    connection_type: ConnectionType,
) -> MailCategoriesUpdate | None:
    """Select the category write, refusing it before any browser opens off the Outlook session."""
    if request.capability is not Capability.MAIL_CATEGORIES_SET:
        return None
    if connection_type is not ConnectionType.OUTLOOK or not isinstance(
        request.payload, MailCategoriesUpdate
    ):
        raise ConnectorFailure(
            ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED,
            "Outlook categories are written only through the Outlook browser session",
        )
    return request.payload


def _requested_account(request: ConnectorRequest) -> str | None:
    payload = request.payload
    if not isinstance(payload, (MailQuery, CalendarQuery, TranscriptQuery, MailCategoriesUpdate)):
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
