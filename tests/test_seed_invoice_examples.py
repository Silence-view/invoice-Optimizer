"""Source-only examples exercise the real scheduler, model, SQL and PDF compiler."""
import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfReader
from sqlalchemy import select, func

from invoice_optimizer.db import Blocked, Conflict, canonical
from invoice_optimizer.billing import regenerate_invoice_format
from invoice_optimizer.ingestion import ingest_file, load_customers
from invoice_optimizer.ml import train_model
from invoice_optimizer.models import (Assessment, Batch, Customer, Invoice, Movement, Outbox,
                                     ReviewCase, RuntimeOption, SourceRecord, now)
from invoice_optimizer.scheduler import prepare_due_invoices
from invoice_optimizer.seed_invoice_examples import FIXTURE, PERIOD, collect_results, fixture_data, seed_data
from invoice_optimizer.synthetic import generate_training_data
from invoice_optimizer.worker import run_job
from test_workflow import rows, write_csv


@pytest.fixture(scope="module")
def example_model(tmp_path_factory):
    root = tmp_path_factory.mktemp("example-model")
    dataset = root / "training.parquet"
    generate_training_data(dataset, rows=2000, seed=45, serving_period="2026-09")
    train_model(dataset, root / "model")
    return root / "model"


@pytest.fixture
def example_db(db, example_model):
    shutil.copytree(example_model, db.settings.model_dir, dirs_exist_ok=True)
    return db


def _work(db):
    for _ in range(30):
        if not run_job(db, kinds=("ASSESS", "SCORE", "RENDER")):
            return
    pytest.fail("Example worker did not reach a bounded idle state")


def _snapshot_existing(db):
    with db.transaction() as session:
        return {cls: {item.id: canonical({column.name: getattr(item, column.name) for column in cls.__table__.columns})
                      for item in session.scalars(select(cls))}
                for cls in (Customer, Batch, Movement, SourceRecord, Assessment, ReviewCase, Invoice, Outbox)}


def _assert_existing_unchanged(db, snapshot):
    with db.transaction() as session:
        for cls, records in snapshot.items():
            for identity, before in records.items():
                item = session.get(cls, identity)
                assert item is not None
                assert canonical({column.name: getattr(item, column.name) for column in cls.__table__.columns}) == before


def test_inputs_only_then_real_scheduler_builds_three_pdf_drafts_and_replay_preserves_legacy(example_db, tmp_path):
    db = example_db
    # The existing September scope contains a real draft and an unresolved source
    # mismatch with its queued alert. Loading August inputs must not rewrite any.
    master = tmp_path / "legacy-customers.json"
    master.write_text(json.dumps([
        dict(id="C1", legal_name="Existing Synthetic Client", address="1 Test Road, London", email="old@example.test", alert_email="review@example.test"),
        dict(id="C2", legal_name="Existing Review Client", address="2 Test Road, London", email="old2@example.test", alert_email="review@example.test")]))
    load_customers(db, master)
    source = rows()
    source[1]["customer_id"] = "C2"
    counterpart = [dict(row) for row in source]
    counterpart[1]["amount"] = "21.20"
    for side, payload in (("A", source), ("B", counterpart)):
        ingest_file(db, write_csv(tmp_path / (side + ".csv"), payload), side, "2026-09", 2, True)
    _work(db)
    prepare_due_invoices(db, as_of=date(2026, 10, 27))
    _work(db)
    before = _snapshot_existing(db)
    assert len(before[Invoice]) == 1 and before[ReviewCase] and before[Outbox]

    loaded = seed_data(db)
    assert loaded["logical_movements"] == 72
    assert loaded["invoice_creation_requested"] is False
    assert loaded["email_requested"] is False
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Invoice).where(Invoice.period == PERIOD)) == 0
        assert session.scalar(select(func.count()).select_from(Movement).where(Movement.period == PERIOD)) == 72
    with pytest.raises(Blocked, match="Waiting for the normal scheduler"):
        collect_results(db, tmp_path / "output")

    _work(db)
    prepare_due_invoices(db, as_of=date(2026, 10, 27))
    _work(db)
    report = collect_results(db, tmp_path / "output")
    assert len(report["invoices"]) == 3
    assert [item["gross_total"] for item in report["invoices"]] == ["131.70", "516.60", "1316.70"]
    for item in report["invoices"]:
        assert item["status"] == "DRAFT" and item["approved_by"] is None and item["issued_at"] is None
        text = "\n".join(page.extract_text() for page in PdfReader(item["pdf_path"]).pages)
        assert item["invoice_number"] in text
        assert "SYNTHETIC" in text.upper() and "DO NOT PAY" in text.upper()
        assert len(item["source_batch_ids"]) == 2 and item["model_versions"]
    _assert_existing_unchanged(db, before)
    completed = _snapshot_existing(db)
    with db.transaction() as session:
        session.add(RuntimeOption(key="worker.heartbeat", value={"status":"RUNNING", "at":now().isoformat()}))
    replay = seed_data(db)
    assert all(item["replayed"] for item in replay["source_batches"])
    assert collect_results(db, tmp_path / "output") == report
    _assert_existing_unchanged(db, completed)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(ReviewCase).where(ReviewCase.period == PERIOD)) == 0
        assert session.scalar(select(func.count()).select_from(Invoice).where(Invoice.period == PERIOD)) == 3
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == "INVOICE")) == 0

    # Presentation changes use the ordinary audited replacement and renderer.
    # Original exported bytes remain historical evidence; collect current drafts
    # into a new directory and preserve source-only replay after regeneration.
    original_pdfs = {item['invoice_id']: Path(item['pdf_path']).read_bytes() for item in report['invoices']}
    replacements = []
    for item in report['invoices']:
        replacements.append(regenerate_invoice_format(db, item['invoice_id'], item['package_fingerprint'],
            'SUMMARY', 'example-summary-' + item['invoice_id'], 'test-reviewer'))
    _work(db)
    after_regeneration = _snapshot_existing(db)
    replay = seed_data(db)
    assert all(item['replayed'] for item in replay['source_batches'])
    _assert_existing_unchanged(db, after_regeneration)
    current = collect_results(db, tmp_path / 'summary-output')
    assert {item['invoice_id'] for item in current['invoices']} == {item['id'] for item in replacements}
    with db.transaction() as session:
        for item in report['invoices']:
            old = session.get(Invoice, item['invoice_id'])
            assert old.status == 'STALE' and old.fingerprint == item['package_fingerprint']
            assert Path(item['pdf_path']).read_bytes() == original_pdfs[old.id]
        assert session.scalar(select(func.count()).select_from(Invoice).where(Invoice.period == PERIOD)) == 6
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == 'INVOICE')) == 0
    with pytest.raises(Conflict, match='different bytes'):
        collect_results(db, tmp_path / 'output')
    with db.transaction() as session:
        session.get(Invoice, current['invoices'][0]['invoice_id']).status = 'STALE'
    with pytest.raises(Conflict, match='not a verified presentation revision'):
        seed_data(db)


def test_conflicting_customer_rejected_before_files_or_source_writes(example_db):
    db = example_db
    customer = dict(fixture_data()["customers"][0], legal_name="An unrelated company")
    with db.transaction() as session:
        session.add(Customer(**customer))
    with pytest.raises(Conflict, match="different master data"):
        seed_data(db)
    assert not (db.settings.data_dir / "synthetic" / FIXTURE).exists()
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Batch)) == 0


def test_foreign_period_batch_is_not_absorbed_into_complete_manifest(example_db):
    db = example_db
    with db.transaction() as session:
        session.add(Batch(id="foreign", source="A", period=PERIOD, object_key="a"*64,
                          sha256="a"*64, original_name="foreign.csv", status="COMPLETE"))
    with pytest.raises(Conflict, match="foreign, altered or incomplete"):
        seed_data(db)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Customer)) == 0


def test_changed_input_file_is_never_overwritten(example_db):
    directory = example_db.settings.data_dir / "synthetic" / FIXTURE
    directory.mkdir(parents=True)
    path = directory / "source_a.csv"
    path.write_text("someone else's input")
    with pytest.raises(Conflict, match="file changed"):
        seed_data(example_db)
    assert path.read_text() == "someone else's input"
    with example_db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Customer)) == 0


def test_running_worker_blocks_source_pair_loading(example_db):
    with example_db.transaction() as session:
        session.add(RuntimeOption(key="worker.heartbeat", value={"status":"RUNNING", "at":now().isoformat()}))
    with pytest.raises(Blocked, match="Stop the background worker"):
        seed_data(example_db)
    with example_db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Batch)) == 0


def test_model_unavailable_rejected_before_mutation(db):
    with pytest.raises(Blocked, match="verified active model"):
        seed_data(db)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Customer)) == 0


def test_scoring_or_cutoff_failure_rejected_before_mutation(example_db, monkeypatch):
    import invoice_optimizer.seed_invoice_examples as module
    monkeypatch.setattr(module, "score_batch", lambda payloads, model_dir: [
        {"status":"UNAVAILABLE", "flagged":False, "score":None} for _ in payloads])
    with pytest.raises(Blocked, match="information cutoff"):
        seed_data(example_db)
    with example_db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Customer)) == 0


def test_fixture_is_reproducible_and_source_pair_matches():
    first, second = fixture_data(), fixture_data()
    assert first == second
    assert first["files"]["source_a.csv"] == first["files"]["source_b.csv"]
    assert len(first["rows"]) == 72
    assert len({row["movement_id"] for row in first["rows"]}) == 72
    assert all(row["period"] == PERIOD for row in first["rows"])
