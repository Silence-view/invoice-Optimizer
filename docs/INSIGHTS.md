# Insights

`invoice_optimizer.insights` pulls read-only statistical signals out of the operational data. Each insight is a **signal for a human reviewer**. None of them is evidence of error or fraud, and none of them changes a movement, case, invoice, assessment or counter.

## Contract

- Every public function is called as `fn(db, period, customer_id=None, limit=...)` and returns a JSON-serialisable dict containing `insight`, `method`, `scope`, `limitations`, `generated_at` and the results.
- Money is exact `Decimal`, written as a string with two decimal places. Statistics that need medians, logarithms or square roots are floating point, and their `method` field says so.
- Each call runs in one PostgreSQL `REPEATABLE READ READ ONLY` transaction with `statement_timeout = 120 s` (`STATEMENT_TIMEOUT_MS`). `generated_at` is the snapshot's `transaction_timestamp()`. `report()` evaluates every section inside **one** snapshot, so the sections reconcile with each other.
- **Population:** movements of the period at their current version (`movement_versions.version_no = movements.current_version`). Pending movements have no version, so they have no amount and are counted only in `overview`. Quarantined source rows have no canonical movement. They show up only in quarantine counts.
- **Bounds:** aggregation happens in SQL. Each list has a hard `LIMIT` (`1 <= limit <= 500`), and rows are never loaded into Python. Scans are scoped by `movements(period)` or `(customer_id, period)` and then follow the unique `(movement_id, version_no)` and `assessments(version_id)` indexes. `supplier_id` and `event_at` are read from the version's JSONB payload, but only for rows already in scope. No schema changes or migrations are needed.
- **Currency:** totals, HHI, outliers and duplicates are partitioned by currency. `overview.total_net` is `null` when the scope holds more than one currency.

## The insights

| Function | What it reports | Formula / threshold |
|---|---|---|
| `overview` | Movement count, versioned and pending counts, net by currency, distinct suppliers and customers, state mix, flagged counts and rate, model-score availability, quarantine top 10 reasons | flagged = current version has a flagged RULE **or** MODEL assessment. `flagged_rate = flagged / versioned movements`. `unavailable` = a MODEL row exists with a NULL score. `not_assessed` = no MODEL row |
| `supplier_concentration` | Top suppliers by amount with their share. HHI per customer | `share_i = amount_i / total`. `HHI = 10000 · Σ amount_i² / total²` (0–10000), computed in exact `numeric` and rounded to 0.01. `> 2500` highly concentrated (flagged), `1500–2500` moderately concentrated, `< 1500` unconcentrated (2010 US merger-guideline bands) |
| `duplicate_candidates` | Pairs of movements with the same customer, supplier, currency and exact amount, no more than `window_days` (default 7) apart, with movement, external and version refs and the gap in hours | `LAG()` over `PARTITION BY customer, supplier, currency, amount ORDER BY event_at`. The previous period is scanned as look-back context, and the later movement must be in the period. Zero amounts are ignored |
| `amount_outliers` | Movements far from their supplier's typical amount | Modified z-score (Iglewicz–Hoaglin): `z = 0.6745 · (x − median) / MAD`, where `MAD = median(|x − median|)`, per supplier and currency, using `percentile_cont(0.5)`. When MAD = 0 it falls back to `z = (x − median) / (1.253314 · MeanAD)`. Reported when `|z| > 3.5`. A group needs `n >= 10` (`min_sample`) |
| `benford` | First-digit distribution against Benford's law | Counts first digits of amounts `>= 10`. `E_d = n · log10(1 + 1/d)`. `χ² = Σ (O_d − E_d)² / E_d` with df = 8 (critical value 15.507 at α = 0.05; the p-value comes from the closed-form survival function for even df). `MAD = (1/9) Σ |O_d/n − log10(1 + 1/d)|`. Nigrini first-digit MAD bands: ≤ 0.006 close, ≤ 0.012 acceptable, ≤ 0.015 marginally acceptable, otherwise nonconformity. When `n < 300` it returns `status: "insufficient data"` with counts only |
| `trends` | Totals and counts for the last 12 periods, zero-filled, with % change. Top movers between the period and the previous one, plus each mover's 12-period series | `change_pct = (x_t − x_{t−1}) / x_{t−1} · 100`. It is `null` when `x_{t−1} = 0`. Movers are ranked by `|net change|` |
| `risk_distribution` | Model-score histogram (10 bins), mean, p50/p90/p99, flagged count, top RULE/MODEL reasons | Bins are `[0,0.1) … [0.9,1.0]` (`width_bucket`, with a score of 1.0 placed in bin 10). NULL scores are counted as `unavailable` or `not_assessed` and are **never** binned or treated as zero |
| `review_ageing` | Open cases by age bucket, by severity, and by reason with severity | `age = as_of − created_at`, buckets `[0,1d) [1d,3d) [3d,7d) [7d,∞)`. `as_of` defaults to the snapshot time |
| `data_quality` | Missing optional fields and their rate, source A/B mismatch reasons, rule failures, source records per side, quarantine by source and reason | A field is missing when its key is absent or empty. The fields checked are `expected_amount`, `customer_age_days`, `supplier_age_days` and `cross_border`. Mismatches are the RULE reasons (`MISSING_SOURCE_A/B`, `AMOUNT_MISMATCH`, `SUPPLIER_ID_MISMATCH`, …) |
| `report` | All of the above, in a single snapshot | — |

## CLI

```
invoice-optimizer insights --period 2026-09 [--customer C0001] [--limit 20] [--json PATH]
```

Without `--json`, the CLI prints the full report. With `--json`, it writes the report atomically (a temporary file, then rename) and prints a short receipt with the path, byte count and section list.

## HTTP API and Insights tab

All endpoints are `GET`, need a signed-in session (shared read-only access may read) and never write.

- `/api/insights/periods?customer=` lists up to 120 periods that have movements, newest first (`latest` is the default period).
- `/api/insights/report?period=YYYY-MM&customer=&limit=` returns `report()`: every section from one snapshot. `period` defaults to the latest period with movements (404 when there are none).
- `/api/insights/{section}` with `section` in `overview`, `suppliers`, `duplicates`, `outliers`, `benford`, `trends`, `risk`, `ageing`, `quality`. `duplicates` also takes `window_days` (0–92); `outliers` takes `threshold` (0–100) and `min_sample` (3–100000). Options for another section are rejected with 422.
- `limit` is 1–100 and `customer` 1–80 characters. Each computation uses the read engine (`DATABASE_READ_URL` when set) in a `REPEATABLE READ READ ONLY` snapshot with a statement timeout of at most 30 s, which maps to 503 + `Retry-After` like other database timeouts. Results are cached per process for 30 s, keyed by every argument (`X-Insights-Cache: hit|miss`); at most two computations run at once per process (429 when busy). Failures are never cached.

The console's **Insights** tab (`g n`) calls `periods` and then `report` only when it is opened, and refreshes on live updates for cases, batches, model or customers, never while a dialog is open.

## Offline history with DuckDB

`insights_from_parquet(export_dir, customer_id=None, limit=50, *, period=None, window_days=7, memory_limit="1GB", threads=4, verify_checksums=True)` computes supplier concentration, duplicate candidates, Benford and trends over the Parquet exports written by `analytics.export_analytics`. It does this without touching PostgreSQL, so history far larger than the database's working set can be analysed offline.

- `export_dir` can be an export root holding `period=YYYY-MM/` partitions, a single partition, or one export directory.
- For a root or a partition, only pointer-published exports are read. A single export directory passed explicitly is read as given, even if a newer export has since been published. Each `latest.json` pointer is checked against its manifest's SHA-256, each manifest must be `COMPLETE` with schema `current-movement-analytics-v1`, file names must match `part-NNNNN.parquet`, and each file's Parquet row count must match the manifest.
- With `verify_checksums=True` (the default), the function also re-hashes every file and reconciles DuckDB's per-period, per-currency row counts and Decimal sums with the manifest before computing anything.
- DuckDB runs in memory with `memory_limit` and `threads` set, and spills to a private temporary directory that is removed afterwards. Queries stream through `read_parquet`.
- The formulas are the same as in PostgreSQL. Two differences: HHI and shares are double precision here (amounts and totals stay exact Decimal), and duplicate look-back spans every supplied period.
- Each period is its own export snapshot, so periods exported at different times are not mutually consistent.

## Limits and interpretation

- Every result is a statistical signal. A duplicate pair can be a legitimate recurring charge. An outlier can be a genuine one-off purchase. HHI measures how much a customer depends on a few suppliers, not wrongdoing. Priced services and fixed tariffs do not follow Benford's law. At large `n`, the χ² test rejects almost any real dataset, which is why MAD is the conformity criterion.
- Model scores come from the demonstration model. They are not calibrated fraud probabilities.
- `trends` reports current versions as they are now, not the values reported at each month end.
- Customer-scoped quarantine counts include only rows whose `customer_id` was validated during ingestion.
- No insight can approve, release or block anything. Review outcomes still go through the existing case workflow, with a human decision.
