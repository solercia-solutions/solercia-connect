from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from time import sleep
from typing import Any, ClassVar, Protocol, cast
from urllib.parse import quote, urlencode, urlparse

from solercia_connect.domain import ReasonCode, utc_now
from solercia_connect.errors import (
    ConnectorFailure,
    TransportNetworkError,
    TransportTimeoutError,
)
from solercia_connect.ports import HTTPTransport, TransportResponse
from solercia_connect.secrets import SecretValue

_MICROSOFT_LOGIN_ORIGIN = "https://login.microsoftonline.com"
_GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_ATLASSIAN_AUTHORIZATION_ENDPOINT = "https://auth.atlassian.com/authorize"
_ATLASSIAN_TOKEN_ENDPOINT = "https://auth.atlassian.com/oauth/token"
# Read-only Jira scopes, read:me for the User Identity API, and offline_access for a refresh token.
ATLASSIAN_JIRA_SCOPES = ("read:jira-work", "read:jira-user", "read:me", "offline_access")
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_DEFAULT_MAX_DEVICE_POLL_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    access_token: SecretValue
    refresh_token: SecretValue | None
    expires_at: datetime
    scopes: tuple[str, ...]
    token_type: str = "Bearer"

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None:
            raise ValueError("OAuth token expiry must be timezone-aware")
        if self.token_type.casefold() != "bearer":
            raise ValueError("Only bearer OAuth tokens are supported")
        if not self.scopes or any(not scope.strip() for scope in self.scopes):
            raise ValueError("OAuth tokens require at least one non-empty scope")


# Google answers the OpenID short names with their userinfo URLs, so the stored grant says
# `https://www.googleapis.com/auth/userinfo.email` where the target asked for `email`.
_SCOPE_ALIASES = {
    "https://www.googleapis.com/auth/userinfo.email": "email",
    "https://www.googleapis.com/auth/userinfo.profile": "profile",
}


def grant_covers(required: Iterable[str], granted: Iterable[str]) -> bool:
    """Whether a stored grant holds every scope a target requires, reading provider aliases."""

    held = {_SCOPE_ALIASES.get(scope, scope) for scope in granted}
    return {_SCOPE_ALIASES.get(scope, scope) for scope in required}.issubset(held)


class OAuthTokenStore(Protocol):
    def load(self, key: str) -> OAuthTokenSet | None: ...

    def save(self, key: str, tokens: OAuthTokenSet) -> None: ...

    def delete(self, key: str) -> None: ...


class KeyringBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class KeyringOAuthTokenStore:
    """Store OAuth state through a ``KeyringBackend``.

    By default the backend is the OS keyring. Only in project mode (``SOLERCIA_CONNECT_HOME``) is
    a file backend injected, which keeps the state as private plaintext files under
    ``<home>/secrets``; see docs/decisions/2026-10-25-modo-proyecto-secretos-en-archivo.md.
    """

    def __init__(
        self, service_name: str = "solercia-connect", *, backend: KeyringBackend | None = None
    ):
        if not service_name.strip():
            raise ValueError("Keyring service name cannot be empty")
        self._service_name = service_name.strip()
        self._configured_backend = backend

    def load(self, key: str) -> OAuthTokenSet | None:
        account = _store_key(key)
        try:
            raw = self._backend().get_password(self._service_name, account)
        except Exception as exc:
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Secure OAuth token storage is unavailable",
            ) from exc
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Token payload root is not an object")
            access_token = _required_text(payload.get("access_token"), "access_token")
            raw_refresh = payload.get("refresh_token")
            refresh_token = (
                SecretValue(_required_text(raw_refresh, "refresh_token"))
                if raw_refresh is not None
                else None
            )
            expires_at = datetime.fromisoformat(
                _required_text(payload.get("expires_at"), "expires_at")
            )
            if expires_at.tzinfo is None:
                raise ValueError("Token expiry is not timezone-aware")
            raw_scopes = payload.get("scopes")
            if not isinstance(raw_scopes, list) or not all(
                isinstance(scope, str) and scope.strip() for scope in raw_scopes
            ):
                raise ValueError("Token scopes are malformed")
            token_type = _required_text(payload.get("token_type"), "token_type")
            return OAuthTokenSet(
                SecretValue(access_token),
                refresh_token,
                expires_at,
                tuple(raw_scopes),
                token_type,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Stored OAuth token state is malformed",
            ) from exc

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        account = _store_key(key)
        payload = json.dumps(
            {
                "access_token": tokens.access_token.reveal(),
                "refresh_token": (
                    tokens.refresh_token.reveal() if tokens.refresh_token is not None else None
                ),
                "expires_at": tokens.expires_at.astimezone(UTC).isoformat(),
                "scopes": list(tokens.scopes),
                "token_type": tokens.token_type,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            self._backend().set_password(self._service_name, account, payload)
        except Exception as exc:
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Secure OAuth token storage is unavailable",
            ) from exc

    def delete(self, key: str) -> None:
        account = _store_key(key)
        try:
            self._backend().delete_password(self._service_name, account)
        except Exception as exc:
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Secure OAuth token storage is unavailable",
            ) from exc

    def _backend(self) -> KeyringBackend:
        if self._configured_backend is not None:
            return self._configured_backend
        try:
            import keyring
        except ImportError as exc:
            raise ConnectorFailure(
                ReasonCode.CONFIGURATION_INVALID,
                "Install the oauth extra to use secure OAuth token storage",
            ) from exc
        return cast(KeyringBackend, keyring)


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    device_code: SecretValue
    user_code: str
    verification_uri: str
    expires_at: datetime
    interval_seconds: int


class _RefreshCapableOAuthProvider:
    # Token-endpoint errors meaning the organization withholds permission (decision D0.1). Empty
    # by default: Microsoft keeps its mapping, and its tenant policy never routes around.
    _organization_policy_errors: ClassVar[frozenset[str]] = frozenset()
    # Token-endpoint dialect. The defaults are the form-encoded RFC 6749 requests Microsoft and
    # Google use, with a refresh token kept when a refresh returns none and token_type required.
    _json_token_requests: ClassVar[bool] = False
    _rotating_refresh_tokens: ClassVar[bool] = False
    _implicit_token_type: ClassVar[str | None] = None

    def __init__(
        self,
        transport: HTTPTransport,
        *,
        client_id: str,
        scopes: tuple[str, ...],
        token_endpoint: str,
        store: OAuthTokenStore,
        store_key: str,
        client_secret: SecretValue | None = None,
        clock: Callable[[], datetime] = utc_now,
        refresh_skew_seconds: int = 60,
    ) -> None:
        if not client_id.strip():
            raise ValueError("OAuth client ID cannot be empty")
        if not scopes or any(not scope.strip() for scope in scopes):
            raise ValueError("OAuth provider requires at least one non-empty scope")
        if refresh_skew_seconds < 0:
            raise ValueError("OAuth refresh skew cannot be negative")
        self._transport = transport
        self._client_id = client_id.strip()
        self._scopes = tuple(dict.fromkeys(scope.strip() for scope in scopes))
        self._token_endpoint = token_endpoint
        self._store = store
        self._store_key = _store_key(store_key)
        self._client_secret = client_secret
        self._clock = clock
        self._refresh_skew = timedelta(seconds=refresh_skew_seconds)

    def authorization_header(self) -> str:
        tokens = self._store.load(self._store_key)
        if tokens is None:
            raise ConnectorFailure(
                ReasonCode.INVALID_CREDENTIALS,
                "OAuth authorization has not been completed for this connection",
            )
        if tokens.expires_at <= self._clock() + self._refresh_skew:
            tokens = self.refresh()
        return f"Bearer {tokens.access_token.reveal()}"

    def exchange_authorization_code(
        self,
        code: SecretValue,
        *,
        redirect_uri: str,
        code_verifier: SecretValue,
    ) -> OAuthTokenSet:
        redirect = _validated_redirect_uri(redirect_uri)
        form = self._client_form()
        form.update(
            {
                "grant_type": "authorization_code",
                "code": code.reveal(),
                "redirect_uri": redirect,
                "code_verifier": code_verifier.reveal(),
            }
        )
        return self._exchange(form, require_refresh_token=True)

    def refresh(self) -> OAuthTokenSet:
        current = self._store.load(self._store_key)
        if current is None or current.refresh_token is None:
            raise ConnectorFailure(
                ReasonCode.INVALID_CREDENTIALS,
                "OAuth refresh token is unavailable for this connection",
            )
        form = self._client_form()
        form.update(
            {
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token.reveal(),
            }
        )
        if self._rotating_refresh_tokens:
            # The used token is disabled once exchanged, so only a new one may replace it.
            return self._exchange(form, require_refresh_token=True)
        return self._exchange(form, preserved_refresh_token=current.refresh_token)

    def _client_form(self) -> dict[str, str]:
        form = {"client_id": self._client_id, "scope": " ".join(self._scopes)}
        if self._client_secret is not None:
            form["client_secret"] = self._client_secret.reveal()
        return form

    def _exchange(
        self,
        form: Mapping[str, str],
        *,
        preserved_refresh_token: SecretValue | None = None,
        require_refresh_token: bool = False,
    ) -> OAuthTokenSet:
        payload = _oauth_payload(
            self._transport,
            self._token_endpoint,
            form,
            organization_policy_errors=self._organization_policy_errors,
            as_json=self._json_token_requests,
        )
        tokens = self._tokens_from_payload(payload, preserved_refresh_token)
        if require_refresh_token and tokens.refresh_token is None:
            raise ConnectorFailure(
                ReasonCode.INVALID_CREDENTIALS,
                "OAuth grant did not return a refresh token",
            )
        self._store.save(self._store_key, tokens)
        return tokens

    def _tokens_from_payload(
        self,
        payload: Mapping[str, Any],
        preserved_refresh_token: SecretValue | None = None,
    ) -> OAuthTokenSet:
        try:
            access_token = SecretValue(_required_text(payload.get("access_token"), "access_token"))
            token_type = _required_text(
                payload.get("token_type", self._implicit_token_type), "token_type"
            )
            if token_type.casefold() != "bearer":
                raise ValueError("Unsupported OAuth token type")
            expires_in = _positive_int(payload.get("expires_in"), "expires_in")
            raw_refresh = payload.get("refresh_token")
            refresh_token = (
                SecretValue(_required_text(raw_refresh, "refresh_token"))
                if raw_refresh is not None
                else preserved_refresh_token
            )
            raw_scope = payload.get("scope")
            scopes = (
                tuple(dict.fromkeys(raw_scope.split()))
                if isinstance(raw_scope, str) and raw_scope.strip()
                else self._scopes
            )
            return OAuthTokenSet(
                access_token,
                refresh_token,
                self._clock() + timedelta(seconds=expires_in),
                scopes,
                "Bearer",
            )
        except (TypeError, ValueError) as exc:
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "OAuth token endpoint returned malformed token state",
            ) from exc


class MicrosoftOAuthProvider(_RefreshCapableOAuthProvider):
    """Microsoft delegated OAuth provider supporting PKCE and bounded device code."""

    def __init__(
        self,
        transport: HTTPTransport,
        *,
        client_id: str,
        tenant: str,
        scopes: tuple[str, ...],
        store: OAuthTokenStore,
        store_key: str,
        client_secret: SecretValue | None = None,
        clock: Callable[[], datetime] = utc_now,
        refresh_skew_seconds: int = 60,
        max_device_poll_interval_seconds: float = _DEFAULT_MAX_DEVICE_POLL_INTERVAL_SECONDS,
    ) -> None:
        normalized_tenant = _microsoft_tenant(tenant)
        self._max_device_poll_interval_seconds = _positive_finite_seconds(
            max_device_poll_interval_seconds,
            "Microsoft device poll interval maximum",
        )
        refresh_scopes = tuple(dict.fromkeys(("offline_access", *scopes)))
        self._authorization_endpoint = (
            f"{_MICROSOFT_LOGIN_ORIGIN}/{normalized_tenant}/oauth2/v2.0/authorize"
        )
        self._device_endpoint = (
            f"{_MICROSOFT_LOGIN_ORIGIN}/{normalized_tenant}/oauth2/v2.0/devicecode"
        )
        super().__init__(
            transport,
            client_id=client_id,
            scopes=refresh_scopes,
            token_endpoint=f"{_MICROSOFT_LOGIN_ORIGIN}/{normalized_tenant}/oauth2/v2.0/token",
            store=store,
            store_key=store_key,
            client_secret=client_secret,
            clock=clock,
            refresh_skew_seconds=refresh_skew_seconds,
        )

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
    ) -> str:
        return _authorization_url(
            self._authorization_endpoint,
            self._client_id,
            self._scopes,
            redirect_uri,
            state,
            code_challenge,
            extra={"response_mode": "query"},
        )

    def start_device_authorization(self) -> DeviceAuthorization:
        payload = _oauth_payload(
            self._transport,
            self._device_endpoint,
            {"client_id": self._client_id, "scope": " ".join(self._scopes)},
        )
        try:
            device_code = SecretValue(_required_text(payload.get("device_code"), "device_code"))
            user_code = _required_text(payload.get("user_code"), "user_code")
            verification_uri = _microsoft_verification_uri(
                _required_text(payload.get("verification_uri"), "verification_uri")
            )
            expires_in = _positive_int(payload.get("expires_in"), "expires_in")
            interval = _positive_int(payload.get("interval", 5), "interval")
        except (TypeError, ValueError) as exc:
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Microsoft device authorization response is malformed",
            ) from exc
        return DeviceAuthorization(
            device_code,
            user_code,
            verification_uri,
            self._clock() + timedelta(seconds=expires_in),
            interval,
        )

    def complete_device_authorization(
        self,
        authorization: DeviceAuthorization,
        *,
        max_attempts: int = 120,
        sleeper: Callable[[float], None] = sleep,
    ) -> OAuthTokenSet:
        if not 1 <= max_attempts <= 120:
            raise ValueError("Device authorization attempts must be between 1 and 120")
        try:
            provider_interval = _positive_int(authorization.interval_seconds, "interval")
        except ValueError as exc:
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Microsoft device authorization interval is malformed",
            ) from exc
        interval = (
            self._max_device_poll_interval_seconds
            if provider_interval > self._max_device_poll_interval_seconds
            else float(provider_interval)
        )
        for attempt in range(max_attempts):
            if self._clock() >= authorization.expires_at:
                raise ConnectorFailure(
                    ReasonCode.AUTHORIZATION_DENIED,
                    "Microsoft device authorization expired before completion",
                )
            form = self._client_form()
            form.update(
                {
                    "grant_type": _DEVICE_GRANT,
                    "device_code": authorization.device_code.reveal(),
                }
            )
            try:
                payload = _oauth_payload(
                    self._transport,
                    self._token_endpoint,
                    form,
                    allow_device_pending=True,
                )
            except _DeviceAuthorizationPending as pending:
                if attempt + 1 >= max_attempts:
                    break
                if pending.slow_down:
                    interval = min(
                        interval + 5.0,
                        self._max_device_poll_interval_seconds,
                    )
                remaining_lifetime = (authorization.expires_at - self._clock()).total_seconds()
                if not isfinite(remaining_lifetime) or remaining_lifetime <= 0:
                    raise ConnectorFailure(
                        ReasonCode.AUTHORIZATION_DENIED,
                        "Microsoft device authorization expired before completion",
                    ) from None
                sleeper(
                    min(
                        interval,
                        self._max_device_poll_interval_seconds,
                        remaining_lifetime,
                    )
                )
                continue
            tokens = self._tokens_from_payload(payload)
            if tokens.refresh_token is None:
                raise ConnectorFailure(
                    ReasonCode.INVALID_CREDENTIALS,
                    "Microsoft device grant did not return a refresh token",
                )
            self._store.save(self._store_key, tokens)
            return tokens
        raise ConnectorFailure(
            ReasonCode.NETWORK_TIMEOUT,
            "Microsoft device authorization did not complete within the fixed attempt bound",
        )


class GoogleOAuthProvider(_RefreshCapableOAuthProvider):
    """Google authorization-code provider with PKCE and refresh-token rotation."""

    # Google documents both as organization blocks: a Workspace admin policy restricts the
    # scopes, or the client belongs to a project limited to another Cloud organization.
    _organization_policy_errors = frozenset({"admin_policy_enforced", "org_internal"})

    def __init__(
        self,
        transport: HTTPTransport,
        *,
        client_id: str,
        scopes: tuple[str, ...],
        store: OAuthTokenStore,
        store_key: str,
        client_secret: SecretValue | None = None,
        clock: Callable[[], datetime] = utc_now,
        refresh_skew_seconds: int = 60,
    ) -> None:
        super().__init__(
            transport,
            client_id=client_id,
            scopes=scopes,
            token_endpoint=_GOOGLE_TOKEN_ENDPOINT,
            store=store,
            store_key=store_key,
            client_secret=client_secret,
            clock=clock,
            refresh_skew_seconds=refresh_skew_seconds,
        )

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
    ) -> str:
        return _authorization_url(
            _GOOGLE_AUTHORIZATION_ENDPOINT,
            self._client_id,
            self._scopes,
            redirect_uri,
            state,
            code_challenge,
            # No include_granted_scopes: every target's grant holds only its own read scopes, even
            # when the Desktop client is shared with tools that were granted more.
            extra={
                "access_type": "offline",
                "prompt": "consent",
            },
        )


class AtlassianOAuthProvider(_RefreshCapableOAuthProvider):
    """Atlassian OAuth 2.0 (3LO) with the user's own app: a confidential client without PKCE.

    Atlassian requires the client secret on the code exchange and on every refresh, and does not
    support PKCE (ECO-283), so ``state`` alone correlates the callback. Its documented token
    requests are JSON. Refresh tokens rotate: each refresh returns a new one and disables the one
    used, so the new one replaces it in a single store write.
    """

    _json_token_requests = True
    _rotating_refresh_tokens = True
    # The documented token responses carry no token_type; they are bearer tokens.
    _implicit_token_type = "Bearer"

    def __init__(
        self,
        transport: HTTPTransport,
        *,
        client_id: str,
        client_secret: SecretValue,
        store: OAuthTokenStore,
        store_key: str,
        scopes: tuple[str, ...] = ATLASSIAN_JIRA_SCOPES,
        clock: Callable[[], datetime] = utc_now,
        refresh_skew_seconds: int = 60,
    ) -> None:
        self._required_secret = client_secret
        super().__init__(
            transport,
            client_id=client_id,
            scopes=scopes,
            token_endpoint=_ATLASSIAN_TOKEN_ENDPOINT,
            store=store,
            store_key=store_key,
            client_secret=client_secret,
            clock=clock,
            refresh_skew_seconds=refresh_skew_seconds,
        )

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        redirect = _validated_redirect_uri(redirect_uri)
        if not state.strip():
            raise ValueError("OAuth state cannot be empty")
        query = {
            "audience": "api.atlassian.com",
            "client_id": self._client_id,
            "scope": " ".join(self._scopes),
            "redirect_uri": redirect,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        }
        return f"{_ATLASSIAN_AUTHORIZATION_ENDPOINT}?{urlencode(query, quote_via=quote)}"

    def exchange_authorization_code(
        self,
        code: SecretValue,
        *,
        redirect_uri: str,
        code_verifier: SecretValue | None = None,
    ) -> OAuthTokenSet:
        if code_verifier is not None:
            raise ValueError("Atlassian OAuth does not support PKCE; state correlates the callback")
        form = self._client_form()
        form.update(
            {
                "grant_type": "authorization_code",
                "code": code.reveal(),
                "redirect_uri": _validated_redirect_uri(redirect_uri),
            }
        )
        return self._exchange(form, require_refresh_token=True)

    def _client_form(self) -> dict[str, str]:
        # Atlassian's token requests name the client only; scopes belong to the authorization URL.
        return {"client_id": self._client_id, "client_secret": self._required_secret.reveal()}


@dataclass(frozen=True, slots=True)
class _DeviceAuthorizationPending(Exception):
    slow_down: bool


def _oauth_payload(
    transport: HTTPTransport,
    url: str,
    form: Mapping[str, str],
    *,
    allow_device_pending: bool = False,
    organization_policy_errors: frozenset[str] = frozenset(),
    as_json: bool = False,
) -> dict[str, Any]:
    try:
        if as_json:
            response = transport.request(
                "POST",
                url,
                headers={"Accept": "application/json"},
                json_body=form,
            )
        else:
            response = transport.request(
                "POST",
                url,
                headers={"Accept": "application/json"},
                form_body=form,
            )
    except TransportTimeoutError as exc:
        raise ConnectorFailure(ReasonCode.NETWORK_TIMEOUT, "OAuth endpoint timed out") from exc
    except TransportNetworkError as exc:
        raise ConnectorFailure(
            ReasonCode.NETWORK_UNAVAILABLE,
            "OAuth endpoint is unavailable",
        ) from exc
    payload = _decoded_object(response)
    if 200 <= response.status_code < 300:
        return payload
    raw_error = payload.get("error")
    error = raw_error if isinstance(raw_error, str) else ""
    if allow_device_pending and error in {"authorization_pending", "slow_down"}:
        raise _DeviceAuthorizationPending(error == "slow_down")
    reason = _oauth_error_reason(response.status_code, error, organization_policy_errors)
    raise ConnectorFailure(reason, "OAuth provider rejected the authorization request")


def _decoded_object(response: TransportResponse) -> dict[str, Any]:
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "OAuth endpoint returned malformed JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "OAuth endpoint returned an unexpected JSON root",
        )
    return cast(dict[str, Any], payload)


def _oauth_error_reason(
    status_code: int,
    error: str,
    organization_policy_errors: frozenset[str] = frozenset(),
) -> ReasonCode:
    if status_code == 429:
        return ReasonCode.RATE_LIMITED
    if 500 <= status_code <= 599:
        return ReasonCode.PROVIDER_UNAVAILABLE
    if error in organization_policy_errors:
        return ReasonCode.ADMIN_CONSENT_REQUIRED
    if error in {"invalid_client", "invalid_grant", "expired_token"}:
        return ReasonCode.INVALID_CREDENTIALS
    if error in {"consent_required", "interaction_required"}:
        return ReasonCode.ADMIN_CONSENT_REQUIRED
    if error in {"access_denied", "authorization_declined"}:
        return ReasonCode.AUTHORIZATION_DENIED
    return ReasonCode.REQUEST_REJECTED


def _authorization_url(
    endpoint: str,
    client_id: str,
    scopes: tuple[str, ...],
    redirect_uri: str,
    state: str,
    code_challenge: str,
    *,
    extra: Mapping[str, str],
) -> str:
    redirect = _validated_redirect_uri(redirect_uri)
    if not state.strip() or not code_challenge.strip():
        raise ValueError("OAuth state and PKCE code challenge cannot be empty")
    query = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        **extra,
    }
    return f"{endpoint}?{urlencode(query)}"


def _validated_redirect_uri(value: str) -> str:
    parsed = urlparse(value)
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        raise ValueError("OAuth redirect URI is invalid")
    if parsed.scheme == "https":
        return value
    if parsed.scheme == "http" and parsed.hostname.casefold() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        return value
    raise ValueError("OAuth redirect URI must use HTTPS or an HTTP loopback host")


def _microsoft_tenant(value: str) -> str:
    candidate = value.strip()
    if not candidate or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
        for character in candidate
    ):
        raise ValueError("Microsoft tenant must be a tenant ID, verified domain, or audience name")
    if candidate.startswith(".") or candidate.endswith(".") or ".." in candidate:
        raise ValueError("Microsoft tenant is malformed")
    return candidate


def _microsoft_verification_uri(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username
        or parsed.password
        or parsed.hostname.casefold() not in {"microsoft.com", "www.microsoft.com"}
    ):
        raise ValueError("Microsoft device verification URI is untrusted")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"OAuth field is missing: {label}")
    return value.strip()


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"OAuth field is not a positive integer: {label}")
    candidate = value.strip() if isinstance(value, str) else str(value)
    if not candidate.isdecimal():
        raise ValueError(f"OAuth field is not a positive integer: {label}")
    result = int(candidate)
    if result <= 0:
        raise ValueError(f"OAuth field is not a positive integer: {label}")
    return result


def _positive_finite_seconds(value: float, label: str) -> float:
    if isinstance(value, bool) or not isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return float(value)


def _store_key(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("OAuth token store key cannot be empty")
    return candidate
