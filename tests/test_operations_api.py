"""Customer isolation and exact retained-file lineage on disposable PostgreSQL."""
import csv
import io
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select, delete, event
from invoice_optimizer.api import create_app
from invoice_optimizer.billing import create_invoice
from invoice_optimizer.ingestion import ingest_file
from invoice_optimizer.models import (Customer, ReviewCase, Movement, MovementVersion,
    Batch, Quarantine, Outbox, Invoice, InvoiceSourceFile, InvoiceSourceManifest)
from test_workflow import prepared, rows, write_csv
from test_bulk_review import case_factory, REASON


def client_for(db):
    return TestClient(create_app(db.settings, db), headers={'Authorization': 'Bearer '+db.settings.api_token})


def add_cases(db):
    with db.transaction() as s:
        s.add(Customer(id='C2', legal_name='Second Client', address='Test', email='c2@example.test', alert_email='ops@example.test'))
        s.flush()
        m=s.scalar(select(Movement).order_by(Movement.id))
        v=s.scalar(select(MovementVersion).where(MovementVersion.movement_id==m.id,MovementVersion.version_no==m.current_version))
        batch=s.scalar(select(Batch).where(Batch.source=='A'))
        s.add(Quarantine(id='q2',batch_id=batch.id,period='2026-09',row_number=100,validated_customer_id='C2',reason='Bad amount',payload={}))
        s.add_all([
            ReviewCase(id='case-c1',dedup_key='manual-c1',movement_id=m.id,version_id=v.id,customer_id='C1',period='2026-09',reason='MODEL_RISK',status='RESOLVED',owner='ops@example.test'),
            ReviewCase(id='case-c2',dedup_key='quarantine:q2',customer_id='C2',period='2026-09',reason='QUARANTINE',owner='ops@example.test')])
        s.flush()
        for cid in ('c1','c2'):
            s.add(Outbox(id='out-'+cid,dedup_key='out-'+cid,kind='ALERT',case_id='case-'+cid,payload={'to':['ops@example.test']}))


def test_customer_filters_files_and_filtered_exports(prepared):
    add_cases(prepared)
    with client_for(prepared) as client:
        for cid in ('C1','C2'):
            cases=client.get('/api/cases',params={'customer_id':cid}).json()
            assert len(cases)==1 and cases[0]['customer_id']==cid
            assert cases[0]['external_movement_id'] in ('m0','m1') if cid=='C1' else cases[0]['external_movement_id'] is None
            assert cases[0]['source_files'] and not cases[0]['release_eligible']
            outbox=client.get('/api/outbox',params={'customer_id':cid}).json()
            assert len(outbox)==1 and outbox[0]['customer_id']==cid and outbox[0]['source_files']
        response=client.get('/api/exports/cases.csv?customer_id=C1&status=')
        exported=list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
        assert len(exported)==1 and exported[0]['customer_id']=='C1'
        assert 'A.csv' in exported[0]['source_files'] and 'B.csv' in exported[0]['source_files']
        assert client.get('/api/cases?customer_id=unknown').json()==[]
        assert client.get('/api/outbox?customer_id=unknown').json()==[]
        assert len(client.get('/api/batches?customer_id=C1').json())==2
        assert len(client.get('/api/batches?customer_id=C2').json())==1  # trusted quarantine attribution
        assert client.get('/api/batches?customer_id=unknown').json()==[]
        detail=client.get('/api/cases/case-c1').json()
        assert {r['original_name'] for r in detail['source_files']}=={'A.csv','B.csv'}
        assert all(o['source_file']['id']==o['batch_id'] for o in detail['observations'])
        assert len(client.get('/api/cases/case-c2').json()['source_files'])==1


def test_source_download_auth_hash_and_detail(prepared):
    with prepared.transaction() as s:batch=s.scalar(select(Batch).where(Batch.source=='A'))
    with TestClient(create_app(prepared.settings,prepared)) as anonymous:
        assert anonymous.get(f'/api/batches/{batch.id}/file').status_code==401
    with client_for(prepared) as client:
        info=client.get(f'/api/batches/{batch.id}').json()
        response=client.get(info['download_url'])
        assert response.status_code==200
        assert 'attachment;' in response.headers['content-disposition']
        import hashlib
        assert hashlib.sha256(response.content).hexdigest()==batch.sha256
        assert client.get('/api/batches/nonexistent/file').status_code==404
        from invoice_optimizer.storage import ObjectStore
        path=ObjectStore(prepared.settings.data_dir/'objects').path(batch.object_key)
        path.chmod(0o600);path.write_bytes(b'changed')
        assert client.get(info['download_url']).status_code==422


def test_repeated_delivery_is_customer_visible_but_not_original_provenance(prepared,tmp_path):
    add_cases(prepared)
    repeated=write_csv(tmp_path/'repeat.csv',rows())
    # Different bytes with identical normalized business records create a new delivery.
    repeated.write_bytes(repeated.read_bytes()+b'\n')
    ingest_file(prepared,repeated,'A','2026-09',2,False)
    with client_for(prepared) as client:
        batches=client.get('/api/batches?customer_id=C1').json()
        assert {b['original_name'] for b in batches}=={'A.csv','B.csv','repeat.csv'}
        assert {r['original_name'] for r in client.get('/api/cases/case-c1').json()['source_files']}=={'A.csv','B.csv'}


def test_invoice_sources_frozen_cached_and_paginated(prepared,tmp_path):
    result=create_invoice(prepared,'C1','2026-09','source-close','reviewer')
    iid=result['id']
    with client_for(prepared) as client:
        listed=client.get('/api/invoices?customer_id=C1').json()
        assert len(listed)==1 and listed[0]['source_file_count']==2
        assert client.get('/api/invoices?customer_id=C2').json()==[]
        assert client.get('/api/exports/dispatch.csv?customer_id=C2').text.count('\n')==1
        first=client.get(f'/api/invoices/{iid}/sources?limit=1').json()
        second=client.get(f'/api/invoices/{iid}/sources?limit=1&offset=1').json()
        assert first['truncated'] and not second['truncated']
        assert first['source_files'][0]['id']!=second['source_files'][0]['id']
        before=client.get(f'/api/invoices/{iid}/sources').json()
        ingest_file(prepared,write_csv(tmp_path/'new-A.csv',rows(revision=2)),'A','2026-09',2,True)
        assert client.get(f'/api/invoices/{iid}/sources').json()==before
        statements=[]
        def record(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
        event.listen(prepared.engine,'before_cursor_execute',record)
        try:client.get('/api/invoices?customer_id=C1')
        finally:event.remove(prepared.engine,'before_cursor_execute',record)
        assert not any('invoice_members' in statement for statement in statements)


def test_old_invoice_lazy_cache_does_not_change_frozen_package(prepared):
    iid=create_invoice(prepared,'C1','2026-09','legacy-close','reviewer')['id']
    with prepared.transaction() as s:
        inv=s.get(Invoice,iid);before=(inv.snapshot_hash,inv.document,inv.package,inv.fingerprint,inv.status)
        s.execute(delete(InvoiceSourceFile).where(InvoiceSourceFile.invoice_id==iid))
        s.execute(delete(InvoiceSourceManifest).where(InvoiceSourceManifest.invoice_id==iid))
    with client_for(prepared) as client:
        assert client.get('/api/invoices').json()[0]['provenance_status']=='NOT_COMPUTED'
        assert client.get(f'/api/invoices/{iid}/sources').json()['source_file_count']==2
        assert client.get('/api/invoices').json()[0]['provenance_status']=='COMPLETE'
    with prepared.transaction() as s:
        inv=s.get(Invoice,iid)
        assert (inv.snapshot_hash,inv.document,inv.package,inv.fingerprint,inv.status)==before


def test_missing_json_reference_is_explicit_not_silently_complete(prepared):
    with prepared.transaction() as s:
        version=s.scalar(select(MovementVersion).order_by(MovementVersion.version_no.desc()))
        version.source_refs=[*version.source_refs,'missing-source-reference']
    iid=create_invoice(prepared,'C1','2026-09','missing-ref-close','reviewer')['id']
    with client_for(prepared) as client:
        value=client.get(f'/api/invoices/{iid}/sources').json()
        assert value['provenance_status']=='INCOMPLETE' and value['missing_reference_count']==1
        assert value['source_file_count']==2


def test_bulk_route_auth_bounds_and_exact_eligibility(db,case_factory):
    valid=case_factory('api-valid');case_factory('api-incomplete',provenance=False)
    with TestClient(create_app(db.settings,db)) as client:
        body={'items':[valid],'reason':REASON,'request_id':'api-bulk'}
        assert client.post('/api/cases/bulk-release',json=body).status_code==401
        logged=client.post('/api/operators/bootstrap',json={'setup_token':db.settings.api_token,
            'username':'review-admin','display_name':'Review Test Administrator','password':'review-test-password-2026'}).json()
        assert client.post('/api/cases/bulk-release',json=body).status_code==403
        headers={'X-CSRF-Token':logged['csrf_token'],'Origin':'http://testserver'}
        listed=client.get('/api/cases').json()
        assert {c['id']:c['release_eligible'] for c in listed}=={'case-api-valid':True,'case-api-incomplete':False}
        assert client.post('/api/cases/bulk-release',json={**body,'items':[]},headers=headers).status_code==422
        assert client.post('/api/cases/bulk-release',json={**body,'items':[valid]*101},headers=headers).status_code==422
        assert client.post('/api/cases/bulk-release',json={**body,'items':[{**valid,'expected_version':True}]},headers=headers).status_code==422
        result=client.post('/api/cases/bulk-release',json=body,headers=headers)
        assert result.status_code==200 and result.json()['counts']['released']==1
        assert client.post('/api/cases/bulk-release',json=body,headers=headers).json()==result.json()
        assert client.post('/api/cases/bulk-release',json={**body,'reason':'Changed review reason'},headers=headers).status_code==409
