# Connect Gmail to Invoice Optimizer

Invoice Optimizer can send real email through Gmail, in **test mode only**. Every
message that leaves the computer goes to **your connected Gmail account and no
one else**; the customer address it would have reached is kept in the
`X-Original-To` header and in the subject prefix `[TEST → original]`. Invoices
are still sent only after a person approves them. Per-case review alerts stay on
the local simulator, so your inbox receives invoices, the month-end approval
reminder and test messages, not one alert per flagged movement.

Connecting Google does not by itself change the delivery provider. You choose
Gmail with `INVOICE_PROVIDER=gmail` (see "Turn on Gmail delivery" below).

## Owner checklist (about 10 minutes)

These steps need your Google account, so the application cannot do them for you.

1. **Enable the Gmail API.** In [Google Cloud Console](https://console.cloud.google.com/)
   select or create a project, open **APIs & Services → Library**, search for
   **Gmail API** and click **Enable**.
2. **Consent screen.** Open **Google Auth Platform → Branding / Audience**.
   Choose audience **External**, publishing status **Testing**, and under
   **Test users** add the Gmail address you will connect (the one set in
   `INVOICE_GMAIL_EXPECTED_EMAIL`). Fill in app name and the two contact emails.
3. **Scopes.** Under **Data access**, add `openid`, `email` and
   `https://www.googleapis.com/auth/gmail.send`. Nothing else.
4. **OAuth client.** Open **Clients → Create client**, type **Web application**.
   Under **Authorized redirect URIs** add exactly:

   ```text
   http://127.0.0.1:8765/api/mail/gmail/callback
   ```

   (Not `localhost`, no trailing slash. `INVOICE_PUBLIC_BASE_URL` must be
   `http://127.0.0.1:8765`.) Click **Create**, then **Download JSON**.
5. **Upload.** Start the app (`python scripts/run_local.py`), open
   http://127.0.0.1:8765/#email in a normal browser, sign in as an administrator,
   and use **Upload OAuth client JSON** with the downloaded file.
6. **Connect.** Click **Connect with Google**, choose the expected account and
   allow the requested permissions. Google returns you to the Email tab, which
   then shows the verified address.
7. **Test.** Click **Send test email** (or `POST /api/mail/gmail/test` with a
   `request_id`). Within a few seconds the message arrives in your Gmail inbox,
   addressed to you, with the original recipient in the subject.

The local `.env` of this workspace already selects `INVOICE_PROVIDER=gmail` and
`INVOICE_MAIL_REDIRECT=connected`. Until step 6 is done the app works normally:
approved invoices stay queued and the automation panel shows
"Connect Gmail first".

## Turn on Gmail delivery

| Setting | Value | Meaning |
|---|---|---|
| `INVOICE_PROVIDER` | `gmail` | Real sending through the Gmail API (`simulated` = local only) |
| `INVOICE_MAIL_REDIRECT` | `connected` (default) | Every real message goes only to the connected account |
| `INVOICE_MAIL_SEND_KINDS` | `INVOICE,DIGEST,TEST` | Which outbox kinds use Gmail; others stay simulated |
| `INVOICE_OPERATOR_EMAIL` | `operations@example.test` | Recipient of reminders/tests before redirect |

`INVOICE_MAIL_REDIRECT` may also name one address, but only the expected Gmail
address or one in `INVOICE_ALLOWED_EMAIL_DOMAINS`. `off` is refused while
`INVOICE_SYNTHETIC=1`: the application will not start, and the outbox will not
submit, without a redirect. Restart `scripts/run_local.py` after editing `.env`.

Outcomes: Google's acceptance is `ACCEPTED` with the Gmail message id. A 4xx
refusal is `REJECTED` (nothing was sent). A timeout, 5xx or lost connection is
`UNKNOWN`; it is never resent automatically, because the send-only permission
cannot look messages up. Check the Gmail **Sent** folder before any manual replay.

The connection requests `gmail.send`, `openid` and `email`. It can identify the
authorised account and send mail as that account; it does not request
permission to read the inbox, delete messages or manage Gmail settings.
[Google's scope reference](https://developers.google.com/workspace/gmail/api/auth/scopes)
describes the send permission.

## The supplied credential type

The credential supplied for this setup was identified from its metadata as a
**service-account key**. This application requires a **Web application OAuth
client JSON** containing a top-level `web` object; the upload validator rejects
service-account credentials before saving or using them.

Google documents domain-wide delegation as a Google Workspace administrator
capability, with the impersonated user belonging to that Workspace account.
A personal `@gmail.com` mailbox is a consumer account. Consequently, that
service-account delegation route cannot authorise this personal mailbox; use
the user-consent flow below. Create the OAuth client in the existing Cloud
project; the service-account key cannot be converted into that client.
See Google's [delegation requirements](https://developers.google.com/identity/protocols/oauth2/service-account#delegatingauthority)
and [consumer-account definition](https://docs.cloud.google.com/architecture/identity/overview-google-authentication#google-for-consumers).
The supplied private key has not been used to request a token or access Gmail.

## One-time Google configuration

Use a normal Chrome, Edge, Firefox or Safari browser for both the local app and
Google consent. Embedded browsers may be rejected by Google's OAuth policies.
Open **http://127.0.0.1:8765/#email** in that browser and sign in to the local app.
Start the Google flow from the same browser; copying only its authorisation URL
to another browser would omit the browser-binding cookie and fail the callback.
[Google OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies)
explain the external user-agent requirement.

1. In [Google Cloud Console](https://console.cloud.google.com/), select or create
   a project for this local demonstration and enable the **Gmail API**.
2. Configure the application's OAuth consent information in Google Auth Platform.
   For a personal `@gmail.com` account use an **External** audience. During
   development, add the expected account shown in Invoice Optimizer as a test user.
   Supply your own app name, support email and developer contact information.
3. Configure the requested scopes: `openid`, `email` and
   `https://www.googleapis.com/auth/gmail.send`.
4. Open **Google Auth platform → Clients → Create Client** and choose
   **Web application**. Add this exact **Authorized redirect URI** for the
   native local stack:

   ```text
   http://127.0.0.1:8765/api/mail/gmail/callback
   ```

5. Download the OAuth client's JSON configuration. In the local app, open
   **Email setup → Upload OAuth client JSON**. Upload it there, not in chat.
   This is the OAuth application configuration, not your Gmail password, an API
   key, a service-account key or a Desktop application client.
6. Choose **Connect with Google**, inspect the account and permissions, then
   continue to Google's consent page. After consent, Google returns to the local
   app. The page shows the verified account once authorisation succeeds.

Google's [credential creation guide](https://developers.google.com/workspace/guides/create-credentials)
describes client registration and redirect URIs. The console navigation labels
may change; the exact client type, scopes and callback must match.

An OAuth client JSON and the user's Google consent are both required. The local
application cannot invent a registered client or grant access to an account by
knowing its email address. A compatible Web OAuth client JSON and interactive
user consent are still needed to complete this setup; the supplied
service-account key does not satisfy the Web OAuth client requirement.

## Account and deployment settings

`INVOICE_GMAIL_EXPECTED_EMAIL` pins the expected account. The local user's supplied
address has been saved only in `.env`. The UI displays it and the backend checks
the verified address returned by Google. Signing in as a different account fails
instead of silently binding the wrong mailbox.

`INVOICE_PUBLIC_BASE_URL` defines the application's canonical origin. It is never
inferred from an untrusted Host header. For the current native stack it is
`http://127.0.0.1:8765`. HTTP is accepted only on loopback; other deployments need
HTTPS. An HTTPS origin always marks every cookie Secure and enables HSTS; live
(non-synthetic) mode refuses to start without one. Uploading the client JSON,
connecting and disconnecting require an administrator account.

Docker uses `INVOICE_DOCKER_PUBLIC_BASE_URL`, defaulting to
`http://127.0.0.1:8000`. Its registered callback must therefore end with
`:8000/api/mail/gmail/callback`. Do not mix `localhost` and `127.0.0.1`: cookies
are host-bound. Use the canonical workspace link displayed in Email setup.

## Credentials, expiry and disconnect

The backend stores client configuration, the pending flow and authorised tokens
under `var/gmail/`, with a private directory and owner-only files. They are
excluded from version control and the Docker build context. Files are atomically
replaced and locked across processes. Filesystem permissions are not encryption
at rest or a production secret manager. Protect the machine and any backup copy.

The flow uses a one-use state value, PKCE S256, a ten-minute lifetime and a separate
HttpOnly browser-binding cookie. Setup commands require a personal operator
login and CSRF validation. The browser binding is signed with the separate
private server key and identifies the initiating operator and credential
version. The callback rechecks that account and records the same operator and
flow ID, even when the main Strict session cookie is absent. Tokens stay on the
server; status responses return only safe
connection metadata. Callback responses remove the code from the address bar
with a fixed redirect and disable referrer propagation. Native Uvicorn access
logging is disabled so callback query parameters are not written to its logs;
deployment proxies must independently redact those parameters.

Offline access permits refresh without another interactive login, subject to
Google's grant lifetime and revocation rules. For an External consent app in
**Testing**, a refresh token involving Gmail scopes normally expires after
**seven days**. That setup is suitable for this connection test, not a reliable
month-end unattended deployment. Before monthly real sending, establish the
appropriate publishing/verification status and account policy; moving to
production is not proof that all verification requirements have been satisfied.
[Google's token-expiration guidance](https://developers.google.com/identity/protocols/oauth2#expiration)
documents the testing restriction and other revocation reasons.

**Disconnect locally** removes locally retained account tokens and pending flow
data. It retains the OAuth application's client configuration for reconnection.
It does not claim to revoke Google's grant remotely. To
remove that grant as well, use your
[Google Account connections](https://myaccount.google.com/connections).

## What was verified

Automated tests exercise the protocol with simulated Google responses, private
file permissions, state/binding/expiry/replay checks, scope and verified-account
validation, token refresh, route authentication, CSRF, upload limits and the
absence of email side effects from connecting. Delivery tests cover the
redirect (multiple To, Cc and Bcc collapse to the connected address), rows
staying queued while Gmail is not connected, the impossibility of sending
without a redirect, unchanged invoice fingerprints, and UNKNOWN/REJECTED error
classification. These checks do not establish a successful real Google
authorisation; that requires the owner's registered client and consent.

## Explicit service-account API diagnostic

On 27 October 2026, the user explicitly requested one real test message using
the supplied service-account identity, without delegated user impersonation.
Google's token endpoint accepted the signed assertion (HTTP 200). The Gmail
`users/me/messages/send` endpoint rejected the message with HTTP 400,
`FAILED_PRECONDITION` / `failedPrecondition`. No message was accepted for sending.
This proves that this key could authenticate that service account at test time;
it does not establish mailbox access, an OAuth connection to the personal Gmail
account, successful submission or delivery.

The redacted diagnostic receipt (`output/verification/gmail-api-service-account-test.json`)
contains no credential, access token or signed assertion. The one-off diagnostic
script refuses to overwrite its receipt and does not retry submission. The
application never uses that service-account key; Gmail delivery uses only the
OAuth connection above. The supplied credential
is owner-readable only and excluded from Git and the Docker build context.
