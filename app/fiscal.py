"""Fiscal-period identity and authority for ``earnings`` rows (Issue #50).

The ``earnings`` table is keyed by ``(symbol, market, report_date, report_type)``
— a *display* key that changes every time a provider reschedules an event.  The
persistent identity of an earnings event is the fiscal period
``(symbol, market, fiscal_year, fiscal_quarter)``: when Longbridge and Futu
disagree about the announcement day, or a provider moves a confirmed date, the
naive ``report_date`` key inserts a *second* row for the same period instead of
updating the existing one.  Left alone, one period is then rendered twice with
contradictory values, and every outlet picks its own winner:

* iCal collapsed by fiscal UID (Issue #40) and used ``_authority_key``,
* ``/api/earnings`` and CSV/JSON export returned **both** rows,
* ``phase3.build_decision_metrics`` overwrote a period's entry with whichever
  row happened to come last in ``ORDER BY report_date``.

This module is the single source of truth for the identity and for *which* row
represents a period, so the read paths (API, export, iCal, derived metrics) and
the reconciliation script all agree.  The write paths use
:func:`collapse_rows_by_period` / :func:`reschedule_confirmed_rows` to update the
period's existing row instead of inserting a new one.

Note on value arbitration: when two rows of one period carry *different* actuals,
picking the value is a product decision (Issue #50 risk section — the source
priority alone would promote suspicious Futu magnitudes).  The read paths
therefore only collapse rows and never merge conflicting values; the
reconciliation script preserves every dropped value in a backup table.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

#: Fields that together identify an earnings event across the whole system.
IDENTITY_FIELDS = ("symbol", "market", "fiscal_year", "fiscal_quarter")

#: Catch-all report type used by every write path when no other type is known.
DEFAULT_REPORT_TYPE = "Q"


def fiscal_key(parts_or_row) -> tuple | None:
    """Return the persistent fiscal identity of a row/mapping, or ``None``.

    ``None`` means the row carries no usable fiscal period (missing symbol,
    market, year or quarter).  Such rows keep the date-based behaviour they had
    before this module existed — they can never be proven to be duplicates.
    """
    if isinstance(parts_or_row, dict):
        get = parts_or_row.get
    else:
        # A 4-tuple/dict-like object (symbol, market, fiscal_year, fiscal_quarter)
        return fiscal_key_from_parts(*parts_or_row[:4])
    symbol, market = get("symbol"), get("market")
    fy, fq = get("fiscal_year"), get("fiscal_quarter")
    return fiscal_key_from_parts(symbol, market, fy, fq)


def fiscal_key_from_parts(symbol, market, fiscal_year, fiscal_quarter) -> tuple | None:
    """Return ``(symbol, market, fiscal_year, fiscal_quarter)`` or ``None``."""
    if not symbol or not market:
        return None
    if fiscal_year in (None, "") or fiscal_quarter in (None, ""):
        return None
    try:
        return (str(symbol), str(market), int(fiscal_year), int(fiscal_quarter))
    except (TypeError, ValueError):
        return None


def report_date_of(row, report_date=None) -> date | None:
    """Coerce a row/argument report date to ``date`` (``None`` when unusable)."""
    value = report_date if report_date is not None else row.get("report_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def has_actuals(row) -> bool:
    """True when the row carries a reported EPS or revenue actual."""
    return row.get("eps_actual") is not None or row.get("revenue_actual") is not None


def authority_key(row, report_date=None) -> tuple:
    """Rank the rows that share one fiscal period; the smallest wins.

    Ordering, and why (Issue #50):

    1. confirmed before predicted — a prediction must never mask real data;
    2. rows carrying actuals before rows without — a ``scheduled`` row must not
       hide the same period's reported actual (2068.HK / 2600.HK in the issue);
    3. newest ``report_date`` — the provider's own latest announcement day, so
       the displayed date no longer depends on *when* each sync happened to run;
    4. newest ``updated_at`` and then highest ``id`` — deterministic tie-breaks.

    Deliberately *not* part of the ranking: ``date_source`` priority.  Promoting
    Futu over Longbridge here is exactly the arbitration #50 requires a sanity
    guard and a product decision for, so it stays out of the read path.
    """
    predicted = 1 if row.get("is_predicted") else 0
    reported = 0 if has_actuals(row) else 1
    row_date = report_date_of(row, report_date)
    date_key = -row_date.toordinal() if row_date else 0
    ts = row.get("updated_at") or row.get("created_at")
    ts_key = -ts.timestamp() if isinstance(ts, datetime) else 0
    row_id = row.get("id")
    id_key = -(row_id if isinstance(row_id, int) else 0)
    return (predicted, reported, date_key, ts_key, id_key)


def sort_key(row) -> tuple:
    """Deterministic ``ORDER BY report_date, market, symbol`` equivalent."""
    row_date = report_date_of(row)
    return (
        "" if row_date is None else row_date.isoformat(),
        str(row.get("market") or ""),
        str(row.get("symbol") or ""),
    )


def collapse_fiscal_duplicates(rows: list[dict]) -> list[dict]:
    """Collapse rows sharing a fiscal period down to one authoritative row.

    Rows without a fiscal identity are passed through untouched.  The winner is
    chosen with :func:`authority_key` and keeps its own field values (no
    cross-row merging, no invented arbitration).  Output stays in the API's
    documented ``report_date, market, symbol`` order.
    """
    kept: list[dict] = []
    positions: dict[tuple, int] = {}
    for row in rows:
        key = fiscal_key(row)
        if key is None:
            kept.append(row)
            continue
        position = positions.get(key)
        if position is None:
            positions[key] = len(kept)
            kept.append(row)
        elif authority_key(row) < authority_key(kept[position]):
            kept[position] = row
    return sorted(kept, key=sort_key)


def collapse_rows_by_period(rows: list, identity_of, date_of) -> list:
    """Write-path variant of :func:`collapse_fiscal_duplicates` for raw tuples.

    One provider response can hold the same fiscal period at two dates (adjacent
    calendar windows overlap).  Inserting both would create a duplicate row, so
    the period's newest candidate survives; candidates without a fiscal identity
    are kept as-is (they are already de-duplicated by their natural key).
    """
    kept: list = []
    positions: dict[tuple, int] = {}
    for row in rows:
        key = identity_of(row)
        if key is None:
            kept.append(row)
            continue
        position = positions.get(key)
        if position is None:
            positions[key] = len(kept)
            kept.append(row)
            continue
        if _is_newer(date_of(row), date_of(kept[position])):
            kept[position] = row
    return kept


def _is_newer(left, right) -> bool:
    if left in (None, ""):
        return False
    if right in (None, ""):
        return True
    return str(left) > str(right)


def _identity_query() -> str:
    return (
        "SELECT e.id, e.symbol, e.market, e.fiscal_year, e.fiscal_quarter,"
        " e.report_date, e.report_type, e.is_predicted,"
        " e.eps_actual, e.revenue_actual, e.updated_at"
        " FROM earnings e"
        " JOIN (VALUES %s) AS v(symbol, market, fiscal_year, fiscal_quarter)"
        " ON e.symbol = v.symbol AND e.market = v.market"
        " AND e.fiscal_year = v.fiscal_year AND e.fiscal_quarter = v.fiscal_quarter"
        " WHERE e.is_predicted = FALSE"
    )


def reschedule_confirmed_rows(cur, rows, *, symbol=0, market=1, report_date=3,
                              report_type=4, fiscal_year=5, fiscal_quarter=6) -> list[dict]:
    """Move each fiscal period's confirmed row onto the provider's new date.

    ``rows`` are the raw provider tuples about to be upserted (the default
    indices match the batch layout used by ``sync_earnings`` and ``sync_futu``:
    ``symbol, market, company_name, report_date, report_type, fiscal_year,
    fiscal_quarter, ...``).

    Without this step a rescheduled event is *inserted* as a second row, because
    the table's unique key is the report date.  The row chosen to move is the one
    :func:`authority_key` already shows for that period, so the visible event
    follows the provider instead of gaining a twin.  Returns the moves performed
    (for logging/audit); no-op when the period already sits on the new date, or
    when another row already occupies it (then the caller's ``ON CONFLICT``
    merge handles it).
    """
    from psycopg2.extras import execute_values

    entries: dict[tuple, dict] = {}
    for row in rows:
        candidate = {
            "symbol": row[symbol],
            "market": row[market],
            "fiscal_year": row[fiscal_year],
            "fiscal_quarter": row[fiscal_quarter],
            "report_date": row[report_date],
            "report_type": row[report_type] or DEFAULT_REPORT_TYPE,
        }
        key = fiscal_key(candidate)
        if key is None or report_date_of(candidate) is None:
            continue
        current = entries.get(key)
        if current is None or _is_newer(candidate["report_date"], current["report_date"]):
            entries[key] = candidate

    if not entries:
        return []

    execute_values(cur, _identity_query(), [list(k) for k in entries])
    existing: dict[tuple, list[dict]] = {}
    for row in cur.fetchall():
        row = dict(row)
        key = fiscal_key(row)
        if not key or row.get("report_date") is None:
            continue
        existing.setdefault(key, []).append(row)

    moves: list[dict] = []
    for key, entry in entries.items():
        group = existing.get(key) or []
        if not group:
            continue
        winner = min(group, key=authority_key)
        new_date = report_date_of(entry)
        if new_date is None or winner["report_date"] == new_date:
            continue
        if any(
            row["id"] != winner["id"]
            and row["report_date"] == new_date
            and row.get("report_type") == entry["report_type"]
            for row in group
        ):
            continue
        cur.execute(
            "UPDATE earnings SET report_date = %s, updated_at = NOW()"
            " WHERE id = %s AND report_date = %s",
            (new_date, winner["id"], winner["report_date"]),
        )
        if cur.rowcount:
            moves.append({
                "id": winner["id"],
                "symbol": key[0],
                "market": key[1],
                "fiscal_year": key[2],
                "fiscal_quarter": key[3],
                "from": winner["report_date"].isoformat(),
                "to": new_date.isoformat(),
            })
    return moves
