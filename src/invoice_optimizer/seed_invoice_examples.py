"""Additional synthetic inputs; invoices are created by the ordinary scheduler.

This module never creates, approves, issues or renders invoices. It adds two
complete source deliveries in an otherwise unused period, then can collect the
exact PDFs that the normal reconciliation/model/billing flow produced.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, func

from .db import Blocked, Conflict, audit, canonical, digest, lock
from .ingestion import ingest_file, load_customers, normalize
from .ml import model_readiness, score_batch
from .models import (Assessment, Audit, Batch, BatchRecord, Customer, Invoice, InvoiceMember,
                     InvoiceSourceFile, InvoiceSourceManifest, Movement, MovementVersion,
                     Outbox, Quarantine, ReviewCase, RuntimeOption, SourceRecord)
from .storage import ObjectStore

FIXTURE = "invoice-examples-v1"
PERIOD = "2026-08"
ACTOR = "synthetic-example-loader"
CUSTOMER_SPECS = (
    ("EXAMPLE-C001", "Aster Office Supplies Ltd (Synthetic)", "12 Example Lane\nLondon\nZZ1 1ZZ\nUnited Kingdom", 12, "8.50"),
    ("EXAMPLE-C002", "Cedar Components Trading Ltd (Synthetic)", "24 Example Road\nManchester\nZZ2 2ZZ\nUnited Kingdom", 24, "17.25"),
    ("EXAMPLE-C003", "Harbour Procurement Services Ltd (Synthetic)", "36 Example Quay\nBristol\nZZ3 3ZZ\nUnited Kingdom", 36, "29.75"),
)


def fixture_data() -> dict:
    """Return the same bounded, human-readable input bytes on every invocation."""
    customers = []
    rows = []
    scopes = []
    for ordinal, (customer_id, name, address, count, base) in enumerate(CUSTOMER_SPECS, 1):
        customers.append(dict(id=customer_id, legal_name=name, address=address,
                              email=f"accounts-{ordinal}@example.test",
                              alert_email=f"review-{ordinal}@example.test", vat_number="DEMO ONLY"))
        subtotal = Decimal("0.00")
        for index in range(count):
            amount = Decimal(base) + Decimal(index % 7) * Decimal("0.25")
            subtotal += amount
            rows.append(dict(record_id=f"{FIXTURE}-{customer_id}-{index+1:03d}", revision=1,
                movement_id=f"{customer_id}-AUG-{index+1:03d}", customer_id=customer_id,
                event_at=f"2026-08-{20+index%6:02d}T12:{index%60:02d}:00+00:00", period=PERIOD,
                currency="GBP", amount=str(amount),
                description=f"Synthetic procurement processing service {index+1:03d} for {name}",
                supplier_id=f"EXAMPLE-S{ordinal:03d}", expected_amount=str(amount),
                customer_age_days="3650", supplier_age_days="2555", cross_border="0"))
        scopes.append(dict(customer_id=customer_id, movement_count=count, net_total=str(subtotal),
                           vat_total=str((subtotal * Decimal("0.20")).quantize(Decimal("0.01"))),
                           gross_total=str((subtotal * Decimal("1.20")).quantize(Decimal("0.01")))))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    source_bytes = stream.getvalue().encode()
    customer_bytes = (json.dumps(customers, indent=2) + "\n").encode()
    files = {"source_a.csv": source_bytes, "source_b.csv": source_bytes, "customers.json": customer_bytes}
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    manifest = dict(fixture=FIXTURE, synthetic=True, period=PERIOD, source_contract="demo-contract-v1",
                    source_rows={"A": len(rows), "B": len(rows)}, logical_movements=len(rows),
                    customers=scopes, file_sha256=hashes,
                    billing_semantics="Amounts are synthetic processing service fees, not purchase principal.",
                    invoice_creation="Existing monthly scheduler and ASSESS/SCORE/RENDER worker only",
                    automatic_approval=False, invoice_email_requested=False)
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    return dict(customers=customers, rows=rows, files=files, manifest=manifest,
                manifest_sha256=hashlib.sha256(files["manifest.json"]).hexdigest())


def _check_files(directory: Path, fixture: dict) -> None:
    for name, expected in fixture["files"].items():
        path = directory / name
        if path.exists() and (not path.is_file() or path.read_bytes() != expected):
            raise Conflict(f"Synthetic fixture file changed: {path.name}; it will not be overwritten")


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if not path.is_file() or path.read_bytes() != content:
            raise Conflict(f"Existing output has different bytes: {path.name}")


def _expected_records(fixture: dict) -> dict:
    return {digest([side, row["record_id"], 1]):
            dict(source=side, payload=normalize(row, PERIOD),
                 movement_id=digest([row["customer_id"], row["movement_id"]]))
            for side in ("A", "B") for row in fixture["rows"]}


def _preflight_database(session, fixture: dict, *, require_stopped: bool) -> None:
    if require_stopped:
        heartbeat = session.get(RuntimeOption, "worker.heartbeat")
        if heartbeat and heartbeat.value.get("status") == "RUNNING":
            try:
                recorded = datetime.fromisoformat(heartbeat.value["at"])
                age = (datetime.now(timezone.utc) - recorded).total_seconds()
            except (KeyError, TypeError, ValueError):
                age = 0
            if age < 30:
                raise Blocked("Stop the background worker before loading both fixture sources; restart it afterward")
    expected_customers = {item["id"]: item for item in fixture["customers"]}
    for customer in session.scalars(select(Customer).where(Customer.id.in_(expected_customers))):
        if not customer.active or any(getattr(customer, key) != value for key, value in expected_customers[customer.id].items()):
            raise Conflict("An example customer ID already has different master data: " + customer.id)
    expected_records = _expected_records(fixture)
    expected_moves = {item["movement_id"]: item["payload"] for item in expected_records.values()}
    for movement in session.scalars(select(Movement).where(
            (Movement.period == PERIOD) | Movement.customer_id.in_(expected_customers))):
        expected = expected_moves.get(movement.id)
        if (not expected or movement.period != PERIOD or movement.customer_id != expected["customer_id"]
                or movement.external_id != expected["movement_id"]):
            raise Conflict("The example period or reserved customer IDs contain foreign movements")
    expected_batches = {digest([side, PERIOD, fixture["manifest"]["file_sha256"][f"source_{side.lower()}.csv"], None]): side
                        for side in ("A", "B")}
    for batch in session.scalars(select(Batch).where(Batch.period == PERIOD)):
        if (batch.id not in expected_batches or batch.source != expected_batches[batch.id]
                or batch.sha256 != fixture["manifest"]["file_sha256"][f"source_{batch.source.lower()}.csv"]
                or batch.object_key != batch.sha256 or batch.status != "COMPLETE"
                or batch.expected_rows != len(fixture["rows"]) or batch.row_count != len(fixture["rows"])
                or batch.accepted_count != len(fixture["rows"]) or batch.rejected_count
                or not batch.declared_complete or batch.superseded_by):
            raise Conflict("The example period contains a foreign, altered or incomplete source batch")
    for record in session.scalars(select(SourceRecord).where(
            (SourceRecord.period == PERIOD) | SourceRecord.customer_id.in_(expected_customers))):
        expected = expected_records.get(record.id)
        if (not expected or record.payload != expected["payload"] or record.payload_hash != digest(expected["payload"])
                or record.source != expected["source"] or record.movement_id != expected["movement_id"]
                or record.customer_id != expected["payload"]["customer_id"]
                or record.record_id != expected["payload"]["record_id"] or record.revision != 1
                or record.period != PERIOD or record.batch_id not in expected_batches):
            raise Conflict("The example period contains foreign or altered source records")
    if session.scalar(select(Quarantine.id).where(Quarantine.period == PERIOD).limit(1)):
        raise Blocked("The example period already contains quarantined rows")
    if session.scalar(select(ReviewCase.id).where(ReviewCase.period == PERIOD).limit(1)):
        raise Blocked("The example period already contains review cases; investigate instead of hiding them")
    for invoice in session.scalars(select(Invoice).where(
            (Invoice.period == PERIOD) | Invoice.customer_id.in_(expected_customers))):
        scope = next((item for item in fixture["manifest"]["customers"] if item["customer_id"] == invoice.customer_id), None)
        if (invoice.customer_id not in expected_customers or invoice.period != PERIOD
                or invoice.status not in ("BUILDING", "DRAFT", "STALE") or invoice.approved_at or invoice.approved_by
                or invoice.issued_at or invoice.currency != "GBP" or not scope
                or any(getattr(invoice, key) != Decimal(scope[key]) for key in ("net_total", "vat_total", "gross_total"))
                or any(invoice.document.get(key) != scope[key] for key in ("net_total", "vat_total", "gross_total"))
                or not invoice.document.get("synthetic") or invoice.document.get("period") != PERIOD
                or invoice.document.get("customer") != {key: value for key, value in expected_customers[invoice.customer_id].items() if key != "alert_email"}):
            raise Conflict("The example scope contains an unexpected, approved or issued invoice")
        if invoice.status == "STALE":
            # A formatting revision is legitimate retained history. Source-
            # invalidated or arbitrarily stale drafts must not be reinterpreted
            # as successful examples merely because the totals still happen to
            # match. Require the atomic regeneration's old/new audit relation.
            relation = session.scalar(select(Audit).where(
                Audit.entity_id == invoice.id, Audit.action == "invoice_format_superseded").order_by(Audit.id.desc()).limit(1))
            replacement = session.get(Invoice, relation.details.get("new_invoice_id")) if relation else None
            if (not relation or relation.details.get("old_invoice_id") != invoice.id
                    or relation.details.get("snapshot_sha256") != invoice.snapshot_hash
                    or relation.details.get("old_fingerprint") != invoice.fingerprint
                    or not replacement or replacement.customer_id != invoice.customer_id
                    or replacement.period != invoice.period or replacement.revision <= invoice.revision
                    or replacement.snapshot_hash != invoice.snapshot_hash
                    or not invoice.package or digest(invoice.package) != invoice.fingerprint
                    or invoice.package.get("pdf_sha256") != invoice.pdf_sha256
                    or invoice.pdf_key != invoice.pdf_sha256):
                raise Conflict("Stale example invoice is not a verified presentation revision")


def seed_data(db) -> dict:
    """Load only source data. The existing worker and scheduler do everything else."""
    fixture = fixture_data()
    directory = db.settings.data_dir / "synthetic" / FIXTURE
    _check_files(directory, fixture)
    ready = model_readiness(db.settings.model_dir)
    if ready["status"] != "READY":
        raise Blocked("An available, verified active model is required before adding example data")
    scores = score_batch(fixture["rows"], db.settings.model_dir)
    if len(scores) != len(fixture["rows"]) or any(item["status"] != "SCORED" or item["flagged"] for item in scores):
        raise Blocked("Example inputs must all score below the active threshold and follow its information cutoff")
    with db.transaction() as session:
        lock(session, "synthetic-fixture:" + FIXTURE)
        _preflight_database(session, fixture, require_stopped=False)
        # A verified complete replay has no new source data to reconcile and may
        # run while the worker is online. A first or partial load must be quiet.
        existing_batches = session.scalar(select(func.count()).select_from(Batch).where(Batch.period == PERIOD))
        if existing_batches != 2:
            _preflight_database(session, fixture, require_stopped=True)
        existing_ids = set(session.scalars(select(Customer.id).where(Customer.id.in_(
            [row["id"] for row in fixture["customers"]]))))
    for name, content in fixture["files"].items():
        _write_exact(directory / name, content)
    store = ObjectStore(db.settings.data_dir / "objects")
    retained = {name: store.put_file(directory / name) for name in fixture["files"]}
    if len(existing_ids) != len(fixture["customers"]):
        load_customers(db, directory / "customers.json")
    results = [ingest_file(db, directory / f"source_{side.lower()}.csv", side, PERIOD,
                          len(fixture["rows"]), True, actor=ACTOR) for side in ("A", "B")]
    response = dict(fixture=FIXTURE, period=PERIOD, synthetic=True,
                    logical_movements=len(fixture["rows"]), customers=fixture["manifest"]["customers"],
                    manifest_sha256=fixture["manifest_sha256"], retained_files=retained,
                    source_batches=results, model_version=scores[0]["model_version"],
                    maximum_preflight_score=max(item["score"] for item in scores),
                    status="INPUTS_READY_FOR_EXISTING_WORKER_AND_SCHEDULER",
                    invoice_creation_requested=False, invoice_approval_requested=False, email_requested=False)
    if not all(item.get("replayed") for item in results):
        with db.transaction() as session:
            audit(session, ACTOR, "synthetic_example_inputs_loaded", FIXTURE, response)
    return response


def collect_results(db, output_dir: Path) -> dict:
    """Validate and copy the unchanged documents created by the normal workflow."""
    fixture = fixture_data()
    _check_files(db.settings.data_dir / "synthetic" / FIXTURE, fixture)
    store = ObjectStore(db.settings.data_dir / "objects")
    for content in fixture["files"].values():
        store.path(hashlib.sha256(content).hexdigest(), verify=True)
    invoice_results = []
    copies = []
    with db.transaction() as session:
        _preflight_database(session, fixture, require_stopped=False)
        batches = list(session.scalars(select(Batch).where(Batch.period == PERIOD)))
        if len(batches) != 2:
            raise Blocked("Both complete example source deliveries are required")
        for batch in batches:
            store.path(batch.object_key, verify=True)
            count = session.scalar(select(func.count()).select_from(BatchRecord).where(BatchRecord.batch_id == batch.id))
            if count != len(fixture["rows"]):
                raise Conflict("Source batch membership is incomplete")
        for scope in fixture["manifest"]["customers"]:
            invoices = list(session.scalars(select(Invoice).where(
                Invoice.customer_id == scope["customer_id"], Invoice.period == PERIOD,
                Invoice.status != "STALE")))
            if len(invoices) != 1 or invoices[0].status != "DRAFT":
                raise Blocked("Waiting for the normal scheduler and renderer: " + scope["customer_id"])
            invoice = invoices[0]
            for key in ("net_total", "vat_total", "gross_total"):
                if getattr(invoice, key) != Decimal(scope[key]) or invoice.document.get(key) != scope[key]:
                    raise Conflict("Invoice totals do not match fixture source amounts")
            customer = next(item for item in fixture["customers"] if item["id"] == scope["customer_id"])
            if (not invoice.document.get("synthetic") or invoice.document.get("period") != PERIOD
                    or invoice.document.get("customer") != {key: value for key, value in customer.items() if key != "alert_email"}):
                raise Conflict("Invoice does not preserve the synthetic source/customer contract")
            members = list(session.execute(select(InvoiceMember, Movement, MovementVersion).join(
                Movement, Movement.id == InvoiceMember.movement_id).join(
                MovementVersion, MovementVersion.id == InvoiceMember.version_id).where(InvoiceMember.invoice_id == invoice.id)))
            if len(members) != scope["movement_count"]:
                raise Conflict("Invoice membership count does not match the supplied movements")
            scores = []
            for member, movement, version in members:
                if (movement.customer_id != scope["customer_id"] or movement.period != PERIOD
                        or movement.state != "ELIGIBLE" or movement.current_version != version.version_no
                        or member.amount != version.amount or version.origin != "SOURCE"):
                    raise Conflict("Invoice contains ineligible or changed movement evidence")
                model = session.scalar(select(Assessment).where(Assessment.version_id == version.id, Assessment.kind == "MODEL"))
                if not model or model.status != "SCORED" or model.flagged or not model.explanation or model.explanation.get("status") != "AVAILABLE":
                    raise Conflict("Invoice lacks complete, unflagged model evidence")
                scores.append(model)
            manifest = session.get(InvoiceSourceManifest, invoice.id)
            source_ids = set(session.scalars(select(InvoiceSourceFile.batch_id).where(InvoiceSourceFile.invoice_id == invoice.id)))
            if (not manifest or manifest.snapshot_hash != invoice.snapshot_hash or manifest.file_count != 2
                    or manifest.missing_reference_count or source_ids != {batch.id for batch in batches}):
                raise Conflict("Invoice source provenance is incomplete")
            if session.scalar(select(Outbox.id).where(Outbox.invoice_id == invoice.id).limit(1)):
                raise Conflict("Example invoice unexpectedly entered email dispatch")
            if (not invoice.package or digest(invoice.package) != invoice.fingerprint
                    or invoice.package.get("pdf_sha256") != invoice.pdf_sha256 or invoice.pdf_key != invoice.pdf_sha256):
                raise Conflict("Invoice package or PDF fingerprint changed")
            pdf = store.path(invoice.pdf_key, verify=True)
            snapshot = store.path(invoice.snapshot_key, verify=True)
            expected_snapshot = "".join(canonical(dict(movement_id=m.id, version_id=v.id, amount=str(member.amount), currency=v.currency)) + "\n"
                                        for member, m, v in sorted(members, key=lambda item: item[1].id)).encode()
            if snapshot.read_bytes() != expected_snapshot:
                raise Conflict("Frozen snapshot no longer matches exact invoice membership")
            destination = Path(output_dir).resolve() / (scope["customer_id"].lower() + ".pdf")
            copies.append((destination, pdf.read_bytes()))
            invoice_results.append(dict(**scope, invoice_id=invoice.id, invoice_number=invoice.number,
                status=invoice.status, pdf_path=str(destination), pdf_sha256=invoice.pdf_sha256,
                snapshot_sha256=invoice.snapshot_hash, package_fingerprint=invoice.fingerprint,
                model_versions=sorted({item.model_version for item in scores}), maximum_score=max(item.score for item in scores),
                source_batch_ids=sorted(source_ids), approved_by=None, approved_at=None, issued_at=None))
    result = dict(fixture=FIXTURE, synthetic=True, period=PERIOD, logical_movements=len(fixture["rows"]),
                  manifest_sha256=fixture["manifest_sha256"], source_file_sha256=fixture["manifest"]["file_sha256"],
                  creation_path="Source ingestion -> reconciliation -> model scoring -> monthly scheduler -> PDF rendering",
                  invoices=invoice_results, invoice_emails_queued=0)
    for path, content in copies:
        _write_exact(path, content)
    _write_exact(Path(output_dir).resolve() / "receipt.json", (json.dumps(result, indent=2) + "\n").encode())
    return result
