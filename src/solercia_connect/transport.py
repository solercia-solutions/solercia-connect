from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import httpx

from solercia_connect.domain import ReasonCode
from solercia_connect.errors import ConnectorFailure, TransportNetworkError, TransportTimeoutError
from solercia_connect.ports import TransportResponse

type JsonPayload = dict[str, Any] | list[Any]


class HttpxTransport:
    """Thin sync httpx adapter; callers own authentication and normalization."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(follow_redirects=False)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> TransportResponse:
        if json_body is not None and form_body is not None:
            raise ValueError("HTTP request cannot contain both JSON and form bodies")
        try:
            response = self._client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=form_body,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise TransportTimeoutError("Provider request timed out") from exc
        except httpx.NetworkError as exc:
            raise TransportNetworkError("Provider network request failed") from exc
        return TransportResponse(response.status_code, dict(response.headers), response.content)


def request_json(
    transport: Any,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Mapping[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> JsonPayload:
    try:
        response = cast(
            TransportResponse,
            transport.request(
                method,
                url,
                headers=headers,
                params=params,
                json_body=json_body,
                timeout_seconds=timeout_seconds,
            ),
        )
    except TransportTimeoutError as exc:
        raise ConnectorFailure(ReasonCode.NETWORK_TIMEOUT, "Provider request timed out") from exc
    except TransportNetworkError as exc:
        raise ConnectorFailure(
            ReasonCode.NETWORK_UNAVAILABLE, "Provider network is unavailable"
        ) from exc

    _raise_for_status(response.status_code)
    if response.status_code == 204 or not response.content:
        return {}
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Provider returned a non-JSON or malformed response",
        ) from exc
    if not isinstance(payload, (dict, list)):
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Provider returned an unexpected JSON root",
        )
    return cast(JsonPayload, payload)


def request_bytes(
    transport: Any,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> TransportResponse:
    try:
        response = cast(
            TransportResponse,
            transport.request(
                method,
                url,
                headers=headers,
                params=params,
                timeout_seconds=timeout_seconds,
            ),
        )
    except TransportTimeoutError as exc:
        raise ConnectorFailure(ReasonCode.NETWORK_TIMEOUT, "Provider request timed out") from exc
    except TransportNetworkError as exc:
        raise ConnectorFailure(
            ReasonCode.NETWORK_UNAVAILABLE, "Provider network is unavailable"
        ) from exc
    _raise_for_status(response.status_code)
    return response


def _raise_for_status(status_code: int) -> None:
    if 200 <= status_code < 300:
        return
    if status_code == 401:
        raise ConnectorFailure(ReasonCode.INVALID_CREDENTIALS, "Provider rejected credentials")
    if status_code == 403:
        raise ConnectorFailure(ReasonCode.AUTHORIZATION_DENIED, "Provider denied authorization")
    if status_code in {408, 504}:
        raise ConnectorFailure(ReasonCode.NETWORK_TIMEOUT, "Provider request timed out")
    if status_code == 429:
        raise ConnectorFailure(ReasonCode.RATE_LIMITED, "Provider rate limit reached")
    if 500 <= status_code <= 599:
        raise ConnectorFailure(ReasonCode.PROVIDER_UNAVAILABLE, "Provider service unavailable")
    if 400 <= status_code <= 499:
        raise ConnectorFailure(ReasonCode.REQUEST_REJECTED, "Provider rejected the request")
    raise ConnectorFailure(ReasonCode.UNKNOWN_ERROR, "Provider returned an unknown status")
