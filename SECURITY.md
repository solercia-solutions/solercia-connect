# Solercia Connect security boundary

Solercia Connect reads institutional communication and service-management data. It has exactly
four bounded writes: assigning Outlook categories to one message, the status transition of one
Jira issue, sending Gmail in two steps (a draft, then the send of that reviewed draft), and
creating one private Bitbucket repository; see
[Write exception: Outlook categories](#write-exception-outlook-categories),
[Write exception: Jira issue transition](#write-exception-jira-issue-transition),
[Write exception: Gmail draft and send](#write-exception-gmail-draft-and-send), and
[Write exception: Bitbucket repository creation](#write-exception-bitbucket-repository-creation).
Treat OAuth
grants, browser profiles, raw provider payloads, normalized output, and local API access as
sensitive.

## Secret and configuration storage

- Store OAuth access/refresh tokens only through an injected `OAuthTokenStore`, and API tokens only
  through an injected `SecretStore`. The OS keyring is the default backend of both
  (`KeyringOAuthTokenStore`, `KeyringSecretStore`); plaintext token files are forbidden outside
  [project mode](#project-mode).
- Store only non-secret connection metadata under the XDG configuration directory, or under
  `<home>/config` in project mode. Profile directories are `0700`; files are atomic `0600` writes;
  symlinks are rejected. Profile reads never repair unsafe permissions and reject wrong-owner,
  public, symlinked, or non-regular state.
- Load environment files only through the explicit `SOLERCIA_CONNECT_ENV_FILE` path or, in project
  mode, `<home>/connect.env`. Never infer a current-directory `.env`.
- Treat `MS_GRAPH_ACCESS_TOKEN`, `GOOGLE_ACCESS_TOKEN`, and their
  `SOLERCIA_CONNECT_*_ACCESS_TOKEN` aliases as development bootstrap only.
- Never print secret values. Doctor and errors report missing variable names only.

### Project mode

`SOLERCIA_CONNECT_HOME`, accepted in
`docs/decisions/2026-10-25-modo-proyecto-secretos-en-archivo.md`, keeps the configuration,
connection profiles, browser profiles, secrets, and `connect.env` of one project in one folder.
Only in this mode does a file backend replace the OS keyring, behind the same `KeyringBackend`
protocol. Without the variable or the discovery switch below, nothing changes: XDG paths, the OS
keyring, and the same env-file rules apply.

- The value must be an absolute path after `~` expansion, without symlink components; a relative
  path is rejected, so the folder never depends on the current directory.
- The folder and its subfolders are `0700` and owned by the user. Secret files are `0600`, named by
  a SHA-256 digest, and written atomically; symlinked or foreign-owned files are rejected.
- A `.gitignore` containing exactly `*` is created when missing. Any other content fails closed and
  is never overwritten.
- Before any secret write and at connect, when an ancestor of the folder contains `.git`, Connect
  runs a bounded `git ls-files` on the folder. Any tracked path fails closed, listing relative
  paths only; git being unavailable or failing also fails closed. Git is never called outside
  project mode.
- `<home>/connect.env` is the lowest environment layer: the process environment first, then
  `SOLERCIA_CONNECT_ENV_FILE`, then `connect.env`. It must be a private (`0600`) regular file owned
  by the user, never a symlink.
- Discovery is opt-in: only with `SOLERCIA_CONNECT_PROJECT_DISCOVERY` on and no
  `SOLERCIA_CONNECT_HOME` does Connect use `<git root>/.solercia-connect`, and only when it is a real
  directory. The switch comes from the process or `SOLERCIA_CONNECT_ENV_FILE`, never from
  `connect.env`. Before anything inside is read, git must track nothing in the folder and the
  folder must be private, so a folder shipped in a cloned repository fails closed before any of its
  settings load. No git root or no folder means global mode, never the bare current directory.
- Accepted risk: tokens are plaintext on disk, readable by any agent or process with read access to
  that folder, which includes every process running as the same user. The guards keep them out of
  git, not out of backups, archives, or synced folders, so the project must not live in shared or
  cloud-synced storage.

## OAuth and account identity

- Microsoft uses bounded device authorization only after an explicit CLI `connect` invocation.
  Public/native clients do not require a Microsoft client secret.
- Google Desktop OAuth binds a finite callback listener to `127.0.0.1`, generates cryptographic
  state and PKCE S256 values, and accepts exactly one correlated callback/code.
- Jira OAuth uses the user's own Atlassian OAuth 2.0 (3LO) app. Its client ID and secret come only
  from `SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID` and `SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET`, never
  from CLI arguments or HTTP. Atlassian does not support PKCE, so cryptographic state alone
  correlates the callback, on the fixed loopback port of `SOLERCIA_CONNECT_ATLASSIAN_REDIRECT_URI`
  (default `http://127.0.0.1:8766/oauth/callback`), which must equal the registered callback URL.
  The grant must list the profile site exactly once in its accessible resources.
- Refresh-token rotation or preservation occurs in the secure store. Expired grants fail closed
  when refresh is unavailable.
- Observe exactly one canonical provider identity before a data read and compare it
  case-insensitively with the requested/profile account. Missing, ambiguous, or mismatched
  identity produces zero data reads.
- Use least-privilege read scopes: Microsoft `Mail.Read`, `Calendars.Read`, and the separate Teams
  `OnlineMeetingTranscript.Read.All`; Google `gmail.readonly`, `calendar.events.readonly`, and
  `webmasters.readonly` are separate grants; Jira OAuth `read:jira-work`, `read:jira-user`,
  `read:me` and `offline_access`; Bitbucket API token scopes `read:user:bitbucket`,
  `read:workspace:bitbucket`, `read:repository:bitbucket` and `read:pullrequest:bitbucket`.
- The read targets request no write scope. The official route of the category write would need
  `Mail.ReadWrite`, and adding it to the `outlook` target invalidates every existing grant, because
  a stored grant must contain all of the target's scopes. That route stays disabled until a
  separate scope decision. The Jira transition would need `write:jira-work`, which the Atlassian
  OAuth grant does not request, so an OAuth Jira profile cannot write; Jira API tokens can.
- The only write scopes are Google `gmail.compose`, requested solely by the separate
  `gmail-compose` target (`openid`, `email`, `gmail.compose`, token key `gmail-compose:<account>`),
  and Bitbucket `admin:repository:bitbucket`, held solely by the API token of the separate
  `bitbucket-admin` target (token key `bitbucket-admin:<email>`). The `gmail` target keeps
  `gmail.readonly` alone and the `bitbucket` token holds read scopes alone, so no read credential
  can send mail or create a repository.
- Teams transcripts require work/school accounts and tenant permission. Personal accounts and
  Microsoft tenant/admin policy blocks never route around policy. Google and Jira organizational
  blocks follow the [browser fallback](#browser-fallback) rules below.

### Bitbucket Cloud API token

Bitbucket Cloud is official-only, as accepted in
`docs/decisions/2026-10-26-conector-bitbucket.md`, and has no browser route. The `bitbucket` target
is read-only; its one write, the repository creation, runs on the separate `bitbucket-admin` target
described in
[Write exception: Bitbucket repository creation](#write-exception-bitbucket-repository-creation).

- The credential is an Atlassian API token created with "Create API token with scopes" for the
  Bitbucket app, holding exactly `read:user:bitbucket`, `read:workspace:bitbucket`,
  `read:repository:bitbucket` and `read:pullrequest:bitbucket`. A classic, unscoped Jira API token
  does not work. It travels as Basic `email:token` only to `https://api.bitbucket.org/2.0`.
- `connect bitbucket <account> --workspace <workspace>` reads the token from `--token-env`, the
  hidden prompt, or `--token-stdin`, never from an argument or over HTTP. It is stored only in the
  `SecretStore` under `bitbucket:<email>` (keyring service `solercia-connect.api-token`, or the
  private secret file in project mode); the profile keeps the workspace and the observed
  `account_id`, never the token.
- Identity is proven before anything is stored and before every read: `/2.0/user` returns the
  stored `account_id`, `/2.0/user/emails` lists the account email as confirmed, and
  `/2.0/workspaces/{workspace}` answers 200. Any mismatch, a missing or unconfirmed email, or an
  unknown response shape fails closed with zero data reads; a failed connect stores nothing.
- Pagination follows only `next` links on the same origin and collection path, with a fixed page
  bound. Reaching it fails with `result_incomplete` instead of returning a truncated list.
- A `403` may quote the missing scope names, which are not secret; nothing else from a provider
  body is ever quoted.

## Service and output

- The FastAPI adapter binds to loopback by default and rejects non-loopback host configuration.
- HTTP never receives an API token: Jira and Bitbucket connect only in the CLI.
- HTTP never writes: `POST /v1/run` rejects `mail.categories.set`, `external.issues.transition`,
  `mail.draft.create`, `mail.draft.send`, and `scm.repositories.create` with 403 before the
  application service runs, with or without the interactive login opt-in.
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

## Browser fallback

- Browser fallback covers the Microsoft, Google, and Jira families, as accepted in
  `docs/decisions/2026-10-25-fallback-navegador-google-jira.md`. It is explicit, typed, and
  allowlisted. It is never primary.
  Every browser attempt requires --allow-browser-fallback; a true environment setting makes the
  route available but never authorizes an invocation. This includes browser-backed `status`,
  `health microsoft365.browser`, and the `m365 login/status` compatibility commands.
- Google `admin_policy_enforced` and a Jira API token blocked by organization policy count as
  organizational permission not available and may enter the fallback, like Microsoft
  `admin_consent_required`. Microsoft `tenant_policy_blocked` and the Teams transcript policy
  never fall back.
- Use a dedicated persistent profile per family and per account, under separate `m365`, `google`,
  and `jira` roots; a profile is never shared across families. Personal Chrome, Edge, Brave, or
  Chromium profiles are rejected. Keep each directory private and outside shared/cloud-synced
  storage.
- The user performs password entry and MFA. The runtime never asks for or types credentials.
- A uniquely verified browser identity may establish `connected_browser`. Jira identity is the
  `accountId` of `/rest/api/3/myself` equal to the `X-AAccountId` response header, bound to the
  site host; a visible `emailAddress` must match the account, and a blank or `unknown` `accountId`
  fails closed. The Google browser identity is the account named by the Google bar's account
  button in Calendar web, which must match the requested account; until task T5.1 proves that
  probe live, any selector mismatch fails closed with zero reads.
  Built-in browser data extraction is enabled only for capabilities backed by documented read-only
  provider endpoints, plus the [scoped Google exception](#scoped-exception-google-browser-reads).
- Jira browser reads are same-origin requests issued from the site's own page; Connect never reads
  or copies the session cookie.
- For Microsoft, the session authorization captured from the user's own supervised browser session
  may be used in memory to call those endpoints. This is the deliberate purpose of the fallback
  route and is gated twice: the environment must make the route available and every invocation
  must pass `--allow-browser-fallback`. The user supplies their own credentials and MFA.
- That authorization is bound by the following non-negotiable rules:
  - It is held only in a mutable in-memory buffer that is zeroed after use and on close.
  - It is never written to disk, never logged, never serialized into an envelope or receipt, and
    never included in a diagnostic.
  - It is read only from HTTPS requests whose host matches the provider allowlist, and it is sent
    only to endpoints on that same allowlist.
  - It is used only for read-only calls to documented provider endpoints, plus the category
    write below when the same invocation also passes `--allow-write`.
- Do not read `document.cookie`, browser local storage, session storage, or any other long-lived
  credential store. Do not persist cookies or profile contents outside the dedicated profile. Do
  not call retired or undocumented provider endpoints, except the scoped Google exception below.

### Scoped exception: Google browser reads

The same decision accepts one exception to the documented-endpoint rule, for Google only and
read-only. It covers exactly three mechanisms:

- the Google Calendar ICS export;
- for Meet, as made precise on 2026-10-26: the Calendar web day view and event-detail DOM, read
  only for the transcript attachment, and the visible text of the Docs `mobilebasic` view of that
  transcript document. Calendar and Drive internal APIs, any header derived from cookies, and the
  signed export URLs stay out;
- the Gmail sent-mail listing from the DOM of the search `in:sent after: before:`, plus the
  original-message (RFC 822) download.

Selectors and formats are versioned, deterministic, and backed by sanitized fixtures. Any mismatch
fails closed with `browser_selector_mismatch`. The documented-endpoint rule stays in force for
every other case: Microsoft and Jira get no such exception, and no other undocumented Google
mechanism is allowed.

## Write exception: Outlook categories

Assigning categories to an existing Outlook message (`mail.categories.set`) is the first of the
three writes, as accepted in `docs/decisions/2026-10-19-escritura-categorias-outlook.md`. Nothing
else is written to the mailbox: no mail is sent, moved, marked, created, or deleted, and no master
category, folder, calendar, file, or ticket changes.

- Every invocation needs `--allow-write`, off by default. Without it the command fails with
  `write_not_authorized` before any registry, browser, or provider call. No environment variable or
  profile stands in for it, and the browser route still needs `--allow-browser-fallback` as well.
- `--account` is required, and the mailbox identity is proven before the read and the write.
- The only write call is one `PATCH` to one message whose body holds nothing but the category
  collection, sent with `If-Match` set to the version read just before. `412` or `409` fails with
  `write_conflict`; writes are never retried automatically.
- When the computed list holds the same names the message already has, nothing is written.
- HTTP rejects the capability by design; it runs only in the canonical CLI and the library.

## Write exception: Jira issue transition

Moving one existing Jira issue to another status through one workflow transition
(`external.issues.transition`) is the second write, as accepted in
`docs/decisions/2026-10-25-escritura-transiciones-jira.md`. Nothing else is written: no issue is
created, edited, commented, assigned, archived, or deleted, no field is set (not even those of the
transition screen), and no worklog, link, attachment, sprint, or project setting changes.

- Every invocation needs `--allow-write`, off by default. Without it the command fails with
  `write_not_authorized` before any registry or provider call. No environment variable or profile
  stands in for it.
- It runs only over the official Jira Cloud REST API. It is not on the Jira browser allowlist, so
  `--allow-browser-fallback` never opens a browser route for it.
- `--account` is required, and the Jira identity (`/rest/api/3/myself`) is proven before the reads
  and the write.
- `--issue` takes exactly one key matching `^[A-Z][A-Z0-9_]+-[1-9][0-9]*$`; lists, ranges, and JQL
  are refused. A key that Jira resolves to another issue fails closed.
- The only write call is one `POST` to the transitions of that issue whose body is exactly
  `{"transition": {"id": "<id>"}}`. Zero or several transitions reaching the requested status, or a
  transition that requires fields, fail closed before the write. `409` fails with
  `write_conflict`; writes are never retried automatically.
- When the issue already has the requested status, nothing is written.
- The status is read again after the write; a mismatch fails with `write_unverified`, stating the
  write may have applied.
- Classic and scoped API tokens may write. An OAuth profile fails closed with
  `write_scope_missing` before any provider call, because its grant lacks `write:jira-work`; the
  default Atlassian OAuth scopes do not change.
- HTTP rejects the capability by design; it runs only in the canonical CLI and the library.

## Write exception: Gmail draft and send

Sending Gmail in two steps is the third write, as accepted in
`docs/decisions/2026-10-25-envio-correo-gmail-dos-pasos.md`: `mail.draft.create` creates one
plain-text draft, the user reviews it in Gmail > Drafts, and only after the user explicitly
approves that draft id does `mail.draft.send` send it. Nothing else is written: no mail is sent
without a draft (`users.messages.send` is never used), no message is replied to, forwarded, moved,
labeled, or deleted, no draft is edited or deleted, no attachment or HTML body is written, and no
filter, signature, or mailbox setting changes.

- Both capabilities run only on the separate `gmail-compose` target and only over the official
  Gmail API. They are not on the Google browser allowlist, and `--allow-browser-fallback` is
  refused as `invalid_input`.
- Every invocation needs `--allow-write`, off by default. Without it the command fails with
  `write_not_authorized` before any registry or provider call. No environment variable or profile
  stands in for it.
- `--account` is required, and the Gmail identity (`users.getProfile`) is proven before the reads
  and the write. `From` is always the connected account: no alias is accepted when creating, and a
  draft whose `From` is missing or different fails closed when sending.
- Recipients come only from explicit `--to` (at least one), `--cc`, and `--bcc`, at most 20 in
  total, validated and without repeats. The subject is one non-empty line. The body is UTF-8 plain
  text up to 200 KB, read from `--body-file` or `--body-stdin`, never from a command-line argument.
- Unless `--allow-duplicate` is passed, the send first reads Sent of the last 30 days through the
  read-only `gmail` connection of the same account, because `users.messages.list` and
  `users.messages.get` do not accept `gmail.compose`. A sent message with the same normalized
  subject and recipient set fails closed with `duplicate_suspected`, reporting its sent date only;
  a missing read connection fails closed with `duplicate_check_unavailable`.
- Each command sends at most one `POST` (`users.drafts.create` or `users.drafts.send` with the exact
  body `{"id": "<draft>"}`) and never retries it. A `5xx`, a timeout, a network failure, or an
  unreadable answer after the `POST` fails with `write_unverified`, stating the draft may exist or
  the message may have been sent; check Gmail before any new attempt.
- A `gmail-compose` grant without `gmail.compose` fails closed with `write_scope_missing` before any
  provider call; the remediation is `solercia-connect connect gmail-compose <account>`.
- Errors and records never carry the full body or a raw provider body; the draft record carries a
  preview of the first 200 characters only.
- HTTP rejects both capabilities by design; they run only in the canonical CLI and the library.

## Write exception: Bitbucket repository creation

Creating one private, empty git repository in an explicit project of a Bitbucket workspace
(`scm.repositories.create`) is the fourth write, as accepted in
`docs/decisions/2026-10-25-escritura-creacion-repositorios-bitbucket.md`. Nothing else is written:
no repository is deleted, renamed, transferred, or changed, no permission, branch, restriction,
webhook, project, or workspace setting changes, and Connect never pushes; `git` pushes with its own
stored credential.

- It runs only on the separate `bitbucket-admin` target and only over the official Bitbucket Cloud
  API. The read-only `bitbucket` target does not offer it, and `--allow-browser-fallback` is
  refused as `invalid_input`.
- The `bitbucket-admin` credential is its own Atlassian API token with scopes for the Bitbucket app,
  holding exactly `read:user:bitbucket`, `read:workspace:bitbucket`, `read:repository:bitbucket`,
  `read:project:bitbucket`, and `admin:repository:bitbucket`. It is stored only in the
  `SecretStore` under `bitbucket-admin:<email>`, apart from the read token under `bitbucket:<email>`,
  and is read from `--token-env`, the hidden prompt, or `--token-stdin`, never from an argument or
  over HTTP.
- Every invocation needs `--allow-write`, off by default. Without it the command fails with
  `write_not_authorized` before any registry or provider call. No environment variable or profile
  stands in for it.
- `--account` is required, and the identity is proven as for the reads (`/2.0/user`,
  `/2.0/user/emails`, `/2.0/workspaces/{workspace}`) before anything else.
- `--repo` takes one lower-case slug of letters, digits, `.`, `_`, and `-` (never `.`, `..`, a
  slash, a space, or a braced UUID), and `--project` one project key; both are required and the
  project has no default.
- Before the write, `GET /repositories/{workspace}/{repo_slug}` must answer 404: a 200 fails with
  `repository_exists` and nothing is touched. `GET /workspaces/{workspace}/projects/{project_key}`
  must answer 200 with the same key, or the command fails with `project_not_found`.
- The only write call is one `POST /repositories/{workspace}/{repo_slug}` whose body is exactly
  `scm: git`, `is_private: true`, `fork_policy: no_public_forks`, and `project: {key}`. It is never
  retried. A `5xx`, a timeout, a network failure, or an unreadable answer after it fails with
  `write_unverified`, stating the repository may exist; check Bitbucket before any new attempt.
- The answer must carry the same slug, workspace, and project key with `is_private: true`; any
  mismatch fails with `write_unverified`, stating the repository was created and must be reviewed
  in Bitbucket.
- A `400` becomes `request_rejected` with a fixed message, and the clone URL in the record carries
  no user name or password; no raw provider body is ever quoted.
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
