"""Gmail delivery with the mandatory test redirect, month-end automation,
automatic inbox refresh and the automation status endpoint.

Google is never contacted: the Gmail connection is replaced by a local fake,
and the real ``send_raw`` is exercised against a fake urllib opener.
"""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import SMTP
import io
import json
import os
import shutil
import time
from urllib import error

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from invoice_optimizer import delivery, gmail, inbox, scheduler
from invoice_optimizer.api import create_app
from invoice_optimizer.config import Settings
from invoice_optimizer.db import digest
from invoice_optimizer.gmail import GmailConnection, GmailSendError
from invoice_optimizer.ingestion import load_customers
from invoice_optimizer.models import Audit, Batch, Customer, DeliveryAttempt, Invoice, Movement, Outbox, now
from invoice_optimizer.synthetic import generate_dataset

OWNER = "owner@example.test"


class FakeGmail:
    connected = True
    error = None
    sent: list = []

    def __init__(self, settings):
        self.settings = settings

    def status(self):
        return {"connected": FakeGmail.connected, "email": OWNER if FakeGmail.connected else None,
                "reason": "connected" if FakeGmail.connected else "not_connected"}

    def get_access_token(self):
        return "fake-access-token"

    def send_raw(self, raw, token):
        assert token == "fake-access-token"
        if FakeGmail.error is not None:
            raise FakeGmail.error
        FakeGmail.sent.append(raw)
        return "gmail-message-1"


@pytest.fixture
def gmail_db(db, monkeypatch):
    FakeGmail.connected, FakeGmail.error, FakeGmail.sent = True, None, []
    monkeypatch.setattr(gmail, "GmailConnection", FakeGmail)
    monkeypatch.setattr(db, "settings", replace(db.settings, provider="gmail", gmail_expected_email=OWNER))
    return db


def _queue(db, kind="DIGEST", to=("first@example.test", "second@example.test"), key=None):
    payload = {"from": "operations@example.test", "to": list(to), "subject": "Monthly summary", "body": "Synthetic body"}
    with db.transaction() as session:
        row = Outbox(dedup_key=key or f"{kind.lower()}:{len(to)}:{time.monotonic_ns()}", kind=kind, payload=payload)
        session.add(row)
        session.flush()
        return row.id, payload


def _row(db, outbox_id):
    with db.transaction() as session:
        return session.get(Outbox, outbox_id)


# ---------------------------------------------------------------- redirect

def test_redirect_helper_leaves_only_the_target_and_keeps_originals():
    msg = EmailMessage(policy=SMTP)
    msg["From"] = "operations@example.test"
    msg["To"] = "a@example.test, b@example.test"
    msg["Cc"] = "c@example.test"
    msg["Bcc"] = "hidden@example.test"
    msg["Reply-To"] = "reply@example.test"
    msg["Subject"] = "Invoice"
    msg.set_content("body")
    original = delivery._apply_test_redirect(msg, OWNER, OWNER)
    assert original == ["a@example.test, b@example.test", "c@example.test", "hidden@example.test"]
    assert msg.get_all("To") == [OWNER]
    for header in ("Cc", "Bcc", "Reply-To"):
        assert msg[header] is None
    assert "hidden@example.test" in msg["X-Original-To"] and "c@example.test" in msg["X-Original-To"]
    assert msg["Subject"].startswith("[TEST → a@example.test, b@example.test")
    assert msg["From"] == OWNER and msg["X-Original-From"] == "operations@example.test"
    parsed = message_from_bytes(msg.as_bytes())
    assert parsed.get_all("To") == [OWNER] and parsed["Bcc"] is None


def test_gmail_send_redirects_every_recipient_and_audits_without_plain_target(gmail_db):
    outbox_id, payload = _queue(gmail_db)
    assert delivery.send_one(gmail_db) is True
    assert len(FakeGmail.sent) == 1
    sent = message_from_bytes(FakeGmail.sent[0])
    assert sent.get_all("To") == [OWNER]
    assert "first@example.test" in sent["X-Original-To"] and "second@example.test" in sent["X-Original-To"]
    assert sent["Subject"].startswith("[TEST")
    row = _row(gmail_db, outbox_id)
    assert row.status == "ACCEPTED"
    # The stored payload (and therefore any invoice fingerprint) is untouched.
    assert row.payload == payload and digest(row.payload) == digest(payload)
    with gmail_db.transaction() as session:
        attempt = session.scalar(select(DeliveryAttempt).where(DeliveryAttempt.outbox_id == outbox_id))
        event = session.scalar(select(Audit).where(Audit.entity_id == outbox_id, Audit.action == "send_accepted"))
    assert attempt.provider_id == "gmail:gmail-message-1"
    assert event.details["redirected"] is True and event.details["provider"] == "gmail"
    assert event.details["original_recipients"] == ["first@example.test, second@example.test"]
    assert event.details["redirect_target_sha256"] and OWNER not in json.dumps(event.details)


def test_not_connected_keeps_gmail_rows_ready_while_alerts_stay_simulated(gmail_db):
    FakeGmail.connected = False
    digest_id, _ = _queue(gmail_db, "DIGEST")
    assert delivery.send_one(gmail_db) is False
    assert _row(gmail_db, digest_id).status == "READY"
    alert_id, _ = _queue(gmail_db, "ALERT", to=("review@example.test",))
    assert delivery.send_one(gmail_db) is True
    assert _row(gmail_db, alert_id).status == "ACCEPTED"
    assert _row(gmail_db, digest_id).status == "READY" and not FakeGmail.sent
    readiness = delivery.mail_readiness(gmail_db.settings)
    assert not readiness["ready"] and "Connect Gmail first" in readiness["reason"]
    FakeGmail.connected = True
    assert delivery.send_one(gmail_db) is True
    assert _row(gmail_db, digest_id).status == "ACCEPTED" and len(FakeGmail.sent) == 1


def test_redirect_cannot_be_switched_off_in_synthetic_mode(gmail_db, tmp_path):
    with pytest.raises(ValueError, match="test redirect"):
        Settings(data_dir=tmp_path, provider="gmail", mail_redirect="off").prepare()
    with pytest.raises(ValueError, match="test redirect"):
        Settings(data_dir=tmp_path, provider="smtp", smtp_host="smtp.example.test", mail_redirect="off").prepare()
    with pytest.raises(ValueError, match="INVOICE_MAIL_REDIRECT"):
        Settings(data_dir=tmp_path, provider="gmail", mail_redirect="stranger@elsewhere.test").prepare()
    # A hand-built Settings that skipped prepare() still cannot send unredirected.
    gmail_db.settings = replace(gmail_db.settings, mail_redirect="off")
    assert delivery.redirect_target(gmail_db.settings)[0] is None
    outbox_id, _ = _queue(gmail_db)
    assert delivery.send_one(gmail_db) is False
    assert _row(gmail_db, outbox_id).status == "READY" and not FakeGmail.sent


def test_unavailable_token_reserves_nothing_and_does_not_spin(gmail_db, monkeypatch):
    calls = []

    def no_token(self):
        calls.append(1)
        raise gmail.GmailError("network_error")

    monkeypatch.setattr(FakeGmail, "get_access_token", no_token)
    outbox_id, _ = _queue(gmail_db)
    alert_id, _ = _queue(gmail_db, "ALERT", to=("review@example.test",))
    # The Gmail row is not reserved; the simulated alert still flows.
    assert delivery.send_one(gmail_db) is True
    assert delivery.send_one(gmail_db) is False
    assert _row(gmail_db, outbox_id).status == "READY" and _row(gmail_db, outbox_id).attempts == 0
    assert _row(gmail_db, alert_id).status == "ACCEPTED" and not FakeGmail.sent
    with gmail_db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(DeliveryAttempt).where(DeliveryAttempt.outbox_id == outbox_id)) == 0
    assert len(calls) == 2


@pytest.mark.parametrize("definite,status", [(False, "UNKNOWN"), (True, "REJECTED")])
def test_gmail_errors_map_to_unknown_or_rejected(gmail_db, definite, status):
    FakeGmail.error = GmailSendError(definite, "synthetic failure")
    outbox_id, _ = _queue(gmail_db)
    assert delivery.send_one(gmail_db) is True
    assert _row(gmail_db, outbox_id).status == status
    with gmail_db.transaction() as session:
        attempt = session.scalar(select(DeliveryAttempt).where(DeliveryAttempt.outbox_id == outbox_id))
    assert attempt.status == status and "synthetic failure" in attempt.error


def test_unknown_gmail_attempt_is_never_reconciled_automatically(gmail_db):
    FakeGmail.error = GmailSendError(False, "timeout")
    outbox_id, _ = _queue(gmail_db)
    delivery.send_one(gmail_db)
    with gmail_db.transaction() as session:
        attempt_id = session.scalar(select(DeliveryAttempt.id).where(DeliveryAttempt.outbox_id == outbox_id))
    with pytest.raises(delivery.Blocked, match="Sent folder"):
        delivery.reconcile_attempt(gmail_db, attempt_id, "reviewer")


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _connection(tmp_path, monkeypatch, behaviour):
    settings = Settings(data_dir=tmp_path)
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append(req)
            return behaviour(req)

    monkeypatch.setattr(gmail.request, "build_opener", lambda *handlers: Opener())
    return GmailConnection(settings), calls


def test_real_send_raw_posts_base64url_json_and_classifies_errors(tmp_path, monkeypatch):
    connection, calls = _connection(tmp_path, monkeypatch, lambda req: _Response(b'{"id":"abc123"}'))
    assert connection.send_raw(b"Subject: x\r\n\r\nbody", "token") == "abc123"
    assert calls[0].full_url == gmail.SEND_URL and calls[0].get_header("Authorization") == "Bearer token"
    assert "raw" in json.loads(calls[0].data)

    def timeout(req):
        raise TimeoutError()
    connection, _ = _connection(tmp_path, monkeypatch, timeout)
    with pytest.raises(GmailSendError) as unknown:
        connection.send_raw(b"x", "token")
    assert unknown.value.definite is False
    for code, definite in ((400, True), (403, True), (503, False), (500, False)):
        def http(req, code=code):
            raise error.HTTPError(gmail.SEND_URL, code, "error", {}, io.BytesIO(b"{}"))
        connection, _ = _connection(tmp_path, monkeypatch, http)
        with pytest.raises(GmailSendError) as failure:
            connection.send_raw(b"x", "token")
        assert failure.value.definite is definite
    with pytest.raises(GmailSendError) as large:
        connection.send_raw(b"x" * (gmail.MAX_RAW_BYTES + 1), "token")
    assert large.value.definite is True


# ------------------------------------------------------- month-end automation

def _customers(db, count=2):
    ids = [f"AUTO-{index:03d}" for index in range(1, count + 1)]
    with db.transaction() as session:
        session.add_all(Customer(id=customer_id, legal_name="Synthetic Automation Customer", address="Synthetic address",
                                 email="accounts@example.test", alert_email="review@example.test", vat_number="DEMO")
                        for customer_id in ids)
    return ids


def _period_scope(db, period):
    key = digest(["automation-batch", period])
    with db.transaction() as session:
        session.add(Batch(id=key, source="A", period=period, object_key=key, sha256=key, original_name="synthetic.csv",
                          status="COMPLETE", expected_rows=0, row_count=0, declared_complete=True))
        for customer_id in session.scalars(select(Customer.id)):
            session.add(Movement(id=digest(["automation-movement", customer_id, period]), external_id="m-" + period,
                                 customer_id=customer_id, period=period, state="PENDING"))


def _invoice(session, customer_id, invoice_id, status, period):
    session.add(Invoice(id=invoice_id, number="DEMO-" + invoice_id, customer_id=customer_id, period=period, revision=1,
                        status=status, currency="GBP", net_total=Decimal("10.00"), vat_total=Decimal("2.00"),
                        gross_total=Decimal("12.00"), snapshot_hash="a" * 64, snapshot_key="a" * 64, document={},
                        fingerprint=digest([invoice_id, "package"]),
                        approved_at=now() if status == "APPROVED" else None,
                        approved_by="human-reviewer" if status == "APPROVED" else None))


def test_month_change_prepares_only_the_ended_month(db, monkeypatch):
    customers = _customers(db, 1)
    _period_scope(db, "2026-10")
    _period_scope(db, "2026-11")
    calls = []
    monkeypatch.setattr(scheduler, "create_invoice",
                        lambda db, customer_id, period, request_id, actor: calls.append((customer_id, period, actor)) or {"id": "none"})
    assert scheduler.prepare_due_invoices(db, date(2026, 10, 31))["attempted"] == 0
    report = scheduler.prepare_due_invoices(db, date(2026, 11, 1))
    assert calls == [(customers[0], "2026-10", scheduler.ACTOR)] and report["attempted"] == 1


def test_dispatch_issues_only_approved_and_blocks_until_gmail_connected(gmail_db, monkeypatch):
    customers = _customers(gmail_db, 2)
    with gmail_db.transaction() as session:
        _invoice(session, customers[0], "draft-1", "DRAFT", "2026-10")
        _invoice(session, customers[1], "approved-1", "APPROVED", "2026-10")
    issued = []
    monkeypatch.setattr(scheduler, "dispatch_invoice",
                        lambda db, invoice_id, request_id, actor: issued.append(invoice_id) or {"id": invoice_id})
    FakeGmail.connected = False
    blocked = scheduler.dispatch_approved(gmail_db)
    assert issued == [] and "Connect Gmail first" in blocked["blocked_reason"]
    FakeGmail.connected = True
    report = scheduler.dispatch_approved(gmail_db)
    assert issued == ["approved-1"] and report["issued"] == 1
    with gmail_db.transaction() as session:
        assert session.get(Invoice, "draft-1").status == "DRAFT"


def test_approval_reminder_is_daily_lists_drafts_and_approves_nothing(db):
    customers = _customers(db, 2)
    with db.transaction() as session:
        _invoice(session, customers[0], "draft-a", "DRAFT", "2026-10")
        _invoice(session, customers[1], "approved-b", "APPROVED", "2026-10")
        _invoice(session, customers[1], "draft-current", "DRAFT", "2026-11")
    first = scheduler.notify_pending_approvals(db, date(2026, 11, 1))
    again = scheduler.notify_pending_approvals(db, date(2026, 11, 1))
    nextday = scheduler.notify_pending_approvals(db, date(2026, 11, 2))
    assert first["attempted"] == 1 and again["attempted"] == 0 and nextday["attempted"] == 1
    with db.transaction() as session:
        rows = list(session.scalars(select(Outbox).where(Outbox.kind == "DIGEST").order_by(Outbox.created_at)))
        assert len(rows) == 2
        assert rows[0].payload["to"] == [db.settings.operator_email]
        assert "DEMO-draft-a" in rows[0].payload["body"] and "GBP 12.00" in rows[0].payload["body"]
        assert "approved-b" not in rows[0].payload["body"] and "draft-current" not in rows[0].payload["body"]
        assert rows[0].payload["subject"].startswith("1 invoice await")
        assert session.get(Invoice, "draft-a").status == "DRAFT"
        assert session.get(Invoice, "approved-b").issued_at is None
        assert session.scalar(select(func.count()).select_from(Audit).where(Audit.action == "approval_digest_queued")) == 2


# ----------------------------------------------------------- inbox refresh

@pytest.fixture
def dataset(db, tmp_path):
    directory = tmp_path / "dataset"
    manifest = generate_dataset(directory, customers=2, movements=20, seed=7, period="2026-11")
    load_customers(db, directory / "customers.json")
    return directory, manifest


def _deliver(db, source_file, name, sidecar):
    target = inbox.inbox_dir(db.settings)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_file, target / name)
    (target / (name + ".json")).write_text(json.dumps(sidecar))
    return target


def test_inbox_ingests_settled_files_once_and_replays_identical_content(db, dataset):
    directory, manifest = dataset
    sidecar = {"source": "A", "period": "2026-11", "expected_rows": manifest["source_rows"]["A"], "complete": True}
    target = _deliver(db, directory / "source_a.csv", "a-first.csv", sidecar)
    assert inbox.scan_inbox(db, at=time.time())["ingested"] == 0  # Not settled yet.
    first = inbox.scan_inbox(db, at=time.time() + 10)
    assert first["ingested"] == 1 and first["results"][0]["status"] == "INGESTED"
    assert not (target / "a-first.csv").exists() and len(list((target / "processed").glob("*.result.json"))) == 1
    _deliver(db, directory / "source_a.csv", "a-again.csv", sidecar)
    second = inbox.scan_inbox(db, at=time.time() + 10)
    assert second["replayed"] == 1 and second["ingested"] == 0
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Batch)) == 1
        events = list(session.scalars(select(Audit.actor).where(Audit.action == "batch_complete")))
    assert events == [inbox.ACTOR]


def test_inbox_rejects_bad_manifests_and_bounds_each_cycle(db, dataset):
    directory, manifest = dataset
    target = _deliver(db, directory / "source_a.csv", "a-bad.csv", {"source": "Z", "period": "2026-11"})
    for index in range(3):
        _deliver(db, directory / "source_b.csv", f"b-{index}.csv",
                 {"source": "B", "period": "2026-11", "expected_rows": manifest["source_rows"]["B"], "complete": True})
    report = inbox.scan_inbox(db, at=time.time() + 10, max_files=2)
    assert report["rejected"] == 1 and len(report["results"]) == 2
    assert list((target / "rejected").glob("*a-bad.csv"))
    rest = inbox.scan_inbox(db, at=time.time() + 10, max_files=5)
    assert rest["ingested"] + rest["replayed"] == 2
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Audit).where(Audit.action == "inbox_file_rejected")) == 1


def test_demo_feed_appends_cumulative_snapshots_and_finalises_the_month(db, monkeypatch):
    monkeypatch.setattr(db, "settings", replace(db.settings, demo_feed_rows=40, demo_feed_seconds=60))
    middle = datetime(2026, 11, 16, tzinfo=timezone.utc)
    first = inbox.feed_tick(db, middle)
    assert len(first["written"]) == 1
    counts = first["written"][0]["counts"]
    assert 0 < counts["A"]
    assert inbox.feed_tick(db, middle)["written"] == []
    later = inbox.feed_tick(db, datetime(2026, 11, 28, tzinfo=timezone.utc))
    assert later["written"][0]["counts"]["A"] > counts["A"] and later["written"][0]["sequence"] == 2
    final = inbox.feed_tick(db, datetime(2026, 12, 1, 0, 5, tzinfo=timezone.utc))
    november = [item for item in final["written"] if item["period"] == "2026-11"]
    assert november and november[0]["final"] is True
    files = sorted(path.name for path in inbox.inbox_dir(db.settings).glob("feed-2026-11-*.csv"))
    assert files[:2] == ["feed-2026-11-000001-A.csv", "feed-2026-11-000001-B.csv"]
    # Only regression-scorable movements: no missing features or quarantine rows.
    labels = {}
    feed_dir = db.settings.data_dir / "synthetic" / "feed-2026-11-40"
    import csv
    with (feed_dir / "scenario_labels.csv").open() as handle:
        for row in csv.DictReader(handle):
            labels[row["movement_id"]] = row["scenario"]
    with (inbox.inbox_dir(db.settings) / "feed-2026-11-000003-A.csv").open() as handle:
        scenarios = {labels[row["movement_id"]] for row in csv.DictReader(handle)}
    assert not scenarios & inbox.FEED_EXCLUDED_SCENARIOS
    report = inbox.scan_inbox(db, at=time.time() + 10, max_files=50)
    assert report["rejected"] == 0 and report["ingested"] >= 6


# -------------------------------------------------------- status endpoints

def test_automation_status_endpoint_reports_schedule_mail_and_refresh(db):
    with TestClient(create_app(db.settings, db), headers={"Authorization": "Bearer " + db.settings.api_token}) as client:
        response = client.get("/api/automation/status")
    assert response.status_code == 200
    body = response.json()
    today = date.fromisoformat(body["today"])
    expected = (today.replace(day=1) + __import__("datetime").timedelta(days=32)).replace(day=1)
    assert body["monthly_close"]["next_run_date"] == expected.isoformat()
    assert body["human_approval_required"] is True and body["mail"]["provider"] == "simulated"
    assert body["mail"]["ready"] is True and "data_refresh" in body and "worker" in body
    assert "/Users/" not in response.text


def test_mail_test_route_queues_one_idempotent_test_message(db):
    settings = replace(db.settings, gmail_expected_email=OWNER)
    with TestClient(create_app(settings, db), base_url=settings.public_base_url) as client:
        login = client.post("/api/operators/bootstrap", json={"setup_token": settings.api_token, "username": "mail-admin",
                            "display_name": "Mail Test Administrator", "password": "mail-test-password-2026"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        assert client.post("/api/mail/gmail/test", json={"request_id": "test-request-1"}).status_code == 403
        first = client.post("/api/mail/gmail/test", headers=headers, json={"request_id": "test-request-1"})
        second = client.post("/api/mail/gmail/test", headers=headers, json={"request_id": "test-request-1"})
        status = client.get("/api/mail/gmail").json()
    assert first.status_code == 200 and not first.json()["replayed"] and second.json()["replayed"]
    assert first.json()["outbox_id"] == second.json()["outbox_id"]
    assert status["delivery_provider"] == "simulated" and status["test_redirect"]["active"] is False
    with db.transaction() as session:
        rows = list(session.scalars(select(Outbox).where(Outbox.kind == "TEST")))
    assert len(rows) == 1 and rows[0].payload["to"] == [settings.operator_email]


def test_refresh_cycle_respects_pause_and_runs_feed_then_inbox(db, monkeypatch):
    from invoice_optimizer.models import RuntimeOption
    from invoice_optimizer.operations import AUTOMATION_KEY
    calls = []
    monkeypatch.setattr(db, "settings", replace(db.settings, demo_feed_seconds=60))
    monkeypatch.setattr(inbox, "feed_tick", lambda db: calls.append("feed") or {})
    monkeypatch.setattr(inbox, "scan_inbox", lambda db: calls.append("inbox") or {})
    _, last = inbox.run_refresh_cycle(db, None, clock=lambda: 100.0)
    assert calls == ["feed", "inbox"] and last == 100.0
    inbox.run_refresh_cycle(db, last, clock=lambda: 130.0)
    assert calls == ["feed", "inbox", "inbox"]  # Feed interval not yet elapsed.
    with db.transaction() as session:
        session.add(RuntimeOption(key=AUTOMATION_KEY, value={"revision": 1, "config": {"paused": True}}))
    result, _ = inbox.run_refresh_cycle(db, last, clock=lambda: 500.0)
    assert result == {"paused": True} and len(calls) == 3


def test_manual_dispatch_says_connect_gmail_first(gmail_db):
    FakeGmail.connected = False
    settings = gmail_db.settings
    with TestClient(create_app(settings, gmail_db), base_url=settings.public_base_url) as client:
        login = client.post("/api/operators/bootstrap", json={"setup_token": settings.api_token, "username": "mail-admin",
                            "display_name": "Mail Test Administrator", "password": "mail-test-password-2026"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        response = client.post("/api/invoices/any-invoice/dispatch", headers=headers, json={"request_id": "dispatch-req-1"})
        status = client.get("/api/automation/status").json()
    assert response.status_code == 409 and "Connect Gmail first" in response.text
    assert status["mail"]["ready"] is False and status["mail"]["provider"] == "gmail"
