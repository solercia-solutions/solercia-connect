# Bitbucket Cloud runbook

Bitbucket Cloud is read through the official REST API 2.0 only (`https://api.bitbucket.org/2.0`),
with one connection per account. It is read-only and has no browser route. The decision behind it
is [the Bitbucket connector ADR](../decisions/2026-10-26-conector-bitbucket.md).

## Create the API token

Bitbucket needs an Atlassian API token created **with Bitbucket scopes**. A classic, unscoped
token, such as the one Jira uses, is rejected with `invalid_credentials`.

1. Open id.atlassian.com, select **Security**, then **Create and manage API tokens**.
2. Select **Create API token with scopes**.
3. Give it a name and an expiry date. Atlassian allows one year at most; note the date, because
   reads fail with `invalid_credentials` once it passes.
4. Select **Bitbucket** as the app.
5. Select exactly these four read scopes; they do not imply one another:
   - `read:user:bitbucket` (the account and its email addresses)
   - `read:workspace:bitbucket` (the workspace)
   - `read:repository:bitbucket` (repositories and commits)
   - `read:pullrequest:bitbucket` (pull requests)
6. Create the token and copy it once. Its scopes cannot be changed later; create a new token
   instead.

Never paste the token into a chat, a command argument, or a file inside the repository.

## Connect

Run `doctor` first, then connect once per account. `--workspace` is the slug in
`bitbucket.org/<workspace>/<repository>`; it becomes the default workspace of the reads.

```bash
solercia-connect doctor bitbucket user@example.com

# The token from a variable of the process, SOLERCIA_CONNECT_ENV_FILE or the project connect.env.
solercia-connect connect bitbucket user@example.com --workspace idartes-tecnologia \
  --token-env BITBUCKET_API_TOKEN

solercia-connect status bitbucket user@example.com
```

Without `--token-env` the CLI asks for the token on a hidden prompt; `--token-stdin` reads one
piped line instead. `--site`, `--token-kind`, `--cloud-id` and `--allow-browser-fallback` are
refused for Bitbucket.

In project mode the token never leaves the project's private folder:

```bash
solercia-connect project init        # creates .solercia-connect/connect.env, values commented out
# Edit .solercia-connect/connect.env in your editor: BITBUCKET_API_TOKEN=<the token>
solercia-connect connect bitbucket user@example.com --workspace idartes-tecnologia \
  --token-env BITBUCKET_API_TOKEN
```

Connect proves the identity before anything is stored, and every later `status` and read proves it
again:

1. `GET /2.0/user` returns the account ID, which must match the one stored at connect.
2. `GET /2.0/user/emails` lists the account email as confirmed.
3. `GET /2.0/workspaces/<workspace>` answers 200.

The token is stored in the OS keyring under `bitbucket:<email>` (service
`solercia-connect.api-token`), or in the project's private secret file in project mode. The profile
keeps the workspace and the account ID only. A failed connect stores nothing.

```bash
solercia-connect disconnect bitbucket user@example.com
```

Disconnect removes the local profile and token. Revoke the token itself at id.atlassian.com.

## Read

Every read needs `--account`. `--workspace` overrides the connected workspace.

```bash
# Repositories the account is a member of, newest update first.
solercia-connect run --provider bitbucket.cloud --capability scm.repositories.list \
  --account user@example.com

# Pull requests updated in the window; --state is repeatable (OPEN, MERGED, DECLINED,
# SUPERSEDED) and defaults to all four.
solercia-connect run --provider bitbucket.cloud --capability scm.pullrequests.list \
  --account user@example.com --repo pandora_proxy \
  --start '2026-10-01T00:00:00-05:00' --end '2026-11-01T00:00:00-05:00' \
  --state OPEN --state MERGED

# Commits of a branch dated in the window; without --branch the main branch is read.
solercia-connect run --provider bitbucket.cloud --capability scm.commits.list \
  --account user@example.com --repo pandora_proxy --branch develop \
  --start '2026-10-01T00:00:00-05:00' --end '2026-11-01T00:00:00-05:00' \
  --output commits.json
```

- Windows are timezone-aware and end-exclusive. Pull requests are filtered by `updated_on` on the
  server and checked again locally; commits have no server-side date filter, so the window is
  applied locally.
- Commits follow the commit graph, like `git log`, so their dates are not strictly ordered. The
  read stops only after a whole page older than the window start minus seven days.
- Output is deterministic: repositories by update time then slug, pull requests by update time
  then id, commits by date then hash. Repeated items are reported once.

## Paging and completeness

Reads follow only the `next` link of the same collection, with a fixed page bound: 20 pages of
repositories (100 each), 20 pages of pull requests (50 each), and 30 pages of commits (100 each).
When the bound is reached first, the read fails with `result_incomplete` and the envelope carries
`"complete": false`; no partial list is ever returned. Narrow the window, or pass a less busy
`--branch`, and run it again.

Bitbucket allows 1,000 requests per hour on `/2.0/repositories/*` for a personal API token. Every
read also spends three identity requests outside that group.

## Errors

| Code | Meaning | Next step |
| --- | --- | --- |
| `invalid_credentials` | The token expired, was revoked, or has no Bitbucket scopes (a classic Jira token) | Create a token with scopes as above and connect again |
| `authorization_denied` | A scope is missing; the message names it when Bitbucket provides it | Create a new token with all four scopes and connect again |
| `authorization_boundary_violation` | The account ID changed, or the account email is missing or unconfirmed | Connect again with the right account; confirm the email in Bitbucket |
| `workspace_not_found` | The workspace slug is wrong, or the account is not a member | Check the slug in the Bitbucket URL |
| `repository_not_found` | The repository slug or the branch is wrong, or not visible | List the slugs with `scm.repositories.list` |
| `result_incomplete` | The page bound was reached; nothing was returned | Narrow the window or the branch |
| `rate_limited` | Bitbucket's hourly request limit was reached | Wait and run it again |
| `malformed_provider_data` | Bitbucket answered with an unexpected shape | Report it; nothing was read |

## Live validation

The offline suite proves the contracts with fakes. The first live run against
`idartes-tecnologia` still has to confirm the shape of `/2.0/user/emails`, the scope names in a
`403`, and a branch name with `/` in the commits path.
