# Analytical exports

`export_analytics(db, period, output_dir)` publishes a read-only analytical
snapshot of the operational PostgreSQL database. PostgreSQL remains authoritative
for corrections, review, invoice membership, approval and dispatch. Editing an
export never changes a movement or authorises an email.

## Dataset and evidence

There is one row per movement in the requested `YYYY-MM` period, joined to the
version identified by `current_version` and to that version's `RULE` and `MODEL`
assessments. Older versions are excluded. A pending movement without a canonical
version remains in the dataset with null version, amount and assessment fields.
Quarantined source rows without a movement are outside this dataset; a complete
quarantine audit therefore requires the operational case/source records too.

The export includes exact `DECIMAL(20,2)` amounts, currency, movement state,
version/content hash, source references, payload JSON, deterministic status and
rule version, nullable model score/status, model/policy/feature schema versions,
feature values, feature cutoff, evaluation timestamps and reasons. A missing
assessment is distinct from `UNAVAILABLE`, and both are distinct from a scored
zero. Original timestamps retain UTC-aware Arrow timestamp types.

The feature cutoff is the value stored with the assessment. It records the
application's assessment provenance; it does not independently prove that every
upstream feature was available at that time. Real-source feature provenance must
still be contracted and validated.

## Consistent snapshot and verification

The exporter opens a PostgreSQL `REPEATABLE READ` transaction and explicitly sets
it to `READ ONLY`. It records the transaction timestamp and PostgreSQL MVCC
snapshot identifier. Both the database control totals and the streamed rows are
read from that same snapshot, so a concurrent correction cannot mix old and new
versions within one export.

Three views must reconcile before publication:

1. PostgreSQL counts, exact net sums and null-model-score counts by currency.
2. Counters accumulated while streaming, using Decimal arithmetic.
3. DuckDB `SELECT` aggregates over the completed Parquet files, through a read-only
   temporary DuckDB catalogue.

The manifest records these aggregates, each file's row count, byte length and
SHA256, the snapshot cutoff and export configuration. These controls verify the
export's consistency. They do not prove source-delivery completeness, correct
tax treatment or monthly billing eligibility.

## Storage and publication

Files are written beneath a temporary directory in the target partition. Each
Parquet shard holds at most 100,000 rows and uses ZSTD compression, statistics
and dictionary encoding. Rows arrive through a server-side SQLAlchemy cursor
with fetch batches of at most 1,000; the writer never loads the complete period
into memory and uses no pandas dataframe.

After verification, the manifest is written and the directory is renamed
atomically on the same filesystem:

```text
output_dir/
  period=2026-09/
    export-<unique-id>/
      part-00000.parquet
      part-00001.parquet
      manifest.json
    latest.json
```

`latest.json` is replaced atomically and references the last successfully
published export and the SHA256 of its manifest. Existing exports are retained.
Failed verification removes the staging directory and leaves the previous
pointer unchanged. Consumers should follow the pointer, verify the manifest and
file hashes, and use the recorded snapshot cutoff. With simultaneous export
processes, “latest” means last published, not necessarily the greatest snapshot
timestamp. Export IDs remain independently addressable.

## Scale and operating limits

Memory used for movement rows is bounded by the fetch/write batch, while file
manifest metadata grows with the number of shards. Currency control totals remain
small under the current GBP-only source contract. The DuckDB verification
connection is limited to two threads and 256 MB; financial sums are decimal.

The operation still scans the requested period twice in PostgreSQL: once for
server-side control totals and once for the streamed evidence, followed by a
Parquet verification scan. Long repeatable-read snapshots retain old row versions
and can delay PostgreSQL vacuum reclamation. Export duration, database load and
disk capacity must therefore be measured on representative volumes. Schedule
large exports appropriately or use an approved consistent read replica.

The dataset contains financial payloads and identifiers. Use an access-controlled
output volume and a retention policy suitable for the deployment. Local atomic
rename semantics are assumed; object-store publication requires a different
manifest/pointer protocol and is not implemented here. The export is complete
only for the database snapshot it records, and it is never a claim that a
two-million-row or full-customer workload has already been benchmarked.
