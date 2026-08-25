from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from base64 import urlsafe_b64encode
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.secrets import SecretValue

_MAX_CALLBACK_TARGET_BYTES = 8_192
_MAX_CALLBACK_QUERY_BYTES = 4_096
_MAX_REQUEST_LINE_BYTES = 16_384
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True, slots=True)
class PKCEPair:
    state: str
    verifier: SecretValue
    challenge: str


@dataclass(frozen=True, slots=True)
class CallbackResult:
    code: SecretValue


def build_pkce_pair(
    *,
    state_factory: Callable[[], str] | None = None,
    verifier_factory: Callable[[], SecretValue] | None = None,
) -> PKCEPair:
    state = (state_factory or (lambda: secrets.token_urlsafe(32)))()
    verifier = (verifier_factory or (lambda: SecretValue(secrets.token_urlsafe(64))))()
    if len(state) < 8:
        raise ValueError("OAuth state must contain at least eight characters")
    raw_verifier = verifier.reveal()
    if not 43 <= len(raw_verifier) <= 128 and verifier_factory is None:
        raise ValueError("PKCE verifier length is outside the RFC 7636 bound")
    challenge = urlsafe_b64encode(hashlib.sha256(raw_verifier.encode()).digest()).rstrip(b"=")
    return PKCEPair(state, verifier, challenge.decode("ascii"))


def parse_callback_target(target: str, *, expected_state: str) -> CallbackResult:
    try:
        encoded_target = target.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _invalid_callback("OAuth callback target encoding is invalid") from exc
    if len(encoded_target) > _MAX_CALLBACK_TARGET_BYTES:
        raise _invalid_callback("OAuth callback target exceeds the fixed size limit")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.path != "/oauth/callback" or parsed.fragment:
        raise _invalid_callback("OAuth callback path is invalid")
    if len(parsed.query.encode("ascii")) > _MAX_CALLBACK_QUERY_BYTES:
        raise _invalid_callback("OAuth callback query exceeds the fixed size limit")
    values = _strict_query(parsed.query)
    state = values.get("state")
    if state is None or not hmac.compare_digest(state, expected_state):
        raise ConnectorFailure(
            ReasonCode.OAUTH_STATE_MISMATCH,
            "OAuth callback state correlation failed",
        )
    code = values.get("code")
    error = values.get("error")
    if code is not None and error is not None:
        raise _invalid_callback("OAuth callback contains conflicting result parameters")
    if error is not None:
        raise ConnectorFailure(
            ReasonCode.AUTHORIZATION_DENIED,
            "OAuth authorization was denied or cancelled",
        )
    if code is None:
        raise _invalid_callback("OAuth callback did not contain one authorization code")
    return CallbackResult(SecretValue(code))


def _strict_query(query: str) -> dict[str, str]:
    if not query:
        return {}
    values: dict[str, str] = {}
    for assignment in query.split("&"):
        if not assignment or "=" not in assignment:
            raise _invalid_callback("OAuth callback query is malformed")
        raw_name, raw_value = assignment.split("=", 1)
        name = _strict_unquote(raw_name)
        value = _strict_unquote(raw_value)
        if not name or not value:
            raise _invalid_callback("OAuth callback contains a missing parameter value")
        if name in values:
            raise _invalid_callback("OAuth callback contains duplicate parameters")
        values[name] = value
    return values


def _strict_unquote(value: str) -> str:
    for index, character in enumerate(value):
        if character == "%" and (
            index + 2 >= len(value)
            or value[index + 1] not in _HEX_DIGITS
            or value[index + 2] not in _HEX_DIGITS
        ):
            raise _invalid_callback("OAuth callback percent encoding is malformed")
    try:
        return unquote_to_bytes(value.replace("+", " ")).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _invalid_callback("OAuth callback text encoding is invalid") from exc


def _invalid_callback(message: str) -> ConnectorFailure:
    return ConnectorFailure(ReasonCode.OAUTH_CALLBACK_INVALID, message)


class LoopbackCallbackServer:
    """Single-use, bounded OAuth callback listener bound only to 127.0.0.1."""

    def __init__(
        self,
        expected_state: str,
        *,
        timeout_seconds: float = 180.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= timeout_seconds <= 600:
            raise ValueError("OAuth callback timeout must be between 1 and 600 seconds")
        self._expected_state = expected_state
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._result: CallbackResult | None = None
        self._failure: ConnectorFailure | None = None
        handler = self._handler_type()
        self._server = HTTPServer(("127.0.0.1", 0), handler)

    @property
    def redirect_uri(self) -> str:
        host, port = self._server.server_address[:2]
        if isinstance(host, bytes):
            host = host.decode("ascii")
        return f"http://{host}:{port}/oauth/callback"

    def __enter__(self) -> LoopbackCallbackServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def wait(self) -> CallbackResult:
        deadline = self._clock() + self._timeout_seconds
        while self._result is None and self._failure is None:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ConnectorFailure(
                    ReasonCode.OAUTH_CALLBACK_TIMEOUT,
                    "OAuth loopback callback did not arrive within the fixed timeout",
                )
            self._server.timeout = min(remaining, 1.0)
            self._server.handle_request()
        if self._failure is not None:
            raise self._failure
        if self._result is None:
            raise ConnectorFailure(
                ReasonCode.OAUTH_CALLBACK_INVALID,
                "OAuth callback completed without a result",
            )
        return self._result

    def close(self) -> None:
        self._server.server_close()

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def handle_one_request(self) -> None:
                try:
                    self.raw_requestline = self.rfile.readline(_MAX_REQUEST_LINE_BYTES + 1)
                    if len(self.raw_requestline) > _MAX_REQUEST_LINE_BYTES:
                        self.requestline = ""
                        self.request_version = ""
                        self.command = ""
                        owner._failure = _invalid_callback(
                            "OAuth callback request line exceeds the fixed size limit"
                        )
                        self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                        return
                    if not self.raw_requestline:
                        self.close_connection = True
                        return
                    if not self.parse_request():
                        owner._failure = _invalid_callback(
                            "OAuth callback HTTP request is malformed"
                        )
                        return
                    method = getattr(self, "do_" + self.command, None)
                    if method is None:
                        self._reject_method()
                        return
                    method()
                    self.wfile.flush()
                except TimeoutError:
                    owner._failure = _invalid_callback("OAuth callback HTTP request timed out")
                    self.close_connection = True

            def do_GET(self) -> None:
                status = 200
                body = b"Authorization received. You may close this window."
                try:
                    owner._result = parse_callback_target(
                        self.path,
                        expected_state=owner._expected_state,
                    )
                except ConnectorFailure as failure:
                    owner._failure = failure
                    status = 400
                    body = b"Authorization callback rejected. Return to the CLI for remediation."
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                self._reject_method()

            def do_PUT(self) -> None:
                self._reject_method()

            def do_PATCH(self) -> None:
                self._reject_method()

            def do_DELETE(self) -> None:
                self._reject_method()

            def do_HEAD(self) -> None:
                self._reject_method()

            def do_OPTIONS(self) -> None:
                self._reject_method()

            def _reject_method(self) -> None:
                owner._failure = _invalid_callback("OAuth callback HTTP method is invalid")
                body = b"Authorization callback rejected. Return to the CLI for remediation."
                self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
                self.send_header("Allow", "GET")
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                del format, args

        return CallbackHandler
