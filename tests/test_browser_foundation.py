from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from solercia_connect.config import ConnectionType
from solercia_connect.domain import Capability, ConnectorRequest, DateWindow, MailQuery, ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.providers.m365_browser import (
    Microsoft365BrowserAdapter,
    browser_extraction_report,
    browser_surface_for_capability,
)
from solercia_connect.providers.m365_browser.driver import (
    BrowserSnapshot,
    PlaywrightBrowserDriver,
    validate_dedicated_profile,
)
from solercia_connect.providers.m365_browser.selectors import ENDPOINTS, SELECTOR_VERSION, SELECTORS
from solercia_connect.providers.m365_browser.states import BrowserState, BrowserStateMachine
from tests.fakes import FakeBrowserDriver, authenticated_snapshot, mail_record


class _FakeControl:
    def __init__(self, label: str | None, *, present: bool = True) -> None:
        self._label = label
        self._present = present

    def count(self) -> int:
        return int(self._present)

    def is_visible(self) -> bool:
        return self._present

    def get_attribute(self, name: str) -> str | None:
        return self._label if name == "aria-label" else None

    def inner_text(self) -> str:
        return self._label or ""


class _FakeLocator:
    def __init__(self, labels: tuple[str | None, ...]) -> None:
        self._controls = tuple(_FakeControl(label) for label in labels)

    @property
    def first(self) -> _FakeControl:
        return self._controls[0] if self._controls else _FakeControl(None, present=False)

    def count(self) -> int:
        return len(self._controls)

    def nth(self, index: int) -> _FakeControl:
        return self._controls[index]


class _FakeSnapshotPage:
    url = "https://teams.microsoft.com/v2/"

    def __init__(self, account_labels: tuple[str, ...]) -> None:
        self._controls: dict[str, tuple[str | None, ...]] = {
            SELECTORS.identity_controls[0]: account_labels,
            SELECTORS.authenticated_shells[0]: (None,),
        }

    def title(self) -> str:
        return "Microsoft Teams"

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self._controls.get(selector, ()))


class _RichControl:
    def __init__(self, attributes: dict[str, str], text: str = "") -> None:
        self.attributes = attributes
        self.text = text
        self.clicked = False

    def count(self) -> int:
        return 1

    def is_visible(self) -> bool:
        return True

    def get_attribute(self, name: str) -> str | None:
        return self.attributes.get(name)

    def inner_text(self) -> str:
        return self.text

    def click(self, *, timeout: int) -> None:
        assert timeout > 0
        self.clicked = True


class _RichLocator:
    def __init__(self, controls: tuple[_RichControl, ...]) -> None:
        self.controls = controls

    @property
    def first(self) -> _RichControl | _FakeControl:
        return self.controls[0] if self.controls else _FakeControl(None, present=False)

    def count(self) -> int:
        return len(self.controls)

    def nth(self, index: int) -> _RichControl:
        return self.controls[index]


class _RichPage:
    url = ENDPOINTS.teams_home

    def __init__(self, controls: dict[str, tuple[_RichControl, ...]]) -> None:
        self.controls = controls

    def title(self) -> str:
        return "Teams"

    def locator(self, selector: str) -> _RichLocator:
        return _RichLocator(self.controls.get(selector, ()))


class _MaterializingProfileControl(_RichControl):
    def __init__(self, page: _DelayedFlyoutPage) -> None:
        super().__init__({"aria-label": "Account manager"})
        self._page = page

    def click(self, *, timeout: int) -> None:
        super().click(timeout=timeout)
        self._page.reveal_clicks += 1


class _DelayedFlyoutPage:
    url = ENDPOINTS.teams_home

    def __init__(self, *, materialize_after_clicks: int) -> None:
        self.materialize_after_clicks = materialize_after_clicks
        self.reveal_clicks = 0
        self.wait_calls: list[int] = []
        self.profile_control = _MaterializingProfileControl(self)
        self.identity_control = _RichControl({"data-upn": "user@example.com"})
        self.shell_control = _RichControl({})

    def title(self) -> str:
        return "Teams"

    def locator(self, selector: str) -> _RichLocator:
        if selector == SELECTORS.profile_controls[0]:
            return _RichLocator((self.profile_control,))
        if (
            selector == SELECTORS.identity_controls[0]
            and self.reveal_clicks >= self.materialize_after_clicks
        ):
            return _RichLocator((self.identity_control,))
        if selector == SELECTORS.authenticated_shells[0]:
            return _RichLocator((self.shell_control,))
        return _RichLocator(())

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.wait_calls.append(milliseconds)


class _InjectedPlaywrightDriver(PlaywrightBrowserDriver):
    def __init__(self, page: _DelayedFlyoutPage) -> None:
        super().__init__()
        self._page = page
        self.open_calls: list[tuple[Path, bool]] = []
        self.navigate_calls: list[tuple[str, int]] = []
        self.close_calls = 0

    def open_persistent(
        self,
        profile_dir: Path,
        *,
        headless: bool,
        connection_type: ConnectionType | None = None,
    ) -> None:
        del connection_type
        self.open_calls.append((profile_dir, headless))

    def navigate(self, url: str, *, timeout_ms: int) -> None:
        self.navigate_calls.append((url, timeout_ms))

    def close(self) -> None:
        self.close_calls += 1


def _real_driver_snapshot(account_labels: tuple[str, ...]) -> BrowserSnapshot:
    driver = PlaywrightBrowserDriver()
    driver._page = _FakeSnapshotPage(account_labels)
    return driver.snapshot(SELECTORS, connection_type=ConnectionType.TEAMS)


def login_snapshot() -> BrowserSnapshot:
    return BrowserSnapshot(
        "https://login.microsoftonline.com/",
        "Sign in",
        (),
        True,
        False,
        False,
    )


def mfa_snapshot() -> BrowserSnapshot:
    return BrowserSnapshot(
        "https://login.microsoftonline.com/common/SAS/BeginAuth",
        "Approve sign in",
        (),
        False,
        True,
        False,
    )


def authenticated_shell_without_identity(*, url: str = ENDPOINTS.teams_home) -> BrowserSnapshot:
    return BrowserSnapshot(
        url,
        "Microsoft 365",
        (),
        False,
        False,
        True,
    )


def test_browser_extraction_report_declares_inbox_for_outlook() -> None:
    report = browser_extraction_report(ConnectionType.OUTLOOK)

    assert report["extraction_status"] == "available"
    assert report["capabilities"] == [
        "mail.sent.list",
        "mail.inbox.list",
        "calendar.events.list",
    ]
    assert browser_surface_for_capability(Capability.MAIL_INBOX_LIST) is ConnectionType.OUTLOOK


def test_outlook_navigates_outlook_endpoint_not_teams(tmp_path: Path) -> None:
    driver = FakeBrowserDriver(
        [authenticated_shell_without_identity(url="https://outlook.cloud.microsoft/mail/")],
        outlook_identity_outcomes=["user@example.com"],
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
    )

    adapter.health()

    assert driver.navigate_calls == [(ENDPOINTS.outlook_sent, 30_000)]
    assert all(url != ENDPOINTS.teams_home for url, _ in driver.navigate_calls)


def test_teams_still_navigates_teams_endpoint(tmp_path: Path) -> None:
    driver = FakeBrowserDriver([authenticated_snapshot()])
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
    )

    adapter.health()

    assert driver.navigate_calls == [(ENDPOINTS.teams_home, 30_000)]


def test_authenticated_shell_with_zero_initial_hints_does_not_fail_immediately(
    tmp_path: Path,
) -> None:
    driver = FakeBrowserDriver(
        [authenticated_shell_without_identity(), authenticated_snapshot("USER@Example.com")]
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
        identity_attestation_cycles=3,
        state_poll_ms=25,
    )

    report = adapter.health()

    assert report.account_id == "user@example.com"
    assert driver.reveal_calls == [25]
    assert driver.wait_calls == [25]


def test_teams_profile_flyout_revelation_produces_one_canonical_identity(
    tmp_path: Path,
) -> None:
    driver = FakeBrowserDriver(
        [
            authenticated_shell_without_identity(),
            BrowserSnapshot(
                ENDPOINTS.teams_home,
                "Teams",
                ("Signed in as USER@Example.COM",),
                False,
                False,
                True,
            ),
        ]
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
        identity_attestation_cycles=2,
        state_poll_ms=10,
    )

    report = adapter.health()

    assert report.account_id == "user@example.com"
    assert driver.reveal_calls == [10]


def test_zero_identity_after_bounded_attestation_is_selector_mismatch(tmp_path: Path) -> None:
    driver = FakeBrowserDriver([authenticated_shell_without_identity()])
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
        identity_attestation_cycles=3,
        state_poll_ms=7,
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.health()

    assert captured.value.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH
    assert "selectors=" in captured.value.safe_message
    assert "signals=0" in captured.value.safe_message
    assert driver.reveal_calls == [7]
    assert driver.wait_calls == [7, 7, 7]


def test_identity_attestation_consumes_identity_after_five_full_wait_reobserve_cycles(
    tmp_path: Path,
) -> None:
    no_identity = authenticated_shell_without_identity(url=ENDPOINTS.teams_home)
    driver = FakeBrowserDriver(
        [no_identity, no_identity, no_identity, no_identity, no_identity, authenticated_snapshot()]
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
        identity_attestation_cycles=5,
        state_poll_ms=11,
    )

    report = adapter.health()

    assert report.account_id == "user@example.com"
    assert driver.wait_calls == [11] * 5


def test_identity_attestation_retries_reveal_until_flyout_materializes(
    tmp_path: Path,
) -> None:
    page = _DelayedFlyoutPage(materialize_after_clicks=2)
    driver = _InjectedPlaywrightDriver(page)
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
        identity_attestation_cycles=3,
        state_poll_ms=13,
    )

    report = adapter.health()

    assert report.account_id == "user@example.com"
    assert page.reveal_clicks == 2
    assert page.wait_calls == [13, 13]


def test_driver_does_not_toggle_open_identity_flyout() -> None:
    identity_open_profile = _RichControl({"aria-label": "Account manager"})
    identity_control = _RichControl({"data-upn": "user@example.com"})
    driver = PlaywrightBrowserDriver()
    driver._page = _RichPage(
        {
            SELECTORS.profile_controls[0]: (identity_open_profile,),
            SELECTORS.identity_controls[0]: (identity_control,),
        }
    )

    assert driver.reveal_identity_menu(SELECTORS, timeout_ms=250) is True
    assert identity_open_profile.clicked is False

    expanded_profile = _RichControl({"aria-label": "Account manager", "aria-expanded": "true"})
    driver._page = _RichPage({SELECTORS.profile_controls[0]: (expanded_profile,)})

    assert driver.reveal_identity_menu(SELECTORS, timeout_ms=250) is True
    assert expanded_profile.clicked is False


def test_real_driver_ignores_global_identity_attributes_outside_authoritative_flyout() -> None:
    driver = PlaywrightBrowserDriver()
    driver._page = _RichPage(
        {
            "[data-email]": (_RichControl({"data-email": "message@example.com"}),),
            "[data-upn]": (_RichControl({"data-upn": "recipient@example.com"}),),
            "[data-user-principal-name]": (
                _RichControl({"data-user-principal-name": "sender@example.com"}),
            ),
            "[data-identity]": (_RichControl({"data-identity": "alternate@example.com"}),),
        }
    )

    snapshot = driver.snapshot(SELECTORS, connection_type=ConnectionType.TEAMS)

    assert snapshot.account_hints == ()


def test_real_driver_keeps_one_authoritative_identity_despite_unrelated_page_email(
    tmp_path: Path,
) -> None:
    real_driver = PlaywrightBrowserDriver()
    real_driver._page = _RichPage(
        {
            SELECTORS.identity_controls[0]: (_RichControl({"data-upn": "user@example.com"}),),
            "[data-email]": (_RichControl({"data-email": "unrelated-message@example.com"}),),
        }
    )
    snapshot = real_driver.snapshot(SELECTORS, connection_type=ConnectionType.TEAMS)
    driver = FakeBrowserDriver([snapshot])
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
    )

    report = adapter.health()

    assert report.account_id == "user@example.com"
    assert snapshot.account_hints == ("user@example.com",)


def test_multiple_authoritative_current_identities_fail_closed(tmp_path: Path) -> None:
    real_driver = PlaywrightBrowserDriver()
    real_driver._page = _RichPage(
        {
            "#mectrl_currentAccount_primary": (_RichControl({"data-upn": "first@example.com"}),),
            "#mectrl_currentAccount_secondary": (_RichControl({"data-upn": "second@example.com"}),),
            "[data-email]": (_RichControl({"data-email": "message@example.com"}),),
        }
    )
    snapshot = real_driver.snapshot(SELECTORS, connection_type=ConnectionType.TEAMS)
    driver = FakeBrowserDriver([snapshot])
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account=None,
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.health()

    assert captured.value.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH
    assert "signals=2" in captured.value.safe_message
    assert "first@example.com" not in captured.value.safe_message
    assert "second@example.com" not in captured.value.safe_message
    assert "message@example.com" not in captured.value.safe_message


def test_identity_attestation_timeout_uses_exact_default_wait_budget_and_safe_diagnostics(
    tmp_path: Path,
) -> None:
    expected_account = "private.user@example.com"
    driver = FakeBrowserDriver(
        [authenticated_shell_without_identity(url=ENDPOINTS.teams_home)],
        reveal_result=False,
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account=expected_account,
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.health()

    assert captured.value.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH
    assert driver.wait_calls == [1_000] * 30
    assert driver.reveal_calls == [1_000] * 30
    assert f"selectors={SELECTORS.version}" in captured.value.safe_message
    assert "signals=0" in captured.value.safe_message
    assert "host=teams.microsoft.com" in captured.value.safe_message
    assert expected_account not in captured.value.safe_message


def test_multiple_observed_identities_fail_closed_without_raw_hint_leakage(
    tmp_path: Path,
) -> None:
    raw_hint = "Accounts: first@example.com and second@example.com"
    driver = FakeBrowserDriver(
        [
            BrowserSnapshot(
                ENDPOINTS.teams_home,
                "Teams",
                (raw_hint,),
                False,
                False,
                True,
            )
        ]
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="first@example.com",
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.health()

    assert captured.value.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH
    assert "first@example.com" not in captured.value.safe_message
    assert "second@example.com" not in captured.value.safe_message


def test_single_different_observed_identity_is_browser_account_mismatch(tmp_path: Path) -> None:
    driver = FakeBrowserDriver([authenticated_snapshot("other@example.com")])
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.health()

    assert captured.value.reason is ReasonCode.BROWSER_ACCOUNT_MISMATCH


def test_headed_login_closes_then_reopens_same_profile_headless_and_verifies_identity(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    driver = FakeBrowserDriver(
        [
            authenticated_shell_without_identity(
                url="https://outlook.cloud.microsoft/mail/sentitems/"
            ),
            authenticated_shell_without_identity(
                url="https://outlook.cloud.microsoft/mail/sentitems/"
            ),
        ],
        outlook_identity_outcomes=["USER@Example.com", "user@example.com"],
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.OUTLOOK,
        profile_dir=profile,
        expected_account="user@example.com",
    )

    report = adapter.interactive_login()

    assert report.account_id == "user@example.com"
    assert driver.open_calls == [(profile, False), (profile, True)]
    assert driver.navigate_calls == [
        (ENDPOINTS.outlook_sent, 30_000),
        (ENDPOINTS.outlook_sent, 30_000),
    ]
    assert driver.close_calls == 2
    assert driver.reveal_calls == []


def test_headless_persistence_check_login_failure_is_typed_and_closes_both_phases(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    driver = FakeBrowserDriver([authenticated_snapshot(), login_snapshot()])
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=profile,
        expected_account="user@example.com",
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.interactive_login()

    assert captured.value.reason is ReasonCode.BROWSER_LOGIN_REQUIRED
    assert driver.open_calls == [(profile, False), (profile, True)]
    assert driver.close_calls == 2


def test_driver_reveals_profile_flyout_through_centralized_control_selector() -> None:
    page = _DelayedFlyoutPage(materialize_after_clicks=1)
    driver = PlaywrightBrowserDriver()
    driver._page = page

    revealed = driver.reveal_identity_menu(SELECTORS, timeout_ms=250)

    assert revealed is True
    assert page.profile_control.clicked is True


def test_driver_collects_centralized_identity_selectors_and_safe_attributes() -> None:
    attribute_values = {
        attribute: f"{index}@example.com"
        for index, attribute in enumerate(SELECTORS.identity_attributes, start=1)
    }
    control = _RichControl(attribute_values, text="text@example.com")
    driver = PlaywrightBrowserDriver()
    driver._page = _RichPage({SELECTORS.identity_controls[0]: (control,)})

    snapshot = driver.snapshot(SELECTORS, connection_type=ConnectionType.TEAMS)

    assert set(snapshot.account_hints) == {*attribute_values.values(), "text@example.com"}
    assert snapshot.authenticated_shell_visible is True


def test_interactive_login_handles_login_then_mfa_with_fixed_polling(tmp_path: Path) -> None:
    driver = FakeBrowserDriver(
        [login_snapshot(), mfa_snapshot(), authenticated_snapshot("user@example.com")]
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
        state_poll_ms=25,
        max_poll_cycles=5,
    )

    report = adapter.interactive_login()

    assert report.account_id == "user@example.com"
    assert driver.open_calls == [
        (tmp_path / "profile", False),
        (tmp_path / "profile", True),
    ]
    assert driver.wait_calls == [25, 25]
    assert driver.close_calls == 2


def test_noninteractive_status_stops_when_login_is_required(tmp_path: Path) -> None:
    driver = FakeBrowserDriver([login_snapshot()])
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.health()

    assert captured.value.reason is ReasonCode.BROWSER_LOGIN_REQUIRED
    assert driver.close_calls == 1


def test_account_mismatch_fails_closed(tmp_path: Path) -> None:
    driver = FakeBrowserDriver([authenticated_snapshot("other@example.com")])
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.interactive_login()

    assert captured.value.reason is ReasonCode.BROWSER_ACCOUNT_MISMATCH


def test_account_binding_rejects_substring_identity_attack(tmp_path: Path) -> None:
    driver = FakeBrowserDriver(
        [authenticated_snapshot("attacker-user@example.com.evil")],
        (mail_record(),),
    )
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(_mail_request())

    assert captured.value.reason is ReasonCode.BROWSER_ACCOUNT_MISMATCH
    assert driver.read_calls == 0


def test_account_binding_is_canonical_and_case_insensitive(tmp_path: Path) -> None:
    driver = FakeBrowserDriver([authenticated_snapshot("USER@Example.COM")], (mail_record(),))
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="User@Example.com",
    )

    records = adapter.execute(_mail_request())

    assert records[0].provenance.account_id == "user@example.com"
    assert driver.read_calls == 1


def test_browser_read_without_expected_account_binds_observed_identity(tmp_path: Path) -> None:
    driver = FakeBrowserDriver([authenticated_snapshot("USER@Example.COM")], (mail_record(),))
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account=None,
    )

    records = adapter.execute(_mail_request())

    assert records[0].provenance.account_id == "user@example.com"
    assert driver.read_calls == 1


def test_real_driver_rejects_conflicting_visible_account_controls_before_read(
    tmp_path: Path,
) -> None:
    snapshot = _real_driver_snapshot(
        ("Profile for user@example.com", "Profile for other@example.com")
    )
    driver = FakeBrowserDriver([snapshot], (mail_record(),))
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account=None,
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(_mail_request())

    assert captured.value.reason is ReasonCode.BROWSER_SELECTOR_MISMATCH
    assert driver.read_calls == 0


def test_real_driver_deduplicates_case_variant_account_controls(tmp_path: Path) -> None:
    snapshot = _real_driver_snapshot(
        ("Profile for user@example.com", "Signed in as USER@Example.COM")
    )
    driver = FakeBrowserDriver([snapshot], (mail_record(),))
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account=None,
    )

    records = adapter.execute(_mail_request())

    assert records[0].provenance.account_id == "user@example.com"
    assert driver.read_calls == 1


def test_malformed_requested_browser_identity_fails_without_open_or_cleanup_masking(
    tmp_path: Path,
) -> None:
    driver = FakeBrowserDriver([authenticated_snapshot()], (mail_record(),))
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account=None,
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(_mail_request("not-an-account"))

    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert driver.open_calls == []
    assert driver.read_calls == 0
    assert driver.close_calls == 0


def test_browser_rejects_ambiguous_expected_account_configuration(tmp_path: Path) -> None:
    driver = FakeBrowserDriver([authenticated_snapshot()])

    with pytest.raises(ValueError, match="one unambiguous email identity"):
        Microsoft365BrowserAdapter(
            driver,
            connection_type=ConnectionType.TEAMS,
            profile_dir=tmp_path / "profile",
            expected_account="Profile for user@example.com",
        )


@pytest.mark.parametrize(
    ("account_hints", "reason"),
    [
        ((), ReasonCode.BROWSER_SELECTOR_MISMATCH),
        (
            ("Profile for user@example.com and other@example.com",),
            ReasonCode.BROWSER_SELECTOR_MISMATCH,
        ),
        (("Profile for other@example.com",), ReasonCode.BROWSER_ACCOUNT_MISMATCH),
    ],
    ids=["missing-after-bound", "ambiguous", "mismatched"],
)
def test_browser_read_requires_one_observed_account(
    account_hints: tuple[str, ...],
    reason: ReasonCode,
    tmp_path: Path,
) -> None:
    snapshot = BrowserSnapshot(
        "https://teams.microsoft.com/v2/",
        "Microsoft Teams",
        account_hints,
        False,
        False,
        True,
    )
    driver = FakeBrowserDriver([snapshot], (mail_record(),))
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account=None,
    )

    with pytest.raises(ConnectorFailure) as captured:
        adapter.execute(_mail_request())

    assert captured.value.reason is reason
    assert driver.read_calls == 0


def test_real_driver_sent_mail_extraction_requires_an_open_session() -> None:
    """Sent mail is implemented, so it fails on the missing session, not on the capability."""
    driver = PlaywrightBrowserDriver()
    window = DateWindow(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
    )

    with pytest.raises(ConnectorFailure) as captured:
        driver.read_records(
            Capability.MAIL_SENT_LIST,
            MailQuery(window),
            SELECTORS,
            timeout_ms=1_000,
        )

    assert captured.value.reason is not ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED
    assert captured.value.reason is ReasonCode.CONFIGURATION_INVALID


@pytest.mark.parametrize(
    "capability",
    [
        Capability.CALENDAR_EVENTS_LIST,
        Capability.MEETING_TRANSCRIPTS_LIST,
        Capability.MEETING_TRANSCRIPT_GET,
    ],
)
def test_real_driver_reports_unimplemented_browser_capabilities_explicitly(
    capability: Capability,
) -> None:
    driver = PlaywrightBrowserDriver()
    window = DateWindow(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
    )

    with pytest.raises(ConnectorFailure) as captured:
        driver.read_records(capability, MailQuery(window), SELECTORS, timeout_ms=1_000)

    assert captured.value.reason is ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED
    assert capability.value in str(captured.value)


def test_personal_chrome_profile_is_rejected() -> None:
    personal = Path.home() / "Library/Application Support/Google/Chrome/Default"

    with pytest.raises(ConnectorFailure) as captured:
        validate_dedicated_profile(personal)

    assert captured.value.reason is ReasonCode.CONFIGURATION_INVALID


def test_selector_bundle_is_versioned() -> None:
    assert SELECTORS.version == SELECTOR_VERSION
    assert SELECTOR_VERSION == "m365-web-2026.08-v3"


def test_state_machine_rejects_nondeterministic_jump() -> None:
    machine = BrowserStateMachine()

    with pytest.raises(RuntimeError):
        machine.advance(BrowserState.READY)


def test_fake_browser_driver_returns_normalized_record(tmp_path: Path) -> None:
    driver = FakeBrowserDriver([authenticated_snapshot()], (mail_record(),))
    adapter = Microsoft365BrowserAdapter(
        driver,
        connection_type=ConnectionType.TEAMS,
        profile_dir=tmp_path / "profile",
        expected_account="user@example.com",
    )
    window = DateWindow(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    request = ConnectorRequest.create(
        "microsoft.graph", Capability.MAIL_SENT_LIST, MailQuery(window)
    )

    records = adapter.execute(request)

    assert len(records) == 1
    assert records[0].provenance.provider_id == "microsoft365.browser"
    assert records[0].provenance.account_id == "user@example.com"


def _mail_request(account_id: str = "user@example.com") -> ConnectorRequest:
    window = DateWindow(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    return ConnectorRequest.create(
        "microsoft.graph",
        Capability.MAIL_SENT_LIST,
        MailQuery(window, account_id=account_id),
    )
