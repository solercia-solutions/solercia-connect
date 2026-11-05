---
name: solercia-connect
description: "Trigger: Outlook, Teams, Gmail, Google Calendar, Search Console, Jira, GLPI, connector, leer correos, extraer calendario, transcripciones de reuniones, métricas SEO. Use the standalone runtime instead of reimplementing integrations."
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

- Search Console takes exact calendar days, inclusive at both ends, through `--start-date` and
  `--end-date`, plus `--site-url sc-domain:example.com`. One query answers with one report record;
  an empty range is `rows: []`.

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
| `calendar.events.list` | yes | yes |
| `files.search` / `files.get` | yes | yes |
| `search.analytics.query` / `search.sitemaps.list` / `search.urls.inspect` | yes, Google Search Console | no |
| `meeting.transcripts.list` / `meeting.transcript.get` | yes, needs tenant scopes | yes |

Records are identical across routes; `provenance` is the only field naming the route.

**Transcripts.** The official route addresses the online meeting, so it needs `--meeting-id` plus
tenant-granted scopes. The browser route reads the recording that carries the transcript, which is
a drive item, so it takes `--drive-id` with `--item-id`, or just a window that reads every
recording in the period — including meetings the account only attended, not organized. Both routes
answer with speaker-named WebVTT.

## Decision gates

| Need | Action |
| --- | --- |
| Missing readiness | Run `doctor`; report missing variable names and remediation |
| Interactive OAuth or browser login | Run `connect TYPE ACCOUNT`; the user completes the provider interaction |
| Existing read | Run the narrow capability; preserve record schema, provenance and receipt |
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
