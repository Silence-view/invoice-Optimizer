# Operating the synthetic invoice service

This manual describes the executable local release, not every component in the
target architecture diagrams. Keep `INVOICE_SYNTHETIC=1` and
`INVOICE_PROVIDER=simulated` for the supplied demonstration. Production activation
is blocked by configuration until the real source and tax contracts are
implemented. No production recovery guarantee or two-million-movement capacity
claim has been established.

## Runtime and operator controls

The application has a PostgreSQL service, an API process and a separate Python
worker process. Start the native API and worker together with:

```bash
.venv/bin/python scripts/run_local.py
```

The launcher prints its localhost URL. Its default port is 8765. Use
`--skip-postgres-start` when an existing configured PostgreSQL service is already
running. Sign in with a personal operator account. The one-time
`INVOICE_SETUP_TOKEN` bootstraps the first administrator and is rejected once any
operator exists; only when it is unset does `INVOICE_API_TOKEN` stand in. The
shared `INVOICE_API_TOKEN` login grants read-only access and never shows the
Gmail account addresses or the workspace origin. To rotate either token, replace
its value in `.env` and restart the API; existing shared-token sessions remain
valid until their 8-hour expiry.
Keep `.env` private. Native
logs are `var/api.log`, `var/worker.log` and `var/postgres.log`. Ctrl-C in the
launcher stops its own API and worker children; PostgreSQL remains running.
For Docker, the equivalent services are `api`, `worker` and `postgres`; the
console is bound to localhost port 8000. Docker execution has not been verified
on this machine; see the deployment evidence in the [README](../README.md).

| Control | Actual effect |
|---|---|
| **Overview → Manage automation** | A personal administrator can pause/resume automation, change preparation/dispatch/transport switches and bounded per-cycle budgets. Every saved revision records the operator and reason; no restart is needed. |
| Pause automation | Stops new worker, training, monthly-close, dispatch and transport claims. Already claimed work can finish; uploads and reads continue. Resume of monthly preparation may wait for the next 60-second cycle. |
| `INVOICE_AUTO_CLOSE=1` | Initial default for monthly preparation until an administrator saves runtime settings. A background thread attempts preparation immediately, then waits 60 seconds after each cycle; the default budget is 20 customer-period scopes. |
| `INVOICE_AUTO_DISPATCH=1` | Initial default enables issuance/queueing of currently approved packages, with a default budget of 10 per cycle. Approval therefore authorises automatic dispatch when runtime dispatch is enabled. |
| `INVOICE_AUTO_CLOSE=0` | Initial default disables automatic preparation. Manual close remains available. |
| `INVOICE_AUTO_DISPATCH=0` | Initial default disables automatic issuance of approved packages. Manual dispatch remains available, and already queued messages can still be submitted if transport is enabled. |
| Stop the worker | Pauses its scheduler, internal jobs and transport. The API can still create work; its simulation tick can also process work. |
| Stop both API and worker | Pauses application work, provided no CLI ingestion, training, export, demo or other worker is still running. Use this boundary for coherent backups. |
| CLI `tick` / UI simulation tick | Processes an existing bounded queue and transport batch. It does not run the monthly scheduler or approve/issue invoices. |

Saved runtime settings override environment defaults. The API and worker read the
same persisted revision. Concurrent edits with an old revision are rejected;
retrying the same actor-bound request does not create another audit event.
Restart both API and worker only after changing environment configuration. These controls are not emergency cancellation of a
submission already in progress. To stop all new work, stop all application
processes and CLI writers, then verify that they have exited.

The monthly thread uses separate database sessions and the shared bounded
connection pool. A slow snapshot does not occupy the continuous job loop, but
both still compete for database capacity and customer-period locks. No maximum
scoring, alert or month-close latency is guaranteed. The console uses
Server-Sent Events with a two-second revision check, bounded tables and a shared
summary cache per API process. Hidden browser tabs suspend their stream and
revalidate on return; this is a display refresh interval, not an
end-to-end processing SLA.

Resource hints limit unnecessary list reloads. A conservative full revalidation
every 15 seconds also covers late database commits whose preallocated audit ID is
lower than an already visible ID, including detail-only changes with unchanged
summary counts. Server-side version checks remain authoritative between updates.

## Source intake and completeness

The real two input schemas are unknown. The implemented **test contract** treats
A and B as independent one-to-one observations of the same purchased service.
The movement identifier joins them; source record identity includes source,
record ID and positive revision. A is the authoritative amount only in this
test contract. It does not establish how a procurement intermediary should bill
real purchases, fees, disbursements or VAT.

Use CSV or Parquet for large deliveries. XLSX is a bounded interchange option,
not the operational database. The service retains the received file bytes in a
content-addressed object store, then parses those retained bytes. Source rows,
revision evidence and batch membership are persisted in PostgreSQL. Equal
replays do not duplicate the movement; conflicting identities or payloads are
quarantined. See the exact fields in the
[implementation contract](IMPLEMENTATION_CONTRACT.md).

Upload through **Data & model** or use an explicit CLI command, for example:

```bash
.venv/bin/python -m invoice_optimizer ingest var/synthetic/manual/source_a.csv \
  --source A --period 2026-09 --expected-rows 2040 --complete
```

The path and `2040` above are examples: take the exact physical row count from
the producer's manifest, including deliberate duplicate and malformed rows.
Do not substitute the number of logical movements or valid parsed records.
`--complete` is a declaration of a **full period snapshot**, not “this delta
file has finished uploading.” The latest A and B complete snapshots must cover
every previously observed source key at its latest revision. Merely matching a
physical row count is insufficient. Do not invent a completeness declaration
when the producer has not supplied one.

Ingestion queues assessment automatically. The worker reconciles source
observations, applies deterministic checks and persists the frozen Logistic
Regression result against the exact movement version. There is no implemented
external database connector or automatic filesystem watcher: new source files
must reach an ingestion command or endpoint before they can be processed.

## Daily monitoring and human review

**Overview** puts the next action and four operational counts first; detailed
verification remains expandable. **Invoices** opens as cards with customer,
period, amount, status and retained source links. Switch to List when comparing
rows. **Preview PDF** loads a single PNG page derived from the exact retained PDF.
Previous/Next navigates the document without downloading all pages. Preview
metadata and each page are bound to the inspected PDF hash; the original PDF
remains downloadable. Closing the view cancels its fetch and releases its image.
The server uses Poppler (`pdfinfo`/`pdftoppm`), a fixed 1600-pixel long edge and
a bounded derivative cache. PDFs over 32 MiB use the direct PDF link; missing
Poppler or render errors display an explicit message instead of a blank frame.

In a case, **Movement versions** supports swipe, arrow keys and Previous/Next
buttons. Five versions load at a time, with a server maximum of 10 per request.
Each version shows its recorded score, frozen model identity, reasons, exact
field changes and original source files. Missing model artifacts make the
explanation explicitly unavailable; the active model never substitutes for a
historical model. Historical versions are read-only. Navigation does not change
the review form's target. New data marks an open case as stale; refresh before
submitting. Every actual decision still validates the live version, personal
operator, reason and request identity on the server.

Automation's advanced limits include jobs and emails per tick, monthly scopes,
dispatches per cycle and the queue admission watermark. HTTP 429 on intake means
retry after the supplied delay. A rejected delivery has not created a batch;
an admitted file completes rather than failing halfway at the watermark. Input
with unknown row count has only an initial reservation, so the watermark is a
soft pressure control, not a hard queue-memory bound.

### Personal accounts and review history

Use one account per person. The first-account setup requires the private local
setup token, a unique username, display name and personal password. Only the
first administrator can be bootstrapped this way; subsequent accounts are
created by an authenticated administrator or a trusted local CLI operator.
Passwords are hashed; neither password hashes nor submitted secrets appear in
history responses. A display name is an account label, not proof of legal
identity. This local release does not implement Google SSO or corporate SSO.

Session cookies use a separate, automatically generated private server key in
`var/private/session-signing.key`, not the shared setup token. Keep the private
directory with the deployment and its protected backups; do not distribute it
to reviewers. This upgrade invalidates the old shared-token cookies, so the
browser requires a new sign-in once.

The header identifies the current account. The server derives the actor from
the authenticated account, never an editable review field. Shared-token sessions
and shared bearer tokens are read-only. Account deactivation invalidates future
requests from its existing sessions. Do not share personal credentials.

To provision an account from the trusted local shell, use the private password
prompt (passwords are not command-line arguments):

```bash
.venv/bin/python -m invoice_optimizer operator-create \
  --username reviewer.one --display-name "Reviewer One" --role reviewer
```

Each successful decision, including an unresolved decision, records a stable
actor ID and display-name snapshot, server UTC timestamp, case/movement/customer,
the exact reviewed business version and retained source references, decision and
reason, before/after states, and an idempotent request identifier. Bulk decisions
also retain the parent request; imported corrections retain the worklist hash and
row. Corrections append versions and events for affected cases. They do not
rewrite previous review events. Retrying a completed request does not create a
second approval or change its reviewer. A failed bulk item is reported separately
and never becomes a successful approval event.

Rejected single-case commands are retained as separate audit attempts with the
request identifier and intended action; they do not update the latest-review
projection. Imported worklist bytes are retained by SHA-256 and can be downloaded
from a referenced review event. New structured history can be exported as CSV;
older shared-login audit records remain separately labelled in the history view.
An old command receipt created before identity binding may require refreshing
the case instead of replaying its original request ID; it is never reassigned to
the newly signed-in operator.

The per-case **Review history** is paginated. Latest-review fields on a case are
a convenience projection; the event history retains prior decisions. Historical
actions made with the old shared credential keep their recorded actor and audit
time, where available. Their individual human reviewer is unknown; migration
does not invent an identity or a missing review timestamp.

PostgreSQL rejects normal UPDATE, DELETE and TRUNCATE operations on
`review_events` and `audit_events`. These controls protect application history;
a database owner can disable triggers, so this is not an independent tamper-proof
archive or a regulatory certification. Automated processing continues to use
explicit system actors, distinct from personal reviewer accounts.

### Daily checks

1. In **Overview**, inspect pending jobs, open cases, quarantine, unavailable
   scores, failed work and uncertain delivery. Check that updates continue while
   the worker is running. `/health` checks API/database availability and returns
   only `{"status":"ok"}`; it is not proof that every queue is progressing.
2. In **Data & model**, compare declared and observed source counts and inspect
   both period deliveries. Review the model version and its evidence boundary.
   Diagnose an `UNAVAILABLE` score as missing evidence, never as probability zero.
3. In **Review queue**, inspect the original observations, current version,
   deterministic outcome, model score/features and case reason. Record an
   evidence-based explanation for each correction or resolution.
4. Corrections append a business version and trigger reassessment. They do not
   overwrite retained source files. A human “verified valid” decision can clear
   a model concern; it cannot override a missing counterpart, invalid source,
   mismatch or another failed deterministic control.
5. Review alert submissions in **Delivery**. New cases enqueue deduplicated
   alerts to the configured customer review contact. Alert generation does not
   wait for the monthly invoice approval checkbox. In this demonstration the
   messages are saved locally by the simulator.

For offline review, export the review CSV. Fill only `source_a_amount`,
`source_b_amount`, `description`, `expected_amount` and `review_reason` where a
change is intended. Preserve `case_id` and `expected_version`. Blank proposed
fields mean no change. Import is bounded to 1,000 rows and 5 MiB through the API.
Each changed row is an independent, idempotent, version-checked transaction;
inspect every rejected row. Importing a spreadsheet never approves an invoice.

Quarantine is handled through corrected source evidence and explicit batch
replacement, not a manual eligibility checkbox. The replacement must satisfy
the same-source/period completeness and revision checks and preserve known
records; history stays retained. An unidentifiable malformed row can block all
customers in its period because the service cannot safely attribute it. Such a
row remains held for adapter investigation if the available evidence cannot
satisfy the replacement contract.

## Model training, explanations and interactive review

The active model and the model recorded on a historical assessment are distinct.
In **Data & model**, `READY` means that the artifact checksum, feature order,
scaler, classifier and numerical probe passed validation. The worker heartbeat
is written at most once every five seconds and is marked stale after fifteen
seconds on the server. The visible status panel polls every five seconds even
without audit changes, pauses when hidden and reports unavailable after a failed
refresh. It preserves expanded run details and open forms. It is evidence that
the main loop was recently alive, not a guarantee
that every job or a separate training thread is making progress.

An administrator can queue 1,000–50,000 historical synthetic rows, choosing a
seed and serving month. Only one training run can be pending at a time. HTTP
returns a queued receipt immediately; a separate worker thread performs training
while the normal assessment loop continues. Native numerical work is capped at
one thread. The API simulation tick excludes training jobs. Trusted local shell
operators can queue the same operation with explicit local attribution:

```bash
.venv/bin/python -m invoice_optimizer train-synthetic \
  --rows 30000 --seed 20261027 --period 2026-09 \
  --request-id synthetic-training-20261027-example
```

Keep the request ID for retries of the same command; a different seed, row count
or period requires a new ID. Use queued training for normal operation. The legacy
`train FILE` and initial `demo` commands are trusted synchronous bootstrap tools;
run them with application writers stopped. They share the admission lock and
refuse to activate while a queued or unresolved run exists. They do not provide
the queued run's cross-store activation recovery workflow.

The training dataset is a separate ZSTD Parquet file
with retained hash and generator manifest. It does not regenerate or overwrite
the source deliveries. Labels describe deliberately separable generated risk
patterns, with 5% positive examples and a 24-hour label delay. They are not
observed fraud or evidence of actual customer behaviour.

The run records an `ACTIVATING` intent and its exact retained target before the
active file pointer changes. A process restart resumes that target without
retraining. If activation cannot be verified, `ACTIVATION_UNCERTAIN` blocks new
training requests. The worker tries at most three times per recovery cycle with
a thirty-second delay; an administrator can then use **Resume recorded activation** to
resume the same target. This makes the database/filesystem boundary recoverable;
it does not claim that those two stores share one atomic transaction.

Training uses chronological 60/20/20 partitions. Rows whose labels were not yet
available at a partition boundary are excluded. Scaling and fitting use only the
training partition; threshold selection maximises validation F2, then the frozen
threshold is evaluated once on the test partition. See the recorded split counts,
cutoffs, confusion matrix, precision, recall, average precision and costs. Perfect
synthetic metrics do not establish performance or calibration on real data.
The [scikit-learn guidance on leakage](https://scikit-learn.org/stable/common_pitfalls.html)
explains why preprocessing is fitted on training data only.

Activation changes future assessments. Existing scores, corrections, cases and
invoice packages remain frozen. Each new model assessment stores its six feature
values and a compact numerical explanation in PostgreSQL, with artifact hash,
model/policy IDs, intercept, logit, threshold and per-feature contributions.
Expanded labels and model parameters are loaded on demand rather than repeated
for every transaction. Historical records without this compact field are
explained from their frozen feature vector and exact retained artifact. If the
artifact cannot be verified or does not reproduce the score, the explanation is
shown as unavailable; the current active model is never substituted.

For feature `j`, the displayed contribution is
`coefficient[j] * (transformed_value[j] - training_mean[j]) / scaling_divisor[j]`.
The intercept plus all six contributions reconstructs the logit, and the sigmoid
of that logit reconstructs the score. The baseline is the vector of training
feature means; it need not describe any real movement. Contributions are in
log-odds, not percentage points or causal explanations. The class being scored
is the generated synthetic risk scenario.

Use the queue filters for customer, status, reason, severity, movement-ID prefix
(at least three characters), score range and ordering. Score ranges exclude
missing scores; they never treat them as zero. The CSV worklist uses the same
filters. **Inspect** is independent of the eligible-only bulk-release selection.
Pin up to eight cases, keep a separate draft per case and compare two or three
snapshots. Switching views does not silently refresh the reviewed version;
explicit refresh discards the selected case's draft after confirmation. Source
files, version IDs and review identity remain visible.

The **What-if sandbox** recalculates only the frozen model on a copy of six input
fields. It checks the expected business version and validates decimal amounts,
ages, cross-border values and UTC timestamps. It writes no assessment, source,
case decision or invoice and does not rerun reconciliation. A read-only session
can use this calculation; saved corrections and releases still require a personal
account. Notes and hypothetical inputs stay in bounded browser memory and clear
on sign-out. Do not rely on them as saved work after a page reload.

The active case automatically loads **Why this case was flagged** once for its
pinned snapshot. Reconciliation failures and missing data have their own
explanations; a high score never substitutes for those reasons. The model text
ranks the actual positive and negative fitted contributions, states the exact
threshold decision, and names the retained model/policy. Raw amounts, ages and
times are used only when all six derived features match the frozen assessment.
Otherwise the text uses verified transformed features or explicitly reports
unavailable evidence. Numeric calculations remain expandable. Hypothetical
previews receive a separate, clearly labelled description and are never saved.

## Additional source-only examples

`scripts/seed_invoice_examples.py` supplies three reserved synthetic customers
and 72 August 2026 movements, delivered as two matching 72-row CSV files. These
amounts are demo processing fees, not purchase principal. August is a separate
completed month; existing September exceptions remain intact. The script checks
the active model, source identities, existing period data and file hashes before
loading anything. Conflicting inputs are rejected, and an exact replay reuses
the existing sources and drafts.

Stop this project's launcher with Ctrl-C and allow its final heartbeat to age
past 30 seconds before the first load. This lets both complete deliveries arrive
before the normal worker evaluates them. Then run:

```bash
.venv/bin/python scripts/seed_invoice_examples.py ingest
.venv/bin/python scripts/run_local.py --skip-postgres-start
```

The loader creates only customer/source data and normal assessment jobs. The
worker reconciles and scores it; the ordinary monthly scheduler creates invoices
when all gates pass, usually by its next 60-second cycle. No direct invoice
creation, rendering, approval or email dispatch runs in the loader. Keep the
launcher running; in another terminal, optionally collect verified copies:

```bash
.venv/bin/python scripts/seed_invoice_examples.py collect --output output/pdf/flow-generated-examples
```

Collection is read-only with respect to the database. It checks exact membership,
totals, source manifests, model assessments, frozen snapshot, package and PDF
hashes, and copies the original scheduler-produced bytes. It refuses incomplete,
approved or issued examples. The three gross totals are GBP 131.70, GBP 516.60
and GBP 1,316.70. All three stay DRAFT until a named operator approves them.
Inputs and their manifest are retained in `var/synthetic/invoice-examples-v1/`.

## Automatic refresh and month-end automation

The worker started by `scripts/run_local.py` runs everything below without any
button press. Pause automation stops all of it; uploads and reads keep working.
`GET /api/automation/status` returns the next month-end date, the last report of
each step, invoice counts awaiting approval, mail readiness and refresh state.

| Step | When | What it does | What it never does |
|---|---|---|---|
| Data inbox | every `INVOICE_INBOX_SECONDS` (30) | Ingests settled file + `<file>.json` sidecar pairs from `var/inbox/`, at most `INVOICE_INBOX_MAX_FILES` (4) per cycle | Ingest the same content twice (content-addressed replay) |
| Demo feed | every `INVOICE_DEMO_FEED_SECONDS` (900 in `run_local`, 0 elsewhere) | Writes cumulative whole-month A/B snapshots of the current month into the inbox as synthetic rows "arrive"; a final full snapshot after the month ends | Add movements the regression cannot score (missing features, quarantined rows) |
| Monthly preparation | every 60 s; due from 00:00 Europe/London on the 1st | Prepares DRAFT invoices for every ended month whose close checks pass | Approve anything |
| Approval reminder | after each preparation cycle, at most once per period per day | Emails `INVOICE_OPERATOR_EMAIL` "N invoices await approval" with numbers and totals | Change an invoice |
| Dispatch | every worker loop (2 s) | Issues and emails invoices a person has APPROVED | Touch DRAFT invoices; issue while Gmail is selected but not connected |

Inbox sidecar example, for `source_a.csv`, written last as `source_a.csv.json`:

```json
{"source": "A", "period": "2026-11", "expected_rows": 120, "complete": true}
```

Each delivery must contain the whole period so far, not only new rows: a month
close needs the latest file from each source to cover every known record.
Ingested files are deleted from the inbox (the object store keeps the bytes)
and a receipt is written to `var/inbox/processed/`. Invalid deliveries move to
`var/inbox/rejected/` with the reason, and are audited as `inbox_file_rejected`.
When the processing queue is full, files wait for the next cycle. The console
updates through the existing live stream as soon as a batch is recorded.

Accounting periods are UTC calendar months (the source contract), while
month-end preparation starts at 00:00 Europe/London. Under British Summer Time
London midnight is 23:00 UTC, so a draft can be prepared up to an hour before
the demo feed writes its final snapshot. When that snapshot arrives, the draft
is marked STALE (any approval is cleared) and the next cycle prepares it again;
nothing is sent in between.

If Gmail is connected but an access token cannot be obtained (for example the
Google token endpoint is unreachable), Gmail rows stay `READY` and no attempt
is reserved until a token is available again; simulated alerts keep flowing.

Real mail is in test mode: see [GMAIL_SETUP.md](GMAIL_SETUP.md). Every message a
real provider sends goes only to the connected Gmail account, with the original
recipient kept in `X-Original-To` and in the subject. Review alerts stay on
the simulator (`INVOICE_MAIL_SEND_KINDS`).

## Monthly preparation, review and issuance

New preparation defaults to `ITEMIZED`; `SUMMARY` can be selected explicitly in
the preparation form. Both formats retain the same accounting summary and
invoice-level VAT calculation. Itemised invoices include an additional section
inside the same PDF with every included movement, a sequential row number,
source event time displayed in UTC, business reference, full description,
supplier, net GBP amount, VAT rate, VAT amount and gross GBP amount. Long descriptions continue without repeating their
amount. Table headers and page numbers repeat across the detailed pages. The
summary page states how many movements are included on subsequent pages.

Each detail is frozen from the exact `MovementVersion` included in
`InvoiceMember`. Coverage, unique IDs, sequence, currency, event month and exact
net sum are validated before compilation. Each movement's VAT is calculated
with `ROUND_HALF_UP(net * rate, 0.01)` and gross equals net plus VAT. The
`line_half_up_reconciled_v1` presentation policy preserves the existing
invoice-level VAT. The detail footer shows movement subtotals, a signed
**Invoice VAT rounding adjustment** when nonzero, and reconciled invoice totals.
That adjustment equals invoice VAT minus the sum of movement VAT, and applies
to VAT and gross only. It is an explicit presentation reconciliation of the
monthly package charge, not a new source movement or a source tax classification.
The frozen `movement_vat` object retains the policy, rate, line VAT/gross sums
and adjustment. Each frozen row retains its own rate, VAT and gross. Decimal
arithmetic and renderer validation reject inconsistent rows or reconciliation.
New `ITEMIZED` requests above 10,000 movements fail before the detail list is
materialised. There is no silent summary fallback or truncated PDF. For larger
scopes, explicitly select `SUMMARY`; multiple detailed volumes and a scalable
full-detail attachment workflow are not implemented in this release.

For a current unapproved DRAFT, **Generate itemised replacement** (or the summary
equivalent) requires a personal session and confirmation. The command binds the
current package fingerprint, target format, request ID and actor. It validates
the current source snapshot, retains the old row as STALE, freezes a replacement
revision and queues the normal rendering job in one transaction. A failed
preparation rolls back the replacement. Old PDF, package, document and snapshot
bytes remain retained. Both sides receive an audit event identifying the old and
new revisions. Approved or issued invoices cannot use this action. The new
package remains DRAFT after rendering and needs its own explicit approval.

A historical net-only ITEMIZED draft can use **Add VAT breakdown**, targeting
ITEMIZED through the same endpoint. The command permits this same-format change
only when the old VAT presentation policy is absent. Repeating the upgrade on a
current VAT breakdown is rejected; retrying its original request returns the
original replacement. Historical documents and PDFs remain unchanged.

The package API returns header/accounting data and a 25-row preview, never the
full movement array. `GET /api/invoices/{id}/movements` pages through frozen
detail with a maximum of 100 rows per request. Polling the invoice list reads
compact format/count fields rather than loading all detailed documents.

Automatic preparation considers only months fully ended according to the
`Europe/London` calendar date. Current and future months are excluded. Persisted
cursors rotate through customers and periods so the first blocked 20 do not
starve later customers. The last bounded preparation and dispatch reports are
stored in `runtime_options` and returned in dashboard automation data; changed
nonempty outcomes are audited. Repeated identical outcomes do not add an audit
event every loop.

The billing transaction rechecks these gates; scheduler selection alone grants
no permission to bill:

- The customer is active, both source types have a latest complete,
  count-verified period snapshot, and that snapshot covers all known source
  record versions. Any active incomplete or failed period batch blocks close.
- No unresolved quarantine affects the customer or has unknown customer
  attribution. All period assessment/scoring jobs are finished, including those
  belonging to other customers in that same source period.
- Every movement for the customer-period is eligible, required model evidence
  or permitted model review is present, and no open customer review case remains.
- There are movements to bill, currencies and exact decimal totals are supported,
  and no current invoice or previously issued movement allocation conflicts.

A successful close freezes membership and amounts, reserves a demonstration
invoice number and queues real TeX/PDF compilation. The PDF uses an A4 UK
full-VAT layout with explicit `SYNTHETIC DEMO / NOT A TAX INVOICE` markings. The
current billing adapter uses one summary service line, GBP and a demonstration
20% VAT calculation; it is not a production tax decision. The renderer separately
supports non-VAT documents, but the automatic billing adapter does not select
that mode. An invoice is not proof of payment. See
[UK invoice format](UK_INVOICE_FORMAT.md) for official field references.

In **Invoices**, open the PDF and package. Check customer, recipient, sender,
period, totals, snapshot and email text. The approval binds the exact package
fingerprint, including PDF bytes and message. **With the default configuration,
approving is the final human action before automatic issuance and queueing.**
There is no guaranteed pause after approval. Disable automatic dispatch before
approving if a separate manual dispatch step is required.

The worker revalidates current coverage, snapshot and approval before issuance.
Issuance atomically records unique movement allocations and the invoice outbox
row; actual provider submission happens afterward. A source/correction change
invalidates an unissued draft. A `STALE` invoice can be replaced automatically
after gates pass. A `FAILED` invoice is held for investigation and does not
consume fresh invoice numbers or rendering jobs on every scheduler cycle.

Revocation works only before issuance. Changes after issuance require an explicit
accounting adjustment workflow, which this release does not implement. Do not
delete an issued invoice, edit allocations, recycle a reserved number or clear
an uncertain send to force the workflow forward.

## Delivery evidence and uncertain submissions

| State or evidence | Meaning and operator action |
|---|---|
| `READY` | The outbox contains work awaiting a submission attempt. It is not delivery evidence. |
| `IN_FLIGHT` / attempt `SUBMITTING` | A worker has reserved an external attempt. The outcome is not yet known. |
| `ACCEPTED` | The configured transport accepted submission. The local simulator writes an `.eml` and a provider journal; it does not contact a recipient. SMTP or Gmail acceptance also does not prove inbox delivery; the Gmail message id is the attempt's `provider_id`. |
| attempt `NOT_SUBMITTED` | A Gmail access token could not be obtained before submission. Nothing was sent; the row returned to `READY`. The worker checks the token before it reserves a Gmail row, so this appears only if the token fails between that check and the send. While the token is unavailable, Gmail rows stay `READY` with no attempt recorded. |
| `REJECTED` | The attempt has a reported rejection. Investigate the cause; no automatic resend is scheduled. |
| `UNKNOWN` | Acceptance may have occurred but its response was lost, or the send lease expired. Hold and reconcile; do not blindly retry. |
| `delivered`, `bounced`, `opened` event | Separate provider-event evidence. Events generated through the simulation controls are explicitly synthetic, not actual customer behaviour. |

The outbox lease is five minutes. An expired external submission becomes
`UNKNOWN`, not `READY`. Internal assessment/render jobs have a separate
ten-minute lease and can be reclaimed using claim tokens; this distinction
prevents treating an uncertain external side effect like a replayable internal
calculation.

To exercise a lost response, choose the simulator's `unknown` outcome and process
an authorized queued message. Inspect the attempt in **Delivery**. The simulator
stores `mail/<attempt_id>.provider.json` and `mail/<attempt_id>.eml` under
`INVOICE_DATA_DIR`. The authenticated reconciliation action
`POST /api/attempts/{attempt_id}/reconcile` reads the matching journal, checks its
identity/status, updates the attempt and records `resubmitted=false`. It does not
send a new message. Missing or inconsistent journal evidence keeps the attempt
held. Preserve those files for investigations and backup.

The optional SMTP adapter has no implemented provider lookup or production
webhook ingestion. Its unknown outcomes require external provider evidence and
a reviewed recovery procedure before any replay. No hidden tracking pixel or
human acknowledgement portal is implemented. An open signal, even from a real
provider, is a different claim from human receipt or reading.

## Model and failure handling

The deployed baseline is frozen Logistic Regression with a validation-selected
threshold and a chronological synthetic test. Scores, nullable availability,
features, model/policy versions and rule outcomes are persisted independently.
Human review does not trigger automatic training or promotion. Synthetic test
metrics are not evidence of production fraud/compliance detection or probability
calibration. Real labels, feature availability, error costs and validation remain
necessary; details are in [ML model](ML_MODEL.md).

Only trusted, locally generated model artifacts may be loaded. Preserve the
active `model/metadata.json`, all referenced version directories, artifact hashes
and the matching dependency versions together. Training changes the active
artifact selection; perform it as a controlled operator change with retained
metadata and an agreed effective period, not as an incident workaround. Historical
assessments remain tied to their recorded model version. A metadata badge alone
does not prove that every movement scored successfully.

| Symptom | Investigate without erasing evidence |
|---|---|
| Close blocked | Read the retained reason, source manifests, unfinished period jobs, quarantine and cases. Fix the originating evidence. |
| `UNAVAILABLE` model result | Inspect missing features, temporal cutoff, artifact availability/checksum and library compatibility. Do not substitute a zero score. |
| Failed internal job | Preserve its payload, exception and worker log. It stays failed; there is no generic operator retry command in this release. Repair/requeue requires a reviewed maintenance action. |
| Failed invoice rendering | Inspect the retained TeX/compiler logs, compiler path, supported characters and exact monetary values. Automatic preparation holds this scope. After diagnosis, an explicit manual close with a new request ID can create a new revision if all business gates pass; retain the failed revision and job. |
| Source change after issuance | Inspect the later source observations and case. Obtain an authorized adjustment/credit-note implementation; do not alter the issued package. |
| Dashboard is stale | Check API/SSE connectivity, worker logs and database availability. A responsive page does not establish queue progress. |
| Disk pressure | Measure objects, document attempts, mail journals, exports and database/WAL growth. No automatic retention deletion policy is implemented; do not remove referenced evidence. |

Work is bounded through ingestion chunks, scoring batches, SQL pagination and
streamed snapshots/exports. It is not bounded solely by the largest customer
count: database growth, exceptions, revisions, mail volume and concurrent closes
also matter. The measured run is 20,000 synthetic movements across 200 customers,
not two million for the largest customer. Use the exact measured stages and
limits in [performance](PERFORMANCE.md); do not extrapolate a production SLA.

## Coherent backup

A recoverable application point comprises the database **and** its referenced
files, code and model versions. A PDF-only export, a spreadsheet or an isolated
database dump is insufficient. The procedure below is a runbook; it is not a
claim that a complete application restore has already been tested.

1. Choose a private, access-controlled backup location outside the active data
   directory, with adequate free space. Record the operator and backup ID.
2. Stop API and worker and any separate ingestion, demo, training or export
   process. With the native launcher, stop it and confirm its children exited;
   with Compose, `docker compose stop api worker`. Verify no other writer is
   connected. Stopping only the scheduler flags does not pause queued sends.
3. Keep PostgreSQL running for the logical dump. Record any `IN_FLIGHT`,
   `SUBMITTING` or `UNKNOWN` attempt and preserve its journal; process termination
   is not proof that an external submission did not occur.
4. Dump the complete application database, including migrations, sequences,
   idempotency commands, batches/membership, all source/version/assessment/case
   rows, counters, invoice members/allocations, approvals, jobs, outbox, attempts,
   events, cursors/reports and audit history.
5. While writers remain stopped, archive `objects/`, `documents/`, `model/` and
   `mail/` from the configured `INVOICE_DATA_DIR`. Include `exports/` and
   `reports/` for retained analytical/verification evidence. Preserve any
   not-yet-ingested input separately. Capture the exact source tree, templates,
   dependency lock, schema version and relevant configuration. Protect secrets
   separately with restricted access; do not commit `.env` or print its contents.
6. Record database/server/client versions, selected model and hashes, application
   code/archive hash, backup timestamps and file inventory/checksums. Check command
   exit statuses and archive readability before marking the backup complete.
7. Restart the same configured application only after the complete backup point
   has been captured. Retain earlier backups according to an agreed policy.

`pg_dump` provides a consistent database snapshot, but it cannot coordinate this
application's external filesystem. Pausing writers is what keeps the two aligned
in this runbook. Custom-format dumps are restored with `pg_restore`. Use client
tools compatible with the server version and verify their actual exit status.
[PostgreSQL SQL-dump documentation](https://www.postgresql.org/docs/current/backup-dump.html).

For the supplied native development database only, the following illustrates a
logical backup after all application writers have stopped. Replace the backup
location, verify the configured data directory, and use a new destination. It
does not run an application restore or capture separately managed secrets:

```bash
set -e
umask 077
INVOICE_BACKUP_DIR="/path/to/new-private-invoice-backup"
INVOICE_BACKUP_DATA_DIR="$PWD/var"
INVOICE_BACKUP_PG_BIN="${INVOICE_PG_BIN:-/opt/homebrew/opt/postgresql@15/bin}"
mkdir "$INVOICE_BACKUP_DIR"
"$INVOICE_BACKUP_PG_BIN/pg_dump" \
  -h "$PWD/var/pg_socket" -p "${INVOICE_PG_PORT:-55432}" \
  -U invoice -d invoice_optimizer --format=custom \
  --file="$INVOICE_BACKUP_DIR/database.dump"
tar -C "$INVOICE_BACKUP_DATA_DIR" -czf "$INVOICE_BACKUP_DIR/application-files.tar.gz" \
  objects documents model mail exports reports
tar -czf "$INVOICE_BACKUP_DIR/application-code.tar.gz" \
  src scripts docs pyproject.toml requirements.lock.txt Dockerfile compose.yaml
"$INVOICE_BACKUP_PG_BIN/pg_restore" --list "$INVOICE_BACKUP_DIR/database.dump" \
  > "$INVOICE_BACKUP_DIR/database-contents.txt"
(
  cd "$INVOICE_BACKUP_DIR"
  shasum -a 256 database.dump application-files.tar.gz application-code.tar.gz \
    > SHA256SUMS
)
```

For Compose, the database dump can be taken with `docker compose exec -T postgres
pg_dump -U invoice -d invoice_optimizer --format=custom` into a private backup
file; capture the application volume separately while all app writers remain
stopped. Do not treat the Docker volume names as paths on the host.

Do not add the live native `var/postgres` directory or the live Compose
`postgres-data` volume to an ordinary file-copy archive and call it a database
backup. Physical backup needs PostgreSQL's appropriate shutdown/base-backup/WAL
or coordinated snapshot procedure. This runbook uses a logical dump instead.
[PostgreSQL filesystem-backup documentation](https://www.postgresql.org/docs/current/backup-file.html).

## Restore drill and restart gate

Restore to a **new isolated database and a new data directory**, using trusted
backup material and the captured application/dependency versions. Do not restore
over the running application. Keep the worker stopped, use a separate localhost
API port and preserve `INVOICE_PROVIDER=simulated`,
`INVOICE_AUTO_CLOSE=0` and `INVOICE_AUTO_DISPATCH=0` during inspection. Disabling
automatic dispatch does not disable submission of an existing ready outbox, so
do not start a transport worker merely to view the restored data.

Create a fresh target database, restore the custom dump with `pg_restore` and
restore the matching application files. For an isolated drill, an operator can
use `--exit-on-error --single-transaction --no-owner --no-acl` with a deliberately
chosen target database and restore role; review privileges separately before any
service restart. Inspect and resolve every restore error. Listing an archive is
not a restore test.
[PostgreSQL pg_restore documentation](https://www.postgresql.org/docs/current/app-pgrestore.html).

Before claiming recovery, verify:

- Backup archive hashes, complete source/object inventory, every referenced
  object's content hash, and required document JSON/TeX/log/PDF evidence.
- Restored schema and sequence state; row counts, movement-state counters and
  exact monetary totals; source/batch coverage, snapshot membership and unique
  issued allocations; command/idempotency and audit history.
- Model metadata/version/checksum compatibility and availability of historical
  artifacts. Retained assessment versions must not be relabelled as the newly
  active model.
- Each retained approved/issued package's fingerprint, recipient/message,
  snapshot hash and exact PDF bytes. Recompiling an invoice is not a substitute
  for recovering the approved bytes.
- Attempts, outbox states, provider journals and events, including externally
  accepted submissions after the restored backup point. A restored `READY` row
  is not proof that no later external send occurred. Reconcile external reality
  before enabling transport; never reset unknown attempts to ready blindly.

Exercise a bounded synthetic intake/review/render workflow in the isolated
environment and retain the results, elapsed restore time, detected gaps and
remediation. A successful local component test or readable dump does not prove
the entire recovery path. Recovery-time and recovery-point objectives remain
unverified until a full restore drill is completed and periodically repeated.
No such guarantee is claimed by this release.

## Customer filters, source files and bulk review

Review queue, Invoices, Delivery and Data & model each have a customer filter.
Filtering and pagination run on the server; only the displayed page is retained
by the browser. The Overview and explicitly labelled workspace summary cards
remain global. Case CSV exports follow both customer and status; the dispatch
register follows the selected customer. Unattributed quarantined input remains
visible in the unfiltered queue and is never assigned from an unvalidated payload.

Original source filenames link to authenticated downloads of the retained bytes.
The server verifies the content hash before serving an attachment. Inspect batch
opens its metadata even when the batch is outside the current Data page. A file
can contain multiple customers: the customer filter selects relevant batches;
downloading a batch retrieves the original whole file, not a filtered extract.
This console uses one authenticated operator scope, not per-customer access control.

Case references follow that case's version to its source record IDs and first
accepted batch. A repeated delivery remains visible under Data through delivery
membership, but does not replace the original evidence. Quarantine references
come from the quarantine batch. Invoice references follow frozen InvoiceMember
versions, never today's movement version. Schema version 4 adds a compact
invoice/file projection and supporting filter indexes. The projection is computed
once in PostgreSQL when a new snapshot is frozen, or on demand for an older
snapshot. Polling reads this projection instead of scanning all invoice members.
It does not alter an invoice document, package, fingerprint or approval. Missing
references are explicitly marked incomplete. Full invoice file lists are paginated.
The additive migration creates indexes normally; large production datasets need
a planned migration window or a separately managed concurrent-index rollout.

To release reviewed model concerns, select eligible cases on the current Review
page, then choose **Review selected releases**. Inspect each movement's version
and source evidence, enter a reason applying to every selection, and check the
explicit review confirmation. The UI allows at most its 50 displayed cases; the
API accepts 1–100 unique case/version pairs. Filters, page changes and explicit
refresh clear the selection. Live updates do not silently replace selected
versions or the open review form.

Each item commits independently and returns its own outcome. The response
separates resolved cases, released movements and failures. Stale or closed cases,
accounting failures, missing/incomplete model evidence, other open blockers and
issued movements cannot be released. An interrupted request can resume using the
same request ID and identical payload; completed items are not applied twice.
Changing that payload with the same ID is rejected. A fresh review of a terminal
failed item uses a new request ID. Per-item results describe the state at commit;
subsequent source updates may change the movement again. Both individual
decisions and batch outcomes are audited. Bulk review does not approve invoices
or send messages.
