---
name: solercia-connect
description: "Trigger: Outlook, Teams, Gmail, Google Calendar, Jira, GLPI, connector, leer correos, extraer calendario, transcripciones de reuniones. Use the standalone runtime instead of reimplementing integrations."
license: MIT
metadata:
  author: solercia
  version: "2.0"
---

## Activation contract

Load this skill before provider configuration, connection/authentication, data reads, routing, or
Microsoft browser fallback.

This skill is self-contained: it works installed anywhere, and never depends on the repository
being the current working directory.

## Hard rules

- Invoke the `solercia-connect` command; never recreate HTTP, OAuth, callback, normalization,
  retry, selector, output, or fallback logic in an agent prompt or ad hoc script.
- Validate with `solercia-connect doctor TYPE ACCOUNT` before connection or reads.
- Use official APIs first. Browser fallback is Microsoft-only and requires explicit opt-in: every
  browser attempt needs `--allow-browser-fallback` on that invocation. Configuration availability
  alone never authorizes one.
- Never expose tokens, cookies, authorization headers, passwords, client secrets, device codes,
  API keys, raw provider errors, or tracebacks.
- Never use an LLM to choose routes, account identity, retries, or failure classes.
- Keep exact timezone-aware end-exclusive ranges. **Empty remains empty**: if a range returns zero
  records, report zero. Never widen the window to find something.
- Write large reads to a file with `--output`; never pour a month of mail through the context.
- The only write is `mail.categories.set`. Add `--allow-write` only when the user asked for that
  exact change in this conversation; never add it to a read, never run it over HTTP, and never
  retry a `write_conflict` without reading the message again.

## Canonical workflow

```bash
solercia-connect doctor  outlook user@example.com
solercia-connect connect outlook user@example.com
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

## What each route reads

| Capability | Official (Graph/Google) | Browser (supervised session) |
| --- | --- | --- |
| `mail.sent.list` | yes | yes |
| `mail.inbox.list` (Outlook only) | yes | yes |
| `calendar.events.list` | yes | yes |
| `files.search` / `files.get` | yes | yes |
| `meeting.transcripts.list` / `meeting.transcript.get` | yes, needs tenant scopes | yes (Microsoft only) |
| `mail.categories.set` (write) | no, needs a scope decision | yes, with `--allow-write` |

Records are identical across routes; `provenance` is the only field naming the route.

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

**Transcripts.** The official route addresses the online meeting, so it needs `--meeting-id` plus
tenant-granted scopes. The browser route reads the recording that carries the transcript, which is
a drive item, so it takes `--drive-id` with `--item-id`, or just a window that reads every
recording in the period — including meetings the account only attended, not organized. Both routes
answer with speaker-named WebVTT.

**Google Meet.** The `google-meet` target (alias `meet`) is official-only: Calendar event attachments
are discovered with `calendar.events.readonly` and transcript Docs are exported through Drive with
`drive.readonly`. It accepts `--meeting-id` or an exact timezone-aware end-exclusive window. Drive
item selectors are Microsoft-only and fail closed. Records use `text/plain` speaker turns and
`last_cue_seconds` is `None`; no timestamps are synthesized. Empty windows stay empty, and
Playwright is never offered for Google Meet.

## Decision gates

| Need | Action |
| --- | --- |
| Missing readiness | Run `doctor`; report missing variable names and remediation |
| Interactive OAuth or browser login | Run `connect TYPE ACCOUNT`; the user completes the provider interaction |
| Existing read | Run the narrow capability; preserve record schema, provenance and receipt |
| Category change | Only on an explicit user request; pass `--allow-write` for that one run |
| Microsoft admin-consent failure | Use browser only when the user explicitly allowed it |
| Teams tenant policy or personal account | Stop with typed remediation; never bypass |
| An `action_required` envelope | Run the exact remediation command in the CLI; never attempt login through the HTTP service |
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

Configuration lives in an environment file the user points to, never in the repository:

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
