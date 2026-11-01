"""Real PostgreSQL review-release boundaries, replay and concurrency tests."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event, local

import pytest
from sqlalchemy import func, select

from invoice_optimizer import assessment, bulk_review
from invoice_optimizer.assessment import correct_case, resolve_case
from invoice_optimizer.bulk_review import bulk_release_cases
from invoice_optimizer.db import Blocked, Conflict, change_state, counter
from invoice_optimizer.models import (Allocation, Assessment, Audit, Command, Counter, Customer,
    DeliveryAttempt, Invoice, Movement, MovementVersion, Outbox, ReviewCase, now)


REASON = 'Verified the retained source evidence and the model risk finding.'


@pytest.fixture
def case_factory(db):
    with db.transaction() as s:
        s.add(Customer(id='BULK-C1',legal_name='Synthetic Bulk Review Ltd',address='Synthetic UK',
                       email='accounts@example.test',alert_email='review@example.test'))

    def create(name, *, case_reason='MODEL_RISK', status='OPEN', rule_status='PASS',
               model_status='SCORED', score=0.92, flagged=True, provenance=True,
               state='REVIEW', current=1, case_version=1, quarantine=False, sibling=False, issued=False):
        mid='movement-'+name;vid='version-'+name+'-'+str(case_version);cid='case-'+name
        with db.transaction() as s:
            if not quarantine:
                s.add(Movement(id=mid,external_id=mid,customer_id='BULK-C1',period='2026-09',current_version=current,state=state))
                s.flush()
                source={'amount':'10.00','currency':'GBP','supplier_id':'SUP1','period':'2026-09','event_at':'2026-09-01T12:00:00+00:00'}
                payload={**source,'description':'Synthetic service','expected_amount':'10.00','source_values':{'A':source,'B':source}}
                for number in sorted({current,case_version}):
                    s.add(MovementVersion(id='version-'+name+'-'+str(number),movement_id=mid,version_no=number,
                        content_hash='hash-'+name+'-'+str(number),payload=payload,source_refs=[],amount=Decimal('10.00'),currency='GBP'))
                s.flush()
                if rule_status is not None:
                    s.add(Assessment(version_id=vid,kind='RULE',status=rule_status,flagged=rule_status!='PASS',reasons=[]))
                if model_status is not None:
                    s.add(Assessment(version_id=vid,kind='MODEL',status=model_status,score=score,flagged=flagged,
                        reasons=['MODEL_RISK'],model_version='fixture-model' if provenance else None,
                        policy_version='fixture-policy',feature_schema_version='fixture-features',features={}))
                counter(s,'movements',1);counter(s,'state:'+state,1)
            s.add(ReviewCase(id=cid,dedup_key=cid,movement_id=None if quarantine else mid,
                version_id=None if quarantine else vid,customer_id='BULK-C1',period='2026-09',
                reason='QUARANTINE: invalid source' if quarantine else case_reason,status=status,owner='review@example.test'))
            if sibling:
                s.add(ReviewCase(id=cid+'-sibling',dedup_key=cid+'-sibling',movement_id=mid,version_id=vid,
                    customer_id='BULK-C1',period='2026-09',reason='OTHER_REVIEW',owner='review@example.test'))
            if issued:
                invoice=Invoice(id='invoice-'+name,number='DEMO-'+name,customer_id='BULK-C1',period='2026-09',
                    revision=1,status='ISSUED',net_total=Decimal('10.00'),vat_total=Decimal('2.00'),gross_total=Decimal('12.00'),
                    snapshot_hash='test',snapshot_key='test',document={},issued_at=now())
                s.add(invoice);s.flush();s.add(Allocation(movement_id=mid,invoice_id=invoice.id))
        return {'case_id':cid,'expected_version':current}
    return create


def counts(db):
    with db.transaction() as s:
        return dict(s.execute(select(Counter.name,Counter.value)).all())


def test_successful_bounded_release_is_idempotent_and_sends_nothing(db,case_factory):
    items=[case_factory('a'),case_factory('b')]
    result=bulk_release_cases(db,items,REASON,'request-'+'x'*112,'reviewer')
    assert result['counts']=={'requested':2,'resolved':2,'released':2,'failed':0}
    assert result['status']=='COMPLETE'
    assert all(item['ok'] and item['status']=='RESOLVED' and item['movement_state']=='ELIGIBLE' for item in result['items'])
    assert bulk_release_cases(db,items,REASON,'request-'+'x'*112,'reviewer')==result
    assert counts(db)=={'movements':2,'state:REVIEW':0,'state:ELIGIBLE':2}
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='case_reviewed'))==2
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='bulk_review_completed'))==1
        assert all(len(command.request_id)<=120 for command in s.scalars(select(Command)))
        assert s.scalar(select(func.count()).select_from(Invoice))==0
        assert s.scalar(select(func.count()).select_from(Outbox))==0
        assert s.scalar(select(func.count()).select_from(DeliveryAttempt))==0


@pytest.mark.parametrize('change', ['reason','version','actor','order'])
def test_parent_request_id_rejects_changed_payload(db,case_factory,change):
    items=[case_factory('a'),case_factory('b')]
    bulk_release_cases(db,items,REASON,'stable-parent','reviewer')
    reason,actor=REASON,'reviewer'
    if change=='reason':reason+=' Changed.'
    elif change=='version':items[0]['expected_version']=2
    elif change=='actor':actor='different-reviewer'
    else:items.reverse()
    with pytest.raises(Conflict):bulk_release_cases(db,items,reason,'stable-parent',actor)


def test_mixed_items_commit_success_and_report_domain_rejections(db,case_factory):
    items=[case_factory('valid'),case_factory('rule',rule_status='FAIL'),
           case_factory('deterministic',case_reason='AMOUNT_MISMATCH'),
           case_factory('unavailable',model_status='UNAVAILABLE',score=None),
           case_factory('missing-rule',rule_status=None),case_factory('missing-model',model_status=None),
           case_factory('incomplete-model',provenance=False),case_factory('null-score',score=None),
           case_factory('unflagged',flagged=False),case_factory('quarantine',quarantine=True),
           case_factory('closed',status='RESOLVED'),case_factory('stale',current=2,case_version=1),
           case_factory('blocked-state',state='BLOCKED'),case_factory('sibling',sibling=True),
           case_factory('issued',issued=True),{'case_id':'missing-case','expected_version':1}]
    before=counts(db)
    result=bulk_release_cases(db,items,REASON,'mixed-parent','reviewer')
    assert result['counts']=={'requested':16,'resolved':1,'released':1,'failed':15}
    assert result['items'][0]['ok'] is True
    assert all(not item['ok'] and item['error'] and item['code'] for item in result['items'][1:])
    assert result['items'][-1]['code']=='NOT_FOUND'
    assert {result['items'][i]['code'] for i in (10,11)}=={'CONFLICT'}
    after=counts(db)
    assert after['movements']==before['movements']
    assert after['state:REVIEW']==before['state:REVIEW']-1
    assert after['state:ELIGIBLE']==1
    assert after['state:BLOCKED']==before['state:BLOCKED']
    with db.transaction() as s:
        assert s.get(ReviewCase,'case-rule').status=='OPEN'
        assert s.get(ReviewCase,'case-issued').status=='OPEN'
        assert s.get(Invoice,'invoice-issued').status=='ISSUED'


def test_interrupted_parent_resumes_committed_children(db,case_factory,monkeypatch):
    items=[case_factory('first'),case_factory('second')]
    real=bulk_review._resolve_case_in_session
    def interrupted(s,case_id,*args,**kwargs):
        if case_id=='case-second':raise RuntimeError('simulated process interruption')
        return real(s,case_id,*args,**kwargs)
    monkeypatch.setattr(bulk_review,'_resolve_case_in_session',interrupted)
    with pytest.raises(RuntimeError):bulk_release_cases(db,items,REASON,'resume-parent','reviewer')
    with db.transaction() as s:
        assert s.get(Command,'resume-parent').response['status']=='RUNNING'
        assert s.get(ReviewCase,'case-first').status=='RESOLVED'
        assert s.get(ReviewCase,'case-second').status=='OPEN'
    with pytest.raises(Conflict):bulk_release_cases(db,items,REASON+' changed','resume-parent','reviewer')
    monkeypatch.setattr(bulk_review,'_resolve_case_in_session',real)
    result=bulk_release_cases(db,items,REASON,'resume-parent','reviewer')
    assert result['counts']['released']==2
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='case_reviewed',Audit.entity_id=='case-first'))==1
    assert counts(db)['state:ELIGIBLE']==2


def test_rejected_item_snapshot_is_stable_until_new_review_request(db,case_factory):
    item=case_factory('blocked',rule_status='FAIL')
    failed=bulk_release_cases(db,[item],REASON,'first-attempt','reviewer')
    assert failed['counts']['failed']==1
    with db.transaction() as s:
        s.scalar(select(Assessment).where(Assessment.version_id=='version-blocked-1',Assessment.kind=='RULE')).status='PASS'
    assert bulk_release_cases(db,[item],REASON,'first-attempt','reviewer')==failed
    assert bulk_release_cases(db,[item],REASON,'new-review','reviewer')['counts']['released']==1


def test_concurrent_same_parent_mutates_each_case_once(db,case_factory):
    items=[case_factory('race-a'),case_factory('race-b')]
    barrier=Barrier(2)
    def run():
        barrier.wait(timeout=5)
        return bulk_release_cases(db,items,REASON,'same-parent','reviewer')
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(run) for _ in range(2)]
        results=[future.result(timeout=10) for future in futures]
    assert results[0]==results[1]
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='case_reviewed'))==2
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='bulk_review_started'))==1
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='bulk_review_completed'))==1
    assert counts(db)['state:ELIGIBLE']==2


def test_waiting_reviewer_refreshes_case_before_overwriting_outcome(db,case_factory,monkeypatch):
    item=case_factory('review-race')
    real_lock=assessment.lock
    waiting=Event();first_done=Event();thread=local()
    def controlled_lock(s,key):
        if key.startswith('customer-period:'):
            if thread.role=='second':
                waiting.set();assert first_done.wait(timeout=5)
            else:assert waiting.wait(timeout=5)
        return real_lock(s,key)
    monkeypatch.setattr(assessment,'lock',controlled_lock)
    def first():
        thread.role='first'
        try:return resolve_case(db,item['case_id'],1,'VERIFIED_VALID',REASON,'review-first','first-reviewer')
        finally:first_done.set()
    def second():
        thread.role='second'
        with pytest.raises(Conflict,match='not open'):
            resolve_case(db,item['case_id'],1,'CONFIRMED_ISSUE',REASON,'review-second','second-reviewer')
    with ThreadPoolExecutor(max_workers=2) as executor:
        second_future=executor.submit(second);first_future=executor.submit(first)
        assert first_future.result(timeout=10)['movement_state']=='ELIGIBLE'
        second_future.result(timeout=10)
    with db.transaction() as s:
        case=s.get(ReviewCase,item['case_id'])
        assert case.outcome=='VERIFIED_VALID' and case.reviewed_by=='first-reviewer'
        assert s.get(Movement,'movement-review-race').state=='ELIGIBLE'


@pytest.mark.parametrize('kwargs',[{'current':2,'case_version':1},{'status':'RESOLVED'}])
def test_correction_rejects_old_or_closed_case(db,case_factory,kwargs):
    item=case_factory('invalid-correction',**kwargs)
    with pytest.raises(Conflict):
        correct_case(db,item['case_id'],item['expected_version'],{'description':'Changed'},REASON,'bad-correction','reviewer')
    with db.transaction() as s:
        assert s.get(Movement,'movement-invalid-correction').current_version==kwargs.get('current',1)


def test_savepoint_domain_failure_restores_operational_counter_deltas(db,case_factory,monkeypatch):
    item=case_factory('rollback')
    before=counts(db)
    def changed_then_rejected(s,*args,**kwargs):
        movement=s.get(Movement,'movement-rollback')
        change_state(s,movement,'ELIGIBLE');s.flush()
        raise Blocked('Synthetic rejection after tentative state change')
    monkeypatch.setattr(bulk_review,'_resolve_case_in_session',changed_then_rejected)
    result=bulk_release_cases(db,[item],REASON,'rollback-parent','reviewer')
    assert result['counts']['failed']==1
    assert result['items'][0]['movement_state']=='REVIEW'
    assert counts(db)==before


@pytest.mark.parametrize('items,reason',[
    ([],REASON),([{'case_id':'x','expected_version':1}]*101,REASON),
    ([{'case_id':'x','expected_version':1}]*2,REASON),
    ([{'case_id':'x','expected_version':True}],REASON),
    ([{'case_id':'x','expected_version':0}],REASON),
    ([{'case_id':'x','expected_version':'1'}],REASON),
    ([{'case_id':'x','expected_version':1,'outcome':'CONFIRMED_ISSUE'}],REASON),
    ([{'case_id':'x','expected_version':1}],'four'),
    ([{'case_id':'x','expected_version':1}],'x'*2001),
])
def test_invalid_bulk_envelope_has_no_partial_effects(db,items,reason):
    with pytest.raises(ValueError):bulk_release_cases(db,items,reason,'invalid-parent','reviewer')
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Command))==0
        assert s.scalar(select(func.count()).select_from(Audit))==0
