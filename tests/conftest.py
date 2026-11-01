"""Integration tests use disposable PostgreSQL schemas, never the app schema."""
import os, re
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from invoice_optimizer.config import Settings
from invoice_optimizer.db import Database

@pytest.fixture
def db(tmp_path):
    url=os.getenv('TEST_DATABASE_URL','postgresql+psycopg://invoice:invoice@127.0.0.1:55432/invoice_optimizer')
    admin=create_engine(url,pool_pre_ping=True)
    schema='invoice_test_'+uuid4().hex
    assert re.fullmatch(r'invoice_test_[a-f0-9]{32}',schema)
    try:
        with admin.begin() as c:c.execute(text(f'CREATE SCHEMA {schema}'))
    except Exception as exc:
        admin.dispose()
        pytest.fail('Real PostgreSQL integration is required; start scripts/local_postgres.sh or set TEST_DATABASE_URL. '+str(exc))
    scoped=make_url(url).update_query_dict({'options':f'-csearch_path={schema}'})
    settings=Settings(database_url=scoped.render_as_string(hide_password=False),data_dir=tmp_path/'data',api_token='integration-only-token-'+'x'*40).prepare()
    database=Database(settings);database.init()
    try:yield database
    finally:
        database.close()
        with admin.begin() as c:c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
