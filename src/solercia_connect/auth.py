from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol

from solercia_connect.secrets import SecretValue


class AuthorizationProvider(Protocol):
    def authorization_header(self) -> str: ...


@dataclass(frozen=True, slots=True)
class BearerTokenProvider:
    token: SecretValue

    def authorization_header(self) -> str:
        return f"Bearer {self.token.reveal()}"


@dataclass(frozen=True, slots=True)
class BasicAuthProvider:
    username: str
    password: SecretValue

    def authorization_header(self) -> str:
        raw = f"{self.username}:{self.password.reveal()}".encode()
        return f"Basic {base64.b64encode(raw).decode()}"


@dataclass(frozen=True, slots=True)
class GlpiCredentials:
    app_token: SecretValue
    user_token: SecretValue

    def init_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "App-Token": self.app_token.reveal(),
            "Authorization": f"user_token {self.user_token.reveal()}",
        }

    def session_headers(self, session_token: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "App-Token": self.app_token.reveal(),
            "Session-Token": session_token,
        }
