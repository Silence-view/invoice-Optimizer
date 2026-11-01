"""Scenario profiles: the demo omits cases that can never carry a regression score."""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
from sqlalchemy import func, select

from invoice_optimizer.synthetic import SCENARIO_PROFILES, generate_dataset

UNSCORED = {"MISSING_FEATURE", "INVALID_AMOUNT", "INVALID_TIMESTAMP", "CONFLICTING_DUPLICATE"}
RULE_ONLY = {"AMOUNT_MISMATCH", "MISSING_COUNTERPART", "AMBIGUOUS_IDENTITY"}


def _rows(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _valid_amount(text: str) -> bool:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return False
    return value.is_finite() and value >= 0


def test_profiles_are_declared_and_full_stays_the_generator_default(tmp_path):
    assert SCENARIO_PROFILES["full"] == frozenset()
    assert SCENARIO_PROFILES["model-evidence"] == UNSCORED
    assert SCENARIO_PROFILES["model-only"] == UNSCORED | RULE_ONLY
    manifest = generate_dataset(tmp_path, customers=4, movements=400, seed=42)
    assert manifest["scenario_profile"] == "full" and manifest["excluded_scenarios"] == []
    assert UNSCORED | RULE_ONLY | {"HIGH_RISK"} <= set(manifest["scenario_counts"])


def test_unknown_profile_is_rejected_before_any_file_is_written(tmp_path):
    with pytest.raises(ValueError, match="scenario_profile"):
        generate_dataset(tmp_path / "out", customers=4, movements=400, scenario_profile="none")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("profile", ["model-evidence", "model-only"])
def test_profile_removes_unscorable_rows_but_keeps_regression_risk(tmp_path, profile):
    full = generate_dataset(tmp_path / "full", customers=4, movements=400, seed=42)
    data = tmp_path / profile
    manifest = generate_dataset(data, customers=4, movements=400, seed=42, scenario_profile=profile)
    excluded = SCENARIO_PROFILES[profile]
    assert manifest["scenario_profile"] == profile
    assert manifest["excluded_scenarios"] == sorted(excluded)
    assert excluded.isdisjoint(manifest["scenario_counts"])
    # Removed slots become NORMAL; regression-flagged rows and the movement total are unchanged.
    assert manifest["scenario_counts"]["HIGH_RISK"] == full["scenario_counts"]["HIGH_RISK"] > 0
    assert sum(manifest["scenario_counts"].values()) == 400
    assert manifest["scenario_counts"]["NORMAL"] == full["scenario_counts"]["NORMAL"] + sum(full["scenario_counts"][s] for s in excluded)
    a, b = _rows(data / "source_a.csv"), _rows(data / "source_b.csv")
    assert len(a) == manifest["source_rows"]["A"] and len(b) == manifest["source_rows"]["B"]
    for row in a + b:
        assert row["expected_amount"] != ""  # MISSING_FEATURE would leave the model UNAVAILABLE
        assert _valid_amount(row["amount"])  # INVALID_AMOUNT would be quarantined
        datetime.fromisoformat(row["event_at"])  # INVALID_TIMESTAMP would be quarantined
    keys = Counter((row["record_id"], row["revision"]) for row in a)
    amounts = {}
    for row in a:
        amounts.setdefault((row["record_id"], row["revision"]), set()).add(row["amount"])
    assert all(len(v) == 1 for v in amounts.values())  # no CONFLICTING_DUPLICATE
    truth = Counter(row["scenario"] for row in _rows(data / "scenario_labels.csv"))
    assert dict(truth) == manifest["scenario_counts"]
    if profile == "model-only":
        assert {row["movement_id"] for row in a} == {row["movement_id"] for row in b}
        assert not any(row["record_id"].endswith("-SECOND") for row in a)
    else:
        assert manifest["scenario_counts"]["AMOUNT_MISMATCH"] > 0
    assert max(keys.values()) <= 2  # only DUPLICATE_RETRY repeats a key


def test_profile_generation_is_repeatable(tmp_path):
    first = generate_dataset(tmp_path / "a", customers=4, movements=400, seed=9, scenario_profile="model-evidence")
    second = generate_dataset(tmp_path / "b", customers=4, movements=400, seed=9, scenario_profile="model-evidence")
    assert first["files"] == second["files"]


def test_demo_default_has_no_review_case_without_a_regression_score(db):
    from invoice_optimizer.cli import demo
    from invoice_optimizer.models import Assessment, Invoice, Quarantine, ReviewCase

    receipt = demo(db, customers=4, movements=400, seed=42, period="2026-10")
    assert receipt["scenario_profile"] == "model-evidence"
    assert receipt["invoice_auto_approved"] is False and receipt["invoice_email_sent"] is False
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Quarantine)) == 0
        cases = s.scalars(select(ReviewCase)).all()
        assert cases, "the demo must still show deliberate exceptions"
        assert all(c.version_id is not None for c in cases)
        scored = {v for v, in s.execute(select(Assessment.version_id).where(Assessment.kind == "MODEL", Assessment.score.is_not(None)))}
        assert {c.version_id for c in cases} <= scored
        reasons = Counter(c.reason for c in cases)
        assert reasons["MODEL_RISK"] > 0
        assert "MODEL_UNAVAILABLE" not in reasons
        assert not any(r.startswith("QUARANTINE") for r in reasons)
        # The only MISSING_SOURCE_A left is the scored companion of an ambiguous A key,
        # not the quarantine knock-on the removed scenarios used to create.
        ambiguous = {c.version_id for c in cases if c.reason == "AMBIGUOUS_SOURCE_KEYS"}
        assert {c.version_id for c in cases if c.reason == "MISSING_SOURCE_A"} <= ambiguous
        assert set(reasons) == {"MODEL_RISK", "AMOUNT_MISMATCH", "MISSING_SOURCE_B", "AMBIGUOUS_SOURCE_KEYS", "MISSING_SOURCE_A"}
        assert s.scalar(select(func.count()).select_from(Assessment).where(Assessment.kind == "MODEL", Assessment.score.is_(None))) == 0
        invoice = s.scalar(select(Invoice).where(Invoice.customer_id == "C0001", Invoice.period == "2026-10", Invoice.status.notin_(["FAILED", "STALE"])))
        assert invoice is not None and invoice.status == "DRAFT"
