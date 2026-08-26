from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from solercia_connect.config import ConnectionType
from solercia_connect.domain import (
    CalendarEvent,
    Capability,
    ConnectorRequest,
    DateWindow,
    MailQuery,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    StepOutcome,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.m365_browser import Microsoft365BrowserAdapter
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import ConnectorRuntime, RoutePolicy
from tests.fakes import (
    NOW,
    FakeAdapter,
    FakeBrowserDriver,
    authenticated_snapshot,
    mail_record,
)


def request() -> ConnectorRequest:
    window = DateWindow(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    return ConnectorRequest.create(
        "microsoft.graph",
        Capability.MAIL_SENT_LIST,
        MailQuery(window, account_id="user@example.com"),
    )


def test_inbox_mail_is_allowlisted_for_the_microsoft_browser_fallback() -> None:
    window = DateWindow(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    inbox_request = ConnectorRequest.create(
        "microsoft.graph",
        Capability.MAIL_INBOX_LIST,
        MailQuery(window, account_id="user@example.com"),
    )
    failure = ConnectorFailure(
        ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE,
        "Official adapter does not expose the requested capability",
    )
    policy = RoutePolicy()

    assert Capability.MAIL_INBOX_LIST in policy.browser_fallback_capabilities
    assert policy.allows_browser(inbox_request, failure, ProviderFamily.MICROSOFT) is True
    assert policy.allows_browser(inbox_request, failure, ProviderFamily.GOOGLE) is False


def test_official_success_never_calls_browser(tmp_path: Path) -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        (mail_record(),),
    )
    browser_driver = FakeBrowserDriver([authenticated_snapshot()], (mail_record(),))
    browser = Microsoft365BrowserAdapter(
        browser_driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
    )
    registry = ProviderRegistry()
    registry.register(official)
    registry.register(browser)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        request(), browser_fallback_enabled=True
    )

    assert outcome.ok is True
    assert outcome.receipt.selected_route is RouteKind.OFFICIAL
    assert official.calls == 1
    assert browser_driver.read_calls == 0
    assert [step.outcome for step in outcome.receipt.steps] == [StepOutcome.SUCCESS]


def test_retry_is_fixed_and_bounded() -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        ConnectorFailure(ReasonCode.RATE_LIMITED, "Rate limited"),
        ConnectorFailure(ReasonCode.NETWORK_TIMEOUT, "Timed out"),
        (mail_record(),),
    )
    registry = ProviderRegistry()
    registry.register(official)
    delays: list[float] = []

    outcome = ConnectorRuntime(
        registry,
        sleeper=delays.append,
        clock=lambda: NOW,
    ).execute(request())

    assert outcome.ok is True
    assert official.calls == 3
    assert delays == [0.0, 0.1]
    assert [step.outcome for step in outcome.receipt.steps] == [
        StepOutcome.RETRY,
        StepOutcome.RETRY,
        StepOutcome.SUCCESS,
    ]


def test_allowlisted_reason_uses_fake_browser_driver(tmp_path: Path) -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        ConnectorFailure(ReasonCode.ADMIN_CONSENT_REQUIRED, "Admin consent required"),
    )
    browser_driver = FakeBrowserDriver(
        [authenticated_snapshot("user@example.com")],
        (mail_record("unbound", route=RouteKind.BROWSER),),
    )
    browser = Microsoft365BrowserAdapter(
        browser_driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=tmp_path / "m365",
        expected_account="user@example.com",
        clock=lambda: NOW,
    )
    registry = ProviderRegistry()
    registry.register(official)
    registry.register(browser)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        request(), browser_fallback_enabled=True
    )

    assert outcome.ok is True
    assert outcome.receipt.selected_route is RouteKind.BROWSER
    assert browser_driver.read_calls == 1
    assert outcome.records[0].provenance.provider_id == "microsoft365.browser"
    assert [step.reason for step in outcome.receipt.steps] == [
        ReasonCode.ADMIN_CONSENT_REQUIRED,
        ReasonCode.SUCCESS,
    ]


def test_invalid_credentials_deny_browser_even_when_enabled(tmp_path: Path) -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        ConnectorFailure(ReasonCode.INVALID_CREDENTIALS, "Credentials rejected"),
    )
    browser_driver = FakeBrowserDriver([authenticated_snapshot()], (mail_record(),))
    browser = Microsoft365BrowserAdapter(
        browser_driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=tmp_path / "m365",
        expected_account="user@example.com",
    )
    registry = ProviderRegistry()
    registry.register(official)
    registry.register(browser)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        request(), browser_fallback_enabled=True
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.INVALID_CREDENTIALS
    assert browser_driver.read_calls == 0
    assert outcome.receipt.steps[-1].reason is ReasonCode.BROWSER_FALLBACK_DENIED


@pytest.mark.parametrize(
    "reason",
    [
        ReasonCode.AUTHORIZATION_DENIED,
        ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
        ReasonCode.MALFORMED_PROVIDER_DATA,
        ReasonCode.REQUEST_REJECTED,
    ],
)
def test_sensitive_official_failures_never_fallback(reason: ReasonCode, tmp_path: Path) -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        ConnectorFailure(reason, "Safe failure"),
    )
    browser_driver = FakeBrowserDriver([authenticated_snapshot()], (mail_record(),))
    browser = Microsoft365BrowserAdapter(
        browser_driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=tmp_path / "m365",
        expected_account="user@example.com",
    )
    registry = ProviderRegistry()
    registry.register(official)
    registry.register(browser)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        request(), browser_fallback_enabled=True
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is reason
    assert browser_driver.read_calls == 0


def test_unknown_exception_fails_closed_without_browser(tmp_path: Path) -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        RuntimeError("unexpected secret-bearing provider failure"),
    )
    browser_driver = FakeBrowserDriver([authenticated_snapshot()], (mail_record(),))
    browser = Microsoft365BrowserAdapter(
        browser_driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=tmp_path / "m365",
        expected_account="user@example.com",
    )
    registry = ProviderRegistry()
    registry.register(official)
    registry.register(browser)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        request(), browser_fallback_enabled=True
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.UNKNOWN_ERROR
    assert browser_driver.read_calls == 0
    assert "secret-bearing" not in str(outcome.receipt)


def test_wrong_record_type_fails_closed_without_browser(tmp_path: Path) -> None:
    wrong_record = CalendarEvent(
        "event-1",
        "Wrong contract",
        NOW,
        NOW,
        None,
        (),
        "accepted",
        False,
        False,
        Provenance(
            "microsoft.graph",
            ProviderFamily.MICROSOFT,
            RouteKind.OFFICIAL,
            "event-1",
            NOW,
        ),
    )
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        (wrong_record,),
    )
    browser_driver = FakeBrowserDriver([authenticated_snapshot()], (mail_record(),))
    browser = Microsoft365BrowserAdapter(
        browser_driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=tmp_path / "m365",
        expected_account="user@example.com",
    )
    registry = ProviderRegistry()
    registry.register(official)
    registry.register(browser)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        request(), browser_fallback_enabled=True
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.MALFORMED_PROVIDER_DATA
    assert browser_driver.read_calls == 0


@pytest.mark.parametrize(
    ("record", "expected_reason"),
    [
        (
            mail_record(provider_id="forged.provider"),
            ReasonCode.MALFORMED_PROVIDER_DATA,
        ),
        (
            mail_record(family=ProviderFamily.GOOGLE),
            ReasonCode.MALFORMED_PROVIDER_DATA,
        ),
        (
            mail_record(route=RouteKind.BROWSER),
            ReasonCode.MALFORMED_PROVIDER_DATA,
        ),
        (
            mail_record(account_id="attacker@example.com"),
            ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
        ),
    ],
    ids=["provider", "family", "official-route", "account"],
)
def test_official_records_with_forged_provenance_fail_closed(
    record: object,
    expected_reason: ReasonCode,
) -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        (record,),
    )
    registry = ProviderRegistry()
    registry.register(official)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(request())

    assert outcome.ok is False
    assert outcome.records == ()
    assert outcome.receipt.final_reason is expected_reason
    assert all(step.outcome is not StepOutcome.SUCCESS for step in outcome.receipt.steps)


def test_browser_record_with_forged_official_provenance_fails_closed() -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        ConnectorFailure(ReasonCode.ADMIN_CONSENT_REQUIRED, "Admin consent required"),
    )
    browser = FakeAdapter(
        "microsoft365.browser",
        ProviderFamily.MICROSOFT,
        RouteKind.BROWSER,
        frozenset({Capability.MAIL_SENT_LIST}),
        (mail_record(provider_id="microsoft365.browser", route=RouteKind.OFFICIAL),),
    )
    registry = ProviderRegistry()
    registry.register(official)
    registry.register(browser)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        request(),
        browser_fallback_enabled=True,
    )

    assert outcome.ok is False
    assert outcome.records == ()
    assert outcome.receipt.final_reason is ReasonCode.MALFORMED_PROVIDER_DATA
    assert outcome.receipt.steps[-1].provider_id == "microsoft365.browser"


def test_missing_official_capability_is_recorded_before_disabled_fallback() -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset(),
    )
    registry = ProviderRegistry()
    registry.register(official)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(request())

    assert outcome.ok is False
    assert official.calls == 0
    assert [step.reason for step in outcome.receipt.steps] == [
        ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE,
        ReasonCode.BROWSER_FALLBACK_DISABLED,
    ]


def test_browser_fallback_is_denied_for_non_microsoft_family() -> None:
    official = FakeAdapter(
        "google.gmail",
        ProviderFamily.GOOGLE,
        RouteKind.OFFICIAL,
        frozenset(),
    )
    browser = FakeAdapter(
        "google.browser",
        ProviderFamily.GOOGLE,
        RouteKind.BROWSER,
        frozenset({Capability.MAIL_SENT_LIST}),
        (mail_record("google.browser", ProviderFamily.GOOGLE, RouteKind.BROWSER),),
    )
    registry = ProviderRegistry()
    registry.register(official)
    registry.register(browser)
    google_request = ConnectorRequest.create(
        "google.gmail",
        Capability.MAIL_SENT_LIST,
        request().payload,
    )

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        google_request,
        browser_fallback_enabled=True,
    )

    assert outcome.ok is False
    assert browser.calls == 0
    assert outcome.receipt.steps[-1].reason is ReasonCode.BROWSER_FALLBACK_DENIED


def test_browser_fallback_must_be_explicit() -> None:
    official = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        ConnectorFailure(ReasonCode.ADMIN_CONSENT_REQUIRED, "Admin consent required"),
    )
    registry = ProviderRegistry()
    registry.register(official)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(request())

    assert outcome.ok is False
    assert outcome.receipt.steps[-1].reason is ReasonCode.BROWSER_FALLBACK_DISABLED


def test_receipt_id_is_deterministic_for_same_trace() -> None:
    def run_once() -> str:
        adapter = FakeAdapter(
            "microsoft.graph",
            ProviderFamily.MICROSOFT,
            RouteKind.OFFICIAL,
            frozenset({Capability.MAIL_SENT_LIST}),
            (mail_record(),),
        )
        registry = ProviderRegistry()
        registry.register(adapter)
        return ConnectorRuntime(registry, clock=lambda: NOW).execute(request()).receipt.receipt_id

    assert run_once() == run_once()
