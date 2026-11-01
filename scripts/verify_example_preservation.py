"""Read-only, streamed hashes of the business records outside the new examples."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from sqlalchemy import select, text
from invoice_optimizer.config import Settings
from invoice_optimizer.db import Database, canonical
from invoice_optimizer.models import (Assessment, Batch, BatchRecord, Customer,
    DeliveryAttempt, DeliveryEvent, Invoice, InvoiceMember, InvoiceSourceFile,
    InvoiceSourceManifest, Movement, MovementVersion, Outbox, Quarantine,
    ReviewCase, SourceRecord, Allocation)
from invoice_optimizer.seed_invoice_examples import CUSTOMER_SPECS, PERIOD


def snapshot(db):
    example_ids=[item[0] for item in CUSTOMER_SPECS]
    old_movements=select(Movement.id).where(Movement.customer_id.not_in(example_ids))
    old_versions=select(MovementVersion.id).where(MovementVersion.movement_id.in_(old_movements))
    old_invoices=select(Invoice.id).where(Invoice.customer_id.not_in(example_ids))
    old_batches=select(Batch.id).where(Batch.period!=PERIOD)
    scopes={
        Customer:Customer.id.not_in(example_ids),
        Batch:Batch.id.in_(old_batches), BatchRecord:BatchRecord.batch_id.in_(old_batches),
        SourceRecord:SourceRecord.customer_id.not_in(example_ids),
        Movement:Movement.id.in_(old_movements), MovementVersion:MovementVersion.id.in_(old_versions),
        Assessment:Assessment.version_id.in_(old_versions),
        ReviewCase:ReviewCase.customer_id.not_in(example_ids) | ReviewCase.customer_id.is_(None),
        Invoice:Invoice.id.in_(old_invoices), InvoiceMember:InvoiceMember.invoice_id.in_(old_invoices),
        InvoiceSourceFile:InvoiceSourceFile.invoice_id.in_(old_invoices),
        InvoiceSourceManifest:InvoiceSourceManifest.invoice_id.in_(old_invoices),
        Quarantine:None, Outbox:None, DeliveryAttempt:None, DeliveryEvent:None, Allocation:None,
    }
    result={}
    with db.transaction() as session:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        for cls,condition in scopes.items():
            table=cls.__table__
            query=select(table).order_by(*table.primary_key.columns)
            if condition is not None:query=query.where(condition)
            checksum=hashlib.sha256();count=0
            for row in session.execute(query.execution_options(yield_per=512)):
                checksum.update(canonical(dict(row._mapping)).encode()+b'\n');count+=1
            result[table.name]={'rows':count,'sha256':checksum.hexdigest()}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    db=Database(Settings.from_env().prepare())
    try:business=snapshot(db)
    finally:db.close()
    receipt={'at':datetime.now(timezone.utc).isoformat(),'business':business,
        'scope':'All columns of pre-existing business rows outside EXAMPLE-C001/2/3 and their August batches; all quarantine, outbox, delivery and allocation rows remain included.',
        'stream_batch_size':512,'status':'CAPTURED'}
    if args.before:
        baseline=json.loads(args.before.read_text())['business']
        changed=[table for table in business if business[table]!=baseline.get(table)]
        receipt.update(status='FAIL' if changed else 'PASS',changed_tables=changed,
                       existing_business_records_unchanged=not changed)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps({'status':receipt['status'],'receipt':str(args.output),
                      'rows':{table:item['rows'] for table,item in business.items()},
                      'changed_tables':receipt.get('changed_tables',[])},indent=2))
    if receipt['status']=='FAIL':raise SystemExit(1)


if __name__=='__main__':main()
