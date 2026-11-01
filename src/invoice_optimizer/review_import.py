"""Version-checked CSV proposals with immutable per-row reviewer evidence."""
from collections import defaultdict
import csv, hashlib, io
from pathlib import Path
from .assessment import _correct_case_in_session
from .db import audit, digest, record_command, replay
from .models import Movement, MovementVersion, ReviewCase
from .review_history import actor_identity, case_state, compact_review_audit, event_response, record_review_event, validate_actor
from .storage import ObjectStore

MAX_REVIEW_CSV_BYTES=5*1024*1024


def _apply_row(db,row,number,lines,file_hash,actor):
    patch={k:str(row.get(k,'')).strip() for k in ('source_a_amount','source_b_amount','description','expected_amount') if row.get(k) is not None and str(row.get(k)).strip()}
    if not patch:return None
    context={'origin':'CSV','parent_request_id':'worklist:'+file_hash,'source_file_sha256':file_hash,
             'source_row_number':number,'source_row_metadata':lines}
    reason=row.get('review_reason') or ''
    args={'case_id':row.get('case_id'),'expected_version':row.get('expected_version'),
          'patch':patch,'reason':reason,'actor':actor,'review_context':context}
    receipt_id='worklist-row:'+digest([file_hash,number])
    request_id='worklist:'+file_hash+':'+str(number)
    with db.transaction() as s:
        cached=replay(s,receipt_id,'review_csv_row',args)
        if cached is not None:return cached
        actor_identity(s,actor)
        before_deltas=dict(s.info.get('counter_deltas',{}))
        try:
            with s.begin_nested():
                if not reason.strip():raise ValueError('Evidence/reason is required for every change')
                result=_correct_case_in_session(s,row['case_id'],int(row['expected_version']),patch,reason,request_id,actor,review_context=context)
            result={**result,'row':number,'case_id':row['case_id'],'movement_status':result.get('status'),'status':'APPLIED'}
        except (ValueError,TypeError) as exc:
            s.info['counter_deltas']=defaultdict(int,before_deltas)
            result={'row':number,'case_id':row.get('case_id'),'status':'REJECTED','error':str(exc)}
            case=s.get(ReviewCase,row.get('case_id'),populate_existing=True) if row.get('case_id') else None
            if case:
                movement=s.get(Movement,case.movement_id,populate_existing=True) if case.movement_id else None
                version=s.get(MovementVersion,case.version_id) if case.version_id else None
                state=case_state(case,movement)
                event=record_review_event(s,case=case,movement=movement,version=version,actor=actor,
                    action='CORRECTION_REJECTED',outcome='REJECTED',reason=reason.strip() or 'No review reason supplied; correction rejected.',
                    request_id=request_id,before=state,after=state,context=context,apply_attribution=False,
                    request_evidence={'expected_version':row.get('expected_version'),'submitted_patch':patch,
                                      'submitted_reason':reason,'error':str(exc)})
                result.update(event_response(event))
                audit(s,actor,'csv_correction_rejected',case.id,compact_review_audit(event))
            else:
                result.update({'event_id':None,'reviewer':actor_identity(s,actor),'reviewed_at':None,'history_status':'AUDIT_ONLY_CASE_NOT_FOUND'})
                audit(s,actor,'csv_correction_rejected',row.get('case_id') or 'missing-case',
                      {**args,'error':str(exc),'reviewer':result['reviewer']})
        return record_command(s,receipt_id,'review_csv_row',args,result)


def import_review_csv(db,path,actor,max_rows=1000):
    validate_actor(actor)
    if type(max_rows) is not int or not 1<=max_rows<=1000:raise ValueError('CSV worklist limit must be between 1 and 1000 rows')
    # Hash exactly the bytes parsed, so an input replacement cannot change the
    # file provenance between hashing and applying its proposals.
    with Path(path).open('rb') as stream:raw=stream.read(MAX_REVIEW_CSV_BYTES+1)
    if len(raw)>MAX_REVIEW_CSV_BYTES:raise ValueError('CSV review file is limited to 5 MiB')
    file_hash=hashlib.sha256(raw).hexdigest()
    try:text=raw.decode('utf-8-sig')
    except UnicodeError:raise ValueError('CSV review file must use UTF-8') from None
    reader=csv.DictReader(io.StringIO(text,newline=''))
    if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames):raise ValueError('Missing or duplicate worklist headers')
    required={'case_id','expected_version','review_reason'}
    if not required.issubset(reader.fieldnames):raise ValueError('Missing worklist headers: '+', '.join(sorted(required-set(reader.fieldnames))))
    rows=[];previous_line=reader.line_num
    for row in reader:
        rows.append((row,{'physical_start_line':previous_line+1,'physical_end_line':reader.line_num}))
        previous_line=reader.line_num
        if len(rows)>max_rows:raise ValueError(f'Worklist import is limited to {max_rows} rows; filter/split the review list')
    with db.transaction() as s:actor_identity(s,actor)
    stored_hash=ObjectStore(db.settings.data_dir/'objects').put_bytes(raw)
    if stored_hash!=file_hash:raise ValueError('Retained CSV review file checksum mismatch')
    results=[]
    for number,(row,lines) in enumerate(rows,start=2):
        try:result=_apply_row(db,row,number,lines,file_hash,actor)
        except ValueError as exc:
            # Includes an actor-bound replay conflict: retain the old receipt,
            # never invent a new reviewer/event for its already applied row.
            result={'row':number,'case_id':row.get('case_id'),'status':'REJECTED','error':str(exc)}
        if result is not None:results.append(result)
    return {'applied':sum(r['status']=='APPLIED' for r in results),'failed':sum(r['status']=='REJECTED' for r in results),
            'results':results,'source_file_sha256':file_hash,'parent_request_id':'worklist:'+file_hash,
            'atomicity':'Each row is an independent version-checked transaction; rejected rows preserve data and retain action evidence.'}
