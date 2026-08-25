from __future__ import annotations

import html
import re
from datetime import UTC, datetime, tzinfo
from typing import Any, cast
from urllib.parse import ParseResult, urlparse

from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure

_HTML_TAG = re.compile(r"<[^>]+>")
_ACCOUNT_IDENTITY = re.compile(
    r"(?<![A-Z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"([A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+)"
    r"(?![A-Z0-9.-])",
    re.IGNORECASE,
)


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            f"Provider field is not an object: {label}",
        )
    return cast(dict[str, Any], value)


def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            f"Provider field is not a list: {label}",
        )
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            f"Provider field is not a non-empty string: {label}",
        )
    return value


def parse_datetime(value: Any, label: str, *, default_timezone: tzinfo = UTC) -> datetime:
    text = require_string(value, label)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            f"Provider datetime is malformed: {label}",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed


def parse_optional_datetime(
    value: Any, label: str, *, default_timezone: tzinfo = UTC
) -> datetime | None:
    if value in (None, ""):
        return None
    return parse_datetime(value, label, default_timezone=default_timezone)


def ensure_https_base_url(value: str, label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{label} must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain credentials, query parameters, or fragments")
    return value.rstrip("/")


def ensure_official_https_base_url(value: str, label: str, official_base_url: str) -> str:
    candidate = ensure_https_base_url(value, label)
    official = ensure_https_base_url(official_base_url, label)
    parsed_candidate = urlparse(candidate)
    parsed_official = urlparse(official)
    if _https_origin(parsed_candidate, label) != _https_origin(
        parsed_official, label
    ) or parsed_candidate.path.rstrip("/") != parsed_official.path.rstrip("/"):
        raise ValueError(f"{label} must use the documented v0.1 official endpoint")
    return candidate


def require_same_origin_https_url(value: str, trusted_base_url: str, label: str) -> str:
    parsed = urlparse(value)
    trusted = urlparse(trusted_base_url)
    try:
        candidate_origin = _https_origin(parsed, label)
        trusted_origin = _https_origin(trusted, label)
    except ValueError as exc:
        raise ConnectorFailure(
            ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
            f"Provider supplied an untrusted credential destination: {label}",
        ) from exc
    if parsed.username or parsed.password or parsed.fragment or candidate_origin != trusted_origin:
        raise ConnectorFailure(
            ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
            f"Provider supplied an untrusted credential destination: {label}",
        )
    return value


def _https_origin(parsed: ParseResult, label: str) -> tuple[str, str, int]:
    if parsed.scheme.lower() != "https" or parsed.hostname is None:
        raise ValueError(f"{label} must be an absolute HTTPS URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} contains an invalid port") from exc
    if port not in (None, 443):
        raise ValueError(f"{label} must use the default HTTPS port")
    return "https", parsed.hostname.casefold(), 443


def clean_html_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    return re.sub(r"\s+", " ", _HTML_TAG.sub(" ", text)).strip()


def as_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def lower_domain(address: str | None) -> str:
    if not address or "@" not in address:
        return ""
    return address.rsplit("@", 1)[1].lower()


def canonical_account_identities(value: Any) -> frozenset[str]:
    """Return every case-insensitively canonicalized account identity in ``value``."""
    if not isinstance(value, str):
        return frozenset()
    candidate = value.strip()
    return frozenset(match.group(1).casefold() for match in _ACCOUNT_IDENTITY.finditer(candidate))


def unique_canonical_account_identity(value: Any, *, exact: bool) -> str | None:
    """Return one case-insensitive account identity or fail closed with ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    identities = canonical_account_identities(candidate)
    if len(identities) != 1:
        return None
    if exact and _ACCOUNT_IDENTITY.fullmatch(candidate) is None:
        return None
    return next(iter(identities))
