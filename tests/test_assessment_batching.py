"""Bounded PostgreSQL persistence, including fault recovery across alert batches.

SQL measurements count DBAPI cursor executions, not individual server statements
inside executemany. Numerical inference is stubbed to isolate persistence costs.
"""
from contextlib import contextmanager
from collections import Counter as Counts
import csv

import pytest
from sqlalchemy import event, func, select

from invoice_optimizer import assessment
from invoice_optimizer.db import digest
from invoice_optimizer.ingestion import ingest_file
from invoice_optimizer.models import (Assessment, Audit, Counter, Customer, DeliveryAttempt,
                                      Job, Movement, MovementVersion, Outbox, ReviewCase)


@contextmanager
def cursor_calls(db):
    calls=[]
    def observe(connection,cursor,statement,parameters,context,executemany):
        calls.append((statement.lstrip().split(None,1)[0].upper(),executemany))
    event.listen(db.engine,'before_cursor_execute',observe)
    try:yield calls
    finally:event.remove(db.engine,'before_cursor_execute',observe)


def fixture(db,tmp_path,count,prefix='batch',rule_reasons=1):
    customer_id='CUSTOMER-'+prefix
    with db.transaction() as session:
        session.add(Customer(id=customer_id,legal_name='Synthetic batching customer',address='Test address',
                             email='accounts@example.test',alert_email='review@example.test'))
    results=[]
    for side in ('A','B'):
        rows=[]
        for index in range(count):
            rows.append(dict(record_id=f'{prefix}-R{index}',revision=1,movement_id=f'{prefix}-M{index}',
                customer_id=customer_id,event_at='2026-09-10T12:00:00Z' if side=='A' or rule_reasons<3 else '2026-09-11T12:00:00Z',
                period='2026-09',currency='GBP',amount='10.00' if side=='A' else '11.00',description='Synthetic batching row',
                supplier_id='SUP-A' if side=='A' or rule_reasons<2 else 'SUP-B',expected_amount='10.00',
                customer_age_days=1000,supplier_age_days=1000,cross_border=0))
        path=tmp_path/f'{prefix}-{side}.csv'
        with path.open('w',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
        results.append(ingest_file(db,path,side,'2026-09',count,False))
    with db.transaction() as session:
        job=session.scalar(select(Job).where(Job.dedup_key.like('ingest:'+results[0]['batch_id']+':%')))
        return job.id,customer_id


def model_stub(payloads,model_dir):
    # Alternating availability catches ORM INSERT grouping by nullable columns.
    return [dict(status='SCORED',score=.99,flagged=True,model_version='batch-test-model',
                 policy_version='batch-test-policy',feature_schema_version='batch-test-features',
                 feature_values={'example':1.0}) if index%2==0 else
            dict(status='UNAVAILABLE',score=None,flagged=False,reason='Controlled unavailable evidence')
            for index in range(len(payloads))]


def score_job_id(db,reconcile_id):
    with db.transaction() as session:
        return session.scalar(select(Job.id).where(Job.dedup_key=='score:'+reconcile_id))


def counters_match(db):
    with db.transaction() as session:
        states=dict(session.execute(select(Movement.state,func.count()).group_by(Movement.state)).all())
        counters={row.name:row.value for row in session.scalars(select(Counter))}
        assert counters['movements']==sum(states.values())
        for state in set(states)|{key[6:] for key in counters if key.startswith('state:')}:
            assert counters.get('state:'+state,0)==states.get(state,0)


def test_sql_cursor_growth_is_bounded_for_100_and_200_movements_with_mixed_scores(db,tmp_path,monkeypatch):
    monkeypatch.setattr('invoice_optimizer.ml.score_batch',model_stub)
    observations={}
    for count in (100,200):
        job_id,customer_id=fixture(db,tmp_path,count,prefix=str(count))
        with cursor_calls(db) as rule_calls:assessment.reconcile_job(db,job_id)
        model_job_id=score_job_id(db,job_id)
        with cursor_calls(db) as score_calls:assessment.score_job(db,model_job_id)
        observations[count]={'rule':Counts(kind for kind,_ in rule_calls),'model':Counts(kind for kind,_ in score_calls)}
        with db.transaction() as session:
            versions=select(MovementVersion.id).join(Movement).where(Movement.customer_id==customer_id)
            assert session.scalar(select(func.count()).select_from(Assessment).where(Assessment.version_id.in_(versions)))==2*count
            assert session.scalar(select(func.count()).select_from(ReviewCase).where(ReviewCase.customer_id==customer_id))==2*count
            assert session.scalar(select(func.count()).select_from(Movement).where(Movement.customer_id==customer_id,Movement.state=='BLOCKED'))==count
        counters_match(db)
    print('Cursor calls by stage and batch size:',observations)
    for stage in ('rule','model'):
        assert observations[200][stage]['SELECT']<=observations[100][stage]['SELECT']+2
        assert sum(observations[200][stage].values())<=sum(observations[100][stage].values())+6
        assert sum(observations[200][stage].values())<40


def test_rule_second_alert_batch_failure_rolls_back_every_case_version_and_counter(db,tmp_path):
    job_id,customer_id=fixture(db,tmp_path,200,rule_reasons=3)
    inserted_alert_batches=[]
    def fail_second_alert_batch(connection,cursor,statement,parameters,context,executemany):
        if statement.lstrip().upper().startswith('INSERT INTO OUTBOX'):
            inserted_alert_batches.append(1)
            if len(inserted_alert_batches)==2:raise RuntimeError('Controlled second alert batch failure')
    event.listen(db.engine,'before_cursor_execute',fail_second_alert_batch)
    try:
        with pytest.raises(RuntimeError,match='second alert batch'):assessment.reconcile_job(db,job_id)
    finally:event.remove(db.engine,'before_cursor_execute',fail_second_alert_batch)
    assert len(inserted_alert_batches)==2
    with db.transaction() as session:
        for model in (MovementVersion,Assessment,ReviewCase,Outbox):
            assert session.scalar(select(func.count()).select_from(model))==0
        assert session.get(Job,job_id).status=='READY'
        assert session.scalar(select(func.count()).select_from(Movement).where(Movement.current_version==0,Movement.state=='PENDING'))==200
        assert session.scalar(select(Audit).where(Audit.action=='rules_evaluated')) is None
    counters_match(db)
    assessment.reconcile_job(db,job_id)
    assessment.reconcile_job(db,job_id)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(MovementVersion))==200
        assert session.scalar(select(func.count()).select_from(Assessment))==200
        assert session.scalar(select(func.count()).select_from(ReviewCase))==600
        assert session.scalar(select(func.count()).select_from(Outbox))==600
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind=='SCORE'))==1
        assert session.scalar(select(func.count()).select_from(Audit).where(Audit.action=='rules_evaluated'))==1
        assert session.scalar(select(func.count()).select_from(DeliveryAttempt))==0
    counters_match(db)


def test_model_alert_failure_preserves_committed_rules_and_retry_is_exactly_once(db,tmp_path,monkeypatch):
    monkeypatch.setattr('invoice_optimizer.ml.score_batch',model_stub)
    job_id,customer_id=fixture(db,tmp_path,200)
    assessment.reconcile_job(db,job_id)
    model_job_id=score_job_id(db,job_id)
    with db.transaction() as session:
        before_cases={row.id:(row.reason,row.status,row.version_id) for row in session.scalars(select(ReviewCase))}
        before_outbox={row.id:digest(row.payload) for row in session.scalars(select(Outbox))}
    def fail_alert_batch(connection,cursor,statement,parameters,context,executemany):
        if statement.lstrip().upper().startswith('INSERT INTO OUTBOX'):
            raise RuntimeError('Controlled model alert failure')
    event.listen(db.engine,'before_cursor_execute',fail_alert_batch)
    try:
        with pytest.raises(RuntimeError,match='model alert failure'):assessment.score_job(db,model_job_id)
    finally:event.remove(db.engine,'before_cursor_execute',fail_alert_batch)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Assessment).where(Assessment.kind=='MODEL'))==0
        assert {row.id:(row.reason,row.status,row.version_id) for row in session.scalars(select(ReviewCase))}==before_cases
        assert {row.id:digest(row.payload) for row in session.scalars(select(Outbox))}==before_outbox
        assert session.get(Job,model_job_id).status=='READY'
    assessment.score_job(db,model_job_id)
    assessment.score_job(db,model_job_id)
    with db.transaction() as session:
        assert dict(session.execute(select(Assessment.status,func.count()).where(Assessment.kind=='MODEL').group_by(Assessment.status)).all())=={'SCORED':100,'UNAVAILABLE':100}
        assert session.scalar(select(func.count()).select_from(ReviewCase))==400
        assert session.scalar(select(func.count()).select_from(Outbox))==400
        assert session.scalar(select(func.count()).select_from(Audit).where(Audit.action=='model_evaluated'))==1
        assert session.scalar(select(func.count()).select_from(DeliveryAttempt))==0
    counters_match(db)
