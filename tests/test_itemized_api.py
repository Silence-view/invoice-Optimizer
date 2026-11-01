"""Itemized presentation stays bounded, frozen and subject to personal review."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select,func

from invoice_optimizer.api import create_app
from invoice_optimizer.billing import create_invoice
from invoice_optimizer.ingestion import ingest_file
from invoice_optimizer.models import Invoice,InvoiceMember,Audit,Outbox
from invoice_optimizer.worker import run_job
from test_workflow import prepared,rows,write_csv
from test_operators_api import bootstrap,headers


@pytest.fixture
def many_movements(prepared,tmp_path):
    records=rows(amounts=('10.10','20.20')+tuple('1.25' for _ in range(33)))
    for side in ('A','B'):
        ingest_file(prepared,write_csv(tmp_path/f'full-{side}.csv',records),side,'2026-09',35,True)
    while run_job(prepared):pass
    return prepared


def render_draft(db,line_format):
    result=create_invoice(db,'C1','2026-09','api-close-'+line_format,'fixture',line_format=line_format)
    while run_job(db,kinds=('RENDER',)):pass
    with db.transaction() as session:return session.get(Invoice,result['id'])


def test_itemized_package_and_paging_return_complete_bounded_frozen_rows(many_movements):
    db=many_movements;inv=render_draft(db,'ITEMIZED')
    with db.transaction() as session:
        before=deepcopy(session.get(Invoice,inv.id).document)
        audits=session.scalar(select(func.count()).select_from(Audit))
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        package=client.get(f'/api/invoices/{inv.id}/package').json()
        assert package['line_format']=='ITEMIZED' and package['line_count']==35
        assert package['movement_vat_policy']=='line_half_up_reconciled_v1'
        assert package['document']['movement_vat']==before['movement_vat']
        assert 'movement_detail' not in package['document']
        page=package['movement_details']
        assert page['total']==35 and page['limit']==25 and len(page['items'])==25 and page['has_more']
        second=client.get(f'/api/invoices/{inv.id}/movements?limit=25&offset=25').json()
        assert len(second['items'])==10 and not second['has_more']
        assert page['items']+second['items']==before['movement_detail']
        all_items=page['items']+second['items']
        for item in all_items:
            net=Decimal(item['amount']);vat=Decimal(item['vat_amount'])
            assert Decimal(item['vat_rate'])==Decimal('.20')
            assert vat==(net*Decimal(item['vat_rate'])).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
            assert Decimal(item['gross_amount'])==net+vat
        assert sum(Decimal(i['vat_amount']) for i in all_items)+Decimal(before['movement_vat']['rounding_adjustment'])==Decimal(before['vat_total'])
        listing=client.get('/api/invoices').json()
        assert listing[0]['line_format']=='ITEMIZED' and listing[0]['line_count']==35
        assert listing[0]['movement_vat_policy']==package['movement_vat_policy']
        assert all(key not in listing[0] for key in ('document','package','movement_detail'))
        assert client.get(f'/api/invoices/{inv.id}/movements?limit=101').status_code==422
        assert client.get(f'/api/invoices/{inv.id}/movements?offset=-1').status_code==422
        assert client.get('/api/invoices/missing/movements').status_code==404
    with db.transaction() as session:
        assert session.get(Invoice,inv.id).document==before
        assert session.scalar(select(func.count()).select_from(Audit))==audits


def test_personal_format_replacement_retains_old_pdf_and_never_approves(prepared):
    db=prepared;old=render_draft(db,'SUMMARY')
    body={'line_format':'ITEMIZED','fingerprint':old.fingerprint,'request_id':'format-http'}
    with TestClient(create_app(db.settings,db)) as client:
        shared=client.post('/api/login',json={'token':db.settings.api_token}).json()
        assert client.post(f'/api/invoices/{old.id}/format',json=body,headers=headers(shared)).status_code==403
        personal=bootstrap(client,db)
        assert client.post(f'/api/invoices/{old.id}/format',json=body).status_code==403
        response=client.post(f'/api/invoices/{old.id}/format',json=body,headers=headers(personal))
        assert response.status_code==200,response.text
        new=response.json();assert new['id']!=old.id and new['old_invoice_id']==old.id
        assert new['status']=='BUILDING' and new['line_format']=='ITEMIZED'
        assert client.post(f'/api/invoices/{old.id}/format',json=body,headers=headers(personal)).json()==new
        pending=client.get(f'/api/invoices/{new["id"]}/package').json()
        assert pending['status']=='BUILDING' and pending['pdf_ready'] is False
        while run_job(db,kinds=('RENDER',)):pass
        result=client.get(f'/api/invoices/{new["id"]}/package').json()
        assert result['status']=='DRAFT' and result['approved_by'] is None and result['pdf_ready'] is True
        assert result['document']['net_total']==old.document['net_total']
        assert result['document']['vat_total']==old.document['vat_total']
        assert result['document']['gross_total']==old.document['gross_total']
        assert len(result['movement_details']['items'])==2
    with db.transaction() as session:
        retained=session.get(Invoice,old.id)
        assert retained.status=='STALE' and retained.document==old.document
        assert retained.pdf_sha256==old.pdf_sha256 and retained.package==old.package and retained.fingerprint==old.fingerprint
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind=='INVOICE'))==0
        events=list(session.scalars(select(Audit).where(Audit.actor==personal['actor'])))
        assert any(e.entity_id==old.id for e in events)


def test_legacy_summary_get_reports_membership_count_without_claiming_detail(prepared):
    db=prepared;old=render_draft(db,'SUMMARY')
    with db.transaction() as session:
        inv=session.get(Invoice,old.id)
        inv.document={key:value for key,value in inv.document.items() if key not in ('line_format','movement_count','movement_detail')}
        # A pre-migration-8 row: derived list columns absent until backfilled.
        inv.line_format=inv.movement_count=inv.movement_vat_policy=None
    db.init()
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        result=client.get(f'/api/invoices/{old.id}/package').json()
        assert result['line_format']=='SUMMARY' and result['line_count']==2
        assert result['movement_vat_policy'] is None
        assert result['movement_details']=={'items':[],'total':0,'limit':25,'offset':0,'has_more':False}
        # Polling old revisions never scans all their memberships; an exact
        # legacy count is computed only when that package is opened.
        assert client.get('/api/invoices').json()[0]['line_count'] is None
        assert client.get('/api/invoices').json()[0]['movement_vat_policy'] is None


def test_legacy_itemized_vat_upgrade_requires_personal_review_and_preserves_totals(prepared):
    db=prepared
    result=create_invoice(db,'C1','2026-09','legacy-net-only-close','fixture',line_format='ITEMIZED')
    # Freeze the historical net-only schema before its first render, so its
    # document, PDF and package fingerprint remain internally consistent.
    with db.transaction() as session:
        inv=session.get(Invoice,result['id']);document=deepcopy(inv.document)
        document.pop('movement_vat')
        for row in document['movement_detail']:
            for key in ('vat_rate','vat_amount','gross_amount'):row.pop(key)
        inv.document=document
    while run_job(db,kinds=('RENDER',)):pass
    with db.transaction() as session:old=session.get(Invoice,result['id'])
    body={'line_format':'ITEMIZED','fingerprint':old.fingerprint,'request_id':'vat-upgrade-http'}
    with TestClient(create_app(db.settings,db)) as client:
        shared=client.post('/api/login',json={'token':db.settings.api_token}).json()
        historical=client.get(f'/api/invoices/{old.id}/package').json()
        assert historical['movement_vat_policy'] is None
        assert all('vat_amount' not in i for i in historical['movement_details']['items'])
        assert client.post(f'/api/invoices/{old.id}/format',json=body,headers=headers(shared)).status_code==403
        personal=bootstrap(client,db)
        response=client.post(f'/api/invoices/{old.id}/format',json=body,headers=headers(personal))
        assert response.status_code==200,response.text
        new=response.json()
        assert client.post(f'/api/invoices/{old.id}/format',json=body,headers=headers(personal)).json()==new
        while run_job(db,kinds=('RENDER',)):pass
        current=client.get(f'/api/invoices/{new["id"]}/package').json()
        assert current['status']=='DRAFT' and current['approved_by'] is None
        assert current['movement_vat_policy']=='line_half_up_reconciled_v1'
        assert current['snapshot_sha256']==historical['snapshot_sha256']
        for name in ('net_total','vat_total','gross_total'):
            assert current['document'][name]==historical['document'][name]
        assert all('vat_amount' in i and 'gross_amount' in i for i in current['movement_details']['items'])
        duplicate=client.post(f'/api/invoices/{new["id"]}/format',json={
            'line_format':'ITEMIZED','fingerprint':current['fingerprint'],'request_id':'vat-upgrade-again'},headers=headers(personal))
        assert duplicate.status_code==409
        assert client.get(f'/api/invoices/{old.id}/package').json()['pdf_sha256']==historical['pdf_sha256']
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind=='INVOICE'))==0
        assert session.get(Invoice,old.id).status=='STALE'


def test_invalid_format_does_not_create_invoice(prepared):
    db=prepared
    with TestClient(create_app(db.settings,db)) as client:
        personal=bootstrap(client,db)
        response=client.post('/api/close',json={'customer_id':'C1','period':'2026-09','request_id':'invalid-format','line_format':'TRUNCATED'},headers=headers(personal))
        assert response.status_code==422
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Invoice))==0
