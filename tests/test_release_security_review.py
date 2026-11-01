"""Shared-token redaction of Gmail account data must hold on every read path, not only /api/mail/gmail."""
from fastapi.testclient import TestClient
from invoice_optimizer.db import audit
from test_release_security import app_for, bootstrap  # noqa: F401 (fixture)


def test_shared_token_cannot_read_gmail_addresses_from_audit_or_dashboard(app_for,db):
    app,settings,fake=app_for()
    with TestClient(app,base_url=settings.public_base_url) as admin,TestClient(app,base_url=settings.public_base_url) as shared:
        headers={'X-CSRF-Token':bootstrap(admin,settings.api_token).json()['csrf_token']}
        assert admin.post('/api/mail/gmail/config',headers=headers,files={'file':('client.json',b'{}')}).status_code==200
        assert admin.post('/api/mail/gmail/connect',headers=headers,json={}).status_code==200
        with db.transaction() as s:audit(s,'user:x','gmail_connected','owner@example.test',{'scopes':[],'delivery_activated':False,'flow_id':'f'})
        full=str(admin.get('/api/audit').json())
        assert 'owner@example.test' in full and settings.public_base_url in full
        assert shared.post('/api/login',json={'token':settings.api_token}).status_code==200
        for response in (shared.get('/api/audit'),shared.get('/api/dashboard'),
                         shared.get('/api/audit',headers={'Authorization':'Bearer '+settings.api_token})):
            assert response.status_code==200
            body=str(response.json())
            assert 'owner@example.test' not in body and settings.public_base_url not in body
        events=[e for e in shared.get('/api/audit').json() if e['action'].startswith('gmail_')]
        assert {e['action'] for e in events}=={'gmail_client_configured','gmail_authorization_started','gmail_connected'}
        assert all(e['entity_id']=='gmail' for e in events)
        assert next(e for e in events if e['action']=='gmail_authorization_started')['details']['flow_id']
        # Append-only events are untouched; only the shared view is redacted.
        assert 'owner@example.test' in str(admin.get('/api/audit').json())
