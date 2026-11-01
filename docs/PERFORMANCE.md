# Measured performance and scaling limits

The benchmark measures the implemented pipeline against local PostgreSQL using
synthetic source files. Results are evidence for the exact recorded run. They
are not throughput guarantees for 200 live clients, two million movements from
the largest client, concurrent operators or peak arrival bursts.

> **Evidence receipts are not published.** Files referenced below under
> `output/verification/` and `output/pdf/*/receipt.json` were written by local
> verification runs and are excluded from this repository because they record
> machine-specific paths; some historical receipts are no longer retained.
> They are cited by path only.

## Current before/after measurement

The console/worker update was compared with the immediately preceding source
snapshot using identical source bytes, 20,000 logical movements, 200 customers,
10,000 movements at the largest customer, seed 42 and the same frozen LR model.
All business counts, state totals, source and model hashes match exactly.

| Measured stage | Before | After |
|---|---:|---:|
| Source A ingestion | 5.120 s | 4.344 s |
| Source B ingestion | 4.039 s | 4.116 s |
| Reconciliation, scoring and evidence persistence | 40.972 s | 12.998 s |
| Verified analytical Parquet export | 1.373 s | 1.218 s |
| Entire run including setup and cleanup | 52.563 s | 23.694 s |
| Python lifetime peak RSS, excluding PostgreSQL | 265,027,584 bytes | 272,695,296 bytes |

Assessment was 3.15 times faster in this pair of runs. This is one sequential run
per candidate on one host, not a statistically controlled latency guarantee.
The candidate creates 144 smaller jobs instead of 74; all completed. Both runs
retain 40,000 source records, 40,000 assessments, 300 quarantine rows and 1,099
alert intents, with no email attempts or invoices. Final states agree exactly:
19,301 eligible, 199 review and 500 blocked movements. Both models report 100
explicitly unavailable scores.

Receipts: baseline (`output/verification/backend-baseline-20k.json`),
candidate (`output/verification/backend-optimized-final-20k.json`),
comparison (`output/verification/backend-comparison-20k.json`).
The preceding 80.987-second historical measurement remains in
performance.json (`output/verification/performance.json`); its different export
time is not used as the comparison baseline.

## Concurrent ingestion and live reads

The final isolated streaming run processed 100,000 logical movements for 200
customers, including 50,000 for the largest customer. Two worker threads ran
ASSESS/SCORE while two authenticated HTTP readers and one SSE reader observed
the actual API. The measured ingest/drain/final-SSE interval was 141.026 s;
input generation, the final UI hold and cleanup are excluded.

| Endpoint | Requests | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|
| Dashboard |407|74.664 ms|119.373 ms|170.286 ms|
| Review cases |408|146.423 ms|207.248 ms|1129.143 ms|
| Invoice list |408|51.946 ms|103.282 ms|127.871 ms|
| Automation |408|79.637 ms|111.953 ms|140.358 ms|
| Version history |404|120.804 ms|174.442 ms|188.359 ms|

All 2,035 measured HTTP requests succeeded. SSE observed 69 updates from 0 to
100,000 movements and the final empty processing queue. Both workers completed
work: 357 and 360 jobs. All 100,000 current versions received both assessment
kinds, including 500 explicitly unavailable model scores. Maintained counters
equal SQL. A stale review against an actual earlier source version returned 409
and retained the synthetic reviewer's rejection audit without an approval.

Python lifetime peak RSS was 260,210,688 bytes (248.16 MiB), excluding PostgreSQL.
The sampled queue peak was 111 READY/2 RUNNING jobs, a lower bound on the true
instantaneous peak. The run retained 5,504 unsent alert intents and created zero
invoices or delivery attempts. Its invoice-list latency therefore covers an
empty invoice list; the main console's 12 retained packages and PDF previews
were verified separately. No PDF or email throughput is inferred.

This is one local run, not a guarantee at two million movements or production
arrival rates. The 1.129-second maximum case-list response is retained above;
percentiles do not eliminate outliers. Source hashes remained unchanged and
the disposable schema and temporary read-only credential were removed.

See the complete receipt (`output/verification/streaming-workload.json`),
[harness methodology](STREAMING_VERIFICATION.md) and
browser observation during ingestion (`output/verification/live-ui-streaming-smoke.json`).

## Implemented work bounds

Ingestion keeps customer lookups local to the current chunk and fetches the latest
source revision. ASSESS jobs contain at most `worker_batch_size` movements.
Reconciliation and scoring reload a batch after customer-period locking, persist
versions/assessments/cases in batches and keep cases with alert intents in the
same transaction. Invoice invalidation remains within the affected scopes.

Job claims rotate ASSESS/SCORE/RENDER probes per worker process and use PostgreSQL
`SKIP LOCKED`. Lease recovery handles at most 32 expired jobs per call; migration 7
adds a partial index for running leases. The scheduler scans actual movement
customer-period scopes through the existing index. Fair probing is not a global
priority or time guarantee when multiple processes compete.

The queue admission watermark rejects new files before creating a batch and
returns retryable HTTP 429 with `Retry-After: 5`. Declared input reserves its
remaining chunks. For unknown-size CLI input, only the first chunk is reserved;
already admitted files finish. This is a soft admission watermark, not a strict
maximum queue size, byte quota or memory cap.

Dashboard summaries are shared for two seconds per API process. Each refresh
uses a coherent read-only database snapshot; a stream generation also changes
when counts or worker status change without a larger audit ID. This handles
out-of-order PostgreSQL transaction commits. Resource hints avoid reloading
unrelated pages; reconnects and skipped generations invalidate conservatively.
A conservative full invalidation every 15 seconds also covers late lower-ID
commits that change detail fields without changing aggregate counts. Exact
operational counts still use SQL, and are not a precomputed warehouse.

## Reproduce a bounded run

Start the local PostgreSQL service, prepare the environment and generate/train the
normal demonstration once. Then run:

```bash
.venv/bin/python -m invoice_optimizer benchmark \
  --customers 200 --movements 20000 \
  --largest-customer-movements 10000 \
  --period 2026-09 --seed 42
```

This creates a uniquely named PostgreSQL schema and a retained directory under
`var/benchmarks/run-<id>/`. The database schema is dropped in a `finally` cleanup
step. The live application schema is not reset or used for this workload. The
benchmark receipt explicitly records whether schema creation and removal
succeeded. Generated files, copied trusted model artifacts, verified Parquet
exports and the receipt remain available for inspection.

The active local model is copied to the isolated data directory. Its information
cutoff must precede the benchmark month. If no local model exists, the benchmark
trains on its bounded historical synthetic dataset and records that separately.
The model preparation stage therefore states whether training was reused or
performed within the measured run.

## What is measured

The receipt separates schema setup, synthetic generation, model preparation,
customer loading, source A and B ingestion, reconciliation/scoring, exact SQL and
operational-counter verification, verified Parquet export and schema cleanup.
The assessment stage includes database-backed job handling, deterministic rules,
vectorised model inference, persisted assessments, cases and alert outbox rows.

Workers drain internal jobs with `max_emails=0`. The benchmark never closes an
invoice, renders a PDF, approves a package or submits an email. It verifies zero
invoice and delivery-attempt rows, and reports queued alert rows separately.

Before success, SQL movement totals and each movement-state total must match the
maintained operational counters. Every generated movement must have a rule and
model assessment; model-unavailable results are counted explicitly. Failed jobs
fail the run. The Parquet export independently reconciles its exact Decimal sums,
counts and null-score counts with its PostgreSQL snapshot.

Input bytes, physical source row counts and logical movement counts are all
reported. Duplicate/revision fixtures mean physical rows differ from logical
movements. Observed rates use the appropriate denominator: physical rows for
ingestion, logical movements for assessment and export. Wall-clock rates include
the work within that stage; they are neither worst-case latency nor sustained
concurrent service capacity.

Peak RSS uses `resource.getrusage(RUSAGE_SELF).ru_maxrss`. macOS reports bytes;
Linux reports KiB, converted to bytes in the receipt. The raw value and original
units are retained. This is the current Python process's lifetime memory
high-water mark and includes imports, model handling, Arrow buffers and prior
work in that process. It excludes PostgreSQL server memory and unrelated
processes. It is not a measurement of the entire infrastructure's RAM needs.

## Larger explicit stress fixtures

To generate a two-million-movement largest customer without assuming the same
volume for all clients:

```bash
.venv/bin/python -m invoice_optimizer generate \
  --customers 200 --movements 2100000 \
  --largest-customer-movements 2000000 \
  --period 2026-09 --seed 42 \
  --output var/synthetic/stress-largest-2m
```

The total of 2.1 million is an explicit test allocation: two million for C0001
and 100,000 shared among the other 199 synthetic customers. It is not an estimate
of the company's total volume. Generation streams CSV and Parquet; adequate disk
space is still required. This command generates files only and has not been
executed as part of the bounded benchmark unless a separate receipt says so.

To measure ingestion, assessment and export at that size, use the same arguments
with `benchmark` instead of `generate` and omit `--output`. That much larger run
must be treated as a separate measurement. Do not multiply the small-run rate to
claim its duration or memory requirements in advance.

## Remaining scale evidence

A deployment decision still needs measured realistic schemas and payload sizes,
arrival bursts, database/index growth, concurrent workers, correction rates,
alert volume, source outage/replay recovery, storage durability, monthly-close
work, backup/restore and delivery-provider limits. The largest-customer skew and
cross-customer workload must both be represented. PostgreSQL snapshot exports
also need monitoring for long transactions and vacuum retention.

The default local launcher runs one worker process. Multiple worker claims and
batched persistence preserve audit and retry semantics. Further optimisation
should follow measured stage profiles and
preserve exact monetary reconciliation, idempotent source identity, independent
rule failures, immutable versions and approval-bound dispatch.

## Database scale changes (migration 8)

Migration 8 runs inside the advisory-locked `init`. It is idempotent.
- **Dropped indexes:** duplicate or prefix indexes, and indexes no query uses. That covers `movements.state`, `customer_id`, versions/assessments/invoices prefixes, `ix_source_latest`, the `review_events` case/period/`occurred_at` indexes, and `jobs.status` and `review_cases.status`.
- **Added indexes:**
  - partial indexes for unfinished jobs, unresolved quarantine and open cases;
  - `(status, created_at DESC, id)` for the case list;
  - `(created_at DESC, id)` for the invoice and outbox lists;
  - `(source, period, record_id, revision)` for close coverage;
  - `(movement_id, source, record_id, revision DESC)` for latest-revision lookups;
  - BRIN indexes on append-mostly time columns.
- **Invoice list columns:** `invoices.line_format`, `movement_count` and `movement_vat_policy` are set at freeze and backfilled from the frozen document. The invoice list reads these columns. When `line_format` is NULL, it reads the document instead.

Indexes are built without `CONCURRENTLY` because `init` is one transaction. On a large live database, create them `CONCURRENTLY` with the same names before upgrading. `init` then skips them.

Close checks cache a positive source-coverage certificate per database engine. The key is the source, the period, the latest batch and every batch ID of that source-period, so any new delivery invalidates it. The quarantine, state and open-case checks were already scoped to the customer and are now index-backed. The unfinished-job check stays period-wide, because job payloads do not record a customer.

Connection settings are per process. Defaults keep the previous behaviour.

| Variable | Default |
|---|---|
| `INVOICE_DB_POOL_SIZE` | 4 |
| `INVOICE_DB_MAX_OVERFLOW` | 2 |
| `INVOICE_DB_POOL_RECYCLE_SECONDS` | 1800 |
| `INVOICE_DB_STATEMENT_TIMEOUT_MS` | 0 (off) |
| `INVOICE_DB_LOCK_TIMEOUT_MS` | 0 (off; also bounds advisory-lock waits) |
| `INVOICE_DB_IDLE_TX_TIMEOUT_MS` | 0 (off) |
| `DATABASE_READ_URL` | unset (same engine) |

`application_name` is `invoice-optimizer-api`, `-worker` or `-cli`, with a `-read` suffix on the read engine. The three timeout variables apply only to the API's sessions. All processes read the same `.env`, and on the worker a timed-out lock wait would mark a job (and a rendering invoice) FAILED. `init-db` and a large freeze are single long transactions. The CSV exports stream from one open transaction, so the API's idle-in-transaction timeout must exceed the longest pause of a slow download client; otherwise PostgreSQL ends the session and the file is cut short after a 200 response.

These endpoints use a `READ ONLY` transaction on the read engine: the case, invoice, outbox, batch, customer and audit lists, and the CSV exports. Close, approve and dispatch validation always use the primary. If a replica lags, a list can briefly trail a write that just happened.

On the API, a PostgreSQL `lock_timeout` (55P03) or `statement_timeout` (57014) error returns HTTP 503 with `Retry-After: 2`.

Keyset pagination is available on `/api/cases`, `/api/invoices`, `/api/outbox` and `/api/audit`:
- Pass `cursor=` (empty) to get `{items, next_cursor, limit}`. Pass the returned `next_cursor` to get the next page.
- Offset requests still return the bare list. They also send the next cursor in an `X-Next-Cursor` header.
- Mixing `cursor` and `offset` is rejected.
- These endpoints add no `total`, because no counter backs these filtered lists and a `COUNT(*)` would scan large tables.

Retention: `invoice-optimizer maintenance --prune-done-jobs-older-than DAYS --prune-commands-older-than DAYS [--apply]`.
- Without `--apply` it is a dry run.
- It deletes in bounded batches and records an audit event for each batch.
- It keeps jobs that a training run references, and RUNNING bulk commands.
- It prints VACUUM/ANALYZE hints and indexes that have never been scanned.
- A pruned request ID no longer replays: a retry after the retention age runs as a new command.

Bounded run on the same host and inputs as above (200 customers, 20,000 movements, 10,000 for the largest customer, seed 42):

| Stage | Documented run (pre-change) | Pre-change code, rerun on this host | Migration 8 |
|---|---:|---:|---:|
| Source A / B ingestion | 4.344 / 4.116 s | 4.481 / 4.114 s | 4.229 / 4.059 s |
| Reconciliation, scoring and evidence | 12.998 s | 13.119 s | 12.866 s |
| Verified Parquet export | 1.218 s | 1.511 s | 1.199 s |
| Entire run | 23.694 s | 23.958 s | 23.272 s |

The business fingerprint is identical across code versions (`82a6bd66…`): counts, states, statuses and counters. At this size, the timing differences are within run-to-run noise. A first pre-change rerun measured 17.0 s and 30.3 s on the same host. The changes target index growth, HOT updates, close-check rescans and deep list pages, and those costs only dominate at much larger tables.

### Next steps (deliberately not done)

- **Locks.**
  - Freeze holds the `period:` lock across the whole snapshot. A shared `pg_advisory_xact_lock_shared` for scope readers, with an exclusive lock only at ingest batch start and completion, would let different customers close in parallel.
  - ASSESS/SCORE jobs take the customer-period lock exclusively. A shared lock plus `FOR UPDATE ... ORDER BY id` on movements would let workers scale for the largest customer.
  - `source:` serialises ingestion. Lifting it depends on unique constraints and `ON CONFLICT`.
  - Each of these needs concurrency tests before it changes.
- **Partitioning.**
  - `source_records` and `batch_records` by period, with a key table to keep source-identity uniqueness.
  - `audit_events` by id range, with triggers on each partition, including TRUNCATE.
  - Both need a real migration tool and an offline copy.
- **Other.**
  - Dashboard counters for open cases, quarantine and outbox status.
  - A separate small pool for CSV exports.
  - COPY-based ingestion staging.
  - `(customer_id, period) INCLUDE (state)` was not added, because an index on `state` would block HOT state updates again.
