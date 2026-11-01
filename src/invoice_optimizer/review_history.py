"""Immutable review evidence, recorded in the same transaction as its action."""
from copy import deepcopy
from datetime import datetime, timezone
import re

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.exc import SQLAlchemyError

from .models import Assessment, Base, now


class ReviewEvent(Base):
    __tablename__ = 'review_events'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey('review_cases.id'))
    movement_id: Mapped[str | None] = mapped_column(ForeignKey('movements.id'), nullable=True, index=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey('customers.id'), nullable=True)
    period: Mapped[str] = mapped_column(String(7))
    reviewed_version_id: Mapped[str | None] = mapped_column(ForeignKey('movement_versions.id'), nullable=True)
    reviewed_version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_id: Mapped[str] = mapped_column(String(120))
    actor_display_name: Mapped[str] = mapped_column(String(240))
    actor_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    actor_identity_kind: Mapped[str] = mapped_column(String(40))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    action: Mapped[str] = mapped_column(String(50))
    outcome: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    before: Mapped[dict] = mapped_column(JSONB)
    after: Mapped[dict] = mapped_column(JSONB)
    request_id: Mapped[str] = mapped_column(String(120), index=True)
    origin: Mapped[str] = mapped_column(String(10))
    parent_request_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    source_file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_row_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    initiator_case_id: Mapped[str | None] = mapped_column(ForeignKey('review_cases.id'), nullable=True)
    correction: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB)
    evidence: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint('case_id', 'request_id', 'action'),
        CheckConstraint("length(trim(actor_id)) > 0"),CheckConstraint("length(trim(actor_display_name)) > 0"),
        CheckConstraint("length(trim(reason)) > 0"),CheckConstraint("origin IN ('SINGLE','BULK','CSV')"),
        Index('ix_review_event_case_cursor','case_id','id'),Index('ix_review_event_customer_cursor','customer_id','id'))


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def validate_actor(actor):
    if not isinstance(actor,str) or not 1<=len(actor)<=120 or actor!=actor.strip() or any(ord(c)<32 for c in actor):
        raise ValueError('An explicit reviewer identity of 1 to 120 characters is required')
    if actor.startswith('user:') and not re.fullmatch(r'user:[a-f0-9]{32}',actor):
        raise ValueError('Registered reviewer identity is invalid')
    return actor


def actor_identity(session, actor):
    validate_actor(actor)
    if actor.startswith('user:'):
        from .operators import Operator
        operator=session.get(Operator,actor[5:])
        if operator is None or not operator.active:
            raise ValueError('Registered reviewer does not exist or is inactive')
        return {'actor_id':actor,'display_name':operator.display_name,'username':operator.username,
                'identity_kind':'REGISTERED_OPERATOR'}
    return {'actor_id':actor,'display_name':actor,'username':None,'identity_kind':'EXPLICIT_LOCAL_ACTOR'}


def review_context(context=None):
    value={'origin':'SINGLE',**(context or {})}
    allowed={'origin','parent_request_id','source_file_sha256','source_row_number','source_row_metadata'}
    if set(value)-allowed or value['origin'] not in ('SINGLE','BULK','CSV'):
        raise ValueError('Invalid review origin context')
    parent=value.get('parent_request_id')
    if parent is not None and (not isinstance(parent,str) or not 1<=len(parent)<=120):
        raise ValueError('Invalid parent review request ID')
    if value['origin'] in ('BULK','CSV') and not parent:
        raise ValueError('Bulk and CSV reviews require a parent request ID')
    if value['origin']=='CSV':
        if not isinstance(value.get('source_file_sha256'),str) or not re.fullmatch('[a-f0-9]{64}',value['source_file_sha256']):
            raise ValueError('CSV review requires the source file SHA-256')
        if type(value.get('source_row_number')) is not int or value['source_row_number']<2:
            raise ValueError('CSV review requires its logical data row number')
    elif any(value.get(key) is not None for key in ('source_file_sha256','source_row_number','source_row_metadata')):
        raise ValueError('Only CSV reviews can carry source-file row metadata')
    if value.get('source_row_metadata') is not None:
        metadata=value['source_row_metadata']
        if not isinstance(metadata,dict) or set(metadata)-{'physical_start_line','physical_end_line'} or any(type(v) is not int or v<2 for v in metadata.values()):
            raise ValueError('Invalid CSV physical line metadata')
    return deepcopy(value)


def case_state(case, movement):
    return {'case_status':case.status,'case_outcome':case.outcome,
            'movement_state':movement.state if movement else None,
            'movement_version_no':movement.current_version if movement else None,
            'reviewed_by':case.reviewed_by,'reviewed_at':utc_text(case.reviewed_at)}


def _evidence(session, version):
    if version is None:
        return {'version':None,'assessments':[]}
    result={'version':{'id':version.id,'version_no':version.version_no,'content_hash':version.content_hash,
            'origin':version.origin,'amount':str(version.amount),'currency':version.currency,
            'payload':deepcopy(version.payload)},'assessments':[]}
    for assessment in session.scalars(select(Assessment).where(Assessment.version_id==version.id).order_by(Assessment.kind,Assessment.id)):
        result['assessments'].append({'id':assessment.id,'kind':assessment.kind,'status':assessment.status,
            'score':assessment.score,'flagged':assessment.flagged,'reasons':deepcopy(assessment.reasons),
            'model_version':assessment.model_version,'policy_version':assessment.policy_version,
            'feature_schema_version':assessment.feature_schema_version,'features':deepcopy(assessment.features),
            'explanation':deepcopy(assessment.explanation),
            'rule_version':assessment.rule_version,'feature_cutoff':utc_text(assessment.feature_cutoff),
            'evaluated_at':utc_text(assessment.evaluated_at)})
    return result


def record_review_event(session, *, case, movement, version, actor, action, outcome, reason,
                        request_id, before, after, context=None, initiator_case_id=None,
                        correction=None, occurred_at=None, apply_attribution=True, request_evidence=None):
    identity=actor_identity(session,actor)
    context=review_context(context)
    timestamp=occurred_at or now()
    before,after=deepcopy(before),deepcopy(after)
    if apply_attribution:
        case.reviewed_by=actor;case.reviewed_at=timestamp
        after.update({'reviewed_by':actor,'reviewed_at':utc_text(timestamp)})
    evidence=_evidence(session,version)
    if request_evidence is not None:evidence['request']=deepcopy(request_evidence)
    event=ReviewEvent(case_id=case.id,movement_id=movement.id if movement else None,
        customer_id=case.customer_id,period=case.period,
        reviewed_version_id=version.id if version else None,reviewed_version_no=version.version_no if version else None,
        actor_id=actor,actor_display_name=identity['display_name'],actor_username=identity['username'],
        actor_identity_kind=identity['identity_kind'],occurred_at=timestamp,action=action,outcome=outcome,
        reason=reason,before=before,after=after,request_id=request_id,origin=context['origin'],
        parent_request_id=context.get('parent_request_id'),source_file_sha256=context.get('source_file_sha256'),
        source_row_number=context.get('source_row_number'),source_row_metadata=context.get('source_row_metadata'),
        initiator_case_id=initiator_case_id or case.id,correction=deepcopy(correction),
        source_refs=deepcopy(version.source_refs) if version else [],evidence=evidence)
    session.add(event);session.flush()
    return event


def event_response(event):
    return {'event_id':event.id,'reviewer':{'actor_id':event.actor_id,'display_name':event.actor_display_name,
            'username':event.actor_username,'identity_kind':event.actor_identity_kind},
            'reviewed_at':utc_text(event.occurred_at)}


def serialize_review_event(event):
    return {'id':event.id,'case_id':event.case_id,'movement_id':event.movement_id,
        'customer_id':event.customer_id,'period':event.period,'reviewed_version_id':event.reviewed_version_id,
        'reviewed_version_no':event.reviewed_version_no,**event_response(event),'occurred_at':utc_text(event.occurred_at),
        'action':event.action,'outcome':event.outcome,'reason':event.reason,'before':deepcopy(event.before),
        'after':deepcopy(event.after),'request_id':event.request_id,'origin':event.origin,
        'parent_request_id':event.parent_request_id,'source_file_sha256':event.source_file_sha256,
        'source_row_number':event.source_row_number,'source_row_metadata':deepcopy(event.source_row_metadata),
        'initiator_case_id':event.initiator_case_id,'correction':deepcopy(event.correction),
        'source_refs':deepcopy(event.source_refs),'evidence':deepcopy(event.evidence)}


def compact_review_audit(event):
    """Operational audit index; the complete frozen evidence lives in ReviewEvent."""
    return {'review_event_id':event.id,'case_id':event.case_id,'version_id':event.reviewed_version_id,
        'version_no':event.reviewed_version_no,'action':event.action,'outcome':event.outcome,'reason':event.reason,
        'request_id':event.request_id,'origin':event.origin,'parent_request_id':event.parent_request_id,
        'source_file_sha256':event.source_file_sha256,'source_row_number':event.source_row_number,
        'initiator_case_id':event.initiator_case_id,**event_response(event),
        'before':deepcopy(event.before),'after':deepcopy(event.after)}


def record_rejected_attempt(db, *, case_id, actor, request_id, expected_version, reason, error,
                            action, intended_outcome=None, intended_patch=None):
    """Separate attempt evidence; it must not turn a rejection into a review.

    Called only after a domain ValueError has rolled back. Invalid principals
    are not accepted as reviewer identities. Storage failure preserves the
    original domain exception rather than claiming that an event was stored.
    """
    from .db import audit
    from .models import Movement, MovementVersion, ReviewCase
    try:
        validate_actor(actor)
        with db.transaction() as session:
            reviewer=actor_identity(session,actor)
            identifier=case_id if isinstance(case_id,str) and len(case_id)<=64 else None
            case=session.get(ReviewCase,identifier) if identifier else None
            movement=session.get(Movement,case.movement_id) if case and case.movement_id else None
            version=session.get(MovementVersion,case.version_id) if case and case.version_id else None
            observed=case_state(case,movement) if case else {'case_status':'NOT_FOUND'}
            observed.update({'case_version_id':version.id if version else None,
                             'case_version_no':version.version_no if version else None})
            details={'request_id':str(request_id)[:120],'expected_version':expected_version if type(expected_version) is int else str(expected_version)[:80],
                'reason':str(reason)[:2000],'error':str(error)[:500],'reviewer':reviewer,
                'observed_after_rejection':observed,'interpretation':'Rejected attempt; no review outcome or approval was applied.'}
            if intended_outcome is not None:details['intended_outcome']=str(intended_outcome)[:40]
            if isinstance(intended_patch,dict):
                details['intended_patch']={key:str(value)[:1000] for key,value in intended_patch.items()
                    if key in ('source_a_amount','source_b_amount','description','expected_amount')}
            audit(session,actor,action,identifier or '<invalid-case-id>',details)
    except ValueError:
        return  # No fabricated reviewer attribution for an invalid principal.
    except SQLAlchemyError:
        error.add_note('The rejected attempt could not be recorded because audit storage was unavailable.')
