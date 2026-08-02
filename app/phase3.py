"""Pure Phase 3 earnings decision transformations with explicit unavailable states."""
from decimal import Decimal


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


def build_decision_metrics(rows, earning_id):
    """Calculate actual-only growth and contiguous EPS beat/miss streak for one earning."""
    by_id = {row.get("id"): row for row in rows}
    current = by_id.get(earning_id)
    unavailable = {"eps_yoy": None, "eps_qoq": None, "revenue_yoy": None, "revenue_qoq": None}
    if not current or not current.get("fiscal_year") or not current.get("fiscal_quarter"):
        return {"actual_growth": unavailable, "beat_miss_streak": {"kind": "unavailable", "count": 0}, "price_reaction": {"status": "unavailable", "reason": "no_reliable_provider_configured", "source": None}}
    fy, fq = int(current["fiscal_year"]), int(current["fiscal_quarter"])
    lookup = {(r.get("fiscal_year"), r.get("fiscal_quarter")): r for r in rows}
    yoy = lookup.get((fy - 1, fq))
    qoq = lookup.get((fy - 1, 4) if fq == 1 else (fy, fq - 1))
    growth = {
        "eps_yoy": _growth(current.get("eps_actual"), yoy.get("eps_actual") if yoy else None),
        "eps_qoq": _growth(current.get("eps_actual"), qoq.get("eps_actual") if qoq else None),
        "revenue_yoy": _growth(current.get("revenue_actual"), yoy.get("revenue_actual") if yoy else None),
        "revenue_qoq": _growth(current.get("revenue_actual"), qoq.get("revenue_actual") if qoq else None),
    }
    ordered = sorted(rows, key=lambda row: str(row.get("report_date") or ""), reverse=True)
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
