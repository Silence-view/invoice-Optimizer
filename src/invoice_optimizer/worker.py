from datetime import timedelta
import time, logging, os
from threading import Event, Thread, Lock
from sqlalchemy import select
from .models import Job, Invoice, RuntimeOption, now, uid
from sqlalchemy.dialects.postgresql import insert as pg_insert
from .db import audit
from .operations import get_effective_automation
from .assessment import reconcile_job, score_job
from .billing import render_job
from .delivery import send_one, recover_expired

log=logging.getLogger(__name__)
_CURSOR_LOCK=Lock()
_JOB_KINDS=('ASSESS','SCORE','RENDER','TRAIN_MODEL')
_RECOVERY_LIMIT=32

def _kind_order(db, kinds):
    kinds=tuple(dict.fromkeys(_JOB_KINDS if kinds is None else kinds))
    if not kinds or set(kinds)-set(_JOB_KINDS):
        raise ValueError('Select at least one supported job kind')
    # Fairness state is small and process-local; PostgreSQL owns claim safety.
    with _CURSOR_LOCK:
        cursors=getattr(db,'_job_kind_cursors',None)
        if cursors is None:db._job_kind_cursors=cursors={}
        offset=cursors.get(kinds,0)%len(kinds)
        cursors[kinds]=(offset+1)%len(kinds)
    return kinds[offset:]+kinds[:offset]

def run_job(db, kinds=None):
    order=_kind_order(db,kinds)
    with db.transaction() as s:
        if not get_effective_automation(s,db.settings)['processing_enabled']:return False
        # Internal jobs are idempotent. Only these may recover automatically.
        expired_query=select(Job).where(Job.status=='RUNNING',Job.lease_until<now())
        expired_query=expired_query.where(Job.kind.in_(order))
        expired=list(s.scalars(expired_query.order_by(Job.lease_until,Job.created_at,Job.id)
                              .limit(_RECOVERY_LIMIT).with_for_update(skip_locked=True)))
        for j in expired:
            j.status='READY'
            audit(s,'worker','job_lease_recovered',j.id,{'kind':j.kind})
        job=None
        for candidate_kind in order:
            # Each probe uses the partial READY(kind,created_at) index; there is
            # no sorting of the entire queue to promote a scarce job kind.
            job=s.scalar(select(Job).where(Job.status=='READY',Job.kind==candidate_kind)
                         .order_by(Job.created_at,Job.id).limit(1).with_for_update(skip_locked=True))
            if job is not None:break
        if not job:return False
        job.status='RUNNING';job.claim_token=uid();job.attempts+=1;job.lease_until=now()+timedelta(minutes=10)
        audit(s,'worker','job_claimed',job.id,{'kind':job.kind,'attempt':job.attempts})
        job_id=job.id;kind=job.kind;claim_token=job.claim_token
    try:
        if kind=='TRAIN_MODEL':
            from .model_ops import run_training_job
            run_training_job(db,job_id,claim_token)
        else:{'ASSESS':reconcile_job,'SCORE':score_job,'RENDER':render_job}[kind](db,job_id,claim_token)
    except Exception as exc:
        log.exception('Job failed: %s',job_id)
        with db.transaction() as s:
            job=s.get(Job,job_id,with_for_update=True)
            if job.status=='DONE' or job.claim_token!=claim_token:return True
            job.status='FAILED';job.error=str(exc)[:2000];job.lease_until=None
            if job.kind=='RENDER':
                inv=s.get(Invoice,job.payload['invoice_id'])
                if inv.status=='BUILDING':inv.status='FAILED'
            audit(s,'worker','job_failed',job_id,{'kind':kind,'error':str(exc)[:500]})
        if kind=='TRAIN_MODEL':
            from .model_ops import mark_training_failed
            mark_training_failed(db,job_id,claim_token,str(exc))
    return True

def tick(db,max_jobs=None,max_emails=None,*,job_kinds=None):
    with db.transaction() as s:control=get_effective_automation(s,db.settings)
    for value,name in ((max_jobs,'max_jobs'),(max_emails,'max_emails')):
        if value is not None and (type(value) is not int or value<0):raise ValueError(name+' must be a nonnegative integer')
    if control['paused']:return {'jobs':0,'emails':0,'uncertain_recovered':0}
    max_jobs=control['jobs_per_tick'] if max_jobs is None else min(max_jobs,control['jobs_per_tick'])
    max_emails=control['emails_per_tick'] if max_emails is None else min(max_emails,control['emails_per_tick'])
    if not control['email_delivery_enabled']:max_emails=0
    recovered=recover_expired(db);jobs=mails=0
    for _ in range(max_jobs):
        if not run_job(db,kinds=job_kinds):break
        jobs+=1
    for _ in range(max_emails):
        if not send_one(db):break
        mails+=1
    return {'jobs':jobs,'emails':mails,'uncertain_recovered':recovered}

def record_heartbeat(db,worker_id,status='RUNNING'):
    value={'at':now().isoformat(),'status':status,'worker_id':worker_id,'pid':os.getpid()}
    with db.transaction() as s:
        statement=pg_insert(RuntimeOption).values(key='worker.heartbeat',value=value)
        s.execute(statement.on_conflict_do_update(index_elements=['key'],set_={'value':value}))


def run_forever(db):
    from .scheduler import prepare_due_invoices, dispatch_approved, notify_pending_approvals
    stopped=Event();worker_id=uid();last_heartbeat=0
    settings=getattr(db,'settings',None)
    poll_seconds=getattr(settings,'poll_seconds',2.0)
    def monthly_preparation():
        while not stopped.is_set():
            try:
                report=prepare_due_invoices(db)
                # Reminders only; approval stays a human decision.
                if isinstance(report,dict) and report.get('enabled') and getattr(settings,'approval_digest',False):notify_pending_approvals(db)
            except Exception:log.exception('Monthly preparation failed; retrying next cycle')
            stopped.wait(60)
    inbox_seconds=getattr(settings,'inbox_seconds',0)
    def data_refresh():
        from .inbox import run_refresh_cycle
        last_feed=None
        while not stopped.is_set():
            try:_,last_feed=run_refresh_cycle(db,last_feed)
            except Exception:log.exception('Automatic data refresh failed; retrying next cycle')
            stopped.wait(inbox_seconds)
    refresher=Thread(target=data_refresh,name='automatic-data-refresh',daemon=True) if inbox_seconds else None
    if refresher:refresher.start()
    # A slow monthly SQL snapshot must not occupy the continuous job loop.
    # Separate sessions use the shared bounded connection pool; per-scope locks
    # still serialise competing financial changes.
    scheduler=Thread(target=monthly_preparation,name='monthly-preparation',daemon=True)
    scheduler.start()
    def training_loop():
        while not stopped.is_set():
            try:
                if run_job(db,kinds=('TRAIN_MODEL',)):continue
            except Exception:log.exception('Training worker failed; retrying after bounded delay')
            stopped.wait(poll_seconds)
    trainer=Thread(target=training_loop,name='synthetic-model-training',daemon=True)
    trainer.start()
    try:
        while True:
            try:
                if time.monotonic()-last_heartbeat>=5:
                    record_heartbeat(db,worker_id);last_heartbeat=time.monotonic()
                dispatch_approved(db)
                result=tick(db,job_kinds=('ASSESS','SCORE','RENDER'))
                if not result['jobs'] and not result['emails']:time.sleep(poll_seconds)
            except KeyboardInterrupt:return
            except Exception:
                log.exception('Worker loop failed; retrying after bounded delay');time.sleep(3)
    finally:
        stopped.set();scheduler.join(timeout=5);trainer.join(timeout=5)
        if refresher:refresher.join(timeout=5)
        try:record_heartbeat(db,worker_id,'STOPPED')
        except Exception:log.exception('Could not record worker shutdown')
