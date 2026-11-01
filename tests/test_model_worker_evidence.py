"""Real inference persistence and deterministic gates in an isolated SQL schema."""
import json

from sqlalchemy import select, func

from invoice_optimizer.ingestion import load_customers, ingest_file
from invoice_optimizer.ml import train_model, explain_recorded
from invoice_optimizer.models import Assessment, Movement, MovementVersion, Invoice, Outbox
from invoice_optimizer.synthetic import generate_training_data
from invoice_optimizer.worker import run_job
from test_workflow import rows, write_csv


def test_real_worker_persists_compact_explanations_without_rewriting_frozen_scores(db,tmp_path):
    training=tmp_path/'train.parquet'
    generate_training_data(training,rows=2000,seed=918,serving_period='2026-09')
    model=train_model(training,db.settings.model_dir)
    master=tmp_path/'customers.json'
    master.write_text(json.dumps([{'id':'C1','legal_name':'Synthetic Client Ltd',
        'address':'1 Test Road, London','email':'client@example.test','alert_email':'review@example.test'}]))
    load_customers(db,master)
    a=rows(amounts=('10.00','10000.00','10000.00','10.00'))
    for row in a[1:3]:row.update(expected_amount='10.00',customer_age_days='2',supplier_age_days='2',cross_border='1')
    a[3]['expected_amount']=''
    b=[dict(row) for row in a];b[2]['amount']='11000.00'
    for side,data in (('A',a),('B',b)):
        ingest_file(db,write_csv(tmp_path/(side+'.csv'),data),side,'2026-09',4,True)
    while run_job(db,kinds=('ASSESS','SCORE')):pass
    with db.transaction() as s:
        states=dict(s.execute(select(Movement.external_id,Movement.state)).all())
        assert states=={'m0':'ELIGIBLE','m1':'REVIEW','m2':'BLOCKED','m3':'REVIEW'}
        assessments=list(s.scalars(select(Assessment).where(Assessment.kind=='MODEL').order_by(Assessment.id)))
        assert len(assessments)==4
        frozen=[(a.id,a.score,a.model_version,a.features,a.explanation) for a in assessments]
        scored=[a for a in assessments if a.status=='SCORED']
        assert len(scored)==3
        for assessment in scored:
            receipt=assessment.explanation
            assert receipt['status']=='AVAILABLE' and receipt['model_version']==model['model_version']
            assert len(json.dumps(receipt).encode())<=1024
            assert 'features' not in receipt
            expanded=explain_recorded(assessment.features,db.settings.model_dir,
                assessment.model_version,assessment.score,assessment.policy_version)
            assert expanded['status']=='AVAILABLE' and abs(expanded['reconstruction_error'])<1e-12
            assert s.get(MovementVersion,assessment.version_id).source_refs
        unavailable=next(a for a in assessments if a.status=='UNAVAILABLE')
        assert unavailable.score is None and unavailable.explanation['status']=='UNAVAILABLE'
        assert s.scalar(select(func.count()).select_from(Invoice))==0
        assert s.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind=='INVOICE'))==0
    next_training=tmp_path/'next.parquet'
    generate_training_data(next_training,rows=2000,seed=919,serving_period='2026-09')
    new_model=train_model(next_training,db.settings.model_dir)
    assert new_model['model_version']!=model['model_version']
    assert not run_job(db,kinds=('ASSESS','SCORE'))
    with db.transaction() as s:
        current=list(s.scalars(select(Assessment).where(Assessment.kind=='MODEL').order_by(Assessment.id)))
        assert [(a.id,a.score,a.model_version,a.features,a.explanation) for a in current]==frozen
    assert explain_recorded(scored[0].features,db.settings.model_dir,scored[0].model_version,
                            scored[0].score,scored[0].policy_version)['status']=='AVAILABLE'
