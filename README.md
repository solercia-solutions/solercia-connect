# Solercia Connect

Solercia Connect is a standalone Python 3.12 connector runtime. It owns provider
configuration, OAuth connection lifecycle, official-first reads, normalized records, and
versioned machine-readable receipts. It does not require a database or another Solercia product.

Offline tests prove the local contracts. They do **not** prove real tenant consent, provider
availability, or external browser stability; those remain explicit live-validation gates.

## Quick path

```bash
make setup
make doctor TYPE=outlook ACCOUNT=user@example.com
make connect TYPE=outlook ACCOUNT=user@example.com
make test
```

For a Microsoft browser-only setup, the `service`, `doctor`, and `connect` Make targets explicitly
select the repository's `.env.local` when that file exists. With
`SOLERCIA_CONNECT_BROWSER_FALLBACK=true` in that file and no Microsoft client ID, authorize this
invocation explicitly:

```bash
make setup-browser
make connect TYPE=outlook ACCOUNT=user@example.com ALLOW_BROWSER_FALLBACK=1
```

The official OAuth route is skipped because it is unavailable. After the user completes login and
MFA in the dedicated Solercia profile, the visible Chromium window intentionally closes. It is
used only for login/MFA; cookies and local storage persist in the private account-scoped profile
on disk, not in an always-running browser daemon. Connect immediately reopens that same profile
headless and requires the same unique observed account before the successful envelope reports
`connected_browser`. Outlook proves that identity with the web session's own read-only
`GET https://outlook.office.com/api/v2.0/me` response and canonical `EmailAddress`; it does not
depend on Outlook DOM identity selectors. The captured browser authorization is transient,
accepted only from HTTPS Outlook/Substrate host allowlists, and is never logged or persisted.
Teams keeps its separate versioned DOM current-account proof. After the headless check, Chromium
closes and the synchronous command returns to the console; no daemon or background worker remains:

```json
{"status": "connected_browser", "route": "browser",
 "data": {"extraction_status": "available", "capabilities": ["mail.sent.list"]}}
```

Once connected, sent mail is read over the same supervised session through the documented
read-only Outlook REST collection, with the same strict range and paging as the official
route. The envelope declares what that route can extract, so a caller never has to guess:

```bash
SOLERCIA_CONNECT_ENV_FILE=.env.local \
  solercia-connect run outlook mail.sent.list user@example.com \
  --start 2026-08-01 --end 2026-09-01 --allow-browser-fallback --output sent.json
```

Make never prints the environment file values. Direct CLI use still requires explicitly exporting
`SOLERCIA_CONNECT_ENV_FILE=.env.local`; configuration availability alone never authorizes a browser
attempt. Later status checks also reopen the same profile headless and close it when verification
finishes:

```bash
make connect TYPE=outlook ACCOUNT=user@example.com ALLOW_BROWSER_FALLBACK=1
SOLERCIA_CONNECT_ENV_FILE=.env.local \
  solercia-connect status outlook user@example.com --allow-browser-fallback
```

The installed Typer CLI is the canonical interactive interface:

```bash
solercia-connect connect outlook user@example.com
solercia-connect status outlook user@example.com
solercia-connect disconnect outlook user@example.com
```

Official status checks need no browser flag. If the stored route is browser-backed, status and
the compatibility commands require a fresh explicit authorization:

```bash
solercia-connect status outlook user@example.com --allow-browser-fallback
solercia-connect m365 status --allow-browser-fallback
```

The root wrapper is only a convenience alias and contains no provider logic:

```bash
./solercia-connect.sh outlook user@example.com
./solercia-connect.sh outlook user@example.com --allow-browser-fallback
```

## What each route can read

| Capability | Official (Graph) | Browser (supervised session) |
|---|---|---|
| `mail.sent.list` (with attachments) | yes | yes |
| `calendar.events.list` | yes | yes |
| `files.search` / `files.get` | yes | yes |
| `meeting.transcripts.*` | yes | yes — different selector, see below |

Records are identical across routes; `provenance` is the only field that names the route.

The two routes reach the same transcript by different names. The official route addresses the
online meeting, so it needs a meeting ID. The browser route reads the recording that carries the
transcript, which is a drive item, so it takes `--drive-id` with `--item-id` — or just a window,
which lists every recording in that period and reads each one.

That is also why a meeting the account merely attended is readable: the site cookie covers every
`personal/` path on the drive host, not only this account's, and the shared collection hands back
the organizer's `driveId` and item id directly. Recordings the account owns and recordings shared
with it are both reported, and `include_shared: false` narrows the read to its own.

Both routes answer with speaker-named WebVTT. The drive-backed rendition that carries speakers is
JSON, so it is converted rather than passed through — the WebVTT that surface serves directly has
no speaker markers at all, which collapses consecutive turns into one unattributed block.

Browser file search walks the drive rather than calling the provider's search endpoint, which
answers that surface with an empty collection even for items that exist. The walk is bounded in
depth and in folders visited, and a folder the account cannot read is skipped rather than failing
the search.

## Files on OneDrive and SharePoint

Files are a capability pair on the `onedrive` target, which also answers to `sharepoint`. The
official route reads Microsoft Graph; the supervised browser route reads the equivalent SharePoint
REST drive on whichever host the session actually uses. Both return the same `DriveFile` records,
so the route never leaks into a caller's code:

```bash
solercia-connect run --provider microsoft.graph --capability files.search \
  --account user@example.com --search "acta" --limit 50

solercia-connect run --provider microsoft.graph --capability files.search \
  --account user@example.com --folder "/Documentos/2026"

solercia-connect run --provider microsoft.graph --capability files.search \
  --account user@example.com --site "contoso.sharepoint.com:/sites/calidad"

solercia-connect run --provider microsoft.graph --capability files.get \
  --account user@example.com --item-id 01ITEMID --with-content \
  --max-content-bytes 5242880 --output acta.json
```

`files.get` returns the item with its content base64-encoded. The byte budget is enforced before
the download starts, so an oversized file fails the request instead of the process. A drive
listing is not a date range, so `--start`/`--end` are optional here and filter by last
modification when given.

The browser route keeps mail and drive authorizations separate, because the two audiences are not
interchangeable: a mail token is never reused for a drive call.

## Provider readiness

| Target | Official local contract | Browser route | Live evidence |
| --- | --- | --- | --- |
| Outlook | Device-code OAuth; Graph identity, sent mail, and calendar reads | Session identity via exact read-only Outlook REST `/api/v2.0/me`; extraction remains `not_implemented` | REST identity shape verified for UAECD; Connect live command remains tenant-gated |
| Teams | Device-code OAuth; known-meeting transcript list/download using `OnlineMeetingTranscript.Read.All` | Recording transcripts over the SharePoint drive, own and shared, by item or by period | Drive transcript contract verified live against a work tenant; official route pending admin proof |
| Gmail | Desktop OAuth loopback with state + PKCE; read-only sent-mail capability | None | Pending dedicated account proof |
| Google Calendar | Desktop OAuth loopback with state + PKCE; read-only event capability | None | Pending dedicated account proof |
| Jira | Existing API-token/JQL compatibility | None | Pending supported-site proof |
| GLPI | Existing app/user-token session compatibility | None | Pending supported-installation proof |

Teams personal Microsoft accounts are unsupported. Tenant policy or admin-disabled transcript
access fails with typed remediation and never routes around policy through browser automation.
See the [provider runbooks](docs/runbooks/) before live validation.

## Configuration contract

Solercia Connect reads process environment variables and, only when explicitly selected,
`SOLERCIA_CONNECT_ENV_FILE`. It never silently loads a `.env` from the current directory.

| Variable | Purpose |
| --- | --- |
| `SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID` | Public/native Microsoft application client ID; no client secret is required for device code |
| `SOLERCIA_CONNECT_MICROSOFT_TENANT` | Tenant ID/domain/audience; defaults to `organizations` |
| `SOLERCIA_CONNECT_GOOGLE_CLIENT_ID` | Google Desktop app client ID |
| `SOLERCIA_CONNECT_GOOGLE_CLIENT_SECRET` | Optional when required by the chosen Desktop client configuration |
| `SOLERCIA_CONNECT_ENV_FILE` | Explicit environment file path |
| `SOLERCIA_CONNECT_BROWSER_FALLBACK` | Makes the Microsoft browser route locally available; it never authorizes an invocation |
| `SOLERCIA_CONNECT_M365_PROFILE_DIR` | Explicit root for dedicated Microsoft browser profiles |
| `SOLERCIA_CONNECT_API_HOST` / `PORT` | Loopback service bind; host defaults to `127.0.0.1` |
| `SOLERCIA_CONNECT_API_KEY` | Required local key for POST connection-test/run endpoints |
| `SOLERCIA_CONNECT_OUTPUT_DIR` | Optional default output location for callers |

Non-secret profiles live under the XDG configuration directory with `0700` directories and
`0600` atomic files. OAuth tokens live only behind `OAuthTokenStore`, using the OS keyring by
default. Profile reads never repair unsafe permissions: public, non-regular, wrong-owner, or
symlinked state is rejected with remediation. Legacy raw access-token variables remain
development bootstrap only.

```bash
solercia-connect config validate
solercia-connect doctor teams user@example.com
solercia-connect profile list
```

## Versioned JSON and secure files

CLI and HTTP responses use `solercia.connect/v1` with stable fields:
`ok`, `operation`, `status`, `provider`, `account`, `route`, `data`, `receipt`, and `error`.
Normalized records carry `solercia.connect/record/v1` in the envelope. Errors include a safe code,
retryability, missing variable **names**, remediation, and a cause category—never provider bodies
or credentials.

Relevant CLI commands accept `--output PATH`; writes are atomic, mode `0600`, reject symlinks, and
do not overwrite unless `--force` is explicit. Stdout remains JSON for machine consumption.

## Run a capability

Use timezone-aware, exact, end-exclusive ranges:

```bash
solercia-connect run \
  --provider google.calendar \
  --capability calendar.events.list \
  --start '2026-08-01T00:00:00-05:00' \
  --end '2026-09-01T00:00:00-05:00' \
  --account user@example.com
```

Transcripts accept whichever selector the route can use — a meeting, a recording, or a period:

```bash
# Official route: the online meeting itself.
solercia-connect run \
  --provider microsoft.graph \
  --capability meeting.transcript.get \
  --meeting-id MEETING_ID \
  --account user@example.com

# Browser route: every recording in the period, own and shared.
solercia-connect run \
  --provider microsoft.graph \
  --capability meeting.transcript.get \
  --start '2026-08-01T00:00:00-05:00' \
  --end '2026-09-01T00:00:00-05:00' \
  --account user@example.com \
  --allow-browser-fallback

# Browser route: one known recording.
solercia-connect run \
  --provider microsoft.graph \
  --capability meeting.transcript.get \
  --drive-id DRIVE_ID --item-id ITEM_ID \
  --account user@example.com \
  --allow-browser-fallback
```

## Loopback HTTP adapter

```bash
make service
curl http://127.0.0.1:8765/v1/health
curl -X POST http://127.0.0.1:8765/v1/connections/test \
  -H 'Content-Type: application/json' \
  -H 'X-Solercia-Connect-Key: LOCAL_API_KEY' \
  -d '{"type":"outlook","account":"user@example.com","allow_browser_fallback":false}'
```

HTTP never opens or invokes a browser and never starts login/MFA. When interaction is needed, it returns
`action_required` and the exact CLI command. All extraction POST routes require the local API key.
See [API examples](docs/runbooks/api.md).

## Deterministic routing

1. Validate target/account before side effects.
2. Use the configured official adapter and one account-bound OAuth token set.
3. Retry only typed transient failures within fixed bounds.
4. Fail closed for credentials, identity mismatch, provider policy, malformed data, and unknowns.
5. Consider Microsoft browser fallback only after the invocation supplies
   `--allow-browser-fallback` and an allowlisted capability and reason. The global environment
   switch can make the route available but cannot grant per-invocation authority. Every browser
   attempt requires --allow-browser-fallback. A connected browser session is not an extraction
   claim.

No LLM, randomness, free-text classification, or unbounded wait selects a route.

## Development and verification

```bash
make setup              # core + dev + OAuth + service, from uv.lock
make build-offline      # no network/build isolation after the locked setup
make setup-browser      # explicit Playwright package + Chromium install
make check
make test
make test-live TYPE=gmail ACCOUNT=user@example.com  # explicit opt-in only
```

Default tests are hermetic and skip every live provider contract unless
`SOLERCIA_CONNECT_LIVE=1` and all dedicated-account prerequisites exist. Live token-store
readiness requires a private official profile plus a usable keyring token containing the selected
capability scopes; otherwise the test skips with a secret-safe diagnostic.

A fresh locked setup requires dependency availability through the package network or a supplied
wheelhouse. After `make setup` succeeds, the subsequent build is offline through
`make build-offline`, which uses locked, installed Hatchling/build tooling without build
isolation. A vendored wheelhouse for a truly empty-cache setup remains unchecked hardening; this
repository does not claim a self-contained clean-cache build.

## Documentation

- [Architecture and contracts](docs/ARCHITECTURE.md)
- [Security guidance](SECURITY.md)
- [Checked roadmap](docs/ROADMAP.md)
- [Provider runbooks](docs/runbooks/)
- [Mandatory platform decisions](docs/decisions/2026-08-23-solercia-platform-decisions.md)

## License

Copyright (C) 2026 Cristian Camilo Calderón Tapia.

Solercia Connect is free software: you can redistribute it and/or modify it under the terms
of the GNU Affero General Public License as published by the Free Software Foundation,
version 3 of the License. It is distributed WITHOUT ANY WARRANTY; see [LICENSE](LICENSE)
for details. If you run a modified version as a network service, you must offer its
source code to the users of that service.
