"""Named review principals, immutable attribution and bounded HTTP history."""
import csv
import io
import asyncio
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import select, func
from itsdangerous import URLSafeTimedSerializer

from invoice_optimizer.api import create_app
from invoice_optimizer.db import audit
from invoice_optimizer.models import Audit, ReviewCase
from invoice_optimizer.operators import Operator, verify_password
from invoice_optimizer.auth_secrets import session_signing_secret
from test_bulk_review import case_factory, REASON

PASSWORD = 'personal-test-password-2026'


def bootstrap(client, db, username='alice', display_name='Alice Reviewer'):
    response=client.post('/api/operators/bootstrap',json={
        'setup_token':db.settings.api_token,'username':username,
        'display_name':display_name,'password':PASSWORD})
    assert response.status_code==200,response.text
    return response.json()


def headers(session):
    return {'X-CSRF-Token':session['csrf_token'],'Origin':'http://testserver'}


def add_user(client, session, username='bob', role='reviewer'):
    response=client.post('/api/operators',headers=headers(session),json={
        'username':username,'display_name':username.title()+' Reviewer',
        'password':PASSWORD,'role':role})
    assert response.status_code==200,response.text
    return response.json()


def test_bootstrap_personal_credentials_and_public_metadata(db):
    with TestClient(create_app(db.settings,db)) as client:
        assert client.get('/api/auth/status').json()=={'setup_required':True}
        session=bootstrap(client,db)
        assert session['username']=='alice' and session['display_name']=='Alice Reviewer'
        assert session['actor']=='user:'+session['user_id']
        assert session['identity_type']=='operator' and session['role']=='admin' and session['can_review']
        assert client.get('/api/auth/status').json()=={'setup_required':False}
        assert client.get('/api/session').json()['actor']==session['actor']
        assert client.post('/api/operators/bootstrap',json={'setup_token':db.settings.api_token,
            'username':'other','display_name':'Other','password':PASSWORD}).status_code==409
        response=client.get('/api/operators')
        assert response.status_code==200 and response.json()[0]['id']==session['user_id']
        assert PASSWORD not in response.text and 'password_hash' not in response.text
        assert client.post('/api/logout',headers=headers(session)).status_code==200
        assert client.get('/api/session').status_code==401
        logged=client.post('/api/login',json={'username':' ALICE ','password':PASSWORD})
        assert logged.status_code==200 and logged.json()['actor']==session['actor']
    with db.transaction() as s:
        operator=s.get(Operator,session['user_id'])
        assert operator.password_hash!=PASSWORD and verify_password(PASSWORD,operator.password_hash)
        assert not verify_password('a different password',operator.password_hash)
        serialized=str(list(s.scalars(select(Audit.details))))
        assert PASSWORD not in serialized and operator.password_hash not in serialized


def test_shared_access_is_read_only_including_bearer_and_legacy_cookie(db,case_factory):
    item=case_factory('legacy-denied')
    app=create_app(db.settings,db)
    with TestClient(app) as client:
        session=client.post('/api/login',json={'token':db.settings.api_token}).json()
        assert session['identity_type']=='legacy' and not session['can_review']
        assert client.get('/api/cases').status_code==200
        requests=[('/api/cases/'+item['case_id']+'/resolve',{'expected_version':1,'outcome':'VERIFIED_VALID','reason':REASON,'request_id':'blocked-legacy'}),
                  ('/api/cases/bulk-release',{'items':[item],'reason':REASON,'request_id':'blocked-bulk'}),
                  ('/api/simulation/provider',{'outcome':'accepted'}),
                  ('/api/mail/gmail/disconnect',{})]
        for path,body in requests:
            response=client.post(path,json=body,headers=headers(session))
            assert response.status_code==403 and 'personal operator' in response.text
        client.cookies.clear()
        client.headers['Authorization']='Bearer '+db.settings.api_token
        for path,body in requests:assert client.post(path,json=body).status_code==403
        assert client.get('/api/cases').status_code==200
    with db.transaction() as s:assert s.get(ReviewCase,item['case_id']).status=='OPEN'


def test_reviews_bind_actual_person_and_paginate_without_rewriting_history(db,case_factory):
    item=case_factory('named')
    app=create_app(db.settings,db)
    with TestClient(app) as alice, TestClient(app) as bob:
        a=bootstrap(alice,db); b_user=add_user(alice,a)
        b=bob.post('/api/login',json={'username':'bob','password':PASSWORD}).json()
        path='/api/cases/'+item['case_id']+'/resolve'
        unresolved={'expected_version':1,'outcome':'UNRESOLVED','reason':REASON,'request_id':'alice-pending'}
        assert alice.post(path,json={**unresolved,'actor':b['actor']},headers=headers(a)).status_code==422
        first=alice.post(path,json=unresolved,headers={**headers(a),'X-Actor':b['actor']})
        assert first.status_code==200,first.text
        assert alice.post(path,json=unresolved,headers=headers(a)).json()==first.json()
        assert bob.post(path,json=unresolved,headers=headers(b)).status_code==409
        final=bob.post(path,json={**unresolved,'outcome':'VERIFIED_VALID','request_id':'bob-release'},headers=headers(b))
        assert final.status_code==200,final.text
        first_page=alice.get('/api/cases/'+item['case_id']+'/history?limit=1').json()
        assert len(first_page['items'])==1 and first_page['next_cursor'] is not None
        event=first_page['items'][0]
        assert event['reviewer']['actor_id']==b['actor'] and event['reviewer']['username']=='bob'
        assert event['reviewed_at'] and event['reviewed_version_no']==1
        older=alice.get('/api/cases/'+item['case_id']+'/history',params={'limit':1,'before_id':first_page['next_cursor']}).json()
        assert older['next_cursor'] is None and older['items'][0]['reviewer']['actor_id']==a['actor']
        assert older['items'][0]['outcome']=='UNRESOLVED' and not older['legacy_items']
        detail=alice.get('/api/cases/'+item['case_id']).json()['case']
        assert detail['reviewed_by']==b['actor'] and detail['reviewed_by_attribution']['username']=='bob'
        assert detail['reviewed_at']
        csv_response=alice.get('/api/exports/review-history.csv',params={'case_id':item['case_id']})
        exported=list(csv.DictReader(io.StringIO(csv_response.text.lstrip('\ufeff'))))
        assert [row['username'] for row in exported]==['alice','bob']
        assert all(row['reviewed_version_no']=='1' and row['reviewed_at_utc'] for row in exported)
        assert alice.get('/api/cases/'+item['case_id']+'/history?limit=51').status_code==422
        assert alice.get('/api/cases/missing/history').status_code==404
        assert b_user['id']==b['user_id']


def test_deactivated_accounts_fail_existing_sessions_and_require_new_login_after_reactivation(db):
    app=create_app(db.settings,db)
    with TestClient(app) as alice, TestClient(app) as bob:
        a=bootstrap(alice,db); b_user=add_user(alice,a)
        b=bob.post('/api/login',json={'username':'bob','password':PASSWORD}).json()
        assert bob.get('/api/operators').status_code==403
        assert bob.post('/api/operators',headers=headers(b),json={'username':'eve','display_name':'Eve','password':PASSWORD}).status_code==403
        assert alice.patch('/api/operators/'+a['user_id'],headers=headers(a),json={'active':False}).status_code==409
        response=alice.patch('/api/operators/'+b_user['id'],headers=headers(a),json={'active':False})
        assert response.status_code==200 and not response.json()['active']
        assert bob.get('/api/session').status_code==401
        assert bob.post('/api/login',json={'username':'bob','password':PASSWORD}).status_code==401
        assert alice.patch('/api/operators/'+b_user['id'],headers=headers(a),json={'active':True}).status_code==200
        assert bob.get('/api/session').status_code==401
        assert bob.post('/api/login',json={'username':'bob','password':PASSWORD}).status_code==200


def test_same_display_name_does_not_merge_people_and_password_inputs_never_echo(db):
    with TestClient(create_app(db.settings,db)) as client:
        session=bootstrap(client,db)
        result=client.post('/api/operators',headers=headers(session),json={'username':'alice.other','display_name':'Alice Reviewer','password':PASSWORD})
        assert result.status_code==200 and result.json()['id']!=session['user_id']
        bad=client.post('/api/operators',headers=headers(session),json={'username':'other','display_name':'Other','password':'shortsecret'})
        assert bad.status_code==422 and 'shortsecret' not in bad.text
        assert client.post('/api/operators',headers=headers(session),json={'username':'ALICE','display_name':'Other','password':PASSWORD}).status_code==409
        assert client.post('/api/login',headers={'Origin':'https://attacker.example'},json={'username':'alice','password':PASSWORD}).status_code==403
        assert client.post('/api/operators',json={'username':'withoutcsrf','display_name':'No CSRF','password':PASSWORD}).status_code==403


def test_concurrent_bootstrap_creates_exactly_one_first_admin(db):
    app=create_app(db.settings,db);barrier=Barrier(2)
    def attempt(username):
        with TestClient(app) as client:
            barrier.wait(timeout=5)
            return client.post('/api/operators/bootstrap',json={'setup_token':db.settings.api_token,
                'username':username,'display_name':username.title(),'password':PASSWORD}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,['first','second']))
    assert sorted(results)==[200,409]
    with db.transaction() as s:assert s.scalar(select(func.count()).select_from(Operator))==1


def test_legacy_history_preserves_unknown_person_and_movement_attribution(db,case_factory):
    item=case_factory('historical')
    with db.transaction() as s:
        case=s.get(ReviewCase,item['case_id']);case.reviewed_by='local-reviewer'
        audit(s,'local-reviewer','case_reviewed',case.id,{'outcome':'UNRESOLVED','reason':'Old shared account'})
        audit(s,'old-cli','correction_applied',case.movement_id,{'previous_version':1,'new_version':2})
    with TestClient(create_app(db.settings,db),headers={'Authorization':'Bearer '+db.settings.api_token}) as client:
        page=client.get('/api/cases/'+item['case_id']+'/history?limit=1').json()
        assert page['items']==[] and page['legacy_next_cursor']
        assert page['legacy_items'][0]['actor']=='old-cli'
        assert page['legacy_items'][0]['case_attribution']=='LEGACY_MOVEMENT_EVENT_CASE_UNAVAILABLE'
        assert not page['legacy_items'][0]['person_verified']
        next_page=client.get('/api/cases/'+item['case_id']+'/history',params={'limit':1,'legacy_before_id':page['legacy_next_cursor']}).json()
        assert next_page['legacy_items'][0]['actor']=='local-reviewer'
        detail=client.get('/api/cases/'+item['case_id']).json()['case']
        assert detail['reviewed_at'] is None
        assert detail['reviewed_by_attribution']['identity_type']=='legacy_or_automation'


def test_shared_setup_token_cannot_forge_personal_cookie_and_private_key_survives_restart(db):
    with TestClient(create_app(db.settings,db)) as client:
        session=bootstrap(client,db)
        genuine=client.cookies.get('invoice_session')
    payload={'operator_id':session['user_id'],'credential_version':1,'csrf_token':'forged-csrf'}
    for salt in ('invoice-session-v1','invoice-session-v2'):
        forged=URLSafeTimedSerializer(db.settings.api_token,salt=salt).dumps(payload)
        with TestClient(create_app(db.settings,db)) as attacker:
            attacker.cookies.set('invoice_session',forged)
            assert attacker.get('/api/session').status_code==401
            assert attacker.post('/api/simulation/provider',json={'outcome':'accepted'},headers={'X-CSRF-Token':'forged-csrf'}).status_code==401
    # A server restart and setup-token rotation preserve the private cookie key.
    rotated=replace(db.settings,api_token='different-shared-token-'+'z'*40)
    with TestClient(create_app(rotated,db)) as restarted:
        restarted.cookies.set('invoice_session',genuine)
        assert restarted.get('/api/session').json()['user_id']==session['user_id']
    secret_path=db.settings.data_dir/'private'/'session-signing.key'
    assert secret_path.stat().st_mode & 0o777==0o600
    assert secret_path.parent.stat().st_mode & 0o777==0o700
    assert len(secret_path.read_bytes())==64
    assert db.settings.api_token.encode() not in secret_path.read_bytes()


def test_private_signing_key_concurrent_initialization_and_corruption_fail_closed(tmp_path):
    import pytest
    from invoice_optimizer.config import Settings
    settings=Settings(data_dir=tmp_path/'secret-state')
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys=list(pool.map(lambda _:session_signing_secret(settings),range(16)))
    assert len(set(keys))==1
    path=settings.data_dir/'private'/'session-signing.key'
    assert list(path.parent.iterdir())==[path]
    path.write_bytes(b'invalid-key')
    with pytest.raises(RuntimeError):session_signing_secret(settings)
    assert path.read_bytes()==b'invalid-key'


def test_shared_success_cannot_reset_personal_login_failure_limit(db):
    with TestClient(create_app(db.settings,db)) as client:
        bootstrap(client,db)
        client.cookies.clear()
        for _ in range(10):
            assert client.post('/api/login',json={'username':'alice','password':'incorrect-password'}).status_code==401
        assert client.post('/api/login',json={'token':db.settings.api_token}).status_code==200
        assert client.post('/api/login',json={'username':'alice','password':PASSWORD}).status_code==429


def test_sse_revokes_an_open_stream_when_account_is_deactivated(db):
    app=create_app(replace(db.settings,poll_seconds=.1),db)
    with TestClient(app) as client:
        bootstrap(client,db)
        who=client.get('/api/session').json()
    endpoint=next(route.endpoint for route in app.routes if getattr(route,'path',None)=='/api/events')
    class ConnectedRequest:
        async def is_disconnected(self):return False
    async def probe():
        response=await endpoint(ConnectedRequest(),who)
        iterator=response.body_iterator
        first=await anext(iterator)
        assert 'event: update' in first
        with db.transaction() as s:s.get(Operator,who['user_id']).active=False
        second=await anext(iterator)
        assert 'event: auth_expired' in second
        try:await anext(iterator)
        except StopAsyncIteration:pass
        else:raise AssertionError('Revoked SSE stream must terminate')
    asyncio.run(probe())


def test_rejected_single_attempt_has_person_and_does_not_claim_a_review(db,case_factory):
    item=case_factory('single-rejected',case_reason='AMOUNT_MISMATCH')
    with TestClient(create_app(db.settings,db)) as client:
        session=bootstrap(client,db)
        response=client.post('/api/cases/'+item['case_id']+'/resolve',headers=headers(session),
            json={'expected_version':1,'outcome':'VERIFIED_VALID','reason':REASON,'request_id':'single-denied'})
        assert response.status_code==409
        history=client.get('/api/cases/'+item['case_id']+'/history').json()
        assert not history['items'] and len(history['attempts'])==1
        assert history['attempts'][0]['reviewer']['actor_id']==session['actor']
        assert history['attempts'][0]['result']=='REJECTED' and history['attempts'][0]['request_id']=='single-denied'
        case=client.get('/api/cases/'+item['case_id']).json()['case']
        assert case['reviewed_by'] is None and case['reviewed_at'] is None


def test_review_worklist_download_requires_referenced_hash_and_exact_bytes(db,case_factory):
    import hashlib
    from invoice_optimizer.storage import ObjectStore
    item=case_factory('worklist-file')
    stream=io.StringIO();writer=csv.writer(stream)
    writer.writerow(['case_id','expected_version','review_reason','description'])
    writer.writerow([item['case_id'],1,REASON,'Correction from retained worklist'])
    raw=stream.getvalue().encode();key=hashlib.sha256(raw).hexdigest()
    app=create_app(db.settings,db)
    with TestClient(app) as client:
        session=bootstrap(client,db)
        response=client.post('/api/review/import',headers=headers(session),files={'file':('edited-worklist.csv',raw,'text/csv')})
        assert response.status_code==200 and response.json()['applied']==1,response.text
        downloaded=client.get('/api/review/files/'+key)
        assert downloaded.status_code==200 and downloaded.content==raw
        unreferenced=ObjectStore(db.settings.data_dir/'objects').put_bytes(b'no review references this object')
        assert client.get('/api/review/files/'+unreferenced).status_code==404
        assert client.get('/api/review/files/invalid').status_code==422
        stored=ObjectStore(db.settings.data_dir/'objects').path(key)
        stored.chmod(0o600);stored.write_bytes(b'corrupt')
        assert client.get('/api/review/files/'+key).status_code==422
    with TestClient(app) as anonymous:assert anonymous.get('/api/review/files/'+key).status_code==401


def test_non_ascii_secrets_are_rejected_with_401_not_a_server_error(db):
    # Review finding: secrets.compare_digest raises TypeError on non-ASCII str, a 500.
    with TestClient(create_app(db.settings,db),raise_server_exceptions=False) as client:
        response=client.post('/api/operators/bootstrap',json={'setup_token':'é'*40,'username':'alice',
            'display_name':'Alice','password':PASSWORD})
        assert response.status_code==401,response.text
        assert client.post('/api/login',json={'token':'é'*40}).status_code==401
        assert client.get('/api/auth/status').json()=={'setup_required':True}
        session=bootstrap(client,db)
        bad=client.post('/api/logout',headers={'X-CSRF-Token':'é'.encode('utf-8'),
                                                'Origin':'http://testserver'})
        assert bad.status_code==403,bad.text
        assert client.post('/api/logout',headers=headers(session)).status_code==200
