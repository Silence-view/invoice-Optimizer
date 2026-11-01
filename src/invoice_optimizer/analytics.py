"""Read-only, bounded-memory PostgreSQL snapshot exports to Parquet.

Exports are analytical snapshots, not the authority for corrections or billing.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re
import shutil
from uuid import uuid4

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import and_, func, select, text

from .models import Assessment, Movement, MovementVersion

FETCH_ROWS = 1000
ROWS_PER_FILE = 100_000
TIMESTAMP = pa.timestamp("us", tz="UTC")
EXPORT_SCHEMA = pa.schema([
    ("movement_id", pa.string()), ("external_id", pa.string()),
    ("customer_id", pa.string()), ("period", pa.string()), ("state", pa.string()),
    ("current_version", pa.int32()), ("version_id", pa.string()),
    ("content_hash", pa.string()), ("amount", pa.decimal128(20, 2)),
    ("currency", pa.string()), ("origin", pa.string()),
    ("version_created_at", TIMESTAMP), ("payload_json", pa.string()),
    ("source_refs_json", pa.string()),
    ("rule_assessment_id", pa.string()), ("rule_status", pa.string()),
    ("rule_flagged", pa.bool_()), ("rule_version", pa.string()),
    ("rule_reasons_json", pa.string()), ("rule_evaluated_at", TIMESTAMP),
    ("model_assessment_id", pa.string()), ("model_status", pa.string()),
    ("model_score", pa.float64()), ("model_flagged", pa.bool_()),
    ("model_version", pa.string()), ("policy_version", pa.string()),
    ("feature_schema_version", pa.string()), ("features_json", pa.string()),
    ("feature_cutoff", TIMESTAMP), ("model_reasons_json", pa.string()),
    ("model_evaluated_at", TIMESTAMP),
])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _query(period: str):
    movements = Movement.__table__
    versions = MovementVersion.__table__
    rules = Assessment.__table__.alias("rules")
    model = Assessment.__table__.alias("model")
    schema_column = model.c.get("feature_schema_version")
    columns = [
        movements.c.id.label("movement_id"), movements.c.external_id,
        movements.c.customer_id, movements.c.period, movements.c.state,
        movements.c.current_version, versions.c.id.label("version_id"),
        versions.c.content_hash, versions.c.amount, versions.c.currency,
        versions.c.origin, versions.c.created_at.label("version_created_at"),
        versions.c.payload.label("payload_json"), versions.c.source_refs.label("source_refs_json"),
        rules.c.id.label("rule_assessment_id"), rules.c.status.label("rule_status"),
        rules.c.flagged.label("rule_flagged"), rules.c.rule_version,
        rules.c.reasons.label("rule_reasons_json"), rules.c.evaluated_at.label("rule_evaluated_at"),
        model.c.id.label("model_assessment_id"), model.c.status.label("model_status"),
        model.c.score.label("model_score"), model.c.flagged.label("model_flagged"),
        model.c.model_version, model.c.policy_version,
        model.c.features.label("features_json"), model.c.feature_cutoff,
        model.c.reasons.label("model_reasons_json"), model.c.evaluated_at.label("model_evaluated_at"),
    ]
    if schema_column is not None:
        columns.append(schema_column)
    return select(*columns).select_from(
        movements.outerjoin(versions, and_(versions.c.movement_id == movements.c.id, versions.c.version_no == movements.c.current_version))
        .outerjoin(rules, and_(rules.c.version_id == versions.c.id, rules.c.kind == "RULE"))
        .outerjoin(model, and_(model.c.version_id == versions.c.id, model.c.kind == "MODEL"))
    ).where(movements.c.period == period)


def _convert_row(row: dict) -> dict:
    converted = dict(row)
    converted.setdefault("feature_schema_version", None)
    for name in ("payload_json", "source_refs_json", "rule_reasons_json", "features_json", "model_reasons_json"):
        value = converted[name]
        converted[name] = None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    amount = converted["amount"]
    if amount is not None and (not isinstance(amount, Decimal) or not amount.is_finite()):
        raise ValueError("analytical amounts must arrive from PostgreSQL as finite Decimal values")
    for name in ("version_created_at", "rule_evaluated_at", "feature_cutoff", "model_evaluated_at"):
        value = converted[name]
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{name} must retain its timezone")
    return converted


class _ShardWriter:
    def __init__(self, directory: Path):
        self.directory = directory
        self.writer = None
        self.part_rows = 0
        self.files: list[dict] = []
        self.path = None

    def _open(self):
        self.path = self.directory / f"part-{len(self.files):05d}.parquet"
        self.writer = pq.ParquetWriter(self.path, EXPORT_SCHEMA, compression="zstd", use_dictionary=True, write_statistics=True)
        self.part_rows = 0

    def write(self, rows: list[dict]):
        offset = 0
        while offset < len(rows):
            if self.writer is None:
                self._open()
            length = min(len(rows) - offset, ROWS_PER_FILE - self.part_rows)
            self.writer.write_table(pa.Table.from_pylist(rows[offset:offset + length], schema=EXPORT_SCHEMA), row_group_size=FETCH_ROWS)
            self.part_rows += length
            offset += length
            if self.part_rows == ROWS_PER_FILE:
                self._close_part()

    def _close_part(self):
        if self.writer is not None:
            self.writer.close()
            self.writer = None
            self.files.append({"path": self.path.name, "rows": self.part_rows, "bytes": self.path.stat().st_size, "sha256": _sha256(self.path)})

    def close(self):
        # Keep a valid empty Parquet schema for a zero-movement export.
        if not self.files and self.writer is None:
            self._open()
        self._close_part()


def _normalise_aggregate(rows) -> list[dict]:
    values = []
    for currency, count, amount_count, amount, null_scores in rows:
        values.append({
            "currency": currency, "rows": int(count), "amount_rows": int(amount_count),
            "net_amount": None if amount is None else format(amount, ".2f"),
            "null_model_scores": int(null_scores),
        })
    return sorted(values, key=lambda item: (item["currency"] is not None, item["currency"] or ""))


def _verify_parquet(directory: Path, files: list[dict]) -> list[dict]:
    # DuckDB cannot open an in-memory database in read-only mode. Initialise an
    # empty temporary catalogue, reopen read-only, and issue SELECT only.
    catalogue = directory / ".verification.duckdb"
    duckdb.connect(str(catalogue)).close()
    try:
        with duckdb.connect(str(catalogue), read_only=True, config={"threads": "2", "memory_limit": "256MB"}) as connection:
            rows = connection.execute(
                "SELECT currency, count(*), count(amount), sum(amount), "
                "count(*) FILTER (WHERE model_score IS NULL) "
                "FROM read_parquet(?, hive_partitioning = false) GROUP BY currency",
                [[str(directory / item["path"]) for item in files]],
            ).fetchall()
        return _normalise_aggregate(rows)
    finally:
        catalogue.unlink(missing_ok=True)
        Path(str(catalogue) + ".wal").unlink(missing_ok=True)


def export_analytics(db, period: str, output_dir: Path) -> dict:
    """Atomically publish current-version evidence from a single MVCC snapshot.

    The connection is explicitly REPEATABLE READ and READ ONLY. Current means
    current in that snapshot, not current when a later consumer reads the files.
    Exported pending movements retain NULL version/amount/assessment fields.
    """
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise ValueError("period must be YYYY-MM")
    if db.engine.dialect.name != "postgresql":
        raise ValueError("analytics snapshot export requires PostgreSQL")
    root = Path(output_dir).resolve() / f"period={period}"
    root.mkdir(parents=True, exist_ok=True)
    export_id = "export-" + uuid4().hex
    staging, final = root / ("." + export_id + ".tmp"), root / export_id
    staging.mkdir()
    writer = _ShardWriter(staging)
    stream_counts = defaultdict(lambda: {"rows": 0, "amount_rows": 0, "net": Decimal("0.00"), "null_scores": 0})
    statement = _query(period)
    snapshot_query = statement.subquery("export_snapshot")
    aggregate_statement = select(
        snapshot_query.c.currency, func.count(), func.count(snapshot_query.c.amount),
        func.sum(snapshot_query.c.amount), func.count().filter(snapshot_query.c.model_score.is_(None)),
    ).group_by(snapshot_query.c.currency)
    try:
        with db.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                cutoff, snapshot_id = connection.execute(text("SELECT transaction_timestamp(), pg_current_snapshot()::text")).one()
                expected = _normalise_aggregate(connection.execute(aggregate_statement).all())
                result = connection.execution_options(stream_results=True, yield_per=FETCH_ROWS).execute(statement).mappings()
                try:
                    with localcontext() as decimal_context:
                        decimal_context.prec = 50
                        for batch in result.partitions(FETCH_ROWS):
                            rows = [_convert_row(row) for row in batch]
                            writer.write(rows)
                            for row in rows:
                                aggregate = stream_counts[row["currency"]]
                                aggregate["rows"] += 1
                                aggregate["null_scores"] += int(row["model_score"] is None)
                                if row["amount"] is not None:
                                    aggregate["amount_rows"] += 1
                                    aggregate["net"] += row["amount"]
                finally:
                    result.close()
        writer.close()
        observed = _normalise_aggregate([
            (currency, group["rows"], group["amount_rows"], group["net"] if group["amount_rows"] else None, group["null_scores"])
            for currency, group in stream_counts.items()
        ])
        parquet_totals = _verify_parquet(staging, writer.files)
        if expected != observed or expected != parquet_totals:
            raise ValueError("PostgreSQL, streamed rows and Parquet aggregates do not reconcile")
        manifest = {
            "schema_version": "current-movement-analytics-v1", "export_id": export_id,
            "period": period, "status": "COMPLETE", "snapshot_cutoff": cutoff.astimezone(timezone.utc).isoformat(),
            "postgres_snapshot": snapshot_id, "isolation": "REPEATABLE READ READ ONLY",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "rows": sum(item["rows"] for item in expected), "aggregates_by_currency": expected,
            "null_model_scores": sum(item["null_model_scores"] for item in expected),
            "verification": "PostgreSQL snapshot aggregates = streamed exact-Decimal aggregates = read-only DuckDB Parquet aggregates",
            "files": writer.files, "fetch_rows": FETCH_ROWS, "max_rows_per_file": ROWS_PER_FILE,
            "compression": "ZSTD", "amount_type": "DECIMAL(20,2)",
            "export_path": str(final),
            "scope": "Every movement in the period, its current version and RULE/MODEL assessments as visible in one database snapshot. No source rows or historical movement versions are substituted.",
            "limitations": [
                "An export is not a billing approval, operational state store or evidence that the source period is complete",
                "Pending movements and missing assessments remain NULL; absent model scores are not zero",
                "Quarantined source rows without canonical movements are outside this current-movement dataset",
                "Feature schema is NULL for legacy assessment records that did not persist it",
            ],
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        manifest_hash = _sha256(manifest_path)
        staging.rename(final)
        pointer = {"export_id": export_id, "manifest": f"{export_id}/manifest.json", "manifest_sha256": manifest_hash}
        pointer_temporary = root / f".latest-{uuid4().hex}.tmp"
        pointer_temporary.write_text(json.dumps(pointer, indent=2) + "\n")
        pointer_temporary.replace(root / "latest.json")
        return {**manifest, "manifest_path": str(final / "manifest.json"), "manifest_sha256": manifest_hash}
    except Exception:
        if writer.writer is not None:
            writer.writer.close()
        shutil.rmtree(staging, ignore_errors=True)
        raise
