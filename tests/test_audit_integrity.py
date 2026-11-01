"""Database enforcement and additive migration evidence on isolated schemas."""
import pytest
from sqlalchemy import text, select, func
from sqlalchemy.exc import DBAPIError, IntegrityError
from invoice_optimizer.db import audit
from invoice_optimizer.models import Audit, ReviewCase


@pytest.mark.parametrize('statement', [
    "UPDATE audit_events SET actor='someone-else'",
    'DELETE FROM audit_events',
    'TRUNCATE audit_events',
    'TRUNCATE review_events',
])
def test_recorded_history_cannot_be_rewritten_or_truncated(db, statement):
    with db.transaction() as s:
        audit(s, 'original-operator', 'integrity_fixture', 'test-case', {'reason': 'Original evidence'})
    with pytest.raises(DBAPIError, match='append-only'):
        with db.transaction() as s:
            s.execute(text(statement))
    with db.transaction() as s:
        row = s.scalar(select(Audit).where(Audit.action == 'integrity_fixture'))
        assert row.actor == 'original-operator'
        assert row.details == {'reason': 'Original evidence'}


def test_audit_rejects_missing_actor_in_helper_and_database(db):
    for actor in ('', '  ', None):
        with pytest.raises(ValueError, match='actor'):
            with db.transaction() as s:
                audit(s, actor, 'invalid_fixture', 'test-case')
    with pytest.raises(IntegrityError):
        with db.transaction() as s:
            s.execute(text("INSERT INTO audit_events(actor,action,entity_id,details,created_at) VALUES ('  ','invalid_fixture','test-case','{}',now())"))
    with db.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Audit)) == 0


def test_repeated_migration_preserves_legacy_identity_without_fabricating_time(db):
    with db.transaction() as s:
        s.add(ReviewCase(id='legacy-case', dedup_key='legacy-case', period='2026-09',
            reason='MODEL_RISK', owner='old@example.test', status='RESOLVED',
            outcome='VERIFIED_VALID', review_reason='Historic shared-login decision',
            reviewed_by='local-reviewer'))
        audit(s, 'local-reviewer', 'case_reviewed', 'legacy-case', {'outcome': 'VERIFIED_VALID'})
        s.flush()
        old = s.scalar(select(Audit))
        original = (old.id, old.actor, old.created_at, old.details)
    db.init()
    db.init()
    with db.transaction() as s:
        row = s.get(ReviewCase, 'legacy-case')
        assert row.reviewed_by == 'local-reviewer' and row.reviewed_at is None
        old = s.scalar(select(Audit))
        assert (old.id, old.actor, old.created_at, old.details) == original
        assert s.scalar(text('SELECT max(version) FROM schema_migrations')) == 8
        assert s.scalar(text('SELECT count(*) FROM review_events')) == 0
