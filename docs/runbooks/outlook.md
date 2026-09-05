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
