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


def test_browser_source_contains_no_token_capture_implementation() -> None:
    source_root = Path(__file__).parents[1] / "src/solercia_connect/providers/m365_browser"
    source = "\n".join(path.read_text() for path in source_root.rglob("*.py"))

    assert "window.__ot" not in source
    assert "localStorage" not in source
    assert "document.cookie" not in source
    assert "Authorization" not in source


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
