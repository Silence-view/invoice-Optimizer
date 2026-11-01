"""Add per-movement VAT to synthetic drafts through the normal billing flow."""
import argparse
from datetime import datetime,timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import select,text
from invoice_optimizer.config import Settings
from invoice_optimizer.db import Database,canonical,digest
from invoice_optimizer.models import (Customer,Batch,BatchRecord,SourceRecord,Quarantine,
    Movement,MovementVersion,Assessment,ReviewCase,Outbox,DeliveryAttempt,DeliveryEvent,
    Allocation,Invoice,InvoiceMember,Audit)
from invoice_optimizer.storage import ObjectStore

ROOT=Path(__file__).resolve().parents[1]
BEFORE=ROOT/'output/verification/line-vat-business-before.json'
PREPARED=ROOT/'output/verification/line-vat-preparation.json'
OUTPUT=ROOT/'output/pdf/line-vat-flow-invoices'
ACTOR='cli:user-requested-line-vat-drafts'


def protected_hashes(session):
    result={}
    for cls in (Customer,Batch,BatchRecord,SourceRecord,Quarantine,Movement,
                MovementVersion,Assessment,ReviewCase,Outbox,DeliveryAttempt,DeliveryEvent,Allocation):
        table=cls.__table__;checksum=hashlib.sha256();count=0
        for row in session.execute(select(table).order_by(*table.primary_key.columns).execution_options(yield_per=512)):
            checksum.update(canonical(dict(row._mapping)).encode()+b'\n');count+=1
        result[table.name]={'rows':count,'sha256':checksum.hexdigest()}
    return result


def invoice_value(inv):
    return json.loads(canonical({c.name:getattr(inv,c.name) for c in Invoice.__table__.columns}))


def capture(db):
    if BEFORE.exists():raise ValueError('Baseline already exists; preserve it rather than overwrite it')
    with db.transaction() as session:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        current=list(session.scalars(select(Invoice).where(Invoice.status=='DRAFT').order_by(Invoice.customer_id)))
        assert {i.customer_id for i in current}=={'C0001','EXAMPLE-C001','EXAMPLE-C002','EXAMPLE-C003'}
        assert all(i.approved_by is None and i.approved_at is None and i.issued_at is None for i in current)
        result={'captured_at':datetime.now(timezone.utc).isoformat(),
                'protected':protected_hashes(session),'invoices':[invoice_value(i) for i in current]}
    BEFORE.write_text(json.dumps(result,indent=2)+'\n')
    return {'status':'CAPTURED','invoices':len(current)}


def prepare(db):
    from invoice_optimizer.billing import regenerate_invoice_format
    baseline=json.loads(BEFORE.read_text())
    with db.transaction() as session:assert protected_hashes(session)==baseline['protected']
    results=[]
    for old in baseline['invoices']:
        # This is a normal, audited, idempotent draft replacement; the ordinary
        # worker subsequently performs every TeX compilation.
        results.append(regenerate_invoice_format(db,old['id'],old['fingerprint'],'ITEMIZED',
                       'line-vat-demo-v1:'+old['id'],ACTOR))
    result={'status':'QUEUED_FOR_NORMAL_RENDER_WORKER','actor':ACTOR,'items':results,
            'approved':False,'email_requested':False}
    PREPARED.write_text(json.dumps(result,indent=2)+'\n')
    return result


def collect(db):
    baseline=json.loads(BEFORE.read_text());prepared=json.loads(PREPARED.read_text())
    store=ObjectStore(db.settings.data_dir/'objects');outputs=[]
    with db.transaction() as session:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        assert protected_hashes(session)==baseline['protected']
        for old_data,entry in zip(baseline['invoices'],prepared['items'],strict=True):
            old=session.get(Invoice,old_data['id']);new=session.get(Invoice,entry['id'])
            assert old.status=='STALE'
            assert invoice_value(old)=={**old_data,'status':'STALE'}
            assert new.status=='DRAFT',f'Normal worker still preparing {new.id}: {new.status}'
            assert new.approved_by is None and new.approved_at is None and new.issued_at is None
            assert (new.net_total,new.vat_total,new.gross_total,new.snapshot_hash)==(old.net_total,old.vat_total,old.gross_total,old.snapshot_hash)
            assert new.document['line_format']=='ITEMIZED'
            details=new.document['movement_detail']
            assert len(details)==new.document['movement_count']
            members=list(session.execute(select(InvoiceMember,Movement,MovementVersion).join(
                Movement,Movement.id==InvoiceMember.movement_id).join(MovementVersion,MovementVersion.id==InvoiceMember.version_id).where(InvoiceMember.invoice_id==new.id)))
            actual={d['movement_id']:d for d in details};assert len(actual)==len(members)==len(details)
            assert sum((Decimal(d['amount']) for d in details),Decimal('0'))==new.net_total
            tax=new.document['movement_vat']
            assert tax['policy']=='line_half_up_reconciled_v1' and Decimal(tax['rate'])==Decimal('.20')
            line_vat=Decimal('0.00');line_gross=Decimal('0.00')
            for detail in details:
                net=Decimal(detail['amount']);rate=Decimal(detail['vat_rate'])
                vat=Decimal(detail['vat_amount']);gross=Decimal(detail['gross_amount'])
                assert rate==Decimal('.20')
                assert vat==(net*rate).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
                assert gross==net+vat
                line_vat+=vat;line_gross+=gross
            adjustment=Decimal(tax['rounding_adjustment'])
            assert line_vat==Decimal(tax['line_vat_total'])
            assert line_gross==Decimal(tax['line_gross_total'])
            assert line_vat+adjustment==new.vat_total
            assert line_gross+adjustment==new.gross_total
            for member,movement,version in members:
                d=actual[movement.id]
                assert (d['version_id'],d['external_id'],d['amount'],d['currency'])==(version.id,movement.external_id,str(member.amount),version.currency)
                for key in ('description','event_at','supplier_id'):assert d[key]==version.payload[key]
            assert digest(new.package)==new.fingerprint
            assert new.package['pdf_sha256']==new.pdf_sha256==new.pdf_key
            original_pdf=store.path(old.pdf_key,verify=True);pdf=store.path(new.pdf_key,verify=True)
            assert hashlib.sha256(original_pdf.read_bytes()).hexdigest()==old_data['pdf_sha256']
            events=[{'action':a.action,'actor':a.actor,'details':a.details,'at':a.created_at.isoformat()} for a in session.scalars(select(Audit).where(Audit.entity_id==new.id).order_by(Audit.id))]
            assert any(e['action']=='invoice_format_regenerated' and e['details']['old_invoice_id']==old.id for e in events)
            assert any(e['action']=='invoice_rendered' and e['actor']=='worker' for e in events)
            reader=PdfReader(pdf);extracted='\n'.join(p.extract_text() for p in reader.pages)
            normalized=''.join(extracted.split())
            for d in details:assert ''.join(d['external_id'].split()) in normalized
            assert 'DO NOT PAY' in extracted.upper()
            assert 'VAT' in extracted and 'Gross' in extracted
            assert 'Movement subtotal' in extracted
            if adjustment:assert 'Invoice VAT rounding adjustment' in extracted
            OUTPUT.mkdir(parents=True,exist_ok=True);target=OUTPUT/(new.customer_id.lower()+'.pdf')
            if target.exists():assert target.read_bytes()==pdf.read_bytes()
            else:target.write_bytes(pdf.read_bytes())
            outputs.append({'customer_id':new.customer_id,'old_invoice_id':old.id,'new_invoice_id':new.id,
                'number':new.number,'line_format':'ITEMIZED','movements':len(details),'pages':len(reader.pages),
                'net':str(new.net_total),'vat':str(new.vat_total),'gross':str(new.gross_total),
                'movement_vat':tax,'movement_vat_rows_verified':len(details),
                'pdf_path':str(target),'pdf_sha256':new.pdf_sha256,'snapshot_sha256':new.snapshot_hash,
                'fingerprint':new.fingerprint,'status':'DRAFT','audit':events})
    result={'status':'PASS','synthetic':True,'source_review_assessment_delivery_unchanged':True,
        'original_draft_bytes_preserved':True,'totals_and_membership_preserved':True,
        'ordinary_worker_rendered':True,'invoice_emails_created':0,'invoices':outputs}
    (OUTPUT/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=('capture','prepare','collect'))
    args=parser.parse_args();db=Database(Settings.from_env().prepare())
    try:result={'capture':capture,'prepare':prepare,'collect':collect}[args.command](db)
    finally:db.close()
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
