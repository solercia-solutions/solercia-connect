from __future__ import annotations

from dataclasses import dataclass

from solercia_connect.config import ConnectionType

SELECTOR_VERSION = "m365-web-2026.08-v3"


@dataclass(frozen=True, slots=True)
class Microsoft365Endpoints:
    teams_home: str = "https://teams.microsoft.com/v2/"
    outlook_sent: str = "https://outlook.office.com/mail/sentitems/"
    outlook_identity: str = "https://outlook.office.com/api/v2.0/me"
    outlook_messages: str = "https://outlook.office.com/api/v2.0/me/messages"
    outlook_calendar_view: str = "https://outlook.office.com/api/v2.0/me/calendarview"
    outlook_sent_messages: str = (
        "https://outlook.office.com/api/v2.0/me/MailFolders/SentItems/messages"
    )
    outlook_inbox_messages: str = (
        "https://outlook.office.com/api/v2.0/me/MailFolders/Inbox/messages"
    )
    onedrive_launch: str = "https://www.microsoft365.com/launch/onedrive?auth=2"

    def home_for(self, connection_type: ConnectionType) -> str:
        if connection_type is ConnectionType.OUTLOOK:
            return self.outlook_sent
        if connection_type is ConnectionType.TEAMS:
            return self.teams_home
        if connection_type is ConnectionType.ONEDRIVE:
            return self.onedrive_launch
        raise ValueError("Microsoft browser fallback supports only Outlook, Teams and OneDrive")


@dataclass(frozen=True, slots=True)
class Microsoft365Selectors:
    version: str = SELECTOR_VERSION
    profile_controls: tuple[str, ...] = (
        "#mectrl_headerPicture",
        '[data-tid="me-control"]',
        '[data-testid="me-control"]',
        'button[aria-controls^="mectrl_"]',
    )
    identity_controls: tuple[str, ...] = (
        "#mectrl_currentAccount",
        "#mectrl_currentAccount_primary",
        "#mectrl_currentAccount_secondary",
        "#mectrl_signedInAs",
        '[data-tid="me-control"] [data-tid="current-account"]',
        '[data-tid="me-control"] [data-current-account="true"]',
        '[data-tid="account-manager"] [data-tid="current-account"]',
        '[data-tid="account-manager"] [data-current-account="true"]',
        '[data-testid="account-manager"] [data-testid="current-account"]',
        '[data-testid="account-manager"] [data-current-account="true"]',
    )
    identity_attributes: tuple[str, ...] = (
        "aria-label",
        "title",
        "data-email",
        "data-upn",
        "data-user-principal-name",
        "data-account",
        "data-identity",
        "data-logonname",
        "data-logon-name",
    )
    authenticated_shells: tuple[str, ...] = (
        '[data-tid="app-layout"]',
        '[data-app-section="NavigationPane"]',
        'main[role="main"]',
    )
    login_indicators: tuple[str, ...] = (
        'input[type="email"]',
        'input[name="loginfmt"]',
        'input[name="passwd"]',
    )
    mfa_indicators: tuple[str, ...] = (
        'input[name="otc"]',
        'input[name="otcPin"]',
        '[data-testid*="mfa" i]',
        "text=/approve sign in request/i",
    )
    # Read selectors are centralized now, but the built-in v0.1 driver intentionally does not
    # claim extraction support until they are validated live and contract fixtures are captured.
    outlook_message_rows: tuple[str, ...] = (
        '[role="option"][data-convid]',
        '[data-testid="message-list-item"]',
    )
    teams_transcript_tabs: tuple[str, ...] = (
        '[role="tab"]:has-text("Transcript")',
        '[role="tab"]:has-text("Transcripción")',
    )


ENDPOINTS = Microsoft365Endpoints()
SELECTORS = Microsoft365Selectors()
