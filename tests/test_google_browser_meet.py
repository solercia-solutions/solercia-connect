"""Google Meet transcripts over the supervised browser route.

Decision 2026-10-25, D1, as made precise on 2026-10-26: the Calendar web day view and each event's
detail discover the transcript attachment, and the Docs ``mobilebasic`` view yields its text. The
fixtures below are synthetic: no real meeting, document, account or person.
"""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from solercia_connect.auth import BearerTokenProvider
from solercia_connect.domain import (
    CalendarQuery,
    Capability,
    ConnectorRequest,
    DateWindow,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    TranscriptQuery,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import BrowserProviderAdapter, TransportResponse
from solercia_connect.providers.google_browser import GoogleBrowserAdapter
from solercia_connect.providers.google_browser.driver import (
    AttachmentLink,
    CalendarDayObservation,
    CalendarEventObservation,
    DocumentObservation,
    GoogleSessionSnapshot,
)
from solercia_connect.providers.google_browser.parsing import (
    document_id_from_href,
    event_reference,
    has_transcript_section,
    normalize_document_text,
    transcript_attachment,
    window_days,
)
from solercia_connect.providers.google_browser.selectors import (
    ENDPOINTS,
    SELECTOR_VERSION,
    SELECTORS,
)
from solercia_connect.providers.google_meet import GoogleMeetAdapter
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport, json_response

ACCOUNT = "user@example.com"
PROFILE = Path("/tmp/solercia-test-google-profile")
BOGOTA = timezone(timedelta(hours=-5))
WINDOW = DateWindow(datetime(2026, 10, 26, tzinfo=BOGOTA), datetime(2026, 10, 27, tzinfo=BOGOTA))
DOC_A = "1AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTt_-0"
DOC_B = "1ZzYyXxWwVvUuTtSsRrQqPpOoNnMmLlKkJjIiHhGg_-9"
TITLE_A = "Revisión de avance - Proyecto Alfa - 2026/10/26 14:58 GMT-05:00 - Transcripción"
TITLE_B = "Weekly sync - 2026/10/26 09:00 GMT-05:00 - Transcript"
TRANSCRIPT_TEXT = (
    "Revisión de avance - Proyecto Alfa - 2026/10/26 14:58 GMT-05:00 - Transcripción\n"
    "\n"
    "Asistentes\n"
    "Hablante Uno, Hablante Dos\n"
    "\n"
    "Transcripción\n"
    "Hablante Uno: Buenas tardes, empezamos con el avance.\n"
    "\n"
    "Hablante Dos: Listo, comparto la pantalla.\n"
    "\n"
    "00:05:00\n"
    "\n"
    "Hablante Uno: Cerramos con los pendientes.\n"
    "\n"
    "La reunión finalizó después de 00:06:10\n"
    "\n"
    "Esta transcripción editable se generó por computadora y puede contener errores."
)


def _reference(event_id: str, calendar_id: str = ACCOUNT, *, urlsafe: bool = True) -> str:
    raw = f"{event_id} {calendar_id}".encode()
    encoded = base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)
    return encoded.decode().rstrip("=")


def _doc_link(document_id: str, title: str) -> AttachmentLink:
    return AttachmentLink(
        f"https://docs.google.com/document/d/{document_id}/edit?usp=drive_web", (title,)
    )


def _recording_link(title: str) -> AttachmentLink:
    return AttachmentLink("https://drive.google.com/file/d/1recording/view?usp=drive_web", (title,))


def _event(event_id: str | None, *links: AttachmentLink) -> CalendarEventObservation:
    return CalendarEventObservation(_reference(event_id) if event_id else None, links)


def _day(day: date, *events: CalendarEventObservation, shell: bool = True) -> tuple[str, Any]:
    url = ENDPOINTS.calendar_day(day)
    return url, CalendarDayObservation(url, shell, events)


def _document(document_id: str, text: str = TRANSCRIPT_TEXT) -> tuple[str, DocumentObservation]:
    url = ENDPOINTS.document_text(document_id)
    return url, DocumentObservation(url, text)


def _ready(*hints: str) -> GoogleSessionSnapshot:
    return GoogleSessionSnapshot(
        "https://calendar.google.com/calendar/u/0/r",
        hints or (f"Cuenta de Google: Usuario de prueba ({ACCOUNT})",),
        False,
        False,
        True,
    )


def _login() -> GoogleSessionSnapshot:
    return GoogleSessionSnapshot(
        "https://accounts.google.com/v3/signin/identifier", (), True, False, False
    )


class FakeGoogleDriver:
    def __init__(
        self,
        snapshots: list[GoogleSessionSnapshot] | None = None,
        days: dict[str, CalendarDayObservation] | None = None,
        documents: dict[str, DocumentObservation | Exception] | None = None,
    ) -> None:
        self.snapshots = deque(snapshots or [_ready()])
        self.days = days or {}
        self.documents = documents or {}
        self.open_calls: list[tuple[Path, bool]] = []
        self.navigations: list[str] = []
        self.day_reads: list[str] = []
        self.document_reads: list[str] = []
        self.waits: list[int] = []
        self.close_calls = 0

    def open_persistent(self, profile_dir: Path, *, headless: bool) -> None:
        self.open_calls.append((profile_dir, headless))

    def navigate(self, url: str, *, timeout_ms: int) -> None:
        self.navigations.append(url)

    def snapshot(self, selectors: Any) -> GoogleSessionSnapshot:
        assert selectors is SELECTORS
        return self.snapshots.popleft() if len(self.snapshots) > 1 else self.snapshots[0]

    def wait(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)

    def calendar_day(
        self, url: str, selectors: Any, *, timeout_ms: int, max_events: int
    ) -> CalendarDayObservation:
        self.day_reads.append(url)
        return self.days.get(url, CalendarDayObservation(url, True, ()))

    def document_text(self, url: str, *, timeout_ms: int, max_bytes: int) -> DocumentObservation:
        self.document_reads.append(url)
        outcome = self.documents[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self.close_calls += 1


def _adapter(driver: FakeGoogleDriver, **overrides: Any) -> GoogleBrowserAdapter:
    options: dict[str, Any] = {
        "profile_dir": PROFILE,
        "expected_account": ACCOUNT,
        "state_poll_ms": 1,
        "max_poll_cycles": 3,
        "identity_attestation_cycles": 2,
        "clock": lambda: NOW,
        **overrides,
    }
    return GoogleBrowserAdapter(driver, **options)


def _request(
    capability: Capability = Capability.MEETING_TRANSCRIPTS_LIST,
    **query: Any,
) -> ConnectorRequest:
    payload = TranscriptQuery(**{"window": WINDOW, "account_id": ACCOUNT, **query})
    return ConnectorRequest.create("google.meet", capability, payload)


def _meeting_driver(**extra_documents: DocumentObservation) -> FakeGoogleDriver:
    return FakeGoogleDriver(
        days=dict(
            [
                _day(
                    date(2026, 10, 26),
                    _event(
                        "evt123abc",
                        _recording_link(TITLE_A.replace("Transcripción", "Grabación")),
                        _doc_link(DOC_A, TITLE_A),
                        _doc_link(DOC_B.replace("9", "8"), "Revisión de avance - Notas de Gemini"),
                    ),
                    _event("evt456def", _doc_link(DOC_B, TITLE_B)),
                    _event(None),
                )
            ]
        ),
        documents=dict([_document(DOC_A), _document(DOC_B), *extra_documents.items()]),
    )


# --- Records ---------------------------------------------------------------------------------


def test_a_window_lists_each_transcript_once_in_time_order() -> None:
    driver = _meeting_driver()

    records = _adapter(driver).execute(_request())

    assert [record.id for record in records] == [
        f"meet:evt456def:{DOC_B}",
        f"meet:evt123abc:{DOC_A}",
    ]
    record = records[1]
    assert record.meeting_id == "evt123abc"
    assert record.item_id == DOC_A
    assert record.display_name == TITLE_A
    assert record.recorded_at == datetime(2026, 10, 26, 14, 58, tzinfo=BOGOTA)
    assert (record.content_format, record.last_cue_seconds) == ("text/plain", None)
    assert (record.speakers, record.speaker_count) == (("Hablante Uno", "Hablante Dos"), 2)
    assert record.content == TRANSCRIPT_TEXT
    assert record.organizer is None and record.drive_id is None
    assert record.provenance.provider_id == "google.browser"
    assert record.provenance.family is ProviderFamily.GOOGLE
    assert record.provenance.route is RouteKind.BROWSER
    assert record.provenance.account_id == ACCOUNT
    assert record.provenance.fetched_at == NOW
    assert record.provenance.endpoint == (
        f"calendar-web→docs-mobilebasic;selectors={SELECTOR_VERSION}"
    )
    assert driver.open_calls == [(PROFILE, True)]
    assert driver.navigations == [ENDPOINTS.calendar_home]
    assert driver.close_calls == 1


def test_the_browser_record_matches_the_official_record_but_for_provenance_and_organizer() -> None:
    """The same meeting, served by each Google route.

    The fixture starts the event at the minute Meet stamps on the document, because the two routes
    take ``recorded_at`` from different places: the official one from the event start, this one
    from the document name. The organizer's address is not on the page, so it stays unset.
    """
    transport = FakeTransport(
        json_response(200, {"id": ACCOUNT}),
        json_response(
            200,
            {
                "items": [
                    {
                        "id": "evt123abc",
                        "summary": "Revisión de avance - Proyecto Alfa",
                        "organizer": {"email": "organizer@example.com"},
                        "start": {"dateTime": "2026-10-26T14:58:00-05:00"},
                        "attachments": [
                            {
                                "fileId": DOC_A,
                                "title": TITLE_A.replace("Transcripción", "Transcript"),
                                "mimeType": "application/vnd.google-apps.document",
                            }
                        ],
                    }
                ]
            },
        ),
        TransportResponse(200, {"Content-Type": "text/plain"}, TRANSCRIPT_TEXT.encode()),
    )
    official = GoogleMeetAdapter(
        transport,
        BearerTokenProvider(SecretValue("google-secret")),
        expected_account=ACCOUNT,
        clock=lambda: NOW,
    ).list_transcripts(TranscriptQuery(window=WINDOW, account_id=ACCOUNT))
    title = TITLE_A.replace("Transcripción", "Transcript")
    url, day = _day(date(2026, 10, 26), _event("evt123abc", _doc_link(DOC_A, title)))
    browser = _adapter(
        FakeGoogleDriver(days={url: day}, documents=dict([_document(DOC_A)]))
    ).execute(_request())

    assert len(official) == len(browser) == 1
    assert official[0].organizer == "organizer@example.com"
    assert replace(official[0], provenance=browser[0].provenance, organizer=None) == browser[0]


def test_get_reads_only_the_earliest_transcript() -> None:
    driver = _meeting_driver()

    records = _adapter(driver).execute(_request(Capability.MEETING_TRANSCRIPT_GET))

    assert [record.item_id for record in records] == [DOC_B]
    assert driver.document_reads == [ENDPOINTS.document_text(DOC_B)]


def test_the_limit_bounds_how_many_documents_are_read() -> None:
    driver = _meeting_driver()

    records = _adapter(driver).execute(_request(limit=1))

    assert len(records) == 1
    assert len(driver.document_reads) == 1


def test_an_empty_window_is_empty_and_reads_every_candidate_day() -> None:
    driver = FakeGoogleDriver()

    assert _adapter(driver).execute(_request()) == ()
    # The calendar's own time zone is unknown, so one day is read on each side of the UTC dates.
    assert driver.day_reads == [
        ENDPOINTS.calendar_day(date(2026, 10, day)) for day in (25, 26, 27, 28)
    ]
    assert driver.document_reads == []


def test_the_window_is_exact_and_end_exclusive() -> None:
    at_end = "Cierre - 2026/10/27 00:00 GMT-05:00 - Transcript"
    at_start = "Apertura - 2026/10/26 00:00 GMT-05:00 - Transcript"
    url, day = _day(
        date(2026, 10, 26),
        _event("evtstart", _doc_link(DOC_A, at_start)),
        _event("evtend", _doc_link(DOC_B, at_end)),
    )
    driver = FakeGoogleDriver(days={url: day}, documents=dict([_document(DOC_A)]))

    records = _adapter(driver).execute(_request())

    assert [record.meeting_id for record in records] == ["evtstart"]


def test_a_transcript_seen_on_two_days_is_listed_once() -> None:
    first = _day(date(2026, 10, 26), _event("evt123abc", _doc_link(DOC_A, TITLE_A)))
    second = _day(date(2026, 10, 27), _event("evt123abc", _doc_link(DOC_A, TITLE_A)))
    driver = FakeGoogleDriver(days=dict([first, second]), documents=dict([_document(DOC_A)]))

    assert len(_adapter(driver).execute(_request())) == 1


def test_events_of_other_calendars_are_left_out_like_the_official_route() -> None:
    other = CalendarEventObservation(
        _reference("evtshared", "team@group.calendar.google.com"), (_doc_link(DOC_A, TITLE_A),)
    )
    url, day = _day(date(2026, 10, 26), other)
    driver = FakeGoogleDriver(days={url: day})

    assert _adapter(driver).execute(_request()) == ()
    assert driver.document_reads == []


def test_document_text_is_normalized_only_in_line_endings_and_outer_space() -> None:
    raw = "﻿" + TRANSCRIPT_TEXT.replace("\n", "\r\n") + "\r\n\r\n"
    url, day = _day(date(2026, 10, 26), _event("evt123abc", _doc_link(DOC_A, TITLE_A)))
    driver = FakeGoogleDriver(days={url: day}, documents=dict([_document(DOC_A, raw)]))

    (record,) = _adapter(driver).execute(_request())

    assert record.content == TRANSCRIPT_TEXT


# --- Fail closed -----------------------------------------------------------------------------


def _failure(driver: FakeGoogleDriver, request: ConnectorRequest | None = None) -> ConnectorFailure:
    with pytest.raises(ConnectorFailure) as raised:
        _adapter(driver).execute(request or _request())
    assert driver.close_calls == (1 if driver.open_calls else 0)
    return raised.value


def test_a_day_view_that_did_not_render_is_a_selector_mismatch_not_an_empty_day() -> None:
    url, day = _day(date(2026, 10, 25), shell=False)

    failure = _failure(FakeGoogleDriver(days={url: day}))

    assert failure.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH


@pytest.mark.parametrize(
    ("landed", "reason"),
    [
        ("https://accounts.google.com/v3/signin/identifier", ReasonCode.BROWSER_LOGIN_REQUIRED),
        ("https://www.google.com/sorry", ReasonCode.BROWSER_SELECTOR_MISMATCH),
        ("not a url [", ReasonCode.BROWSER_SELECTOR_MISMATCH),
    ],
)
def test_a_day_read_that_left_calendar_fails_closed(landed: str, reason: ReasonCode) -> None:
    url = ENDPOINTS.calendar_day(date(2026, 10, 25))
    driver = FakeGoogleDriver(days={url: CalendarDayObservation(landed, True, ())})

    assert _failure(driver).reason is reason


def test_an_event_with_a_transcript_but_no_readable_reference_fails_closed() -> None:
    unreadable = CalendarEventObservation("not-base64-!!", (_doc_link(DOC_A, TITLE_A),))
    url, day = _day(date(2026, 10, 26), unreadable)

    failure = _failure(FakeGoogleDriver(days={url: day}))

    assert failure.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH
    assert "event reference" in failure.safe_message


def test_a_transcript_name_without_the_meet_stamp_fails_closed() -> None:
    url, day = _day(
        date(2026, 10, 26), _event("evt123abc", _doc_link(DOC_A, "Revisión - Transcript"))
    )

    assert _failure(FakeGoogleDriver(days={url: day})).reason is (
        ReasonCode.BROWSER_SELECTOR_MISMATCH
    )


@pytest.mark.parametrize(
    ("landed", "text", "reason"),
    [
        (
            "https://accounts.google.com/ServiceLogin",
            TRANSCRIPT_TEXT,
            ReasonCode.BROWSER_LOGIN_REQUIRED,
        ),
        (None, "Necesitas permiso\nSolicita acceso", ReasonCode.BROWSER_SELECTOR_MISMATCH),
    ],
)
def test_a_document_page_that_is_not_a_transcript_fails_closed(
    landed: str | None, text: str, reason: ReasonCode
) -> None:
    url, day = _day(date(2026, 10, 26), _event("evt123abc", _doc_link(DOC_A, TITLE_A)))
    document_url = ENDPOINTS.document_text(DOC_A)
    driver = FakeGoogleDriver(
        days={url: day},
        documents={document_url: DocumentObservation(landed or document_url, text)},
    )

    assert _failure(driver).reason is reason


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        (
            {"meeting_id": "evt123abc", "window": None},
            ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED,
        ),
        ({"drive_id": "b!drive", "item_id": "01ITEM"}, ReasonCode.INVALID_INPUT),
        ({"window": None}, ReasonCode.INVALID_INPUT),
        (
            {"window": DateWindow(WINDOW.start, WINDOW.start + timedelta(days=32))},
            ReasonCode.INVALID_INPUT,
        ),
    ],
)
def test_selectors_this_route_cannot_serve_fail_before_any_browser_opens(
    query: dict[str, Any], reason: ReasonCode
) -> None:
    driver = FakeGoogleDriver()

    failure = _failure(driver, _request(**query))

    assert failure.reason is reason
    assert driver.open_calls == []


def test_a_capability_outside_meet_is_not_implemented() -> None:
    driver = FakeGoogleDriver()
    request = ConnectorRequest.create(
        "google.calendar", Capability.CALENDAR_EVENTS_LIST, CalendarQuery(window=WINDOW)
    )

    assert _failure(driver, request).reason is ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED


# --- Identity --------------------------------------------------------------------------------


def test_another_signed_in_account_is_a_mismatch_with_zero_reads() -> None:
    driver = FakeGoogleDriver([_ready("Google Account: Otra persona (other@example.com)")])

    assert _failure(driver).reason is ReasonCode.BROWSER_ACCOUNT_MISMATCH
    assert driver.day_reads == []


def test_a_requested_account_outside_the_connection_is_refused() -> None:
    driver = FakeGoogleDriver()
    request = _request(account_id="other@example.com")

    assert _failure(driver, request).reason is ReasonCode.BROWSER_ACCOUNT_MISMATCH


def test_an_ambiguous_requested_account_is_an_authorization_boundary_violation() -> None:
    request = _request(account_id="a@example.com, b@example.com")

    assert _failure(FakeGoogleDriver(), request).reason is (
        ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    )


def test_an_account_button_naming_two_accounts_is_a_selector_mismatch() -> None:
    driver = FakeGoogleDriver([_ready(f"Cuenta de Google: ({ACCOUNT})", "x (other@example.com)")])

    assert _failure(driver).reason is ReasonCode.BROWSER_SELECTOR_MISMATCH


def test_identity_is_re_observed_a_bounded_number_of_times() -> None:
    driver = FakeGoogleDriver([_ready(""), _ready(""), _ready("")])

    failure = _failure(driver)

    assert failure.reason is ReasonCode.BROWSER_IDENTITY_UNAVAILABLE
    assert driver.waits == [1, 1]


def test_an_account_button_that_renders_late_still_proves_identity() -> None:
    driver = _meeting_driver()
    driver.snapshots = deque([_ready(""), _ready()])

    assert len(_adapter(driver).execute(_request())) == 2


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (_login(), ReasonCode.BROWSER_LOGIN_REQUIRED),
        (
            GoogleSessionSnapshot(
                "https://accounts.google.com/challenge/totp", (), False, True, False
            ),
            ReasonCode.BROWSER_MFA_REQUIRED,
        ),
    ],
)
def test_a_headless_read_never_waits_for_the_user(
    snapshot: GoogleSessionSnapshot, reason: ReasonCode
) -> None:
    assert _failure(FakeGoogleDriver([snapshot])).reason is reason


def test_interactive_login_waits_for_the_user_then_verifies_headlessly() -> None:
    driver = FakeGoogleDriver([_login(), _login(), _ready(), _ready()])

    report = _adapter(driver).interactive_login()

    assert report.account_id == ACCOUNT
    assert driver.open_calls == [(PROFILE, False), (PROFILE, True)]
    assert driver.close_calls == 2


def test_health_reports_the_proven_account_and_the_selector_version() -> None:
    driver = FakeGoogleDriver()

    report = _adapter(driver).health()

    assert report.account_id == ACCOUNT
    assert SELECTOR_VERSION in report.detail
    assert driver.close_calls == 1


def test_the_adapter_answers_the_vendor_neutral_browser_port() -> None:
    adapter = _adapter(FakeGoogleDriver())

    assert isinstance(adapter, BrowserProviderAdapter)
    assert adapter.descriptor.capabilities == {
        Capability.MEETING_TRANSCRIPTS_LIST,
        Capability.MEETING_TRANSCRIPT_GET,
    }


@pytest.mark.parametrize(
    "overrides",
    [{"read_timeout_ms": 0}, {"max_window_days": 0}, {"expected_account": "not an address"}],
)
def test_invalid_construction_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _adapter(FakeGoogleDriver(), **overrides)


# --- Parsing ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (TITLE_A, datetime(2026, 10, 26, 14, 58, tzinfo=BOGOTA)),
        (TITLE_B, datetime(2026, 10, 26, 9, 0, tzinfo=BOGOTA)),
        (
            "Sync - 2026/10/26 20:30 GMT+05:30 - Transcript",
            datetime(2026, 10, 26, 15, 0, tzinfo=UTC),
        ),
        ("Sync - 2026/10/26 15:00 GMT - Transcript", datetime(2026, 10, 26, 15, 0, tzinfo=UTC)),
    ],
)
def test_the_meet_document_name_yields_its_time_stamp(text: str, expected: datetime) -> None:
    href = f"https://docs.google.com/document/d/{DOC_A}/edit?usp=drive_web"

    attachment = transcript_attachment(href, (text,), endpoints=ENDPOINTS)

    assert attachment is not None
    assert attachment.recorded_at == expected
    assert attachment.document_id == DOC_A


def test_a_messy_link_text_falls_back_to_its_label() -> None:
    href = f"https://docs.google.com/document/d/{DOC_A}/edit"

    attachment = transcript_attachment(href, ("Documento\nadjunto", TITLE_B), endpoints=ENDPOINTS)

    assert attachment is not None and attachment.display_name == TITLE_B


@pytest.mark.parametrize(
    "text",
    [
        "Proyecto - Notas de Gemini",
        "Transcript review plan",
        "Revisión de transcripciones - 2026/10/26 10:00 GMT-05:00 - Grabación",
    ],
)
def test_other_documents_are_not_transcripts(text: str) -> None:
    href = f"https://docs.google.com/document/d/{DOC_A}/edit"

    assert transcript_attachment(href, (text,), endpoints=ENDPOINTS) is None


@pytest.mark.parametrize(
    "text",
    [
        "Sync - 2026/13/40 10:00 GMT-05:00 - Transcript",
        "Sync - 2026/10/26 10:00 GMT-15:00 - Transcript",
        "Sync - 26/10/2026 - Transcripción",
    ],
)
def test_a_transcript_name_with_a_broken_stamp_fails_closed(text: str) -> None:
    href = f"https://docs.google.com/document/d/{DOC_A}/edit"

    with pytest.raises(ConnectorFailure) as raised:
        transcript_attachment(href, (text,), endpoints=ENDPOINTS)

    assert raised.value.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH


def test_a_transcript_name_on_a_link_that_is_not_one_document_fails_closed() -> None:
    with pytest.raises(ConnectorFailure) as raised:
        transcript_attachment(
            "https://docs.google.com/spreadsheets/d/x/edit", (TITLE_B,), endpoints=ENDPOINTS
        )

    assert raised.value.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        (f"https://docs.google.com/document/d/{DOC_A}/edit?usp=drive_web", DOC_A),
        (f"https://docs.google.com/document/u/0/d/{DOC_A}/edit", DOC_A),
        (f"https://DOCS.google.com:443/document/d/{DOC_A}", DOC_A),
        (f"http://docs.google.com/document/d/{DOC_A}/edit", None),
        (f"https://docs.google.com.evil.test/document/d/{DOC_A}/edit", None),
        (f"https://user:pass@docs.google.com/document/d/{DOC_A}/edit", None),
        (f"https://docs.google.com:8443/document/d/{DOC_A}/edit", None),
        ("https://docs.google.com:notaport/document/d/x/edit", None),
        ("https://docs.google.com/document/d/short/edit", None),
        ("https://drive.google.com/file/d/1recording/view", None),
    ],
)
def test_only_one_https_docs_document_yields_an_id(href: str, expected: str | None) -> None:
    assert document_id_from_href(href, endpoints=ENDPOINTS) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (_reference("evt123abc"), ("evt123abc", ACCOUNT)),
        (
            _reference("evt_20261026T200000Z", "USER@Example.com", urlsafe=False),
            ("evt_20261026T200000Z", ACCOUNT),
        ),
        (None, None),
        ("", None),
        ("!!!!", None),
        (base64.b64encode(b"\xff\xfe\xfd\xfc").decode(), None),
        (base64.b64encode(b"onlyonepart").decode(), None),
        (base64.b64encode(b"bad/id user@example.com").decode(), None),
        (base64.b64encode(b"evt123 not-a-calendar").decode(), None),
        (base64.b64encode(b"a b c").decode(), None),
    ],
)
def test_the_event_reference_decodes_to_event_and_calendar(
    value: str | None, expected: tuple[str, str] | None
) -> None:
    assert event_reference(value) == expected


def test_text_normalization_and_section_marker() -> None:
    assert normalize_document_text("﻿ a\r\nb\rc \n") == "a\nb\nc"
    assert has_transcript_section("x\n  Transcript \ny") is True
    assert has_transcript_section("Transcripción: no es un encabezado") is False


def test_window_days_add_one_day_each_side_and_are_bounded() -> None:
    window = DateWindow(
        datetime(2026, 10, 26, 23, 0, tzinfo=UTC), datetime(2026, 10, 27, 0, 0, tzinfo=UTC)
    )

    assert window_days(window, max_days=31) == (
        date(2026, 10, 25),
        date(2026, 10, 26),
        date(2026, 10, 27),
    )
    with pytest.raises(ConnectorFailure) as raised:
        window_days(DateWindow(window.start, window.start + timedelta(days=2)), max_days=1)
    assert raised.value.reason is ReasonCode.INVALID_INPUT


def test_endpoints_build_only_allowlisted_urls() -> None:
    assert ENDPOINTS.calendar_day(date(2026, 1, 5)) == (
        "https://calendar.google.com/calendar/u/0/r/day/2026/1/5"
    )
    assert ENDPOINTS.document_text(DOC_A) == (
        f"https://docs.google.com/document/d/{DOC_A}/mobilebasic"
    )
    with pytest.raises(ValueError):
        ENDPOINTS.document_text("../../evil")
