"""Real PostgreSQL API gates and live-cache convergence under concurrent commits."""
from copy import deepcopy

from fastapi.testclient import TestClient
from sqlalchemy import select, func

from invoice_optimizer.api import create_app, SummaryCache, changed_resources, ALL_RESOURCES
from invoice_optimizer.db import audit
from invoice_optimizer.models import Audit, Batch, Counter, Invoice, Outbox, SourceRecord
from test_operators_api import bootstrap, headers, add_user, PASSWORD
from test_automation_operations import customer, source, jobs, change
from test_bulk_review import case_factory


def test_automation_http_identity_csrf_revision_and_audit(db):
    app=create_app(db.settings,db)
    body={'patch':{'paused':True},'expected_revision':0,
          'request_id':'http-settings','reason':'Pause for controlled verification'}
    with TestClient(app) as admin, TestClient(app) as reviewer, TestClient(app) as shared:
        who=bootstrap(admin,db);add_user(admin,who)
        other=reviewer.post('/api/login',json={'username':'bob','password':PASSWORD}).json()
        legacy=shared.post('/api/login',json={'token':db.settings.api_token}).json()
        assert shared.get('/api/automation').status_code==200
        assert shared.patch('/api/automation',json=body,headers=headers(legacy)).status_code==403
        assert reviewer.patch('/api/automation',json=body,headers=headers(other)).status_code==403
        assert admin.patch('/api/automation',json=body).status_code==403
        response=admin.patch('/api/automation',json=body,headers=headers(who))
        assert response.status_code==200,response.text
        assert response.json()['revision']==1
        assert response.json()['effective']['processing_enabled'] is False
        assert admin.patch('/api/automation',json=body,headers=headers(who)).json()==response.json()
        assert admin.patch('/api/automation',json={**body,'request_id':'stale'},headers=headers(who)).status_code==409
        assert admin.patch('/api/automation',json={**body,'patch':{'automatic_approval':True}},headers=headers(who)).status_code==422
        assert admin.patch('/api/automation',json={**body,'expected_revision':True},headers=headers(who)).status_code==422
        assert admin.get('/api/dashboard').json()['automation_control']['paused'] is True
    with db.transaction() as session:
        events=list(session.scalars(select(Audit).where(Audit.action=='automation_settings_changed')))
        assert len(events)==1 and events[0].actor==who['actor']
        assert session.scalar(select(func.count()).select_from(Invoice))==0
        assert session.scalar(select(func.count()).select_from(Outbox))==0


def test_ingest_pressure_http_is_retryable_and_creates_no_batch(db,tmp_path):
    customer(db);jobs(db,100);change(db,{'queue_high_watermark':100})
    path=source(tmp_path/'pressure.csv')
    with TestClient(create_app(db.settings,db)) as client:
        who=bootstrap(client,db)
        response=client.post('/api/ingest',headers=headers(who),
            files={'file':('pressure.csv',path.read_bytes(),'text/csv')},
            data={'source':'A','period':'2026-09','expected_rows':'1','complete':'false'})
        assert response.status_code==429,response.text
        assert response.headers['Retry-After']=='5' and response.json()['retryable'] is True
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Batch))==0
        assert session.scalar(select(func.count()).select_from(SourceRecord))==0
    assert not list((db.settings.data_dir/'incoming').iterdir())


def test_versions_endpoint_is_bounded_and_read_only(db,case_factory):
    item=case_factory('http-versions')
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        path='/api/cases/'+item['case_id']+'/versions'
        value=client.get(path)
        assert value.status_code==200,value.text
        assert len(value.json()['items'])==1 and value.json()['current_version']==1
        assert client.get(path,params={'limit':11}).status_code==422
        assert client.get(path,params={'cursor':'invalid-cursor'}).status_code==422
        assert client.get('/api/cases/absent/versions').status_code==404
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Audit))==0


def test_cache_converges_when_lower_audit_id_commits_after_higher_id(db):
    # PostgreSQL sequence allocation order is not transaction commit order.
    low=db.Session()
    try:
        low.begin()
        audit(low,'worker','rules_evaluated','late',{})
        low.flush()
        low.add(Counter(name='movements',value=7))
        low.flush()
        with db.transaction() as high:audit(high,'worker','job_claimed','early',{})
        cache=SummaryCache(db)
        before=deepcopy(cache.get())
        assert before['counts']['movements']==0
        low.commit()
        cache.checked=0
        after=cache.get()
        assert after['revision']==before['revision']
        assert after['counts']['movements']==7
        assert after['stream_version']!=before['stream_version']
        assert set(after['changed_resources'])==set(ALL_RESOURCES)
    finally:
        low.rollback();low.close()


def test_cache_shared_reads_and_worker_status_change_without_audit(db,monkeypatch):
    import invoice_optimizer.api as api
    actual=api.dashboard;calls=[];worker={'status':'RUNNING','stale':False,'worker_id':'synthetic-worker'}
    monkeypatch.setattr(api,'worker_info',lambda _:deepcopy(worker))
    def measured(database):calls.append(1);return actual(database)
    monkeypatch.setattr(api,'dashboard',measured)
    cache=SummaryCache(db);first=cache.get()
    assert cache.get() is first and len(calls)==1
    worker.update(status='STALE',stale=True)
    cache.checked=0;second=cache.get()
    assert first['revision']==second['revision']==0
    assert first['stream_version']!=second['stream_version']
    cache.checked=0;third=cache.get()
    assert third['stream_version']==second['stream_version']
    assert third['changed_resources']==[]


def test_resource_hints_are_conservative_and_target_common_events():
    assert changed_resources(['job_claimed'])==['audit','automation']
    assert set(changed_resources(['rules_evaluated']))>={'cases','invoices','outbox','automation'}
    assert changed_resources(['future_unknown_event'])==ALL_RESOURCES
    assert 'outbox' in changed_resources(['case_correction_applied'])


def test_periodic_revalidation_catches_detail_changes_without_count_or_revision_progress(db):
    cache=SummaryCache(db);first=cache.get()
    cache.checked=0;cache.last_full_invalidation-=16
    second=cache.get()
    assert first['revision']==second['revision']
    assert first['counts']==second['counts']
    assert first['stream_version']!=second['stream_version']
    assert set(second['changed_resources'])==set(ALL_RESOURCES)
