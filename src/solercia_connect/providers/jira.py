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
    ExternalIssue,
    HealthReport,
    HealthState,
    IssueQuery,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
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
ATLASSIAN_JIRA_GATEWAY = "https://api.atlassian.com/ex/jira"
# Atlassian documents tenant_info only for <site>.atlassian.net ("How to Find Your Atlassian
# Cloud Site's Cloud ID"); any other host must name its cloud ID explicitly.
_TENANT_INFO_HOST_SUFFIX = ".atlassian.net"


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


class JiraAdapter:
    descriptor = ProviderDescriptor(
        "jira.cloud",
        ProviderFamily.JIRA,
        RouteKind.OFFICIAL,
        frozenset({Capability.EXTERNAL_ISSUES_SEARCH}),
    )

    def __init__(
        self,
        transport: HTTPTransport,
        auth: AuthorizationProvider,
        *,
        base_url: str,
        expected_account_id: str | None = None,
        expected_account: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._transport = transport
        self._auth = auth
        self._base_url = ensure_https_base_url(base_url, "Jira base URL")
        self._expected_account_id = expected_account_id
        self._expected_account = expected_account
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

    def execute(self, request: ConnectorRequest) -> tuple[ExternalIssue, ...]:
        if request.capability is Capability.EXTERNAL_ISSUES_SEARCH and isinstance(
            request.payload, IssueQuery
        ):
            return self.search_issues(request.payload)
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "Jira request does not match the external issue capability contract",
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
        result: list[ExternalIssue] = []
        next_page_token: str | None = None
        pages = 0
        while len(result) < query.limit:
            pages += 1
            if pages > 20:
                raise ConnectorFailure(
                    ReasonCode.MALFORMED_PROVIDER_DATA,
                    "Jira pagination exceeded the fixed page bound",
                )
            body: dict[str, Any] = {
                "jql": jql,
                "fields": fields,
                "maxResults": min(query.limit - len(result), 100),
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token
            payload = request_json(
                self._transport,
                "POST",
                f"{self._base_url}/rest/api/3/search/jql",
                headers=self._headers(),
                json_body=body,
            )
            data = require_mapping(payload, "jira.search")
            for raw in require_list(data.get("issues") or [], "jira.search.issues"):
                issue = self._normalize_issue(require_mapping(raw, "jira.issue"), account_id)
                if issue.resolved_at and query.window.start <= issue.resolved_at < query.window.end:
                    result.append(issue)
                if len(result) >= query.limit:
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
                "/rest/api/3/search/jql",
            ),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self._auth.authorization_header(),
        }


def _default_jql(query: IssueQuery) -> str:
    assignee = f'"{_escape_jql(query.assignee)}"' if query.assignee else "currentUser()"
    start = query.window.start.date().isoformat()
    end = query.window.end.date().isoformat()
    return (
        f"assignee = {assignee} AND statusCategory = Done "
        f'AND resolved >= "{start}" AND resolved < "{end}" ORDER BY resolved DESC'
    )


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
