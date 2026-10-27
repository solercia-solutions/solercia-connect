"""Supervised-browser route for Google Meet transcripts.

Discovery and reading follow decision 2026-10-25, D1, as made precise on 2026-10-26: the Calendar
web day view finds each event's transcript attachment, and the Docs ``mobilebasic`` view yields
its text. The records are the official route's records; only provenance tells them apart, apart
from the two fields this surface cannot observe (see ``_record``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    DateWindow,
    HealthReport,
    HealthState,
    MeetingTranscript,
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
from solercia_connect.providers.google_browser.driver import (
    GoogleBrowserDriver,
    GoogleSessionSnapshot,
)
from solercia_connect.providers.google_browser.parsing import (
    TranscriptAttachment,
    event_reference,
    has_transcript_section,
    normalize_document_text,
    transcript_attachment,
    window_days,
)
from solercia_connect.providers.google_browser.selectors import ENDPOINTS, SELECTORS
from solercia_connect.providers.google_meet import meet_transcript_speakers

# Names the vendor in the user-facing messages of the shared sign-in loop.
_PRODUCT = "Google"
_TRANSCRIPT_CAPABILITIES = frozenset(
    {Capability.MEETING_TRANSCRIPTS_LIST, Capability.MEETING_TRANSCRIPT_GET}
)
# Same bound as the official route's Drive export.
_MAX_TRANSCRIPT_BYTES = 10 * 1024 * 1024
_ENDPOINT = "calendar-web→docs-mobilebasic"


@dataclass(frozen=True, slots=True)
class _TranscriptReference:
    event_id: str
    attachment: TranscriptAttachment

    @property
    def source_id(self) -> str:
        return f"meet:{self.event_id}:{self.attachment.document_id}"


class GoogleBrowserAdapter:
    descriptor = ProviderDescriptor(
        "google.browser",
        ProviderFamily.GOOGLE,
        RouteKind.BROWSER,
        _TRANSCRIPT_CAPABILITIES,
    )

    def __init__(
        self,
        driver: GoogleBrowserDriver,
        *,
        profile_dir: Path,
        expected_account: str | None,
        navigation_timeout_ms: int = 30_000,
        state_poll_ms: int = 1_000,
        max_poll_cycles: int = 120,
        identity_attestation_cycles: int = 10,
        read_timeout_ms: int = 30_000,
        max_window_days: int = 31,
        max_events_per_day: int = 200,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if (
            min(
                navigation_timeout_ms,
                state_poll_ms,
                max_poll_cycles,
                identity_attestation_cycles,
                read_timeout_ms,
                max_window_days,
                max_events_per_day,
            )
            <= 0
        ):
            raise ValueError("Browser timeouts and bounds must be positive")
        self._driver = driver
        self._profile_dir = profile_dir.expanduser()
        self._expected_account = (
            _canonical_expected_account(expected_account) if expected_account else None
        )
        self._navigation_timeout_ms = navigation_timeout_ms
        self._state_poll_ms = state_poll_ms
        self._max_poll_cycles = max_poll_cycles
        self._identity_attestation_cycles = identity_attestation_cycles
        self._read_timeout_ms = read_timeout_ms
        self._max_window_days = max_window_days
        self._max_events_per_day = max_events_per_day
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
                    "Google browser session is ready; identity=google-account-button; "
                    f"selectors={SELECTORS.version}"
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
                "Interactive Google login was persisted and verified headlessly in the dedicated "
                "profile",
            )
        finally:
            self._close(verified)

    def execute(self, request: ConnectorRequest) -> tuple[MeetingTranscript, ...]:
        # Every refusal that needs no page happens before the browser opens.
        query, window = _transcript_window(request)
        days = window_days(window, max_days=self._max_window_days)
        requested_account = (
            _canonical_requested_account(query.account_id) if query.account_id is not None else None
        )
        limit = 1 if request.capability is Capability.MEETING_TRANSCRIPT_GET else query.limit
        machine = BrowserStateMachine()
        try:
            account = self._prepare(
                machine,
                interactive=False,
                headless=True,
                requested_account=requested_account,
            )
            machine.advance(BrowserState.READING)
            references = sorted(
                self._discover(days, window, account),
                key=lambda item: (item.attachment.recorded_at, item.source_id),
            )
            records = tuple(self._record(item, account) for item in references[:limit])
            machine.advance(BrowserState.COMPLETE)
            return records
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
        self._driver.open_persistent(self._profile_dir, headless=headless)
        self._driver.navigate(ENDPOINTS.calendar_home, timeout_ms=self._navigation_timeout_ms)
        return poll_until_ready(
            machine,
            _GoogleSessionProbe(self._driver, self._attest),
            interactive=interactive,
            product=_PRODUCT,
            boundaries=(self._expected_account, requested_account),
            max_poll_cycles=self._max_poll_cycles,
            state_poll_ms=self._state_poll_ms,
        )

    def _attest(self, machine: BrowserStateMachine, snapshot: GoogleSessionSnapshot) -> str:
        """Prove the account from the Google bar's account button, re-observing a bounded time.

        The button may render after the calendar itself, so an empty observation is retried;
        two different accounts, or none after the bound, fail closed.
        """
        for cycle in range(self._identity_attestation_cycles + 1):
            identities = frozenset(
                identity
                for hint in snapshot.account_hints
                for identity in canonical_account_identities(hint)
            )
            if len(identities) > 1:
                machine.advance(BrowserState.FAILED)
                raise ConnectorFailure(
                    ReasonCode.BROWSER_SELECTOR_MISMATCH,
                    "Google account button names more than one account; "
                    f"selectors={SELECTORS.version}",
                )
            if len(identities) == 1:
                return next(iter(identities))
            if cycle == self._identity_attestation_cycles:
                break
            self._driver.wait(self._state_poll_ms)
            snapshot = self._driver.snapshot(SELECTORS)
        machine.advance(BrowserState.FAILED)
        raise ConnectorFailure(
            ReasonCode.BROWSER_IDENTITY_UNAVAILABLE,
            f"Google account button did not name the signed-in account; selectors="
            f"{SELECTORS.version}",
        )

    def _discover(
        self,
        days: tuple[date, ...],
        window: DateWindow,
        account: str,
    ) -> list[_TranscriptReference]:
        found: dict[str, _TranscriptReference] = {}
        for day in days:
            observation = self._driver.calendar_day(
                ENDPOINTS.calendar_day(day),
                SELECTORS,
                timeout_ms=self._read_timeout_ms,
                max_events=self._max_events_per_day,
            )
            _require_page(observation.url, ENDPOINTS.calendar_host, "Google Calendar")
            if not observation.shell_visible:
                raise ConnectorFailure(
                    ReasonCode.BROWSER_SELECTOR_MISMATCH,
                    f"Google Calendar day view did not render; selectors={SELECTORS.version}",
                )
            for event in observation.events:
                attachments = [
                    attachment
                    for link in event.links
                    if (
                        attachment := transcript_attachment(
                            link.href, link.texts, endpoints=ENDPOINTS
                        )
                    )
                    is not None
                ]
                if not attachments:
                    continue
                decoded = event_reference(event.reference)
                if decoded is None:
                    raise ConnectorFailure(
                        ReasonCode.BROWSER_SELECTOR_MISMATCH,
                        "Google Calendar event with a transcript exposes no readable event "
                        f"reference; selectors={SELECTORS.version}",
                    )
                event_id, calendar_id = decoded
                # The official route reads the account's primary calendar only.
                if calendar_id != account:
                    continue
                for attachment in attachments:
                    if attachment.document_id in found or not (
                        window.start <= attachment.recorded_at < window.end
                    ):
                        continue
                    found[attachment.document_id] = _TranscriptReference(event_id, attachment)
        return list(found.values())

    def _record(self, reference: _TranscriptReference, account: str) -> MeetingTranscript:
        attachment = reference.attachment
        observation = self._driver.document_text(
            ENDPOINTS.document_text(attachment.document_id),
            timeout_ms=self._read_timeout_ms,
            max_bytes=_MAX_TRANSCRIPT_BYTES,
        )
        _require_page(observation.url, ENDPOINTS.document_host, "Google Docs")
        content = normalize_document_text(observation.text)
        if not has_transcript_section(content):
            raise ConnectorFailure(
                ReasonCode.BROWSER_SELECTOR_MISMATCH,
                "Google document page does not show a Meet transcript section; "
                f"selectors={SELECTORS.version}",
            )
        speakers = meet_transcript_speakers(content)
        # Two fields differ from the official route, because this surface does not show them:
        # recorded_at is the stamp Meet writes in the document name, not the event start, and
        # the organizer's address is not on the page, so it stays unset rather than guessed.
        return MeetingTranscript(
            reference.source_id,
            reference.event_id,
            content,
            "text/plain",
            len(speakers),
            None,
            Provenance(
                self.descriptor.provider_id,
                self.descriptor.family,
                self.descriptor.route,
                reference.source_id,
                self._clock(),
                account,
                f"{_ENDPOINT};selectors={SELECTORS.version}",
            ),
            display_name=attachment.display_name,
            recorded_at=attachment.recorded_at,
            item_id=attachment.document_id,
            organizer=None,
            speakers=speakers,
        )

    def _close(self, machine: BrowserStateMachine) -> None:
        close_session(machine, self._driver.close)


class _GoogleSessionProbe:
    """How the Google pages show sign-in, a challenge and a ready calendar to the core loop."""

    def __init__(
        self,
        driver: GoogleBrowserDriver,
        attest: Callable[[BrowserStateMachine, GoogleSessionSnapshot], str],
    ) -> None:
        self._driver = driver
        self._attest = attest

    def observe(self) -> GoogleSessionSnapshot:
        return self._driver.snapshot(SELECTORS)

    def phase(self, snapshot: GoogleSessionSnapshot) -> SessionPhase:
        if snapshot.mfa_visible:
            return SessionPhase.MFA_REQUIRED
        if snapshot.login_visible:
            return SessionPhase.LOGIN_REQUIRED
        if snapshot.shell_visible:
            return SessionPhase.AUTHENTICATED
        return SessionPhase.PENDING

    def attest(self, machine: BrowserStateMachine, snapshot: GoogleSessionSnapshot) -> str:
        return self._attest(machine, snapshot)

    def wait(self, milliseconds: int) -> None:
        self._driver.wait(milliseconds)


def _transcript_window(request: ConnectorRequest) -> tuple[TranscriptQuery, DateWindow]:
    if request.capability not in _TRANSCRIPT_CAPABILITIES or not isinstance(
        request.payload, TranscriptQuery
    ):
        raise ConnectorFailure(
            ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED,
            f"Google browser route does not implement {request.capability.value}",
        )
    query = request.payload
    if query.drive_id is not None or query.item_id is not None:
        raise ConnectorFailure(
            ReasonCode.INVALID_INPUT,
            "Google Meet transcripts se direccionan por meeting_id o window; los selectores "
            "de drive item son exclusivos de la ruta Microsoft",
        )
    if query.meeting_id is not None:
        raise ConnectorFailure(
            ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED,
            "The Google Meet browser route discovers transcripts by window only; "
            "--meeting-id needs the official route",
        )
    if query.window is None:
        raise ConnectorFailure(
            ReasonCode.INVALID_INPUT,
            "The Google Meet browser route needs an exact --start/--end window",
        )
    return query, query.window


def _require_page(url: str, host: str, product: str) -> None:
    """Fail closed when a read landed anywhere but the page it asked for."""
    try:
        observed = (urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        observed = ""
    if observed == ENDPOINTS.sign_in_host:
        raise ConnectorFailure(
            ReasonCode.BROWSER_LOGIN_REQUIRED,
            f"{product} sent the browser session back to Google sign-in",
        )
    if observed != host:
        raise ConnectorFailure(
            ReasonCode.BROWSER_SELECTOR_MISMATCH,
            f"{product} read did not stay on {host}; selectors={SELECTORS.version}",
        )


def _canonical_expected_account(value: str) -> str:
    identity = unique_canonical_account_identity(value, exact=True)
    if identity is None:
        raise ValueError("Expected Google account must be one unambiguous email identity")
    return identity


def _canonical_requested_account(value: str) -> str:
    identity = unique_canonical_account_identity(value, exact=True)
    if identity is None:
        raise ConnectorFailure(
            ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
            "Requested Google account is not one unambiguous account identity",
        )
    return identity
