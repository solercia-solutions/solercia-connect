"""Google returns OpenID scopes in their long form; a valid grant must still count as complete."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from solercia_connect.config import ConnectionProfile, ProfileStore, load_settings
from solercia_connect.connections import ConnectionService
from solercia_connect.oauth import GoogleOAuthProvider, OAuthTokenSet, grant_covers
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport

ACCOUNT = "user@example.com"
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
USERINFO_PROFILE = "https://www.googleapis.com/auth/userinfo.profile"


class MemoryTokenStore:
    def __init__(self, tokens: dict[str, OAuthTokenSet] | None = None) -> None:
        self.values = dict(tokens or {})

    def load(self, key: str) -> OAuthTokenSet | None:
        return self.values.get(key)

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        self.values[key] = tokens

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class FakeGoogleOAuth:
    def authorization_header(self) -> str:
        return "Bearer access"


def _tokens(*scopes: str) -> OAuthTokenSet:
    return OAuthTokenSet(
        access_token=SecretValue("access"),
        refresh_token=SecretValue("refresh"),
        expires_at=NOW + timedelta(hours=1),
        scopes=scopes,
    )


def test_the_long_google_userinfo_scopes_cover_their_openid_short_names() -> None:
    granted = ("openid", USERINFO_EMAIL, USERINFO_PROFILE, GMAIL_READONLY)

    assert grant_covers(("openid", "email", GMAIL_READONLY), granted)
    assert grant_covers(("openid", "profile"), granted)


def test_a_missing_provider_scope_is_never_covered_by_an_alias() -> None:
    assert not grant_covers(("openid", "email", GMAIL_READONLY), ("openid", USERINFO_EMAIL))
    assert not grant_covers(("email",), ("openid", GMAIL_READONLY))


def test_gmail_status_accepts_a_grant_that_names_email_in_its_long_form(tmp_path: Path) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "google-client",
        }
    )
    profiles = ProfileStore.from_settings(settings)
    profiles.save(ConnectionProfile.create("gmail", ACCOUNT, now=NOW))
    tokens = MemoryTokenStore(
        {f"gmail:{ACCOUNT}": _tokens("openid", USERINFO_EMAIL, GMAIL_READONLY)}
    )
    service = ConnectionService(
        settings,
        profiles,
        tokens,
        oauth_builder=lambda type, account: FakeGoogleOAuth(),
        identity_verifier=lambda type, account, auth: account,
        transport=FakeTransport(),
        clock=lambda: NOW,
    )

    result = service.status("gmail", ACCOUNT)

    assert result.status == "connected_official"


def test_google_authorization_never_requests_previously_granted_scopes() -> None:
    provider = GoogleOAuthProvider(
        FakeTransport(),
        client_id="google-client",
        scopes=("openid", "email", GMAIL_READONLY),
        store=MemoryTokenStore(),
        store_key=f"gmail:{ACCOUNT}",
    )

    url = provider.authorization_url(
        redirect_uri="http://127.0.0.1:8765/oauth/callback",
        state="state",
        code_challenge="challenge",
    )

    query = parse_qs(urlsplit(url).query)
    assert "include_granted_scopes" not in query
    assert query["scope"] == [f"openid email {GMAIL_READONLY}"]
