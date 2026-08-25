# Connect Microsoft Teams transcripts

The official contract supports transcript list/download for a **known online meeting ID**. It
requires a Microsoft work/school account and delegated `OnlineMeetingTranscript.Read.All`.
Personal accounts are unsupported; tenant administrators can disable Graph transcript access.

## Connect

```bash
export SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID='PUBLIC_NATIVE_CLIENT_ID'
export SOLERCIA_CONNECT_MICROSOFT_TENANT='organizations'
make doctor TYPE=teams ACCOUNT=user@example.com
solercia-connect connect teams user@example.com
```

The Teams grant remains distinct from Outlook. Solercia Connect uses `User.Read` for identity and
`OnlineMeetingTranscript.Read.All` for transcript access.

Official endpoint and permission reference:
<https://learn.microsoft.com/en-us/graph/api/onlinemeeting-list-transcripts?view=graph-rest-1.0>.

## Read a known meeting

```bash
solercia-connect run \
  --provider microsoft.graph \
  --capability meeting.transcript.get \
  --meeting-id MEETING_ID \
  --account user@example.com
```

A 403 on transcript list/content becomes `tenant_policy_blocked` with admin remediation. It never
falls back around tenant policy. Microsoft browser fallback may verify a Teams session, but its
built-in extraction remains `not_implemented`.

## Live proof

```bash
export SOLERCIA_CONNECT_LIVE=1
export SOLERCIA_CONNECT_LIVE_TYPE=teams
export SOLERCIA_CONNECT_LIVE_ACCOUNT=user@example.com
export SOLERCIA_CONNECT_LIVE_MEETING_ID='MEETING_ID'
make test-live TYPE=teams ACCOUNT=user@example.com
```

Use a sanitized dedicated meeting with a known transcript. Live scope, admin policy, and content
stability remain unproven until this succeeds and evidence is retained.
