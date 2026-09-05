# Run the loopback API

The FastAPI service is non-interactive by default and binds to `127.0.0.1`. Configure a local key
before using POST endpoints. Interactive browser login over HTTP exists only behind an explicit
switch, described in [Interactive login (opt-in)](#interactive-login-opt-in).

## Start

```bash
export SOLERCIA_CONNECT_API_KEY='GENERATE_A_HIGH_ENTROPY_LOCAL_KEY'
make service
```

FastAPI request bodies are typed Pydantic models, following the official body contract:
<https://fastapi.tiangolo.com/tutorial/body/>.

## Read-only diagnostics

```bash
curl http://127.0.0.1:8765/v1/health
curl http://127.0.0.1:8765/v1/providers
curl 'http://127.0.0.1:8765/v1/doctor/outlook?account=user@example.com'
```

## Test a connection

```bash
curl -X POST http://127.0.0.1:8765/v1/connections/test \
  -H 'Content-Type: application/json' \
  -H 'X-Solercia-Connect-Key: GENERATE_A_HIGH_ENTROPY_LOCAL_KEY' \
  -d '{"type":"outlook","account":"user@example.com","allow_browser_fallback":false}'
```

If interaction is required, the response is HTTP 409 with `action_required` and the exact CLI
command. By default HTTP never opens or invokes a browser. A request with
`"allow_browser_fallback":true` is stopped before the connection/application service and returns
two remediations: `solercia-connect connect TYPE ACCOUNT --allow-browser-fallback` and the
`SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN=true` switch described below. With no local official
profile/token, remediation is exactly `solercia-connect connect TYPE ACCOUNT`.

By default the service executes only an official, private local profile backed by the secure token
store, and a browser-route profile is treated as interactive and returns `action_required`. Unsafe
profile directory/file modes, ownership, regular-file type, or symlinks are configuration failures;
reads do not repair them.

## Interactive login (opt-in)

Set all three in the service environment, then start the service:

```bash
export SOLERCIA_CONNECT_API_KEY='GENERATE_A_HIGH_ENTROPY_LOCAL_KEY'
export SOLERCIA_CONNECT_BROWSER_FALLBACK=true
export SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN=true
make service
curl http://127.0.0.1:8765/v1/health   # data.interactive_login must be true
```

The gate is effective only when the switch, the browser fallback, and the API key are all present;
`GET /v1/health` reports the effective value as `data.interactive_login`. While the gate is off,
every request below returns the HTTP 409 `action_required` envelope with both remediations.

Start the supervised browser for a Microsoft target (`outlook`, `teams`, `onedrive`):

```bash
curl -X POST http://127.0.0.1:8765/v1/connections/connect \
  -H 'Content-Type: application/json' \
  -H 'X-Solercia-Connect-Key: GENERATE_A_HIGH_ENTROPY_LOCAL_KEY' \
  -d '{"type":"outlook","account":"user@example.com","allow_browser_fallback":true}'
```

The response is HTTP 202 with `status: "started"` and `data.session_id`. A headed Chromium opens
on the machine running the service, on the persistent profile for that account; sign in and
complete MFA there. Password and MFA never travel over HTTP. `allow_browser_fallback` must be
`true`: the official device-code route has no terminal over HTTP and returns HTTP 422 with
`Use the CLI for the official device-code route.` A second connect while one session is still
running returns HTTP 409 with `status: "busy"`, `error.code: "interactive_session_busy"`, and the
running `data.session_id`. Only one headed browser runs at a time, for any type or account.

Poll the session:

```bash
curl http://127.0.0.1:8765/v1/connections/sessions/SESSION_ID \
  -H 'X-Solercia-Connect-Key: GENERATE_A_HIGH_ENTROPY_LOCAL_KEY'
```

While the login runs the envelope is `status: "running"`. When it finishes, the response is the
login's own envelope under `operation: "connections.session"`: `status: "connected_browser"` on
success, or `ok: false` with the typed error (`browser_login_required`, `browser_mfa_required`,
`browser_account_mismatch`, `unknown_error`). `data` always carries `session_id`, `started_at`,
and `finished_at`. Finished sessions stay readable for one hour (`data.expires_in_seconds` on the
202), then the process forgets them; an unknown id returns HTTP 404 with `session_not_found`.
Sessions live in memory only: restarting the service drops them, not the connected profile.

With the gate on, `POST /v1/connections/test` and `POST /v1/run` accept
`"allow_browser_fallback":true`. A connection test verifies a browser-route profile headless
against the persistent profile (never a headed window); with no profile yet it returns
`action_required` naming both the CLI command and `POST /v1/connections/connect`. A run serves
the read headless over the browser route; an expired session comes back as `action_required`
with the same two remediations, so a caller can tell "needs login" from "failed".

## Run a capability

```bash
curl -X POST http://127.0.0.1:8765/v1/run \
  -H 'Content-Type: application/json' \
  -H 'X-Solercia-Connect-Key: GENERATE_A_HIGH_ENTROPY_LOCAL_KEY' \
  -d '{
    "type":"google-calendar",
    "account":"user@example.com",
    "capability":"calendar.events.list",
    "start":"2026-08-01T00:00:00-05:00",
    "end":"2026-09-01T00:00:00-05:00",
    "limit":50
  }'
```

All successful and failed responses use `solercia.connect/v1`. Do not put provider credentials or
tokens in HTTP bodies. `POST /v1/connections/test`, `POST /v1/run`, `POST /v1/connections/connect`,
and `GET /v1/connections/sessions/{id}` require the API key. Missing, empty, malformed, non-ASCII,
and incorrect API-key headers receive the same non-leaking HTTP 401 envelope.
