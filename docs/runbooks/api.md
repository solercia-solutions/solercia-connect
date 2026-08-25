# Run the loopback API

The FastAPI service is non-interactive and binds to `127.0.0.1` by default. Configure a local key
before using POST endpoints.

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
command. HTTP never opens or invokes a browser. A request with
`"allow_browser_fallback":true` is stopped before the connection/application service and returns
`solercia-connect connect TYPE ACCOUNT --allow-browser-fallback`; execute that command manually.
With no local official profile/token, remediation is exactly
`solercia-connect connect TYPE ACCOUNT`.

The service executes only an official, private local profile backed by the secure token store. A
browser-route profile is treated as interactive and returns `action_required`. Unsafe profile
directory/file modes, ownership, regular-file type, or symlinks are configuration failures; reads
do not repair them.

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
tokens in HTTP bodies. Missing, empty, malformed, non-ASCII, and incorrect API-key headers receive
the same non-leaking HTTP 401 envelope.
