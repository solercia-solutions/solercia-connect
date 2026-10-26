---
name: solercia-connect
description: "Trigger: Outlook, Teams, Gmail, Google Calendar, Jira, GLPI, connector, leer correos, extraer calendario, transcripciones de reuniones. Use the standalone runtime instead of reimplementing integrations."
license: MIT
metadata:
  author: solercia
  version: "2.1"
---

## Activation contract

Load this skill before provider configuration, connection/authentication, data reads, routing, or
browser fallback.

This skill is self-contained: it works installed anywhere, and never depends on the repository
being the current working directory.

## Hard rules

- Invoke the `solercia-connect` command; never recreate HTTP, OAuth, callback, normalization,
  retry, selector, output, or fallback logic in an agent prompt or ad hoc script. Call it by name,
  never through a per-project launcher or an absolute path to a binary.
- Validate with `solercia-connect doctor TYPE ACCOUNT` before connection or reads.
- Never read, print, or copy `connect.env`, `secrets/`, or `data/` of a project folder
  (`.solercia-connect`). Never attempt a provider login on the user's behalf, and never ask for
  passwords or tokens in chat.
- Use official APIs first. Browser fallback requires explicit opt-in: every browser attempt needs
  `--allow-browser-fallback` on that invocation. Configuration availability alone never authorizes
  one. Today it serves Microsoft only; the Google and Jira browser routes are accepted in the
  2026-10-25 ADR and are still being implemented, so never promise or attempt them yet.
- Never expose tokens, cookies, authorization headers, passwords, client secrets, device codes,
  API keys, raw provider errors, or tracebacks.
- Never use an LLM to choose routes, account identity, retries, or failure classes.
- Keep exact timezone-aware end-exclusive ranges. **Empty remains empty**: if a range returns zero
  records, report zero. Never widen the window to find something.
- Write large reads to a file with `--output`; never pour a month of mail through the context.
- There are exactly two writes: `mail.categories.set` (the categories of one Outlook message) and
  `external.issues.transition` (the status of one Jira issue). Add `--allow-write` only when the
  user asked for that exact change in this conversation; never add it to a read, never run it over
  HTTP, and never retry a `write_conflict` without reading the message or issue again.
- Transition a Jira issue only when the user explicitly asks for it, one issue per invocation.
  Never loop over issues in bulk unless the user listed each key. When unsure of the exact target
  status name, run `external.issues.transitions.list` for that issue first.

## Canonical workflow

```bash
solercia-connect doctor  outlook user@example.com
solercia-connect connect outlook user@example.com   # the user runs it: it opens the login
solercia-connect status  outlook user@example.com

solercia-connect run \
  --provider microsoft.graph \
  --capability mail.sent.list \
  --start '2026-08-01T00:00:00-05:00' \
  --end   '2026-09-01T00:00:00-05:00' \
  --account user@example.com \
  --output correos.json
```

Add `--allow-browser-fallback` to every command when the account is connected over the supervised
browser session rather than official OAuth.

**Connecting Jira.** `connect jira ACCOUNT` takes:

- `--site`: the Jira site URL, such as `https://your-site.atlassian.net`.
- `--token-kind classic|scoped|oauth`: `classic` (default) and `scoped` are API tokens; a scoped
  token outside `atlassian.net` also needs `--cloud-id`. `oauth` authorizes the configured
  Atlassian app in the browser and takes no token.
- `--token-env NAME`: reads the API token from variable `NAME` of the process environment,
  `SOLERCIA_CONNECT_ENV_FILE` or the project `connect.env`. The token is never an argument.

```bash
solercia-connect connect jira user@example.com \
  --site https://your-site.atlassian.net --token-kind classic --token-env JIRA_API_TOKEN
```

## Project mode

Project mode keeps one project's connections, browser sessions, secrets and `connect.env` in its
own private, git-ignored `.solercia-connect` folder. It is transparent: when the user enabled
`SOLERCIA_CONNECT_PROJECT_DISCOVERY` (once, in the shell profile) or set `SOLERCIA_CONNECT_HOME`,
every `solercia-connect …` command run inside the repository uses that folder. The commands do not
change and never go through a per-project launcher; with discovery on, nothing is exported.

To set up a project:

1. Run `solercia-connect project init` from inside the repository. It creates
   `<git root>/.solercia-connect` and a `connect.env` holding commented variable names and no
   values; it never overwrites an existing one. Its JSON `next_steps` say what remains, such as
   enabling discovery.
2. Ask the user to fill `connect.env` in their own editor.
3. Ask the user to run each `connect` command once; the user completes the provider interaction.

`doctor` reports `project_mode` and `project_home_source` (`explicit` or `discovered`). Without
either variable, Connect stays in its global mode. A `configuration_invalid` error naming
`SOLERCIA_CONNECT_HOME` or `.solercia-connect`, for example because git tracks a file inside it,
is a stop: relay its remediation to the user. If `project init` is an unknown command, the
installed runtime is older than this skill; ask the user to reinstall it.

## What each route reads

| Capability | Official (Graph/Google) | Browser (supervised session) |
| --- | --- | --- |
| `mail.sent.list` | yes | yes |
| `mail.inbox.list` (Outlook only) | yes | yes |
| `calendar.events.list` | yes | yes |
| `files.search` / `files.get` | yes | yes |
| `meeting.transcripts.list` / `meeting.transcript.get` | yes, needs tenant scopes | yes (Microsoft only) |
| `mail.categories.set` (write) | no, needs a scope decision | yes, with `--allow-write` |
| `external.issues.transitions.list` (Jira) | yes | no |
| `external.issues.transition` (Jira write) | yes, with `--allow-write`, API token only | no |

The browser column describes Microsoft. Google (Gmail, Calendar, Meet) and Jira browser routes are
being implemented per the 2026-10-25 ADR; until they ship, those providers are official-only.

Records are identical across routes; `provenance` is the only field naming the route.
Gmail currently offers `mail.sent.list` only.

**Outlook categories.** Every Outlook mail record carries `categories`, the category names on the
message (empty list when none). Read a case's state from it; do not infer state from the subject.
There is no capability to list the master categories.

**Assigning categories.** `mail.categories.set` takes `--message-id` (the record's `id` field, not
`internet_message_id`), repeatable `--add`/`--remove` or `--replace`, `--account`, and
`--allow-write`; over the browser route it also needs `--allow-browser-fallback`:

```bash
solercia-connect run --provider microsoft.graph --capability mail.categories.set \
  --account user@example.com --message-id '<record id>' \
  --add 'Resuelto' --remove 'Pendiente' --allow-write --allow-browser-fallback
```

It reads the current list, writes only when the names change (`changed: false` otherwise), and
sends one `PATCH` of the categories conditioned on the version read. `write_conflict` means the team
changed the message meanwhile: read it again before deciding. `invalid_input` on the id usually
means the message moved folders; read the folder again for the current id.

**Changing a Jira issue status.** Only when the user explicitly asks, one issue per run. List the
transitions first when unsure of the exact target status name, then transition with `--issue`,
`--to`, `--account`, and `--allow-write` (official route only, never `--allow-browser-fallback`):

```bash
solercia-connect run --provider jira.cloud --capability external.issues.transitions.list \
  --account user@example.com --issue CP-1356
solercia-connect run --provider jira.cloud --capability external.issues.transition \
  --account user@example.com --issue CP-1356 --to "En curso" --allow-write
```

`--issue` takes one exact key; never pass lists or JQL, and never loop over issues the user did not
name one by one. `changed: false` means the issue already had that status. `transition_unavailable`
lists the reachable statuses: report them and ask, do not guess. `transition_ambiguous` and
`transition_requires_fields` are stops. `write_unverified` means the write may have applied: list
the transitions again and report before any retry. `write_scope_missing` means the account is
connected over OAuth: ask the user to reconnect it with an API token (`--token-env`).

**Transcripts.** The official route addresses the online meeting, so it needs `--meeting-id` plus
tenant-granted scopes. The browser route reads the recording that carries the transcript, which is
a drive item, so it takes `--drive-id` with `--item-id`, or just a window that reads every
recording in the period — including meetings the account only attended, not organized. Both routes
answer with speaker-named WebVTT.

**Google Meet.** The `google-meet` target (alias `meet`) is official-only: Calendar event attachments
are discovered with `calendar.events.readonly` and transcript Docs are exported through Drive with
`drive.readonly`. It accepts `--meeting-id` or an exact timezone-aware end-exclusive window. Drive
item selectors are Microsoft-only and fail closed. Records use `text/plain` speaker turns and
`last_cue_seconds` is `None`; no timestamps are synthesized. Empty windows stay empty. A Google Meet
browser route (Docs export to plain text) is accepted in the 2026-10-25 ADR but not implemented
yet; until it ships, Playwright is not offered for Google Meet.

## Decision gates

| Need | Action |
| --- | --- |
| Missing readiness | Run `doctor`; report missing variable names and remediation |
| Interactive OAuth or browser login | Ask the user to run `connect TYPE ACCOUNT`; the user completes the provider interaction |
| Existing read | Run the narrow capability; preserve record schema, provenance and receipt |
| Category change | Only on an explicit user request; pass `--allow-write` for that one run |
| Jira status change | Only on an explicit user request, one issue key per run with `--allow-write`; list the transitions first when the target name is uncertain |
| Microsoft admin-consent failure | Use browser only when the user explicitly allowed it |
| Google `admin_policy_enforced` or Jira API token blocked by the organization | Report the typed error; the browser route for these families is still being implemented |
| Teams tenant policy or personal account | Stop with typed remediation; never bypass |
| An `action_required` envelope | Stop and ask the user to run the exact `connect` command from its remediation; never attempt the login yourself or through the HTTP service, and never ask for passwords or tokens in chat |
| Zero records | Report zero. Do not widen the range, and do not improvise scraping |
| Live proof | Only with explicit opt-in and a dedicated account |

## Output contract

Return the exact command, the `solercia.connect/v1` status, provider/type, account boundary,
route, record count, receipt ID, and the safe typed error with its remediation. State whether the
browser fallback was disabled, denied, attempted, or used.

## Installing the runtime

```bash
uv tool install solercia-connect        # or: uv tool install <path-to-clone>
solercia-connect --help
```

After the clone is updated, the user reinstalls with `uv tool install --reinstall <path-to-clone>`
so the CLI gains new commands such as `project init`.

Global configuration lives in an environment file the user points to, never in the repository; in
project mode it lives in the git-ignored `.solercia-connect/connect.env` instead:

```bash
export SOLERCIA_CONNECT_ENV_FILE=~/.solercia/connect.env
```

For the browser route that file needs `SOLERCIA_CONNECT_BROWSER_FALLBACK=true`. For the official
Microsoft route it needs `SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID` from a public/native app
registration — which the browser route does not require at all.

## Reference

Full documentation lives in the runtime's own repository: `README.md` for the capability matrix,
`docs/runbooks/` for one guide per provider, `docs/ARCHITECTURE.md` for the route contract, and
`SECURITY.md` for the supervised-session policy. Read them from the clone when it is available;
never guess a command that `solercia-connect --help` can confirm.
