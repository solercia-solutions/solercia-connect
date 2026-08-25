from __future__ import annotations

import importlib
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from solercia_connect.browser_profile import (
    audit_browser_profile_path,
    ensure_browser_profile_path,
)
from solercia_connect.config import ConnectionType
from solercia_connect.domain import Capability, DomainRecord, QueryPayload, ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers._shared import unique_canonical_account_identity
from solercia_connect.providers.m365_browser.selectors import ENDPOINTS, Microsoft365Selectors

_OUTLOOK_AUTHORIZATION_HOST_SUFFIXES = (
    "outlook.cloud.microsoft",
    "outlook.office.com",
    "substrate.office.com",
)
_MAX_OUTLOOK_BEARER_BYTES = 8_192
_BEARER_AUTHORIZATION = re.compile(
    r"Bearer[ \t]+([A-Z0-9._~+/-]+=*)",
    re.IGNORECASE,
)
_OUTLOOK_IDENTITY_PROBE = """
async ({ endpoint, method, authorization, timeoutMs }) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(endpoint, {
      method,
      headers: { Authorization: authorization, Accept: "application/json" },
      body: undefined,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      signal: controller.signal,
    });
    if (!response.ok) return { kind: "http_error", status: response.status };
    let payload;
    try {
      payload = await response.json();
    } catch (_) {
      return { kind: "malformed" };
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      return { kind: "malformed" };
    }
    return { kind: "ok", emailAddress: payload.EmailAddress };
  } catch (_) {
    return { kind: "unavailable" };
  } finally {
    clearTimeout(timer);
  }
}
"""


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

    def open_persistent(
        self,
        profile_dir: Path,
        *,
        headless: bool,
        connection_type: ConnectionType | None = None,
    ) -> None:
        self._capture_outlook_authorization = False
        self._clear_outlook_bearer()
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
                "Playwright Chromium could not start; install the browser runtime",
            ) from exc
        try:
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            if connection_type is ConnectionType.OUTLOOK:
                self._install_outlook_request_observers()
                self._capture_outlook_authorization = True
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
        del capability, payload, selectors, timeout_ms
        raise ConnectorFailure(
            ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED,
            "Built-in Microsoft 365 browser extraction is not live-validated in v0.1",
        )

    def close(self) -> None:
        self._capture_outlook_authorization = False
        self._clear_outlook_bearer()
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

    def _install_outlook_request_observers(self) -> None:
        for emitter in (self._context, self._page):
            try:
                emitter.on("request", self._observe_outlook_request)
            except Exception:
                continue

    def _observe_outlook_request(self, request: Any) -> None:
        if not self._capture_outlook_authorization or self._probing_outlook_identity:
            return
        try:
            url = request.url
            if not isinstance(url, str) or not _is_allowed_outlook_request_url(url):
                return
            all_headers = getattr(request, "all_headers", None)
            headers = all_headers() if callable(all_headers) else request.headers
            if not isinstance(headers, Mapping):
                return
            authorization = next(
                (
                    value
                    for name, value in headers.items()
                    if isinstance(name, str) and name.casefold() == "authorization"
                ),
                None,
            )
            captured = _parse_bearer(authorization)
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
        authorization = bearer.authorization_header()
        payload: dict[str, object] = {
            "endpoint": ENDPOINTS.outlook_identity,
            "method": "GET",
            "authorization": authorization,
            "timeoutMs": timeout_ms,
        }
        self._probing_outlook_identity = True
        try:
            return page.evaluate(_OUTLOOK_IDENTITY_PROBE, payload)
        except Exception:
            return {"kind": "unavailable"}
        finally:
            self._probing_outlook_identity = False
            payload["authorization"] = ""
            authorization = ""

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


def _is_allowed_outlook_request_url(value: str) -> bool:
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
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _OUTLOOK_AUTHORIZATION_HOST_SUFFIXES
    )


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
