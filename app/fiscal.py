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

Issue #52 hardening: ``reschedule_confirmed_rows`` moves a period's row onto the
provider's new date, but the table's unique key is the *display* key
``(symbol, market, report_date, report_type)``, which a **different** fiscal
period (or a predicted row) may already own.  Each move is therefore validated
against that natural key and wrapped in a savepoint, so one unmovable row can
never abort a whole sync run.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
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


# ── Estimate/actual comparability (Issue #61) ──────────────────────────────
#
# Every display of a "surprise" (``(actual - estimate) / |estimate|``) used to be
# unconditional, yet the two figures come from different providers with different
# attribution: the estimate is Longbridge's calendar figure in the listing's quote
# currency, while the actual is often Futu's financial statement in the *reporting*
# currency (TSM: USD estimate vs TWD actual, ×32; BABA/PDD/NIO/XPEV: USD vs CNY,
# ×6.8).  For US small caps the same row carried two numbers 5×–1800× apart with
# same currency code (KTOS -0.00512 vs 5.540795), i.e. a base/unit difference the
# API could not describe at all.  A row now claims its attribution and this module
# decides whether the two numbers may be subtracted; the reason codes are
# language-independent, so no user-visible prose lives in the API.

#: Label meaning "the provider did not state this".
UNKNOWN_ATTRIBUTION = "unknown"

#: Row-level reason codes for :func:`comparison_unavailable_reason`.
COMPARISON_CURRENCY_UNKNOWN = "currency_unknown"
COMPARISON_CURRENCY_MISMATCH = "currency_mismatch"
COMPARISON_BASIS_MISMATCH = "basis_mismatch"
COMPARISON_BASIS_UNVERIFIED = "basis_unverified"


def declared_attribution(row, field: str) -> str | None:
    """Return a row's currency/basis label, or ``None`` when it is not declared.

    ``None`` (missing, empty or the explicit ``unknown`` marker) means the
    provider never said which currency/base the number is in — callers must treat
    it as "unattributed" rather than assume the listing's default.
    """
    value = row.get(field)
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == UNKNOWN_ATTRIBUTION:
        return None
    return text.upper()


def has_comparable_values(row) -> bool:
    """True when the row carries at least one complete estimate/actual pair."""
    return (
        row.get("eps_estimate") is not None and row.get("eps_actual") is not None
    ) or (
        row.get("revenue_estimate") is not None and row.get("revenue_actual") is not None
    )


def _same_declared_source(row) -> bool:
    estimate_source = row.get("estimate_source")
    actual_source = row.get("actual_source")
    if not estimate_source or not actual_source:
        return False
    return str(estimate_source).strip().lower() == str(actual_source).strip().lower()


def comparison_unavailable_reason(row) -> str | None:
    """Why the row's estimate/actual pair must not be subtracted (Issue #61).

    ``None`` means the pair is attributed consistently — same known currency, and
    either the same stated basis on both sides or one provider computing both
    numbers — so a surplus percentage describes a real difference.  Anything else
    returns a language-independent reason code:

    * ``currency_unknown`` — the estimate or the actual carries no currency, so
      the two numbers cannot be proven to be the same unit of money;
    * ``currency_mismatch`` — both sides are attributed and differ (quote currency
      against reporting currency);
    * ``basis_mismatch`` — both sides state a basis and the bases differ
      (GAAP against adjusted);
    * ``basis_unverified`` — different providers (or an unattributed source) and
      at least one side does not state its basis, so a GAAP-vs-adjusted or
      per-share-unit difference cannot be ruled out.

    Rows without a complete estimate/actual pair return ``None``: there is nothing
    to compare, and the read paths already render a missing value as "—".
    """
    if not has_comparable_values(row):
        return None
    estimate_currency = declared_attribution(row, "estimate_currency")
    actual_currency = declared_attribution(row, "actual_currency")
    if estimate_currency is None or actual_currency is None:
        return COMPARISON_CURRENCY_UNKNOWN
    if estimate_currency != actual_currency:
        return COMPARISON_CURRENCY_MISMATCH
    estimate_basis = declared_attribution(row, "estimate_basis")
    actual_basis = declared_attribution(row, "actual_basis")
    if estimate_basis and actual_basis:
        return None if estimate_basis == actual_basis else COMPARISON_BASIS_MISMATCH
    # At least one side left its basis unstated. Within one provider that is
    # harmless (that provider produced both numbers the same way); across
    # providers it means the two figures are not proven to share a basis.
    return None if _same_declared_source(row) else COMPARISON_BASIS_UNVERIFIED


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


#: Savepoint guarding a single reschedule UPDATE (Issue #52).
_RESCHEDULE_SAVEPOINT = "fincal_reschedule"


@dataclass
class RescheduleOutcome:
    """What one :func:`reschedule_confirmed_rows` pass changed and left alone.

    ``moves`` are the rows that were re-dated onto the provider's date;
    ``skipped`` records the periods that could **not** be re-dated because
    another row already owns the target
    ``(symbol, market, report_date, report_type)`` key.  A skip is a data
    conflict to be logged/reconciled, not a failure: before Issue #52 the
    corresponding ``UPDATE`` raised ``UniqueViolation`` and aborted the synced
    run after ~10.8k of ~17.8k rows.
    """

    moves: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _target_holder(cur, symbol, market, new_date, report_type) -> dict | None:
    """Return the row already occupying a natural key, whoever it belongs to.

    ``earnings`` is unique on ``(symbol, market, report_date, report_type)`` —
    the *display* key — so the occupant decides whether a date move is possible
    at all.  Unlike :func:`_identity_query` this query deliberately filters
    neither by fiscal period (a row of another quarter is exactly the collision
    in Issue #52) nor by ``is_predicted`` (a prediction holds the same unique
    key and would raise just the same).
    """
    cur.execute(
        "SELECT e.id, e.fiscal_year, e.fiscal_quarter, e.is_predicted"
        " FROM earnings e"
        " WHERE e.symbol = %s AND e.market = %s AND e.report_date = %s"
        " AND e.report_type = %s"
        " LIMIT 1",
        (symbol, market, new_date, report_type),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def reschedule_confirmed_rows(cur, rows, *, symbol=0, market=1, report_date=3,
                              report_type=4, fiscal_year=5, fiscal_quarter=6) -> RescheduleOutcome:
    """Move each fiscal period's confirmed row onto the provider's new date.

    ``rows`` are the raw provider tuples about to be upserted (the default
    indices match the batch layout used by ``sync_earnings`` and ``sync_futu``:
    ``symbol, market, company_name, report_date, report_type, fiscal_year,
    fiscal_quarter, ...``).

    Without this step a rescheduled event is *inserted* as a second row, because
    the table's unique key is the report date.  The row chosen to move is the one
    :func:`authority_key` already shows for that period, so the visible event
    follows the provider instead of gaining a twin.

    A move is issued only while the target natural key is free: the unique
    constraint is ``(symbol, market, report_date, report_type)``, while the
    period grouping below is ``(symbol, market, fiscal_year, fiscal_quarter)``,
    so another quarter's row (or a prediction) may already sit on the provider's
    date — updating onto it raised ``UniqueViolation`` and killed the run
    (Issue #52).  Such a period is reported in ``skipped`` and the caller's
    ``ON CONFLICT`` merge handles the incoming row instead.  Every ``UPDATE``
    additionally runs inside a savepoint, so a conflict that slips past the
    check (a concurrent writer) degrades to one skipped period instead of a
    failed batch.  Returns the :class:`RescheduleOutcome` for logging/audit.
    """
    from psycopg2 import errors as pg_errors
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

    outcome = RescheduleOutcome()
    if not entries:
        return outcome

    execute_values(cur, _identity_query(), [list(k) for k in entries])
    existing: dict[tuple, list[dict]] = {}
    for row in cur.fetchall():
        row = dict(row)
        key = fiscal_key(row)
        if not key or row.get("report_date") is None:
            continue
        existing.setdefault(key, []).append(row)

    for key, entry in entries.items():
        group = existing.get(key) or []
        if not group:
            continue
        winner = min(group, key=authority_key)
        new_date = report_date_of(entry)
        if new_date is None or winner["report_date"] == new_date:
            continue
        record = {
            "id": winner["id"],
            "symbol": key[0],
            "market": key[1],
            "fiscal_year": key[2],
            "fiscal_quarter": key[3],
            "report_type": entry["report_type"],
            "from": winner["report_date"].isoformat(),
            "to": new_date.isoformat(),
        }
        cur.execute(f"SAVEPOINT {_RESCHEDULE_SAVEPOINT}")
        try:
            holder = _target_holder(cur, key[0], key[1], new_date, entry["report_type"])
            if holder is not None:
                # The provider's date is owned by another period (or by a
                # prediction).  Leave both rows and their fiscal labels alone;
                # the caller's ON CONFLICT merge still lands the incoming values.
                outcome.skipped.append({**record, "reason": "target_occupied", "holder": holder})
            else:
                cur.execute(
                    "UPDATE earnings SET report_date = %s, updated_at = NOW()"
                    " WHERE id = %s AND report_date = %s",
                    (new_date, winner["id"], winner["report_date"]),
                )
                if cur.rowcount:
                    outcome.moves.append(record)
        except pg_errors.UniqueViolation:
            cur.execute(f"ROLLBACK TO SAVEPOINT {_RESCHEDULE_SAVEPOINT}")
            outcome.skipped.append({**record, "reason": "unique_violation", "holder": None})
        cur.execute(f"RELEASE SAVEPOINT {_RESCHEDULE_SAVEPOINT}")

    for skip in outcome.skipped:
        logger.warning(
            "reschedule skipped %s.%s FY%s Q%s: %s → %s (row %s, %s, held by %s)",
            skip["symbol"], skip["market"], skip["fiscal_year"], skip["fiscal_quarter"],
            skip["from"], skip["to"], skip["id"], skip["reason"], skip["holder"],
        )
    return outcome
