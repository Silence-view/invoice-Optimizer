"""Small read-only card projection of the case's retained version.

No model loading, inference or filesystem reads occur on the paginated list.
An explanatory threshold is shown only from a coherent persisted receipt;
the detailed explanation separately verifies the frozen model artifact.
"""
import math
from decimal import Decimal, InvalidOperation


def _probability(value):
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def _money(value):
    # Normalized source amounts and database NUMERIC values are exact decimals.
    # Do not manufacture precision by converting an unexpected JSON float.
    if not isinstance(value, (str, Decimal)):
        return None
    try:
        number = Decimal(value)
        return str(value) if number.is_finite() else None
    except InvalidOperation:
        return None


def _text(value, limit=240):
    return value[:limit] if isinstance(value, str) else None


def _reasons(assessment):
    reasons = assessment.reasons if assessment else None
    return [r[:500] for r in reasons if isinstance(r, str)][:5] if isinstance(reasons, list) else []


def case_card(version, rule, model):
    payload = version.payload if version and isinstance(version.payload, dict) else {}
    sources = payload.get('source_values')
    sources = sources if isinstance(sources, dict) else {}
    a, b = sources.get('A'), sources.get('B')
    a = a if isinstance(a, dict) else {}
    b = b if isinstance(b, dict) else {}
    score = model.score if model and model.status == 'SCORED' and _probability(model.score) else None
    receipt = model.explanation if model and isinstance(model.explanation, dict) else {}
    threshold = receipt.get('threshold')
    coherent = bool(score is not None and receipt.get('status') == 'AVAILABLE'
        and model.model_version and model.policy_version
        and receipt.get('model_version') == model.model_version
        and receipt.get('policy_version') == model.policy_version
        and _probability(receipt.get('score')) and receipt['score'] == score
        and _probability(threshold) and model.flagged == (score >= threshold))
    return {
        'transaction': {
            'amount': _money(version.amount) if version else None,
            'currency': version.currency if version else None,
            'source_a_amount': _money(a.get('amount')),
            'source_b_amount': _money(b.get('amount')),
            'source_a_currency': _text(a.get('currency'), 3),
            'source_b_currency': _text(b.get('currency'), 3),
            'description': _text(payload.get('description')),
            'event_at': _text(payload.get('event_at'), 64),
            'supplier_id': _text(payload.get('supplier_id'), 160),
        },
        'model': {
            'status': model.status if model else 'NOT_RECORDED',
            'recorded_score': score,
            'flagged': model.flagged if model else None,
            'model_version': model.model_version if model else None,
            'policy_version': model.policy_version if model else None,
            'threshold': threshold if coherent else None,
            'threshold_status': 'RECORDED_RECEIPT' if coherent else 'UNAVAILABLE',
            'recorded_reasons': _reasons(model),
        },
        'rule': {
            'status': rule.status if rule else 'NOT_RECORDED',
            'recorded_reasons': _reasons(rule),
        },
    }
