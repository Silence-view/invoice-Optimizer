"""Bounded file ingestion. The demo contract must be replaced for real sources."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from itertools import islice
import csv, json, re
from sqlalchemy import select, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from .models import Customer, Batch, SourceRecord, Quarantine, Movement, Job, ReviewCase, Outbox, BatchRecord, now
from .db import digest, audit, lock, Conflict, canonical, counter
from .storage import ObjectStore

REQUIRED=('record_id','revision','movement_id','customer_id','event_at','period','currency','amount','description','supplier_id')

def money(value):
    if isinstance(value,bool): raise ValueError('Boolean amount is invalid')
    try: result=Decimal(str(value))
    except (InvalidOperation,TypeError): raise ValueError('Invalid decimal amount')
    if not result.is_finite() or result<0 or result>Decimal('9999999999999999.99') or result.as_tuple().exponent < -2:
        raise ValueError('Amount requires a nonnegative finite Decimal with at most two decimal places')
    return result.quantize(Decimal('.01'))

def normalize(row, period):
    data={str(k):v for k,v in row.items() if k is not None}
    for key in REQUIRED:
        if data.get(key) is None or str(data[key]).strip()=='': raise ValueError('Missing '+key)
    for key in ('record_id','movement_id','customer_id','supplier_id'):
        data[key]=str(data[key]).strip()
        if len(data[key])>160 or '\x00' in data[key]: raise ValueError('Invalid '+key)
    if not re.fullmatch(r'[1-9][0-9]*',str(data['revision'])): raise ValueError('Revision must be a positive integer')
    data['revision']=int(data['revision'])
    data['amount']=str(money(data['amount']))
    if data.get('expected_amount') is not None and str(data['expected_amount']).strip():
        data['expected_amount']=str(money(data['expected_amount']))
    elif 'expected_amount' in data:data['expected_amount']=''
    for key in ('customer_age_days','supplier_age_days','cross_border'):
        if key in data:data[key]='' if data[key] is None else str(data[key]).strip()
    data['currency']=str(data['currency']).upper()
    if data['currency']!='GBP': raise ValueError('Demo contract supports GBP only')
    try: ts=datetime.fromisoformat(str(data['event_at']).replace('Z','+00:00'))
    except ValueError: raise ValueError('Invalid event_at')
    if ts.tzinfo is None: raise ValueError('event_at needs a timezone')
    ts=ts.astimezone(timezone.utc)
    if data['period']!=period or ts.strftime('%Y-%m')!=period: raise ValueError('Event and declared period must agree in UTC')
    data['event_at']=ts.isoformat()
    data['description']=str(data['description'])
    if len(data['description'])>1000: raise ValueError('Description too long')
    # Excel numeric money crosses a decimal-text boundary here, before ML.
    # Nonmonetary ML fields retain strict feature validation; truth is excluded.
    allowed=set(REQUIRED)|{'expected_amount','customer_age_days','supplier_age_days','cross_border'}
    return {k:v for k,v in data.items() if k in allowed}

def iter_rows(path, sheet=None, batch_size=1000, format_hint=None):
    path=Path(path)
    suffix=format_hint or path.suffix.lower()
    if suffix=='.csv':
        with path.open(newline='',encoding='utf-8-sig') as f:
            reader=csv.DictReader(f)
            if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames): raise ValueError('Missing or duplicate CSV headers')
            yield from reader
    elif suffix=='.parquet':
        import pyarrow.parquet as pq
        for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_size): yield from batch.to_pylist()
    elif suffix=='.xlsx':
        from openpyxl import load_workbook
        handle=path.open('rb')
        wb=load_workbook(handle,read_only=True,data_only=False)
        try:
            if sheet not in wb.sheetnames: raise ValueError('Explicit valid XLSX sheet name required')
            rows=wb[sheet].iter_rows(values_only=True); headers=next(rows)
            if any(x is None or not str(x).strip() for x in headers) or len(set(headers))!=len(headers):raise ValueError('Missing or duplicate XLSX headers')
            for values in rows:
                if all(x is None for x in values): continue
                if any(isinstance(x,str) and x.startswith('=') for x in values): raise ValueError('Source formulas are not evaluated; export values first')
                yield dict(zip(headers,values))
        finally: wb.close();handle.close()
    else: raise ValueError('Supported input types: CSV, Parquet, XLSX')

def persist_cases(s, cases, messages):
    """Insert cases, then one alert per newly created case, in bounded statements.

    ON CONFLICT ... RETURNING keeps a replayed case from creating another alert.
    Columns are uniform per call, so ORM defaults apply per row as in s.add().
    """
    values=list(cases.values())
    for start in range(0,len(values),500):
        created=list(s.scalars(pg_insert(ReviewCase).values(values[start:start+500])
            .on_conflict_do_nothing(index_elements=['id']).returning(ReviewCase.id)))
        if created:s.execute(insert(Outbox),[messages[key] for key in created])


def load_customers(db, path):
    data=json.loads(Path(path).read_text())
    if isinstance(data,dict): data=data.get('customers',[])
    with db.transaction() as s:
        for row in data:
            for field in ('email','alert_email'):
                if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',row.get(field,'')): raise ValueError('Invalid customer '+field)
            values={k:row.get(k,'') for k in ('id','legal_name','address','email','alert_email','vat_number')}
            if isinstance(values['address'],list): values['address']='\n'.join(values['address'])
            if isinstance(values['address'],dict): values['address']='\n'.join(str(v) for v in values['address'].values())
            s.execute(pg_insert(Customer).values(**values).on_conflict_do_nothing(index_elements=['id']))
        audit(s,'system','customers_loaded','master',{'count':len(data)})
    return len(data)

def ingest_file(db, path, source, period, expected_rows=None, complete=False, sheet=None, original_name=None, actor='ingestion'):
    if not isinstance(actor,str) or not actor.strip() or len(actor)>120:raise ValueError('An ingestion actor is required')
    if source not in ('A','B') or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',period): raise ValueError('Invalid source or period')
    if expected_rows is not None and (type(expected_rows) is not int or expected_rows < 0):raise ValueError('Expected rows must be a nonnegative integer')
    if complete and expected_rows is None: raise ValueError('Complete delivery requires an explicit expected row count')
    settings=db.settings; store=ObjectStore(settings.data_dir/'objects'); key=store.put_file(path)
    bid=digest([source,period,key,sheet]); name=Path(original_name or path).name
    with db.transaction() as s:
        lock(s,'period:'+period)
        old=s.get(Batch,bid)
        if old:
            if old.status=='COMPLETE':
                if old.expected_rows!=expected_rows or old.declared_complete!=complete:raise Conflict('Identical file was already imported with different manifest semantics')
                return {'batch_id':bid,'status':'COMPLETE','replayed':True,'rows':old.row_count}
            raise Conflict('This file has an incomplete prior batch. Inspect its failure before retrying with a new corrected delivery.')
        from .operations import check_ingestion_admission
        admission = check_ingestion_admission(s, settings, expected_rows)
        s.add(Batch(id=bid,source=source,period=period,object_key=key,sha256=key,original_name=name,expected_rows=expected_rows,declared_complete=complete))
        audit(s,actor,'batch_started',bid,{'source':source,'period':period,'original_name':name,'file_sha256':key,'expected_rows':expected_rows,'declared_complete':complete,'sheet':sheet,'queue_admission':admission})
    count=accepted=rejected=duplicates=0
    try:
        iterator=iter_rows(store.path(key,verify=True),sheet,settings.batch_size,Path(path).suffix.lower())
        while rows:=list(islice(iterator,settings.batch_size)):
            with db.transaction() as s:
                lock(s,'source:'+source)
                referenced_customers={str(row.get('customer_id','')).strip() for row in rows}
                customers={x.id:x for x in s.scalars(select(Customer).where(Customer.id.in_(referenced_customers)))}
                clean={}; quarantine=[]; this_dup=0
                for n,row in enumerate(rows,start=count+1):
                    try:
                        payload=normalize(row,period)
                        if payload['customer_id'] not in customers: raise ValueError('Unknown customer_id')
                        rid=digest([source,payload['record_id'],payload['revision']])
                        mid=digest([payload['customer_id'],payload['movement_id']])
                        record={'id':rid,'batch_id':bid,'source':source,'record_id':payload['record_id'],'revision':payload['revision'],'movement_id':mid,'customer_id':payload['customer_id'],'period':period,'payload_hash':digest(payload),'payload':payload}
                        if rid in clean:
                            if clean[rid]['payload_hash']!=record['payload_hash']: raise ValueError('Conflicting duplicate source key/revision')
                            this_dup+=1
                        else: clean[rid]=record
                    except (ValueError,TypeError) as exc:
                        quarantine.append({'id':digest([bid,n]),'batch_id':bid,'period':period,'row_number':n,'reason':str(exc),'payload':json.loads(canonical(row))})
                existing={x.id:x for x in s.scalars(select(SourceRecord).where(SourceRecord.id.in_(list(clean))))} if clean else {}
                # A revised source record may not switch customer/movement identity in this contract.
                prior={}
                if clean:
                    for x in s.scalars(select(SourceRecord).where(SourceRecord.source==source,SourceRecord.record_id.in_([r['record_id'] for r in clean.values()])).distinct(SourceRecord.record_id).order_by(SourceRecord.record_id,SourceRecord.revision.desc(),SourceRecord.id)):
                        prior[x.record_id]=x
                identities={}
                for row in clean.values(): identities.setdefault(row['record_id'],set()).add((row['customer_id'],row['movement_id'],row['period']))
                known_movements={m.id:m for m in s.scalars(select(Movement).where(Movement.id.in_([r['movement_id'] for r in clean.values()])))} if clean else {}
                new=[]
                for rid,row in clean.items():
                    old=existing.get(rid)
                    reason=None
                    if len(identities[row['record_id']])>1: reason='Source revisions disagree on business identity within this delivery'
                    elif row['movement_id'] in known_movements and known_movements[row['movement_id']].period!=period: reason='Movement identity cannot move between accounting periods'
                    if reason: pass
                    elif old:
                        if old.payload_hash==row['payload_hash']: this_dup+=1; continue
                        reason='Source key/revision reused with changed payload'
                    elif row['record_id'] in prior and (prior[row['record_id']].movement_id!=row['movement_id'] or prior[row['record_id']].period!=row['period']): reason='Source revision changed business identity or period'
                    if reason:
                        quarantine.append({'id':digest([bid,rid,'conflict']),'batch_id':bid,'period':period,'row_number':count+1,'reason':reason,'payload':row['payload']})
                    else: new.append(row)
                customer_ids=sorted({r['customer_id'] for r in new})
                for cid in customer_ids: lock(s,'customer-period:'+cid+':'+period)
                moves={r['movement_id']:{'id':r['movement_id'],'external_id':r['payload']['movement_id'],'customer_id':r['customer_id'],'period':period,'current_version':0,'state':'PENDING'} for r in new}
                if moves:
                    inserted=list(s.scalars(pg_insert(Movement).values(list(moves.values())).on_conflict_do_nothing(index_elements=['id']).returning(Movement.id)))
                    counter(s,'movements',len(inserted));counter(s,'state:PENDING',len(inserted))
                    actual={m.id:m for m in s.scalars(select(Movement).where(Movement.id.in_(list(moves))).with_for_update())}
                    invalid={mid for mid,m in actual.items() if m.period!=period}
                    for r in new:
                        if r['movement_id'] in invalid:quarantine.append({'id':digest([bid,r['id'],'period-conflict']),'batch_id':bid,'period':period,'row_number':count+1,'reason':'Movement already belongs to another period','payload':r['payload']})
                    new=[r for r in new if r['movement_id'] not in invalid]
                    moves={mid:r for mid,r in moves.items() if mid not in invalid}
                if new: s.execute(insert(SourceRecord),new)
                membership=[{'batch_id':bid,'record_id':r['id']} for r in new]+[{'batch_id':bid,'record_id':rid} for rid,r in clean.items() if rid in existing and existing[rid].payload_hash==r['payload_hash']]
                if membership:s.execute(pg_insert(BatchRecord).values(membership).on_conflict_do_nothing())
                if quarantine:
                    for q in quarantine:q['validated_customer_id']=q['payload'].get('customer_id') if q['payload'].get('customer_id') in customers else None
                    s.execute(insert(Quarantine),quarantine)
                    cases={};messages={}
                    for q in quarantine:
                        cid=q['payload'].get('customer_id'); customer=customers.get(cid)
                        case_id=digest(['quarantine',q['id']]); owner=customer.alert_email if customer else 'operations@example.test'
                        cases[case_id]={'id':case_id,'dedup_key':'quarantine:'+q['id'],'customer_id':cid if customer else None,'period':period,'reason':'QUARANTINE: '+q['reason'][:220],'owner':owner}
                        messages[case_id]={'dedup_key':'flag:'+case_id,'kind':'ALERT','case_id':case_id,'payload':{'to':[owner],'from':'operations@example.test','subject':'Source data requires review','body':'Case '+case_id+' requires review. '+q['reason']}}
                    persist_cases(s,cases,messages)
                if moves:
                    movement_ids=list(moves)
                    for offset in range(0,len(movement_ids),settings.worker_batch_size):
                        s.add(Job(dedup_key=f'ingest:{bid}:{count}:{offset}',kind='ASSESS',period=period,
                                  payload={'movement_ids':movement_ids[offset:offset+settings.worker_batch_size]}))
                    # Prevent approval of a prior unissued snapshot as soon as new data arrives.
                    from .models import Invoice
                    for inv in s.scalars(select(Invoice).where(Invoice.customer_id.in_(customer_ids),Invoice.period==period,Invoice.issued_at.is_(None),Invoice.status.in_(['BUILDING','DRAFT','APPROVED']))):
                        inv.status='STALE'; inv.approved_at=None; inv.approved_by=None
                accepted+=len(new); rejected+=len(quarantine); duplicates+=this_dup; count+=len(rows)
                batch=s.get(Batch,bid); batch.row_count=count; batch.accepted_count=accepted; batch.rejected_count=rejected; batch.duplicate_count=duplicates
                audit(s,actor,'batch_progress',bid,{'rows':count,'accepted':accepted,'quarantined':rejected})
        if expected_rows is not None and expected_rows!=count: raise ValueError(f'Manifest row count mismatch: expected {expected_rows}, read {count}')
        with db.transaction() as s:
            lock(s,'period:'+period)
            batch=s.get(Batch,bid); batch.status='COMPLETE'; batch.finished_at=now()
            audit(s,actor,'batch_complete',bid,{'rows':count,'accepted':accepted,'quarantined':rejected,'duplicates':duplicates})
    except Exception as exc:
        with db.transaction() as s:
            batch=s.get(Batch,bid); batch.status='FAILED'; batch.error=str(exc)[:2000]
            audit(s,actor,'batch_failed',bid,{'error':str(exc)[:500]})
        raise
    return {'batch_id':bid,'status':'COMPLETE','rows':count,'accepted':accepted,'quarantined':rejected,'duplicates':duplicates}


def supersede_batch(db,old_id,new_id,reason,request_id,actor):
    """Explicit operator confirmation; retain historical observations and findings."""
    from .db import replay, record_command, Blocked, NotFound
    from .models import Job
    if old_id==new_id or not reason.strip(): raise ValueError('Different batch IDs and an evidence-based reason are required')
    args={'old_id':old_id,'new_id':new_id,'reason':reason}
    with db.transaction() as s:
        cached=replay(s,request_id,'supersede_batch',args)
        if cached:return cached
        old=s.get(Batch,old_id);new=s.get(Batch,new_id)
        if not old or not new:raise NotFound('Batch not found')
        lock(s,'period:'+old.period);lock(s,'source:'+old.source)
        s.refresh(old);s.refresh(new)
        if new.superseded_by:raise Blocked('Cannot replace a batch with a superseded batch')
        if old.source!=new.source or old.period!=new.period:raise Blocked('Replacement must be the same source and accounting period')
        if new.status!='COMPLETE' or not new.declared_complete or new.rejected_count or new.expected_rows!=new.row_count:raise Blocked('Replacement must be complete, count-verified and free of quarantine')
        if old.superseded_by:raise Conflict('Original batch was already superseded')
        if old.status not in ('FAILED','COMPLETE'):raise Blocked('Cannot replace an active import')
        # Every identifiable old row must have a replacement with the same record
        # key at an equal/higher revision. Never silently drop prior good records.
        old_members=select(BatchRecord.record_id).where(BatchRecord.batch_id==old_id)
        from sqlalchemy.orm import aliased
        replacement_record=aliased(SourceRecord)
        replacement_member=aliased(BatchRecord)
        covered=select(replacement_record.id).join(replacement_member,replacement_member.record_id==replacement_record.id).where(replacement_member.batch_id==new_id,replacement_record.record_id==SourceRecord.record_id,replacement_record.revision>=SourceRecord.revision).exists()
        uncovered=s.scalar(select(SourceRecord.id).where(SourceRecord.id.in_(old_members),~covered).limit(1))
        if uncovered:raise Blocked('Replacement does not preserve previous record coverage')
        quarantines=s.scalars(select(Quarantine).where(Quarantine.batch_id==old_id,Quarantine.resolved_by_batch.is_(None)).execution_options(yield_per=500))
        resolved_count=0
        for q in quarantines:
            record_id=str(q.payload.get('record_id','')).strip()
            replacement=s.scalar(select(SourceRecord).join(BatchRecord,BatchRecord.record_id==SourceRecord.id).where(BatchRecord.batch_id==new_id,SourceRecord.record_id==record_id).order_by(SourceRecord.revision.desc()).limit(1))
            if not record_id or not replacement:raise Blocked('Each quarantined row needs a traceable corrected record ID; anonymous rows need a source-adapter investigation')
            try: old_revision=int(q.payload.get('revision',0))
            except (TypeError,ValueError):old_revision=0
            if replacement.revision<=old_revision:raise Blocked('Corrected quarantined records need a newer source revision')
            if replacement.batch_id!=new_id:raise Blocked('Correction evidence must belong to the declared replacement batch')
            q.resolved_by_batch=new_id;resolved_count+=1
            case=s.get(ReviewCase,digest(['quarantine',q.id]))
            if case:case.status='RESOLVED';case.outcome='CORRECTED_SOURCE';case.review_reason=reason;case.reviewed_by=actor
        old.superseded_by=new_id
        # Failed batches can have durable completed chunks. Their assessments must
        # still finish; no pending job is silently cancelled by supersession.
        audit(s,actor,'batch_superseded',old_id,{'replacement_batch_id':new_id,'reason':reason,'resolved_quarantine':resolved_count})
        return record_command(s,request_id,'supersede_batch',args,{'id':old_id,'replacement_batch_id':new_id,'resolved_quarantine':resolved_count})
