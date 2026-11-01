"""Bounded asynchronous synthetic training, with an auditable activation receipt."""
from datetime import datetime, timedelta
from pathlib import Path
import re
import json
from sqlalchemy import String, Integer, DateTime, ForeignKey, Text, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, Job, now, uid
from .db import audit, lock, replay, record_command, Blocked
from .review_history import actor_identity
from .storage import ObjectStore


class ModelTrainingRun(Base):
    __tablename__='model_training_runs'
    id:Mapped[str]=mapped_column(String(32),primary_key=True,default=uid)
    request_id:Mapped[str]=mapped_column(String(120),unique=True)
    job_id:Mapped[str]=mapped_column(ForeignKey('jobs.id'),unique=True)
    actor:Mapped[str]=mapped_column(String(120))
    requester:Mapped[dict]=mapped_column(JSONB)
    parameters:Mapped[dict]=mapped_column(JSONB)
    status:Mapped[str]=mapped_column(String(20),default='QUEUED',index=True)
    model_version:Mapped[str|None]=mapped_column(String(160),nullable=True)
    training_sha256:Mapped[str|None]=mapped_column(String(64),nullable=True)
    manifest_sha256:Mapped[str|None]=mapped_column(String(64),nullable=True)
    result:Mapped[dict|None]=mapped_column(JSONB,nullable=True)
    error:Mapped[str|None]=mapped_column(Text,nullable=True)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
    started_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    finished_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)


def serialize_training_run(run):
    value={c.name:(v.isoformat() if isinstance(v,datetime) else v)
           for c in run.__table__.columns for v in [getattr(run,c.name)]}
    value['training_download_url']=f'/api/model/runs/{run.id}/files/training' if run.training_sha256 else None
    value['manifest_download_url']=f'/api/model/runs/{run.id}/files/manifest' if run.manifest_sha256 else None
    return value


def queue_training(db,rows,seed,period,request_id,actor):
    if not db.settings.synthetic:raise Blocked('Synthetic training is available only in simulation mode')
    if type(rows) is not int or not 1000<=rows<=50000:raise ValueError('Training must contain 1,000–50,000 synthetic rows')
    if type(seed) is not int or not 0<=seed<=2147483647:raise ValueError('Seed must be an integer from 0 to 2,147,483,647')
    if not isinstance(period,str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])',period):raise ValueError('Period must be YYYY-MM')
    if not 2000<=int(period[:4])<=2100:raise ValueError('Synthetic serving year must be between 2000 and 2100')
    parameters={'rows':rows,'seed':seed,'period':period,'synthetic':True}
    args={'parameters':parameters,'actor':actor}
    with db.transaction() as s:
        cached=replay(s,request_id,'model_training_requested',args)
        if cached is not None:return cached
        requester=actor_identity(s,actor)
        lock(s,'model-training:admission')
        if s.scalar(select(ModelTrainingRun.id).where(ModelTrainingRun.status.in_(['QUEUED','RUNNING','ACTIVATING','ACTIVATION_UNCERTAIN'])).limit(1)):
            raise Blocked('A model training run is already queued or running. Wait for its result before starting another.')
        run_id=uid();job=Job(id=uid(),dedup_key='train-model:'+run_id,kind='TRAIN_MODEL',period=period,payload={'run_id':run_id})
        s.add(job);s.flush()
        run=ModelTrainingRun(id=run_id,request_id=request_id,job_id=job.id,actor=actor,requester=requester,parameters=parameters)
        s.add(run);s.flush()
        audit(s,actor,'model_training_requested',run.id,{'job_id':job.id,'request_id':request_id,'parameters':parameters,'requester':requester})
        return record_command(s,request_id,'model_training_requested',args,serialize_training_run(run))


def direct_train(db,path,*,actor='cli:direct-training'):
    """Offline bootstrap/maintenance publication, excluded from queued runs.

    This legacy direct path has no queued activation-recovery protocol. Its
    admission lock prevents it from replacing a queued run's retained target.
    """
    from .ml import train_model
    with db.transaction() as s:
        requester=actor_identity(s,actor)
        lock(s,'model-training:admission')
        if s.scalar(select(ModelTrainingRun.id).where(ModelTrainingRun.status.in_(
                ['QUEUED','RUNNING','ACTIVATING','ACTIVATION_UNCERTAIN'])).limit(1)):
            raise Blocked('Direct model publication is blocked while a queued training or activation intent is outstanding')
        metadata=train_model(Path(path),db.settings.model_dir)
        audit(s,actor,'model_trained_directly',metadata['model_version'],
              {'training_sha256':metadata['training_sha256'],'artifact_sha256':metadata['artifact_sha256'],
               'requested_by':requester,'workflow':'OFFLINE_DIRECT_PUBLICATION'})
        return metadata


def _finish_activation(s,run,job,metadata,*,reconciled=False):
    run.status='SUCCEEDED';run.error=None;run.finished_at=now()
    run.result={**run.result,'metadata':metadata,'activation_state':'ACTIVE_VERIFIED'}
    job.status='DONE';job.lease_until=None;job.error=None
    audit(s,'worker','model_activation_reconciled' if reconciled else 'model_training_succeeded',run.id,
          {'model_version':run.model_version,'training_sha256':run.training_sha256,
           'manifest_sha256':run.manifest_sha256,'requested_by':run.requester,
           'activation_scope':'FUTURE_ASSESSMENTS_ONLY','parameters':run.parameters})


def _matches_activation_intent(run,checked):
    metadata=checked.get('metadata')
    return (checked.get('status')=='READY' and isinstance(metadata,dict)
            and metadata.get('model_version')==run.model_version
            and metadata.get('training_sha256')==run.training_sha256
            and metadata==(run.result or {}).get('metadata'))


def retry_activation(db,run_id,request_id,actor):
    args={'run_id':run_id,'actor':actor}
    with db.transaction() as s:
        cached=replay(s,request_id,'model_activation_retry_requested',args)
        if cached is not None:return cached
        requester=actor_identity(s,actor)
        lock(s,'model-training:admission')
        run=s.get(ModelTrainingRun,run_id)
        if not run:
            raise Blocked('Only a run with a retained activation intent can be resumed')
        job=s.get(Job,run.job_id,with_for_update=True)
        s.refresh(run,with_for_update=True)
        if run.status not in ('ACTIVATING','ACTIVATION_UNCERTAIN'):
            raise Blocked('Only a run with a retained activation intent can be resumed')
        if not run.model_version or not run.training_sha256 or not run.manifest_sha256:
            raise Blocked('The recorded activation intent is incomplete')
        run.status='ACTIVATING';run.error=None
        run.result={**run.result,'activation_state':'INTENT_RECORDED','activation_retry_baseline':job.attempts}
        job.status='READY';job.claim_token=None;job.lease_until=None;job.error=None
        audit(s,actor,'model_activation_retry_requested',run.id,
              {'request_id':request_id,'model_version':run.model_version,'requested_by':requester,
               'original_requester':run.requester,'prior_job_attempts':job.attempts,'retrain':False})
        return record_command(s,request_id,'model_activation_retry_requested',args,serialize_training_run(run))


def mark_training_failed(db,job_id,claim_token,error):
    from .ml import model_readiness
    with db.transaction() as s:
        lock(s,'model-training:execution')
        job=s.get(Job,job_id,with_for_update=True)
        if not job or job.kind!='TRAIN_MODEL' or job.claim_token!=claim_token or job.status=='DONE':return
        run=s.get(ModelTrainingRun,job.payload['run_id'],with_for_update=True)
        if not run or run.status=='SUCCEEDED':return
        if run.status in ('ACTIVATING','ACTIVATION_UNCERTAIN'):
            # A filesystem pointer and a SQL commit cannot be one atomic write.
            # Resolve the durable intent by observation; never label a switched
            # active model as an ordinary pre-activation training failure.
            current=model_readiness(db.settings.model_dir)
            if _matches_activation_intent(run,current):
                _finish_activation(s,run,job,current['metadata'],reconciled=True)
                return
            run.status='ACTIVATION_UNCERTAIN';run.error=str(error)[:2000]
            run.result={**run.result,'activation_state':'RECOVERY_REQUIRED'}
            # The existing lease-recovery loop supplies a bounded delay. After
            # three attempts an administrator explicitly retries this same
            # retained target; a new training run remains blocked.
            attempts=job.attempts-int((run.result or {}).get('activation_retry_baseline',0))
            if attempts<3:
                job.status='RUNNING';job.lease_until=now()+timedelta(seconds=30)
            else:
                job.status='FAILED';job.lease_until=None
            job.error=str(error)[:2000]
            audit(s,'worker','model_activation_recovery_required',run.id,
                  {'model_version':run.model_version,'error':str(error)[:500],
                   'automatic_retry_scheduled':attempts<3,'requested_by':run.requester})
            return
        if run.status!='FAILED':
            run.status='FAILED';run.error=str(error)[:2000];run.finished_at=now()
            audit(s,'worker','model_training_failed',run.id,{'error':str(error)[:500],'requested_by':run.requester})


def run_training_job(db,job_id,claim_token):
    from .synthetic import generate_training_data
    from .ml import train_model, activate_model, model_readiness
    with db.transaction() as s:
        job=s.get(Job,job_id,with_for_update=True)
        if not job or job.kind!='TRAIN_MODEL' or job.status!='RUNNING' or job.claim_token!=claim_token:return
        run=s.get(ModelTrainingRun,job.payload['run_id'],with_for_update=True)
        if run.status=='SUCCEEDED':job.status='DONE';job.lease_until=None;return
        has_activation_intent=run.status in ('ACTIVATING','ACTIVATION_UNCERTAIN')
        if not has_activation_intent:
            run.status='RUNNING';run.started_at=run.started_at or now();run.error=None
            audit(s,'worker','model_training_started',run.id,{'requested_by':run.requester,'parameters':run.parameters})
    # Keep the claimed job row locked while fitting. Lease recovery uses SKIP
    # LOCKED, so another worker cannot fit or activate the same run concurrently.
    # Only the dedicated training worker holds this bounded transaction; normal
    # scoring continues in its separate worker loop and database connection.
    if not has_activation_intent:
      with db.transaction() as s:
        lock(s,'model-training:execution')
        job=s.get(Job,job_id,with_for_update=True)
        if not job or job.status!='RUNNING' or job.claim_token!=claim_token:return
        run=s.get(ModelTrainingRun,job.payload['run_id'],with_for_update=True)
        directory=db.settings.model_dir/'training'/run.id
        directory.mkdir(parents=True,exist_ok=True)
        training_path=directory/'training.parquet'
        manifest_path=training_path.with_suffix('.manifest.json')
        if training_path.exists() or manifest_path.exists():
            if not training_path.is_file() or not manifest_path.is_file() or manifest_path.stat().st_size>100000:
                raise ValueError('Interrupted training inputs are incomplete; start a new run without overwriting retained artifacts')
            manifest=json.loads(manifest_path.read_text())
            if (manifest.get('rows')!=run.parameters['rows'] or manifest.get('seed')!=run.parameters['seed']
                    or manifest.get('serving_period')!=run.parameters['period'] or manifest.get('synthetic') is not True
                    or manifest.get('sha256')!=ObjectStore.hash_file(training_path)):
                raise ValueError('Retained training inputs do not match the queued run')
        else:
            manifest=generate_training_data(training_path,rows=run.parameters['rows'],seed=run.parameters['seed'],serving_period=run.parameters['period'])
        metadata=train_model(training_path,db.settings.model_dir,activate=False)
        checked=model_readiness(db.settings.model_dir,metadata['model_version'])
        if checked.get('status')!='READY':raise ValueError('Trained artifact validation failed: '+str(checked.get('reason')))
        store=ObjectStore(db.settings.data_dir/'objects')
        training_hash=store.put_file(training_path)
        manifest_hash=store.put_file(manifest_path)
        if training_hash!=metadata['training_sha256']:raise ValueError('Training artifact hash does not match model metadata')
        run.status='ACTIVATING';run.model_version=metadata['model_version']
        run.training_sha256=training_hash;run.manifest_sha256=manifest_hash
        run.result={'metadata':metadata,'manifest':manifest,'training_sha256':training_hash,
                    'manifest_sha256':manifest_hash,'activation_scope':'FUTURE_ASSESSMENTS_ONLY',
                    'historical_scores_changed':False,'synthetic_only':True,'activation_state':'INTENT_RECORDED'}
        job.lease_until=now()+timedelta(minutes=10)
        audit(s,'worker','model_activation_intended',run.id,{'model_version':run.model_version,
              'training_sha256':training_hash,'manifest_sha256':manifest_hash,'requested_by':run.requester})
      # The activation intent and retained target are now committed BEFORE the
      # filesystem pointer can move. A recovered claim resumes this exact target.
    with db.transaction() as s:
        lock(s,'model-training:execution')
        job=s.get(Job,job_id,with_for_update=True)
        if not job or job.status!='RUNNING' or job.claim_token!=claim_token:return
        run=s.get(ModelTrainingRun,job.payload['run_id'],with_for_update=True)
        if run.status not in ('ACTIVATING','ACTIVATION_UNCERTAIN'):return
        store=ObjectStore(db.settings.data_dir/'objects')
        store.path(run.training_sha256,verify=True);store.path(run.manifest_sha256,verify=True)
        checked=model_readiness(db.settings.model_dir,run.model_version)
        if not _matches_activation_intent(run,checked):
            raise ValueError('Retained activation target is unavailable or differs from its durable intent')
        current=model_readiness(db.settings.model_dir)
        already_active=_matches_activation_intent(run,current)
        if not already_active:activate_model(db.settings.model_dir,run.model_version)
        current=model_readiness(db.settings.model_dir)
        if not _matches_activation_intent(run,current):
            raise ValueError('Model activation could not be verified; the recorded target requires recovery')
        _finish_activation(s,run,job,current['metadata'],reconciled=already_active)
        return serialize_training_run(run)
