from __future__ import annotations

import base64
import importlib
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from solercia_connect.browser_profile import (
    audit_browser_profile_path,
    ensure_browser_profile_path,
)
from solercia_connect.config import ConnectionType
from solercia_connect.domain import (
    Capability,
    DateWindow,
    DomainRecord,
    DriveFile,
    FileQuery,
    MailMessage,
    MailQuery,
    QueryPayload,
    ReasonCode,
    RouteKind,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers._microsoft import (
    field,
    microsoft_provenance,
    normalize_drive_item,
    normalize_mail_attachment,
    normalize_mail_message,
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
_SENT_MAIL_SELECT = (
    "Id,Subject,SentDateTime,From,ToRecipients,CcRecipients,Body,ConversationId,HasAttachments"
)
_BEARER_AUTHORIZATION = re.compile(
    r"Bearer[ \t]+([A-Z0-9._~+/-]+=*)",
    re.IGNORECASE,
)


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
        if capability in {Capability.FILES_SEARCH, Capability.FILES_GET} and isinstance(
            payload, FileQuery
        ):
            return self._read_files(
                payload,
                timeout_ms=timeout_ms,
                with_content=capability is Capability.FILES_GET,
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
        messages: list[MailMessage] = []
        url: str | None = ENDPOINTS.outlook_sent_messages
        params: Mapping[str, str] | None = {
            "$filter": (
                f"SentDateTime ge {_outlook_timestamp(query.window.start)}"
                f" and SentDateTime lt {_outlook_timestamp(query.window.end)}"
            ),
            "$select": _SENT_MAIL_SELECT,
            "$orderby": "SentDateTime asc",
            "$top": str(min(_OUTLOOK_PAGE_SIZE, query.limit)),
        }

        for _ in range(_OUTLOOK_MAX_PAGES):
            if url is None or len(messages) >= query.limit:
                break
            payload = self._authorized_outlook_get(url, params=params, timeout_ms=timeout_ms)
            items = payload.get("value")
            if not isinstance(items, list):
                raise ConnectorFailure(
                    ReasonCode.BROWSER_EXTRACTION_FAILED,
                    "Outlook sent-mail collection returned an unexpected payload shape",
                )
            for item in items:
                if len(messages) >= query.limit:
                    break
                if isinstance(item, Mapping):
                    messages.append(_normalize_outlook_message(item))
            next_link = payload.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) else None
            params = None

        if query.include_attachments:
            messages = [
                self._message_with_attachments(message, query, timeout_ms=timeout_ms)
                for message in messages
            ]
        return tuple(messages)

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
        if query.text is not None:
            escaped = query.text.strip().replace("'", "''")
            return f"{drive_url}/root/search(q='{quote(escaped, safe='')}')", params
        if query.folder_path is not None:
            path = query.folder_path.strip().strip("/")
            return f"{drive_url}/root:/{quote(path, safe='/')}:/children", params
        return f"{drive_url}/root/children", params

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
    ) -> Mapping[str, object]:
        bearer = self._await_files_bearer(timeout_ms=timeout_ms)
        try:
            outcome = self._rest_get(
                url,
                bearer,
                params=params,
                timeout_ms=timeout_ms,
                allowed=_is_allowed_files_request_url,
            )
        finally:
            bearer.clear()
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
    ) -> Mapping[str, object]:
        """Issue one read-only Outlook REST call with a freshly captured authorization."""
        bearer = self._await_outlook_bearer(timeout_ms=timeout_ms)
        try:
            outcome = self._outlook_rest_get(url, bearer, params=params, timeout_ms=timeout_ms)
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


def _normalize_outlook_message(item: Mapping[str, object]) -> MailMessage:
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
    )


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
