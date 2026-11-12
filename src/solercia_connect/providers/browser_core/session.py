"""Vendor-neutral Playwright session pieces shared by every family's browser driver.

Nothing here knows a provider: hosts, endpoints, identity probes and record shapes stay in each
family package. What lives here is the discipline every family shares: a dedicated profile
audited before launch, downloads refused, a session authorization held only transiently and read
only from allowlisted HTTPS hosts, and a close that always releases the browser.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from solercia_connect.browser_profile import (
    audit_browser_profile_path,
    ensure_browser_profile_path,
)
from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure

MAX_BEARER_BYTES = 8_192
_BEARER_AUTHORIZATION = re.compile(
    r"Bearer[ \t]+([A-Z0-9._~+/-]+=*)",
    re.IGNORECASE,
)


class TransientBearer:
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
        return "TransientBearer(<redacted>)"

    def __del__(self) -> None:
        self.clear()


class PlaywrightSession:
    """One persistent Chromium context over a dedicated, audited profile.

    A family driver extends this with its own hosts, observers and reads. The flag
    ``_issuing_session_request`` is raised while the session issues its own call, so a request
    observer can tell the session's traffic apart from the provider page's.
    """

    def __init__(self) -> None:
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._issuing_session_request = False

    def _open_persistent_context(
        self,
        profile_dir: Path,
        *,
        headless: bool,
        on_page: Callable[[], None] | None = None,
    ) -> None:
        """Launch over the audited profile, acquire the page, then let the family attach to it.

        ``on_page`` runs once the page exists; any failure there closes the browser before the
        typed failure is raised, so a half-initialized context never stays open.
        """
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
                launch_failure_message(exc),
            ) from exc
        try:
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            if on_page is not None:
                on_page()
        except Exception as exc:
            with suppress(Exception):
                self.close()
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Playwright Chromium context could not initialize",
            ) from exc

    def navigate(self, url: str, *, timeout_ms: int) -> None:
        self._require_page().goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    def wait(self, milliseconds: int) -> None:
        self._require_page().wait_for_timeout(milliseconds)

    def close(self) -> None:
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

    def _rest_get(
        self,
        url: str,
        bearer: TransientBearer,
        *,
        params: Mapping[str, str] | None,
        timeout_ms: int,
        allowed: Callable[[str], bool],
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        """Read-only REST call issued outside the page's cross-origin sandbox.

        The page is served from one provider host while the REST endpoint may live on another,
        so an in-page fetch is subject to that origin's CORS policy and fails for reasons that
        have nothing to do with the session. Playwright's request context shares the session
        but is not bound by CORS, which makes the outcome depend only on the session itself.
        ``allowed`` is the explicit host allowlist: nothing is sent to a URL it refuses.
        """
        if not allowed(url):
            return {"kind": "unavailable"}
        context = self._context
        if context is None:
            return {"kind": "unavailable"}
        self._issuing_session_request = True
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
            self._issuing_session_request = False

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


def parse_bearer(value: object, *, max_bytes: int = MAX_BEARER_BYTES) -> TransientBearer | None:
    """Capture one ASCII ``Bearer`` authorization of at most ``max_bytes`` token bytes."""
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
    if not token or len(encoded) > len("Bearer ") + max_bytes:
        return None
    captured = TransientBearer(token)
    token = ""
    return captured


def is_allowed_request_url(value: str, suffixes: tuple[str, ...]) -> bool:
    """Accept only HTTPS on the default port, without credentials, on or below a suffix."""
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


def authorization_header(request: Any) -> object:
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


def launch_failure_message(exc: Exception) -> str:
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
