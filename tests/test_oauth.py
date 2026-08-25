from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from solercia_connect.auth import AuthorizationProvider
from solercia_connect.domain import ReasonCode
from solercia_connect.errors import (
    ConnectorFailure,
    TransportNetworkError,
    TransportTimeoutError,
)
from solercia_connect.oauth import (
    DeviceAuthorization,
    GoogleOAuthProvider,
    KeyringOAuthTokenStore,
    MicrosoftOAuthProvider,
    OAuthTokenSet,
)
from solercia_connect.ports import TransportResponse
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport, json_response


class FakeTokenStore:
    def __init__(self, tokens: OAuthTokenSet | None = None) -> None:
        self.tokens = tokens
        self.saved: list[tuple[str, OAuthTokenSet]] = []

    def load(self, key: str) -> OAuthTokenSet | None:
        del key
        return self.tokens

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        self.tokens = tokens
        self.saved.append((key, tokens))


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def test_microsoft_authorization_code_exchange_is_refresh_capable() -> None:
    transport = FakeTransport(
        json_response(
            200,
            {
                "access_token": "ms-access",
                "refresh_token": "ms-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "Mail.Read Calendars.Read",
            },
        ),
        json_response(
            200,
            {
                "access_token": "ms-refreshed",
                "token_type": "Bearer",
                "expires_in": 7200,
            },
        ),
    )
    store = FakeTokenStore()
    provider: AuthorizationProvider = MicrosoftOAuthProvider(
        transport,
        client_id="client-id",
        tenant="organizations",
        scopes=("Mail.Read", "Calendars.Read"),
        store=store,
        store_key="microsoft:user",
        clock=lambda: NOW,
    )

    assert isinstance(provider, MicrosoftOAuthProvider)
    provider.exchange_authorization_code(
        SecretValue("authorization-code"),
        redirect_uri="http://127.0.0.1:8765/callback",
        code_verifier=SecretValue("pkce-verifier"),
    )

    first_request = transport.requests[0]
    assert first_request.url.endswith("/organizations/oauth2/v2.0/token")
    assert first_request.form_body == {
        "client_id": "client-id",
        "scope": "offline_access Mail.Read Calendars.Read",
        "grant_type": "authorization_code",
        "code": "authorization-code",
        "redirect_uri": "http://127.0.0.1:8765/callback",
        "code_verifier": "pkce-verifier",
    }
    assert provider.authorization_header() == "Bearer ms-access"

    store.tokens = OAuthTokenSet(
        SecretValue("expired-access"),
        SecretValue("ms-refresh"),
        NOW - timedelta(seconds=1),
        ("Mail.Read", "Calendars.Read"),
    )

    assert provider.authorization_header() == "Bearer ms-refreshed"
    assert store.tokens is not None
    assert store.tokens.refresh_token is not None
    assert store.tokens.refresh_token.reveal() == "ms-refresh"
    assert transport.requests[1].form_body is not None
    assert transport.requests[1].form_body["grant_type"] == "refresh_token"


def test_microsoft_device_code_flow_is_bounded_and_offline_testable() -> None:
    transport = FakeTransport(
        json_response(
            200,
            {
                "device_code": "device-code",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900,
                "interval": 5,
            },
        ),
        json_response(400, {"error": "authorization_pending"}),
        json_response(
            200,
            {
                "access_token": "device-access",
                "refresh_token": "device-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        ),
    )
    store = FakeTokenStore()
    provider = MicrosoftOAuthProvider(
        transport,
        client_id="client-id",
        tenant="organizations",
        scopes=("Mail.Read",),
        store=store,
        store_key="microsoft:user",
        clock=lambda: NOW,
    )
    delays: list[float] = []

    authorization = provider.start_device_authorization()
    tokens = provider.complete_device_authorization(
        authorization,
        max_attempts=3,
        sleeper=delays.append,
    )

    assert authorization.user_code == "ABCD-EFGH"
    assert authorization.verification_uri == "https://microsoft.com/devicelogin"
    assert delays == [5.0]
    assert tokens.access_token.reveal() == "device-access"
    assert len(transport.requests) == 3
    assert transport.requests[1].form_body is not None
    assert transport.requests[1].form_body["grant_type"] == (
        "urn:ietf:params:oauth:grant-type:device_code"
    )


def test_device_code_polling_exhaustion_fails_without_unbounded_retry() -> None:
    transport = FakeTransport(
        json_response(
            200,
            {
                "device_code": "device-code",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900,
                "interval": 1,
            },
        ),
        json_response(400, {"error": "authorization_pending"}),
        json_response(400, {"error": "authorization_pending"}),
    )
    provider = MicrosoftOAuthProvider(
        transport,
        client_id="client-id",
        tenant="organizations",
        scopes=("Mail.Read",),
        store=FakeTokenStore(),
        store_key="microsoft:user",
        clock=lambda: NOW,
    )
    authorization = provider.start_device_authorization()

    with pytest.raises(ConnectorFailure) as captured:
        provider.complete_device_authorization(
            authorization,
            max_attempts=2,
            sleeper=lambda _: None,
        )

    assert captured.value.reason is ReasonCode.NETWORK_TIMEOUT
    assert len(transport.requests) == 3


def test_device_code_huge_provider_interval_is_capped_by_local_maximum() -> None:
    transport = FakeTransport(
        json_response(200, _device_authorization_payload(interval=1_000_000_000)),
        json_response(400, {"error": "authorization_pending"}),
        json_response(200, _device_token_payload()),
    )
    provider = MicrosoftOAuthProvider(
        transport,
        client_id="client-id",
        tenant="organizations",
        scopes=("Mail.Read",),
        store=FakeTokenStore(),
        store_key="microsoft:user",
        clock=lambda: NOW,
        max_device_poll_interval_seconds=7,
    )
    delays: list[float] = []

    authorization = provider.start_device_authorization()
    provider.complete_device_authorization(
        authorization,
        max_attempts=2,
        sleeper=delays.append,
    )

    assert delays == [7.0]


def test_device_code_slow_down_growth_remains_capped() -> None:
    transport = FakeTransport(
        json_response(200, _device_authorization_payload(interval=29)),
        json_response(400, {"error": "slow_down"}),
        json_response(200, _device_token_payload()),
    )
    provider = MicrosoftOAuthProvider(
        transport,
        client_id="client-id",
        tenant="organizations",
        scopes=("Mail.Read",),
        store=FakeTokenStore(),
        store_key="microsoft:user",
        clock=lambda: NOW,
        max_device_poll_interval_seconds=30,
    )
    delays: list[float] = []

    authorization = provider.start_device_authorization()
    provider.complete_device_authorization(
        authorization,
        max_attempts=2,
        sleeper=delays.append,
    )

    assert delays == [30.0]


def test_device_code_sleep_is_capped_by_remaining_lifetime() -> None:
    transport = FakeTransport(
        json_response(200, _device_authorization_payload(interval=10, expires_in=2)),
        json_response(400, {"error": "authorization_pending"}),
    )
    clock = MutableClock()
    provider = MicrosoftOAuthProvider(
        transport,
        client_id="client-id",
        tenant="organizations",
        scopes=("Mail.Read",),
        store=FakeTokenStore(),
        store_key="microsoft:user",
        clock=clock,
        max_device_poll_interval_seconds=30,
    )
    delays: list[float] = []

    def advance_clock(delay: float) -> None:
        delays.append(delay)
        clock.advance(delay)

    authorization = provider.start_device_authorization()

    with pytest.raises(ConnectorFailure) as captured:
        provider.complete_device_authorization(
            authorization,
            max_attempts=2,
            sleeper=advance_clock,
        )

    assert captured.value.reason is ReasonCode.AUTHORIZATION_DENIED
    assert delays == [2.0]
    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    "interval",
    [float("inf"), float("nan"), -1, 0, 1.5],
    ids=["infinite", "nan", "negative", "zero", "fractional"],
)
def test_device_code_rejects_malformed_provider_interval(interval: object) -> None:
    transport = FakeTransport(json_response(200, _device_authorization_payload(interval=interval)))
    provider = MicrosoftOAuthProvider(
        transport,
        client_id="client-id",
        tenant="organizations",
        scopes=("Mail.Read",),
        store=FakeTokenStore(),
        store_key="microsoft:user",
        clock=lambda: NOW,
    )

    with pytest.raises(ConnectorFailure) as captured:
        provider.start_device_authorization()

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


def test_google_authorization_code_uses_keyring_store_and_offline_access() -> None:
    transport = FakeTransport(
        json_response(
            200,
            {
                "access_token": "google-access",
                "refresh_token": "google-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid https://www.googleapis.com/auth/calendar.events.readonly",
            },
        )
    )
    backend = FakeKeyring()
    store = KeyringOAuthTokenStore("solercia-connect-tests", backend=backend)
    provider = GoogleOAuthProvider(
        transport,
        client_id="google-client-id",
        scopes=("https://www.googleapis.com/auth/calendar.events.readonly",),
        store=store,
        store_key="google:user",
        clock=lambda: NOW,
    )

    url = provider.authorization_url(
        redirect_uri="http://localhost:8765/callback",
        state="opaque-state",
        code_challenge="pkce-challenge",
    )
    query = parse_qs(urlparse(url).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]

    provider.exchange_authorization_code(
        SecretValue("google-code"),
        redirect_uri="http://localhost:8765/callback",
        code_verifier=SecretValue("google-verifier"),
    )

    restored = store.load("google:user")
    assert restored is not None
    assert restored.access_token.reveal() == "google-access"
    assert provider.authorization_header() == "Bearer google-access"
    assert "google-access" not in repr(restored)


def test_oauth_rejects_malformed_token_response_without_persisting() -> None:
    transport = FakeTransport(json_response(200, {"refresh_token": "must-not-store"}))
    store = FakeTokenStore()
    provider = GoogleOAuthProvider(
        transport,
        client_id="google-client-id",
        scopes=("openid",),
        store=store,
        store_key="google:user",
        clock=lambda: datetime(2026, 8, 23, tzinfo=UTC),
    )

    with pytest.raises(ConnectorFailure) as captured:
        provider.exchange_authorization_code(
            SecretValue("google-code"),
            redirect_uri="http://localhost:8765/callback",
            code_verifier=SecretValue("google-verifier"),
        )

    assert captured.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA
    assert store.saved == []


def test_keyring_store_round_trip_delete_and_malformed_state_fail_closed() -> None:
    backend = FakeKeyring()
    store = KeyringOAuthTokenStore("solercia-connect-tests", backend=backend)
    tokens = OAuthTokenSet(
        SecretValue("stored-access"),
        None,
        NOW + timedelta(hours=1),
        ("openid",),
    )

    assert store.load("missing") is None
    store.save("google:user", tokens)
    assert store.load("google:user") == tokens
    store.delete("google:user")
    assert store.load("google:user") is None

    backend.values[("solercia-connect-tests", "google:user")] = "not-json"
    with pytest.raises(ConnectorFailure) as captured:
        store.load("google:user")

    assert captured.value.reason is ReasonCode.CONFIGURATION_INVALID
    assert "not-json" not in str(captured.value)


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [
        (400, "invalid_grant", ReasonCode.INVALID_CREDENTIALS),
        (400, "consent_required", ReasonCode.ADMIN_CONSENT_REQUIRED),
        (400, "access_denied", ReasonCode.AUTHORIZATION_DENIED),
        (400, "unsupported_grant_type", ReasonCode.REQUEST_REJECTED),
        (429, "", ReasonCode.RATE_LIMITED),
        (503, "", ReasonCode.PROVIDER_UNAVAILABLE),
    ],
)
def test_oauth_maps_provider_failures_without_exposing_error_bodies(
    status: int,
    error: str,
    reason: ReasonCode,
) -> None:
    transport = FakeTransport(
        json_response(status, {"error": error, "error_description": "sensitive-provider-detail"})
    )
    provider = GoogleOAuthProvider(
        transport,
        client_id="google-client-id",
        scopes=("openid",),
        store=FakeTokenStore(),
        store_key="google:user",
        clock=lambda: NOW,
    )

    with pytest.raises(ConnectorFailure) as captured:
        provider.exchange_authorization_code(
            SecretValue("google-code"),
            redirect_uri="http://localhost:8765/callback",
            code_verifier=SecretValue("google-verifier"),
        )

    assert captured.value.reason is reason
    assert "sensitive-provider-detail" not in str(captured.value)


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (TransportTimeoutError("timeout detail"), ReasonCode.NETWORK_TIMEOUT),
        (TransportNetworkError("network detail"), ReasonCode.NETWORK_UNAVAILABLE),
        (TransportResponse(200, {}, b"not-json"), ReasonCode.MALFORMED_PROVIDER_DATA),
        (json_response(200, []), ReasonCode.MALFORMED_PROVIDER_DATA),
    ],
)
def test_oauth_transport_and_payload_failures_are_typed_and_safe(
    response: TransportResponse | Exception,
    reason: ReasonCode,
) -> None:
    provider = GoogleOAuthProvider(
        FakeTransport(response),
        client_id="google-client-id",
        scopes=("openid",),
        store=FakeTokenStore(),
        store_key="google:user",
        clock=lambda: NOW,
    )

    with pytest.raises(ConnectorFailure) as captured:
        provider.exchange_authorization_code(
            SecretValue("google-code"),
            redirect_uri="http://localhost:8765/callback",
            code_verifier=SecretValue("google-verifier"),
        )

    assert captured.value.reason is reason
    assert "detail" not in str(captured.value)


def test_oauth_requires_stored_refresh_state_and_trusted_configuration() -> None:
    empty_store = FakeTokenStore()
    provider = GoogleOAuthProvider(
        FakeTransport(),
        client_id="google-client-id",
        scopes=("openid",),
        store=empty_store,
        store_key="google:user",
        clock=lambda: NOW,
    )

    with pytest.raises(ConnectorFailure) as missing:
        provider.authorization_header()
    assert missing.value.reason is ReasonCode.INVALID_CREDENTIALS

    empty_store.tokens = OAuthTokenSet(
        SecretValue("expired-access"),
        None,
        NOW - timedelta(seconds=1),
        ("openid",),
    )
    with pytest.raises(ConnectorFailure) as unrefreshable:
        provider.authorization_header()
    assert unrefreshable.value.reason is ReasonCode.INVALID_CREDENTIALS

    with pytest.raises(ValueError, match="tenant"):
        MicrosoftOAuthProvider(
            FakeTransport(),
            client_id="client-id",
            tenant="../common",
            scopes=("Mail.Read",),
            store=empty_store,
            store_key="microsoft:user",
        )
    with pytest.raises(ValueError, match="HTTPS or an HTTP loopback"):
        provider.authorization_url(
            redirect_uri="http://attacker.example/callback",
            state="opaque-state",
            code_challenge="pkce-challenge",
        )


def test_microsoft_authorization_url_and_device_expiry_are_bounded() -> None:
    provider = MicrosoftOAuthProvider(
        FakeTransport(),
        client_id="client-id",
        tenant="organizations",
        scopes=("Mail.Read",),
        store=FakeTokenStore(),
        store_key="microsoft:user",
        clock=lambda: NOW,
    )
    url = provider.authorization_url(
        redirect_uri="https://connect.example/callback",
        state="opaque-state",
        code_challenge="pkce-challenge",
    )
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["offline_access Mail.Read"]
    assert query["response_mode"] == ["query"]

    expired = DeviceAuthorization(
        SecretValue("device-code"),
        "ABCD-EFGH",
        "https://microsoft.com/devicelogin",
        NOW - timedelta(seconds=1),
        5,
    )
    with pytest.raises(ConnectorFailure) as captured:
        provider.complete_device_authorization(expired, max_attempts=1)

    assert captured.value.reason is ReasonCode.AUTHORIZATION_DENIED


def _device_authorization_payload(
    *,
    interval: object,
    expires_in: int = 900,
) -> dict[str, object]:
    return {
        "device_code": "device-code",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://microsoft.com/devicelogin",
        "expires_in": expires_in,
        "interval": interval,
    }


def _device_token_payload() -> dict[str, object]:
    return {
        "access_token": "device-access",
        "refresh_token": "device-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
