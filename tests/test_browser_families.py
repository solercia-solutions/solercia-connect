"""Browser routes per family: dedicated profile roots and deterministic adapter selection."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from solercia_connect.browser_profile import (
    audit_browser_profile_path,
    browser_profile_path,
    browser_profile_root,
    microsoft_browser_profile_path,
    microsoft_browser_profile_root,
)
from solercia_connect.config import TARGET_SPECS, ConnectSettings, load_settings
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    DateWindow,
    ExternalIssue,
    IssueQuery,
    MailQuery,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    official_provider_family,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers import (
    GlpiAdapter,
    GoogleCalendarAdapter,
    GoogleGmailAdapter,
    GoogleMeetAdapter,
    JiraAdapter,
    MicrosoftGraphAdapter,
)
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import ConnectorRuntime, RoutePolicy
from tests.fakes import NOW, FakeAdapter, mail_record

_ACCOUNT = "user@example.com"
_SITE = "https://acme.atlassian.net"


def _settings(tmp_path: Path, **overrides: str) -> ConnectSettings:
    return load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            **overrides,
        }
    )


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# --- Profile roots and paths ---------------------------------------------------------------


def test_microsoft_profile_path_is_byte_identical_to_the_pre_family_formula(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    # Pre-refactor: (m365_profile_dir or data_dir / "m365") / sha256(normalized account)[:16].
    expected = settings.data_dir / "m365" / "b4c9a289323b21a0"

    assert microsoft_browser_profile_path(settings, " User@Example.COM ") == expected
    assert browser_profile_path(settings, ProviderFamily.MICROSOFT, _ACCOUNT) == expected
    assert microsoft_browser_profile_root(settings) == settings.data_dir / "m365"


def test_microsoft_explicit_profile_root_is_still_honoured(tmp_path: Path) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_M365_PROFILE_DIR=str(tmp_path / "m"))

    assert microsoft_browser_profile_path(settings, _ACCOUNT) == (
        tmp_path / "m" / _digest(_ACCOUNT)
    )


def test_each_family_has_its_own_default_root_and_none_nests_in_another(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    roots = {
        family: browser_profile_root(settings, family)
        for family in (ProviderFamily.MICROSOFT, ProviderFamily.GOOGLE, ProviderFamily.JIRA)
    }

    assert roots == {
        ProviderFamily.MICROSOFT: settings.data_dir / "m365",
        ProviderFamily.GOOGLE: settings.data_dir / "google",
        ProviderFamily.JIRA: settings.data_dir / "jira",
    }
    for family, root in roots.items():
        for other_family, other in roots.items():
            if family is not other_family:
                assert root != other
                assert not root.is_relative_to(other)


def test_google_profile_is_isolated_by_account(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    path = browser_profile_path(settings, ProviderFamily.GOOGLE, " User@Example.com ")

    assert path == settings.data_dir / "google" / _digest(_ACCOUNT)
    assert path != browser_profile_path(settings, ProviderFamily.GOOGLE, "other@example.com")


def test_jira_profile_is_isolated_by_site_and_account(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    path = browser_profile_path(settings, ProviderFamily.JIRA, _ACCOUNT, site=_SITE)

    assert path == settings.data_dir / "jira" / _digest(f"{_SITE}\n{_ACCOUNT}")
    assert path == browser_profile_path(
        settings, ProviderFamily.JIRA, "USER@example.com", site="https://ACME.atlassian.net/"
    )
    assert path != browser_profile_path(
        settings, ProviderFamily.JIRA, _ACCOUNT, site="https://other.atlassian.net"
    )
    assert path != browser_profile_path(
        settings, ProviderFamily.JIRA, "other@example.com", site=_SITE
    )


def test_jira_profile_requires_its_site(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="site"):
        browser_profile_path(_settings(tmp_path), ProviderFamily.JIRA, _ACCOUNT)


def test_a_site_applies_only_to_jira_profiles(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="site"):
        browser_profile_path(_settings(tmp_path), ProviderFamily.GOOGLE, _ACCOUNT, site=_SITE)


def test_a_family_without_a_browser_route_has_no_profile_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="glpi"):
        browser_profile_root(_settings(tmp_path), ProviderFamily.GLPI)


def test_google_and_jira_profile_roots_follow_their_explicit_settings(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        SOLERCIA_CONNECT_GOOGLE_PROFILE_DIR="~/google-profiles",
        SOLERCIA_CONNECT_JIRA_PROFILE_DIR=str(tmp_path / "jira-profiles"),
    )

    assert settings.google_profile_dir == Path("~/google-profiles").expanduser()
    assert settings.jira_profile_dir == tmp_path / "jira-profiles"
    assert browser_profile_root(settings, ProviderFamily.GOOGLE) == settings.google_profile_dir
    assert browser_profile_root(settings, ProviderFamily.JIRA) == tmp_path / "jira-profiles"


@pytest.mark.parametrize(
    ("overrides", "family"),
    [
        ({"SOLERCIA_CONNECT_GOOGLE_PROFILE_DIR": "{data}/m365/google"}, ProviderFamily.GOOGLE),
        ({"SOLERCIA_CONNECT_JIRA_PROFILE_DIR": "{data}/m365"}, ProviderFamily.JIRA),
        ({"SOLERCIA_CONNECT_GOOGLE_PROFILE_DIR": "{data}/jira"}, ProviderFamily.GOOGLE),
        ({"SOLERCIA_CONNECT_JIRA_PROFILE_DIR": "{data}/google/jira"}, ProviderFamily.JIRA),
        ({"SOLERCIA_CONNECT_GOOGLE_PROFILE_DIR": "{data}"}, ProviderFamily.GOOGLE),
    ],
)
def test_google_and_jira_roots_never_equal_or_nest_in_another_family_root(
    tmp_path: Path,
    overrides: dict[str, str],
    family: ProviderFamily,
) -> None:
    data = tmp_path / "data" / "solercia-connect"
    settings = _settings(
        tmp_path,
        **{name: value.format(data=data) for name, value in overrides.items()},
    )

    with pytest.raises(ConnectorFailure) as raised:
        browser_profile_root(settings, family)

    assert raised.value.reason is ReasonCode.CONFIGURATION_INVALID


def test_microsoft_root_is_unaffected_by_a_misplaced_google_root(tmp_path: Path) -> None:
    data = tmp_path / "data" / "solercia-connect"
    settings = _settings(tmp_path, SOLERCIA_CONNECT_GOOGLE_PROFILE_DIR=str(data / "m365" / "g"))

    assert microsoft_browser_profile_path(settings, _ACCOUNT) == data / "m365" / _digest(_ACCOUNT)


def test_family_profile_roots_get_the_same_symlink_audit(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "google-link"
    link.symlink_to(real, target_is_directory=True)
    settings = _settings(tmp_path, SOLERCIA_CONNECT_GOOGLE_PROFILE_DIR=str(link))
    root = browser_profile_root(settings, ProviderFamily.GOOGLE)

    with pytest.raises(ConnectorFailure) as raised:
        audit_browser_profile_path(
            browser_profile_path(settings, ProviderFamily.GOOGLE, _ACCOUNT),
            private_root=root,
        )

    assert raised.value.reason is ReasonCode.CONFIGURATION_INVALID


# --- Family of an official provider --------------------------------------------------------


def test_every_target_provider_has_one_declared_family() -> None:
    assert {
        spec.provider_id: official_provider_family(spec.provider_id) for spec in TARGET_SPECS
    } == {
        "microsoft.graph": ProviderFamily.MICROSOFT,
        "google.gmail": ProviderFamily.GOOGLE,
        "google.calendar": ProviderFamily.GOOGLE,
        "google.meet": ProviderFamily.GOOGLE,
        "jira.cloud": ProviderFamily.JIRA,
        "glpi.rest": ProviderFamily.GLPI,
    }
    assert official_provider_family("unknown.provider") is None


@pytest.mark.parametrize(
    "adapter",
    [
        MicrosoftGraphAdapter,
        GoogleGmailAdapter,
        GoogleCalendarAdapter,
        GoogleMeetAdapter,
        JiraAdapter,
        GlpiAdapter,
    ],
)
def test_declared_family_matches_the_official_adapter_descriptor(adapter: type) -> None:
    descriptor = adapter.descriptor

    assert official_provider_family(descriptor.provider_id) is descriptor.family


# --- Deterministic browser selection -------------------------------------------------------


def _browser(provider_id: str, family: ProviderFamily) -> FakeAdapter:
    return FakeAdapter(
        provider_id,
        family,
        RouteKind.BROWSER,
        frozenset({Capability.MAIL_SENT_LIST}),
        (mail_record(provider_id, family, RouteKind.BROWSER),),
    )


def _request(provider_id: str) -> ConnectorRequest:
    return ConnectorRequest.create(
        provider_id,
        Capability.MAIL_SENT_LIST,
        MailQuery(DateWindow(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))),
    )


@pytest.mark.parametrize("microsoft_first", [True, False])
@pytest.mark.parametrize(
    ("provider_id", "expected"),
    [("google.gmail", "google.browser"), ("microsoft.graph", "microsoft365.browser")],
)
def test_target_family_selects_the_browser_adapter_whatever_the_registration_order(
    microsoft_first: bool,
    provider_id: str,
    expected: str,
) -> None:
    microsoft = _browser("microsoft365.browser", ProviderFamily.MICROSOFT)
    google = _browser("google.browser", ProviderFamily.GOOGLE)
    registry = ProviderRegistry()
    for adapter in (microsoft, google) if microsoft_first else (google, microsoft):
        registry.register(adapter)
    policy = RoutePolicy(
        browser_fallback_families=frozenset({ProviderFamily.MICROSOFT, ProviderFamily.GOOGLE})
    )

    outcome = ConnectorRuntime(registry, policy, clock=lambda: NOW).execute(
        _request(provider_id), browser_fallback_enabled=True
    )

    assert outcome.ok is True
    assert outcome.receipt.steps[-1].provider_id == expected
    assert (microsoft.calls, google.calls) == (
        (1, 0) if expected == "microsoft365.browser" else (0, 1)
    )


@pytest.mark.parametrize("microsoft_first", [True, False])
def test_registry_picks_the_named_family_and_never_the_first_match(microsoft_first: bool) -> None:
    microsoft = _browser("microsoft365.browser", ProviderFamily.MICROSOFT)
    google = _browser("google.browser", ProviderFamily.GOOGLE)
    registry = ProviderRegistry()
    for adapter in (microsoft, google) if microsoft_first else (google, microsoft):
        registry.register(adapter)

    pick = registry.browser_for_capability
    assert pick(Capability.MAIL_SENT_LIST, family=ProviderFamily.GOOGLE) is google
    assert pick(Capability.MAIL_SENT_LIST, family=ProviderFamily.MICROSOFT) is microsoft
    assert pick(Capability.MAIL_SENT_LIST, family=ProviderFamily.JIRA) is None
    # Without a known family, two candidates are ambiguous: none is chosen.
    assert pick(Capability.MAIL_SENT_LIST) is None


def test_registry_keeps_the_single_family_capability_match() -> None:
    microsoft = _browser("microsoft365.browser", ProviderFamily.MICROSOFT)
    registry = ProviderRegistry()
    registry.register(microsoft)

    assert registry.browser_for_capability(Capability.MAIL_SENT_LIST) is microsoft
    assert registry.browser_for_capability(Capability.FILES_GET) is None
    assert (
        registry.browser_for_capability(Capability.MAIL_SENT_LIST, family=ProviderFamily.GOOGLE)
        is None
    )


def test_a_target_whose_family_has_no_browser_adapter_never_borrows_another_family() -> None:
    registry = ProviderRegistry()
    registry.register(_browser("microsoft365.browser", ProviderFamily.MICROSOFT))

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        _request("google.gmail"), browser_fallback_enabled=True
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED


# --- Route policy: Google and Jira families ------------------------------------------------


def _window() -> DateWindow:
    return DateWindow(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))


def _issue(provider_id: str, route: RouteKind) -> ExternalIssue:
    return ExternalIssue(
        "10001",
        "OPS-1",
        "Status",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        Provenance(provider_id, ProviderFamily.JIRA, route, "10001", NOW),
    )


def _family_case(family: ProviderFamily) -> tuple[str, str, ConnectorRequest, FakeAdapter]:
    """Official provider ID, browser provider ID, request, and a browser adapter for it."""
    if family is ProviderFamily.JIRA:
        browser = FakeAdapter(
            "jira.browser",
            family,
            RouteKind.BROWSER,
            frozenset({Capability.EXTERNAL_ISSUES_SEARCH}),
            (_issue("jira.browser", RouteKind.BROWSER),),
        )
        request = ConnectorRequest.create(
            "jira.cloud", Capability.EXTERNAL_ISSUES_SEARCH, IssueQuery(_window())
        )
        return "jira.cloud", "jira.browser", request, browser
    return (
        "google.gmail",
        "google.browser",
        _request("google.gmail"),
        _browser("google.browser", family),
    )


def _official_failing(provider_id: str, family: ProviderFamily, reason: ReasonCode) -> FakeAdapter:
    capability = (
        Capability.EXTERNAL_ISSUES_SEARCH
        if family is ProviderFamily.JIRA
        else Capability.MAIL_SENT_LIST
    )
    return FakeAdapter(
        provider_id,
        family,
        RouteKind.OFFICIAL,
        frozenset({capability}),
        ConnectorFailure(reason, "official route unavailable"),
    )


@pytest.mark.parametrize("family", [ProviderFamily.GOOGLE, ProviderFamily.JIRA])
@pytest.mark.parametrize(
    "reason",
    [ReasonCode.ADMIN_CONSENT_REQUIRED, ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE],
)
def test_google_and_jira_reach_their_browser_adapter_under_the_default_policy(
    family: ProviderFamily,
    reason: ReasonCode,
) -> None:
    official_id, browser_id, request, browser = _family_case(family)
    registry = ProviderRegistry()
    registry.register(_official_failing(official_id, family, reason))
    registry.register(browser)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        request, browser_fallback_enabled=True
    )

    assert outcome.ok is True
    assert outcome.receipt.selected_route is RouteKind.BROWSER
    assert outcome.receipt.steps[-1].provider_id == browser_id
    assert browser.calls == 1


@pytest.mark.parametrize("family", [ProviderFamily.GOOGLE, ProviderFamily.JIRA])
def test_an_unconfigured_google_or_jira_official_route_may_enter_its_browser_route(
    family: ProviderFamily,
) -> None:
    _, browser_id, request, browser = _family_case(family)
    registry = ProviderRegistry()
    registry.register(browser)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        request, browser_fallback_enabled=True
    )

    assert outcome.ok is True
    assert outcome.receipt.steps[0].reason is ReasonCode.CONFIGURATION_INVALID
    assert outcome.receipt.steps[-1].provider_id == browser_id


@pytest.mark.parametrize("family", [ProviderFamily.GOOGLE, ProviderFamily.JIRA])
def test_google_and_jira_browser_routes_still_need_the_per_invocation_flag(
    family: ProviderFamily,
) -> None:
    official_id, _, request, browser = _family_case(family)
    registry = ProviderRegistry()
    registry.register(_official_failing(official_id, family, ReasonCode.ADMIN_CONSENT_REQUIRED))
    registry.register(browser)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(request)

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.ADMIN_CONSENT_REQUIRED
    assert outcome.receipt.steps[-1].reason is ReasonCode.BROWSER_FALLBACK_DISABLED
    assert browser.calls == 0


@pytest.mark.parametrize("family", [ProviderFamily.GOOGLE, ProviderFamily.JIRA])
@pytest.mark.parametrize(
    "reason",
    [
        ReasonCode.INVALID_CREDENTIALS,
        ReasonCode.AUTHORIZATION_DENIED,
        ReasonCode.TENANT_POLICY_BLOCKED,
        ReasonCode.MALFORMED_PROVIDER_DATA,
        ReasonCode.UNKNOWN_ERROR,
    ],
)
def test_google_and_jira_never_fall_back_on_a_reason_outside_the_allowlist(
    family: ProviderFamily,
    reason: ReasonCode,
) -> None:
    official_id, _, request, browser = _family_case(family)
    registry = ProviderRegistry()
    registry.register(_official_failing(official_id, family, reason))
    registry.register(browser)

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        request, browser_fallback_enabled=True
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is reason
    assert outcome.receipt.steps[-1].reason is ReasonCode.BROWSER_FALLBACK_DENIED
    assert browser.calls == 0


@pytest.mark.parametrize("family", [ProviderFamily.GOOGLE, ProviderFamily.JIRA])
@pytest.mark.parametrize("registered_official", [True, False])
def test_without_a_google_or_jira_browser_adapter_the_outcome_is_not_implemented(
    family: ProviderFamily,
    registered_official: bool,
) -> None:
    official_id, _, request, _ = _family_case(family)
    registry = ProviderRegistry()
    if registered_official:
        registry.register(_official_failing(official_id, family, ReasonCode.ADMIN_CONSENT_REQUIRED))

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        request, browser_fallback_enabled=True
    )

    assert outcome.ok is False
    assert outcome.receipt.final_reason is ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED
    assert outcome.receipt.steps[-1].detail == (
        "No browser adapter is configured for the provider family"
    )


# --- Per-family capability allowlist (ADR 2026-10-25, D0) ----------------------------------

_MICROSOFT_BROWSER_CAPABILITIES = frozenset(
    {
        Capability.MAIL_SENT_LIST,
        Capability.MAIL_INBOX_LIST,
        Capability.MAIL_CATEGORIES_SET,
        Capability.CALENDAR_EVENTS_LIST,
        Capability.MEETING_TRANSCRIPTS_LIST,
        Capability.MEETING_TRANSCRIPT_GET,
        Capability.FILES_SEARCH,
        Capability.FILES_GET,
    }
)


def _allows(family: ProviderFamily, capability: Capability) -> bool:
    request = ConnectorRequest.create("any.provider", capability, MailQuery(_window()))
    failure = ConnectorFailure(ReasonCode.ADMIN_CONSENT_REQUIRED, "official route unavailable")
    return RoutePolicy().allows_browser(request, failure, family)


def test_each_family_lists_exactly_the_capabilities_its_decision_names() -> None:
    assert RoutePolicy().browser_fallback_capabilities_by_family == {
        ProviderFamily.MICROSOFT: _MICROSOFT_BROWSER_CAPABILITIES,
        ProviderFamily.GOOGLE: {
            Capability.MAIL_SENT_LIST,
            Capability.CALENDAR_EVENTS_LIST,
            Capability.MEETING_TRANSCRIPTS_LIST,
            Capability.MEETING_TRANSCRIPT_GET,
        },
        ProviderFamily.JIRA: {Capability.EXTERNAL_ISSUES_SEARCH},
    }


@pytest.mark.parametrize(
    ("family", "capability", "allowed"),
    [
        (ProviderFamily.JIRA, Capability.MAIL_CATEGORIES_SET, False),
        (ProviderFamily.JIRA, Capability.MAIL_SENT_LIST, False),
        (ProviderFamily.GOOGLE, Capability.MAIL_INBOX_LIST, False),
        (ProviderFamily.GOOGLE, Capability.MAIL_CATEGORIES_SET, False),
        (ProviderFamily.GOOGLE, Capability.FILES_GET, False),
        (ProviderFamily.GOOGLE, Capability.EXTERNAL_ISSUES_SEARCH, False),
        (ProviderFamily.GLPI, Capability.TICKETS_SEARCH, False),
        (ProviderFamily.GOOGLE, Capability.MAIL_SENT_LIST, True),
        (ProviderFamily.GOOGLE, Capability.CALENDAR_EVENTS_LIST, True),
        (ProviderFamily.GOOGLE, Capability.MEETING_TRANSCRIPTS_LIST, True),
        (ProviderFamily.GOOGLE, Capability.MEETING_TRANSCRIPT_GET, True),
        (ProviderFamily.JIRA, Capability.EXTERNAL_ISSUES_SEARCH, True),
    ],
)
def test_a_family_falls_back_only_for_a_capability_on_its_own_list(
    family: ProviderFamily,
    capability: Capability,
    allowed: bool,
) -> None:
    assert _allows(family, capability) is allowed


@pytest.mark.parametrize("capability", list(Capability))
def test_microsoft_browser_policy_is_exactly_what_it_was(capability: Capability) -> None:
    assert _allows(ProviderFamily.MICROSOFT, capability) is (
        capability in _MICROSOFT_BROWSER_CAPABILITIES
    )


def test_the_shared_reason_set_applies_to_every_family() -> None:
    policy = RoutePolicy()
    request = ConnectorRequest.create(
        "jira.cloud", Capability.EXTERNAL_ISSUES_SEARCH, IssueQuery(_window())
    )

    for reason in ReasonCode:
        failure = ConnectorFailure(reason, "official route unavailable")
        assert policy.allows_browser(request, failure, ProviderFamily.JIRA) is (
            reason in policy.browser_fallback_reasons
        )


def test_a_google_capability_outside_its_list_is_denied_even_with_a_google_adapter() -> None:
    browser = FakeAdapter(
        "google.browser",
        ProviderFamily.GOOGLE,
        RouteKind.BROWSER,
        frozenset({Capability.MAIL_INBOX_LIST}),
        (mail_record("google.browser", ProviderFamily.GOOGLE, RouteKind.BROWSER),),
    )
    registry = ProviderRegistry()
    registry.register(browser)
    inbox = ConnectorRequest.create(
        "google.gmail", Capability.MAIL_INBOX_LIST, MailQuery(_window())
    )

    outcome = ConnectorRuntime(registry, clock=lambda: NOW).execute(
        inbox, browser_fallback_enabled=True
    )

    assert outcome.receipt.steps[-1].reason is ReasonCode.BROWSER_FALLBACK_DENIED
    assert browser.calls == 0
