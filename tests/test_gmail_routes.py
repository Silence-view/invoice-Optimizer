"""HTTP/session integration for Gmail setup; Google itself is never contacted."""
from dataclasses import replace
import json
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select,func
from invoice_optimizer.api import create_app
from invoice_optimizer.models import Audit,DeliveryAttempt,Outbox


@pytest.fixture
def mail_client(db,monkeypatch):
    settings=replace(db.settings,gmail_expected_email='owner@example.test')
    import invoice_optimizer.gmail_routes as routes
    class FakeConnection:
        def __init__(self,settings):self.settings=settings;self.configured=False;self.connected=False;self.binding=None;self.calls=[]
        def status(self):return {'client_configured':self.configured,'connected':self.connected,'email':'owner@example.test' if self.connected else None,'scopes':[],'callback_url':settings.public_base_url+routes.CALLBACK,'reason':None}
        def configure(self,data):self.configured=True;self.calls.append(('configure',len(data)));return self.status()
        def begin(self,binding,expected_email=None):
            self.binding=binding;self.calls.append(('begin',expected_email))
            return {'authorization_url':'https://accounts.google.com/o/oauth2/v2/auth?state=fake-state','state':'fake-state'}
        def finish(self,code,state,binding):
            assert state=='fake-state' and binding==self.binding
            self.connected=True;self.calls.append(('finish',))
            return {'email':'owner@example.test','scopes':['openid','email','https://www.googleapis.com/auth/gmail.send'],'connected':True}
        def disconnect(self):self.connected=False;self.calls.append(('disconnect',));return self.status()
    fake=FakeConnection(settings)
    monkeypatch.setattr(routes,'GmailConnection',lambda _:fake)
    client=TestClient(create_app(settings,db),base_url=settings.public_base_url)
    yield client,fake,settings,db
    client.close()


def login(client,settings):
    if client.get('/api/auth/status').json()['setup_required']:
        result=client.post('/api/operators/bootstrap',json={'setup_token':settings.api_token,
            'username':'mail-admin','display_name':'Mail Test Administrator','password':'mail-test-password-2026'})
    else:
        result=client.post('/api/login',json={'username':'mail-admin','password':'mail-test-password-2026'})
    assert result.status_code==200
    return {'X-CSRF-Token':result.json()['csrf_token']}


def test_setup_requires_login_and_csrf_and_never_activates_provider(mail_client):
    client,fake,settings,db=mail_client
    assert client.get('/api/mail/gmail').status_code==401
    headers=login(client,settings)
    result=client.get('/api/mail/gmail').json()
    assert result['automatic_delivery']=='simulated'
    assert result['expected_email']=='owner@example.test' and not result['connection_activates_delivery']
    assert client.post('/api/mail/gmail/connect',json={}).status_code==403
    assert client.post('/api/mail/gmail/disconnect',json={}).status_code==403
    assert client.post('/api/mail/gmail/connect',headers=headers,json={'expected_email':'other@example.test'}).status_code==422
    assert not fake.calls


def test_config_upload_is_bounded_and_does_not_return_secrets(mail_client):
    client,fake,settings,db=mail_client;headers=login(client,settings)
    assert client.post('/api/mail/gmail/config',headers=headers,files={'file':('bad.txt',b'{}')}).status_code==422
    assert client.post('/api/mail/gmail/config',headers=headers,files={'file':('large.json',b'x'*32769)}).status_code==413
    secret='never-return-this-client-secret'
    result=client.post('/api/mail/gmail/config',headers=headers,files={'file':('client.json',json.dumps({'secret':secret}).encode())})
    assert result.status_code==200 and result.json()['client_configured']
    assert secret not in result.text
    with db.transaction() as s:assert secret not in str(list(s.scalars(select(Audit.details))))


def test_oauth_uses_separate_lax_binding_and_clean_callback_without_app_cookie(mail_client):
    client,fake,settings,db=mail_client;headers=login(client,settings)
    begin=client.post('/api/mail/gmail/connect',headers=headers,json={})
    assert begin.status_code==200
    initiating_actor=client.get('/api/session').json()['actor']
    cookie=begin.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=lax' in cookie and 'Max-Age=600' in cookie
    assert 'Path=/api/mail/gmail/callback' in cookie
    assert fake.binding and fake.binding not in begin.text
    for item in list(client.cookies.jar):
        if item.name=='invoice_session':client.cookies.jar.clear(item.domain,item.path,item.name)
    result=client.get('/api/mail/gmail/callback?state=fake-state&code=never-log-this-code',follow_redirects=False)
    assert result.status_code==303 and result.headers['location']==settings.public_base_url+'/#email'
    assert result.headers['referrer-policy']=='no-referrer'
    assert 'never-log-this-code' not in result.text and 'never-log-this-code' not in result.headers['location']
    assert fake.connected
    assert not any(item.name=='invoice_gmail_oauth' for item in client.cookies.jar)
    headers=login(client,settings)
    status=client.get('/api/mail/gmail').json()
    assert status['connected'] and status['automatic_delivery']=='simulated'
    assert status['connection_result']['ok']
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Outbox))==0
        assert s.scalar(select(func.count()).select_from(DeliveryAttempt))==0
        assert 'never-log-this-code' not in str(list(s.scalars(select(Audit.details))))
        started=s.scalar(select(Audit).where(Audit.action=='gmail_authorization_started'))
        connected=s.scalar(select(Audit).where(Audit.action=='gmail_connected'))
        assert started.actor==connected.actor==initiating_actor
        assert started.details['flow_id']==connected.details['flow_id']


def test_missing_browser_binding_never_calls_google_exchange(mail_client):
    client,fake,settings,db=mail_client
    result=client.get('/api/mail/gmail/callback?state=fake-state&code=private-code',follow_redirects=False)
    assert result.status_code==303 and not fake.calls
    login(client,settings)
    status=client.get('/api/mail/gmail').json()
    assert status['last_error'] and 'private-code' not in json.dumps(status)
    assert not status['connected']


def test_disconnect_is_local_authenticated_and_audited(mail_client):
    client,fake,settings,db=mail_client;headers=login(client,settings);fake.connected=True
    result=client.post('/api/mail/gmail/disconnect',headers=headers,json={})
    assert result.status_code==200 and not result.json()['connected']
    with db.transaction() as s:assert s.scalar(select(Audit.action).where(Audit.action=='gmail_disconnected_locally'))


def test_public_callback_origin_rejects_remote_http_and_unsafe_components(tmp_path):
    from invoice_optimizer.config import Settings
    for url in ('http://example.com','https://user:secret@example.com','https://example.com/path','https://example.com/?code=x','javascript:alert(1)'):
        with pytest.raises(ValueError):Settings(data_dir=tmp_path,public_base_url=url).prepare()


def test_callback_rejects_inactive_initiator_before_google_exchange(mail_client):
    from invoice_optimizer.operators import Operator
    client,fake,settings,db=mail_client;headers=login(client,settings)
    assert client.post('/api/mail/gmail/connect',headers=headers,json={}).status_code==200
    user_id=client.get('/api/session').json()['user_id']
    with db.transaction() as s:s.get(Operator,user_id).active=False
    client.get('/api/mail/gmail/callback?state=fake-state&code=private-code',follow_redirects=False)
    assert not fake.connected
    assert not any(call[0]=='finish' for call in fake.calls)
    with db.transaction() as s:assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='gmail_connected'))==0
