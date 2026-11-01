"""Small authenticated operations console, bounded pages and shared SSE summary."""
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from collections import defaultdict, deque
from threading import RLock
import asyncio, base64, csv, io, json, secrets, time, tempfile
import re
from copy import deepcopy
from typing import Literal
from fastapi import FastAPI, Request, HTTPException, Depends, Query, UploadFile, File, Form
from fastapi import Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func, text, or_, and_, case as sql_case
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import defer
from .config import Settings
from .db import Database, Blocked, Conflict, NotFound, canonical, audit, digest, is_timeout
from .models import (Customer, Batch, Movement, MovementVersion, SourceRecord, Assessment, ReviewCase,
                     Invoice, InvoiceMember, Outbox, DeliveryAttempt, DeliveryEvent, Audit, Counter, Quarantine, RuntimeOption, Job, Allocation, BatchRecord)
from .assessment import correct_case, resolve_case
from .billing import create_invoice, approve_invoice, revoke_invoice, dispatch_invoice, reject_invoice, reopen_invoice
from .triage import personal_postponements, case_binding, invoice_binding, postpone, resume
from .delivery import simulation_event, reconcile_attempt
from .ingestion import ingest_file, supersede_batch
from .storage import ObjectStore
from .provenance import case_sources, file_reference, invoice_previews, invoice_sources_page
from .operators import (Operator, operator_identity, public_operator, create_operator,
                        count_operators, account_lock, verify_password,
                        normalize_username, DUMMY_HASH, actor_attributions)
from .auth_secrets import session_signing_secret
from .invoice_preview import preview_info, open_page, PreviewUnavailable, PreviewBusy
from .review_cards import case_card


def serial(obj):
    if obj is None:return None
    if isinstance(obj,(datetime,Decimal)):return str(obj)
    return {column.name:(str(v) if isinstance(v,(datetime,Decimal)) else v) for column in obj.__table__.columns for v in [getattr(obj,column.name)]}

class Keyset:
    """Opaque position after the last returned row: (sort value, id), id ascending.

    Every supported order is NULLS LAST on its value with id as tie-breaker, so
    the next page is "value beyond, or equal value and larger id, or NULL".
    """
    def __init__(self,kind,column,id_column,descending,parse=lambda v:v,value=lambda row:None,id_type=str):
        self.kind,self.column,self.id_column,self.descending,self.parse,self.value,self.id_type=kind,column,id_column,descending,parse,value,id_type

    def where(self,query,cursor):
        if cursor is None:return query
        try:
            kind,value,identifier=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
            if kind!=self.kind or type(identifier) is not self.id_type or (self.id_type is int and not 0<identifier<2**63):raise ValueError()
            # PostgreSQL text cannot hold NUL: a tampered cursor is a 422, not a driver error (500).
            if any(isinstance(part,str) and '\x00' in part for part in (value,identifier)):raise ValueError()
            value=None if value is None else self.parse(value)
        except (ValueError,TypeError):raise ValueError('Invalid pagination cursor for this list and sort order') from None
        if value is None:return query.where(self.column.is_(None),self.id_column>identifier)
        beyond=self.column<value if self.descending else self.column>value
        if not self.column.expression.nullable:
            # A bare OR is only a filter: the scan would restart at the first row.
            # The redundant bound is the index condition that seeks to the cursor.
            bound=self.column<=value if self.descending else self.column>=value
            return query.where(bound,or_(beyond,self.id_column>identifier))
        return query.where(or_(beyond,and_(self.column==value,self.id_column>identifier),self.column.is_(None)))

    def next(self,rows,limit,identifier):
        """rows holds limit+1 fetched rows; returns None on the last page."""
        if len(rows)<=limit:return None
        last=rows[limit-1];value=self.value(last)
        value=value.isoformat() if isinstance(value,datetime) else value
        return base64.urlsafe_b64encode(canonical([self.kind,value,identifier(last)]).encode()).rstrip(b'=').decode()


def same_secret(supplied,expected):
    """Constant-time; bytes, because compare_digest raises TypeError (a 500) on non-ASCII str."""
    return secrets.compare_digest(str(supplied).encode('utf-8'),str(expected).encode('utf-8'))


def page(items,next_cursor,limit,cursor,response):
    """Offset callers keep the bare list; next_cursor is also an X-Next-Cursor header."""
    if next_cursor:response.headers['X-Next-Cursor']=next_cursor
    return items if cursor is None else {'items':items,'next_cursor':next_cursor,'limit':limit}


def model_info(settings):
    from .ml import model_readiness
    checked=model_readiness(settings.model_dir)
    return {**(checked.get('metadata') or {}),**checked,
            'artifact_integrity_verified':checked.get('status')=='READY'}


def worker_info(db):
    with db.transaction() as s:
        option=s.get(RuntimeOption,'worker.heartbeat')
        value=deepcopy(option.value) if option else None
    if not isinstance(value,dict):return {'status':'UNAVAILABLE','stale':True,'at':None,'reason':'No worker heartbeat has been recorded'}
    try:
        at=datetime.fromisoformat(value['at'].replace('Z','+00:00'))
        if at.tzinfo is None:raise ValueError('Missing heartbeat timezone')
        age=(datetime.now(timezone.utc)-at).total_seconds()
    except (ValueError,TypeError,KeyError,AttributeError):return {'status':'UNAVAILABLE','stale':True,'at':value.get('at'),'reason':'Worker heartbeat is invalid'}
    stale=age>15 or age < -5
    status='STALE' if stale else ('RUNNING' if value.get('status')=='RUNNING' else 'STOPPED')
    return {**value,'status':status,'recorded_status':value.get('status'),'stale':stale,
            'age_seconds':round(age,1),'reason':'Worker heartbeat is stale or its clock differs' if stale else None}


ALL_RESOURCES=['cases','invoices','outbox','batches','model','automation','audit','customers']


def changed_resources(actions):
    """Invalidation hints, never a replacement for durable version checks."""
    result={'audit'}
    for action in actions:
        if action.startswith('job_'):result.add('automation')
        elif action.startswith('batch_'):result.update(('batches','cases','invoices','outbox','automation'))
        elif action in ('rules_evaluated','model_evaluated'):result.update(('cases','invoices','outbox','automation'))
        elif action.startswith(('case_','correction_','bulk_','review_')):result.update(('cases','invoices','outbox','automation'))
        elif action.startswith(('invoice_','snapshot_')):result.update(('invoices','outbox','automation'))
        elif action.startswith(('send_','submission_','simulated_','approval_digest','mail_test')):result.update(('outbox','automation'))
        elif action.startswith('inbox_'):result.update(('batches','automation'))
        elif action.startswith(('model_','training_')):result.update(('model','automation'))
        elif action=='automation_settings_changed':result.add('automation')
        elif action=='customers_loaded':result.update(('customers','batches','invoices'))
        else:return ALL_RESOURCES.copy()
    return sorted(result)


def dashboard(db):
    from .operations import get_effective_automation
    with db.transaction() as s:
        s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        counters=dict(s.execute(select(Counter.name,Counter.value)).all())
        counts={'movements':counters.get('movements',0),'eligible':counters.get('state:ELIGIBLE',0)}
        counts['customers']=s.scalar(select(func.count()).select_from(Customer))
        counts['batches']=s.scalar(select(func.count()).select_from(Batch))
        counts['quarantined']=s.scalar(select(func.count()).select_from(Quarantine).where(Quarantine.resolved_by_batch.is_(None)))
        counts['open_cases'],counts['score_unavailable']=s.execute(select(func.count(),
            func.count().filter(ReviewCase.reason=='MODEL_UNAVAILABLE')).select_from(ReviewCase).where(ReviewCase.status=='OPEN')).one()
        counts['invoices'],counts['pending_approvals']=s.execute(select(func.count(),
            func.count().filter(Invoice.status=='DRAFT')).select_from(Invoice)).one()
        # Historical DONE jobs need not be counted for the operational queue.
        jobs=dict(s.execute(select(Job.status,func.count()).where(Job.status.in_(('READY','RUNNING','FAILED'))).group_by(Job.status)).all())
        for status in ('READY','RUNNING','FAILED'):counts['jobs_'+status.lower()]=jobs.get(status,0)
        out=dict(s.execute(select(Outbox.status,func.count()).group_by(Outbox.status)).all())
        counts.update(queued=out.get('READY',0),accepted=out.get('ACCEPTED',0),unknown_delivery=out.get('UNKNOWN',0))
        # Evidence counts identify distinct attempts, not webhook replay volume.
        deliveries=dict(s.execute(select(DeliveryEvent.event,func.count(func.distinct(DeliveryEvent.attempt_id)))
            .where(DeliveryEvent.event.in_(('delivered','bounced'))).group_by(DeliveryEvent.event)).all())
        for event in ('delivered','bounced'):counts[event]=deliveries.get(event,0)
        periods=list(s.scalars(select(Batch.period).distinct().order_by(Batch.period.desc())))
        revision=s.scalar(select(func.max(Audit.id))) or 0
        recent=[serial(x) for x in s.scalars(select(Audit).order_by(Audit.id.desc()).limit(6))]
        options={x.key:x.value for x in s.scalars(select(RuntimeOption).where(RuntimeOption.key.in_(('scheduler.close.report','scheduler.dispatch.report','scheduler.digest.report','inbox.report'))))}
        control=get_effective_automation(s,db.settings)
        queue=s.execute(select(func.min(Job.created_at),func.count()).where(Job.status=='READY')).one()
        oldest_age=max(0,(datetime.now(timezone.utc)-queue[0]).total_seconds()) if queue[0] else None
        value={'counts':counts,'mode':db.settings.mode,'periods':periods,'revision':revision,
            'updated_at':datetime.now(timezone.utc).isoformat(),'recent_events':recent,'provider':db.settings.provider,
            'refresh_seconds':max(2,db.settings.poll_seconds),'auto_dispatch_after_approval':control['dispatch_after_approval_enabled'],
            'auto_monthly_preparation':control['monthly_preparation_enabled'],'automation':options,
            'automation_control':control,'queue':{'ready':queue[1],'oldest_ready_age_seconds':round(oldest_age,1) if oldest_age is not None else None,
                'high_watermark':control['queue_high_watermark'],'paused':control['paused']}}
    # File verification and the independent worker clock never hold a database
    # snapshot open while doing model I/O or waiting for another connection.
    return {**value,'model':model_info(db.settings),'worker':worker_info(db)}


class SummaryCache:
    def __init__(self,db):
        self.db=db;self.value=None;self.checked=0.;self.guard=RLock();self.generation=0;self.signature=None;self.last_full_invalidation=0.
    def get(self):
        with self.guard:
            if self.value and time.monotonic()-self.checked<2:return self.value
            previous=self.value;value=dashboard(self.db)
            resources=ALL_RESOURCES.copy()
            if previous:
                with self.db.transaction() as s:
                    actions=list(s.scalars(select(Audit.action).where(Audit.id>previous['revision'],Audit.id<=value['revision'])
                        .order_by(Audit.id).limit(257)))
                resources=changed_resources(actions) if 0<len(actions)<=256 else ALL_RESOURCES.copy()
            stable={key:value[key] for key in ('counts','mode','periods','revision','model','provider','automation','automation_control')}
            stable['worker']={key:value['worker'].get(key) for key in ('status','stale','worker_id')}
            signature=digest(stable)
            # Audit IDs are allocated before commit. A late lower-ID write can
            # change a detail row without changing any displayed count. Bound
            # that blind spot with a conservative periodic revalidation.
            full_revalidation=previous is None or time.monotonic()-self.last_full_invalidation>=15
            if full_revalidation:
                resources=ALL_RESOURCES.copy();self.last_full_invalidation=time.monotonic()
            if signature!=self.signature or full_revalidation:
                self.generation+=1;self.signature=signature
            elif previous:
                resources=[]
            value['stream_version']=f"{value['revision']}:{self.generation}"
            value['previous_stream_version']=previous.get('stream_version') if previous else None
            value['changed_resources']=resources
            value['full_revalidation_seconds']=15
            self.value=value;self.checked=time.monotonic();return value

INSIGHT_SECTIONS={'overview':'overview','suppliers':'supplier_concentration','duplicates':'duplicate_candidates',
    'outliers':'amount_outliers','benford':'benford','trends':'trends','risk':'risk_distribution',
    'ageing':'review_ageing','quality':'data_quality'}
INSIGHT_TTL_SECONDS=30
INSIGHT_TIMEOUT_MS=30_000
INSIGHT_PERIOD=r'^[0-9]{4}-(0[1-9]|1[0-2])$'


class InsightReader:
    """The insights engine reads `db.engine` and `db.settings` only: route it to
    the read engine with a statement timeout no looser than 30 s for the API."""
    def __init__(self,db):
        configured=int(getattr(db.settings,'db_statement_timeout_ms',0) or 0)
        self.engine=db.read_engine
        self.settings=type('InsightSettings',(),{'db_statement_timeout_ms':min(configured,INSIGHT_TIMEOUT_MS) if configured>0 else INSIGHT_TIMEOUT_MS})()


class InsightCache:
    """Short per-process cache keyed by arguments. One computation per key at a
    time and at most two concurrent snapshots per process protect the database;
    a failed computation is never cached."""
    def __init__(self,ttl=INSIGHT_TTL_SECONDS,size=128,slots=2):
        self.ttl,self.size=ttl,size;self.values={};self.keys=defaultdict(RLock);self.guard=RLock()
        from threading import BoundedSemaphore
        self.slots=BoundedSemaphore(slots)
    def get(self,key,compute):
        with self.guard:hit=self.values.get(key)
        if hit and time.monotonic()-hit[0]<self.ttl:return hit[1],True
        with self.guard:key_lock=self.keys[key]
        with key_lock:
            with self.guard:hit=self.values.get(key)
            if hit and time.monotonic()-hit[0]<self.ttl:return hit[1],True
            if not self.slots.acquire(timeout=10):
                from .operations import Backpressure
                busy=Backpressure('Insights are busy; retry shortly');busy.retry_after=2;raise busy
            try:value=compute()
            finally:self.slots.release()
            with self.guard:
                now=time.monotonic()
                for stale in [k for k,(at,_) in self.values.items() if now-at>=self.ttl]:self.values.pop(stale,None);self.keys.pop(stale,None)
                while len(self.values)>=self.size:
                    oldest=min(self.values,key=lambda k:self.values[k][0]);self.values.pop(oldest,None);self.keys.pop(oldest,None)
                self.values[key]=(now,value)
            return value,False


class Body(BaseModel):model_config=ConfigDict(extra='forbid')
class AutomationUpdate(Body):
    patch:dict=Field(min_length=1,max_length=9)
    expected_revision:int=Field(ge=0,strict=True)
    request_id:str=Field(min_length=1,max_length=120)
    reason:str=Field(min_length=5,max_length=1000)
class Login(Body):
    token:str|None=Field(default=None,min_length=1,max_length=512)
    username:str|None=Field(default=None,min_length=1,max_length=80)
    password:str|None=Field(default=None,min_length=1,max_length=256)
class OperatorCreate(Body):
    username:str=Field(min_length=3,max_length=80)
    display_name:str=Field(min_length=1,max_length=120)
    password:str=Field(min_length=12,max_length=256)
    role:str='reviewer'
class OperatorBootstrap(Body):
    setup_token:str=Field(min_length=1,max_length=512)
    username:str=Field(min_length=3,max_length=80)
    display_name:str=Field(min_length=1,max_length=120)
    password:str=Field(min_length=12,max_length=256)
class OperatorUpdate(Body):active:bool
class ModelTrain(Body):
    rows:int=Field(default=10000,ge=1000,le=50000,strict=True)
    seed:int=Field(default=42,ge=0,le=2147483647,strict=True)
    period:str=Field(pattern=r'^[0-9]{4}-(0[1-9]|1[0-2])$')
    request_id:str=Field(min_length=1,max_length=120)
class ModelPreview(Body):
    expected_version:int=Field(ge=1,strict=True)
    patch:dict=Field(min_length=1,max_length=6)
class Correction(Body):
    expected_version:int=Field(ge=1);patch:dict;reason:str=Field(min_length=5,max_length=2000);request_id:str=Field(min_length=1,max_length=120)
class Resolution(Body):
    expected_version:int=Field(ge=1);outcome:str;reason:str=Field(min_length=5,max_length=2000);request_id:str=Field(min_length=1,max_length=120)
class BulkItem(Body):
    case_id:str=Field(min_length=1,max_length=64);expected_version:int=Field(ge=1,strict=True)
class BulkRelease(Body):
    items:list[BulkItem]=Field(min_length=1,max_length=100)
    reason:str=Field(min_length=5,max_length=2000);request_id:str=Field(min_length=1,max_length=120)
class Close(Body):
    customer_id:str;period:str;request_id:str
    line_format:Literal['ITEMIZED','SUMMARY']='ITEMIZED'
class RequestID(Body):request_id:str
class Approval(RequestID):
    request_id:str=Field(min_length=1,max_length=120)
    fingerprint:str
    reason:str|None=Field(default=None,min_length=5,max_length=2000)
class InvoiceFormat(Approval):line_format:Literal['ITEMIZED','SUMMARY']
class Revoke(RequestID):reason:str
class InvoiceDecision(RequestID):
    request_id:str=Field(min_length=1,max_length=120)
    fingerprint:str=Field(pattern=r'^[a-f0-9]{64}$')
    reason:str=Field(min_length=5,max_length=2000)
class PersonalTriage(RequestID):
    request_id:str=Field(min_length=1,max_length=120)
    kind:Literal['CASE','INVOICE']
    id:str=Field(min_length=1,max_length=64)
    expected_version:int|None=Field(default=None,ge=1,strict=True)
    fingerprint:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')
    reason:str=Field(min_length=5,max_length=2000)
class SimEvent(Body):attempt_id:str;event:str
class SimProvider(Body):outcome:str
class Replacement(RequestID):replacement_batch_id:str;reason:str


def create_app(settings=None, db=None):
    settings=(settings or Settings.from_env()).prepare()
    if len(settings.api_token)<32:raise RuntimeError('Set INVOICE_API_TOKEN to a generated secret of at least 32 characters; run scripts/bootstrap.py')
    db=db or Database(settings,role='api');signer=URLSafeTimedSerializer(session_signing_secret(settings),salt='invoice-session-v2')
    cache=SummaryCache(db);login_failures=defaultdict(deque);login_guard=RLock()
    app=FastAPI(title='Invoice Optimizer',version='0.1.0',docs_url=None,redoc_url=None,openapi_url=None)
    app.state.db=db;app.state.settings=settings
    assets=Path(__file__).parent
    app.mount('/static',StaticFiles(directory=assets/'static'),name='static')

    @app.exception_handler(ValueError)
    async def domain_error(request,exc):
        from .operations import Backpressure
        if isinstance(exc,Backpressure):
            return JSONResponse({'detail':str(exc),'retryable':True,'retry_after':exc.retry_after},status_code=429,
                headers={'Retry-After':str(exc.retry_after)})
        status=404 if isinstance(exc,NotFound) else (409 if isinstance(exc,(Conflict,Blocked)) else 422)
        return JSONResponse({'detail':str(exc)},status_code=status)

    @app.exception_handler(DBAPIError)
    async def database_error(request,exc):
        # The transaction rolled back; reads and request-ID commands are safe to retry.
        if is_timeout(exc):
            return JSONResponse({'detail':'The database is busy; retry shortly','retryable':True,'retry_after':2},
                status_code=503,headers={'Retry-After':'2'})
        raise exc

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request,exc):
        # Validation errors must not echo password or setup-token input values.
        if request.url.path in ('/api/login','/api/operators','/api/operators/bootstrap'):
            return JSONResponse({'detail':'Invalid account input. Check the username, display name and password requirements.'},status_code=422)
        return JSONResponse({'detail':[{'type':e['type'],'loc':list(e['loc']),'msg':e['msg']} for e in exc.errors()]},status_code=422)

    @app.exception_handler(PreviewUnavailable)
    async def preview_error(request,exc):
        busy=isinstance(exc,PreviewBusy)
        return JSONResponse({'detail':str(exc),'retryable':busy},status_code=429 if busy else 503,
            headers={'Retry-After':'2'} if busy else None)

    @app.middleware('http')
    async def headers(request,call_next):
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        if 'Referrer-Policy' not in response.headers:
            response.headers['Referrer-Policy']='no-referrer' if request.url.path=='/api/mail/gmail/callback' else 'same-origin'
        response.headers['X-Frame-Options']='SAMEORIGIN'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
        response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=(), browsing-topics=()'
        response.headers['Cross-Origin-Opener-Policy']='same-origin'
        if settings.https:response.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
        # Assets are revalidated on every load (ETag/Last-Modified -> 304), so a
        # deploy is never masked by a stale cached script.
        response.headers['Cache-Control']='no-cache' if request.url.path.startswith('/static/') else 'no-store'
        return response

    def setup_required():
        with db.transaction() as s:return count_operators(s)==0

    def legacy_identity():
        return {'actor':settings.actor,'user_id':None,'username':None,
                'display_name':'Shared workspace access','role':'read_only',
                'identity_type':'legacy','can_review':False}

    def shared_events(who,events):
        # As on /api/mail/gmail: the shared token sees Gmail activity, not the account address or origin.
        if who.get('identity_type')!='legacy':return events
        return [{**e,'entity_id':'gmail','details':{k:v for k,v in (e.get('details') or {}).items() if k not in ('expected_email','callback_url')}}
                if str(e.get('action') or '').startswith('gmail_') else e for e in events]
    def shared_summary(who,value):
        return value if who.get('identity_type')!='legacy' else {**value,'recent_events':shared_events(who,value.get('recent_events') or [])}

    def authenticate(request:Request,*,allow_readonly_post=False):
        auth=request.headers.get('Authorization','')
        bearer=bool(auth.startswith('Bearer ') and same_secret(auth[7:],settings.api_token))
        if bearer:data={**legacy_identity(),'csrf_token':None,'bearer':True}
        else:
            cookie=request.cookies.get('invoice_session')
            try:stored,issued_at=signer.loads(cookie or '',max_age=8*3600,return_timestamp=True)
            except (BadSignature,SignatureExpired):raise HTTPException(401,'Sign in to continue')
            if not isinstance(stored,dict):raise HTTPException(401,'Sign in to continue')
            if stored.get('operator_id'):
                with db.transaction() as s:
                    operator=s.get(Operator,stored['operator_id'])
                    if not operator or not operator.active or operator.credential_version!=stored.get('credential_version'):
                        raise HTTPException(401,'This personal session is no longer valid; sign in again')
                    data={**operator_identity(operator),'csrf_token':stored.get('csrf_token'),'bearer':False,'credential_version':operator.credential_version}
            else:data={**legacy_identity(),'csrf_token':stored.get('csrf_token'),'bearer':False}
            data['session_expires_at']=issued_at.timestamp()+8*3600
        if request.method not in ('GET','HEAD','OPTIONS'):
            if not bearer and (not data.get('csrf_token') or not same_secret(request.headers.get('X-CSRF-Token',''),data['csrf_token'])):
                raise HTTPException(403,'Invalid CSRF token')
            origin=request.headers.get('Origin')
            if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):raise HTTPException(403,'Cross-origin command rejected')
            if data['identity_type']!='operator' and request.url.path!='/api/logout' and not allow_readonly_post:
                raise HTTPException(403,'A personal operator account is required for changes. Shared workspace access is read-only; create the first operator or sign in with your username and password.')
        return data

    def identity(request:Request):return authenticate(request)
    def preview_identity(request:Request):return authenticate(request,allow_readonly_post=True)

    def administrator(who=Depends(identity)):
        if who.get('identity_type')!='operator' or who.get('role')!='admin':
            raise HTTPException(403,'An active administrator account is required')
        return who

    def check_login_rate(request,principal):
        ip=request.client.host if request.client else 'local';stamp=time.monotonic()
        credential_key=('credential',ip,*principal);ip_key=('ip',ip)
        with login_guard:
            if len(login_failures)>=2048:
                for key in list(login_failures):
                    q=login_failures[key]
                    while q and stamp-q[0]>60:q.popleft()
                    if not q:login_failures.pop(key,None)
                if len(login_failures)>=2048:raise HTTPException(429,'Too many attempts; retry in one minute')
            for key,maximum in ((ip_key,60),(credential_key,10)):
                q=login_failures[key]
                while q and stamp-q[0]>60:q.popleft()
                if len(q)>=maximum:raise HTTPException(429,'Too many attempts; retry in one minute')
            # Reserve both limits before expensive password verification. A
            # successful shared-token login cannot clear personal-login failures
            # or the aggregate IP rate limit.
            login_failures[ip_key].append(stamp);login_failures[credential_key].append(stamp)
        return credential_key

    def signed_response(operator=None):
        csrf=secrets.token_urlsafe(32)
        stored={'csrf_token':csrf}
        if operator:
            stored.update(operator_id=operator.id,credential_version=operator.credential_version)
            who=operator_identity(operator)
        else:who=legacy_identity()
        response=JSONResponse({'authenticated':True,**who,'csrf_token':csrf,
                               'mode':settings.mode,'setup_required':setup_required()})
        response.set_cookie('invoice_session',signer.dumps(stored),httponly=True,secure=settings.cookie_secure,samesite='strict',max_age=8*3600)
        return response

    @app.get('/')
    def index():return FileResponse(assets/'templates/index.html')

    @app.get('/health')
    def health():
        # Unauthenticated: liveness plus database reachability, nothing that fingerprints the deployment.
        with db.transaction() as s:s.execute(text('SELECT 1'))
        return {'status':'ok'}

    @app.post('/api/login')
    def login(body:Login,request:Request):
        personal=body.username is not None and body.password is not None and body.token is None
        principal=('personal',(body.username or '').strip().lower()) if personal else ('shared','workspace')
        attempt_key=check_login_rate(request,principal)
        origin=request.headers.get('Origin')
        if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):raise HTTPException(403,'Cross-origin sign-in rejected')
        operator=None
        if personal:
            try:username=normalize_username(body.username)
            except ValueError:username=''
            with db.transaction() as s:
                operator=s.scalar(select(Operator).where(Operator.username==username))
                valid=verify_password(body.password,operator.password_hash if operator else DUMMY_HASH)
                if not valid or not operator or not operator.active:raise HTTPException(401,'Invalid personal credentials')
                audit(s,'user:'+operator.id,'operator_signed_in',operator.id,{'username':operator.username})
        elif body.token is not None and body.username is None and body.password is None:
            if not same_secret(body.token,settings.api_token):raise HTTPException(401,'Invalid access token')
        else:raise HTTPException(401,'Provide personal credentials or a shared read-only access token')
        with login_guard:login_failures.pop(attempt_key,None)
        return signed_response(operator)

    @app.get('/api/auth/status')
    def auth_status():return {'setup_required':setup_required()}

    @app.post('/api/operators/bootstrap')
    def bootstrap_operator(body:OperatorBootstrap,request:Request):
        attempt_key=check_login_rate(request,('setup','workspace'))
        origin=request.headers.get('Origin')
        if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):raise HTTPException(403,'Cross-origin command rejected')
        # INVOICE_SETUP_TOKEN, when set, replaces the shared token here; it is
        # spent once an operator exists (the count check below runs under the account lock).
        if not same_secret(body.setup_token,settings.bootstrap_secret):raise HTTPException(401,'Invalid setup token')
        with db.transaction() as s:
            account_lock(s)
            if count_operators(s):raise Conflict('An operator already exists. Sign in with a personal account; an administrator can create additional accounts.')
            operator=create_operator(s,username=body.username,display_name=body.display_name,password=body.password,role='admin',actor='system:operator-bootstrap')
            audit(s,'user:'+operator.id,'operator_signed_in',operator.id,{'username':operator.username,'initial_setup':True,
                'setup_secret':'dedicated' if settings.setup_token else 'shared_token_fallback'})
        with login_guard:login_failures.pop(attempt_key,None)
        return signed_response(operator)

    @app.get('/api/operators')
    def operators(limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=1000),who=Depends(administrator)):
        with db.transaction() as s:return [public_operator(op) for op in s.scalars(select(Operator).order_by(Operator.username).limit(limit).offset(offset))]

    @app.post('/api/operators')
    def add_operator(body:OperatorCreate,who=Depends(administrator)):
        with db.transaction() as s:return public_operator(create_operator(s,**body.model_dump(),actor=who['actor']))

    @app.patch('/api/operators/{operator_id}')
    def update_operator(operator_id:str,body:OperatorUpdate,who=Depends(administrator)):
        with db.transaction() as s:
            account_lock(s)
            operator=s.get(Operator,operator_id,with_for_update=True)
            if not operator:raise NotFound('Operator not found')
            if not body.active:
                if operator.id==who['user_id']:raise Blocked('You cannot deactivate your own active session')
                if operator.active and operator.role=='admin' and s.scalar(select(func.count()).select_from(Operator).where(Operator.active.is_(True),Operator.role=='admin'))<=1:
                    raise Blocked('At least one active administrator must remain')
            if operator.active!=body.active:
                operator.active=body.active;operator.credential_version+=1
                audit(s,who['actor'],'operator_activated' if body.active else 'operator_deactivated',operator.id,{'username':operator.username})
            return public_operator(operator)

    @app.get('/api/session')
    def session(who=Depends(identity)):return {'authenticated':True,**who,'mode':settings.mode,'setup_required':setup_required()}
    @app.post('/api/logout')
    def logout(who=Depends(identity)):
        if who.get('user_id'):
            with db.transaction() as s:audit(s,who['actor'],'operator_signed_out',who['user_id'],{})
        r=JSONResponse({'ok':True});r.delete_cookie('invoice_session');return r
    @app.get('/api/dashboard')
    def summary(who=Depends(identity)):return shared_summary(who,cache.get())
    @app.get('/api/automation')
    def automation(who=Depends(identity)):
        from .operations import get_automation
        return get_automation(db)
    @app.get('/api/automation/status')
    def automation_status(who=Depends(identity)):
        from .scheduler import automation_status as status
        return {**status(db),'worker':worker_info(db)}
    @app.patch('/api/automation')
    def change_automation(body:AutomationUpdate,who=Depends(administrator)):
        from .operations import update_automation
        result=update_automation(db,**body.model_dump(),actor=who['actor'])
        with cache.guard:cache.checked=0.
        return result
    @app.get('/api/model')
    def model(who=Depends(identity)):return {**model_info(settings),'worker':worker_info(db)}

    @app.post('/api/model/train',status_code=202)
    def train(body:ModelTrain,who=Depends(administrator)):
        from .model_ops import queue_training
        return queue_training(db,**body.model_dump(),actor=who['actor'])

    @app.get('/api/model/runs')
    def training_runs(limit:int=Query(20,ge=1,le=20),who=Depends(identity)):
        from .model_ops import ModelTrainingRun, serialize_training_run
        with db.transaction() as s:
            return [serialize_training_run(run) for run in s.scalars(select(ModelTrainingRun).order_by(ModelTrainingRun.created_at.desc(),ModelTrainingRun.id).limit(limit))]

    @app.post('/api/model/runs/{run_id}/retry',status_code=202)
    def retry_model_activation(run_id:str,body:RequestID,who=Depends(administrator)):
        from .model_ops import retry_activation
        return retry_activation(db,run_id,body.request_id,who['actor'])

    @app.get('/api/model/runs/{run_id}/files/{kind}')
    def training_file(run_id:str,kind:str,who=Depends(identity)):
        from .model_ops import ModelTrainingRun
        if kind not in ('training','manifest'):raise NotFound('Unknown model-training file')
        with db.transaction() as s:
            run=s.get(ModelTrainingRun,run_id)
            key=(run.training_sha256 if kind=='training' else run.manifest_sha256) if run else None
            if not key:raise NotFound('Model-training file is not available')
        try:path=ObjectStore(settings.data_dir/'objects').path(key,verify=True)
        except FileNotFoundError:raise NotFound('Retained model-training file is missing')
        extension='parquet' if kind=='training' else 'json'
        return FileResponse(path,media_type='application/octet-stream' if kind=='training' else 'application/json',
                            filename=f'{kind}-{run_id}.{extension}',content_disposition_type='attachment')

    @app.get('/api/events')
    async def events(request:Request,who=Depends(identity)):
        async def stream():
            last=None;last_heartbeat=0
            while not await request.is_disconnected():
                if who.get('session_expires_at') and time.time()>=who['session_expires_at']:
                    yield 'event: auth_expired\ndata: {"detail":"Sign in again to continue"}\n\n'
                    break
                # SSE is a long-lived request, so the initial auth dependency
                # alone cannot revoke a stream after account deactivation.
                if who.get('user_id'):
                    def current_operator_session():
                        with db.transaction() as s:
                            op=s.get(Operator,who['user_id'])
                            return bool(op and op.active and op.credential_version==who.get('credential_version'))
                    if not await asyncio.to_thread(current_operator_session):
                        yield 'event: auth_expired\ndata: {"detail":"Sign in again to continue"}\n\n'
                        break
                value=shared_summary(who,await asyncio.to_thread(cache.get))
                token=value['stream_version']
                if token!=last:
                    # A slow/reconnected client may have missed coalesced cache
                    # generations. Invalidate conservatively instead of losing
                    # resource changes from an earlier intermediate generation.
                    resources=value['changed_resources'] if last==value.get('previous_stream_version') else ALL_RESOURCES
                    yield f"id: {token}\nevent: update\ndata: {canonical({'revision':value['revision'],'stream_version':token,'changed_resources':resources,'summary':value})}\n\n";last=token
                elif time.monotonic()-last_heartbeat>15:
                    yield ': keepalive\n\n';last_heartbeat=time.monotonic()
                await asyncio.sleep(min(settings.poll_seconds,30))
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

    @app.get('/api/customers')
    def customers(limit:int=Query(200,ge=1,le=1000),offset:int=Query(0,ge=0,le=100000),who=Depends(identity)):
        with db.read_transaction() as s:return [serial(x) for x in s.scalars(select(Customer).order_by(Customer.id).limit(limit).offset(offset))]

    # Read-only statistical signals (docs/INSIGHTS.md): one REPEATABLE READ READ
    # ONLY snapshot on the read engine per computation, cached for 30 s per process.
    insight_reader=InsightReader(db);insight_cache=InsightCache()
    def insight_periods(customer):
        # Loose index scan over movements(period) / (customer_id, period): at most 120 probes.
        cw='' if customer is None else ' AND m.customer_id=:customer'
        sql=f"""WITH RECURSIVE p(period) AS (
            (SELECT m.period FROM movements m WHERE true{cw} ORDER BY m.period DESC LIMIT 1)
            UNION ALL
            SELECT (SELECT m.period FROM movements m WHERE m.period<p.period{cw} ORDER BY m.period DESC LIMIT 1) FROM p WHERE p.period IS NOT NULL)
            SELECT period FROM p WHERE period IS NOT NULL LIMIT 120"""
        with db.read_transaction() as s:return list(s.scalars(text(sql),{'customer':customer}))
    def insight_customer(customer):
        if customer is not None and not customer.strip():raise ValueError('customer must be 1..80 characters')
        # PostgreSQL text cannot hold NUL: reject it here rather than fail the query with a 500.
        if customer is not None and '\x00' in customer:raise ValueError('customer must not contain NUL characters')
        return customer
    def insight_cached(response,key,compute):
        value,hit=insight_cache.get(key,compute)
        response.headers['X-Insights-Cache']='hit' if hit else 'miss'
        return value
    def insight_period(period,customer):
        if period is not None:return period
        periods,_=insight_cache.get(('periods',customer),lambda:insight_periods(customer))
        if not periods:raise NotFound('No movements have been recorded for this scope yet')
        return periods[0]
    insight_period_query=Query(None,pattern=INSIGHT_PERIOD,description='Accounting period YYYY-MM; defaults to the latest period with movements')
    insight_customer_query=Query(None,min_length=1,max_length=80)

    @app.get('/api/insights/periods')
    def insight_period_list(response:Response,customer:str|None=insight_customer_query,who=Depends(identity)):
        customer=insight_customer(customer)
        periods=insight_cached(response,('periods',customer),lambda:insight_periods(customer))
        return {'periods':periods,'latest':periods[0] if periods else None,'customer_id':customer,'cache_seconds':INSIGHT_TTL_SECONDS}

    @app.get('/api/insights/report')
    def insight_report(response:Response,period:str|None=insight_period_query,customer:str|None=insight_customer_query,
                       limit:int=Query(20,ge=1,le=100),who=Depends(identity)):
        from . import insights
        customer=insight_customer(customer);period=insight_period(period,customer)
        return insight_cached(response,('report',period,customer,limit),lambda:insights.report(insight_reader,period,customer,limit))

    @app.get('/api/insights/{section}')
    def insight_section(section:str,response:Response,period:str|None=insight_period_query,customer:str|None=insight_customer_query,
                        limit:int=Query(20,ge=1,le=100),window_days:int|None=Query(None,ge=0,le=92),
                        threshold:float|None=Query(None,gt=0,le=100),min_sample:int|None=Query(None,ge=3,le=100000),who=Depends(identity)):
        from . import insights
        name=INSIGHT_SECTIONS.get(section)
        if not name:raise NotFound('Unknown insight section')
        options={k:v for k,v in (('window_days',window_days),('threshold',threshold),('min_sample',min_sample)) if v is not None}
        extra=set(options)-{'duplicates':{'window_days'},'outliers':{'threshold','min_sample'}}.get(section,set())
        if extra:raise ValueError(f"{', '.join(sorted(extra))} does not apply to the {section} insight")
        customer=insight_customer(customer);period=insight_period(period,customer)
        compute=getattr(insights,name)
        return insight_cached(response,('section',section,period,customer,limit,tuple(sorted(options.items()))),
                              lambda:compute(insight_reader,period,customer,limit,**options))

    def filtered_cases(query,*,status=None,customer_id=None,search=None,reason=None,severity=None,min_score=None,max_score=None,sort='newest'):
        if status:query=query.where(ReviewCase.status==status)
        if customer_id:query=query.where(ReviewCase.customer_id==customer_id)
        if reason:query=query.where(ReviewCase.reason==reason)
        if severity:query=query.where(ReviewCase.severity==severity)
        if min_score is not None:query=query.where(Assessment.score>=min_score)
        if max_score is not None:query=query.where(Assessment.score<=max_score)
        if min_score is not None and max_score is not None and min_score>max_score:raise ValueError('Minimum score cannot exceed maximum score')
        if search and search.strip():
            if len(search.strip())<3:raise ValueError('Movement ID prefix must contain at least three characters')
            value=search.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            query=query.where(func.lower(Movement.external_id).like(value.lower()+'%',escape='\\'))
        sorts={'newest':ReviewCase.created_at.desc(),'oldest':ReviewCase.created_at.asc(),
               'score_desc':Assessment.score.desc().nullslast(),'score_asc':Assessment.score.asc().nullslast(),
               'customer':ReviewCase.customer_id.asc().nullslast()}
        if sort not in sorts:raise ValueError('Unknown review sort order')
        return query.order_by(sorts[sort],ReviewCase.id)

    case_keysets={'newest':Keyset('cases:newest',ReviewCase.created_at,ReviewCase.id,True,datetime.fromisoformat,lambda r:r[0].created_at),
        'oldest':Keyset('cases:oldest',ReviewCase.created_at,ReviewCase.id,False,datetime.fromisoformat,lambda r:r[0].created_at),
        'score_desc':Keyset('cases:score_desc',Assessment.score,ReviewCase.id,True,float,lambda r:r[2]),
        'score_asc':Keyset('cases:score_asc',Assessment.score,ReviewCase.id,False,float,lambda r:r[2]),
        'customer':Keyset('cases:customer',ReviewCase.customer_id,ReviewCase.id,False,str,lambda r:r[0].customer_id)}

    def case_rows(s,status,limit,offset,customer_id=None,actor=None,cursor=None,**filters):
        q=select(ReviewCase,MovementVersion,Assessment.score).outerjoin(MovementVersion,MovementVersion.id==ReviewCase.version_id).outerjoin(Assessment,(Assessment.version_id==ReviewCase.version_id)&(Assessment.kind=='MODEL')).outerjoin(Movement,Movement.id==ReviewCase.movement_id)
        q=filtered_cases(q,status=status,customer_id=customer_id,**filters)
        keyset=case_keysets[filters.get('sort','newest')]
        rows=list(s.execute(keyset.where(q,cursor).limit(limit+1).offset(offset)))
        next_cursor=keyset.next(rows,limit,lambda r:r[0].id);rows=rows[:limit]
        items=[c for c,_,_ in rows];lineage=case_sources(s,items)
        movements={m.id:m for m in s.scalars(select(Movement).where(Movement.id.in_([c.movement_id for c in items])))}
        allocated=set(s.scalars(select(Allocation.movement_id).where(Allocation.movement_id.in_(movements))))
        assessments={(a.version_id,a.kind):a for a in s.scalars(select(Assessment).where(Assessment.version_id.in_([c.version_id for c in items])))}
        opens=dict(s.execute(select(ReviewCase.version_id,func.count()).where(ReviewCase.version_id.in_([c.version_id for c in items]),ReviewCase.status=='OPEN').group_by(ReviewCase.version_id)).all())
        reviewer_labels=actor_attributions(s,[c.reviewed_by for c in items])
        postponed=personal_postponements(s,actor,'CASE',[(c.id,case_binding(c,version.version_no if version else None)) for c,version,_ in rows])
        result=[]
        for c,version,score in rows:
            v=version.version_no if version else None
            movement=movements.get(c.movement_id);rule=assessments.get((c.version_id,'RULE'));model=assessments.get((c.version_id,'MODEL'))
            eligible=bool(c.status=='OPEN' and c.reason=='MODEL_RISK' and movement and movement.current_version==v and movement.state=='REVIEW' and movement.id not in allocated and rule and rule.status=='PASS' and model and model.status=='SCORED' and model.flagged and score is not None and 0<=score<=1 and all((model.model_version,model.policy_version,model.feature_schema_version)) and isinstance(model.features,dict) and opens.get(c.version_id)==1)
            result.append({**serial(c),'reviewed_by_attribution':reviewer_labels.get(c.reviewed_by),'version_no':v,'score':score,'external_movement_id':movement.external_id if movement else None,'current_version':movement.current_version if movement else None,'release_eligible':eligible,'personal_postponement':postponed.get(c.id),**case_card(version,rule,model),**lineage[c.id]})
        return result,next_cursor

    def no_offset_with_cursor(offset,cursor):
        if cursor is not None and offset:raise ValueError('Use either offset or cursor pagination, not both')

    @app.get('/api/cases')
    def cases(status:str|None=None,customer_id:str|None=None,q:str|None=Query(None,max_length=100),reason:str|None=Query(None,max_length=240),severity:str|None=Query(None,max_length=20),min_score:float|None=Query(None,ge=0,le=1),max_score:float|None=Query(None,ge=0,le=1),sort:str='newest',limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),cursor:str|None=Query(None,max_length=1024),response:Response=None,who=Depends(identity)):
        no_offset_with_cursor(offset,cursor)
        with db.read_transaction() as s:
            items,next_cursor=case_rows(s,status,limit,offset,customer_id,actor=who['actor'] if who.get('identity_type')=='operator' else None,cursor=cursor or None,search=q,reason=reason,severity=severity,min_score=min_score,max_score=max_score,sort=sort)
        return page(items,next_cursor,limit,cursor,response)
    @app.get('/api/case-drivers')
    def case_drivers(case_id:list[str]=Query(...,min_length=1,max_length=50),who=Depends(identity)):
        from .review_card_drivers import case_driver_page
        return case_driver_page(db,case_id)
    @app.post('/api/review/postpone')
    def postpone_review(body:PersonalTriage,who=Depends(identity)):
        return postpone(db,**body.model_dump(),actor=who['actor'])
    @app.post('/api/review/resume')
    def resume_review(body:PersonalTriage,who=Depends(identity)):
        return resume(db,**body.model_dump(),actor=who['actor'])
    @app.post('/api/cases/bulk-release')
    def bulk_release(body:BulkRelease,who=Depends(identity)):
        from .bulk_review import bulk_release_cases
        return bulk_release_cases(db,**body.model_dump(),actor=who['actor'])
    @app.get('/api/cases/{case_id}')
    def case_detail(case_id:str,who=Depends(identity)):
        with db.transaction() as s:
            c=s.get(ReviewCase,case_id)
            if not c:raise NotFound('Case not found')
            m=s.get(Movement,c.movement_id) if c.movement_id else None
            v=s.get(MovementVersion,c.version_id) if c.version_id else None
            assessments=list(s.scalars(select(Assessment).where(Assessment.version_id==c.version_id))) if c.version_id else []
            sources=list(s.scalars(select(SourceRecord).where(SourceRecord.id.in_(v.source_refs)))) if v else []
            case=serial(c);case['version_no']=v.version_no if v else None;case['score']=next((a.score for a in assessments if a.kind=='MODEL'),None)
            case['reviewed_by_attribution']=actor_attributions(s,[c.reviewed_by]).get(c.reviewed_by)
            quarantine=None
            if not m:
                qid=c.dedup_key.removeprefix('quarantine:');quarantine=serial(s.get(Quarantine,qid))
            latest=list(s.scalars(select(SourceRecord).where(SourceRecord.movement_id==m.id).order_by(SourceRecord.received_at.desc()).limit(20))) if m and c.reason=='SOURCE_CHANGE_AFTER_ISSUE' else []
            batches={b.id:file_reference(b) for b in s.scalars(select(Batch).where(Batch.id.in_([r.batch_id for r in sources+latest])))}
            def observation(r):return {**serial(r),'source_file':batches.get(r.batch_id)}
            return {'case':case,'movement':serial(m),'version':serial(v),'assessments':[serial(a) for a in assessments],'observations':[observation(r) for r in sources],'quarantine':quarantine,'latest_observations':[observation(r) for r in latest],**case_sources(s,[c])[c.id]}

    @app.get('/api/cases/{case_id}/versions')
    def versions(case_id:str,limit:int=Query(5,ge=1,le=10),cursor:str|None=Query(None,max_length=1024),who=Depends(identity)):
        from .review_versions import case_versions
        return case_versions(db,case_id,limit=limit,cursor=cursor)

    def case_model_snapshot(case_id):
        with db.transaction() as s:
            case=s.get(ReviewCase,case_id)
            if not case:raise NotFound('Case not found')
            version=s.get(MovementVersion,case.version_id) if case.version_id else None
            movement=s.get(Movement,case.movement_id) if case.movement_id else None
            assessment=s.scalar(select(Assessment).where(Assessment.version_id==case.version_id,Assessment.kind=='MODEL')) if version else None
            return case,version,movement,assessment

    def recorded_model_explanation(case,version,movement,assessment):
        from .ml import explain_recorded
        from .features import build_features
        from .risk_explanations import describe_model_result
        explanation=(explain_recorded(assessment.features,settings.model_dir,assessment.model_version,
                    assessment.score,assessment.policy_version) if assessment and assessment.status=='SCORED' else
                    {'status':'UNAVAILABLE','reason':'This case has no recorded model score to explain'})
        payload=deepcopy(version.payload) if version else {}
        preview_reason=None
        try:
            if not version or not movement or movement.current_version!=version.version_no:raise ValueError('This case is not for the current movement version')
            if not assessment or explanation.get('status')!='AVAILABLE':raise ValueError('The exact frozen model explanation is unavailable')
            if version.currency!='GBP' or payload.get('currency')!='GBP':raise ValueError('Synthetic previews support GBP movements only')
            if build_features(payload)!=assessment.features:raise ValueError('Source-version inputs do not reproduce the recorded features')
        except (ValueError,TypeError,ArithmeticError) as exc:preview_reason=str(exc)
        keys=('amount','expected_amount','customer_age_days','supplier_age_days','cross_border','event_at')
        inputs={key:payload.get(key) for key in keys} if version else {}
        for key in ('amount','expected_amount'):
            if inputs.get(key) is not None:inputs[key]=str(inputs[key])
        for key in ('customer_age_days','supplier_age_days','cross_border'):
            if inputs.get(key) is not None:
                try:inputs[key]=int(inputs[key])
                except (TypeError,ValueError):pass
        narrative=describe_model_result(explanation,payload=payload,
                    feature_values=assessment.features if assessment else None)
        return {'case_id':case.id,'case_reason':case.reason,'case_status':case.status,
                'version_id':version.id if version else None,
                'version_no':version.version_no if version else None,
                'current_version':movement.current_version if movement else None,
                'assessment_id':assessment.id if assessment else None,
                'model_version':assessment.model_version if assessment else None,
                'policy_version':assessment.policy_version if assessment else None,
                'recorded_score':assessment.score if assessment else None,
                'explanation':explanation,'narrative':narrative,'editable_inputs':inputs,
                'preview_available':preview_reason is None,'preview_unavailable_reason':preview_reason,
                'hypothetical':False,'currency':version.currency if version else None}

    @app.get('/api/cases/{case_id}/model/explanation')
    def model_explanation(case_id:str,who=Depends(identity)):
        return recorded_model_explanation(*case_model_snapshot(case_id))

    @app.post('/api/cases/{case_id}/model/preview')
    def model_preview(case_id:str,body:ModelPreview,who=Depends(preview_identity)):
        from .features import build_features, parse_event_at
        from .ml import explain_features
        from .risk_explanations import describe_model_result
        if not settings.synthetic:raise Blocked('Hypothetical model previews are simulation only')
        case,version,movement,assessment=case_model_snapshot(case_id)
        baseline=recorded_model_explanation(case,version,movement,assessment)
        if not version or not movement or version.version_no!=body.expected_version or movement.current_version!=body.expected_version:
            raise Conflict('The movement version changed. Refresh this case before previewing its frozen model.')
        if not baseline['preview_available']:raise Blocked(baseline['preview_unavailable_reason'])
        allowed={'amount','expected_amount','customer_age_days','supplier_age_days','cross_border','event_at'}
        if set(body.patch)-allowed:raise ValueError('Only the six declared model inputs can be previewed')
        payload=deepcopy(version.payload)
        for key,value in body.patch.items():
            if key in ('amount','expected_amount'):
                if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,18}(\.[0-9]{1,2})?',value):raise ValueError(key+' must be an exact decimal string with at most two decimal places')
            elif key in ('customer_age_days','supplier_age_days'):
                if type(value) is not int or not 0<=value<=365250:raise ValueError(key+' must be an integer between 0 and 365250')
            elif key=='cross_border':
                if type(value) is not int or value not in (0,1):raise ValueError('cross_border must be integer 0 or 1')
            elif key=='event_at':
                if not isinstance(value,str) or len(value)>64:raise ValueError('event_at must be an ISO timestamp with a timezone')
                event=parse_event_at(value)
                if event.strftime('%Y-%m')!=case.period:raise ValueError('Hypothetical event time must remain in the case period in UTC')
                if 'hour_utc' in payload:payload['hour_utc']=event.hour
            payload[key]=value
        features=build_features(payload)
        after=explain_features(features,settings.model_dir,assessment.model_version,assessment.policy_version,payload=payload)
        keys=('amount','expected_amount','customer_age_days','supplier_age_days','cross_border','event_at')
        return {'case_id':case.id,'version_id':version.id,'version_no':version.version_no,
                'assessment_id':assessment.id,'model_version':assessment.model_version,
                'policy_version':assessment.policy_version,'hypothetical':True,
                'before':baseline['explanation'],'after':after,
                'before_narrative':baseline['narrative'],
                'after_narrative':describe_model_result(after,payload=payload,feature_values=features),
                'editable_inputs':{key:payload.get(key) for key in keys},
                'original_inputs':baseline['editable_inputs'],'changed_inputs':body.patch,
                'persisted':False,'deterministic_checks_recomputed':False,
                'notice':'Hypothetical model calculation only. Source data, assessments, review decisions and invoice approvals are unchanged.'}
    @app.get('/api/cases/{case_id}/history')
    def case_history(case_id:str,limit:int=Query(20,ge=1,le=50),before_id:int|None=Query(None,ge=1),legacy_before_id:int|None=Query(None,ge=1),attempt_before_id:int|None=Query(None,ge=1),who=Depends(identity)):
        from .review_history import ReviewEvent, serialize_review_event
        with db.transaction() as s:
            case=s.get(ReviewCase,case_id)
            if not case:raise NotFound('Case not found')
            query=select(ReviewEvent).where(ReviewEvent.case_id==case_id)
            if before_id is not None:query=query.where(ReviewEvent.id<before_id)
            events=list(s.scalars(query.order_by(ReviewEvent.id.desc()).limit(limit+1)))
            # Older records are retained exactly as recorded. A shared actor
            # identifies a credential/context, not a retrospectively known person.
            legacy=select(Audit).where(
                Audit.entity_id.in_([case_id,case.movement_id] if case.movement_id else [case_id]),
                Audit.action.in_(['case_reviewed','correction_applied']),
                ~Audit.details.has_key('review_event_id'),
                ~Audit.details.has_key('review_event_ids'))
            if legacy_before_id is not None:legacy=legacy.where(Audit.id<legacy_before_id)
            older=list(s.scalars(legacy.order_by(Audit.id.desc()).limit(limit+1)))
            attempted=select(Audit).where(Audit.entity_id==case_id,
                Audit.action.in_(['case_review_rejected','case_correction_rejected']))
            if attempt_before_id is not None:attempted=attempted.where(Audit.id<attempt_before_id)
            attempts=list(s.scalars(attempted.order_by(Audit.id.desc()).limit(limit+1)))
            return {'items':[serialize_review_event(e) for e in events[:limit]],
                    'next_cursor':events[limit-1].id if len(events)>limit else None,
                    'attempts':[{**serial(a),'reviewer':a.details.get('reviewer'),
                                 'attempted_at':a.created_at.isoformat(),'result':'REJECTED',
                                 'request_id':a.details.get('request_id'),'reason':a.details.get('reason'),
                                 'error':a.details.get('error')} for a in attempts[:limit]],
                    'attempt_next_cursor':attempts[limit-1].id if len(attempts)>limit else None,
                    'legacy_items':[{**serial(a),'identity_kind':'LEGACY_SHARED_OR_LOCAL_ACTOR',
                                     'person_verified':False,'version_verified':False,
                                     'case_attribution':'DIRECT_CASE_EVENT' if a.entity_id==case_id else 'LEGACY_MOVEMENT_EVENT_CASE_UNAVAILABLE'} for a in older[:limit]],
                    'legacy_next_cursor':older[limit-1].id if len(older)>limit else None,
                    'legacy_truncated':len(older)>limit,
                    'legacy_note':'Historical shared or local actor labels cannot establish which person performed an action. Their original timestamps and details are preserved; missing version evidence is not inferred.'}
    @app.post('/api/cases/{case_id}/correct')
    def correct(case_id:str,body:Correction,who=Depends(identity)):return correct_case(db,case_id,**body.model_dump(),actor=who['actor'])
    @app.post('/api/cases/{case_id}/resolve')
    def resolve(case_id:str,body:Resolution,who=Depends(identity)):return resolve_case(db,case_id,**body.model_dump(),actor=who['actor'])

    invoice_keyset=Keyset('invoices',Invoice.created_at,Invoice.id,True,datetime.fromisoformat,lambda r:r.created_at)
    @app.get('/api/invoices')
    def invoices(status:str|None=None,customer_id:str|None=None,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),cursor:str|None=Query(None,max_length=1024),response:Response=None,who=Depends(identity)):
        no_offset_with_cursor(offset,cursor)
        with db.read_transaction() as s:
            columns=[column for column in Invoice.__table__.columns if column.name not in ('document','package','line_format','movement_count','movement_vat_policy')]
            # Derived columns are set at freeze; NULL line_format marks a row not
            # yet backfilled by migration 8, read from the document instead.
            derived=Invoice.line_format.isnot(None)
            q=select(*columns,func.coalesce(Invoice.line_format,Invoice.document['line_format'].astext,'SUMMARY').label('line_format'),
                     sql_case((derived,Invoice.movement_count),else_=Invoice.document['movement_count'].as_integer()).label('line_count'),
                     sql_case((derived,Invoice.movement_vat_policy),else_=Invoice.document['movement_vat']['policy'].astext).label('movement_vat_policy'))
            if status:q=q.where(Invoice.status==status)
            if customer_id:q=q.where(Invoice.customer_id==customer_id)
            items=list(s.execute(invoice_keyset.where(q,cursor or None).order_by(Invoice.created_at.desc(),Invoice.id).limit(limit+1).offset(offset)))
            next_cursor=invoice_keyset.next(items,limit,lambda r:r.id);items=items[:limit]
            lineage=invoice_previews(s,items)
            postponed=personal_postponements(s,who['actor'] if who.get('identity_type')=='operator' else None,'INVOICE',[(item.id,invoice_binding(item)) for item in items])
            return page([{**{key:(str(value) if isinstance(value,(datetime,Decimal)) else value) for key,value in x._mapping.items()},
                     'pdf_ready':bool(x.pdf_key),'personal_postponement':postponed.get(x.id),**lineage[x.id]} for x in items],next_cursor,limit,cursor,response)

    def movement_details(s,invoice_id,limit=25,offset=0):
        # The database returns only this page; detailed documents are never
        # copied in full into Python or sent to the browser by these endpoints.
        count=s.execute(text("SELECT jsonb_array_length(COALESCE(document->'movement_detail','[]'::jsonb)) FROM invoices WHERE id=:id"),{'id':invoice_id}).scalar_one_or_none()
        if count is None:raise NotFound('Invoice not found')
        items=list(s.scalars(text("""SELECT detail.item FROM invoices i
            CROSS JOIN LATERAL jsonb_array_elements(COALESCE(i.document->'movement_detail','[]'::jsonb))
                WITH ORDINALITY AS detail(item,position)
            WHERE i.id=:id ORDER BY detail.position LIMIT :limit OFFSET :offset"""),
            {'id':invoice_id,'limit':limit,'offset':offset}))
        return {'items':items,'total':count,'limit':limit,'offset':offset,'has_more':offset+len(items)<count}

    @app.get('/api/invoices/{invoice_id}/movements')
    def invoice_movements(invoice_id:str,limit:int=Query(25,ge=1,le=100),offset:int=Query(0,ge=0,le=10000),who=Depends(identity)):
        with db.transaction() as s:return movement_details(s,invoice_id,limit,offset)

    @app.get('/api/invoices/{invoice_id}/package')
    def package(invoice_id:str,who=Depends(identity)):
        with db.transaction() as s:
            member_count=select(func.count()).select_from(InvoiceMember).where(InvoiceMember.invoice_id==Invoice.id).correlate(Invoice).scalar_subquery()
            columns=[column for column in Invoice.__table__.columns if column.name not in ('document','package')]
            inv=s.execute(select(*columns,Invoice.document.op('-')('movement_detail').label('document'),
                Invoice.package['message'].label('message'),func.coalesce(Invoice.document['line_format'].astext,'SUMMARY').label('line_format'),
                func.coalesce(Invoice.document['movement_count'].as_integer(),member_count).label('line_count')).where(Invoice.id==invoice_id)).first()
            if not inv:raise NotFound('Invoice not found')
            return {'id':inv.id,'status':inv.status,'fingerprint':inv.fingerprint,'document':inv.document,
                'line_format':inv.line_format,'line_count':inv.line_count,'movement_details':movement_details(s,inv.id),
                'movement_vat_policy':(inv.document.get('movement_vat') or {}).get('policy'),
                'message':inv.message or {},'pdf_ready':bool(inv.pdf_key),'pdf_url':f'/api/invoices/{inv.id}/pdf','pdf_sha256':inv.pdf_sha256,'snapshot_sha256':inv.snapshot_hash,
                'approved_by':inv.approved_by,'approved_at':str(inv.approved_at) if inv.approved_at else None,
                'approved_by_attribution':actor_attributions(s,[inv.approved_by]).get(inv.approved_by),**invoice_sources_page(s,inv,limit=6)}
    @app.get('/api/invoices/{invoice_id}/sources')
    def invoice_sources(invoice_id:str,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),who=Depends(identity)):
        with db.transaction() as s:
            inv=s.get(Invoice,invoice_id,options=[defer(Invoice.document),defer(Invoice.package)])
            if not inv:raise NotFound('Invoice not found')
            return invoice_sources_page(s,inv,limit,offset)
    @app.get('/api/invoices/{invoice_id}/pdf')
    def pdf(invoice_id:str,who=Depends(identity)):
        with db.transaction() as s:
            inv=s.get(Invoice,invoice_id,options=[defer(Invoice.document),defer(Invoice.package)])
            if not inv or not inv.pdf_key:raise NotFound('PDF is not ready')
            path=ObjectStore(settings.data_dir/'objects').path(inv.pdf_key,verify=True)
            return FileResponse(path,media_type='application/pdf',filename=inv.number+'.pdf',content_disposition_type='inline')
    def invoice_preview_key(invoice_id,expected=None):
        with db.transaction() as s:
            row=s.execute(select(Invoice.pdf_key,Invoice.pdf_sha256).where(Invoice.id==invoice_id)).one_or_none()
            if not row or not row.pdf_key:raise NotFound('PDF is not ready')
            if row.pdf_key!=row.pdf_sha256:raise Conflict('The retained PDF identity does not match this package')
            if expected is not None and expected!=row.pdf_sha256:raise Conflict('The PDF has changed. Refresh the package before previewing.')
            return row.pdf_sha256
    @app.get('/api/invoices/{invoice_id}/preview')
    def invoice_preview(invoice_id:str,who=Depends(identity)):
        key=invoice_preview_key(invoice_id)
        result=preview_info(settings,key)
        return {**result,'page_url':f'/api/invoices/{invoice_id}/preview/pages/1?sha256={key}',
            'source':'EXACT_RETAINED_PDF','read_only':True}
    @app.get('/api/invoices/{invoice_id}/preview/pages/{page}')
    def invoice_preview_page(invoice_id:str,page:int,sha256:str=Query(pattern=r'^[a-f0-9]{64}$'),who=Depends(identity)):
        key=invoice_preview_key(invoice_id,sha256)
        handle=open_page(settings,key,page)
        size=handle.seek(0,2);handle.seek(0)
        def chunks():
            try:
                while chunk:=handle.read(64*1024):yield chunk
            finally:handle.close()
        return StreamingResponse(chunks(),media_type='image/png',
            headers={'X-PDF-SHA256':key,'X-PDF-Page':str(page),'Content-Length':str(size)},
            background=BackgroundTask(handle.close))
    @app.post('/api/close')
    def close(body:Close,who=Depends(identity)):return create_invoice(db,**body.model_dump(),actor=who['actor'])
    @app.post('/api/invoices/{invoice_id}/format')
    def invoice_format(invoice_id:str,body:InvoiceFormat,who=Depends(identity)):
        from .billing import regenerate_invoice_format
        return regenerate_invoice_format(db,invoice_id,body.fingerprint,body.line_format,body.request_id,who['actor'])
    @app.post('/api/invoices/{invoice_id}/approve')
    def approve(invoice_id:str,body:Approval,who=Depends(identity)):return approve_invoice(db,invoice_id,**body.model_dump(),actor=who['actor'])
    @app.post('/api/invoices/{invoice_id}/revoke')
    def revoke(invoice_id:str,body:Revoke,who=Depends(identity)):return revoke_invoice(db,invoice_id,**body.model_dump(),actor=who['actor'])
    @app.post('/api/invoices/{invoice_id}/reject')
    def reject_package(invoice_id:str,body:InvoiceDecision,who=Depends(identity)):
        return reject_invoice(db,invoice_id,**body.model_dump(),actor=who['actor'])
    @app.post('/api/invoices/{invoice_id}/reopen')
    def reopen_package(invoice_id:str,body:InvoiceDecision,who=Depends(identity)):
        return reopen_invoice(db,invoice_id,**body.model_dump(),actor=who['actor'])
    @app.post('/api/invoices/{invoice_id}/dispatch')
    def dispatch(invoice_id:str,body:RequestID,who=Depends(identity)):
        from .delivery import mail_readiness
        readiness=mail_readiness(db.settings)
        if not readiness['ready']:raise Blocked(readiness['reason'])
        return dispatch_invoice(db,invoice_id,body.request_id,who['actor'])

    outbox_keyset=Keyset('outbox',Outbox.created_at,Outbox.id,True,datetime.fromisoformat,lambda r:r.created_at)
    @app.get('/api/outbox')
    def outbox(customer_id:str|None=None,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),cursor:str|None=Query(None,max_length=1024),response:Response=None,who=Depends(identity)):
        no_offset_with_cursor(offset,cursor)
        with db.read_transaction() as s:
            query=select(Outbox).outerjoin(Invoice,Invoice.id==Outbox.invoice_id).outerjoin(ReviewCase,ReviewCase.id==Outbox.case_id)
            if customer_id:query=query.where(or_(Invoice.customer_id==customer_id,ReviewCase.customer_id==customer_id))
            items=list(s.scalars(outbox_keyset.where(query,cursor or None).order_by(Outbox.created_at.desc(),Outbox.id).limit(limit+1).offset(offset)))
            next_cursor=outbox_keyset.next(items,limit,lambda r:r.id);items=items[:limit]
            invs={v.id:v for v in s.scalars(select(Invoice).where(Invoice.id.in_([x.invoice_id for x in items if x.invoice_id])))}
            cases={c.id:c for c in s.scalars(select(ReviewCase).where(ReviewCase.id.in_([x.case_id for x in items if x.case_id])))}
            invoice_files=invoice_previews(s,list(invs.values()));case_files=case_sources(s,list(cases.values()))
            attempts=list(s.scalars(select(DeliveryAttempt).where(DeliveryAttempt.outbox_id.in_([x.id for x in items])).order_by(DeliveryAttempt.created_at)))
            events=list(s.scalars(select(DeliveryEvent).where(DeliveryEvent.attempt_id.in_([a.id for a in attempts])).order_by(DeliveryEvent.created_at)))
            result=[]
            for item in items:
                matches=[a for a in attempts if a.outbox_id==item.id];ev=[e for e in events if e.attempt_id in {a.id for a in matches}]
                evidence={e.event for e in ev}
                delivery_status='BOUNCED' if 'bounced' in evidence else ('DELIVERED' if 'delivered' in evidence else 'UNKNOWN')
                message=item.payload.get('message',item.payload)
                owner=invs.get(item.invoice_id) or cases.get(item.case_id)
                lineage=invoice_files.get(item.invoice_id) or case_files.get(item.case_id) or {'source_files':[],'provenance_status':'UNAVAILABLE'}
                result.append({**serial(item),'customer_id':owner.customer_id if owner else None,**lineage,'attempts':[serial(a) for a in matches],'attempt_count':item.attempts,'attempt_id':matches[-1].id if matches else None,'events':[serial(e) for e in ev],'recipient':', '.join(message.get('to',[])),'delivery_status':delivery_status,'opened_signal':'opened' in evidence})
            return page(result,next_cursor,limit,cursor,response)
    @app.get('/api/batches')
    def batches(customer_id:str|None=None,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),who=Depends(identity)):
        with db.read_transaction() as s:
            query=select(Batch)
            if customer_id:
                records=select(BatchRecord.batch_id).join(SourceRecord,SourceRecord.id==BatchRecord.record_id).where(BatchRecord.batch_id==Batch.id,SourceRecord.customer_id==customer_id).exists()
                quarantine=select(Quarantine.id).where(Quarantine.batch_id==Batch.id,Quarantine.validated_customer_id==customer_id).exists()
                query=query.where(or_(records,quarantine))
            return [{**serial(x),'download_url':file_reference(x)['download_url']} for x in s.scalars(query.order_by(Batch.created_at.desc(),Batch.id).limit(limit).offset(offset))]
    @app.get('/api/batches/{batch_id}')
    def batch_detail(batch_id:str,who=Depends(identity)):
        with db.transaction() as s:
            batch=s.get(Batch,batch_id)
            if not batch:raise NotFound('Source file not found')
            return {**serial(batch),'download_url':file_reference(batch)['download_url']}
    @app.get('/api/batches/{batch_id}/file')
    def batch_file(batch_id:str,who=Depends(identity)):
        with db.transaction() as s:
            batch=s.get(Batch,batch_id)
            if not batch:raise NotFound('Source file not found')
            try:path=ObjectStore(settings.data_dir/'objects').path(batch.object_key,verify=True)
            except FileNotFoundError:raise NotFound('Retained source file is missing')
            return FileResponse(path,media_type='application/octet-stream',filename=Path(batch.original_name).name,content_disposition_type='attachment')
    audit_keyset=Keyset('audit',Audit.id,Audit.id,True,int,lambda r:r.id,id_type=int)
    @app.get('/api/audit')
    def audits(limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),cursor:str|None=Query(None,max_length=1024),response:Response=None,who=Depends(identity)):
        no_offset_with_cursor(offset,cursor)
        with db.read_transaction() as s:
            events=list(s.scalars(audit_keyset.where(select(Audit),cursor or None).order_by(Audit.id.desc()).limit(limit+1).offset(offset)))
            next_cursor=audit_keyset.next(events,limit,lambda r:r.id);events=events[:limit]
            labels=actor_attributions(s,[event.actor for event in events])
            return page(shared_events(who,[{**serial(event),'actor_display_name':labels[event.actor]['display_name'],
                     'actor_username':labels[event.actor]['username'],
                     'actor_identity_type':labels[event.actor]['identity_type']} for event in events]),next_cursor,limit,cursor,response)
    @app.post('/api/batches/{batch_id}/supersede')
    def supersede(batch_id:str,body:Replacement,who=Depends(identity)):return supersede_batch(db,batch_id,body.replacement_batch_id,body.reason,body.request_id,who['actor'])

    @app.post('/api/ingest')
    def ingest(file:UploadFile=File(...),source:str=Form(...),period:str=Form(...),expected_rows:int=Form(...),complete:bool=Form(False),sheet:str|None=Form(None),who=Depends(identity)):
        suffix=Path(file.filename or '').suffix.lower()
        if suffix not in ('.csv','.parquet','.xlsx'):raise ValueError('Supported file types: CSV, Parquet, XLSX')
        with tempfile.NamedTemporaryFile(suffix=suffix,dir=settings.data_dir/'incoming',delete=False) as target:
            path=Path(target.name);total=0
            while chunk:=file.file.read(1024*1024):
                total+=len(chunk)
                if total>250*1024*1024:path.unlink(missing_ok=True);raise HTTPException(413,'Web uploads are capped at 250 MiB. Use the streaming ingestion CLI for larger files.')
                target.write(chunk)
        try:return ingest_file(db,path,source,period,expected_rows,complete,sheet,original_name=file.filename,actor=who['actor'])
        finally:path.unlink(missing_ok=True)

    @app.post('/api/worker/tick')
    def worker_tick(who=Depends(identity)):
        from .worker import tick
        if not settings.synthetic:raise Blocked('Manual worker tick is simulation only')
        with db.transaction() as s:audit(s,who['actor'],'manual_worker_tick_requested','worker',{'job_limit':10,'outbox_limit':20})
        result=tick(db,10,20,job_kinds=['ASSESS','SCORE','RENDER'])
        with db.transaction() as s:audit(s,who['actor'],'manual_worker_tick_completed','worker',{'result':result})
        return result
    @app.post('/api/simulation/provider')
    def provider(body:SimProvider,who=Depends(identity)):
        if settings.provider!='simulated':raise Blocked('Only the simulator has selectable outcomes')
        if body.outcome not in ('accepted','rejected','unknown'):raise ValueError('Invalid outcome')
        with db.transaction() as s:
            option=s.get(RuntimeOption,'provider_outcome')
            if option:option.value={'outcome':body.outcome}
            else:s.add(RuntimeOption(key='provider_outcome',value={'outcome':body.outcome}))
            audit(s,who['actor'],'simulation_provider_changed','provider',{'outcome':body.outcome})
        return {'outcome':body.outcome,'synthetic':True}
    @app.post('/api/simulation/events')
    def sim_event(body:SimEvent,who=Depends(identity)):return simulation_event(db,body.attempt_id,body.event,who['actor'])
    @app.post('/api/attempts/{attempt_id}/reconcile')
    def reconcile(attempt_id:str,who=Depends(identity)):return reconcile_attempt(db,attempt_id,who['actor'])

    def csv_response(headers,iterator,filename):
        def chunks():
            b=io.StringIO();w=csv.writer(b);w.writerow(headers);yield '\ufeff'+b.getvalue();b.seek(0);b.truncate(0)
            for values in iterator:
                safe=[("'"+v if isinstance(v,str) and v[:1] in ('=','+','-','@','\t','\r') else v) for v in values]
                w.writerow(safe);yield b.getvalue();b.seek(0);b.truncate(0)
        return StreamingResponse(chunks(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{filename}"'})

    @app.get('/api/exports/cases.csv')
    def export_cases(request:Request,customer_id:str|None=None,status:str|None='OPEN',q:str|None=Query(None,max_length=100),reason:str|None=Query(None,max_length=240),severity:str|None=Query(None,max_length=20),min_score:float|None=Query(None,ge=0,le=1),max_score:float|None=Query(None,ge=0,le=1),sort:str='newest',who=Depends(identity)):
        # Validate before starting the streaming response; a later generator
        # error cannot be converted into a normal HTTP validation response.
        filtered_cases(select(ReviewCase),search=q,min_score=min_score,max_score=max_score,sort=sort)
        headers=['case_id','customer_id','period','reason','status','expected_version','current_a_amount','current_b_amount','current_description','current_expected_amount','source_a_amount','source_b_amount','description','expected_amount','review_reason','case_url','source_files','source_file_ids','source_download_urls','provenance_status','last_review_actor','last_review_username','last_review_display_name','last_reviewed_at','last_review_outcome']
        def rows():
            with db.read_transaction() as s:
                query=select(ReviewCase,MovementVersion.version_no,MovementVersion.payload).outerjoin(MovementVersion,MovementVersion.id==ReviewCase.version_id).outerjoin(Assessment,(Assessment.version_id==ReviewCase.version_id)&(Assessment.kind=='MODEL')).outerjoin(Movement,Movement.id==ReviewCase.movement_id)
                query=filtered_cases(query,status=status,customer_id=customer_id,search=q,reason=reason,severity=severity,min_score=min_score,max_score=max_score,sort=sort).execution_options(yield_per=500)
                for page in s.execute(query).partitions(500):
                    lineage=case_sources(s,[c for c,_,_ in page])
                    labels=actor_attributions(s,[c.reviewed_by for c,_,_ in page])
                    for c,v,payload in page:
                        payload=payload or {};sides=payload.get('source_values',{});refs=lineage[c.id]
                        label=labels.get(c.reviewed_by,{})
                        yield [c.id,c.customer_id,c.period,c.reason,c.status,v,sides.get('A',{}).get('amount',''),sides.get('B',{}).get('amount',''),payload.get('description',''),payload.get('expected_amount',''),'','','','','',f'{request.base_url}#case/{c.id}',json.dumps([f['original_name'] for f in refs['source_files']]),json.dumps([f['id'] for f in refs['source_files']]),json.dumps([str(request.base_url).rstrip('/')+f['download_url'] for f in refs['source_files']]),refs['provenance_status'],c.reviewed_by or '',label.get('username') or '',label.get('display_name') or '',str(c.reviewed_at) if c.reviewed_at else '',c.outcome or '']
        return csv_response(headers,rows(),'review-worklist.csv')

    @app.post('/api/review/import')
    def import_worklist(file:UploadFile=File(...),who=Depends(identity)):
        from .review_import import import_review_csv
        if Path(file.filename or '').suffix.lower()!='.csv':raise ValueError('Upload the edited CSV review worklist')
        with tempfile.NamedTemporaryFile(suffix='.csv',dir=settings.data_dir/'incoming',delete=False) as f:
            path=Path(f.name);total=0
            while chunk:=file.file.read(65536):
                total+=len(chunk)
                if total>5*1024*1024:
                    path.unlink(missing_ok=True);raise HTTPException(413,'Review CSV limit is 5 MiB')
                f.write(chunk)
        try:return import_review_csv(db,path,who['actor'])
        finally:path.unlink(missing_ok=True)

    @app.get('/api/exports/review-history.csv')
    def export_review_history(customer_id:str|None=None,case_id:str|None=None,who=Depends(identity)):
        from .review_history import ReviewEvent
        headers=['event_id','case_id','customer_id','period','movement_id','reviewed_version_id',
                 'reviewed_version_no','actor_id','username','display_name','identity_kind',
                 'reviewed_at_utc','action','outcome','reason','origin','request_id',
                 'parent_request_id','initiator_case_id','source_file_sha256','source_row_number',
                 'before','after','correction','source_refs','source_row_metadata','evidence']
        def rows():
            with db.read_transaction() as s:
                query=select(ReviewEvent)
                if customer_id:query=query.where(ReviewEvent.customer_id==customer_id)
                if case_id:query=query.where(ReviewEvent.case_id==case_id)
                for e in s.scalars(query.order_by(ReviewEvent.id).execution_options(yield_per=500)):
                    yield [e.id,e.case_id,e.customer_id,e.period,e.movement_id,e.reviewed_version_id,
                           e.reviewed_version_no,e.actor_id,e.actor_username,e.actor_display_name,
                           e.actor_identity_kind,e.occurred_at.isoformat(),e.action,e.outcome,e.reason,
                           e.origin,e.request_id,e.parent_request_id,e.initiator_case_id,
                           e.source_file_sha256,e.source_row_number,canonical(e.before),
                           canonical(e.after),canonical(e.correction),canonical(e.source_refs),
                           canonical(e.source_row_metadata),canonical(e.evidence)]
        return csv_response(headers,rows(),'review-history.csv')

    @app.get('/api/review/files/{sha256}')
    def review_worklist_file(sha256:str,who=Depends(identity)):
        from .review_history import ReviewEvent
        if len(sha256)!=64 or any(c not in '0123456789abcdef' for c in sha256):
            raise ValueError('Invalid review-file SHA-256')
        with db.transaction() as s:
            if not s.scalar(select(ReviewEvent.id).where(ReviewEvent.source_file_sha256==sha256).limit(1)):
                raise NotFound('No review event references this worklist file')
        try:path=ObjectStore(settings.data_dir/'objects').path(sha256,verify=True)
        except FileNotFoundError:raise NotFound('The retained review worklist file is missing')
        return FileResponse(path,media_type='text/csv',filename=f'review-{sha256}.csv',content_disposition_type='attachment')

    @app.get('/api/exports/dispatch.csv')
    def export_dispatch(customer_id:str|None=None,who=Depends(identity)):
        headers=['invoice_id','invoice_number','customer_id','period','invoice_status','recipient','subject','body','pdf_url','fingerprint']
        def rows():
            with db.read_transaction() as s:
                query=select(Invoice)
                if customer_id:query=query.where(Invoice.customer_id==customer_id)
                for inv in s.scalars(query.order_by(Invoice.created_at).execution_options(yield_per=500)):
                    message=inv.package.get('message',{}) if inv.package else {}
                    yield [inv.id,inv.number,inv.customer_id,inv.period,inv.status,', '.join(message.get('to',[])),message.get('subject',''),message.get('body',''),f'/api/invoices/{inv.id}/pdf',inv.fingerprint]
        return csv_response(headers,rows(),'invoice-dispatch-register.csv')
    from .gmail_routes import install_gmail_routes
    install_gmail_routes(app,db,settings,identity,administrator)
    return app
