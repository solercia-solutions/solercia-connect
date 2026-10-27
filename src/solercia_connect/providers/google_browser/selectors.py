"""Every Google web page assumption of the supervised browser route, under one version.

The route reads two Google pages and nothing else (decision 2026-10-25, D1 as made precise on
2026-10-26): the Calendar web day view with each event's detail, and the Docs ``mobilebasic`` view
of a transcript document. A change in either page must surface as ``browser_selector_mismatch``,
never as partial data, so the selectors live here and the adapter never guesses at a page.

The identity probe and the event reference attribute have not been proven live yet (task T5.1);
their selectors are versioned so a live correction is one change in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

SELECTOR_VERSION = "google-web-2026.10-v1"

# A Google document ID: URL-safe base64 letters, never a path separator or a query.
DOCUMENT_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")


@dataclass(frozen=True, slots=True)
class GoogleWebEndpoints:
    calendar_home: str = "https://calendar.google.com/calendar/u/0/r"
    calendar_host: str = "calendar.google.com"
    document_host: str = "docs.google.com"
    sign_in_host: str = "accounts.google.com"
    # The only hosts the route ever navigates to.
    navigation_hosts: tuple[str, ...] = ("calendar.google.com", "docs.google.com")

    def calendar_day(self, day: date) -> str:
        return f"{self.calendar_home}/day/{day.year}/{day.month}/{day.day}"

    def document_text(self, document_id: str) -> str:
        if DOCUMENT_ID.fullmatch(document_id) is None:
            raise ValueError("Google document ID is not one URL-safe identifier")
        return f"https://{self.document_host}/document/d/{document_id}/mobilebasic"


@dataclass(frozen=True, slots=True)
class GoogleWebSelectors:
    version: str = SELECTOR_VERSION
    # The account button of the Google bar names the signed-in account in its label, as
    # "Google Account: Name (address)" or "Cuenta de Google: Nombre (dirección)".
    identity_controls: tuple[str, ...] = (
        'a[href^="https://accounts.google.com/SignOutOptions"]',
        '[aria-label^="Google Account:"]',
        '[aria-label^="Cuenta de Google:"]',
    )
    identity_attributes: tuple[str, ...] = ("aria-label", "title")
    calendar_shells: tuple[str, ...] = ('[role="main"]',)
    login_indicators: tuple[str, ...] = ('input[type="email"]', 'input[type="password"]')
    mfa_indicators: tuple[str, ...] = ('input[name="totpPin"]', "[data-challengetype]")
    # One button per event in the day view; the attribute carries the event reference.
    event_chips: str = '[role="button"][data-eventid]'
    event_reference_attribute: str = "data-eventid"
    event_detail: str = '[role="dialog"]'
    # Attachments that are Google Docs; only the transcript among them is read.
    document_links: str = 'a[href^="https://docs.google.com/document/d/"]'
    link_text_attributes: tuple[str, ...] = ("aria-label", "title")
    # Meet names the transcript "<title> - YYYY/MM/DD HH:MM GMT±HH:MM - Transcript" in the
    # account's language, and starts its body section with the same word.
    transcript_names: tuple[str, ...] = ("Transcript", "Transcripción")


ENDPOINTS = GoogleWebEndpoints()
SELECTORS = GoogleWebSelectors()
