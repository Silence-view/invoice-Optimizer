"""Itemized presentation freezes exact source membership without changing money."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal

import pytest
from pypdf import PdfReader
from sqlalchemy import func, select

from invoice_optimizer import billing
from invoice_optimizer.billing import create_invoice, regenerate_invoice_format, approve_invoice
from invoice_optimizer.db import Blocked, Conflict, canonical, digest
from invoice_optimizer.ingestion import ingest_file
from invoice_optimizer.models import (Allocation, Audit, Command, Invoice, InvoiceMember, Job,
                                      Movement, MovementVersion, Outbox, Assessment)
from invoice_optimizer.movement_vat import MOVEMENT_VAT_POLICY
from invoice_optimizer.storage import ObjectStore
from invoice_optimizer.worker import run_job
from test_workflow import prepared, rows, write_csv


def _work(db):
    while run_job(db, kinds=('ASSESS', 'SCORE', 'RENDER')):
        pass


def _draft(db, line_format='SUMMARY'):
    result = create_invoice(db, 'C1', '2026-09', 'itemized-close-' + line_format, 'reviewer', line_format=line_format)
    _work(db)
    with db.transaction() as session:
        invoice = session.get(Invoice, result['id'])
        assert invoice.status == 'DRAFT'
        return invoice


def test_default_itemization_contains_all_exact_frozen_source_items(prepared):
    result = create_invoice(prepared, 'C1', '2026-09', 'new-default', 'reviewer')
    with prepared.transaction() as session:
        invoice = session.get(Invoice, result['id'])
        document = invoice.document
        assert document['line_format'] == 'ITEMIZED'
        assert document['movement_count'] == len(document['movement_detail']) == 2
        assert [row['row_no'] for row in document['movement_detail']] == [1, 2]
        assert [row['external_id'] for row in document['movement_detail']] == ['m0', 'm1']
        for detail in document['movement_detail']:
            version = session.get(MovementVersion, detail['version_id'])
            member = session.get(InvoiceMember, (invoice.id, detail['movement_id']))
            assert member.version_id == version.id and member.amount == Decimal(detail['amount'])
            assert version.movement_id == detail['movement_id']
            assert detail['event_at'] == version.payload['event_at']
            assert detail['description'] == version.payload['description']
            assert detail['supplier_id'] == version.payload['supplier_id']
        assert sum(Decimal(row['amount']) for row in document['movement_detail']) == invoice.net_total
        assert len(document['lines']) == 1
        assert document['lines'][0]['net_amount'] == '30.30'
        assert invoice.vat_total == Decimal('6.06')
        assert [row['vat_amount'] for row in document['movement_detail']] == ['2.02', '4.04']
        assert [row['gross_amount'] for row in document['movement_detail']] == ['12.12', '24.24']
        assert document['movement_vat'] == {'policy':MOVEMENT_VAT_POLICY, 'rate':'0.20',
            'line_vat_total':'6.06', 'line_gross_total':'36.36', 'rounding_adjustment':'0.00'}
    assert create_invoice(prepared, 'C1', '2026-09', 'new-default', 'reviewer') == result
    with pytest.raises(Conflict, match='different content'):
        create_invoice(prepared, 'C1', '2026-09', 'new-default', 'reviewer', line_format='SUMMARY')


def test_summary_is_explicit_streaming_alternative_and_cap_never_truncates(prepared, monkeypatch):
    monkeypatch.setattr(billing, 'MAX_ITEMIZED_MOVEMENTS', 1)
    with pytest.raises(Blocked, match='at most 1 movements'):
        create_invoice(prepared, 'C1', '2026-09', 'too-large', 'reviewer')
    with prepared.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 0
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == 'RENDER')) == 0
        assert session.get(Command, 'too-large') is None
    result = create_invoice(prepared, 'C1', '2026-09', 'explicit-summary', 'reviewer', line_format='SUMMARY')
    with prepared.transaction() as session:
        document = session.get(Invoice, result['id']).document
        assert document['line_format'] == 'SUMMARY' and document['movement_count'] == 2
        assert 'movement_detail' not in document


def test_itemization_preserves_invoice_level_vat_rounding(prepared, tmp_path):
    revised = rows(amounts=('0.03', '0.03'), revision=2)
    for side in ('A', 'B'):
        ingest_file(prepared, write_csv(tmp_path / ('pennies-' + side + '.csv'), revised), side, '2026-09', 2, True)
    _work(prepared)
    result = create_invoice(prepared, 'C1', '2026-09', 'penny-rounding', 'reviewer')
    with prepared.transaction() as session:
        invoice = session.get(Invoice, result['id'])
        assert (invoice.net_total, invoice.vat_total, invoice.gross_total) == (Decimal('0.06'), Decimal('0.01'), Decimal('0.07'))
        assert [row['amount'] for row in invoice.document['movement_detail']] == ['0.03', '0.03']
        assert [row['vat_amount'] for row in invoice.document['movement_detail']] == ['0.01', '0.01']
        assert [row['gross_amount'] for row in invoice.document['movement_detail']] == ['0.04', '0.04']
        assert invoice.document['movement_vat'] == {'policy':MOVEMENT_VAT_POLICY, 'rate':'0.20',
            'line_vat_total':'0.02', 'line_gross_total':'0.08', 'rounding_adjustment':'-0.01'}
        assert invoice.document['lines'][0]['vat_amount'] == '0.01'


def test_summary_regeneration_is_atomic_audited_and_requires_fresh_approval(prepared):
    db = prepared
    old = _draft(db)
    store = ObjectStore(db.settings.data_dir / 'objects')
    old_pdf = store.path(old.pdf_key, verify=True).read_bytes()
    old_document = deepcopy(old.document)
    with db.transaction() as session:
        scores = [(item.id, item.score, item.model_version) for item in session.scalars(select(Assessment).order_by(Assessment.id))]
        members = [(item.movement_id, item.version_id, item.amount) for item in session.scalars(select(InvoiceMember).where(InvoiceMember.invoice_id == old.id).order_by(InvoiceMember.movement_id))]
    changed = regenerate_invoice_format(db, old.id, old.fingerprint, 'ITEMIZED', 'format-1', 'reviewer')
    assert changed['approval_required'] and changed['old_invoice_id'] == old.id
    with db.transaction() as session:
        previous = session.get(Invoice, old.id)
        new = session.get(Invoice, changed['id'])
        assert previous.status == 'STALE' and previous.document == old_document
        assert (previous.fingerprint, previous.pdf_sha256) == (old.fingerprint, old.pdf_sha256)
        assert new.status == 'BUILDING' and new.revision == old.revision + 1 and new.number != old.number
        assert (new.net_total, new.vat_total, new.gross_total, new.snapshot_hash) == (old.net_total, old.vat_total, old.gross_total, old.snapshot_hash)
        assert new.approved_at is None and new.approved_by is None and new.issued_at is None
        assert [(item.movement_id, item.version_id, item.amount) for item in session.scalars(select(InvoiceMember).where(InvoiceMember.invoice_id == new.id).order_by(InvoiceMember.movement_id))] == members
        assert [(item.id, item.score, item.model_version) for item in session.scalars(select(Assessment).order_by(Assessment.id))] == scores
        audit = session.scalar(select(Audit).where(Audit.action == 'invoice_format_regenerated', Audit.entity_id == new.id))
        assert audit.actor == 'reviewer' and audit.details['old_invoice_id'] == old.id
        assert audit.details['new_invoice_id'] == new.id and audit.details['human_approval_required']
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == 'INVOICE')) == 0
        assert session.scalar(select(func.count()).select_from(Allocation)) == 0
    assert store.path(old.pdf_key, verify=True).read_bytes() == old_pdf
    assert regenerate_invoice_format(db, old.id, old.fingerprint, 'ITEMIZED', 'format-1', 'reviewer') == changed
    with pytest.raises(Conflict, match='different content'):
        regenerate_invoice_format(db, old.id, old.fingerprint, 'ITEMIZED', 'format-1', 'different-reviewer')
    _work(db)
    with db.transaction() as session:
        new = session.get(Invoice, changed['id'])
        assert new.status == 'DRAFT' and new.fingerprint != old.fingerprint
        assert new.approved_at is None
        text = '\n'.join(page.extract_text() for page in PdfReader(store.path(new.pdf_key)).pages)
        assert 'm0' in text and 'm1' in text
        assert session.get(Invoice, old.id).status == 'STALE'


@pytest.mark.parametrize('failure', ['fingerprint', 'approved', 'version', 'membership_amount', 'snapshot', 'limit', 'freeze_failure'])
def test_regeneration_failure_keeps_original_and_creates_no_replacement(prepared, monkeypatch, failure):
    db = prepared
    old = _draft(db)
    fingerprint = old.fingerprint
    if failure == 'fingerprint':
        fingerprint = 'f' * 64
    elif failure == 'approved':
        approve_invoice(db, old.id, old.fingerprint, 'approved-before-format', 'reviewer')
    elif failure == 'version':
        with db.transaction() as session:
            session.scalar(select(Movement)).current_version += 1
    elif failure == 'membership_amount':
        with db.transaction() as session:
            session.scalar(select(InvoiceMember)).amount += Decimal('1.00')
    elif failure == 'snapshot':
        with db.transaction() as session:
            item = session.get(Invoice, old.id)
            item.snapshot_hash = 'f' * 64
    elif failure == 'limit':
        monkeypatch.setattr(billing, 'MAX_ITEMIZED_MOVEMENTS', 1)
    elif failure == 'freeze_failure':
        def fail(*args, **kwargs):
            raise RuntimeError('Injected transaction rollback after old status change')
        monkeypatch.setattr(billing, '_freeze_invoice_in_session', fail)
    with db.transaction() as session:
        original_state = session.get(Invoice, old.id).status
        render_count = session.scalar(select(func.count()).select_from(Job).where(Job.kind == 'RENDER'))
    with pytest.raises((Blocked, Conflict, RuntimeError)):
        regenerate_invoice_format(db, old.id, fingerprint, 'ITEMIZED', 'rejected-format', 'reviewer')
    with db.transaction() as session:
        assert session.get(Invoice, old.id).status == original_state
        assert session.scalar(select(func.count()).select_from(Invoice)) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == 'RENDER')) == render_count
        assert session.get(Command, 'rejected-format') is None
        assert session.scalar(select(func.count()).select_from(Audit).where(Audit.action == 'invoice_format_regenerated')) == 0


def test_concurrent_format_regeneration_creates_only_one_replacement(prepared):
    old = _draft(prepared)
    def replace(index):
        try:
            return regenerate_invoice_format(prepared, old.id, old.fingerprint, 'ITEMIZED', f'concurrent-format-{index}', 'reviewer')
        except (Blocked, Conflict):
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(replace, range(2)))
    assert sum(item is not None for item in results) == 1
    with prepared.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 2
        assert session.get(Invoice, old.id).status == 'STALE'


def test_legacy_command_replays_when_format_omitted_but_not_explicitly_repurposed(prepared):
    result = {'id':'legacy-frozen-id', 'number':'DEMO-LEGACY', 'status':'BUILDING'}
    with prepared.transaction() as session:
        session.add(Command(request_id='legacy-close-command', action='close',
            payload_hash=digest({'customer_id':'C1', 'period':'2026-09'}), response=result))
    assert create_invoice(prepared, 'C1', '2026-09', 'legacy-close-command', 'reviewer') == result
    with pytest.raises(Conflict, match='different content'):
        create_invoice(prepared, 'C1', '2026-09', 'legacy-close-command', 'reviewer', line_format='ITEMIZED')


def test_invalid_format_rejected_before_invoice_mutation(prepared):
    with pytest.raises(ValueError, match='SUMMARY or ITEMIZED'):
        create_invoice(prepared, 'C1', '2026-09', 'bad-format', 'reviewer', line_format='AUTOMATIC')
    with prepared.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 0


def test_legacy_itemized_draft_upgrades_vat_once_with_frozen_totals_and_legacy_replay(prepared):
    db = prepared
    close_request = 'pre-vat-itemized-close'
    created = create_invoice(db, 'C1', '2026-09', close_request, 'reviewer', line_format='ITEMIZED')
    # Reproduce the previous release's frozen, not-yet-rendered itemized document
    # and close receipt, then let the unchanged worker produce its real PDF.
    with db.transaction() as session:
        old = session.get(Invoice, created['id'])
        document = deepcopy(old.document)
        document.pop('movement_vat')
        for row in document['movement_detail']:
            for field in ('vat_rate', 'vat_amount', 'gross_amount'):
                row.pop(field)
        old.document = document
        close_command = session.get(Command, close_request)
        close_command.payload_hash = digest({'customer_id':'C1','period':'2026-09','line_format':'ITEMIZED'})
        legacy_response = {key:value for key,value in close_command.response.items() if key != 'movement_vat_policy'}
        close_command.response = legacy_response
    _work(db)
    with db.transaction() as session:
        old = session.get(Invoice, created['id'])
        old_document = deepcopy(old.document)
    old_bytes = ObjectStore(db.settings.data_dir / 'objects').path(old.pdf_key, verify=True).read_bytes()
    changed = regenerate_invoice_format(db, old.id, old.fingerprint, 'ITEMIZED', 'add-movement-vat', 'reviewer')
    assert changed['movement_vat_policy'] == MOVEMENT_VAT_POLICY
    assert regenerate_invoice_format(db, old.id, old.fingerprint, 'ITEMIZED', 'add-movement-vat', 'reviewer') == changed
    assert create_invoice(db, 'C1', '2026-09', close_request, 'reviewer', line_format='ITEMIZED') == legacy_response
    with db.transaction() as session:
        previous = session.get(Invoice, old.id)
        new = session.get(Invoice, changed['id'])
        assert previous.status == 'STALE' and previous.document == old_document
        assert (new.net_total, new.vat_total, new.gross_total, new.snapshot_hash) == (old.net_total, old.vat_total, old.gross_total, old.snapshot_hash)
        assert new.document['movement_vat']['policy'] == MOVEMENT_VAT_POLICY
        assert new.document['movement_vat']['rounding_adjustment'] == '0.00'
        assert [row['vat_amount'] for row in new.document['movement_detail']] == ['2.02','4.04']
        command = session.get(Command, 'add-movement-vat')
        assert command.payload_hash == digest({'invoice_id':old.id, 'expected_fingerprint':old.fingerprint,
            'line_format':'ITEMIZED', 'actor':'reviewer', 'movement_vat_policy':MOVEMENT_VAT_POLICY})
        event = session.scalar(select(Audit).where(Audit.action == 'invoice_format_regenerated', Audit.entity_id == new.id))
        assert event.details['from_format'] == event.details['to_format'] == 'ITEMIZED'
        assert event.details['from_movement_vat_policy'] is None
        assert event.details['to_movement_vat_policy'] == MOVEMENT_VAT_POLICY
        assert new.approved_at is None and new.approved_by is None and new.issued_at is None
    _work(db)
    with db.transaction() as session:
        new = session.get(Invoice, changed['id'])
        assert new.status == 'DRAFT'
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == 'INVOICE')) == 0
        assert session.scalar(select(func.count()).select_from(Allocation)) == 0
        legacy_regen_response = {key:value for key,value in changed.items() if key != 'movement_vat_policy'}
        session.add(Command(request_id='pre-policy-regeneration', action='regenerate_invoice_format',
            payload_hash=digest({'invoice_id':old.id, 'expected_fingerprint':old.fingerprint,
                'line_format':'ITEMIZED', 'actor':'reviewer'}), response=legacy_regen_response))
    assert ObjectStore(db.settings.data_dir / 'objects').path(old.pdf_key, verify=True).read_bytes() == old_bytes
    assert regenerate_invoice_format(db, old.id, old.fingerprint, 'ITEMIZED', 'pre-policy-regeneration', 'reviewer') == legacy_regen_response
    with pytest.raises(Conflict, match='different content'):
        regenerate_invoice_format(db, old.id, old.fingerprint, 'ITEMIZED', 'pre-policy-regeneration', 'someone-else')
    with pytest.raises(Conflict, match='already uses'):
        regenerate_invoice_format(db, new.id, new.fingerprint, 'ITEMIZED', 'duplicate-vat-upgrade', 'reviewer')


@pytest.mark.parametrize('alteration', ['aggregate_missing', 'unknown_policy', 'partial_policy'])
def test_partial_or_unknown_vat_evidence_is_not_misclassified_as_a_legacy_upgrade(prepared, alteration):
    old = _draft(prepared, line_format='ITEMIZED')
    with prepared.transaction() as session:
        invoice = session.get(Invoice, old.id)
        document = deepcopy(invoice.document)
        if alteration == 'aggregate_missing':
            document.pop('movement_vat')
        elif alteration == 'unknown_policy':
            document['movement_vat']['policy'] = 'unrecognized-vat-policy'
        else:
            document['movement_vat'] = {}
        invoice.document = document
    with pytest.raises(Conflict, match='unsupported VAT evidence'):
        regenerate_invoice_format(prepared, old.id, old.fingerprint, 'ITEMIZED', 'reject-partial-policy', 'reviewer')
    with prepared.transaction() as session:
        assert session.get(Invoice, old.id).status == 'DRAFT'
        assert session.scalar(select(func.count()).select_from(Invoice)) == 1
        assert session.get(Command, 'reject-partial-policy') is None
