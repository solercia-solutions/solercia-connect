"""The Playwright driver of the Google Meet browser route, against a scripted page.

The page below imitates only the Playwright calls the driver makes. It proves what the driver
observes and refuses; it does not prove Google's real pages, which only the live gate can.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.google_browser.driver import PlaywrightGoogleBrowserDriver
from solercia_connect.providers.google_browser.selectors import ENDPOINTS, SELECTORS

ACCOUNT_LABEL = "Cuenta de Google: Usuario de prueba (user@example.com)"
DAY_URL = "https://calendar.google.com/calendar/u/0/r/day/2026/10/26"
DOC_URL = "https://docs.google.com/document/d/1AaBbCcDdEeFfGgHhIiJj/edit?usp=drive_web"


class Element:
    def __init__(
        self,
        *,
        attributes: dict[str, str] | None = None,
        text: str = "",
        visible: bool = True,
        children: dict[str, list[Element]] | None = None,
        on_click: Callable[[], None] | None = None,
    ) -> None:
        self.attributes = attributes or {}
        self.text = text
        self.visible = visible
        self.children = children or {}
        self.on_click = on_click


class Locator:
    def __init__(self, elements: list[Element]) -> None:
        self._elements = elements

    def count(self) -> int:
        return len(self._elements)

    def nth(self, index: int) -> Locator:
        return Locator([self._elements[index]])

    @property
    def first(self) -> Locator:
        return Locator(self._elements[:1])

    def is_visible(self) -> bool:
        return bool(self._elements) and self._elements[0].visible

    def get_attribute(self, name: str) -> str | None:
        return self._elements[0].attributes.get(name)

    def inner_text(self, timeout: int | None = None) -> Any:
        if isinstance(self._elements[0].text, Exception):
            raise self._elements[0].text
        return self._elements[0].text

    def click(self, timeout: int | None = None) -> None:
        handler = self._elements[0].on_click
        if handler is None:
            raise RuntimeError("element is not clickable")
        handler()

    def wait_for(self, *, state: str, timeout: int) -> None:
        if (state == "visible") != self.is_visible():
            raise TimeoutError(state)

    def locator(self, selector: str) -> Locator:
        found = [
            child for element in self._elements for child in element.children.get(selector, [])
        ]
        return Locator(found)


class DialogLocator(Locator):
    """Like a real locator, re-queries the page: the detail it names opens and closes."""

    def __init__(self, page: Page) -> None:
        self._owner = page

    @property
    def _elements(self) -> list[Element]:  # type: ignore[override]
        return [self._owner.dialog] if self._owner.dialog is not None else []

    @property
    def first(self) -> Locator:
        return self


class Page:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.elements: dict[str, list[Element]] = {}
        self.dialog: Element | None = None
        self.dialog_closes = True
        self.redirects: dict[str, str] = {}
        self.visits: list[str] = []
        self.keys: list[str] = []
        self.keyboard = SimpleNamespace(press=self._press)

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.visits.append(url)
        self.url = self.redirects.get(url, url)

    def locator(self, selector: str) -> Locator:
        if selector == SELECTORS.event_detail:
            return DialogLocator(self)
        return Locator(self.elements.get(selector, []))

    def _press(self, key: str) -> None:
        self.keys.append(key)
        if key == "Escape" and self.dialog_closes:
            self.dialog = None


def _driver(page: Page) -> PlaywrightGoogleBrowserDriver:
    driver = PlaywrightGoogleBrowserDriver()
    driver._page = page  # the open page, without launching Chromium
    return driver


def _chip(page: Page, reference: str | None, *links: Element) -> Element:
    detail = Element(children={SELECTORS.document_links: list(links)})

    def open_detail() -> None:
        page.dialog = detail

    return Element(
        attributes={SELECTORS.event_reference_attribute: reference} if reference else {},
        on_click=open_detail,
    )


def _calendar_page(*chips: Callable[[Page], Element]) -> Page:
    page = Page()
    page.elements[SELECTORS.calendar_shells[0]] = [Element()]
    page.elements[SELECTORS.event_chips] = [build(page) for build in chips]
    return page


def _link(text: str, *, href: str = DOC_URL, label: str | None = None) -> Element:
    attributes = {"href": href}
    if label is not None:
        attributes["aria-label"] = label
    return Element(attributes=attributes, text=text)


# --- Navigation and session ------------------------------------------------------------------


def test_navigation_outside_calendar_and_docs_is_refused_before_the_page_moves() -> None:
    page = Page()

    with pytest.raises(ConnectorFailure) as raised:
        _driver(page).navigate("https://drive.google.com/file/d/x/view", timeout_ms=10)

    assert raised.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert page.visits == []


def test_open_persistent_uses_the_core_session(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[tuple[Path, bool]] = []
    driver = PlaywrightGoogleBrowserDriver()
    monkeypatch.setattr(
        driver,
        "_open_persistent_context",
        lambda profile, *, headless: opened.append((profile, headless)),
    )

    driver.open_persistent(Path("/tmp/profile"), headless=True)

    assert opened == [(Path("/tmp/profile"), True)]


def test_a_ready_calendar_shows_its_shell_and_the_account_button() -> None:
    page = _calendar_page()
    page.url = ENDPOINTS.calendar_home
    page.elements[SELECTORS.identity_controls[0]] = [
        Element(attributes={"aria-label": ACCOUNT_LABEL, "title": ACCOUNT_LABEL})
    ]

    snapshot = _driver(page).snapshot(SELECTORS)

    assert snapshot.account_hints == (ACCOUNT_LABEL,)
    assert (snapshot.login_visible, snapshot.mfa_visible, snapshot.shell_visible) == (
        False,
        False,
        True,
    )


@pytest.mark.parametrize(
    ("url", "login", "mfa"),
    [
        ("https://accounts.google.com/v3/signin/identifier", True, False),
        ("https://accounts.google.com/v3/signin/challenge/pwd", False, True),
    ],
)
def test_the_sign_in_pages_are_login_or_challenge(url: str, login: bool, mfa: bool) -> None:
    page = Page(url)
    page.elements[SELECTORS.identity_controls[0]] = [Element(attributes={"aria-label": "x"})]

    snapshot = _driver(page).snapshot(SELECTORS)

    assert (snapshot.login_visible, snapshot.mfa_visible, snapshot.shell_visible) == (
        login,
        mfa,
        False,
    )
    assert snapshot.account_hints == ()


# --- Calendar day ----------------------------------------------------------------------------


def test_a_day_opens_each_event_once_reads_its_docs_links_and_closes_it() -> None:
    transcript = _link(
        "Revisión - 2026/10/26 14:58 GMT-05:00 - Transcripción",
        label="Archivo adjunto: Revisión - 2026/10/26 14:58 GMT-05:00 - Transcripción",
    )
    page = _calendar_page(
        lambda page: _chip(page, "ref-1", transcript, _link("", href="")),
        lambda page: _chip(page, "ref-1", transcript),
        lambda page: _chip(page, None),
    )

    observation = _driver(page).calendar_day(DAY_URL, SELECTORS, timeout_ms=10, max_events=5)

    assert observation.url == DAY_URL and observation.shell_visible is True
    assert [event.reference for event in observation.events] == ["ref-1", None]
    (link,) = observation.events[0].links
    assert link.href == DOC_URL
    assert link.texts == (
        "Revisión - 2026/10/26 14:58 GMT-05:00 - Transcripción",
        "Archivo adjunto: Revisión - 2026/10/26 14:58 GMT-05:00 - Transcripción",
    )
    assert observation.events[1].links == ()
    assert page.keys == ["Escape", "Escape"]
    assert page.visits == [DAY_URL]


def test_a_day_without_its_shell_reports_it_instead_of_an_empty_day() -> None:
    page = Page()

    observation = _driver(page).calendar_day(DAY_URL, SELECTORS, timeout_ms=10, max_events=5)

    assert observation.shell_visible is False
    assert observation.events == ()


def test_a_day_redirected_to_sign_in_reports_no_shell() -> None:
    page = _calendar_page()
    page.redirects[DAY_URL] = "https://accounts.google.com/v3/signin/identifier"

    observation = _driver(page).calendar_day(DAY_URL, SELECTORS, timeout_ms=10, max_events=5)

    assert observation.shell_visible is False
    assert observation.url.startswith("https://accounts.google.com/")


def test_a_day_over_the_event_bound_fails_closed() -> None:
    page = _calendar_page(*(lambda page: _chip(page, None) for _ in range(3)))

    with pytest.raises(ConnectorFailure) as raised:
        _driver(page).calendar_day(DAY_URL, SELECTORS, timeout_ms=10, max_events=2)

    assert raised.value.reason is ReasonCode.BROWSER_EXTRACTION_FAILED


def test_an_event_that_cannot_be_clicked_fails_closed() -> None:
    page = _calendar_page(lambda page: Element(attributes={"data-eventid": "ref"}))

    with pytest.raises(ConnectorFailure) as raised:
        _driver(page).calendar_day(DAY_URL, SELECTORS, timeout_ms=10, max_events=5)

    assert raised.value.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH


def test_an_event_whose_detail_never_opens_fails_closed() -> None:
    page = _calendar_page(lambda page: Element(on_click=lambda: None))

    with pytest.raises(ConnectorFailure) as raised:
        _driver(page).calendar_day(DAY_URL, SELECTORS, timeout_ms=10, max_events=5)

    assert raised.value.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH
    assert "did not open" in raised.value.safe_message


def test_an_event_whose_detail_never_closes_fails_closed() -> None:
    page = _calendar_page(lambda page: _chip(page, "ref-1"))
    page.dialog_closes = False

    with pytest.raises(ConnectorFailure) as raised:
        _driver(page).calendar_day(DAY_URL, SELECTORS, timeout_ms=10, max_events=5)

    assert raised.value.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH
    assert "did not close" in raised.value.safe_message


# --- Document --------------------------------------------------------------------------------


def _document_page(text: Any) -> tuple[Page, str]:
    url = ENDPOINTS.document_text("1AaBbCcDdEeFfGgHhIiJj")
    page = Page()
    page.elements["body"] = [Element(text=text)]
    return page, url


def test_a_document_page_yields_its_visible_text_and_final_address() -> None:
    page, url = _document_page("Transcript\nUno: hola")

    observation = _driver(page).document_text(url, timeout_ms=10, max_bytes=100)

    assert (observation.url, observation.text) == (url, "Transcript\nUno: hola")


@pytest.mark.parametrize(
    ("text", "max_bytes"),
    [("ñ" * 60, 100), (RuntimeError("detached"), 100), (None, 100)],
)
def test_an_unreadable_or_oversized_document_fails_closed(text: Any, max_bytes: int) -> None:
    page, url = _document_page(text)

    with pytest.raises(ConnectorFailure) as raised:
        _driver(page).document_text(url, timeout_ms=10, max_bytes=max_bytes)

    assert raised.value.reason is ReasonCode.BROWSER_EXTRACTION_FAILED
