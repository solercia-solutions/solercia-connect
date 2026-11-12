# AGENTS.md — Solercia Connect

Solercia Connect is a Python 3.12, official-first connector runtime for Microsoft 365,
Google Workspace, Jira, Bitbucket Cloud, and GLPI. It normalizes read operations into typed
records with provenance and deterministic execution receipts. It has exactly four bounded writes:
assigning Outlook categories to one message (`mail.categories.set`), accepted in
`docs/decisions/2026-10-19-escritura-categorias-outlook.md`; the status transition of one Jira
issue (`external.issues.transition`), accepted in
`docs/decisions/2026-10-25-escritura-transiciones-jira.md`; sending Gmail in two steps, a
draft (`mail.draft.create`) and then the send of that reviewed draft (`mail.draft.send`) over the
separate `gmail-compose` connection, accepted in
`docs/decisions/2026-10-25-envio-correo-gmail-dos-pasos.md`; and creating one private, empty
Bitbucket repository in an explicit project (`scm.repositories.create`) over the separate
`bitbucket-admin` connection, accepted in
`docs/decisions/2026-10-25-escritura-creacion-repositorios-bitbucket.md`. Bitbucket Cloud is
official-only, accepted in `docs/decisions/2026-10-26-conector-bitbucket.md`.
The `bitbucket` target stays read-only.

## Stack

- **Language:** Python 3.12, src layout
- **HTTP:** httpx through an injected transport port
- **CLI:** Typer
- **Browser fallback:** optional Playwright with a dedicated persistent profile
- **Tests:** pytest; static checks with Ruff and mypy

## Commands

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev,browser]'
.venv/bin/pytest
.venv/bin/pytest --cov=solercia_connect --cov-branch --cov-report=term-missing
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy src
uv build
.venv/bin/solercia-connect providers
```

## Architecture

- `domain.py` owns capabilities, normalized DTOs, provenance, and receipts.
- `ports.py` owns provider, token, transport, and browser interfaces.
- `runtime.py` owns official-first routing, bounded retry, and fallback policy.
- `providers/` contains vendor adapters; vendor payloads never cross this boundary.
- Browser fallback covers Microsoft, Google, and Jira per
  `docs/decisions/2026-10-25-fallback-navegador-google-jira.md`, with one package per family;
  `providers/m365_browser/` is the Microsoft one and `providers/google_browser/` the Google one,
  which serves Meet transcripts only.
- No adapter owns persistence or transactions. This first slice has no database.

## Non-negotiable conventions

- Official APIs and delegated least-privilege authentication are always primary.
- Never decide routes with an LLM, heuristics, randomness, or unbounded retries.
- Browser fallback requires an explicit flag plus a typed capability/reason allowlist.
- Fail closed on invalid credentials, account mismatch, authorization-boundary risk,
  malformed provider data, and unknown failures.
- Never log credentials, cookies, bearer tokens, authorization headers, or raw error bodies.
- Browser operations are read-only, except the category write above, and use a dedicated profile,
  never a personal Chrome profile. The Jira transition, the Gmail writes and the Bitbucket
  repository creation run only over the official REST APIs.
- Writes need an explicit per-invocation `--allow-write` flag, off by default, never run over HTTP,
  never retry, and touch nothing but the categories of one Outlook message, the status of one Jira
  issue through one transition that requires no fields, one plain-text Gmail draft from the
  connected account and the send of one named draft after a duplicate check, or one new private
  Bitbucket repository in an explicit project that did not exist before. The `gmail` and
  `bitbucket` targets stay read-only. Connect never pushes to git. Any other write needs a new ADR.
- Keep provider access behind semantic ports; do not add CRUD repositories for vendor APIs.
- Use timezone-aware datetimes and end-exclusive ranges.
- Tests stay with the behavior they prove. Do not commit or push unless explicitly requested.

## Agent skill

Load `agents/skills/solercia-connect/SKILL.md` for connector work. Agents must invoke the
library or CLI instead of reimplementing OAuth, API calls, routing, or browser selectors.

## Mandatory platform decision

Before architecture, persistence, integration, authentication, provider identity, data modeling,
or cross-product changes, read and follow
`docs/decisions/2026-08-23-solercia-platform-decisions.md`. A conflicting request requires an
explicit new ADR or decision and an update to that record; never override it silently.
