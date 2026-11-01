"""Insights are read-only, bounded and reconcile with the operational tables."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import sys

import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select

from invoice_optimizer import cli, insights
from invoice_optimizer.analytics import export_analytics
from invoice_optimizer.features import build_features
from invoice_optimizer.ingestion import ingest_file, load_customers
from invoice_optimizer.models import (Assessment, Audit, Batch, Customer, Movement, MovementVersion, Quarantine,
                                      ReviewCase)
from invoice_optimizer.synthetic import SOURCE_FIELDS, generate_dataset
from invoice_optimizer.worker import run_job

T = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
AS_OF = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
# (movement, customer, period, supplier, amount, model score | None=UNAVAILABLE | "none"=not assessed, rule reasons)
EXACT_ROWS = [
    ("M1", "C1", "2026-09", "S1", "400.00", 0.05, []),
    ("M2", "C1", "2026-09", "S1", "200.00", 0.95, []),
    ("M3", "C1", "2026-09", "S2", "300.00", None, []),
    ("M4", "C1", "2026-09", "S3", "100.00", "none", ["AMOUNT_MISMATCH"]),
    ("M5", "C2", "2026-09", "S1", "250.00", 0.10, []),
    ("M6", "C2", "2026-09", "S2", "250.00", 0.15, []),
    ("M7", "C2", "2026-09", "S3", "250.00", 1.0, []),
    ("M8", "C2", "2026-09", "S4", "250.00", 0.0, []),
    ("M9", "C1", "2026-08", "S1", "500.00", 0.20, []),
]


@pytest.fixture
def exact(db):
    with db.transaction() as s:
        for cid in ("C1", "C2"):
            s.add(Customer(id=cid, legal_name=f"Insight {cid} Ltd", address="Example UK", email=f"{cid}@example.test", alert_email="review@example.test"))
        s.flush()
        for i, (mid, cid, period, supplier, amount, score, rules) in enumerate(EXACT_ROWS):
            event = (T if period == "2026-09" else T - timedelta(days=31)) + timedelta(hours=i)
            payload = {"amount": amount, "supplier_id": supplier, "event_at": event.isoformat(), "currency": "GBP",
                       "expected_amount": "" if mid == "M4" else amount, "customer_age_days": "200",
                       "supplier_age_days": "400", "cross_border": "0"}
            state = "BLOCKED" if rules else "REVIEW" if score is None or (score != "none" and score > 0.5) else "ELIGIBLE"
            s.add(Movement(id=mid, external_id="EXT-" + mid, customer_id=cid, period=period, current_version=1, state=state))
            s.flush()
            s.add(MovementVersion(id="V" + mid, movement_id=mid, version_no=1, content_hash="h" + mid, payload=payload,
                                  source_refs=["R" + mid], amount=Decimal(amount), currency="GBP", created_at=event))
            s.flush()
            s.add(Assessment(version_id="V" + mid, kind="RULE", status="FAIL" if rules else "PASS", reasons=rules, flagged=bool(rules), feature_cutoff=event))
            if score != "none":
                s.add(Assessment(version_id="V" + mid, kind="MODEL", status="UNAVAILABLE" if score is None else "SCORED", score=score,
                                 flagged=score is not None and score > 0.5, reasons=["MODEL_UNAVAILABLE"] if score is None else [],
                                 model_version="m", policy_version="p", feature_schema_version="f", feature_cutoff=event))
        s.add(Movement(id="M10", external_id="EXT-M10", customer_id="C1", period="2026-09", current_version=0, state="PENDING"))
        s.add(Batch(id="B1", source="A", period="2026-09", object_key="k", sha256="k", original_name="a.csv", status="COMPLETE"))
        s.flush()
        for n, (reason, cid) in enumerate([("Missing amount", "C1"), ("Missing amount", None), ("Invalid event_at", "C2")]):
            s.add(Quarantine(id=f"Q{n}", batch_id="B1", period="2026-09", row_number=n + 1, reason=reason, payload={}, validated_customer_id=cid))
        for n, (hours, severity, status) in enumerate([(2, "HIGH", "OPEN"), (48, "MEDIUM", "OPEN"), (120, "HIGH", "OPEN"),
                                                      (240, "HIGH", "OPEN"), (400, "HIGH", "RESOLVED")]):
            s.add(ReviewCase(id=f"RC{n}", dedup_key=f"rc{n}", movement_id="M2", version_id="VM2", customer_id="C1", period="2026-09",
                             reason="MODEL_RISK" if severity == "MEDIUM" else "AMOUNT_MISMATCH", severity=severity, status=status,
                             owner="review@example.test", created_at=AS_OF - timedelta(hours=hours)))
    return db


def test_overview_counts_are_exact(exact):
    result = insights.overview(exact, "2026-09")
    assert (result["movements"], result["customers"], result["versioned_movements"], result["movements_without_version"]) == (9, 2, 8, 1)
    assert result["total_net"] == "2000.00" and result["net_by_currency"] == [{"currency": "GBP", "movements": 8, "net_amount": "2000.00"}]
    assert result["distinct_suppliers"] == 4
    assert (result["flagged"], result["rule_flagged"], result["model_flagged"], result["flagged_rate"]) == (3, 1, 2, "0.3750")
    assert result["model_scores"] == {"scored": 6, "unavailable": 1, "not_assessed": 1}
    assert result["quarantine"]["rows"] == 3
    assert result["quarantine"]["top_reasons"][0] == {"reason": "Missing amount", "rows": 2, "open": 2}
    assert {"state": "PENDING", "movements": 1} in result["state_mix"]
    assert {"method", "scope", "limitations", "generated_at"} <= set(result)
    scoped = insights.overview(exact, "2026-09", "C1")
    assert scoped["total_net"] == "1000.00" and scoped["quarantine"]["rows"] == 1


def test_hhi_is_exact_and_flags_above_2500(exact):
    result = insights.supplier_concentration(exact, "2026-09")
    customers = {row["customer_id"]: row for row in result["customers"]}
    # C1: shares 0.6, 0.3, 0.1 -> 3600 + 900 + 100. C2: four equal shares -> 4 * 625.
    assert customers["C1"]["hhi"] == "4600.00" and customers["C1"]["flagged"] is True
    assert customers["C1"]["classification"] == "highly concentrated" and customers["C1"]["top_supplier_share"] == "0.600000"
    assert customers["C2"]["hhi"] == "2500.00" and customers["C2"]["flagged"] is False
    assert customers["C2"]["classification"] == "moderately concentrated"
    assert (result["customers_evaluated"], result["customers_flagged"]) == (2, 1)
    top = result["top_suppliers"][0]
    assert (top["supplier_id"], top["net_amount"], top["share"], top["customers"]) == ("S1", "850.00", "0.425000", 2)
    assert len(insights.supplier_concentration(exact, "2026-09", limit=1)["top_suppliers"]) == 1


def test_trends_month_over_month_and_movers(exact):
    result = insights.trends(exact, "2026-09")
    assert len(result["periods"]) == 12 and result["periods"][-1] == "2026-09" and result["periods"][0] == "2025-10"
    series = {row["period"]: row for row in result["scope_series"]}
    assert series["2026-08"]["net_amount"] == "500.00" and series["2026-07"]["net_amount"] == "0.00"
    assert series["2026-09"]["net_amount"] == "2000.00" and series["2026-09"]["net_change_pct"] == "300.00"
    assert series["2026-08"]["net_change_pct"] is None
    movers = {row["customer_id"]: row for row in result["top_movers"]}
    assert movers["C2"]["net_change"] == "1000.00" and movers["C2"]["net_change_pct"] is None
    assert movers["C1"]["net_change_pct"] == "100.00" and movers["C1"]["previous_movements"] == 1
    assert set(result["mover_series"]) == {"C1", "C2"}


def test_risk_distribution_never_bins_null_scores(exact):
    result = insights.risk_distribution(exact, "2026-09")
    bins = {row["bin"]: row["versions"] for row in result["histogram"]}
    assert (bins[1], bins[2], bins[10]) == (2, 2, 2) and sum(bins.values()) == result["scored"] == 6
    assert (result["unavailable"], result["not_assessed"], result["flagged"]) == (1, 1, 2)
    assert {"kind": "RULE", "reason": "AMOUNT_MISMATCH", "versions": 1} in result["top_reasons"]


def test_review_ageing_buckets(exact):
    result = insights.review_ageing(exact, "2026-09", as_of=AS_OF)
    assert result["open_cases"] == 4
    assert [row["cases"] for row in result["by_age"]] == [1, 1, 1, 1]
    assert result["oldest_age_hours"] == 240.0
    reasons = {(row["reason"], row["severity"]): row for row in result["by_reason"]}
    assert reasons[("AMOUNT_MISMATCH", "HIGH")]["cases"] == 3 and reasons[("AMOUNT_MISMATCH", "HIGH")]["older_than_7d"] == 1
    with pytest.raises(ValueError, match="timezone"):
        insights.review_ageing(exact, "2026-09", as_of=datetime(2026, 9, 30))


def test_data_quality_and_benford_insufficient(exact):
    result = insights.data_quality(exact, "2026-09")
    missing = {row["field"]: row for row in result["missing_optional_fields"]}
    assert missing["expected_amount"] == {"field": "expected_amount", "missing": 1, "rate": "0.1250"}
    assert missing["cross_border"]["missing"] == 0
    assert result["source_mismatches"] == [{"reason": "AMOUNT_MISMATCH", "versions": 1}] and result["rule_failures"] == 1
    assert {"source": "A", "reason": "Missing amount", "rows": 2, "open": 2} in result["quarantine"]
    small = insights.benford(exact, "2026-09")
    assert small["status"] == "insufficient data" and small["n"] == 8
    assert small["chi_square"] is None and small["conformity"] is None
    assert sum(row["observed"] for row in small["digits"]) == 8


def test_duplicates_use_previous_period_only_as_context(exact):
    result = insights.duplicate_candidates(exact, "2026-09")
    assert result["total_pairs"] == 0 and result["pairs"] == []


@pytest.mark.parametrize("call", [
    lambda db: insights.overview(db, "2026-13"),
    lambda db: insights.overview(db, "2026-09", limit=0),
    lambda db: insights.overview(db, "2026-09", limit=insights.MAX_LIMIT + 1),
    lambda db: insights.overview(db, "2026-09", "x" * 81),
    lambda db: insights.duplicate_candidates(db, "2026-09", window_days=-1),
    lambda db: insights.amount_outliers(db, "2026-09", min_sample=1),
])
def test_inputs_are_validated(db, call):
    with pytest.raises(ValueError):
        call(db)


def test_benford_statistics_classification():
    expected = {d: round(100_000 * math.log10(1 + 1 / d)) for d in range(1, 10)}
    close = insights.benford_statistics(expected)
    assert close["status"] == "computed" and close["conformity"] == "close conformity"
    assert close["chi_square"] < 1 and close["chi_square_rejects_at_0_05"] is False and close["p_value"] > 0.99
    uniform = insights.benford_statistics({d: 100 for d in range(1, 10)})
    assert uniform["conformity"] == "nonconformity" and uniform["chi_square_rejects_at_0_05"] is True
    assert insights.benford_statistics({1: 299})["status"] == "insufficient data"


def _stub_scores(payloads, _):
    results = []
    for payload in payloads:
        try:
            features = build_features(payload)
        except ValueError as exc:
            results.append({"status": "UNAVAILABLE", "score": None, "flagged": False, "reason": str(exc)})
            continue
        score = min(1.0, features["amount_to_expected"] / 12)
        results.append({"status": "SCORED", "score": score, "flagged": score > 0.5, "model_version": "unit-model",
                        "policy_version": "unit-policy", "feature_schema_version": "unit-features", "feature_values": features})
    return results


def _planted(customer, record, supplier, amount, event):
    return {"record_id": record, "revision": "1", "movement_id": "MOV-" + record, "customer_id": customer,
            "event_at": event, "period": "2026-09", "currency": "GBP", "amount": amount, "description": "Planted insight row",
            "supplier_id": supplier, "expected_amount": amount, "customer_age_days": "300", "supplier_age_days": "500",
            "hour_utc": "", "cross_border": "0"}


@pytest.fixture
def synthetic(db, tmp_path, monkeypatch):
    import invoice_optimizer.ml
    monkeypatch.setattr(invoice_optimizer.ml, "score_batch", _stub_scores)
    data = tmp_path / "synthetic"
    manifest = generate_dataset(data, customers=4, movements=500, seed=7, period="2026-09")
    load_customers(db, data / "customers.json")
    for side in ("A", "B"):
        ingest_file(db, data / f"source_{side.lower()}.csv", side, "2026-09", manifest["source_rows"][side], True)
    planted = [_planted("C0002", "PLANT-DUP-1", "SUP-PLANT-D", "1234.56", "2026-09-15T10:00:00+00:00"),
               _planted("C0002", "PLANT-DUP-2", "SUP-PLANT-D", "1234.56", "2026-09-15T12:00:00+00:00")]
    planted += [_planted("C0003", f"PLANT-O-{i:02d}", "SUP-PLANT-O", f"{100 + i}.00", f"2026-09-{i + 2:02d}T09:00:00+00:00") for i in range(12)]
    planted.append(_planted("C0003", "PLANT-O-BIG", "SUP-PLANT-O", "5000.00", "2026-09-20T09:00:00+00:00"))
    for side in ("A", "B"):
        path = tmp_path / f"planted_{side}.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SOURCE_FIELDS)
            writer.writeheader()
            writer.writerows([{**row, "record_id": side + "-" + row["record_id"]} for row in planted])
        ingest_file(db, path, side, "2026-09", len(planted), True)
    while run_job(db):
        pass
    return db


def _ids(db, *records):
    with db.transaction() as s:
        return {external: mid for mid, external in s.execute(select(Movement.id, Movement.external_id).where(
            Movement.external_id.in_(["MOV-" + r for r in records])))}


def test_synthetic_planted_signals_and_reconciliation(synthetic):
    db = synthetic
    ids = _ids(db, "PLANT-DUP-1", "PLANT-DUP-2", "PLANT-O-BIG")
    dupes = insights.duplicate_candidates(db, "2026-09")
    pair = next(p for p in dupes["pairs"] if p["supplier_id"] == "SUP-PLANT-D")
    assert (pair["customer_id"], pair["amount"], pair["gap_hours"]) == ("C0002", "1234.56", 2.0)
    assert (pair["first"]["movement_id"], pair["second"]["movement_id"]) == (ids["MOV-PLANT-DUP-1"], ids["MOV-PLANT-DUP-2"])
    assert dupes["total_pairs"] >= 1
    outliers = insights.amount_outliers(db, "2026-09", limit=500)
    planted = [row for row in outliers["outliers"] if row["supplier_id"] == "SUP-PLANT-O"]
    assert [row["movement_id"] for row in planted] == [ids["MOV-PLANT-O-BIG"]]
    assert planted[0]["direction"] == "high" and planted[0]["robust_z"] > 100 and planted[0]["supplier_median"] == "106.00"
    assert outliers["groups_evaluated"] >= 1 and outliers["total_outliers"] == len(outliers["outliers"])

    big = insights.benford(db, "2026-09")
    assert big["status"] == "computed" and big["n"] >= 300
    counts = {row["digit"]: row["observed"] for row in big["digits"]}
    assert sum(counts.values()) == big["n"]
    recomputed = sum((counts[d] - big["n"] * math.log10(1 + 1 / d)) ** 2 / (big["n"] * math.log10(1 + 1 / d)) for d in range(1, 10))
    assert big["chi_square"] == round(recomputed, 4) and big["conformity"] in {label for _, label in insights.NIGRINI_FIRST_DIGIT} | {"nonconformity"}

    overview = insights.overview(db, "2026-09")
    with db.transaction() as s:
        current = (select(func.count(), func.sum(MovementVersion.amount)).select_from(Movement)
                   .join(MovementVersion, (MovementVersion.movement_id == Movement.id) & (MovementVersion.version_no == Movement.current_version))
                   .where(Movement.period == "2026-09"))
        count, net = s.execute(current).one()
        assert overview["movements"] == s.scalar(select(func.count()).select_from(Movement).where(Movement.period == "2026-09"))
        assert overview["quarantine"]["rows"] == s.scalar(select(func.count()).select_from(Quarantine).where(Quarantine.period == "2026-09"))
        unavailable = s.scalar(select(func.count()).select_from(Assessment).where(Assessment.kind == "MODEL", Assessment.score.is_(None)))
    assert overview["versioned_movements"] == count and overview["total_net"] == format(net, ".2f")
    assert overview["model_scores"]["unavailable"] == unavailable > 0
    risk = insights.risk_distribution(db, "2026-09")
    assert sum(row["versions"] for row in risk["histogram"]) == risk["scored"]
    assert risk["unavailable"] == unavailable
    assert insights.data_quality(db, "2026-09")["quarantine"]


def test_report_is_one_read_only_snapshot_and_cli_writes_json(synthetic, tmp_path, monkeypatch, capsys):
    db = synthetic
    with db.transaction() as s:
        before = (s.scalar(select(func.count()).select_from(Audit)), s.scalar(select(func.count()).select_from(ReviewCase)))
    result = insights.report(db, "2026-09", limit=5)
    assert set(result["sections"]) == {name for name, _ in insights.SECTIONS}
    assert {section["generated_at"] for section in result["sections"].values()} == {result["generated_at"]}
    json.dumps(result)
    assert all(len(section.get("pairs", [])) <= 5 and len(section.get("outliers", [])) <= 5 for section in result["sections"].values())
    scoped = insights.report(db, "2026-09", "C0002", limit=5)
    assert scoped["sections"]["overview"]["customers"] == 1
    with db.transaction() as s:
        assert (s.scalar(select(func.count()).select_from(Audit)), s.scalar(select(func.count()).select_from(ReviewCase))) == before

    target = tmp_path / "reports" / "insights.json"
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: db.settings))
    monkeypatch.setattr(sys, "argv", ["invoice-optimizer", "insights", "--period", "2026-09", "--customer", "C0002", "--json", str(target)])
    cli.main()
    receipt = json.loads(capsys.readouterr().out)
    written = json.loads(target.read_text())
    assert receipt["report_path"] == str(target.resolve()) and receipt["sections"] == sorted(written["sections"])
    assert written["scope"]["customer_id"] == "C0002" and written["insight"] == "report"


def test_parquet_history_matches_postgresql(synthetic, tmp_path):
    db = synthetic
    exported = tmp_path / "exports"
    export_analytics(db, "2026-09", exported)
    offline = insights.insights_from_parquet(exported, limit=500, memory_limit="256MB", threads=2)
    assert offline["scope"]["periods"] == ["2026-09"]
    online_pairs = insights.duplicate_candidates(db, "2026-09", limit=500)["pairs"]
    assert [(p["first"]["movement_id"], p["second"]["movement_id"]) for p in offline["duplicate_candidates"]["pairs"]] == \
           [(p["first"]["movement_id"], p["second"]["movement_id"]) for p in online_pairs]
    online_benford = insights.benford(db, "2026-09")
    assert offline["benford"]["digits"] == online_benford["digits"] and offline["benford"]["mad"] == online_benford["mad"]
    online_hhi = {(c["customer_id"], c["hhi"]) for c in insights.supplier_concentration(db, "2026-09", limit=500)["customers"]}
    assert {(c["customer_id"], c["hhi"]) for c in offline["supplier_concentration"]["customers"]} == online_hhi
    online_trends = insights.trends(db, "2026-09", limit=500)
    assert offline["trends"]["scope_series"] == online_trends["scope_series"]
    assert offline["trends"]["top_movers"] == online_trends["top_movers"]
    json.dumps(offline)

    part = next((exported / "period=2026-09").glob("export-*/part-00000.parquet"))
    # Same rows, different bytes: only the manifest checksum can detect it.
    pq.write_table(pq.ParquetFile(part).read(), part, compression="snappy")
    with pytest.raises(ValueError, match="checksum"):
        insights.insights_from_parquet(exported)
    with pytest.raises(ValueError, match="no published"):
        insights.insights_from_parquet(tmp_path / "empty")
