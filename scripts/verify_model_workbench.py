#!/usr/bin/env python3
"""Bounded read-only receipt for training activation and preserved business state."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from sqlalchemy import text, select
from invoice_optimizer.config import Settings
from invoice_optimizer.db import Database, canonical
from invoice_optimizer.ml import model_readiness, score_batch, compact_explanation, explain_recorded
from invoice_optimizer.models import Movement, MovementVersion, Assessment
from invoice_optimizer.storage import ObjectStore


def business_snapshot(db):
    # Excludes the additive explanation column so pre/post migration rows compare.
    tables={
        'review_cases':'id,dedup_key,movement_id,version_id,customer_id,period,reason,status,outcome,review_reason,reviewed_by,reviewed_at',
        'movements':'id,external_id,customer_id,period,state,current_version',
        'movement_versions':'id,movement_id,version_no,content_hash,payload,source_refs,amount,currency,origin',
        'assessments':'id,version_id,kind,status,score,flagged,model_version,policy_version,feature_schema_version,features,reasons,feature_cutoff',
        'invoices':'id,status,fingerprint,approved_by,approved_at,pdf_sha256',
    }
    result={}
    with db.transaction() as session:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        for table,columns in tables.items():
            checksum=hashlib.sha256();count=0
            records=session.execute(text(f'SELECT {columns} FROM {table} ORDER BY id').execution_options(yield_per=512))
            for row in records:
                checksum.update(canonical(dict(row._mapping)).encode());checksum.update(b'\n');count+=1
            result[table]={'rows':count,'sha256':checksum.hexdigest()}
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--before',type=Path,help='Compare to a previously captured baseline and verify the active run')
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    db=Database(Settings.from_env().prepare())
    receipt={'at':datetime.now(timezone.utc).isoformat(),'scope':'Read-only local synthetic training/inference and preserved business records'}
    try:
        receipt['business']=business_snapshot(db)
        if args.before:
            from invoice_optimizer.model_ops import ModelTrainingRun, serialize_training_run
            from invoice_optimizer.api import worker_info
            before=json.loads(args.before.read_text())
            assert receipt['business']==before['business'],'Business records changed during model activation'
            receipt['business_records_unchanged']=True
            ready=model_readiness(db.settings.model_dir)
            assert ready['status']=='READY',ready.get('reason')
            receipt['active_model']=ready['metadata']
            receipt['worker']=worker_info(db)
            assert receipt['worker']['status']=='RUNNING'
            with db.transaction() as session:
                run=session.scalar(select(ModelTrainingRun).order_by(ModelTrainingRun.created_at.desc()).limit(1))
                assert run and run.status=='SUCCEEDED' and run.model_version==ready['metadata']['model_version']
                receipt['training_run']=serialize_training_run(run)
                store=ObjectStore(db.settings.data_dir/'objects')
                store.path(run.training_sha256,verify=True);store.path(run.manifest_sha256,verify=True)
                receipt['retained_training_files_verified']=True
                receipt['schema_version']=session.scalar(text('SELECT max(version) FROM schema_migrations'))
            counts=Counter();flagged=0;max_error=0.;max_bytes=0;last_id='';started=time.perf_counter()
            while True:
                with db.transaction() as session:
                    batch=session.execute(select(Movement.id,MovementVersion.payload).join(MovementVersion,
                        (MovementVersion.movement_id==Movement.id)&(MovementVersion.version_no==Movement.current_version))
                        .where(Movement.id>last_id).order_by(Movement.id).limit(500)).all()
                if not batch:break
                for result in score_batch([row.payload for row in batch],db.settings.model_dir):
                    counts[result['status']]+=1
                    if result['status']=='SCORED':
                        assert result['model_version']==ready['metadata']['model_version']
                        assert result['explanation']['status']=='AVAILABLE'
                        flagged+=int(result['flagged'])
                        max_error=max(max_error,abs(result['explanation']['reconstruction_error']))
                        max_bytes=max(max_bytes,len(json.dumps(compact_explanation(result['explanation'])).encode()))
                last_id=batch[-1].id
            assert max_error<1e-12 and max_bytes<=1024
            receipt['active_inference_read_only']={'counts':dict(counts),'flagged':flagged,'elapsed_seconds':time.perf_counter()-started,
                'batch_limit':500,'max_reconstruction_error':max_error,'max_compact_explanation_bytes':max_bytes,
                'persisted':False,'effectiveness_measured':False}
            with db.transaction() as session:
                old=session.scalar(select(Assessment).where(Assessment.kind=='MODEL',Assessment.status=='SCORED',
                    Assessment.model_version!=ready['metadata']['model_version']).order_by(Assessment.id).limit(1))
                assert old,'Expected retained historical model evidence'
                historical=explain_recorded(old.features,db.settings.model_dir,old.model_version,old.score,old.policy_version)
                assert historical['status']=='AVAILABLE'
                receipt['historical_model_verified']={'model_version':old.model_version,'assessment_id':old.id,
                    'recorded_score':old.score,'reconstructed_score':historical['reconstructed_score']}
            assert business_snapshot(db)==receipt['business'],'Read-only inference changed business records'
        receipt['status']='PASS'
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(receipt,indent=2,default=str)+'\n')
        print(json.dumps({'status':'PASS','receipt':str(args.output),'business':receipt['business'],
            'active_model':receipt.get('active_model',{}).get('model_version'),
            'inference':receipt.get('active_inference_read_only')},indent=2))
    finally:db.close()


if __name__=='__main__':main()
