"""Bounded feature bars reconstructed from the case's exact recorded model.

Percentages describe absolute feature-contribution shares, never independent
fraud probabilities. Model artifacts are verified once per version per batch.
"""
import math
from decimal import Decimal

from sqlalchemy import select

from .features import FEATURE_NAMES, build_features
from .ml import _get_model, _explain, compact_explanation
from .models import Assessment, MovementVersion, ReviewCase
from .review_cards import _probability


def _verify_receipt(receipt, explanation):
    """Do not conceal contradictory retained mathematics with a reconstruction."""
    if receipt is None:
        return  # Legacy assessments can be reconstructed without inventing a receipt.
    if not isinstance(receipt, dict):
        raise ValueError('Malformed recorded mathematical receipt.')
    if receipt.get('status') != 'AVAILABLE':
        return
    compact = compact_explanation(explanation)
    expected = {**explanation, **compact}
    if 'schema_version' in receipt or 'contributions' in receipt:
        if not set(compact).issubset(receipt):
            raise ValueError('Incomplete recorded mathematical receipt.')

    def same(recorded, reconstructed):
        if type(reconstructed) in (int, float):
            return (type(recorded) in (int, float) and math.isfinite(recorded)
                    and math.isclose(recorded, reconstructed, rel_tol=1e-12, abs_tol=1e-12))
        if isinstance(reconstructed, dict):
            return (isinstance(recorded, dict) and set(recorded) == set(reconstructed)
                    and all(same(recorded[key], value) for key, value in reconstructed.items()))
        return type(recorded) is type(reconstructed) and recorded == reconstructed

    for key in ('schema_version', 'explanation_version', 'score', 'threshold',
                'model_version', 'policy_version', 'feature_schema_version',
                'artifact_sha256', 'baseline_logit', 'baseline_log_odds', 'intercept',
                'baseline_score', 'logit', 'reconstructed_score', 'contribution_units',
                'contributions', 'flagged'):
        if key in receipt and not same(receipt[key], expected.get(key)):
            raise ValueError('Recorded mathematical receipt differs from the frozen model.')
    if 'features' in receipt:
        features = receipt['features']
        if not isinstance(features, list) or len(features) != len(explanation['features']):
            raise ValueError('Malformed recorded feature contributions.')
        for recorded, reconstructed in zip(features, explanation['features']):
            if not isinstance(recorded, dict) or not {'feature', 'contribution'}.issubset(recorded):
                raise ValueError('Incomplete recorded feature contribution.')
            for key in ('feature', 'contribution', 'transformed_value', 'training_mean',
                        'training_std', 'scaling_divisor', 'standardized_value', 'coefficient', 'effect'):
                if key in recorded and not same(recorded[key], reconstructed[key]):
                    raise ValueError('Recorded feature contribution differs from the frozen model.')


def feature_bars(case, version, assessment, load_model):
    result = {'case_id': case.id, 'version_id': case.version_id,
        'version_no': version.version_no if version else None,
        'assessment_id': assessment.id if assessment else None,
        'status': 'UNAVAILABLE', 'reason': 'No usable recorded model score.',
        'features': [], 'threshold': None, 'raw_binding': 'UNAVAILABLE',
        'percent_meaning': 'Share of the sum of absolute feature contributions; not a probability.',
        'rescored': False}
    if not assessment or assessment.status != 'SCORED' or not _probability(assessment.score):
        return result
    try:
        if not version or not assessment.model_version:
            raise ValueError('The retained version or recorded model identity is unavailable.')
        model, metadata = load_model(assessment.model_version)
        if metadata['policy_version'] != assessment.policy_version:
            raise ValueError('Recorded policy differs from the frozen model.')
        if metadata['feature_schema_version'] != assessment.feature_schema_version:
            raise ValueError('Recorded feature schema differs from the frozen model.')
        explanation = _explain(assessment.features, model, metadata, assessment.score)
        if explanation['flagged'] != assessment.flagged:
            raise ValueError('Recorded flag differs from the frozen score threshold.')
        _verify_receipt(assessment.explanation, explanation)
        payload = version.payload
        try:
            if not isinstance(payload, dict) or build_features(payload) != assessment.features:
                raise ValueError('Retained inputs do not reproduce the recorded feature vector.')
            if Decimal(str(payload.get('amount'))) != version.amount or payload.get('currency') != version.currency:
                raise ValueError('Stored amount/currency differs from the retained model input.')
            explanation = _explain(assessment.features, model, metadata, assessment.score, payload=payload)
            result['raw_binding'] = 'VERIFIED_SOURCE_SNAPSHOT'
        except (ValueError, TypeError, ArithmeticError) as exc:
            result['raw_binding_reason'] = str(exc)
        total = math.fsum(abs(f['contribution']) for f in explanation['features'])
        if not math.isfinite(total):
            raise ValueError('Non-finite contribution total.')
        result.update(status='AVAILABLE', reason=None, model_version=assessment.model_version,
            policy_version=assessment.policy_version, recorded_score=assessment.score,
            threshold=explanation['threshold'], baseline_log_odds=explanation['baseline_log_odds'],
            logit=explanation['logit'], reconstruction_error=explanation['reconstruction_error'],
            artifact_sha256=explanation['artifact_sha256'], contribution_units='log_odds',
            absolute_contribution_total=total, currency=version.currency,
            features=[{key: feature[key] for key in ('feature', 'label', 'raw_value',
                'raw_value_status', 'transformed_value', 'contribution', 'effect', 'transformation')} |
                {'share_percent': abs(feature['contribution']) / total * 100 if total else 0.0}
                for feature in explanation['features']])
        if [feature['feature'] for feature in result['features']] != list(FEATURE_NAMES):
            raise ValueError('Feature order differs from the recorded schema.')
    except (ValueError, TypeError, ArithmeticError, OSError, KeyError) as exc:
        result.update(status='UNAVAILABLE', reason=str(exc)[:300], features=[], threshold=None)
    return result


def case_driver_page(db, ids):
    if not 1 <= len(ids) <= 50 or len(set(ids)) != len(ids) or any(not isinstance(key, str) or not 1 <= len(key) <= 64 for key in ids):
        raise ValueError('Request 1–50 unique case identifiers.')
    with db.transaction() as session:
        rows = list(session.execute(select(ReviewCase, MovementVersion, Assessment)
            .outerjoin(MovementVersion, MovementVersion.id == ReviewCase.version_id)
            .outerjoin(Assessment, (Assessment.version_id == ReviewCase.version_id) & (Assessment.kind == 'MODEL'))
            .where(ReviewCase.id.in_(ids))))
    loaded = {}
    def load(version):
        if version not in loaded:
            try: loaded[version] = _get_model(db.settings.model_dir, version, verify_fresh=True)
            except Exception as exc: loaded[version] = ValueError('Frozen model unavailable: ' + type(exc).__name__)
        value = loaded[version]
        if isinstance(value, Exception): raise value
        return value
    by_id = {case.id: feature_bars(case, version, assessment, load) for case, version, assessment in rows}
    return {'items': [by_id[key] for key in ids if key in by_id],
        'missing_case_ids': [key for key in ids if key not in by_id], 'read_only': True,
        'percent_meaning': 'Absolute contribution share; not per-feature fraud risk.'}
