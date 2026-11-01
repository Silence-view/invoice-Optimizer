"""Review fixes for the scale track: keyset seeks, engine disposal."""
from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import event, text

from invoice_optimizer.api import create_app
from invoice_optimizer.db import Database
from invoice_optimizer.models import Audit, Outbox


def test_keyset_cursor_is_an_index_seek_not_a_filtered_rescan(db):
    with db.transaction() as s:
        s.add_all([Audit(actor='pager', action='page_test', entity_id=str(i), details={}) for i in range(40)])
        s.add_all([Outbox(id=f'out-{i:03d}', dedup_key=f'page:{i}', kind='ALERT', payload={'to': ['a@example.test']})
                   for i in range(40)])
    seen = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith('SELECT') and ('FROM audit_events' in statement or 'FROM outbox' in statement):
            seen.append((statement, parameters))
    with TestClient(create_app(db.settings, db), headers={'Authorization': 'Bearer ' + db.settings.api_token}) as c:
        for path, table in (('/api/audit', 'audit_events'), ('/api/outbox', 'outbox')):
            cursor = c.get(path, params={'limit': 5, 'cursor': ''}).json()['next_cursor']
            seen.clear()
            event.listen(db.engine, 'before_cursor_execute', record)
            try:
                page = c.get(path, params={'limit': 5, 'cursor': cursor})
            finally:
                event.remove(db.engine, 'before_cursor_execute', record)
            assert page.status_code == 200 and len(page.json()['items']) == 5
            statement, parameters = next(x for x in seen if 'FROM ' + table in x[0] and 'LIMIT' in x[0])
            with db.engine.connect() as conn:
                for setting in ('enable_seqscan', 'enable_sort', 'enable_bitmapscan'):
                    conn.exec_driver_sql(f'SET {setting} = off')
                plan = '\n'.join(row[0] for row in conn.exec_driver_sql('EXPLAIN ' + statement, parameters))
            # The cursor must bound the index scan; a bare OR filter rereads every earlier row.
            assert 'Index Cond' in plan, plan


def test_close_disposes_a_separate_read_engine(db):
    other = Database(replace(db.settings, database_read_url=db.settings.database_url))
    with other.read_transaction() as s:
        s.execute(text('SELECT 1'))
    assert other.read_engine.pool.checkedin() == 1
    other.close()
    assert other.read_engine.pool.checkedin() == 0


def test_timeouts_from_a_shared_env_never_reach_worker_or_cli_sessions(db):
    settings = replace(db.settings, db_statement_timeout_ms=1234, db_lock_timeout_ms=567, db_idle_tx_timeout_ms=8910)
    for role in ('worker', 'cli'):
        other = Database(settings, role=role)
        try:
            with other.engine.connect() as c:
                assert [c.execute(text(f'SHOW {name}')).scalar_one() for name in
                        ('statement_timeout', 'lock_timeout', 'idle_in_transaction_session_timeout')] == ['0', '0', '0']
                assert c.execute(text('SHOW search_path')).scalar_one().startswith('invoice_test_')
        finally:
            other.close()
