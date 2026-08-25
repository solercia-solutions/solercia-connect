# Connect Outlook

Use official Microsoft Graph device authorization first. Browser fallback is an explicit session
fallback and does not currently provide built-in extraction.

## Prepare

```bash
export SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID='PUBLIC_NATIVE_CLIENT_ID'
export SOLERCIA_CONNECT_MICROSOFT_TENANT='organizations'
make doctor TYPE=outlook ACCOUNT=user@example.com
```

Microsoft public/native device-code clients do not require a client secret. Request the device
and user code only by invoking connect:

```bash
solercia-connect connect outlook user@example.com
solercia-connect status outlook user@example.com
```

Open the verification URL, enter the displayed user code, and complete consent/MFA. Polling is
bounded by attempts, local delay, and expiry. The connection is saved only after Graph observes
`user@example.com` as the unique identity.

Microsoft device authorization reference:
<https://learn.microsoft.com/en-ie/entra/identity-platform/v2-oauth2-device-code>.

## Explicit browser fallback

Use this only after an allowlisted admin-consent/capability failure:

```bash
make setup-browser
solercia-connect connect outlook user@example.com --allow-browser-fallback
```

The user performs login/MFA in the dedicated profile. `connected_browser` means the account-bound
session was verified; `extraction_status: not_implemented` remains authoritative.

## Validate a dedicated account

```bash
export SOLERCIA_CONNECT_LIVE=1
export SOLERCIA_CONNECT_LIVE_TYPE=outlook
export SOLERCIA_CONNECT_LIVE_ACCOUNT=user@example.com
export SOLERCIA_CONNECT_LIVE_START='2026-08-01T00:00:00-05:00'
export SOLERCIA_CONNECT_LIVE_END='2026-09-01T00:00:00-05:00'
make test-live TYPE=outlook ACCOUNT=user@example.com
```

The range is exact and end-exclusive. Do not use a production mailbox for first validation.
