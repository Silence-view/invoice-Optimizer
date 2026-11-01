"""Financial/state-machine integration on PostgreSQL; model stub isolates gates."""
import csv,json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from invoice_optimizer.models import Invoice,Allocation,Outbox,Job,Movement,ReviewCase,DeliveryAttempt,RuntimeOption
from invoice_optimizer.ingestion import load_customers,ingest_file
from invoice_optimizer.worker import run_job
from invoice_optimizer.billing import create_invoice,approve_invoice,dispatch_invoice,revoke_invoice
from invoice_optimizer.db import Conflict,Blocked
from invoice_optimizer.api import create_app
from invoice_optimizer.delivery import send_one,simulation_event


def rows(amounts=('10.10','20.20'),revision=1):
    return [dict(record_id=f'r{i}',revision=revision,movement_id=f'm{i}',customer_id='C1',event_at='2026-09-10T12:00:00+00:00',period='2026-09',currency='GBP',amount=a,description='Test service',supplier_id='S1',expected_amount=a,customer_age_days='200',supplier_age_days='400',cross_border='0') for i,a in enumerate(amounts)]

def write_csv(path, records):
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)
    return path

@pytest.fixture
def prepared(db,tmp_path,monkeypatch):
    import invoice_optimizer.ml
    def scores(payloads,_):return [dict(status='SCORED',score=.05,flagged=False,model_version='unit-model',policy_version='unit-policy',feature_schema_version='unit-features',feature_values={'example':1.0}) for x in payloads]
    monkeypatch.setattr(invoice_optimizer.ml,'score_batch',scores)
    master=tmp_path/'customers.json';master.write_text(json.dumps([dict(id='C1',legal_name='Example Client Ltd',address='1 Example Road\nLondon\nZZ1 1ZZ',email='client@example.test',alert_email='review@example.test',vat_number='DEMO')]))
    load_customers(db,master)
    for side in ('A','B'):ingest_file(db,write_csv(tmp_path/f'{side}.csv',rows()),side,'2026-09',2,True)
    while run_job(db):pass
    return db


def draft(db):
    result=create_invoice(db,'C1','2026-09','close-1','reviewer')
    while run_job(db):pass
    with db.transaction() as s:
        inv=s.get(Invoice,result['id'])
        assert inv.status=='DRAFT'
        return inv


def test_full_frozen_approval_issue_and_distinct_delivery_evidence(prepared):
    db=prepared;inv=draft(db)
    assert (inv.net_total,inv.vat_total,inv.gross_total)==(Decimal('30.30'),Decimal('6.06'),Decimal('36.36'))
    assert inv.document['synthetic'] is True
    with pytest.raises(Blocked):dispatch_invoice(db,inv.id,'unapproved-send','reviewer')
    with pytest.raises(Conflict):approve_invoice(db,inv.id,'0'*64,'wrong-hash','reviewer')
    result=approve_invoice(db,inv.id,inv.fingerprint,'approve-1','reviewer')
    assert result==approve_invoice(db,inv.id,inv.fingerprint,'approve-1','reviewer')
    dispatch_invoice(db,inv.id,'send-1','reviewer')
    assert dispatch_invoice(db,inv.id,'send-1','reviewer')['status']=='ISSUED'
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Allocation))==2
        assert s.scalar(select(func.count()).select_from(Outbox))==1
    assert send_one(db)
    with db.transaction() as s:
        out=s.scalar(select(Outbox));attempt=s.scalar(select(DeliveryAttempt))
        assert out.status=='ACCEPTED';attempt_id=attempt.id
    event=simulation_event(db,attempt_id,'delivered','reviewer')
    assert simulation_event(db,attempt_id,'delivered','reviewer')['replayed']
    with pytest.raises(Blocked):revoke_invoice(db,inv.id,'already issued','revoke-after','reviewer')
    with pytest.raises((Conflict,Blocked)):create_invoice(db,'C1','2026-09','close-again','reviewer')


def test_no_auto_approval_or_send(prepared):
    inv=draft(prepared)
    with prepared.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind=='INVOICE'))==0
        assert s.get(Invoice,inv.id).approved_at is None


def test_source_update_invalidates_approved_snapshot(prepared,tmp_path):
    db=prepared;inv=draft(db);approve_invoice(db,inv.id,inv.fingerprint,'a','reviewer')
    updated=rows(amounts=('11.10','20.20'),revision=2)
    ingest_file(db,write_csv(tmp_path/'revised.csv',updated),'A','2026-09',2,True)
    with db.transaction() as s:assert s.get(Invoice,inv.id).status=='STALE'
    with pytest.raises(Blocked):dispatch_invoice(db,inv.id,'stale-send','reviewer')


def test_same_request_id_different_payload_rejected(prepared):
    inv=draft(prepared)
    approve_invoice(prepared,inv.id,inv.fingerprint,'same-key','reviewer')
    with pytest.raises(Conflict):approve_invoice(prepared,inv.id,'f'*64,'same-key','reviewer')


def test_concurrent_issue_has_one_allocation_and_one_outbox(prepared):
    inv=draft(prepared);approve_invoice(prepared,inv.id,inv.fingerprint,'approve','reviewer')
    def issue(i):
        try:return dispatch_invoice(prepared,inv.id,f'parallel-{i}','reviewer')
        except (Conflict,Blocked):return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(issue,range(2)))
    assert sum(x is not None for x in results)==1
    with prepared.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Allocation))==2
        assert s.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind=='INVOICE'))==1


def test_revocation_and_dispatch_serialize(prepared):
    inv=draft(prepared);approve_invoice(prepared,inv.id,inv.fingerprint,'approve','reviewer')
    def operation(kind):
        try:
            return dispatch_invoice(prepared,inv.id,'race-send','reviewer') if kind=='send' else revoke_invoice(prepared,inv.id,'Changed decision','race-revoke','reviewer')
        except (Conflict,Blocked):return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(operation,['send','revoke']))
    assert sum(x is not None for x in results)==1
    with prepared.transaction() as s:
        current=s.get(Invoice,inv.id)
        assert (current.status=='ISSUED') == (s.scalar(select(func.count()).select_from(Outbox))==1)


def test_latest_partial_delivery_cannot_certify_month(prepared,tmp_path):
    ingest_file(prepared,write_csv(tmp_path/'partial.csv',rows(revision=2)),'A','2026-09',2,False)
    while run_job(prepared):pass
    with pytest.raises(Blocked,match='manifest'):create_invoice(prepared,'C1','2026-09','partial-close','reviewer')


def test_latest_full_manifest_must_cover_all_known_records(prepared,tmp_path):
    ingest_file(prepared,write_csv(tmp_path/'partial-labelled-full.csv',rows(revision=2)[:1]),'A','2026-09',1,True)
    while run_job(prepared):pass
    with pytest.raises(Blocked,match='cover'):create_invoice(prepared,'C1','2026-09','missing-coverage','reviewer')


def test_api_auth_csrf_bounds_and_real_pdf(prepared):
    inv=draft(prepared)
    with TestClient(create_app(prepared.settings,prepared)) as client:
        assert client.get('/health').status_code==200
        assert client.get('/api/dashboard').status_code==401
        assert client.post('/api/login',json={'token':'wrong'}).status_code==401
        login=client.post('/api/operators/bootstrap',json={'setup_token':prepared.settings.api_token,
            'username':'workflow-reviewer','display_name':'Workflow Reviewer','password':'workflow-personal-test-password'})
        assert login.status_code==200
        csrf=login.json()['csrf_token']
        assert client.get('/api/session').json()['csrf_token']==csrf
        assert client.get('/api/cases?limit=100001').status_code==422
        summary=client.get('/api/dashboard').json();assert summary['counts']['movements']==2
        assert client.post('/api/invoices/'+inv.id+'/approve',json={'fingerprint':inv.fingerprint,'request_id':'browser-approve'}).status_code==403
        assert client.post('/api/invoices/'+inv.id+'/approve',headers={'X-CSRF-Token':csrf,'Origin':'https://attacker.example'},json={'fingerprint':inv.fingerprint,'request_id':'browser-approve'}).status_code==403
        package=client.get('/api/invoices/'+inv.id+'/package').json();assert package['fingerprint']==inv.fingerprint
        pdf=client.get('/api/invoices/'+inv.id+'/pdf');assert pdf.content.startswith(b'%PDF-')
        assert client.get('/api/exports/dispatch.csv').status_code==200
        assert client.post('/api/invoices/'+inv.id+'/approve',headers={'X-CSRF-Token':csrf},json={'fingerprint':inv.fingerprint,'request_id':'browser-approve'}).status_code==200
        assert client.post('/api/logout',headers={'X-CSRF-Token':csrf},json={}).status_code==200
        assert client.get('/api/dashboard').status_code==401
