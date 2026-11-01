"""Insight snapshots are truly read-only, respect configured bounds, and the CLI publishes whole files."""
from __future__ import annotations

from dataclasses import replace
import json
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from invoice_optimizer import cli, insights


def test_snapshot_is_read_only_repeatable_read_and_does_not_leak(db):
    with insights._snapshot(db) as (c, _):
        assert c.execute(text("SELECT current_setting('transaction_read_only'), current_setting('transaction_isolation')")).one() == \
            ("on", "repeatable read")
        with pytest.raises(DBAPIError, match="read-only"):
            c.execute(text("INSERT INTO operational_counters(name, value) VALUES ('insight-probe', 1)"))
    with db.transaction() as s:
        assert s.execute(text("SELECT current_setting('transaction_read_only'), current_setting('transaction_isolation')")).one() == \
            ("off", "read committed")


def test_statement_timeout_never_loosens_a_configured_bound(db, monkeypatch):
    with insights._snapshot(db) as (c, _):
        assert c.execute(text("SHOW statement_timeout")).scalar_one() == "2min"
    monkeypatch.setattr(db, "settings", replace(db.settings, db_statement_timeout_ms=5000))
    with insights._snapshot(db) as (c, _):
        assert c.execute(text("SHOW statement_timeout")).scalar_one() == "5s"
    monkeypatch.setattr(db, "settings", replace(db.settings, db_statement_timeout_ms=600_000))
    with insights._snapshot(db) as (c, _):
        assert c.execute(text("SHOW statement_timeout")).scalar_one() == "2min"


def test_empty_period_report_and_cli_leaves_no_temporary_files(db, tmp_path, monkeypatch, capsys):
    result = insights.report(db, "2026-09")
    overview = result["sections"]["overview"]
    assert (overview["movements"], overview["total_net"], overview["flagged_rate"]) == (0, None, None)
    assert result["sections"]["benford"]["status"] == "insufficient data"
    target = tmp_path / "out" / "report.json"
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: db.settings))
    for _ in range(2):
        monkeypatch.setattr(sys, "argv", ["invoice-optimizer", "insights", "--period", "2026-09", "--json", str(target)])
        cli.main()
        assert json.loads(capsys.readouterr().out)["report_path"] == str(target.resolve())
    assert json.loads(target.read_text())["insight"] == "report"
    assert [p.name for p in target.parent.iterdir()] == ["report.json"]
