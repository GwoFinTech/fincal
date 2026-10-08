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
period's existing row instead of inserting a new one; ``predict_earnings``
re-dates a superseded *prediction* the same way (Issue #60), which is why
:func:`authority_key` ranks two pure predictions by their latest computation.

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

# ── Report type: one period may carry two Longbridge events ────────────────
#
# Longbridge publishes *two* calendar events for the same fiscal period of many
# companies and both declare ``period='4'``:
#
# * ``period_type='qf'/'3q'`` — 业绩公布, the quarterly release (quarterly figures);
# * ``period_type='saf'`` / ``'af'`` — 半年报/年报 disclosure, whose figures are
#   the half-year / full-year totals (DEA 2026-02-21 qf revenue 87.7M against
#   2026-02-23 af revenue 334M for the same ``FY2025 Q4``).
#
# The persistent identity is the fiscal period, so the two events cannot both
# own it.  The product convention is that the **release** row represents the
# period and a disclosure never becomes its own row; ``report_type`` records
# which kind of event a row came from so both the read path and the write path
# can apply that rule instead of guessing from dates.

REPORT_TYPE_QUARTERLY = "Q"
REPORT_TYPE_HALF_YEAR = "H"
REPORT_TYPE_ANNUAL = "A"

#: Longbridge ``ext.financial_report.period_type`` → persisted ``report_type``.
PERIOD_TYPE_REPORT_TYPES = {"qf": REPORT_TYPE_QUARTERLY, "3q": REPORT_TYPE_QUARTERLY,
                            "saf": REPORT_TYPE_HALF_YEAR, "af": REPORT_TYPE_ANNUAL}

#: Report types that are a disclosure *about* a period, not its release.
DISCLOSURE_REPORT_TYPES = frozenset({REPORT_TYPE_HALF_YEAR, REPORT_TYPE_ANNUAL})


def report_type_for_period_type(period_type) -> str:
    """Map a Longbridge ``period_type`` to the persisted ``report_type``."""
    return PERIOD_TYPE_REPORT_TYPES.get(str(period_type or "").strip().lower(),
                                        DEFAULT_REPORT_TYPE)


def is_disclosure(report_type) -> bool:
    """True when ``report_type`` is a half-year/annual disclosure, not a release."""
    return str(report_type or "").strip().upper() in DISCLOSURE_REPORT_TYPES


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

    Ordering, and why (Issue #50, extended by Issue #60 and the report-type rule):

    1. confirmed before predicted — a prediction must never mask real data;
    2. a quarterly **release** row before a half-year/annual **disclosure** row —
       Longbridge emits both events for one period (``qf/4`` quarterly figures and
       ``af`` full-year figures, both ``period='4'``); the release is the period's
       event, so the disclosure must not win the identity even though its date is
       later (DEA: qf 2026-02-21 revenue 87.7M vs af 2026-02-23 revenue 334M);
    3. rows carrying actuals before rows without — a ``scheduled`` row must not
       hide the same period's reported actual (2068.HK / 2600.HK in the issue);
    4. for two *pure predictions* (algorithm rows without actuals) the newest
       ``updated_at`` — a prediction is derived data and the most recent
       computation is the authoritative one.  A corrected prediction may
       legitimately move to an *earlier* date (Issue #60 fixes dates that were
       invented from averaged months), and the older row only survives because
       the table's key is the display date; ranking by date first would keep
       showing the stale one.  Rows carrying provider data are unaffected: for
       them this slot is always ``0``;
    5. newest ``report_date`` — the provider's own latest announcement day, so
       the displayed date no longer depends on *when* each sync happened to run;
    6. newest ``updated_at`` and then highest ``id`` — deterministic tie-breaks.

    Deliberately *not* part of the ranking: ``date_source`` priority.  Promoting
    Futu over Longbridge here is exactly the arbitration #50 requires a sanity
    guard and a product decision for, so it stays out of the read path.
    """
    predicted = 1 if row.get("is_predicted") else 0
    disclosure = 1 if is_disclosure(row.get("report_type")) else 0
    reported = 0 if has_actuals(row) else 1
    ts = row.get("updated_at") or row.get("created_at")
    # Only a pure prediction (algorithm-owned, no actuals) ranks by recency first.
    pure_prediction = predicted and not has_actuals(row)
    recency = -ts.timestamp() if pure_prediction and isinstance(ts, datetime) else 0
    row_date = report_date_of(row, report_date)
    date_key = -row_date.toordinal() if row_date else 0
    ts_key = -ts.timestamp() if isinstance(ts, datetime) else 0
    row_id = row.get("id")
    id_key = -(row_id if isinstance(row_id, int) else 0)
    return (predicted, disclosure, reported, recency, date_key, ts_key, id_key)


def sort_key(row) -> tuple:
    """Deterministic ``ORDER BY report_date, market, symbol`` equivalent."""
    row_date = report_date_of(row)
    return (
        "" if row_date is None else row_date.isoformat(),
        str(row.get("market") or ""),
        str(row.get("symbol") or ""),
    )


def fiscal_label_consistent(symbol, market, fiscal_year, fiscal_quarter,
                            report_date, existing_rows) -> bool:
    """Return whether a fiscal label preserves date order for one symbol/year.

    A higher fiscal quarter must not be dated before a lower quarter of the same
    fiscal year, and a lower quarter must not be dated after a higher one.  The
    check is deliberately conservative: incomplete rows or a different symbol,
    market, or fiscal year do not provide evidence to reject the candidate.
    """
    candidate = report_date_of({}, report_date)
    try:
        candidate_quarter = int(fiscal_quarter)
        candidate_year = int(fiscal_year)
    except (TypeError, ValueError):
        return True
    if candidate is None or not symbol or not market:
        return True

    for row in existing_rows or ():
        if row.get("symbol") != symbol or row.get("market") != market:
            continue
        try:
            row_year = int(row.get("fiscal_year"))
            row_quarter = int(row.get("fiscal_quarter"))
        except (TypeError, ValueError):
            continue
        if row_year != candidate_year or row_quarter == candidate_quarter:
            continue
        row_date = report_date_of(row)
        if row_date is None:
            continue
        if row_quarter < candidate_quarter and row_date > candidate:
            return False
        if row_quarter > candidate_quarter and row_date < candidate:
            return False
    return True


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


def collapse_rows_by_period(rows: list, identity_of, date_of, rank_of=None, dropped=None) -> list:
    """Write-path variant of :func:`collapse_fiscal_duplicates` for raw tuples.

    One provider response can hold the same fiscal period at two dates (adjacent
    calendar windows overlap).  Inserting both would create a duplicate row, so
    the period's newest candidate survives; candidates without a fiscal identity
    are kept as-is (they are already de-duplicated by their natural key).

    ``rank_of`` (optional) returns the row's report-type rank: a lower rank wins
    before the date is compared, so a quarterly release beats the later-dated
    annual disclosure of the same period instead of being replaced by it.

    ``dropped`` (optional) is a list the discarded candidates are appended to, so
    a caller can report how many rows the collapse removed and why (Issue #50
    follow-up: a dropped half-year/annual disclosure of a period whose release is
    in the same response is not a duplicate to be silent about).
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
        incumbent = kept[position]
        if rank_of is not None:
            new_rank, old_rank = rank_of(row), rank_of(incumbent)
            if new_rank != old_rank:
                if new_rank < old_rank:
                    kept[position] = row
                    if dropped is not None:
                        dropped.append(incumbent)
                elif dropped is not None:
                    dropped.append(row)
                continue
        if _is_newer(date_of(row), date_of(incumbent)):
            kept[position] = row
            if dropped is not None:
                dropped.append(incumbent)
        elif dropped is not None:
            dropped.append(row)
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


def _same_actual_source(first_row, second_row) -> bool:
    """True when one provider wrote both rows' actuals."""
    first_source = first_row.get("actual_source")
    second_source = second_row.get("actual_source")
    if not first_source or not second_source:
        return False
    return str(first_source).strip().lower() == str(second_source).strip().lower()


#: The derived metrics that compare two different fiscal periods' actuals.
GROWTH_VALUE_FIELDS = {"eps": "eps_actual", "revenue": "revenue_actual"}


def growth_unavailable_reason(current_row, prior_row, metric: str = "eps") -> str | None:
    """Why two periods' actuals must not be turned into a growth percentage (#63).

    The cross-period counterpart of :func:`comparison_unavailable_reason`: 同比
    (same quarter, prior year) and 环比 (previous quarter) subtract the same
    metric from two **different** rows, so the row-level contract does not cover
    them — production rendered ``+5647.7%`` for TSM while the same panel called
    the same row's estimate/actual pair non-comparable.

    ``None`` means the two actuals are attributed consistently — a declared,
    identical currency, and either the same stated basis on both sides or both
    numbers written by one provider — so the ratio describes a real change.
    Otherwise the same language-independent reason codes as the row-level rule
    are returned (``currency_unknown`` / ``currency_mismatch`` /
    ``basis_mismatch`` / ``basis_unverified``).

    ``metric`` selects the compared column (``eps`` → ``eps_actual``,
    ``revenue`` → ``revenue_actual``).  A missing row or a missing value returns
    ``None``: there is no pair to compute, which the read paths already render as
    "—" without it being a comparability problem.
    """
    try:
        field = GROWTH_VALUE_FIELDS[metric]
    except KeyError:  # pragma: no cover - a typo must never silently compare EPS
        raise ValueError(f"unknown growth metric: {metric!r}") from None
    if not current_row or not prior_row:
        return None
    if current_row.get(field) is None or prior_row.get(field) is None:
        return None
    current_currency = declared_attribution(current_row, "actual_currency")
    prior_currency = declared_attribution(prior_row, "actual_currency")
    if current_currency is None or prior_currency is None:
        return COMPARISON_CURRENCY_UNKNOWN
    if current_currency != prior_currency:
        return COMPARISON_CURRENCY_MISMATCH
    current_basis = declared_attribution(current_row, "actual_basis")
    prior_basis = declared_attribution(prior_row, "actual_basis")
    if current_basis and prior_basis:
        return None if current_basis == prior_basis else COMPARISON_BASIS_MISMATCH
    # At least one period left its basis unstated. One provider computing both
    # actuals is harmless; across providers the two figures are not proven to
    # share a basis (GAAP against adjusted, per-share against per-ADR).
    return None if _same_actual_source(current_row, prior_row) else COMPARISON_BASIS_UNVERIFIED


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

    ``moves`` are the rows that were re-dated onto the provider's date (a
    ``kind='takeover'`` move additionally promotes a stored half-year/annual
    disclosure row to the period's quarterly release); ``skipped`` records the
    periods that could **not** be re-dated because another row already owns the
    target ``(symbol, market, report_date, report_type)`` key.  A skip is a data
    conflict to be logged/reconciled, not a failure: before Issue #52 the
    corresponding ``UPDATE`` raised ``UniqueViolation`` and aborted the synced
    run after ~10.8k of ~17.8k rows.

    ``rows`` are the caller's batch rows that may still be written (possibly
    re-typed by ``align_report_type``) and ``dropped`` the incoming rows this
    pass removed because their fiscal period is already represented: the caller
    must insert ``rows``, not its own list, or one period gets two rows again
    (Issue #50 follow-up).
    """

    moves: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    rows: list = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)


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


def _entry_precedes(candidate: dict, incumbent: dict) -> bool:
    """True when an incoming batch entry should represent its fiscal period.

    A quarterly release outranks a half-year/annual disclosure of the same
    period, so the annual event's later date cannot make it the period's row;
    among entries of the same kind the newest report date wins.
    """
    candidate_disclosure = is_disclosure(candidate.get("report_type"))
    incumbent_disclosure = is_disclosure(incumbent.get("report_type"))
    if candidate_disclosure != incumbent_disclosure:
        return not candidate_disclosure
    return _is_newer(candidate.get("report_date"), incumbent.get("report_date"))


def _takeover_values(entry: dict) -> tuple:
    """Bind values for :data:`_TAKEOVER_ASSIGNMENT` (report type then figures)."""
    values: list = list(entry.get("values") or ())
    while len(values) < 4:
        values.append(None)
    return (entry["report_type"], *values[:4])


#: SET clause that promotes a stored disclosure row to the period's release row.
#: Incoming figures win (`COALESCE` keeps the disclosure's number only where the
#: release event itself carried none), and the previous values stay recoverable
#: through ``earnings_estimate_snapshots``/the reconciliation backup tables.
_TAKEOVER_ASSIGNMENT = (
    "report_type = %s,"
    " eps_estimate = COALESCE(%s, eps_estimate),"
    " eps_actual = COALESCE(%s, eps_actual),"
    " revenue_estimate = COALESCE(%s, revenue_estimate),"
    " revenue_actual = COALESCE(%s, revenue_actual),"
    " updated_at = NOW()"
)


def reschedule_confirmed_rows(cur, rows, *, symbol=0, market=1, report_date=3,
                              report_type=4, fiscal_year=5, fiscal_quarter=6,
                              value_fields=(7, 8, 9, 10), takeover=True,
                              align_report_type=False) -> RescheduleOutcome:
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
    failed batch.

    Report-type direction (Issue #50 follow-up): a fiscal period is one row and
    its quarterly **release** owns it, so

    * an incoming half-year/annual **disclosure** is never written for a period
      whose release row exists (stored or in this same batch) — the caller must
      insert ``outcome.rows``, which excludes it, and ``dropped`` says why;
    * a stored release row is never re-dated onto a disclosure's date;
    * an incoming release **takes over** a stored disclosure row
      (``report_type`` and the release's own figures, recorded as
      ``kind='takeover'``), so the calendar shows the quarter's figures rather
      than the disclosure's half-year/annual totals.

    Returns the :class:`RescheduleOutcome` for logging/audit.

    ``align_report_type`` is for callers whose rows carry a date and actuals but
    no period sequence (``sync_futu`` writes ``report_type='Q'``): their rows
    inherit the report type the period already stores, so the upsert updates the
    period's row instead of inserting a twin for it.  Such callers also pass
    ``takeover=False`` and ``value_fields=()``, meaning no stored row changes its
    report type or values — a date/actual provider never promotes or demotes a
    period's accounting label.
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
            "values": tuple(row[index] if index < len(row) else None for index in value_fields),
        }
        key = fiscal_key(candidate)
        if key is None or report_date_of(candidate) is None:
            continue
        current = entries.get(key)
        if current is None or _entry_precedes(candidate, current):
            entries[key] = candidate

    outcome = RescheduleOutcome(rows=list(rows))
    if not entries:
        return outcome

    # In-batch rule: a disclosure loses to the release of its own period, even
    # when no row is stored yet (Longbridge returns both events in one window).
    dropped_entries: set[tuple] = set()
    for key, entry in entries.items():
        if is_disclosure(entry["report_type"]):
            continue
        for row in rows:
            if not is_disclosure(row[report_type]):
                continue
            if fiscal_key_from_parts(row[symbol], row[market], row[fiscal_year], row[fiscal_quarter]) != key:
                continue
            mark = (key, str(row[report_date]), str(row[report_type] or "").upper())
            if mark in dropped_entries:
                continue
            dropped_entries.add(mark)
            outcome.dropped.append({
                "id": None,
                "symbol": row[symbol],
                "market": row[market],
                "fiscal_year": row[fiscal_year],
                "fiscal_quarter": row[fiscal_quarter],
                "report_type": str(row[report_type] or "").upper(),
                "report_date": str(row[report_date]),
                "reason": "release_in_same_batch",
                "holder": {"id": None, "report_type": entry["report_type"]},
            })

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
        # A date/actual provider (Futu) has no period sequence of its own: give
        # its row the report type the period stores, so the upsert updates that
        # row instead of inserting a twin for the period.
        if align_report_type and winner.get("report_type"):
            entry["report_type"] = str(winner["report_type"]).strip().upper()
        new_date = report_date_of(entry)
        if new_date is None:
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
        if is_disclosure(entry["report_type"]) and not is_disclosure(winner.get("report_type")):
            # The period's quarterly release already owns the row, so this
            # disclosure is not a row of its own — and it must not drag the
            # release onto its own date (Longbridge dates the annual report a
            # day or two after the release).
            outcome.dropped.append({
                **record, "reason": "release_row_owns_period",
                "report_date": new_date.isoformat(),
                "holder": {"id": winner["id"], "report_type": winner.get("report_type")},
            })
            dropped_entries.add((key, str(entry["report_date"]), str(entry["report_type"] or "").upper()))
            continue
        # An incoming release takes the period over from a stored disclosure row:
        # the release's own figures replace the half-year/annual totals that the
        # disclosure had left on the row.
        takeover = takeover and not is_disclosure(entry["report_type"]) and is_disclosure(winner.get("report_type"))
        if winner["report_date"] == new_date:
            if takeover:
                cur.execute(f"SAVEPOINT {_RESCHEDULE_SAVEPOINT}")
                try:
                    cur.execute(
                        "UPDATE earnings SET " + _TAKEOVER_ASSIGNMENT + " WHERE id = %s",
                        (*_takeover_values(entry), winner["id"]),
                    )
                    if cur.rowcount:
                        outcome.moves.append({**record, "kind": "takeover"})
                except pg_errors.Error as exc:  # pragma: no cover - defensive
                    cur.execute(f"ROLLBACK TO SAVEPOINT {_RESCHEDULE_SAVEPOINT}")
                    outcome.skipped.append({**record, "reason": "takeover_failed", "holder": str(exc)})
                cur.execute(f"RELEASE SAVEPOINT {_RESCHEDULE_SAVEPOINT}")
            continue
        cur.execute(f"SAVEPOINT {_RESCHEDULE_SAVEPOINT}")
        try:
            holder = _target_holder(cur, key[0], key[1], new_date, entry["report_type"])
            if holder is not None:
                # The provider's date is owned by another period (or by a
                # prediction).  Leave both rows and their fiscal labels alone;
                # the caller's ON CONFLICT merge still lands the incoming values.
                outcome.skipped.append({**record, "reason": "target_occupied", "holder": holder})
            elif takeover:
                cur.execute(
                    "UPDATE earnings SET report_date = %s, " + _TAKEOVER_ASSIGNMENT
                    + " WHERE id = %s AND report_date = %s",
                    (new_date, *_takeover_values(entry), winner["id"], winner["report_date"]),
                )
                if cur.rowcount:
                    outcome.moves.append({**record, "kind": "takeover"})
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

    if dropped_entries:
        outcome.rows = [
            row for row in rows
            if (fiscal_key_from_parts(row[symbol], row[market], row[fiscal_year], row[fiscal_quarter]),
                str(row[report_date]), str(row[report_type] or "").upper()) not in dropped_entries
        ]
    elif align_report_type:
        outcome.rows = _retyped_rows(rows, entries, symbol=symbol, market=market,
                                     fiscal_year=fiscal_year, fiscal_quarter=fiscal_quarter,
                                     report_type=report_type)
    for drop in outcome.dropped:
        logger.info(
            "dropped %s disclosure %s.%s FY%s Q%s (%s): %s",
            drop["report_type"], drop["symbol"], drop["market"], drop["fiscal_year"],
            drop["fiscal_quarter"], drop["report_date"], drop["reason"],
        )
    for skip in outcome.skipped:
        logger.warning(
            "reschedule skipped %s.%s FY%s Q%s: %s → %s (row %s, %s, held by %s)",
            skip["symbol"], skip["market"], skip["fiscal_year"], skip["fiscal_quarter"],
            skip["from"], skip["to"], skip["id"], skip["reason"], skip["holder"],
        )
    return outcome


def _retyped_rows(rows: list, entries: dict, *, symbol: int, market: int, fiscal_year: int,
                  fiscal_quarter: int, report_type: int) -> list:
    """Copy the batch rows that had their report type aligned to the stored one."""
    aligned: list = []
    for row in rows:
        entry = entries.get(
            fiscal_key_from_parts(row[symbol], row[market], row[fiscal_year], row[fiscal_quarter])
        )
        if entry is None or str(entry["report_type"] or "").upper() == str(row[report_type] or "").upper():
            aligned.append(row)
            continue
        row = list(row)
        row[report_type] = entry["report_type"]
        aligned.append(tuple(row))
    return aligned
