"""Bitbucket repository creation over the separate ``bitbucket-admin`` connection.

Decision: docs/decisions/2026-10-25-escritura-creacion-repositorios-bitbucket.md. The order is
fixed (R-D2): the B-D2 identity proof, a read showing the repository does not exist, a read of the
project, one ``POST`` with a fixed body, and a check of Bitbucket's answer. Every refusal happens
before the ``POST``, which is never retried. A failure after it that does not prove a refusal is
``write_unverified``, because Bitbucket may already have created the repository.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    DomainRecord,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    ScmRepositoryCreateRequest,
    ScmRepositoryCreation,
    require_bitbucket_project_key,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import ProviderDescriptor
from solercia_connect.providers._shared import (
    as_string,
    parse_datetime,
    require_mapping,
    require_string,
)
from solercia_connect.providers.bitbucket import (
    BITBUCKET_API_BASE_URL,
    BitbucketAdapter,
    BitbucketIdentity,
    bitbucket_refiner,
    required_scopes,
    workspace_not_found,
)
from solercia_connect.transport import FailureRefiner, JsonPayload, request_json

_REPOSITORY_ENDPOINT = "/2.0/repositories/{workspace}/{repo_slug}"
# The only clone host a record may name; any other link is dropped rather than passed on.
_CLONE_HOST = "bitbucket.org"
# Failures after the POST that do not prove Bitbucket refused it.
_UNCONFIRMED_REASONS = frozenset(
    {
        ReasonCode.PROVIDER_UNAVAILABLE,
        ReasonCode.NETWORK_TIMEOUT,
        ReasonCode.NETWORK_UNAVAILABLE,
        ReasonCode.MALFORMED_PROVIDER_DATA,
        ReasonCode.UNKNOWN_ERROR,
    }
)
_REJECTED_CREATION = (
    "Bitbucket rejected the repository creation: the request was invalid or this account may "
    "not create repositories in the workspace; nothing was created"
)


class BitbucketAdminAdapter(BitbucketAdapter):
    """The write side of Bitbucket: it reuses the identity proof and offers only the creation."""

    descriptor = ProviderDescriptor(
        "bitbucket.cloud",
        ProviderFamily.BITBUCKET,
        RouteKind.OFFICIAL,
        frozenset({Capability.SCM_REPOSITORIES_CREATE}),
    )

    def execute(self, request: ConnectorRequest) -> tuple[DomainRecord, ...]:
        if request.capability is Capability.SCM_REPOSITORIES_CREATE and isinstance(
            request.payload, ScmRepositoryCreateRequest
        ):
            return self.create_repository(request.payload)
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "Bitbucket admin request does not match the repository creation contract",
        )

    def create_repository(
        self, request: ScmRepositoryCreateRequest
    ) -> tuple[ScmRepositoryCreation]:
        """Create one private, empty repository in the named project, or refuse before writing."""

        if not request.write_authorized:
            raise ConnectorFailure(
                ReasonCode.WRITE_NOT_AUTHORIZED,
                "The Bitbucket repository creation was not explicitly authorized for this "
                "invocation",
            )
        workspace = request.workspace or self._workspace
        identity = self.verify_identity(workspace)
        slug = request.repository
        url = f"{BITBUCKET_API_BASE_URL}/repositories/{workspace}/{slug}"
        self._require_absent(url, workspace, slug)
        self._require_project(workspace, request.project_key)
        answer = self._post(
            url,
            {
                "scm": "git",
                "is_private": True,
                "fork_policy": "no_public_forks",
                "project": {"key": request.project_key},
            },
            workspace,
        )
        try:
            return (self._verified(answer, request, identity),)
        except ConnectorFailure as failure:
            raise ConnectorFailure(
                ReasonCode.WRITE_UNVERIFIED,
                f"Bitbucket created the repository {workspace}/{slug}, but its answer does not "
                f"match the request ({failure.safe_message}); the repository was created, so "
                "review it in Bitbucket before any new attempt",
            ) from None

    def _require_absent(self, url: str, workspace: str, slug: str) -> None:
        """Stop unless Bitbucket answers that no repository exists at this slug."""

        absent = ConnectorFailure(ReasonCode.REPOSITORY_NOT_FOUND, "absent")
        try:
            self._get(url, not_found=absent)
        except ConnectorFailure as failure:
            if failure is absent:
                return
            if failure.reason is ReasonCode.AUTHORIZATION_DENIED:
                raise ConnectorFailure(
                    ReasonCode.AUTHORIZATION_DENIED,
                    f"{failure.safe_message}. A private repository this account cannot see may "
                    f"already use {workspace}/{slug}; nothing was created",
                ) from None
            raise
        raise ConnectorFailure(
            ReasonCode.REPOSITORY_EXISTS,
            f"Bitbucket repository {workspace}/{slug} already exists; nothing was changed",
        )

    def _require_project(self, workspace: str, key: str) -> None:
        """Read the project and require Bitbucket to name it with exactly the requested key."""

        project = require_mapping(
            self._get(
                f"{BITBUCKET_API_BASE_URL}/workspaces/{workspace}/projects/{key}",
                not_found=ConnectorFailure(
                    ReasonCode.PROJECT_NOT_FOUND,
                    f"Bitbucket project {key} was not found in workspace {workspace} or is not "
                    "visible to this account; nothing was created",
                ),
            ),
            "bitbucket.project",
        )
        try:
            found = require_bitbucket_project_key(
                require_string(project.get("key"), "bitbucket.project.key")
            )
        except ValueError:
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Provider field is not a project key: bitbucket.project.key",
            ) from None
        if found != key:
            raise ConnectorFailure(
                ReasonCode.INVALID_INPUT,
                f"Bitbucket names this project {found}; run again with --project {found}. Nothing "
                "was created",
            )

    def _post(self, url: str, body: dict[str, Any], workspace: str) -> JsonPayload:
        """Send the one POST; a failure that does not prove a refusal becomes unverified."""

        # The authorization header is built first, so nothing that fails here follows the write.
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self._auth.authorization_header(),
        }
        try:
            return request_json(
                self._transport,
                "POST",
                url,
                headers=headers,
                json_body=body,
                failure_refiner=_creation_failure(workspace),
            )
        except ConnectorFailure as failure:
            if failure.reason in _UNCONFIRMED_REASONS:
                raise _unverified(failure.reason) from None
            raise
        except Exception:
            raise _unverified(ReasonCode.UNKNOWN_ERROR) from None

    def _verified(
        self,
        answer: JsonPayload,
        request: ScmRepositoryCreateRequest,
        identity: BitbucketIdentity,
    ) -> ScmRepositoryCreation:
        """Build the record only when Bitbucket's answer is exactly what was requested."""

        item = require_mapping(answer, "bitbucket.repository")
        workspace = identity.workspace
        slug = require_string(item.get("slug"), "bitbucket.repository.slug")
        full_name = require_string(item.get("full_name"), "bitbucket.repository.full_name")
        owner, _, path_slug = full_name.partition("/")
        project = item.get("project")
        project_key = project.get("key") if isinstance(project, dict) else None
        answered_workspace = item.get("workspace")
        workspace_slug = (
            answered_workspace.get("slug") if isinstance(answered_workspace, dict) else workspace
        )
        mismatches = [
            name
            for name, matches in (
                ("slug", slug == request.repository and path_slug == request.repository),
                ("workspace", owner.lower() == workspace and workspace_slug == workspace),
                ("is_private", item.get("is_private") is True),
                ("project", project_key == request.project_key),
            )
            if not matches
        ]
        if mismatches:
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA, "mismatched " + ", ".join(mismatches)
            )
        links = item.get("links")
        return ScmRepositoryCreation(
            workspace,
            slug,
            full_name,
            request.project_key,
            True,
            _web_url(links),
            _clone_https(links),
            parse_datetime(item.get("created_on"), "bitbucket.repository.created_on"),
            self._provenance(full_name, identity, _REPOSITORY_ENDPOINT),
        )


def _creation_failure(workspace: str) -> FailureRefiner:
    """Classify a refused POST; a 400 gets a fixed message and no body is ever quoted."""

    reads = bitbucket_refiner(workspace_not_found(workspace))

    def refine(status_code: int, body: bytes) -> ConnectorFailure | None:
        if status_code == 400:
            return ConnectorFailure(ReasonCode.REQUEST_REJECTED, _REJECTED_CREATION)
        if status_code == 403:
            scopes = required_scopes(body)
            return ConnectorFailure(
                ReasonCode.AUTHORIZATION_DENIED,
                "Bitbucket denied the repository creation; "
                + (
                    "the API token lacks the required scopes: " + ", ".join(scopes)
                    if scopes
                    else "the token may lack admin:repository:bitbucket, or the account may not "
                    "create repositories in this workspace"
                ),
            )
        return reads(status_code, body)

    return refine


def _unverified(reason: ReasonCode) -> ConnectorFailure:
    return ConnectorFailure(
        ReasonCode.WRITE_UNVERIFIED,
        f"Bitbucket did not confirm the repository creation ({reason.value}); the repository may "
        "have been created, so check Bitbucket before any new attempt",
    )


def _web_url(links: Any) -> str | None:
    html = links.get("html") if isinstance(links, dict) else None
    return _bitbucket_url(html.get("href")) if isinstance(html, dict) else None


def _clone_https(links: Any) -> str | None:
    """Return the HTTPS clone URL without any user name or password Bitbucket put in it."""

    clones = links.get("clone") if isinstance(links, dict) else None
    if not isinstance(clones, list):
        return None
    for clone in clones:
        if isinstance(clone, dict) and clone.get("name") == "https":
            return _bitbucket_url(clone.get("href"))
    return None


def _bitbucket_url(value: Any) -> str | None:
    """Rebuild an HTTPS link on bitbucket.org from its path alone, or drop it.

    Rebuilding instead of passing the link on keeps any user name or password out of the record.
    """

    href = as_string(value)
    if href is None:
        return None
    parts = urlsplit(href)
    if (
        parts.scheme != "https"
        or parts.hostname != _CLONE_HOST
        or parts.port is not None
        or parts.query
        or parts.fragment
        or not parts.path.startswith("/")
    ):
        return None
    return f"https://{_CLONE_HOST}{parts.path}"
