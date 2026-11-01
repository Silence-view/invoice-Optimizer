# Changelog

All notable changes to this project are documented here. Versions use the
`YYYY.MM` release scheme.

## [2026.11.1] - 2026-11-01

Gmail delivery in test mode, automatic data refresh and month-end email, a
cleaner demo dataset and a market-style console. The suite has **823 tests**.
Human approval before any dispatch is unchanged.

### Added

- **Gmail sending** (`gmail.py`, `delivery.py`, `INVOICE_PROVIDER=gmail`).
  `send_raw` posts to the Gmail API send endpoint. A 4xx answer is a definite
  `REJECTED`; a timeout, 5xx, lost connection or missing message id is
  `UNKNOWN` and is never reconciled automatically (check the Sent folder).
  Messages are capped at 4 MiB.
- **Mandatory test redirect** (`INVOICE_MAIL_REDIRECT`, default `connected`).
  Every real message goes only to the connected Gmail account: `To` is replaced,
  `Cc`, `Bcc` and `Reply-To` are removed, the original recipients are kept in
  `X-Original-To` and the subject prefix `[TEST → …]`. `off` is refused in
  synthetic mode at start-up and again at send time. The audit records the
  original recipients and a hash of the target, never the plain address. The
  stored payload and invoice fingerprint are unchanged.
- **Per-kind routing** (`INVOICE_MAIL_SEND_KINDS`, default
  `INVOICE,DIGEST,TEST`): other kinds, such as review alerts, stay on the
  simulator. While Gmail is not connected, real-provider rows stay `READY` and
  nothing is issued.
- **Admin test email**: `POST /api/mail/gmail/test` (CSRF-protected,
  idempotent by request ID) queues one TEST message through the same redirect.
- **Automatic data refresh** (`inbox.py`): the worker polls `var/inbox/` for
  settled data files with a `<file>.json` sidecar, skips links, takes a file
  lock and replays identical content. An optional synthetic demo feed writes
  cumulative monthly snapshots (`INVOICE_DEMO_FEED_SECONDS`, 900 s in
  `run_local`). New settings `INVOICE_INBOX_SECONDS`, `INVOICE_INBOX_MAX_FILES`,
  `INVOICE_DEMO_FEED_ROWS`.
- **Month-end email**: after each preparation cycle the worker queues an
  "N invoices await approval" reminder (`INVOICE_APPROVAL_DIGEST`,
  `INVOICE_OPERATOR_EMAIL`), at most once per period per London day. It never
  approves anything.
- **`GET /api/automation/status`** and CLI `automation-status`, `inbox-scan`.
- **Demo scenario profiles**: `demo --scenario-profile`
  (`model-evidence` by default) leaves out cases the logistic regression can
  never score (missing feature, invalid amount or timestamp, conflicting
  duplicate). `full` keeps the previous dataset; `model-only` keeps only model
  flags. Production code still treats a missing score as unknown, never zero.
- **Console**: market-style layer (`market.css`), area trend chart with range
  control, three-band risk bars, automation card (next month-end run, data
  refresh, email transport, redirect banner), Email setup progress strip and
  test button, invoice status filter chips, bottom tab bar on phones.

### Changed

- The command palette is 720 px wide and centred (a floating panel with an
  8 px gutter on phones) instead of sticking to the top-left corner below
  640 px. It has filter pills, grouped results, highlighted matches and a
  "Find movement" entry for movement IDs.
- `dispatch_approved` and manual "Queue now" do nothing (409 "Connect Gmail
  first") while Gmail is selected but not connected.
- The Review queue opens highest risk first; probabilities near 1 show
  `>99.9%` instead of `100.0%`; Insights opens on the last complete month.
- README screenshots retaken from the regenerated demo.

### Fixed

- With Gmail connected but no access token available, the worker no longer
  reserves and releases the same outbox row in a loop: Gmail rows stay `READY`
  with no attempt recorded until a token is available.

## [2026.11] - 2026-11-01

Release overhaul: invoice formats, insights, database scale work, security
hardening and a redesigned console. The regression suite grew from 627 to
**796 passing tests**. Retained synthetic PDFs and earlier receipts still
verify byte for byte.

### Added

- **Invoice template packs and format profiles** (`formats.py`,
  `config/invoice_profiles/`, `docs/INVOICE_FORMATS.md`).
  - New default `uk-modern-v1` layout: colour header, "Bill to" and "Invoice
    details" cards, striped line table, a single "Amount due" panel, payment
    card with optional IBAN/BIC and logo, "Page N of M" and the synthetic
    banner on every page. A4 and US Letter, deterministic bytes.
  - The legacy layout is kept as `uk-classic-v3`; its template is unchanged
    byte for byte. Documents without a `presentation` key keep their original
    render identity.
  - Packs are pinned by SHA-256 and refused if the installed files drift.
  - Profiles are validated strictly: field whitelists, WCAG 4.5:1 colour
    contrast, logo path, size and type limits, escaped labels of at most 80
    characters, and required quantity, unit price and VAT columns.
  - The resolved profile, pack, profile and logo hashes are frozen into each
    draft and covered by the approval fingerprint. Restyling a draft marks the
    old one STALE and requires a fresh human approval.
  - CLI: `format list`, `format validate PROFILE`, `format preview`.
  - Settings: `INVOICE_FORMAT_PROFILE`, `INVOICE_PROFILE_DIR`.
- **Insights engine** (`insights.py`, `docs/INSIGHTS.md`): overview, supplier
  concentration (exact HHI), duplicate candidates, robust amount outliers
  (median/MAD), Benford first-digit test with exact p-value, trends, risk
  distribution, review ageing and data quality. Each report runs in one
  read-only `REPEATABLE READ` snapshot with bounded limits. NULL model scores
  are reported as unavailable, never as zero. A DuckDB path computes the same
  results from checksum-verified Parquet exports.
  - CLI `insights --period YYYY-MM [--customer] [--limit] [--json PATH]`.
  - Read-only API `/api/insights/periods`, `/api/insights/report` and
    `/api/insights/{section}`, plus a new Insights view in the console.
- **Maintenance command** (`maintenance.py`): `invoice-optimizer maintenance`
  is a dry run unless `--apply` is given. It prunes old jobs and commands in
  audited batches and never touches audit or review events, versions or
  invoices.
- **Console interaction layer**: command palette (Cmd/Ctrl+K) and keyboard
  shortcuts, Appearance panel (theme, six OKLCH accent presets, glass or solid
  surface, density, motion, sidebar), accessible SVG charts with text
  summaries and keyboard tooltips, workflow funnel, delivery donut, activity
  feed, toast stack, in-page confirmation dialogs, Back-button navigation and
  screen-reader announcements.
- **Database settings**: `INVOICE_DB_POOL_SIZE`, `INVOICE_DB_MAX_OVERFLOW`,
  `INVOICE_DB_POOL_RECYCLE_SECONDS`, `INVOICE_DB_STATEMENT_TIMEOUT_MS`,
  `INVOICE_DB_LOCK_TIMEOUT_MS`, `INVOICE_DB_IDLE_TX_TIMEOUT_MS` and an optional
  `DATABASE_READ_URL` for read-only list endpoints. Timeouts apply to API
  sessions only, never to the worker or CLI.
- **Dedicated setup secret** `INVOICE_SETUP_TOKEN` for creating the first
  administrator, separate from the shared read-only API token.
- Repository hygiene: proprietary `LICENSE`, `SECURITY.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, GitHub CI and secret-scanning
  workflows, Dependabot, issue and pull request templates, `.editorconfig`,
  `.gitattributes` and pre-commit hooks.

### Changed

- **Schema version 8** (idempotent migration): 12 redundant indexes dropped;
  9 targeted B-tree and 7 BRIN indexes added; invoice list columns
  (`line_format`, `movement_count`, `movement_vat_policy`) added and backfilled
  from the frozen document. New ids are time-ordered UUIDv7 (still 32 hex
  characters).
- **Cursor pagination** on the cases, invoices, outbox and audit lists. Offset
  requests behave as before and add an `X-Next-Cursor` header; `cursor=`
  returns `{items, next_cursor, limit}`. Cursors use an index seek rather than
  a filtered rescan. Lock or statement timeouts return 503 with
  `Retry-After`.
- Quarantine cases and alerts are inserted in batches of 500; monthly close
  caches a coverage pass per source delivery.
- Seller details now come from the active format profile instead of a
  hard-coded constant.
- **Visual redesign**: OKLCH design tokens with light and dark themes,
  rewritten stylesheet, grouped navigation with icons, glass surfaces, motion
  that respects reduced-motion settings, minimum 12px text and 342 colour
  pairs checked for contrast.
- `/health` returns only `{"status":"ok"}`.

### Security

- The default database DSN contains no password; `scripts/bootstrap.py`
  generates the database password and setup token into an owner-only `.env`
  and never overwrites an existing one. `scripts/local_postgres.sh` refuses to
  create a cluster without a generated password.
- New response headers: `Permissions-Policy`,
  `Cross-Origin-Opener-Policy: same-origin`, and HSTS when the public base URL
  is https. Cookies are Secure whenever the public base URL is https.
  Non-synthetic mode refuses to start without an https base URL.
- Gmail configure, connect and disconnect require an administrator. Shared
  read-only sessions no longer see the expected or connected Gmail address,
  workspace URL or callback URL.
- `.gitignore` and `.dockerignore` use generic credential patterns; test
  fixtures use `example.test` addresses only.

### Fixed

- Insights no longer loosen a stricter configured statement timeout.
- `insights --json` writes through a unique temporary file, so concurrent runs
  cannot publish a mixed report.
- A separate read engine is now disposed on shutdown.
- `scripts/verify_streaming_workload.py` no longer reads lists from an
  inherited `DATABASE_READ_URL`.

### Known limitations

- A two-million-movement run has not been executed or certified; the 20,000
  movement benchmark shows no measurable speed change at that size.
- New indexes are built inside `init-db` without `CONCURRENTLY`; on a large
  live database create them concurrently first.
- Pruning old commands means a very old `request_id` runs again instead of
  replaying.
- The Docker image build was not executed in this release.
- Production tax decisions, credit notes, FX, many-to-many matching and real
  source adapters remain unsupported.

## [0.1.0] - 2026-10-27

Initial executable synthetic release: ingestion of two synthetic sources,
deterministic controls, frozen Logistic Regression assessment, human review
workbench, monthly close, LaTeX/PDF invoices, approval-bound dispatch through a
transactional outbox, Parquet/DuckDB analytics and the operations console.
