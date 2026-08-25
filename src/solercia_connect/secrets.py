from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

REDACTED = "<redacted>"

_HEADER_PATTERN = re.compile(
    r"(?i)(authorization|proxy-authorization|app-token|session-token)\s*[:=]\s*"
    r"(?:bearer\s+|basic\s+|user_token\s+)?[^\s,;]+"
)
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|COOKIE|API_KEY)[A-Z0-9_]*)"
    r"\s*[:=]\s*[^\s,;]+"
)


@dataclass(frozen=True, slots=True)
class SecretValue:
    """A deliberately non-printable secret wrapper for injected credentials."""

    _value: str

    def __post_init__(self) -> None:
        if not self._value:
            raise ValueError("A secret value cannot be empty")

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    def __str__(self) -> str:
        return REDACTED


def redact_text(value: object, secrets: Iterable[str] = ()) -> str:
    """Redact common credential forms and caller-known secret values."""

    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _HEADER_PATTERN.sub(lambda match: f"{match.group(1)}: {REDACTED}", text)
    return _ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
