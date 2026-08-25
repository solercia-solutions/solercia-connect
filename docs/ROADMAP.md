# Solercia Connect roadmap

A checked item means implementation plus offline proof exists. It does **not** prove live tenant
consent, provider stability, browser selectors, or production readiness; those gates stay
unchecked until dedicated live evidence exists.

## Standalone product foundation

- [x] Independent Git repository on `main` with no remote or commits.
- [x] Python 3.12 src-layout package and installed Typer CLI.
- [x] Canonical `AGENTS.md` / `CLAUDE.md` and project skill source structure.
- [x] Typed mail, calendar, meeting transcript, external issue, and ticket contracts.
- [x] Source-qualified provenance and deterministic execution receipts.
- [x] Explicit official/browser registry and bounded official-first route policy.
- [x] Offline-proven fail-closed credential, identity, malformed-data, policy, and unknown-error handling.
- [x] Injected HTTP, authorization, and OAuth token-store ports.
- [x] Typed target aliases, explicit env/env-file settings, and pre-side-effect account validation.
- [x] Private atomic XDG non-secret profiles with strict private read validation and list/show/delete lifecycle.
- [x] Structured doctor/readiness checks, including the driver-shared browser path auditor.
- [x] Canonical connect/status/disconnect lifecycle wired to secure keyring OAuth storage.
- [x] Shared `solercia.connect/v1` CLI/HTTP envelope and versioned normalized record wrapper.
- [x] Atomic `0600` output files with JSON-normalized OS failures, cleanup, symlink, and overwrite protection.
- [x] Loopback FastAPI adapter with typed bodies, no-browser enforcement, uniform errors, and API-key protection.
- [x] Root wrapper, JSON-clean Make workflows, locked build tooling, and post-setup offline build.
- [x] Hermetic regressions with fake transports, token stores, strict callbacks, browser sentinels, and API clients.
- [x] Opt-in provider live harness with default isolation and profile/token-store readiness guards.

## Microsoft 365 — Outlook and Teams

- [x] Microsoft Graph identity/capability health validation.
- [x] Outlook sent-mail full-body read, paging, and strict end-exclusive range.
- [x] Microsoft Graph calendar-view read and acceptance/cancellation filters.
- [x] Refresh-capable Microsoft authorization-code/device-code provider with bounded polling.
- [x] CLI device authorization for Outlook/Teams with distinct least-privilege scopes.
- [x] Official known-meeting Teams transcript list/content implementation with sanitized offline fixtures.
- [x] Offline-proven dedicated Playwright path safety, login/MFA state machine, unique binding, and status contract.
- [x] Explicit browser session status that never claims built-in extraction support.
- [ ] Obtain real tenant consent for Outlook `Mail.Read` and `Calendars.Read`.
- [ ] Validate Graph mail/calendar contracts against dedicated work/school test tenants.
- [ ] Prove `OnlineMeetingTranscript.Read.All` and transcript content against a dedicated tenant and known meeting.
- [ ] Confirm tenant-admin-disabled and personal-account transcript errors against live Graph responses.
- [ ] Implement and live-validate built-in Outlook Web read extraction.
- [ ] Implement and live-validate Teams calendar/recap navigation.
- [ ] Implement and live-validate Teams/SharePoint browser transcript fallback without token capture.
- [ ] Capture sanitized selector fixtures and define an upgrade compatibility policy.
- [ ] Add mail attachment metadata/content reads behind explicit capabilities.

## Google Workspace — Gmail and Calendar

- [x] Gmail profile identity plus sent-message list and full MIME read foundation.
- [x] MIME text/plain preference with HTML fallback and normalized recipients.
- [x] Google Calendar primary identity, recurrence expansion, paging, and acceptance filters.
- [x] Refresh-capable Google OAuth provider and secure token storage.
- [x] CLI-owned Desktop OAuth loopback with cryptographic state, PKCE S256, and finite timeout.
- [x] Separate Gmail readonly and Calendar events-readonly connection profiles/scopes.
- [ ] Complete real Google OAuth consent and app verification where required.
- [ ] Validate Gmail and Calendar contracts against dedicated Workspace test accounts.
- [ ] Add Gmail thread expansion as an explicit capability.
- [ ] Add attachment reads behind explicit size/content controls.

## Jira Cloud

- [x] `/myself` health/account validation and enhanced JQL representative read.
- [x] Normalized completed/resolved issue records with provenance.
- [ ] Validate classic API-token and OAuth integrations against real Jira Cloud sites.
- [ ] Add configurable project/type/status policy objects without embedding report rules.
- [ ] Add bounded paging fixtures for sites with more than one result page.

## GLPI

- [x] Session lifecycle, Ticket search-option validation, and read-only Ticket search.
- [x] Configurable GLPI search-field mapping with normalized ticket output.
- [ ] Validate against supported GLPI 10 and GLPI 11 installations.
- [ ] Discover and persist per-installation search-option mappings through non-secret configuration.
- [ ] Add optional ticket follow-up reads as a separate capability.

## Cross-provider hardening

- [ ] Add a production secret-manager-backed `OAuthTokenStore` adapter.
- [ ] Run and retain sanitized evidence from every provider-specific live suite.
- [ ] Add delta/sync cursors and idempotent checkpoints without a shared database.
- [ ] Add OpenTelemetry-compatible secret-safe metrics and traces.
- [ ] Define package publishing, semantic versioning, and signed release workflow.
- [ ] Vendor a wheelhouse for a self-contained fresh empty-cache locked setup/build.
- [ ] Add consumer integration examples for Solercia products and AUTOMATINFORMES.
