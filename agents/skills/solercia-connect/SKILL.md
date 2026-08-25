---
name: solercia-connect
description: "Trigger: Outlook, Teams, Gmail, Google Calendar, Jira, GLPI, connector. Use the standalone runtime instead of reimplementing integrations."
license: MIT
metadata:
  author: solercia
  version: "1.1"
---

## Activation contract

Load this skill before provider configuration, connection/authentication, data reads, routing, or
Microsoft browser fallback.

## Hard rules

- Invoke `solercia-connect` or import `solercia_connect`; never recreate HTTP, OAuth, callback,
  normalization, retry, selector, output, or fallback logic in an agent prompt or ad hoc script.
- Validate with `solercia-connect doctor TYPE ACCOUNT` before connection or reads.
- Use official APIs first. Browser fallback is Microsoft-only and requires explicit opt-in plus an
  allowlisted typed reason/capability. Every browser attempt requires `--allow-browser-fallback`;
  configuration availability alone never authorizes one invocation.
- Never expose tokens, cookies, authorization headers, passwords, client secrets, device codes,
  API keys, raw provider errors, or tracebacks.
- Never use an LLM to choose routes, account identity, retries, or failure classes.
- A `connected_browser` session is not an extraction claim. Respect `not_implemented`.
- Keep exact timezone-aware end-exclusive ranges. Empty remains empty.

## Canonical workflow

```bash
make setup
solercia-connect doctor outlook user@example.com
solercia-connect connect outlook user@example.com
solercia-connect status outlook user@example.com
solercia-connect run --provider microsoft.graph --capability mail.sent.list \
  --start '2026-08-01T00:00:00-05:00' --end '2026-09-01T00:00:00-05:00' \
  --account user@example.com
```

The convenience wrapper maps only to connect:

```bash
./solercia-connect.sh outlook user@example.com
```

HTTP is non-interactive and never invokes a browser or provider browser adapter. On
`action_required`, execute the exact remediation command in the CLI; do not attempt login through
the service request.

## Decision gates

| Need | Action |
| --- | --- |
| Missing readiness | Run `doctor`; report missing variable names/remediation |
| Interactive OAuth | Run `connect TYPE ACCOUNT`; the user completes provider interaction |
| Existing read | Run the narrow capability and preserve record schema/provenance/receipt |
| Microsoft admin-consent failure | Use browser only when the user explicitly allowed it |
| Browser-backed status/compatibility command | Require `--allow-browser-fallback` for that invocation |
| Teams tenant policy/personal account | Stop with typed remediation; never bypass |
| Browser extraction reports `not_implemented` | Stop; do not improvise scraping |
| Live proof | Use `make test-live TYPE=... ACCOUNT=...` only with explicit opt-in and dedicated accounts |

## Output contract

Return the exact command, `solercia.connect/v1` status, provider/type, account boundary, route,
record count, receipt ID, and safe typed error/remediation. State whether browser fallback was
disabled, denied, attempted, or used.

## References

- `../../../README.md`
- `../../../docs/ARCHITECTURE.md`
- `../../../docs/ROADMAP.md`
- `../../../docs/runbooks/`
- `../../../SECURITY.md`
