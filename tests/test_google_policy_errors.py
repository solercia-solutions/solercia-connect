"""Google organizational-policy failures are classified per decision D0.1.

Only Google's documented organization blocks become ``ADMIN_CONSENT_REQUIRED``: the token
errors ``admin_policy_enforced`` and ``org_internal``, and a 403 whose structured reason is
``domainPolicy``. Every other failure keeps its fail-closed reason, and Microsoft is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from solercia_connect.auth import BearerTokenProvider
from solercia_connect.domain import (
    CalendarQuery,
    DateWindow,
    MailQuery,
    ReasonCode,
    TranscriptQuery,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import GoogleOAuthProvider, MicrosoftOAuthProvider, OAuthTokenSet
from solercia_connect.ports import TransportResponse
from solercia_connect.providers.google import GoogleCalendarAdapter, GoogleGmailAdapter
from solercia_connect.providers.google_meet import GoogleMeetAdapter
from solercia_connect.secrets import SecretValue
from solercia_connect.transport import request_json
from tests.fakes import NOW, FakeTransport, json_response

ACCOUNT = "user@example.com"
SENSITIVE = "sensitive-provider-detail"
WINDOW = DateWindow(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))


class _TokenStore:
    def __init__(self) -> None:
        self.tokens: OAuthTokenSet | None = OAuthTokenSet(
            SecretValue("old-access"), SecretValue("old-refresh"), NOW, ("openid",)
        )
        self.saved: list[OAuthTokenSet] = []

    def load(self, key: str) -> OAuthTokenSet | None:
        del key
        return self.tokens

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        del key
        self.saved.append(tokens)

    def delete(self, key: str) -> None:
        del key


def _google_oauth(transport: FakeTransport, store: _TokenStore) -> GoogleOAuthProvider:
    return GoogleOAuthProvider(
        transport,
        client_id="google-client-id",
        scopes=("openid",),
        store=store,
        store_key="google:user",
        clock=lambda: NOW,
    )


def _microsoft_oauth(transport: FakeTransport, store: _TokenStore) -> MicrosoftOAuthProvider:
    return MicrosoftOAuthProvider(
        transport,
        client_id="client-id",
        tenant="organizations",
        scopes=("Mail.Read",),
        store=store,
        store_key="microsoft:user",
        clock=lambda: NOW,
    )


def _token_failure(
    build: Callable[[FakeTransport, _TokenStore], GoogleOAuthProvider | MicrosoftOAuthProvider],
    error: str,
    *,
    refresh: bool,
) -> tuple[ConnectorFailure, _TokenStore]:
    transport = FakeTransport(json_response(400, {"error": error, "error_description": SENSITIVE}))
    store = _TokenStore()
    provider = build(transport, store)
    with pytest.raises(ConnectorFailure) as captured:
        if refresh:
            provider.refresh()
        else:
            provider.exchange_authorization_code(
                SecretValue("auth-code"),
                redirect_uri="http://localhost:8765/callback",
                code_verifier=SecretValue("verifier"),
            )
    return captured.value, store


@pytest.mark.parametrize("refresh", [False, True], ids=["exchange", "refresh"])
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("admin_policy_enforced", ReasonCode.ADMIN_CONSENT_REQUIRED),
        ("org_internal", ReasonCode.ADMIN_CONSENT_REQUIRED),
        ("invalid_grant", ReasonCode.INVALID_CREDENTIALS),
        ("access_denied", ReasonCode.AUTHORIZATION_DENIED),
    ],
)
def test_google_token_errors_for_organization_policy_are_admin_consent_required(
    error: str, reason: ReasonCode, refresh: bool
) -> None:
    failure, store = _token_failure(_google_oauth, error, refresh=refresh)

    assert failure.reason is reason
    assert SENSITIVE not in str(failure) and SENSITIVE not in failure.safe_message
    assert store.saved == []


@pytest.mark.parametrize("refresh", [False, True], ids=["exchange", "refresh"])
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("admin_policy_enforced", ReasonCode.REQUEST_REJECTED),
        ("org_internal", ReasonCode.REQUEST_REJECTED),
        ("invalid_grant", ReasonCode.INVALID_CREDENTIALS),
        ("consent_required", ReasonCode.ADMIN_CONSENT_REQUIRED),
        ("access_denied", ReasonCode.AUTHORIZATION_DENIED),
    ],
)
def test_microsoft_token_errors_keep_their_previous_mapping(
    error: str, reason: ReasonCode, refresh: bool
) -> None:
    failure, _ = _token_failure(_microsoft_oauth, error, refresh=refresh)

    assert failure.reason is reason


def _google_error(status: int, *reasons: str) -> TransportResponse:
    errors = [{"domain": "global", "reason": reason, "message": SENSITIVE} for reason in reasons]
    return json_response(
        status, {"error": {"errors": errors, "code": status, "message": SENSITIVE}}
    )


def _auth() -> BearerTokenProvider:
    return BearerTokenProvider(SecretValue("google-secret"))


def _read_gmail(response: TransportResponse) -> Any:
    adapter = GoogleGmailAdapter(
        FakeTransport(response), _auth(), expected_account=ACCOUNT, clock=lambda: NOW
    )
    return adapter.list_sent_mail(MailQuery(WINDOW, ACCOUNT))


def _read_calendar(response: TransportResponse) -> Any:
    adapter = GoogleCalendarAdapter(
        FakeTransport(response), _auth(), expected_account=ACCOUNT, clock=lambda: NOW
    )
    return adapter.list_events(CalendarQuery(WINDOW, account_id=ACCOUNT))


def _meet(*responses: TransportResponse) -> GoogleMeetAdapter:
    return GoogleMeetAdapter(
        FakeTransport(*responses), _auth(), expected_account=ACCOUNT, clock=lambda: NOW
    )


def _read_meet_calendar(response: TransportResponse) -> Any:
    return _meet(response).list_transcripts(TranscriptQuery(meeting_id="event"))


def _read_meet_drive(response: TransportResponse) -> Any:
    event = {
        "id": "event",
        "start": {"dateTime": "2026-08-10T10:00:00Z"},
        "attachments": [
            {
                "fileId": "file",
                "title": "Transcript",
                "mimeType": "application/vnd.google-apps.document",
            }
        ],
    }
    adapter = _meet(json_response(200, {"id": ACCOUNT}), json_response(200, event), response)
    return adapter.list_transcripts(TranscriptQuery(meeting_id="event"))


READERS: dict[str, Callable[[TransportResponse], Any]] = {
    "gmail": _read_gmail,
    "calendar": _read_calendar,
    "meet-calendar": _read_meet_calendar,
    "meet-drive-export": _read_meet_drive,
}


def _failure(reader: str, response: TransportResponse) -> ConnectorFailure:
    with pytest.raises(ConnectorFailure) as captured:
        READERS[reader](response)
    return captured.value


@pytest.mark.parametrize("reader", READERS)
def test_documented_domain_policy_403_is_admin_consent_required(reader: str) -> None:
    failure = _failure(reader, _google_error(403, "domainPolicy"))

    assert failure.reason is ReasonCode.ADMIN_CONSENT_REQUIRED
    assert SENSITIVE not in str(failure) and SENSITIVE not in failure.safe_message


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize(
    "response",
    [
        _google_error(403, "insufficientPermissions"),
        _google_error(403, "forbiddenForNonOrganizer"),
        _google_error(403, "rateLimitExceeded"),
        _google_error(403, "domainPolicy", "insufficientPermissions"),
        _google_error(403),
        json_response(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}}),
        json_response(403, {"error": "domainPolicy"}),
        json_response(403, {"error": {"errors": "domainPolicy"}}),
        json_response(403, {"error": {"errors": [{"reason": ["domainPolicy"]}]}}),
        json_response(403, ["domainPolicy"]),
        TransportResponse(403, {}, b"raw domainPolicy body bearer google-secret"),
        TransportResponse(403, {}, b"\xff\xfe"),
        TransportResponse(403, {}, _google_error(403, "domainPolicy").content + b" " * (64 * 1024)),
    ],
    ids=[
        "insufficient-permissions",
        "non-organizer",
        "rate-limit",
        "mixed-reasons",
        "no-reasons",
        "status-only",
        "error-string",
        "errors-string",
        "reason-list",
        "list-root",
        "not-json",
        "not-utf8",
        "oversized",
    ],
)
def test_other_403_reasons_and_unparseable_bodies_stay_authorization_denied(
    reader: str, response: TransportResponse
) -> None:
    failure = _failure(reader, response)

    assert failure.reason is ReasonCode.AUTHORIZATION_DENIED
    assert SENSITIVE not in str(failure) and "raw domainPolicy body" not in str(failure)
    assert "google-secret" not in str(failure)


def test_domain_policy_outside_a_403_keeps_the_status_mapping() -> None:
    failure = _failure("gmail", _google_error(400, "domainPolicy"))

    assert failure.reason is ReasonCode.REQUEST_REJECTED


def test_the_shared_transport_still_maps_every_403_to_authorization_denied() -> None:
    """Microsoft adapters call the shared transport directly, so their 403 mapping is unchanged."""

    with pytest.raises(ConnectorFailure) as captured:
        request_json(
            FakeTransport(_google_error(403, "domainPolicy")),
            "GET",
            "https://graph.microsoft.com/v1.0/me",
        )

    assert captured.value.reason is ReasonCode.AUTHORIZATION_DENIED
