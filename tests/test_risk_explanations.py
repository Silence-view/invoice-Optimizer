"""English narrative claims must follow actual frozen mathematical evidence."""
from copy import deepcopy
from decimal import Decimal
import json
import math

import pytest

from invoice_optimizer.features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, build_features
from invoice_optimizer.ml import score_batch, explain_recorded, explain_features
from invoice_optimizer.risk_explanations import describe_model_result
from test_ml import trained, _sources


def source():
    return {'amount':'8500.00','expected_amount':'1000.00','customer_age_days':'12',
            'supplier_age_days':'3','cross_border':'1','event_at':'2026-09-04T07:30:00+02:00',
            'hour_utc':'5','currency':'GBP'}


def mathematical_explanation(payload=None,contributions=None,intercept=0.,threshold=.5,kind='RECORDED'):
    """Controlled frozen-output fixture, not a claim about a trained artifact."""
    values=build_features(payload or source())
    contributions=contributions if contributions is not None else [1.,2.,-.5,3.,.25,-.1]
    factors=[]
    for name,contribution in zip(FEATURE_NAMES,contributions,strict=True):
        # Unit training standard deviation and a displacement of one unit make
        # each fitted coefficient exactly equal its signed contribution.
        factors.append({'feature':name,'transformed_value':values[name],
            'training_mean':values[name]-1,'training_std':1.,'scaling_divisor':1.,
            'standardized_value':1.,'coefficient':contribution,'contribution':contribution,
            'label':'Untrusted label says fraud confirmed','effect':'WRONG',
            'raw_value':'unverified source value'})
    logit=intercept+math.fsum(contributions)
    score=1/(1+math.exp(-logit))
    return {'status':'AVAILABLE','explanation_version':'exact-linear-logodds-v1',
        'feature_schema_version':FEATURE_SCHEMA_VERSION,'calculation_kind':kind,
        'model_version':'lr-'+'a'*20,'policy_version':'controlled-policy','artifact_sha256':'b'*64,
        'contribution_units':'log_odds','intercept':intercept,'baseline_log_odds':intercept,
        'logit':logit,'score':score,'threshold':threshold,'flagged':score>=threshold,
        'features':factors,'top_reasons':[{'feature':'cross_border','contribution':999999.}]}


def test_actual_frozen_model_narratives_match_flags_and_all_contribution_signs(trained):
    data,model,_,_=trained
    rows=[row for row in _sources(data/'source_a.csv') if row['expected_amount'] and row['event_at']!='not-a-timestamp' and not row['amount'].startswith('-')]
    results=score_batch(rows,model)
    assert {item['flagged'] for item in results}=={False,True}
    for row,result in zip(rows,results,strict=True):
        verified=explain_recorded(result['feature_values'],model,result['model_version'],result['score'],result['policy_version'])
        narrative=describe_model_result(verified,row,result['feature_values'])
        assert narrative['status']=='AVAILABLE'
        assert narrative['threshold']['flagged']==result['flagged']
        assert narrative['threshold']['score']==result['score']
        assert narrative['provenance']['raw_binding']=='VERIFIED'
        assert narrative['provenance']['calculation_kind']=='RECORDED'
        all_drivers=narrative['drivers']+narrative['mitigating_drivers']+narrative['neutral_drivers']
        expected={item['feature']:item['contribution'] for item in verified['features']}
        assert {item['feature']:item['contribution_log_odds'] for item in all_drivers}==expected
        assert all(item['contribution_log_odds']>0 for item in narrative['drivers'])
        assert all(item['contribution_log_odds']<0 for item in narrative['mitigating_drivers'])
        assert all(item['raw_value_status']=='VERIFIED_SOURCE_SNAPSHOT' for item in all_drivers)
        assert all(item['contribution_log_odds']!=0 or item['direction']=='NEUTRAL' for item in all_drivers)


def test_raw_amount_ratio_ages_night_and_cross_border_have_exact_source_evidence():
    payload=source();explanation=mathematical_explanation(payload)
    result=describe_model_result(explanation,payload,build_features(payload))
    drivers={item['feature']:item for key in ('drivers','mitigating_drivers','neutral_drivers') for item in result[key]}
    assert 'GBP 8,500.00' in drivers['log_amount']['text']
    assert 'GBP 1,000.00 expected (8.5 times expected)' in drivers['amount_to_expected']['text']
    assert drivers['amount_to_expected']['raw_value']=={'amount':'8500.00','expected_amount':'1000.00'}
    assert '12 days' in drivers['log_customer_age_days']['text']
    assert '3 days' in drivers['log_supplier_age_days']['text']
    assert '05:30:00 UTC' in drivers['night_hour']['text']
    assert 'night-time indicator is 1' in drivers['night_hour']['text']
    assert 'cross-border indicator is 1 (yes)' in drivers['cross_border']['text']
    assert drivers['cross_border']['direction']=='DECREASE'
    assert drivers['log_customer_age_days']['direction']=='DECREASE'
    assert 'supplier age 3 days and an amount 8.5 times expected' in result['summary']
    assert 'fraud confirmed' not in json.dumps(result)
    assert 'model training split' in result['provenance']['baseline_definition']


def test_mismatched_source_values_are_excluded_but_verified_features_remain():
    payload=source();explanation=mathematical_explanation(payload)
    changed={**payload,'supplier_age_days':'999'}
    result=describe_model_result(explanation,changed,build_features(payload))
    assert result['status']=='AVAILABLE' and result['provenance']['raw_binding']=='MISMATCH'
    assert '999' not in result['summary']
    assert 'supplier age 999 days' not in json.dumps(result)
    for key in ('drivers','mitigating_drivers','neutral_drivers'):
        for item in result[key]:
            assert item['raw_value'] is None and item['raw_value_status']=='UNAVAILABLE'
            assert item['evidence_source']=='VERIFIED_EXPLANATION_FEATURES'
            assert 'transformed feature' in item['source_note']
    assert result['threshold']['score']==explanation['score']


def test_missing_raw_snapshot_is_not_fabricated_from_logarithms_or_embedded_raw_fields():
    explanation=mathematical_explanation()
    result=describe_model_result(explanation)
    assert result['status']=='AVAILABLE' and result['provenance']['raw_binding']=='UNAVAILABLE'
    assert 'GBP' not in result['summary']
    assert 'unverified source value' not in json.dumps(result)
    assert all(item['raw_value'] is None for key in ('drivers','mitigating_drivers','neutral_drivers') for item in result[key])


def test_wrong_recorded_vector_invalidates_assessment_binding():
    explanation=mathematical_explanation();features=build_features(source())
    features['cross_border']=0.
    result=describe_model_result(explanation,source(),features)
    assert result['status']=='UNAVAILABLE' and result['reason']=='RECORDED_FEATURE_VECTOR_MISMATCH'
    assert result['threshold'] is None and not result['drivers']


@pytest.mark.parametrize('damage',['score','coefficient','mean','scale','flag','order','duplicate','missing','nonfinite','wrong_units','wrong_artifact','wrong_schema','no_policy'])
def test_inconsistent_explanation_never_creates_narrative(damage):
    explanation=mathematical_explanation()
    if damage=='score':explanation['score']=.01
    elif damage=='coefficient':explanation['features'][0]['coefficient']+=1
    elif damage=='mean':explanation['features'][0]['training_mean']+=1
    elif damage=='scale':explanation['features'][0]['scaling_divisor']=0
    elif damage=='flag':explanation['flagged']=False
    elif damage=='order':explanation['features'].reverse()
    elif damage=='duplicate':explanation['features'][1]['feature']=explanation['features'][0]['feature']
    elif damage=='missing':explanation['features'].pop()
    elif damage=='nonfinite':explanation['features'][0]['contribution']=float('nan')
    elif damage=='wrong_units':explanation['contribution_units']='probability_percent'
    elif damage=='wrong_artifact':explanation['artifact_sha256']='not-a-hash'
    elif damage=='wrong_schema':explanation['feature_schema_version']='future-schema'
    else:explanation['policy_version']=None
    result=describe_model_result(explanation,source())
    assert result['status']=='UNAVAILABLE' and result['category']=='MODEL_UNAVAILABLE'
    assert not result['drivers'] and not result['mitigating_drivers'] and result['threshold'] is None


def test_unavailable_evidence_does_not_claim_safe_movement_or_zero_score():
    for value in (None,{}, {'status':'UNAVAILABLE','reason':'MODEL_UNAVAILABLE'}):
        result=describe_model_result(value,source())
        assert result['status']=='UNAVAILABLE' and result['threshold'] is None
        assert 'does not mean a zero score or a safe movement' in result['caveat']
        assert 'model-based reason cannot be stated' in result['summary']


def test_high_baseline_can_trigger_without_any_upward_feature_driver():
    explanation=mathematical_explanation(contributions=[-.1]*6,intercept=3.,threshold=.9)
    result=describe_model_result(explanation,source())
    assert result['threshold']['flagged'] and not result['drivers']
    assert len(result['mitigating_drivers'])==6
    assert 'baseline already meets the threshold, with no upward feature contributions' in result['summary']
    assert 'largest upward' not in result['summary']


def test_upward_contributions_below_threshold_are_not_mislabelled_as_flags():
    explanation=mathematical_explanation(contributions=[.1]*6,intercept=-3.,threshold=.9)
    result=describe_model_result(explanation,source())
    assert not result['threshold']['flagged'] and not result['mitigating_drivers']
    assert result['category']=='BELOW_MODEL_THRESHOLD'
    assert 'did not flag' in result['summary'] and 'insufficient to reach' in result['summary']


def test_zero_contributions_are_neutral_and_exact_threshold_equality_flags():
    explanation=mathematical_explanation(contributions=[0.]*6,intercept=0.,threshold=.5)
    result=describe_model_result(explanation,source())
    assert result['threshold']['flagged'] and result['threshold']['comparison']=='at_or_above'
    assert len(result['neutral_drivers'])==6 and not result['drivers'] and not result['mitigating_drivers']
    assert all('does not change' in item['text'] for item in result['neutral_drivers'])


def test_close_score_threshold_values_remain_distinguishable_in_text():
    explanation=mathematical_explanation(contributions=[0.]*6,threshold=.50000000000001)
    result=describe_model_result(explanation,source())
    assert not result['threshold']['flagged']
    assert '0.50000000000000999' in result['summary'] or '0.50000000000001' in result['summary']
    assert 'score 0.5 is below' in result['summary']


def test_tiny_actual_contributions_are_not_hidden_by_arbitrary_significance_cutoffs():
    explanation=mathematical_explanation(contributions=[1e-12,-1e-12,0.,0.,0.,0.])
    result=describe_model_result(explanation)
    assert len(result['drivers'])==1 and result['drivers'][0]['contribution_log_odds']==1e-12
    assert len(result['mitigating_drivers'])==1 and result['mitigating_drivers'][0]['contribution_log_odds']==-1e-12
    assert '+1e-12' in result['drivers'][0]['text']


def test_hypothetical_narrative_stays_distinct_and_does_not_mutate_inputs(trained):
    data,model,_,metadata=trained
    payload=_sources(data/'source_a.csv')[0]
    changed={**payload,'expected_amount':'1.00'};features=build_features(changed)
    explanation=explain_features(features,model,metadata['model_version'],metadata['policy_version'],payload=changed)
    before=deepcopy((explanation,changed,features))
    result=describe_model_result(explanation,changed,features)
    assert result['status']=='AVAILABLE' and result['provenance']['calculation_kind']=='HYPOTHETICAL'
    assert 'hypothetical' in result['headline'] and 'hypothetical' in result['summary']
    assert all(item['raw_value_status']=='VERIFIED_HYPOTHETICAL_INPUT' for key in ('drivers','mitigating_drivers','neutral_drivers') for item in result[key])
    assert (explanation,changed,features)==before


def test_extra_source_labels_identifiers_and_future_information_never_enter_text():
    payload=source();explanation=mathematical_explanation(payload)
    plain=describe_model_result(explanation,payload)
    extra={**payload,'scenario':'CONFIRMED_FRAUD','label':1,'customer_id':'PRIVATE-ID',
           'future_review':'unsafe','description':'UNTRUSTED-INSTRUCTION'}
    result=describe_model_result(explanation,extra)
    assert result==plain


@pytest.mark.parametrize(('hour','flag'),[(0,1),(5,1),(6,0),(23,0)])
def test_utc_night_indicator_uses_exact_declared_interval(hour,flag):
    payload={**source(),'event_at':f'2026-09-04T{hour:02d}:00:00Z','hour_utc':str(hour)}
    result=describe_model_result(mathematical_explanation(payload),payload)
    night=next(item for key in ('drivers','mitigating_drivers','neutral_drivers') for item in result[key] if item['feature']=='night_hour')
    assert f'night-time indicator is {flag}' in night['text']
    assert 'daytime' not in night['text']+night['summary_phrase']
    assert night['raw_value']['hour_utc']==hour


@pytest.mark.parametrize('payload',[{}, {'amount':'NaN'}, ['not-a-source-payload']])
def test_invalid_source_snapshot_does_not_destroy_valid_recorded_feature_evidence(payload):
    result=describe_model_result(mathematical_explanation(),payload)
    assert result['status']=='AVAILABLE' and result['provenance']['raw_binding']=='MISMATCH'
    assert all(item['raw_value'] is None for key in ('drivers','mitigating_drivers','neutral_drivers') for item in result[key])
