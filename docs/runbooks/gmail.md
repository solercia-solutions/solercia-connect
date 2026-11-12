# Connect Gmail

Gmail uses a Google OAuth **Desktop app** client, a loopback callback on `127.0.0.1`, exact state
correlation, and PKCE S256. The `gmail` grant requests only Gmail read-only plus identity scopes.
Sending uses a separate `gmail-compose` connection; see
[Send in two steps](#send-in-two-steps-gmail-compose).

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

## Read the inbox and the sent mail

The `gmail` connection serves two reads over the official API only:

```bash
solercia-connect run --provider google.gmail --capability mail.inbox.list \
  --account user@example.com \
  --start '2026-08-01T00:00:00-05:00' --end '2026-09-01T00:00:00-05:00' --output entrada.json
solercia-connect run --provider google.gmail --capability mail.sent.list \
  --account user@example.com \
  --start '2026-08-01T00:00:00-05:00' --end '2026-09-01T00:00:00-05:00' --output enviados.json
```

`mail.inbox.list` searches `in:inbox` with the `INBOX` label, reads each message in full, and keeps
only the messages whose `internalDate` (the instant Google accepted them) falls inside the
end-exclusive window. Records match the Outlook inbox record: `folder` is `inbox`, `received_at` is
`internalDate`, `sent_at` comes from the `Date` header (or `internalDate` when that header is
unusable), and `internet_message_id` is the `Message-ID` header. Records come oldest first; when the
window holds more than `--limit` messages, Gmail returns the latest ones. There is no browser route
for the inbox.

## Your own Google Cloud OAuth client

The Desktop client belongs to your own Google Cloud project. Its publishing status and the policy
of the account's Workspace organization decide how long a grant lasts and whether it is allowed.

### Official sources

Verbatim quotes:

> Projects configured with a publishing status of **Testing** are limited to up to 100 test users
> listed in the OAuth consent screen.
>
> Authorizations by a test user will expire seven days from the time of consent. If your OAuth
> client requests an `offline` access type and receives a refresh token, that token will also
> expire.

[Manage App Audience](https://support.google.com/cloud/answer/15549945), section "Publishing
status".

> One use case is if you are the only user of your app or if your app is used by only a few users,
> all of whom are known personally to you. You and your limited number of users might be
> comfortable with advancing through the unverified app screen and granting your personal accounts
> access to your app.

[Restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification),
section "Personal use".

> A Google Cloud Platform project with an OAuth consent screen configured for an external user
> type and a publishing status of "Testing" is issued a refresh token expiring in 7 days

[Using OAuth 2.0 to Access Google APIs](https://developers.google.com/identity/protocols/oauth2),
section "Refresh token expiration".

> 100 new users in total, after the app presents the unverified app screen
>
> The user cap applies over the entire lifetime of the project, and it cannot be reset or changed.

[Manage App Audience](https://support.google.com/cloud/answer/15549945), section "OAuth user cap".

> **Don't allow users to access any third-party apps**—Users can't sign in with Google to any
> third-party apps and websites until you configure those apps and sites with an access setting.
>
> Gmail, Google Drive, Docs, and Chat can also restrict access to a predefined list of high-risk
> OAuth scopes.

[Control which apps access Google Workspace data](https://support.google.com/a/answer/7281227).

### What this means for Connect

This is analysis of the sources above, not Google text:

- **Testing.** At most 100 test users. The grant, including its refresh token, stops working seven
  days after consent, and `status` then asks for `connect` again.
- **In production, for personal use.** The owner, or a few known users, can continue past the
  unverified app screen, and the seven-day expiry of Testing no longer applies. Every new account
  that passes that screen counts toward a cap of 100 for the life of the project. An
  authorization given while the app was still in Testing keeps its seven-day expiry, so reconnect
  once after publishing.
- **Organization block.** A Workspace administrator can block unconfigured apps or restrict Gmail
  and Drive. Google then shows `admin_policy_enforced`, or `org_internal` when the project belongs
  to another Cloud organization, on its own page and does not redirect to the loopback listener.
  The CLI only observes `oauth_callback_timeout`; its remediation names this case and points to
  the browser fallback route in `docs/decisions/2026-10-25-fallback-navegador-google-jira.md`
  (`--allow-browser-fallback` once the Google browser route is available).
- **Scopes.** None of this changes the scopes Connect requests; existing grants keep theirs.

## Send in two steps (`gmail-compose`)

Sending is the third write of Connect, accepted in
[the decision](../decisions/2026-10-25-envio-correo-gmail-dos-pasos.md). It uses its own connection,
`gmail-compose`, with `openid`, `email`, and `gmail.compose`, and its own token key
`gmail-compose:<account>`. The `gmail` connection keeps `gmail.readonly` and is never widened.
`gmail.compose` is a restricted scope: add it to the OAuth consent screen of your Google Cloud
project before connecting.

```bash
solercia-connect connect gmail-compose user@example.com
solercia-connect status  gmail-compose user@example.com
```

The flow has three steps, and only the first and the last are commands:

1. **Draft.** `mail.draft.create` creates one plain-text draft from the connected account:

   ```bash
   solercia-connect run --provider google.gmail --capability mail.draft.create \
     --account user@example.com --to recipient@example.com --bcc archive@example.com \
     --subject 'Weekly report' --body-file ./body.txt --allow-write
   ```

   `--to` is repeatable and required; `--cc` and `--bcc` are optional; 20 recipients at most. The
   body comes from `--body-file` or piped `--body-stdin` (UTF-8, up to 200 KB). The record carries
   `draft_id` and a 200-character `body_preview`.
2. **Review.** The user opens Gmail > Drafts, reads the draft, and approves its id. Edits made in
   Gmail are kept; the send reads the draft as it stands.
3. **Send.** `mail.draft.send` sends that draft, once:

   ```bash
   solercia-connect run --provider google.gmail --capability mail.draft.send \
     --account user@example.com --draft-id '<draft id>' --allow-write
   ```

### Duplicate check

`gmail.compose` cannot list or read messages, so before the send Connect reads Sent through the
read-only `gmail` connection of the same account. Connect both once:

```bash
solercia-connect connect gmail user@example.com
solercia-connect connect gmail-compose user@example.com
```

The check searches `in:sent subject:"<subject>"` over the last 30 days, reads the headers of up to
100 candidates, and compares the normalized subject and the set of `To`, `Cc`, and `Bcc`
addresses. A match fails with `duplicate_suspected` and reports when the earlier message was sent;
more candidates than the bound also fail closed. Without the `gmail` connection the send fails with
`duplicate_check_unavailable`. `--allow-duplicate` skips the check entirely; pass it only when the
user explicitly approves sending again.

### Failures

| Code | Meaning | Next step |
| --- | --- | --- |
| `write_not_authorized` | `--allow-write` is missing; nothing was sent | Add it only for an approved write |
| `write_scope_missing` | The `gmail-compose` grant lacks `gmail.compose` | `solercia-connect connect gmail-compose <account>` |
| `authorization_boundary_violation` | Another identity, or the draft's `From` is not the account | Use the account's own draft |
| `draft_not_found` | The draft does not exist, or was already sent or deleted | Read Gmail > Drafts |
| `duplicate_suspected` | Sent holds the same subject to the same recipients | Ask the user; `--allow-duplicate` only with approval |
| `duplicate_check_unavailable` | The read-only `gmail` connection is missing or cannot read | `solercia-connect connect gmail <account>` |
| `write_unverified` | Gmail did not confirm the `POST`; it may have applied | Check Gmail > Sent and Drafts; never resend blindly |

Both writes run only in the canonical CLI and the library, over the official API. HTTP rejects
them with `write_not_available_over_http`, and `--allow-browser-fallback` is refused.

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
