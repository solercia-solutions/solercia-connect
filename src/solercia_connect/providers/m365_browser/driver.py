from __future__ import annotations

import base64
import importlib
import re
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit

from solercia_connect.browser_profile import (
    audit_browser_profile_path,
    ensure_browser_profile_path,
)
from solercia_connect.config import ConnectionType
from solercia_connect.domain import (
    CalendarEvent,
    CalendarQuery,
    Capability,
    DateWindow,
    DomainRecord,
    DriveFile,
    FileQuery,
    MailCategoriesUpdate,
    MailCategoryAssignment,
    MailMessage,
    MailQuery,
    MeetingTranscript,
    QueryPayload,
    ReasonCode,
    RouteKind,
    TranscriptQuery,
    same_categories,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers._microsoft import (
    MailFolderName,
    field,
    microsoft_categories,
    microsoft_provenance,
    normalize_calendar_event,
    normalize_drive_item,
    normalize_mail_attachment,
    normalize_mail_message,
    normalize_stream_transcript,
    optional_microsoft_datetime,
)
from solercia_connect.providers._shared import unique_canonical_account_identity
from solercia_connect.providers.m365_browser.selectors import ENDPOINTS, Microsoft365Selectors

_OUTLOOK_AUTHORIZATION_HOST_SUFFIXES = (
    "outlook.cloud.microsoft",
    "outlook.office.com",
    "substrate.office.com",
)
_FILES_AUTHORIZATION_HOST_SUFFIXES = (
    "sharepoint.com",
    "onedrive.live.com",
)
_MAX_OUTLOOK_BEARER_BYTES = 8_192
_READ_BEARER_WAIT_INTERVAL_MS = 500
_READ_BEARER_MAX_CYCLES = 20
_OUTLOOK_PAGE_SIZE = 50
_OUTLOOK_MAX_PAGES = 20
_DRIVE_WALK_MAX_DEPTH = 4
_DRIVE_WALK_MAX_FOLDERS = 60
# Teams names the recordings folder in the language the account created it with, and the other
# name is a 404 rather than an empty collection, so both are asked for and misses are expected.
_RECORDING_FOLDER_NAMES = ("Recordings", "Grabaciones")
_RECORDING_SELECT = "id,name,lastModifiedDateTime,createdDateTime,parentReference,createdBy"
_SHARED_SELECT = "id,name,lastModifiedDateTime,remoteItem,createdBy"
_MAX_TRANSCRIPT_BYTES = 8_388_608
_OUTLOOK_MAIL_SELECT_BASE = (
    "Id",
    "Subject",
    "SentDateTime",
    "From",
    "ToRecipients",
    "CcRecipients",
    "Body",
    "ConversationId",
    "HasAttachments",
    "InternetMessageId",
    "Categories",
)
_SENT_MAIL_SELECT = ",".join(_OUTLOOK_MAIL_SELECT_BASE)
_INBOX_MAIL_SELECT = ",".join((*_OUTLOOK_MAIL_SELECT_BASE, "ReceivedDateTime"))
_OutlookTimeField = Literal["SentDateTime", "ReceivedDateTime"]
# Single source of truth: the time field a folder is filtered/ordered by determines both its
# `$select` projection and the `folder` value stamped on each normalized record.
_OUTLOOK_MAIL_READS: dict[_OutlookTimeField, tuple[str, MailFolderName]] = {
    "SentDateTime": (_SENT_MAIL_SELECT, "sent"),
    "ReceivedDateTime": (_INBOX_MAIL_SELECT, "inbox"),
}
_BEARER_AUTHORIZATION = re.compile(
    r"Bearer[ \t]+([A-Z0-9._~+/-]+=*)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _Recording:
    """One meeting recording, wherever it lives, addressed the way transcripts require."""

    drive_id: str
    item_id: str
    name: str | None
    recorded_at: datetime | None
    organizer: str | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.drive_id, self.item_id)


class _TransientBearer:
    """Mutable, non-printable storage for one captured browser bearer token."""

    __slots__ = ("_buffer",)

    def __init__(self, token: str) -> None:
        self._buffer = bytearray(token, "ascii")

    @property
    def cleared(self) -> bool:
        return not self._buffer

    def authorization_header(self) -> str:
        if self.cleared:
            raise RuntimeError("Transient bearer has already been cleared")
        return f"Bearer {self._buffer.decode('ascii')}"

    def clear(self) -> None:
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        self._buffer.clear()

    def __repr__(self) -> str:
        return "_TransientBearer(<redacted>)"

    def __del__(self) -> None:
        self.clear()


@dataclass(frozen=True, slots=True)
class BrowserSnapshot:
    url: str
    title: str
    account_hints: tuple[str, ...]
    login_visible: bool
    mfa_visible: bool
    authenticated_shell_visible: bool


class BrowserDriver(Protocol):
    def open_persistent(
        self,
        profile_dir: Path,
        *,
        headless: bool,
        connection_type: ConnectionType | None = None,
    ) -> None: ...

    def navigate(self, url: str, *, timeout_ms: int) -> None: ...

    def snapshot(
        self,
        selectors: Microsoft365Selectors,
        *,
        connection_type: ConnectionType,
    ) -> BrowserSnapshot: ...

    def outlook_session_identity(
        self,
        *,
        wait_interval_ms: int,
        max_wait_cycles: int,
        max_probe_attempts: int,
        probe_timeout_ms: int,
    ) -> str: ...

    def onedrive_session_identity(self, *, timeout_ms: int) -> str: ...

    def observed_session_authorization(self) -> bool: ...

    def reveal_identity_menu(
        self,
        selectors: Microsoft365Selectors,
        *,
        timeout_ms: int,
    ) -> bool: ...

    def wait(self, milliseconds: int) -> None: ...

    def read_records(
        self,
        capability: Capability,
        payload: QueryPayload,
        selectors: Microsoft365Selectors,
        *,
        timeout_ms: int,
    ) -> tuple[DomainRecord, ...]: ...

    def set_mail_categories(
        self,
        update: MailCategoriesUpdate,
        *,
        timeout_ms: int,
    ) -> MailCategoryAssignment: ...

    def close(self) -> None: ...


class PlaywrightBrowserDriver:
    """Persistent Chromium login/status driver; v0.1 read extraction fails explicitly."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._outlook_bearer: _TransientBearer | None = None
        self._capture_outlook_authorization = False
        self._probing_outlook_identity = False
        self._files_bearer: _TransientBearer | None = None
        self._files_origin: str | None = None
        self._capture_files_authorization = False

    def open_persistent(
        self,
        profile_dir: Path,
        *,
        headless: bool,
        connection_type: ConnectionType | None = None,
    ) -> None:
        self._capture_outlook_authorization = False
        self._capture_files_authorization = False
        self._clear_outlook_bearer()
        self._clear_files_bearer()
        profile_dir = ensure_browser_profile_path(profile_dir)
        try:
            sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
        except ImportError as exc:
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Playwright optional dependency is not installed",
            ) from exc
        audit_browser_profile_path(profile_dir, private_root=profile_dir.parent)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                accept_downloads=False,
            )
        except Exception as exc:
            self._playwright.stop()
            self._playwright = None
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                _launch_failure_message(exc),
            ) from exc
        try:
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            if connection_type is ConnectionType.OUTLOOK:
                self._install_request_observer(self._observe_outlook_request)
                self._capture_outlook_authorization = True
            elif connection_type is ConnectionType.ONEDRIVE:
                self._install_request_observer(self._observe_files_request)
                self._capture_files_authorization = True
        except Exception as exc:
            with suppress(Exception):
                self.close()
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Playwright Chromium context could not initialize",
            ) from exc

    def navigate(self, url: str, *, timeout_ms: int) -> None:
        self._require_page().goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    def snapshot(
        self,
        selectors: Microsoft365Selectors,
        *,
        connection_type: ConnectionType,
    ) -> BrowserSnapshot:
        page = self._require_page()
        account_hints = (
            _visible_identity_hints(
                page,
                selectors.identity_controls,
                selectors.identity_attributes,
            )
            if connection_type is ConnectionType.TEAMS
            else ()
        )
        return BrowserSnapshot(
            page.url,
            page.title(),
            account_hints,
            _any_visible(page, selectors.login_indicators)
            or "login.microsoftonline.com" in page.url.lower(),
            _any_visible(page, selectors.mfa_indicators),
            _any_visible(page, selectors.authenticated_shells) or bool(account_hints),
        )

    def outlook_session_identity(
        self,
        *,
        wait_interval_ms: int,
        max_wait_cycles: int,
        max_probe_attempts: int,
        probe_timeout_ms: int,
    ) -> str:
        """Return only the canonical Outlook REST ``EmailAddress`` for this session."""
        if (
            min(
                wait_interval_ms,
                max_wait_cycles,
                max_probe_attempts,
                probe_timeout_ms,
            )
            <= 0
        ):
            raise ValueError("Outlook identity wait and probe bounds must be positive")
        page = self._require_page()
        wait_cycles = 0
        probe_attempts = 0
        observed_authorization = False
        last_failure = "Outlook session authorization was not observed within the bounded wait"

        while True:
            bearer = self._take_outlook_bearer()
            if bearer is not None:
                observed_authorization = True
                probe_attempts += 1
                try:
                    outcome = self._probe_outlook_identity(
                        page,
                        bearer,
                        timeout_ms=probe_timeout_ms,
                    )
                finally:
                    bearer.clear()

                if not isinstance(outcome, Mapping):
                    raise _outlook_identity_failure(
                        "Outlook identity endpoint returned a malformed response"
                    )
                kind = outcome.get("kind")
                if kind == "ok":
                    identity = unique_canonical_account_identity(
                        outcome.get("emailAddress"),
                        exact=True,
                    )
                    if identity is None:
                        raise _outlook_identity_failure(
                            "Outlook identity endpoint did not return one canonical EmailAddress"
                        )
                    return identity
                if kind == "malformed":
                    raise _outlook_identity_failure(
                        "Outlook identity endpoint returned a malformed response"
                    )
                if kind == "http_error":
                    status = outcome.get("status")
                    if isinstance(status, int) and status in {401, 403}:
                        last_failure = (
                            "Outlook identity endpoint rejected the captured session authorization"
                        )
                    elif isinstance(status, int) and status in {404, 410}:
                        raise _outlook_identity_failure(
                            "Outlook identity endpoint is unavailable for this browser session"
                        )
                    else:
                        last_failure = (
                            "Outlook identity endpoint rejected the bounded read-only probe"
                        )
                else:
                    last_failure = (
                        "Outlook identity endpoint did not complete the bounded read-only probe"
                    )
                if probe_attempts >= max_probe_attempts:
                    raise _outlook_identity_failure(last_failure)

            if wait_cycles >= max_wait_cycles:
                break
            page.wait_for_timeout(wait_interval_ms)
            wait_cycles += 1

        if observed_authorization:
            raise _outlook_identity_failure(last_failure)
        raise _outlook_identity_failure(
            "Outlook session authorization was not observed within the bounded wait"
        )

    def reveal_identity_menu(
        self,
        selectors: Microsoft365Selectors,
        *,
        timeout_ms: int,
    ) -> bool:
        """Reveal the identity flyout and report only observed open/materialized state."""
        page = self._require_page()
        if _identity_menu_is_open(page, selectors):
            return True
        for selector in selectors.profile_controls:
            try:
                locator = page.locator(selector)
                count = locator.count()
            except Exception:
                continue
            for index in range(count):
                try:
                    control = locator.nth(index)
                    if not control.is_visible():
                        continue
                    control.click(timeout=timeout_ms)
                    return _identity_menu_is_open(page, selectors)
                except Exception:
                    continue
        return False

    def wait(self, milliseconds: int) -> None:
        self._require_page().wait_for_timeout(milliseconds)

    def read_records(
        self,
        capability: Capability,
        payload: QueryPayload,
        selectors: Microsoft365Selectors,
        *,
        timeout_ms: int,
    ) -> tuple[DomainRecord, ...]:
        del selectors
        if capability is Capability.MAIL_SENT_LIST and isinstance(payload, MailQuery):
            return self._read_sent_mail(payload, timeout_ms=timeout_ms)
        if capability is Capability.MAIL_INBOX_LIST and isinstance(payload, MailQuery):
            return self._read_folder_mail(
                payload,
                url=ENDPOINTS.outlook_inbox_messages,
                time_field="ReceivedDateTime",
                timeout_ms=timeout_ms,
            )
        if capability is Capability.CALENDAR_EVENTS_LIST and isinstance(payload, CalendarQuery):
            return self._read_calendar(payload, timeout_ms=timeout_ms)
        if capability in {Capability.FILES_SEARCH, Capability.FILES_GET} and isinstance(
            payload, FileQuery
        ):
            return self._read_files(
                payload,
                timeout_ms=timeout_ms,
                with_content=capability is Capability.FILES_GET,
            )
        if capability in {
            Capability.MEETING_TRANSCRIPTS_LIST,
            Capability.MEETING_TRANSCRIPT_GET,
        } and isinstance(payload, TranscriptQuery):
            return self._read_transcripts(
                payload,
                timeout_ms=timeout_ms,
                with_content=capability is Capability.MEETING_TRANSCRIPT_GET,
            )
        raise ConnectorFailure(
            ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED,
            f"Microsoft 365 browser route does not implement {capability.value}",
        )

    def _read_sent_mail(
        self,
        query: MailQuery,
        *,
        timeout_ms: int,
    ) -> tuple[MailMessage, ...]:
        """List sent mail through the documented read-only Outlook REST collection."""
        return self._read_folder_mail(
            query,
            url=ENDPOINTS.outlook_sent_messages,
            time_field="SentDateTime",
            timeout_ms=timeout_ms,
        )

    def _read_folder_mail(
        self,
        query: MailQuery,
        *,
        url: str,
        time_field: _OutlookTimeField,
        timeout_ms: int,
    ) -> tuple[MailMessage, ...]:
        """List one Outlook mail folder through its documented read-only REST collection."""
        select, folder = _OUTLOOK_MAIL_READS[time_field]
        messages: list[MailMessage] = []
        next_url: str | None = url
        params: Mapping[str, str] | None = {
            "$filter": (
                f"{time_field} ge {_outlook_timestamp(query.window.start)}"
                f" and {time_field} lt {_outlook_timestamp(query.window.end)}"
            ),
            "$select": select,
            "$orderby": f"{time_field} asc",
            "$top": str(min(_OUTLOOK_PAGE_SIZE, query.limit)),
        }

        for _ in range(_OUTLOOK_MAX_PAGES):
            if next_url is None or len(messages) >= query.limit:
                break
            payload = self._authorized_outlook_get(next_url, params=params, timeout_ms=timeout_ms)
            items = payload.get("value")
            if not isinstance(items, list):
                raise ConnectorFailure(
                    ReasonCode.BROWSER_EXTRACTION_FAILED,
                    "Outlook mail collection returned an unexpected payload shape",
                )
            for item in items:
                if len(messages) >= query.limit:
                    break
                if isinstance(item, Mapping):
                    messages.append(_normalize_outlook_message(item, folder=folder))
            next_link = payload.get("@odata.nextLink")
            next_url = next_link if isinstance(next_link, str) else None
            params = None

        if query.include_attachments:
            messages = [
                self._message_with_attachments(message, query, timeout_ms=timeout_ms)
                for message in messages
            ]
        return tuple(messages)

    def _read_calendar(
        self,
        query: CalendarQuery,
        *,
        timeout_ms: int,
    ) -> tuple[CalendarEvent, ...]:
        """Read the calendar view, which expands recurring series into real occurrences."""
        events: list[CalendarEvent] = []
        url: str | None = ENDPOINTS.outlook_calendar_view
        params: Mapping[str, str] | None = {
            "startDateTime": _outlook_timestamp(query.window.start),
            "endDateTime": _outlook_timestamp(query.window.end),
            "$orderby": "Start/DateTime asc",
            "$top": str(min(_OUTLOOK_PAGE_SIZE, query.limit)),
        }
        # Without this the provider answers in the mailbox's own zone, which would make the same
        # request return different instants for different mailboxes.
        headers = {"Prefer": 'outlook.timezone="UTC"'}

        for _ in range(_OUTLOOK_MAX_PAGES):
            if url is None or len(events) >= query.limit:
                break
            payload = self._authorized_outlook_get(
                url,
                params=params,
                timeout_ms=timeout_ms,
                extra_headers=headers,
            )
            items = payload.get("value")
            if not isinstance(items, list):
                raise ConnectorFailure(
                    ReasonCode.BROWSER_EXTRACTION_FAILED,
                    "Outlook calendar view returned an unexpected payload shape",
                )
            for item in items:
                if len(events) >= query.limit:
                    break
                if not isinstance(item, Mapping):
                    continue
                event = normalize_calendar_event(
                    item,
                    provenance=microsoft_provenance(
                        provider_id="microsoft365.browser",
                        route=RouteKind.BROWSER,
                        source_id=str(field(item, "id", "Id") or ""),
                        fetched_at=utc_now(),
                        account_id=None,
                        endpoint="outlook-rest-v2-calendarview",
                    ),
                )
                events.append(event)
            next_link = payload.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) else None
            params = None

        if query.accepted_only:
            events = [event for event in events if _attendance_confirmed(event)]
        if query.excluded_organizer_domains:
            excluded = {domain.casefold() for domain in query.excluded_organizer_domains}
            events = [
                event
                for event in events
                if event.organizer is None
                or event.organizer.address.rsplit("@", 1)[-1].casefold() not in excluded
            ]
        return tuple(events)

    def _message_with_attachments(
        self,
        message: MailMessage,
        query: MailQuery,
        *,
        timeout_ms: int,
    ) -> MailMessage:
        """Attach the message's own files, skipping any that exceed the caller's byte budget."""
        if not message.has_attachments:
            return message
        url = f"{ENDPOINTS.outlook_messages}/{quote(message.id, safe='')}/attachments"
        # ContentBytes belongs to a subtype and cannot be projected: asking for it in $select is
        # rejected outright, and any $select silently drops it. The collection is requested whole.
        payload = self._authorized_outlook_get(url, params=None, timeout_ms=timeout_ms)
        raw = payload.get("value")
        if not isinstance(raw, list):
            return message
        attachments = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            attachment = normalize_mail_attachment(item)
            if attachment is None:
                continue
            if (
                attachment.size_bytes is not None
                and attachment.size_bytes > query.max_attachment_bytes
            ):
                attachment = replace(attachment, content_base64=None)
            attachments.append(attachment)
        return replace(message, attachments=tuple(attachments))

    def set_mail_categories(
        self,
        update: MailCategoriesUpdate,
        *,
        timeout_ms: int,
    ) -> MailCategoryAssignment:
        """Assign categories to one message: the only write this driver performs.

        The current list and its version are read first. Nothing is written when the computed
        list carries the same names; otherwise one PATCH sends only the category collection,
        conditioned on the version read, so a concurrent edit by the team is never overwritten.
        """
        if not update.write_authorized:
            raise ConnectorFailure(
                ReasonCode.WRITE_NOT_AUTHORIZED,
                "Category write was not explicitly authorized for this invocation",
            )
        url = f"{ENDPOINTS.outlook_messages}/{quote(update.message_id, safe='')}"
        bearer = self._await_outlook_bearer(timeout_ms=timeout_ms)
        try:
            outcome = self._rest_get(
                url,
                bearer,
                params={"$select": "Id,Categories"},
                timeout_ms=timeout_ms,
                allowed=_is_allowed_outlook_request_url,
            )
        finally:
            bearer.clear()
        state = _require_message_state(outcome)
        previous = microsoft_categories(field(state, "Categories", "categories"))
        version = state.get("@odata.etag")
        if not isinstance(version, str) or not version.strip():
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Outlook message carried no version to condition the category write on",
            )

        final = update.resolve(previous)
        if same_categories(final, previous):
            return _category_assignment(update.message_id, previous, previous, changed=False)

        bearer = self._await_outlook_bearer(timeout_ms=timeout_ms)
        try:
            written = _require_category_write(
                self._rest_patch_categories(
                    url,
                    bearer,
                    categories=final,
                    version=version,
                    timeout_ms=timeout_ms,
                )
            )
        finally:
            bearer.clear()
        confirmed = microsoft_categories(field(written, "Categories", "categories"))
        if not same_categories(confirmed, final):
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Outlook did not confirm the requested categories after the write",
            )
        return _category_assignment(update.message_id, confirmed, previous, changed=True)

    def observed_session_authorization(self) -> bool:
        """Report whether the session has already issued an authorized provider call.

        A drive surface exposes no stable shell selector, so DOM presence is the wrong readiness
        signal there. Traffic the session itself authorized is direct evidence that sign-in
        completed, and it does not depend on a vendor's markup staying still.
        """
        return self._outlook_bearer is not None or self._files_bearer is not None

    def onedrive_session_identity(self, *, timeout_ms: int) -> str:
        """Return the canonical account the drive session itself reports.

        The v2 ``/me`` document carries only an opaque id on this surface, so identity is read
        from the drive's own owner. The classic current-user document is a fixed second choice,
        not a retry loop: both are read-only and the order never varies.
        """
        origin = self._drive_origin(timeout_ms=timeout_ms)
        for url, extract in (
            (f"{origin}/_api/v2.0/me/drive", _drive_owner_identity),
            (f"{origin}/_api/web/currentuser", _current_user_identity),
        ):
            payload = self._authorized_files_get(url, params=None, timeout_ms=timeout_ms)
            identity = extract(payload)
            if identity is not None:
                return identity
        raise ConnectorFailure(
            ReasonCode.BROWSER_IDENTITY_UNAVAILABLE,
            "Drive session did not report one canonical account",
        )

    def _drive_origin(self, *, timeout_ms: int) -> str:
        bearer = self._await_files_bearer(timeout_ms=timeout_ms)
        self._files_bearer = bearer
        origin = self._files_origin
        if origin is None:
            raise ConnectorFailure(
                ReasonCode.BROWSER_IDENTITY_UNAVAILABLE,
                "SharePoint drive host was not observed in the supervised session",
            )
        return origin

    def _read_files(
        self,
        query: FileQuery,
        *,
        timeout_ms: int,
        with_content: bool,
    ) -> tuple[DriveFile, ...]:
        """List, search or fetch drive items over the supervised SharePoint session."""
        select = (
            "id,name,size,lastModifiedDateTime,createdDateTime,file,folder,webUrl,parentReference"
        )
        drive_url = self._drive_base_url(query.site, timeout_ms=timeout_ms)

        if query.item_id is not None:
            payload = self._authorized_files_get(
                f"{drive_url}/items/{quote(query.item_id.strip(), safe='')}",
                params={"$select": select},
                timeout_ms=timeout_ms,
            )
            items: list[Mapping[str, object]] = [payload]
        elif query.text is not None:
            items = self._walk_drive_for_text(drive_url, query, select, timeout_ms=timeout_ms)
        else:
            url, params = self._drive_listing(drive_url, query, select)
            payload = self._authorized_files_get(url, params=params, timeout_ms=timeout_ms)
            raw = payload.get("value")
            if not isinstance(raw, list):
                raise ConnectorFailure(
                    ReasonCode.BROWSER_EXTRACTION_FAILED,
                    "SharePoint drive collection returned an unexpected payload shape",
                )
            items = [item for item in raw if isinstance(item, Mapping)]

        files = [
            normalize_drive_item(
                item,
                provenance=microsoft_provenance(
                    provider_id="microsoft365.browser",
                    route=RouteKind.BROWSER,
                    source_id=str(field(item, "id", "Id") or ""),
                    fetched_at=utc_now(),
                    account_id=None,
                    endpoint="sharepoint-rest-v2",
                ),
            )
            for item in items
        ]
        files = [item for item in files if _within_window(item, query.window)]
        if with_content:
            files = [
                self._file_with_content(item, drive_url, query, timeout_ms=timeout_ms)
                for item in files
            ]
        return tuple(files[: query.limit])

    def _drive_listing(
        self,
        drive_url: str,
        query: FileQuery,
        select: str,
    ) -> tuple[str, dict[str, str]]:
        params = {"$select": select, "$top": str(min(query.limit, 200))}
        if query.folder_path is not None:
            path = query.folder_path.strip().strip("/")
            return f"{drive_url}/root:/{quote(path, safe='/')}:/children", params
        return f"{drive_url}/root/children", params

    def _walk_drive_for_text(
        self,
        drive_url: str,
        query: FileQuery,
        select: str,
        *,
        timeout_ms: int,
    ) -> list[Mapping[str, object]]:
        """Find items by name with a bounded walk instead of the provider's search endpoint.

        That endpoint answers this drive surface with an empty collection even for items that
        demonstrably exist, which is the one failure mode worth refusing to inherit: an empty
        result is indistinguishable from "nothing matched". A bounded walk is slower and honest.
        """
        needle = (query.text or "").strip().casefold()
        start = (
            f"root:/{quote(query.folder_path.strip().strip('/'), safe='/')}:"
            if (query.folder_path is not None)
            else "root"
        )
        pending: deque[tuple[str, int]] = deque([(start, 0)])
        matches: list[Mapping[str, object]] = []
        visited = 0
        bearer = self._await_files_bearer(timeout_ms=timeout_ms)

        try:
            while pending and len(matches) < query.limit and visited < _DRIVE_WALK_MAX_FOLDERS:
                anchor, depth = pending.popleft()
                visited += 1
                try:
                    payload = self._authorized_files_get(
                        f"{drive_url}/{anchor}/children",
                        params={"$select": select, "$top": "200"},
                        timeout_ms=timeout_ms,
                        bearer=bearer,
                    )
                except ConnectorFailure:
                    # One unreadable folder is not a failed search; the rest of the drive stands.
                    continue
                items = payload.get("value")
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    name = field(item, "name", "Name")
                    if isinstance(name, str) and needle in name.casefold():
                        matches.append(item)
                        if len(matches) >= query.limit:
                            break
                    is_folder = field(item, "folder", "Folder") is not None
                    identifier = field(item, "id", "Id")
                    if is_folder and depth < _DRIVE_WALK_MAX_DEPTH and isinstance(identifier, str):
                        pending.append((f"items/{quote(identifier, safe='')}", depth + 1))
        finally:
            bearer.clear()
        return matches

    def _file_with_content(
        self,
        item: DriveFile,
        drive_url: str,
        query: FileQuery,
        *,
        timeout_ms: int,
    ) -> DriveFile:
        if item.is_folder:
            return item
        if item.size_bytes is not None and item.size_bytes > query.max_content_bytes:
            raise ConnectorFailure(
                ReasonCode.INVALID_INPUT,
                "Drive item exceeds the requested maximum content size",
            )
        bearer = self._await_files_bearer(timeout_ms=timeout_ms)
        try:
            content = self._outlook_rest_bytes(
                f"{drive_url}/items/{quote(item.id, safe='')}/content",
                bearer,
                timeout_ms=timeout_ms,
            )
        finally:
            bearer.clear()
        if len(content) > query.max_content_bytes:
            raise ConnectorFailure(
                ReasonCode.INVALID_INPUT,
                "Drive item exceeds the requested maximum content size",
            )
        return replace(item, content_base64=base64.b64encode(content).decode("ascii"))

    def _read_transcripts(
        self,
        query: TranscriptQuery,
        *,
        timeout_ms: int,
        with_content: bool,
    ) -> tuple[MeetingTranscript, ...]:
        """Read meeting transcripts over the supervised SharePoint session.

        A transcript is not a Teams resource here. It hangs off the recording in whichever drive
        holds it, which is why a shared meeting the account merely attended is readable at all:
        the site cookie covers every ``personal/`` path on the host, not just this account's.
        """
        origin = self._drive_origin(timeout_ms=timeout_ms)
        bearer = self._await_files_bearer(timeout_ms=timeout_ms)
        try:
            recordings = self._transcript_recordings(
                origin, query, bearer=bearer, timeout_ms=timeout_ms
            )
            transcripts: list[MeetingTranscript] = []
            for recording in recordings:
                if len(transcripts) >= query.limit:
                    break
                for entry in self._transcript_manifest(
                    origin, recording, bearer=bearer, timeout_ms=timeout_ms
                ):
                    transcript = self._transcript_record(
                        origin,
                        recording,
                        entry,
                        bearer=bearer,
                        timeout_ms=timeout_ms,
                        with_content=with_content,
                        meeting_id=query.meeting_id,
                    )
                    if transcript is not None:
                        transcripts.append(transcript)
            return tuple(transcripts[: query.limit])
        finally:
            bearer.clear()

    def _transcript_recordings(
        self,
        origin: str,
        query: TranscriptQuery,
        *,
        bearer: _TransientBearer,
        timeout_ms: int,
    ) -> tuple[_Recording, ...]:
        """Resolve which recordings to read, either named outright or discovered for a period."""
        if query.drive_id is not None and query.item_id is not None:
            return (_Recording(query.drive_id, query.item_id, None, None, None),)

        found: list[_Recording] = []
        seen: set[tuple[str, str]] = set()

        for folder in _RECORDING_FOLDER_NAMES:
            payload = self._authorized_files_get(
                f"{origin}/_api/v2.0/me/drive/root:/{folder}:/children",
                params={"$select": _RECORDING_SELECT, "$top": "200"},
                timeout_ms=timeout_ms,
                bearer=bearer,
                # The folder Teams did not create answers 404, which is an absence, not a fault.
                optional=True,
            )
            for item in _collection(payload):
                recording = _own_recording(item)
                if recording is not None and recording.key not in seen:
                    seen.add(recording.key)
                    found.append(recording)

        if query.include_shared:
            payload = self._authorized_files_get(
                f"{origin}/_api/v2.0/me/drive/sharedWithMe",
                params={"$select": _SHARED_SELECT},
                timeout_ms=timeout_ms,
                bearer=bearer,
                optional=True,
            )
            for item in _collection(payload):
                recording = _shared_recording(item)
                if recording is not None and recording.key not in seen:
                    seen.add(recording.key)
                    found.append(recording)

        within = [item for item in found if _recording_in_window(item, query.window)]
        within.sort(key=_recording_order, reverse=True)
        return tuple(within)

    def _transcript_manifest(
        self,
        origin: str,
        recording: _Recording,
        *,
        bearer: _TransientBearer,
        timeout_ms: int,
    ) -> tuple[Mapping[str, object], ...]:
        """List the transcripts attached to one recording.

        The collection hangs off ``drives/{driveId}/items/{itemId}``. The account-relative
        ``me/drive/items/{itemId}`` form that serves file reads answers 404 here, so the drive is
        always named explicitly even when the recording belongs to this account.
        """
        payload = self._authorized_files_get(
            f"{origin}/_api/v2.0/drives/{quote(recording.drive_id, safe='')}"
            f"/items/{quote(recording.item_id, safe='')}/media/transcripts",
            params=None,
            timeout_ms=timeout_ms,
            bearer=bearer,
            # A meeting that was recorded without transcription has no collection to read.
            optional=True,
        )
        return _collection(payload)

    def _transcript_record(
        self,
        origin: str,
        recording: _Recording,
        entry: Mapping[str, object],
        *,
        bearer: _TransientBearer,
        timeout_ms: int,
        with_content: bool,
        meeting_id: str | None,
    ) -> MeetingTranscript | None:
        transcript_id = field(entry, "id", "Id")
        if not isinstance(transcript_id, str) or not transcript_id.strip():
            return None

        content: Mapping[str, object] = {}
        if with_content:
            content = self._transcript_content(
                origin, recording, transcript_id, bearer=bearer, timeout_ms=timeout_ms
            )

        display_name = field(entry, "displayName", "DisplayName")
        record = normalize_stream_transcript(
            content,
            transcript_id=transcript_id,
            provenance=microsoft_provenance(
                provider_id="microsoft365.browser",
                route=RouteKind.BROWSER,
                source_id=transcript_id,
                fetched_at=utc_now(),
                account_id=None,
                endpoint="sharepoint-rest-v2",
            ),
            display_name=display_name if isinstance(display_name, str) else recording.name,
            recorded_at=recording.recorded_at,
            drive_id=recording.drive_id,
            item_id=recording.item_id,
            organizer=recording.organizer,
            meeting_id=meeting_id,
        )
        if len(record.content.encode("utf-8")) > _MAX_TRANSCRIPT_BYTES:
            raise ConnectorFailure(
                ReasonCode.BROWSER_EXTRACTION_FAILED,
                "Microsoft Teams transcript exceeds the fixed offline processing bound",
            )
        return record

    def _transcript_content(
        self,
        origin: str,
        recording: _Recording,
        transcript_id: str,
        *,
        bearer: _TransientBearer,
        timeout_ms: int,
    ) -> Mapping[str, object]:
        """Download one transcript in the only rendition that names its speakers.

        ``streamContent`` defaults to WebVTT with no speaker markers, and asking for
        ``text/vtt`` outright is rejected, so ``format=json`` is the single usable request.
        """
        return self._authorized_files_get(
            f"{origin}/_api/v2.0/drives/{quote(recording.drive_id, safe='')}"
            f"/items/{quote(recording.item_id, safe='')}"
            f"/media/transcripts/{quote(transcript_id, safe='')}/streamContent",
            params={"format": "json"},
            timeout_ms=timeout_ms,
            bearer=bearer,
        )

    def _drive_base_url(self, site: str | None, *, timeout_ms: int) -> str:
        bearer_origin = self._drive_origin(timeout_ms=timeout_ms)
        if site is None:
            return f"{bearer_origin}/_api/v2.0/me/drive"
        cleaned = site.strip().strip("/")
        return f"{bearer_origin}/_api/v2.0/sites/{quote(cleaned, safe=':/')}/drive"

    def _authorized_files_get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        timeout_ms: int,
        bearer: _TransientBearer | None = None,
        optional: bool = False,
    ) -> Mapping[str, object]:
        """Issue one read-only drive call.

        A caller doing many reads in one operation passes a single authorization and clears it
        once at the end. Re-capturing per call meant renavigating the drive for every folder,
        which turned a bounded walk into something that could not finish.

        ``optional`` marks a surface whose absence is an ordinary answer: a recordings folder
        named in the other language, or a recording that carries no transcript. Only 404 is read
        that way, so a rejected authorization or a broken surface still fails loudly instead of
        being reported as an empty result.
        """
        held = bearer if bearer is not None else self._await_files_bearer(timeout_ms=timeout_ms)
        try:
            outcome = self._rest_get(
                url,
                held,
                params=params,
                timeout_ms=timeout_ms,
                allowed=_is_allowed_files_request_url,
            )
        finally:
            if bearer is None:
                held.clear()
        if optional and outcome.get("kind") == "http_error" and outcome.get("status") == 404:
            return {}
        return _require_rest_payload(outcome)

    def _await_files_bearer(self, *, timeout_ms: int) -> _TransientBearer:
        page = self._require_page()
        for cycle in range(_READ_BEARER_MAX_CYCLES + 1):
            bearer = self._take_files_bearer()
            if bearer is not None:
                return bearer
            if cycle == _READ_BEARER_MAX_CYCLES:
                break
            if cycle == 0:
                # Opening the drive makes the session issue authorized SharePoint calls.
                with suppress(Exception):
                    page.goto(
                        ENDPOINTS.onedrive_launch,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
            page.wait_for_timeout(_READ_BEARER_WAIT_INTERVAL_MS)
        raise ConnectorFailure(
            ReasonCode.BROWSER_IDENTITY_UNAVAILABLE,
            "SharePoint session authorization was not observed within the bounded read window",
        )

    def _outlook_rest_bytes(
        self,
        url: str,
        bearer: _TransientBearer,
        *,
        timeout_ms: int,
    ) -> bytes:
        if not _is_allowed_files_request_url(url):
            raise ConnectorFailure(
                ReasonCode.BROWSER_EXTRACTION_FAILED,
                "Drive content host is not on the provider allowlist",
            )
        context = self._context
        if context is None:
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Playwright persistent context is not open",
            )
        try:
            response = context.request.get(
                url,
                headers={"Authorization": bearer.authorization_header()},
                params=None,
                timeout=timeout_ms,
                fail_on_status_code=False,
            )
            status = int(response.status)
            if status < 200 or status >= 300:
                raise ConnectorFailure(
                    ReasonCode.BROWSER_EXTRACTION_FAILED,
                    "SharePoint rejected the bounded read-only content call",
                )
            body = response.body()
        except ConnectorFailure:
            raise
        except Exception as exc:
            raise ConnectorFailure(
                ReasonCode.BROWSER_EXTRACTION_FAILED,
                "SharePoint content call did not complete",
            ) from exc
        return body if isinstance(body, bytes) else bytes(body)

    def _authorized_outlook_get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        timeout_ms: int,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        """Issue one read-only Outlook REST call with a freshly captured authorization."""
        bearer = self._await_outlook_bearer(timeout_ms=timeout_ms)
        try:
            outcome = self._rest_get(
                url,
                bearer,
                params=params,
                timeout_ms=timeout_ms,
                allowed=_is_allowed_outlook_request_url,
                extra_headers=extra_headers,
            )
        finally:
            bearer.clear()

        return _require_rest_payload(outcome)

    def _await_outlook_bearer(self, *, timeout_ms: int) -> _TransientBearer:
        """Wait, within a bounded window, for the session to emit an authorization."""
        page = self._require_page()
        for cycle in range(_READ_BEARER_MAX_CYCLES + 1):
            bearer = self._take_outlook_bearer()
            if bearer is not None:
                return bearer
            if cycle == _READ_BEARER_MAX_CYCLES:
                break
            if cycle == 0:
                # Re-entering the mailbox makes the session issue authorized calls again.
                with suppress(Exception):
                    page.goto(
                        ENDPOINTS.outlook_sent,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
            page.wait_for_timeout(_READ_BEARER_WAIT_INTERVAL_MS)
        raise ConnectorFailure(
            ReasonCode.BROWSER_IDENTITY_UNAVAILABLE,
            "Outlook session authorization was not observed within the bounded read window",
        )

    def close(self) -> None:
        self._capture_outlook_authorization = False
        self._capture_files_authorization = False
        self._clear_outlook_bearer()
        self._clear_files_bearer()
        self._files_origin = None
        context = self._context
        playwright = self._playwright
        self._page = None
        self._context = None
        self._playwright = None
        try:
            if context is not None:
                context.close()
        finally:
            if playwright is not None:
                playwright.stop()

    def _install_request_observer(self, observer: Any) -> None:
        for emitter in (self._context, self._page):
            try:
                emitter.on("request", observer)
            except Exception:
                continue

    def _observe_files_request(self, request: Any) -> None:
        """Capture the drive authorization and the drive host that issued it."""
        if not self._capture_files_authorization or self._probing_outlook_identity:
            return
        try:
            url = request.url
            if not isinstance(url, str) or not _is_allowed_files_request_url(url):
                return
            captured = _parse_bearer(_authorization_header(request))
            if captured is None:
                return
            parsed = urlsplit(url)
            if parsed.hostname is None:
                captured.clear()
                return
            previous = self._files_bearer
            self._files_bearer = captured
            self._files_origin = f"https://{parsed.hostname.casefold()}"
            if previous is not None:
                previous.clear()
        except Exception:
            return

    def _take_files_bearer(self) -> _TransientBearer | None:
        bearer = self._files_bearer
        self._files_bearer = None
        return bearer

    def _clear_files_bearer(self) -> None:
        bearer = self._take_files_bearer()
        if bearer is not None:
            bearer.clear()

    def _observe_outlook_request(self, request: Any) -> None:
        if not self._capture_outlook_authorization or self._probing_outlook_identity:
            return
        try:
            url = request.url
            if not isinstance(url, str) or not _is_allowed_outlook_request_url(url):
                return
            captured = _parse_bearer(_authorization_header(request))
            if captured is None:
                return
            previous = self._outlook_bearer
            self._outlook_bearer = captured
            if previous is not None:
                previous.clear()
        except Exception:
            return

    def _take_outlook_bearer(self) -> _TransientBearer | None:
        bearer = self._outlook_bearer
        self._outlook_bearer = None
        return bearer

    def _clear_outlook_bearer(self) -> None:
        bearer = self._take_outlook_bearer()
        if bearer is not None:
            bearer.clear()

    def _probe_outlook_identity(
        self,
        page: Any,
        bearer: _TransientBearer,
        *,
        timeout_ms: int,
    ) -> object:
        del page
        return self._outlook_rest_get(
            ENDPOINTS.outlook_identity,
            bearer,
            params={"$select": "EmailAddress"},
            timeout_ms=timeout_ms,
        )

    def _outlook_rest_get(
        self,
        url: str,
        bearer: _TransientBearer,
        *,
        params: Mapping[str, str] | None,
        timeout_ms: int,
    ) -> dict[str, object]:
        return self._rest_get(
            url,
            bearer,
            params=params,
            timeout_ms=timeout_ms,
            allowed=_is_allowed_outlook_request_url,
        )

    def _rest_get(
        self,
        url: str,
        bearer: _TransientBearer,
        *,
        params: Mapping[str, str] | None,
        timeout_ms: int,
        allowed: Callable[[str], bool],
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        """Read-only Outlook REST call issued outside the page's cross-origin sandbox.

        The page is served from one Microsoft host while the REST endpoint lives on another,
        so an in-page fetch is subject to that origin's CORS policy and fails for reasons that
        have nothing to do with the session. Playwright's request context shares the session
        but is not bound by CORS, which makes the outcome depend only on the session itself.
        """
        if not allowed(url):
            return {"kind": "unavailable"}
        context = self._context
        if context is None:
            return {"kind": "unavailable"}
        self._probing_outlook_identity = True
        try:
            response = context.request.get(
                url,
                headers={
                    "Authorization": bearer.authorization_header(),
                    "Accept": "application/json",
                    **(dict(extra_headers) if extra_headers else {}),
                },
                params=dict(params) if params else None,
                timeout=timeout_ms,
                fail_on_status_code=False,
            )
        except Exception:
            return {"kind": "unavailable"}
        finally:
            self._probing_outlook_identity = False

        try:
            status = int(response.status)
            if status < 200 or status >= 300:
                return {"kind": "http_error", "status": status}
            payload = response.json()
        except Exception:
            return {"kind": "malformed"}
        finally:
            with suppress(Exception):
                response.dispose()

        if not isinstance(payload, Mapping):
            return {"kind": "malformed"}
        return {"kind": "ok", "payload": payload, "emailAddress": payload.get("EmailAddress")}

    def _rest_patch_categories(
        self,
        url: str,
        bearer: _TransientBearer,
        *,
        categories: tuple[str, ...],
        version: str,
        timeout_ms: int,
    ) -> dict[str, object]:
        """Send the single write: the category collection of one message, and nothing else.

        The body is built here from the names alone, so no caller can widen it to another
        property. Redirects are not followed, so the write and its authorization go only to the
        allowlisted endpoint that was named.
        """
        if not _is_allowed_outlook_request_url(url):
            return {"kind": "unavailable"}
        context = self._context
        if context is None:
            return {"kind": "unavailable"}
        self._probing_outlook_identity = True
        try:
            response = context.request.patch(
                url,
                headers={
                    "Authorization": bearer.authorization_header(),
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "If-Match": version,
                },
                data={"Categories": list(categories)},
                timeout=timeout_ms,
                fail_on_status_code=False,
                max_redirects=0,
            )
        except Exception:
            return {"kind": "unavailable"}
        finally:
            self._probing_outlook_identity = False

        try:
            status = int(response.status)
            if status < 200 or status >= 300:
                return {"kind": "http_error", "status": status}
            payload = response.json()
        except Exception:
            return {"kind": "malformed"}
        finally:
            with suppress(Exception):
                response.dispose()

        if not isinstance(payload, Mapping):
            return {"kind": "malformed"}
        return {"kind": "ok", "payload": payload}

    def _require_page(self) -> Any:
        if self._page is None:
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Playwright persistent context is not open",
            )
        return self._page


def validate_dedicated_profile(profile_dir: Path) -> None:
    audit_browser_profile_path(profile_dir, private_root=profile_dir.parent)


def _parse_bearer(value: object) -> _TransientBearer | None:
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return None
    match = _BEARER_AUTHORIZATION.fullmatch(value)
    if match is None:
        return None
    token = match.group(1)
    if not token or len(encoded) > len("Bearer ") + _MAX_OUTLOOK_BEARER_BYTES:
        return None
    captured = _TransientBearer(token)
    token = ""
    return captured


def _is_allowed_request_url(value: str, suffixes: tuple[str, ...]) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return False
    host = parsed.hostname.casefold().rstrip(".")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def _is_allowed_outlook_request_url(value: str) -> bool:
    return _is_allowed_request_url(value, _OUTLOOK_AUTHORIZATION_HOST_SUFFIXES)


def _outlook_timestamp(value: datetime) -> str:
    """Render a UTC instant in the literal form the Outlook REST filter expects."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_outlook_message(
    item: Mapping[str, object],
    *,
    folder: MailFolderName,
) -> MailMessage:
    """Normalize through the shared Microsoft layer so both routes return one record shape."""
    identifier = field(item, "id", "Id")
    source_id = identifier if isinstance(identifier, str) else ""
    return normalize_mail_message(
        item,
        provenance=microsoft_provenance(
            provider_id="microsoft365.browser",
            route=RouteKind.BROWSER,
            source_id=source_id,
            fetched_at=utc_now(),
            account_id=None,
            endpoint="outlook-rest-v2",
        ),
        folder=folder,
    )


def _collection(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    raw = payload.get("value")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _own_recording(item: Mapping[str, object]) -> _Recording | None:
    identifier = field(item, "id", "Id")
    drive = _drive_id_of(field(item, "parentReference", "ParentReference"))
    if not isinstance(identifier, str) or drive is None:
        return None
    name = field(item, "name", "Name")
    return _Recording(
        drive,
        identifier,
        name if isinstance(name, str) else None,
        optional_microsoft_datetime(field(item, "lastModifiedDateTime", "LastModifiedDateTime")),
        # `createdBy` on a recording in this account's own folder is the Teams service that wrote
        # the file, not whoever convened the meeting. Reporting it as the organizer would state
        # something the surface never said, so an own recording simply has none.
        None,
    )


def _shared_recording(item: Mapping[str, object]) -> _Recording | None:
    """Read one entry of the shared collection, which nests the real identifiers.

    A shared item's own id addresses the link in this account's drive, not the file. The pair
    that reaches the transcript lives under ``remoteItem``, which is why the sharer's drive is
    read from there rather than from the entry itself.
    """
    remote = field(item, "remoteItem", "RemoteItem")
    if not isinstance(remote, Mapping):
        return None
    identifier = field(remote, "id", "Id")
    drive = _drive_id_of(field(remote, "parentReference", "ParentReference"))
    if not isinstance(identifier, str) or drive is None:
        return None
    name = field(item, "name", "Name")
    shared = field(remote, "shared", "Shared")
    organizer = None
    if isinstance(shared, Mapping):
        organizer = _user_display_name(field(shared, "sharedBy", "SharedBy"))
    return _Recording(
        drive,
        identifier,
        name if isinstance(name, str) else None,
        optional_microsoft_datetime(field(item, "lastModifiedDateTime", "LastModifiedDateTime")),
        organizer or _user_display_name(field(item, "createdBy", "CreatedBy")),
    )


def _drive_id_of(reference: object) -> str | None:
    if not isinstance(reference, Mapping):
        return None
    drive = field(reference, "driveId", "DriveId")
    return drive if isinstance(drive, str) and drive.strip() else None


def _user_display_name(container: object) -> str | None:
    if not isinstance(container, Mapping):
        return None
    user = field(container, "user", "User")
    if not isinstance(user, Mapping):
        return None
    name = field(user, "displayName", "DisplayName")
    return name if isinstance(name, str) and name.strip() else None


def _recording_order(recording: _Recording) -> datetime:
    """Sort newest first, with undated recordings last rather than incomparable."""
    return recording.recorded_at or datetime.min.replace(tzinfo=UTC)


def _recording_in_window(recording: _Recording, window: DateWindow | None) -> bool:
    if window is None:
        return True
    if recording.recorded_at is None:
        return False
    return window.start <= recording.recorded_at < window.end


def _require_rest_payload(outcome: Mapping[str, object]) -> Mapping[str, object]:
    """Map one read-only REST outcome onto the typed failures the contract declares."""
    kind = outcome.get("kind")
    if kind == "ok":
        payload = outcome.get("payload")
        if isinstance(payload, Mapping):
            return payload
        raise ConnectorFailure(
            ReasonCode.BROWSER_EXTRACTION_FAILED,
            "Read-only collection returned a malformed response",
        )
    if kind == "http_error":
        status = outcome.get("status")
        if isinstance(status, int) and status in {401, 403}:
            raise ConnectorFailure(
                ReasonCode.BROWSER_IDENTITY_UNAVAILABLE,
                "Provider rejected the captured session authorization for a read-only call",
            )
        raise ConnectorFailure(
            ReasonCode.BROWSER_EXTRACTION_FAILED,
            "Provider rejected the bounded read-only collection call",
        )
    if kind == "malformed":
        raise ConnectorFailure(
            ReasonCode.BROWSER_EXTRACTION_FAILED,
            "Read-only collection returned a malformed response",
        )
    raise ConnectorFailure(
        ReasonCode.BROWSER_EXTRACTION_FAILED,
        "Read-only collection call did not complete",
    )


def _require_message_state(outcome: Mapping[str, object]) -> Mapping[str, object]:
    """Map the read that precedes a category write, naming a message the mailbox lacks."""
    if outcome.get("kind") == "http_error" and outcome.get("status") in {400, 404}:
        raise ConnectorFailure(
            ReasonCode.INVALID_INPUT,
            "Outlook found no message with that id in this mailbox; ids change when a message "
            "moves, so read the folder again for the current id",
        )
    return _require_rest_payload(outcome)


def _require_category_write(outcome: Mapping[str, object]) -> Mapping[str, object]:
    """Map the category write onto typed failures; none of them is ever reported as success."""
    kind = outcome.get("kind")
    if kind == "ok":
        payload = outcome.get("payload")
        if isinstance(payload, Mapping):
            return payload
    if kind == "http_error":
        status = outcome.get("status")
        if status in {409, 412}:
            raise ConnectorFailure(
                ReasonCode.WRITE_CONFLICT,
                "The message changed after it was read; nothing was written. Run the command "
                "again to apply it over the current categories",
            )
        if status == 401:
            raise ConnectorFailure(
                ReasonCode.BROWSER_IDENTITY_UNAVAILABLE,
                "Provider rejected the captured session authorization for the category write",
            )
        if status == 403:
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_DENIED,
                "Provider denied the category write for this mailbox",
            )
        if status in {400, 404}:
            raise ConnectorFailure(
                ReasonCode.INVALID_INPUT,
                "Outlook rejected the category write for that message id",
            )
        raise ConnectorFailure(
            ReasonCode.BROWSER_EXTRACTION_FAILED,
            "Provider rejected the bounded category write",
        )
    raise ConnectorFailure(
        ReasonCode.BROWSER_EXTRACTION_FAILED,
        "Category write did not complete or was not confirmed; read the message again before "
        "repeating it",
    )


def _category_assignment(
    message_id: str,
    categories: tuple[str, ...],
    previous: tuple[str, ...],
    *,
    changed: bool,
) -> MailCategoryAssignment:
    return MailCategoryAssignment(
        message_id,
        categories,
        previous,
        changed,
        microsoft_provenance(
            provider_id="microsoft365.browser",
            route=RouteKind.BROWSER,
            source_id=message_id,
            fetched_at=utc_now(),
            account_id=None,
            endpoint="outlook-rest-v2-message-categories",
        ),
    )


def _within_window(item: DriveFile, window: DateWindow | None) -> bool:
    if window is None:
        return True
    if item.modified_at is None:
        return False
    return window.start <= item.modified_at < window.end


def _launch_failure_message(exc: Exception) -> str:
    """Name the actual obstacle instead of always blaming a missing runtime.

    A dedicated profile still held by an earlier browser process produced the same message as an
    uninstalled runtime, which sent the reader to reinstall something that was already there.
    """
    text = str(exc).casefold()
    if any(marker in text for marker in ("singletonlock", "profile", "already in use", "lock")):
        return (
            "The dedicated browser profile is already in use by another process; close it and retry"
        )
    return "Playwright Chromium could not start; install the browser runtime"


def _drive_owner_identity(payload: Mapping[str, object]) -> str | None:
    owner = payload.get("owner")
    if not isinstance(owner, Mapping):
        return None
    user = owner.get("user")
    if not isinstance(user, Mapping):
        return None
    return unique_canonical_account_identity(user.get("email"), exact=True)


def _current_user_identity(payload: Mapping[str, object]) -> str | None:
    for key in ("Email", "UserPrincipalName"):
        identity = unique_canonical_account_identity(payload.get(key), exact=True)
        if identity is not None:
            return identity
    return None


_CONFIRMED_ATTENDANCE = frozenset({"accepted", "organizer"})


def _attendance_confirmed(event: CalendarEvent) -> bool:
    """Keep only meetings the account actually committed to.

    Microsoft reports the organizer's own meetings as "organizer" rather than "accepted", and a
    tentative or unanswered invitation is not attendance.
    """
    if event.is_cancelled:
        return False
    status = (event.response_status or "").strip().casefold()
    return status in _CONFIRMED_ATTENDANCE


def _authorization_header(request: Any) -> object:
    all_headers = getattr(request, "all_headers", None)
    headers = all_headers() if callable(all_headers) else request.headers
    if not isinstance(headers, Mapping):
        return None
    return next(
        (
            value
            for name, value in headers.items()
            if isinstance(name, str) and name.casefold() == "authorization"
        ),
        None,
    )


def _is_allowed_files_request_url(value: str) -> bool:
    """Accept only HTTPS drive hosts; the mail authorization is not valid here."""
    return _is_allowed_request_url(value, _FILES_AUTHORIZATION_HOST_SUFFIXES)


def _outlook_identity_failure(message: str) -> ConnectorFailure:
    return ConnectorFailure(ReasonCode.BROWSER_IDENTITY_UNAVAILABLE, message)


def _any_visible(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                return True
        except Exception:
            continue
    return False


def _identity_menu_is_open(page: Any, selectors: Microsoft365Selectors) -> bool:
    if _any_visible(page, selectors.identity_controls):
        return True
    for selector in selectors.profile_controls:
        try:
            locator = page.locator(selector)
            count = locator.count()
        except Exception:
            continue
        for index in range(count):
            try:
                control = locator.nth(index)
                if not control.is_visible():
                    continue
                expanded = control.get_attribute("aria-expanded")
                if expanded is not None and expanded.strip().casefold() == "true":
                    return True
            except Exception:
                continue
    return False


def _visible_identity_hints(
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
                if not control.is_visible():
                    continue
                for attribute in attributes:
                    value = control.get_attribute(attribute)
                    if value:
                        hints.append(str(value))
                text = control.inner_text()
                if text:
                    hints.append(str(text))
            except Exception:
                continue
    return tuple(dict.fromkeys(hints))
