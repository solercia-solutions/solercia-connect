from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from solercia_connect.auth import AuthorizationProvider
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    DomainRecord,
    ExternalIssue,
    HealthReport,
    HealthState,
    IssueQuery,
    IssueTransitionOption,
    IssueTransitionRequest,
    IssueTransitionResult,
    IssueTransitionsQuery,
    OpenIssue,
    OpenIssueQuery,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    same_status_name,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import HTTPTransport, ProviderDescriptor
from solercia_connect.providers._shared import (
    as_string,
    ensure_https_base_url,
    parse_optional_datetime,
    require_list,
    require_mapping,
    require_string,
)
from solercia_connect.transport import request_json

# Scoped API tokens are not accepted by the site URL: Atlassian routes them through this gateway,
# still with Basic email:token. Source: "Manage API tokens for your Atlassian account".
# OAuth 2.0 (3LO) access tokens use the same gateway ("OAuth 2.0 (3LO) apps", step 3.2).
ATLASSIAN_JIRA_GATEWAY = "https://api.atlassian.com/ex/jira"
# Atlassian documents tenant_info only for <site>.atlassian.net ("How to Find Your Atlassian
# Cloud Site's Cloud ID"); any other host must name its cloud ID explicitly.
_TENANT_INFO_HOST_SUFFIX = ".atlassian.net"
# User Identity API (scope read:me) and the sites an OAuth access token can reach.
ATLASSIAN_ME_URL = "https://api.atlassian.com/me"
ATLASSIAN_ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"


def normalize_jira_site(value: str) -> str:
    """Reduce a Jira site URL to its HTTPS origin, the non-secret half of a Jira connection."""

    parsed = urlparse(ensure_https_base_url(value.strip(), "Jira site"))
    if parsed.hostname is None:
        raise ValueError("Jira site must name a host")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"https://{parsed.hostname.casefold()}{port}"


def normalize_jira_cloud_id(value: str) -> str:
    """Accept only a UUID, so a cloud ID can never reshape the gateway path it is joined to."""

    try:
        return str(uuid.UUID(value.strip()))
    except (AttributeError, ValueError):
        raise ValueError("Jira cloud ID must be a UUID") from None


def jira_api_base_url(site: str, cloud_id: str | None) -> str:
    """REST base for a Jira connection: the site itself, or the gateway for a scoped token."""

    return site if cloud_id is None else f"{ATLASSIAN_JIRA_GATEWAY}/{cloud_id}"


def jira_site_publishes_tenant_info(site: str) -> bool:
    host = urlparse(site).hostname or ""
    return host.endswith(_TENANT_INFO_HOST_SUFFIX)


def resolve_jira_cloud_id(transport: HTTPTransport, site: str) -> str:
    """Read a site's cloud ID from ``/_edge/tenant_info``; the endpoint is public, so no token."""

    payload = request_json(
        transport,
        "GET",
        f"{site}/_edge/tenant_info",
        headers={"Accept": "application/json"},
    )
    data = require_mapping(payload, "jira.tenant_info")
    raw = require_string(data.get("cloudId"), "jira.tenant_info.cloudId")
    try:
        return normalize_jira_cloud_id(raw)
    except ValueError:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Jira tenant_info did not return a UUID cloud ID",
        ) from None


@dataclass(frozen=True, slots=True)
class JiraIdentity:
    """Identity proven by ``/myself``: the opaque accountId plus whether the email was visible.

    Basic authentication already binds the email to the token, so a hidden ``emailAddress``
    still leaves the account proven by authentication rather than by the profile field.
    """

    account_id: str
    email_visible: bool


@dataclass(frozen=True, slots=True)
class _IssueStatus:
    name: str
    category: str | None


@dataclass(frozen=True, slots=True)
class _Transition:
    id: str
    name: str
    to_status: str
    to_category: str | None
    requires_fields: bool


_TRANSITIONS_ENDPOINT = "/rest/api/3/issue/{issueIdOrKey}/transitions"
# Bounds the target names quoted back when no transition matches; they are not secret.
_MAX_LISTED_TARGETS = 20
_SEARCH_ENDPOINT = "/rest/api/3/search/jql"
# Every JQL read stops after this many pages, so a page token that never ends cannot hold a run.
_MAX_SEARCH_PAGES = 20
_OPEN_ISSUE_FIELDS = ("summary", "status", "issuetype", "priority", "updated", "assignee")


class JiraAdapter:
    descriptor = ProviderDescriptor(
        "jira.cloud",
        ProviderFamily.JIRA,
        RouteKind.OFFICIAL,
        frozenset(
            {
                Capability.EXTERNAL_ISSUES_SEARCH,
                Capability.EXTERNAL_ISSUES_OPEN_LIST,
                Capability.EXTERNAL_ISSUES_TRANSITIONS_LIST,
                Capability.EXTERNAL_ISSUES_TRANSITION,
            }
        ),
    )

    def __init__(
        self,
        transport: HTTPTransport,
        auth: AuthorizationProvider,
        *,
        base_url: str,
        expected_account_id: str | None = None,
        expected_account: str | None = None,
        grant_can_write: bool = True,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._transport = transport
        self._auth = auth
        self._base_url = ensure_https_base_url(base_url, "Jira base URL")
        self._expected_account_id = expected_account_id
        self._expected_account = expected_account
        # False for an Atlassian OAuth grant, which never requests write:jira-work.
        self._grant_can_write = grant_can_write
        self._clock = clock

    def health(self) -> HealthReport:
        account_id = self._identity()
        return HealthReport(
            self.descriptor.provider_id,
            HealthState.READY,
            ReasonCode.SUCCESS,
            tuple(self.descriptor.capabilities),
            account_id,
            "Jira identity and enhanced JQL search are reachable",
        )

    def execute(self, request: ConnectorRequest) -> tuple[DomainRecord, ...]:
        if request.capability is Capability.EXTERNAL_ISSUES_SEARCH and isinstance(
            request.payload, IssueQuery
        ):
            return self.search_issues(request.payload)
        if request.capability is Capability.EXTERNAL_ISSUES_OPEN_LIST and isinstance(
            request.payload, OpenIssueQuery
        ):
            return self.list_open_issues(request.payload)
        if request.capability is Capability.EXTERNAL_ISSUES_TRANSITIONS_LIST and isinstance(
            request.payload, IssueTransitionsQuery
        ):
            return self.list_transitions(request.payload)
        if request.capability is Capability.EXTERNAL_ISSUES_TRANSITION and isinstance(
            request.payload, IssueTransitionRequest
        ):
            return self.transition_issue(request.payload)
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "Jira request does not match the external issue capability contract",
        )

    def list_transitions(self, query: IssueTransitionsQuery) -> tuple[IssueTransitionOption, ...]:
        """List the transitions one issue can take now, after proving the account identity."""
        account_id = self._identity()
        current = self._issue_status(query.issue_key)
        return tuple(
            IssueTransitionOption(
                query.issue_key,
                transition.id,
                transition.name,
                transition.to_status,
                transition.to_category,
                transition.requires_fields,
                current.name,
                current.category,
                self._provenance(f"{query.issue_key}:{transition.id}", account_id),
            )
            for transition in self._transitions(query.issue_key)
        )

    def transition_issue(self, request: IssueTransitionRequest) -> tuple[IssueTransitionResult]:
        """Move one issue to the requested status through exactly one transition.

        Docs/decisions/2026-10-25-escritura-transiciones-jira.md fixes the order: identity, the
        status before, an idempotent stop, one unambiguous field-free transition, one POST, and
        the status after. Every refusal happens before the POST.
        """
        if not request.write_authorized:
            raise ConnectorFailure(
                ReasonCode.WRITE_NOT_AUTHORIZED,
                "The Jira transition was not explicitly authorized for this invocation",
            )
        if not self._grant_can_write:
            raise ConnectorFailure(
                ReasonCode.WRITE_SCOPE_MISSING,
                "The stored Atlassian OAuth grant has no write scope (write:jira-work); "
                "reconnect this account with an API token to transition issues",
            )
        key = request.issue_key
        account_id = self._identity()
        before = self._issue_status(key)
        provenance = self._provenance(key, account_id)
        if same_status_name(before.name, request.to_status):
            return (
                IssueTransitionResult(key, before.name, before.name, None, None, False, provenance),
            )
        selected = _select_transition(self._transitions(key), request.to_status)
        request_json(
            self._transport,
            "POST",
            f"{self._issue_url(key)}/transitions",
            headers=self._headers(),
            json_body={"transition": {"id": selected.id}},
            failure_refiner=_write_failure,
        )
        try:
            after = self._issue_status(key)
        except ConnectorFailure as failure:
            raise ConnectorFailure(
                ReasonCode.WRITE_UNVERIFIED,
                f"Jira accepted the transition but its status could not be read back "
                f"({failure.reason.value}); the write may have applied",
            ) from None
        if not same_status_name(after.name, request.to_status):
            raise ConnectorFailure(
                ReasonCode.WRITE_UNVERIFIED,
                f"After the transition Jira reports status '{after.name}' instead of "
                f"'{request.to_status}'; the write may have applied partially",
            )
        return (
            IssueTransitionResult(
                key, before.name, after.name, selected.id, selected.name, True, provenance
            ),
        )

    def search_issues(self, query: IssueQuery) -> tuple[ExternalIssue, ...]:
        account_id = self._identity()
        jql = query.jql or _default_jql(query)
        fields = [
            "summary",
            "status",
            "issuetype",
            "assignee",
            "reporter",
            "priority",
            "created",
            "updated",
            "resolutiondate",
            "project",
        ]

        def resolved_in_window(item: dict[str, Any]) -> ExternalIssue | None:
            issue = self._normalize_issue(item, account_id)
            if issue.resolved_at and query.window.start <= issue.resolved_at < query.window.end:
                return issue
            return None

        return self._search_jql(jql, fields, query.limit, resolved_in_window)

    def list_open_issues(self, query: OpenIssueQuery) -> tuple[OpenIssue, ...]:
        """List the open issues of the proven account through the same enhanced JQL search."""
        account_id = self._identity()
        return self._search_jql(
            query.jql or _open_issues_jql(query),
            list(_OPEN_ISSUE_FIELDS),
            query.limit,
            lambda item: self._normalize_open_issue(item, account_id),
        )

    def _search_jql[T](
        self,
        jql: str,
        fields: list[str],
        limit: int,
        convert: Callable[[dict[str, Any]], T | None],
    ) -> tuple[T, ...]:
        """Page through ``/search/jql`` until ``limit`` kept issues or the fixed page bound.

        ``convert`` normalizes one raw issue, or returns ``None`` for one the caller drops.
        """
        result: list[T] = []
        next_page_token: str | None = None
        pages = 0
        while len(result) < limit:
            pages += 1
            if pages > _MAX_SEARCH_PAGES:
                raise ConnectorFailure(
                    ReasonCode.MALFORMED_PROVIDER_DATA,
                    "Jira pagination exceeded the fixed page bound",
                )
            body: dict[str, Any] = {
                "jql": jql,
                "fields": fields,
                "maxResults": min(limit - len(result), 100),
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token
            payload = request_json(
                self._transport,
                "POST",
                f"{self._base_url}{_SEARCH_ENDPOINT}",
                headers=self._headers(),
                json_body=body,
            )
            data = require_mapping(payload, "jira.search")
            for raw in require_list(data.get("issues") or [], "jira.search.issues"):
                item = convert(require_mapping(raw, "jira.issue"))
                if item is not None:
                    result.append(item)
                if len(result) >= limit:
                    break
            raw_token = data.get("nextPageToken")
            next_page_token = raw_token if isinstance(raw_token, str) and raw_token else None
            if data.get("isLast") is True or not next_page_token:
                break
        return tuple(result)

    def verify_identity(self) -> JiraIdentity:
        payload = request_json(
            self._transport,
            "GET",
            f"{self._base_url}/rest/api/3/myself",
            headers=self._headers(),
        )
        data = require_mapping(payload, "jira.myself")
        account_id = require_string(data.get("accountId"), "jira.myself.accountId")
        if account_id.strip().casefold() == "unknown":
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Jira did not return a verifiable account identity",
            )
        if self._expected_account_id and account_id != self._expected_account_id:
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Authenticated Jira account does not match configured account",
            )
        email = as_string(data.get("emailAddress"))
        if (
            email is not None
            and self._expected_account is not None
            and email.casefold() != self._expected_account.casefold()
        ):
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Authenticated Jira email does not match the requested account",
            )
        return JiraIdentity(account_id, email is not None)

    def _identity(self) -> str:
        return self.verify_identity().account_id

    def _issue_url(self, issue_key: str) -> str:
        # The key already matched the exact Jira key pattern, so it cannot reshape this path.
        return f"{self._base_url}/rest/api/3/issue/{issue_key}"

    def _issue_status(self, issue_key: str) -> _IssueStatus:
        payload = request_json(
            self._transport,
            "GET",
            f"{self._issue_url(issue_key)}?fields=status",
            headers=self._headers(),
            failure_refiner=_issue_not_found,
        )
        data = require_mapping(payload, "jira.issue")
        if require_string(data.get("key"), "jira.issue.key") != issue_key:
            # Jira answers a moved or differently cased key with the issue it found instead.
            raise ConnectorFailure(
                ReasonCode.ISSUE_NOT_FOUND,
                "Jira answered with another issue key; the issue may have moved, so read it "
                "again with its current key",
            )
        fields = require_mapping(data.get("fields"), "jira.issue.fields")
        status = require_mapping(fields.get("status"), "jira.issue.fields.status")
        return _IssueStatus(
            require_string(status.get("name"), "jira.issue.fields.status.name"),
            _nested(status, "statusCategory", "key"),
        )

    def _transitions(self, issue_key: str) -> tuple[_Transition, ...]:
        payload = request_json(
            self._transport,
            "GET",
            f"{self._issue_url(issue_key)}/transitions?expand=transitions.fields",
            headers=self._headers(),
            failure_refiner=_issue_not_found,
        )
        data = require_mapping(payload, "jira.transitions")
        return tuple(
            _transition(require_mapping(raw, "jira.transition"))
            for raw in require_list(data.get("transitions"), "jira.transitions.transitions")
        )

    def _provenance(self, source_id: str, account_id: str) -> Provenance:
        return Provenance(
            self.descriptor.provider_id,
            self.descriptor.family,
            self.descriptor.route,
            source_id,
            self._clock(),
            account_id,
            _TRANSITIONS_ENDPOINT,
        )

    def _normalize_issue(self, item: dict[str, Any], account_id: str) -> ExternalIssue:
        source_id = require_string(item.get("id"), "jira.issue.id")
        key = require_string(item.get("key"), "jira.issue.key")
        fields = require_mapping(item.get("fields"), "jira.issue.fields")
        status = _nested(fields.get("status"), "name")
        status_category = _nested(_mapping_or_empty(fields.get("status")), "statusCategory", "name")
        return ExternalIssue(
            source_id,
            key,
            str(fields.get("summary") or ""),
            _nested(fields.get("issuetype"), "name"),
            status,
            status_category,
            _nested(fields.get("project"), "name"),
            _nested(fields.get("assignee"), "displayName"),
            _nested(fields.get("reporter"), "displayName"),
            _nested(fields.get("priority"), "name"),
            parse_optional_datetime(fields.get("created"), "jira.issue.created"),
            parse_optional_datetime(fields.get("updated"), "jira.issue.updated"),
            parse_optional_datetime(fields.get("resolutiondate"), "jira.issue.resolutiondate"),
            Provenance(
                self.descriptor.provider_id,
                self.descriptor.family,
                self.descriptor.route,
                source_id,
                self._clock(),
                account_id,
                _SEARCH_ENDPOINT,
            ),
        )

    def _normalize_open_issue(self, item: dict[str, Any], account_id: str) -> OpenIssue:
        source_id = require_string(item.get("id"), "jira.issue.id")
        fields = require_mapping(item.get("fields"), "jira.issue.fields")
        return OpenIssue(
            require_string(item.get("key"), "jira.issue.key"),
            as_string(fields.get("summary")) or "",
            _nested(fields.get("status"), "name"),
            _nested(fields.get("status"), "statusCategory", "key"),
            _nested(fields.get("issuetype"), "name"),
            _nested(fields.get("priority"), "name"),
            parse_optional_datetime(fields.get("updated"), "jira.issue.updated"),
            _nested(fields.get("assignee"), "accountId"),
            Provenance(
                self.descriptor.provider_id,
                self.descriptor.family,
                self.descriptor.route,
                source_id,
                self._clock(),
                account_id,
                _SEARCH_ENDPOINT,
            ),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self._auth.authorization_header(),
        }


@dataclass(frozen=True, slots=True)
class AtlassianOAuthIdentity:
    """An Atlassian OAuth grant bound to one account and one site.

    ``email_source`` names where the requested email was observed: ``me`` (User Identity API)
    or ``myself`` (Jira). Unlike Basic authentication, a bearer token binds no email by itself.
    """

    account_id: str
    cloud_id: str
    email_source: str


def verify_atlassian_oauth_identity(
    transport: HTTPTransport,
    auth: AuthorizationProvider,
    *,
    site: str,
    account: str,
    expected_account_id: str | None = None,
    expected_cloud_id: str | None = None,
) -> AtlassianOAuthIdentity:
    """Prove the grant's account, resolve the site's cloud ID, and match Jira's ``/myself``.

    Each mismatch, a missing email on both identity endpoints, and a site the grant lists zero
    or several times fail closed before any data read.
    """

    me = require_mapping(
        request_json(transport, "GET", ATLASSIAN_ME_URL, headers=_bearer_headers(auth)),
        "atlassian.me",
    )
    account_id = require_string(me.get("account_id"), "atlassian.me.account_id")
    email = as_string(me.get("email"))
    if email is not None and email.casefold() != account.casefold():
        raise _boundary("Atlassian account email does not match the requested account")
    if expected_account_id is not None and account_id != expected_account_id:
        raise _boundary("Atlassian account does not match the connection profile")
    cloud_id = _accessible_cloud_id(transport, auth, site)
    if expected_cloud_id is not None and cloud_id != expected_cloud_id:
        raise _boundary("The Jira site cloud ID no longer matches the connection profile")
    identity = JiraAdapter(
        transport,
        auth,
        base_url=jira_api_base_url(site, cloud_id),
        expected_account_id=account_id,
        expected_account=account,
    ).verify_identity()
    if email is None and not identity.email_visible:
        raise _boundary("Atlassian exposed no email that proves the requested account")
    return AtlassianOAuthIdentity(account_id, cloud_id, "me" if email is not None else "myself")


def _accessible_cloud_id(transport: HTTPTransport, auth: AuthorizationProvider, site: str) -> str:
    """Select the one resource whose URL is exactly the site; never the first of several."""

    payload = request_json(
        transport, "GET", ATLASSIAN_ACCESSIBLE_RESOURCES_URL, headers=_bearer_headers(auth)
    )
    matches: list[str] = []
    for raw in require_list(payload, "atlassian.accessible_resources"):
        item = require_mapping(raw, "atlassian.accessible_resources.item")
        try:
            origin = normalize_jira_site(as_string(item.get("url")) or "")
        except ValueError:
            continue
        if origin == site:
            matches.append(require_string(item.get("id"), "atlassian.accessible_resources.id"))
    if not matches:
        raise _boundary("The Atlassian grant does not include the requested Jira site")
    if len(matches) > 1:
        raise _boundary("The Atlassian grant lists the requested Jira site more than once")
    try:
        return normalize_jira_cloud_id(matches[0])
    except ValueError:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Atlassian accessible resources returned a cloud ID that is not a UUID",
        ) from None


def _bearer_headers(auth: AuthorizationProvider) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": auth.authorization_header()}


def _boundary(message: str) -> ConnectorFailure:
    return ConnectorFailure(ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION, message)


def _issue_not_found(status_code: int, body: bytes) -> ConnectorFailure | None:
    """Jira answers 404 both for a missing issue and for one the account may not browse."""
    del body
    if status_code == 404:
        return ConnectorFailure(
            ReasonCode.ISSUE_NOT_FOUND,
            "Jira issue was not found or is not visible to this account",
        )
    return None


def _write_failure(status_code: int, body: bytes) -> ConnectorFailure | None:
    if status_code == 409:
        return ConnectorFailure(
            ReasonCode.WRITE_CONFLICT,
            "Jira refused the transition because of a conflicting update; read the issue again",
        )
    return _issue_not_found(status_code, body)


def _select_transition(transitions: tuple[_Transition, ...], to_status: str) -> _Transition:
    """Pick the one field-free transition that reaches ``to_status``, or refuse to choose."""
    matches = [item for item in transitions if same_status_name(item.to_status, to_status)]
    if not matches:
        targets = sorted({item.to_status for item in transitions}, key=str.casefold)
        available = (
            "available target statuses: " + ", ".join(targets[:_MAX_LISTED_TARGETS])
            if targets
            else "no transition is available to this account now"
        )
        raise ConnectorFailure(
            ReasonCode.TRANSITION_UNAVAILABLE,
            f"No available transition leads to status '{to_status}'; {available}",
        )
    if len(matches) > 1:
        raise ConnectorFailure(
            ReasonCode.TRANSITION_AMBIGUOUS,
            f"{len(matches)} transitions lead to status '{to_status}'; refusing to choose one",
        )
    if matches[0].requires_fields:
        raise ConnectorFailure(
            ReasonCode.TRANSITION_REQUIRES_FIELDS,
            f"The transition to '{to_status}' requires fields, and this write never sets fields",
        )
    return matches[0]


def _transition(item: dict[str, Any]) -> _Transition:
    target = require_mapping(item.get("to"), "jira.transition.to")
    fields = require_mapping(item.get("fields", {}), "jira.transition.fields")
    required: list[bool] = []
    for raw in fields.values():
        flag = require_mapping(raw, "jira.transition.field").get("required")
        if not isinstance(flag, bool):
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Provider field is not a boolean: jira.transition.field.required",
            )
        required.append(flag)
    return _Transition(
        require_string(item.get("id"), "jira.transition.id"),
        require_string(item.get("name"), "jira.transition.name"),
        require_string(target.get("name"), "jira.transition.to.name"),
        _nested(target, "statusCategory", "key"),
        any(required),
    )


def _default_jql(query: IssueQuery) -> str:
    assignee = f'"{_escape_jql(query.assignee)}"' if query.assignee else "currentUser()"
    start = query.window.start.date().isoformat()
    end = query.window.end.date().isoformat()
    return (
        f"assignee = {assignee} AND statusCategory = Done "
        f'AND resolved >= "{start}" AND resolved < "{end}" ORDER BY resolved DESC'
    )


def _open_issues_jql(query: OpenIssueQuery) -> str:
    """The fixed filter of the open-issue read, in a fixed clause order."""
    clauses = ["assignee = currentUser()", "statusCategory != Done"]
    if query.project is not None:
        # The key already matched the exact project-key pattern; quoting it keeps a key that
        # spells a JQL word, such as AND or IN, a plain value.
        clauses.append(f'project = "{query.project}"')
    if query.current_sprint:
        clauses.append("sprint in openSprints()")
    return " AND ".join(clauses) + " ORDER BY updated DESC"


def _escape_jql(value: str | None) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nested(value: Any, *keys: str) -> str | None:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return as_string(current)
