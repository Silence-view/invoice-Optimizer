from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select, update

from invoice_optimizer import analytics
from invoice_optimizer.models import Assessment, Audit, Customer, Movement, MovementVersion


@pytest.fixture
def analytics_data(db):
    timestamp = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    with db.transaction() as session:
        session.add(Customer(id="AN-C1", legal_name="Synthetic Analytics Ltd", address="Example UK", email="analytics@example.test", alert_email="review@example.test"))
        session.flush()
        for i, current, state, period in [(1, 2, "ELIGIBLE", "2026-09"), (2, 1, "REVIEW", "2026-09"), (3, 0, "PENDING", "2026-09"), (4, 1, "REVIEW", "2026-09"), (5, 1, "ELIGIBLE", "2026-08")]:
            session.add(Movement(id=f"AN-M{i}", external_id=f"EXT-{i}", customer_id="AN-C1", period=period, current_version=current, state=state))
        session.flush()
        for mid, number, amount in [(1, 1, "99999.99"), (1, 2, "0.10"), (2, 1, "0.20"), (2, 2, "9.00"), (4, 1, "0.00"), (5, 1, "100.00")]:
            session.add(MovementVersion(
                id=f"AN-V{mid}-{number}", movement_id=f"AN-M{mid}", version_no=number,
                content_hash=f"hash-{mid}-{number}", payload={"amount": amount, "event_at": timestamp.isoformat()},
                source_refs=[f"SOURCE-{mid}"], amount=Decimal(amount), currency="GBP", created_at=timestamp,
            ))
        session.flush()
        for mid, version, score in [(1, 2, 0.0), (2, 1, 0.8), (4, 1, None)]:
            session.add(Assessment(id=f"AN-R{mid}", version_id=f"AN-V{mid}-{version}", kind="RULE", status="PASS", score=None, flagged=False, reasons=[], rule_version="fixture-rules-v1", feature_cutoff=timestamp, evaluated_at=timestamp))
            session.add(Assessment(
                id=f"AN-A{mid}", version_id=f"AN-V{mid}-{version}", kind="MODEL",
                status="UNAVAILABLE" if score is None else "SCORED", score=score,
                flagged=score is not None and score > 0.5,
                reasons=["FEATURES_UNAVAILABLE"] if score is None else [],
                model_version="fixture-lr-v1", policy_version="fixture-threshold-v1",
                feature_schema_version="fixture-features-v1", features=None if score is None else {"amount_to_expected": 1.0},
                feature_cutoff=timestamp, evaluated_at=timestamp,
            ))
    return db


def _read_export(manifest):
    rows = []
    for file in manifest["files"]:
        rows.extend(pq.ParquetFile(Path(manifest["export_path"]) / file["path"]).read().to_pylist())
    return rows


def test_current_version_exact_decimal_and_unknown_evidence_export(analytics_data, tmp_path):
    manifest = analytics.export_analytics(analytics_data, "2026-09", tmp_path)
    rows = {row["movement_id"]: row for row in _read_export(manifest)}
    assert set(rows) == {"AN-M1", "AN-M2", "AN-M3", "AN-M4"}
    assert rows["AN-M1"]["version_id"] == "AN-V1-2"
    assert rows["AN-M1"]["amount"] == Decimal("0.10")
    assert rows["AN-M1"]["model_score"] == 0.0
    assert rows["AN-M4"]["model_score"] is None
    assert rows["AN-M4"]["model_status"] == "UNAVAILABLE"
    assert rows["AN-M3"]["amount"] is None and rows["AN-M3"]["model_status"] is None
    assert rows["AN-M1"]["feature_schema_version"] == "fixture-features-v1"
    assert json.loads(rows["AN-M1"]["features_json"]) == {"amount_to_expected": 1.0}
    assert manifest["rows"] == 4 and manifest["null_model_scores"] == 2
    gbp = next(item for item in manifest["aggregates_by_currency"] if item["currency"] == "GBP")
    assert gbp == {"currency": "GBP", "rows": 3, "amount_rows": 3, "net_amount": "0.30", "null_model_scores": 1}
    assert manifest["isolation"] == "REPEATABLE READ READ ONLY"
    for file in manifest["files"]:
        path = Path(manifest["export_path"]) / file["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == file["sha256"]
        assert pq.ParquetFile(path).schema_arrow.field("amount").type.scale == 2
    pointer = json.loads((tmp_path / "period=2026-09" / "latest.json").read_text())
    saved_manifest = tmp_path / "period=2026-09" / pointer["manifest"]
    assert hashlib.sha256(saved_manifest.read_bytes()).hexdigest() == pointer["manifest_sha256"]


def test_export_has_no_operational_database_mutations(analytics_data, tmp_path):
    def state():
        with analytics_data.engine.connect() as connection:
            return (
                connection.execute(select(Movement.id, Movement.current_version, Movement.state).order_by(Movement.id)).all(),
                connection.execute(select(func.count()).select_from(Audit)).scalar_one(),
                connection.execute(select(func.count()).select_from(Assessment)).scalar_one(),
            )
    before = state()
    analytics.export_analytics(analytics_data, "2026-09", tmp_path)
    assert state() == before


def test_snapshot_remains_consistent_during_concurrent_current_version_change(analytics_data, tmp_path, monkeypatch):
    original = analytics._convert_row
    changed = False

    def concurrent_update(row):
        nonlocal changed
        if not changed:
            changed = True
            with analytics_data.engine.begin() as connection:
                connection.execute(update(Movement).where(Movement.id == "AN-M2").values(current_version=2, state="PENDING_MODEL"))
        return original(row)

    monkeypatch.setattr(analytics, "_convert_row", concurrent_update)
    first = analytics.export_analytics(analytics_data, "2026-09", tmp_path)
    first_rows = {row["movement_id"]: row for row in _read_export(first)}
    assert first_rows["AN-M2"]["version_id"] == "AN-V2-1"
    assert first_rows["AN-M2"]["amount"] == Decimal("0.20")
    second = analytics.export_analytics(analytics_data, "2026-09", tmp_path)
    second_rows = {row["movement_id"]: row for row in _read_export(second)}
    assert second_rows["AN-M2"]["version_id"] == "AN-V2-2"
    assert second_rows["AN-M2"]["amount"] == Decimal("9.00")
    assert first["export_id"] != second["export_id"]


def test_shards_are_bounded_and_failed_verification_does_not_publish(analytics_data, tmp_path, monkeypatch):
    monkeypatch.setattr(analytics, "ROWS_PER_FILE", 2)
    good = analytics.export_analytics(analytics_data, "2026-09", tmp_path)
    assert [file["rows"] for file in good["files"]] == [2, 2]
    pointer = tmp_path / "period=2026-09" / "latest.json"
    before = pointer.read_bytes()
    monkeypatch.setattr(analytics, "_verify_parquet", lambda *_: [])
    with pytest.raises(ValueError, match="do not reconcile"):
        analytics.export_analytics(analytics_data, "2026-09", tmp_path)
    assert pointer.read_bytes() == before
    assert len(list((tmp_path / "period=2026-09").glob("export-*"))) == 1
    assert not list((tmp_path / "period=2026-09").glob(".export-*.tmp"))


def test_empty_period_exports_valid_schema_without_invented_totals(db, tmp_path):
    result = analytics.export_analytics(db, "2020-01", tmp_path)
    assert result["rows"] == 0 and result["aggregates_by_currency"] == []
    assert result["null_model_scores"] == 0
    assert result["files"][0]["rows"] == 0
    assert _read_export(result) == []


@pytest.mark.parametrize("period", ["2026-13", "../2026-09", "2026-9", "2026-09/elsewhere"])
def test_period_cannot_escape_partition(db, tmp_path, period):
    with pytest.raises(ValueError, match="YYYY-MM"):
        analytics.export_analytics(db, period, tmp_path)
