# Isolated streaming verification

`scripts/verify_streaming_workload.py` exercises actual PostgreSQL ingestion,
assessment and scoring while two authenticated HTTP readers and an SSE client
read the same application. It is a bounded synthetic integration measurement,
not a production capacity claim.

```sh
.venv/bin/python scripts/verify_streaming_workload.py \
  --movements 100000 --customers 200 --largest-customer-movements 50000 \
  --hold-seconds 120
```

The harness requires the existing local PostgreSQL server and a verified active
Logistic Regression artifact whose information cutoff precedes the serving
month. It copies that exact version into a new private benchmark directory;
it does not train or activate a model. Port 8766 must be free. The main console
on 8765 and its data are not changed.

The generated input contains 100,000 logical movements, with 50,000 assigned to
the largest of 200 synthetic customers. Actual source row counts differ because
the generator deliberately includes missing counterparts, retries, revisions,
quarantine scenarios and ambiguous records. Each retained delivery contains at
most 1,000 rows and enters `ingest_file(..., complete=False)`. No delivery is
declared to establish monthly completeness. The generator's complete fixture
manifest describes the generated files only and is not used as an ingestion
completeness assertion.

Two worker threads call only `run_job(..., kinds=('ASSESS', 'SCORE'))`. Monthly
preparation, dispatch and email delivery are disabled in the isolated runtime.
No invoice, renderer or email worker is invoked. Alert intents can accumulate
in the outbox, and the final SQL checks require zero invoices and zero delivery
attempts.

The harness sends two newer revisions of an existing record through both
source inputs. It waits for the actual persisted rules, model assessments and
MODEL_RISK cases. A clearly named synthetic operator then submits the earlier
case version to the normal authenticated review endpoint. A passing run
requires HTTP 409, an attributed rejection audit, zero review events for that
attempt, and no reviewer attribution on the superseded case. No direct SQL
mutation creates these source versions or performs the review probe.

The measurement interval starts immediately before the worker and reader
threads start, and ends after ingestion, job draining and the final SSE count
are observed. It excludes input generation, the final browser inspection hold
and cleanup. Endpoint latencies report successful HTTP request p50/p95/p99/max
using linear interpolation; errors and denominators are reported separately.
Queue peaks are sampled dashboard observations and therefore lower bounds on
the instantaneous peaks. RSS is the Python process lifetime high-water mark,
including the API/workers/generator, and excludes PostgreSQL and other processes.

Evidence is written to:

- `output/verification/streaming-workload.json`: latest complete or failed receipt.
- `var/benchmarks/stream-<run-id>/streaming-workload.json`: retained per-run receipt,
  inputs, chunk files and exact model copy.
- `output/verification/streaming-ready.json`: current observer status, origin,
  schema and actual multi-version case IDs. It contains no credentials.

The readiness document references an owner-only access file containing a
temporary shared **read-only** credential. The observer can log in once and
then browse; this credential cannot approve, correct or change configuration.
The file is deleted during cleanup. The harness retains no personal operator
password in its receipt or access file. Its synthetic operator exists only in
the disposable schema.

The receipt binds input and source-code hashes, the frozen model artifact hash,
all delivery outcomes and row denominators, final SQL counts and operational
counters, current-version assessment coverage, SSE revisions/counts, rejection
audit evidence, errors, timestamps and cleanup status. Each run creates only
`invoice_stream_<uuid>` and drops only that generated schema after its threads
have stopped. A failed run retains its receipt and generated files. If threads
fail to stop within their deadlines, the schema is deliberately not dropped
under running transactions and the receipt reports cleanup failure.

Run results must be read from the receipt. The source alone establishes neither
a passing integration test nor a latency result. A 100,000-movement run does not
establish performance at two million movements, production peak arrival rates,
real source quality, real model detection quality or multi-host availability.

## Verified run on 27 October 2026

Run `f39a9cb06ac24939b40076aa5777182d` passed all receipt checks. The measured
interval was **141.025638 seconds**, from 23:15:34.238145 to 23:17:55.305300 UTC.
Incremental file delivery and the source-revision probe took 114.381519 seconds;
the remaining measured interval drained processing jobs and observed the final
SSE state. Input generation/customer loading took 2.114262 seconds separately.
The configured 120-second UI hold and 14.379064-second cleanup are excluded.

The final data comprised 100,000 logical movements across 200 customers, with
50,000 for the largest customer. The harness delivered 202,504 source rows in
207 bounded files: 200,004 accepted, 1,500 quarantined and 1,000 duplicate retries.
These rows include four explicit revision-update rows beyond the generated
202,500 source rows. The delivered files totalled 33,909,930 bytes, including
their repeated CSV headers and the revision files.

Both workers processed jobs, completing 357 and 360 respectively. All 717 jobs
finished with status DONE. The database retained 100,003 movement versions and
200,006 assessments. Every one of the 100,000 **current** versions had both
assessment kinds: 99,500 model results were SCORED and 500 were UNAVAILABLE;
97,500 rule assessments passed and 2,500 failed. Missing model evidence was
retained as unavailable. SQL counts matched every operational state counter.

The two HTTP readers completed 2,035 measured requests without errors:

| Resource | Requests | p50 ms | p95 ms | p99 ms | Maximum ms |
|---|---:|---:|---:|---:|---:|
| Dashboard |407|4.484|74.664|119.373|170.286|
| Review cases |408|27.081|146.423|207.248|1,129.143|
| Invoices (empty, by design) |408|6.658|51.946|103.282|127.871|
| Automation settings |408|5.584|79.637|111.953|140.358|
| Movement version history |404|25.980|120.804|174.442|188.359|

The measured SSE reader received 69 distinct, monotonically increasing audit
revisions, from zero movements to 100,000 with no pending jobs. The sampled queue
peaked at 111 ready and two running jobs. Python process peak RSS was
260,210,688 bytes; this excludes PostgreSQL and is a process-lifetime high-water
mark, not memory measured solely during the interval above.

The source revision probe retained actual model-risk versions 3 and 4. Submitting
version 3 after version 4 became current returned HTTP 409 and wrote an attributed
rejection audit for the isolated synthetic reviewer. It created no review event
and did not approve the case. An independent browser observed eight actual SSE
states over 15.081 seconds, saw the movement count rise from zero to 17,000 during
ingestion, and navigated historical version 3 while the review target remained 4.
Its only POST was shared read-only login. See
`output/verification/live-ui-streaming-smoke.json` for the separate browser proof.

No invoices or delivery attempts were created; 5,504 alert intents remained
unsent. Source hashes were unchanged across the run, the temporary access file
was removed, and only the generated schema was dropped. The exact receipt is
`output/verification/streaming-workload.json`; an identical per-run copy remains
under the run directory. The earlier 2,000-movement qualification is preserved
as `output/verification/streaming-pre-final-dry-run.json` and is not substituted
for this 100,000-movement result.
