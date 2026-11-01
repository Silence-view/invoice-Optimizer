# Security policy

## Reporting a vulnerability

Please do **not** open a public issue, discussion or pull request for a
security problem.

Report it privately through GitHub Security Advisories:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability** (private vulnerability reporting).
3. Describe the issue, the affected version or commit, reproduction steps and
   the impact you expect.

Do not include real customer data, real credentials or real email addresses in
a report. Synthetic examples (`*.example.test`) are enough.

You can expect an acknowledgement within 5 working days and a status update at
least every 14 days until the advisory is resolved or closed. Please give us a
reasonable time to fix the issue before any public disclosure. We will credit
reporters in the advisory unless you ask us not to.

## Supported versions

| Version | Supported |
|---------|-----------|
| 2026.11 (current `main`) | Yes |
| Earlier snapshots | No |

Only the latest release on `main` receives security fixes.

## Scope

In scope: the FastAPI application, the worker, authentication and session
handling, CSRF and origin checks, the Gmail and SMTP adapters, the LaTeX
rendering pipeline (shell escape must stay disabled), template packs and format
profiles, the scripts in `scripts/` and the Docker definitions.

Out of scope: findings that need an attacker who already controls the host,
the `.env` file or the database; issues in third-party dependencies without a
demonstrated impact here (report those upstream); and the synthetic demo
documents, which are deliberately not valid tax invoices.

## Secret handling rules

- Secrets live only in `.env` (owner-only, created by `scripts/bootstrap.py`)
  and in `var/private/`. Both are git-ignored and docker-ignored. Never commit,
  paste, log or attach them.
- `.env.example` contains placeholders only. Copying it never creates a secret.
- Google OAuth client files and service-account JSON keys must stay outside the
  repository. Pass them by path at runtime; never place them under `output/`,
  `docs/` or `tests/`.
- Tests and fixtures use synthetic identities and `example.test` addresses
  only. No real customer data, invoices or email addresses.
- The repository runs `gitleaks` in CI (`.github/workflows/secret-scan.yml`)
  and as a pre-commit hook (`.pre-commit-config.yaml`). Run
  `pre-commit install` once after cloning.
- If a secret is committed by mistake, treat it as compromised: rotate it
  first, then remove it from history. Deleting the file in a later commit is
  not enough.

## Key rotation

Rotate a credential immediately if it may have been exposed, and in any case
before the repository is first made public.

- **Google service-account key:** in Google Cloud IAM, create a new key for the
  service account, switch the deployment to it, then delete the old key. Delete
  any local copies of the old JSON file.
- **Gmail OAuth client secret:** reset the secret in the Google Cloud console,
  reconfigure Gmail as an administrator in the console, then reconnect.
- **`INVOICE_API_TOKEN` / `INVOICE_SETUP_TOKEN`:** generate new random values
  (at least 32 characters, different from each other) in `.env` and restart the
  API and worker. See `docs/OPERATIONS.md`.
- **Session signing key (`var/private/session-signing.key`):** replace the file
  and restart; every existing session is signed out.
- **Database password:** change it in PostgreSQL, then update `DATABASE_URL`
  and `POSTGRES_PASSWORD` in `.env`.
- **SMTP password:** change it with the provider, then update `SMTP_PASSWORD`.
