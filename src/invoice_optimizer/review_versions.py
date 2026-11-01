"""Bounded read-only version comparison using recorded assessment evidence.

This projection never scores with the active model, creates a version, changes
an assessment or performs a review. Eligibility is advisory; the existing
mutation endpoints must still validate the reviewer, request and live version.
"""
import base64
from collections import defaultdict
from copy import deepcopy
import json
import math

from sqlalchemy import func, select, text

from .db import Conflict, NotFound
from .ml import explain_recorded
from .models import Allocation, Assessment, Batch, Movement, MovementVersion, ReviewCase, SourceRecord
from .provenance import file_reference
from .review_history import ReviewEvent, event_response, utc_text
from .risk_explanations import describe_model_result

MAX_VERSIONS = 10
MAX_SOURCE_REFS = 50
MAX_RELATED_CASES = 10
MAX_REVIEW_EVENTS = 5
MAX_DIFF_FIELDS = 100


def _cursor(case_id, movement_id, ceiling, before):
    value={'v':1,'case':case_id,'movement':movement_id,'ceiling':ceiling,'before':before}
    return base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).decode().rstrip('=')


def _read_cursor(value,case_id,movement_id,current):
    try:
        if not isinstance(value,str) or not 1<=len(value)<=1024:raise ValueError()
        decoded=base64.b64decode(value+'='*((-len(value))%4),altchars=b'-_',validate=True)
        data=json.loads(decoded)
        if not isinstance(data,dict) or set(data)!={'v','case','movement','ceiling','before'}:raise ValueError()
        if type(data['v']) is not int or data['v']!=1 or data['case']!=case_id or data['movement']!=movement_id:raise ValueError()
        if any(type(data[key]) is not int or data[key]<1 for key in ('ceiling','before')):raise ValueError()
        if data['before']>data['ceiling'] or data['ceiling']>current:raise ValueError()
        return data['ceiling'],data['before']
    except (ValueError,TypeError,UnicodeError,OverflowError) as exc:
        raise ValueError('Invalid version-history cursor for this case') from exc


def _finite_score(value):
    return type(value) in (int,float) and math.isfinite(value) and 0<=value<=1


def _model_evidence(assessment,version,model_dir):
    missing={'status':'UNAVAILABLE','reason':'No recorded model assessment is available for this version.'}
    explanation=missing
    if assessment and assessment.status=='SCORED' and _finite_score(assessment.score):
        explanation=explain_recorded(assessment.features,model_dir,assessment.model_version,
                                     assessment.score,assessment.policy_version)
        if explanation.get('status')=='AVAILABLE':
            if (explanation.get('flagged')!=assessment.flagged or
                    explanation.get('feature_schema_version')!=assessment.feature_schema_version):
                explanation={'status':'UNAVAILABLE','reason':'Recorded assessment metadata does not match its frozen explanation.',
                             'model_version':assessment.model_version,'policy_version':assessment.policy_version}
            elif isinstance(assessment.explanation,dict) and assessment.explanation.get('status')=='AVAILABLE':
                # New compact receipts bind the explanatory threshold/artifact;
                # older assessments without a receipt still use their exact ID.
                receipt=assessment.explanation
                for key in ('model_version','policy_version','artifact_sha256','threshold','score'):
                    if key in receipt and receipt[key]!=explanation.get(key):
                        explanation={'status':'UNAVAILABLE','reason':'The recorded mathematical receipt differs from its frozen explanation.',
                                     'model_version':assessment.model_version,'policy_version':assessment.policy_version}
                        break
    elif assessment:
        explanation={'status':'UNAVAILABLE','reason':'This version has no valid recorded SCORED model result.',
                     'model_version':assessment.model_version,'policy_version':assessment.policy_version}
    available=explanation.get('status')=='AVAILABLE'
    return {'assessment_id':assessment.id if assessment else None,
        'status':assessment.status if assessment else 'NOT_RECORDED',
        'recorded_score':assessment.score if assessment and assessment.status=='SCORED' and _finite_score(assessment.score) else None,
        'flagged':assessment.flagged if assessment else None,
        'model_version':assessment.model_version if assessment else None,
        'policy_version':assessment.policy_version if assessment else None,
        'feature_schema_version':assessment.feature_schema_version if assessment else None,
        'features':deepcopy(assessment.features) if assessment else None,
        'recorded_reasons':deepcopy(assessment.reasons) if assessment else [],
        'feature_cutoff':utc_text(assessment.feature_cutoff) if assessment else None,
        'evaluated_at':utc_text(assessment.evaluated_at) if assessment else None,
        'threshold':explanation.get('threshold') if available else None,
        'threshold_status':'VERIFIED_FROZEN_MODEL' if available else 'UNAVAILABLE',
        'explanation':explanation,
        'narrative':describe_model_result(explanation,payload=version.payload,
                                          feature_values=assessment.features if assessment else None),
        'evidence_origin':'RECORDED_ASSESSMENT','rescored':False}


def _rule_evidence(assessment):
    return {'assessment_id':assessment.id if assessment else None,
        'status':assessment.status if assessment else 'NOT_RECORDED',
        'flagged':assessment.flagged if assessment else None,
        'recorded_reasons':deepcopy(assessment.reasons) if assessment else [],
        'rule_version':assessment.rule_version if assessment else None,
        'feature_cutoff':utc_text(assessment.feature_cutoff) if assessment else None,
        'evaluated_at':utc_text(assessment.evaluated_at) if assessment else None,
        'evidence_origin':'RECORDED_ASSESSMENT','recomputed':False}


def _eligibility(case,movement,version,assessments,allocated,open_count):
    review=[]
    def block(code,message):review.append({'code':code,'message':message})
    if not movement or not version:block('NO_MOVEMENT_VERSION','No retained movement version can be reviewed.')
    else:
        if version.version_no!=movement.current_version:block('HISTORICAL_VERSION','Historical versions are read-only. Refresh the current case before reviewing.')
        if case.version_id!=version.id:block('DIFFERENT_CASE_VERSION','This version does not belong to the requested review case.')
    if case.status!='OPEN':block('CASE_NOT_OPEN','The requested review case is no longer open.')
    if allocated:block('ISSUED_MOVEMENT','This movement is allocated to an issued invoice; ordinary release or correction is unavailable.')
    release=deepcopy(review)
    def release_block(code,message):release.append({'code':code,'message':message})
    if case.reason!='MODEL_RISK':release_block('NOT_MODEL_RISK','Human release cannot clear a deterministic failure, quarantine or missing model score.')
    if movement and movement.state!='REVIEW':release_block('NOT_IN_REVIEW','The movement is not in the review state.')
    rule=assessments.get((version.id,'RULE')) if version else None
    model=assessments.get((version.id,'MODEL')) if version else None
    if not rule or rule.status!='PASS':release_block('RULE_EVIDENCE_NOT_PASS','Recorded deterministic checks must pass before a model-risk release.')
    complete=(model and model.status=='SCORED' and _finite_score(model.score) and model.flagged
              and all((model.model_version,model.policy_version,model.feature_schema_version)) and isinstance(model.features,dict))
    if not complete:release_block('MODEL_EVIDENCE_INCOMPLETE','A complete flagged model assessment is required for release.')
    if version and open_count.get(version.id,0)>1:release_block('OTHER_OPEN_CASES','Other open cases on this version must be reviewed individually.')
    return {'can_review':not review,'can_release':not release,'case_id':case.id,
        'expected_version':version.version_no if version else None,'advisory':True,
        'review_blocked_reasons':review,'release_blocked_reasons':release,'blocked_reasons':release,
        'notice':'Navigation is read-only. A separate authenticated review must recheck the current version and record the reviewer and reason.'}


def _diff(version,previous):
    if previous is None or previous.version_no!=version.version_no-1:
        return {'status':'FIRST_VERSION' if version.version_no==1 else 'PREDECESSOR_UNAVAILABLE',
            'previous_version_id':None,'previous_version_no':None,'changes':[],'changes_truncated':False,
            'source_refs_added':[],'source_refs_removed':[],'source_ref_changes_truncated':False}
    changes=[]
    def walk(before,after,path,before_present=True,after_present=True):
        if len(changes)>MAX_DIFF_FIELDS:return
        if before_present and after_present and isinstance(before,dict) and isinstance(after,dict):
            for key in sorted(set(before)|set(after)):
                escaped=str(key).replace('~','~0').replace('/','~1')
                walk(before.get(key),after.get(key),path+'/'+escaped,key in before,key in after)
                if len(changes)>MAX_DIFF_FIELDS:break
        elif before_present!=after_present or type(before)!=type(after) or before!=after:
            changes.append({'path':path,'before':deepcopy(before),'after':deepcopy(after),
                            'before_present':before_present,'after_present':after_present})
    walk(previous.payload,version.payload,'/payload')
    for key in ('amount','currency'):
        before,after=getattr(previous,key),getattr(version,key)
        if key=='amount':before,after=str(before),str(after)
        walk(before,after,'/'+key)
    old_refs,new_refs=set(previous.source_refs),set(version.source_refs)
    added,removed=sorted(new_refs-old_refs),sorted(old_refs-new_refs)
    return {'status':'AVAILABLE','previous_version_id':previous.id,'previous_version_no':previous.version_no,
        'changes':changes[:MAX_DIFF_FIELDS],'changes_truncated':len(changes)>MAX_DIFF_FIELDS,
        'source_refs_added':added[:MAX_SOURCE_REFS],'source_refs_removed':removed[:MAX_SOURCE_REFS],
        'source_ref_changes_truncated':len(added)>MAX_SOURCE_REFS or len(removed)>MAX_SOURCE_REFS,
        'source_refs_added_count':len(added),'source_refs_removed_count':len(removed)}


def case_versions(db,case_id,limit=5,cursor=None):
    """Return one coherent, bounded historical page without any mutation."""
    if type(limit) is not int or not 1<=limit<=MAX_VERSIONS:raise ValueError('Version limit must be between 1 and 10')
    if not isinstance(case_id,str) or not 1<=len(case_id)<=64:raise ValueError('Invalid case ID')
    with db.transaction() as session:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        observed_at=utc_text(session.scalar(select(func.current_timestamp())))
        case=session.get(ReviewCase,case_id)
        if not case:raise NotFound('Case not found')
        movement=session.get(Movement,case.movement_id) if case.movement_id else None
        case_version=session.get(MovementVersion,case.version_id) if case.version_id else None
        base={'case_id':case.id,'requested_case_version_id':case.version_id,
              'current_version':movement.current_version if movement else None,
              'current_version_no':movement.current_version if movement else None,
              'movement_id':movement.id if movement else None,
              'external_movement_id':movement.external_id if movement else None,
              'customer_id':case.customer_id,'period':case.period,'case_status':case.status,'case_reason':case.reason,
              'observed_at':observed_at,'read_only':True,'limit':limit}
        if not movement:
            if cursor is not None:raise ValueError('A quarantine case has no version-history cursor')
            return {**base,'status':'NO_MOVEMENT_VERSION','reason':'Quarantined source evidence has no retained movement version.',
                    'snapshot_max_version':None,'items':[],'has_more':False,'next_cursor':None,
                    'requested_case_eligibility':_eligibility(case,None,None,{},False,{})}
        if case_version and case_version.movement_id!=movement.id:raise Conflict('Case and movement version do not match')
        ceiling,before=(movement.current_version,None) if cursor is None else _read_cursor(cursor,case.id,movement.id,movement.current_version)
        query=select(MovementVersion).where(MovementVersion.movement_id==movement.id,MovementVersion.version_no<=ceiling)
        if before is not None:query=query.where(MovementVersion.version_no<before)
        fetched=list(session.scalars(query.order_by(MovementVersion.version_no.desc()).limit(limit+1)))
        versions=fetched[:limit]
        version_ids={v.id for v in versions}|({case_version.id} if case_version else set())
        assessments={(a.version_id,a.kind):a for a in session.scalars(select(Assessment).where(
            Assessment.version_id.in_(version_ids),Assessment.kind.in_(('RULE','MODEL'))))}
        allocated=session.get(Allocation,movement.id) is not None
        open_counts=dict(session.execute(select(ReviewCase.version_id,func.count()).where(
            ReviewCase.movement_id==movement.id,ReviewCase.version_id.in_(version_ids),ReviewCase.status=='OPEN').group_by(ReviewCase.version_id)).all())
        requested_eligibility=_eligibility(case,movement,case_version,assessments,allocated,open_counts)
        ranked_cases=select(ReviewCase.id,func.row_number().over(partition_by=ReviewCase.version_id,
            order_by=(ReviewCase.created_at.desc(),ReviewCase.id)).label('position')).where(
                ReviewCase.movement_id==movement.id,ReviewCase.version_id.in_([v.id for v in versions])).subquery()
        cases=defaultdict(list)
        for related in session.scalars(select(ReviewCase).join(ranked_cases,ranked_cases.c.id==ReviewCase.id).where(
                ranked_cases.c.position<=MAX_RELATED_CASES+1).order_by(ReviewCase.created_at.desc(),ReviewCase.id)):
            cases[related.version_id].append({'id':related.id,'reason':related.reason,'status':related.status,
                'outcome':related.outcome,'review_reason':related.review_reason,'reviewed_by':related.reviewed_by,
                'reviewed_at':utc_text(related.reviewed_at),'created_at':utc_text(related.created_at)})
        ranked_events=select(ReviewEvent.id,func.row_number().over(partition_by=ReviewEvent.reviewed_version_id,
            order_by=ReviewEvent.id.desc()).label('position')).where(ReviewEvent.movement_id==movement.id,
                ReviewEvent.reviewed_version_id.in_([v.id for v in versions])).subquery()
        events=defaultdict(list)
        for event in session.scalars(select(ReviewEvent).join(ranked_events,ranked_events.c.id==ReviewEvent.id).where(
                ranked_events.c.position<=MAX_REVIEW_EVENTS+1).order_by(ReviewEvent.id.desc())):
            events[event.reviewed_version_id].append({'id':event.id,'case_id':event.case_id,**event_response(event),
                'action':event.action,'outcome':event.outcome,'reason':event.reason,'origin':event.origin,
                'request_id':event.request_id,'parent_request_id':event.parent_request_id,
                'initiator_case_id':event.initiator_case_id,'reviewed_version_id':event.reviewed_version_id,
                'reviewed_version_no':event.reviewed_version_no,'correction':deepcopy(event.correction),
                'history_url':f'/api/cases/{event.case_id}/history'})
        refs={ref for version in versions for ref in version.source_refs[:MAX_SOURCE_REFS]}
        sources={r.id:r for r in session.scalars(select(SourceRecord).where(SourceRecord.id.in_(refs),
            SourceRecord.movement_id==movement.id,SourceRecord.customer_id==movement.customer_id,
            SourceRecord.period==movement.period))}
        batches={b.id:file_reference(b) for b in session.scalars(select(Batch).where(Batch.id.in_({r.batch_id for r in sources.values()})))}
        items=[]
        for index,version in enumerate(versions):
            selected_refs=version.source_refs[:MAX_SOURCE_REFS]
            missing=sum(ref not in sources for ref in selected_refs)+(0 if version.source_refs else 1)
            batch_ids={sources[ref].batch_id for ref in selected_refs if ref in sources}
            missing+=sum(batch_id not in batches for batch_id in batch_ids)
            truncated=len(version.source_refs)>MAX_SOURCE_REFS
            items.append({'id':version.id,'version_no':version.version_no,'is_current':version.version_no==movement.current_version,
                'is_case_version':version.id==case.version_id,'origin':version.origin,'created_at':utc_text(version.created_at),
                'content_hash':version.content_hash,'amount':str(version.amount),'currency':version.currency,'payload':deepcopy(version.payload),
                'source_refs':deepcopy(selected_refs),'source_ref_count':len(version.source_refs),'source_refs_truncated':truncated,
                'source_files':[batches[key] for key in sorted(batch_ids) if key in batches],
                'source_files_truncated':truncated,'missing_reference_count_in_preview':missing,
                'provenance_status':'INCOMPLETE' if missing else 'TRUNCATED' if truncated else 'COMPLETE',
                'rule':_rule_evidence(assessments.get((version.id,'RULE'))),
                'model':_model_evidence(assessments.get((version.id,'MODEL')),version,db.settings.model_dir),
                'diff':_diff(version,fetched[index+1] if index+1<len(fetched) else None),
                'related_cases':cases[version.id][:MAX_RELATED_CASES],
                'related_cases_truncated':len(cases[version.id])>MAX_RELATED_CASES,
                'review_events':events[version.id][:MAX_REVIEW_EVENTS],
                'review_events_truncated':len(events[version.id])>MAX_REVIEW_EVENTS,
                'eligibility':_eligibility(case,movement,version,assessments,allocated,open_counts)})
        has_more=len(fetched)>limit
        return {**base,'status':'AVAILABLE' if items else 'NO_MOVEMENT_VERSION',
            'snapshot_max_version':ceiling,'newer_versions_available':movement.current_version>ceiling,
            'items':items,'has_more':has_more,
            'next_cursor':_cursor(case.id,movement.id,ceiling,versions[-1].version_no) if has_more else None,
            'requested_case_eligibility':requested_eligibility,
            'notice':'Stored versions and assessment evidence only. Reconstructing a frozen explanation does not rescore or modify history.'}
