from __future__ import annotations

import hashlib
import http.client
import socket
import threading
from base64 import urlsafe_b64encode
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest

from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth_session import (
    LoopbackCallbackServer,
    build_pkce_pair,
    parse_callback_target,
)
from solercia_connect.secrets import SecretValue


def test_pkce_pair_uses_s256_and_cryptographic_correlation_inputs() -> None:
    pair = build_pkce_pair(
        state_factory=lambda: "state-value",
        verifier_factory=lambda: SecretValue("verifier-value"),
    )
    expected = urlsafe_b64encode(hashlib.sha256(b"verifier-value").digest()).rstrip(b"=")

    assert pair.state == "state-value"
    assert pair.challenge == expected.decode("ascii")
    assert pair.verifier.reveal() == "verifier-value"


def test_callback_requires_exact_single_state_and_code() -> None:
    result = parse_callback_target(
        "/oauth/callback?state=expected&code=authorization-code",
        expected_state="expected",
    )

    assert result.code.reveal() == "authorization-code"

    for target, reason in (
        (
            "/oauth/callback?state=wrong&code=authorization-code",
            ReasonCode.OAUTH_STATE_MISMATCH,
        ),
        (
            "/oauth/callback?state=expected&state=expected&code=authorization-code",
            ReasonCode.OAUTH_CALLBACK_INVALID,
        ),
        (
            "/oauth/callback?state=expected&code=one&code=two",
            ReasonCode.OAUTH_CALLBACK_INVALID,
        ),
        (
            "/oauth/callback?state=expected&error=access_denied",
            ReasonCode.AUTHORIZATION_DENIED,
        ),
    ):
        with pytest.raises(ConnectorFailure) as captured:
            parse_callback_target(target, expected_state="expected")
        assert captured.value.reason is reason
        assert "authorization-code" not in captured.value.safe_message


@pytest.mark.parametrize(
    "target,reason",
    [
        ("/wrong?state=expected&code=code", ReasonCode.OAUTH_CALLBACK_INVALID),
        (
            "/oauth/callback?state=expected&code=code#fragment",
            ReasonCode.OAUTH_CALLBACK_INVALID,
        ),
        ("/oauth/callback?code=code", ReasonCode.OAUTH_STATE_MISMATCH),
        ("/oauth/callback?state=expected", ReasonCode.OAUTH_CALLBACK_INVALID),
        ("/oauth/callback?state=expected&code=", ReasonCode.OAUTH_CALLBACK_INVALID),
    ],
)
def test_callback_rejects_invalid_path_fragment_state_and_missing_code(
    target: str,
    reason: ReasonCode,
) -> None:
    with pytest.raises(ConnectorFailure) as captured:
        parse_callback_target(target, expected_state="expected")
    assert captured.value.reason is reason


@pytest.mark.parametrize(
    "target",
    [
        "/oauth/callback?state=expected&code=%ZZ",
        "/oauth/callback?state=expected&code=%FF",
        "/oauth/callback?state=&code=code",
        "/oauth/callback?state=expected&code=",
        "/oauth/callback?state=expected&error=",
        "/oauth/callback?state=expected&error=one&error=two",
        "/oauth/callback?state=expected&code=code&error=access_denied",
    ],
)
def test_callback_strictly_rejects_malformed_encoding_values_duplicates_and_mixed_results(
    target: str,
) -> None:
    with pytest.raises(ConnectorFailure) as captured:
        parse_callback_target(target, expected_state="expected")

    assert captured.value.reason in {
        ReasonCode.OAUTH_CALLBACK_INVALID,
        ReasonCode.OAUTH_STATE_MISMATCH,
    }
    for secret_fragment in ("access_denied", "%zz", "%ff"):
        assert secret_fragment not in captured.value.safe_message.casefold()


def test_callback_rejects_oversized_target_and_query() -> None:
    oversized_value = "x" * 20_000

    for target in (
        f"/oauth/callback?state=expected&code={oversized_value}",
        f"/oauth/callback?state=expected&extra={oversized_value}&code=code",
    ):
        with pytest.raises(ConnectorFailure) as captured:
            parse_callback_target(target, expected_state="expected")
        assert captured.value.reason is ReasonCode.OAUTH_CALLBACK_INVALID


def test_loopback_server_rejects_unexpected_http_method_without_waiting_for_timeout() -> None:
    statuses: list[int] = []
    with LoopbackCallbackServer("expected-state", timeout_seconds=2) as callback:
        parsed = urlsplit(callback.redirect_uri)

        def send() -> None:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            connection.request(
                "POST",
                "/oauth/callback?state=expected-state&code=must-not-leak",
            )
            statuses.append(connection.getresponse().status)
            connection.close()

        worker = threading.Thread(target=send)
        worker.start()
        with pytest.raises(ConnectorFailure) as captured:
            callback.wait()
        worker.join(timeout=2)

    assert statuses == [405]
    assert captured.value.reason is ReasonCode.OAUTH_CALLBACK_INVALID
    assert "must-not-leak" not in captured.value.safe_message


def test_loopback_server_rejects_oversized_request_line_without_timeout() -> None:
    with LoopbackCallbackServer("expected-state", timeout_seconds=2) as callback:
        parsed = urlsplit(callback.redirect_uri)

        def send() -> None:
            with socket.create_connection(
                (str(parsed.hostname), int(parsed.port or 0)), timeout=2
            ) as sock:
                target = b"/oauth/callback?state=expected-state&code=" + (b"x" * 70_000)
                sock.sendall(b"GET " + target + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                sock.recv(1024)

        worker = threading.Thread(target=send)
        worker.start()
        with pytest.raises(ConnectorFailure) as captured:
            callback.wait()
        worker.join(timeout=2)

    assert captured.value.reason is ReasonCode.OAUTH_CALLBACK_INVALID


def test_pkce_rejects_short_state_and_default_verifier_is_rfc_bounded() -> None:
    with pytest.raises(ValueError, match="state"):
        build_pkce_pair(state_factory=lambda: "short")

    pair = build_pkce_pair()
    assert len(pair.state) >= 8
    assert 43 <= len(pair.verifier.reveal()) <= 128


def test_loopback_server_accepts_one_exact_callback_on_ipv4() -> None:
    with LoopbackCallbackServer("expected-state", timeout_seconds=2) as callback:
        assert callback.redirect_uri.startswith("http://127.0.0.1:")

        def send() -> None:
            with urlopen(
                callback.redirect_uri + "?state=expected-state&code=one-time-code",
                timeout=2,
            ) as response:
                assert response.status == 200
                assert response.headers["Cache-Control"] == "no-store"

        worker = threading.Thread(target=send)
        worker.start()
        result = callback.wait()
        worker.join(timeout=2)

    assert worker.is_alive() is False
    assert result.code.reveal() == "one-time-code"


def test_loopback_server_surfaces_callback_rejection_without_provider_body() -> None:
    responses: list[int] = []
    with LoopbackCallbackServer("expected-state", timeout_seconds=2) as callback:

        def send() -> None:
            try:
                urlopen(
                    callback.redirect_uri + "?state=wrong&code=discarded",
                    timeout=2,
                )
            except HTTPError as failure:
                responses.append(failure.code)

        worker = threading.Thread(target=send)
        worker.start()
        with pytest.raises(ConnectorFailure) as captured:
            callback.wait()
        worker.join(timeout=2)

    assert responses == [400]
    assert captured.value.reason is ReasonCode.OAUTH_STATE_MISMATCH
    assert "discarded" not in captured.value.safe_message


def test_loopback_server_has_a_finite_timeout_and_validates_timeout_bound() -> None:
    with pytest.raises(ValueError, match="between 1 and 600"):
        LoopbackCallbackServer("state-value", timeout_seconds=0)

    ticks = iter((10.0, 12.0))
    with (
        LoopbackCallbackServer(
            "state-value",
            timeout_seconds=1,
            clock=lambda: next(ticks),
        ) as callback,
        pytest.raises(ConnectorFailure) as captured,
    ):
        callback.wait()

    assert captured.value.reason is ReasonCode.OAUTH_CALLBACK_TIMEOUT
