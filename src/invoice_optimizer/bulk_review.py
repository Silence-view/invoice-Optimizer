"""Bounded, resumable review release; each case has its own commit boundary."""
from collections import defaultdict
from copy import deepcopy

from .assessment import _resolve_case_in_session
from .db import Blocked, Conflict, NotFound, audit, digest, record_command, replay
from .models import Command, Movement, MovementVersion, ReviewCase
from .review_history import (actor_identity, case_state, compact_review_audit, event_response, record_review_event,
                             validate_actor)


def _validate(items, reason, request_id, actor):
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError('Select between 1 and 100 review cases')
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 2000 or len(reason)>2000:
        raise ValueError('A review reason of 5 to 2000 characters is required')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
        raise ValueError('A request ID of 1 to 120 characters is required')
    validate_actor(actor)
    seen=set()
    for item in items:
        if not isinstance(item,dict) or set(item)!={'case_id','expected_version'}:
            raise ValueError('Each selection requires only case_id and expected_version')
        identifier=item['case_id']
        if not isinstance(identifier,str) or not 1<=len(identifier)<=64 or identifier.strip()!=identifier or any(ord(c)<33 for c in identifier):
            raise ValueError('Invalid case ID')
        if identifier in seen: raise ValueError('Select each case only once')
        seen.add(identifier)
        if type(item['expected_version']) is not int or item['expected_version']<1:
            raise ValueError('Every selection requires a positive expected version')


def _failure(s, identifier, exc, *, actor, reason, request_id, context, expected_version):
    case=s.get(ReviewCase,identifier,populate_existing=True)
    movement=s.get(Movement,case.movement_id,populate_existing=True) if case and case.movement_id else None
    code='NOT_FOUND' if isinstance(exc,NotFound) else 'CONFLICT' if isinstance(exc,Conflict) else 'BLOCKED' if isinstance(exc,Blocked) else 'INVALID_REQUEST'
    result={'id':identifier,'ok':False,'status':case.status if case else 'NOT_FOUND',
            'outcome':case.outcome if case else None,'movement_state':movement.state if movement else None,
            'error':str(exc),'code':code}
    if case:
        version=s.get(MovementVersion,case.version_id) if case.version_id else None
        state=case_state(case,movement)
        event=record_review_event(s,case=case,movement=movement,version=version,actor=actor,
            action='RELEASE_REJECTED',outcome='REJECTED',reason=reason,request_id=request_id,
            before=state,after=state,context=context,apply_attribution=False,
            request_evidence={'expected_version':expected_version,'intended_outcome':'VERIFIED_VALID','code':code,'error':str(exc)})
        result.update(event_response(event))
        audit(s,actor,'bulk_case_rejected',identifier,compact_review_audit(event))
    else:
        result.update({'event_id':None,'reviewer':actor_identity(s,actor),'reviewed_at':None,'history_status':'AUDIT_ONLY_CASE_NOT_FOUND'})
        audit(s,actor,'bulk_case_rejected',identifier,{'parent_request_id':context['parent_request_id'],
              'expected_version':expected_version,'code':code,'reason':reason,'reviewer':result['reviewer']})
    return result


def _honest_replay(response):
    # Pre-history receipts remain evidence of their original operation only.
    # Never assign the current caller as the reviewer of an old cached result.
    if 'items' in response and any('reviewer' not in item for item in response['items']):
        return {**deepcopy(response),'history_status':'LEGACY_RECEIPT_WITHOUT_REVIEW_EVENTS'}
    if 'items' not in response and 'reviewer' not in response:
        return {**deepcopy(response),'history_status':'LEGACY_RECEIPT_WITHOUT_REVIEW_EVENTS'}
    return response


def _release_item(db, item, reason, request_id, actor):
    suffix=digest([request_id,item['case_id']])
    child_id='bulk-item:'+suffix
    resolve_id='bulk-resolve:'+suffix
    args={'parent_request_id':request_id,'item':item,'reason':reason,'actor':actor}
    context={'origin':'BULK','parent_request_id':request_id}
    with db.transaction() as s:
        cached=replay(s,child_id,'bulk_release_item',args)
        if cached is not None: return _honest_replay(cached)
        before_deltas=dict(s.info.get('counter_deltas',{}))
        try:
            # The wrapper command lock and item receipt share this transaction.
            # Savepoint rollback also prevents partially changed ORM state from
            # leaking into a recorded, expected domain rejection.
            with s.begin_nested():
                resolved=_resolve_case_in_session(s,item['case_id'],item['expected_version'],
                    'VERIFIED_VALID',reason,resolve_id,actor,review_context=context)
            result={'id':resolved['id'],'ok':True,'status':resolved['status'],
                    'outcome':resolved['outcome'],'movement_state':resolved['movement_state'],
                    'error':None,'code':None,**{key:resolved[key] for key in ('event_id','reviewer','reviewed_at')}}
        except (Blocked,Conflict,NotFound,ValueError) as exc:
            # db.change_state accumulates Python-side deltas, which a SQL
            # savepoint alone cannot roll back.
            s.info['counter_deltas']=defaultdict(int,before_deltas)
            result=_failure(s,item['case_id'],exc,actor=actor,reason=reason,request_id=resolve_id,
                            context=context,expected_version=item['expected_version'])
        return record_command(s,child_id,'bulk_release_item',args,result)


def bulk_release_cases(db,items,reason,request_id,actor):
    """Release selected MODEL_RISK cases without approving invoices or mail.

    Expected domain failures are terminal per-item receipts. An unexpected
    failure aborts that item and leaves the parent RUNNING; repeating the exact
    request resumes committed children. A new review of a terminal failed item
    needs a new parent request ID. Results describe states at each item commit.
    """
    _validate(items,reason,request_id,actor)
    # Copy caller-owned containers so a caller cannot mutate the reserved input
    # between hashing, resolving and finalising the parent operation.
    items=[{'case_id':item['case_id'],'expected_version':item['expected_version']} for item in items]
    args={'items':items,'reason':reason,'actor':actor}
    with db.transaction() as s:
        cached=replay(s,request_id,'bulk_release',args)
        if cached is not None and cached.get('status')=='COMPLETE': return _honest_replay(cached)
        reviewer=actor_identity(s,actor)
        if cached is None:
            record_command(s,request_id,'bulk_release',args,{'status':'RUNNING','request_id':request_id})
            audit(s,actor,'bulk_review_started',request_id,{'items':items,'reason':reason,'reviewer':reviewer})
    results=[_release_item(db,item,reason,request_id,actor) for item in items]
    counts={'requested':len(results),'resolved':sum(item['ok'] and item['status']=='RESOLVED' for item in results),
            'released':sum(item['ok'] and item['movement_state']=='ELIGIBLE' for item in results),
            'failed':sum(not item['ok'] for item in results)}
    response={'request_id':request_id,'status':'COMPLETE','items':results,'counts':counts}
    with db.transaction() as s:
        cached=replay(s,request_id,'bulk_release',args)
        if cached is not None and cached.get('status')=='COMPLETE': return _honest_replay(cached)
        s.get(Command,request_id).response=response
        audit(s,actor,'bulk_review_completed',request_id,counts)
    return response
