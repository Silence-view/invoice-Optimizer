"""Authenticated exact-PDF preview API; no invoice or audit mutations."""
from fastapi.testclient import TestClient
from sqlalchemy import func,select

from invoice_optimizer.api import create_app
from invoice_optimizer.models import Audit, Invoice
from test_workflow import prepared, draft


def test_preview_exact_pdf_pages_auth_hash_and_bounds(prepared):
    db=prepared;invoice=draft(db)
    with db.transaction() as session:
        original=session.get(Invoice,invoice.id)
        fingerprint=original.fingerprint
        before=session.scalar(select(func.count()).select_from(Audit))
    with TestClient(create_app(db.settings,db)) as client:
        base='/api/invoices/'+invoice.id+'/preview'
        assert client.get(base).status_code==401
        client.headers['Authorization']='Bearer '+db.settings.api_token
        metadata=client.get(base)
        assert metadata.status_code==200,metadata.text
        info=metadata.json()
        assert info['pdf_sha256']==invoice.pdf_sha256 and info['page_count']>=1
        assert info['read_only'] and info['source']=='EXACT_RETAINED_PDF'
        page=client.get(info['page_url'])
        assert page.status_code==200 and page.content.startswith(b'\x89PNG\r\n\x1a\n')
        assert page.headers['X-PDF-SHA256']==invoice.pdf_sha256
        assert page.headers['X-PDF-Page']=='1'
        assert client.get(base+'/pages/1',params={'sha256':'0'*64}).status_code==409
        assert client.get(base+'/pages/0',params={'sha256':invoice.pdf_sha256}).status_code==422
        assert client.get(base+f'/pages/{info["page_count"]+1}',params={'sha256':invoice.pdf_sha256}).status_code==422
        assert client.get(base+'/pages/1').status_code==422
        assert client.get('/api/invoices/absent/preview').status_code==404
    with db.transaction() as session:
        assert session.get(Invoice,invoice.id).fingerprint==fingerprint
        assert session.scalar(select(func.count()).select_from(Audit))==before


def test_preview_unavailable_and_busy_have_visible_retry_semantics(prepared,monkeypatch):
    import invoice_optimizer.api as api
    db=prepared;invoice=draft(db)
    def unavailable(*args):raise api.PreviewUnavailable('PDF page preview is unavailable')
    def busy(*args):raise api.PreviewBusy('PDF page preview is busy. Try again shortly.')
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        path='/api/invoices/'+invoice.id+'/preview'
        monkeypatch.setattr(api,'preview_info',unavailable)
        response=client.get(path)
        assert response.status_code==503 and response.json()['retryable'] is False
        monkeypatch.setattr(api,'preview_info',busy)
        response=client.get(path)
        assert response.status_code==429 and response.headers['Retry-After']=='2'
        assert response.json()['retryable'] is True
