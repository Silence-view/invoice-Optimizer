"""CSV correction contracts on disposable PostgreSQL schemas.

These tests use the same real ingestion/assessment helpers as the invariant
suite. Only model inference is stubbed; mutations run through the production
versioned correction path, and no application/demo schema is touched.
"""
from copy import deepcopy
import csv
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from invoice_optimizer.api import create_app
from invoice_optimizer.models import (
    Assessment, Audit, Command, Job, Movement, MovementVersion, Outbox,
    ReviewCase, SourceRecord,
)
from invoice_optimizer.review_import import import_review_csv
from invoice_optimizer.worker import tick
from test_invariants import _ingest, _row, benign_model, customer


FIELDS = [
    "case_id", "expected_version", "current_a_amount", "current_b_amount",
    "current_description", "current_expected_amount", "source_a_amount",
    "source_b_amount", "description", "expected_amount", "review_reason",
]


@pytest.fixture
def mismatch(db, customer, benign_model, tmp_path):
    _ingest(db, tmp_path, "A", [_row(amount="10.10")])
    _ingest(db, tmp_path, "B", [_row(record="B1", amount="10.20")])
    tick(db, max_jobs=20, max_emails=0)
    with db.transaction() as session:
        case = session.scalar(select(ReviewCase).where(ReviewCase.reason == "AMOUNT_MISMATCH"))
        assert case is not None and case.status == "OPEN"
        version = session.get(MovementVersion, case.version_id)
        return {
            "case_id": case.id, "movement_id": case.movement_id,
            "version_id": version.id, "version_no": version.version_no,
        }


def worklist(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def correction(case, **changes):
    return {
        "case_id": case["case_id"], "expected_version": case["version_no"],
        "source_a_amount": "10.20",
        "review_reason": "Verified source A against retained purchase evidence.",
        **changes,
    }


def database_evidence(db, movement_id):
    """Compare domain state plus side effects, not merely a returned status."""
    with db.transaction() as session:
        movement = session.get(Movement, movement_id)
        versions = list(session.scalars(
            select(MovementVersion).where(MovementVersion.movement_id == movement_id)
            .order_by(MovementVersion.version_no)
        ))
        return {
            "current_version": movement.current_version,
            "movement_state": movement.state,
            "versions": [
                (v.id, v.version_no, v.amount, deepcopy(v.payload), list(v.source_refs))
                for v in versions
            ],
            "source_records": [
                (r.id, r.payload_hash, deepcopy(r.payload))
                for r in session.scalars(select(SourceRecord).order_by(SourceRecord.id))
            ],
            "cases": [
                (c.id, c.status, c.review_reason, c.reviewed_by)
                for c in session.scalars(select(ReviewCase).order_by(ReviewCase.id))
            ],
            "commands": session.scalar(select(func.count()).select_from(Command)),
            "jobs": session.scalar(select(func.count()).select_from(Job)),
            "outbox": session.scalar(select(func.count()).select_from(Outbox)),
            "audit": session.scalar(select(func.count()).select_from(Audit)),
        }


def test_exact_csv_correction_creates_new_version_and_preserves_raw_evidence(db, mismatch, tmp_path):
    before = database_evidence(db, mismatch["movement_id"])
    description = 'Verified service, item "A"\nSupporting purchase reference PO-123'
    path = worklist(tmp_path / "correction.csv", [correction(
        mismatch, description=description, expected_amount="10.20",
    )])
    result = import_review_csv(db, path, "csv-reviewer")
    assert result["applied"] == 1 and result["failed"] == 0
    assert result["results"][0]["row"] == 2
    assert result["results"][0]["status"] == "APPLIED"
    after = database_evidence(db, mismatch["movement_id"])
    assert after["current_version"] == mismatch["version_no"] + 1
    assert after["versions"][:-1] == before["versions"]
    assert after["source_records"] == before["source_records"]
    latest = after["versions"][-1]
    assert latest[2] == Decimal("10.20")
    assert latest[3]["source_values"]["A"]["amount"] == "10.20"
    assert latest[3]["source_values"]["B"]["amount"] == "10.20"
    assert latest[3]["description"] == description
    assert latest[3]["expected_amount"] == "10.20"
    assert latest[4] == before["versions"][-1][4]
    with db.transaction() as session:
        assert session.get(ReviewCase, mismatch["case_id"]).status == "SUPERSEDED"
        rule = session.scalar(select(Assessment).where(
            Assessment.version_id == latest[0], Assessment.kind == "RULE",
        ))
        assert rule.status == "PASS"
        assert session.scalar(select(Job).where(Job.kind == "SCORE", Job.status == "READY"))
        audit = session.scalar(select(Audit).where(Audit.action == "correction_applied"))
        assert audit.actor == "csv-reviewer"
        assert audit.details["reason"] == correction(mismatch)["review_reason"]


def test_stale_worklist_cannot_overwrite_a_successful_correction(db, mismatch, tmp_path):
    original = worklist(tmp_path / "original.csv", [correction(mismatch)])
    assert import_review_csv(db, original, "reviewer")["applied"] == 1
    expected = database_evidence(db, mismatch["movement_id"])
    stale = worklist(tmp_path / "stale.csv", [correction(mismatch, source_a_amount="999.99")])
    result = import_review_csv(db, stale, "reviewer")
    assert result["applied"] == 0 and result["failed"] == 1
    assert result["results"][0]["status"] == "REJECTED"
    assert "changed" in result["results"][0]["error"].lower()
    after=database_evidence(db, mismatch["movement_id"])
    assert {k:v for k,v in after.items() if k not in ('audit','commands')} == {k:v for k,v in expected.items() if k not in ('audit','commands')}
    assert after['audit']==expected['audit']+1 and after['commands']==expected['commands']+1


def test_identical_file_retry_is_idempotent_after_version_advance(db, mismatch, tmp_path):
    path = worklist(tmp_path / "retry.csv", [correction(mismatch)])
    first = import_review_csv(db, path, "reviewer")
    expected = database_evidence(db, mismatch["movement_id"])
    assert first["applied"] == 1
    assert import_review_csv(db, path, "reviewer") == first
    assert database_evidence(db, mismatch["movement_id"]) == expected
    assert expected["commands"] == 2


def test_1001_rows_are_rejected_before_even_the_first_valid_change(db, mismatch, tmp_path):
    before = database_evidence(db, mismatch["movement_id"])
    # First row would change a movement; later blank proposals must still count.
    records = [correction(mismatch)] + [
        {"case_id": mismatch["case_id"], "expected_version": mismatch["version_no"], "review_reason": ""}
        for _ in range(1000)
    ]
    path = worklist(tmp_path / "too-many.csv", records)
    with pytest.raises(ValueError, match="1000 rows"):
        import_review_csv(db, path, "reviewer")
    assert database_evidence(db, mismatch["movement_id"]) == before


def test_current_evidence_columns_never_become_proposed_patches(db, mismatch, tmp_path):
    before = database_evidence(db, mismatch["movement_id"])
    evidence = {
        "current_a_amount": "888.88", "current_b_amount": "777.77",
        "current_description": "Changed evidence must not become a patch",
        "current_expected_amount": "999.99",
    }
    untouched = worklist(tmp_path / "evidence-only.csv", [correction(
        mismatch, source_a_amount="", review_reason="", **evidence,
    )])
    result = import_review_csv(db, untouched, "reviewer")
    assert result["applied"] == 0 and result["failed"] == 0 and result["results"] == []
    assert database_evidence(db, mismatch["movement_id"]) == before

    proposed = worklist(tmp_path / "one-proposed-field.csv", [correction(mismatch, **evidence)])
    assert import_review_csv(db, proposed, "reviewer")["applied"] == 1
    after = database_evidence(db, mismatch["movement_id"])
    assert after["versions"][-1][3]["description"] == before["versions"][-1][3]["description"]
    assert after["versions"][-1][3]["expected_amount"] == before["versions"][-1][3]["expected_amount"]
    with db.transaction() as session:
        audit = session.scalar(select(Audit).where(Audit.action == "correction_applied"))
        assert audit.details["patch"] == {"source_a_amount": "10.20"}


def test_review_upload_auth_csrf_and_size_cap_leave_domain_state_unchanged(db, mismatch):
    before = database_evidence(db, mismatch["movement_id"])
    with TestClient(create_app(db.settings, db)) as client:
        small = {"file": ("review.csv", b"case_id,expected_version,review_reason\n", "text/csv")}
        assert client.post("/api/review/import", files=small).status_code == 401
        login = client.post("/api/operators/bootstrap", json={"setup_token": db.settings.api_token,
            "username":"reviewer","display_name":"Test Reviewer","password":"valid-long-test-password"})
        assert login.status_code == 200
        # Account creation is audited independently; import failures must add no
        # domain mutations or additional action evidence.
        before = database_evidence(db, mismatch["movement_id"])
        assert client.post("/api/review/import", files=small).status_code == 403
        response = client.post(
            "/api/review/import", headers={"X-CSRF-Token": login.json()["csrf_token"]},
            files={"file": ("oversize.csv", b"x" * (5 * 1024 * 1024 + 1), "text/csv")},
        )
        assert response.status_code == 413
        assert "5 MiB" in response.json()["detail"]
    assert database_evidence(db, mismatch["movement_id"]) == before
