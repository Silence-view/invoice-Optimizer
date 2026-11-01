"""Pre-release hardening: secrets bootstrap, admin-only Gmail, cookies, headers, setup token."""
from dataclasses import replace
from pathlib import Path
import os, shutil, subprocess, sys
from urllib.parse import unquote
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from invoice_optimizer.api import create_app
from invoice_optimizer.config import Settings
from invoice_optimizer.models import Audit

ROOT=Path(__file__).resolve().parents[1]
SETUP='dedicated-setup-token-'+'s'*40
ADMIN={'username':'release-admin','display_name':'Release Admin','password':'release-admin-password-2026'}
REVIEWER={'username':'release-reviewer','display_name':'Release Reviewer','password':'release-reviewer-password-2026','role':'reviewer'}


class FakeGmail:
    def __init__(self,settings):self.settings=settings;self.calls=[];self.connected=True
    def status(self):return {'client_configured':True,'connected':self.connected,'email':'owner@example.test',
                             'scopes':[],'callback_url':self.settings.public_base_url+'/api/mail/gmail/callback','reason':None}
    def configure(self,data):self.calls.append('configure');return self.status()
    def begin(self,binding,expected_email=None):
        self.calls.append('begin');return {'authorization_url':'https://accounts.google.com/o/oauth2/v2/auth?state=s','state':'s'}
    def disconnect(self):self.calls.append('disconnect');self.connected=False;return self.status()


@pytest.fixture
def app_for(db,monkeypatch):
    import invoice_optimizer.gmail_routes as routes
    fakes=[]
    def make(**changes):
        settings=replace(db.settings,gmail_expected_email='owner@example.test',**changes)
        monkeypatch.setattr(routes,'GmailConnection',lambda s:fakes.append(FakeGmail(s)) or fakes[-1])
        app=create_app(settings,db)
        return app,settings,fakes[-1]
    return make


def bootstrap(client,token):
    return client.post('/api/operators/bootstrap',json={'setup_token':token,**ADMIN})


def test_bootstrap_dsn_uses_the_generated_password_and_env_is_owner_only(tmp_path):
    (tmp_path/'scripts').mkdir()
    shutil.copy(ROOT/'scripts/bootstrap.py',tmp_path/'scripts/bootstrap.py')
    subprocess.run([sys.executable,'-I',str(tmp_path/'scripts/bootstrap.py')],check=True,capture_output=True)
    env_file=tmp_path/'.env'
    assert env_file.stat().st_mode&0o777==0o600
    values=dict(line.split('=',1) for line in env_file.read_text().splitlines() if '=' in line)
    url=make_url(values['DATABASE_URL'])
    assert unquote(url.password)==values['POSTGRES_PASSWORD'] and len(values['POSTGRES_PASSWORD'])>=24
    assert values['POSTGRES_PASSWORD']!='invoice'
    assert len(values['INVOICE_SETUP_TOKEN'])>=32 and values['INVOICE_SETUP_TOKEN']!=values['INVOICE_API_TOKEN']
    before=env_file.read_text()
    subprocess.run([sys.executable,'-I',str(tmp_path/'scripts/bootstrap.py')],check=True,capture_output=True)
    assert env_file.read_text()==before
    assert make_url(Settings.database_url).password is None


def test_local_postgres_refuses_to_initdb_without_a_generated_password(tmp_path):
    (tmp_path/'scripts').mkdir();(tmp_path/'bin').mkdir()
    shutil.copy(ROOT/'scripts/local_postgres.sh',tmp_path/'scripts/local_postgres.sh')
    for tool in ('initdb','pg_ctl','psql','createdb'):
        stub=tmp_path/'bin'/tool;stub.write_text('#!/bin/sh\necho "'+tool+' $*" >> "$(dirname "$0")/calls"\nexit 1\n');stub.chmod(0o755)
    env={**os.environ,'INVOICE_PG_BIN':str(tmp_path/'bin')};env.pop('INVOICE_DB_PASSWORD',None)
    result=subprocess.run(['bash',str(tmp_path/'scripts/local_postgres.sh')],env=env,capture_output=True,text=True)
    assert result.returncode==1 and 'bootstrap.py' in result.stderr and not (tmp_path/'bin/calls').exists()
    (tmp_path/'.env').write_text('POSTGRES_PASSWORD=generated-password-value\n')
    subprocess.run(['bash',str(tmp_path/'scripts/local_postgres.sh')],env=env,capture_output=True,text=True)
    assert (tmp_path/'bin/calls').read_text().startswith('initdb ')


def test_gmail_changes_require_an_administrator(app_for):
    app,settings,fake=app_for()
    with TestClient(app,base_url=settings.public_base_url) as admin,TestClient(app,base_url=settings.public_base_url) as reviewer:
        admin_headers={'X-CSRF-Token':bootstrap(admin,settings.api_token).json()['csrf_token']}
        assert admin.post('/api/operators',headers=admin_headers,json=REVIEWER).status_code==200
        login=reviewer.post('/api/login',json={'username':REVIEWER['username'],'password':REVIEWER['password']})
        headers={'X-CSRF-Token':login.json()['csrf_token']}
        assert reviewer.get('/api/mail/gmail').status_code==200
        assert reviewer.post('/api/mail/gmail/config',headers=headers,files={'file':('client.json',b'{}')}).status_code==403
        assert reviewer.post('/api/mail/gmail/connect',headers=headers,json={}).status_code==403
        assert reviewer.post('/api/mail/gmail/disconnect',headers=headers,json={}).status_code==403
        assert not fake.calls
        assert admin.post('/api/mail/gmail/config',headers=admin_headers,files={'file':('client.json',b'{}')}).status_code==200
        assert admin.post('/api/mail/gmail/connect',headers=admin_headers,json={}).status_code==200
        assert admin.post('/api/mail/gmail/disconnect',headers=admin_headers,json={}).status_code==200
        assert fake.calls==['configure','begin','disconnect']


def test_shared_session_cannot_see_account_addresses_or_workspace_origin(app_for):
    app,settings,fake=app_for()
    with TestClient(app,base_url=settings.public_base_url) as shared,TestClient(app,base_url=settings.public_base_url) as admin:
        assert shared.post('/api/login',json={'token':settings.api_token}).status_code==200
        status=shared.get('/api/mail/gmail').json()
        assert status['connected'] and status['automatic_delivery']=='simulated'
        assert status['expected_email'] is status['workspace_url'] is status['email'] is status['callback_url'] is None
        bearer=shared.get('/api/mail/gmail',headers={'Authorization':'Bearer '+settings.api_token}).json()
        assert bearer['expected_email'] is None and 'owner@example.test' not in str(bearer)
        bootstrap(admin,settings.api_token)
        full=admin.get('/api/mail/gmail').json()
        assert full['expected_email']==full['email']=='owner@example.test' and full['workspace_url']==settings.public_base_url


def cookie_named(response,name):
    return next(v for k,v in response.headers.multi_items() if k=='set-cookie' and v.startswith(name+'='))


def test_https_origin_means_secure_cookies_everywhere_and_hsts(app_for):
    app,settings,fake=app_for(public_base_url='https://invoice.example.test',session_secure=False)
    assert settings.cookie_secure
    with TestClient(app,base_url=settings.public_base_url) as client:
        login=bootstrap(client,settings.api_token)
        assert 'Secure' in cookie_named(login,'invoice_session')
        assert login.headers['strict-transport-security']=='max-age=31536000; includeSubDomains'
        begin=client.post('/api/mail/gmail/connect',headers={'X-CSRF-Token':login.json()['csrf_token']},json={})
        assert 'Secure' in cookie_named(begin,'invoice_gmail_oauth')


def test_loopback_http_has_no_hsts_and_cookies_follow_the_explicit_flag(app_for):
    app,settings,fake=app_for()
    with TestClient(app,base_url=settings.public_base_url) as client:
        login=bootstrap(client,settings.api_token)
        assert 'Secure' not in cookie_named(login,'invoice_session')
        assert 'strict-transport-security' not in login.headers
    assert replace(settings,session_secure=True).cookie_secure


def test_live_mode_refuses_plain_http(tmp_path):
    with pytest.raises(ValueError,match='HTTPS'):Settings(data_dir=tmp_path,synthetic=False).prepare()
    with pytest.raises(ValueError,match='synthetic source'):
        Settings(data_dir=tmp_path,synthetic=False,public_base_url='https://invoice.example.test').prepare()


def test_security_headers_and_static_revalidation(app_for):
    app,settings,fake=app_for()
    with TestClient(app,base_url=settings.public_base_url) as client:
        page=client.get('/')
        assert page.headers['cache-control']=='no-store' and page.headers['referrer-policy']=='same-origin'
        assert page.headers['cross-origin-opener-policy']=='same-origin'
        assert 'camera=()' in page.headers['permissions-policy'] and 'geolocation=()' in page.headers['permissions-policy']
        assert "script-src 'self';" in page.headers['content-security-policy']
        asset=client.get('/static/app.css')
        assert asset.status_code==200 and asset.headers['cache-control']=='no-cache' and asset.headers['etag']
        again=client.get('/static/app.css',headers={'If-None-Match':asset.headers['etag']})
        assert again.status_code==304 and again.headers['cache-control']=='no-cache'


def test_health_is_minimal(app_for):
    app,settings,fake=app_for()
    with TestClient(app,base_url=settings.public_base_url) as client:
        assert client.get('/health').json()=={'status':'ok'}


def test_dedicated_setup_token_replaces_the_shared_token_once(app_for,db):
    app,settings,fake=app_for(setup_token=SETUP)
    with TestClient(app,base_url=settings.public_base_url) as client:
        assert bootstrap(client,settings.api_token).status_code==401
        assert client.get('/api/auth/status').json()['setup_required']
        assert bootstrap(client,SETUP).status_code==200
        assert bootstrap(client,SETUP).status_code==409
        client.cookies.clear()
        assert client.post('/api/login',json={'token':SETUP}).status_code==401
        assert client.get('/api/session',headers={'Authorization':'Bearer '+SETUP}).status_code==401
    with db.transaction() as s:
        details=s.scalar(select(Audit.details).where(Audit.action=='operator_signed_in'))
        assert details['initial_setup'] and details['setup_secret']=='dedicated'


def test_setup_token_falls_back_to_shared_token_only_when_unset(app_for,db):
    app,settings,fake=app_for()
    with TestClient(app,base_url=settings.public_base_url) as client:
        assert bootstrap(client,settings.api_token).status_code==200
    with db.transaction() as s:
        assert s.scalar(select(Audit.details).where(Audit.action=='operator_signed_in'))['setup_secret']=='shared_token_fallback'


@pytest.mark.parametrize('token',['too-short',None])
def test_setup_token_must_be_long_and_distinct(tmp_path,token):
    api_token='shared-api-token-'+'a'*40
    with pytest.raises(ValueError,match='INVOICE_SETUP_TOKEN'):
        Settings(data_dir=tmp_path,api_token=api_token,setup_token=token or api_token).prepare()


def test_ignore_files_use_generic_credential_patterns():
    for name in ('.gitignore','.dockerignore'):
        rules=(ROOT/name).read_text().splitlines()
        assert {'.env','*.pem','*service-account*.json','client_secret*.json','/*.json'}<=set(rules)
        assert not any(rule.startswith('/') and rule.endswith('.json') and rule!='/*.json' for rule in rules)
    assert '!.env.example' in (ROOT/'.gitignore').read_text().splitlines()
