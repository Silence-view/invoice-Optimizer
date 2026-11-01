"""Historical comparison is bounded, frozen-model-specific and read-only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import json

import pytest
from sqlalchemy import event, func, select

from invoice_optimizer import ml, review_versions
from invoice_optimizer.assessment import correct_case, resolve_case
from invoice_optimizer.db import Conflict, NotFound, digest
from invoice_optimizer.models import (Assessment, Audit, Batch, Command, Job, Movement,
    MovementVersion, Outbox, ReviewCase, SourceRecord)
from invoice_optimizer.operators import Operator
from invoice_optimizer.review_history import ReviewEvent
from invoice_optimizer.review_versions import case_versions
from invoice_optimizer.synthetic import generate_training_data
from test_bulk_review import case_factory, REASON
from test_model_ops_api import actual_model, actual_case
from test_review_history import reviewers


def add_versions(db,name,numbers,*,current=None):
    with db.transaction() as session:
        movement=session.get(Movement,'movement-'+name)
        original=session.get(MovementVersion,'version-'+name+'-1')
        for number in numbers:
            payload={**deepcopy(original.payload),'description':f'Version {number} exact description',
                     'expected_amount':format(Decimal(number),'.2f')}
            if number==4:payload['optional']=None
            if number==5:payload['source_values']['A']['amount']='10.01'
            session.add(MovementVersion(id=f'version-{name}-{number}',movement_id=movement.id,
                version_no=number,payload=payload,source_refs=[],content_hash=digest([payload,[]]),
                amount=Decimal(payload['amount']),currency='GBP',origin='HUMAN_CORRECTION'))
        movement.current_version=current or max(numbers)


def counts(db):
    with db.transaction() as session:
        return {model.__tablename__:session.scalar(select(func.count()).select_from(model))
                for model in (MovementVersion,Assessment,Audit,ReviewEvent,Command,Job,Outbox)}


def test_keyset_versions_keep_ceiling_compare_adjacent_values_and_never_action_historical_case(db,case_factory):
    case=case_factory('pages')
    add_versions(db,'pages',[2,3,4,5])
    with db.transaction() as session:session.get(ReviewCase,case['case_id']).status='SUPERSEDED'
    before=counts(db)
    first=case_versions(db,case['case_id'],limit=2)
    assert first['read_only'] and first['current_version']==first['current_version_no']==5
    assert first['snapshot_max_version']==5 and first['has_more']
    assert [v['version_no'] for v in first['items']]==[5,4]
    assert first['items'][0]['is_current'] and not first['items'][0]['is_case_version']
    assert all(not item['eligibility']['can_review'] and not item['eligibility']['can_release'] for item in first['items'])
    assert not first['requested_case_eligibility']['can_release']
    changes={item['path']:item for item in first['items'][0]['diff']['changes']}
    assert changes['/payload/source_values/A/amount']['before']=='10.00'
    assert changes['/payload/source_values/A/amount']['after']=='10.01'
    assert changes['/payload/optional']=={'path':'/payload/optional','before':None,'after':None,
        'before_present':True,'after_present':False}
    assert first['items'][1]['diff']['previous_version_no']==3  # The extra row crosses the page boundary.
    assert counts(db)==before
    add_versions(db,'pages',[6])
    second=case_versions(db,case['case_id'],limit=2,cursor=first['next_cursor'])
    assert [v['version_no'] for v in second['items']]==[3,2]
    assert second['current_version']==6 and second['snapshot_max_version']==5 and second['newer_versions_available']
    last=case_versions(db,case['case_id'],limit=2,cursor=second['next_cursor'])
    assert [v['version_no'] for v in last['items']]==[1] and not last['has_more'] and last['next_cursor'] is None
    assert last['items'][0]['diff']['status']=='FIRST_VERSION'


def test_missing_predecessor_is_unavailable_and_never_fabricated(db,case_factory):
    case=case_factory('gap');add_versions(db,'gap',[3])
    result=case_versions(db,case['case_id'])
    assert result['items'][0]['diff']['status']=='PREDECESSOR_UNAVAILABLE'
    assert result['items'][0]['diff']['changes']==[]
    assert result['items'][0]['model']['status']=='NOT_RECORDED'
    assert result['items'][0]['model']['recorded_score'] is None


def test_each_version_uses_its_exact_frozen_model_after_activation_of_another_model(db,actual_case,tmp_path,monkeypatch):
    with db.transaction() as session:
        first=session.get(MovementVersion,'version-actual-explanation-1')
        first.payload={**first.payload,'amount':'10000.00','expected_amount':'10.00',
            'customer_age_days':2,'supplier_age_days':2,'cross_border':1}
        old=ml.score_batch([first.payload],db.settings.model_dir)[0]
        assert old['flagged']
        assessment=session.scalar(select(Assessment).where(Assessment.version_id==first.id,Assessment.kind=='MODEL'))
        for key in ('score','flagged','model_version','policy_version','feature_schema_version'):setattr(assessment,key,old[key])
        assessment.features=old['feature_values'];assessment.explanation=ml.compact_explanation(old['explanation'])
        payload={**first.payload,'amount':'10.00','expected_amount':'10.00','customer_age_days':200,'supplier_age_days':300,'cross_border':0}
    training=tmp_path/'second-training.parquet'
    generate_training_data(training,rows=1000,seed=31415,serving_period='2026-09')
    metadata=ml.train_model(training,db.settings.model_dir)
    assert metadata['model_version']!=old['model_version']
    new=ml.score_batch([payload],db.settings.model_dir)[0]
    with db.transaction() as session:
        movement=session.get(Movement,'movement-actual-explanation');movement.current_version=2
        session.add(MovementVersion(id='version-second',movement_id=movement.id,version_no=2,
            payload=payload,source_refs=[],content_hash=digest([payload,[]]),amount=Decimal('10.00'),currency='GBP'))
        session.flush()
        session.add(Assessment(version_id='version-second',kind='MODEL',status='SCORED',
            score=new['score'],flagged=new['flagged'],features=new['feature_values'],explanation=ml.compact_explanation(new['explanation']),
            model_version=new['model_version'],policy_version=new['policy_version'],feature_schema_version=new['feature_schema_version']))
    before=counts(db)
    monkeypatch.setattr(ml,'score_batch',lambda *a,**k:pytest.fail('Historical comparison must never score new predictions'))
    monkeypatch.setattr(ml,'explain_features',lambda *a,**k:pytest.fail('Historical comparison is not a hypothetical preview'))
    result=case_versions(db,actual_case['case_id'])
    for item,expected in zip(result['items'],[new,old]):
        model=item['model']
        assert model['recorded_score']==expected['score'] and model['model_version']==expected['model_version']
        assert model['threshold']==expected['explanation']['threshold']
        assert model['threshold_status']=='VERIFIED_FROZEN_MODEL' and model['rescored'] is False
        assert model['explanation']['calculation_kind']=='RECORDED'
        assert model['narrative']['provenance']['raw_binding']=='VERIFIED'
        assert model['narrative']['threshold']['score']==expected['score']
    assert counts(db)==before
    # The active model stays usable, but cannot substitute for a missing older artifact.
    (db.settings.model_dir/'versions'/old['model_version']/'model.joblib').rename(tmp_path/'unavailable-old-model.joblib')
    historical=case_versions(db,actual_case['case_id'])['items'][1]['model']
    assert historical['recorded_score']==old['score'] and historical['threshold'] is None
    assert historical['narrative']['status']=='UNAVAILABLE' and historical['model_version']==old['model_version']
    assert counts(db)==before


def test_source_feature_mismatch_does_not_invent_raw_model_reasons(db,actual_case):
    with db.transaction() as session:
        version=session.get(MovementVersion,'version-actual-explanation-1')
        version.payload={**version.payload,'expected_amount':'999999.00'}
    model=case_versions(db,actual_case['case_id'])['items'][0]['model']
    assert model['explanation']['status']=='AVAILABLE'
    assert model['narrative']['provenance']['raw_binding']=='MISMATCH'
    assert all(driver['raw_value'] is None for driver in model['narrative']['drivers']+model['narrative']['mitigating_drivers'])


def test_changed_mathematical_receipt_cannot_silently_supply_a_threshold(db,actual_case):
    with db.transaction() as session:
        version=session.get(MovementVersion,'version-actual-explanation-1')
        result=ml.score_batch([version.payload],db.settings.model_dir)[0]
        assessment=session.scalar(select(Assessment).where(Assessment.version_id==version.id,Assessment.kind=='MODEL'))
        receipt=ml.compact_explanation(result['explanation'])
        receipt['threshold']=0.123456789
        assert receipt['threshold']!=result['explanation']['threshold']
        assessment.explanation=receipt
        score=assessment.score
    model=case_versions(db,actual_case['case_id'])['items'][0]['model']
    assert model['recorded_score']==score and model['threshold'] is None
    assert model['explanation']['status']==model['narrative']['status']=='UNAVAILABLE'


def test_missing_or_wrong_movement_source_reference_is_not_presented_as_verified_provenance(db,case_factory):
    case=case_factory('source-target');other=case_factory('source-other')
    with db.transaction() as session:
        session.add(Batch(id='foreign-batch',source='A',period='2026-09',object_key='raw/test',sha256='a'*64,
                          original_name='other-movement.csv',status='READY'));session.flush()
        session.add(SourceRecord(id='foreign-reference',batch_id='foreign-batch',source='A',record_id='foreign-record',
            revision=1,movement_id='movement-source-other',customer_id='BULK-C1',period='2026-09',payload_hash='b'*64,payload={}))
        session.get(MovementVersion,'version-source-target-1').source_refs=['foreign-reference','missing-reference']
    version=case_versions(db,case['case_id'])['items'][0]
    assert version['source_refs']==['foreign-reference','missing-reference']
    assert version['source_files']==[] and version['provenance_status']=='INCOMPLETE'
    assert version['missing_reference_count_in_preview']==2


@pytest.mark.parametrize('change,expected_code',[
    ({},None),({'rule_status':'FAIL'},'RULE_EVIDENCE_NOT_PASS'),
    ({'model_status':'UNAVAILABLE','score':None},'MODEL_EVIDENCE_INCOMPLETE'),
    ({'sibling':True},'OTHER_OPEN_CASES'),({'issued':True},'ISSUED_MOVEMENT'),
    ({'case_reason':'AMOUNT_MISMATCH'},'NOT_MODEL_RISK'),({'status':'RESOLVED'},'CASE_NOT_OPEN'),
])
def test_release_advice_matches_existing_domain_boundaries(db,case_factory,change,expected_code):
    case=case_factory('eligible',**change)
    result=case_versions(db,case['case_id'])
    eligibility=result['requested_case_eligibility']
    assert eligibility['can_release']==(expected_code is None)
    assert result['items'][0]['eligibility']==eligibility
    if expected_code:assert expected_code in {row['code'] for row in eligibility['blocked_reasons']}
    assert result['items'][0]['model']['threshold'] is None  # Deliberately unavailable fixture model, never active fallback.


def test_review_actor_snapshots_and_correction_provenance_are_historical_not_reassigned(db,case_factory,reviewers):
    case=case_factory('reviewed');alice,bob=reviewers
    first=resolve_case(db,case['case_id'],1,'UNRESOLVED',REASON,'earlier-review',alice)
    correct_case(db,case['case_id'],1,{'description':'Corrected description'},REASON,'correction',bob)
    with db.transaction() as session:session.get(Operator,alice[5:]).display_name='Changed current display name'
    before=counts(db)
    result=case_versions(db,case['case_id'])
    old=result['items'][1]
    original=next(row for row in old['review_events'] if row['id']==first['event_id'])
    assert original['reviewer']['display_name']=='Alice Reviewer'
    assert original['reviewer']['actor_id']==alice and original['reviewed_at']==first['reviewed_at']
    corrected=next(row for row in old['review_events'] if row['action']=='CORRECTION_APPLIED')
    assert corrected['reviewer']['actor_id']==bob
    assert corrected['correction']['old_version']['version_no']==1
    assert corrected['correction']['new_version']['version_no']==2
    assert result['items'][0]['diff']['changes']==[{'path':'/payload/description','before':'Synthetic service',
        'after':'Corrected description','before_present':True,'after_present':True}]
    assert counts(db)==before


def test_linked_evidence_limits_are_explicit_and_queries_page_bounded(db,case_factory):
    case=case_factory('bounded')
    with db.transaction() as session:
        version=session.get(MovementVersion,'version-bounded-1')
        refs=[]
        for index in range(55):
            batch=Batch(id=f'batch-{index}',source='A',period='2026-09',object_key='raw/test',
                sha256=f'{index:064x}',original_name=f'original-{index}.csv',status='READY')
            session.add(batch);session.flush()
            record=SourceRecord(id=f'ref-{index}',batch_id=batch.id,source='A',record_id=f'original-record-{index}',
                revision=1,movement_id=version.movement_id,customer_id='BULK-C1',period='2026-09',payload_hash='a'*64,payload={})
            session.add(record);refs.append(record.id)
        version.source_refs=refs
        for index in range(12):session.add(ReviewCase(id=f'extra-case-{index}',dedup_key=f'extra-case-{index}',
            movement_id=version.movement_id,version_id=version.id,customer_id='BULK-C1',period='2026-09',reason='OTHER',owner='test@example.test'))
    for index in range(7):resolve_case(db,case['case_id'],1,'UNRESOLVED',REASON,f'review-{index}','test:reviewer')
    statements=[]
    def captured(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(db.engine,'before_cursor_execute',captured)
    try:result=case_versions(db,case['case_id'])
    finally:event.remove(db.engine,'before_cursor_execute',captured)
    item=result['items'][0]
    assert item['source_ref_count']==55 and len(item['source_refs'])==len(item['source_files'])==50
    assert item['source_refs_truncated'] and item['source_files_truncated'] and item['provenance_status']=='TRUNCATED'
    assert len(item['related_cases'])==10 and item['related_cases_truncated']
    assert len(item['review_events'])==5 and item['review_events_truncated']
    assert item['source_files'][0]['download_url'].endswith('/file')
    assert len(statements)<=13
    assert 'REPEATABLE READ, READ ONLY' in statements[0]
    assert not any(statement.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for statement in statements)


def test_read_snapshot_is_coherent_and_later_release_still_rejects_stale_version(db,case_factory):
    case=case_factory('concurrent');committed=False
    def concurrently_change(conn,cursor,statement,parameters,context,executemany):
        nonlocal committed
        if committed or 'FROM movements ' not in statement:return
        committed=True
        with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(add_versions,db,'concurrent',[2]).result(timeout=10)
    event.listen(db.engine,'after_cursor_execute',concurrently_change)
    try:page=case_versions(db,case['case_id'])
    finally:event.remove(db.engine,'after_cursor_execute',concurrently_change)
    assert committed and page['current_version']==1 and [v['version_no'] for v in page['items']]==[1]
    assert page['requested_case_eligibility']['can_release']
    with pytest.raises(Conflict):resolve_case(db,case['case_id'],1,'VERIFIED_VALID',REASON,'stale-review','test:human')
    with db.transaction() as session:
        assert session.get(ReviewCase,case['case_id']).reviewed_by is None
        assert session.scalar(select(func.count()).select_from(ReviewEvent))==0
        attempt=session.scalar(select(Audit).where(Audit.action=='case_review_rejected'))
        assert attempt.actor=='test:human'


def test_quarantine_missing_and_invalid_cursors_are_explicit(db,case_factory):
    quarantined=case_factory('quarantine',quarantine=True)
    response=case_versions(db,quarantined['case_id'])
    assert response['status']=='NO_MOVEMENT_VERSION' and response['items']==[]
    assert not response['requested_case_eligibility']['can_review']
    case=case_factory('cursor');add_versions(db,'cursor',[2])
    other=case_factory('other');add_versions(db,'other',[2])
    cursor=case_versions(db,case['case_id'],limit=1)['next_cursor']
    with pytest.raises(ValueError):case_versions(db,other['case_id'],cursor=cursor)
    for value in ('!','a'*1025,'e30'):
        with pytest.raises(ValueError):case_versions(db,case['case_id'],cursor=value)
    for limit in (0,11,True,'5'):
        with pytest.raises(ValueError):case_versions(db,case['case_id'],limit=limit)
    with pytest.raises(NotFound):case_versions(db,'missing-case')
