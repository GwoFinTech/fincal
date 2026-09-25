"""Pure Phase 3 earnings decision transformations with explicit unavailable states."""
from decimal import Decimal

from . import fiscal


def as_decimal(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except Exception:
        return None


def rating_row(symbol, market, payload):
    """Map the documented Longbridge institution-rating summary to durable columns."""
    rating = payload.get("instratings") or {}
    evaluate = rating.get("evaluate") or {}
    return (
        symbol, market, rating.get("ccy_symbol"), as_decimal(rating.get("target")),
        int(evaluate.get("strong_buy") or 0), int(evaluate.get("buy") or 0),
        int(evaluate.get("hold") or 0), int(evaluate.get("under") or 0),
        int(evaluate.get("sell") or 0), rating.get("recommend"), rating.get("updated_at"),
        payload,
    )


def _series_change(rows, metric):
    values = [(row.get("captured_at"), as_decimal(row.get(metric))) for row in rows]
    values = [(captured, value) for captured, value in values if value is not None]
    if len(values) < 2:
        return {"first": None, "latest": values[-1][1] if values else None, "change": None, "direction": "unavailable"}
    first, latest = values[0][1], values[-1][1]
    change = latest - first
    return {"first": first, "latest": latest, "change": change, "direction": "up" if change > 0 else "down" if change < 0 else "flat"}


def revision_trend(snapshots):
    """Derive direction only from append-only estimate snapshots, never provider guesses."""
    ordered = sorted(snapshots, key=lambda row: str(row.get("captured_at") or ""))
    return {"status": "available" if len(ordered) >= 2 else "insufficient_history", "sample_count": len(ordered), "eps": _series_change(ordered, "eps_estimate"), "revenue": _series_change(ordered, "revenue_estimate")}


def _growth(current, prior):
    current, prior = as_decimal(current), as_decimal(prior)
    return None if current is None or prior in (None, Decimal("0")) else (current - prior) / abs(prior)


#: ``(response key, metric, span)`` of every derived growth the panel renders.
#: ``span`` decides which period the ratio is taken against: 同比 against the same
#: quarter one year earlier, 环比 against the previous quarter.
GROWTH_METRICS = (
    ("eps_yoy", "eps", "yoy"),
    ("eps_qoq", "eps", "qoq"),
    ("revenue_yoy", "revenue", "yoy"),
    ("revenue_qoq", "revenue", "qoq"),
)


def _unavailable_growth() -> dict:
    """Every growth key at ``None``, each paired with its own reason key.

    The reason keys are always present so a consumer never has to tell "the API
    did not send a reason" apart from "this pair is comparable".
    """
    growth: dict = {}
    for key, _metric, _span in GROWTH_METRICS:
        growth[key] = None
        growth[f"{key}_reason"] = None
    return growth


def _period_lookup(rows, earning_id):
    """Map each fiscal period to the row that represents it.

    Duplicate rows for one period used to silently overwrite each other in
    ``ORDER BY report_date`` order, so growth could be computed from a different
    row than the one the detail panel showed (Issue #50).  The period the user
    opened always keeps the row the user opened; every other period resolves to
    the same authoritative row the calendar/API/iCal show.
    """
    lookup: dict[tuple, dict] = {}
    for row in rows:
        fy, fq = row.get("fiscal_year"), row.get("fiscal_quarter")
        if fy is None or fq is None:
            continue
        key = (fy, fq)
        current = lookup.get(key)
        if current is None or row.get("id") == earning_id:
            lookup[key] = row
        elif current.get("id") != earning_id and fiscal.authority_key(row) < fiscal.authority_key(current):
            lookup[key] = row
    return lookup


def build_decision_metrics(rows, earning_id):
    """Calculate actual-only growth and contiguous EPS beat/miss streak for one earning."""
    by_id = {row.get("id"): row for row in rows}
    current = by_id.get(earning_id)
    unavailable = _unavailable_growth()
    if not current or not current.get("fiscal_year") or not current.get("fiscal_quarter"):
        return {"actual_growth": unavailable, "beat_miss_streak": {"kind": "unavailable", "count": 0}, "price_reaction": {"status": "unavailable", "reason": "no_reliable_provider_configured", "source": None}}
    fy, fq = int(current["fiscal_year"]), int(current["fiscal_quarter"])
    lookup = _period_lookup(rows, earning_id)
    yoy = lookup.get((fy - 1, fq))
    qoq = lookup.get((fy - 1, 4) if fq == 1 else (fy, fq - 1))
    growth = {}
    for key, metric, span in GROWTH_METRICS:
        prior = yoy if span == "yoy" else qoq
        field = fiscal.GROWTH_VALUE_FIELDS[metric]
        # Issue #63: two periods' actuals may only be subtracted when they are
        # attributed to the same currency/basis. Otherwise no ratio is returned
        # at all — a value here is rendered as a percentage and must never be a
        # cross-currency/cross-basis artefact.
        reason = fiscal.growth_unavailable_reason(current, prior, metric)
        growth[key] = None if reason else _growth(current.get(field), prior.get(field) if prior else None)
        growth[f"{key}_reason"] = reason
    # One row per fiscal period: a duplicated period must not be counted twice in
    # the streak, and must not hide the streak behind a staleness mismatch.
    ordered = sorted(lookup.values(), key=lambda row: str(row.get("report_date") or ""), reverse=True)
    kind, count = None, 0
    for row in ordered:
        actual, estimate = as_decimal(row.get("eps_actual")), as_decimal(row.get("eps_estimate"))
        if actual is None or estimate is None or actual == estimate:
            if row.get("id") == earning_id:
                break
            continue
        row_kind = "beat" if actual > estimate else "miss"
        if kind is None:
            kind = row_kind
        if row_kind != kind:
            break
        count += 1
    return {"actual_growth": growth, "beat_miss_streak": {"kind": kind or "unavailable", "count": count}, "price_reaction": {"status": "unavailable", "reason": "no_reliable_provider_configured", "source": None}}
