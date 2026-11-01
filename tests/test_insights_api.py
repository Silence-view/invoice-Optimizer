"""The read-only insights API: authenticated, validated, bounded and cached."""
from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from invoice_optimizer import insights
from invoice_optimizer.api import INSIGHT_SECTIONS, INSIGHT_TIMEOUT_MS, InsightCache, InsightReader, create_app
from invoice_optimizer.models import Audit

from test_insights import exact  # noqa: F401  (fixture)

SECTION_NAMES = {"overview": "overview", "suppliers": "supplier_concentration", "duplicates": "duplicate_candidates",
                 "outliers": "amount_outliers", "benford": "benford", "trends": "trends", "risk": "risk_distribution",
                 "ageing": "review_ageing", "quality": "data_quality"}


def shared_client(db):
    return TestClient(create_app(db.settings, db), headers={"Authorization": "Bearer " + db.settings.api_token})


def test_insights_require_authentication(exact):
    with TestClient(create_app(exact.settings, exact)) as anonymous:
        for path in ("/api/insights/periods", "/api/insights/report?period=2026-09", "/api/insights/overview?period=2026-09",
                     "/api/insights/risk"):
            assert anonymous.get(path).status_code == 401, path
        assert anonymous.get("/api/insights/report", headers={"Authorization": "Bearer wrong-token"}).status_code == 401


def test_shared_read_only_sessions_may_read(exact):
    with TestClient(create_app(exact.settings, exact)) as client:
        login = client.post("/api/login", json={"token": exact.settings.api_token})
        assert login.status_code == 200 and login.json()["identity_type"] == "legacy"
        response = client.get("/api/insights/report?period=2026-09")
        assert response.status_code == 200 and response.json()["insight"] == "report"
    with shared_client(exact) as client:
        assert client.get("/api/insights/overview?period=2026-09").status_code == 200


@pytest.mark.parametrize("query", ["period=2026-13", "period=26-09", "period=2026-9", "period=2026-09x", "limit=0", "limit=101",
                                   "limit=abc", "customer=", "customer=" + "x" * 81, "customer=%20%20",
                                   "customer=a%00b"])
def test_report_parameters_are_validated(exact, query):
    with shared_client(exact) as client:
        assert client.get("/api/insights/report?" + query).status_code == 422, query


def test_section_parameters_are_validated(exact):
    with shared_client(exact) as client:
        assert client.get("/api/insights/unknown?period=2026-09").status_code == 404
        assert client.get("/api/insights/supplier_concentration?period=2026-09").status_code == 404
        for query in ("overview?window_days=3", "risk?threshold=2", "duplicates?min_sample=5", "duplicates?window_days=93",
                      "duplicates?window_days=-1", "outliers?threshold=0", "outliers?threshold=101", "outliers?min_sample=2",
                      "benford?period=2026-00", "trends?limit=500"):
            assert client.get("/api/insights/" + query).status_code == 422, query
        assert client.get("/api/insights/duplicates?period=2026-09&window_days=0").json()["window_days"] == 0
        outliers = client.get("/api/insights/outliers?period=2026-09&threshold=2.5&min_sample=3").json()
        assert (outliers["threshold"], outliers["min_sample"]) == (2.5, 3)


def test_report_shape_and_default_period(exact):
    with shared_client(exact) as client:
        periods = client.get("/api/insights/periods").json()
        assert periods["periods"] == ["2026-09", "2026-08"] and periods["latest"] == "2026-09"
        assert client.get("/api/insights/periods?customer=C2").json()["periods"] == ["2026-09"]
        report = client.get("/api/insights/report").json()
        assert report["scope"]["period"] == "2026-09" and report["scope"]["customer_id"] is None
        assert set(report["sections"]) == set(SECTION_NAMES.values())
        assert {"insight", "method", "scope", "limitations", "generated_at"} <= set(report)
        for name, section in report["sections"].items():
            assert section["insight"] == name and isinstance(section["limitations"], list) and section["limitations"]
            assert section["generated_at"] == report["generated_at"]  # one snapshot
        scoped = client.get("/api/insights/report?period=2026-09&customer=C1&limit=1").json()
        assert scoped["scope"]["customer_id"] == "C1" and scoped["sections"]["overview"]["total_net"] == "1000.00"
        assert len(scoped["sections"]["supplier_concentration"]["top_suppliers"]) == 1
        assert INSIGHT_SECTIONS == SECTION_NAMES
        for section, name in SECTION_NAMES.items():
            body = client.get(f"/api/insights/{section}?period=2026-09").json()
            assert body["insight"] == name and body["scope"]["period"] == "2026-09", section
        assert client.get("/api/insights/overview").json()["scope"]["period"] == "2026-09"


def test_money_is_exact_strings(exact):
    with shared_client(exact) as client:
        sections = client.get("/api/insights/report?period=2026-09").json()["sections"]
    overview = sections["overview"]
    assert overview["total_net"] == "2000.00"
    assert overview["net_by_currency"] == [{"currency": "GBP", "movements": 8, "net_amount": "2000.00"}]
    assert overview["flagged_rate"] == "0.3750"
    for row in sections["supplier_concentration"]["top_suppliers"]:
        assert isinstance(row["net_amount"], str) and isinstance(row["share"], str)
    for row in sections["supplier_concentration"]["customers"]:
        assert isinstance(row["net_amount"], str) and isinstance(row["hhi"], str)
    series = {row["period"]: row for row in sections["trends"]["scope_series"]}
    assert series["2026-09"]["net_amount"] == "2000.00" and series["2026-08"]["net_amount"] == "500.00"
    assert all(isinstance(row["net_amount"], str) for row in sections["trends"]["scope_series"])


def test_null_scores_are_counted_never_zero(exact):
    with shared_client(exact) as client:
        risk = client.get("/api/insights/risk?period=2026-09").json()
        overview = client.get("/api/insights/overview?period=2026-09").json()
    assert sum(row["versions"] for row in risk["histogram"]) == risk["scored"] == 6
    assert (risk["unavailable"], risk["not_assessed"]) == (1, 1)
    # Two scores of 0.0 and 0.05 are in bin 1; the NULL score is not among them.
    assert risk["histogram"][0] == {"bin": 1, "lower": 0.0, "upper": 0.1, "versions": 2}
    assert overview["model_scores"] == {"scored": 6, "unavailable": 1, "not_assessed": 1}
    with shared_client(exact) as client:
        empty = client.get("/api/insights/risk?period=2026-08&customer=C2").json()
    assert empty["mean_score"] is None and empty["quantiles"] == {"p50": None, "p90": None, "p99": None}
    assert empty["scored"] == 0 and all(row["versions"] == 0 for row in empty["histogram"])


def test_empty_workspace_has_no_default_period(db):
    with shared_client(db) as client:
        assert client.get("/api/insights/periods").json() == {"periods": [], "latest": None, "customer_id": None, "cache_seconds": 30}
        missing = client.get("/api/insights/report")
        assert missing.status_code == 404 and "No movements" in missing.json()["detail"]
        assert client.get("/api/insights/benford?period=2026-09").json()["status"] == "insufficient data"


def test_cache_reuses_results_per_arguments(exact, monkeypatch):
    calls = []
    original = insights.report
    monkeypatch.setattr(insights, "report", lambda *a, **k: calls.append(a[1:]) or original(*a, **k))
    with shared_client(exact) as client:
        first = client.get("/api/insights/report?period=2026-09")
        second = client.get("/api/insights/report?period=2026-09")
        other = client.get("/api/insights/report?period=2026-09&customer=C1")
    assert (first.headers["X-Insights-Cache"], second.headers["X-Insights-Cache"], other.headers["X-Insights-Cache"]) == ("miss", "hit", "miss")
    assert first.json() == second.json() and len(calls) == 2
    assert first.headers["Cache-Control"] == "no-store"


def test_cache_expires_and_evicts():
    cache, clock = InsightCache(ttl=30, size=2), [0.0]
    import invoice_optimizer.api as api
    original = api.time.monotonic
    api.time.monotonic = lambda: clock[0]
    try:
        assert cache.get("a", lambda: 1) == (1, False) and cache.get("a", lambda: 2) == (1, True)
        clock[0] = 31.0
        assert cache.get("a", lambda: 3) == (3, False)
        cache.get("b", lambda: 4);cache.get("c", lambda: 5)
        assert len(cache.values) == 2
    finally:
        api.time.monotonic = original


def test_timeouts_map_to_503_and_are_not_cached(exact, monkeypatch):
    class Cancelled(Exception):
        sqlstate = "57014"
    original = insights.report

    def timeout(*args, **kwargs):
        raise OperationalError("SELECT ...", {}, Cancelled("canceling statement due to statement timeout"))
    monkeypatch.setattr(insights, "report", timeout)
    with shared_client(exact) as client:
        busy = client.get("/api/insights/report?period=2026-09")
        assert busy.status_code == 503 and busy.headers["Retry-After"] == "2" and busy.json()["retryable"] is True
        monkeypatch.setattr(insights, "report", original)
        recovered = client.get("/api/insights/report?period=2026-09")
        assert recovered.status_code == 200 and recovered.headers["X-Insights-Cache"] == "miss"


def test_insights_use_the_read_engine_and_change_nothing(exact):
    reader = InsightReader(exact)
    assert reader.engine is exact.read_engine and 0 < reader.settings.db_statement_timeout_ms <= INSIGHT_TIMEOUT_MS
    with exact.transaction() as s:
        before = s.scalar(select(func.count()).select_from(Audit))
    with shared_client(exact) as client:
        for section in SECTION_NAMES:
            assert client.get(f"/api/insights/{section}?period=2026-09").status_code == 200
        assert client.get("/api/insights/report?period=2026-09").status_code == 200
    with exact.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Audit)) == before
