from contextlib import contextmanager
from hashlib import sha256
import json
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from .models import Base, Audit, Command, Counter
from collections import defaultdict
from sqlalchemy.dialects.postgresql import insert as pg_insert

class Conflict(ValueError): pass
class Blocked(ValueError): pass
class NotFound(ValueError): pass


def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=str)
def digest(value): return sha256(canonical(value).encode()).hexdigest()

def lock(session, key):
    n=int.from_bytes(sha256(key.encode()).digest()[:8],'big',signed=True)
    session.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':n})

def counter(session, name, delta):
    session.info.setdefault("counter_deltas",defaultdict(int))[name]+=delta

def change_state(session, movement, new_state):
    if movement.state!=new_state:
        counter(session,"state:"+movement.state,-1)
        counter(session,"state:"+new_state,1)
        movement.state=new_state

def audit(session, actor, action, entity_id, details=None):
    if not isinstance(actor,str) or not actor.strip() or len(actor)>120:
        raise ValueError('A non-empty audit actor of at most 120 characters is required')
    session.add(Audit(actor=actor,action=action,entity_id=str(entity_id),details=details or {}))

def replay(session, request_id, action, payload):
    if not request_id or len(request_id)>120: raise ValueError('A request ID of 1..120 characters is required')
    lock(session,'command:'+request_id)
    old=session.get(Command,request_id)
    if old:
        if old.action!=action or old.payload_hash!=digest(payload): raise Conflict('Request ID was already used for different content')
        return old.response
    return None

def record_command(session, request_id, action, payload, response):
    session.add(Command(request_id=request_id,action=action,payload_hash=digest(payload),response=response))
    return response

ROLES=('api','worker','cli')
# lock_timeout also bounds pg_advisory_xact_lock waits; 0 keeps PostgreSQL's
# unbounded default. Options are appended to any URL options (search_path).
TIMEOUT_OPTIONS=(('statement_timeout','db_statement_timeout_ms'),('lock_timeout','db_lock_timeout_ms'),
                 ('idle_in_transaction_session_timeout','db_idle_tx_timeout_ms'))

def engine_for(settings, url, role, pool_size=None):
    url=make_url(url)
    # API only: one .env feeds every process, and on the worker a lock wait that
    # times out turns a job (and a rendering invoice) FAILED; init-db and a large
    # freeze are single long transactions.
    options=' '.join([url.query.get('options','')]+[f'-c {name}={int(getattr(settings,field))}'
        for name,field in TIMEOUT_OPTIONS if getattr(settings,field) and role.startswith('api')]).strip()
    url=url.update_query_dict({'options':options}) if options else url
    return create_engine(url,pool_pre_ping=True,pool_size=pool_size or settings.db_pool_size,
        max_overflow=settings.db_max_overflow,pool_recycle=settings.db_pool_recycle_seconds,
        connect_args={'application_name':'invoice-optimizer-'+role})

def is_timeout(exc):
    """lock_timeout (55P03) or statement_timeout (57014): rolled back, retryable."""
    return getattr(getattr(exc,'orig',None),'sqlstate',None) in ('55P03','57014')

class Database:
    def __init__(self, settings, role='cli'):
        if not settings.database_url.startswith('postgresql'): raise ValueError('PostgreSQL is required; no SQLite concurrency substitute')
        if role not in ROLES: raise ValueError('Unknown database role')
        self.settings=settings;self.role=role
        self.engine=engine_for(settings,settings.database_url,role)
        self.Session=sessionmaker(self.engine,expire_on_commit=False)
        # Optional replica for list/report reads; never for close/approve/dispatch
        # validation, which must see the primary. Default: the same engine.
        self.read_engine=(engine_for(settings,settings.database_read_url,role+'-read')
                          if settings.database_read_url else self.engine)
        self.ReadSession=sessionmaker(self.read_engine,expire_on_commit=False)

    @contextmanager
    def read_transaction(self):
        """READ ONLY even on the primary, so replica routing cannot change behaviour."""
        with self.ReadSession.begin() as s:
            s.execute(text('SET TRANSACTION READ ONLY'))
            yield s

    @contextmanager
    def transaction(self):
        with self.Session.begin() as s:
            yield s
            for name,delta in sorted(s.info.get("counter_deltas",{}).items()):
                if delta:
                    stmt=pg_insert(Counter).values(name=name,value=delta)
                    s.execute(stmt.on_conflict_do_update(index_elements=["name"],set_={"value":Counter.value+delta}))

    def init(self):
        # Register separately defined models before creating tables. Local imports
        # avoid making domain modules a dependency of transaction helpers.
        from . import operators, review_history, model_ops  # noqa: F401
        with self.engine.begin() as c:
            c.execute(text('SELECT pg_advisory_xact_lock(918204660)'))
            Base.metadata.create_all(c)
            c.execute(text('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())'))
            c.execute(text('INSERT INTO schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING'))
            c.execute(text('CREATE SEQUENCE IF NOT EXISTS invoice_number_seq START 1'))
            c.execute(text('ALTER TABLE quarantine ADD COLUMN IF NOT EXISTS validated_customer_id VARCHAR(80) REFERENCES customers(id)'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_quarantine_validated_customer_id ON quarantine(validated_customer_id)'))
            c.execute(text('INSERT INTO schema_migrations(version) VALUES(2) ON CONFLICT DO NOTHING'))
            c.execute(text('ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claim_token VARCHAR(64)'))
            c.execute(text('INSERT INTO schema_migrations(version) VALUES(3) ON CONFLICT DO NOTHING'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_jobs_claim ON jobs(created_at) WHERE status = \'READY\''))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_outbox_claim ON outbox(created_at) WHERE status = \'READY\''))
            # Compact invoice provenance tables are created above. These indexes
            # support customer filters without reading raw files into the UI.
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_cases_customer_status_created ON review_cases(customer_id, status, created_at DESC, id)'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_invoices_customer_created ON invoices(customer_id, created_at DESC, id)'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_source_customer_id ON source_records(customer_id, id)'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_batch_record_reverse ON batch_records(record_id, batch_id)'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_quarantine_batch_customer ON quarantine(batch_id, validated_customer_id)'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_outbox_case ON outbox(case_id)'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_outbox_invoice ON outbox(invoice_id)'))
            c.execute(text('INSERT INTO schema_migrations(version) VALUES(4) ON CONFLICT DO NOTHING'))
            c.execute(text('ALTER TABLE review_cases ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_audit_entity_history ON audit_events(entity_id, id DESC)'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_review_file_hash ON review_events(source_file_sha256) WHERE source_file_sha256 IS NOT NULL'))
            c.execute(text('''DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_audit_actor_present'
                    AND conrelid = 'audit_events'::regclass) THEN
                    ALTER TABLE audit_events ADD CONSTRAINT ck_audit_actor_present
                        CHECK (length(btrim(actor)) > 0) NOT VALID;
                END IF;
            END $$'''))
            # Ordinary SQL cannot silently rewrite or remove recorded evidence.
            # A database owner can still disable triggers: this is an application
            # integrity control, not independently tamper-proof archival storage.
            c.execute(text('''CREATE OR REPLACE FUNCTION invoice_reject_ledger_mutation()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'Table % is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
                END;
                $$'''))
            for table in ('audit_events','review_events'):
                c.execute(text(f'DROP TRIGGER IF EXISTS protect_ledger_rows ON {table}'))
                c.execute(text(f'CREATE TRIGGER protect_ledger_rows BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION invoice_reject_ledger_mutation()'))
                c.execute(text(f'DROP TRIGGER IF EXISTS protect_ledger_truncate ON {table}'))
                c.execute(text(f'CREATE TRIGGER protect_ledger_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION invoice_reject_ledger_mutation()'))
            c.execute(text('INSERT INTO schema_migrations(version) VALUES(5) ON CONFLICT DO NOTHING'))
            c.execute(text('ALTER TABLE assessments ADD COLUMN IF NOT EXISTS explanation JSONB'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_jobs_kind_ready ON jobs(kind, created_at) WHERE status = \'READY\''))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_cases_reason_status_created ON review_cases(reason, status, created_at DESC, id)'))
            c.execute(text('CREATE INDEX IF NOT EXISTS ix_movements_external_prefix ON movements (lower(external_id) text_pattern_ops)'))
            c.execute(text('INSERT INTO schema_migrations(version) VALUES(6) ON CONFLICT DO NOTHING'))
            c.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_running_lease ON jobs(lease_until, created_at, id) WHERE status = 'RUNNING'"))
            c.execute(text('INSERT INTO schema_migrations(version) VALUES(7) ON CONFLICT DO NOTHING'))
            self._migration_8(c)

    @staticmethod
    def _migration_8(c):
        # Plain (non-CONCURRENT) builds: init is one locked transaction. On a
        # large live database pre-create these CONCURRENTLY with the same names.
        # Each dropped index is a prefix/duplicate of a retained index or has no
        # query in src; movements.state, jobs.status and review_cases.status also
        # stop HOT updates on every state transition.
        for name in ('ix_source_latest','ix_movement_versions_movement_id','ix_assessments_version_id',
                     'ix_movements_customer_id','ix_movements_state','ix_invoices_customer_id',
                     'ix_review_events_case_id','ix_review_events_period','ix_review_events_occurred_at',
                     'ix_jobs_status','ix_review_cases_status','ix_source_records_movement_id'):
            c.execute(text(f'DROP INDEX IF EXISTS {name}'))
        for ddl in (
            # Close coverage scans one source-period; DISTINCT ON latest revision per movement.
            'ix_source_period_record ON source_records(source, period, record_id, revision)',
            'ix_source_movement_latest ON source_records(movement_id, source, record_id, revision DESC)',
            "ix_cases_open_version ON review_cases(version_id) WHERE status = 'OPEN'",
            "ix_cases_open_scope ON review_cases(customer_id, period) WHERE status = 'OPEN'",
            'ix_cases_status_created ON review_cases(status, created_at DESC, id)',
            'ix_quarantine_unresolved ON quarantine(period, validated_customer_id) WHERE resolved_by_batch IS NULL',
            "ix_jobs_unfinished ON jobs(period, kind, status) WHERE status <> 'DONE'",
            'ix_outbox_created ON outbox(created_at DESC, id)',
            'ix_invoices_created ON invoices(created_at DESC, id)',
            # Append-mostly time columns: retention and date-range scans.
            'brin_audit_created ON audit_events USING brin(created_at)',
            'brin_review_events_occurred ON review_events USING brin(occurred_at)',
            'brin_source_received ON source_records USING brin(received_at)',
            'brin_versions_created ON movement_versions USING brin(created_at)',
            'brin_assessments_evaluated ON assessments USING brin(evaluated_at)',
            'brin_jobs_created ON jobs USING brin(created_at)',
            'brin_commands_created ON commands USING brin(created_at)'):
            c.execute(text('CREATE INDEX IF NOT EXISTS '+ddl))
        for ddl in ('line_format VARCHAR(20)','movement_count BIGINT','movement_vat_policy VARCHAR(160)'):
            c.execute(text('ALTER TABLE invoices ADD COLUMN IF NOT EXISTS '+ddl))
        # Same expressions the invoice list used; the document stays untouched.
        c.execute(text("""UPDATE invoices SET line_format=COALESCE(document->>'line_format','SUMMARY'),
            movement_count=CASE WHEN document->>'movement_count' ~ '^[0-9]{1,18}$' THEN (document->>'movement_count')::bigint END,
            movement_vat_policy=document->'movement_vat'->>'policy' WHERE line_format IS NULL"""))
        c.execute(text('INSERT INTO schema_migrations(version) VALUES(8) ON CONFLICT DO NOTHING'))

    def close(self):
        self.engine.dispose()
        if self.read_engine is not self.engine:self.read_engine.dispose()
