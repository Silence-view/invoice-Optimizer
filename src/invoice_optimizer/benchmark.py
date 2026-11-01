"""Measured synthetic pipeline run isolated in a disposable PostgreSQL schema."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import re
import resource
import shutil
import sys
import time
from uuid import uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from .analytics import export_analytics
from .db import Database, digest
from .ingestion import ingest_file, load_customers
from .models import Assessment, Batch, Counter, Customer, DeliveryAttempt, Invoice, Job, Movement, MovementVersion, Outbox, Quarantine, ReviewCase, SourceRecord
from .synthetic import generate_dataset


def _peak_rss() -> dict:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        value, units = int(raw), "bytes"
    elif sys.platform.startswith("linux"):
        value, units = int(raw * 1024), "KiB"
    else:
        value, units = None, "platform-specific; conversion unavailable"
    return {"bytes": value, "raw_ru_maxrss": raw, "raw_units": units}


def _collect_counts(db) -> dict:
    models = {
        "customers": Customer, "batches": Batch, "source_records": SourceRecord,
        "movements": Movement, "movement_versions": MovementVersion,
        "assessments": Assessment, "quarantine_rows": Quarantine,
        "review_cases": ReviewCase, "outbox_rows": Outbox,
        "delivery_attempts": DeliveryAttempt, "invoices": Invoice,
    }
    with db.transaction() as session:
        result = {name: session.scalar(select(func.count()).select_from(model)) for name, model in models.items()}
        result["movement_states"] = dict(session.execute(select(Movement.state, func.count()).group_by(Movement.state)).all())
        result["job_statuses"] = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        result["outbox_statuses"] = dict(session.execute(select(Outbox.status, func.count()).group_by(Outbox.status)).all())
        result["model_statuses"] = dict(session.execute(select(Assessment.status, func.count()).where(Assessment.kind == "MODEL").group_by(Assessment.status)).all())
        result["rule_statuses"] = dict(session.execute(select(Assessment.status, func.count()).where(Assessment.kind == "RULE").group_by(Assessment.status)).all())
        result["counters"] = dict(session.execute(select(Counter.name, Counter.value)).all())
        result["failed_jobs"] = [{"id": job.id, "kind": job.kind, "error": job.error} for job in session.scalars(select(Job).where(Job.status == "FAILED"))]
    states = result["movement_states"]
    counter_states = {name.removeprefix("state:"): value for name, value in result["counters"].items() if name.startswith("state:")}
    result["counter_reconciliation"] = {
        "movement_count_matches": result["counters"].get("movements", 0) == result["movements"],
        "every_state_matches": all(states.get(name, 0) == counter_states.get(name, 0) for name in set(states) | set(counter_states)),
        "state_sum_matches": sum(counter_states.values()) == result["movements"],
    }
    return result


def _database_footprint(db) -> dict:
    """Sizes and HOT ratios for before/after index comparisons; stats may lag ~1 s."""
    with db.transaction() as session:
        rows = session.execute(text("""SELECT c.relname, pg_table_size(c.oid) AS table_bytes, pg_indexes_size(c.oid) AS index_bytes,
                (SELECT count(*) FROM pg_index i WHERE i.indrelid = c.oid) AS indexes,
                coalesce(s.n_tup_upd, 0) AS updates, coalesce(s.n_tup_hot_upd, 0) AS hot_updates
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE n.nspname = current_schema() AND c.relkind = 'r' ORDER BY c.relname""")).all()
        version = session.scalar(text("SELECT max(version) FROM schema_migrations"))
    tables = {row.relname: {"table_bytes": row.table_bytes, "index_bytes": row.index_bytes, "indexes": row.indexes,
                            "updates": row.updates, "hot_updates": row.hot_updates} for row in rows}
    return {"schema_version": version, "table_bytes": sum(t["table_bytes"] for t in tables.values()),
            "index_bytes": sum(t["index_bytes"] for t in tables.values()),
            "indexes": sum(t["indexes"] for t in tables.values()), "tables": tables}


def run_benchmark(settings, customers, movements, seed, period, largest_customer_movements=None) -> dict:
    """Measure a real local pipeline, without billing, rendering or email sends.

    The benchmark's database schema is created and removed by this call. Files
    and a receipt remain under settings.data_dir/benchmarks for inspection.
    Existing application tables, model files and inbox/outbox rows are untouched.
    """
    if not settings.synthetic:
        raise ValueError("benchmark runs require the synthetic simulation contract")
    started = time.perf_counter()
    before_rss = _peak_rss()
    identifier = uuid4().hex
    schema = "invoice_bench_" + identifier
    if not re.fullmatch(r"invoice_bench_[a-f0-9]{32}", schema):
        raise ValueError("invalid generated benchmark schema")
    root = settings.data_dir.resolve() / "benchmarks" / ("run-" + identifier)
    root.mkdir(parents=True, exist_ok=False)
    receipt_path = root / "performance.json"
    stages = {}
    receipt = {
        "kind": "synthetic_postgresql_pipeline_benchmark", "synthetic": True,
        "status": "RUNNING", "started_at": datetime.now(timezone.utc).isoformat(),
        "run_id": identifier, "data_path": str(root), "receipt_path": str(receipt_path),
        "parameters": {"customers": customers, "movements": movements, "seed": seed, "period": period, "largest_customer_movements": largest_customer_movements},
        "stages_seconds": stages, "process_peak_rss_before": before_rss,
        "host": {"platform": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "database_isolation": {"schema": schema, "created": False, "dropped": False},
        "email_send_calls": 0, "invoice_render_calls": 0,
        "limitations": [
            "A single bounded synthetic run, not a production throughput, latency, concurrency or capacity guarantee",
            "No extrapolation to two million movements, all 200 customers or peak arrival rates",
            "Peak RSS is this Python process lifetime high-water mark, not PostgreSQL server memory or total host memory",
            "Assessment stage includes reconciliation, model batch inference, transactional persistence and alert outbox creation",
            "No PDF rendering, invoice close, approvals or email delivery are exercised by this benchmark",
            "Synthetic source and risk distributions do not establish real-world correctness or model detection quality",
        ],
    }

    def measured(name, function):
        mark = time.perf_counter()
        try:
            return function()
        finally:
            stages[name] = round(time.perf_counter() - mark, 6)

    admin = create_engine(settings.database_url, pool_pre_ping=True)
    database = None
    failure = None
    try:
        def initialise():
            nonlocal database
            with admin.begin() as connection:
                connection.execute(text(f"CREATE SCHEMA {schema}"))
            receipt["database_isolation"]["created"] = True
            scoped_url = make_url(settings.database_url).update_query_dict({"options": f"-csearch_path={schema}"})
            scoped_settings = replace(settings, database_url=scoped_url.render_as_string(hide_password=False), database_read_url="", data_dir=root / "data", provider="simulated").prepare()
            database = Database(scoped_settings)
            database.init()
            with database.engine.connect() as connection:
                receipt["postgresql_version"] = connection.execute(text("SHOW server_version")).scalar_one()
        measured("schema_initialisation", initialise)
        dataset = root / "inputs"
        manifest = measured("synthetic_generation", lambda: generate_dataset(dataset, customers, movements, seed, period, largest_customer_movements=largest_customer_movements))
        receipt["input"] = {
            "logical_movements": manifest["movements"], "customers": manifest["customers"],
            "largest_customer_movements": manifest["largest_customer_movements"],
            "source_rows": manifest["source_rows"], "files": manifest["files"],
            "csv_source_bytes": sum(manifest["files"][name]["bytes"] for name in ("source_a.csv", "source_b.csv")),
            "parquet_source_bytes": sum(manifest["files"][name]["bytes"] for name in ("source_a.parquet", "source_b.parquet")),
            "scenario_counts": manifest["scenario_counts"],
        }

        def prepare_model():
            metadata_file = settings.model_dir / "metadata.json"
            if metadata_file.exists():
                metadata = json.loads(metadata_file.read_text())
                period_start = datetime.strptime(period, "%Y-%m").replace(tzinfo=timezone.utc)
                information_cutoff = datetime.fromisoformat(metadata["information_cutoff"])
                if information_cutoff >= period_start:
                    raise ValueError("existing model cutoff does not precede the benchmark month; train an appropriate historical model first")
                shutil.copytree(settings.model_dir, database.settings.model_dir, dirs_exist_ok=True)
                return {"reused": True, "model_version": metadata["model_version"], "artifact_sha256": metadata["artifact_sha256"], "information_cutoff": metadata["information_cutoff"]}
            from .ml import train_model
            metadata = train_model(dataset / "training.parquet", database.settings.model_dir)
            return {"reused": False, "model_version": metadata["model_version"], "artifact_sha256": metadata["artifact_sha256"], "information_cutoff": metadata["information_cutoff"]}

        receipt["model"] = measured("model_preparation", prepare_model)
        measured("customer_load", lambda: load_customers(database, dataset / "customers.json"))
        batches = []
        for source in ("A", "B"):
            batches.append(measured("ingest_" + source.lower(), lambda source=source: ingest_file(database, dataset / f"source_{source.lower()}.csv", source, period, expected_rows=manifest["source_rows"][source], complete=True)))
        receipt["ingestion_batches"] = batches

        def assess_all():
            from .worker import tick
            totals = {"jobs": 0, "emails": 0, "uncertain_recovered": 0}
            while True:
                batch = tick(database, max_jobs=20, max_emails=0)
                for name, value in batch.items():
                    totals[name] += value
                if batch["jobs"] == 0:
                    break
            return totals
        receipt["worker"] = measured("reconciliation_and_scoring", assess_all)
        receipt["counts"] = measured("sql_and_counter_verification", lambda: _collect_counts(database))
        counts = receipt["counts"]
        checks = {
            "customer_count": counts["customers"] == customers,
            "movement_count": counts["movements"] == movements,
            "all_jobs_done": not counts["failed_jobs"] and all(status == "DONE" for status in counts["job_statuses"]),
            "model_inference_exercised": counts["model_statuses"].get("SCORED", 0) > 0,
            "model_assessment_coverage": sum(counts["model_statuses"].values()) == movements,
            "rule_assessment_coverage": sum(counts["rule_statuses"].values()) == movements,
            "no_delivery_attempts": counts["delivery_attempts"] == 0,
            "no_invoices": counts["invoices"] == 0,
            "no_email_dispatch": receipt["worker"]["emails"] == 0,
            **counts["counter_reconciliation"],
        }
        receipt["checks"] = checks
        # Timing-free business result: equal across code versions for equal inputs.
        receipt["business_fingerprint"] = digest({key: value for key, value in counts.items() if key != "failed_jobs"})
        receipt["database_footprint"] = measured("database_footprint", lambda: _database_footprint(database))
        if not all(checks.values()):
            raise RuntimeError("benchmark integrity checks failed: " + ", ".join(name for name, passed in checks.items() if not passed))
        export = measured("verified_parquet_export", lambda: export_analytics(database, period, root / "analytics"))
        receipt["analytics"] = {
            "rows": export["rows"], "null_model_scores": export["null_model_scores"],
            "manifest_path": export["manifest_path"], "manifest_sha256": export["manifest_sha256"],
            "bytes": sum(file["bytes"] for file in export["files"]), "files": len(export["files"]),
            "aggregates_by_currency": export["aggregates_by_currency"], "snapshot_cutoff": export["snapshot_cutoff"],
        }
        if export["rows"] != movements:
            raise RuntimeError("export count differs from requested movements")
        ingest_seconds = stages["ingest_a"] + stages["ingest_b"]
        receipt["observed_rates"] = {
            "physical_source_rows_per_ingest_second": round(sum(manifest["source_rows"].values()) / ingest_seconds, 3),
            "logical_movements_per_assessment_second": round(movements / stages["reconciliation_and_scoring"], 3),
            "logical_movements_per_export_second": round(movements / stages["verified_parquet_export"], 3),
            "scope": "Observed averages within this run only; not sustained or concurrent service rates",
        }
        receipt["status"] = "PASS"
    except Exception as exc:
        failure = exc
        receipt["status"] = "FAILED"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if database is not None:
            database.close()
        if receipt["database_isolation"]["created"]:
            try:
                def cleanup():
                    with admin.begin() as connection:
                        connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
                measured("schema_cleanup", cleanup)
                receipt["database_isolation"]["dropped"] = True
            except Exception as exc:
                receipt["status"] = "FAILED"
                receipt["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                if failure is None:
                    failure = exc
        admin.dispose()
        receipt["total_elapsed_seconds"] = round(time.perf_counter() - started, 6)
        receipt["process_peak_rss_after"] = _peak_rss()
        receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n")
    if failure is not None:
        raise RuntimeError(f"Benchmark failed; inspect {receipt_path}: {failure}") from failure
    return receipt
