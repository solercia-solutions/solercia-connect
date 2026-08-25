from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from solercia_connect.auth import BearerTokenProvider
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    DateWindow,
    MailQuery,
    ProviderFamily,
    ReasonCode,
    RouteKind,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.m365_browser.driver import (
    _is_allowed_outlook_request_url,
    _TransientBearer,
)
from solercia_connect.providers.microsoft_graph import MicrosoftGraphAdapter
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import ConnectorRuntime
from solercia_connect.secrets import REDACTED, SecretValue, redact_text
from solercia_connect.serialization import to_json
from tests.fakes import NOW, FakeAdapter, FakeTransport, json_response, mail_record


def test_secret_wrapper_never_prints_value() -> None:
    secret = SecretValue("super-secret-token")

    assert str(secret) == REDACTED
    assert "super-secret-token" not in repr(secret)
    assert secret.reveal() == "super-secret-token"


def test_redactor_handles_headers_assignments_and_known_values() -> None:
    raw = "Authorization: Bearer abc123 APP_TOKEN=xyz PASSWORD=hunter2 opaque-value"

    redacted = redact_text(raw, ["opaque-value"])

    assert "abc123" not in redacted
    assert "xyz" not in redacted
    assert "hunter2" not in redacted
    assert "opaque-value" not in redacted


def test_raw_provider_error_body_is_not_exposed() -> None:
    transport = FakeTransport(json_response(401, {"error": "Authorization: Bearer leaked-token"}))
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("leaked-token")),
        expected_account="user@example.com",
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.health()

    assert captured.value.reason is ReasonCode.INVALID_CREDENTIALS
    assert "leaked-token" not in str(captured.value)


def test_unknown_exception_text_does_not_enter_receipt() -> None:
    adapter = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        RuntimeError("Authorization: Bearer leaked-token"),
    )
    registry = ProviderRegistry()
    registry.register(adapter)
    window = DateWindow(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    request = ConnectorRequest.create(
        "microsoft.graph", Capability.MAIL_SENT_LIST, MailQuery(window)
    )

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(request)
    rendered = to_json(outcome)

    assert outcome.receipt.final_reason is ReasonCode.UNKNOWN_ERROR
    assert "leaked-token" not in rendered


def test_serialization_redacts_secret_dataclass() -> None:
    rendered = to_json({"token": SecretValue("never-print-me")})

    assert "never-print-me" not in rendered
    assert REDACTED in rendered


def test_browser_source_contains_no_dom_storage_or_cookie_token_capture() -> None:
    source_root = Path(__file__).parents[1] / "src/solercia_connect/providers/m365_browser"
    source = "\n".join(path.read_text() for path in source_root.rglob("*.py"))

    forbidden_capture_surfaces = (
        "window.__ot",
        "localStorage",
        "sessionStorage",
        "document.cookie",
        ".cookies(",
        "storage_state",
    )
    for marker in forbidden_capture_surfaces:
        assert marker not in source


def test_transient_bearer_never_reveals_and_does_not_survive_clearing() -> None:
    """SECURITY.md: held only in memory, zeroed after use, never printable."""
    bearer = _TransientBearer("opaque-session-token")

    assert "opaque-session-token" not in repr(bearer)
    assert bearer.authorization_header() == "Bearer opaque-session-token"

    bearer.clear()

    assert bearer.cleared is True
    with pytest.raises(RuntimeError):
        bearer.authorization_header()


@pytest.mark.parametrize(
    "url",
    [
        "http://outlook.office.com/api/v2.0/me",
        "https://outlook.office.com.evil.test/api/v2.0/me",
        "https://evil.test/api/v2.0/me",
        "https://user:pass@outlook.office.com/api/v2.0/me",
        "https://outlook.office.com:8443/api/v2.0/me",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "https://outlook.office.com@evil.test/api/v2.0/me",
    ],
)
def test_session_authorization_is_never_read_outside_the_allowlist(url: str) -> None:
    """SECURITY.md: read only from HTTPS requests whose host matches the allowlist."""
    assert _is_allowed_outlook_request_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://outlook.office.com/api/v2.0/me",
        "https://outlook.cloud.microsoft/api/v2.0/me",
        "https://substrate.office.com/api/beta/me",
        "https://OUTLOOK.OFFICE.COM/api/v2.0/me",
    ],
)
def test_session_authorization_is_read_from_allowlisted_provider_hosts(url: str) -> None:
    assert _is_allowed_outlook_request_url(url) is True


def test_success_receipt_contains_no_provider_credentials() -> None:
    adapter = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        (mail_record(),),
    )
    registry = ProviderRegistry()
    registry.register(adapter)
    request = ConnectorRequest.create(
        "microsoft.graph",
        Capability.MAIL_SENT_LIST,
        MailQuery(
            DateWindow(
                datetime(2026, 8, 1, tzinfo=UTC),
                datetime(2026, 9, 1, tzinfo=UTC),
            )
        ),
    )

    rendered = to_json(ConnectorRuntime(registry, clock=lambda: NOW).execute(request))

    assert "token" not in rendered.lower()
    assert "cookie" not in rendered.lower()
