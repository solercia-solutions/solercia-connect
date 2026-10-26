# Connect Outlook

Use official Microsoft Graph device authorization first. The browser route is an explicit session
fallback for when the tenant has not granted the delegated scopes; it reads mail, calendar and
drive files over the supervised session, and Teams recordings carry their transcripts.

## Prepare

```bash
export SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID='PUBLIC_NATIVE_CLIENT_ID'
export SOLERCIA_CONNECT_MICROSOFT_TENANT='organizations'
make doctor TYPE=outlook ACCOUNT=user@example.com
```

Microsoft public/native device-code clients do not require a client secret. Request the device
and user code only by invoking connect:

```bash
solercia-connect connect outlook user@example.com
solercia-connect status outlook user@example.com
```

Open the verification URL, enter the displayed user code, and complete consent/MFA. Polling is
bounded by attempts, local delay, and expiry. The connection is saved only after Graph observes
`user@example.com` as the unique identity.

Microsoft device authorization reference:
<https://learn.microsoft.com/en-ie/entra/identity-platform/v2-oauth2-device-code>.

## Explicit browser fallback

Use this only after an allowlisted admin-consent/capability failure:

```bash
make setup-browser
solercia-connect connect outlook user@example.com --allow-browser-fallback
```

The user performs login/MFA in the dedicated profile. `connected_browser` means the account-bound
session was verified. The envelope's `extraction_status` and `capabilities` fields are
authoritative about what that route can read: `outlook` reports `mail.sent.list`,
`mail.inbox.list` and `calendar.events.list`, and `onedrive` reports `files.search`/`files.get`
plus `meeting.transcripts.*`.

Transcripts answer to the `onedrive` target rather than to `teams`, because a transcript is a
drive item hanging off the recording. That is also why a meeting the account merely attended is
readable: the site cookie covers the organizer's drive too. The `teams` target itself extracts
nothing over this route — the Teams client degrades under automation.

```bash
# Every recording in a period, own and shared, with speaker-named WebVTT.
solercia-connect run \
  --provider microsoft.graph \
  --capability meeting.transcript.get \
  --start '2026-08-01T00:00:00-05:00' \
  --end   '2026-09-01T00:00:00-05:00' \
  --account user@example.com \
  --allow-browser-fallback \
  --output transcripts.json
```

By default the loopback service does not offer this route: interactive authorization stays in the
CLI, so a connector reading through the local API needs an account connected over the official
route. With `SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN=true` (plus `SOLERCIA_CONNECT_BROWSER_FALLBACK=true`
and the API key) the service can start the same supervised browser through
`POST /v1/connections/connect` and then serve these reads over the saved profile with
`"allow_browser_fallback":true`; see the [API runbook](api.md#interactive-login-opt-in).

## Read the inbox

`mail.inbox.list` dispatches over the official route first; only an explicit
`--allow-browser-fallback` retries it over the supervised session after an allowlisted official
failure. The two routes normalize to identical records: same fields, ascending order by receipt
instant, only `provenance` names which route answered.

Each inbox and sent record carries `categories`, the Outlook category names on the message in
provider order (an empty list when it has none). Teams that track a case's state with categories
read it from that field; a malformed value fails closed instead of reporting no categories. The
master category list is not offered by either route; assigning categories is the write below.

```bash
solercia-connect run \
  --provider microsoft.graph \
  --capability mail.inbox.list \
  --account cccalderon@sdmujer.gov.co \
  --start 2026-06-01T00:00:00-05:00 \
  --end 2026-07-01T00:00:00-05:00 \
  --limit 1000 \
  --allow-browser-fallback \
  --output bandeja-junio.json
```

**Mandatory guidance:** if the returned record count equals `--limit`, the read may have been
truncated — narrow the window and read again. The ascending order guarantees the returned records
are the oldest prefix of the window, so narrowing to a later start resumes deterministically.
**Empty stays empty:** zero records is reported as zero, never as a reason to widen the window.

## Assign categories (a bounded write)

`mail.categories.set` assigns Outlook categories to one message. It is one of the four
exceptions to the read-only rule; see
[the decision](../decisions/2026-10-19-escritura-categorias-outlook.md). The others are the Jira
issue transition in the [Jira runbook](jira.md), the Gmail send in two steps in the
[Gmail runbook](gmail.md), and the Bitbucket repository creation in the
[Bitbucket runbook](bitbucket.md).

```bash
solercia-connect run \
  --provider microsoft.graph \
  --capability mail.categories.set \
  --account user@example.com \
  --message-id '<id field of a mail.inbox.list record>' \
  --add 'Resuelto' \
  --remove 'Pendiente' \
  --allow-write \
  --allow-browser-fallback \
  --output categoria.json
```

- `--message-id` takes the record's `id` (Graph `id`, Outlook REST `Id`), never
  `internet_message_id` or `conversation_id`. The id is case-sensitive and changes when the message
  moves to another folder; an unknown id answers `invalid_input`, so read the folder again.
- `--add` and `--remove` are repeatable and combine, so a case changes state in one write.
  `--replace` sets the whole list and excludes both. Names are trimmed and compared without case.
- `--allow-write` authorizes this invocation only and is off by default. Without it the command
  exits with `write_not_authorized` and nothing reaches the provider. `--account` is required and
  the mailbox identity is proven before the read and the write.
- The command reads the current list and its version, computes the result, and writes only when the
  names change; repeating it answers `changed: false`. The single `PATCH` carries nothing but the
  categories and `If-Match` with the version read, so a concurrent edit answers `write_conflict`
  instead of being overwritten. Writes are never retried automatically.
- The record carries `message_id`, `categories`, `previous_categories`, and `changed`; keep
  `previous_categories` to undo a write with `--replace`.
- Only the supervised browser route serves it. The official route would need `Mail.ReadWrite`, which
  is not requested: adding it to the `outlook` target would invalidate every stored grant. The HTTP
  service rejects the capability with 403 even with the interactive login switch on.

## Validate a dedicated account

```bash
export SOLERCIA_CONNECT_LIVE=1
export SOLERCIA_CONNECT_LIVE_TYPE=outlook
export SOLERCIA_CONNECT_LIVE_ACCOUNT=user@example.com
export SOLERCIA_CONNECT_LIVE_START='2026-08-01T00:00:00-05:00'
export SOLERCIA_CONNECT_LIVE_END='2026-09-01T00:00:00-05:00'
make test-live TYPE=outlook ACCOUNT=user@example.com
```

The range is exact and end-exclusive. Do not use a production mailbox for first validation.
