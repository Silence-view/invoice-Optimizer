"""Read-only before/after business-row and object checks across a console upgrade."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text
from invoice_optimizer.config import Settings
from invoice_optimizer.db import Database, canonical
from invoice_optimizer.models import Base, Invoice, Batch
from invoice_optimizer.operators import Operator  # register mapped table
from invoice_optimizer.review_history import ReviewEvent  # register mapped table
from invoice_optimizer.model_ops import ModelTrainingRun  # register mapped table
from invoice_optimizer.storage import ObjectStore

ROOT=Path(__file__).resolve().parents[1]
BEFORE=ROOT/'output/verification/console-business-before.json'
AFTER=ROOT/'output/verification/console-business-after.json'


def snapshot(db):
    tables={};objects={};store=ObjectStore(db.settings.data_dir/'objects')
    with db.transaction() as session:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        for table in Base.metadata.sorted_tables:
            # Worker heartbeats/reports and their append-only audit activity are
            # expected to advance while the application remains available.
            if table.name in ('runtime_options','audit_events'):continue
            checksum=hashlib.sha256();count=0
            for row in session.execute(select(table).order_by(*table.primary_key.columns).execution_options(yield_per=512)):
                checksum.update(canonical(dict(row._mapping)).encode()+b'\n');count+=1
            tables[table.name]={'rows':count,'sha256':checksum.hexdigest()}
        for key in session.scalars(select(Batch.object_key)):
            path=store.path(key,verify=True);objects[key]=path.stat().st_size
        for inv in session.scalars(select(Invoice)):
            for key in (inv.snapshot_key,inv.pdf_key):
                if key:
                    path=store.path(key,verify=True);objects[key]=path.stat().st_size
        schema=session.scalar(text('SELECT max(version) FROM schema_migrations'))
    return {'at':datetime.now(timezone.utc).isoformat(),'tables':tables,
            'verified_objects':objects,'schema_version':schema}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['capture','verify']);args=parser.parse_args()
    db=Database(Settings.from_env().prepare())
    try:current=snapshot(db)
    finally:db.close()
    if args.action=='capture':
        if BEFORE.exists():raise RuntimeError('Preserve the existing baseline; refusing to overwrite it')
        BEFORE.write_text(json.dumps(current,indent=2)+'\n')
        print(json.dumps({'status':'CAPTURED','tables':len(current['tables']),'schema_version':current['schema_version']}))
    else:
        previous=json.loads(BEFORE.read_text())
        current['business_rows_unchanged']=current['tables']==previous['tables']
        current['retained_objects_unchanged']=current['verified_objects']==previous['verified_objects']
        current['status']='PASS' if current['business_rows_unchanged'] and current['retained_objects_unchanged'] else 'FAIL'
        AFTER.write_text(json.dumps(current,indent=2)+'\n')
        print(json.dumps({k:current[k] for k in ('status','business_rows_unchanged','retained_objects_unchanged','schema_version')}))
        assert current['status']=='PASS'


if __name__=='__main__':main()
