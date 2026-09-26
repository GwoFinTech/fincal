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


# ── EPS beat/miss streak (Issue #64) ───────────────────────────────────────
#
# The third derived metric of the detail panel, and the last one still counted
# without the Issue #61 rule: ``beat_miss_streak`` decided direction from
# ``actual > estimate`` and never asked whether the two numbers were comparable,
# so the panel rendered "不及预期 3季" for ASML FY2026 Q2 while the *same* row's
# 较预期 was already "—（预期与实际币种不同）".  In the default universe window
# 147 of 180 visible disclosed rows produced a directional count, and 247 of the
# 290 quarters those counts were built from were incomparable pairs.  A quarter is
# now counted only when its own pair passes
# :func:`app.fiscal.comparison_unavailable_reason`, and a quarter that fails
# neither counts nor gets crossed.

#: Break codes for a streak boundary that is *not* a comparability problem.  The
#: rest of the vocabulary is :mod:`app.fiscal`'s ``COMPARISON_*`` codes.
STREAK_BREAK_MISSING_VALUES = "missing_values"
STREAK_BREAK_DIRECTION_CHANGED = "direction_changed"
STREAK_BREAK_NOT_ADJACENT = "not_adjacent"


def _streak_period(row) -> dict | None:
    """The fiscal period of a streak row, shaped as the API reports it."""
    fiscal_year, fiscal_quarter = row.get("fiscal_year"), row.get("fiscal_quarter")
    if fiscal_year is None or fiscal_quarter is None:
        return None
    return {"fiscal_year": int(fiscal_year), "fiscal_quarter": int(fiscal_quarter)}


def _previous_period(fiscal_year: int, fiscal_quarter: int) -> tuple[int, int]:
    """The fiscal period immediately before ``(fiscal_year, fiscal_quarter)``."""
    return (fiscal_year - 1, 4) if fiscal_quarter == 1 else (fiscal_year, fiscal_quarter - 1)


def _no_streak(reason: str | None = None, period: dict | None = None,
               break_reason: str | None = None) -> dict:
    """No run can be stated — ``count`` stays 0 whenever ``reason`` is set.

    ``reason`` carries the comparability code of the quarter the panel is open
    on (its own estimate/actual pair cannot be compared, so no direction and no
    count exist); ``break_period``/``break_reason`` name the boundary the count
    stopped at, which is that same quarter in this case.
    """
    return {
        "kind": "unavailable", "count": 0, "reason": reason,
        "break_period": period,
        "break_reason": break_reason if break_reason is not None else reason,
    }


def _beat_miss_streak(rows, fiscal_year: int, fiscal_quarter: int) -> dict:
    """Count the contiguous comparable EPS beats/misses ending at one quarter.

    ``rows`` is the symbol's history, newest first and one row per fiscal period,
    whose first entry is the quarter the panel is open on: the run is counted
    *backwards* from that quarter, so a predicted/unreported quarter after it can
    neither be counted nor shorten it.

    A quarter extends the run only while it is

    * the immediate predecessor of the counted quarter (相邻财季 — a gap in the
      stored history ends the run instead of being crossed),
    * comparable per :func:`app.fiscal.comparison_unavailable_reason`,
    * decidable (both values present and different), and
    * in the same direction as the run.

    Anything else ends the run at that quarter and is reported in
    ``break_reason``/``break_period``.  A quarter that itself fails the rule is
    neither a beat nor a miss, so it can never be crossed by a count.
    """
    kind, count = None, 0
    break_period: dict | None = None
    break_reason: str | None = None
    expected = (fiscal_year, fiscal_quarter)
    for row in rows:
        period = _streak_period(row)
        current = None if period is None else (period["fiscal_year"], period["fiscal_quarter"])
        if current != expected:
            break_period, break_reason = period, STREAK_BREAK_NOT_ADJACENT
            break
        reason = fiscal.comparison_unavailable_reason(row)
        if reason:
            if count == 0:
                return _no_streak(reason, period)
            break_period, break_reason = period, reason
            break
        actual, estimate = as_decimal(row.get("eps_actual")), as_decimal(row.get("eps_estimate"))
        if actual is None or estimate is None or actual == estimate:
            if count == 0:
                return _no_streak(None, period, STREAK_BREAK_MISSING_VALUES)
            break_period, break_reason = period, STREAK_BREAK_MISSING_VALUES
            break
        row_kind = "beat" if actual > estimate else "miss"
        if kind is None:
            kind = row_kind
        elif row_kind != kind:
            break_period, break_reason = period, STREAK_BREAK_DIRECTION_CHANGED
            break
        count += 1
        expected = _previous_period(*expected)
    return {
        "kind": kind or "unavailable", "count": count, "reason": None,
        "break_period": break_period, "break_reason": break_reason,
    }


def build_decision_metrics(rows, earning_id):
    """Calculate actual-only growth and contiguous EPS beat/miss streak for one earning."""
    by_id = {row.get("id"): row for row in rows}
    current = by_id.get(earning_id)
    unavailable = _unavailable_growth()
    if not current or not current.get("fiscal_year") or not current.get("fiscal_quarter"):
        return {"actual_growth": unavailable, "beat_miss_streak": _no_streak(), "price_reaction": {"status": "unavailable", "reason": "no_reliable_provider_configured", "source": None}}
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
    # The run belongs to the quarter the panel is open on, so counting starts
    # there: rows newer than it (a rescheduled or predicted later quarter) are
    # not part of this quarter's streak and must not shorten it (Issue #64).
    start = next((index for index, row in enumerate(ordered) if row.get("id") == earning_id), None)
    streak = _no_streak() if start is None else _beat_miss_streak(ordered[start:], fy, fq)
    return {"actual_growth": growth, "beat_miss_streak": streak, "price_reaction": {"status": "unavailable", "reason": "no_reliable_provider_configured", "source": None}}
