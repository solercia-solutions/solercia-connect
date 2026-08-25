from __future__ import annotations

import time
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any, Protocol, cast

from solercia_connect.browser_profile import microsoft_browser_profile_path
from solercia_connect.config import (
    ConnectionProfile,
    ConnectionType,
    ConnectSettings,
    ProfileStore,
    normalize_account,
    target_spec,
)
from solercia_connect.domain import ReasonCode, utc_now
from solercia_connect.envelope import ErrorDetail, ResultEnvelope
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import (
    DeviceAuthorization,
    GoogleOAuthProvider,
    MicrosoftOAuthProvider,
    OAuthTokenStore,
)
from solercia_connect.oauth_session import LoopbackCallbackServer, build_pkce_pair
from solercia_connect.providers.google import GoogleCalendarAdapter, GoogleGmailAdapter
from solercia_connect.providers.m365_browser import (
    Microsoft365BrowserAdapter,
    PlaywrightBrowserDriver,
    browser_extraction_report,
)
from solercia_connect.providers.microsoft_graph import MicrosoftGraphAdapter
from solercia_connect.transport import HttpxTransport

_MICROSOFT_FALLBACK_REASONS = frozenset(
    {ReasonCode.ADMIN_CONSENT_REQUIRED, ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE}
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
        action_sink: Callable[[ResultEnvelope], None] | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._profiles = profiles
        self._tokens = token_store
        self._transport = HttpxTransport()
        self._oauth_builder = oauth_builder or self._build_oauth
        self._identity_verifier = identity_verifier or self._verify_identity
        self._browser_connector = browser_connector or self._connect_browser
        self._browser_status = browser_status or self._status_browser
        self._google_callback_flow = google_callback_flow or self._run_google_callback
        self._action_sink = action_sink or (lambda envelope: None)
        self._clock = clock
        self._sleeper = sleeper

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
        if parsed in {ConnectionType.JIRA, ConnectionType.GLPI}:
            return self._failure(
                "connect",
                parsed,
                normalized,
                ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
                "Interactive connection lifecycle is not used for this API-token provider",
                ("Configure the provider variables and use doctor or health.",),
                "capability",
            )
        microsoft_target = parsed in {ConnectionType.OUTLOOK, ConnectionType.TEAMS}
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
            if parsed in {ConnectionType.OUTLOOK, ConnectionType.TEAMS}:
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
                parsed in {ConnectionType.OUTLOOK, ConnectionType.TEAMS}
                and self._browser_route_authorized(allow_browser_fallback)
                and failure.reason in _MICROSOFT_FALLBACK_REASONS
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
                            "Microsoft browser fallback is not globally available",
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
            tokens = self._tokens.load(_token_key(parsed, normalized))
            if tokens is None:
                return self._action_required("status", parsed, normalized)
            required_scopes = set(target_spec(parsed).oauth_scopes)
            if not required_scopes.issubset(tokens.scopes):
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
            if profile is None or profile.route == "browser":
                command = connect_command(parsed, normalized)
                if allow_browser_fallback:
                    command += " --allow-browser-fallback"
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
                        remediation=(command,),
                        cause_category="interactive_authorization",
                    ),
                )
            result = self.status(parsed, normalized)
            return ResultEnvelope(
                result.ok,
                "connections.test",
                result.status,
                result.provider,
                result.account,
                result.route,
                result.data,
                result.receipt,
                result.error,
            )
        except ConnectorFailure as failure:
            return self._connector_failure("connections.test", parsed, normalized, failure)
        except (TypeError, ValueError) as failure:
            return self._configuration_exception("connections.test", parsed, normalized, failure)

    def disconnect(self, type: str | ConnectionType, account: str) -> ResultEnvelope:
        parsed, normalized, invalid = self._validated_identity(type, account, "disconnect")
        if invalid is not None:
            return invalid
        assert parsed is not None and normalized is not None
        try:
            profile_removed = self._profiles.delete(parsed, normalized)
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
        if parsed in {ConnectionType.OUTLOOK, ConnectionType.TEAMS}:
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
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "This provider does not use OAuth identity verification",
        )

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
    ) -> Microsoft365BrowserAdapter:
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
        if parsed in {ConnectionType.OUTLOOK, ConnectionType.TEAMS}:
            return (
                ()
                if self._settings.microsoft_client_id
                else ("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID",)
            )
        return () if self._settings.google_client_id else ("SOLERCIA_CONNECT_GOOGLE_CLIENT_ID",)

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
            return ConnectionType.parse(type), normalize_account(account), None
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

    def _action_required(
        self,
        operation: str,
        parsed: ConnectionType,
        account: str,
        message: str = "Interactive authorization is required",
        *,
        allow_browser_fallback: bool = False,
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
    ) -> ResultEnvelope:
        remediation = _remediation(failure.reason, parsed, account)
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


def oauth_provider_from_settings(
    settings: ConnectSettings,
    token_store: OAuthTokenStore,
    parsed: ConnectionType,
    account: str,
    *,
    transport: HttpxTransport | None = None,
) -> OAuthProvider:
    spec = target_spec(parsed)
    key = _token_key(parsed, account)
    http = transport or HttpxTransport()
    if parsed in {ConnectionType.OUTLOOK, ConnectionType.TEAMS}:
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
    if parsed in {ConnectionType.GMAIL, ConnectionType.GOOGLE_CALENDAR}:
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
) -> str:
    command = f"solercia-connect connect {parsed.value} {account}"
    return command + (" --allow-browser-fallback" if allow_browser_fallback else "")


def status_command(
    parsed: ConnectionType,
    account: str,
    *,
    allow_browser_fallback: bool = False,
) -> str:
    command = f"solercia-connect status {parsed.value} {account}"
    return command + (" --allow-browser-fallback" if allow_browser_fallback else "")


def _remediation(
    reason: ReasonCode,
    parsed: ConnectionType,
    account: str,
) -> tuple[str, ...]:
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
