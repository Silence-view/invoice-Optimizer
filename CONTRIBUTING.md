# Contributing

Thank you for your interest. This is a proprietary, source-available project
(see [LICENSE](LICENSE)). Issues and pull requests are welcome, but code is
accepted only under the contribution terms in the licence, and the repository
owner may decline any change. For anything security-related, follow
[SECURITY.md](SECURITY.md) instead of opening an issue.

## Development setup

Requirements: Python 3.11+, PostgreSQL 15+, `pdflatex` with the LaTeX packages
listed in the `Dockerfile` (on Debian/Ubuntu: `texlive-latex-base`,
`texlive-latex-recommended`, `texlive-latex-extra`, `texlive-fonts-recommended`,
`texlive-fonts-extra`, `texlive-lang-greek`, `tex-gyre`), and Poppler (`pdfinfo`, `pdftoppm`) for page previews.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python scripts/bootstrap.py          # writes an owner-only .env with generated secrets
bash scripts/local_postgres.sh                  # project-local cluster on 127.0.0.1:55432
.venv/bin/python -m invoice_optimizer init-db
.venv/bin/python -m invoice_optimizer demo --customers 8 --movements 2000
.venv/bin/python scripts/run_local.py           # API + worker on localhost
```

`scripts/local_postgres.sh` reads the generated `POSTGRES_PASSWORD` from `.env`
(or `INVOICE_DB_PASSWORD`). `INVOICE_PG_BIN` points it at your PostgreSQL
binaries and `INVOICE_PG_PORT` changes the port. Stop it with
`bash scripts/local_postgres.sh stop`.

Install the git hooks once:

```bash
pip install pre-commit
pre-commit install
```

## Running tests

The integration tests need a real PostgreSQL server and a real `pdflatex`;
they fail rather than skip when either is missing. Every test runs in its own
disposable `invoice_test_<uuid>` schema and never touches the application
schema.

```bash
export TEST_DATABASE_URL='postgresql+psycopg://invoice:<password>@127.0.0.1:55432/invoice_optimizer'
.venv/bin/pytest -q
```

Use a dedicated database (or cluster) for tests if you keep demo data you care
about. The full suite takes several minutes; while iterating, run a single file
such as `.venv/bin/pytest -q tests/test_workflow.py`.

Before opening a pull request:

- `.venv/bin/pytest -q` passes with no failures.
- `node --check` passes for every changed file in `src/invoice_optimizer/static/`.
- `pre-commit run --all-files` is clean (gitleaks, whitespace, large files,
  private keys).

### UI verification runners

`scripts/verify_*_ui.cjs` are fixture-based browser checks: they intercept every
network request and need no live data. `scripts/verify_*_live.cjs` drive a
running local console. Both need Node.js and Playwright, which are **not**
dependencies of this repository. Install them yourself, outside the project
tree or in a git-ignored `node_modules/`:

```bash
npm install --no-save playwright
npx playwright install chromium
node scripts/verify_operations_ui.cjs
```

`PLAYWRIGHT_CHROMIUM_EXECUTABLE` selects an existing Chromium where supported.
Runners write receipts and screenshots to `output/verification/`, which is
git-ignored; do not commit them.

## Coding invariants

These rules come from [docs/IMPLEMENTATION_CONTRACT.md](docs/IMPLEMENTATION_CONTRACT.md)
and are enforced by the test suite. A change that weakens one of them will not
be accepted.

- **Immutable versions.** Raw source bytes and observations are retained.
  Corrections append a new movement version; nothing rewrites history.
  Snapshot membership and frozen invoice documents never change after freezing.
- **Append-only audit.** Every command records actor and request IDs in the
  audit log. Audit and review events are never updated or deleted, including
  by `maintenance`.
- **Idempotent commands.** Source record identity is `(source, record_id,
  revision)`; equal retries are no-ops and conflicting payloads quarantine.
  API commands carry a `request_id` and replay their recorded result.
- **Optimistic concurrency.** Review commands send `expected_version`; a stale
  version is rejected, never merged silently.
- **Decimal money.** Amounts are `Decimal` (PostgreSQL `numeric`) with explicit
  currency and rounding. Never use binary floats for financial values.
- **NULL score is not zero.** A missing score, missing input or unknown send
  outcome is stored and shown as unavailable, never as `0`.
- **Deterministic controls win.** A model score cannot clear a failed rule or
  accounting control. "Verified valid" clears only a model concern.
- **No label leakage.** Features are point-in-time; truth labels never enter
  serving payloads. Historical assessments are never rescored by activation.
- **Human approval before dispatch.** Approval binds the exact PDF, sender,
  recipients, message and attachments by fingerprint. Any change invalidates an
  unissued draft and needs a fresh approval. Nothing is sent automatically
  without that approval.
- **Uncertain sends are not retried.** Ambiguous provider submissions are held
  for reconciliation. Provider acceptance, delivery, opening and human
  acknowledgement are separate evidence.
- **Serialization.** The customer-period lock serializes ingestion, correction,
  close and issuance. Workers claim jobs with `SKIP LOCKED` and leases.
- **Safe rendering.** LaTeX runs with shell escape disabled and a timeout, and
  every field is escaped. There is no fallback to a fake PDF. Template packs are
  pinned by SHA-256.
- **Synthetic by default.** Fixtures and demos use synthetic identities and
  `example.test` addresses. Every demo PDF is marked
  `SYNTHETIC DEMO / NOT A TAX INVOICE`.

## Style

- Follow the formatting of the file you are editing (`.editorconfig` sets the
  basics). Keep diffs focused; do not reformat unrelated code.
- Database changes go through an idempotent migration step in `db.py` and need
  a test that upgrades an existing schema.
- Documentation is written in English.
- Never commit `.env`, anything under `var/`, credentials, real email
  addresses, absolute local paths or generated `output/verification/` files.
