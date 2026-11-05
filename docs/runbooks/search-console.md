# Connect Google Search Console

Search Console uses a separate Desktop OAuth profile and the read-only `webmasters.readonly` scope,
plus `openid email` to prove identity. It does not reuse or silently broaden a Gmail or Calendar
grant, and it never uses a browser route.

## Prepare Google Cloud (once)

1. In the Google Cloud project that owns the Desktop OAuth client, enable the **Google Search
   Console API**.
2. In **Google Auth Platform → Data Access**, add `.../auth/webmasters.readonly`, `openid` and
   `.../auth/userinfo.email`.
3. In **Google Auth Platform → Audience**, publish the app to **In production** before relying on
   unattended runs (see below).
4. The Google account you connect must have access to the property in Search Console.

### Why the consent screen must be published for unattended use

While the OAuth consent screen is external and in **Testing**, the refresh token stops working
after seven days, so a daily job would break every week.

> "A Google Cloud Platform project with an OAuth consent screen configured for an external user type
> and a publishing status of "Testing" is issued a refresh token expiring in 7 days, unless the only
> OAuth scopes requested are a subset of name, email address, and user profile"
> — [Using OAuth 2.0 to Access Google APIs, "Refresh token expiration"](https://developers.google.com/identity/protocols/oauth2#expiration)

> "Authorizations by a test user will expire seven days from the time of consent."
> — [Manage App Audience, "Publishing status"](https://support.google.com/cloud/answer/15549945)

`webmasters.readonly` is outside that exception, so it applies here. In practice this means: publish
the app. Whether Google asks for verification of this scope, and what the consent screen warns
while it is unverified, is shown by the console for your project; check it there rather than assuming.

The same page lists other reasons a refresh token stops working, among them a revoked grant and six
months without use. Any of them surfaces as `action_required` with the exact `connect` command.

## Connect

```bash
export SOLERCIA_CONNECT_GOOGLE_CLIENT_ID='DESKTOP_CLIENT_ID'
make doctor TYPE=search-console ACCOUNT=user@example.com
solercia-connect connect search-console user@example.com
solercia-connect status search-console user@example.com
```

The CLI owns the loopback listener, cryptographic state, PKCE verifier/challenge and finite timeout.
Use `--no-open-browser` to open the official URL manually.

Search Console has no user-profile endpoint. Identity comes from the OpenID Connect `userinfo`
endpoint, whose address is the `userinfo_endpoint` value in
<https://accounts.google.com/.well-known/openid-configuration>. A missing, unverified, ambiguous, or
different address fails closed before any data read.

## Reads

The property goes in `--site-url` exactly as Search Console names it. A Domain property is
`sc-domain:example.com`; a URL-prefix property is an absolute URL with its trailing slash. The API
takes it as one path segment, so the connector percent-encodes it. Do not encode it yourself.

Dates are calendar days in `America/Los_Angeles`, **inclusive at both ends**, as the API defines
them. This is the one capability family that does not use an end-exclusive window, because those
values are days and not instants.

```bash
solercia-connect run --provider google.searchconsole --capability search.analytics.query \
  --site-url sc-domain:example.com --start-date 2026-10-01 --end-date 2026-10-31 \
  --dimension date --dimension query --dimension page --dimension device --dimension country \
  --row-limit 100000 --account user@example.com --output analytics.json

solercia-connect run --provider google.searchconsole --capability search.sitemaps.list \
  --site-url sc-domain:example.com --account user@example.com --output sitemaps.json

solercia-connect run --provider google.searchconsole --capability search.urls.inspect \
  --site-url sc-domain:example.com --url https://www.example.com/ \
  --account user@example.com --output inspection.json
```

### Reading a `search.analytics.query` report

A query always returns exactly one `SearchAnalyticsReport` record.

- **Empty stays empty.** No data means `rows: []`, never a widened range.
- `first_day_with_data` and `last_day_with_data` come only from the returned rows, and are `null`
  when the query did not group by `date`. To learn the first available day, ask for a range that
  reaches back as far as the property's history and group by `date`.
- Google omits days without data, so a missing day is not proof of a pipeline failure, and a day
  after the last final day is unknown, not zero.
- `first_incomplete_date` is reported only for `--data-state all` grouped by `date`, when the range
  contains incomplete days. Everything on or after it may still change.
- `truncated: true` means `--row-limit` was reached and more rows may exist.
- Google "does not guarantee to return all data rows but rather top ones", and grouping by query
  leaves out anonymized queries, so totals from a query-level report can undercount. Read totals
  from a report grouped without `query`.

## Limits

URL Inspection: 2000 calls per day and 600 per minute per site
([Usage Limits](https://developers.google.com/webmaster-tools/limits)). `search.urls.inspect` reads
one URL per call; bounding the daily sample is the caller's job.

## Live proof

```bash
export SOLERCIA_CONNECT_LIVE=1
export SOLERCIA_CONNECT_LIVE_TYPE=search-console
export SOLERCIA_CONNECT_LIVE_ACCOUNT=user@example.com
export SOLERCIA_CONNECT_LIVE_SITE_URL=sc-domain:example.com
make test-live TYPE=search-console ACCOUNT=user@example.com
```

References: [Search Analytics query](https://developers.google.com/webmaster-tools/v1/searchanalytics/query),
[URL Inspection](https://developers.google.com/webmaster-tools/v1/urlInspection.index/inspect),
[Sitemaps list](https://developers.google.com/webmaster-tools/v1/sitemaps/list).
