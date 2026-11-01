"""Review identity and immutable evidence on isolated PostgreSQL schemas."""
from copy import deepcopy
import hashlib

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from invoice_optimizer import assessment
from invoice_optimizer.assessment import correct_case, resolve_case
from invoice_optimizer.bulk_review import bulk_release_cases
from invoice_optimizer.db import Conflict, digest
from invoice_optimizer.models import Audit, Command, Movement, MovementVersion, ReviewCase
from invoice_optimizer.operators import Operator
from invoice_optimizer.review_history import ReviewEvent, serialize_review_event
from invoice_optimizer.review_import import import_review_csv
from invoice_optimizer.storage import ObjectStore
from test_bulk_review import case_factory, counts, REASON
from test_review_import import mismatch, worklist, correction
from test_invariants import customer, benign_model


@pytest.fixture
def reviewers(db):
    with db.transaction() as s:
        for value,name in [('a','Alice Reviewer'),('b','Bob Reviewer')]:
            s.add(Operator(id=value*32,username='reviewer-'+value,display_name=name,password_hash='unused-domain-test-password-hash',active=True,role='reviewer'))
    return 'user:'+'a'*32,'user:'+'b'*32


def events(db):
    with db.transaction() as s:
        return [serialize_review_event(event) for event in s.scalars(select(ReviewEvent).order_by(ReviewEvent.id))]


def test_review_stores_identity_exact_evidence_and_replay_original_snapshot(db,case_factory,reviewers):
    item=case_factory('identity');alice,bob=reviewers
    first=resolve_case(db,item['case_id'],1,'UNRESOLVED',REASON,'identity-first',alice)
    saved=events(db)
    assert len(saved)==1 and first['event_id']==saved[0]['id']
    event=saved[0]
    assert event['reviewer']=={'actor_id':alice,'display_name':'Alice Reviewer','username':'reviewer-a','identity_kind':'REGISTERED_OPERATOR'}
    assert event['origin']=='SINGLE' and event['reviewed_at'].endswith('+00:00')
    assert event['reviewed_version_id']=='version-identity-1' and event['reviewed_version_no']==1
    assert event['before']['case_outcome'] is None and event['after']['case_outcome']=='UNRESOLVED'
    assert event['evidence']['version']['amount']=='10.00'
    assert event['evidence']['version']['payload']['source_values']['A']['amount']=='10.00'
    model=next(row for row in event['evidence']['assessments'] if row['kind']=='MODEL')
    assert model['id'] and model['model_version']=='fixture-model' and model['policy_version']=='fixture-policy'
    with db.transaction() as s:
        s.get(Operator,'a'*32).display_name='Alice Renamed'
    assert resolve_case(db,item['case_id'],1,'UNRESOLVED',REASON,'identity-first',alice)==first
    assert events(db)==saved
    with pytest.raises(Conflict):resolve_case(db,item['case_id'],1,'UNRESOLVED',REASON,'identity-first',bob)
    second=resolve_case(db,item['case_id'],1,'CONFIRMED_ISSUE','A later review identified an issue.','identity-second',bob)
    assert second['reviewer']['actor_id']==bob and second['movement_state']=='BLOCKED'
    assert events(db)[0]==saved[0]
    with db.transaction() as s:
        case=s.get(ReviewCase,item['case_id'])
        assert case.reviewed_by==bob and case.reviewed_at.isoformat()==second['reviewed_at']
        audit=s.scalar(select(Audit).where(Audit.action=='case_reviewed',Audit.actor==alice))
        assert audit.details['review_event_id']==first['event_id']
        assert audit.details['version_id']==event['reviewed_version_id']
        assert 'evidence' not in audit.details and 'source_refs' not in audit.details


def test_correction_appends_events_for_initiator_and_all_superseded_cases(db,case_factory,reviewers):
    item=case_factory('correction',sibling=True);alice,bob=reviewers
    resolve_case(db,item['case_id'],1,'UNRESOLVED',REASON,'earlier-review',alice)
    old_event=deepcopy(events(db)[0])
    result=correct_case(db,item['case_id'],1,{'description':'Corrected description'},REASON,'correct-history',bob)
    history=events(db)
    assert len(history)==3 and history[0]==old_event
    latest=history[1:]
    assert {row['action'] for row in latest}=={'CORRECTION_APPLIED','SUPERSEDED_BY_CORRECTION'}
    assert all(row['initiator_case_id']==item['case_id'] and row['reviewer']['actor_id']==bob for row in latest)
    assert {row['case_id'] for row in latest}=={item['case_id'],item['case_id']+'-sibling'}
    assert all(row['before']['case_status']=='OPEN' and row['after']['case_status']=='SUPERSEDED' for row in latest)
    assert all(row['before']['movement_version_no']==1 and row['after']['movement_version_no']==2 for row in latest)
    proof=latest[0]['correction']
    assert proof['changes']['description']=={'before':'Synthetic service','after':'Corrected description'}
    assert proof['old_version']['id']=='version-correction-1' and proof['new_version']['version_no']==2
    assert len(result['events'])==2 and result['reviewer']['actor_id']==bob
    assert correct_case(db,item['case_id'],1,{'description':'Corrected description'},REASON,'correct-history',bob)==result
    assert events(db)==history


@pytest.mark.parametrize('operation',['resolve','correct'])
def test_event_and_domain_mutations_rollback_together(db,case_factory,reviewers,monkeypatch,operation):
    item=case_factory('rollback-'+operation);before=counts(db)
    real_audit=assessment.audit
    def fail_after_event(s,actor,action,*args,**kwargs):
        if action in ('case_reviewed','case_correction_applied'):raise RuntimeError('Injected post-event failure')
        return real_audit(s,actor,action,*args,**kwargs)
    monkeypatch.setattr(assessment,'audit',fail_after_event)
    with pytest.raises(RuntimeError):
        if operation=='resolve':resolve_case(db,item['case_id'],1,'VERIFIED_VALID',REASON,'rollback-command',reviewers[0])
        else:correct_case(db,item['case_id'],1,{'description':'new'},REASON,'rollback-command',reviewers[0])
    assert events(db)==[] and counts(db)==before
    with db.transaction() as s:
        case=s.get(ReviewCase,item['case_id'])
        assert case.status=='OPEN' and case.reviewed_by is None and case.reviewed_at is None
        assert s.scalar(select(func.count()).select_from(MovementVersion))==1
        assert s.get(Command,'rollback-command') is None


def test_bulk_success_and_rejection_retain_actor_and_parent_context(db,case_factory,reviewers):
    good=case_factory('bulk-good');bad=case_factory('bulk-bad',rule_status='FAIL')
    result=bulk_release_cases(db,[good,bad],REASON,'bulk-history',reviewers[0])
    assert result['counts']=={'requested':2,'resolved':1,'released':1,'failed':1}
    history=events(db)
    assert len(history)==2 and {row['action'] for row in history}=={'REVIEW_OUTCOME','RELEASE_REJECTED'}
    assert all(row['origin']=='BULK' and row['parent_request_id']=='bulk-history' for row in history)
    assert all(row['reviewer']['actor_id']==reviewers[0] for row in history)
    assert all(item['event_id'] and item['reviewer']['actor_id']==reviewers[0] for item in result['items'])
    assert bulk_release_cases(db,[good,bad],REASON,'bulk-history',reviewers[0])==result
    assert events(db)==history
    with db.transaction() as s:
        case=s.get(ReviewCase,bad['case_id'])
        assert case.reviewed_by is None and case.reviewed_at is None and case.status=='OPEN'


def test_csv_exact_file_and_multiline_row_attribution_and_cross_actor_retry(db,mismatch,tmp_path,reviewers):
    path=worklist(tmp_path/'review.csv',[correction(mismatch,description='New description\nwith second line')])
    first=import_review_csv(db,path,reviewers[0])
    history=events(db)
    assert first['applied']==1 and len(history)==1
    event=history[0]
    assert event['origin']=='CSV' and event['source_file_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert ObjectStore(db.settings.data_dir/'objects').path(event['source_file_sha256'],verify=True).read_bytes()==path.read_bytes()
    assert event['source_row_number']==2 and event['source_row_metadata']=={'physical_start_line':2,'physical_end_line':3}
    assert event['parent_request_id']=='worklist:'+event['source_file_sha256']
    assert event['source_refs'] and event['reviewed_version_id']==mismatch['version_id']
    assert event['reviewer']['actor_id']==reviewers[0]
    assert import_review_csv(db,path,reviewers[0])==first
    rejected=import_review_csv(db,path,reviewers[1])
    assert rejected['applied']==0 and rejected['failed']==1
    assert 'different content' in rejected['results'][0]['error']
    assert 'reviewer' not in rejected['results'][0]
    assert events(db)==history


@pytest.mark.parametrize('version',[True,0,-1,'1',1.0])
def test_correction_requires_strict_positive_integer_version(db,case_factory,version):
    item=case_factory('version-type')
    with pytest.raises(ValueError,match='positive expected version'):
        correct_case(db,item['case_id'],version,{'description':'changed'},REASON,'bad-version','explicit-reviewer')
    assert events(db)==[]


def test_rejected_csv_proposal_is_traced_without_marking_case_reviewed(db,mismatch,tmp_path,reviewers):
    path=worklist(tmp_path/'invalid.csv',[correction(mismatch,review_reason='')])
    first=import_review_csv(db,path,reviewers[0])
    assert first['failed']==1
    history=events(db)
    assert len(history)==1 and history[0]['action']=='CORRECTION_REJECTED'
    assert history[0]['before']==history[0]['after']
    assert history[0]['evidence']['request']['submitted_reason']==''
    assert import_review_csv(db,path,reviewers[0])==first and events(db)==history
    with db.transaction() as s:
        case=s.get(ReviewCase,mismatch['case_id'])
        assert case.status=='OPEN' and case.reviewed_at is None


@pytest.mark.parametrize('actor',[None,'','   ',' user:bad','user:unknown'])
def test_actor_is_required_and_cannot_be_malformed(db,case_factory,actor):
    item=case_factory('bad-actor')
    with pytest.raises(ValueError):resolve_case(db,item['case_id'],1,'UNRESOLVED',REASON,'bad-actor',actor)
    assert events(db)==[]


def test_explicit_cli_identity_is_labeled_without_claiming_authenticated_person(db,case_factory):
    item=case_factory('cli')
    response=resolve_case(db,item['case_id'],1,'UNRESOLVED',REASON,'cli-command','explicit-cli-operator')
    assert response['reviewer']['identity_kind']=='EXPLICIT_LOCAL_ACTOR'
    assert response['reviewer']['username'] is None


def test_missing_and_inactive_registered_reviewers_cannot_mutate(db,case_factory,reviewers):
    item=case_factory('inactive')
    with db.transaction() as s:s.get(Operator,'a'*32).active=False
    for actor in (reviewers[0],'user:'+'c'*32):
        with pytest.raises(ValueError,match='inactive'):
            resolve_case(db,item['case_id'],1,'UNRESOLVED',REASON,'inactive-'+actor,actor)
    assert events(db)==[]


def test_legacy_receipt_is_never_relabelled_as_current_reviewer(db,case_factory,reviewers):
    item=case_factory('legacy')
    old_args={'case_id':item['case_id'],'expected_version':1,'outcome':'UNRESOLVED','reason':REASON}
    with db.transaction() as s:
        s.add(Command(request_id='legacy-request',action='resolve',payload_hash=digest(old_args),response={'id':item['case_id'],'status':'OPEN','outcome':'UNRESOLVED'}))
    with pytest.raises(Conflict):resolve_case(db,item['case_id'],1,'UNRESOLVED',REASON,'legacy-request',reviewers[0])
    assert events(db)==[]


def test_review_event_database_trigger_prevents_update_and_delete(db,case_factory,reviewers):
    item=case_factory('immutable')
    response=resolve_case(db,item['case_id'],1,'UNRESOLVED',REASON,'immutable-event',reviewers[0])
    before=events(db)
    for statement in (update(ReviewEvent).where(ReviewEvent.id==response['event_id']).values(reason='tampered'),
                      delete(ReviewEvent).where(ReviewEvent.id==response['event_id'])):
        with pytest.raises(DBAPIError):
            with db.transaction() as s:s.execute(statement)
    assert events(db)==before


def test_single_rejected_attempt_tracks_caller_without_changing_review(db,case_factory,reviewers):
    item=case_factory('single-rejected',rule_status='FAIL')
    with pytest.raises(ValueError):resolve_case(db,item['case_id'],1,'VERIFIED_VALID',REASON,'blocked-single',reviewers[0])
    assert events(db)==[]
    with db.transaction() as s:
        case=s.get(ReviewCase,item['case_id'])
        assert case.reviewed_by is None and case.reviewed_at is None
        audit=s.scalar(select(Audit).where(Audit.action=='case_review_rejected'))
        assert audit.actor==reviewers[0] and audit.details['reviewer']['display_name']=='Alice Reviewer'
        assert audit.details['request_id']=='blocked-single'
        assert audit.details['observed_after_rejection']['case_status']=='OPEN'
        assert audit.details['intended_outcome']=='VERIFIED_VALID'


def test_cross_actor_replay_records_only_new_attempt_not_a_new_outcome(db,case_factory,reviewers):
    item=case_factory('replay-attempt')
    original=resolve_case(db,item['case_id'],1,'UNRESOLVED',REASON,'same-attribution',reviewers[0])
    with pytest.raises(Conflict):resolve_case(db,item['case_id'],1,'UNRESOLVED',REASON,'same-attribution',reviewers[1])
    assert len(events(db))==1 and events(db)[0]['id']==original['event_id']
    with db.transaction() as s:
        assert s.get(ReviewCase,item['case_id']).reviewed_by==reviewers[0]
        attempt=s.scalar(select(Audit).where(Audit.action=='case_review_rejected'))
        assert attempt.actor==reviewers[1]
        assert attempt.details['observed_after_rejection']['reviewed_by']==reviewers[0]
