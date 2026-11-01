"""Versioned, strictly row-local features for the demonstration risk model.

Financial validation and reconciliation remain separate deterministic controls.
Float conversion happens only at the numerical-model boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math

FEATURE_SCHEMA_VERSION = "movement-risk-v1"
FEATURE_NAMES = (
    "log_amount",
    "amount_to_expected",
    "log_customer_age_days",
    "log_supplier_age_days",
    "night_hour",
    "cross_border",
)
FEATURE_DESCRIPTIONS = {
    "log_amount": {"label": "Movement amount", "transformation": "log(1 + amount)"},
    "amount_to_expected": {"label": "Amount relative to expected amount", "transformation": "amount / expected_amount"},
    "log_customer_age_days": {"label": "Customer age", "transformation": "log(1 + customer_age_days)"},
    "log_supplier_age_days": {"label": "Supplier age", "transformation": "log(1 + supplier_age_days)"},
    "night_hour": {"label": "Night-time indicator", "transformation": "1 when UTC hour is 00:00–05:59; otherwise 0"},
    "cross_border": {"label": "Cross-border indicator", "transformation": "Declared binary cross_border input"},
}


def feature_raw_values(payload: dict) -> dict:
    """Preserve supplied evidence; never invert floating-point transformations."""
    return {
        "log_amount": str(payload["amount"]),
        "amount_to_expected": {"amount": str(payload["amount"]), "expected_amount": str(payload["expected_amount"])},
        "log_customer_age_days": int(payload["customer_age_days"]),
        "log_supplier_age_days": int(payload["supplier_age_days"]),
        "night_hour": {"event_at": payload["event_at"], "hour_utc": parse_event_at(payload["event_at"]).hour},
        "cross_border": int(payload["cross_border"]),
    }


def parse_event_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("event_at must be an ISO timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("event_at must be an ISO timestamp with a timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("event_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _money(payload: dict, name: str, *, positive: bool = False) -> Decimal:
    raw = payload.get(name)
    if raw is None or isinstance(raw, (bool, float)) or not isinstance(raw, (str, int, Decimal)):
        raise ValueError(f"{name} must be an exact decimal string")
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} is not a decimal") from exc
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    if value.as_tuple().exponent < -2:
        raise ValueError(f"{name} must have at most two decimal places")
    return value


def _integer(payload: dict, name: str, *, maximum: int | None = None) -> int:
    raw = payload.get(name)
    if isinstance(raw, bool) or raw is None or not isinstance(raw, (str, int)):
        raise ValueError(f"{name} must be a nonnegative integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a nonnegative integer") from exc
    if isinstance(raw, str) and str(value) != raw.strip():
        raise ValueError(f"{name} must be a nonnegative integer")
    if value < 0 or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is outside the permitted range")
    return value


def build_features(payload: dict) -> dict[str, float]:
    """Use only declared information available on this movement at assessment.

    Labels, row IDs, customer IDs, later review outcomes, scenario names and
    other rows are deliberately never read. No missing-value imputation is
    permitted by schema v1; unavailable inputs must produce UNAVAILABLE scores.
    """
    amount = _money(payload, "amount")
    expected = _money(payload, "expected_amount", positive=True)
    customer_age = _integer(payload, "customer_age_days")
    supplier_age = _integer(payload, "supplier_age_days")
    cross_border = _integer(payload, "cross_border", maximum=1)
    timestamp = parse_event_at(payload.get("event_at"))
    if payload.get("hour_utc") not in (None, ""):
        supplied_hour = _integer(payload, "hour_utc", maximum=23)
        if supplied_hour != timestamp.hour:
            raise ValueError("hour_utc conflicts with event_at in UTC")
    try:
        values = (
            math.log1p(float(amount)),
            float(amount / expected),
            math.log1p(customer_age),
            math.log1p(supplier_age),
            float(timestamp.hour < 6),
            float(cross_border),
        )
    except (ValueError, ArithmeticError) as exc:
        raise ValueError("feature transformation is outside the numerical range") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError("feature transformation must be finite")
    return dict(zip(FEATURE_NAMES, values, strict=True))
