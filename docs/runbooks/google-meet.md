# Google Meet transcripts

`google-meet` (alias `meet`) is the official-only Google Workspace transcript target. It does not
use Playwright or any browser fallback.

## Readiness and authorization

```bash
solercia-connect config validate
solercia-connect doctor google-meet user@example.com
solercia-connect connect google-meet user@example.com
```

The OAuth profile requires a Google Desktop client and these read-only scopes:

- `openid`
- `email`
- `https://www.googleapis.com/auth/calendar.events.readonly`
- `https://www.googleapis.com/auth/drive.readonly`

The account is checked against the authenticated Google identity before Calendar or Drive data is
read. Workspace policy may require administrator approval, and Meet transcription must be enabled.

## Your own Google Cloud OAuth client

The verbatim Google sources are quoted in the
[Gmail runbook](gmail.md#your-own-google-cloud-oauth-client). What they mean for this target
(analysis, not Google text):

- **Testing.** At most 100 test users; the grant and its refresh token expire seven days after
  consent ([Manage App Audience](https://support.google.com/cloud/answer/15549945)).
- **In production, for personal use.** The owner, or a few known users, can continue past the
  unverified app screen and the seven-day expiry no longer applies; a cap of 100 new users holds
  for the life of the project
  ([Restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification),
  [Using OAuth 2.0](https://developers.google.com/identity/protocols/oauth2)).
- **Organization block.** A Workspace administrator can block unconfigured apps or restrict Drive
  ([Control which apps access Google Workspace data](https://support.google.com/a/answer/7281227)).
  Google then shows `admin_policy_enforced` or `org_internal` on its own page and does not
  redirect to the loopback listener, so the CLI reports `oauth_callback_timeout` with a
  remediation that points to the browser fallback route in
  `docs/decisions/2026-10-25-fallback-navegador-google-jira.md` (`--allow-browser-fallback` once
  the Google browser route is available). That route does not exist yet for this target.
- **Scopes.** None of this changes the scopes Connect requests; existing grants keep theirs.

## Selectors

Calendar discovers the event and its Google Docs attachment titled with `Transcript`; Drive exports
that document as plain text. Select either a meeting or an exact timezone-aware, end-exclusive window:

```bash
solercia-connect run \
  --provider google.meet \
  --capability meeting.transcript.get \
  --meeting-id EVENT_ID \
  --account user@example.com

solercia-connect run \
  --provider google.meet \
  --capability meeting.transcripts.list \
  --start '2026-08-01T00:00:00-05:00' \
  --end '2026-09-01T00:00:00-05:00' \
  --account user@example.com \
  --output transcripts.json
```

`--drive-id` and `--item-id` are Microsoft-only selectors and fail closed for this target. An event
without a qualifying transcript attachment is not an error; an empty window returns zero records.

## Record and failure contract

Records use `content_format: "text/plain"` and preserve the exported text. Speaker names come from
`Hablante: texto` turn prefixes; `last_cue_seconds` is `null` because Google does not provide cue
times, and the runtime never synthesizes them. The provenance endpoint is `calendar→drive` and the
route is `official`.

Invalid selectors, account mismatches, insufficient permissions, malformed UTF-8, and exports over
10 MiB produce typed, safe failures. Error details never include access tokens, cookies, client
secrets, or raw provider response bodies. Live provider proof for third-party transcript sharing is
deferred; the capability remains `implemented-offline` until that gate is approved and run.
