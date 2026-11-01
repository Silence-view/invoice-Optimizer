"""Reconciliation and model evidence; deterministic failures never depend on ML."""
from collections import defaultdict
from decimal import Decimal
from copy import deepcopy
import math
from sqlalchemy import select, insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from .models import (Movement, MovementVersion, SourceRecord, Assessment, ReviewCase, Outbox,
                     Customer, Job, Invoice, Allocation, now, uid)
from .db import digest, lock, audit, Conflict, Blocked, NotFound, replay, record_command, change_state
from .ingestion import money, persist_cases
from .review_history import (actor_identity, case_state, compact_review_audit, event_response, record_review_event,
                             record_rejected_attempt, review_context as normalize_review_context, validate_actor)


def rule_result(payload):
    values=payload.get('source_values',{}); reasons=[]
    for side in ('A','B'):
        if side not in values: reasons.append('MISSING_SOURCE_'+side)
    if payload.get('ambiguous'): reasons.append('AMBIGUOUS_SOURCE_KEYS')
    if 'A' in values and 'B' in values:
        a,b=values['A'],values['B']
        if Decimal(a['amount'])!=Decimal(b['amount']): reasons.append('AMOUNT_MISMATCH')
        for field in ('currency','supplier_id','period','event_at'):
            if a.get(field)!=b.get(field): reasons.append(field.upper()+'_MISMATCH')
    return {'status':'FAIL' if reasons else 'PASS','reasons':reasons}


def invalidate(s, customer_id, period):
    for inv in s.scalars(select(Invoice).where(Invoice.customer_id==customer_id,Invoice.period==period,Invoice.issued_at.is_(None),Invoice.status.in_(['BUILDING','DRAFT','APPROVED']))):
        inv.status='STALE'; inv.approved_at=None; inv.approved_by=None


def flag_case(s, movement, version, reason, owner, kind='RULE'):
    case_id=digest([version.id,reason]); dedup='assessment:'+case_id
    if s.get(ReviewCase,case_id): return
    case=ReviewCase(id=case_id,dedup_key=dedup,movement_id=movement.id,version_id=version.id,
        customer_id=movement.customer_id,period=movement.period,reason=reason,owner=owner,severity='MEDIUM' if kind=='MODEL' else 'HIGH')
    s.add(case); s.flush()
    s.add(Outbox(dedup_key='flag:'+case_id,kind='ALERT',case_id=case_id,payload={'from':'operations@example.test','to':[owner],
        'subject':'Movement requires review: '+reason,'body':f'Customer {movement.customer_id}; movement {movement.external_id}; case {case_id}. Open the operations console to inspect the evidence.'}))


def _apply_rule_evidence(s,movement,version,owner):
    rules=rule_result(version.payload)
    s.add(Assessment(version_id=version.id,kind='RULE',status=rules['status'],reasons=rules['reasons'],flagged=bool(rules['reasons']),feature_cutoff=version.created_at or now()))
    for reason in rules['reasons']: flag_case(s,movement,version,reason,owner)
    change_state(s,movement,'BLOCKED' if rules['reasons'] else 'PENDING_MODEL')
    return rules


def _persist_flag_batch(s, flags):
    """Write bounded cases and their alert outbox rows in dependency order.

    Customer-period locks are already held by the assessment transaction.
    Returning inserted IDs keeps retries from creating another alert for an
    existing case. This is the same durable intent as flag_case, in batches.
    """
    cases={};messages={}
    for movement,version_id,reason,owner,kind in flags:
        case_id=digest([version_id,reason])
        cases[case_id]={'id':case_id,'dedup_key':'assessment:'+case_id,
            'movement_id':movement.id,'version_id':version_id,'customer_id':movement.customer_id,
            'period':movement.period,'reason':reason,'owner':owner,
            'severity':'MEDIUM' if kind=='MODEL' else 'HIGH'}
        messages[case_id]={'dedup_key':'flag:'+case_id,'kind':'ALERT','case_id':case_id,
            'payload':{'from':'operations@example.test','to':[owner],
                'subject':'Movement requires review: '+reason,
                'body':f'Customer {movement.customer_id}; movement {movement.external_id}; case {case_id}. Open the operations console to inspect the evidence.'}}
    # Chunk again because one movement can produce multiple rule cases.
    persist_cases(s,cases,messages)


def reconcile_job(db, job_id, claim_token=None):
    """First transaction commits rule outcomes/cases even if model inference fails."""
    with db.transaction() as s:
        job=s.get(Job,job_id,with_for_update=True)
        if not job or job.status=='DONE' or (claim_token is not None and job.claim_token!=claim_token): return
        ids=sorted(set(job.payload['movement_ids']))
        movements=list(s.scalars(select(Movement).where(Movement.id.in_(ids)).order_by(Movement.id)))
        for cid in sorted({m.customer_id for m in movements}): lock(s,'customer-period:'+cid+':'+job.period)
        # Reload once after acquiring scope locks. Per-row refresh previously
        # performed a round trip for every movement in both processing stages.
        movements=list(s.scalars(select(Movement).where(Movement.id.in_(ids)).order_by(Movement.id)
            .execution_options(populate_existing=True)))
        existing_versions={v.movement_id:v for v in s.scalars(select(MovementVersion).join(
            Movement,(Movement.id==MovementVersion.movement_id)&(Movement.current_version==MovementVersion.version_no))
            .where(Movement.id.in_(ids)))}
        sources=defaultdict(dict)
        for r in s.scalars(select(SourceRecord).where(SourceRecord.movement_id.in_(ids))
                .distinct(SourceRecord.movement_id,SourceRecord.source,SourceRecord.record_id)
                .order_by(SourceRecord.movement_id,SourceRecord.source,SourceRecord.record_id,SourceRecord.revision.desc())):
            sources[r.movement_id][(r.source,r.record_id)]=r
        customers={c.id:c for c in s.scalars(select(Customer).where(Customer.id.in_({m.customer_id for m in movements})))}
        allocated=set(s.scalars(select(Allocation.movement_id).where(Allocation.movement_id.in_(ids))))
        new_version_ids=[];version_rows=[];rule_rows=[];flags=[];changed_movements=[];changed_customers=set()
        for m in movements:
            refs=list(sources[m.id].values()); sides=defaultdict(list)
            for r in refs: sides[r.source].append(r)
            view={side:rows[0].payload for side,rows in sides.items() if len(rows)==1}
            authoritative=view.get('A') or view.get('B')
            if not authoritative:
                # Ambiguous-only observations are retained; select a value only to hold an unbillable version.
                authoritative=refs[0].payload
            payload=deepcopy(authoritative); payload['source_values']=deepcopy(view); payload['ambiguous']=any(len(rs)!=1 for rs in sides.values())
            source_refs=sorted(r.id for r in refs); h=digest([payload,source_refs])
            old=existing_versions.get(m.id)
            # A human correction remains effective until its source evidence changes.
            if old and old.source_refs==source_refs: continue
            if m.id in allocated:
                if old:flags.append((m,old.id,'SOURCE_CHANGE_AFTER_ISSUE',customers[m.customer_id].alert_email,'RULE'))
                change_state(s,m,'BLOCKED_AFTER_ISSUE'); continue
            m.current_version+=1
            version_id=uid();created_at=now()
            version_rows.append(dict(id=version_id,movement_id=m.id,version_no=m.current_version,content_hash=h,
                payload=payload,source_refs=source_refs,amount=money(payload['amount']),currency=payload['currency'],origin='SOURCE',created_at=created_at))
            rules=rule_result(payload)
            rule_rows.append(dict(version_id=version_id,kind='RULE',status=rules['status'],reasons=rules['reasons'],
                flagged=bool(rules['reasons']),feature_cutoff=created_at))
            flags.extend((m,version_id,reason,customers[m.customer_id].alert_email,'RULE') for reason in rules['reasons'])
            change_state(s,m,'BLOCKED' if rules['reasons'] else 'PENDING_MODEL')
            new_version_ids.append(version_id);changed_movements.append(m.id);changed_customers.add(m.customer_id)
        if version_rows:
            s.execute(insert(MovementVersion),version_rows)
            s.execute(update(ReviewCase).where(ReviewCase.movement_id.in_(changed_movements),ReviewCase.status=='OPEN').values(status='SUPERSEDED'))
            s.execute(insert(Assessment),rule_rows)
            s.execute(update(Invoice).where(Invoice.customer_id.in_(changed_customers),Invoice.period==job.period,
                Invoice.issued_at.is_(None),Invoice.status.in_(['BUILDING','DRAFT','APPROVED']))
                .values(status='STALE',approved_at=None,approved_by=None))
        _persist_flag_batch(s,flags)
        if new_version_ids:
            s.add(Job(dedup_key='score:'+job.id,kind='SCORE',period=job.period,payload={'version_ids':new_version_ids}))
        job.status='DONE'; job.lease_until=None
        audit(s,'worker','rules_evaluated',job.id,{'versions':len(new_version_ids)})


def score_job(db,job_id,claim_token=None):
    from .ml import score_batch, compact_explanation
    with db.transaction() as s:
        job=s.get(Job,job_id)
        if not job or job.status=='DONE' or (claim_token is not None and job.claim_token!=claim_token): return
        versions=list(s.scalars(select(MovementVersion).where(MovementVersion.id.in_(job.payload['version_ids']))))
        payloads=[v.payload for v in versions]
    try: results=score_batch(payloads,db.settings.model_dir)
    except Exception as exc:
        results=[{'status':'UNAVAILABLE','score':None,'flagged':False,'reason':str(exc)[:500]} for _ in versions]
    if len(results)!=len(versions): raise ValueError('Model result count mismatch')
    with db.transaction() as s:
        job=s.get(Job,job_id,with_for_update=True)
        if job.status=='DONE' or (claim_token is not None and job.claim_token!=claim_token): return
        movements={m.id:m for m in s.scalars(select(Movement).where(Movement.id.in_([v.movement_id for v in versions])))}
        for cid in sorted({m.customer_id for m in movements.values()}): lock(s,'customer-period:'+cid+':'+job.period)
        movements={m.id:m for m in s.scalars(select(Movement).where(Movement.id.in_(movements))
            .execution_options(populate_existing=True))}
        customers={c.id:c for c in s.scalars(select(Customer).where(Customer.id.in_({m.customer_id for m in movements.values()})))}
        existing=set(s.scalars(select(Assessment.version_id).where(Assessment.kind=='MODEL',Assessment.version_id.in_([v.id for v in versions]))))
        unavailable_count=0;assessment_rows=[];flags=[]
        for version,result in zip(versions,results):
            if version.id in existing: continue
            status=result.get('status','UNAVAILABLE'); score=result.get('score') if status=='SCORED' else None
            complete=(type(score) in (int,float) and math.isfinite(score) and 0<=score<=1 and all(result.get(k) for k in ('model_version','policy_version','feature_schema_version')) and isinstance(result.get('feature_values'),dict))
            if status!='SCORED' or not complete:
                status='UNAVAILABLE';score=None;result={**result,'feature_values':None,'flagged':False}
            if status!='SCORED':unavailable_count+=1
            flagged=status=='SCORED' and result.get('flagged',False)
            reason='MODEL_RISK' if flagged else ('MODEL_UNAVAILABLE' if status!='SCORED' else None)
            assessment_rows.append(dict(version_id=version.id,kind='MODEL',status=status,score=score,flagged=flagged,
                model_version=result.get('model_version'),policy_version=result.get('policy_version'),feature_schema_version=result.get('feature_schema_version'),features=result.get('feature_values'),explanation=compact_explanation(result.get('explanation')),
                reasons=([reason]+([str(result['reason'])[:500]] if result.get('reason') else [])) if reason else [],feature_cutoff=version.created_at))
            m=movements[version.movement_id]
            if m.current_version!=version.version_no: continue
            if reason:flags.append((m,version.id,reason,customers[m.customer_id].alert_email,'MODEL'))
            if m.state=='PENDING_MODEL': change_state(s,m,'REVIEW' if reason else 'ELIGIBLE')
        if assessment_rows:s.execute(insert(Assessment).execution_options(render_nulls=True),assessment_rows)
        _persist_flag_batch(s,flags)
        job.status='DONE'; job.lease_until=None
        audit(s,'worker','model_evaluated',job.id,{'versions':len(versions),'model_unavailable':unavailable_count})


def _correct_case_in_session(s,case_id,expected_version,patch,reason,request_id,actor,*,review_context=None):
    validate_actor(actor)
    context=normalize_review_context(review_context)
    if type(expected_version) is not int or expected_version<1: raise ValueError('A positive expected version is required')
    if not isinstance(reason,str) or not reason.strip(): raise ValueError('Correction reason is required')
    allowed={'source_a_amount','source_b_amount','description','expected_amount'}
    if not patch or set(patch)-allowed: raise ValueError('Allowed changes: source_a_amount, source_b_amount, description, expected_amount')
    args={'case_id':case_id,'expected_version':expected_version,'patch':patch,'reason':reason,'actor':actor,'review_context':context}
    cached=replay(s,request_id,'correct',args)
    if cached: return cached
    actor_identity(s,actor)
    case=s.get(ReviewCase,case_id)
    if not case: raise NotFound('Case not found')
    if not case.movement_id: raise Blocked('Quarantined source rows require a corrected source delivery. Inspect original row and reason before replacement.')
    lock(s,'customer-period:'+case.customer_id+':'+case.period)
    s.refresh(case,with_for_update=True)
    m=s.get(Movement,case.movement_id,with_for_update=True)
    if m.current_version!=expected_version: raise Conflict('Movement changed. Refresh and review the latest version.')
    case_version=s.get(MovementVersion,case.version_id)
    if not case_version or case_version.movement_id!=m.id or case_version.version_no!=m.current_version: raise Conflict('Case is for an old version')
    if case.status!='OPEN': raise Conflict('Case is not open')
    if s.get(Allocation,m.id): raise Blocked('Issued movement cannot be changed. An authorized credit/debit adjustment is required.')
    old=s.scalar(select(MovementVersion).where(MovementVersion.movement_id==m.id,MovementVersion.version_no==m.current_version))
    affected=list(s.scalars(select(ReviewCase).where(ReviewCase.movement_id==m.id,ReviewCase.status=='OPEN').order_by(ReviewCase.id).with_for_update()))
    before_states={item.id:case_state(item,m) for item in affected}
    payload=deepcopy(old.payload)
    for key,value in patch.items():
        if key in ('source_a_amount','source_b_amount'):
            side='A' if key=='source_a_amount' else 'B'
            if side not in payload.get('source_values',{}) or payload.get('ambiguous'): raise Blocked('Cannot invent a missing or ambiguous source counterpart')
            payload['source_values'][side]['amount']=str(money(value))
        elif key=='expected_amount':
            if money(value)<=0: raise ValueError('Expected amount must be positive')
            payload[key]=str(money(value))
        else:
            if len(str(value))>1000: raise ValueError('Description too long')
            payload[key]=str(value)
    payload['amount']=payload.get('source_values',{}).get('A',payload).get('amount',payload['amount'])
    if payload==old.payload: raise ValueError('Correction does not change any value')
    def patch_value(data,key):
        if key in ('source_a_amount','source_b_amount'):
            return data.get('source_values',{}).get('A' if key=='source_a_amount' else 'B',{}).get('amount')
        return data.get(key)
    changes={key:{'before':deepcopy(patch_value(old.payload,key)),'after':deepcopy(patch_value(payload,key))} for key in patch}
    m.current_version+=1
    v=MovementVersion(id=uid(),movement_id=m.id,version_no=m.current_version,payload=payload,source_refs=old.source_refs,
        content_hash=digest([payload,old.source_refs]),amount=money(payload['amount']),currency=payload['currency'],origin='HUMAN_CORRECTION',created_at=now())
    s.add(v); s.flush()
    for affected_case in affected:
        affected_case.status='SUPERSEDED';affected_case.review_reason=reason
    owner=s.get(Customer,m.customer_id).alert_email
    _apply_rule_evidence(s,m,v,owner)
    s.add(Job(dedup_key='score:'+v.id,kind='SCORE',period=m.period,payload={'version_ids':[v.id]}))
    invalidate(s,m.customer_id,m.period)
    correction={'submitted_patch':deepcopy(patch),'changes':changes,
        'old_version':{'id':old.id,'version_no':old.version_no,'content_hash':old.content_hash},
        'new_version':{'id':v.id,'version_no':v.version_no,'content_hash':v.content_hash}}
    timestamp=now();events=[];initiating_event=None
    for affected_case in affected:
        reviewed=s.get(MovementVersion,affected_case.version_id)
        initiating=affected_case.id==case_id
        event=record_review_event(s,case=affected_case,movement=m,version=reviewed,actor=actor,
            action='CORRECTION_APPLIED' if initiating else 'SUPERSEDED_BY_CORRECTION',
            outcome='CORRECTED' if initiating else 'SUPERSEDED',reason=reason,request_id=request_id,
            before=before_states[affected_case.id],after=case_state(affected_case,m),context=context,
            initiator_case_id=case_id,correction=correction,occurred_at=timestamp)
        events.append({'case_id':affected_case.id,**event_response(event)})
        if initiating:initiating_event=event
        audit(s,actor,'case_correction_applied' if initiating else 'case_superseded_by_correction',affected_case.id,
              compact_review_audit(event))
    result={'movement_id':m.id,'version_no':m.current_version,'status':m.state,
            **event_response(initiating_event),'events':events}
    audit(s,actor,'correction_applied',m.id,{'previous_version':expected_version,'new_version':m.current_version,
          'patch':patch,'reason':reason,'review_event_ids':[e['event_id'] for e in events],
          'request_id':request_id,'review_context':context,'reviewer':result['reviewer']})
    return record_command(s,request_id,'correct',args,result)



def correct_case(db,case_id,expected_version,patch,reason,request_id,actor,*,review_context=None):
    try:
        with db.transaction() as s:
            return _correct_case_in_session(s,case_id,expected_version,patch,reason,request_id,actor,review_context=review_context)
    except ValueError as exc:
        record_rejected_attempt(db,case_id=case_id,actor=actor,request_id=request_id,expected_version=expected_version,
            reason=reason,error=exc,action='case_correction_rejected',intended_patch=patch)
        raise


def _resolve_case_in_session(s,case_id,expected_version,outcome,reason,request_id,actor,*,review_context=None):
    """Shared single-item domain operation; caller owns its transaction."""
    if outcome not in ('VERIFIED_VALID','CONFIRMED_ISSUE','UNRESOLVED') or not isinstance(reason,str) or not reason.strip(): raise ValueError('Valid outcome and review reason required')
    if type(expected_version) is not int or expected_version<1: raise ValueError('A positive expected version is required')
    validate_actor(actor)
    context=normalize_review_context(review_context)
    args={'case_id':case_id,'expected_version':expected_version,'outcome':outcome,'reason':reason,'actor':actor,'review_context':context}
    cached=replay(s,request_id,'resolve',args)
    if cached: return cached
    actor_identity(s,actor)
    case=s.get(ReviewCase,case_id)
    if not case: raise NotFound('Case not found')
    if not case.movement_id: raise Blocked('Quarantined input needs corrected source evidence; review cannot make it billable')
    lock(s,'customer-period:'+case.customer_id+':'+case.period)
    # A waiting reviewer may already have OPEN in the identity map. Reload only
    # after the shared period lock, so another review cannot be overwritten.
    s.refresh(case,with_for_update=True)
    m=s.get(Movement,case.movement_id,with_for_update=True)
    v=s.get(MovementVersion,case.version_id)
    if not v or v.movement_id!=m.id or expected_version!=m.current_version or v.version_no!=m.current_version: raise Conflict('Case is for an old version')
    if case.status!='OPEN': raise Conflict('Case is not open')
    if outcome=='VERIFIED_VALID':
        if case.reason!='MODEL_RISK': raise Blocked('Human review cannot clear a deterministic failure or a missing model score')
        if s.get(Allocation,m.id): raise Blocked('Issued movement cannot be released by review')
        rule=s.scalar(select(Assessment).where(Assessment.version_id==v.id,Assessment.kind=='RULE'))
        model=s.scalar(select(Assessment).where(Assessment.version_id==v.id,Assessment.kind=='MODEL'))
        if not rule or rule.status!='PASS': raise Blocked('A deterministic rule failure or missing rule evidence cannot be released by review')
        complete_model=(model and model.status=='SCORED' and type(model.score) in (int,float)
                        and math.isfinite(model.score) and 0<=model.score<=1 and model.flagged
                        and all((model.model_version,model.policy_version,model.feature_schema_version))
                        and isinstance(model.features,dict))
        if not complete_model: raise Blocked('A missing or incomplete model score cannot be released by review')
        if m.state!='REVIEW': raise Blocked('Movement is not in the review state')
        remaining=s.scalar(select(ReviewCase.id).where(ReviewCase.version_id==v.id,ReviewCase.status=='OPEN',ReviewCase.id!=case.id).limit(1))
        if remaining: raise Blocked('Other open cases on this version require individual review')
    before=case_state(case,m)
    case.outcome=outcome; case.review_reason=reason
    if outcome=='VERIFIED_VALID':
        case.status='RESOLVED'
        change_state(s,m,'ELIGIBLE')
    elif outcome=='CONFIRMED_ISSUE': change_state(s,m,'BLOCKED')
    invalidate(s,m.customer_id,m.period)
    event=record_review_event(s,case=case,movement=m,version=v,actor=actor,action='REVIEW_OUTCOME',outcome=outcome,
        reason=reason,request_id=request_id,before=before,after=case_state(case,m),context=context)
    audit(s,actor,'case_reviewed',case_id,compact_review_audit(event))
    return record_command(s,request_id,'resolve',args,{'id':case_id,'status':case.status,'outcome':outcome,
        'movement_state':m.state,**event_response(event)})


def resolve_case(db,case_id,expected_version,outcome,reason,request_id,actor,*,review_context=None):
    try:
        with db.transaction() as s:
            return _resolve_case_in_session(s,case_id,expected_version,outcome,reason,request_id,actor,review_context=review_context)
    except ValueError as exc:
        record_rejected_attempt(db,case_id=case_id,actor=actor,request_id=request_id,expected_version=expected_version,
            reason=reason,error=exc,action='case_review_rejected',intended_outcome=outcome)
        raise
