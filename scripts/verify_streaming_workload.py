#!/usr/bin/env python3
"""Bounded, real PostgreSQL/HTTP/SSE streaming workload in a disposable schema.

Run only against the local development database. The retained receipt describes
this run, not production capacity. No invoice or transport worker is called.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from itertools import islice, zip_longest
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
import uvicorn

from invoice_optimizer.api import create_app
from invoice_optimizer.benchmark import _collect_counts, _peak_rss
from invoice_optimizer.config import Settings
from invoice_optimizer.db import Database, digest
from invoice_optimizer.ingestion import ingest_file, load_customers
from invoice_optimizer.ml import model_readiness
from invoice_optimizer.models import Assessment, Audit, Batch, Job, Movement, MovementVersion, ReviewCase
from invoice_optimizer.operations import Backpressure
from invoice_optimizer.review_history import ReviewEvent
from invoice_optimizer.synthetic import SOURCE_FIELDS, generate_dataset
from invoice_optimizer.worker import record_heartbeat, run_job


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_json(path, value, *, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}
    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return round(1000 * (ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)), 3)
    return {"count": len(ordered), "p50_ms": percentile(.50), "p95_ms": percentile(.95),
            "p99_ms": percentile(.99), "max_ms": round(1000 * ordered[-1], 3)}


def source_hashes():
    names = ["api.py", "assessment.py", "worker.py", "ingestion.py", "operations.py", "ml.py",
             "synthetic.py", "review_versions.py", "models.py", "db.py", "config.py", "invoice_preview.py"]
    return {"src/invoice_optimizer/" + name: sha256(ROOT / "src/invoice_optimizer" / name) for name in names}


def chunks(path, source, destination):
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        index = 0
        while rows := list(islice(reader, 1000)):
            index += 1
            output = destination / f"{source}-{index:05d}.csv"
            write_rows(output, rows)
            yield source, output, len(rows)


def write_rows(path, rows):
    with Path(path).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SOURCE_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def run(args):
    base = Settings.from_env().prepare()
    if not base.synthetic:
        raise ValueError("Only synthetic environments are supported")
    url = make_url(base.database_url)
    if url.host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("This harness requires a loopback PostgreSQL host")
    identifier = uuid4().hex
    schema = "invoice_stream_" + identifier
    assert re.fullmatch(r"invoice_stream_[a-f0-9]{32}", schema)
    run_root = base.data_dir / "benchmarks" / ("stream-" + identifier)
    run_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    private_access = run_root / "readonly-access.json"
    ready_path = ROOT / "output/verification/streaming-ready.json"
    receipt_path = ROOT / "output/verification/streaming-workload.json"
    public_url = f"http://127.0.0.1:{args.port}"
    token = secrets.token_urlsafe(48)
    password = secrets.token_urlsafe(36)
    receipt = {
        "kind": "isolated_synthetic_concurrent_streaming_workload", "status": "RUNNING", "synthetic": True,
        "run_label": args.label,
        "run_id": identifier, "started_at": utc_now(), "run_path": str(run_root),
        "parameters": {"movements": args.movements, "customers": args.customers,
                       "largest_customer_movements": args.largest_customer_movements,
                       "period": args.period, "seed": args.seed, "source_chunk_max_rows": 1000,
                       "worker_count": 2, "http_reader_count": 2, "sse_reader_count": 1,
                       "port": args.port, "hold_seconds": args.hold_seconds,
                       "source_manifest_complete": False, "worker_kinds": ["ASSESS", "SCORE"],
                       "deadline_seconds": args.deadline_seconds},
        "database_isolation": {"schema": schema, "created": False, "dropped": False},
        "stages_seconds": {}, "source_hashes_before": source_hashes(),
        "harness_sha256": sha256(__file__), "process_peak_rss_before": _peak_rss(),
        "host": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "limitations": [
            "One bounded local synthetic run; no extrapolation or two-million-movement capacity guarantee.",
            "Python peak RSS includes the API, two workers and input generation; excludes PostgreSQL and total host memory.",
            "Two synthetic HTTP readers plus one SSE client are measured; separate browser QA may add a small, unmeasured request load.",
            "Source delivery files are incremental and explicitly incomplete for monthly close.",
            "No invoice preparation, PDF rendering, issuance or email transport is exercised.",
            "Synthetic labels and amounts do not establish real-world fraud-detection quality.",
            "Latency percentiles use linear interpolation over actual successful HTTP request durations; failed requests are reported separately.",
        ],
    }
    stop = threading.Event()
    guard = threading.Lock()
    workers = []
    readers = []
    server_thread = None
    server = None
    database = None
    admin = None
    listener = None
    operator_client = None
    stage_start = time.perf_counter()
    measurement_start = None
    measurement_end = None
    deadline = None
    state = {"phase": "SETUP", "case_id": None, "previous_case_id": None, "movement_id": None}
    observations = {"http": [], "sse": [], "queue": [], "errors": [], "worker_jobs": [0, 0],
                    "source_batches": [], "backpressure_retries": 0}

    def phase():
        with guard:
            return state["phase"]

    def ready(status):
        with guard:
            identity = {key: state[key] for key in ("case_id", "previous_case_id", "movement_id")}
        write_json(ready_path, {"status": status, "synthetic": True, "url": public_url, "schema": schema,
            "run_id": identifier, "updated_at": utc_now(), "run_path": str(run_root),
            "private_access_path": str(private_access), "access": "shared read-only session; no approval permission",
            "parameters": receipt["parameters"], **identity})
        print(json.dumps({"status": status, "ready_path": str(ready_path), **identity}), flush=True)

    def error(component, exc):
        # Deliberately omit exception messages: client/DB errors can contain input or URL secrets.
        with guard:
            observations["errors"].append({"component": component, "exception": type(exc).__name__,
                                           "phase": state["phase"], "at": utc_now()})

    def ensure_deadline():
        if deadline is not None and time.perf_counter() > deadline:
            raise TimeoutError("Bounded workload deadline exceeded")
        with guard:
            if any(item["component"].startswith("worker") for item in observations["errors"]):
                raise RuntimeError("A worker thread failed")

    def wait_until(predicate, seconds=90):
        end = min(time.perf_counter() + seconds, deadline or float("inf"))
        while time.perf_counter() < end:
            ensure_deadline()
            value = predicate()
            if value:
                return value
            stop.wait(.1)
        raise TimeoutError("A bounded verification condition was not reached")

    def worker(index):
        heartbeat_at = 0.
        try:
            while not stop.is_set():
                if index == 0 and time.monotonic() - heartbeat_at >= 5:
                    record_heartbeat(database, "synthetic-stream-" + identifier)
                    heartbeat_at = time.monotonic()
                if run_job(database, kinds=("ASSESS", "SCORE")):
                    with guard:
                        observations["worker_jobs"][index] += 1
                else:
                    stop.wait(.05)
        except Exception as exc:
            error("worker-" + str(index), exc)

    def read_http(index):
        endpoints = ["/api/dashboard", "/api/cases?limit=25", "/api/invoices?limit=25", "/api/automation", "VERSIONS"]
        with httpx.Client(base_url=public_url, headers={"Authorization": "Bearer " + token}, timeout=15) as client:
            cursor = index
            while not stop.is_set():
                endpoint = endpoints[cursor % len(endpoints)]
                cursor += 1
                with guard:
                    request_phase, case_id = state["phase"], state["case_id"]
                if endpoint == "VERSIONS":
                    if case_id is None:
                        stop.wait(.1)
                        continue
                    endpoint = f"/api/cases/{case_id}/versions?limit=5"
                mark = time.perf_counter()
                status = None
                try:
                    response = client.get(endpoint)
                    status = response.status_code
                    response.raise_for_status()
                    payload = response.json()
                    if endpoint == "/api/dashboard":
                        with guard:
                            observations["queue"].append({"at": utc_now(), "phase": request_phase,
                                "ready": payload["counts"]["jobs_ready"], "running": payload["counts"]["jobs_running"],
                                "movements": payload["counts"]["movements"]})
                except Exception as exc:
                    error("http:" + endpoint.split("?")[0], exc)
                finally:
                    with guard:
                        observations["http"].append({"endpoint": "versions" if "/versions" in endpoint else endpoint.split("?")[0],
                            "phase": request_phase, "status": status, "seconds": time.perf_counter() - mark,
                            "started_elapsed_seconds": mark - (measurement_start or mark)})
                stop.wait(.1)

    def read_sse():
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=5, read=20, write=5, pool=5)) as client:
                with client.stream("GET", public_url + "/api/events", headers={"Authorization": "Bearer " + token}) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if stop.is_set():
                            return
                        if not line.startswith("data: "):
                            continue
                        payload = json.loads(line[6:])
                        summary = payload.get("summary", {})
                        with guard:
                            observations["sse"].append({"at": utc_now(), "phase": state["phase"],
                                "elapsed_seconds": time.perf_counter() - (measurement_start or time.perf_counter()),
                                "revision": payload.get("revision"), "stream_version": payload.get("stream_version"),
                                "counts": summary.get("counts", {})})
        except Exception as exc:
            if not stop.is_set():
                error("sse", exc)

    def ingest_delivery(source, path, rows, *, purpose="STREAM"):
        ensure_deadline()
        mark = time.perf_counter()
        while True:
            try:
                result = ingest_file(database, path, source, args.period, expected_rows=rows,
                                     complete=False, actor="test:synthetic-stream-ingestion")
                break
            except Backpressure:
                observations["backpressure_retries"] += 1
                ensure_deadline()
                stop.wait(.25)
        observations["source_batches"].append({"source": source, "path": str(path), "sha256": sha256(path),
            "bytes": path.stat().st_size, "rows": rows, "purpose": purpose,
            "seconds": round(time.perf_counter() - mark, 6), "result": result})

    def current_risk(movement_id, minimum_version=1):
        with database.transaction() as session:
            movement = session.get(Movement, movement_id)
            if not movement or movement.current_version < minimum_version:
                return None
            version = session.scalar(select(MovementVersion).where(MovementVersion.movement_id == movement_id,
                                      MovementVersion.version_no == movement.current_version))
            if not version:
                return None
            assessments = {a.kind: a for a in session.scalars(select(Assessment).where(Assessment.version_id == version.id))}
            rule, model = assessments.get("RULE"), assessments.get("MODEL")
            case = session.scalar(select(ReviewCase).where(ReviewCase.version_id == version.id,
                                  ReviewCase.reason == "MODEL_RISK", ReviewCase.status == "OPEN"))
            if not (case and rule and rule.status == "PASS" and model and model.status == "SCORED" and model.flagged):
                return None
            return {"movement_id": movement.id, "version_no": version.version_no, "version_id": version.id,
                    "case_id": case.id, "score": model.score, "model_version": model.model_version}

    def queue_idle():
        with database.transaction() as session:
            statuses = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
            if statuses.get("FAILED", 0):
                raise RuntimeError("One or more workload jobs failed")
            return not statuses.get("READY", 0) and not statuses.get("RUNNING", 0)

    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
        readiness = model_readiness(base.model_dir)
        if readiness["status"] != "READY":
            raise ValueError("A verified existing frozen model is required; this harness does not train")
        metadata = readiness["metadata"]
        cutoff = datetime.fromisoformat(metadata["information_cutoff"])
        if cutoff >= datetime.strptime(args.period, "%Y-%m").replace(tzinfo=timezone.utc):
            raise ValueError("Frozen model information cutoff must precede the serving period")
        scoped_url = url.update_query_dict({"options": f"-csearch_path={schema} -cstatement_timeout=30000 -clock_timeout=20000"})
        settings = replace(base, database_url=scoped_url.render_as_string(hide_password=False), database_read_url="",
            data_dir=run_root / "data", api_token=token, actor="test:synthetic-stream-read-only",
            provider="simulated", batch_size=1000, worker_batch_size=500, poll_seconds=.5,
            auto_close=False, auto_dispatch=False, public_base_url=public_url,
            session_secure=False, gmail_expected_email="").prepare()
        version = metadata["model_version"]
        source_version = base.model_dir / "versions" / version
        destination_version = settings.model_dir / "versions" / version
        destination_version.mkdir(parents=True)
        for name in ("metadata.json", "model.joblib"):
            shutil.copy2(source_version / name, destination_version / name)
        shutil.copy2(base.model_dir / "metadata.json", settings.model_dir / "metadata.json")
        copied = model_readiness(settings.model_dir)
        if copied["status"] != "READY" or copied["metadata"] != metadata:
            raise ValueError("Copied model does not match the verified active frozen model")
        receipt["model"] = {"model_version": version, "artifact_sha256": metadata["artifact_sha256"],
            "metadata_sha256": sha256(destination_version / "metadata.json"),
            "active_metadata_sha256": sha256(settings.model_dir / "metadata.json"),
            "information_cutoff": metadata["information_cutoff"], "reused_exact_existing_model": True}
        admin = create_engine(base.database_url, pool_pre_ping=True)
        with admin.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {schema}"))
        receipt["database_isolation"]["created"] = True
        database = Database(settings)
        database.init()
        receipt["stages_seconds"]["initialisation_and_model_copy"] = round(time.perf_counter() - stage_start, 6)
        mark = time.perf_counter()
        inputs = run_root / "inputs"
        manifest = generate_dataset(inputs, args.customers, args.movements, args.seed, args.period,
                                    largest_customer_movements=args.largest_customer_movements)
        load_customers(database, inputs / "customers.json")
        receipt["stages_seconds"]["input_generation_and_customer_load"] = round(time.perf_counter() - mark, 6)
        receipt["input"] = manifest
        receipt["input"]["manifest_sha256"] = sha256(inputs / "manifest.json")
        deliveries = run_root / "deliveries"
        deliveries.mkdir()
        app = create_app(settings, database)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False,
                                             log_level="warning", timeout_graceful_shutdown=5))
        server_thread = threading.Thread(target=lambda: server.run(sockets=[listener]), name="stream-api", daemon=True)
        server_thread.start()
        wait_until(lambda: server.started, 15)
        operator_client = httpx.Client(base_url=public_url, timeout=15)
        response = operator_client.post("/api/operators/bootstrap", json={"setup_token": token,
            "username": "synthetic.streaming.reviewer", "display_name": "Synthetic Streaming Test Reviewer",
            "password": password})
        response.raise_for_status()
        principal = response.json()
        operator_client.headers.update({"X-CSRF-Token": principal["csrf_token"], "Origin": public_url})
        response = operator_client.patch("/api/automation", json={"patch": {"email_delivery_enabled": False},
            "expected_revision": 0, "request_id": "stream-disable-mail-" + identifier,
            "reason": "Isolated synthetic verification: no email transport or invoice automation."})
        response.raise_for_status()
        receipt["synthetic_operator"] = {"actor": principal["actor"], "display_name": principal["display_name"],
                                        "identity_scope": "disposable synthetic schema only"}
        write_json(private_access, {"url": public_url, "token": token, "read_only": True}, private=True)
        if private_access.stat().st_mode & 0o077:
            raise RuntimeError("Private browser access file is not owner-only")
        ready("SERVER_READY")
        # A bounded connection window lets the separate UI observer attach before input starts.
        if args.observer_wait_seconds:
            time.sleep(args.observer_wait_seconds)
        measurement_start = time.perf_counter()
        deadline = measurement_start + args.deadline_seconds
        receipt["measurement_interval"] = {"started_at": utc_now(), "scope": "first worker/read start through final drained queue and SSE observation; excludes input generation, final UI hold and cleanup"}
        with guard:
            state["phase"] = "STREAMING"
        for index in range(2):
            thread = threading.Thread(target=worker, args=(index,), name=f"stream-worker-{index}", daemon=True)
            workers.append(thread)
            thread.start()
        for index in range(2):
            thread = threading.Thread(target=read_http, args=(index,), name=f"stream-reader-{index}", daemon=True)
            readers.append(thread)
            thread.start()
        sse_thread = threading.Thread(target=read_sse, name="stream-sse", daemon=True)
        readers.append(sse_thread)
        sse_thread.start()
        first_rows = {}
        for source in ("A", "B"):
            with (inputs / f"source_{source.lower()}.csv").open(newline="") as stream:
                first_rows[source] = next(csv.DictReader(stream))
        paired = zip_longest(chunks(inputs / "source_a.csv", "A", deliveries),
                             chunks(inputs / "source_b.csv", "B", deliveries))
        for pair_index, pair in enumerate(paired):
            for item in pair:
                if item is not None:
                    ingest_delivery(*item)
            if pair_index == 0:
                movement_id = digest([first_rows["A"]["customer_id"], first_rows["A"]["movement_id"]])
                with guard:
                    state["movement_id"] = movement_id
                # Same source record identity, strictly newer revisions; no direct domain/SQL edits.
                for revision in (2, 3):
                    for source in ("A", "B"):
                        row = {**first_rows[source], "revision": str(revision), "amount": "10000.00",
                               "expected_amount": "10.00", "customer_age_days": "1", "supplier_age_days": "1",
                               "cross_border": "1", "event_at": args.period + "-01T01:00:00+00:00", "hour_utc": "1",
                               "description": f"Synthetic streaming source revision {revision}; actual retained evidence"}
                        path = deliveries / f"revision-{revision}-{source}.csv"
                        write_rows(path, [row])
                        ingest_delivery(source, path, 1, purpose="VERSION_UPDATE")
                    if revision == 2:
                        old_risk = wait_until(lambda: current_risk(movement_id))
                        with guard:
                            state["case_id"] = old_risk["case_id"]
                    else:
                        new_risk = wait_until(lambda: current_risk(movement_id, old_risk["version_no"] + 1))
                        with guard:
                            state.update(case_id=new_risk["case_id"], previous_case_id=old_risk["case_id"])
                request_id = "stream-stale-review-" + identifier
                response = operator_client.post(f"/api/cases/{old_risk['case_id']}/resolve", json={
                    "expected_version": old_risk["version_no"], "outcome": "VERIFIED_VALID",
                    "reason": "Synthetic stale-version rejection probe; this request must never approve.",
                    "request_id": request_id})
                with database.transaction() as session:
                    attempt = session.scalar(select(Audit).where(Audit.action == "case_review_rejected",
                        Audit.entity_id == old_risk["case_id"], Audit.details["request_id"].astext == request_id))
                    old_case = session.get(ReviewCase, old_risk["case_id"])
                    approved = session.scalar(select(func.count()).select_from(ReviewEvent).where(ReviewEvent.request_id == request_id))
                    stale_ok = response.status_code == 409 and attempt is not None and attempt.actor == principal["actor"] and approved == 0 and old_case.reviewed_by is None
                    receipt["stale_review_probe"] = {"passed": stale_ok, "http_status": response.status_code,
                        "before": old_risk, "after": new_risk, "request_id": request_id,
                        "rejection_audit_id": attempt.id if attempt else None,
                        "rejection_actor": attempt.actor if attempt else None, "review_events_created": approved,
                        "old_case_status": old_case.status, "old_case_reviewed_by": old_case.reviewed_by}
                if not stale_ok:
                    raise AssertionError("Stale review was not safely rejected and attributed")
                ready("STREAMING_WITH_VERSION_HISTORY")
        receipt["source_delivery_finished_at"] = utc_now()
        receipt["stages_seconds"]["streaming_ingest_and_revision_probe"] = round(time.perf_counter() - measurement_start, 6)
        with guard:
            state["phase"] = "DRAINING"
        wait_until(queue_idle, args.deadline_seconds)
        def sse_caught_up():
            with guard:
                return any(event["counts"].get("movements") == args.movements
                           and event["counts"].get("jobs_ready") == 0
                           and event["counts"].get("jobs_running") == 0 for event in observations["sse"])
        wait_until(sse_caught_up, 30)
        measurement_end = time.perf_counter()
        receipt["measurement_interval"].update(finished_at=utc_now(), seconds=round(measurement_end - measurement_start, 6))
        with guard:
            state["phase"] = "VERIFYING"
        mark = time.perf_counter()
        counts = _collect_counts(database)
        with database.transaction() as session:
            current_versions = select(MovementVersion.id).join(Movement,
                (Movement.id == MovementVersion.movement_id) & (Movement.current_version == MovementVersion.version_no))
            coverage = {kind: session.scalar(select(func.count()).select_from(Assessment).where(
                Assessment.kind == kind, Assessment.version_id.in_(current_versions))) for kind in ("RULE", "MODEL")}
            coverage["current_versions"] = session.scalar(select(func.count()).select_from(current_versions.subquery()))
            coverage["model_statuses"] = dict(session.execute(select(Assessment.status, func.count()).where(
                Assessment.kind == "MODEL", Assessment.version_id.in_(current_versions)).group_by(Assessment.status)).all())
            coverage["rule_statuses"] = dict(session.execute(select(Assessment.status, func.count()).where(
                Assessment.kind == "RULE", Assessment.version_id.in_(current_versions)).group_by(Assessment.status)).all())
            declared_complete = session.scalar(select(func.count()).select_from(Batch).where(Batch.declared_complete.is_(True)))
            largest = session.scalar(select(func.count()).select_from(Movement).where(Movement.customer_id == "C0001"))
            review_count = session.scalar(select(func.count()).select_from(ReviewEvent))
            receipt["postgresql_version"] = session.execute(text("SHOW server_version")).scalar_one()
        receipt["counts"] = counts
        receipt["current_version_assessment_coverage"] = coverage
        receipt["stages_seconds"]["final_sql_verification"] = round(time.perf_counter() - mark, 6)
        with guard:
            measured_http = [item for item in observations["http"] if item["phase"] in ("STREAMING", "DRAINING")]
            measured_sse = [item for item in observations["sse"] if item["phase"] in ("STREAMING", "DRAINING")]
        checks = {"logical_movement_count": counts["movements"] == args.movements,
            "customer_count": counts["customers"] == args.customers, "largest_customer_count": largest == args.largest_customer_movements,
            "all_current_versions_have_both_assessments": all(coverage[key] == args.movements for key in ("current_versions", "RULE", "MODEL")),
            "all_jobs_done": bool(counts["job_statuses"]) and set(counts["job_statuses"]) == {"DONE"},
            "no_invoice_or_transport": counts["invoices"] == 0 and counts["delivery_attempts"] == 0,
            "no_reviews_approved": review_count == 0, "all_deliveries_incomplete_for_monthly_close": declared_complete == 0,
            "counters_match_sql": all(counts["counter_reconciliation"].values()),
            "stale_review_rejected_with_synthetic_reviewer_audit": receipt["stale_review_probe"]["passed"],
            "sse_observed_progress": len({item["counts"].get("movements") for item in measured_sse}) >= 2,
            "sse_observed_final_drained_count": sse_caught_up(),
            "all_five_http_resources_exercised": len({item["endpoint"] for item in measured_http if item["status"] == 200}) == 5,
            "no_measured_http_failures": bool(measured_http) and all(item["status"] == 200 for item in measured_http),
            "both_workers_processed_jobs": all(observations["worker_jobs"]),
            "input_row_accounting": all(item["rows"] == sum(item["result"].get(key, 0)
                for key in ("accepted", "quarantined", "duplicates")) for item in observations["source_batches"]),
            "delivered_rows_match_generated_sources_plus_four_revision_rows": sum(item["rows"] for item in observations["source_batches"]) == sum(manifest["source_rows"].values()) + 4,
            "every_delivery_bounded_to_1000_rows": all(1 <= item["rows"] <= 1000 for item in observations["source_batches"]),
            "no_background_errors": not observations["errors"],
        }
        receipt["checks"] = checks
        if not all(checks.values()):
            raise AssertionError("One or more streaming receipt checks failed")
        with guard:
            state["phase"] = "UI_HOLD"
        ready("HOLDING_FOR_READ_ONLY_UI")
        hold_until = time.perf_counter() + args.hold_seconds
        while time.perf_counter() < hold_until:
            stop.wait(min(1, hold_until - time.perf_counter()))
        receipt["status"] = "PASS"
    except BaseException as exc:
        receipt["status"] = "FAIL"
        receipt["failure"] = {"type": type(exc).__name__, "phase": phase(),
                              "message": "Verification failed; inspect retained counts/checks and bounded error categories.",
                              "locations": [{"file": Path(frame.filename).name, "line": frame.lineno,
                                             "function": frame.name} for frame in traceback.extract_tb(exc.__traceback__)[-6:]]}
        if measurement_start and not measurement_end:
            receipt.setdefault("measurement_interval", {}).update(finished_at=utc_now(), seconds=round(time.perf_counter() - measurement_start, 6), incomplete=True)
    finally:
        cleanup_mark = time.perf_counter()
        stop.set()
        for thread in workers:
            thread.join(timeout=35)
        for thread in readers:
            thread.join(timeout=22)
        # Let the SSE client's next bounded keepalive close its stream before
        # Uvicorn begins graceful shutdown; otherwise its five-second grace
        # unnecessarily cancels an otherwise healthy streaming request.
        if server:
            server.should_exit = True
        if server_thread:
            server_thread.join(timeout=10)
        alive = [thread.name for thread in workers + readers + ([server_thread] if server_thread else []) if thread.is_alive()]
        if operator_client:
            operator_client.close()
        if listener:
            listener.close()
        if database:
            database.close()
        if receipt["database_isolation"]["created"] and not alive:
            try:
                with admin.begin() as connection:
                    connection.execute(text("SET LOCAL lock_timeout='10s'"))
                    connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
                receipt["database_isolation"]["dropped"] = True
            except Exception as exc:
                receipt["cleanup_error"] = type(exc).__name__
                receipt["status"] = "FAIL"
        if admin:
            admin.dispose()
        if alive:
            receipt["cleanup_threads_still_alive"] = alive
            receipt["status"] = "FAIL"
        private_access.unlink(missing_ok=True)
        receipt["private_access_removed"] = not private_access.exists()
        receipt["source_hashes_after"] = source_hashes()
        receipt["source_candidate_unchanged"] = receipt["source_hashes_before"] == receipt["source_hashes_after"]
        if not receipt["source_candidate_unchanged"]:
            receipt["status"] = "FAIL"
        receipt["process_peak_rss_after"] = _peak_rss()
        receipt["finished_at"] = utc_now()
        receipt["stages_seconds"]["cleanup"] = round(time.perf_counter() - cleanup_mark, 6)
        receipt["total_wall_seconds_including_generation_hold_cleanup"] = round(time.perf_counter() - stage_start, 6)
        with guard:
            receipt["observations"] = observations
        selected = [item for item in observations["http"] if item["phase"] in ("STREAMING", "DRAINING")]
        groups = defaultdict(list)
        for item in selected:
            if item["status"] == 200:
                groups[item["endpoint"]].append(item["seconds"])
        receipt["http_latency"] = {endpoint: percentiles(values) for endpoint, values in sorted(groups.items())}
        receipt["http_measured_requests"] = len(selected)
        receipt["http_measured_errors"] = sum(item["status"] != 200 for item in selected)
        measured_sse = [event for event in observations["sse"] if event["phase"] in ("STREAMING", "DRAINING")]
        receipt["sse_summary"] = {"events": len(measured_sse), "first": measured_sse[0] if measured_sse else None,
            "last": measured_sse[-1] if measured_sse else None,
            "distinct_revisions": len({item["revision"] for item in measured_sse}),
            "monotonic_revisions": all(a["revision"] <= b["revision"] for a, b in zip(measured_sse, measured_sse[1:]))}
        receipt["queue_summary"] = {"sample_count": len(observations["queue"]),
            "sampled_peak_ready": max((item["ready"] for item in observations["queue"]), default=None),
            "sampled_peak_running": max((item["running"] for item in observations["queue"]), default=None),
            "final_pending": sum(receipt.get("counts", {}).get("job_statuses", {}).get(status, 0)
                                 for status in ("READY", "RUNNING")) if "counts" in receipt else None,
            "scope": "Sampled dashboard observations; a lower bound on the instantaneous peak, not a continuous queue maximum."}
        receipt["source_row_denominators"] = {
            "generated_logical_movements": args.movements,
            "generated_source_rows": receipt.get("input", {}).get("source_rows"),
            "generated_csv_source_bytes": sum(receipt.get("input", {}).get("files", {}).get(name, {}).get("bytes", 0)
                                               for name in ("source_a.csv", "source_b.csv")),
            "delivered_rows": sum(item["rows"] for item in observations["source_batches"]),
            "delivered_bytes_including_chunk_headers_and_revision_files": sum(item["bytes"] for item in observations["source_batches"]),
            "revision_update_rows": sum(item["rows"] for item in observations["source_batches"] if item["purpose"] == "VERSION_UPDATE"),
            "accepted_rows": sum(item["result"].get("accepted", 0) for item in observations["source_batches"]),
            "quarantined_rows": sum(item["result"].get("quarantined", 0) for item in observations["source_batches"]),
            "duplicate_rows": sum(item["result"].get("duplicates", 0) for item in observations["source_batches"]),
        }
        write_json(run_root / "streaming-workload.json", receipt)
        write_json(receipt_path, receipt)
        ready("FINISHED" if receipt["status"] == "PASS" else "FAILED")
        print(json.dumps({"status": receipt["status"], "receipt": str(receipt_path),
                          "measurement_interval": receipt.get("measurement_interval"),
                          "schema_dropped": receipt["database_isolation"]["dropped"]}), flush=True)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--movements", type=int, default=100000)
    parser.add_argument("--customers", type=int, default=200)
    parser.add_argument("--largest-customer-movements", type=int, default=50000)
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--period", default="2026-09")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--hold-seconds", type=int, default=120)
    parser.add_argument("--observer-wait-seconds", type=int, default=15)
    parser.add_argument("--deadline-seconds", type=int, default=900)
    parser.add_argument("--label", default="final-candidate-streaming-workload")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", args.label):
        parser.error("Run label must contain 1..80 letters, digits, underscores or hyphens")
    if not 200 <= args.movements <= 100000 or not 2 <= args.customers <= 200:
        parser.error("Bounded run requires 200..100000 movements and 2..200 customers")
    if not 0 <= args.hold_seconds <= 300 or not 0 <= args.observer_wait_seconds <= 30:
        parser.error("UI hold must be 0..300s and initial observer wait 0..30s")
    if not 60 <= args.deadline_seconds <= 1800 or not 1024 <= args.port <= 65535:
        parser.error("Deadline must be 60..1800s; port 1024..65535")
    receipt = run(args)
    raise SystemExit(0 if receipt["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
