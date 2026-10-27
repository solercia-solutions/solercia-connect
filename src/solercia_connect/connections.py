from __future__ import annotations

import time
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from solercia_connect.auth import BasicAuthProvider
from solercia_connect.browser_profile import browser_profile_path, microsoft_browser_profile_path
from solercia_connect.browser_routes import has_browser_route, target_family
from solercia_connect.config import (
    BITBUCKET_TARGETS,
    GOOGLE_TARGETS,
    MICROSOFT_TARGETS,
    ConnectionProfile,
    ConnectionType,
    ConnectSettings,
    JiraTokenKind,
    ProfileStore,
    ProjectHomeError,
    missing_atlassian_configuration,
    normalize_account,
    normalize_bitbucket_workspace,
    normalize_jira_site,
    normalize_jira_token_binding,
    target_spec,
)
from solercia_connect.domain import ProviderFamily, ReasonCode, utc_now
from solercia_connect.envelope import ErrorDetail, ResultEnvelope
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import (
    AtlassianOAuthProvider,
    DeviceAuthorization,
    GoogleOAuthProvider,
    MicrosoftOAuthProvider,
    OAuthTokenStore,
    grant_covers,
)
from solercia_connect.oauth_session import LoopbackCallbackServer, build_pkce_pair
from solercia_connect.ports import BrowserProviderAdapter, HTTPTransport
from solercia_connect.project_home import GitRunner, prepare_project_home
from solercia_connect.project_secrets import api_token_store_from_settings
from solercia_connect.providers.bitbucket import (
    BITBUCKET_ADMIN_TOKEN_GUIDANCE,
    BITBUCKET_TOKEN_GUIDANCE,
    BitbucketAdapter,
    BitbucketIdentity,
)
from solercia_connect.providers.gmail_compose import GmailComposeAdapter
from solercia_connect.providers.google import GoogleCalendarAdapter, GoogleGmailAdapter
from solercia_connect.providers.google_browser import (
    GoogleBrowserAdapter,
    PlaywrightGoogleBrowserDriver,
)
from solercia_connect.providers.google_meet import GoogleMeetAdapter
from solercia_connect.providers.jira import (
    JiraAdapter,
    JiraIdentity,
    jira_api_base_url,
    resolve_jira_cloud_id,
    verify_atlassian_oauth_identity,
)
from solercia_connect.providers.m365_browser import (
    Microsoft365BrowserAdapter,
    PlaywrightBrowserDriver,
    browser_extraction_report,
)
from solercia_connect.providers.microsoft_graph import MicrosoftGraphAdapter
from solercia_connect.runtime import CONNECT_FALLBACK_REASONS
from solercia_connect.secret_store import SecretStore
from solercia_connect.secrets import SecretValue
from solercia_connect.transport import HttpxTransport

_GOOGLE_TARGETS = GOOGLE_TARGETS
# Google shows admin_policy_enforced or org_internal on its own page and never redirects to the
# loopback listener, so a blocked connect reaches the CLI only as a callback timeout.
GOOGLE_POLICY_BLOCK_REMEDIATION = (
    "If Google showed admin_policy_enforced or org_internal instead of returning here, the "
    "organization blocks this app; see the browser fallback route in "
    "docs/decisions/2026-10-25-fallback-navegador-google-jira.md (google-meet only, with "
    "--allow-browser-fallback)."
)


class OAuthProvider(Protocol):
    def authorization_header(self) -> str: ...


class MicrosoftInteractiveOAuth(OAuthProvider, Protocol):
    def start_device_authorization(self) -> DeviceAuthorization: ...

    def complete_device_authorization(
        self,
        authorization: DeviceAuthorization,
        *,
        max_attempts: int = 120,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> Any: ...


class ConnectionService:
    def __init__(
        self,
        settings: ConnectSettings,
        profiles: ProfileStore,
        token_store: OAuthTokenStore,
        *,
        oauth_builder: Callable[[ConnectionType, str], OAuthProvider] | None = None,
        identity_verifier: Callable[[ConnectionType, str, OAuthProvider], str] | None = None,
        browser_connector: Callable[[ConnectionType, str], str] | None = None,
        browser_status: Callable[[ConnectionType, str], str] | None = None,
        google_callback_flow: Callable[[GoogleOAuthProvider, bool], None] | None = None,
        atlassian_callback_flow: Callable[[AtlassianOAuthProvider, bool], None] | None = None,
        action_sink: Callable[[ResultEnvelope], None] | None = None,
        transport: HTTPTransport | None = None,
        secret_store: SecretStore | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleeper: Callable[[float], None] = time.sleep,
        git_runner: GitRunner | None = None,
    ) -> None:
        self._settings = settings
        self._profiles = profiles
        self._tokens = token_store
        self._secrets: SecretStore = secret_store or api_token_store_from_settings(settings)
        self._transport: HTTPTransport = transport or HttpxTransport()
        self._oauth_builder = oauth_builder or self._build_oauth
        self._identity_verifier = identity_verifier or self._verify_identity
        self._browser_connector = browser_connector or self._connect_browser
        self._browser_status = browser_status or self._status_browser
        self._google_callback_flow = google_callback_flow or self._run_google_callback
        self._atlassian_callback_flow = atlassian_callback_flow or self._run_atlassian_callback
        self._action_sink = action_sink or (lambda envelope: None)
        self._clock = clock
        self._sleeper = sleeper
        self._git_runner = git_runner

    def connect(
        self,
        type: str | ConnectionType,
        account: str,
        *,
        allow_browser_fallback: bool = False,
        open_browser: bool = True,
    ) -> ResultEnvelope:
        parsed, normalized, invalid = self._validated_identity(type, account, "connect")
        if invalid is not None:
            return invalid
        assert parsed is not None and normalized is not None
        spec = target_spec(parsed)
        if parsed is ConnectionType.JIRA:
            return jira_connect_requires_cli("connect", normalized)
        if parsed in BITBUCKET_TARGETS:
            return bitbucket_connect_requires_cli("connect", normalized, parsed)
        if parsed is ConnectionType.GLPI:
            return self._failure(
                "connect",
                parsed,
                normalized,
                ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
                "Interactive connection lifecycle is not used for this API-token provider",
                ("Configure the provider variables and use doctor or health.",),
                "capability",
            )
        microsoft_target = parsed in MICROSOFT_TARGETS
        if (
            parsed is ConnectionType.TEAMS
            and self._settings.microsoft_tenant.casefold() == "consumers"
        ):
            return self._failure(
                "connect",
                parsed,
                normalized,
                ReasonCode.PERSONAL_ACCOUNT_UNSUPPORTED,
                "Teams transcript APIs do not support personal Microsoft accounts",
                ("Use a Microsoft work or school tenant account.",),
                "provider_policy",
            )
        if microsoft_target and self._settings.microsoft_client_id is None:
            if self._browser_route_authorized(allow_browser_fallback):
                return self._browser_fallback(parsed, normalized)
            return self._missing_microsoft_route(
                parsed,
                normalized,
                allow_browser_fallback=allow_browser_fallback,
            )
        if (
            parsed in GOOGLE_TARGETS
            and self._settings.google_client_id is None
            and has_browser_route(parsed)
            and self._browser_route_authorized(allow_browser_fallback)
        ):
            # Like Microsoft without a client ID: no OAuth app, so the supervised browser is the
            # route the caller explicitly authorized.
            return self._browser_fallback(parsed, normalized)
        missing = self._missing_configuration(parsed)
        if missing:
            return ResultEnvelope.failure(
                operation="connect",
                status="configuration_error",
                provider=parsed.value,
                account=normalized,
                error=ErrorDetail(
                    "configuration_invalid",
                    "Required OAuth configuration is missing",
                    False,
                    missing,
                    tuple(f"Set {name}." for name in missing),
                    "configuration",
                ),
            )
        try:
            oauth = self._oauth_builder(parsed, normalized)
            if parsed in MICROSOFT_TARGETS:
                microsoft = cast(MicrosoftInteractiveOAuth, oauth)
                authorization = microsoft.start_device_authorization()
                self._emit_device_action(parsed, normalized, authorization)
                microsoft.complete_device_authorization(
                    authorization,
                    max_attempts=120,
                    sleeper=self._sleeper,
                )
            else:
                self._google_callback_flow(cast(GoogleOAuthProvider, oauth), open_browser)
            observed = normalize_account(self._identity_verifier(parsed, normalized, oauth))
            if observed != normalized:
                self._tokens.delete(_token_key(parsed, normalized))
                raise ConnectorFailure(
                    ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                    "Observed provider identity does not match the requested account",
                )
            profile = ConnectionProfile.create(
                parsed, normalized, route="official", now=self._clock()
            )
            self._profiles.save(profile)
            return ResultEnvelope.success(
                operation="connect",
                status="connected_official",
                provider=parsed.value,
                account=normalized,
                route="official",
                data={
                    "provider_id": spec.provider_id,
                    "capabilities": list(spec.capabilities),
                    "identity_verified": True,
                },
            )
        except ConnectorFailure as failure:
            if (
                has_browser_route(parsed)
                and self._browser_route_authorized(allow_browser_fallback)
                and failure.reason in CONNECT_FALLBACK_REASONS
            ):
                return self._browser_fallback(parsed, normalized)
            return self._connector_failure("connect", parsed, normalized, failure)
        except (TypeError, ValueError) as failure:
            return self._configuration_exception("connect", parsed, normalized, failure)
        except Exception:
            return self._failure(
                "connect",
                parsed,
                normalized,
                ReasonCode.UNKNOWN_ERROR,
                "Connection failed with an unknown error",
                ("Run doctor for structured readiness diagnostics.",),
                "unknown",
            )

    def connect_jira(
        self,
        account: str,
        *,
        site: str,
        token: SecretValue,
        token_kind: str = JiraTokenKind.CLASSIC,
        cloud_id: str | None = None,
    ) -> ResultEnvelope:
        """Connect one Jira site and account with an API token read by the canonical CLI.

        ``/myself`` proves the token before anything is stored. The profile keeps only the site,
        the token kind, the cloud ID of a scoped token, and the observed accountId; the token goes
        to the secret store and nowhere else. A classic token calls the site; a scoped token calls
        the Atlassian gateway, with the cloud ID given or read from the site's tenant_info.
        """

        parsed = ConnectionType.JIRA
        _, normalized, invalid = self._validated_identity(parsed, account, "connect")
        if invalid is not None:
            return invalid
        assert normalized is not None
        try:
            origin = normalize_jira_site(site)
            kind, cloud_id = normalize_jira_token_binding(origin, token_kind, cloud_id)
            if kind is JiraTokenKind.OAUTH:
                raise ValueError("--token-kind oauth authorizes in the browser and takes no token")
        except ValueError as failure:
            return _jira_invalid_input(normalized, failure)
        spec = target_spec(parsed)
        key = _token_key(parsed, normalized)
        try:
            if kind is JiraTokenKind.SCOPED and cloud_id is None:
                cloud_id = resolve_jira_cloud_id(self._transport, origin)
            identity = self._jira_identity(jira_api_base_url(origin, cloud_id), normalized, token)
            profile = ConnectionProfile.create(
                parsed,
                normalized,
                route="official",
                now=self._clock(),
                site=origin,
                provider_account_id=identity.account_id,
                token_kind=kind,
                cloud_id=cloud_id,
            )
            self._secrets.save(key, token)
            try:
                self._profiles.save(profile)
            except Exception:
                with suppress(Exception):
                    self._secrets.delete(key)
                raise
            return ResultEnvelope.success(
                operation="connect",
                status="connected_official",
                provider=parsed.value,
                account=normalized,
                route="official",
                data={
                    "provider_id": spec.provider_id,
                    "capabilities": list(spec.capabilities),
                    "identity_verified": True,
                    **_jira_identity_data(
                        profile, identity.account_id, _api_token_email_source(identity)
                    ),
                },
            )
        except ConnectorFailure as failure:
            return self._connector_failure(
                "connect", parsed, normalized, failure, site=origin, cloud_id=cloud_id
            )
        except (TypeError, ValueError) as failure:
            return self._configuration_exception("connect", parsed, normalized, failure)
        except Exception:
            return self._failure(
                "connect",
                parsed,
                normalized,
                ReasonCode.UNKNOWN_ERROR,
                "Connection failed with an unknown error",
                ("Run doctor for structured readiness diagnostics.",),
                "unknown",
            )

    def connect_bitbucket(
        self,
        account: str,
        *,
        workspace: str,
        token: SecretValue,
        connection_type: str | ConnectionType = ConnectionType.BITBUCKET,
    ) -> ResultEnvelope:
        """Connect one Bitbucket Cloud account with an API token read by the canonical CLI.

        The three-step identity proof of decision B-D2 runs before anything is stored. The token
        then goes to the secret store under ``<type>:<email>``, so the read-only ``bitbucket`` and
        the separate ``bitbucket-admin`` connections never share a token, and the profile keeps
        only the workspace and the observed account ID; a profile that cannot be written takes the
        token back out.
        """

        parsed = ConnectionType.parse(connection_type)
        if parsed not in BITBUCKET_TARGETS:
            raise ValueError("connect_bitbucket connects only bitbucket or bitbucket-admin")
        _, normalized, invalid = self._validated_identity(parsed, account, "connect")
        if invalid is not None:
            return invalid
        assert normalized is not None
        try:
            slug = normalize_bitbucket_workspace(workspace)
        except ValueError as failure:
            return _bitbucket_invalid_input(normalized, failure, parsed)
        spec = target_spec(parsed)
        key = _token_key(parsed, normalized)
        try:
            identity = self._bitbucket_identity(normalized, slug, token)
            profile = ConnectionProfile.create(
                parsed,
                normalized,
                route="official",
                now=self._clock(),
                provider_account_id=identity.account_id,
                workspace=slug,
            )
            self._secrets.save(key, token)
            try:
                self._profiles.save(profile)
            except Exception:
                with suppress(Exception):
                    self._secrets.delete(key)
                raise
            return ResultEnvelope.success(
                operation="connect",
                status="connected_official",
                provider=parsed.value,
                account=normalized,
                route="official",
                data={
                    "provider_id": spec.provider_id,
                    "capabilities": list(spec.capabilities),
                    "identity_verified": True,
                    **_bitbucket_identity_data(identity),
                },
            )
        except ConnectorFailure as failure:
            return self._connector_failure("connect", parsed, normalized, failure, workspace=slug)
        except (TypeError, ValueError) as failure:
            return self._configuration_exception("connect", parsed, normalized, failure)
        except Exception:
            return self._failure(
                "connect",
                parsed,
                normalized,
                ReasonCode.UNKNOWN_ERROR,
                "Connection failed with an unknown error",
                ("Run doctor for structured readiness diagnostics.",),
                "unknown",
            )

    def connect_jira_oauth(
        self,
        account: str,
        *,
        site: str,
        open_browser: bool = True,
    ) -> ResultEnvelope:
        """Connect one Jira site and account with Atlassian OAuth 2.0 (3LO) and the user's app.

        The grant goes to the OAuth token store under ``jira:<email>``. ``/me``, the grant's
        accessible resources and ``/myself`` prove the account and resolve the site's cloud ID
        before the profile is saved; any failure after the code exchange discards the grant.
        """

        parsed = ConnectionType.JIRA
        kind = JiraTokenKind.OAUTH
        _, normalized, invalid = self._validated_identity(parsed, account, "connect")
        if invalid is not None:
            return invalid
        assert normalized is not None
        try:
            origin = normalize_jira_site(site)
        except ValueError as failure:
            return _jira_invalid_input(normalized, failure, token_kind=kind)
        missing = missing_atlassian_configuration(self._settings)
        if missing:
            return _atlassian_configuration_error("connect", normalized, missing, self._settings)
        spec = target_spec(parsed)
        key = _token_key(parsed, normalized)
        exchanged = False
        try:
            oauth = atlassian_oauth_provider_from_settings(
                self._settings,
                self._tokens,
                normalized,
                transport=self._transport,
                clock=self._clock,
            )
            self._atlassian_callback_flow(oauth, open_browser)
            exchanged = True
            identity = verify_atlassian_oauth_identity(
                self._transport, oauth, site=origin, account=normalized
            )
            profile = ConnectionProfile.create(
                parsed,
                normalized,
                route="official",
                now=self._clock(),
                site=origin,
                provider_account_id=identity.account_id,
                token_kind=kind,
                cloud_id=identity.cloud_id,
            )
            self._profiles.save(profile)
            return ResultEnvelope.success(
                operation="connect",
                status="connected_official",
                provider=parsed.value,
                account=normalized,
                route="official",
                data={
                    "provider_id": spec.provider_id,
                    "capabilities": list(spec.capabilities),
                    "identity_verified": True,
                    **_jira_identity_data(profile, identity.account_id, identity.email_source),
                },
            )
        except Exception as failure:
            if exchanged:
                # A grant that did not prove this account and site is never kept.
                self._discard_oauth_tokens(key)
            if isinstance(failure, ConnectorFailure):
                return self._connector_failure(
                    "connect", parsed, normalized, failure, site=origin, token_kind=kind
                )
            if isinstance(failure, (TypeError, ValueError)):
                return self._configuration_exception("connect", parsed, normalized, failure)
            return self._failure(
                "connect",
                parsed,
                normalized,
                ReasonCode.UNKNOWN_ERROR,
                "Connection failed with an unknown error",
                ("Run doctor for structured readiness diagnostics.",),
                "unknown",
            )

    def status(
        self,
        type: str | ConnectionType,
        account: str,
        *,
        allow_browser_fallback: bool = False,
    ) -> ResultEnvelope:
        parsed, normalized, invalid = self._validated_identity(type, account, "status")
        if invalid is not None:
            return invalid
        assert parsed is not None and normalized is not None
        try:
            profile = self._profiles.get(parsed, normalized)
            if profile is None:
                return self._action_required("status", parsed, normalized)
            if profile.route == "browser":
                if not allow_browser_fallback:
                    return ResultEnvelope.failure(
                        operation="status",
                        status="action_required",
                        provider=parsed.value,
                        account=normalized,
                        route="browser",
                        data={"interactive_flow_started": False},
                        error=ErrorDetail(
                            "action_required",
                            "Browser session inspection requires explicit CLI authorization",
                            True,
                            remediation=(
                                "solercia-connect status "
                                f"{parsed.value} {normalized} --allow-browser-fallback",
                            ),
                            cause_category="interactive_authorization",
                        ),
                    )
                if not self._settings.browser_fallback_enabled:
                    return ResultEnvelope.failure(
                        operation="status",
                        status="configuration_error",
                        provider=parsed.value,
                        account=normalized,
                        route="browser",
                        data={"interactive_flow_started": False},
                        error=ErrorDetail(
                            "configuration_invalid",
                            "Browser fallback is not globally available",
                            False,
                            remediation=(
                                "SOLERCIA_CONNECT_BROWSER_FALLBACK=true solercia-connect status "
                                f"{parsed.value} {normalized} --allow-browser-fallback",
                            ),
                            cause_category="configuration",
                        ),
                    )
                observed = normalize_account(self._browser_status(parsed, normalized))
                if observed != normalized:
                    raise ConnectorFailure(
                        ReasonCode.BROWSER_ACCOUNT_MISMATCH,
                        "Browser session identity no longer matches the connection profile",
                    )
                return ResultEnvelope.success(
                    operation="status",
                    status="connected_browser",
                    provider=parsed.value,
                    account=normalized,
                    route="browser",
                    data={
                        "identity_verified": True,
                        **browser_extraction_report(parsed),
                    },
                )
            if parsed is ConnectionType.JIRA:
                return self._status_jira(profile, normalized)
            if parsed in BITBUCKET_TARGETS:
                return self._status_bitbucket(profile, normalized, parsed)
            tokens = self._tokens.load(_token_key(parsed, normalized))
            if tokens is None:
                return self._action_required("status", parsed, normalized)
            if not grant_covers(target_spec(parsed).oauth_scopes, tokens.scopes):
                return self._failure(
                    "status",
                    parsed,
                    normalized,
                    ReasonCode.INVALID_CREDENTIALS,
                    "Stored OAuth grant does not contain every required capability scope",
                    (connect_command(parsed, normalized),),
                    "authorization",
                )
            oauth = self._oauth_builder(parsed, normalized)
            oauth.authorization_header()
            observed = normalize_account(self._identity_verifier(parsed, normalized, oauth))
            if observed != normalized:
                raise ConnectorFailure(
                    ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                    "Observed provider identity does not match the connection profile",
                )
            return ResultEnvelope.success(
                operation="status",
                status="connected_official",
                provider=parsed.value,
                account=normalized,
                route="official",
                data={"identity_verified": True, "token_refresh_checked": True},
            )
        except ConnectorFailure as failure:
            if failure.reason in {
                ReasonCode.BROWSER_LOGIN_REQUIRED,
                ReasonCode.BROWSER_MFA_REQUIRED,
            }:
                return self._action_required(
                    "status",
                    parsed,
                    normalized,
                    failure.safe_message,
                    allow_browser_fallback=True,
                )
            if failure.reason is ReasonCode.INVALID_CREDENTIALS:
                return self._action_required("status", parsed, normalized, failure.safe_message)
            return self._connector_failure("status", parsed, normalized, failure)
        except (TypeError, ValueError) as failure:
            return self._configuration_exception("status", parsed, normalized, failure)
        except Exception:
            return self._failure(
                "status",
                parsed,
                normalized,
                ReasonCode.UNKNOWN_ERROR,
                "Connection status failed with an unknown error",
                ("Run doctor for structured readiness diagnostics.",),
                "unknown",
            )

    def test_connection(
        self,
        type: str | ConnectionType,
        account: str,
        *,
        allow_browser_fallback: bool = False,
    ) -> ResultEnvelope:
        """Non-interactive connection test for HTTP and automation adapters."""

        parsed, normalized, invalid = self._validated_identity(type, account, "connections.test")
        if invalid is not None:
            return invalid
        assert parsed is not None and normalized is not None
        try:
            profile = self._profiles.get(parsed, normalized)
            browser_check = (
                profile is not None
                and profile.route == "browser"
                and self._browser_route_authorized(allow_browser_fallback)
            )
            if profile is None or (profile.route == "browser" and not browser_check):
                command = connect_command(
                    parsed,
                    normalized,
                    allow_browser_fallback=allow_browser_fallback,
                )
                remediation: tuple[str, ...] = (command,)
                if profile is None and allow_browser_fallback:
                    # Nothing to test yet: the HTTP caller can start the supervised browser
                    # itself when the service allows interactive login.
                    remediation += (http_connect_hint(parsed, normalized),)
                return ResultEnvelope.failure(
                    operation="connections.test",
                    status="action_required",
                    provider=parsed.value,
                    account=normalized,
                    route=profile.route if profile is not None else None,
                    data={"interactive_flow_started": False},
                    error=ErrorDetail(
                        "action_required",
                        (
                            "Interactive browser verification must run in the canonical CLI"
                            if profile is not None
                            else "Interactive authorization must run in the canonical CLI"
                        ),
                        True,
                        remediation=remediation,
                        cause_category="interactive_authorization",
                    ),
                )
            # A browser profile is verified headless against the persistent profile; the
            # official profile keeps the token-backed identity check.
            result = self.status(parsed, normalized, allow_browser_fallback=browser_check)
            return replace(result, operation="connections.test")
        except ConnectorFailure as failure:
            return self._connector_failure("connections.test", parsed, normalized, failure)
        except (TypeError, ValueError) as failure:
            return self._configuration_exception("connections.test", parsed, normalized, failure)

    def connect_browser(self, type: str | ConnectionType, account: str) -> ResultEnvelope:
        """Start the supervised headed browser login for a Microsoft target.

        This is the one documented deviation from official-first: the loopback service has no
        terminal in which to show a device code, so this entry point goes straight to the
        browser route. Password and MFA stay on Microsoft's page inside the persistent profile.
        """

        parsed, normalized, invalid = self._validated_identity(type, account, "connections.connect")
        if invalid is not None:
            return invalid
        assert parsed is not None and normalized is not None
        if not has_browser_route(parsed):
            return self._failure(
                "connections.connect",
                parsed,
                normalized,
                ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
                "Browser login is available only for Microsoft targets and Google Meet",
                (connect_command(parsed, normalized),),
                "capability",
            )
        if (
            parsed is ConnectionType.TEAMS
            and self._settings.microsoft_tenant.casefold() == "consumers"
        ):
            return self._failure(
                "connections.connect",
                parsed,
                normalized,
                ReasonCode.PERSONAL_ACCOUNT_UNSUPPORTED,
                "Teams transcript APIs do not support personal Microsoft accounts",
                ("Use a Microsoft work or school tenant account.",),
                "provider_policy",
            )
        if not self._settings.browser_fallback_enabled:
            return ResultEnvelope.failure(
                operation="connections.connect",
                status="configuration_error",
                provider=parsed.value,
                account=normalized,
                route="browser",
                data={"interactive_flow_started": False},
                error=ErrorDetail(
                    "configuration_invalid",
                    "Browser fallback is not globally available",
                    False,
                    ("SOLERCIA_CONNECT_BROWSER_FALLBACK",),
                    (
                        "Set SOLERCIA_CONNECT_BROWSER_FALLBACK=true and restart the service.",
                        connect_command(parsed, normalized, allow_browser_fallback=True),
                    ),
                    "configuration",
                ),
            )
        try:
            result = self._browser_fallback(parsed, normalized)
        except (TypeError, ValueError) as failure:
            return self._configuration_exception("connections.connect", parsed, normalized, failure)
        except Exception:
            return self._failure(
                "connections.connect",
                parsed,
                normalized,
                ReasonCode.UNKNOWN_ERROR,
                "Browser login failed with an unknown error",
                ("Run doctor for structured readiness diagnostics.",),
                "unknown",
            )
        return replace(result, operation="connections.connect")

    def disconnect(self, type: str | ConnectionType, account: str) -> ResultEnvelope:
        parsed, normalized, invalid = self._validated_identity(type, account, "disconnect")
        if invalid is not None:
            return invalid
        assert parsed is not None and normalized is not None
        try:
            profile_removed = self._profiles.delete(parsed, normalized)
            if parsed is ConnectionType.JIRA:
                # Every credential of the account goes: the API token and any Atlassian OAuth
                # grant, which a real keyring refuses to delete when it is absent.
                key = _token_key(parsed, normalized)
                self._secrets.delete(key)
                if self._tokens.load(key) is not None:
                    self._tokens.delete(key)
            elif parsed in BITBUCKET_TARGETS:
                # Only this target's API token of this account; Bitbucket keeps no OAuth state.
                self._secrets.delete(_token_key(parsed, normalized))
            else:
                self._tokens.delete(_token_key(parsed, normalized))
            return ResultEnvelope.success(
                operation="disconnect",
                status="disconnected",
                provider=parsed.value,
                account=normalized,
                data={
                    "local_profile_removed": profile_removed,
                    "local_token_removed": True,
                    "remote_revocation": "not_performed",
                    "remote_revocation_remediation": (
                        "Revoke Solercia Connect in the provider account or tenant portal when "
                        "remote revocation is required."
                    ),
                },
            )
        except ConnectorFailure as failure:
            return self._connector_failure("disconnect", parsed, normalized, failure)
        except (TypeError, ValueError) as failure:
            return self._configuration_exception("disconnect", parsed, normalized, failure)

    def _browser_fallback(
        self,
        parsed: ConnectionType,
        normalized: str,
    ) -> ResultEnvelope:
        try:
            observed = normalize_account(self._browser_connector(parsed, normalized))
            if observed != normalized:
                raise ConnectorFailure(
                    ReasonCode.BROWSER_ACCOUNT_MISMATCH,
                    "Observed browser identity does not match the requested account",
                )
            self._profiles.save(
                ConnectionProfile.create(parsed, normalized, route="browser", now=self._clock())
            )
            return ResultEnvelope.success(
                operation="connect",
                status="connected_browser",
                provider=parsed.value,
                account=normalized,
                route="browser",
                data={
                    "identity_verified": True,
                    "session_status": "connected_browser",
                    **browser_extraction_report(parsed),
                },
            )
        except ConnectorFailure as failure:
            return self._connector_failure("connect", parsed, normalized, failure)

    def _build_oauth(self, parsed: ConnectionType, account: str) -> OAuthProvider:
        return oauth_provider_from_settings(
            self._settings,
            self._tokens,
            parsed,
            account,
            transport=self._transport,
        )

    def _verify_identity(
        self,
        parsed: ConnectionType,
        account: str,
        oauth: OAuthProvider,
    ) -> str:
        if parsed in MICROSOFT_TARGETS:
            return (
                MicrosoftGraphAdapter(
                    self._transport,
                    oauth,
                    expected_account=account,
                )
                .health()
                .account_id
                or ""
            )
        if parsed is ConnectionType.GMAIL:
            return (
                GoogleGmailAdapter(
                    self._transport,
                    oauth,
                    expected_account=account,
                )
                .health()
                .account_id
                or ""
            )
        if parsed is ConnectionType.GMAIL_COMPOSE:
            # gmail.compose may read the profile, so the compose grant proves its own identity.
            return (
                GmailComposeAdapter(
                    self._transport,
                    oauth,
                    expected_account=account,
                )
                .health()
                .account_id
                or ""
            )
        if parsed is ConnectionType.GOOGLE_CALENDAR:
            return (
                GoogleCalendarAdapter(
                    self._transport,
                    oauth,
                    expected_account=account,
                )
                .health()
                .account_id
                or ""
            )
        if parsed is ConnectionType.GOOGLE_MEET:
            return (
                GoogleMeetAdapter(
                    self._transport,
                    oauth,
                    expected_account=account,
                )
                .health()
                .account_id
                or ""
            )
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "This provider does not use OAuth identity verification",
        )

    def _status_jira(self, profile: ConnectionProfile, account: str) -> ResultEnvelope:
        if profile.token_kind is JiraTokenKind.OAUTH:
            return self._status_jira_oauth(profile, account)
        parsed = ConnectionType.JIRA
        site = profile.site
        cloud_id = profile.cloud_id
        try:
            token = self._secrets.load(_token_key(parsed, account))
            if site is None or profile.provider_account_id is None or token is None:
                return self._action_required(
                    "status",
                    parsed,
                    account,
                    "The Jira API token connection must be renewed in the canonical CLI",
                    site=site,
                    cloud_id=cloud_id,
                )
            identity = self._jira_identity(
                jira_api_base_url(site, cloud_id),
                account,
                token,
                expected_account_id=profile.provider_account_id,
            )
        except ConnectorFailure as failure:
            if failure.reason is ReasonCode.INVALID_CREDENTIALS:
                return self._action_required(
                    "status", parsed, account, failure.safe_message, site=site, cloud_id=cloud_id
                )
            return self._connector_failure(
                "status", parsed, account, failure, site=site, cloud_id=cloud_id
            )
        return ResultEnvelope.success(
            operation="status",
            status="connected_official",
            provider=parsed.value,
            account=account,
            route="official",
            data={
                "identity_verified": True,
                **_jira_identity_data(
                    profile, identity.account_id, _api_token_email_source(identity)
                ),
            },
        )

    def _status_jira_oauth(self, profile: ConnectionProfile, account: str) -> ResultEnvelope:
        """Refresh the grant when due, then re-prove the account, accountId and cloud ID."""

        parsed = ConnectionType.JIRA
        kind = JiraTokenKind.OAUTH
        site = profile.site
        missing = missing_atlassian_configuration(self._settings)
        if missing:
            return _atlassian_configuration_error("status", account, missing, self._settings)
        try:
            key = _token_key(parsed, account)
            if (
                site is None
                or profile.provider_account_id is None
                or profile.cloud_id is None
                or self._tokens.load(key) is None
            ):
                return self._action_required(
                    "status",
                    parsed,
                    account,
                    "The Atlassian authorization must be renewed in the canonical CLI",
                    site=site,
                    token_kind=kind,
                )
            oauth = atlassian_oauth_provider_from_settings(
                self._settings, self._tokens, account, transport=self._transport, clock=self._clock
            )
            identity = verify_atlassian_oauth_identity(
                self._transport,
                oauth,
                site=site,
                account=account,
                expected_account_id=profile.provider_account_id,
                expected_cloud_id=profile.cloud_id,
            )
        except ConnectorFailure as failure:
            if failure.reason is ReasonCode.INVALID_CREDENTIALS:
                return self._action_required(
                    "status", parsed, account, failure.safe_message, site=site, token_kind=kind
                )
            return self._connector_failure(
                "status", parsed, account, failure, site=site, token_kind=kind
            )
        return ResultEnvelope.success(
            operation="status",
            status="connected_official",
            provider=parsed.value,
            account=account,
            route="official",
            data={
                "identity_verified": True,
                "token_refresh_checked": True,
                **_jira_identity_data(profile, identity.account_id, identity.email_source),
            },
        )

    def _status_bitbucket(
        self, profile: ConnectionProfile, account: str, parsed: ConnectionType
    ) -> ResultEnvelope:
        """Repeat the identity proof against the stored account ID and workspace."""

        workspace = profile.workspace
        try:
            token = self._secrets.load(_token_key(parsed, account))
            if workspace is None or profile.provider_account_id is None or token is None:
                return self._action_required(
                    "status",
                    parsed,
                    account,
                    "The Bitbucket API token connection must be renewed in the canonical CLI",
                    workspace=workspace,
                )
            identity = self._bitbucket_identity(
                account, workspace, token, expected_account_id=profile.provider_account_id
            )
        except ConnectorFailure as failure:
            if failure.reason is ReasonCode.INVALID_CREDENTIALS:
                return self._action_required(
                    "status", parsed, account, failure.safe_message, workspace=workspace
                )
            return self._connector_failure("status", parsed, account, failure, workspace=workspace)
        return ResultEnvelope.success(
            operation="status",
            status="connected_official",
            provider=parsed.value,
            account=account,
            route="official",
            data={"identity_verified": True, **_bitbucket_identity_data(identity)},
        )

    def _bitbucket_identity(
        self,
        account: str,
        workspace: str,
        token: SecretValue,
        *,
        expected_account_id: str | None = None,
    ) -> BitbucketIdentity:
        return BitbucketAdapter(
            self._transport,
            BasicAuthProvider(account, token),
            account=account,
            workspace=workspace,
            expected_account_id=expected_account_id,
            clock=self._clock,
        ).verify_identity()

    def _discard_oauth_tokens(self, key: str) -> None:
        """Delete stored OAuth state if present; a keyring refuses to delete a missing entry."""

        with suppress(Exception):
            if self._tokens.load(key) is not None:
                self._tokens.delete(key)

    def _jira_identity(
        self,
        base_url: str,
        account: str,
        token: SecretValue,
        *,
        expected_account_id: str | None = None,
    ) -> JiraIdentity:
        return JiraAdapter(
            self._transport,
            BasicAuthProvider(account, token),
            base_url=base_url,
            expected_account_id=expected_account_id,
            expected_account=account,
        ).verify_identity()

    def _run_google_callback(self, oauth: GoogleOAuthProvider, open_browser: bool) -> None:
        pair = build_pkce_pair()
        with LoopbackCallbackServer(pair.state) as callback:
            url = oauth.authorization_url(
                redirect_uri=callback.redirect_uri,
                state=pair.state,
                code_challenge=pair.challenge,
            )
            self._action_sink(
                ResultEnvelope.failure(
                    operation="connect",
                    status="action_required",
                    error=ErrorDetail(
                        "action_required",
                        "Complete Google authorization in a browser",
                        True,
                        remediation=("Open the authorization URL and return to the CLI.",),
                        cause_category="interactive_authorization",
                    ),
                    data={"authorization_url": url, "callback_timeout_seconds": 180},
                )
            )
            if open_browser:
                with suppress(Exception):
                    webbrowser.open(url, new=1, autoraise=True)
            result = callback.wait()
            oauth.exchange_authorization_code(
                result.code,
                redirect_uri=callback.redirect_uri,
                code_verifier=pair.verifier,
            )

    def _run_atlassian_callback(self, oauth: AtlassianOAuthProvider, open_browser: bool) -> None:
        # Atlassian has no PKCE, so only the state of the pair is used; the redirect URI is the
        # exact callback registered for the app, served on its fixed loopback port.
        redirect_uri = self._settings.atlassian_redirect_uri
        state = build_pkce_pair().state
        with LoopbackCallbackServer(state, port=urlsplit(redirect_uri).port or 0) as callback:
            url = oauth.authorization_url(redirect_uri=redirect_uri, state=state)
            self._action_sink(
                ResultEnvelope.failure(
                    operation="connect",
                    status="action_required",
                    provider=ConnectionType.JIRA.value,
                    error=ErrorDetail(
                        "action_required",
                        "Complete Atlassian authorization in a browser",
                        True,
                        remediation=("Open the authorization URL and return to the CLI.",),
                        cause_category="interactive_authorization",
                    ),
                    data={"authorization_url": url, "callback_timeout_seconds": 180},
                )
            )
            if open_browser:
                with suppress(Exception):
                    webbrowser.open(url, new=1, autoraise=True)
            result = callback.wait()
            oauth.exchange_authorization_code(result.code, redirect_uri=redirect_uri)

    def _connect_browser(self, parsed: ConnectionType, account: str) -> str:
        adapter = self._browser_adapter(parsed, account)
        return adapter.interactive_login().account_id or ""

    def _status_browser(self, parsed: ConnectionType, account: str) -> str:
        adapter = self._browser_adapter(parsed, account)
        return adapter.health().account_id or ""

    def _browser_adapter(
        self,
        parsed: ConnectionType,
        account: str,
    ) -> BrowserProviderAdapter:
        family = target_family(parsed)
        if not has_browser_route(parsed) or family not in {
            ProviderFamily.MICROSOFT,
            ProviderFamily.GOOGLE,
        }:
            raise ConnectorFailure(
                ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED,
                f"No browser adapter exists for {parsed.value}",
            )
        if family is ProviderFamily.GOOGLE:
            return GoogleBrowserAdapter(
                PlaywrightGoogleBrowserDriver(),
                profile_dir=browser_profile_path(self._settings, family, account),
                expected_account=account,
            )
        return Microsoft365BrowserAdapter(
            PlaywrightBrowserDriver(),
            connection_type=parsed,
            profile_dir=microsoft_browser_profile_path(self._settings, account),
            expected_account=account,
        )

    def _emit_device_action(
        self,
        parsed: ConnectionType,
        account: str,
        authorization: DeviceAuthorization,
    ) -> None:
        self._action_sink(
            ResultEnvelope.failure(
                operation="connect",
                status="action_required",
                provider=parsed.value,
                account=account,
                error=ErrorDetail(
                    "action_required",
                    "Complete Microsoft device authorization",
                    True,
                    remediation=("Open the verification URL and enter the user code.",),
                    cause_category="interactive_authorization",
                ),
                data={
                    "verification_uri": authorization.verification_uri,
                    "user_code": authorization.user_code,
                    "expires_at": authorization.expires_at.isoformat(),
                },
            )
        )

    def _missing_configuration(self, parsed: ConnectionType) -> tuple[str, ...]:
        """Report what this target declares it needs, never what a fallback branch assumed.

        Deciding by an else-branch made every unlisted target report Google's client ID, so a
        Microsoft target could be told to configure the wrong provider entirely.
        """
        configured = {
            "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID": self._settings.microsoft_client_id,
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": self._settings.google_client_id,
        }
        return tuple(
            name
            for name in target_spec(parsed).required_configuration
            if name in configured and not configured[name]
        )

    def _browser_route_authorized(self, allow_browser_fallback: bool) -> bool:
        return self._settings.browser_fallback_enabled and allow_browser_fallback

    def _missing_microsoft_route(
        self,
        parsed: ConnectionType,
        account: str,
        *,
        allow_browser_fallback: bool,
    ) -> ResultEnvelope:
        command = connect_command(parsed, account, allow_browser_fallback=True)
        if self._settings.browser_fallback_enabled and not allow_browser_fallback:
            return ResultEnvelope.failure(
                operation="connect",
                status="action_required",
                provider=parsed.value,
                account=account,
                data={
                    "official_route_available": False,
                    "browser_route_available": True,
                    "interactive_flow_started": False,
                },
                error=ErrorDetail(
                    "action_required",
                    "The available Microsoft browser route requires explicit authorization",
                    True,
                    ("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID",),
                    (command,),
                    "interactive_authorization",
                ),
            )
        return ResultEnvelope.failure(
            operation="connect",
            status="configuration_error",
            provider=parsed.value,
            account=account,
            data={
                "official_route_available": False,
                "browser_route_available": False,
                "interactive_flow_started": False,
            },
            error=ErrorDetail(
                "configuration_invalid",
                "Neither Microsoft connection route is currently available",
                False,
                ("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID",),
                (f"SOLERCIA_CONNECT_BROWSER_FALLBACK=true {command}",),
                "configuration",
            ),
        )

    def _validated_identity(
        self,
        type: str | ConnectionType,
        account: str,
        operation: str,
    ) -> tuple[ConnectionType | None, str | None, ResultEnvelope | None]:
        try:
            parsed, normalized = ConnectionType.parse(type), normalize_account(account)
        except (TypeError, ValueError) as exc:
            return (
                None,
                None,
                ResultEnvelope.failure(
                    operation=operation,
                    status="invalid_input",
                    error=ErrorDetail(
                        "invalid_input",
                        str(exc),
                        False,
                        remediation=("Use a supported provider type and one email account.",),
                        cause_category="input",
                    ),
                ),
            )
        home = self._settings.project_home
        if home is not None:
            # Every entry point validates its identity first, so project mode prepares its folder
            # and checks git here: status and test can refresh tokens or reopen a browser session,
            # which writes under the home just like connect does.
            try:
                prepare_project_home(home, runner=self._git_runner)
            except ProjectHomeError as failure:
                return (
                    parsed,
                    normalized,
                    self._configuration_exception(operation, parsed, normalized, failure),
                )
        return parsed, normalized, None

    def _action_required(
        self,
        operation: str,
        parsed: ConnectionType,
        account: str,
        message: str = "Interactive authorization is required",
        *,
        allow_browser_fallback: bool = False,
        site: str | None = None,
        cloud_id: str | None = None,
        token_kind: JiraTokenKind | None = None,
        workspace: str | None = None,
    ) -> ResultEnvelope:
        return ResultEnvelope.failure(
            operation=operation,
            status="action_required",
            provider=parsed.value,
            account=account,
            error=ErrorDetail(
                "action_required",
                message,
                True,
                remediation=(
                    connect_command(
                        parsed,
                        account,
                        allow_browser_fallback=allow_browser_fallback,
                        site=site,
                        cloud_id=cloud_id,
                        token_kind=token_kind,
                        workspace=workspace,
                    ),
                ),
                cause_category="interactive_authorization",
            ),
        )

    def _connector_failure(
        self,
        operation: str,
        parsed: ConnectionType,
        account: str,
        failure: ConnectorFailure,
        *,
        site: str | None = None,
        cloud_id: str | None = None,
        token_kind: JiraTokenKind | None = None,
        workspace: str | None = None,
    ) -> ResultEnvelope:
        remediation = _remediation(
            failure.reason, parsed, account, site, cloud_id, token_kind, workspace=workspace
        )
        return self._failure(
            operation,
            parsed,
            account,
            failure.reason,
            failure.safe_message,
            remediation,
            _cause_category(failure.reason),
        )

    def _configuration_exception(
        self,
        operation: str,
        parsed: ConnectionType,
        account: str,
        failure: Exception,
    ) -> ResultEnvelope:
        return self._failure(
            operation,
            parsed,
            account,
            ReasonCode.CONFIGURATION_INVALID,
            str(failure),
            ("Run solercia-connect config validate and doctor.",),
            "configuration",
        )

    def _failure(
        self,
        operation: str,
        parsed: ConnectionType,
        account: str,
        reason: ReasonCode,
        message: str,
        remediation: tuple[str, ...],
        category: str,
    ) -> ResultEnvelope:
        return ResultEnvelope.failure(
            operation=operation,
            status=("invalid_input" if reason is ReasonCode.INVALID_INPUT else "failed"),
            provider=parsed.value,
            account=account,
            error=ErrorDetail(
                reason.value,
                message,
                reason
                in {
                    ReasonCode.NETWORK_TIMEOUT,
                    ReasonCode.NETWORK_UNAVAILABLE,
                    ReasonCode.PROVIDER_UNAVAILABLE,
                    ReasonCode.RATE_LIMITED,
                    ReasonCode.BROWSER_IDENTITY_UNAVAILABLE,
                    ReasonCode.BROWSER_SELECTOR_MISMATCH,
                },
                remediation=remediation,
                cause_category=category,
            ),
        )


def _token_key(parsed: ConnectionType, account: str) -> str:
    return f"{parsed.value}:{normalize_account(account)}"


def _jira_identity_data(
    profile: ConnectionProfile, account_id: str, email_source: str
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "site": profile.site,
        "provider_account_id": account_id,
        "email_source": email_source,
        # Profiles written before scoped tokens carry no kind and are classic.
        "token_kind": (profile.token_kind or JiraTokenKind.CLASSIC).value,
    }
    if profile.cloud_id is not None:
        data["cloud_id"] = profile.cloud_id
    return data


def _api_token_email_source(identity: JiraIdentity) -> str:
    # A hidden emailAddress leaves the account proven by Basic authentication alone.
    return "myself" if identity.email_visible else "authentication"


def _bitbucket_identity_data(identity: BitbucketIdentity) -> dict[str, Any]:
    # The email is proven by /user/emails, where Bitbucket marks it confirmed.
    return {
        "workspace": identity.workspace,
        "provider_account_id": identity.account_id,
        "email_source": "user/emails",
    }


def _bitbucket_invalid_input(
    account: str, failure: ValueError, parsed: ConnectionType
) -> ResultEnvelope:
    return ResultEnvelope.failure(
        operation="connect",
        status="invalid_input",
        provider=parsed.value,
        account=account,
        error=ErrorDetail(
            "invalid_input",
            str(failure),
            False,
            remediation=(connect_command(parsed, account),),
            cause_category="input",
        ),
    )


def bitbucket_connect_requires_cli(
    operation: str, account: str, parsed: ConnectionType = ConnectionType.BITBUCKET
) -> ResultEnvelope:
    """Refuse a Bitbucket connect that lacks the workspace and the token only the CLI may read."""

    return ResultEnvelope.failure(
        operation=operation,
        status="configuration_error",
        provider=parsed.value,
        account=account,
        data={"interactive_flow_started": False},
        error=ErrorDetail(
            "configuration_invalid",
            "Bitbucket connects with --workspace and an API token read by the CLI: the hidden "
            "prompt, --token-stdin or --token-env",
            False,
            ("workspace", "api_token"),
            (connect_command(parsed, account),),
            "configuration",
        ),
    )


def _jira_invalid_input(
    account: str, failure: ValueError, *, token_kind: JiraTokenKind | None = None
) -> ResultEnvelope:
    return ResultEnvelope.failure(
        operation="connect",
        status="invalid_input",
        provider=ConnectionType.JIRA.value,
        account=account,
        error=ErrorDetail(
            "invalid_input",
            str(failure),
            False,
            remediation=(connect_command(ConnectionType.JIRA, account, token_kind=token_kind),),
            cause_category="input",
        ),
    )


def _atlassian_configuration_error(
    operation: str, account: str, missing: tuple[str, ...], settings: ConnectSettings
) -> ResultEnvelope:
    return ResultEnvelope.failure(
        operation=operation,
        status="configuration_error",
        provider=ConnectionType.JIRA.value,
        account=account,
        error=ErrorDetail(
            "configuration_invalid",
            "Required Atlassian OAuth configuration is missing",
            False,
            missing,
            (
                *(f"Set {name}." for name in missing),
                f"Register {settings.atlassian_redirect_uri} as the app's callback URL, or set "
                "SOLERCIA_CONNECT_ATLASSIAN_REDIRECT_URI to the registered one.",
            ),
            "configuration",
        ),
    )


def atlassian_oauth_provider_from_settings(
    settings: ConnectSettings,
    token_store: OAuthTokenStore,
    account: str,
    *,
    transport: HTTPTransport | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> AtlassianOAuthProvider:
    """Build the Jira OAuth provider of one account; its grant lives under ``jira:<email>``."""

    client_id = settings.atlassian_client_id
    client_secret = settings.atlassian_client_secret
    if client_id is None or client_secret is None:
        missing = ", ".join(missing_atlassian_configuration(settings))
        raise ValueError(f"Atlassian OAuth is not configured; set {missing}")
    return AtlassianOAuthProvider(
        transport or HttpxTransport(),
        client_id=client_id,
        client_secret=client_secret,
        store=token_store,
        store_key=_token_key(ConnectionType.JIRA, account),
        clock=clock,
    )


def jira_connect_requires_cli(operation: str, account: str) -> ResultEnvelope:
    """Refuse a Jira connect that lacks the site and the token only the CLI prompt may read."""

    return ResultEnvelope.failure(
        operation=operation,
        status="configuration_error",
        provider=ConnectionType.JIRA.value,
        account=account,
        data={"interactive_flow_started": False},
        error=ErrorDetail(
            "configuration_invalid",
            "Jira connects with --site and an API token entered in the CLI hidden prompt",
            False,
            ("site", "api_token"),
            (connect_command(ConnectionType.JIRA, account),),
            "configuration",
        ),
    )


def oauth_provider_from_settings(
    settings: ConnectSettings,
    token_store: OAuthTokenStore,
    parsed: ConnectionType,
    account: str,
    *,
    transport: HTTPTransport | None = None,
) -> OAuthProvider:
    spec = target_spec(parsed)
    key = _token_key(parsed, account)
    http = transport or HttpxTransport()
    if parsed in MICROSOFT_TARGETS:
        client_id = settings.microsoft_client_id
        if client_id is None:
            raise ValueError("Microsoft client ID is not configured")
        return MicrosoftOAuthProvider(
            http,
            client_id=client_id,
            tenant=settings.microsoft_tenant,
            scopes=spec.oauth_scopes,
            store=token_store,
            store_key=key,
        )
    if parsed in GOOGLE_TARGETS:
        client_id = settings.google_client_id
        if client_id is None:
            raise ValueError("Google client ID is not configured")
        return GoogleOAuthProvider(
            http,
            client_id=client_id,
            scopes=spec.oauth_scopes,
            store=token_store,
            store_key=key,
            client_secret=settings.google_client_secret,
        )
    raise ValueError("Provider does not use the OAuth connection lifecycle")


def connect_command(
    parsed: ConnectionType,
    account: str,
    *,
    allow_browser_fallback: bool = False,
    site: str | None = None,
    cloud_id: str | None = None,
    token_kind: JiraTokenKind | None = None,
    workspace: str | None = None,
) -> str:
    command = f"solercia-connect connect {parsed.value} {account}"
    if parsed in BITBUCKET_TARGETS:
        command += f" --workspace {workspace or '<workspace>'}"
    if parsed is ConnectionType.JIRA:
        command += f" --site {site or '<site>'}"
        if token_kind is JiraTokenKind.OAUTH:
            command += " --token-kind oauth"
        elif cloud_id is not None:
            command += f" --token-kind scoped --cloud-id {cloud_id}"
    return command + (" --allow-browser-fallback" if allow_browser_fallback else "")


def status_command(
    parsed: ConnectionType,
    account: str,
    *,
    allow_browser_fallback: bool = False,
) -> str:
    command = f"solercia-connect status {parsed.value} {account}"
    return command + (" --allow-browser-fallback" if allow_browser_fallback else "")


def http_connect_hint(parsed: ConnectionType, account: str) -> str:
    """Name the loopback endpoint that starts the supervised browser when the service allows it."""

    return (
        "POST /v1/connections/connect "
        f'{{"type":"{parsed.value}","account":"{account}","allow_browser_fallback":true}}'
    )


def _remediation(
    reason: ReasonCode,
    parsed: ConnectionType,
    account: str,
    site: str | None = None,
    cloud_id: str | None = None,
    token_kind: JiraTokenKind | None = None,
    *,
    workspace: str | None = None,
) -> tuple[str, ...]:
    if parsed in BITBUCKET_TARGETS and (
        steps := bitbucket_failure_remediation(
            reason, account, workspace=workspace, connection_type=parsed
        )
    ):
        return steps
    oauth_jira = parsed is ConnectionType.JIRA and token_kind is JiraTokenKind.OAUTH
    if oauth_jira and reason is ReasonCode.INVALID_CREDENTIALS:
        return (
            "Run the connect command again to renew the Atlassian authorization.",
            connect_command(parsed, account, site=site, token_kind=token_kind),
        )
    if oauth_jira and reason in {
        ReasonCode.AUTHORIZATION_DENIED,
        ReasonCode.OAUTH_CALLBACK_INVALID,
        ReasonCode.OAUTH_STATE_MISMATCH,
        ReasonCode.OAUTH_CALLBACK_TIMEOUT,
    }:
        return (connect_command(parsed, account, site=site, token_kind=token_kind),)
    if parsed is ConnectionType.JIRA and reason is ReasonCode.INVALID_CREDENTIALS:
        return (
            "Check the account email and the API token, or create a new token at "
            "https://id.atlassian.com/manage-profile/security/api-tokens.",
            connect_command(parsed, account, site=site, cloud_id=cloud_id),
        )
    if parsed is ConnectionType.JIRA and reason is ReasonCode.AUTHORIZATION_DENIED:
        return (
            "Ask the Jira site administrator to allow API token access for this account.",
            connect_command(parsed, account, site=site, cloud_id=cloud_id),
        )
    if reason is ReasonCode.OAUTH_CALLBACK_TIMEOUT and parsed in _GOOGLE_TARGETS:
        return (connect_command(parsed, account), GOOGLE_POLICY_BLOCK_REMEDIATION)
    if reason is ReasonCode.ADMIN_CONSENT_REQUIRED:
        return (
            "Ask the tenant administrator to grant the required delegated scopes.",
            connect_command(parsed, account),
        )
    if reason is ReasonCode.TENANT_POLICY_BLOCKED:
        return ("Ask the tenant administrator to enable Microsoft Graph transcript access.",)
    if reason is ReasonCode.PERSONAL_ACCOUNT_UNSUPPORTED:
        return ("Use a Microsoft work or school tenant account.",)
    if reason in {
        ReasonCode.BROWSER_IDENTITY_UNAVAILABLE,
        ReasonCode.BROWSER_SELECTOR_MISMATCH,
    }:
        return (
            connect_command(parsed, account, allow_browser_fallback=True),
            status_command(parsed, account, allow_browser_fallback=True),
        )
    if reason in {
        ReasonCode.BROWSER_LOGIN_REQUIRED,
        ReasonCode.BROWSER_MFA_REQUIRED,
        ReasonCode.BROWSER_TIMEOUT,
        ReasonCode.BROWSER_ACCOUNT_MISMATCH,
    }:
        return (connect_command(parsed, account, allow_browser_fallback=True),)
    if reason in {
        ReasonCode.INVALID_CREDENTIALS,
        ReasonCode.AUTHORIZATION_DENIED,
        ReasonCode.OAUTH_CALLBACK_INVALID,
        ReasonCode.OAUTH_STATE_MISMATCH,
        ReasonCode.OAUTH_CALLBACK_TIMEOUT,
    }:
        return (connect_command(parsed, account),)
    return ("Run solercia-connect doctor for structured remediation.",)


# The next step of each Bitbucket failure the user can fix, followed by the exact connect command.
_BITBUCKET_REMEDIATION: dict[ReasonCode, str] = {
    ReasonCode.INVALID_CREDENTIALS: BITBUCKET_TOKEN_GUIDANCE,
    ReasonCode.AUTHORIZATION_DENIED: (
        "Create a new API token with scopes for the Bitbucket app that holds all four read "
        "scopes; the scopes of an existing token cannot be changed."
    ),
    ReasonCode.WORKSPACE_NOT_FOUND: (
        "Check the workspace slug in bitbucket.org/<workspace>/<repository> and that this "
        "account belongs to that workspace."
    ),
}


# The bitbucket-admin token carries the five scopes of decision R-D1, not the four read scopes.
_BITBUCKET_ADMIN_REMEDIATION: dict[ReasonCode, str] = {
    **_BITBUCKET_REMEDIATION,
    ReasonCode.INVALID_CREDENTIALS: BITBUCKET_ADMIN_TOKEN_GUIDANCE,
    ReasonCode.AUTHORIZATION_DENIED: (
        "Create a new API token with scopes for the Bitbucket app that holds all five scopes of "
        "the bitbucket-admin connection; the scopes of an existing token cannot be changed."
    ),
}


def bitbucket_failure_remediation(
    reason: ReasonCode,
    account: str,
    *,
    workspace: str | None = None,
    connection_type: ConnectionType = ConnectionType.BITBUCKET,
) -> tuple[str, ...] | None:
    """The next step of a Bitbucket failure the user can fix, or ``None`` when there is none."""

    remediation = (
        _BITBUCKET_ADMIN_REMEDIATION
        if connection_type is ConnectionType.BITBUCKET_ADMIN
        else _BITBUCKET_REMEDIATION
    )
    guidance = remediation.get(reason)
    if guidance is None:
        return None
    return (guidance, connect_command(connection_type, account, workspace=workspace))


def _cause_category(reason: ReasonCode) -> str:
    if reason in {
        ReasonCode.CONFIGURATION_INVALID,
        ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED,
    }:
        return "configuration"
    if reason in {
        ReasonCode.INVALID_CREDENTIALS,
        ReasonCode.AUTHORIZATION_DENIED,
        ReasonCode.ADMIN_CONSENT_REQUIRED,
        ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
        ReasonCode.OAUTH_CALLBACK_INVALID,
        ReasonCode.OAUTH_STATE_MISMATCH,
        ReasonCode.OAUTH_CALLBACK_TIMEOUT,
    }:
        return "authorization"
    if reason in {ReasonCode.PERSONAL_ACCOUNT_UNSUPPORTED, ReasonCode.TENANT_POLICY_BLOCKED}:
        return "provider_policy"
    if reason in {
        ReasonCode.NETWORK_TIMEOUT,
        ReasonCode.NETWORK_UNAVAILABLE,
        ReasonCode.PROVIDER_UNAVAILABLE,
        ReasonCode.RATE_LIMITED,
    }:
        return "network"
    return "runtime"
