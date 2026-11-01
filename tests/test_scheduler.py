"""Scheduler selection tests on PostgreSQL; financial gates remain in billing."""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text

from invoice_optimizer import scheduler
from invoice_optimizer.db import Blocked, digest
from invoice_optimizer.models import Audit, Batch, Customer, Invoice, Job, Movement, Outbox, RuntimeOption, now


def _customers(db, count=1):
    ids = [f"SCHED-{index:03d}" for index in range(1, count + 1)]
    with db.transaction() as session:
        session.add_all(Customer(id=customer_id, legal_name="Synthetic Scheduler Customer",
                                 address="Synthetic address", email="accounts@example.test",
                                 alert_email="review@example.test", vat_number="DEMO")
                        for customer_id in ids)
    return ids


def _batch(db, period="2026-09", source="A", status="COMPLETE", superseded=False):
    key = digest(["scheduler-batch", period, source])
    with db.transaction() as session:
        session.add(Batch(id=key, source=source, period=period, object_key=key, sha256=key,
                          original_name="synthetic.csv", status=status, expected_rows=0,
                          row_count=0, declared_complete=True,
                          superseded_by="replacement" if superseded else None))
        # Selection fixtures represent actual customer-period movement scopes.
        for customer_id in session.scalars(select(Customer.id)):
            movement_id=digest(["scheduler-movement",customer_id,period])
            if session.get(Movement,movement_id) is None:
                session.add(Movement(id=movement_id,external_id="selection-"+period,
                                     customer_id=customer_id,period=period,state="PENDING"))
    return key


def _invoice(session, customer_id, invoice_id, status="DRAFT", period="2026-09", revision=1):
    invoice = Invoice(id=invoice_id, number="DEMO-" + invoice_id, customer_id=customer_id,
                      period=period, revision=revision, status=status, currency="GBP",
                      net_total=Decimal("0.00"), vat_total=Decimal("0.00"), gross_total=Decimal("0.00"),
                      snapshot_hash="a" * 64, snapshot_key="a" * 64, document={},
                      fingerprint=digest([invoice_id, "approved-package"]),
                      approved_at=now() if status in ("APPROVED", "ISSUED") else None,
                      approved_by="human-reviewer" if status in ("APPROVED", "ISSUED") else None,
                      issued_at=now() if status == "ISSUED" else None)
    session.add(invoice)
    return invoice


def _record_close(monkeypatch):
    calls = []

    def prepare(db, customer_id, period, request_id, actor):
        calls.append((customer_id, period, request_id, actor))
        return {"id": "mock-selection-only", "status": "BUILDING"}

    monkeypatch.setattr(scheduler, "create_invoice", prepare)
    return calls


def test_only_ended_months_active_sources_customers_and_missing_invoices_are_candidates(db, monkeypatch):
    customers = _customers(db, 3)
    for period in ("2026-09", "2026-10", "2026-11"):
        _batch(db, period)
    _batch(db, "2026-07", status="FAILED")
    _batch(db, "2026-08", superseded=True)
    with db.transaction() as session:
        session.get(Customer, customers[2]).active = False
        _invoice(session, customers[1], "existing-draft")
    calls = _record_close(monkeypatch)
    result = scheduler.prepare_due_invoices(db, date(2026, 10, 27))
    assert [(call[0], call[1]) for call in calls] == [(customers[0], "2026-09")]
    assert result["attempted"] == result["prepared"] == 1
    assert result["timezone"] == "Europe/London"


def test_blocked_first_twenty_do_not_starve_later_customers(db, monkeypatch):
    customers = _customers(db, 25)
    _batch(db)
    calls = []

    def blocked(db, customer_id, period, request_id, actor):
        calls.append(customer_id)
        raise Blocked("Missing source coverage")

    monkeypatch.setattr(scheduler, "create_invoice", blocked)
    first = scheduler.prepare_due_invoices(db, date(2026, 10, 1))
    second = scheduler.prepare_due_invoices(db, date(2026, 10, 1))
    assert first["attempted"] == second["attempted"] == 20
    assert first["blocked"] == second["blocked"] == 20
    assert calls[:20] == customers[:20]
    assert calls[20:25] == customers[20:]
    assert len(set(calls[:20])) == len(set(calls[20:])) == 20
    assert set(calls) == set(customers)
    with db.transaction() as session:
        report = session.get(RuntimeOption, "scheduler.close.report").value["report"]
        assert all(item["reason"] == "Missing source coverage" for item in report["results"])
        assert session.get(RuntimeOption, "scheduler.close.cursor") is not None


def test_cursor_rotates_across_periods_and_does_not_duplicate_small_candidate_sets(db, monkeypatch):
    customers = _customers(db, 2)
    _batch(db, "2026-08")
    _batch(db, "2026-09")
    calls = _record_close(monkeypatch)
    scheduler.prepare_due_invoices(db, date(2026, 10, 1), max_customers=2)
    scheduler.prepare_due_invoices(db, date(2026, 10, 1), max_customers=2)
    assert [(call[1], call[0]) for call in calls] == [
        (period, customer) for period in ("2026-08", "2026-09") for customer in customers
    ]
    third = scheduler.prepare_due_invoices(db, date(2026, 10, 1), max_customers=20)
    assert third["attempted"] == 4
    assert len({(item["period"], item["customer_id"]) for item in third["results"]}) == 4


def test_concurrent_cycles_reserve_different_cursor_ranges(db, monkeypatch):
    customers = _customers(db, 25)
    _batch(db)
    calls = []

    def blocked(db, customer_id, period, request_id, actor):
        calls.append(customer_id)
        raise Blocked("Still awaiting review")

    monkeypatch.setattr(scheduler, "create_invoice", blocked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        runs = [executor.submit(scheduler.prepare_due_invoices, db, date(2026, 10, 1), 10) for _ in range(2)]
        results = [run.result(timeout=20) for run in runs]
    assert [result["attempted"] for result in results] == [10, 10]
    assert len(calls) == len(set(calls)) == 20
    assert set(calls) == set(customers[:20])


def test_real_close_gate_remains_blocking_and_reason_is_persisted(db):
    _customers(db)
    _batch(db, source="A")
    result = scheduler.prepare_due_invoices(db, date(2026, 10, 1))
    assert result["prepared"] == 0 and result["blocked"] == 1
    assert "source B" in result["results"][0]["reason"]
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 0
        assert session.get(RuntimeOption, "scheduler.close.report").value["report"] == result
        assert session.scalar(select(Audit).where(Audit.action == "scheduler_cycle")) is not None


def test_new_revision_does_not_replay_stale_invoice_forever(db, monkeypatch):
    customer = _customers(db)[0]
    _batch(db)
    calls = []

    def create(db, customer_id, period, request_id, actor):
        calls.append(request_id)
        revision = len(calls)
        invoice_id = "prepared-" + str(revision)
        with db.transaction() as session:
            _invoice(session, customer_id, invoice_id, status="BUILDING", period=period, revision=revision)
        return {"id": invoice_id, "status": "BUILDING"}

    monkeypatch.setattr(scheduler, "create_invoice", create)
    first = scheduler.prepare_due_invoices(db, date(2026, 10, 1))
    assert first["prepared"] == 1
    assert scheduler.prepare_due_invoices(db, date(2026, 10, 1))["attempted"] == 0
    with db.transaction() as session:
        session.get(Invoice, "prepared-1").status = "STALE"
    second = scheduler.prepare_due_invoices(db, date(2026, 10, 1))
    assert second["prepared"] == 1
    assert calls[0] != calls[1]
    assert len(calls[0]) <= 120 and calls[0].startswith("automatic:close:")


def test_failed_render_is_held_without_new_invoices_jobs_or_sequence_values(db, monkeypatch):
    customer = _customers(db)[0]
    _batch(db)
    with db.transaction() as session:
        _invoice(session, customer, "failed-render", status="FAILED")
        session.add(Job(dedup_key="render:failed-render", kind="RENDER", period="2026-09",
                        payload={"invoice_id": "failed-render"}, status="FAILED",
                        error="Demonstration: compiler unavailable"))
        session.execute(text("SELECT nextval('invoice_number_seq')"))
    calls = _record_close(monkeypatch)
    with db.transaction() as session:
        before = tuple(session.execute(text("SELECT last_value, is_called FROM invoice_number_seq")).one())
    for _ in range(3):
        result = scheduler.prepare_due_invoices(db, date(2026, 10, 1))
        assert result["attempted"] == result["prepared"] == 0
    assert calls == []
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 1
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        assert session.get(Invoice, "failed-render").status == "FAILED"
        assert tuple(session.execute(text("SELECT last_value, is_called FROM invoice_number_seq")).one()) == before


def test_failure_during_preparation_reports_hold_then_stops_automatic_retries(db, monkeypatch):
    customer = _customers(db)[0]
    _batch(db)

    def failed(db, customer_id, period, request_id, actor):
        with db.transaction() as session:
            _invoice(session, customer_id, "failed-before-return", status="FAILED")
        return {"id": "failed-before-return", "status": "BUILDING"}

    monkeypatch.setattr(scheduler, "create_invoice", failed)
    first = scheduler.prepare_due_invoices(db, date(2026, 10, 1))
    assert first["blocked"] == 1 and first["prepared"] == 0
    assert "held for investigation" in first["results"][0]["reason"]
    assert scheduler.prepare_due_invoices(db, date(2026, 10, 1))["attempted"] == 0


def test_one_scheduler_error_does_not_skip_other_customers(db, monkeypatch):
    customers = _customers(db, 3)
    _batch(db)

    def create(db, customer_id, period, request_id, actor):
        if customer_id == customers[0]:
            raise RuntimeError("Synthetic scope failure")
        if customer_id == customers[1]:
            raise Blocked("Review is still open")
        return {"id": "selection-only", "status": "BUILDING"}

    monkeypatch.setattr(scheduler, "create_invoice", create)
    result = scheduler.prepare_due_invoices(db, date(2026, 10, 1))
    assert result["attempted"] == 3
    assert result["errors"] == result["blocked"] == result["prepared"] == 1
    assert "Synthetic scope failure" in result["results"][0]["reason"]


def test_disabled_automation_leaves_all_state_untouched(db, monkeypatch):
    _customers(db)
    _batch(db)
    settings = {**vars(db.settings), "auto_close": False, "auto_dispatch": False}
    monkeypatch.setattr(db, "settings", SimpleNamespace(**settings))
    assert scheduler.prepare_due_invoices(db, date(2026, 10, 1))["enabled"] is False
    assert scheduler.dispatch_approved(db)["enabled"] is False
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(RuntimeOption)) == 0
        assert session.scalar(select(func.count()).select_from(Invoice)) == 0


def test_default_clock_is_london_local_date_at_month_boundary(db, monkeypatch):
    _customers(db)
    _batch(db, "2026-03")

    class LocalClock:
        @staticmethod
        def now(tz):
            assert str(tz) == "Europe/London"
            # This is still 31 March in UTC, but April has begun in London.
            return datetime(2026, 4, 1, 0, 30, tzinfo=tz)

    monkeypatch.setattr(scheduler, "datetime", LocalClock)
    calls = _record_close(monkeypatch)
    result = scheduler.prepare_due_invoices(db)
    assert result["as_of"] == "2026-04-01"
    assert len(calls) == 1 and calls[0][1] == "2026-03"


def test_dispatch_only_selects_current_human_approved_unissued_packages(db, monkeypatch):
    customers = _customers(db, 8)
    states = ["DRAFT", "APPROVED", "ISSUED", "BUILDING", "STALE", "FAILED", "UNKNOWN", "APPROVED"]
    with db.transaction() as session:
        for index, (customer, status) in enumerate(zip(customers, states)):
            invoice = _invoice(session, customer, f"invoice-{index}", status=status)
            if index == 7:
                invoice.approved_at = None
    calls = []
    monkeypatch.setattr(scheduler, "dispatch_invoice", lambda db, invoice_id, request_id, actor:
                        calls.append((invoice_id, request_id, actor)) or {"id": invoice_id, "status": "ISSUED"})
    result = scheduler.dispatch_approved(db)
    assert [call[0] for call in calls] == ["invoice-1"]
    assert result["issued"] == 1
    assert result["mode"] == "automatic_after_human_approval"
    with db.transaction() as session:
        assert session.get(Invoice, "invoice-0").status == "DRAFT"
        assert session.get(Invoice, "invoice-0").approved_at is None


def test_blocked_approved_packages_do_not_starve_later_approvals(db, monkeypatch):
    customers = _customers(db, 12)
    with db.transaction() as session:
        for index, customer in enumerate(customers):
            _invoice(session, customer, f"invoice-{index:02d}", status="APPROVED")
    calls = []

    def blocked(db, invoice_id, request_id, actor):
        calls.append(invoice_id)
        raise Blocked("New quarantine requires review")

    monkeypatch.setattr(scheduler, "dispatch_invoice", blocked)
    first, second = scheduler.dispatch_approved(db), scheduler.dispatch_approved(db)
    assert first["attempted"] == second["attempted"] == 10
    assert first["blocked"] == second["blocked"] == 10
    assert calls[10:12] == ["invoice-10", "invoice-11"]
    assert len(set(calls)) == 12
    with db.transaction() as session:
        assert session.get(RuntimeOption, "scheduler.dispatch.report").value["report"] == second


def test_dispatch_request_id_binds_candidate_fingerprint(db, monkeypatch):
    customer = _customers(db)[0]
    with db.transaction() as session:
        _invoice(session, customer, "one-approved", status="APPROVED")
    calls = []
    monkeypatch.setattr(scheduler, "dispatch_invoice", lambda db, invoice_id, request_id, actor:
                        calls.append(request_id) or {"id": invoice_id, "status": "ISSUED"})
    scheduler.dispatch_approved(db)
    scheduler.dispatch_approved(db)
    assert calls[0] == calls[1]
    assert calls[0].startswith("automatic:one-approved:") and len(calls[0]) <= 120
    with db.transaction() as session:
        session.get(Invoice, "one-approved").fingerprint = "b" * 64
    scheduler.dispatch_approved(db)
    assert calls[-1] != calls[0]


def test_real_dispatch_rechecks_revoked_approval_after_candidate_selection(db, monkeypatch):
    customer = _customers(db)[0]
    with db.transaction() as session:
        _invoice(session, customer, "revoked-after-read", status="APPROVED")
    original_candidates = scheduler._approved_candidates

    def revoke_after_read(db, limit):
        candidates = original_candidates(db, limit)
        with db.transaction() as session:
            invoice = session.get(Invoice, "revoked-after-read")
            invoice.status = "DRAFT"
            invoice.approved_at = None
        return candidates

    monkeypatch.setattr(scheduler, "_approved_candidates", revoke_after_read)
    result = scheduler.dispatch_approved(db)
    assert result["issued"] == 0 and result["blocked"] == 1
    with db.transaction() as session:
        assert session.get(Invoice, "revoked-after-read").issued_at is None
        assert session.scalar(select(func.count()).select_from(Outbox)) == 0


def test_unchanged_blocked_reports_do_not_grow_audit_every_tick(db, monkeypatch):
    _customers(db)
    _batch(db)
    scheduler.prepare_due_invoices(db, date(2026, 10, 1))
    with db.transaction() as session:
        before = session.scalar(select(func.count()).select_from(Audit))
    scheduler.prepare_due_invoices(db, date(2026, 10, 1))
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Audit)) == before


@pytest.mark.parametrize("value", [0, -1, 21, True, 1.5])
def test_close_attempt_budget_is_bounded(db, value):
    with pytest.raises(ValueError, match="max_customers"):
        scheduler.prepare_due_invoices(db, date(2026, 10, 1), max_customers=value)
