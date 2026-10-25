# Connect Google Calendar

Google Calendar uses a separate Desktop OAuth profile and the read-only events scope. It does not
reuse or silently broaden a Gmail grant.

## Prepare and connect

```bash
export SOLERCIA_CONNECT_GOOGLE_CLIENT_ID='DESKTOP_CLIENT_ID'
make doctor TYPE=google-calendar ACCOUNT=user@example.com
solercia-connect connect google-calendar user@example.com
solercia-connect status google-calendar user@example.com
```

The CLI owns the loopback listener, cryptographic state, PKCE verifier/challenge, finite timeout,
and observed primary-calendar identity. Use `--no-open-browser` to open the official URL manually.

Scope reference: <https://developers.google.com/workspace/calendar/api/auth>. Loopback guidance:
<https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration>.

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
- **Organization block.** A Workspace administrator can block unconfigured apps
  ([Control which apps access Google Workspace data](https://support.google.com/a/answer/7281227)).
  Google then shows `admin_policy_enforced` or `org_internal` on its own page and does not
  redirect to the loopback listener, so the CLI reports `oauth_callback_timeout` with a
  remediation that points to the browser fallback route in
  `docs/decisions/2026-10-25-fallback-navegador-google-jira.md` (`--allow-browser-fallback` once
  the Google browser route is available).
- **Scopes.** None of this changes the scopes Connect requests; existing grants keep theirs.

## Exact event read and live proof

```bash
solercia-connect run \
  --provider google.calendar \
  --capability calendar.events.list \
  --start '2026-08-01T00:00:00-05:00' \
  --end '2026-09-01T00:00:00-05:00' \
  --account user@example.com

export SOLERCIA_CONNECT_LIVE=1
export SOLERCIA_CONNECT_LIVE_TYPE=google-calendar
export SOLERCIA_CONNECT_LIVE_ACCOUNT=user@example.com
export SOLERCIA_CONNECT_LIVE_START='2026-08-01T00:00:00-05:00'
export SOLERCIA_CONNECT_LIVE_END='2026-09-01T00:00:00-05:00'
make test-live TYPE=google-calendar ACCOUNT=user@example.com
```
