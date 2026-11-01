#!/usr/bin/env python3
"""Bounded, read-only independent comparison of live case cards and frozen evidence."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import urllib.request

from sqlalchemy import select, text
from invoice_optimizer.config import Settings
from invoice_optimizer.db import Database, canonical
from invoice_optimizer.features import build_features
from invoice_optimizer.models import ReviewCase, Movement, MovementVersion, Assessment, SourceRecord


def main():
    settings=Settings.from_env()
    db=Database(settings)
    origin='http://127.0.0.1:8765'
    requests=[]
    def get(path):
        requests.append(path)
        request=urllib.request.Request(origin+path,headers={'Authorization':'Bearer '+settings.api_token})
        with urllib.request.urlopen(request,timeout=20) as response:return json.load(response)
    def listing():
        result=[]
        for offset in range(0,1000,50):
            part=get('/api/cases?status=OPEN&limit=50&offset='+str(offset));result.extend(part)
            if len(part)<50:return result
        raise AssertionError('Review value audit exceeded its1000-case bound')
    receipt={'scope':'Read-only live API GETs plus repeatable-read READ ONLY PostgreSQL; no review, invoice, source, model or mail mutation.',
             'at':datetime.now(timezone.utc).isoformat(),'mismatches':[],'expected_differences':[]}
    try:
        before=listing();ids=[item['id'] for item in before]
        assert len(ids)==len(set(ids))
        with db.Session() as session:
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            cases={item.id:item for item in session.scalars(select(ReviewCase).where(ReviewCase.id.in_(ids)))}
            versions={item.id:item for item in session.scalars(select(MovementVersion).where(MovementVersion.id.in_([item.version_id for item in cases.values()])))}
            movements={item.id:item for item in session.scalars(select(Movement).where(Movement.id.in_([item.movement_id for item in cases.values()])))}
            assessments={(item.version_id,item.kind):item for item in session.scalars(select(Assessment).where(Assessment.version_id.in_(versions)))}
            sources={item.id:item for item in session.scalars(select(SourceRecord).where(SourceRecord.id.in_({rid for item in versions.values() for rid in item.source_refs})))}
            rows=[];statuses=Counter();raw_bindings=Counter();reasons=Counter();max_reconstruction_error=0.;max_feature_error=0.
            for index,item in enumerate(before):
                case=cases[item['id']];version=versions.get(case.version_id);movement=movements.get(case.movement_id)
                model=assessments.get((case.version_id,'MODEL'));payload=version.payload if version else {}
                tx=item.get('transaction') or {};list_model=item.get('model') or {};reasons[case.reason]+=1
                row={'case_id':case.id,'first_visible_page':index<50,'case_version':version.version_no if version else None,
                     'current_version':movement.current_version if movement else None,'reason':case.reason,
                     'amount':tx.get('amount'),'currency':tx.get('currency'),'source_a_amount':tx.get('source_a_amount'),
                     'source_b_amount':tx.get('source_b_amount'),'model_status':model.status if model else None}
                def equal(name,actual,expected):
                    if actual!=expected:receipt['mismatches'].append({'case_id':case.id,'field':name,'actual':actual,'expected':expected})
                def money(name,actual,expected):
                    if actual is None or expected is None:equal(name,actual,expected)
                    else:equal(name,Decimal(str(actual)),Decimal(str(expected)))
                equal('case.version_id',item.get('version_id'),case.version_id)
                equal('case.version_no',item.get('version_no'),version.version_no if version else None)
                equal('case.current_version',item.get('current_version'),movement.current_version if movement else None)
                if version:
                    money('card.amount versus case version.amount',tx.get('amount'),version.amount)
                    equal('card.currency versus case version.currency',tx.get('currency'),version.currency)
                    money('case version.amount versus payload.amount',version.amount,payload.get('amount'))
                    equal('case version.currency versus payload.currency',version.currency,payload.get('currency'))
                    if movement and movement.current_version!=version.version_no:receipt['expected_differences'].append({'case_id':case.id,'kind':'HISTORICAL_CASE_VERSION','case_version':version.version_no,'current_version':movement.current_version})
                    for source in ('A','B'):
                        value=payload.get('source_values',{}).get(source,{})
                        value=value if isinstance(value,dict) else {}
                        money('card.source_'+source+'_amount versus retained payload',tx.get('source_'+source.lower()+'_amount'),value.get('amount'))
                        equal('card.source_'+source+'_currency versus retained payload',tx.get('source_'+source.lower()+'_currency'),value.get('currency'))
                        retained=[sources[rid] for rid in version.source_refs if rid in sources and sources[rid].source==source]
                        if len(retained)==1 and value and version.origin=='SOURCE':
                            money('source_'+source+'_amount versus retained source record',value.get('amount'),retained[0].payload.get('amount'))
                            equal('source_'+source+'_currency versus retained source record',value.get('currency'),retained[0].payload.get('currency'))
                if model:
                    statuses[model.status]+=1
                    equal('card.model_version',list_model.get('model_version'),model.model_version)
                    equal('card.recorded_score',list_model.get('recorded_score'),model.score if model.status=='SCORED' else None)
                    if model.status=='SCORED':
                        rebuilt=build_features(payload)
                        equal('rebuilt_features versus retained model.features',rebuilt,model.features)
                        for key,value in rebuilt.items():max_feature_error=max(max_feature_error,abs(value-model.features[key]))
                        explanation=get('/api/cases/'+case.id+'/model/explanation')
                        detail=explanation.get('explanation',{});provenance=explanation.get('narrative',{}).get('provenance',{})
                        raw_bindings[provenance.get('raw_binding','NOT_RETURNED')]+=1
                        equal('explanation.version_id',explanation.get('version_id'),version.id)
                        equal('explanation.model_version',explanation.get('model_version'),model.model_version)
                        equal('explanation.recorded_score',explanation.get('recorded_score'),model.score)
                        equal('explanation.raw_binding',provenance.get('raw_binding'),'VERIFIED')
                        equal('explanation.status',detail.get('status'),'AVAILABLE')
                        if detail.get('status')=='AVAILABLE':
                            logit=detail['baseline_log_odds']+math.fsum(factor['contribution'] for factor in detail['features'])
                            reconstructed=1/(1+math.exp(-logit))
                            error=abs(reconstructed-model.score);max_reconstruction_error=max(max_reconstruction_error,error)
                            if error>1e-12:receipt['mismatches'].append({'case_id':case.id,'field':'logistic reconstruction','max_absolute_error':error})
                            # The cheap list deliberately trusts a retained receipt only;
                            # legacy records are reconstructed by the separate artifact GET.
                            if model.explanation is None and list_model.get('threshold') is None:
                                equal('card.threshold_status',list_model.get('threshold_status'),'UNAVAILABLE')
                                receipt['expected_differences'].append({'case_id':case.id,'kind':'LEGACY_THRESHOLD_NOT_RETAINED',
                                    'list_threshold':None,'frozen_artifact_threshold':detail.get('threshold')})
                            else:equal('card.threshold',list_model.get('threshold'),detail.get('threshold'))
                            for factor in detail['features']:
                                equal('explanation.transformed.'+factor['feature'],factor['transformed_value'],model.features[factor['feature']])
                            row.update(model_version=model.model_version,score=model.score,raw_binding=provenance.get('raw_binding'),features=rebuilt)
                rows.append(row)
            session.rollback()
        after=listing()
        receipt.update(pass_=not receipt['mismatches'],open_cases=len(rows),first_visible_page_cases=min(50,len(rows)),
            read_only_requests=len(requests),model_status_counts=dict(statuses),raw_binding_counts=dict(raw_bindings),case_reason_counts=dict(reasons),
            max_feature_absolute_error=max_feature_error,max_logistic_score_absolute_error=max_reconstruction_error,
            list_unchanged=before==after,list_sha256=hashlib.sha256(canonical(before).encode()).hexdigest(),rows=rows)
        receipt['expected_difference_counts']=dict(Counter(item['kind'] for item in receipt['expected_differences']))
        receipt['missing_version_cases']=sum(item['case_version'] is None for item in rows)
        receipt['pass']=receipt.pop('pass_') and receipt['list_unchanged']
        target=Path('output/verification/values-audit.json');target.write_text(json.dumps(receipt,indent=2,default=str)+'\n')
        print(json.dumps({key:value for key,value in receipt.items() if key not in ('rows','expected_differences','mismatches')},default=str,indent=2))
        assert receipt['pass'],'Live case values did not match their frozen evidence'
    finally:db.close()


if __name__=='__main__':main()
