"""Bitbucket Cloud, read-only and official-only: docs/decisions/2026-10-26-conector-bitbucket.md.

An Atlassian API token created with Bitbucket scopes authenticates by Basic ``email:token``. Every
read first proves the identity in three steps (decision B-D2): the account ID of ``/user``, the
confirmed account email in ``/user/emails``, and access to the workspace that is read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse

from solercia_connect.auth import AuthorizationProvider
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    DomainRecord,
    HealthReport,
    HealthState,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    ScmRepository,
    ScmRepositoryQuery,
    normalize_bitbucket_workspace,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import HTTPTransport, ProviderDescriptor
from solercia_connect.providers._shared import (
    as_string,
    parse_datetime,
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
# Fixed page bounds: reaching one fails with result_incomplete instead of truncating.
_MAX_REPOSITORY_PAGES = 20
_REPOSITORY_PAGE_LENGTH = 100


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
        frozenset({Capability.SCM_REPOSITORIES_LIST}),
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
        if request.capability is Capability.SCM_REPOSITORIES_LIST and isinstance(
            request.payload, ScmRepositoryQuery
        ):
            return self.list_repositories(request.payload)
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "Bitbucket request does not match a source-control capability contract",
        )

    def list_repositories(self, query: ScmRepositoryQuery) -> tuple[ScmRepository, ...]:
        """List the workspace repositories the account is a member of, newest update first."""

        workspace = query.workspace or self._workspace
        identity = self.verify_identity(workspace)
        found: dict[str, ScmRepository] = {}
        for page in self._pages(
            f"{BITBUCKET_API_BASE_URL}/repositories/{workspace}",
            {"role": "member", "sort": "-updated_on", "pagelen": _REPOSITORY_PAGE_LENGTH},
            max_pages=_MAX_REPOSITORY_PAGES,
            what="repositories",
            not_found=_workspace_not_found(workspace),
        ):
            for raw in require_list(page.get("values"), "bitbucket.repositories.values"):
                repository = self._repository(
                    require_mapping(raw, "bitbucket.repository"), identity
                )
                found[repository.slug] = repository
        return tuple(
            sorted(found.values(), key=lambda item: (-item.updated_at.timestamp(), item.slug))
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
            f"{BITBUCKET_API_BASE_URL}/workspaces/{target}", not_found=_workspace_not_found(target)
        )
        return BitbucketIdentity(account_id, target)

    def _pages(
        self,
        collection: str,
        params: Mapping[str, Any],
        *,
        max_pages: int,
        what: str,
        not_found: ConnectorFailure | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield the pages of one collection, following only its ``next`` link.

        A caller may stop early. Reaching the bound while a next page still exists fails with
        ``result_incomplete``, so a read is either complete or reports nothing.
        """

        url: str = collection
        query: Mapping[str, Any] | None = params
        for _ in range(max_pages):
            page = require_mapping(
                self._get(url, params=query, not_found=not_found), f"bitbucket.{what}"
            )
            yield page
            next_url = next_page_url(page, collection)
            if next_url is None:
                return
            url, query = next_url, None
        raise ConnectorFailure(
            ReasonCode.RESULT_INCOMPLETE,
            f"Bitbucket has more {what} than the fixed bound of {max_pages} pages; nothing is "
            "reported rather than a partial list",
        )

    def _repository(self, item: dict[str, Any], identity: BitbucketIdentity) -> ScmRepository:
        full_name = require_string(item.get("full_name"), "bitbucket.repository.full_name")
        owner, _, path_slug = full_name.partition("/")
        if owner.lower() != identity.workspace or not path_slug or "/" in path_slug:
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Bitbucket returned a repository outside the requested workspace",
            )
        is_private = item.get("is_private")
        if not isinstance(is_private, bool):
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Provider field is not a boolean: bitbucket.repository.is_private",
            )
        return ScmRepository(
            identity.workspace,
            as_string(item.get("slug")) or path_slug,
            require_string(item.get("name"), "bitbucket.repository.name"),
            full_name,
            is_private,
            _nested(item, "mainbranch", "name"),
            _nested(item, "project", "key"),
            parse_datetime(item.get("updated_on"), "bitbucket.repository.updated_on"),
            _nested(item, "links", "html", "href"),
            self._provenance(full_name, identity, "/2.0/repositories/{workspace}"),
        )

    def _provenance(self, source_id: str, identity: BitbucketIdentity, endpoint: str) -> Provenance:
        return Provenance(
            self.descriptor.provider_id,
            self.descriptor.family,
            self.descriptor.route,
            source_id,
            self._clock(),
            identity.account_id,
            endpoint,
        )

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


def _workspace_not_found(workspace: str) -> ConnectorFailure:
    return ConnectorFailure(
        ReasonCode.WORKSPACE_NOT_FOUND,
        f"Bitbucket workspace {workspace} was not found or is not visible to this account",
    )


def _nested(value: Any, *keys: str) -> str | None:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return as_string(current)


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
