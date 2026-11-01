"""Bounded monthly preparation and automatic dispatch after human approval.

The scheduler only selects candidates. Existing close/dispatch transactions remain
the authority for coverage, model evidence, immutable approval and issuance.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, true, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .billing import create_invoice, dispatch_invoice
from .db import Blocked, Conflict, NotFound, audit, digest, lock
from .models import Batch, Customer, Invoice, Movement, Outbox, RuntimeOption, now


from .operations import get_effective_automation


ACTOR = "automatic-scheduler"
_CLOSE_CURSOR = "scheduler.close.cursor"
_DISPATCH_CURSOR = "scheduler.dispatch.cursor"
_CLOSE_REPORT = "scheduler.close.report"
_DISPATCH_REPORT = "scheduler.dispatch.report"
_DIGEST_REPORT = "scheduler.digest.report"
DIGEST_ACTOR = "automatic-approval-reminder"
_UNUSABLE_INVOICE_STATES = ("STALE", "FAILED")


def _local_date() -> date:
    return datetime.now(ZoneInfo("Europe/London")).date()


def _limit(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _put_option(session, key: str, value: dict) -> None:
    option = session.get(RuntimeOption, key)
    if option is None:
        session.add(RuntimeOption(key=key, value=value))
    else:
        option.value = value


def _save_report(db, key: str, report: dict) -> None:
    """Retain bounded outcomes; unchanged idle/blocked loops do not grow audit."""
    with db.transaction() as session:
        lock(session, key)
        previous = session.get(RuntimeOption, key)
        if previous is not None and previous.value.get("report") == report:
            return
        _put_option(session, key, {"checked_at": now().isoformat(), "report": report})
        if report["attempted"]:
            audit(session, ACTOR, "scheduler_cycle", key, report)


def _close_candidates(db, before_period: str, limit: int) -> list[tuple[str, str]]:
    due_periods = (
        select(Batch.period.label("period"))
        .where(Batch.status == "COMPLETE", Batch.superseded_by.is_(None), Batch.period < before_period)
        .distinct().subquery()
    )
    active_invoice = select(Invoice.id).where(
        Invoice.customer_id == Customer.id,
        Invoice.period == due_periods.c.period,
        # Failed renders remain held for investigation. Recreating a failed
        # package every cycle would burn invoice numbers and repeat the fault.
        # Only evidence-staled packages permit automatic replacement.
        Invoice.status != "STALE",
    ).exists()
    has_movements = select(1).where(
        Movement.customer_id == Customer.id, Movement.period == due_periods.c.period).exists()
    query = (
        select(due_periods.c.period, Customer.id)
        .select_from(Customer).join(due_periods, true())
        .where(Customer.active.is_(True), has_movements, ~active_invoice)
        .order_by(due_periods.c.period, Customer.id)
    )
    with db.transaction() as session:
        # Reserve the cursor advance before doing slower business transactions.
        # Concurrent cycles may wrap to the same scope; stable command IDs plus
        # create_invoice's scope locks still prevent duplicate preparation.
        lock(session, _CLOSE_CURSOR)
        saved = session.get(RuntimeOption, _CLOSE_CURSOR)
        cursor = saved.value if saved else {}
        last_period, last_customer = cursor.get("period"), cursor.get("customer_id")
        position = (last_period, last_customer) if isinstance(last_period, str) and isinstance(last_customer, str) else None
        key = tuple_(due_periods.c.period, Customer.id)
        selected = list(session.execute(query.where(key > position).limit(limit)).all()) if position else list(session.execute(query.limit(limit)).all())
        if position and len(selected) < limit:
            selected.extend(session.execute(query.where(key <= position).limit(limit - len(selected))).all())
        result = [(period, customer_id) for period, customer_id in selected]
        if result:
            period, customer_id = result[-1]
            _put_option(session, _CLOSE_CURSOR, {"period": period, "customer_id": customer_id})
        return result


def prepare_due_invoices(db, as_of: date | None = None, max_customers: int | None = None) -> dict:
    """Attempt at most 20 customer-period closes, rotating blocked scopes fairly.

    Only fully ended calendar months are due. ``as_of`` is a London local date
    override for controlled simulations/tests, not an invoice/tax-date override.
    Successful calls prepare BUILDING/DRAFT documents; they never approve them.
    """
    if max_customers is not None:_limit(max_customers, "max_customers", 20)
    with db.transaction() as session:control = get_effective_automation(session, db.settings)
    limit = min(max_customers or 20, control["monthly_customers_per_cycle"])
    if as_of is None:
        as_of = _local_date()
    if type(as_of) is not date:
        raise ValueError("as_of must be a date interpreted in Europe/London")
    report = {"mode": "automatic_monthly_preparation", "as_of": as_of.isoformat(),
              "timezone": "Europe/London", "enabled": control["monthly_preparation_enabled"],
              "attempted": 0, "prepared": 0, "blocked": 0, "errors": 0, "results": []}
    if not report["enabled"]:
        return report
    for period, customer_id in _close_candidates(db, as_of.strftime("%Y-%m"), limit):
        with db.transaction() as session:
            if not get_effective_automation(session, db.settings)["monthly_preparation_enabled"]:break
        item = {"customer_id": customer_id, "period": period}
        report["attempted"] += 1
        try:
            with db.transaction() as session:
                previous_revision = session.scalar(select(func.max(Invoice.revision)).where(
                    Invoice.customer_id == customer_id, Invoice.period == period)) or 0
            request_id = "automatic:close:" + digest([customer_id, period, previous_revision + 1])
            result = create_invoice(db, customer_id, period, request_id, ACTOR)
            # A concurrent source change can stale a just-created invoice before
            # another scheduler receives the command's cached creation response.
            # Report that outcome accurately; the next rotation uses revision+1.
            with db.transaction() as session:
                prepared = session.get(Invoice, result["id"])
                if prepared is not None and prepared.status in _UNUSABLE_INVOICE_STATES:
                    reason = ("Prepared invoice failed; automatic preparation is held for investigation"
                              if prepared.status == "FAILED" else
                              "Prepared invoice became stale; a later cycle will re-evaluate the scope")
                    raise Blocked(reason)
            item.update(status="PREPARED", invoice=result)
            report["prepared"] += 1
        except (Blocked, Conflict, NotFound) as exc:
            item.update(status="BLOCKED", reason=str(exc)[:1000])
            report["blocked"] += 1
        except Exception as exc:
            # Retain one failing scope's cause without starving later customers.
            item.update(status="ERROR", reason=f"{type(exc).__name__}: {exc}"[:1000])
            report["errors"] += 1
        report["results"].append(item)
    _save_report(db, _CLOSE_REPORT, report)
    return report


def _approved_candidates(db, limit: int) -> list[tuple[str, str]]:
    query = select(Invoice.id, Invoice.fingerprint).where(
        Invoice.status == "APPROVED", Invoice.issued_at.is_(None),
        Invoice.approved_at.is_not(None), Invoice.fingerprint.is_not(None),
    ).order_by(Invoice.id)
    with db.transaction() as session:
        lock(session, _DISPATCH_CURSOR)
        saved = session.get(RuntimeOption, _DISPATCH_CURSOR)
        cursor = saved.value.get("invoice_id") if saved else None
        if not isinstance(cursor, str):
            cursor = None
        selected = list(session.execute(query.where(Invoice.id > cursor).limit(limit)).all()) if cursor else list(session.execute(query.limit(limit)).all())
        if cursor and len(selected) < limit:
            selected.extend(session.execute(query.where(Invoice.id <= cursor).limit(limit - len(selected))).all())
        result = [(invoice_id, fingerprint) for invoice_id, fingerprint in selected]
        if result:
            _put_option(session, _DISPATCH_CURSOR, {"invoice_id": result[-1][0]})
        return result


def dispatch_approved(db, limit: int | None = None) -> dict:
    """Issue approved packages and enqueue transport; never approve or resend.

    ``dispatch_invoice`` rechecks the current approval, package hash, coverage and
    stable allocations under its own locks. Issued invoices and uncertain email
    attempts are not candidates. The transport worker alone submits the outbox.
    """
    if limit is not None:_limit(limit, "limit", 100)
    with db.transaction() as session:control = get_effective_automation(session, db.settings)
    limit = min(limit or 100, control["dispatch_per_cycle"])
    report = {"mode": "automatic_after_human_approval",
              "enabled": control["dispatch_after_approval_enabled"],
              "attempted": 0, "issued": 0, "blocked": 0, "errors": 0, "results": []}
    if not report["enabled"]:
        return report
    from .delivery import mail_readiness
    readiness = mail_readiness(db.settings)
    if not readiness["ready"]:
        # Issue nothing while the provider cannot send: approved invoices stay
        # APPROVED (unissued) and the reason is visible in the automation panel.
        report.update(blocked_reason=readiness["reason"])
        _save_report(db, _DISPATCH_REPORT, report)
        return report
    for invoice_id, fingerprint in _approved_candidates(db, limit):
        with db.transaction() as session:
            if not get_effective_automation(session, db.settings)["dispatch_after_approval_enabled"]:break
        report["attempted"] += 1
        item = {"invoice_id": invoice_id, "fingerprint": fingerprint}
        try:
            # Ordinary UUID identifiers fit the explicit id:fingerprint format.
            # Preserve binding for longer imported IDs within the 120-char limit.
            request_id = f"automatic:{invoice_id}:{fingerprint}"
            if len(request_id) > 120:
                request_id = "automatic:" + digest([invoice_id, fingerprint])
            result = dispatch_invoice(db, invoice_id, request_id, ACTOR)
            item.update(status="ISSUED", issuance=result)
            report["issued"] += 1
        except (Blocked, Conflict, NotFound) as exc:
            item.update(status="BLOCKED", reason=str(exc)[:1000])
            report["blocked"] += 1
        except Exception as exc:
            item.update(status="ERROR", reason=f"{type(exc).__name__}: {exc}"[:1000])
            report["errors"] += 1
        report["results"].append(item)
    _save_report(db, _DISPATCH_REPORT, report)
    return report


def _next_month(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


def notify_pending_approvals(db, as_of: date | None = None) -> dict:
    """Queue at most one reminder per closed period per London day.

    The reminder lists DRAFT invoices that wait for a human decision. It never
    approves, issues or changes an invoice. In test mode the outbox redirects
    it to the connected Gmail account like every other real message.
    """
    as_of = as_of or _local_date()
    if type(as_of) is not date:
        raise ValueError("as_of must be a date interpreted in Europe/London")
    settings = db.settings
    report = {"mode": "approval_reminder", "as_of": as_of.isoformat(), "enabled": bool(settings.approval_digest),
              "attempted": 0, "queued": [], "pending": 0}
    if not settings.approval_digest:
        return report
    before = as_of.strftime("%Y-%m")
    with db.transaction() as session:
        drafts = list(session.execute(select(Invoice.period, Invoice.number, Invoice.customer_id, Invoice.gross_total,
                                             Invoice.currency)
                      .where(Invoice.status == "DRAFT", Invoice.issued_at.is_(None), Invoice.period < before)
                      .order_by(Invoice.period, Invoice.number)).all())
        report["pending"] = len(drafts)
        by_period: dict[str, list] = {}
        for row in drafts:
            by_period.setdefault(row.period, []).append(row)
        for period, rows in by_period.items():
            shown = rows[:50]
            lines = [f"- {row.number} | {row.customer_id} | {row.currency} {row.gross_total}" for row in shown]
            if len(rows) > len(shown):
                lines.append(f"... and {len(rows) - len(shown)} more")
            count = len(rows)
            body = (f"{count} invoice{'s' if count != 1 else ''} for {period} "
                    f"{'are' if count != 1 else 'is'} prepared and wait for your approval.\n\n"
                    + "\n".join(lines)
                    + f"\n\nReview and approve: {settings.public_base_url}/#invoices\n"
                    "Nothing is sent to customers until a person approves each invoice.\n")
            payload = {"from": "operations@example.test", "to": [settings.operator_email],
                       "subject": f"{count} invoice{'s' if count != 1 else ''} await approval ({period})", "body": body}
            key = f"approval-digest:{period}:{as_of.isoformat()}"
            inserted = session.scalar(pg_insert(Outbox).values(dedup_key=key, kind="DIGEST", payload=payload)
                                      .on_conflict_do_nothing(index_elements=["dedup_key"]).returning(Outbox.id))
            if inserted:
                report["attempted"] += 1
                report["queued"].append({"period": period, "invoices": count, "outbox_id": inserted})
                audit(session, DIGEST_ACTOR, "approval_digest_queued", inserted,
                      {"period": period, "invoices": [row.number for row in shown], "count": count,
                       "approves_anything": False})
    if report["attempted"]:
        _save_report(db, _DIGEST_REPORT, report)
    return report


def automation_status(db) -> dict:
    """Read-only summary for the console: schedule, reports, mail and refresh."""
    from .delivery import mail_readiness
    from .operations import get_effective_automation
    settings = db.settings
    today = _local_date()
    keys = (_CLOSE_REPORT, _DISPATCH_REPORT, _DIGEST_REPORT, "inbox.report", "demo_feed.state")
    with db.transaction() as session:
        control = get_effective_automation(session, settings)
        options = {row.key: row.value for row in session.scalars(select(RuntimeOption).where(RuntimeOption.key.in_(keys)))}
        counts = dict(session.execute(select(Invoice.status, func.count()).where(Invoice.issued_at.is_(None),
                      Invoice.status.in_(("BUILDING", "DRAFT", "APPROVED"))).group_by(Invoice.status)).all())
    next_close = _next_month(today)
    return {
        "timezone": "Europe/London", "today": today.isoformat(),
        "monthly_close": {
            "enabled": control["monthly_preparation_enabled"],
            "next_run_date": next_close.isoformat(), "next_period": today.strftime("%Y-%m"),
            "rule": "From 00:00 Europe/London on the 1st, every ended month is prepared as DRAFT invoices; a person approves each one.",
            "check_interval_seconds": 60, "last_report": options.get(_CLOSE_REPORT),
        },
        "dispatch": {
            "enabled": control["dispatch_after_approval_enabled"],
            "rule": "Only APPROVED invoices are issued and emailed, within seconds of approval.",
            "last_report": options.get(_DISPATCH_REPORT),
        },
        "approval_reminder": {"enabled": bool(settings.approval_digest), "last_report": options.get(_DIGEST_REPORT)},
        "invoices": {"building": counts.get("BUILDING", 0), "awaiting_approval": counts.get("DRAFT", 0),
                     "approved_unissued": counts.get("APPROVED", 0)},
        "mail": mail_readiness(settings),
        "data_refresh": {
            "inbox_enabled": bool(settings.inbox_seconds) and control["processing_enabled"],
            "inbox_seconds": settings.inbox_seconds, "inbox_path": "var/inbox (INVOICE_DATA_DIR/inbox)",
            "demo_feed_seconds": settings.demo_feed_seconds, "demo_feed_enabled": bool(settings.demo_feed_seconds),
            "last_inbox_report": options.get("inbox.report"), "demo_feed_state": options.get("demo_feed.state"),
        },
        "paused": control["paused"], "human_approval_required": True,
    }
