# Connect Gmail

Gmail uses a Google OAuth **Desktop app** client, a loopback callback on `127.0.0.1`, exact state
correlation, and PKCE S256. The grant requests only Gmail read-only plus identity scopes.

## Prepare and connect

```bash
export SOLERCIA_CONNECT_GOOGLE_CLIENT_ID='DESKTOP_CLIENT_ID'
# Export SOLERCIA_CONNECT_GOOGLE_CLIENT_SECRET only when the Desktop client requires it.
make doctor TYPE=gmail ACCOUNT=user@example.com
solercia-connect connect gmail user@example.com
```

If the CLI cannot open a browser automatically:

```bash
solercia-connect connect gmail user@example.com --no-open-browser
```

Open the returned official URL while the finite loopback listener is waiting. The profile is saved
only after Gmail observes the requested identity. Google loopback guidance:
<https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration>.

## Live proof

```bash
export SOLERCIA_CONNECT_LIVE=1
export SOLERCIA_CONNECT_LIVE_TYPE=gmail
export SOLERCIA_CONNECT_LIVE_ACCOUNT=user@example.com
export SOLERCIA_CONNECT_LIVE_START='2026-08-01T00:00:00-05:00'
export SOLERCIA_CONNECT_LIVE_END='2026-09-01T00:00:00-05:00'
make test-live TYPE=gmail ACCOUNT=user@example.com
```

Gmail readonly is a broad/restricted scope. Use a dedicated account and complete any Workspace
admin/app-verification requirements before claiming readiness.
