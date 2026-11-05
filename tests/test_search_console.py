from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import pytest
from typer.testing import CliRunner

from solercia_connect import cli
from solercia_connect.application import _query, run_application
from solercia_connect.auth import BearerTokenProvider
from solercia_connect.config import (
    ConnectionProfile,
    ConnectionType,
    ProfileStore,
    load_settings,
    target_spec,
)
from solercia_connect.connections import oauth_provider_from_settings
from solercia_connect.doctor import DoctorService
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    ReasonCode,
    SearchAnalyticsQuery,
    SearchSitemapsQuery,
    SearchUrlInspectionQuery,
    url_belongs_to_search_property,
    validate_search_property,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.oauth import GoogleOAuthProvider, OAuthTokenSet, grant_covers_scopes
from solercia_connect.providers.search_console import GoogleSearchConsoleAdapter
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import ConnectorRuntime
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport, json_response

SITE = "sc-domain:solercia.com.co"
IDENTITY = {"email": "user@example.com", "email_verified": True}


def _adapter(*responses: object) -> tuple[GoogleSearchConsoleAdapter, FakeTransport]:
    transport = FakeTransport(json_response(200, IDENTITY), *responses)  # type: ignore[arg-type]
    adapter = GoogleSearchConsoleAdapter(
        transport,
        BearerTokenProvider(SecretValue("search-secret")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )
    return adapter, transport


def _query_for(**overrides: object) -> SearchAnalyticsQuery:
    values: dict[str, object] = {
        "site_url": SITE,
        "start_date": date(2026, 10, 1),
        "end_date": date(2026, 10, 31),
        "dimensions": ("date", "query"),
    }
    values.update(overrides)
    return SearchAnalyticsQuery(**values)  # type: ignore[arg-type]


def _row(day: str, query: str) -> dict[str, object]:
    return {"keys": [day, query], "clicks": 1, "impressions": 4, "ctr": 0.25, "position": 4.8}


def test_analytics_encodes_the_domain_property_and_reports_coverage() -> None:
    adapter, transport = _adapter(
        json_response(
            200,
            {
                "rows": [_row("2026-10-20", "boki"), _row("2026-10-03", "solercia")],
                "responseAggregationType": "byPage",
            },
        )
    )

    report = adapter.query_analytics(_query_for())

    request = transport.requests[1]
    assert request.method == "POST"
    assert "/sites/sc-domain%3Asolercia.com.co/searchAnalytics/query" in request.url
    assert unquote(request.url).count(SITE) == 1
    assert request.json_body is not None
    assert request.json_body["startDate"] == "2026-10-01"
    assert request.json_body["endDate"] == "2026-10-31"
    assert request.json_body["dimensions"] == ["date", "query"]
    assert request.json_body["type"] == "web"
    assert request.json_body["dataState"] == "final"
    assert request.json_body["startRow"] == 0
    assert report.first_day_with_data == date(2026, 10, 3)
    assert report.last_day_with_data == date(2026, 10, 20)
    assert report.aggregation_type == "byPage"
    assert report.truncated is False
    assert report.provenance.account_id == "user@example.com"


def test_analytics_empty_range_stays_empty_without_a_claimed_first_day() -> None:
    adapter, transport = _adapter(json_response(200, {"responseAggregationType": "auto"}))

    report = adapter.query_analytics(_query_for())

    assert report.rows == ()
    assert report.first_day_with_data is None
    assert report.last_day_with_data is None
    assert report.truncated is False
    assert len(transport.requests) == 2


def test_analytics_without_date_dimension_does_not_claim_a_first_day() -> None:
    adapter, _ = _adapter(
        json_response(
            200,
            {"rows": [{"keys": ["boki"], "clicks": 0, "impressions": 3, "ctr": 0, "position": 2}]},
        )
    )

    report = adapter.query_analytics(_query_for(dimensions=("query",)))

    assert len(report.rows) == 1
    assert report.first_day_with_data is None


def test_analytics_pages_until_a_short_page_and_marks_a_reached_limit() -> None:
    full = {"rows": [_row("2026-10-01", f"q{index}") for index in range(25_000)]}
    short = {"rows": [_row("2026-10-02", "last")]}
    adapter, transport = _adapter(json_response(200, full), json_response(200, short))

    report = adapter.query_analytics(_query_for(row_limit=50_000))

    assert len(report.rows) == 25_001
    assert report.truncated is False
    assert transport.requests[2].json_body is not None
    assert transport.requests[2].json_body["startRow"] == 25_000

    capped = {"rows": [_row("2026-10-01", f"q{index}") for index in range(3)]}
    adapter, transport = _adapter(json_response(200, capped))

    limited = adapter.query_analytics(_query_for(row_limit=3))

    assert limited.truncated is True
    assert len(transport.requests) == 2


def test_analytics_surfaces_first_incomplete_date_from_metadata() -> None:
    adapter, _ = _adapter(
        json_response(
            200,
            {
                "rows": [_row("2026-10-30", "boki")],
                "metadata": {"first_incomplete_date": "2026-11-02"},
            },
        )
    )

    report = adapter.query_analytics(_query_for(data_state="all"))

    assert report.first_incomplete_date == date(2026, 11, 2)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"rows": [{"keys": ["only-one"], "clicks": 1}]}, ReasonCode.MALFORMED_PROVIDER_DATA),
        (
            {
                "rows": [
                    {"keys": ["a", "b"], "clicks": "x", "impressions": 1, "ctr": 0, "position": 1}
                ]
            },
            ReasonCode.MALFORMED_PROVIDER_DATA,
        ),
        (
            {
                "rows": [
                    {"keys": ["nope", "b"], "clicks": 1, "impressions": 1, "ctr": 0, "position": 1}
                ]
            },
            ReasonCode.MALFORMED_PROVIDER_DATA,
        ),
    ],
)
def test_analytics_malformed_rows_fail_closed(
    payload: dict[str, object], reason: ReasonCode
) -> None:
    adapter, _ = _adapter(json_response(200, payload))

    with pytest.raises(ConnectorFailure) as failure:
        adapter.query_analytics(_query_for())

    assert failure.value.reason is reason


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"email": "user@example.com"},
        {"email": "user@example.com", "email_verified": False},
        {"email": "other@example.com", "email_verified": True},
        {"email": "a@example.com and b@example.com", "email_verified": True},
    ],
    ids=["missing", "unasserted", "unverified", "mismatched", "ambiguous"],
)
def test_unverified_identity_prevents_every_data_read(identity: dict[str, object]) -> None:
    transport = FakeTransport(json_response(200, identity))
    adapter = GoogleSearchConsoleAdapter(
        transport,
        BearerTokenProvider(SecretValue("search-secret")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )

    with pytest.raises(ConnectorFailure) as failure:
        adapter.query_analytics(_query_for())

    assert failure.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert len(transport.requests) == 1
    assert "userinfo" in transport.requests[0].url


def test_requested_account_must_match_the_observed_identity() -> None:
    adapter, transport = _adapter()

    with pytest.raises(ConnectorFailure) as failure:
        adapter.query_analytics(_query_for(account_id="other@example.com"))

    assert failure.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert len(transport.requests) == 1


def test_health_reads_identity_and_site_list() -> None:
    adapter, transport = _adapter(json_response(200, {"siteEntry": []}))

    report = adapter.health()

    assert report.account_id == "user@example.com"
    assert transport.requests[1].url.endswith("/webmasters/v3/sites")


def test_sitemaps_normalize_counts_and_keep_empty_empty() -> None:
    adapter, transport = _adapter(
        json_response(
            200,
            {
                "sitemap": [
                    {
                        "path": "https://www.solercia.com.co/sitemap.xml",
                        "lastSubmitted": "2026-11-04T10:00:00.000Z",
                        "isPending": False,
                        "isSitemapsIndex": False,
                        "lastDownloaded": "2026-11-04T11:00:00.000Z",
                        "warnings": "2",
                        "errors": "1",
                        "contents": [{"type": "web", "submitted": "40", "indexed": "0"}],
                    }
                ]
            },
        )
    )

    items = adapter.list_sitemaps(SearchSitemapsQuery(SITE))

    assert "/sites/sc-domain%3Asolercia.com.co/sitemaps" in transport.requests[1].url
    assert items[0].errors == 1
    assert items[0].warnings == 2
    assert items[0].contents[0].submitted == 40
    assert items[0].last_downloaded == datetime(2026, 11, 4, 11, tzinfo=UTC)
    assert not hasattr(items[0].contents[0], "indexed")

    adapter, _ = _adapter(json_response(200, {}))
    assert adapter.list_sitemaps(SearchSitemapsQuery(SITE)) == ()


def test_url_inspection_posts_one_url_and_normalizes_status() -> None:
    url = "https://boki.solercia.com.co/"
    adapter, transport = _adapter(
        json_response(
            200,
            {
                "inspectionResult": {
                    "inspectionResultLink": "https://search.google.com/search-console/inspect",
                    "indexStatusResult": {
                        "verdict": "PASS",
                        "coverageState": "Submitted and indexed",
                        "robotsTxtState": "ALLOWED",
                        "indexingState": "INDEXING_ALLOWED",
                        "pageFetchState": "SUCCESSFUL",
                        "lastCrawlTime": "2026-11-01T12:00:00Z",
                        "googleCanonical": url,
                        "userCanonical": url,
                        "crawledAs": "MOBILE",
                        "sitemap": ["https://boki.solercia.com.co/sitemap.xml"],
                    },
                }
            },
        )
    )

    result = adapter.inspect_url(SearchUrlInspectionQuery(SITE, url))

    request = transport.requests[1]
    assert request.url == "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
    assert request.json_body == {"inspectionUrl": url, "siteUrl": SITE, "languageCode": "es-CO"}
    assert result.verdict == "PASS"
    assert result.coverage_state == "Submitted and indexed"
    assert result.google_canonical == url
    assert result.sitemaps == ("https://boki.solercia.com.co/sitemap.xml",)
    assert result.referring_urls == ()


def test_url_inspection_without_status_fails_closed() -> None:
    adapter, _ = _adapter(json_response(200, {"inspectionResult": {}}))

    with pytest.raises(ConnectorFailure) as failure:
        adapter.inspect_url(SearchUrlInspectionQuery(SITE, "https://www.solercia.com.co/"))

    assert failure.value.reason is ReasonCode.MALFORMED_PROVIDER_DATA


def test_property_and_url_contract_rejects_what_the_api_would_not_recognize() -> None:
    assert validate_search_property(SITE) == SITE
    assert validate_search_property("https://www.solercia.com.co/") == (
        "https://www.solercia.com.co/"
    )
    for value in ("solercia.com.co", "https://www.solercia.com.co", "sc-domain:", "ftp://x/"):
        with pytest.raises(ValueError, match="site_url"):
            validate_search_property(value)

    assert url_belongs_to_search_property("https://boki.solercia.com.co/x", SITE)
    assert url_belongs_to_search_property("https://solercia.com.co/", SITE)
    assert not url_belongs_to_search_property("https://evilsolercia.com.co/", SITE)
    assert not url_belongs_to_search_property("/relative", SITE)
    with pytest.raises(ValueError, match="under site_url"):
        SearchUrlInspectionQuery(SITE, "https://example.com/")

    with pytest.raises(ValueError, match="start_date"):
        _query_for(start_date=date(2026, 11, 1))
    with pytest.raises(ValueError, match="dimensions"):
        _query_for(dimensions=("nope",))
    with pytest.raises(ValueError, match="repeat"):
        _query_for(dimensions=("date", "date"))
    with pytest.raises(ValueError, match="search_type"):
        _query_for(search_type="nope")
    with pytest.raises(ValueError, match="data_state"):
        _query_for(data_state="nope")
    with pytest.raises(ValueError, match="row_limit"):
        _query_for(row_limit=0)


def test_application_builds_typed_queries_from_payloads() -> None:
    analytics = _query(
        ConnectionType.SEARCH_CONSOLE,
        Capability.SEARCH_ANALYTICS_QUERY,
        "user@example.com",
        {
            "site_url": SITE,
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "dimensions": ["date", "page"],
            "row_limit": 100,
        },
    )
    assert isinstance(analytics, SearchAnalyticsQuery)
    assert analytics.dimensions == ("date", "page")
    assert analytics.row_limit == 100

    sitemaps = _query(
        ConnectionType.SEARCH_CONSOLE,
        Capability.SEARCH_SITEMAPS_LIST,
        None,
        {"site_url": SITE},
    )
    assert isinstance(sitemaps, SearchSitemapsQuery)

    inspection = _query(
        ConnectionType.SEARCH_CONSOLE,
        Capability.SEARCH_URLS_INSPECT,
        None,
        {"site_url": SITE, "inspection_url": "https://boki.solercia.com.co/"},
    )
    assert isinstance(inspection, SearchUrlInspectionQuery)

    for broken in (
        {"site_url": SITE, "start_date": "ayer", "end_date": "2026-10-02"},
        {"site_url": SITE, "start_date": "2026-10-01", "end_date": "2026-10-02", "row_limit": True},
        {
            "site_url": SITE,
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "dimensions": "date",
        },
    ):
        with pytest.raises(ValueError):
            _query(ConnectionType.SEARCH_CONSOLE, Capability.SEARCH_ANALYTICS_QUERY, None, broken)


def test_runtime_delivers_an_empty_report_as_a_successful_answer() -> None:
    adapter, _ = _adapter(json_response(200, {}))
    registry = ProviderRegistry()
    registry.register(adapter)

    outcome = ConnectorRuntime(registry).execute(
        ConnectorRequest.create(
            "google.searchconsole", Capability.SEARCH_ANALYTICS_QUERY, _query_for()
        )
    )

    assert outcome.ok is True
    assert outcome.records[0].rows == ()  # type: ignore[union-attr]
    assert outcome.receipt.selected_route is not None


def test_scope_grant_compares_openid_aliases_by_meaning() -> None:
    required = target_spec(ConnectionType.SEARCH_CONSOLE).oauth_scopes

    assert grant_covers_scopes(required, required)
    assert grant_covers_scopes(
        required,
        (
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/webmasters.readonly",
        ),
    )
    assert not grant_covers_scopes(required, ("openid", "email"))


def test_target_requests_only_read_scopes_and_builds_google_oauth(tmp_path: Path) -> None:
    spec = target_spec("search-console")
    assert spec.provider_id == "google.searchconsole"
    assert "https://www.googleapis.com/auth/webmasters.readonly" in spec.oauth_scopes
    assert "https://www.googleapis.com/auth/webmasters" not in spec.oauth_scopes

    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "client",
        }
    )
    provider = oauth_provider_from_settings(
        settings,
        _NoTokens(),
        ConnectionType.SEARCH_CONSOLE,
        "user@example.com",
    )
    assert isinstance(provider, GoogleOAuthProvider)


class _NoTokens:
    def load(self, key: str) -> OAuthTokenSet | None:
        return None

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        return None

    def delete(self, key: str) -> None:
        return None


class _ExpiringTokens(_NoTokens):
    def load(self, key: str) -> OAuthTokenSet | None:
        return OAuthTokenSet(
            SecretValue("expired-access"),
            SecretValue("revoked-refresh"),
            datetime.now(UTC) - timedelta(hours=1),
            (
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/webmasters.readonly",
            ),
            "Bearer",
        )


def _settings(tmp_path: Path):  # type: ignore[no-untyped-def]
    return load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "client",
        }
    )


def _run_payload() -> dict[str, object]:
    return {
        "type": "search-console",
        "account": "user@example.com",
        "capability": "search.sitemaps.list",
        "site_url": SITE,
    }


def test_noninteractive_run_without_a_connection_asks_for_the_canonical_command(
    tmp_path: Path,
) -> None:
    envelope = run_application(
        _run_payload(),
        interactive=False,
        settings=_settings(tmp_path),
        token_store=_NoTokens(),
    )

    assert envelope.status == "action_required"
    assert envelope.error is not None
    assert envelope.error.remediation == (
        "solercia-connect connect search-console user@example.com",
    )


def test_expired_refresh_token_becomes_action_required_without_a_data_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    ProfileStore.from_settings(settings).save(
        ConnectionProfile.create("search-console", "user@example.com", now=NOW)
    )
    transport = FakeTransport(json_response(400, {"error": "invalid_grant"}))
    monkeypatch.setattr("solercia_connect.bootstrap.HttpxTransport", lambda: transport)
    monkeypatch.setattr("solercia_connect.bootstrap.KeyringOAuthTokenStore", _ExpiringTokens)

    envelope = run_application(
        _run_payload(),
        interactive=False,
        settings=settings,
        token_store=_ExpiringTokens(),
    )

    assert envelope.status == "action_required"
    assert envelope.error is not None
    assert envelope.error.remediation == (
        "solercia-connect connect search-console user@example.com",
    )
    assert [request.url for request in transport.requests] == [
        "https://oauth2.googleapis.com/token"
    ]
    assert "revoked-refresh" not in repr(envelope.to_dict())


def test_cli_rejects_a_bad_property_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            "--provider",
            "google.searchconsole",
            "--capability",
            "search.sitemaps.list",
            "--site-url",
            "solercia.com.co",
            "--account",
            "user@example.com",
        ],
    )

    assert '"status": "invalid_input"' in result.output


def test_doctor_reports_prerequisites_for_the_new_target(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    report = DoctorService(settings, ProfileStore.from_settings(settings)).inspect(
        "search-console", "user@example.com"
    )
    rendered = repr(report)

    assert "webmasters.readonly" in rendered or "google.searchconsole" in rendered
    assert "7 days" in rendered
    assert (
        ConnectionProfile.create("search-console", "user@example.com", now=NOW).type
        is ConnectionType.SEARCH_CONSOLE
    )
