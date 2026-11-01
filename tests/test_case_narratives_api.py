"""Narrative HTTP evidence stays tied to frozen features and never saves edits."""
from copy import deepcopy

from fastapi.testclient import TestClient
from sqlalchemy import select,func

from invoice_optimizer.api import create_app
from invoice_optimizer.ml import score_batch
from invoice_optimizer.models import Assessment,Audit,Movement,MovementVersion,ReviewCase
from test_model_ops_api import actual_model,actual_case
from test_bulk_review import case_factory


def test_recorded_risk_narrative_uses_exact_source_inputs_and_signed_model_terms(db,actual_case):
    with db.transaction() as session:
        version=session.get(MovementVersion,'version-actual-explanation-1')
        version.payload={**version.payload,'amount':'10000.00','expected_amount':'10.00',
                         'customer_age_days':2,'supplier_age_days':2,'cross_border':1}
        value=score_batch([version.payload],db.settings.model_dir)[0]
        assert value['status']=='SCORED' and value['flagged']
        assessment=session.scalar(select(Assessment).where(Assessment.kind=='MODEL'))
        assessment.features=value['feature_values'];assessment.score=value['score'];assessment.flagged=True
        old=(deepcopy(assessment.features),assessment.score,deepcopy(version.payload))
        audit_count=session.scalar(select(func.count()).select_from(Audit))
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        response=client.get('/api/cases/'+actual_case['case_id']+'/model/explanation')
        assert response.status_code==200
        data=response.json();narrative=data['narrative']
        assert data['case_reason']=='MODEL_RISK'
        assert narrative['status']=='AVAILABLE' and narrative['category']=='MODEL_RISK'
        assert narrative['provenance']['raw_binding']=='VERIFIED'
        assert narrative['provenance']['model_version']==data['model_version']
        assert narrative['threshold']['flagged'] is True
        positive={d['feature']:d for d in narrative['drivers']}
        assert positive and all(d['contribution_log_odds']>0 for d in positive.values())
        for factor in data['explanation']['features']:
            if factor['feature'] in positive:
                assert positive[factor['feature']]['contribution_log_odds']==factor['contribution']
        assert positive['amount_to_expected']['raw_value']=={'amount':'10000.00','expected_amount':'10.00'}
        assert 'threshold' in narrative['summary'].lower()
    with db.transaction() as session:
        version=session.get(MovementVersion,'version-actual-explanation-1')
        assessment=session.scalar(select(Assessment).where(Assessment.kind=='MODEL'))
        assert (assessment.features,assessment.score,version.payload)==old
        assert session.scalar(select(func.count()).select_from(Audit))==audit_count
        assert session.get(ReviewCase,actual_case['case_id']).reviewed_by is None


def test_mismatched_source_payload_cannot_supply_fabricated_raw_narrative(db,actual_case):
    with db.transaction() as session:
        version=session.get(MovementVersion,'version-actual-explanation-1')
        version.payload={**version.payload,'amount':'999999.00'}
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        data=client.get('/api/cases/'+actual_case['case_id']+'/model/explanation').json()
        assert data['explanation']['status']=='AVAILABLE'
        assert not data['preview_available']
        assert data['narrative']['provenance']['raw_binding']=='MISMATCH'
        for driver in data['narrative']['drivers']+data['narrative']['mitigating_drivers']:
            assert driver['raw_value'] is None
            assert '999999' not in driver['text']


def test_hypothetical_narrative_and_stale_case_keep_their_own_evidence(db,actual_case):
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        endpoint='/api/cases/'+actual_case['case_id']+'/model'
        preview=client.post(endpoint+'/preview',json={'expected_version':1,'patch':{'amount':'10000.00'}})
        assert preview.status_code==200
        data=preview.json()
        assert data['hypothetical'] and data['persisted'] is False
        assert data['before_narrative']['provenance']['calculation_kind']=='RECORDED'
        assert data['after_narrative']['provenance']['calculation_kind']=='HYPOTHETICAL'
        assert data['before_narrative']['threshold']['score']==data['before']['score']
        assert data['after_narrative']['threshold']['score']==data['after']['score']
        with db.transaction() as session:
            movement=session.get(Movement,'movement-actual-explanation');movement.current_version=2
        historical=client.get(endpoint+'/explanation').json()
        assert historical['version_no']==1 and historical['current_version']==2
        assert historical['narrative']['provenance']['raw_binding']=='VERIFIED'
        assert historical['editable_inputs']['amount']=='10.00'
        assert not historical['preview_available']
