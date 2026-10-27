"""Pure readings of what the Google pages show: no browser, no I/O, no guessing.

Each function either returns a typed reading, returns ``None`` for something that is simply not a
transcript, or fails closed with ``browser_selector_mismatch`` when a page shows something that
looks like a transcript but does not have the shape this selector version expects.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from solercia_connect.domain import DateWindow, ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.google_browser.selectors import (
    SELECTORS,
    GoogleWebEndpoints,
    GoogleWebSelectors,
)

# "<title> - 2026/10/26 14:58 GMT-05:00 - Transcript", the name Meet gives the document. The title
# itself may contain " - ", so the stamp and the closing word anchor the match.
_TRANSCRIPT_NAME = re.compile(
    r"(?P<title>.+) - (?P<stamp>\d{4}/\d{2}/\d{2} \d{2}:\d{2}) "
    r"GMT(?:(?P<sign>[+-])(?P<hours>\d{2}):(?P<minutes>\d{2}))? - (?P<word>\S+)"
)
_DOCUMENT_PATH = re.compile(r"/document/(?:u/\d{1,2}/)?d/(?P<id>[A-Za-z0-9_-]{10,200})(?:/.*)?")
_EVENT_REFERENCE = re.compile(r"[A-Za-z0-9+/_-]{4,4096}={0,2}")
_EVENT_ID = re.compile(r"[A-Za-z0-9_]{1,1024}")


@dataclass(frozen=True, slots=True)
class TranscriptAttachment:
    """One transcript document attached to a Calendar event, as its detail names it."""

    document_id: str
    display_name: str
    recorded_at: datetime


def transcript_attachment(
    href: str,
    texts: tuple[str, ...],
    *,
    endpoints: GoogleWebEndpoints,
    selectors: GoogleWebSelectors = SELECTORS,
) -> TranscriptAttachment | None:
    """Read one Docs attachment link; ``None`` when it is some other document.

    A link named like a transcript (its last `` - `` part is the transcript word) that does not
    carry the stamp Meet writes, or whose address is not one Docs document, fails closed instead
    of being skipped.
    """
    candidates = tuple(" ".join(text.split()) for text in texts if text and text.strip())
    names = tuple(name.casefold() for name in selectors.transcript_names)
    named_like_transcript = False
    for candidate in candidates:
        if " - " not in candidate or candidate.rsplit(" - ", 1)[1].casefold() not in names:
            continue
        named_like_transcript = True
        match = _TRANSCRIPT_NAME.fullmatch(candidate)
        if match is None:
            continue
        document_id = document_id_from_href(href, endpoints=endpoints)
        if document_id is None:
            raise _mismatch("A Meet transcript attachment does not link one Docs document")
        return TranscriptAttachment(document_id, candidate, _stamp(match))
    if named_like_transcript:
        raise _mismatch("A Meet transcript attachment name does not have the expected shape")
    return None


def document_id_from_href(href: str, *, endpoints: GoogleWebEndpoints) -> str | None:
    """Return the document ID of an HTTPS Docs link on the default port, or ``None``."""
    try:
        parsed = urlsplit(href.strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != endpoints.document_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    match = _DOCUMENT_PATH.fullmatch(parsed.path)
    return match.group("id") if match is not None else None


def event_reference(value: str | None) -> tuple[str, str] | None:
    """Decode an event chip's reference into the event ID and the calendar it belongs to.

    Calendar web encodes ``"<event id> <calendar id>"`` in base64. Anything else is unreadable
    here and returns ``None``, so the caller can fail closed.
    """
    if value is None or _EVENT_REFERENCE.fullmatch(value.strip()) is None:
        return None
    raw = value.strip().replace("+", "-").replace("/", "_")
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    parts = decoded.split(" ")
    if len(parts) != 2:
        return None
    event_id, calendar_id = parts
    if _EVENT_ID.fullmatch(event_id) is None or "@" not in calendar_id:
        return None
    return event_id, calendar_id.casefold()


def normalize_document_text(text: str) -> str:
    """Unify line endings and drop a byte-order mark and outer blank space; nothing else."""
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿").strip()


def has_transcript_section(text: str, selectors: GoogleWebSelectors = SELECTORS) -> bool:
    names = {name.casefold() for name in selectors.transcript_names}
    return any(line.strip().casefold() in names for line in text.splitlines())


def window_days(window: DateWindow, *, max_days: int) -> tuple[date, ...]:
    """Return every day view that can hold an event of the window, bounded and in order.

    The day view follows the calendar's own time zone, which the route cannot read, so one day
    is added on each side of the window's UTC dates. The exact window is applied afterwards to
    the transcript's own time stamp.
    """
    if window.end - window.start > timedelta(days=max_days):
        raise ConnectorFailure(
            ReasonCode.INVALID_INPUT,
            f"The Google Meet browser route reads at most {max_days} days per invocation",
        )
    first = window.start.astimezone(UTC).date() - timedelta(days=1)
    last = (window.end - timedelta(microseconds=1)).astimezone(UTC).date() + timedelta(days=1)
    return tuple(first + timedelta(days=offset) for offset in range((last - first).days + 1))


def _stamp(match: re.Match[str]) -> datetime:
    zone = UTC
    if match.group("sign") is not None:
        hours, minutes = int(match.group("hours")), int(match.group("minutes"))
        if hours > 14 or minutes > 59:
            raise _mismatch("A Meet transcript attachment carries an invalid UTC offset")
        delta = timedelta(hours=hours, minutes=minutes)
        zone = timezone(-delta if match.group("sign") == "-" else delta)
    try:
        return datetime.strptime(match.group("stamp"), "%Y/%m/%d %H:%M").replace(tzinfo=zone)
    except ValueError as exc:
        raise _mismatch("A Meet transcript attachment carries an invalid time stamp") from exc


def _mismatch(message: str) -> ConnectorFailure:
    return ConnectorFailure(
        ReasonCode.BROWSER_SELECTOR_MISMATCH,
        f"{message}; selectors={SELECTORS.version}",
    )
