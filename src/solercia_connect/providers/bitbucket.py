"""Bitbucket Cloud, read-only and official-only: docs/decisions/2026-10-26-conector-bitbucket.md.

An Atlassian API token created with Bitbucket scopes authenticates by Basic ``email:token``. Every
read first proves the identity in three steps (decision B-D2): the account ID of ``/user``, the
confirmed account email in ``/user/emails``, and access to the workspace that is read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse

from solercia_connect.auth import AuthorizationProvider
from solercia_connect.domain import (
    ConnectorRequest,
    DomainRecord,
    HealthReport,
    HealthState,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    normalize_bitbucket_workspace,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import HTTPTransport, ProviderDescriptor
from solercia_connect.providers._shared import (
    require_list,
    require_mapping,
    require_same_origin_https_url,
    require_string,
)
from solercia_connect.transport import FailureRefiner, JsonPayload, request_json

BITBUCKET_API_BASE_URL = "https://api.bitbucket.org/2.0"
# The four read scopes of decision B-D1. API token scopes never imply one another.
BITBUCKET_READ_SCOPES = (
    "read:user:bitbucket",
    "read:workspace:bitbucket",
    "read:repository:bitbucket",
    "read:pullrequest:bitbucket",
)
BITBUCKET_TOKEN_GUIDANCE = (
    "Create an API token with scopes for the Bitbucket app at "
    "https://id.atlassian.com/manage-profile/security/api-tokens, with "
    + ", ".join(BITBUCKET_READ_SCOPES[:-1])
    + f" and {BITBUCKET_READ_SCOPES[-1]}; a classic Jira API token does not work."
)
_REJECTED_CREDENTIALS = (
    "Bitbucket rejected the credentials: it needs the account email and an API token with scopes "
    "for the Bitbucket app; a classic Jira API token does not work"
)
# Bounds the email pages read to prove one address; an account has a handful of them.
_MAX_EMAIL_PAGES = 5
# Only names shaped like a Bitbucket scope are ever quoted back from a 403 body.
_SCOPE_NAME = re.compile(r"[a-z]+:[a-z]+:bitbucket")
_MAX_QUOTED_SCOPES = 8


@dataclass(frozen=True, slots=True)
class BitbucketIdentity:
    """The account ID proven by ``/user`` and the workspace it was shown to reach."""

    account_id: str
    workspace: str


class BitbucketAdapter:
    descriptor = ProviderDescriptor(
        "bitbucket.cloud",
        ProviderFamily.BITBUCKET,
        RouteKind.OFFICIAL,
        frozenset(),
    )

    def __init__(
        self,
        transport: HTTPTransport,
        auth: AuthorizationProvider,
        *,
        account: str,
        workspace: str,
        expected_account_id: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._transport = transport
        self._auth = auth
        self._account = account.strip().casefold()
        self._workspace = normalize_bitbucket_workspace(workspace)
        self._expected_account_id = expected_account_id
        self._clock = clock

    def health(self) -> HealthReport:
        identity = self.verify_identity()
        return HealthReport(
            self.descriptor.provider_id,
            HealthState.READY,
            ReasonCode.SUCCESS,
            tuple(self.descriptor.capabilities),
            identity.account_id,
            "Bitbucket identity and workspace are reachable",
        )

    def execute(self, request: ConnectorRequest) -> tuple[DomainRecord, ...]:
        del request
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "Bitbucket request does not match a source-control capability contract",
        )

    def verify_identity(self, workspace: str | None = None) -> BitbucketIdentity:
        """Prove the account in three steps before any data read (decision B-D2)."""

        target = self._workspace if workspace is None else normalize_bitbucket_workspace(workspace)
        user = require_mapping(self._get(f"{BITBUCKET_API_BASE_URL}/user"), "bitbucket.user")
        account_id = require_string(user.get("account_id"), "bitbucket.user.account_id")
        if not account_id.strip():
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Provider field is not a non-empty string: bitbucket.user.account_id",
            )
        if self._expected_account_id is not None and account_id != self._expected_account_id:
            raise _boundary("Authenticated Bitbucket account does not match the connection profile")
        self._prove_confirmed_email()
        self._get(
            f"{BITBUCKET_API_BASE_URL}/workspaces/{target}",
            not_found=ConnectorFailure(
                ReasonCode.WORKSPACE_NOT_FOUND,
                f"Bitbucket workspace {target} was not found or is not visible to this account",
            ),
        )
        return BitbucketIdentity(account_id, target)

    def _prove_confirmed_email(self) -> None:
        collection = f"{BITBUCKET_API_BASE_URL}/user/emails"
        url: str = collection
        params: dict[str, Any] | None = {"pagelen": 100}
        for _ in range(_MAX_EMAIL_PAGES):
            data = require_mapping(self._get(url, params=params), "bitbucket.user.emails")
            for raw in require_list(data.get("values"), "bitbucket.user.emails.values"):
                entry = require_mapping(raw, "bitbucket.user.emails.value")
                email = require_string(entry.get("email"), "bitbucket.user.emails.value.email")
                if email.strip().casefold() != self._account:
                    continue
                confirmed = entry.get("is_confirmed")
                if not isinstance(confirmed, bool):
                    raise ConnectorFailure(
                        ReasonCode.MALFORMED_PROVIDER_DATA,
                        "Provider field is not a boolean: bitbucket.user.emails.value.is_confirmed",
                    )
                if not confirmed:
                    raise _boundary("The account email is not confirmed in Bitbucket")
                return
            next_url = next_page_url(data, collection)
            if next_url is None:
                raise _boundary("The Bitbucket account does not list the requested email")
            url, params = next_url, None
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Bitbucket listed more email pages than the fixed bound",
        )

    def _get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        not_found: ConnectorFailure | None = None,
    ) -> JsonPayload:
        return request_json(
            self._transport,
            "GET",
            url,
            headers={
                "Accept": "application/json",
                "Authorization": self._auth.authorization_header(),
            },
            params=params,
            failure_refiner=_refiner(not_found),
        )


def next_page_url(data: Mapping[str, Any], collection: str) -> str | None:
    """Return the ``next`` link only when it stays on the same origin and collection path.

    The link is opaque and carries its own query, so it is followed as is; a link to any other
    origin or resource would send the token elsewhere, so it fails closed instead.
    """

    raw = data.get("next")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Provider pagination link is not a non-empty string: bitbucket.next",
        )
    require_same_origin_https_url(raw, collection, "bitbucket.next")
    if _collection_path(raw) != _collection_path(collection):
        raise _boundary("Bitbucket pagination left the requested collection")
    return raw


def _collection_path(url: str) -> str:
    return unquote(urlparse(url).path).rstrip("/")


def _refiner(not_found: ConnectorFailure | None) -> FailureRefiner:
    def refine(status_code: int, body: bytes) -> ConnectorFailure | None:
        if status_code == 401:
            return ConnectorFailure(ReasonCode.INVALID_CREDENTIALS, _REJECTED_CREDENTIALS)
        if status_code == 403:
            return _scope_denied(body)
        if status_code == 404 and not_found is not None:
            return not_found
        return None

    return refine


def _scope_denied(body: bytes) -> ConnectorFailure:
    scopes = _required_scopes(body)
    if scopes:
        return ConnectorFailure(
            ReasonCode.AUTHORIZATION_DENIED,
            "Bitbucket denied the request; the API token lacks the required scopes: "
            + ", ".join(scopes),
        )
    return ConnectorFailure(
        ReasonCode.AUTHORIZATION_DENIED,
        "Bitbucket denied the request; the API token may lack a Bitbucket read scope, or the "
        "account may lack access",
    )


def _required_scopes(body: bytes) -> tuple[str, ...]:
    """Read the missing scope names a 403 may carry; the shape is not documented, so be wary."""

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ()
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ()
    found: set[str] = set()
    for container in (error.get("detail"), error.get("data")):
        required = container.get("required") if isinstance(container, dict) else None
        if isinstance(required, list):
            found.update(
                item for item in required if isinstance(item, str) and _SCOPE_NAME.fullmatch(item)
            )
    return tuple(sorted(found))[:_MAX_QUOTED_SCOPES]


def _boundary(message: str) -> ConnectorFailure:
    return ConnectorFailure(ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION, message)
