from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapabilityRow:
    provider: str
    channel: str
    capability: str
    v0_1_status: str
    notes: str


CAPABILITY_MATRIX: tuple[CapabilityRow, ...] = (
    CapabilityRow(
        "Microsoft Graph",
        "official",
        "mail.sent.list",
        "implemented",
        "Identity validation, strict range, paging, full message body",
    ),
    CapabilityRow(
        "Microsoft Graph",
        "official",
        "calendar.events.list",
        "implemented",
        "calendarView, paging, accepted/organizer and cancellation filters",
    ),
    CapabilityRow(
        "Microsoft Graph",
        "official",
        "meeting.transcripts.*",
        "implemented-offline",
        "Known-meeting list/content contract; tenant scope and live stability still pending",
    ),
    CapabilityRow(
        "Microsoft Graph",
        "official",
        "files.search",
        "implemented",
        "OneDrive and SharePoint drives, root/folder listing, provider search, window filter",
    ),
    CapabilityRow(
        "Microsoft Graph",
        "official",
        "files.get",
        "implemented",
        "Single drive item with optional content, capped by an explicit byte budget",
    ),
    CapabilityRow(
        "Gmail API",
        "official",
        "mail.sent.list",
        "implemented",
        "messages.list + messages.get full MIME body",
    ),
    CapabilityRow(
        "Google Calendar API",
        "official",
        "calendar.events.list",
        "implemented",
        "Recurring expansion, paging, exact account/calendar boundary",
    ),
    CapabilityRow(
        "Jira Cloud REST v3",
        "official",
        "external.issues.search",
        "implemented",
        "Enhanced JQL endpoint, done/resolved representative query",
    ),
    CapabilityRow(
        "GLPI REST",
        "official",
        "tickets.search",
        "implemented",
        "Session lifecycle, search-option validation, read-only ticket search",
    ),
    CapabilityRow(
        "Microsoft 365 Web",
        "browser",
        "login/status",
        "implemented",
        "Dedicated profile and unique account verification; interactive CLI only",
    ),
    CapabilityRow(
        "Microsoft 365 Web",
        "browser",
        "mail.sent.list",
        "implemented",
        "Documented read-only Outlook REST collection, strict range, paging, full message body",
    ),
    CapabilityRow(
        "Microsoft 365 Web",
        "browser",
        "files.search / files.get",
        "implemented",
        "SharePoint REST drive on the observed host; separate authorization from mail",
    ),
    CapabilityRow(
        "Microsoft 365 Web",
        "browser",
        "calendar/transcripts",
        "foundation-only",
        "Routing and injected driver contract exist; built-in extraction fails explicitly",
    ),
)
