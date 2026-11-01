"""PostgreSQL integration regressions for financial workflow invariants.

The root conftest owns the isolated-schema ``db`` fixture. Model inference is
controlled here so these tests test workflow evidence rather than model quality.
"""
from __future__ import annotations

import csv
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import func, select

from invoice_optimizer import assessment, ingestion
from invoice_optimizer.assessment import correct_case, reconcile_job, resolve_case
from invoice_optimizer.billing import close_checks, create_invoice
from invoice_optimizer.db import Blocked, Conflict, counter, digest
from invoice_optimizer.delivery import recover_expired, reconcile_attempt, send_one
from invoice_optimizer.ingestion import ingest_file, load_customers, supersede_batch
from invoice_optimizer.models import (
    Assessment, Batch, BatchRecord, Counter, DeliveryAttempt, Job, Movement,
    MovementVersion, Outbox, Quarantine, ReviewCase, RuntimeOption, SourceRecord, now,
)
from invoice_optimizer.storage import ObjectStore
from invoice_optimizer.worker import tick


PERIOD = "2026-09"
CUSTOMER = "TEST-C1"


@pytest.fixture
def customer(db, tmp_path):
    path = tmp_path / "customers.json"
    path.write_text(json.dumps([{
        "id": CUSTOMER, "legal_name": "Invariant Test Customer Ltd (DEMO)",
        "address": "Synthetic address", "email": "accounts@example.test",
        "alert_email": "review@example.test", "vat_number": "DEMO-NOT-VALID",
    }]))
    load_customers(db, path)
    return CUSTOMER


@pytest.fixture
def benign_model(monkeypatch):
    def score(payloads, model_dir):
        return [{"status": "SCORED", "score": 0.02, "flagged": False,
                 "model_version": "test-frozen-model", "policy_version": "test-policy",
                 "feature_schema_version": "test-features", "feature_values": {}, "reason": None}
                for payload in payloads]
    monkeypatch.setattr("invoice_optimizer.ml.score_batch", score)


def _row(movement="M1", record="R1", revision="1", amount="10.00", period=PERIOD):
    return {"record_id": record, "revision": revision, "movement_id": movement,
            "customer_id": CUSTOMER, "event_at": period + "-15T12:00:00+00:00",
            "period": period, "currency": "GBP", "amount": amount,
            "description": "Synthetic invoice workflow service", "supplier_id": "SUP-1",
            "expected_amount": "10.00", "customer_age_days": "500",
            "supplier_age_days": "500", "cross_border": "0"}


def _csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(_row()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _ingest(db, tmp_path, source, rows, name=None, period=PERIOD, complete=True):
    path = _csv(tmp_path / (name or f"source-{source}.csv"), rows)
    return ingest_file(db, path, source, period, expected_rows=len(rows), complete=complete)


def _counts_match(db):
    with db.transaction() as session:
        actual = dict(session.execute(select(Movement.state, func.count()).group_by(Movement.state)).all())
        counters = {item.name: item.value for item in session.scalars(select(Counter))}
        assert counters.get("movements", 0) == sum(actual.values())
        for state in set(actual) | {key[6:] for key in counters if key.startswith("state:")}:
            assert counters.get("state:" + state, 0) == actual.get(state, 0)
        assert all(value >= 0 for key, value in counters.items() if key.startswith("state:"))


def test_stale_job_claim_cannot_write_assessments(db, customer, tmp_path):
    _ingest(db, tmp_path, 'A', [_row()])
    with db.transaction() as session:
        job=session.scalar(select(Job));job.status='RUNNING';job.claim_token='new-owner'
        job_id=job.id
    reconcile_job(db,job_id,'expired-owner')
    with db.transaction() as session:
        assert session.get(Job,job_id).status=='RUNNING'
        assert session.scalar(select(func.count()).select_from(Assessment))==0
        assert session.scalar(select(func.count()).select_from(MovementVersion))==0


def test_excel_numeric_expected_amount_has_same_canonical_money_boundary():
    data=_row();data['amount']=10.1;data['expected_amount']=10.1
    normalised=ingestion.normalize(data,PERIOD)
    assert normalised['amount']==normalised['expected_amount']=='10.10'
    from invoice_optimizer.features import build_features
    assert isinstance(build_features(normalised),dict)


def test_csv_and_excel_feature_types_replay_identically(db,customer,tmp_path):
    from openpyxl import Workbook
    row=_row();_ingest(db,tmp_path,'A',[row])
    typed={**row,'revision':1,'amount':10,'expected_amount':10,'customer_age_days':500,'supplier_age_days':500,'cross_border':0}
    workbook=Workbook();sheet=workbook.active;sheet.title='Source A'
    sheet.append(list(typed));sheet.append(list(typed.values()))
    path=tmp_path/'same-input.xlsx';workbook.save(path)
    result=ingest_file(db,path,'A',PERIOD,1,True,'Source A')
    assert result['duplicates']==1 and result['quarantined']==0 and result['accepted']==0
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SourceRecord))==1
        assert session.scalar(select(func.count()).select_from(BatchRecord))==2


def test_ingestion_parses_retained_bytes_not_mutable_original(db, customer, tmp_path, monkeypatch):
    original = _csv(tmp_path / "mutable.csv", [_row(amount="10.00")])
    put_file = ObjectStore.put_file

    def mutate_after_retention(store, path):
        key = put_file(store, path)
        if path == original:
            _csv(original, [_row(amount="999.00")])
        return key

    monkeypatch.setattr(ObjectStore, "put_file", mutate_after_retention)
    result = ingest_file(db, original, "A", PERIOD, expected_rows=1, complete=True)
    with db.transaction() as session:
        record = session.scalar(select(SourceRecord))
        batch = session.get(Batch, result["batch_id"])
        assert record.payload["amount"] == "10.00"
        assert "10.00" in ObjectStore(db.settings.data_dir / "objects").path(batch.object_key).read_text()
    assert "999.00" in original.read_text()
    _counts_match(db)


def test_same_file_retry_and_duplicate_rows_do_not_duplicate_evidence(db, customer, tmp_path, benign_model):
    a = _csv(tmp_path / "a.csv", [_row(), _row()])
    first = ingest_file(db, a, "A", PERIOD, expected_rows=2, complete=True)
    replay = ingest_file(db, a, "A", PERIOD, expected_rows=2, complete=True)
    _ingest(db, tmp_path, "B", [_row(record="B1")])
    tick(db, max_jobs=20, max_emails=0)
    assert replay["replayed"] is True
    assert first["accepted"] == 1 and first["duplicates"] == 1
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 2
        assert session.scalar(select(func.count()).select_from(Movement)) == 1
        assert session.scalar(select(func.count()).select_from(MovementVersion)) == 1
        assert session.scalar(select(func.count()).select_from(Assessment)) == 2
        assert session.scalar(select(func.count()).select_from(BatchRecord)) == 2
    _counts_match(db)


def test_overlapping_concurrent_deliveries_are_idempotent(db, customer, tmp_path, monkeypatch):
    rows = [_row(), _row(movement="M2", record="R2")]
    first = _csv(tmp_path / "first.csv", rows)
    second = _csv(tmp_path / "second.csv", list(reversed(rows)))
    barrier = Barrier(2)
    actual_lock = ingestion.lock

    def lock_with_barrier(session, key):
        if key == "source:A":
            barrier.wait(timeout=15)
        return actual_lock(session, key)

    monkeypatch.setattr(ingestion, "lock", lock_with_barrier)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(ingest_file, db, path, "A", PERIOD, 2, True) for path in (first, second)]
        results = [future.result(timeout=30) for future in futures]
    assert sum(result["accepted"] for result in results) == 2
    assert sum(result["duplicates"] for result in results) == 2
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 2
        assert session.scalar(select(func.count()).select_from(BatchRecord)) == 4
        assert all(batch.status == "COMPLETE" for batch in session.scalars(select(Batch)))
    _counts_match(db)


def test_concurrent_assessment_refreshes_version_after_lock(db, customer, tmp_path, monkeypatch):
    _ingest(db, tmp_path, "A", [_row()])
    _ingest(db, tmp_path, "B", [_row(record="B1")])
    with db.transaction() as session:
        jobs = list(session.scalars(select(Job.id).where(Job.kind == "ASSESS")))
    assert len(jobs) == 2
    barrier = Barrier(2)
    actual_lock = assessment.lock

    def lock_with_barrier(session, key):
        if key.startswith("customer-period:"):
            barrier.wait(timeout=15)
        return actual_lock(session, key)

    monkeypatch.setattr(assessment, "lock", lock_with_barrier)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reconcile_job, db, job) for job in jobs]
        for future in futures:
            future.result(timeout=30)
    with db.transaction() as session:
        assert session.scalar(select(Movement.current_version)) == 1
        assert session.scalar(select(func.count()).select_from(MovementVersion)) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "SCORE")) == 1
        assert all(session.get(Job, job).status == "DONE" for job in jobs)
    _counts_match(db)


def test_cross_source_first_observations_cannot_put_one_movement_in_two_periods(
    db, customer, tmp_path, monkeypatch,
):
    a = _csv(tmp_path / "september.csv", [_row(period="2026-09")])
    b = _csv(tmp_path / "october.csv", [_row(record="B1", period="2026-10")])
    barrier = Barrier(2)
    actual_lock = ingestion.lock

    def lock_with_barrier(session, key):
        if key.startswith("customer-period:"):
            barrier.wait(timeout=15)
        return actual_lock(session, key)

    monkeypatch.setattr(ingestion, "lock", lock_with_barrier)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(ingest_file, db, a, "A", "2026-09", 1, True),
                   executor.submit(ingest_file, db, b, "B", "2026-10", 1, True)]
        results = [future.result(timeout=30) for future in futures]
    assert sum(result["accepted"] for result in results) == 1
    assert sum(result["quarantined"] for result in results) == 1
    with db.transaction() as session:
        movement = session.scalar(select(Movement))
        sources = list(session.scalars(select(SourceRecord)))
        assert len(sources) == 1
        assert sources[0].period == movement.period
        assert sources[0].payload["period"] == movement.period
    _counts_match(db)


def test_xlsx_ingestion_reads_retained_object_with_original_format_hint(db, customer, tmp_path):
    from openpyxl import Workbook
    row = _row()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Movements"
    sheet.append(list(row))
    sheet.append(list(row.values()))
    path = tmp_path / "movements.xlsx"
    workbook.save(path)
    workbook.close()
    result = ingest_file(db, path, "A", PERIOD, expected_rows=1, complete=True, sheet="Movements")
    assert result["accepted"] == 1
    with db.transaction() as session:
        assert session.scalar(select(SourceRecord)).payload["amount"] == "10.00"
    _counts_match(db)


def test_xlsx_duplicate_headers_are_rejected_before_ingestion(db, customer, tmp_path):
    from openpyxl import Workbook
    row = _row()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Movements"
    sheet.append([*row, "amount"])
    sheet.append([*row.values(), "999.00"])
    path = tmp_path / "ambiguous-headers.xlsx"
    workbook.save(path)
    workbook.close()
    with pytest.raises(ValueError, match="[Hh]eader"):
        ingest_file(db, path, "A", PERIOD, expected_rows=1, complete=True, sheet="Movements")
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 0


def test_period_move_and_in_chunk_identity_change_are_quarantined(db, customer, tmp_path):
    _ingest(db, tmp_path, "A", [_row()])
    later = _ingest(db, tmp_path, "A", [_row(revision="2", period="2026-10")],
                    name="later.csv", period="2026-10")
    assert later["accepted"] == 0 and later["quarantined"] == 1
    conflict = _ingest(db, tmp_path, "B", [
        _row(record="B-COLLISION", movement="M2"),
        _row(record="B-COLLISION", revision="2", movement="M3"),
    ], name="identity-conflict.csv")
    assert conflict["accepted"] == 0 and conflict["quarantined"] == 2
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 1
        assert session.scalar(select(Movement.period)) == PERIOD
    _counts_match(db)


def test_rule_failure_commits_when_model_is_unavailable(db, customer, tmp_path, monkeypatch):
    _ingest(db, tmp_path, "A", [_row(amount="10.00")])
    _ingest(db, tmp_path, "B", [_row(record="B1", amount="12.00")])

    def unavailable(payloads, model_dir):
        raise OSError("Synthetic unavailable-model scenario")

    monkeypatch.setattr("invoice_optimizer.ml.score_batch", unavailable)
    tick(db, max_jobs=20, max_emails=0)
    with db.transaction() as session:
        rule = session.scalar(select(Assessment).where(Assessment.kind == "RULE"))
        model = session.scalar(select(Assessment).where(Assessment.kind == "MODEL"))
        assert rule.status == "FAIL" and "AMOUNT_MISMATCH" in rule.reasons
        assert model.status == "UNAVAILABLE" and model.score is None
        assert session.scalar(select(Movement.state)) == "BLOCKED"
        cases = list(session.scalars(select(ReviewCase)))
        assert {case.reason for case in cases} == {"AMOUNT_MISMATCH", "MODEL_UNAVAILABLE"}
        assert session.scalar(select(func.count()).select_from(Outbox)) == len(cases)
    with db.transaction() as session, pytest.raises(Blocked):
        close_checks(session, CUSTOMER, PERIOD)
    _counts_match(db)


def test_missing_score_cannot_be_labelled_scored_and_make_movement_eligible(db, customer, tmp_path, monkeypatch):
    _ingest(db, tmp_path, "A", [_row()])
    _ingest(db, tmp_path, "B", [_row(record="B1")])
    monkeypatch.setattr("invoice_optimizer.ml.score_batch", lambda payloads, model_dir: [
        {"status": "SCORED", "score": None, "flagged": False,
         "model_version": "test-model", "policy_version": "test-policy",
         "feature_schema_version": "test-features", "feature_values": {}}
        for _ in payloads
    ])
    tick(db, max_jobs=20, max_emails=0)
    with db.transaction() as session:
        assert session.scalar(select(Movement.state)) != "ELIGIBLE"
        model = session.scalar(select(Assessment).where(Assessment.kind == "MODEL"))
        if model:
            assert model.score is None
            assert model.status != "SCORED"
    with pytest.raises(Blocked):
        create_invoice(db, CUSTOMER, PERIOD, "close-with-missing-score", "tester")
    _counts_match(db)


def test_correction_versions_evidence_and_cannot_bypass_rule_with_review(db, customer, tmp_path, benign_model):
    _ingest(db, tmp_path, "A", [_row(amount="10.00")])
    _ingest(db, tmp_path, "B", [_row(record="B1", amount="12.00")])
    tick(db, max_jobs=20, max_emails=0)
    with db.transaction() as session:
        case = session.scalar(select(ReviewCase).where(ReviewCase.reason == "AMOUNT_MISMATCH"))
        case_id = case.id
        originals = {record.id: (record.payload_hash, record.payload) for record in session.scalars(select(SourceRecord))}
    with pytest.raises(Blocked, match="deterministic"):
        resolve_case(db, case_id, 1, "VERIFIED_VALID", "Manual approval cannot change evidence", "reject-rule-override", "tester")
    result = correct_case(db, case_id, 1, {"source_b_amount": "10.00"},
                          "Matched the retained source correction evidence", "correct-1", "tester")
    assert result["version_no"] == 2
    assert correct_case(db, case_id, 1, {"source_b_amount": "10.00"},
                        "Matched the retained source correction evidence", "correct-1", "tester") == result
    with pytest.raises(Conflict):
        correct_case(db, case_id, 1, {"source_b_amount": "11.00"}, "Different content", "correct-1", "tester")
    tick(db, max_jobs=20, max_emails=0)
    with db.transaction() as session:
        assert {record.id: (record.payload_hash, record.payload) for record in session.scalars(select(SourceRecord))} == originals
        assert session.scalar(select(func.count()).select_from(MovementVersion)) == 2
        assert session.scalar(select(Movement.state)) == "ELIGIBLE"
        assert session.get(ReviewCase, case_id).status == "SUPERSEDED"
        assert close_checks(session, CUSTOMER, PERIOD)[1] == 1
    _counts_match(db)


def test_missing_counterpart_blocks_close_despite_complete_file_counts(db, customer, tmp_path, benign_model):
    _ingest(db, tmp_path, "A", [_row(), _row(movement="M2", record="R2")])
    _ingest(db, tmp_path, "B", [_row(record="B1")])
    tick(db, max_jobs=20, max_emails=0)
    with pytest.raises(Blocked):
        create_invoice(db, CUSTOMER, PERIOD, "close-incomplete", "tester")
    _counts_match(db)


@pytest.mark.parametrize("unassignable_customer", ["", "UNKNOWN-CUSTOMER"])
def test_unassignable_quarantine_blocks_close_for_known_customers(
    db, customer, tmp_path, benign_model, unassignable_customer,
):
    invalid = _row(movement="M-UNKNOWN", record="R-UNKNOWN")
    invalid["customer_id"] = unassignable_customer
    _ingest(db, tmp_path, "A", [_row(), invalid])
    _ingest(db, tmp_path, "B", [_row(record="B1")])
    tick(db, max_jobs=20, max_emails=0)
    with pytest.raises(Blocked):
        create_invoice(db, CUSTOMER, PERIOD, "close-with-unassignable-row", "tester")
    _counts_match(db)


def test_quarantine_requires_newer_replacement_and_preserves_raw_history(db, customer, tmp_path, benign_model):
    bad = _row(amount="invalid")
    original = _ingest(db, tmp_path, "A", [bad], name="bad.csv")
    replacement = _ingest(db, tmp_path, "A", [_row(revision="2")], name="corrected.csv")
    _ingest(db, tmp_path, "B", [_row(record="B1")])
    tick(db, max_jobs=20, max_emails=0)
    with db.transaction() as session, pytest.raises(Blocked, match="quarantined"):
        close_checks(session, CUSTOMER, PERIOD)
    result = supersede_batch(db, original["batch_id"], replacement["batch_id"],
                             "Newer source revision supplies the corrected amount", "replace-bad", "tester")
    assert result["resolved_quarantine"] == 1
    with db.transaction() as session:
        old = session.get(Batch, original["batch_id"])
        assert old.superseded_by == replacement["batch_id"]
        assert session.scalar(select(Quarantine)).resolved_by_batch == replacement["batch_id"]
        assert "invalid" in ObjectStore(db.settings.data_dir / "objects").path(old.object_key).read_text()
        assert close_checks(session, CUSTOMER, PERIOD)[1] == 1
    _counts_match(db)


def test_unknown_submission_is_reconciled_without_another_send(db, tmp_path):
    with db.transaction() as session:
        row = Outbox(dedup_key="test-unknown-alert", kind="ALERT",
                     payload={"from": "ops@example.test", "to": ["review@example.test"],
                              "subject": "Synthetic alert", "body": "Synthetic test message"})
        session.add(row)
        session.add(RuntimeOption(key="provider_outcome", value={"outcome": "unknown"}))
    assert send_one(db) is True
    with db.transaction() as session:
        outbox = session.scalar(select(Outbox))
        attempt = session.scalar(select(DeliveryAttempt))
        assert outbox.status == attempt.status == "UNKNOWN"
        attempt_id = attempt.id
        assert outbox.attempts == 1
    assert send_one(db) is False
    result = reconcile_attempt(db, attempt_id, "tester")
    assert result["status"] == "ACCEPTED" and result["resubmitted"] is False
    assert send_one(db) is False
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(DeliveryAttempt)) == 1
    assert len(list((db.settings.data_dir / "mail").glob("*.eml"))) == 1


def test_expired_transport_lease_becomes_unknown_without_retry(db):
    with db.transaction() as session:
        row = Outbox(id="expired-outbox", dedup_key="expired", kind="ALERT", payload={},
                     status="IN_FLIGHT", lease_until=now() - timedelta(seconds=1), attempts=1)
        session.add(row)
        session.flush()
        session.add(DeliveryAttempt(id="expired-attempt", outbox_id=row.id, status="SUBMITTING"))
    assert recover_expired(db) == 1
    assert recover_expired(db) == 0
    assert send_one(db) is False
    with db.transaction() as session:
        assert session.get(Outbox, "expired-outbox").status == "UNKNOWN"
        assert session.get(DeliveryAttempt, "expired-attempt").status == "UNKNOWN"


def test_counter_deltas_rollback_with_business_transaction(db):
    with pytest.raises(RuntimeError):
        with db.transaction() as session:
            counter(session, "movements", 100)
            raise RuntimeError("Synthetic transactional rollback")
    with db.transaction() as session:
        assert session.get(Counter, "movements") is None
