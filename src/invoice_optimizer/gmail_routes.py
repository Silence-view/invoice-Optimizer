"""Authenticated Gmail setup and a redirected test send.

Connecting an account never changes INVOICE_PROVIDER; the operator selects
``gmail`` explicitly. In synthetic mode every real message is redirected to
the connected account (see delivery._apply_test_redirect).
"""
from pathlib import Path
import secrets
from fastapi import Request, Depends, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import select
from .db import audit
from .models import Outbox
from .delivery import mail_readiness
from .gmail import GmailConnection, GmailError
from .operators import Operator
from .auth_secrets import session_signing_secret

CALLBACK='/api/mail/gmail/callback'
BINDING_COOKIE='invoice_gmail_oauth'
FLASH_COOKIE='invoice_gmail_result'


class TestSendRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id:str=Field(min_length=8,max_length=100,pattern=r'^[A-Za-z0-9._:-]+$')


class ConnectRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_email:str|None=Field(default=None,max_length=320)


def _redirect_summary(settings):
    readiness=mail_readiness(settings)
    return {'active':readiness['redirect_active'],'mode':readiness['redirect_mode'],
            'send_kinds':readiness['send_kinds'],'ready':readiness['ready'],'reason':readiness['reason'],
            'rule':'Every real message goes only to the connected Gmail account; the original recipient is kept in X-Original-To and the subject prefix.'
                   if readiness['redirect_active'] else 'Simulator only: no message leaves this computer.'}


def install_gmail_routes(app,db,settings,identity,administrator):
    gmail=GmailConnection(settings)
    signing_secret=session_signing_secret(settings)
    flash=URLSafeTimedSerializer(signing_secret,salt='gmail-connection-result-v2')
    binding_signer=URLSafeTimedSerializer(signing_secret,salt='gmail-operator-binding-v1')
    secure=settings.cookie_secure

    @app.exception_handler(GmailError)
    async def gmail_error(request,exc):
        return JSONResponse({'detail':str(exc),'code':exc.code},status_code=422)

    @app.get('/api/mail/gmail')
    def status(request:Request,who=Depends(identity)):
        value={**gmail.status(),'expected_email':settings.gmail_expected_email or None,
               'automatic_delivery':settings.provider,'workspace_url':settings.public_base_url,
               'connection_activates_delivery':False,'last_error':None,
               'delivery_provider':settings.provider,'test_redirect':_redirect_summary(settings)}
        if who.get('identity_type')=='legacy':
            # The shared read-only token is not a person: no account addresses or deployment origin.
            value.update(email=None,expected_email=None,workspace_url=None,callback_url=None)
        result_cookie=request.cookies.get(FLASH_COOKIE)
        if result_cookie:
            try:
                outcome=flash.loads(result_cookie,max_age=120)
                value['last_error']=None if outcome['ok'] else outcome['message']
                value['connection_result']=outcome
            except (BadSignature,SignatureExpired,KeyError):pass
        response=JSONResponse(value)
        if result_cookie:response.delete_cookie(FLASH_COOKIE,path='/')
        return response

    @app.post('/api/mail/gmail/config')
    def configure(file:UploadFile=File(...),who=Depends(administrator)):
        if Path(file.filename or '').suffix.lower()!='.json':raise ValueError('Upload the Google OAuth web-client JSON file')
        data=file.file.read(32769)
        if len(data)>32768:raise HTTPException(413,'OAuth client file limit is 32 KiB')
        result=gmail.configure(data)
        with db.transaction() as s:audit(s,who['actor'],'gmail_client_configured','gmail',{'callback_url':result['callback_url']})
        return result

    @app.post('/api/mail/gmail/connect')
    def connect(body:ConnectRequest,who=Depends(administrator)):
        expected=(body.expected_email or settings.gmail_expected_email or '').strip().lower() or None
        if settings.gmail_expected_email and expected!=settings.gmail_expected_email:
            raise ValueError('Use the configured expected Gmail account')
        binding=secrets.token_urlsafe(32)
        with db.transaction() as s:
            operator=s.get(Operator,who['user_id'])
            if not operator or not operator.active:raise HTTPException(401,'Sign in with an active personal account')
            binding_context={'binding':binding,'operator_id':operator.id,
                'credential_version':operator.credential_version,'flow_id':secrets.token_hex(16)}
        result=gmail.begin(binding,expected_email=expected)
        response=JSONResponse({'authorization_url':result['authorization_url']})
        response.set_cookie(BINDING_COOKIE,binding_signer.dumps(binding_context),httponly=True,secure=secure,samesite='lax',path=CALLBACK,max_age=600)
        with db.transaction() as s:audit(s,who['actor'],'gmail_authorization_started','gmail',{'expected_email':expected,'flow_id':binding_context['flow_id']})
        return response

    @app.get(CALLBACK)
    def callback(request:Request):
        # Google returns cross-site: the main Strict cookie is deliberately not
        # required. This separate cookie was issued by an authenticated CSRF-
        # protected command and is bound to one server-side OAuth state/PKCE flow.
        response=RedirectResponse(settings.public_base_url+'/#email',status_code=303)
        response.headers['Referrer-Policy']='no-referrer'
        response.delete_cookie(BINDING_COOKIE,path=CALLBACK)
        outcome={'ok':False,'message':'Google connection was not completed. Start Connect with Google again in the same browser.'}
        binding_cookie=request.cookies.get(BINDING_COOKIE,'')
        try:
            if not binding_cookie:raise ValueError('Missing OAuth browser binding')
            binding_context=binding_signer.loads(binding_cookie,max_age=600)
            with db.transaction() as s:
                operator=s.get(Operator,binding_context['operator_id'])
                if not operator or not operator.active or operator.credential_version!=binding_context['credential_version']:
                    raise ValueError('The initiating personal account is no longer active')
                actor='user:'+operator.id
            code=request.query_params.get('code','');state=request.query_params.get('state','')
            if not code or not state or len(code)>4096 or len(state)>256:raise ValueError('Incomplete OAuth response')
            result=gmail.finish(code,state,binding_context['binding'])
            with db.transaction() as s:audit(s,actor,'gmail_connected',result['email'],{'scopes':result['scopes'],'delivery_activated':False,'flow_id':binding_context['flow_id']})
            outcome={'ok':True,'message':'Google account connected. Automatic delivery is unchanged.'}
        except GmailError as exc:
            outcome['message']=str(exc)
            outcome['code']=exc.code
        except Exception:
            # Never persist or echo provider query strings, codes or responses.
            pass
        response.set_cookie(FLASH_COOKIE,flash.dumps(outcome),httponly=True,secure=secure,samesite='lax',path='/',max_age=120)
        return response

    @app.post('/api/mail/gmail/test')
    def test_send(body:TestSendRequest,who=Depends(administrator)):
        """Queue one test message through the normal outbox and redirect path."""
        key='test:'+body.request_id
        payload={'from':'operations@example.test','to':[settings.operator_email],
                 'subject':'Invoice Optimizer test message',
                 'body':'This is a test message from Invoice Optimizer. If you can read it, automatic delivery works.\n'
                        'Customer invoices are still sent only after a person approves them.\n'}
        with db.transaction() as s:
            inserted=s.scalar(pg_insert(Outbox).values(dedup_key=key,kind='TEST',payload=payload)
                              .on_conflict_do_nothing(index_elements=['dedup_key']).returning(Outbox.id))
            row=s.scalar(select(Outbox).where(Outbox.dedup_key==key))
            if row.kind!='TEST':raise HTTPException(409,'Request ID was already used for another message')
            if inserted:audit(s,who['actor'],'mail_test_queued',row.id,{'provider':settings.provider,'redirected':settings.provider!='simulated'})
            result={'outbox_id':row.id,'status':row.status,'replayed':not inserted}
        readiness=mail_readiness(settings)
        result.update(delivery_provider=settings.provider,ready=readiness['ready'],reason=readiness['reason'])
        return result

    @app.post('/api/mail/gmail/disconnect')
    def disconnect(who=Depends(administrator)):
        result=gmail.disconnect()
        with db.transaction() as s:audit(s,who['actor'],'gmail_disconnected_locally','gmail',{'delivery_activated':False})
        response=JSONResponse(result)
        response.delete_cookie(BINDING_COOKIE,path=CALLBACK)
        return response
