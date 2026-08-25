# Runtime usage boundary

Use the installed CLI or Python package as the only integration boundary. An agent may prepare
typed inputs, invoke doctor/connect/status/run, and interpret the versioned envelope. It must not
obtain credentials, reproduce provider requests, run callback servers, scrape a UI, access browser
storage, or make fallback decisions outside the runtime.

Interactive OAuth belongs to `solercia-connect connect TYPE ACCOUNT`. HTTP callers must stop on
`action_required` and relay its exact CLI remediation. Preserve normalized record schema,
provenance, route, and receipt with downstream data.
