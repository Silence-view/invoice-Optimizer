"""Deterministic English descriptions of already verified model evidence.

This module neither loads models nor scores, trains, edits or classifies cases.
Callers supply the exact frozen-model explanation from ``explain_recorded`` or
``explain_features``. The additional arithmetic checks reject inconsistent
narratives; they do not replace the upstream artifact and version verification.
Deterministic reconciliation failures remain separate from this model signal.
"""
from copy import deepcopy
from decimal import Decimal
import math
import re

from .features import (FEATURE_DESCRIPTIONS, FEATURE_NAMES, FEATURE_SCHEMA_VERSION,
                       build_features, feature_raw_values, parse_event_at)

NARRATIVE_VERSION = 'verified-model-narrative-v1'
BASELINE_DEFINITION = 'The mean of each transformed feature in the model training split, before standardisation; it is not a typical customer or an average amount in pounds.'
CAVEAT = ('This describes a synthetic model output, not proof of fraud or a real-world fraud probability. '
          'Contributions are signed log-odds relative to the transformed training-mean baseline, not probability percentages or causal effects. '
          'Deterministic reconciliation checks and human review are separate.')


def _number(value):
    if type(value) not in (int,float) or not math.isfinite(value):
        raise ValueError('A finite numerical value is required')
    return float(value)


def _close(left,right):
    return math.isclose(left,right,rel_tol=1e-12,abs_tol=1e-12)


def _sigmoid(value):
    if value>=0:return 1/(1+math.exp(-value))
    exponential=math.exp(value)
    return exponential/(1+exponential)


def _vector(value):
    if not isinstance(value,dict) or set(value)!=set(FEATURE_NAMES):
        raise ValueError('The feature vector does not match the supported schema')
    return {name:_number(value[name]) for name in FEATURE_NAMES}


def _validated(explanation):
    if not isinstance(explanation,dict) or explanation.get('status')!='AVAILABLE':
        raise ValueError('No verified explanation is available')
    if explanation.get('feature_schema_version')!=FEATURE_SCHEMA_VERSION or explanation.get('explanation_version')!='exact-linear-logodds-v1':
        raise ValueError('The explanation schema is unsupported')
    if explanation.get('contribution_units')!='log_odds' or explanation.get('calculation_kind') not in ('RECORDED','ASSESSMENT','HYPOTHETICAL'):
        raise ValueError('The explanation units or calculation kind are unsupported')
    if not re.fullmatch(r'lr-[a-f0-9]{20}',str(explanation.get('model_version',''))) or not re.fullmatch(r'[a-f0-9]{64}',str(explanation.get('artifact_sha256',''))):
        raise ValueError('The frozen artifact identity is missing')
    if not isinstance(explanation.get('policy_version'),str) or not explanation['policy_version'].strip():
        raise ValueError('The threshold policy identity is missing')
    factors=explanation.get('features')
    if not isinstance(factors,list) or len(factors)!=len(FEATURE_NAMES) or any(not isinstance(item,dict) for item in factors):
        raise ValueError('Complete per-feature evidence is required')
    if [item.get('feature') for item in factors]!=list(FEATURE_NAMES):
        raise ValueError('The declared feature order differs')
    for item in factors:
        value,mean,scale,std,z,coefficient,contribution=(_number(item[key]) for key in (
            'transformed_value','training_mean','scaling_divisor','training_std',
            'standardized_value','coefficient','contribution'))
        if scale<=0 or std<0 or not _close(scale,std if std else 1):
            raise ValueError('The training scale is invalid')
        if not _close((value-mean)/scale,z) or not _close(z*coefficient,contribution):
            raise ValueError('Feature evidence does not reconstruct its contribution')
    intercept,baseline,logit,score,threshold=(_number(explanation[key]) for key in (
        'intercept','baseline_log_odds','logit','score','threshold'))
    if not 0<=score<=1 or not 0<=threshold<=1 or not _close(intercept,baseline):
        raise ValueError('The baseline, score or threshold is invalid')
    if not _close(intercept+math.fsum(item['contribution'] for item in factors),logit) or not _close(_sigmoid(logit),score):
        raise ValueError('The explanation does not reconstruct the recorded score')
    if type(explanation.get('flagged')) is not bool or explanation['flagged']!=(score>=threshold):
        raise ValueError('The flag does not agree with its threshold')
    return factors,{item['feature']:float(item['transformed_value']) for item in factors}


def _provenance(explanation):
    explanation=explanation if isinstance(explanation,dict) else {}
    return {'narrative_version':NARRATIVE_VERSION,
        **{key:explanation.get(key) for key in ('model_version','policy_version','artifact_sha256',
            'feature_schema_version','explanation_version','calculation_kind')},
        'raw_binding':'UNAVAILABLE','raw_binding_reason':'No source snapshot has been matched to the explained feature vector.',
        'baseline_definition':BASELINE_DEFINITION,'contribution_units':'log_odds',
        'text_rounding':'Narrative text rounds numbers for readability; the structured contributions retain the exact floating-point values.'}


def _unavailable(explanation,reason):
    return {'status':'UNAVAILABLE','category':'MODEL_UNAVAILABLE','reason':reason,
        'headline':'Model explanation unavailable',
        'summary':'Verified frozen-model evidence is unavailable, so a model-based reason cannot be stated for this movement.',
        'drivers':[],'mitigating_drivers':[],'neutral_drivers':[],'threshold':None,
        'caveat':CAVEAT+' Unavailable evidence does not mean a zero score or a safe movement.',
        'provenance':_provenance(explanation)}


def _display(value):
    return format(value,'.6g')


def _score_text(score,threshold):
    for precision in (6,12,17):
        shown_score,shown_threshold=format(score,f'.{precision}g'),format(threshold,f'.{precision}g')
        if score==threshold or shown_score!=shown_threshold:return shown_score,shown_threshold
    return shown_score,shown_threshold


def _raw_description(name,raw,payload):
    currency=payload.get('currency')
    prefix=currency+' ' if isinstance(currency,str) and re.fullmatch('[A-Z]{3}',currency) else ''
    if name=='log_amount':
        amount=format(Decimal(raw),',.2f')
        return f'an amount of {prefix}{amount}',f'The amount is {prefix}{amount}'
    if name=='amount_to_expected':
        ratio=Decimal(raw['amount'])/Decimal(raw['expected_amount'])
        shown=format(ratio,'.6g')
        approximate='about ' if Decimal(shown)!=ratio else ''
        amount,expected=(format(Decimal(raw[key]),',.2f') for key in ('amount','expected_amount'))
        return f'an amount {approximate}{shown} times expected',f'The amount is {prefix}{amount} versus {prefix}{expected} expected ({approximate}{shown} times expected)'
    if name in ('log_customer_age_days','log_supplier_age_days'):
        entity='customer' if name=='log_customer_age_days' else 'supplier'
        return f'{entity} age {raw} days',f'The {entity} age supplied for the movement is {raw} days'
    if name=='night_hour':
        timestamp=parse_event_at(payload['event_at'])
        time=timestamp.strftime('%H:%M:%S UTC')
        night=timestamp.hour<6
        return f'the UTC night-time indicator ({int(night)})',f'The movement time is {time}, so the 00:00–05:59 UTC night-time indicator is {int(night)}'
    return f'the declared cross-border indicator ({raw})',f'The supplied cross-border indicator is {raw} ({"yes" if raw else "no"})'


def _driver(item,raw,payload,kind):
    name=item['feature'];value=float(item['transformed_value']);contribution=float(item['contribution'])
    direction='INCREASE' if contribution>0 else 'DECREASE' if contribution<0 else 'NEUTRAL'
    label=FEATURE_DESCRIPTIONS[name]['label']
    hypothetical=kind=='HYPOTHETICAL'
    if raw is not None:
        short,observation=_raw_description(name,raw,payload)
        evidence_source='HYPOTHETICAL_INPUT' if hypothetical else 'FEATURE_MATCHED_SOURCE_SNAPSHOT'
        raw_status='VERIFIED_HYPOTHETICAL_INPUT' if hypothetical else 'VERIFIED_SOURCE_SNAPSHOT'
        source_note='These are hypothetical inputs.' if hypothetical else 'These values come from the source snapshot that reproduces the explained feature vector.'
    else:
        short=f'the {label.lower()} feature'
        observation=f'The {label.lower()} feature is {_display(value)} after {FEATURE_DESCRIPTIONS[name]["transformation"]}'
        evidence_source='VERIFIED_EXPLANATION_FEATURES'
        raw_status='UNAVAILABLE'
        source_note='No matching source snapshot is attached; this observation uses only the explained transformed feature.'
    effect='raises' if contribution>0 else 'lowers' if contribution<0 else 'does not change'
    signed=format(contribution,'+.6g')
    text=f'{observation}; its fitted contribution is {signed} log-odds and {effect} the model score relative to the training-mean reference.'
    return {'feature':name,'label':label,'direction':direction,'contribution_log_odds':contribution,
        'feature_value':value,'training_mean':float(item['training_mean']),
        'raw_value':deepcopy(raw),'raw_value_status':raw_status,'text':text,
        'summary_phrase':short,'evidence_source':evidence_source,'source_note':source_note,
        'transformation':FEATURE_DESCRIPTIONS[name]['transformation']}


def describe_model_result(explanation,payload=None,feature_values=None):
    """Explain only the verified model signal, never the domain case reason.

    Source values are used only after full feature equality. A mismatching
    source snapshot disables raw-value claims but leaves the valid transformed
    feature explanation usable. A supplied recorded feature vector that differs
    from the explanation itself invalidates the narrative's assessment binding.
    """
    try:
        factors,explained_values=_validated(explanation)
    except (ValueError,TypeError,KeyError,ArithmeticError):
        return _unavailable(explanation,'EXPLANATION_UNAVAILABLE_OR_INCONSISTENT')
    if feature_values is not None:
        try:
            if _vector(feature_values)!=explained_values:
                return _unavailable(explanation,'RECORDED_FEATURE_VECTOR_MISMATCH')
        except (ValueError,TypeError,ArithmeticError):
            return _unavailable(explanation,'RECORDED_FEATURE_VECTOR_MISMATCH')
    provenance=_provenance(explanation)
    raw_values={}
    if payload is not None:
        try:
            if build_features(payload)!=explained_values:
                provenance.update(raw_binding='MISMATCH',raw_binding_reason='The supplied source snapshot does not reproduce the explained feature vector; its raw values are excluded.')
            else:
                raw_values=feature_raw_values(payload)
                provenance.update(raw_binding='VERIFIED',raw_binding_reason='All source inputs reproduce the exact explained feature vector.')
        except (ValueError,TypeError,KeyError,ArithmeticError,AttributeError):
            provenance.update(raw_binding='MISMATCH',raw_binding_reason='The supplied source snapshot cannot reproduce the explained feature vector; its raw values are excluded.')
    items=[_driver(item,raw_values.get(item['feature']),payload,explanation['calculation_kind']) for item in factors]
    positive=sorted((item for item in items if item['contribution_log_odds']>0),key=lambda item:-item['contribution_log_odds'])
    negative=sorted((item for item in items if item['contribution_log_odds']<0),key=lambda item:item['contribution_log_odds'])
    neutral=[item for item in items if item['contribution_log_odds']==0]
    score,threshold=float(explanation['score']),float(explanation['threshold'])
    score_text,threshold_text=_score_text(score,threshold)
    flagged=score>=threshold
    hypothetical=explanation['calculation_kind']=='HYPOTHETICAL'
    if flagged:
        headline='The hypothetical inputs meet the model review threshold' if hypothetical else 'The model score meets the review threshold'
        lead=f'{"The hypothetical movement would be flagged" if hypothetical else "The model flagged this movement"}: score {score_text} meets or exceeds its review threshold of {threshold_text}'
        if positive:
            detail='the largest upward '+('contribution comes' if len(positive)==1 else 'contributions come')+' from '+' and '.join(item['summary_phrase'] for item in positive[:2])
        else:
            detail='the training-mean baseline already meets the threshold, with no upward feature contributions'
    else:
        headline='The hypothetical inputs stay below the model review threshold' if hypothetical else 'The model score is below the review threshold'
        lead=f'{"The hypothetical movement would not be flagged by this model" if hypothetical else "The model did not flag this movement"}: score {score_text} is below its review threshold of {threshold_text}'
        if negative:
            detail='the largest downward '+('contribution comes' if len(negative)==1 else 'contributions come')+' from '+' and '.join(item['summary_phrase'] for item in negative[:2])
        elif positive:
            detail='the upward feature contributions are insufficient to reach the threshold'
        else:
            detail='no feature contribution changes the training-mean baseline log-odds'
    return {'status':'AVAILABLE','category':'MODEL_RISK' if flagged else 'BELOW_MODEL_THRESHOLD',
        'reason':None,'headline':headline,'summary':lead+'; '+detail+'.',
        'drivers':positive,'mitigating_drivers':negative,'neutral_drivers':neutral,
        'threshold':{'score':score,'value':threshold,'flagged':flagged,'comparison':'at_or_above' if flagged else 'below'},
        'caveat':CAVEAT,'provenance':provenance}
