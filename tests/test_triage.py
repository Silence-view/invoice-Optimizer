"""Personal swipe workflow decisions preserve financial and identity guards."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select

from invoice_optimizer import triage
from invoice_optimizer.api import create_app
from invoice_optimizer.assessment import resolve_case
from invoice_optimizer.billing import (approve_invoice, create_invoice, dispatch_invoice,
                                      reject_invoice, reopen_invoice)
from invoice_optimizer.db import Blocked, Conflict, digest
from invoice_optimizer.models import (Allocation, Audit, Command, Invoice, Job, Movement,
    MovementVersion, Outbox, ReviewCase, RuntimeOption)
from invoice_optimizer.operators import Operator
from invoice_optimizer.storage import ObjectStore
from test_bulk_review import case_factory
from test_review_history import reviewers
from test_workflow import prepared


REASON = 'Deferred for a later personal review; financial status remains unchanged.'


def case_args(item, actor, key='postpone-case'):
    return {'kind': 'CASE', 'id': item['case_id'], 'expected_version': item['expected_version'],
            'fingerprint': None, 'reason': REASON, 'request_id': key, 'actor': actor}


@pytest.fixture
def frozen_invoice(prepared):
    """Use real frozen membership; a stored byte fixture isolates PDF rendering."""
    db = prepared
    result = create_invoice(db, 'C1', '2026-09', 'triage-freeze', 'test:fixture')
    key = ObjectStore(db.settings.data_dir / 'objects').put_bytes(b'Synthetic immutable byte fixture; not a rendered PDF.')
    with db.transaction() as session:
        invoice = session.get(Invoice, result['id'])
        invoice.pdf_key = invoice.pdf_sha256 = key
        invoice.package = {'invoice_id': invoice.id, 'pdf_key': key, 'pdf_sha256': key,
                           'snapshot_sha256': invoice.snapshot_hash, 'message': {'to': ['test@example.test']}}
        invoice.fingerprint = digest(invoice.package)
        invoice.status = 'DRAFT'
        for job in session.scalars(select(Job).where(Job.kind == 'RENDER')):
            job.status = 'DONE'
        return invoice.id, invoice.fingerprint


def invoice_args(invoice, actor, key='postpone-invoice'):
    return {'kind': 'INVOICE', 'id': invoice[0], 'expected_version': None, 'fingerprint': invoice[1],
            'reason': REASON, 'request_id': key, 'actor': actor}


def audit_count(db, action):
    with db.transaction() as session:
        return session.scalar(select(func.count()).select_from(Audit).where(Audit.action == action))


def test_postpone_is_personal_bound_24_hours_and_idempotent_without_domain_decision(db, case_factory, reviewers):
    item = case_factory('personal')
    alice, bob = reviewers
    args = case_args(item, alice)
    first = triage.postpone(db, **args)
    assert triage.postpone(db, **args) == first
    personal = first['personal_postponement']
    assert datetime.fromisoformat(personal['until']) - datetime.fromisoformat(personal['postponed_at']) == timedelta(hours=24)
    assert first['financial_status_changed'] is False and first['reviewer']['actor_id'] == alice
    with db.transaction() as session:
        case = session.get(ReviewCase, item['case_id'])
        binding = triage.case_binding(case, 1)
        assert triage.personal_postponements(session, alice, 'CASE', [(case.id, binding)])[case.id] == personal
        assert triage.personal_postponements(session, bob, 'CASE', [(case.id, binding)]) == {}
        assert triage.personal_postponements(session, 'shared', 'CASE', [(case.id, binding)]) == {}
        assert triage.personal_postponements(session, alice, 'CASE', [(case.id, {**binding, 'version_no': 2})]) == {}
        assert case.status == 'OPEN' and case.reviewed_by is None
        assert session.get(Movement, case.movement_id).state == 'REVIEW'
        assert session.scalar(select(func.count()).select_from(Outbox)) == 0
    assert audit_count(db, 'review_postponed') == 1
    with pytest.raises(Conflict):
        triage.postpone(db, **{**args, 'actor': bob})
    with pytest.raises(Conflict):
        triage.postpone(db, **{**args, 'reason': REASON + ' Changed.'})
    assert audit_count(db, 'review_postpone_rejected') == 2


def test_expired_postponement_is_inactive_and_resume_removes_only_callers_choice(db, case_factory, reviewers, monkeypatch):
    item = case_factory('resume')
    alice, bob = reviewers
    first = triage.postpone(db, **case_args(item, alice))
    triage.postpone(db, **case_args(item, bob, 'bob-postpone'))
    with monkeypatch.context() as patch:
        patch.setattr(triage, 'now', lambda: datetime.fromisoformat(first['personal_postponement']['until']) + timedelta(seconds=1))
        with db.transaction() as session:
            binding = triage.case_binding(session.get(ReviewCase, item['case_id']), 1)
            assert triage.personal_postponements(session, alice, 'CASE', [(item['case_id'], binding)]) == {}
    args = case_args(item, alice, 'resume-alice')
    result = triage.resume(db, **args)
    assert result['status'] == 'RESUMED' and result['personal_postponement'] is None
    assert triage.resume(db, **args) == result
    with db.transaction() as session:
        case = session.get(ReviewCase, item['case_id'])
        values = [(case.id, triage.case_binding(case, 1))]
        assert triage.personal_postponements(session, alice, 'CASE', values) == {}
        assert case.id in triage.personal_postponements(session, bob, 'CASE', values)
    assert audit_count(db, 'review_resumed') == 1


def test_stale_closed_and_inactive_postpone_requests_are_rejected_and_attributed(db, case_factory, reviewers):
    stale = case_factory('stale', current=2, case_version=1)
    closed = case_factory('closed', status='RESOLVED')
    alice, _ = reviewers
    with pytest.raises(Conflict):
        triage.postpone(db, **{**case_args(stale, alice, 'stale-postpone'), 'expected_version': 1})
    with pytest.raises(Conflict):
        triage.postpone(db, **case_args(closed, alice, 'closed-postpone'))
    with pytest.raises(Blocked):
        triage.postpone(db, **case_args(stale, 'shared', 'shared-postpone'))
    with db.transaction() as session:
        session.get(Operator, alice[5:]).active = False
    with pytest.raises(ValueError):
        triage.postpone(db, **case_args(stale, alice, 'inactive-postpone'))
    assert audit_count(db, 'review_postpone_rejected') == 2
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(RuntimeOption).where(RuntimeOption.key.like('triage:%'))) == 0


def test_quarantine_can_be_personally_postponed_without_inventing_a_version(db, case_factory, reviewers):
    item = case_factory('quarantine', quarantine=True)
    args = {**case_args(item, reviewers[0]), 'expected_version': None}
    result = triage.postpone(db, **args)
    assert result['binding'] == {'case_id': item['case_id'], 'version_id': None, 'version_no': None}
    with pytest.raises(Conflict):
        triage.postpone(db, **{**args, 'expected_version': 1, 'request_id': 'invented-version'})


def test_concurrent_same_postpone_request_has_one_audit_and_one_receipt(db, case_factory, reviewers):
    item = case_factory('parallel')
    barrier = Barrier(2)
    def run():
        barrier.wait(timeout=5)
        return triage.postpone(db, **case_args(item, reviewers[0]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=10) for future in [pool.submit(run), pool.submit(run)]]
    assert results[0] == results[1]
    assert audit_count(db, 'review_postponed') == 1


def test_personal_projection_is_one_read_only_lookup_for_one_or_fifty_cards(db, case_factory, reviewers):
    items = [case_factory(f'bounded-{index:02d}') for index in range(50)]
    actor = reviewers[0]
    triage.postpone(db, **case_args(items[0], actor))
    with db.transaction() as session:
        cases = list(session.scalars(select(ReviewCase).order_by(ReviewCase.id)))
        bindings = [(case.id, triage.case_binding(case, 1)) for case in cases]
        statements = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(db.engine, 'before_cursor_execute', capture)
        try:
            one = triage.personal_postponements(session, actor, 'CASE', bindings[:1])
            first = list(statements)
            statements.clear()
            fifty = triage.personal_postponements(session, actor, 'CASE', bindings)
            second = list(statements)
        finally:
            event.remove(db.engine, 'before_cursor_execute', capture)
        assert one == fifty and len(fifty) == 1
        assert len(first) == len(second) == 1
        assert all(sql.lstrip().upper().startswith('SELECT') for sql in first + second)


def test_invoice_postpone_and_resume_keep_package_and_status_unchanged(prepared, frozen_invoice, reviewers):
    args = invoice_args(frozen_invoice, reviewers[0])
    result = triage.postpone(prepared, **args)
    assert result['binding'] == {'invoice_id': frozen_invoice[0], 'fingerprint': frozen_invoice[1]}
    with prepared.transaction() as session:
        invoice = session.get(Invoice, frozen_invoice[0])
        assert invoice.status == 'DRAFT' and invoice.approved_at is None
        assert session.scalar(select(func.count()).select_from(Outbox)) == 0
    with pytest.raises(Conflict):
        triage.postpone(prepared, **{**args, 'fingerprint': 'f' * 64, 'request_id': 'wrong-package'})
    assert triage.resume(prepared, **{**args, 'request_id': 'resume-package'})['status'] == 'RESUMED'


@pytest.mark.parametrize('initial', ['DRAFT', 'APPROVED'])
def test_invoice_rejection_holds_scope_and_reopen_requires_fresh_approval(prepared, frozen_invoice, reviewers, initial):
    db = prepared
    identifier, fingerprint = frozen_invoice
    actor = reviewers[0]
    if initial == 'APPROVED':
        approve_invoice(db, identifier, fingerprint, 'approval-before-reject', actor, reason='Reviewed the exact frozen package.')
    result = reject_invoice(db, identifier, fingerprint, 'The package requires a further check.', 'reject-package', actor)
    assert result['status'] == 'REJECTED' and result['reviewer']['actor_id'] == actor
    assert reject_invoice(db, identifier, fingerprint, 'The package requires a further check.', 'reject-package', actor) == result
    with pytest.raises(Conflict):
        create_invoice(db, 'C1', '2026-09', 'do-not-regenerate', actor)
    with pytest.raises(Blocked):
        dispatch_invoice(db, identifier, 'do-not-send', actor)
    reopened = reopen_invoice(db, identifier, fingerprint, 'Investigation is complete; ready for another review.', 'reopen-package', actor)
    assert reopened['status'] == 'DRAFT'
    with db.transaction() as session:
        invoice = session.get(Invoice, identifier)
        assert invoice.approved_at is None and invoice.approved_by is None and invoice.issued_at is None
        assert invoice.fingerprint == fingerprint
        assert session.scalar(select(func.count()).select_from(Outbox)) == 0
        assert session.scalar(select(func.count()).select_from(Allocation)) == 0
    assert audit_count(db, 'invoice_rejected') == audit_count(db, 'invoice_reopened') == 1


def test_invoice_decisions_bind_actor_fingerprint_and_reason_and_preserve_legacy_receipt(prepared, frozen_invoice, reviewers):
    db = prepared
    identifier, fingerprint = frozen_invoice
    alice, bob = reviewers
    with pytest.raises(Conflict):
        reject_invoice(db, identifier, 'f' * 64, 'The viewed package changed.', 'wrong-fingerprint', alice)
    result = approve_invoice(db, identifier, fingerprint, 'approve-with-reason', alice, reason='Verified the frozen package and recipient.')
    assert approve_invoice(db, identifier, fingerprint, 'approve-with-reason', alice, reason='Verified the frozen package and recipient.') == result
    with pytest.raises(Conflict):
        approve_invoice(db, identifier, fingerprint, 'approve-with-reason', bob, reason='Verified the frozen package and recipient.')
    with pytest.raises(Conflict):
        approve_invoice(db, identifier, fingerprint, 'approve-with-reason', alice, reason='A different note must conflict.')
    legacy = {'id': identifier, 'status': 'APPROVED', 'fingerprint': fingerprint}
    with db.transaction() as session:
        session.add(Command(request_id='legacy-unbound', action='approve', payload_hash=digest({'id': identifier, 'fingerprint': fingerprint}), response=legacy))
    with pytest.raises(Conflict):
        approve_invoice(db, identifier, fingerprint, 'legacy-unbound', alice)
    with db.transaction() as session:
        assert session.get(Command, 'legacy-unbound').response == legacy
    assert audit_count(db, 'invoice_approved') == 1
    assert audit_count(db, 'invoice_decision_rejected') == 4


def test_reopen_rejects_changed_scope_and_issued_invoice_is_never_rejected(prepared, frozen_invoice, reviewers):
    db = prepared
    identifier, fingerprint = frozen_invoice
    actor = reviewers[0]
    reject_invoice(db, identifier, fingerprint, 'Hold for investigation.', 'hold', actor)
    with db.transaction() as session:
        movement = session.scalar(select(Movement))
        movement.current_version += 1
    with pytest.raises(Blocked):
        reopen_invoice(db, identifier, fingerprint, 'Attempt reopen with changed membership.', 'stale-reopen', actor)
    with db.transaction() as session:
        assert session.get(Invoice, identifier).status == 'REJECTED'
        session.scalar(select(Movement).where(Movement.id == movement.id)).current_version -= 1
    reopen_invoice(db, identifier, fingerprint, 'Membership is again the frozen version.', 'valid-reopen', actor)
    approve_invoice(db, identifier, fingerprint, 'ready-to-issue', actor)
    dispatch_invoice(db, identifier, 'issue-only-no-send', actor)
    with pytest.raises(Blocked):
        reject_invoice(db, identifier, fingerprint, 'Issued packages cannot be rejected.', 'issued-reject', actor)
    with pytest.raises(Conflict):
        triage.postpone(db, **invoice_args(frozen_invoice, actor, 'issued-postpone'))


def test_invoice_reject_races_dispatch_with_one_consistent_financial_outcome(prepared, frozen_invoice, reviewers):
    db = prepared
    identifier, fingerprint = frozen_invoice
    actor = reviewers[0]
    approve_invoice(db, identifier, fingerprint, 'race-approve', actor)
    barrier = Barrier(2)
    def decision(kind):
        barrier.wait(timeout=5)
        try:
            return reject_invoice(db, identifier, fingerprint, 'Reject before issuance if still possible.', 'race-reject', actor) if kind == 'reject' else dispatch_invoice(db, identifier, 'race-dispatch', actor)
        except (Conflict, Blocked):
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=10) for future in [pool.submit(decision, 'reject'), pool.submit(decision, 'dispatch')]]
    assert sum(item is not None for item in results) == 1
    with db.transaction() as session:
        invoice = session.get(Invoice, identifier)
        assert invoice.status in ('REJECTED', 'ISSUED')
        assert (invoice.status == 'ISSUED') == bool(session.scalar(select(func.count()).select_from(Outbox)))


def test_http_postpone_requires_personal_csrf_and_projects_only_the_current_operator(db, case_factory):
    item = case_factory('api')
    body = {key: value for key, value in case_args(item, 'unused').items() if key != 'actor'}
    app = create_app(db.settings, db)
    with TestClient(app) as shared, TestClient(app) as personal:
        shared.headers['Authorization'] = 'Bearer ' + db.settings.api_token
        assert shared.post('/api/review/postpone', json=body).status_code == 403
        login = personal.post('/api/operators/bootstrap', json={'setup_token': db.settings.api_token,
            'username': 'triage-reviewer', 'display_name': 'Synthetic Triage Reviewer',
            'password': 'a-valid-synthetic-reviewer-password'})
        assert login.status_code == 200
        who = login.json()
        assert personal.post('/api/review/postpone', json=body).status_code == 403
        headers = {'X-CSRF-Token': who['csrf_token']}
        assert personal.post('/api/review/postpone', json=body, headers={**headers, 'Origin': 'https://foreign.example'}).status_code == 403
        assert personal.post('/api/review/postpone', json=body, headers=headers).status_code == 200
        mine = personal.get('/api/cases').json()[0]
        assert mine['personal_postponement']['actor'] == who['actor']
        assert shared.get('/api/cases').json()[0]['personal_postponement'] is None
        assert personal.post('/api/review/resume', json={**body, 'request_id': 'http-resume'}, headers=headers).status_code == 200
        assert personal.get('/api/cases').json()[0]['personal_postponement'] is None
        assert personal.post('/api/review/postpone', json={**body, 'expected_version': True, 'request_id': 'bad-type'}, headers=headers).status_code == 422
        assert personal.post('/api/review/postpone', json={**body, 'request_id': 'x' * 121}, headers=headers).status_code == 422


def test_http_invoice_list_exposes_fingerprint_and_personal_reject_reopen_routes(prepared, frozen_invoice):
    db = prepared
    identifier, fingerprint = frozen_invoice
    with TestClient(create_app(db.settings, db)) as client:
        login = client.post('/api/operators/bootstrap', json={'setup_token': db.settings.api_token,
            'username': 'invoice-triage', 'display_name': 'Synthetic Invoice Reviewer',
            'password': 'a-valid-synthetic-invoice-password'})
        assert login.status_code == 200
        who = login.json()
        client.headers['X-CSRF-Token'] = who['csrf_token']
        body = {'fingerprint': fingerprint, 'reason': 'A personal rejection of this exact package.', 'request_id': 'http-invoice-reject'}
        assert client.post(f'/api/invoices/{identifier}/reject', json={**body, 'request_id': 'x' * 121}).status_code == 422
        listed = client.get('/api/invoices').json()[0]
        assert listed['fingerprint'] == fingerprint and listed['pdf_ready'] and listed['personal_postponement'] is None
        postpone_body = {key: value for key, value in invoice_args(frozen_invoice, who['actor']).items() if key != 'actor'}
        assert client.post('/api/review/postpone', json=postpone_body).status_code == 200
        assert client.get('/api/invoices').json()[0]['personal_postponement']['binding']['fingerprint'] == fingerprint
        assert client.post(f'/api/invoices/{identifier}/reject', json=body).json()['status'] == 'REJECTED'
        assert client.post(f'/api/invoices/{identifier}/reopen', json={**body, 'request_id': 'http-reopen'}).json()['status'] == 'DRAFT'
        assert client.post(f'/api/invoices/{identifier}/approve', json={**body, 'reason': 'Approved after checking the whole package.', 'request_id': 'http-approved'}).json()['status'] == 'APPROVED'
        with db.transaction() as session:
            assert session.get(Invoice, identifier).approved_by == who['actor']
            assert session.scalar(select(func.count()).select_from(Outbox)) == 0


def test_case_reject_is_existing_confirmed_issue_not_an_approval(db, case_factory, reviewers):
    item = case_factory('reject')
    result = resolve_case(db, item['case_id'], 1, 'CONFIRMED_ISSUE', 'Confirmed the source issue after review.', 'reject-case', reviewers[0])
    assert result['status'] == 'OPEN' and result['movement_state'] == 'BLOCKED'
    assert result['reviewer']['actor_id'] == reviewers[0]
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Outbox)) == 0
