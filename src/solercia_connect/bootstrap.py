from __future__ import annotations

from collections.abc import Mapping

from solercia_connect.auth import (
    AuthorizationProvider,
    BasicAuthProvider,
    BearerTokenProvider,
    GlpiCredentials,
)
from solercia_connect.browser_profile import (
    microsoft_browser_profile_path,
    microsoft_browser_profile_root,
)
from solercia_connect.browser_routes import has_browser_route
from solercia_connect.config import (
    MICROSOFT_TARGETS,
    ConnectionType,
    ConnectSettings,
    JiraTokenKind,
    ProfileStore,
    effective_environment,
    load_settings,
    normalize_account,
)
from solercia_connect.connections import (
    atlassian_oauth_provider_from_settings,
    connect_command,
    oauth_provider_from_settings,
)
from solercia_connect.domain import Capability, ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import KeyringOAuthTokenStore, OAuthTokenStore
from solercia_connect.ports import HTTPTransport
from solercia_connect.providers.glpi import GlpiAdapter
from solercia_connect.providers.google import GoogleCalendarAdapter, GoogleGmailAdapter
from solercia_connect.providers.google_meet import GoogleMeetAdapter
from solercia_connect.providers.jira import JiraAdapter, jira_api_base_url
from solercia_connect.providers.m365_browser import (
    Microsoft365BrowserAdapter,
    PlaywrightBrowserDriver,
    browser_surface_for_capability,
)
from solercia_connect.providers.microsoft_graph import MicrosoftGraphAdapter
from solercia_connect.registry import ProviderRegistry
from solercia_connect.secret_store import KeyringSecretStore, SecretStore
from solercia_connect.secrets import SecretValue
from solercia_connect.transport import HttpxTransport


def registry_from_environment(
    *,
    connection_type: str | ConnectionType | None = None,
    account: str | None = None,
    capability: Capability | None = None,
    token_store: OAuthTokenStore | None = None,
    environ: Mapping[str, str] | None = None,
    include_browser: bool = True,
    settings_override: ConnectSettings | None = None,
    secret_store: SecretStore | None = None,
    transport: HTTPTransport | None = None,
) -> ProviderRegistry:
    values = effective_environment(environ=environ)
    settings = settings_override or load_settings(environ=values)
    registry = ProviderRegistry()
    transport = transport or HttpxTransport()
    parsed_type = ConnectionType.parse(connection_type) if connection_type is not None else None
    normalized_account = normalize_account(account) if account is not None else None
    profiles = ProfileStore.from_settings(settings)
    secure_store = token_store or KeyringOAuthTokenStore()

    microsoft_registered = False
    microsoft_token = _first_value(
        values,
        "SOLERCIA_CONNECT_MICROSOFT_ACCESS_TOKEN",
        "MS_GRAPH_ACCESS_TOKEN",
    )
    if microsoft_token:
        expected = normalized_account or _first_value(
            values,
            "SOLERCIA_CONNECT_MICROSOFT_EXPECTED_ACCOUNT",
            "MS_GRAPH_EXPECTED_ACCOUNT",
        )
        registry.register(
            MicrosoftGraphAdapter(
                transport,
                BearerTokenProvider(SecretValue(microsoft_token)),
                expected_account=expected,
            )
        )
        microsoft_registered = True
    elif (
        parsed_type in MICROSOFT_TARGETS
        and normalized_account is not None
        and (profile := profiles.get(parsed_type, normalized_account)) is not None
        and profile.route == "official"
    ):
        auth = oauth_provider_from_settings(
            settings,
            secure_store,
            parsed_type,
            normalized_account,
            transport=transport,
        )
        registry.register(
            MicrosoftGraphAdapter(
                transport,
                auth,
                expected_account=normalized_account,
            )
        )
        microsoft_registered = True

    google_token = _first_value(
        values,
        "SOLERCIA_CONNECT_GOOGLE_ACCESS_TOKEN",
        "GOOGLE_ACCESS_TOKEN",
    )
    if google_token:
        auth = BearerTokenProvider(SecretValue(google_token))
        expected = normalized_account or _first_value(
            values,
            "SOLERCIA_CONNECT_GOOGLE_EXPECTED_ACCOUNT",
            "GOOGLE_EXPECTED_ACCOUNT",
        )
        registry.register(GoogleGmailAdapter(transport, auth, expected_account=expected))
        registry.register(
            GoogleCalendarAdapter(
                transport,
                auth,
                expected_account=expected,
                health_calendar_id=_value(values, "GOOGLE_CALENDAR_ID") or "primary",
            )
        )
        registry.register(GoogleMeetAdapter(transport, auth, expected_account=expected))
    elif (
        parsed_type
        in {
            ConnectionType.GMAIL,
            ConnectionType.GOOGLE_CALENDAR,
            ConnectionType.GOOGLE_MEET,
        }
        and normalized_account is not None
        and (profile := profiles.get(parsed_type, normalized_account)) is not None
        and profile.route == "official"
    ):
        auth = oauth_provider_from_settings(
            settings,
            secure_store,
            parsed_type,
            normalized_account,
            transport=transport,
        )
        if parsed_type is ConnectionType.GMAIL:
            registry.register(
                GoogleGmailAdapter(transport, auth, expected_account=normalized_account)
            )
        elif parsed_type is ConnectionType.GOOGLE_CALENDAR:
            registry.register(
                GoogleCalendarAdapter(
                    transport,
                    auth,
                    expected_account=normalized_account,
                    health_calendar_id=_value(values, "GOOGLE_CALENDAR_ID") or "primary",
                )
            )
        else:
            registry.register(
                GoogleMeetAdapter(
                    transport,
                    auth,
                    expected_account=normalized_account,
                )
            )

    jira_url = _value(values, "JIRA_BASE_URL")
    jira_email = _value(values, "JIRA_EMAIL")
    jira_token = _value(values, "JIRA_API_TOKEN")
    if parsed_type is ConnectionType.JIRA and normalized_account is not None:
        registry.register(
            _jira_adapter_for_account(
                transport,
                profiles,
                secret_store or KeyringSecretStore(),
                normalized_account,
                settings=settings,
                token_store=secure_store,
            )
        )
    elif jira_url and jira_email and jira_token:
        registry.register(
            JiraAdapter(
                transport,
                BasicAuthProvider(jira_email, SecretValue(jira_token)),
                base_url=jira_url,
                expected_account_id=_value(values, "JIRA_EXPECTED_ACCOUNT_ID"),
            )
        )

    glpi_url = _value(values, "GLPI_BASE_URL")
    glpi_app_token = _value(values, "GLPI_APP_TOKEN")
    glpi_user_token = _value(values, "GLPI_USER_TOKEN")
    if glpi_url and glpi_app_token and glpi_user_token:
        registry.register(
            GlpiAdapter(
                transport,
                GlpiCredentials(SecretValue(glpi_app_token), SecretValue(glpi_user_token)),
                base_url=glpi_url,
            )
        )

    # Where a capability's records live is not always where the capability belongs. Transcripts
    # belong to Teams and are stored in the drive, so the session to open — and the profile that
    # proves it was opened — are the drive's, not the Teams client's.
    browser_type = (
        browser_surface_for_capability(capability)
        if capability is not None and (parsed_type is None or parsed_type in MICROSOFT_TARGETS)
        else None
    ) or parsed_type
    browser_profile = (
        profiles.get(browser_type, normalized_account)
        if browser_type is not None
        and has_browser_route(browser_type)
        and normalized_account is not None
        else None
    )
    if (
        include_browser
        and settings.browser_fallback_enabled
        and (
            (browser_profile is not None and browser_profile.route == "browser")
            or (
                microsoft_registered
                and settings.m365_profile_dir is not None
                and settings.m365_profile_dir.is_dir()
                and not settings.m365_profile_dir.is_symlink()
            )
        )
    ):
        registry.register(
            browser_adapter_from_environment(
                account=normalized_account,
                connection_type=browser_type,
                environ=values,
            )
        )
    return registry


def _jira_adapter_for_account(
    transport: HTTPTransport,
    profiles: ProfileStore,
    secrets: SecretStore,
    account: str,
    *,
    settings: ConnectSettings,
    token_store: OAuthTokenStore,
) -> JiraAdapter:
    """Build Jira from this account's own connection; never borrow the JIRA_* variables.

    An API-token profile authenticates with Basic email:token; an OAuth profile with the bearer
    grant of the Atlassian provider. Both call the base bound to the stored site and cloud ID.
    """

    profile = profiles.get(ConnectionType.JIRA, account)
    if (
        profile is None
        or profile.route != "official"
        or profile.site is None
        or profile.provider_account_id is None
    ):
        command = connect_command(
            ConnectionType.JIRA,
            account,
            site=profile.site if profile else None,
            cloud_id=profile.cloud_id if profile else None,
            token_kind=profile.token_kind if profile else None,
        )
        raise ValueError(f"No official Jira connection exists for this account; run {command}")
    key = f"{ConnectionType.JIRA.value}:{account}"
    auth: AuthorizationProvider
    if profile.token_kind is JiraTokenKind.OAUTH:
        auth = atlassian_oauth_provider_from_settings(
            settings, token_store, account, transport=transport
        )
        if token_store.load(key) is None:
            raise ConnectorFailure(
                ReasonCode.INVALID_CREDENTIALS,
                "The Atlassian authorization for this account is not stored; reconnect it",
            )
    else:
        token = secrets.load(key)
        if token is None:
            raise ConnectorFailure(
                ReasonCode.INVALID_CREDENTIALS,
                "The Jira API token for this account is not stored; reconnect it in the CLI",
            )
        auth = BasicAuthProvider(account, token)
    return JiraAdapter(
        transport,
        auth,
        base_url=jira_api_base_url(profile.site, profile.cloud_id),
        expected_account_id=profile.provider_account_id,
        expected_account=account,
    )


def browser_adapter_from_environment(
    *,
    account: str | None = None,
    connection_type: str | ConnectionType | None = None,
    environ: Mapping[str, str] | None = None,
) -> Microsoft365BrowserAdapter:
    parsed_type = (
        ConnectionType.TEAMS if connection_type is None else ConnectionType.parse(connection_type)
    )
    if parsed_type not in MICROSOFT_TARGETS:
        raise ValueError("Microsoft browser fallback supports only Outlook, Teams and OneDrive")
    values = effective_environment(environ=environ)
    settings = load_settings(environ=values)
    if not settings.browser_fallback_enabled:
        raise ConnectorFailure(
            ReasonCode.CONFIGURATION_INVALID,
            "Microsoft browser fallback is not globally available",
        )
    expected = account or _first_value(
        values,
        "SOLERCIA_CONNECT_MICROSOFT_EXPECTED_ACCOUNT",
        "MS_GRAPH_EXPECTED_ACCOUNT",
    )
    profile_root = microsoft_browser_profile_root(settings)
    if expected:
        normalized = normalize_account(expected)
        profile_path = microsoft_browser_profile_path(settings, normalized)
        expected = normalized
    else:
        profile_path = profile_root / "legacy"
    return Microsoft365BrowserAdapter(
        PlaywrightBrowserDriver(),
        connection_type=parsed_type,
        profile_dir=profile_path,
        expected_account=expected,
    )


def browser_fallback_enabled_from_environment(
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    return load_settings(environ=effective_environment(environ=environ)).browser_fallback_enabled


def _value(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name, "").strip()
    return value or None


def _first_value(values: Mapping[str, str], *names: str) -> str | None:
    return next((value for name in names if (value := _value(values, name))), None)
