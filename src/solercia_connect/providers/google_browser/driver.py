"""Playwright driver for the Google pages the Meet browser route reads.

The driver only observes: it opens the dedicated profile, navigates to allowlisted Google hosts,
and reports what the page shows as plain values. It never reads the session's credentials, never
calls Google's internal APIs, and never follows a signed export URL. Deciding what an observation
means — identity, transcript or mismatch — belongs to the adapter and to ``parsing``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.browser_core.session import (
    PlaywrightSession,
    is_allowed_request_url,
)
from solercia_connect.providers.google_browser.selectors import (
    ENDPOINTS,
    GoogleWebSelectors,
)


@dataclass(frozen=True, slots=True)
class GoogleSessionSnapshot:
    url: str
    account_hints: tuple[str, ...]
    login_visible: bool
    mfa_visible: bool
    shell_visible: bool


@dataclass(frozen=True, slots=True)
class AttachmentLink:
    """One Docs link in an event's detail: its address and every text that names it."""

    href: str
    texts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CalendarEventObservation:
    reference: str | None
    links: tuple[AttachmentLink, ...]


@dataclass(frozen=True, slots=True)
class CalendarDayObservation:
    url: str
    shell_visible: bool
    events: tuple[CalendarEventObservation, ...]


@dataclass(frozen=True, slots=True)
class DocumentObservation:
    url: str
    text: str


class GoogleBrowserDriver(Protocol):
    def open_persistent(self, profile_dir: Path, *, headless: bool) -> None: ...

    def navigate(self, url: str, *, timeout_ms: int) -> None: ...

    def snapshot(self, selectors: GoogleWebSelectors) -> GoogleSessionSnapshot: ...

    def wait(self, milliseconds: int) -> None: ...

    def calendar_day(
        self,
        url: str,
        selectors: GoogleWebSelectors,
        *,
        timeout_ms: int,
        max_events: int,
    ) -> CalendarDayObservation: ...

    def document_text(
        self, url: str, *, timeout_ms: int, max_bytes: int
    ) -> DocumentObservation: ...

    def close(self) -> None: ...


class PlaywrightGoogleBrowserDriver(PlaywrightSession):
    """Persistent Chromium over the dedicated Google profile, reading pages through the DOM."""

    def open_persistent(self, profile_dir: Path, *, headless: bool) -> None:
        self._open_persistent_context(profile_dir, headless=headless)

    def navigate(self, url: str, *, timeout_ms: int) -> None:
        if not is_allowed_request_url(url, ENDPOINTS.navigation_hosts):
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Google browser navigation is limited to Calendar and Docs",
            )
        super().navigate(url, timeout_ms=timeout_ms)

    def snapshot(self, selectors: GoogleWebSelectors) -> GoogleSessionSnapshot:
        page = self._require_page()
        url = str(page.url)
        host, path = _host_and_path(url)
        on_sign_in = host == ENDPOINTS.sign_in_host
        challenge = on_sign_in and "/challenge/" in path
        account_hints = (
            _attribute_and_text_hints(
                page, selectors.identity_controls, selectors.identity_attributes
            )
            if host == ENDPOINTS.calendar_host
            else ()
        )
        return GoogleSessionSnapshot(
            url,
            account_hints,
            (on_sign_in and not challenge) or _any_visible(page, selectors.login_indicators),
            challenge or _any_visible(page, selectors.mfa_indicators),
            host == ENDPOINTS.calendar_host and _any_visible(page, selectors.calendar_shells),
        )

    def calendar_day(
        self,
        url: str,
        selectors: GoogleWebSelectors,
        *,
        timeout_ms: int,
        max_events: int,
    ) -> CalendarDayObservation:
        """Open one day view and read the Docs links of every event's detail.

        Each event is opened, read and closed in turn. An event whose detail does not open or
        does not close again stops the read: a half-read day would look like an empty one.
        """
        self.navigate(url, timeout_ms=timeout_ms)
        page = self._require_page()
        shell_visible = any(
            _became_visible(page, selector, timeout_ms) for selector in selectors.calendar_shells
        )
        if not shell_visible or _host_and_path(str(page.url))[0] != ENDPOINTS.calendar_host:
            return CalendarDayObservation(str(page.url), False, ())
        chips = page.locator(selectors.event_chips)
        count = chips.count()
        if count > max_events:
            raise ConnectorFailure(
                ReasonCode.BROWSER_EXTRACTION_FAILED,
                f"Google Calendar day view exceeds the fixed bound of {max_events} events",
            )
        events: list[CalendarEventObservation] = []
        seen: set[str] = set()
        for index in range(count):
            chip = chips.nth(index)
            reference = chip.get_attribute(selectors.event_reference_attribute)
            if reference is not None and reference in seen:
                continue
            if reference is not None:
                seen.add(reference)
            events.append(
                CalendarEventObservation(reference, self._event_links(chip, selectors, timeout_ms))
            )
        return CalendarDayObservation(str(page.url), True, tuple(events))

    def document_text(self, url: str, *, timeout_ms: int, max_bytes: int) -> DocumentObservation:
        """Return the visible text of one document page, refusing it above ``max_bytes``."""
        self.navigate(url, timeout_ms=timeout_ms)
        page = self._require_page()
        try:
            text = page.locator("body").inner_text(timeout=timeout_ms)
        except Exception as exc:
            raise ConnectorFailure(
                ReasonCode.BROWSER_EXTRACTION_FAILED,
                "Google document page could not be read",
            ) from exc
        if not isinstance(text, str):
            raise ConnectorFailure(
                ReasonCode.BROWSER_EXTRACTION_FAILED,
                "Google document page did not expose its text",
            )
        if len(text.encode("utf-8")) > max_bytes:
            raise ConnectorFailure(
                ReasonCode.BROWSER_EXTRACTION_FAILED,
                "Google Meet transcript exceeds the fixed offline processing bound",
            )
        return DocumentObservation(str(page.url), text)

    def _event_links(
        self,
        chip: Any,
        selectors: GoogleWebSelectors,
        timeout_ms: int,
    ) -> tuple[AttachmentLink, ...]:
        page = self._require_page()
        try:
            chip.click(timeout=timeout_ms)
        except Exception as exc:
            raise _mismatch("Google Calendar event could not be opened", selectors) from exc
        detail = page.locator(selectors.event_detail).first
        if not _became_visible(page, selectors.event_detail, timeout_ms):
            raise _mismatch("Google Calendar event detail did not open", selectors)
        anchors = detail.locator(selectors.document_links)
        links: list[AttachmentLink] = []
        for index in range(anchors.count()):
            anchor = anchors.nth(index)
            href = anchor.get_attribute("href")
            if not href:
                continue
            texts = [anchor.inner_text(timeout=timeout_ms)]
            texts.extend(anchor.get_attribute(name) for name in selectors.link_text_attributes)
            links.append(AttachmentLink(href, tuple(text for text in texts if text)))
        page.keyboard.press("Escape")
        try:
            detail.wait_for(state="hidden", timeout=timeout_ms)
        except Exception as exc:
            raise _mismatch("Google Calendar event detail did not close", selectors) from exc
        return tuple(links)


def _host_and_path(url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "", ""
    return (parsed.hostname or "").casefold().rstrip("."), parsed.path


def _became_visible(page: Any, selector: str, timeout_ms: int) -> bool:
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        return False
    return True


def _any_visible(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                return True
        except Exception:
            continue
    return False


def _attribute_and_text_hints(
    page: Any,
    selectors: tuple[str, ...],
    attributes: tuple[str, ...],
) -> tuple[str, ...]:
    hints: list[str] = []
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()
        except Exception:
            continue
        for index in range(count):
            try:
                control = locator.nth(index)
                for attribute in attributes:
                    value = control.get_attribute(attribute)
                    if value:
                        hints.append(str(value))
            except Exception:
                continue
    return tuple(dict.fromkeys(hints))


def _mismatch(message: str, selectors: GoogleWebSelectors) -> ConnectorFailure:
    return ConnectorFailure(
        ReasonCode.BROWSER_SELECTOR_MISMATCH,
        f"{message}; selectors={selectors.version}",
    )
