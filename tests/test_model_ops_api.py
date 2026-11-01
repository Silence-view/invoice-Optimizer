"""Actual bounded training and frozen-model inspection on disposable PostgreSQL."""
import csv
import io
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func, event

from invoice_optimizer.api import create_app
from invoice_optimizer.db import Blocked, Conflict
from invoice_optimizer.model_ops import ModelTrainingRun, queue_training, run_training_job, direct_train
from invoice_optimizer.models import Assessment, Audit, Job, Movement, MovementVersion, ReviewCase, RuntimeOption, now
from invoice_optimizer.ml import train_model, score_batch, model_readiness
from invoice_optimizer.synthetic import generate_training_data
from invoice_optimizer.worker import run_job
from test_bulk_review import case_factory
from test_operators_api import bootstrap, headers, add_user, PASSWORD


@pytest.fixture
def actual_model(db,tmp_path):
    path=tmp_path/'real-training.parquet'
    generate_training_data(path,rows=2000,seed=763,serving_period='2026-09')
    return train_model(path,db.settings.model_dir)


@pytest.fixture
def actual_case(db,case_factory,actual_model):
    item=case_factory('actual-explanation')
    with db.transaction() as s:
        version=s.get(MovementVersion,'version-actual-explanation-1')
        payload={**version.payload,'customer_age_days':200,'supplier_age_days':300,'cross_border':0,
                 'event_at':'2026-09-10T12:00:00+00:00'}
        result=score_batch([payload],db.settings.model_dir)[0]
        assert result['status']=='SCORED'
        version.payload=payload
        assessment=s.scalar(select(Assessment).where(Assessment.version_id==version.id,Assessment.kind=='MODEL'))
        assessment.score=result['score'];assessment.features=result['feature_values']
        assessment.model_version=result['model_version'];assessment.policy_version=result['policy_version']
        assessment.feature_schema_version=result['feature_schema_version'];assessment.flagged=result['flagged']
    return item


def test_actual_training_queue_worker_receipt_and_future_only_activation(db):
    queued=queue_training(db,2000,42,'2026-09','actual-run','test:training-requester')
    assert queued['status']=='QUEUED' and queued['requester']['actor_id']=='test:training-requester'
    assert not (db.settings.model_dir/'metadata.json').exists()
    assert queue_training(db,2000,42,'2026-09','actual-run','test:training-requester')==queued
    with pytest.raises(Conflict):queue_training(db,2000,43,'2026-09','actual-run','test:training-requester')
    with pytest.raises(Blocked):queue_training(db,2000,43,'2026-09','other-run','test:training-requester')
    assert run_job(db,kinds=('TRAIN_MODEL',))
    with db.transaction() as s:
        run=s.get(ModelTrainingRun,queued['id']);job=s.get(Job,run.job_id)
        assert run.status=='SUCCEEDED' and job.status=='DONE'
        assert run.started_at and run.finished_at and run.result['historical_scores_changed'] is False
        assert run.result['metadata']['training_rows']==2000
        assert run.result['metadata']['test_metrics']['rows']>0
        assert run.training_sha256==run.result['metadata']['training_sha256']
        assert s.scalar(select(func.count()).select_from(Movement))==0
        assert s.scalar(select(func.count()).select_from(Assessment))==0
    assert model_readiness(db.settings.model_dir)['metadata']['model_version']==run.model_version
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        runs=client.get('/api/model/runs').json();assert runs[0]['status']=='SUCCEEDED'
        import hashlib
        data=client.get(runs[0]['training_download_url'])
        assert data.status_code==200 and hashlib.sha256(data.content).hexdigest()==run.training_sha256
        manifest=client.get(runs[0]['manifest_download_url'])
        assert manifest.status_code==200 and hashlib.sha256(manifest.content).hexdigest()==run.manifest_sha256


def test_training_admission_is_single_and_claim_token_is_fenced(db):
    barrier=Barrier(2)
    def enqueue(number):
        barrier.wait(timeout=5)
        try:return queue_training(db,1000,number,'2026-09','concurrent-'+str(number),'test:actor')['id']
        except Blocked:return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(enqueue,[1,2]))
    assert results.count('blocked')==1
    run_id=next(value for value in results if value!='blocked')
    with db.transaction() as s:
        run=s.get(ModelTrainingRun,run_id);job=s.get(Job,run.job_id)
        job.status='RUNNING';job.claim_token='current-token';job_id=job.id
    assert run_training_job(db,job_id,'stale-token') is None
    assert not (db.settings.model_dir/'training'/run_id).exists()
    with db.transaction() as s:assert s.get(ModelTrainingRun,run_id).status=='QUEUED'


def test_training_failure_is_visible_and_preserves_active_model(db,actual_model,monkeypatch):
    import invoice_optimizer.ml as ml
    queued=queue_training(db,1000,20,'2026-09','failed-run','test:actor')
    def fail(*args,**kwargs):raise ValueError('Deliberate fixture training failure')
    monkeypatch.setattr(ml,'train_model',fail)
    assert run_job(db,kinds=('TRAIN_MODEL',))
    with db.transaction() as s:
        run=s.get(ModelTrainingRun,queued['id'])
        assert run.status=='FAILED' and 'Deliberate fixture' in run.error
        assert s.get(Job,run.job_id).status=='FAILED'
    assert model_readiness(db.settings.model_dir)['metadata']['model_version']==actual_model['model_version']
    assert queue_training(db,1000,21,'2026-09','next-run','test:actor')['status']=='QUEUED'


def test_training_api_requires_admin_and_remains_asynchronous(db):
    app=create_app(db.settings,db)
    with TestClient(app) as admin,TestClient(app) as reviewer:
        a=bootstrap(admin,db);add_user(admin,a)
        b=reviewer.post('/api/login',json={'username':'bob','password':PASSWORD}).json()
        body={'rows':1000,'seed':42,'period':'2026-09','request_id':'api-train'}
        assert reviewer.post('/api/model/train',json=body,headers=headers(b)).status_code==403
        assert admin.post('/api/model/train',json=body).status_code==403
        for change in ({'rows':999},{'rows':50001},{'rows':True},{'seed':-1},{'period':'2026-13'}):
            assert admin.post('/api/model/train',json={**body,**change},headers=headers(a)).status_code==422
        response=admin.post('/api/model/train',json=body,headers=headers(a))
        assert response.status_code==202 and response.json()['status']=='QUEUED'
        assert response.json()['actor']==a['actor']
        assert admin.get('/api/model/runs?limit=21').status_code==422
        assert not (db.settings.model_dir/'metadata.json').exists()


def test_readiness_requires_valid_artifact_and_worker_status_is_fresh(db,actual_model):
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        model=client.get('/api/model').json()
        assert model['status']=='READY' and model['artifact_integrity_verified']
        assert model['worker']['status']=='UNAVAILABLE'
        with db.transaction() as s:s.add(RuntimeOption(key='worker.heartbeat',value={'at':(now()-timedelta(seconds=30)).isoformat(),'status':'RUNNING'}))
        assert client.get('/api/model').json()['worker']['status']=='STALE'
        with db.transaction() as s:s.get(RuntimeOption,'worker.heartbeat').value={'at':now().isoformat(),'status':'RUNNING'}
        assert client.get('/api/model').json()['worker']['status']=='RUNNING'
        artifact=db.settings.model_dir/actual_model['artifact_path'];artifact.write_bytes(b'corrupt model')
        broken=client.get('/api/model').json()
        assert broken['status']=='UNAVAILABLE' and not broken['artifact_integrity_verified']


def test_frozen_explanation_and_hypothetical_preview_change_no_database_record(db,actual_case,actual_model,tmp_path):
    # Activate a newer model first: historical explanation must use the old one.
    newer=tmp_path/'newer.parquet';generate_training_data(newer,rows=2000,seed=999,serving_period='2026-09')
    new_model=train_model(newer,db.settings.model_dir);assert new_model['model_version']!=actual_model['model_version']
    with db.transaction() as s:
        baseline_audit=s.scalar(select(func.count()).select_from(Audit))
        assessment=s.scalar(select(Assessment).where(Assessment.kind=='MODEL'))
        baseline=(assessment.score,assessment.features,assessment.model_version)
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        path='/api/cases/'+actual_case['case_id']+'/model'
        detail=client.get(path+'/explanation').json()
        assert detail['explanation']['status']=='AVAILABLE' and detail['preview_available']
        assert detail['model_version']==actual_model['model_version'] and detail['editable_inputs']['amount']=='10.00'
        preview=client.post(path+'/preview',json={'expected_version':1,'patch':{'amount':'10000.00','cross_border':1}})
        assert preview.status_code==200,preview.text
        value=preview.json()
        assert value['hypothetical'] and not value['persisted'] and not value['deterministic_checks_recomputed']
        assert value['after']['status']=='AVAILABLE' and value['after']['score']>value['before']['score']
        assert value['after']['model_version']==actual_model['model_version']
        assert abs(value['after']['reconstruction_error'])<1e-12
    with db.transaction() as s:
        assessment=s.scalar(select(Assessment).where(Assessment.kind=='MODEL'))
        assert (assessment.score,assessment.features,assessment.model_version)==baseline
        assert s.scalar(select(func.count()).select_from(Audit))==baseline_audit
        assert s.get(MovementVersion,'version-actual-explanation-1').payload['amount']=='10.00'
        assert s.get(ReviewCase,actual_case['case_id']).reviewed_by is None


def test_preview_rejects_stale_versions_invalid_inputs_and_cookie_csrf(db,actual_case):
    app=create_app(db.settings,db)
    with TestClient(app) as client:
        session=client.post('/api/login',json={'token':db.settings.api_token}).json()
        path='/api/cases/'+actual_case['case_id']+'/model/preview'
        assert client.post(path,json={'expected_version':1,'patch':{'amount':'20.00'}}).status_code==403
        for patch in ({'amount':20.0},{'amount':'-1.00'},{'amount':'1e1000'},{'expected_amount':'0.00'},
                      {'cross_border':True},{'cross_border':2},{'customer_age_days':-1},
                      {'event_at':'2026-09-01T00:00:00'},{'event_at':'2026-10-01T00:00:00Z'},
                      {'model_version':'other'}):
            assert client.post(path,json={'expected_version':1,'patch':patch},headers=headers(session)).status_code==422
        assert client.post(path,json={'expected_version':2,'patch':{'amount':'20.00'}},headers=headers(session)).status_code==409
        assert client.post(path,json={'expected_version':1,'patch':{'event_at':'2026-09-01T01:00:00Z'}},headers=headers(session)).status_code==200


def test_review_filters_match_export_sorting_and_never_treat_missing_score_as_zero(db,case_factory):
    case_factory('alpha',score=.9);case_factory('beta',score=.1);case_factory('missing',model_status='UNAVAILABLE',score=None)
    case_factory('literal_percent',score=.5)
    with db.transaction() as s:s.get(Movement,'movement-literal_percent').external_id='value%_exact'
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        query={'min_score':0,'max_score':.5,'reason':'MODEL_RISK','severity':'HIGH','sort':'score_asc'}
        listed=client.get('/api/cases',params=query).json()
        assert [row['id'] for row in listed]==['case-beta','case-literal_percent']
        response=client.get('/api/exports/cases.csv',params=query)
        exported=list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
        assert [row['case_id'] for row in exported]==[row['id'] for row in listed]
        assert [row['id'] for row in client.get('/api/cases',params={'q':'value%_'}).json()]==['case-literal_percent']
        assert client.get('/api/cases',params={'q':'pha'}).json()==[]
        assert client.get('/api/cases',params={'q':'MOVEMENT-AL'}).json()[0]['id']=='case-alpha'
        for endpoint in ('/api/cases','/api/exports/cases.csv'):
            assert client.get(endpoint,params={'min_score':.9,'max_score':.1}).status_code==422
            assert client.get(endpoint,params={'q':'ab'}).status_code==422
            assert client.get(endpoint,params={'sort':'score; DROP TABLE'}).status_code==422


def test_post_activation_commit_failure_reconciles_durable_intent(db,monkeypatch):
    import invoice_optimizer.ml as ml
    queued=queue_training(db,1000,53,'2026-09','commit-failure-run','test:actor')
    activate=ml.activate_model;observed={'intent':False,'failed_commit':False}
    def checked_activate(directory,version):
        with db.transaction() as s:
            run=s.get(ModelTrainingRun,queued['id'])
            assert run.status=='ACTIVATING' and run.model_version==version
            assert run.training_sha256 and run.manifest_sha256
            assert s.scalar(select(Audit.id).where(Audit.action=='model_activation_intended',Audit.entity_id==run.id))
        observed['intent']=True
        return activate(directory,version)
    def fail_commit(session):
        if not observed['failed_commit'] and any(isinstance(obj,Audit) and obj.action=='model_training_succeeded' for obj in session.new):
            observed['failed_commit']=True
            raise RuntimeError('Injected SQL commit failure after pointer activation')
    monkeypatch.setattr(ml,'activate_model',checked_activate)
    event.listen(db.Session,'before_commit',fail_commit)
    try:assert run_job(db,kinds=('TRAIN_MODEL',))
    finally:event.remove(db.Session,'before_commit',fail_commit)
    assert observed=={'intent':True,'failed_commit':True}
    with db.transaction() as s:
        run=s.get(ModelTrainingRun,queued['id'])
        assert run.status=='SUCCEEDED' and s.get(Job,run.job_id).status=='DONE'
        assert model_readiness(db.settings.model_dir)['metadata']['model_version']==run.model_version
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='model_activation_reconciled'))==1
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='model_training_failed'))==0


def test_process_loss_after_activation_resumes_exact_target_without_retraining(db,monkeypatch):
    import invoice_optimizer.ml as ml
    queued=queue_training(db,1000,54,'2026-09','lost-process-run','test:actor')
    activate=ml.activate_model
    def lost_process(directory,version):
        activate(directory,version)
        raise KeyboardInterrupt('Simulated process loss after atomic pointer update')
    monkeypatch.setattr(ml,'activate_model',lost_process)
    with pytest.raises(KeyboardInterrupt):run_job(db,kinds=('TRAIN_MODEL',))
    with db.transaction() as s:
        run=s.get(ModelTrainingRun,queued['id']);target=run.model_version
        assert run.status=='ACTIVATING' and target
        s.get(Job,run.job_id).lease_until=now()-timedelta(seconds=1)
    monkeypatch.setattr(ml,'activate_model',activate)
    def forbidden_fit(*args,**kwargs):raise AssertionError('Recovered activation must not train again')
    monkeypatch.setattr(ml,'train_model',forbidden_fit)
    assert run_job(db,kinds=('TRAIN_MODEL',))
    with db.transaction() as s:
        run=s.get(ModelTrainingRun,queued['id'])
        assert run.status=='SUCCEEDED' and run.model_version==target
        assert s.get(Job,run.job_id).attempts==2


def test_uncertain_activation_blocks_new_runs_and_admin_retries_same_target(db,monkeypatch):
    import invoice_optimizer.ml as ml
    queued=queue_training(db,1000,55,'2026-09','uncertain-run','test:actor')
    activate=ml.activate_model
    def unavailable(*args,**kwargs):raise ValueError('Injected publication failure')
    monkeypatch.setattr(ml,'activate_model',unavailable)
    for attempt in range(3):
        assert run_job(db,kinds=('TRAIN_MODEL',))
        with db.transaction() as s:
            run=s.get(ModelTrainingRun,queued['id']);job=s.get(Job,run.job_id);target=run.model_version
            assert run.status=='ACTIVATION_UNCERTAIN'
            if attempt<2:
                assert job.status=='RUNNING';job.lease_until=now()-timedelta(seconds=1)
            else:assert job.status=='FAILED'
    with pytest.raises(Blocked):queue_training(db,1000,56,'2026-09','another-run','test:actor')
    with TestClient(create_app(db.settings,db)) as client:
        session=bootstrap(client,db)
        resumed=client.post('/api/model/runs/'+queued['id']+'/retry',headers=headers(session),json={'request_id':'manual-resume'})
        assert resumed.status_code==202 and resumed.json()['model_version']==target
        assert client.post('/api/model/runs/'+queued['id']+'/retry',headers=headers(session),json={'request_id':'manual-resume'}).json()==resumed.json()
    monkeypatch.setattr(ml,'activate_model',activate)
    def forbidden_fit(*args,**kwargs):raise AssertionError('Activation retry must retain its trained target')
    monkeypatch.setattr(ml,'train_model',forbidden_fit)
    assert run_job(db,kinds=('TRAIN_MODEL',))
    with db.transaction() as s:
        run=s.get(ModelTrainingRun,queued['id'])
        assert run.status=='SUCCEEDED' and run.model_version==target
        assert s.get(Job,run.job_id).attempts==4


def test_activation_recovery_rejects_metadata_drift_under_same_model_version(db,monkeypatch):
    import json
    import invoice_optimizer.ml as ml
    queued=queue_training(db,1000,57,'2026-09','drift-run','test:actor')
    activate=ml.activate_model
    def lost_process(directory,version):
        activate(directory,version)
        raise KeyboardInterrupt('Pause after pointer switch')
    monkeypatch.setattr(ml,'activate_model',lost_process)
    with pytest.raises(KeyboardInterrupt):run_job(db,kinds=('TRAIN_MODEL',))
    with db.transaction() as s:
        run=s.get(ModelTrainingRun,queued['id']);target=run.model_version
        original=run.result['metadata']['threshold'];s.get(Job,run.job_id).lease_until=now()-timedelta(seconds=1)
    frozen=db.settings.model_dir/'versions'/target/'metadata.json'
    changed=json.loads(frozen.read_text());changed['threshold']=original/2
    frozen.write_text(json.dumps(changed))
    (db.settings.model_dir/'metadata.json').write_text(json.dumps(changed))
    monkeypatch.setattr(ml,'activate_model',activate)
    assert run_job(db,kinds=('TRAIN_MODEL',))
    with db.transaction() as s:
        run=s.get(ModelTrainingRun,queued['id'])
        assert run.status=='ACTIVATION_UNCERTAIN'
        assert run.result['metadata']['threshold']==original
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='model_training_succeeded'))==0
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='model_activation_reconciled'))==0


@pytest.mark.parametrize('status',['QUEUED','RUNNING','ACTIVATING','ACTIVATION_UNCERTAIN'])
def test_legacy_direct_publication_cannot_bypass_training_admission(db,status,monkeypatch):
    import invoice_optimizer.ml as ml
    queued=queue_training(db,1000,58,'2026-09','direct-guard-run','test:actor')
    with db.transaction() as s:s.get(ModelTrainingRun,queued['id']).status=status
    def forbidden_fit(*args,**kwargs):raise AssertionError('Admission must reject before fitting or publication')
    monkeypatch.setattr(ml,'train_model',forbidden_fit)
    with pytest.raises(Blocked):direct_train(db,db.settings.data_dir/'unused.parquet')
