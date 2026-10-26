# Jira runbook

Jira Cloud is read and written through the official REST API v3 only, with one connection per
account and site. The decisions behind it are
[the platform record](../decisions/2026-08-23-solercia-platform-decisions.md) and
[the transition write](../decisions/2026-10-25-escritura-transiciones-jira.md).

## Connect

Run `doctor` first, then connect once per account. The token is never an argument and is never
printed.

```bash
solercia-connect doctor jira user@example.com

# Classic API token (default): calls the site itself.
solercia-connect connect jira user@example.com \
  --site https://your-site.atlassian.net --token-env JIRA_API_TOKEN

# Scoped API token: calls api.atlassian.com/ex/jira/{cloudId}; a site outside atlassian.net
# also needs --cloud-id.
solercia-connect connect jira user@example.com \
  --site https://your-site.atlassian.net --token-kind scoped --token-env JIRA_API_TOKEN

# OAuth 2.0 (3LO): authorizes the SOLERCIA_CONNECT_ATLASSIAN_* app in the browser; no token.
solercia-connect connect jira user@example.com \
  --site https://your-site.atlassian.net --token-kind oauth

solercia-connect status jira user@example.com
```

- `--token-env NAME` reads the API token from variable `NAME` of the process environment,
  `SOLERCIA_CONNECT_ENV_FILE`, or the project `connect.env`. Without it the CLI asks for the token
  on a hidden prompt, or reads one stdin line with `--token-stdin`.
- Connect proves the identity with `GET /rest/api/3/myself` and stores the opaque `accountId`.
  Every later run proves it again before any data call; another account fails closed with
  `authorization_boundary_violation`.
- OAuth requests read scopes only (`read:jira-work`, `read:jira-user`, `read:me`,
  `offline_access`). An OAuth connection can read but cannot transition issues; see below.

## Search issues

```bash
solercia-connect run --provider jira.cloud --capability external.issues.search \
  --account user@example.com \
  --start '2026-08-01T00:00:00-05:00' --end '2026-09-01T00:00:00-05:00' \
  --output issues.json
```

Without `--jql` it lists the account's issues resolved in the end-exclusive window; `--assignee`
changes whose issues that default query reads. `--jql` replaces the query, but the records are
still limited to issues resolved inside the window.

## List open issues

```bash
solercia-connect run --provider jira.cloud --capability external.issues.open.list \
  --account user@example.com --project CP --sprint current --limit 50
```

- It reads, with no date window, the issues assigned to the connected account whose status
  category is not Done. The default JQL is deterministic:
  `assignee = currentUser() AND statusCategory != Done`, then `project = "<KEY>"` when `--project`
  is given, then `sprint in openSprints()` when `--sprint current` is given, and always
  `ORDER BY updated DESC`.
- `--project` takes one project key matching `^[A-Z][A-Z0-9_]+$`; anything else is refused with
  `invalid_input`. `--sprint` accepts only `current`. `--assignee`, `--start`, and `--end` do not
  apply and are refused.
- `--jql` replaces the whole default filter, including the assignee and the open-only condition.
  It cannot be blank and cannot be combined with `--project` or `--sprint`.
- It uses the same `POST /rest/api/3/search/jql`, the same 20-page bound, and the same `/myself`
  identity proof as the search. `--limit` is capped at 100.
- It requests only `summary`, `status`, `issuetype`, `priority`, `updated`, and `assignee`. Each
  record carries `key`, `summary`, `status`, `status_category` (the stable key, such as `new` or
  `indeterminate`), `issue_type`, `priority`, `updated_at`, `assignee_account_id`, and
  `provenance`.
- Official route only: `--allow-browser-fallback` never opens a browser route for it.

## List the transitions of one issue

```bash
solercia-connect run --provider jira.cloud --capability external.issues.transitions.list \
  --account user@example.com --issue CP-1356
```

- `--issue` takes exactly one key matching `^[A-Z][A-Z0-9_]+-[1-9][0-9]*$`. Lists, ranges, and
  JQL are refused with `invalid_input`. No window is needed.
- It reads the current status (`GET /rest/api/3/issue/{key}?fields=status`) and the transitions
  with their screen fields (`GET /rest/api/3/issue/{key}/transitions?expand=transitions.fields`).
- Each record carries `transition_id`, `transition_name`, `to_status`, `to_status_category`,
  `requires_fields`, `current_status`, and `current_status_category`. An account without the
  _Transition issues_ permission gets zero records.
- `issue_not_found` means Jira answered 404, which covers both a missing issue and one the account
  may not browse, or that Jira resolved the key to another, moved issue.

## Transition one issue (a bounded write)

`external.issues.transition` moves one issue to another status through one workflow transition.
It is the second exception to the read-only rule, after the Outlook categories.

```bash
solercia-connect run --provider jira.cloud --capability external.issues.transition \
  --account user@example.com --issue CP-1356 --to "En curso" --allow-write
```

1. `--allow-write` is required on every invocation and is off by default; without it the command
   fails with `write_not_authorized` before any provider call. `--account` is required.
2. The identity is proven, then the current status is read. When it already equals `--to`
   (trimmed, without case), nothing is written and the record says `changed: false`.
3. Otherwise exactly one available transition must lead to a status named `--to`. Zero matches
   fail with `transition_unavailable`, listing the available target statuses; several fail with
   `transition_ambiguous`; one that requires fields fails with `transition_requires_fields`.
4. One `POST /rest/api/3/issue/{key}/transitions` sends exactly `{"transition": {"id": "<id>"}}`.
   `409` fails with `write_conflict`. A failed write is never retried; run the command again and
   it reads the state afresh.
5. The status is read back. If it is not `--to`, or cannot be read, the command fails with
   `write_unverified`: the write may have applied, so list the transitions again before acting.

The record holds `issue_key`, `previous_status`, `status`, `transition_id`, `transition_name`, and
`changed`; the envelope carries the receipt.

Limits that do not bend:

- Official route only. `--allow-browser-fallback` never opens a browser route for transitions.
- The HTTP service rejects the write with 403; it runs only in the CLI and the library.
- Classic and scoped API tokens can write. An OAuth connection fails with `write_scope_missing`
  before any call, because its grant lacks `write:jira-work`; reconnect with an API token to
  transition issues.
- One issue per invocation. Agents transition only on an explicit request and never loop over
  issues the user did not name one by one.

## Project mode

With `SOLERCIA_CONNECT_HOME=<project>/.solercia-connect`, or with
`SOLERCIA_CONNECT_PROJECT_DISCOVERY` on inside a repository that has that folder, the Jira
profile, the API token, and `connect.env` live in the project's private, git-ignored folder. The
commands above do not change. Put the token in `connect.env` under the name you pass to
`--token-env`, then run `connect` once; `doctor jira ACCOUNT` reports `project_mode`.
