# Bitbucket Cloud runbook

Bitbucket Cloud is read through the official REST API 2.0 only (`https://api.bitbucket.org/2.0`),
with one connection per account. The `bitbucket` connection is read-only and has no browser route.
The decision behind it is [the Bitbucket connector ADR](../decisions/2026-10-26-conector-bitbucket.md).
Creating repositories is a separate write on its own `bitbucket-admin` connection; see
[Create a repository](#create-a-repository).

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

## Create a repository

`scm.repositories.create` creates one private, empty git repository in an existing project of a
workspace. It is the fourth bounded write of Solercia Connect, accepted in
[the repository creation ADR](../decisions/2026-10-25-escritura-creacion-repositorios-bitbucket.md).
It never deletes, changes, or pushes anything, and it runs only in the CLI, never over HTTP.

### The bitbucket-admin token

The creation runs on its own connection, `bitbucket-admin`, with its own API token; the read
token of `bitbucket` never gains a write scope. Create the token as in
[Create the API token](#create-the-api-token), but select exactly these five scopes:

- `read:user:bitbucket` (the account and its email addresses)
- `read:workspace:bitbucket` (the workspace)
- `read:repository:bitbucket` (the check that the repository does not exist yet)
- `read:project:bitbucket` (the check that the project exists)
- `admin:repository:bitbucket` (the creation itself)

### Connect bitbucket-admin

```bash
solercia-connect doctor bitbucket-admin user@example.com

solercia-connect connect bitbucket-admin user@example.com --workspace idartes-tecnologia \
  --token-env BITBUCKET_ADMIN_API_TOKEN

solercia-connect status bitbucket-admin user@example.com
```

In project mode, put `BITBUCKET_ADMIN_API_TOKEN=<the token>` in `.solercia-connect/connect.env`;
`solercia-connect project init` already lists it, commented out. The identity is proven exactly as
for `bitbucket`. The token is stored under `bitbucket-admin:<email>`, apart from the read token
under `bitbucket:<email>`, and `disconnect` removes only its own:

```bash
solercia-connect disconnect bitbucket-admin user@example.com
```

### Create

`--repo` (the slug) and `--project` (the project key) are required, and `--allow-write` is
required on every invocation. `--workspace` overrides the connected workspace.

```bash
solercia-connect run --provider bitbucket.cloud --capability scm.repositories.create \
  --account user@example.com --repo pandora-front --project PAN --allow-write
```

- The slug is checked as given: lower-case letters, digits, `.`, `_` and `-` only, never `.` or
  `..`. Upper case, spaces, `/` and braced UUIDs are refused, never converted.
- The project key is letters, digits and `_`. There is no default project: Bitbucket would
  otherwise put the repository in the oldest project of the workspace.
- Before writing, Connect proves the identity, checks that `GET /2.0/repositories/<workspace>/<slug>`
  answers 404 and that `GET /2.0/workspaces/<workspace>/projects/<key>` answers with the same key.
- The one `POST` always sends `scm: git`, `is_private: true`, `fork_policy: no_public_forks` and
  the project key, and is never retried. The answer must carry the same slug, workspace, and
  project with `is_private: true`.
- The record carries `workspace`, `slug`, `full_name`, `project_key`, `is_private`, `web_url`,
  `clone_https` and `created_at`. `clone_https` never holds a user name or password.

### Push with git

Solercia Connect never pushes. Push the existing local repository with `git`, which uses the
credential it already stores, never the Connect token:

```bash
git remote add origin https://x-bitbucket-api-token-auth@bitbucket.org/idartes-tecnologia/pandora-front.git
git push -u origin main
```

`x-bitbucket-api-token-auth` is the user name Bitbucket expects with an API token. If `git` has
no stored credential for `bitbucket.org` yet, it asks for one; never paste it into a chat or a
command argument.

### Creation errors

| Code | Meaning | Next step |
| --- | --- | --- |
| `write_not_authorized` | `--allow-write` is missing; nothing was sent | Add it only when the user asked for this creation |
| `repository_exists` | A repository already uses that slug; nothing was changed | Report it; never pick another slug on your own |
| `project_not_found` | The project key does not exist in the workspace, or is not visible | Check the key on the workspace's Projects page |
| `invalid_input` | The slug or the key breaks the rules above, or Bitbucket spells the key otherwise | Use the exact key the message names |
| `authorization_denied` | A scope is missing, or a private repository the account cannot see uses the slug | Create a token with all five scopes; check the slug |
| `request_rejected` | Bitbucket refused the creation (invalid input or no permission to create) | Check the slug, the key, and the account's permission |
| `write_unverified` | No readable confirmation, or an answer that does not match the request | Review the repository in Bitbucket before any new attempt |
| `invalid_credentials` | The admin token expired or has no Bitbucket scopes | Create a new token with the five scopes and connect again |

## Live validation

The offline suite proves the contracts with fakes. The first live run against
`idartes-tecnologia` still has to confirm the shape of `/2.0/user/emails`, the scope names in a
`403`, and a branch name with `/` in the commits path.

The creation is proven offline as well. Its first live run still has to confirm the shape of a
`400`, whether Bitbucket treats project keys case-sensitively, and the clone link it returns.
