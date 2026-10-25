# Solercia Connect standalone architecture

Solercia Connect is an independently installable connector product. Interactive OAuth belongs to
the installed CLI; the loopback FastAPI service is a non-interactive adapter over the same Python
application services, with one explicit local opt-in for the supervised Microsoft browser login.
Skills and docs orchestrate the runtime but contain no provider behavior.

## Request paths

```text
Typer CLI (interactive) -----------+
                                   |
FastAPI loopback (non-interactive, +--> application services
  opt-in browser login)            |
                                          |-- typed settings + doctor
                                          |-- connection lifecycle + OAuth
                                          |-- capability execution
                                          v
                                   ConnectorRuntime + RoutePolicy
                                     | official ProviderAdapter
                                     ` browser fallback (explicit): Microsoft, Google, Jira
                                          v
                              versioned envelope + record(s) + receipt
```

By default HTTP never opens or invokes a browser. The FastAPI boundary rejects a requested browser
route before calling an application service; non-interactive application execution also excludes
the browser adapter before runtime construction. An interactive prerequisite becomes
`action_required` with the exact `solercia-connect connect TYPE ACCOUNT` command.

The opt-in is `SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN=true`, effective only together with
`SOLERCIA_CONNECT_BROWSER_FALLBACK=true` and a configured API key (`http_interactive_allowed`).
With the gate on, `POST /v1/connections/connect` runs `ConnectionService.connect_browser` in a
daemon thread tracked by a process-local `InteractiveSessionRegistry` (one headed browser at a
time, results kept in memory for one hour), `GET /v1/connections/sessions/{id}` reports it, and
`/v1/connections/test` and `/v1/run` accept `allow_browser_fallback` so a browser-route profile
serves reads headless (`run_application(..., browser_via_http=True)`). `connect_browser` is the one
documented deviation from official-first: over HTTP there is no terminal in which to show a device
code, so it goes straight to the browser route. Login and MFA still happen on Microsoft's page
inside the persistent profile; no credential crosses HTTP.

## Boundaries

| Boundary | Owns | Must not own |
| --- | --- | --- |
| Configuration | Explicit env/env-file parsing, target aliases, account validation | Token plaintext, arbitrary cwd `.env` loading |
| Profile store | Atomic private non-secret connection metadata | OAuth tokens, remote revocation |
| OAuth token store | Account/target-qualified secret token state | Provider records or shared database state |
| Connection service | Connect/status/disconnect, identity proof, official-first auth, typed browser handoff | Provider business reads, hidden interaction in HTTP |
| OAuth session | Microsoft bounded device polling; Google state/PKCE loopback correlation | Unbounded wait, raw error/body exposure |
| Domain | Capabilities, queries, DTOs, provenance, receipt vocabulary | HTTP, OAuth, selectors |
| Provider adapters | Vendor endpoints, paging, normalization, pre-read identity | Retry/fallback policy, persistence |
| Runtime | Fixed retries, fallback decision, immutable receipt | Vendor parsing, LLM route selection |
| FastAPI adapter | Typed POST bodies, loopback API-key boundary, HTTP status mapping, opt-in supervised browser sessions | Hidden interaction, credentials in HTTP, provider-specific logic |
| Browser adapter | Dedicated-profile login/MFA/status, transient Outlook session authorization, and identity binding | Persisted tokens, passwords, undocumented extraction claims |

There is no database. Provider clients are semantic integration ports, not CRUD repositories.

## Configuration and profiles

`ConnectionType` establishes ordered targets: Outlook, Teams, Gmail, Google Calendar, Jira, and
GLPI. Account identifiers are validated before factories, files, keyrings, browsers, or network
operations run. Settings come from process environment plus an optional explicit
`SOLERCIA_CONNECT_ENV_FILE`.

Non-secret profile identity is `(type, normalized account)`. The XDG profile store hashes that key
for filenames, uses private directories/files, refuses symlinks, and replaces files atomically.
Profile reads never repair unsafe permissions; the controlled directory must be private and each
profile must be a private, regular, non-symlink file owned by the current user where ownership is
available.
OAuth tokens use the same logical key through `OAuthTokenStore`; local keyring is one adapter.

## OAuth lifecycle

### Microsoft

Outlook and Teams use official device authorization after explicit CLI invocation. Polling is
bounded by attempt count, local maximum delay, and provider expiry. `offline_access` enables
refresh; `User.Read` proves identity. Outlook and Teams have distinct least-privilege scope sets.
Microsoft public device-code clients do not require a client secret.

### Google

Gmail and Google Calendar use separate read-only grants. A single-use HTTP listener binds to
`127.0.0.1`, with finite timeout, exact callback path, one state, one code, strict UTF-8/percent
decoding, bounded request/query sizes, and PKCE S256. The
browser can be opened by the CLI or the URL can be opened manually with `--no-open-browser`.

## Provider contracts

- Outlook: `/me`, Sent Items messages, and `calendarView` with strict end-exclusive ranges.
- Teams: `/me/onlineMeetings/{online-meeting-id}/transcripts` followed by the transcript content
  endpoint. A known meeting ID is required. Any transcript 403 is surfaced as a tenant/admin
  policy boundary; no browser bypass occurs.
- Gmail: profile identity, messages list, and full MIME message reads.
- Google Calendar: primary-calendar identity, expanded events, and acceptance/cancellation filters.
- Jira and GLPI retain their existing API-token/session contracts.

Official references: [Teams transcript listing](https://learn.microsoft.com/en-us/graph/api/onlinemeeting-list-transcripts?view=graph-rest-1.0),
[Microsoft device authorization](https://learn.microsoft.com/en-ie/entra/identity-platform/v2-oauth2-device-code),
[Google loopback redirects](https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration),
[Google Calendar scopes](https://developers.google.com/workspace/calendar/api/auth), and
[FastAPI typed bodies](https://fastapi.tiangolo.com/tutorial/body/).

## Routing and browser status

Official failures are typed before routing. Only `admin_consent_required`,
`official_capability_unavailable`, and `configuration_invalid` can enter browser fallback, only
for a capability on the requesting family's own allowlist, and only after the current CLI
invocation explicitly supplies `--allow-browser-fallback`. Configuration can make the route
available but cannot authorize a call; every browser attempt requires --allow-browser-fallback.
Invalid credentials, identity mismatch, tenant transcript policy, malformed data, and unknown
errors never fall back.

The reasons have one source in `runtime.py`: `RUNTIME_FALLBACK_REASONS`, shared by every family,
and `CONNECT_FALLBACK_REASONS`, the same set without `configuration_invalid`, which `connect`
uses because a missing client ID is detected before OAuth starts and offered the browser route
on its own.

`docs/decisions/2026-10-25-fallback-navegador-google-jira.md` extends the fallback families from
Microsoft to Google and Jira, with one browser package and one profile root per family.
`RoutePolicy.browser_fallback_families` holds the three families, and
`BROWSER_FALLBACK_CAPABILITIES` in `runtime.py` lists what each one may serve:

| Family | Browser capabilities |
| --- | --- |
| Microsoft | `mail.sent.list`, `mail.inbox.list`, `mail.categories.set`, `calendar.events.list`, `meeting.transcripts.list`, `meeting.transcript.get`, `files.search`, `files.get` |
| Google | `mail.sent.list`, `calendar.events.list`, `meeting.transcripts.list`, `meeting.transcript.get` |
| Jira | `external.issues.search` |

A family's route exists only once its browser adapter does (`browser_routes.py`). Today that is
Microsoft alone, so a Google or Jira run that is allowed by policy ends in
`browser_capability_not_implemented`. The decision's D0.1 adds Google `admin_policy_enforced` and
a Jira API token blocked by organization policy as organizational permission not available,
eligible like `admin_consent_required`.

The only write, `mail.categories.set`, adds a second per-invocation gate: the runtime refuses it
with `write_not_authorized` before any adapter runs unless the request carries the CLI's
`--allow-write`, and it never retries a write. The official Graph adapter does not expose it (its
`Mail.ReadWrite` scope would invalidate stored Outlook grants), so it reaches the browser adapter
through `official_capability_unavailable`. There the driver reads the message's categories and
version, writes nothing when the names already match, and otherwise sends one `If-Match` `PATCH`
whose body holds only the category collection. The HTTP service rejects it with 403.

The same per-invocation boundary applies to browser-backed connection status, explicit browser
health, and the legacy `m365 login/status` aliases. Merely loading a browser profile or setting the
global availability variable is never authority to launch Playwright.

The browser adapter can prove a unique account and return `connected_browser`. Outlook identity is
the canonical PascalCase `EmailAddress` from an exact read-only
`GET https://outlook.office.com/api/v2.0/me`; Outlook never enters DOM identity revelation or
attestation. The driver installs context/page request observers before Outlook navigation, observes
a Bearer header only from explicit HTTPS Outlook/Substrate host suffixes, bounds token wait and
probe attempts, keeps the value in non-printable transient storage, and clears it on success,
failure, reopen, and close. Login hosts, HTTP, lookalikes, malformed
headers, oversized tokens, redirects, alternate endpoints, and non-GET probes are rejected. Teams
retains the separate versioned DOM current-account selector strategy.

Built-in `read_records` still returns `browser_capability_not_implemented`; the Outlook identity
probe is not mailbox extraction, and a profile path or session is not an extraction capability.

Browser session persistence is a bounded reopen lifecycle, not a daemon. Connect opens a headed
browser at the endpoint selected by `ConnectionType` (Outlook or Teams), lets the user complete
login/MFA, and applies the provider-specific identity proof above. It then closes the browser to
flush the private account-scoped profile, immediately reopens the same profile headless, and
repeats the same authoritative proof before saving `connected_browser`. Browser-backed status
repeats only the headless open/verify/close phase. The final close releases the profile lock and
the synchronous CLI returns to the console; there is no retained process or unverified background
worker. Missing, malformed, ambiguous, or different identities remain fail-closed.

Doctor and the Playwright driver call the same browser-profile path auditor. It rejects final or
ancestor symlinks and unsafe controlled ownership/modes before creation or browser startup; it
never chmods an existing unsafe path or a symlink target.

## Build boundary

The development lock includes Hatchling and `build`. A fresh `make setup` requires dependency
availability from the package network/cache or a wheelhouse. Once setup succeeds, the subsequent
build is offline with `make build-offline` and no build isolation. A vendored wheelhouse for an
empty-cache setup is not present and remains a hardening item.

## Versioned output

CLI and HTTP share `solercia.connect/v1`; normalized records are wrapped as
`solercia.connect/record/v1`. Internal dataclasses remain provider-neutral. Structured errors carry
safe code/message, retryability, missing configuration names, remediation, and cause category.
Receipts preserve deterministic route evidence. No token, device code, auth header, cookie,
client secret, provider body, or traceback crosses this boundary.
