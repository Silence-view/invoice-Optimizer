"""Reproducible, bounded-memory synthetic source and training files.

The two-source one-to-one matching rule is a simulation contract, not an
inference about the customer's real spreadsheets. No real personal data is used.
"""

from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

SOURCE_FIELDS = (
    "record_id", "revision", "movement_id", "customer_id", "event_at", "period",
    "currency", "amount", "description", "supplier_id", "expected_amount",
    "customer_age_days", "supplier_age_days", "hour_utc", "cross_border",
)
SOURCE_SCHEMA = pa.schema([(name, pa.string()) for name in SOURCE_FIELDS])
TRAINING_SCHEMA = pa.schema(
    [(name, pa.string()) for name in SOURCE_FIELDS]
    + [("label", pa.int8()), ("label_available_at", pa.string()), ("synthetic", pa.bool_())]
)
PARQUET_BATCH_SIZE = 8192
TRAINING_GENERATOR_VERSION = "separable-historical-risk-v1"

# Deliberate exceptions injected into the serving sources, one slot each per 100
# non-C0001 movements. A scenario profile only removes slots (they become NORMAL);
# it never changes how production code treats what remains.
_SCENARIO_SLOTS = {
    0: "HIGH_RISK", 1: "AMOUNT_MISMATCH", 2: "MISSING_COUNTERPART",
    3: "INVALID_AMOUNT", 4: "MISSING_FEATURE", 5: "DUPLICATE_RETRY",
    6: "CONFLICTING_DUPLICATE", 7: "AMBIGUOUS_IDENTITY",
    8: "INVALID_TIMESTAMP", 9: "SOURCE_REVISION",
}
# Scenarios whose review case can never carry a logistic-regression score: the
# model is UNAVAILABLE (missing feature) or the row is quarantined before it
# becomes a movement, which also opens a knock-on MISSING_SOURCE_A case.
_UNSCORED_SCENARIOS = frozenset({"MISSING_FEATURE", "INVALID_AMOUNT", "INVALID_TIMESTAMP", "CONFLICTING_DUPLICATE"})
# Rule-only exceptions: scored by the model, but flagged by a reconciliation rule.
_RULE_ONLY_SCENARIOS = frozenset({"AMOUNT_MISMATCH", "MISSING_COUNTERPART", "AMBIGUOUS_IDENTITY"})
SCENARIO_PROFILES = {
    # Every exception kind, including unscored ones (tests and stress fixtures).
    "full": frozenset(),
    # Demo default: every flagged case has a recorded regression score.
    "model-evidence": _UNSCORED_SCENARIOS,
    # Only cases the regression itself flags (plus harmless replays/revisions).
    "model-only": _UNSCORED_SCENARIOS | _RULE_ONLY_SCENARIOS,
}


def generate_training_data(output_path: Path, rows: int = 30000, seed: int = 42,
                           serving_period: str = "2026-09") -> dict:
    """Generate an independent bounded historical fixture, without source edits.

    The supplied path must be new. Publication uses an exclusive hard link, so
    concurrent generators cannot overwrite an existing training data artifact.
    A manifest is published only after its complete Parquet file is available.
    """
    if type(rows) is not int or not 100 <= rows <= 100_000:
        raise ValueError("training rows must be between 100 and 100000")
    if type(seed) is not int or not 0 <= seed <= 2**63 - 1:
        raise ValueError("seed must be a nonnegative 63-bit integer")
    start = _month_start(serving_period)
    target = Path(output_path)
    if target.suffix.lower() != ".parquet":
        raise ValueError("training output must be a .parquet file")
    manifest_path = target.with_suffix(".manifest.json")
    if target.exists() or manifest_path.exists():
        raise ValueError("training output already exists; use a new run path")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".training-", suffix=".parquet", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    rng = random.Random(seed)
    first, finish = start - timedelta(days=120), start - timedelta(days=2)
    pending = []
    positives = 0
    try:
        with pq.ParquetWriter(temporary, TRAINING_SCHEMA, compression="zstd") as writer:
            for index in range(rows):
                event = first + (finish-first) * (index/rows)
                risk = index % 20 == 0
                positives += int(risk)
                row = _row(rng,index+1,f"HIST-{index % 200 + 1:04d}",event,risk)
                row.update(label=int(risk),label_available_at=(event+timedelta(hours=24)).isoformat(),synthetic=True)
                pending.append(row)
                if len(pending) >= PARQUET_BATCH_SIZE:
                    writer.write_table(pa.Table.from_pylist(pending,schema=TRAINING_SCHEMA))
                    pending.clear()
            if pending:
                writer.write_table(pa.Table.from_pylist(pending,schema=TRAINING_SCHEMA))
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        manifest = {
            "schema_version": "synthetic-training-manifest-v1", "synthetic": True,
            "generator_version": TRAINING_GENERATOR_VERSION,"seed": seed,"rows": rows,
            "positive_rows": positives,"serving_period": serving_period,
            "event_start": first.isoformat(),"event_end": event.isoformat(),
            "last_label_available_at": (event+timedelta(hours=24)).isoformat(),
            "label_generation_method": "Every twentieth row is a synthetic high-risk scenario: amount 4–11 times expected, customer age 1–45 days, supplier age 1–14 days, cross_border=1. Other rows are deliberately separable normal scenarios. Labels become available 24 hours after the event.",
            "evaluation_limitations": "Synthetic labels and deliberately separable patterns; no claim about real fraud, calibration, compliance or production accuracy.",
            "format": "Parquet", "compression": "ZSTD", "row_group_limit": PARQUET_BATCH_SIZE,
            "file": target.name,"sha256": _sha256(temporary),"bytes": temporary.stat().st_size,
            "manifest_file": manifest_path.name,
        }
        os.link(temporary,target)
        descriptor, name = tempfile.mkstemp(prefix=".training-manifest-",dir=target.parent)
        manifest_temporary = Path(name)
        try:
            with os.fdopen(descriptor,"w") as stream:
                json.dump(manifest,stream,indent=2,sort_keys=True)
                stream.write("\n");stream.flush();os.fsync(stream.fileno())
            os.link(manifest_temporary,manifest_path)
        finally:
            manifest_temporary.unlink(missing_ok=True)
        return manifest
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _month_start(period: str) -> datetime:
    try:
        result = datetime.strptime(period, "%Y-%m").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ValueError("period must be YYYY-MM") from exc
    if result.strftime("%Y-%m") != period:
        raise ValueError("period must be YYYY-MM")
    return result


def _next_month(value: datetime) -> datetime:
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


def _money(cents: int) -> str:
    return format(Decimal(cents).scaleb(-2), ".2f")


def _row(rng: random.Random, index: int, customer: str, when: datetime, high_risk: bool) -> dict:
    expected_cents = rng.randint(1000, 250_000)
    if high_risk:
        amount_cents = expected_cents * rng.randint(4, 11)
        customer_age = rng.randint(1, 45)
        supplier_age = rng.randint(1, 14)
        cross_border = 1
    else:
        amount_cents = expected_cents * rng.randint(90, 110) // 100
        customer_age = rng.randint(180, 5000)
        supplier_age = rng.randint(90, 3000)
        cross_border = int(rng.random() < 0.12)
    return {
        "record_id": f"REC-{when:%Y%m}-{index:010d}", "revision": "1",
        "movement_id": f"MOV-{when:%Y%m}-{index:010d}", "customer_id": customer,
        "event_at": when.isoformat(), "period": when.strftime("%Y-%m"),
        "currency": "GBP", "amount": _money(amount_cents),
        "description": "Synthetic purchased business service",
        "supplier_id": f"SUP-{rng.randint(1, 50):04d}",
        "expected_amount": _money(expected_cents),
        "customer_age_days": str(customer_age), "supplier_age_days": str(supplier_age),
        "hour_utc": str(when.hour), "cross_border": str(cross_border),
    }


class _SourceWriter:
    def __init__(self, directory: Path, stem: str):
        self.csv_path = directory / f"{stem}.csv"
        self.parquet_path = directory / f"{stem}.parquet"
        self.handle = self.csv_path.open("w", encoding="utf-8", newline="")
        self.csv = csv.DictWriter(self.handle, fieldnames=SOURCE_FIELDS, lineterminator="\n")
        self.csv.writeheader()
        self.parquet = pq.ParquetWriter(self.parquet_path, SOURCE_SCHEMA, compression="zstd")
        self.pending: list[dict] = []
        self.count = 0

    def write(self, row: dict) -> None:
        self.csv.writerow(row)
        self.pending.append(row.copy())
        self.count += 1
        if len(self.pending) >= PARQUET_BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        if self.pending:
            self.parquet.write_table(pa.Table.from_pylist(self.pending, schema=SOURCE_SCHEMA))
            self.pending.clear()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self.handle.close()
            self.parquet.close()


def generate_dataset(
    output_dir: Path,
    customers: int = 8,
    movements: int = 2000,
    seed: int = 42,
    period: str = "2026-09",
    *,
    largest_customer_movements: int | None = None,
    scenario_profile: str = "full",
) -> dict:
    """Write CSV + ZSTD Parquet sources in fixed-size batches.

    ``movements`` is the TOTAL number of logical movements, not a per-customer
    count. Optionally allocate ``largest_customer_movements`` to customer C0001
    for an explicit stress fixture. That customer is always clean/billable.
    ``scenario_profile`` selects which deliberate exceptions are injected (see
    ``SCENARIO_PROFILES``); removed exceptions become NORMAL rows.
    A manifest is written only after all files and their checksums are complete.
    """
    if scenario_profile not in SCENARIO_PROFILES:
        raise ValueError("scenario_profile must be one of: " + ", ".join(SCENARIO_PROFILES))
    excluded_scenarios = SCENARIO_PROFILES[scenario_profile]
    if isinstance(customers, bool) or not isinstance(customers, int) or not 1 <= customers <= 10000:
        raise ValueError("customers must be between 1 and 10000")
    if isinstance(movements, bool) or not isinstance(movements, int) or movements < customers:
        raise ValueError("movements must be an integer greater than or equal to customers")
    if largest_customer_movements is not None:
        if not isinstance(largest_customer_movements, int) or isinstance(largest_customer_movements, bool):
            raise ValueError("largest_customer_movements must be an integer")
        if not 1 <= largest_customer_movements <= movements - customers + 1:
            raise ValueError("largest_customer_movements must leave at least one row for every other customer")
        if largest_customer_movements * customers < movements:
            raise ValueError("largest_customer_movements cannot be smaller than the average customer allocation")
        if customers == 1 and largest_customer_movements != movements:
            raise ValueError("a one-customer dataset must allocate all movements to that customer")
    start = _month_start(period)
    finish = _next_month(start)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # An old complete manifest must never advertise partially rewritten files.
    (directory / "manifest.json").unlink(missing_ok=True)
    rng = random.Random(seed)
    customer_rows = [
        {
            "id": f"C{i:04d}", "legal_name": f"Synthetic Customer {i:04d} Ltd",
            "address": f"{i} Example Street, London, ZZ1 1ZZ, United Kingdom",
            "email": f"accounts.c{i:04d}@example.test",
            "alert_email": f"review.c{i:04d}@example.test", "vat_number": "DEMO-NOT-VALID",
        }
        for i in range(1, customers + 1)
    ]
    (directory / "customers.json").write_text(json.dumps(customer_rows, indent=2) + "\n")
    scenario_counts: Counter = Counter()
    customer_counts: Counter = Counter()
    source_a, source_b = _SourceWriter(directory, "source_a"), _SourceWriter(directory, "source_b")
    issue_index = 0
    truth_path = directory / "scenario_labels.csv"
    try:
        with truth_path.open("w", encoding="utf-8", newline="") as truth_handle:
            truth = csv.DictWriter(truth_handle, fieldnames=["movement_id", "customer_id", "scenario", "synthetic_risk_label"], lineterminator="\n")
            truth.writeheader()
            for i in range(movements):
                if largest_customer_movements is None:
                    customer_no = i % customers + 1
                elif i < largest_customer_movements:
                    customer_no = 1
                else:
                    customer_no = (i - largest_customer_movements) % (customers - 1) + 2
                customer = f"C{customer_no:04d}"
                scenario = "NORMAL"
                if customer_no > 1:
                    scenario = _SCENARIO_SLOTS.get(issue_index % 100, "NORMAL")
                    if scenario in excluded_scenarios:
                        scenario = "NORMAL"
                    issue_index += 1
                when = start + timedelta(seconds=i * int((finish - start).total_seconds() - 1) // movements)
                row = _row(rng, i + 1, customer, when, scenario == "HIGH_RISK")
                a, b = row.copy(), row.copy()
                a["record_id"], b["record_id"] = "A-" + row["record_id"], "B-" + row["record_id"]
                if scenario == "INVALID_AMOUNT":
                    a["amount"] = "-5.00"
                elif scenario == "MISSING_FEATURE":
                    a["expected_amount"] = ""
                elif scenario == "INVALID_TIMESTAMP":
                    a["event_at"] = "not-a-timestamp"
                elif scenario == "AMOUNT_MISMATCH":
                    b["amount"] = format(Decimal(b["amount"]) + Decimal("1.00"), ".2f")
                source_a.write(a)
                if scenario != "MISSING_COUNTERPART":
                    source_b.write(b)
                if scenario == "DUPLICATE_RETRY":
                    source_a.write(a)
                    source_b.write(b)
                elif scenario == "CONFLICTING_DUPLICATE":
                    conflict = a.copy()
                    conflict["amount"] = format(Decimal(a["amount"]) + Decimal("3.00"), ".2f")
                    source_a.write(conflict)
                elif scenario == "AMBIGUOUS_IDENTITY":
                    ambiguous = a.copy()
                    ambiguous["record_id"] += "-SECOND"
                    source_a.write(ambiguous)
                elif scenario == "SOURCE_REVISION":
                    for source, original in ((source_a, a), (source_b, b)):
                        revised = original.copy()
                        revised["revision"] = "2"
                        revised["description"] = "Synthetic purchased business service, source revision"
                        source.write(revised)
                truth.writerow({"movement_id": row["movement_id"], "customer_id": customer, "scenario": scenario, "synthetic_risk_label": int(scenario == "HIGH_RISK")})
                scenario_counts[scenario] += 1
                customer_counts[customer] += 1
    finally:
        source_a.close()
        source_b.close()

    # Supervised labels are intentionally present only in the historical training
    # artifact and the separate truth file, never in either serving source.
    training_rows = min(30000, max(3000, movements * 2))
    training_rng = random.Random(seed + 104729)
    training_start, training_end = start - timedelta(days=120), start - timedelta(days=2)
    training_path = directory / "training.parquet"
    pending: list[dict] = []
    with pq.ParquetWriter(training_path, TRAINING_SCHEMA, compression="zstd") as writer:
        for i in range(training_rows):
            event = training_start + (training_end - training_start) * (i / training_rows)
            risk = i % 20 == 0
            row = _row(training_rng, i + 1, f"HIST-{i % customers + 1:04d}", event, risk)
            row.update(label=int(risk), label_available_at=(event + timedelta(hours=24)).isoformat(), synthetic=True)
            pending.append(row)
            if len(pending) >= PARQUET_BATCH_SIZE:
                writer.write_table(pa.Table.from_pylist(pending, schema=TRAINING_SCHEMA))
                pending.clear()
        if pending:
            writer.write_table(pa.Table.from_pylist(pending, schema=TRAINING_SCHEMA))

    files = {}
    for path in (directory / "customers.json", source_a.csv_path, source_b.csv_path, source_a.parquet_path, source_b.parquet_path, training_path, truth_path):
        files[path.name] = {"sha256": _sha256(path), "bytes": path.stat().st_size}
    manifest = {
        "schema_version": "synthetic-source-v1", "synthetic": True,
        "contract": "Two independent one-to-one service records; A authoritative only in this simulation",
        "period": period, "seed": seed, "customers": customers, "movements": movements,
        "largest_customer_movements": max(customer_counts.values()),
        "customer_movement_counts": dict(sorted(customer_counts.items())),
        "clean_customer_id": "C0001", "scenario_profile": scenario_profile,
        "excluded_scenarios": sorted(excluded_scenarios),
        "scenario_counts": dict(sorted(scenario_counts.items())),
        "source_files": {"A": "source_a.csv", "B": "source_b.csv"},
        "source_rows": {"A": source_a.count, "B": source_b.count},
        "source_manifests": {
            "A": {"complete": True, "rows": source_a.count, "sha256": files["source_a.csv"]["sha256"]},
            "B": {"complete": True, "rows": source_b.count, "sha256": files["source_b.csv"]["sha256"]},
        },
        "training_rows": training_rows, "training_period_end_exclusive": start.isoformat(),
        "truth_file": truth_path.name, "files": files,
        "limitations": [
            "Synthetic, deliberately learnable patterns; metrics do not establish real detection quality",
            "C0001 is reserved for a clean monthly-close demonstration",
            "Counterpart completeness is a simulated source-delivery assertion, not real-source reconciliation evidence",
        ],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest
