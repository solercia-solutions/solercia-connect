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
