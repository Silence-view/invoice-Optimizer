#!/usr/bin/env python3
"""Read-only consistency receipt for the actual local application dataset."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from sqlalchemy import select,func,text
from invoice_optimizer.config import Settings
from invoice_optimizer.db import Database,digest
from invoice_optimizer.models import Movement,Counter,Invoice,InvoiceMember,Allocation,Batch,Job,Assessment,Outbox,DeliveryAttempt
from invoice_optimizer.review_history import ReviewEvent
from invoice_optimizer.operators import Operator
from invoice_optimizer.storage import ObjectStore

ROOT=Path(__file__).resolve().parents[1]


def main():
    settings=Settings.from_env().prepare();db=Database(settings)
    store=ObjectStore(settings.data_dir/'objects')
    receipt={'checked_at':datetime.now(timezone.utc).isoformat(),'synthetic':settings.synthetic,'provider':settings.provider,'checks':{}}
    try:
        with db.transaction() as s:
            s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            states=dict(s.execute(select(Movement.state,func.count()).group_by(Movement.state)).all())
            counters=dict(s.execute(select(Counter.name,Counter.value)).all())
            assert counters.get('movements',0)==sum(states.values())
            for state in set(states)|{k[6:] for k in counters if k.startswith('state:')}:
                assert counters.get('state:'+state,0)==states.get(state,0)
            receipt['checks']['transactional_counters_equal_sql']=True
            receipt['movement_states']=states
            receipt['jobs']=dict(s.execute(select(Job.status,func.count()).group_by(Job.status)).all())
            receipt['assessments']=[dict(kind=k,status=st,count=n) for k,st,n in s.execute(select(Assessment.kind,Assessment.status,func.count()).group_by(Assessment.kind,Assessment.status))]
            receipt['outbox']=[dict(kind=k,status=st,count=n) for k,st,n in s.execute(select(Outbox.kind,Outbox.status,func.count()).group_by(Outbox.kind,Outbox.status))]
            receipt['delivery_attempts']=s.scalar(select(func.count()).select_from(DeliveryAttempt))
            batches=list(s.scalars(select(Batch)))
            for batch in batches:store.path(batch.object_key,verify=True)
            receipt['checks']['retained_source_hashes_verified']=len(batches)
            invoices=[]
            for inv in s.scalars(select(Invoice)):
                store.path(inv.snapshot_key,verify=True)
                total=s.scalar(select(func.sum(InvoiceMember.amount)).where(InvoiceMember.invoice_id==inv.id))
                assert total==inv.net_total
                if inv.pdf_key:
                    store.path(inv.pdf_key,verify=True)
                    assert inv.pdf_key==inv.pdf_sha256 and digest(inv.package)==inv.fingerprint
                invoices.append(dict(id=inv.id,number=inv.number,status=inv.status,net=str(inv.net_total),vat=str(inv.vat_total),gross=str(inv.gross_total),approved=inv.approved_at is not None,pdf_sha256=inv.pdf_sha256,fingerprint=inv.fingerprint))
            receipt['invoices']=invoices
            receipt['checks']['invoice_membership_and_package_hashes_verified']=len(invoices)
            receipt['allocations']=s.scalar(select(func.count()).select_from(Allocation))
            receipt['review_identity']={
                'schema_version':s.scalar(text('SELECT max(version) FROM schema_migrations')),
                'personal_accounts':s.scalar(select(func.count()).select_from(Operator)),
                'review_events':s.scalar(select(func.count()).select_from(ReviewEvent)),
                'legacy_reviewed_cases_without_new_timestamp':s.scalar(text('SELECT count(*) FROM review_cases WHERE reviewed_by IS NOT NULL AND reviewed_at IS NULL')),
            }
            assert s.scalar(text('''SELECT count(*) FROM review_cases c WHERE c.reviewed_at IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM review_events e WHERE e.case_id=c.id AND e.actor_id=c.reviewed_by AND e.occurred_at=c.reviewed_at)'''))==0
            receipt['checks']['new_review_projections_have_matching_events']=True
            protected=s.scalar(text('''SELECT count(*) FROM pg_trigger
                WHERE tgrelid IN ('review_events'::regclass,'audit_events'::regclass)
                AND tgname IN ('protect_ledger_rows','protect_ledger_truncate') AND tgenabled='O' '''))
            assert protected==4
            assert s.scalar(text("SELECT to_regclass('ix_review_file_hash') IS NOT NULL"))
            receipt['checks']['review_and_audit_database_guards_enabled']=True
            receipt['checks']['retained_review_lookup_index_present']=True
    finally:db.close()
    tests=ROOT/'output/verification/pytest.xml'
    if tests.exists():
        suites=ET.parse(tests).getroot().iter('testsuite')
        receipt['regression_tests']=[{k:suite.attrib[k] for k in ('tests','failures','errors','skipped','time') if k in suite.attrib} for suite in suites]
    files=[]
    for directory in ('src','tests','scripts'):
        for path in sorted((ROOT/directory).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix in ('.py','.js','.cjs','.css','.html','.j2','.sh','.mjs'):
                files.append(path)
    files.extend(ROOT/name for name in ('pyproject.toml','requirements.lock.txt','Dockerfile','compose.yaml'))
    receipt['candidate_files']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    receipt['status']='PASS'
    receipt['scope']='Current local synthetic dataset consistency and artifact hashes; not production, scale, backup, model effectiveness or delivery certification.'
    output=ROOT/'output/verification/local-acceptance.json';output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps({'status':receipt['status'],'receipt':str(output),'movements':sum(receipt['movement_states'].values()),'invoices':receipt['invoices'],'tests':receipt.get('regression_tests')},indent=2))


if __name__=='__main__':main()
