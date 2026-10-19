# Solercia Connect security boundary

Solercia Connect reads institutional communication and service-management data. Its only write is
assigning Outlook categories to one message; see [Write exception](#write-exception-outlook-categories).
Treat OAuth grants, browser profiles, raw provider payloads, normalized output, and local API access
as sensitive.

## Secret and configuration storage

- Store OAuth access/refresh tokens only through an injected `OAuthTokenStore`.
  `KeyringOAuthTokenStore` is the local default; plaintext token files are forbidden.
- Store only non-secret connection metadata under the XDG configuration directory. Profile
  directories are `0700`; files are atomic `0600` writes; symlinks are rejected. Profile reads
  never repair unsafe permissions and reject wrong-owner, public, symlinked, or non-regular state.
- Load environment files only through the explicit `SOLERCIA_CONNECT_ENV_FILE` path. Never infer a
  current-directory `.env`.
- Treat `MS_GRAPH_ACCESS_TOKEN`, `GOOGLE_ACCESS_TOKEN`, and their
  `SOLERCIA_CONNECT_*_ACCESS_TOKEN` aliases as development bootstrap only.
- Never print secret values. Doctor and errors report missing variable names only.

## OAuth and account identity

- Microsoft uses bounded device authorization only after an explicit CLI `connect` invocation.
  Public/native clients do not require a Microsoft client secret.
- Google Desktop OAuth binds a finite callback listener to `127.0.0.1`, generates cryptographic
  state and PKCE S256 values, and accepts exactly one correlated callback/code.
- Refresh-token rotation or preservation occurs in the secure store. Expired grants fail closed
  when refresh is unavailable.
- Observe exactly one canonical provider identity before a data read and compare it
  case-insensitively with the requested/profile account. Missing, ambiguous, or mismatched
  identity produces zero data reads.
- Use least-privilege read scopes: Microsoft `Mail.Read`, `Calendars.Read`, and the separate Teams
  `OnlineMeetingTranscript.Read.All`; Google `gmail.readonly` and
  `calendar.events.readonly` are separate grants.
- No write scope is requested. The official route of the category write would need
  `Mail.ReadWrite`, and adding it to the `outlook` target invalidates every existing grant, because
  a stored grant must contain all of the target's scopes. That route stays disabled until a
  separate scope decision.
- Teams transcripts require work/school accounts and tenant permission. Personal accounts and
  tenant/admin policy blocks never route around policy.

## Service and output

- The FastAPI adapter binds to loopback by default and rejects non-loopback host configuration.
- HTTP never writes: `POST /v1/run` rejects `mail.categories.set` with 403 before the application
  service runs, with or without the interactive login opt-in.
- By default HTTP never opens or invokes a browser, starts interactive OAuth, or waits for
  login/MFA. Browser requests are rejected before application-service, runtime, or provider
  invocation. It returns an `action_required` envelope with the canonical CLI command.
- `SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN=true` is an explicit local opt-in. It is effective only
  together with `SOLERCIA_CONNECT_BROWSER_FALLBACK=true` and a configured API key, so an
  unauthenticated loopback client can never open a headed window. It starts the same supervised
  browser the CLI uses, on the persistent profile of the requested account; password and MFA stay
  on Microsoft's page and never travel over HTTP. One headed browser runs at a time and session
  state lives only in process memory.
- `POST /v1/connections/test`, `POST /v1/run`, `POST /v1/connections/connect`, and
  `GET /v1/connections/sessions/{id}` require `X-Solercia-Connect-Key`; comparison is
  constant-time. Doctor reports when the key is absent.
- CLI/HTTP use `solercia.connect/v1`. Unknown exceptions return safe categories, not tracebacks.
- `--output` writes atomically with mode `0600`, refuses symlinks, and never overwrites unless
  `--force` is explicit.
- Disconnect removes local profile/token state and explicitly reports that provider-side
  revocation was not performed. Revoke remotely in the provider/tenant portal when required.

## Microsoft browser fallback

- Browser fallback is Microsoft-only, explicit, typed, and allowlisted. It is never primary.
  Every browser attempt requires --allow-browser-fallback; a true environment setting makes the
  route available but never authorizes an invocation. This includes browser-backed `status`,
  `health microsoft365.browser`, and the `m365 login/status` compatibility commands.
- Use a dedicated persistent profile; personal Chrome, Edge, Brave, or Chromium profiles are
  rejected. Keep its directory private and outside shared/cloud-synced storage.
- The user performs password entry and MFA. The runtime never asks for or types credentials.
- A uniquely verified browser identity may establish `connected_browser`. Built-in browser data
  extraction is enabled only for capabilities backed by documented read-only provider endpoints.
- The session authorization captured from the user's own supervised browser session may be used
  in memory to call those endpoints. This is the deliberate purpose of the fallback route and is
  gated twice: the environment must make the route available and every invocation must pass
  `--allow-browser-fallback`. The user supplies their own credentials and MFA.
- That authorization is bound by the following non-negotiable rules:
  - It is held only in a mutable in-memory buffer that is zeroed after use and on close.
  - It is never written to disk, never logged, never serialized into an envelope or receipt, and
    never included in a diagnostic.
  - It is read only from HTTPS requests whose host matches the provider allowlist, and it is sent
    only to endpoints on that same allowlist.
  - It is used only for read-only calls to documented provider endpoints, plus the single
    category write below when the same invocation also passes `--allow-write`.
- Do not read `document.cookie`, browser local storage, session storage, or any other long-lived
  credential store. Do not persist cookies or profile contents outside the dedicated profile. Do
  not call retired or undocumented provider endpoints.

## Write exception: Outlook categories

Assigning categories to an existing Outlook message (`mail.categories.set`) is the only write, as
accepted in `docs/decisions/2026-10-19-escritura-categorias-outlook.md`. Nothing else is written:
no mail is sent, moved, marked, created, or deleted, and no master category, folder, calendar,
file, or ticket changes.

- Every invocation needs `--allow-write`, off by default. Without it the command fails with
  `write_not_authorized` before any registry, browser, or provider call. No environment variable or
  profile stands in for it, and the browser route still needs `--allow-browser-fallback` as well.
- `--account` is required, and the mailbox identity is proven before the read and the write.
- The only write call is one `PATCH` to one message whose body holds nothing but the category
  collection, sent with `If-Match` set to the version read just before. `412` or `409` fails with
  `write_conflict`; writes are never retried automatically.
- When the computed list holds the same names the message already has, nothing is written.
- HTTP rejects the capability by design; it runs only in the canonical CLI and the library.

## Never log or serialize

- authorization, app-token, session-token, or cookie values;
- access tokens, refresh tokens, device codes, client secrets, passwords, or API keys;
- raw provider error bodies or full tracebacks;
- unsanitized live fixtures or institutional account names.

Safe diagnostics may include provider/capability IDs, account boundary selected by the caller,
missing configuration names, attempt counts, stable reason codes, receipt IDs, and bounded
remediation.

## Release gates

Run hermetic tests, branch coverage, Ruff, mypy, locked setup followed by the offline/no-isolation
build, fresh-wheel smoke, API
startup/readiness smoke, shell checks, gitleaks, a source secret-pattern scan, and the final
manifest comparison. A fresh setup still needs dependency network/cache availability or a
wheelhouse; the subsequent build is offline. A vendored wheelhouse is not currently shipped.
Live suites are separate, opt-in, and require live token-store readiness.

## Reporting a vulnerability

Use the deployment's private security channel. Include sanitized reproduction steps and stable
reason codes only—never credentials, tenant names, provider content, browser profiles, or raw
traces.
