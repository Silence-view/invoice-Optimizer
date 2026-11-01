"""Migration 8, scoped close checks, batched quarantine, keyset pages, engines, retention."""
import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import threading, time, uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from invoice_optimizer.api import create_app
from invoice_optimizer.billing import close_checks, create_invoice
from invoice_optimizer.db import Blocked, Database, digest, is_timeout, record_command
from invoice_optimizer.ingestion import ingest_file
from invoice_optimizer.maintenance import maintenance
from invoice_optimizer.model_ops import ModelTrainingRun
from invoice_optimizer.models import (Audit, Command, Invoice, Job, MovementVersion, Outbox, Quarantine,
                                      ReviewCase, uid)
from invoice_optimizer.review_history import ReviewEvent
from invoice_optimizer.worker import run_job
from test_workflow import prepared, rows, write_csv  # noqa: F401

DROPPED = {'ix_source_latest', 'ix_movement_versions_movement_id', 'ix_assessments_version_id',
           'ix_movements_customer_id', 'ix_movements_state', 'ix_invoices_customer_id', 'ix_review_events_case_id',
           'ix_review_events_period', 'ix_review_events_occurred_at', 'ix_jobs_status', 'ix_review_cases_status',
           'ix_source_records_movement_id'}
ADDED = {'ix_source_period_record', 'ix_source_movement_latest', 'ix_cases_open_version', 'ix_cases_open_scope',
         'ix_cases_status_created', 'ix_quarantine_unresolved', 'ix_jobs_unfinished', 'ix_outbox_created',
         'ix_invoices_created', 'brin_audit_created', 'brin_review_events_occurred', 'brin_source_received',
         'brin_versions_created', 'brin_assessments_evaluated', 'brin_jobs_created', 'brin_commands_created'}


def index_names(db):
    with db.transaction() as s:
        return set(s.scalars(text('SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()')))


def statements(db):
    seen = []
    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)
    event.listen(db.engine, 'before_cursor_execute', record)
    return seen, lambda: event.remove(db.engine, 'before_cursor_execute', record)


def client(db):
    return TestClient(create_app(db.settings, db), headers={'Authorization': 'Bearer ' + db.settings.api_token})


def test_fresh_and_upgraded_schemas_match_migration_8(db):
    fresh = index_names(db)
    assert not fresh & DROPPED and ADDED <= fresh
    with db.transaction() as s:
        assert s.scalar(text('SELECT max(version) FROM schema_migrations')) == 8
        # Simulate a version-7 database: old indexes present, derived columns empty.
        s.execute(text('CREATE INDEX ix_movements_state ON movements(state)'))
        s.execute(text('CREATE INDEX ix_source_latest ON source_records(source, record_id, revision)'))
        s.execute(text('DROP INDEX ix_cases_status_created'))
    db.init(); db.init()
    assert index_names(db) == fresh


def test_uid_is_time_ordered_uuid7_hex():
    first = [uid() for _ in range(200)]
    time.sleep(0.003)
    later = uid()
    assert all(len(x) == 32 and x == x.lower() and int(x, 16) >= 0 for x in first + [later])
    assert all(uuid.UUID(x).version == 7 and uuid.UUID(x).variant == uuid.RFC_4122 for x in first)
    assert len(set(first)) == 200 and later > max(first)
    stamp = int(later[:12], 16) / 1000
    assert abs(stamp - time.time()) < 5


def test_freeze_sets_derived_list_columns_and_backfill_matches_document(prepared):
    db = prepared
    for fmt in ('ITEMIZED', 'SUMMARY'):
        if fmt == 'SUMMARY':
            with db.transaction() as s:
                s.execute(text("UPDATE invoices SET status='STALE'"))
        create_invoice(db, 'C1', '2026-09', 'derived-' + fmt, 'reviewer', line_format=fmt)
    with db.transaction() as s:
        frozen = {i.line_format: (i.movement_count, i.movement_vat_policy, i.document) for i in s.scalars(select(Invoice))}
    for fmt, (count, policy, document) in frozen.items():
        assert document['line_format'] == fmt and count == document['movement_count'] == 2
        assert policy == ((document.get('movement_vat') or {}).get('policy'))
    assert frozen['SUMMARY'][1] is None and frozen['ITEMIZED'][1]
    with client(db) as c:
        before = c.get('/api/invoices').json()
        with db.transaction() as s:
            s.execute(text('UPDATE invoices SET line_format=NULL, movement_count=NULL, movement_vat_policy=NULL'))
        assert c.get('/api/invoices').json() == before  # JSON fallback
        db.init()
        assert c.get('/api/invoices').json() == before  # backfilled
    with db.transaction() as s:
        assert {i.line_format: (i.movement_count, i.movement_vat_policy, i.document) for i in s.scalars(select(Invoice))} == frozen
    assert all(set(item) >= {'line_format', 'line_count', 'movement_vat_policy'} and 'movement_count' not in item for item in before)


def coverage_queries(seen):
    return [x for x in seen if 'source_records' in x and 'NOT (EXISTS' in x]


def test_coverage_certificate_is_cached_until_any_new_source_delivery(prepared, tmp_path):
    db = prepared
    seen, stop = statements(db)
    try:
        with db.transaction() as s: close_checks(s, 'C1', '2026-09')
        assert len(coverage_queries(seen)) == 2
        seen.clear()
        with db.transaction() as s: close_checks(s, 'C1', '2026-09')
        assert coverage_queries(seen) == []
        # A complete, count-verified delivery that omits a known record must
        # invalidate the certificate: a new batch changes the key.
        ingest_file(db, write_csv(tmp_path / 'thin.csv', rows(revision=2)[:1]), 'A', '2026-09', 1, True)
        while run_job(db): pass
        seen.clear()
        with db.transaction() as s, pytest.raises(Blocked, match='cover'): close_checks(s, 'C1', '2026-09')
        assert len(coverage_queries(seen)) == 1
        # A failed delta that is later superseded by a covering delivery.
        full = rows(amounts=('10.10', '20.20'), revision=3)
        ingest_file(db, write_csv(tmp_path / 'full.csv', full), 'A', '2026-09', 2, True)
        while run_job(db): pass
        with db.transaction() as s: close_checks(s, 'C1', '2026-09')
        seen.clear()
        with db.transaction() as s: close_checks(s, 'C1', '2026-09')
        assert coverage_queries(seen) == []
    finally:
        stop()
    # Another database (schema) never reuses this engine's certificates.
    other = Database(db.settings)
    try:
        seen, stop = statements(other)
        with other.transaction() as s: close_checks(s, 'C1', '2026-09')
        stop()
        assert len(coverage_queries(seen)) == 2
    finally:
        other.close()


def test_quarantine_cases_and_alerts_are_batched_with_identical_evidence(prepared, tmp_path):
    db = prepared
    bad = rows(amounts=tuple('1.00' for _ in range(1100)))
    for i, row in enumerate(bad):
        row['record_id'] = f'q{i}'
        if i % 3 == 0: row['amount'] = 'not-money'
        elif i % 3 == 1: row['customer_id'] = 'UNKNOWN'
        else: row['event_at'] = 'later'
    path = write_csv(tmp_path / 'bad.csv', bad)
    seen, stop = statements(db)
    try:
        result = ingest_file(db, path, 'B', '2026-09', 1100, False)
    finally:
        stop()
    assert result['quarantined'] == 1100 and result['accepted'] == 0
    assert len([x for x in seen if x.startswith('INSERT INTO review_cases')]) <= 3
    with db.transaction() as s:
        quarantine = {q.row_number: q for q in s.scalars(select(Quarantine).where(Quarantine.batch_id == result['batch_id']))}
        cases = {c.id: c for c in s.scalars(select(ReviewCase).where(ReviewCase.reason.like('QUARANTINE:%')))}
        alerts = {o.case_id: o for o in s.scalars(select(Outbox).where(Outbox.case_id.in_(list(cases))))}
    assert sorted(quarantine) == list(range(1, 1101)) and len(cases) == len(alerts) == 1100
    for n, q in quarantine.items():
        assert q.id == digest([result['batch_id'], n])
        case = cases[digest(['quarantine', q.id])]
        known = q.payload['customer_id'] == 'C1'
        owner = 'review@example.test' if known else 'operations@example.test'
        assert (case.dedup_key, case.customer_id, case.period, case.reason, case.owner, case.severity, case.status) == (
            'quarantine:' + q.id, 'C1' if known else None, '2026-09', 'QUARANTINE: ' + q.reason[:220], owner, 'HIGH', 'OPEN')
        alert = alerts[case.id]
        assert (alert.dedup_key, alert.kind, alert.status, alert.attempts) == ('flag:' + case.id, 'ALERT', 'READY', 0)
        assert alert.payload == {'to': [owner], 'from': 'operations@example.test', 'subject': 'Source data requires review',
                                 'body': 'Case ' + case.id + ' requires review. ' + q.reason}
        assert len(alert.id) == 32 and uuid.UUID(alert.id).version == 7


def seed_pages(db):
    """Ties on sort values, NULL scores and NULL customers exercise every keyset branch."""
    base = datetime(2026, 9, 20, tzinfo=timezone.utc)
    with db.transaction() as s:
        versions = list(s.scalars(select(MovementVersion)))
        for i in range(23):
            version = versions[i % len(versions)] if i % 4 else None
            s.add(ReviewCase(id=f'case-{i:03d}', dedup_key=f'page:{i}', movement_id=version.movement_id if version else None,
                version_id=version.id if version else None, customer_id='C1' if i % 5 else None, period='2026-09',
                reason='PAGE_TEST', owner='review@example.test', created_at=base + timedelta(seconds=i // 3)))
            s.add(Outbox(id=f'out-{i:03d}', dedup_key=f'page:{i}', kind='ALERT', payload={'to': ['a@example.test']},
                         created_at=base + timedelta(seconds=i // 4)))
            s.add(Invoice(id=f'inv-{i:03d}', number=f'PAGE-{i:03d}', customer_id='C1', period='2026-08', revision=i + 1,
                status='STALE', net_total=Decimal('1.00'), vat_total=Decimal('0.20'), gross_total=Decimal('1.20'),
                snapshot_hash='0' * 64, snapshot_key='0' * 64, document={'line_format': 'SUMMARY', 'movement_count': i},
                line_format=None if i % 2 else 'SUMMARY', movement_count=None if i % 2 else i,
                created_at=base + timedelta(seconds=i // 2)))
        for i in range(30):
            s.add(Audit(actor='pager', action='page_test', entity_id=str(i), details={}))
        s.flush()
        s.execute(text("UPDATE assessments SET score=NULL WHERE kind='MODEL' AND version_id=:v"), {'v': versions[0].id})


def walk(c, path, limit, **params):
    items, cursor, pages = [], '', 0
    while cursor is not None:
        response = c.get(path, params={**params, 'limit': limit, 'cursor': cursor})
        assert response.status_code == 200, response.text
        body = response.json()
        assert response.headers.get('X-Next-Cursor') == body['next_cursor'] and len(body['items']) <= limit
        items += body['items']; cursor = body['next_cursor']; pages += 1
    return items, pages


@pytest.mark.parametrize('sort', ['newest', 'oldest', 'score_desc', 'score_asc', 'customer'])
def test_case_keyset_pages_equal_offset_order(prepared, sort):
    db = prepared; seed_pages(db)
    with client(db) as c:
        full = c.get('/api/cases', params={'reason': 'PAGE_TEST', 'sort': sort, 'limit': 100}).json()
        assert isinstance(full, list) and len(full) == 23
        paged, pages = walk(c, '/api/cases', 4, reason='PAGE_TEST', sort=sort)
        assert [x['id'] for x in paged] == [x['id'] for x in full] and pages == 6
        if sort == 'score_desc':
            assert any(x['score'] is None for x in full) and full[-1]['score'] is None
        # Offset mode keeps the bare list and also advertises the next cursor.
        offset = c.get('/api/cases', params={'reason': 'PAGE_TEST', 'sort': sort, 'limit': 5, 'offset': 5})
        assert [x['id'] for x in offset.json()] == [x['id'] for x in full[5:10]] and offset.headers['X-Next-Cursor']
        resumed = c.get('/api/cases', params={'reason': 'PAGE_TEST', 'sort': sort, 'limit': 5,
                                              'cursor': offset.headers['X-Next-Cursor']}).json()
        assert [x['id'] for x in resumed['items']] == [x['id'] for x in full[10:15]]


def test_invoice_outbox_audit_keyset_pages_and_cursor_validation(prepared):
    db = prepared; seed_pages(db)
    with client(db) as c:
        for path, limit in (('/api/invoices', 5), ('/api/outbox', 6), ('/api/audit', 7)):
            full = c.get(path, params={'limit': 100}).json()
            paged, _ = walk(c, path, limit)
            assert [x['id'] for x in paged] == [x['id'] for x in full] and len(full) > limit
        invoices = {x['id']: x for x in c.get('/api/invoices', params={'limit': 100}).json()}
        assert all(invoices[f'inv-{i:03d}']['line_count'] == i for i in range(23))
        newest = c.get('/api/cases', params={'limit': 2, 'cursor': ''}).json()['next_cursor']
        for params in ({'cursor': newest}, {'cursor': 'not-base64!'}, {'cursor': '', 'offset': 3}):
            response = c.get('/api/invoices', params=params)
            assert response.status_code == 422, response.text
        assert c.get('/api/cases', params={'sort': 'oldest', 'cursor': newest}).status_code == 422
        last = c.get('/api/audit', params={'limit': 100}).json()
        assert c.get('/api/audit', params={'limit': 100}).headers.get('X-Next-Cursor') is None and len(last) < 100


def test_engine_options_merge_with_url_options_and_read_engine_is_read_only(db):
    settings = replace(db.settings, db_statement_timeout_ms=1234, db_lock_timeout_ms=567, db_idle_tx_timeout_ms=8910,
                       db_pool_size=3, db_max_overflow=1, database_read_url=db.settings.database_url)
    other = Database(settings, role='api')
    try:
        with db.engine.connect() as c: expected_path = c.execute(text('SHOW search_path')).scalar_one()
        with other.engine.connect() as c:
            assert c.execute(text('SHOW search_path')).scalar_one() == expected_path
            assert [c.execute(text(f'SHOW {name}')).scalar_one() for name in
                    ('statement_timeout', 'lock_timeout', 'idle_in_transaction_session_timeout', 'application_name')] == [
                    '1234ms', '567ms', '8910ms', 'invoice-optimizer-api']
        assert other.engine.pool.size() == 3 and other.read_engine is not other.engine
        with other.read_transaction() as s:
            assert s.execute(text("SELECT current_setting('application_name')")).scalar_one() == 'invoice-optimizer-api-read'
        with pytest.raises(DBAPIError) as raised, other.read_transaction() as s:
            s.execute(text("INSERT INTO runtime_options(key, value) VALUES ('x', '{}')"))
        assert raised.value.orig.sqlstate == '25006'
        with pytest.raises(DBAPIError) as raised, other.transaction() as s:
            s.execute(text('SELECT pg_sleep(2)'))
        assert is_timeout(raised.value)
    finally:
        other.close()
    with db.engine.connect() as c:  # defaults keep PostgreSQL's unbounded behaviour
        assert c.execute(text('SHOW statement_timeout')).scalar_one() == '0'
    for bad in ({'db_pool_size': 0}, {'db_lock_timeout_ms': -1}, {'database_read_url': 'sqlite://'}):
        with pytest.raises(ValueError): replace(db.settings, **bad).prepare()
    with pytest.raises(ValueError): Database(db.settings, role='robot')


def test_lock_timeout_on_api_read_is_retryable_503(db):
    api_db = Database(replace(db.settings, db_lock_timeout_ms=200), role='api')
    holder = db.engine.connect()
    try:
        with TestClient(create_app(api_db.settings, api_db), headers={'Authorization': 'Bearer ' + db.settings.api_token}) as c:
            assert c.get('/api/audit').status_code == 200
            tx = holder.begin(); holder.execute(text('LOCK TABLE audit_events IN ACCESS EXCLUSIVE MODE'))
            started = time.monotonic()
            response = c.get('/api/audit')
            assert response.status_code == 503 and response.headers['Retry-After'] == '2'
            assert response.json()['retryable'] is True and time.monotonic() - started < 5
            tx.rollback()
            assert c.get('/api/audit').status_code == 200
    finally:
        holder.close(); api_db.close()


def test_maintenance_prunes_only_old_done_jobs_and_commands(db):
    old = datetime.now(timezone.utc) - timedelta(days=40)
    with db.transaction() as s:
        for key, status, created in (('old-done', 'DONE', old), ('new-done', 'DONE', None), ('old-ready', 'READY', old),
                                     ('old-train', 'DONE', old)):
            s.add(Job(id=key, dedup_key=key, kind='ASSESS', period='2026-09', payload={}, status=status,
                      **({'created_at': created} if created else {})))
        s.flush()
        s.add(ModelTrainingRun(request_id='train-1', job_id='old-train', actor='a', requester={}, parameters={}))
        record_command(s, 'old-cmd', 'x', {}, {'ok': True}); record_command(s, 'old-bulk', 'x', {}, {'status': 'RUNNING'})
        record_command(s, 'new-cmd', 'x', {}, {'ok': True})
        s.flush()
        s.execute(text("UPDATE commands SET created_at=:t WHERE request_id LIKE 'old-%'"), {'t': old})
        audits = s.scalar(select(func.count()).select_from(Audit)); events = s.scalar(select(func.count()).select_from(ReviewEvent))
    dry = maintenance(db, jobs_days=30, commands_days=30)
    assert dry['mode'] == 'DRY_RUN' and dry['done_jobs']['eligible'] == 1 and dry['commands']['eligible'] == 1
    assert dry['done_jobs']['deleted'] == dry['commands']['deleted'] == 0 and 'warning' in dry['commands']
    assert set(dry['maintenance_hints']) == {'tables', 'indexes_never_scanned_since_stats_reset'}
    applied = maintenance(db, jobs_days=30, commands_days=30, apply=True)
    assert applied['done_jobs']['deleted'] == 1 and applied['commands']['deleted'] == 1
    with db.transaction() as s:
        assert set(s.scalars(select(Job.id))) == {'new-done', 'old-ready', 'old-train'}
        assert set(s.scalars(select(Command.request_id))) == {'old-bulk', 'new-cmd'}
        assert s.scalar(select(func.count()).select_from(ReviewEvent)) == events
        logged = list(s.scalars(select(Audit).where(Audit.action == 'retention_pruned').order_by(Audit.id)))
        assert s.scalar(select(func.count()).select_from(Audit)) == audits + 2
        assert [(a.entity_id, a.details['rows']) for a in logged] == [('jobs', 1), ('commands', 1)]
    assert maintenance(db, jobs_days=30, apply=True)['done_jobs'] == {'older_than_days': 30, 'eligible': 0, 'deleted': 0}
    for bad in (0, -1, 1.5):
        with pytest.raises(ValueError): maintenance(db, jobs_days=bad)


def test_default_case_list_and_close_counts_are_index_backed(prepared):
    db = prepared
    with db.transaction() as s:
        # Tiny tables favour a sort; forbid the alternatives so the plan proves an index exists.
        for setting in ('enable_seqscan', 'enable_sort', 'enable_bitmapscan'):
            s.execute(text(f'SET LOCAL {setting} = off'))
        plans = {name: '\n'.join(s.scalars(text('EXPLAIN ' + sql))) for name, sql in {
            'cases': "SELECT id FROM review_cases WHERE status='OPEN' ORDER BY created_at DESC, id LIMIT 51",
            'jobs': "SELECT count(*) FROM jobs WHERE period='2026-09' AND kind IN ('ASSESS','SCORE') AND status<>'DONE'",
            'quarantine': "SELECT count(*) FROM quarantine WHERE period='2026-09' AND resolved_by_batch IS NULL AND (validated_customer_id='C1' OR validated_customer_id IS NULL)",
            'open_cases': "SELECT count(*) FROM review_cases WHERE period='2026-09' AND customer_id='C1' AND status='OPEN'",
            'invoices': 'SELECT id FROM invoices ORDER BY created_at DESC, id LIMIT 51',
            'outbox': 'SELECT id FROM outbox ORDER BY created_at DESC, id LIMIT 51'}.items()}
    for name, index in (('cases', 'ix_cases_status_created'), ('jobs', 'ix_jobs_unfinished'),
                        ('quarantine', 'ix_quarantine_unresolved'), ('open_cases', 'ix_cases_open_scope'),
                        ('invoices', 'ix_invoices_created'), ('outbox', 'ix_outbox_created')):
        assert index in plans[name], plans[name]
        assert 'Sort' not in plans[name], plans[name]


def test_tampered_cursor_with_nul_character_is_rejected_not_a_server_error(db):
    # Review finding: a NUL inside a cursor reached psycopg and surfaced as a 500.
    import base64, json
    def cursor(*parts):
        return base64.urlsafe_b64encode(json.dumps(list(parts)).encode()).rstrip(b'=').decode()
    with client(db) as c:
        for path, params in (('/api/cases', {'sort': 'customer', 'cursor': cursor('cases:customer', 'C\x00', 'a')}),
                             ('/api/cases', {'sort': 'customer', 'cursor': cursor('cases:customer', 'C', 'a\x00')}),
                             ('/api/invoices', {'cursor': cursor('invoices', '2026-01-01T00:00:00+00:00', 'i\x00')}),
                             ('/api/outbox', {'cursor': cursor('outbox', '2026-01-01T00:00:00\x00', 'o')})):
            response = c.get(path, params=params)
            assert response.status_code == 422, (path, response.status_code, response.text)
            assert response.json()['detail'] == 'Invalid pagination cursor for this list and sort order'
