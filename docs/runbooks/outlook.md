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
authoritative about what that route can read: `outlook` reports `mail.sent.list` and
`calendar.events.list`, and `onedrive` reports `files.search`/`files.get` plus
`meeting.transcripts.*`.

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

The loopback service does not offer this route: interactive authorization is CLI-only by design,
so a connector reading through the local API needs an account connected over the official route.

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
