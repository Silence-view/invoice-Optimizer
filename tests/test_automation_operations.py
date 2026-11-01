"""Durable operations and bounded throughput tests in disposable PostgreSQL schemas."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta, date
import csv

import pytest
from sqlalchemy import select, func

from invoice_optimizer import worker, scheduler
from invoice_optimizer.db import Blocked, Conflict, digest
from invoice_optimizer.delivery import send_one
from invoice_optimizer.ingestion import ingest_file
from invoice_optimizer.models import (Audit, Batch, Customer, DeliveryAttempt, Job,
                                      Movement, Outbox, RuntimeOption, SourceRecord, now)
from invoice_optimizer.operations import (AUTOMATION_KEY, Backpressure, get_automation,
                                          update_automation, check_ingestion_admission)
from invoice_optimizer.operators import Operator


def change(db, patch, revision=0, request='settings-1', actor='operations-test-admin'):
    return update_automation(db, patch, revision, request, actor, 'Controlled isolated test')


def customer(db, name='C1'):
    with db.transaction() as session:
        session.add(Customer(id=name, legal_name='Synthetic throughput customer', address='Test address',
                             email='accounts@example.test', alert_email='review@example.test'))


def source(path, count=1, revision=1, changed_identity=False):
    records=[dict(record_id=f'r{i}', revision=revision, movement_id=f'm{i}' if not changed_identity else f'changed{i}',
                  customer_id='C1', event_at='2026-09-10T12:00:00Z', period='2026-09', currency='GBP',
                  amount='10.00', description='Synthetic throughput row', supplier_id='S1',
                  expected_amount='10.00',customer_age_days='1000',supplier_age_days='1000',cross_border='0')
             for i in range(count)]
    with path.open('w',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=records[0]);writer.writeheader();writer.writerows(records)
    return path


def jobs(db, count=1, kind='ASSESS', status='READY'):
    stamp=now()-timedelta(hours=1)
    with db.transaction() as session:
        session.add_all(Job(id=f'{kind}-{index}',dedup_key=f'{kind}-{index}',kind=kind,period='2026-09',
                            payload={'movement_ids':[]}, status=status,created_at=stamp+timedelta(microseconds=index),
                            lease_until=stamp if status=='RUNNING' else None)
                        for index in range(count))


def done_handler(calls):
    def handle(db, job_id, token):
        with db.transaction() as session:
            job=session.get(Job,job_id,with_for_update=True)
            assert job.status=='RUNNING' and job.claim_token==token
            calls.append(job.kind);job.status='DONE';job.lease_until=None
    return handle


def test_read_defaults_has_no_database_side_effects(db):
    value=get_automation(db)
    assert value['revision']==0 and value['config']['paused'] is False
    assert value['effective']['processing_enabled'] and value['effective']['email_delivery_enabled']
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(RuntimeOption))==0
        assert session.scalar(select(func.count()).select_from(Audit))==0


def test_actor_bound_revision_update_audits_once_and_replays(db):
    result=change(db,{'paused':True,'jobs_per_tick':3})
    assert result['revision']==1 and result['effective']['processing_enabled'] is False
    assert result==change(db,{'paused':True,'jobs_per_tick':3})
    with pytest.raises(Conflict,match='different content'):
        change(db,{'paused':True,'jobs_per_tick':3},actor='someone-else')
    with pytest.raises(Conflict,match='changed'):
        change(db,{'paused':False},request='stale-revision')
    with db.transaction() as session:
        events=list(session.scalars(select(Audit).where(Audit.action=='automation_settings_changed')))
        assert len(events)==1 and events[0].id==result['audit_event_id']
        assert events[0].actor=='operations-test-admin'
        assert events[0].details['reason']=='Controlled isolated test'
        assert events[0].details['before']['paused'] is False
        assert events[0].details['after']['paused'] is True
        assert events[0].details['human_approval_required'] is True


def test_registered_reviewer_cannot_change_settings_but_admin_can(db):
    with db.transaction() as session:
        session.add_all([Operator(id='a'*32,username='reviewer',display_name='Review Operator',password_hash='not-used',role='reviewer'),
                         Operator(id='b'*32,username='admin',display_name='Admin Operator',password_hash='not-used',role='admin')])
    with pytest.raises(Blocked,match='administrator'):
        change(db,{'paused':True},actor='user:'+'a'*32)
    result=change(db,{'paused':True},actor='user:'+'b'*32)
    assert result['updated_by']['identity_kind']=='REGISTERED_OPERATOR'
    assert result['updated_by']['display_name']=='Admin Operator'


def test_concurrent_setting_writes_only_one_expected_revision_wins(db):
    def update(index):
        try:return change(db,{'jobs_per_tick':index+1},request=f'concurrent-{index}')
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(update,[1,2]))
    assert sum(result is not None for result in results)==1
    assert get_automation(db)['revision']==1


@pytest.mark.parametrize('patch',[{'paused':1},{'jobs_per_tick':True},{'jobs_per_tick':101},
                                  {'emails_per_tick':-1},{'queue_high_watermark':99},{'automatic_approval':True},{}])
def test_invalid_settings_rejected_without_partial_write(db,patch):
    with pytest.raises(ValueError):change(db,patch)
    assert get_automation(db)['revision']==0


def test_pause_blocks_direct_worker_transport_and_tick_then_resume(db, monkeypatch):
    jobs(db)
    with db.transaction() as session:
        session.add(Outbox(id='alert',dedup_key='alert',kind='ALERT',payload={
            'from':'operations@example.test','to':['review@example.test'],'subject':'Synthetic test','body':'Test only'}))
    calls=[];monkeypatch.setattr(worker,'reconcile_job',done_handler(calls))
    change(db,{'paused':True})
    assert worker.run_job(db) is False
    assert send_one(db) is False
    assert worker.tick(db)=={'jobs':0,'emails':0,'uncertain_recovered':0}
    assert scheduler.prepare_due_invoices(db,date(2026,10,1))['enabled'] is False
    assert scheduler.dispatch_approved(db)['enabled'] is False
    with db.transaction() as session:
        assert session.get(Job,'ASSESS-0').status=='READY'
        assert session.get(Outbox,'alert').status=='READY'
        assert session.scalar(select(func.count()).select_from(DeliveryAttempt))==0
    change(db,{'paused':False,'email_delivery_enabled':False},1,'resume-processing')
    assert worker.run_job(db) is True and calls==['ASSESS']
    assert send_one(db) is False
    # Re-enable the isolated, local simulator; no external transport is used.
    change(db,{'email_delivery_enabled':True},2,'resume-mail')
    assert send_one(db) is True
    with db.transaction() as session:
        assert session.get(Outbox,'alert').status=='ACCEPTED'
        assert session.scalar(select(func.count()).select_from(DeliveryAttempt))==1


def test_tick_honors_configured_budget_and_does_not_override_approval(db,monkeypatch):
    jobs(db,5)
    calls=[];monkeypatch.setattr(worker,'reconcile_job',done_handler(calls))
    change(db,{'jobs_per_tick':2,'emails_per_tick':0})
    assert worker.tick(db,max_jobs=100,max_emails=100)['jobs']==2
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(Job.status=='READY'))==3
        assert session.scalar(select(func.count()).select_from(Outbox))==0


def test_backlog_cannot_starve_scoring_and_rendering(db,monkeypatch):
    jobs(db,50);jobs(db,1,'SCORE');jobs(db,1,'RENDER')
    calls=[]
    for handler in ('reconcile_job','score_job','render_job'):monkeypatch.setattr(worker,handler,done_handler(calls))
    for _ in range(3):assert worker.run_job(db,kinds=('ASSESS','SCORE','RENDER'))
    assert calls==['ASSESS','SCORE','RENDER']


def test_expired_recovery_is_bounded_and_claims_are_unique_under_concurrency(db,monkeypatch):
    jobs(db,100,status='RUNNING')
    calls=[];monkeypatch.setattr(worker,'reconcile_job',done_handler(calls))
    assert worker.run_job(db,kinds=('ASSESS',))
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(Job.status=='RUNNING'))==68
        assert session.scalar(select(func.count()).select_from(Audit).where(Audit.action=='job_lease_recovered'))==32
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert all(pool.map(lambda _:worker.run_job(db,kinds=('ASSESS',)),range(3)))
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(Job.status=='DONE'))==4
        claimed=list(session.scalars(select(Audit.entity_id).where(Audit.action=='job_claimed')))
        assert len(claimed)==len(set(claimed))==4


def test_backpressure_rejection_has_no_batch_or_business_side_effect_then_retry(db,tmp_path):
    customer(db);jobs(db,100)
    change(db,{'queue_high_watermark':100})
    path=source(tmp_path/'delivery.csv')
    with pytest.raises(Backpressure) as rejected:ingest_file(db,path,'A','2026-09',1,True)
    assert rejected.value.retry_after==5
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Batch))==0
        assert session.scalar(select(func.count()).select_from(SourceRecord))==0
        assert session.scalar(select(func.count()).select_from(Movement))==0
        assert session.scalar(select(Audit).where(Audit.action=='batch_failed')) is None
        for job in session.scalars(select(Job)):job.status='DONE'
    result=ingest_file(db,path,'A','2026-09',1,True)
    assert result['status']=='COMPLETE' and result['accepted']==1
    with db.transaction() as session:
        for job in session.scalars(select(Job)):job.status='READY'
    assert ingest_file(db,path,'A','2026-09',1,True)['replayed'] is True


def test_declared_running_deliveries_reserve_remaining_queue_work(db):
    change(db,{'queue_high_watermark':100})
    with db.transaction() as session:
        session.add(Batch(id='reserved',source='A',period='2026-09',object_key='a'*64,sha256='a'*64,
                          original_name='reserved.csv',expected_rows=50000,row_count=0,status='RUNNING'))
    with db.transaction() as session:
        with pytest.raises(Backpressure):check_ingestion_admission(session,db.settings,1)
    with db.transaction() as session:
        session.get(Batch,'reserved').row_count=1000
    with db.transaction() as session:
        admission=check_ingestion_admission(session,db.settings,1)
        assert admission['pending_and_reserved_jobs']==98 and admission['requested_jobs_upper_bound']==2


def test_ingestion_creates_bounded_jobs_and_retains_all_source_rows(db,tmp_path,monkeypatch):
    monkeypatch.setattr(db,'settings',replace(db.settings,worker_batch_size=200))
    customer(db)
    result=ingest_file(db,source(tmp_path/'bounded.csv',1200),'A','2026-09',1200,True)
    assert result['accepted']==1200
    with db.transaction() as session:
        payloads=list(session.scalars(select(Job.payload)))
        assert len(payloads)==6 and max(len(item['movement_ids']) for item in payloads)==200
        assert len({mid for item in payloads for mid in item['movement_ids']})==1200
        assert session.scalar(select(func.count()).select_from(SourceRecord))==1200
        assert session.get(Batch,result['batch_id']).row_count==1200


def test_latest_revision_lookup_still_rejects_changed_identity(db,tmp_path):
    customer(db)
    for revision in range(1,5):
        result=ingest_file(db,source(tmp_path/f'revision-{revision}.csv',revision=revision),'A','2026-09',1,True)
        assert result['accepted']==1
    bad=ingest_file(db,source(tmp_path/'identity-change.csv',revision=5,changed_identity=True),'A','2026-09',1,True)
    assert bad['accepted']==0 and bad['quarantined']==1
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SourceRecord))==4
        assert session.scalar(select(func.count()).select_from(Movement))==1


def test_scheduler_ignores_customer_period_pairs_without_movements(db,monkeypatch):
    customer(db,'C1');customer(db,'C2')
    with db.transaction() as session:
        session.add(Batch(id='complete',source='A',period='2026-09',object_key='b'*64,sha256='b'*64,
                          original_name='source.csv',status='COMPLETE',expected_rows=1,row_count=1,declared_complete=True))
        session.add(Movement(id='one',external_id='one',customer_id='C2',period='2026-09',state='PENDING'))
    calls=[]
    monkeypatch.setattr(scheduler,'create_invoice',lambda db,cid,period,request,actor:
                        calls.append((cid,period)) or {'id':'selection-only','status':'BUILDING'})
    result=scheduler.prepare_due_invoices(db,date(2026,10,1))
    assert result['attempted']==1 and calls==[('C2','2026-09')]


def test_pause_during_tick_stops_next_claim_without_reverting_finished_work(db,monkeypatch):
    jobs(db,3)
    calls=[]
    complete=done_handler(calls)
    def finish_then_pause(database,job_id,token):
        complete(database,job_id,token)
        change(database,{'paused':True})
    monkeypatch.setattr(worker,'reconcile_job',finish_then_pause)
    assert worker.tick(db,max_jobs=3,max_emails=0)['jobs']==1
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(Job.status=='DONE'))==1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.status=='READY'))==2


def test_concurrent_declared_admissions_share_one_capacity_reservation(db):
    change(db,{'queue_high_watermark':100})
    def reserve(index):
        try:
            with db.transaction() as session:
                check_ingestion_admission(session,db.settings,30000)
                session.add(Batch(id=f'admitted-{index}',source='A',period='2026-09',object_key=digest(index),
                                  sha256=digest(index),original_name='controlled.csv',expected_rows=30000,
                                  row_count=0,status='RUNNING'))
            return True
        except Backpressure:return False
    with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(reserve,[1,2]))
    assert outcomes.count(True)==1
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Batch))==1


@pytest.mark.parametrize('field,value',[('worker_batch_size',0),('worker_batch_size',10001),
                                         ('worker_batch_size',True),('poll_seconds',0),
                                         ('poll_seconds',float('nan')),('poll_seconds',61)])
def test_invalid_worker_settings_fail_before_start(db,field,value):
    with pytest.raises(ValueError):replace(db.settings,**{field:value}).prepare()
