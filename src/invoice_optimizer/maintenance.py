"""Retention for operational scratch rows, plus vacuum/analyze hints.

Only DONE jobs and old command (idempotency) records are eligible. Ledgers,
movement versions, invoices, cases and outbox evidence are never deleted here.
"""
from datetime import timedelta
from sqlalchemy import and_, delete, exists, func, select, text
from .db import audit
from .models import Command, Job, now

CHUNK = 5000


def _eligible(model, days):
    from .model_ops import ModelTrainingRun
    cutoff = now() - timedelta(days=days)
    if model is Job:
        # A training run keeps its job as evidence through a foreign key.
        return and_(Job.status == 'DONE', Job.created_at < cutoff,
                    ~exists().where(ModelTrainingRun.job_id == Job.id))
    # A RUNNING response belongs to an unfinished bulk command that updates it.
    return and_(Command.created_at < cutoff, Command.response['status'].astext.is_distinct_from('RUNNING'))


def _prune(db, model, key, days, apply, actor):
    with db.transaction() as s:
        matched = s.scalar(select(func.count()).select_from(model).where(_eligible(model, days)))
    deleted = 0
    while apply:
        # Bounded transactions keep row locks and WAL bursts small.
        with db.transaction() as s:
            ids = select(key).where(_eligible(model, days)).limit(CHUNK).scalar_subquery()
            removed = s.execute(delete(model).where(key.in_(ids))).rowcount
            if removed:
                audit(s, actor, 'retention_pruned', model.__tablename__, {'older_than_days': days, 'rows': removed})
        deleted += removed
        if removed < CHUNK:
            break
    return {'older_than_days': days, 'eligible': matched, 'deleted': deleted}


def hints(db):
    """Advisory only: autovacuum normally suffices; nothing is run here."""
    with db.transaction() as s:
        tables = s.execute(text("""SELECT relname, n_live_tup, n_dead_tup, n_mod_since_analyze,
                greatest(last_vacuum, last_autovacuum) AS vacuumed, greatest(last_analyze, last_autoanalyze) AS analyzed
            FROM pg_stat_user_tables WHERE schemaname = current_schema() ORDER BY n_dead_tup DESC, relname""")).all()
        unused = s.scalars(text("""SELECT i.indexrelname FROM pg_stat_user_indexes i JOIN pg_index x ON x.indexrelid = i.indexrelid
            WHERE i.schemaname = current_schema() AND i.idx_scan = 0 AND NOT x.indisunique AND NOT x.indisprimary
            ORDER BY i.indexrelname""")).all()
    result = []
    for row in tables:
        if row.n_dead_tup > 1000 and row.n_dead_tup > 0.1 * max(row.n_live_tup, 1):
            result.append({'table': row.relname, 'hint': f'VACUUM (ANALYZE) {row.relname}',
                           'dead_rows': row.n_dead_tup, 'live_rows': row.n_live_tup, 'last_vacuum': row.vacuumed})
        elif row.n_mod_since_analyze > 1000 and row.n_mod_since_analyze > 0.1 * max(row.n_live_tup, 1):
            result.append({'table': row.relname, 'hint': f'ANALYZE {row.relname}',
                           'modified_since_analyze': row.n_mod_since_analyze, 'last_analyze': row.analyzed})
    return {'tables': result, 'indexes_never_scanned_since_stats_reset': list(unused)}


def maintenance(db, *, jobs_days=None, commands_days=None, apply=False, actor='cli:maintenance'):
    for value in (jobs_days, commands_days):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError('Retention ages must be whole days of at least 1')
    result = {'mode': 'APPLY' if apply else 'DRY_RUN'}
    if jobs_days is not None:
        result['done_jobs'] = _prune(db, Job, Job.id, jobs_days, apply, actor)
    if commands_days is not None:
        result['commands'] = _prune(db, Command, Command.request_id, commands_days, apply, actor)
        result['commands']['warning'] = ('A pruned request ID no longer replays its recorded response; '
                                         'a retry older than the retention age executes as a new command.')
    result['maintenance_hints'] = hints(db)
    return result
