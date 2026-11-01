"""Bounded, read-only statistical signals over current movement versions.

Each insight runs in one REPEATABLE READ READ ONLY snapshot, is scoped to an
accounting period (optionally one customer), aggregates in SQL and returns at
most ``limit`` list items. Results are signals for human review: they never
change movement state, cases or invoices and are not findings of fraud.
Money is exact Decimal rendered as strings; statistics that need square roots,
logarithms or medians are floating point and say so in ``method``.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile

from sqlalchemy import text

PERIOD = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
MAX_LIMIT = 500
STATEMENT_TIMEOUT_MS = 120_000
OPTIONAL_FIELDS = ("expected_amount", "customer_age_days", "supplier_age_days", "cross_border")
HHI_HIGH, HHI_MODERATE = Decimal(2500), Decimal(1500)
MODIFIED_Z, MEAN_AD_SCALE = 0.6745, 1.253314
OUTLIER_Z, OUTLIER_MIN_SAMPLE = 3.5, 10
DUPLICATE_WINDOW_DAYS = 7
BENFORD_MIN_N, BENFORD_MIN_AMOUNT = 300, 10
BENFORD_CHI2_CRITICAL = 15.507  # chi-square, 8 degrees of freedom, alpha 0.05
# Nigrini (2012) first-digit MAD conformity ranges, upper bounds inclusive.
NIGRINI_FIRST_DIGIT = ((0.006, "close conformity"), (0.012, "acceptable conformity"),
                       (0.015, "marginally acceptable conformity"))
TREND_PERIODS = 12
AGE_BUCKETS = ("0-1d", "1-3d", "3-7d", ">7d")
POPULATION = "Movements in the period at their current version; pending movements without a version carry no amount"
LIMITATIONS = [
    "Statistical signal for human review, not proof of error or fraud",
    "Read-only: no movement, case or invoice state is changed by an insight",
    "Quarantined source rows have no canonical movement and are outside amount statistics",
]


def _money(value) -> str | None:
    return None if value is None else format(Decimal(value).quantize(Decimal("0.01")), "f")


def _ratio(numerator, denominator, places: int = 4) -> str | None:
    if not denominator:
        return None
    return format((Decimal(numerator) / Decimal(denominator)).quantize(Decimal(1).scaleb(-places)), "f")


def _pct(current, previous) -> str | None:
    if previous is None or current is None or Decimal(previous) == 0:
        return None
    return format(((Decimal(current) - Decimal(previous)) * 100 / Decimal(previous)).quantize(Decimal("0.01")), "f")


def _float(value, places: int = 6) -> float | None:
    return None if value is None else round(float(value), places)


def _time(value) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _validate(period, customer_id, limit):
    if not isinstance(period, str) or not PERIOD.fullmatch(period):
        raise ValueError("period must be YYYY-MM")
    if customer_id is not None and (not isinstance(customer_id, str) or not customer_id.strip() or len(customer_id) > 80):
        raise ValueError("customer_id must be 1..80 characters")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIMIT}")


def _periods_back(period: str, count: int) -> list[str]:
    year, month = map(int, period.split("-"))
    index = year * 12 + month - 1
    return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(index - count + 1, index + 1)]


def _envelope(name, method, at, period, customer_id, limitations=(), **data):
    return {"insight": name, "method": method,
            "scope": {"period": period, "customer_id": customer_id, "population": POPULATION,
                      "snapshot": "PostgreSQL REPEATABLE READ READ ONLY"},
            "limitations": LIMITATIONS + list(limitations), "generated_at": _time(at), **data}


def _current(customer_id, periods: str | None = None) -> str:
    """Index path: movements(period) or (customer_id, period), then the unique
    (movement_id, version_no) version. JSONB is read only for scoped rows."""
    where = f"m.period = ANY(:{periods})" if periods else "m.period = :period"
    if customer_id is not None:
        where += " AND m.customer_id = :customer_id"
    return f"""cur AS (
        SELECT m.id AS movement_id, m.external_id, m.customer_id, m.period, v.id AS version_id,
               v.version_no, v.amount, v.currency, v.origin, v.payload, v.payload->>'supplier_id' AS supplier_id
        FROM movements m JOIN movement_versions v ON v.movement_id = m.id AND v.version_no = m.current_version
        WHERE {where})"""


ASSESSED = ("LEFT JOIN assessments r ON r.version_id = cur.version_id AND r.kind = 'RULE' "
            "LEFT JOIN assessments mo ON mo.version_id = cur.version_id AND mo.kind = 'MODEL'")
# Ingestion normalises event_at to ISO-8601 with an offset; anything else is NULL.
EVENT_AT = r"CASE WHEN cur.payload->>'event_at' ~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}' THEN CAST(cur.payload->>'event_at' AS timestamptz) END"


@contextmanager
def _snapshot(db):
    if db.engine.dialect.name != "postgresql":
        raise ValueError("insights require PostgreSQL")
    # SET LOCAL may only tighten an operator-configured statement_timeout.
    configured = int(getattr(db.settings, "db_statement_timeout_ms", 0) or 0)
    timeout = min(STATEMENT_TIMEOUT_MS, configured) if configured > 0 else STATEMENT_TIMEOUT_MS
    with db.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text(f"SET LOCAL statement_timeout = {int(timeout)}"))
            yield connection, connection.execute(text("SELECT transaction_timestamp()")).scalar_one()


def _run(db, inner, period, customer_id, limit, **options):
    _validate(period, customer_id, limit)
    with _snapshot(db) as (connection, at):
        return inner(connection, at, period, customer_id, limit, **options)


def _overview(c, at, period, customer_id, limit):
    p = {"period": period, "customer_id": customer_id, "limit": limit}
    cw = "" if customer_id is None else " AND customer_id = :customer_id"
    movements, customers = c.execute(text(f"SELECT count(*), count(DISTINCT customer_id) FROM movements WHERE period = :period{cw}"), p).one()
    states = c.execute(text(f"SELECT state, count(*) FROM movements WHERE period = :period{cw} GROUP BY state ORDER BY count(*) DESC, state LIMIT 50"), p).all()
    money = c.execute(text(f"""WITH {_current(customer_id)}
        SELECT GROUPING(currency), currency, count(*), sum(amount), count(DISTINCT supplier_id)
        FROM cur GROUP BY GROUPING SETS ((currency), ()) ORDER BY 1 DESC, 2 LIMIT 21"""), p).all()
    total = next((row for row in money if row[0]), (1, None, 0, None, 0))
    by_currency = [row for row in money if not row[0]]
    flags = c.execute(text(f"""WITH {_current(customer_id)}
        SELECT count(*) FILTER (WHERE coalesce(r.flagged, false) OR coalesce(mo.flagged, false)),
               count(*) FILTER (WHERE r.flagged), count(*) FILTER (WHERE mo.flagged),
               count(*) FILTER (WHERE mo.score IS NOT NULL), count(*) FILTER (WHERE mo.id IS NOT NULL AND mo.score IS NULL),
               count(*) FILTER (WHERE mo.id IS NULL)
        FROM cur {ASSESSED}"""), p).one()
    qw = "" if customer_id is None else " AND validated_customer_id = :customer_id"
    q_rows, q_open = c.execute(text(f"SELECT count(*), count(*) FILTER (WHERE resolved_by_batch IS NULL) FROM quarantine WHERE period = :period{qw}"), p).one()
    reasons = c.execute(text(f"""SELECT reason, count(*), count(*) FILTER (WHERE resolved_by_batch IS NULL)
        FROM quarantine WHERE period = :period{qw} GROUP BY reason ORDER BY 2 DESC, reason LIMIT 10"""), p).all()
    versioned = int(total[2])
    return _envelope(
        "overview", "Exact SQL counts and Decimal sums; flagged = current version has a flagged RULE or MODEL assessment; flagged_rate = flagged / versioned movements",
        at, period, customer_id,
        ["total_net is NULL when the scope holds more than one currency; see net_by_currency",
         "Customer-scoped quarantine counts only rows whose customer_id was validated"],
        movements=int(movements), customers=int(customers), versioned_movements=versioned,
        movements_without_version=int(movements) - versioned,
        total_net=_money(total[3]) if len(by_currency) == 1 else None,
        net_by_currency=[{"currency": cur, "movements": int(n), "net_amount": _money(net)} for _, cur, n, net, _ in by_currency],
        distinct_suppliers=int(total[4]),
        state_mix=[{"state": state, "movements": int(n)} for state, n in states],
        flagged=int(flags[0]), rule_flagged=int(flags[1]), model_flagged=int(flags[2]),
        flagged_rate=_ratio(flags[0], versioned),
        model_scores={"scored": int(flags[3]), "unavailable": int(flags[4]), "not_assessed": int(flags[5])},
        quarantine={"rows": int(q_rows), "open": int(q_open),
                    "top_reasons": [{"reason": reason, "rows": int(n), "open": int(o)} for reason, n, o in reasons]},
    )


def _hhi_class(hhi: Decimal | None) -> str | None:
    if hhi is None:
        return None
    return "highly concentrated" if hhi > HHI_HIGH else "moderately concentrated" if hhi >= HHI_MODERATE else "unconcentrated"


def _supplier_concentration(c, at, period, customer_id, limit):
    """Share_i = amount_i / total; HHI = 10000 * sum(amount_i^2) / total^2 per
    customer and currency (0..10000), computed exactly in PostgreSQL numeric."""
    p = {"period": period, "customer_id": customer_id, "limit": limit}
    base = f"""WITH {_current(customer_id)},
        s AS (SELECT customer_id, currency, supplier_id, sum(amount) amount, count(*) n FROM cur
              WHERE supplier_id IS NOT NULL GROUP BY 1, 2, 3),
        t AS (SELECT customer_id, currency, sum(amount) total, count(*) suppliers, sum(n) movements,
                     max(amount) top_amount, sum(amount * amount) squares FROM s GROUP BY 1, 2),
        h AS (SELECT t.*, CASE WHEN total > 0 THEN round(10000 * squares / (total * total), 2) END hhi FROM t)"""
    suppliers = c.execute(text(base + """
        , g AS (SELECT supplier_id, currency, sum(amount) amount, sum(n) n, count(DISTINCT customer_id) customers FROM s GROUP BY 1, 2)
        SELECT supplier_id, currency, amount, n, customers,
               CASE WHEN sum(amount) OVER w > 0 THEN round(amount / sum(amount) OVER w, 6) END
        FROM g WINDOW w AS (PARTITION BY currency) ORDER BY amount DESC, supplier_id LIMIT :limit"""), p).all()
    customers = c.execute(text(base + """
        SELECT customer_id, currency, total, suppliers, movements, hhi,
               CASE WHEN total > 0 THEN round(top_amount / total, 6) END
        FROM h ORDER BY hhi DESC NULLS LAST, customer_id, currency LIMIT :limit"""), p).all()
    evaluated, high = c.execute(text(base + " SELECT count(*), count(*) FILTER (WHERE hhi > 2500) FROM h"), p).one()
    return _envelope(
        "supplier_concentration",
        "share = supplier amount / scope amount per currency; HHI = 10000 * sum(share^2) per customer and currency (exact numeric, rounded to 0.01); >2500 highly, 1500-2500 moderately concentrated (2010 US merger guideline bands)",
        at, period, customer_id,
        ["HHI measures dependency on few suppliers, not wrongdoing; a single-supplier customer is always 10000",
         "Movements without supplier_id are excluded"],
        top_suppliers=[{"supplier_id": s, "currency": cur, "net_amount": _money(a), "movements": int(n),
                        "customers": int(k), "share": None if sh is None else format(sh, "f")}
                       for s, cur, a, n, k, sh in suppliers],
        customers=[{"customer_id": cid, "currency": cur, "net_amount": _money(tot), "suppliers": int(ns),
                    "movements": int(n), "hhi": None if hhi is None else format(hhi, "f"),
                    "top_supplier_share": None if top is None else format(top, "f"),
                    "classification": _hhi_class(hhi), "flagged": hhi is not None and hhi > HHI_HIGH}
                   for cid, cur, tot, ns, n, hhi, top in customers],
        customers_evaluated=int(evaluated), customers_flagged=int(high), hhi_threshold=str(HHI_HIGH),
    )


def _duplicate_candidates(c, at, period, customer_id, limit, window_days=DUPLICATE_WINDOW_DAYS):
    """Consecutive movements of one customer, supplier, currency and exact
    amount whose event_at values are at most ``window_days`` apart (LAG)."""
    if type(window_days) is not int or not 0 <= window_days <= 92:
        raise ValueError("window_days must be an integer between 0 and 92")
    periods = _periods_back(period, 2)
    p = {"period": period, "customer_id": customer_id, "limit": limit, "periods": periods, "days": window_days}
    rows = c.execute(text(f"""WITH {_current(customer_id, 'periods')},
        e AS (SELECT cur.movement_id, cur.external_id, cur.version_id, cur.customer_id, cur.period, cur.currency,
                     cur.amount, cur.supplier_id, {EVENT_AT} event_at FROM cur WHERE cur.amount > 0 AND cur.supplier_id IS NOT NULL),
        w AS (SELECT e.*, lag(movement_id) OVER k prev_movement_id, lag(external_id) OVER k prev_external_id,
                     lag(version_id) OVER k prev_version_id, lag(event_at) OVER k prev_event_at
              FROM e WHERE event_at IS NOT NULL
              WINDOW k AS (PARTITION BY customer_id, supplier_id, currency, amount ORDER BY event_at, movement_id))
        SELECT customer_id, supplier_id, currency, amount, prev_movement_id, prev_external_id, prev_version_id, prev_event_at,
               movement_id, external_id, version_id, event_at, EXTRACT(EPOCH FROM event_at - prev_event_at) / 3600, count(*) OVER ()
        FROM w WHERE prev_movement_id IS NOT NULL AND period = :period AND event_at - prev_event_at <= make_interval(days => :days)
        ORDER BY amount DESC, customer_id, event_at, movement_id LIMIT :limit"""), p).all()
    return _envelope(
        "duplicate_candidates",
        f"Window LAG over (customer, supplier, currency, amount) ordered by event_at; pair reported when the gap is <= {window_days} days and the later movement is in the period",
        at, period, customer_id,
        ["The previous period is scanned only as look-back context; older history is not",
         "Three or more repeats appear as consecutive pairs",
         "Recurring legitimate charges of identical value are expected to appear"],
        window_days=window_days, total_pairs=int(rows[0][13]) if rows else 0,
        pairs=[{"customer_id": r[0], "supplier_id": r[1], "currency": r[2], "amount": _money(r[3]),
                "gap_hours": _float(r[12], 3),
                "first": {"movement_id": r[4], "external_id": r[5], "version_id": r[6], "event_at": _time(r[7])},
                "second": {"movement_id": r[8], "external_id": r[9], "version_id": r[10], "event_at": _time(r[11])}}
               for r in rows],
    )


def _amount_outliers(c, at, period, customer_id, limit, threshold=OUTLIER_Z, min_sample=OUTLIER_MIN_SAMPLE):
    """Modified z = 0.6745 * (x - median) / MAD with MAD = median(|x - median|)
    per supplier and currency (Iglewicz-Hoaglin). When MAD = 0 the mean absolute
    deviation is used: z = (x - median) / (1.253314 * MeanAD)."""
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 < threshold <= 100:
        raise ValueError("threshold must be a positive number")
    if type(min_sample) is not int or not 3 <= min_sample <= 100_000:
        raise ValueError("min_sample must be an integer between 3 and 100000")
    p = {"period": period, "customer_id": customer_id, "limit": limit, "threshold": float(threshold), "min_sample": min_sample}
    base = f"""WITH {_current(customer_id)},
        x AS (SELECT movement_id, external_id, version_id, customer_id, supplier_id, currency, amount FROM cur WHERE supplier_id IS NOT NULL),
        n AS (SELECT supplier_id, currency, count(*) n FROM x GROUP BY 1, 2)"""
    groups, skipped, sampled = c.execute(text(base + """
        SELECT count(*) FILTER (WHERE n >= :min_sample), count(*) FILTER (WHERE n < :min_sample),
               coalesce(sum(n) FILTER (WHERE n >= :min_sample), 0) FROM n"""), p).one()
    rows = c.execute(text(base + f""",
        g AS (SELECT supplier_id, currency, count(*) n, percentile_cont(0.5) WITHIN GROUP (ORDER BY amount::float8) med
              FROM x GROUP BY 1, 2 HAVING count(*) >= :min_sample),
        d AS (SELECT x.*, g.n, g.med, abs(x.amount::float8 - g.med) dev FROM x JOIN g USING (supplier_id, currency)),
        s AS (SELECT supplier_id, currency, percentile_cont(0.5) WITHIN GROUP (ORDER BY dev) mad, avg(dev) mean_ad FROM d GROUP BY 1, 2),
        z AS (SELECT d.*, s.mad, s.mean_ad,
                     CASE WHEN s.mad > 0 THEN {MODIFIED_Z} * (d.amount::float8 - d.med) / s.mad
                          WHEN s.mean_ad > 0 THEN (d.amount::float8 - d.med) / ({MEAN_AD_SCALE} * s.mean_ad) END z
              FROM d JOIN s USING (supplier_id, currency))
        SELECT movement_id, external_id, version_id, customer_id, supplier_id, currency, amount, n, med, mad, mean_ad, z, count(*) OVER ()
        FROM z WHERE abs(z) > :threshold ORDER BY abs(z) DESC, movement_id LIMIT :limit"""), p).all()
    return _envelope(
        "amount_outliers",
        f"Modified z-score per supplier and currency: 0.6745 * (amount - median) / MAD, MeanAD fallback when MAD = 0; |z| > {threshold}; groups need n >= {min_sample}",
        at, period, customer_id,
        ["Medians and z-scores are double precision; amounts are exact",
         "Suppliers below the minimum sample size are not evaluated",
         "A supplier with genuinely bimodal pricing produces expected outliers"],
        threshold=float(threshold), min_sample=min_sample, groups_evaluated=int(groups),
        groups_below_min_sample=int(skipped), movements_evaluated=int(sampled),
        total_outliers=int(rows[0][12]) if rows else 0,
        outliers=[{"movement_id": r[0], "external_id": r[1], "version_id": r[2], "customer_id": r[3],
                   "supplier_id": r[4], "currency": r[5], "amount": _money(r[6]), "supplier_movements": int(r[7]),
                   "supplier_median": _money(Decimal(repr(r[8]))), "mad": _float(r[9], 4),
                   "dispersion": "MAD" if r[9] else "MeanAD", "robust_z": _float(r[11], 4),
                   "direction": "high" if r[11] > 0 else "low"} for r in rows],
    )


def benford_statistics(counts: dict[int, int], min_n: int = BENFORD_MIN_N) -> dict:
    """First-digit test. E_d = n * log10(1 + 1/d); chi2 = sum (O_d - E_d)^2 / E_d
    with 8 degrees of freedom; MAD = mean_d |O_d/n - log10(1 + 1/d)|."""
    n = sum(counts.get(d, 0) for d in range(1, 10))
    expected = {d: math.log10(1 + 1 / d) for d in range(1, 10)}
    digits = [{"digit": d, "observed": int(counts.get(d, 0)), "expected_share": round(expected[d], 6),
               "observed_share": round(counts.get(d, 0) / n, 6) if n else None,
               "expected_count": round(n * expected[d], 3)} for d in range(1, 10)]
    if n < min_n:
        return {"status": "insufficient data", "n": n, "min_n": min_n, "digits": digits,
                "chi_square": None, "p_value": None, "mad": None, "conformity": None}
    chi2 = sum((counts.get(d, 0) - n * expected[d]) ** 2 / (n * expected[d]) for d in range(1, 10))
    # Chi-square survival function for even df = 2k: exp(-x/2) * sum_{i<k} (x/2)^i / i!
    half = chi2 / 2
    p_value = math.exp(-half) * sum(half ** i / math.factorial(i) for i in range(4))
    mad = sum(abs(counts.get(d, 0) / n - expected[d]) for d in range(1, 10)) / 9
    conformity = next((label for bound, label in NIGRINI_FIRST_DIGIT if mad <= bound), "nonconformity")
    return {"status": "computed", "n": n, "min_n": min_n, "digits": digits, "chi_square": round(chi2, 4),
            "degrees_of_freedom": 8, "chi_square_critical_0_05": BENFORD_CHI2_CRITICAL,
            "chi_square_rejects_at_0_05": chi2 > BENFORD_CHI2_CRITICAL, "p_value": round(p_value, 6),
            "mad": round(mad, 6), "conformity": conformity}


BENFORD_METHOD = (f"First significant digit of amounts >= {BENFORD_MIN_AMOUNT}; chi-square (df 8) and MAD against log10(1 + 1/d); "
                  "MAD conformity per Nigrini: <=0.006 close, <=0.012 acceptable, <=0.015 marginal, otherwise nonconformity")
BENFORD_LIMITATIONS = [f"Requires n >= {BENFORD_MIN_N}; otherwise 'insufficient data' with counts only",
                       "Priced services, fixed tariffs and narrow ranges do not follow Benford's law; nonconformity alone is not evidence",
                       "Chi-square rejects almost any real dataset at large n; MAD is the conformity criterion"]


def _benford(c, at, period, customer_id, limit, min_n=BENFORD_MIN_N):
    if type(min_n) is not int or min_n < 1:
        raise ValueError("min_n must be a positive integer")
    p = {"period": period, "customer_id": customer_id, "minimum": BENFORD_MIN_AMOUNT}
    rows = c.execute(text(f"""WITH {_current(customer_id)}
        SELECT CASE WHEN amount >= :minimum THEN CAST(left(CAST(CAST(floor(amount) AS bigint) AS text), 1) AS int) END d, count(*)
        FROM cur GROUP BY 1"""), p).all()
    counts = {int(d): int(n) for d, n in rows if d is not None}
    excluded = sum(int(n) for d, n in rows if d is None)
    return _envelope("benford", BENFORD_METHOD, at, period, customer_id, BENFORD_LIMITATIONS,
                     excluded_below_minimum_amount=excluded, **benford_statistics(counts, min_n))


def _series(rows, periods) -> list[dict]:
    """Zero-filled per-currency monthly series; change_pct = (x_t - x_{t-1}) / x_{t-1} * 100."""
    by = {(cur, p): (int(n), Decimal(total or 0)) for p, cur, n, total in rows}
    result = []
    for currency in sorted({cur for _, cur, _, _ in rows}):
        previous = None
        for p in periods:
            n, total = by.get((currency, p), (0, Decimal("0.00")))
            result.append({"period": p, "currency": currency, "movements": n, "net_amount": _money(total),
                           "net_change_pct": _pct(total, previous[1]) if previous else None,
                           "movement_change_pct": _pct(n, previous[0]) if previous else None})
            previous = (n, total)
    return result


def _movers(rows) -> list[dict]:
    return [{"customer_id": cid, "currency": cur, "net_amount": _money(now_total), "previous_net_amount": _money(prev_total),
             "net_change": _money(Decimal(now_total) - Decimal(prev_total)), "net_change_pct": _pct(now_total, prev_total),
             "movements": int(now_n), "previous_movements": int(prev_n)} for cid, cur, now_total, prev_total, now_n, prev_n in rows]


TREND_METHOD = ("Monthly exact Decimal totals and counts for the last 12 periods, zero-filled; change_pct = (x_t - x_{t-1}) / x_{t-1} * 100, "
                "NULL when x_{t-1} = 0; top movers ranked by |net change| between the period and the previous period")


def _trends(c, at, period, customer_id, limit):
    periods = _periods_back(period, TREND_PERIODS)
    p = {"period": period, "customer_id": customer_id, "limit": limit, "periods": periods,
         "pair": periods[-2:], "previous": periods[-2]}
    totals = c.execute(text(f"""WITH {_current(customer_id, 'periods')}
        SELECT period, currency, count(*), sum(amount) FROM cur GROUP BY 1, 2 ORDER BY 1, 2 LIMIT 240"""), p).all()
    movers = c.execute(text(f"""WITH {_current(customer_id, 'pair')},
        g AS (SELECT customer_id, currency, period, count(*) n, sum(amount) total FROM cur GROUP BY 1, 2, 3),
        a AS (SELECT * FROM g WHERE period = :period), b AS (SELECT * FROM g WHERE period = :previous)
        SELECT coalesce(a.customer_id, b.customer_id) cid, coalesce(a.currency, b.currency) cur,
               coalesce(a.total, 0), coalesce(b.total, 0), coalesce(a.n, 0), coalesce(b.n, 0)
        FROM a FULL JOIN b ON a.customer_id = b.customer_id AND a.currency = b.currency
        ORDER BY abs(coalesce(a.total, 0) - coalesce(b.total, 0)) DESC, cid, cur LIMIT :limit"""), p).all()
    series = {}
    if movers:
        p["ids"] = sorted({row[0] for row in movers})
        rows = c.execute(text(f"""WITH {_current(None, 'periods')}
            SELECT customer_id, period, currency, count(*), sum(amount) FROM cur WHERE customer_id = ANY(:ids)
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3 LIMIT :series_limit"""), {**p, "series_limit": len(p["ids"]) * TREND_PERIODS * 20}).all()
        for cid in p["ids"]:
            series[cid] = _series([row[1:] for row in rows if row[0] == cid], periods)
    return _envelope("trends", TREND_METHOD, at, period, customer_id,
                     ["Period totals reflect current versions now, not as reported at each month end"],
                     periods=periods, scope_series=_series(totals, periods), top_movers=_movers(movers),
                     mover_series=series)


def _risk_distribution(c, at, period, customer_id, limit):
    p = {"period": period, "customer_id": customer_id, "limit": limit}
    bins = dict(c.execute(text(f"""WITH {_current(customer_id)}
        SELECT LEAST(width_bucket(mo.score, 0, 1, 10), 10), count(*) FROM cur
        JOIN assessments mo ON mo.version_id = cur.version_id AND mo.kind = 'MODEL'
        WHERE mo.score IS NOT NULL GROUP BY 1"""), p).all())
    stats = c.execute(text(f"""WITH {_current(customer_id)}
        SELECT count(*), count(mo.id), count(mo.score), count(*) FILTER (WHERE mo.flagged),
               count(*) FILTER (WHERE mo.id IS NOT NULL AND mo.score IS NULL),
               avg(mo.score), percentile_cont(ARRAY[0.5, 0.9, 0.99]) WITHIN GROUP (ORDER BY mo.score)
        FROM cur LEFT JOIN assessments mo ON mo.version_id = cur.version_id AND mo.kind = 'MODEL'"""), p).one()
    reasons = c.execute(text(f"""WITH {_current(customer_id)}
        SELECT a.kind, left(reason, 240), count(*) FROM cur JOIN assessments a ON a.version_id = cur.version_id
        CROSS JOIN LATERAL jsonb_array_elements_text(a.reasons) AS reason
        GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT :limit"""), p).all()
    quantiles = stats[6] or [None, None, None]
    return _envelope(
        "risk_distribution",
        "Histogram of SCORED model scores in 10 equal-width bins [0,0.1) ... [0.9,1.0]; NULL scores are counted as unavailable or not assessed and never binned",
        at, period, customer_id,
        ["Model scores are demonstration-model outputs, not calibrated fraud probabilities",
         "Mean and quantiles are over scored versions only"],
        versions=int(stats[0]), scored=int(stats[2]), unavailable=int(stats[4]), not_assessed=int(stats[0]) - int(stats[1]),
        flagged=int(stats[3]), mean_score=_float(stats[5]),
        quantiles={"p50": _float(quantiles[0]), "p90": _float(quantiles[1]), "p99": _float(quantiles[2])},
        histogram=[{"bin": i, "lower": (i - 1) / 10, "upper": i / 10, "versions": int(bins.get(i, 0))} for i in range(1, 11)],
        top_reasons=[{"kind": kind, "reason": reason, "versions": int(n)} for kind, reason, n in reasons],
    )


def _review_ageing(c, at, period, customer_id, limit, as_of=None):
    if as_of is not None and (not isinstance(as_of, datetime) or as_of.tzinfo is None):
        raise ValueError("as_of must be a timezone-aware datetime")
    as_of = as_of or at
    cw = "" if customer_id is None else " AND customer_id = :customer_id"
    p = {"period": period, "customer_id": customer_id, "limit": limit, "as_of": as_of}
    bucket = ("CASE WHEN :as_of - created_at < interval '1 day' THEN '0-1d' WHEN :as_of - created_at < interval '3 days' THEN '1-3d' "
              "WHEN :as_of - created_at < interval '7 days' THEN '3-7d' ELSE '>7d' END")
    where = f"FROM review_cases WHERE status = 'OPEN' AND period = :period{cw}"
    buckets = dict(c.execute(text(f"SELECT {bucket}, count(*) {where} GROUP BY 1"), p).all())
    total, oldest = c.execute(text(f"SELECT count(*), min(created_at) {where}"), p).one()
    severity = c.execute(text(f"SELECT severity, count(*) {where} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 20"), p).all()
    reasons = c.execute(text(f"""SELECT reason, severity, count(*), min(created_at),
        count(*) FILTER (WHERE :as_of - created_at >= interval '7 days') {where}
        GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT :limit"""), p).all()
    return _envelope(
        "review_ageing", "Open review cases by age = as_of - created_at in buckets [0,1d), [1d,3d), [3d,7d), [7d,inf)",
        at, period, customer_id, ["Cases created after as_of are counted in 0-1d"],
        as_of=_time(as_of), open_cases=int(total), oldest_created_at=_time(oldest),
        oldest_age_hours=None if oldest is None else round((as_of - oldest).total_seconds() / 3600, 2),
        by_age=[{"bucket": b, "cases": int(buckets.get(b, 0))} for b in AGE_BUCKETS],
        by_severity=[{"severity": s, "cases": int(n)} for s, n in severity],
        by_reason=[{"reason": r, "severity": s, "cases": int(n), "oldest_created_at": _time(o), "older_than_7d": int(old)}
                   for r, s, n, o, old in reasons],
    )


def _data_quality(c, at, period, customer_id, limit):
    p = {"period": period, "customer_id": customer_id, "limit": limit}
    missing = ", ".join(f"count(*) FILTER (WHERE coalesce(payload->>'{f}', '') = '')" for f in OPTIONAL_FIELDS)
    fields = c.execute(text(f"WITH {_current(customer_id)} SELECT count(*), count(*) FILTER (WHERE origin <> 'SOURCE'), {missing} FROM cur"), p).one()
    versions = int(fields[0])
    rules = c.execute(text(f"""WITH {_current(customer_id)}
        SELECT reason, count(*) FROM cur JOIN assessments r ON r.version_id = cur.version_id AND r.kind = 'RULE'
        CROSS JOIN LATERAL jsonb_array_elements_text(r.reasons) AS reason GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT :limit"""), p).all()
    failing = c.execute(text(f"""WITH {_current(customer_id)}
        SELECT count(*) FILTER (WHERE r.status = 'FAIL'), count(*) FILTER (WHERE r.id IS NULL) FROM cur
        LEFT JOIN assessments r ON r.version_id = cur.version_id AND r.kind = 'RULE'"""), p).one()
    sw = "" if customer_id is None else " AND customer_id = :customer_id"
    sources = c.execute(text(f"SELECT source, count(*), count(DISTINCT movement_id) FROM source_records WHERE period = :period{sw} GROUP BY 1 ORDER BY 1"), p).all()
    qw = "" if customer_id is None else " AND q.validated_customer_id = :customer_id"
    quarantine = c.execute(text(f"""SELECT b.source, q.reason, count(*), count(*) FILTER (WHERE q.resolved_by_batch IS NULL)
        FROM quarantine q JOIN ingestion_batches b ON b.id = q.batch_id WHERE q.period = :period{qw}
        GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT :limit"""), p).all()
    return _envelope(
        "data_quality", "Exact counts: missing = key absent or empty string in the current version payload; mismatches = RULE assessment reasons on current versions",
        at, period, customer_id, ["Source record counts include every stored revision"],
        versions=versions, corrected_versions=int(fields[1]),
        missing_optional_fields=[{"field": f, "missing": int(n), "rate": _ratio(n, versions)} for f, n in zip(OPTIONAL_FIELDS, fields[2:])],
        rule_failures=int(failing[0]), versions_without_rule_assessment=int(failing[1]),
        source_mismatches=[{"reason": r, "versions": int(n)} for r, n in rules],
        source_records=[{"source": s, "records": int(n), "movements": int(m)} for s, n, m in sources],
        quarantine=[{"source": s, "reason": r, "rows": int(n), "open": int(o)} for s, r, n, o in quarantine],
    )


def overview(db, period, customer_id=None, limit=10):
    return _run(db, _overview, period, customer_id, limit)


def supplier_concentration(db, period, customer_id=None, limit=20):
    return _run(db, _supplier_concentration, period, customer_id, limit)


def duplicate_candidates(db, period, customer_id=None, limit=50, window_days=DUPLICATE_WINDOW_DAYS):
    return _run(db, _duplicate_candidates, period, customer_id, limit, window_days=window_days)


def amount_outliers(db, period, customer_id=None, limit=50, threshold=OUTLIER_Z, min_sample=OUTLIER_MIN_SAMPLE):
    return _run(db, _amount_outliers, period, customer_id, limit, threshold=threshold, min_sample=min_sample)


def benford(db, period, customer_id=None, limit=1, min_n=BENFORD_MIN_N):
    return _run(db, _benford, period, customer_id, limit, min_n=min_n)


def trends(db, period, customer_id=None, limit=10):
    return _run(db, _trends, period, customer_id, limit)


def risk_distribution(db, period, customer_id=None, limit=20):
    return _run(db, _risk_distribution, period, customer_id, limit)


def review_ageing(db, period, customer_id=None, limit=20, as_of=None):
    return _run(db, _review_ageing, period, customer_id, limit, as_of=as_of)


def data_quality(db, period, customer_id=None, limit=20):
    return _run(db, _data_quality, period, customer_id, limit)


SECTIONS = (("overview", _overview), ("supplier_concentration", _supplier_concentration),
            ("duplicate_candidates", _duplicate_candidates), ("amount_outliers", _amount_outliers),
            ("benford", _benford), ("trends", _trends), ("risk_distribution", _risk_distribution),
            ("review_ageing", _review_ageing), ("data_quality", _data_quality))


def report(db, period, customer_id=None, limit=20):
    """Every insight from one database snapshot, so sections reconcile."""
    _validate(period, customer_id, limit)
    with _snapshot(db) as (connection, at):
        sections = {name: inner(connection, at, period, customer_id, limit) for name, inner in SECTIONS}
    return _envelope("report", "All PostgreSQL insights evaluated in a single REPEATABLE READ READ ONLY snapshot",
                     at, period, customer_id, sections=sections)


# Offline history: DuckDB over verified analytics exports.

PART_NAME = re.compile(r"part-\d{5}\.parquet")
MEMORY = re.compile(r"\d+(\.\d+)?\s*(KB|MB|GB|TB|KiB|MiB|GiB|TiB)")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_exports(export_dir, verify_checksums: bool) -> list[tuple[dict, list[Path]]]:
    """Accept one export directory, one period partition or a root of
    ``period=YYYY-MM`` partitions. Roots and partitions read only
    pointer-published manifests; an explicit export directory is read as given.
    Manifests must be COMPLETE with matching hashes and Parquet row counts."""
    import pyarrow.parquet as pq
    root = Path(export_dir).resolve()
    if (root / "manifest.json").is_file():
        manifests = [root / "manifest.json"]
    else:
        pointers = [root / "latest.json"] if (root / "latest.json").is_file() else sorted(root.glob("period=*/latest.json"))
        manifests = []
        for pointer in pointers:
            data = json.loads(pointer.read_text())
            path = (pointer.parent / str(data.get("manifest", ""))).resolve()
            if path.parent.parent != pointer.parent.resolve() or not path.is_file() or _sha256(path) != data.get("manifest_sha256"):
                raise ValueError(f"export pointer {pointer} does not match its manifest")
            manifests.append(path)
    if not manifests:
        raise ValueError("no published analytics export found")
    exports = []
    for path in manifests:
        manifest = json.loads(path.read_text())
        if manifest.get("status") != "COMPLETE" or manifest.get("schema_version") != "current-movement-analytics-v1" \
                or not PERIOD.fullmatch(str(manifest.get("period", ""))):
            raise ValueError(f"{path} is not a complete current-movement analytics export")
        files = []
        for item in manifest["files"]:
            if not PART_NAME.fullmatch(str(item.get("path", ""))):
                raise ValueError(f"{path} lists an unexpected file name")
            part = path.parent / item["path"]
            if not part.is_file() or pq.read_metadata(part).num_rows != item["rows"]:
                raise ValueError(f"{part} is missing or has a different row count")
            if verify_checksums and _sha256(part) != item["sha256"]:
                raise ValueError(f"{part} does not match its manifest checksum")
            files.append(part)
        exports.append((manifest, files))
    periods = [manifest["period"] for manifest, _ in exports]
    if len(set(periods)) != len(periods):
        raise ValueError("more than one export per period was supplied")
    return exports


def insights_from_parquet(export_dir, customer_id=None, limit=50, *, period=None, window_days=DUPLICATE_WINDOW_DAYS,
                          memory_limit="1GB", threads=4, verify_checksums=True) -> dict:
    """Supplier concentration, duplicate candidates, Benford and trends over
    every published period with DuckDB, bounded by ``memory_limit`` and spilling
    to a private temporary directory. Formulas match the PostgreSQL insights;
    HHI and shares are double precision here."""
    import duckdb
    if customer_id is not None and (not isinstance(customer_id, str) or not customer_id.strip() or len(customer_id) > 80):
        raise ValueError("customer_id must be 1..80 characters")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    if type(window_days) is not int or not 0 <= window_days <= 92:
        raise ValueError("window_days must be an integer between 0 and 92")
    if not isinstance(memory_limit, str) or not MEMORY.fullmatch(memory_limit):
        raise ValueError("memory_limit must look like 512MB or 4GB")
    if type(threads) is not int or not 1 <= threads <= 64:
        raise ValueError("threads must be an integer between 1 and 64")
    exports = _verified_exports(export_dir, verify_checksums)
    files = [str(path) for _, paths in exports for path in paths]
    available = sorted(manifest["period"] for manifest, _ in exports)
    target = period or available[-1]
    if not PERIOD.fullmatch(str(target)):
        raise ValueError("period must be YYYY-MM")
    cw = "" if customer_id is None else " AND customer_id = $customer"
    base = f"""WITH m AS (SELECT movement_id, external_id, customer_id, period, version_id, amount, currency,
            json_extract_string(payload_json, '$.supplier_id') AS supplier_id,
            try_cast(json_extract_string(payload_json, '$.event_at') AS TIMESTAMPTZ) AS event_at
        FROM read_parquet($files, hive_partitioning = false) WHERE amount IS NOT NULL{cw})"""
    spill = Path(tempfile.mkdtemp(prefix="insights-duckdb-"))
    try:
        with duckdb.connect(":memory:", config={"memory_limit": memory_limit, "threads": str(threads),
                                                "temp_directory": str(spill), "preserve_insertion_order": "false"}) as con:
            con.execute("SET TimeZone = 'UTC'")

            def query(sql, **extra):
                params = {"files": files, **({"customer": customer_id} if customer_id is not None else {}), **extra}
                return con.execute(base + sql, {k: v for k, v in params.items() if "$" + k in base + sql}).fetchall()

            if verify_checksums:
                observed = {(p, cur): (int(n), _money(total)) for p, cur, n, total in con.execute(
                    "SELECT period, currency, count(*), sum(amount) FROM read_parquet($files, hive_partitioning = false) "
                    "WHERE currency IS NOT NULL GROUP BY 1, 2", {"files": files}).fetchall()}
                declared = {(manifest["period"], item["currency"]): (item["amount_rows"], item["net_amount"])
                            for manifest, _ in exports for item in manifest["aggregates_by_currency"] if item["currency"] is not None}
                if observed != declared:
                    raise ValueError("Parquet aggregates do not reconcile with their manifests")
            s = """, s AS (SELECT customer_id, currency, supplier_id, sum(amount) amount, count(*) n FROM m
                    WHERE supplier_id IS NOT NULL GROUP BY ALL)"""
            suppliers = query(s + """, g AS (SELECT supplier_id, currency, sum(amount) amount, sum(n) n, count(DISTINCT customer_id) k FROM s GROUP BY ALL)
                SELECT supplier_id, currency, amount, n, k, round(CAST(amount AS DOUBLE) / sum(CAST(amount AS DOUBLE)) OVER (PARTITION BY currency), 6)
                FROM g ORDER BY amount DESC, supplier_id LIMIT $limit""", limit=limit)
            hhi_sql = s + """, h AS (SELECT customer_id, currency, sum(amount) total, count(*) suppliers, sum(n) n,
                    CASE WHEN sum(amount) > 0 THEN round(10000 * sum(CAST(amount AS DOUBLE) ^ 2) / CAST(sum(amount) AS DOUBLE) ^ 2, 2) END hhi,
                    CASE WHEN sum(amount) > 0 THEN round(CAST(max(amount) AS DOUBLE) / CAST(sum(amount) AS DOUBLE), 6) END top
                FROM s GROUP BY ALL)"""
            customers = query(hhi_sql + " SELECT * FROM h ORDER BY hhi DESC NULLS LAST, customer_id, currency LIMIT $limit", limit=limit)
            evaluated, high = query(hhi_sql + " SELECT count(*), count(*) FILTER (WHERE hhi > 2500) FROM h")[0]
            pairs = query(""", w AS (SELECT *, lag(movement_id) OVER k pm, lag(external_id) OVER k pe, lag(version_id) OVER k pv, lag(event_at) OVER k pt
                    FROM m WHERE amount > 0 AND supplier_id IS NOT NULL AND event_at IS NOT NULL
                    WINDOW k AS (PARTITION BY customer_id, supplier_id, currency, amount ORDER BY event_at, movement_id))
                SELECT customer_id, supplier_id, currency, amount, pm, pe, pv, strftime(pt, '%Y-%m-%dT%H:%M:%S+00:00'),
                       movement_id, external_id, version_id, strftime(event_at, '%Y-%m-%dT%H:%M:%S+00:00'),
                       (epoch(event_at) - epoch(pt)) / 3600, count(*) OVER ()
                FROM w WHERE pm IS NOT NULL AND event_at - pt <= to_days(CAST($days AS INTEGER))
                ORDER BY amount DESC, customer_id, event_at, movement_id LIMIT $limit""", limit=limit, days=window_days)
            digits = query(f""" SELECT CASE WHEN amount >= {BENFORD_MIN_AMOUNT} THEN CAST(left(CAST(CAST(floor(amount) AS BIGINT) AS VARCHAR), 1) AS INTEGER) END, count(*)
                FROM m GROUP BY 1""")
            periods = _periods_back(target, TREND_PERIODS)
            totals = query(" SELECT period, currency, count(*), sum(amount) FROM m WHERE period IN (SELECT unnest($periods)) GROUP BY ALL ORDER BY 1, 2 LIMIT 240",
                           periods=periods)
            movers = query(""", g AS (SELECT customer_id, currency, period, count(*) n, sum(amount) total FROM m
                    WHERE period IN ($target, $previous) GROUP BY ALL),
                a AS (SELECT * FROM g WHERE period = $target), b AS (SELECT * FROM g WHERE period = $previous)
                SELECT coalesce(a.customer_id, b.customer_id) cid, coalesce(a.currency, b.currency) cur,
                       coalesce(a.total, 0), coalesce(b.total, 0), coalesce(a.n, 0), coalesce(b.n, 0)
                FROM a FULL JOIN b ON a.customer_id = b.customer_id AND a.currency = b.currency
                ORDER BY abs(coalesce(a.total, 0) - coalesce(b.total, 0)) DESC, cid, cur LIMIT $limit""",
                           target=target, previous=periods[-2], limit=limit)
    finally:
        shutil.rmtree(spill, ignore_errors=True)
    counts = {int(d): int(n) for d, n in digits if d is not None}
    generated = datetime.now(timezone.utc).isoformat()
    return {
        "insight": "parquet_history", "generated_at": generated,
        "method": f"DuckDB over verified analytics Parquet exports (memory_limit {memory_limit}, {threads} threads, spill to disk)",
        "scope": {"periods": available, "trend_period": target, "customer_id": customer_id,
                  "exports": [{"period": m["period"], "export_id": m["export_id"], "rows": m["rows"],
                               "snapshot_cutoff": m["snapshot_cutoff"]} for m, _ in exports],
                  "population": "Exported movements with an amount (current version at each export's snapshot)"},
        "limitations": LIMITATIONS + [
            "Each period is its own snapshot; periods exported at different times are not mutually consistent",
            "HHI and shares are double precision in DuckDB; amounts and totals are exact Decimal",
            "Duplicate look-back spans every supplied period, unlike the PostgreSQL insight",
        ] + BENFORD_LIMITATIONS,
        "supplier_concentration": {
            "top_suppliers": [{"supplier_id": sid, "currency": cur, "net_amount": _money(a), "movements": int(n),
                               "customers": int(k), "share": None if sh is None else f"{sh:.6f}"} for sid, cur, a, n, k, sh in suppliers],
            "customers": [{"customer_id": cid, "currency": cur, "net_amount": _money(tot), "suppliers": int(ns), "movements": int(n),
                           "hhi": None if hhi is None else f"{hhi:.2f}", "top_supplier_share": None if top is None else f"{top:.6f}",
                           "classification": _hhi_class(None if hhi is None else Decimal(f"{hhi:.2f}")),
                           "flagged": hhi is not None and hhi > HHI_HIGH} for cid, cur, tot, ns, n, hhi, top in customers],
            "customers_evaluated": int(evaluated), "customers_flagged": int(high), "hhi_threshold": str(HHI_HIGH)},
        "duplicate_candidates": {
            "window_days": window_days, "total_pairs": int(pairs[0][13]) if pairs else 0,
            "pairs": [{"customer_id": r[0], "supplier_id": r[1], "currency": r[2], "amount": _money(r[3]), "gap_hours": _float(r[12], 3),
                       "first": {"movement_id": r[4], "external_id": r[5], "version_id": r[6], "event_at": r[7]},
                       "second": {"movement_id": r[8], "external_id": r[9], "version_id": r[10], "event_at": r[11]}} for r in pairs]},
        "benford": {"method": BENFORD_METHOD, "excluded_below_minimum_amount": sum(int(n) for d, n in digits if d is None),
                    **benford_statistics(counts)},
        "trends": {"method": TREND_METHOD, "periods": periods, "scope_series": _series(totals, periods), "top_movers": _movers(movers)},
    }
