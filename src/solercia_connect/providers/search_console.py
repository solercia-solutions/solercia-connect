from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from solercia_connect.auth import AuthorizationProvider
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    HealthReport,
    HealthState,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    SearchAnalyticsQuery,
    SearchAnalyticsReport,
    SearchAnalyticsRow,
    SearchSitemap,
    SearchSitemapContent,
    SearchSitemapsQuery,
    SearchUrlInspection,
    SearchUrlInspectionQuery,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import HTTPTransport, ProviderDescriptor
from solercia_connect.providers._shared import (
    as_string,
    parse_optional_datetime,
    require_list,
    require_mapping,
    require_string,
    unique_canonical_account_identity,
)
from solercia_connect.transport import request_json

_WEBMASTERS_BASE_URL = "https://www.googleapis.com/webmasters/v3"
_URL_INSPECTION_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
# The value of userinfo_endpoint in https://accounts.google.com/.well-known/openid-configuration.
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_PAGE_SIZE = 25_000


class GoogleSearchConsoleAdapter:
    """Read Search Console through the official API.

    Search Console has no user-profile endpoint, so identity comes from OpenID Connect userinfo,
    which the granted ``openid email`` scopes unlock. Every data read proves it first.
    """

    descriptor = ProviderDescriptor(
        "google.searchconsole",
        ProviderFamily.GOOGLE,
        RouteKind.OFFICIAL,
        frozenset(
            {
                Capability.SEARCH_ANALYTICS_QUERY,
                Capability.SEARCH_SITEMAPS_LIST,
                Capability.SEARCH_URLS_INSPECT,
            }
        ),
    )

    def __init__(
        self,
        transport: HTTPTransport,
        auth: AuthorizationProvider,
        *,
        expected_account: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._transport = transport
        self._auth = auth
        self._expected_account = _canonical_expected_account(expected_account)
        self._clock = clock

    def health(self) -> HealthReport:
        account = self._verified_identity(None)
        payload = request_json(
            self._transport,
            "GET",
            f"{_WEBMASTERS_BASE_URL}/sites",
            headers=self._headers(),
        )
        require_mapping(payload, "searchconsole.sites")
        return HealthReport(
            self.descriptor.provider_id,
            HealthState.READY,
            ReasonCode.SUCCESS,
            tuple(self.descriptor.capabilities),
            account,
            "Search Console read capability is reachable",
        )

    def execute(
        self, request: ConnectorRequest
    ) -> tuple[SearchAnalyticsReport, ...] | tuple[SearchSitemap, ...] | tuple[SearchUrlInspection]:
        payload = request.payload
        if request.capability is Capability.SEARCH_ANALYTICS_QUERY and isinstance(
            payload, SearchAnalyticsQuery
        ):
            return (self.query_analytics(payload),)
        if request.capability is Capability.SEARCH_SITEMAPS_LIST and isinstance(
            payload, SearchSitemapsQuery
        ):
            return self.list_sitemaps(payload)
        if request.capability is Capability.SEARCH_URLS_INSPECT and isinstance(
            payload, SearchUrlInspectionQuery
        ):
            return (self.inspect_url(payload),)
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "Search Console request does not match the capability contract",
        )

    def query_analytics(self, query: SearchAnalyticsQuery) -> SearchAnalyticsReport:
        account = self._verified_identity(query.account_id)
        rows: list[SearchAnalyticsRow] = []
        aggregation: str | None = None
        first_incomplete: date | None = None
        truncated = False
        while True:
            page_size = min(_PAGE_SIZE, query.row_limit - len(rows))
            body: dict[str, Any] = {
                "startDate": query.start_date.isoformat(),
                "endDate": query.end_date.isoformat(),
                "dimensions": list(query.dimensions),
                "type": query.search_type,
                "dataState": query.data_state,
                "rowLimit": page_size,
                "startRow": len(rows),
            }
            payload = request_json(
                self._transport,
                "POST",
                f"{_WEBMASTERS_BASE_URL}/sites/{_site_path(query.site_url)}/searchAnalytics/query",
                headers=self._headers(),
                json_body=body,
                timeout_seconds=120.0,
            )
            data = require_mapping(payload, "searchconsole.analytics")
            page = [
                _analytics_row(raw, len(query.dimensions))
                for raw in require_list(data.get("rows") or [], "searchconsole.analytics.rows")
            ]
            rows.extend(page)
            aggregation = as_string(data.get("responseAggregationType")) or aggregation
            first_incomplete = _first_incomplete_date(data.get("metadata")) or first_incomplete
            if len(page) < page_size:
                break
            if len(rows) >= query.row_limit:
                truncated = True
                break
        first_day, last_day = _days_with_data(rows, query.dimensions)
        identifier = (
            f"{query.site_url}|{query.start_date}|{query.end_date}|"
            f"{','.join(query.dimensions)}|{query.search_type}|{query.data_state}"
        )
        return SearchAnalyticsReport(
            identifier,
            query.site_url,
            query.start_date,
            query.end_date,
            query.dimensions,
            query.search_type,
            query.data_state,
            tuple(rows),
            first_day,
            last_day,
            first_incomplete,
            aggregation,
            truncated,
            self._provenance(identifier, account, "/sites/{siteUrl}/searchAnalytics/query"),
        )

    def list_sitemaps(self, query: SearchSitemapsQuery) -> tuple[SearchSitemap, ...]:
        account = self._verified_identity(query.account_id)
        payload = request_json(
            self._transport,
            "GET",
            f"{_WEBMASTERS_BASE_URL}/sites/{_site_path(query.site_url)}/sitemaps",
            headers=self._headers(),
        )
        data = require_mapping(payload, "searchconsole.sitemaps")
        return tuple(
            self._sitemap(require_mapping(raw, "searchconsole.sitemap"), query.site_url, account)
            for raw in require_list(data.get("sitemap") or [], "searchconsole.sitemaps.sitemap")
        )

    def inspect_url(self, query: SearchUrlInspectionQuery) -> SearchUrlInspection:
        account = self._verified_identity(query.account_id)
        payload = request_json(
            self._transport,
            "POST",
            _URL_INSPECTION_URL,
            headers=self._headers(),
            json_body={
                "inspectionUrl": query.inspection_url,
                "siteUrl": query.site_url,
                "languageCode": query.language_code,
            },
        )
        data = require_mapping(payload, "searchconsole.inspection")
        result = require_mapping(data.get("inspectionResult"), "searchconsole.inspectionResult")
        status = require_mapping(
            result.get("indexStatusResult"), "searchconsole.inspectionResult.indexStatusResult"
        )
        return SearchUrlInspection(
            query.inspection_url,
            query.site_url,
            query.inspection_url,
            as_string(status.get("verdict")),
            as_string(status.get("coverageState")),
            as_string(status.get("robotsTxtState")),
            as_string(status.get("indexingState")),
            as_string(status.get("pageFetchState")),
            parse_optional_datetime(status.get("lastCrawlTime"), "inspection.lastCrawlTime"),
            as_string(status.get("googleCanonical")),
            as_string(status.get("userCanonical")),
            as_string(status.get("crawledAs")),
            _string_tuple(status.get("sitemap"), "inspection.sitemap"),
            _string_tuple(status.get("referringUrls"), "inspection.referringUrls"),
            as_string(result.get("inspectionResultLink")),
            self._provenance(query.inspection_url, account, "/urlInspection/index:inspect"),
        )

    def _sitemap(self, item: dict[str, Any], site_url: str, account: str) -> SearchSitemap:
        path = require_string(item.get("path"), "searchconsole.sitemap.path")
        contents = tuple(
            SearchSitemapContent(
                require_string(content.get("type"), "searchconsole.sitemap.content.type"),
                _optional_count(content.get("submitted"), "sitemap.content.submitted"),
            )
            for content in (
                require_mapping(raw, "searchconsole.sitemap.content")
                for raw in require_list(item.get("contents") or [], "sitemap.contents")
            )
        )
        return SearchSitemap(
            path,
            site_url,
            path,
            item.get("isPending") is True,
            item.get("isSitemapsIndex") is True,
            as_string(item.get("type")),
            parse_optional_datetime(item.get("lastSubmitted"), "sitemap.lastSubmitted"),
            parse_optional_datetime(item.get("lastDownloaded"), "sitemap.lastDownloaded"),
            _optional_count(item.get("errors"), "sitemap.errors") or 0,
            _optional_count(item.get("warnings"), "sitemap.warnings") or 0,
            contents,
            self._provenance(path, account, "/sites/{siteUrl}/sitemaps"),
        )

    def _verified_identity(self, requested_account: str | None) -> str:
        payload = request_json(
            self._transport,
            "GET",
            _USERINFO_URL,
            headers=self._headers(),
        )
        data = require_mapping(payload, "searchconsole.identity")
        observed = unique_canonical_account_identity(data.get("email"), exact=True)
        if observed is None or data.get("email_verified") is not True:
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Google did not expose one verified authenticated account identity",
            )
        requested = None
        if requested_account is not None:
            requested = unique_canonical_account_identity(requested_account, exact=True)
            if requested is None:
                raise ConnectorFailure(
                    ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                    "Requested Google account is not one unambiguous account identity",
                )
        if any(
            boundary is not None and observed != boundary
            for boundary in (self._expected_account, requested)
        ):
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Authenticated Google account does not match the configured or requested account",
            )
        return observed

    def _provenance(self, source_id: str, account: str, endpoint: str) -> Provenance:
        return Provenance(
            self.descriptor.provider_id,
            self.descriptor.family,
            self.descriptor.route,
            source_id,
            self._clock(),
            account,
            endpoint,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": self._auth.authorization_header(),
        }


def _canonical_expected_account(value: str | None) -> str | None:
    if value is None:
        return None
    identity = unique_canonical_account_identity(value, exact=True)
    if identity is None:
        raise ValueError("Expected Google account must be one unambiguous email identity")
    return identity


def _site_path(site_url: str) -> str:
    # The API takes the property as one path segment: sc-domain:example.com travels encoded.
    return quote(site_url, safe="")


def _analytics_row(raw: Any, dimension_count: int) -> SearchAnalyticsRow:
    item = require_mapping(raw, "searchconsole.analytics.row")
    keys = tuple(
        require_string(key, "searchconsole.analytics.row.key")
        for key in require_list(item.get("keys") or [], "searchconsole.analytics.row.keys")
    )
    if len(keys) != dimension_count:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Search Console row keys do not match the requested dimensions",
        )
    return SearchAnalyticsRow(
        keys,
        _number(item.get("clicks"), "clicks"),
        _number(item.get("impressions"), "impressions"),
        _number(item.get("ctr"), "ctr"),
        _number(item.get("position"), "position"),
    )


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            f"Search Console row metric is not a number: {label}",
        )
    return float(value)


def _optional_count(value: Any, label: str) -> int | None:
    # The API declares these as "long", which Google's JSON may carry as a decimal string.
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            f"Search Console count is malformed: {label}",
        )
    try:
        return int(value)
    except ValueError as exc:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            f"Search Console count is malformed: {label}",
        ) from exc


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    return tuple(require_string(item, label) for item in require_list(value or [], label))


def _first_incomplete_date(metadata: Any) -> date | None:
    if metadata is None:
        return None
    raw = require_mapping(metadata, "searchconsole.analytics.metadata").get("first_incomplete_date")
    return _parse_date(raw) if raw is not None else None


def _days_with_data(
    rows: list[SearchAnalyticsRow], dimensions: tuple[str, ...]
) -> tuple[date | None, date | None]:
    if "date" not in dimensions or not rows:
        return None, None
    index = dimensions.index("date")
    days = [_parse_date(row.keys[index]) for row in rows]
    return min(days), max(days)


def _parse_date(value: Any) -> date:
    text = require_string(value, "searchconsole.analytics.date")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ConnectorFailure(
            ReasonCode.MALFORMED_PROVIDER_DATA,
            "Search Console returned a malformed date",
        ) from exc
