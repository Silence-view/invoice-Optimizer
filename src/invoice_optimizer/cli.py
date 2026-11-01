"""Reproducible entry points; synthetic runs never approve or send invoices."""
import argparse,json,time,resource
from pathlib import Path
from sqlalchemy import select,func,text
from .config import Settings
from .db import Database,canonical
from .models import Job,Invoice


def drain(db,send_alerts=True):
    from .worker import tick
    totals={'jobs':0,'emails':0,'uncertain_recovered':0}
    while True:
        result=tick(db,20,100 if send_alerts else 0)
        for k,v in result.items():totals[k]+=v
        if not result['jobs'] and not result['emails']:break
    return totals


def demo(db,customers=8,movements=2000,seed=42,period='2026-09',largest_customer_movements=None,scenario_profile='model-evidence'):
    """The demo defaults to 'model-evidence': every review case it creates carries a
    logistic-regression score. Production still treats a missing score as unknown."""
    from .synthetic import generate_dataset
    from .model_ops import direct_train
    from .ingestion import load_customers,ingest_file
    from .billing import create_invoice
    suffix='' if scenario_profile=='full' else '-'+scenario_profile
    dataset=db.settings.data_dir/'synthetic'/f'{period}-{seed}-{movements}{suffix}'
    manifest=generate_dataset(dataset,customers=customers,movements=movements,seed=seed,period=period,largest_customer_movements=largest_customer_movements,scenario_profile=scenario_profile)
    model=direct_train(db,dataset/'training.parquet',actor='cli:'+db.settings.actor)
    load_customers(db,dataset/'customers.json')
    started=time.perf_counter();batches=[]
    for source in ('A','B'):
        batches.append(ingest_file(db,dataset/f'source_{source.lower()}.csv',source,period,manifest['source_rows'][source],True))
    processed=drain(db)
    with db.transaction() as s:
        failed=[{'id':j.id,'kind':j.kind,'error':j.error} for j in s.scalars(select(Job).where(Job.status=='FAILED'))]
        inv=s.scalar(select(Invoice).where(Invoice.customer_id=='C0001',Invoice.period==period,Invoice.status.notin_(['FAILED','STALE'])))
    if failed:raise RuntimeError('Worker failures: '+canonical(failed))
    if inv is None:
        prepared=create_invoice(db,'C0001',period,f'demo-close-{period}-{seed}-{movements}{suffix}','synthetic-demo')
        drain(db)
        with db.transaction() as s:
            completed=s.get(Invoice,prepared['id']);prepared={'id':completed.id,'number':completed.number,'status':completed.status}
    else:prepared={'id':inv.id,'status':inv.status,'number':inv.number}
    from .api import dashboard
    receipt={'kind':'synthetic_end_to_end','synthetic':True,'invoice_auto_approved':False,'invoice_email_sent':False,
        'logical_movements_requested':movements,'customers':customers,'scenario_profile':scenario_profile,'source_batches':batches,'processed':processed,
        'elapsed_processing_seconds':round(time.perf_counter()-started,3),'prepared_invoice':prepared,'dashboard':dashboard(db),
        'model_version':model.get('model_version'),'memory_note':'RSS includes Python, model and PyArrow imports; not a production capacity certification.'}
    (db.settings.data_dir/'reports'/'demo-receipt.json').write_text(json.dumps(receipt,indent=2,default=str))
    return receipt


def format_command(args,settings):
    """Packs and profiles are files; these commands never touch the database."""
    from .formats import PackIntegrityError,ProfileError,build_presentation,list_packs,list_profiles,render_preview
    from .documents import InvoiceCompilationError,InvoiceValidationError
    try:
        if args.format_command=='list':
            return {'active_profile':settings.format_profile,'profile_dir':str(settings.profile_dir),
                    'packs':list_packs(),'profiles':list_profiles(settings.profile_dir)}
        if args.format_command=='validate':
            presentation,_=build_presentation(settings.profile_dir,args.profile)
            return {'valid':True,**{k:v for k,v in presentation.items() if k!='profile'},'resolved_profile':presentation['profile']}
        return render_preview(settings.profile_dir,args.profile,args.out,settings.compiler,itemized=args.itemized)
    except (ProfileError,PackIntegrityError,InvoiceValidationError,InvoiceCompilationError) as exc:
        print(json.dumps({'valid':False,'profile_id':getattr(args,'profile',None),'error':str(exc)},indent=2))
        raise SystemExit(1) from None


def main():
    p=argparse.ArgumentParser(prog='invoice-optimizer');sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('init-db');sub.add_parser('worker');sub.add_parser('tick')
    serve=sub.add_parser('serve');serve.add_argument('--host',default='127.0.0.1');serve.add_argument('--port',type=int,default=8000)
    for name in ('demo','generate','benchmark'):
        cmd=sub.add_parser(name);cmd.add_argument('--customers',type=int,default=8);cmd.add_argument('--movements',type=int,default=2000);cmd.add_argument('--seed',type=int,default=42);cmd.add_argument('--period',default='2026-09');cmd.add_argument('--largest-customer-movements',type=int)
        if name=='generate':cmd.add_argument('--output',type=Path,default=Path('var/synthetic/manual'))
        if name=='demo':
            from .synthetic import SCENARIO_PROFILES
            cmd.add_argument('--scenario-profile',choices=sorted(SCENARIO_PROFILES),default='model-evidence',help='Deliberate exceptions to inject; model-evidence omits cases without a regression score')
    sub.add_parser('inbox-scan',help='Ingest settled deliveries from var/inbox once (the worker does this automatically)')
    sub.add_parser('automation-status',help='Next month-end run, last automation reports, mail readiness')
    train=sub.add_parser('train');train.add_argument('file',type=Path)
    synthetic_train=sub.add_parser('train-synthetic',help='Queue a bounded, auditable synthetic model training run')
    synthetic_train.add_argument('--rows',type=int,default=30000);synthetic_train.add_argument('--seed',type=int,default=42)
    synthetic_train.add_argument('--period',default='2026-09');synthetic_train.add_argument('--request-id',required=True)
    ingest=sub.add_parser('ingest');ingest.add_argument('file',type=Path);ingest.add_argument('--source',required=True,choices=['A','B']);ingest.add_argument('--period',required=True);ingest.add_argument('--expected-rows',type=int);ingest.add_argument('--complete',action='store_true');ingest.add_argument('--sheet')
    cust=sub.add_parser('load-customers');cust.add_argument('file',type=Path)
    close=sub.add_parser('close');close.add_argument('--customer',required=True);close.add_argument('--period',required=True);close.add_argument('--request-id',required=True)
    export=sub.add_parser('export');export.add_argument('--period',required=True);export.add_argument('--output',type=Path,default=Path('var/exports'))
    insights=sub.add_parser('insights',help='Read-only statistical signals for one period')
    insights.add_argument('--period',required=True);insights.add_argument('--customer');insights.add_argument('--limit',type=int,default=20)
    insights.add_argument('--json',type=Path,dest='json_path')
    operator=sub.add_parser('operator-create',help='Create a personal operator; prompts privately for a password')
    operator.add_argument('--username',required=True);operator.add_argument('--display-name',required=True)
    operator.add_argument('--role',choices=('admin','reviewer'),default='reviewer')
    keep=sub.add_parser('maintenance',help='Retention for DONE jobs and old command records; dry-run unless --apply')
    keep.add_argument('--prune-done-jobs-older-than',type=int,metavar='DAYS')
    keep.add_argument('--prune-commands-older-than',type=int,metavar='DAYS')
    keep.add_argument('--apply',action='store_true',help='Delete eligible rows; default only counts them')
    fmt=sub.add_parser('format',help='Invoice format packs and profiles').add_subparsers(dest='format_command',required=True)
    fmt.add_parser('list',help='Installed packs and profiles with validation status')
    fmt.add_parser('validate',help='Validate one profile and print its resolved form').add_argument('profile')
    preview=fmt.add_parser('preview',help='Render a synthetic sample PDF (and page-1 PNG) with a profile')
    preview.add_argument('--profile',required=True);preview.add_argument('--out',type=Path,required=True)
    preview.add_argument('--itemized',action='store_true',help='Include itemised movement detail pages')
    args=p.parse_args();settings=Settings.from_env().prepare()
    if args.command=='format':
        print(json.dumps(format_command(args,settings),indent=2,default=str));return
    if args.command=='serve':
        import uvicorn
        from .api import create_app
        # OAuth callbacks contain short-lived codes; never write query strings
        # into access logs. Domain events remain in the application audit trail.
        uvicorn.run(create_app(settings),host=args.host,port=args.port,log_level='info',access_log=False);return
    db=Database(settings,role='worker' if args.command=='worker' else 'cli')
    try:
        if args.command=='init-db':db.init();result={'schema_version':8,'status':'ready'}
        elif args.command=='maintenance':
            from .maintenance import maintenance
            result=maintenance(db,jobs_days=args.prune_done_jobs_older_than,commands_days=args.prune_commands_older_than,
                apply=args.apply,actor='cli:'+settings.actor)
        elif args.command=='operator-create':
            from getpass import getpass
            from .operators import create_operator, operator_identity
            password=getpass('Personal password: ')
            if password!=getpass('Confirm personal password: '):raise ValueError('Passwords do not match')
            with db.transaction() as s:
                operator=create_operator(s,username=args.username,display_name=args.display_name,
                    password=password,role=args.role,actor='cli-admin')
                result={'created':True,**operator_identity(operator)}
        elif args.command=='worker':
            from .worker import run_forever
            run_forever(db);return
        elif args.command=='inbox-scan':
            from .inbox import scan_inbox
            result=scan_inbox(db)
        elif args.command=='automation-status':
            from .scheduler import automation_status
            result=automation_status(db)
        elif args.command=='tick':
            from .worker import tick
            result=tick(db)
        elif args.command=='generate':
            from .synthetic import generate_dataset
            result=generate_dataset(args.output,args.customers,args.movements,args.seed,args.period,largest_customer_movements=args.largest_customer_movements)
        elif args.command=='train':
            from .model_ops import direct_train
            result=direct_train(db,args.file,actor='cli:'+settings.actor)
        elif args.command=='train-synthetic':
            from .model_ops import queue_training
            result=queue_training(db,args.rows,args.seed,args.period,args.request_id,'cli:'+settings.actor)
        elif args.command=='load-customers':
            from .ingestion import load_customers
            result={'customers_loaded':load_customers(db,args.file)}
        elif args.command=='ingest':
            from .ingestion import ingest_file
            result=ingest_file(db,args.file,args.source,args.period,args.expected_rows,args.complete,args.sheet,actor='cli:'+settings.actor)
        elif args.command=='close':
            from .billing import create_invoice
            result=create_invoice(db,args.customer,args.period,args.request_id,settings.actor)
        elif args.command=='export':
            from .analytics import export_analytics
            result=export_analytics(db,args.period,args.output)
        elif args.command=='insights':
            from .insights import report
            result=report(db,args.period,args.customer,args.limit)
            if args.json_path:
                # Publish whole files only; a reader never sees a partial report.
                # A per-run temporary name keeps concurrent runs from sharing one inode.
                from uuid import uuid4
                body=json.dumps(result,indent=2,sort_keys=True)+'\n';target=args.json_path.resolve()
                target.parent.mkdir(parents=True,exist_ok=True);temporary=target.with_name(f'.{target.name}.{uuid4().hex}.tmp')
                try:temporary.write_text(body);temporary.replace(target)
                finally:temporary.unlink(missing_ok=True)
                result={'report_path':str(target),'bytes':len(body.encode()),'generated_at':result['generated_at'],'sections':sorted(result['sections'])}
        elif args.command=='demo':
            db.init();result=demo(db,args.customers,args.movements,args.seed,args.period,args.largest_customer_movements,args.scenario_profile)
        elif args.command=='benchmark':
            from .benchmark import run_benchmark
            result=run_benchmark(settings,args.customers,args.movements,args.seed,args.period,args.largest_customer_movements)
        print(json.dumps(result,indent=2,default=str))
    finally:db.close()
