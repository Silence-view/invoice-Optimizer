"""Durable outbox with explicit unknown outcomes, and a local provider simulator."""
from datetime import timedelta
from email.message import EmailMessage
from email.policy import SMTP
import json, smtplib, ssl
from pathlib import Path
from sqlalchemy import select, update
from .models import Outbox, DeliveryAttempt, DeliveryEvent, Invoice, RuntimeOption, now, uid
from .db import audit, digest, canonical, Blocked, NotFound, Conflict
from .storage import ObjectStore


def _message(payload,attempt_id,store):
    m=payload.get('message',payload)
    msg=EmailMessage(policy=SMTP);msg['From']=m['from'];msg['To']=', '.join(m['to']);msg['Subject']=m['subject'];msg['Message-ID']=f'<{attempt_id}@invoice.example.test>'
    msg.set_content(m['body'])
    if payload.get('pdf_key'):
        content=store.path(payload['pdf_key'],verify=True).read_bytes()
        msg.add_attachment(content,maintype='application',subtype='pdf',filename=payload['invoice_number']+'.pdf')
    return msg


def recover_expired(db):
    with db.transaction() as s:
        rows=list(s.scalars(select(Outbox).where(Outbox.status=='IN_FLIGHT',Outbox.lease_until<now()).with_for_update(skip_locked=True)))
        for row in rows:
            row.status='UNKNOWN';row.lease_until=None
            s.execute(update(DeliveryAttempt).where(DeliveryAttempt.outbox_id==row.id,DeliveryAttempt.status=='SUBMITTING').values(status='UNKNOWN',error='Worker lease expired; external submission may have occurred'))
            audit(s,'worker','submission_unknown',row.id,{'reason':'lease expired'})
        return len(rows)


CONNECT_GMAIL_FIRST='Connect Gmail first: open Email setup and click Connect with Google. Approved invoices stay queued until then.'
_REAL_ROUTES=('smtp','gmail')


def _route(settings,kind):
    """Which transport an outbox kind uses; unlisted kinds stay simulated."""
    if settings.provider in _REAL_ROUTES and kind in settings.mail_send_kinds:return settings.provider
    return 'simulated'


def _gmail_status(settings):
    from .gmail import GmailConnection, GmailError
    try:return GmailConnection(settings).status()
    except GmailError as exc:return {'connected':False,'email':None,'reason':exc.code}


def redirect_target(settings,gmail_status=None):
    """The one address real mail may reach in test mode, or None (cannot send).

    Returns (target, reason). ``off`` is refused in synthetic mode here as
    well as in Settings.prepare, so a hand-built Settings cannot bypass it.
    """
    mode=settings.mail_redirect
    if mode=='off':
        if settings.synthetic:return None,'Synthetic mode never sends real email without a test redirect'
        return '',None
    if mode=='connected':
        status=gmail_status if gmail_status is not None else _gmail_status(settings)
        if not status.get('connected') or not status.get('email'):return None,CONNECT_GMAIL_FIRST
        return status['email'],None
    return mode,None


def mail_readiness(settings):
    """Cheap local check used by dispatch, status endpoints and the worker."""
    gmail=_gmail_status(settings) if settings.provider=='gmail' or settings.mail_redirect=='connected' and settings.provider=='smtp' else None
    result={'provider':settings.provider,'send_kinds':list(settings.mail_send_kinds) if settings.provider!='simulated' else [],
            'redirect_mode':'connected' if settings.mail_redirect=='connected' else ('off' if settings.mail_redirect=='off' else 'address'),
            'redirect_active':settings.provider!='simulated' and settings.mail_redirect!='off','ready':True,'reason':None,
            'gmail_connected':bool(gmail and gmail.get('connected'))}
    if settings.provider=='simulated':return result
    if settings.provider=='gmail' and not result['gmail_connected']:
        result.update(ready=False,reason=CONNECT_GMAIL_FIRST);return result
    target,reason=redirect_target(settings,gmail)
    if target is None:result.update(ready=False,reason=reason)
    return result


def _fingerprint(value):return digest(['redirect-target',value.casefold()])


def _apply_test_redirect(msg,target,from_address=None):
    """The single place a real-provider message is readdressed for testing.

    Every original recipient header is removed; only ``target`` remains.
    Returns the original recipients for the delivery audit.
    """
    original=[x for name in ('To','Cc','Bcc') for x in (msg.get_all(name) or [])]
    for name in ('To','Cc','Bcc','Reply-To','X-Original-To','X-Original-From'):
        del msg[name]
    shown=', '.join(original)
    msg['To']=target
    msg['X-Original-To']=shown or '(none)'
    subject=msg['Subject'] or '';del msg['Subject']
    label=shown if len(shown)<=120 else shown[:117]+'...'
    msg['Subject']=f'[TEST \u2192 {label}] {subject}'
    if from_address:
        msg['X-Original-From']=msg['From'] or '';del msg['From'];msg['From']=from_address
    return original


def send_one(db):
    settings=db.settings;store=ObjectStore(settings.data_dir/'objects')
    gmail_status=None;excluded=();token=None
    if settings.provider in _REAL_ROUTES:
        if settings.provider=='gmail' or settings.mail_redirect=='connected':gmail_status=_gmail_status(settings)
        # Not connected (or no safe redirect target): real-provider rows stay
        # READY with nothing submitted, while simulated alerts keep flowing.
        if (settings.provider=='gmail' and not gmail_status.get('connected')) or redirect_target(settings,gmail_status)[0] is None:
            excluded=tuple(settings.mail_send_kinds)
        elif settings.provider=='gmail':
            # Check the token before reserving a row: an unreachable token
            # endpoint must not reserve/release the same row in a tight loop.
            from .gmail import GmailConnection, GmailError
            try:token=GmailConnection(settings).get_access_token()
            except GmailError:excluded=tuple(settings.mail_send_kinds)
    with db.transaction() as s:
        from .operations import get_effective_automation
        if not get_effective_automation(s, settings)['email_delivery_enabled']:
            return False
        query=select(Outbox).where(Outbox.status=='READY')
        if excluded:query=query.where(Outbox.kind.notin_(excluded))
        row=s.scalar(query.order_by(Outbox.created_at).with_for_update(skip_locked=True).limit(1))
        if not row:return False
        if row.invoice_id:
            inv=s.get(Invoice,row.invoice_id)
            if inv.issued_at is None or inv.fingerprint!=digest(row.payload): raise Blocked('Outbox is not an issued approved package')
        route=_route(settings,row.kind)
        row.status='IN_FLIGHT';row.lease_until=now()+timedelta(minutes=5);row.attempts+=1
        attempt=DeliveryAttempt(id=uid(),outbox_id=row.id,status='SUBMITTING');s.add(attempt)
        payload=row.payload;attempt_id=attempt.id;outbox_id=row.id
        option=s.get(RuntimeOption,'provider_outcome');outcome=(option.value.get('outcome','accepted') if option else 'accepted')
        audit(s,'worker','send_reserved',outbox_id,{'attempt_id':attempt_id,'provider':route})
    status='UNKNOWN';provider_id=None;error=None;eml_key=None;evidence={'provider':route,'redirected':False}
    try:
        msg=_message(payload,attempt_id,store)
        if route=='simulated':
            eml_key=store.put_bytes(msg.as_bytes())
            # Local .eml files are a mailbox simulator, never an actual send.
            local=settings.data_dir/'mail'/f'{attempt_id}.eml'
            local.write_bytes(msg.as_bytes())
            provider_id='sim-'+attempt_id
            external_status='REJECTED' if outcome=='rejected' else 'ACCEPTED'
            journal={'attempt_id':attempt_id,'provider_id':provider_id,'status':external_status,'synthetic':True,'response_lost':outcome=='unknown','eml_key':eml_key}
            (settings.data_dir/'mail'/f'{attempt_id}.provider.json').write_text(canonical(journal))
            status='UNKNOWN' if outcome=='unknown' else external_status
            if status=='UNKNOWN':error='Simulated provider accepted message but its response was lost'
        else:
            target,reason=redirect_target(settings,gmail_status)
            if target is None:raise Blocked(reason)
            connected=(gmail_status or {}).get('email') if route=='gmail' else None
            owner=target if settings.mail_redirect=='connected' else None
            if target:
                original=_apply_test_redirect(msg,target,connected)
                evidence.update(redirected=True,original_recipients=original,redirect_target_sha256=_fingerprint(target))
            recipients=[x.strip() for x in msg['To'].split(',')]
            eml_key=store.put_bytes(msg.as_bytes())
            if route=='gmail':
                from .gmail import GmailConnection, GmailError, GmailSendError
                if not target:raise Blocked('Gmail delivery requires the test redirect in this release')
                try:token=token or GmailConnection(settings).get_access_token()
                except GmailError as exc:
                    # Nothing was submitted: release the row for a later cycle,
                    # and report no progress so the worker backs off.
                    _release(db,outbox_id,attempt_id,f'Gmail token unavailable: {exc}')
                    return False
                try:
                    provider_id='gmail:'+GmailConnection(settings).send_raw(msg.as_bytes(),token);status='ACCEPTED'
                except GmailSendError as exc:
                    status='REJECTED' if exc.definite else 'UNKNOWN';error=str(exc)
            else:
                # The SMTP allow-list is skipped only for the connected owner account.
                if any(x!=owner and x.rsplit('@',1)[-1].lower() not in settings.allowed_email_domains for x in recipients):
                    raise Blocked('Recipient domain is outside the configured SMTP allowlist')
                with smtplib.SMTP(settings.smtp_host,settings.smtp_port,timeout=30) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    if settings.smtp_user:smtp.login(settings.smtp_user,settings.smtp_password)
                    refused=smtp.send_message(msg)
                    status='REJECTED' if len(refused)==len(recipients) else ('PARTIAL' if refused else 'ACCEPTED')
                    error=str(refused) if refused else None
    except Blocked as exc:status='REJECTED';error=str(exc)
    except smtplib.SMTPRecipientsRefused as exc:status='REJECTED';error=str(exc)
    except Exception as exc:error=str(exc)[:1000]  # Conservatively uncertain after any external-call ambiguity.
    with db.transaction() as s:
        row=s.get(Outbox,outbox_id,with_for_update=True);attempt=s.get(DeliveryAttempt,attempt_id)
        attempt.status=status;attempt.provider_id=provider_id;attempt.error=error;attempt.eml_key=eml_key
        row.status=status;row.lease_until=None
        audit(s,'worker','send_'+status.lower(),row.id,{'attempt_id':attempt_id,'provider':route,'error':error,
              'provider_id':provider_id,**evidence})
    return True


def _release(db,outbox_id,attempt_id,reason):
    with db.transaction() as s:
        row=s.get(Outbox,outbox_id,with_for_update=True);attempt=s.get(DeliveryAttempt,attempt_id)
        attempt.status='NOT_SUBMITTED';attempt.error=reason[:1000]
        row.status='READY';row.lease_until=None
        audit(s,'worker','send_not_submitted',outbox_id,{'attempt_id':attempt_id,'reason':reason[:500]})


def simulation_event(db,attempt_id,event,actor,event_id=None):
    if not db.settings.synthetic:raise Blocked('Simulation disabled')
    if event not in ('delivered','bounced','opened'):raise ValueError('Unknown simulated event')
    eid=event_id or digest(['simulation',attempt_id,event])
    with db.transaction() as s:
        attempt=s.get(DeliveryAttempt,attempt_id)
        if not attempt:raise NotFound('Attempt not found')
        if attempt.status!='ACCEPTED':raise Blocked('Only accepted submissions can receive simulated provider events')
        existing=s.get(DeliveryEvent,eid)
        if existing:
            if existing.attempt_id!=attempt_id or existing.event!=event:raise Conflict('Event ID conflict')
            return {'id':eid,'replayed':True}
        s.add(DeliveryEvent(id=eid,attempt_id=attempt_id,event=event,evidence={'synthetic':True,'source':'authenticated-simulator','actor':actor}))
        audit(s,actor,'simulated_'+event,attempt_id)
        return {'id':eid,'event':event,'synthetic':True}


def reconcile_attempt(db,attempt_id,actor):
    path=db.settings.data_dir/'mail'/f'{attempt_id}.provider.json'
    # Simulator journals exist only for simulated routes, including alerts kept
    # on the simulator while a real provider is configured.
    if db.settings.provider=='gmail' and not path.is_file():
        raise Blocked('The Gmail send-only permission cannot look messages up. Check the connected mailbox Sent folder before any authorized replay.')
    if db.settings.provider=='smtp' and not path.is_file():raise Blocked('SMTP has no provider lookup. Reconcile against provider evidence before any authorized replay.')
    if not path.is_file():raise Blocked('No provider evidence found; status remains unknown')
    evidence=json.loads(path.read_text())
    with db.transaction() as s:
        attempt=s.get(DeliveryAttempt,attempt_id,with_for_update=True)
        if not attempt:raise NotFound('Attempt not found')
        if attempt.status!='UNKNOWN':raise Conflict('Attempt is not unknown')
        if evidence.get('attempt_id')!=attempt_id or evidence.get('status') not in ('ACCEPTED','REJECTED'):raise Blocked('Provider evidence is inconsistent')
        attempt.status=evidence['status'];attempt.provider_id=evidence['provider_id'];attempt.error=None
        row=s.get(Outbox,attempt.outbox_id);row.status=attempt.status
        audit(s,actor,'submission_reconciled',attempt_id,{'evidence':evidence})
        return {'attempt_id':attempt_id,'status':attempt.status,'resubmitted':False}
