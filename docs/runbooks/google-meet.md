# Google Meet transcripts

`google-meet` (alias `meet`) is the Google Workspace transcript target. The official route is
always primary. When the organization blocks the Google app, a supervised browser route can read
the same transcripts (see [Browser route](#browser-route)).

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
  `docs/decisions/2026-10-25-fallback-navegador-google-jira.md`. For this target that route
  exists: see [Browser route](#browser-route).
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

## Browser route

Decision `2026-10-25-fallback-navegador-google-jira.md`, D1, as made precise on 2026-10-26, allows
two page reads for Meet and nothing else:

1. **Discovery.** The Calendar web day view
   (`https://calendar.google.com/calendar/u/0/r/day/YYYY/M/D`) lists each event as a button. Connect
   opens each event's detail, keeps only the Docs attachment named
   `<title> - YYYY/MM/DD HH:MM GMT±HH:MM - Transcript` (or `Transcripción`), and closes the detail.
   The recording attachment (`- Recording` / `- Grabación`) is not read.
2. **Reading.** The document's `mobilebasic` view
   (`https://docs.google.com/document/d/{id}/mobilebasic`) shows the transcript as plain HTML;
   Connect takes its visible text. The `/edit` view draws the document on a canvas and is not used.

Connect never calls Calendar's or Drive's internal APIs (`sync.fetcheventrange`,
`calendar-pa.clients6.google.com`, `drivefrontend-pa.clients6.google.com`), never reads cookies or
builds headers from them, and never follows the signed export URL on `googleusercontent.com`.
Navigation is limited to `calendar.google.com` and `docs.google.com`.

### Preparing the browser session

```bash
export SOLERCIA_CONNECT_BROWSER_FALLBACK=true
solercia-connect doctor google-meet user@example.com
solercia-connect connect google-meet user@example.com --allow-browser-fallback
solercia-connect status google-meet user@example.com --allow-browser-fallback
```

`connect` takes the browser route when no Google client ID is configured, or when the official
connection fails with `admin_consent_required`. It opens Chromium on the dedicated Google profile
(`data/google/<hash>`, never a personal Chrome profile); the user signs in and completes MFA.
Connect then reopens the profile headless and proves the identity again before saving
`connected_browser`. Google may refuse sign-in in an automated browser ("This browser or app may
not be secure"); that is the live gate T5.0 and the route stops there.

The identity is the account named by the Google bar's account button in Calendar web
(`Google Account: Name (address)` / `Cuenta de Google: Nombre (dirección)`). It must match the
connected account. A missing, ambiguous or different account fails closed before any read.

### Reading

```bash
solercia-connect run \
  --provider google.meet \
  --capability meeting.transcripts.list \
  --start '2026-10-26T00:00:00-05:00' \
  --end '2026-10-27T00:00:00-05:00' \
  --account user@example.com \
  --allow-browser-fallback
```

- Window only, at most 31 days. `--meeting-id` needs the official route and fails with
  `browser_capability_not_implemented`.
- Calendar web shows days in the calendar's own time zone, which Connect cannot read, so it opens
  one extra day on each side and then applies the exact end-exclusive window to the stamp in the
  document name. An empty window stays empty; a day view that did not render is
  `browser_selector_mismatch`, never an empty day.
- Only events of the account's own calendar count, like the official route.
- `meeting.transcript.get` returns the earliest transcript in the window.

### Record differences

`provenance` names the route (`google.browser`, endpoint `calendar-web→docs-mobilebasic` plus the
selector version). Two fields also differ, because the page does not show what the API returns:

- `recorded_at` is the stamp Meet writes in the document name (when transcription started); the
  official route uses the event start.
- `organizer` is `null`; the organizer's address is not on the page.

The text is normalized only in line endings, a leading byte-order mark and outer blank space.

### Failures

| Reason | Meaning |
| --- | --- |
| `browser_login_required` / `browser_mfa_required` | The profile's session expired; run `connect … --allow-browser-fallback` again. |
| `browser_account_mismatch` | The profile is signed in to another account. |
| `browser_identity_unavailable` | The account button did not name an account. |
| `browser_selector_mismatch` | A page no longer has the shape this selector version expects. |
| `browser_extraction_failed` | A bound was exceeded (200 events in a day, 10 MiB per document) or a page could not be read. |

### Live validation pending

The selectors were written from the live observation of 2026-10-26 but have not run in Connect
against Google yet. Two assumptions in particular need the live gates: the identity probe (T5.1)
and the event reference attribute `data-eventid`, which Connect decodes into the event ID and the
calendar it belongs to. Until then the capability stays `implemented-offline`.

## Record and failure contract

Records use `content_format: "text/plain"` and preserve the exported text. Speaker names come from
`Hablante: texto` turn prefixes; `last_cue_seconds` is `null` because Google does not provide cue
times, and the runtime never synthesizes them. The provenance endpoint is `calendar→drive` and the
route is `official`.

Invalid selectors, account mismatches, insufficient permissions, malformed UTF-8, and exports over
10 MiB produce typed, safe failures. Error details never include access tokens, cookies, client
secrets, or raw provider response bodies. Live provider proof for third-party transcript sharing is
deferred; the capability remains `implemented-offline` until that gate is approved and run.
