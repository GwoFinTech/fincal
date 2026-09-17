#!/usr/bin/env python3
"""Predict future earnings dates from historical patterns.

Logic:
- For each symbol+market, examine historical earnings grouped by quarter
- Find the latest reported (fiscal_year, fiscal_quarter) pair
- Compute the next expected quarter: (fy, fq) → (fy, fq+1) or (fy+1, 1)
- Use the median month/day from historical same-quarter data for the prediction
- Mark with is_predicted=TRUE; confirmed data from sync overwrites later
"""
import logging
import sys
import os
import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from collections import defaultdict
import statistics

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor
from app.symbol import is_dirty_hk_5digit, normalize
from app.watchlist import get_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# How many future quarters ahead to predict (max 4 = ~1 year)
MAX_PREDICT_AHEAD = 4
# Don't predict more than this many days into the future
MAX_FUTURE_DAYS = 420


def next_quarter(fy: int, fq: int) -> tuple[int, int]:
    """Given (fiscal_year, fiscal_quarter), return the next quarter."""
    if fq < 4:
        return (fy, fq + 1)
    else:
        return (fy + 1, 1)


def predict_for_symbol(symbol: str, market: str) -> int:
    """Predict next earnings date(s) for a single symbol."""
    with db_cursor() as cur:
        cur.execute(
            """SELECT report_date, fiscal_year, fiscal_quarter, before_after, is_predicted
            FROM earnings
            WHERE symbol = %s AND market = %s AND fiscal_year IS NOT NULL AND fiscal_quarter IS NOT NULL
            ORDER BY report_date""",
            (symbol, market),
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    # Separate confirmed vs predicted
    confirmed: dict[tuple[int, int], dict] = {}
    predicted: dict[tuple[int, int], dict] = {}

    for row in rows:
        key = (row["fiscal_year"], row["fiscal_quarter"])
        target = predicted if row["is_predicted"] else confirmed
        # Keep the latest entry per quarter
        target[key] = {
            "report_date": row["report_date"],
            "before_after": row["before_after"],
        }

    # Build historical quarter patterns from confirmed data only
    # year_offset = report_year - fiscal_year (captures cross-year reporting like Q4 in Jan)
    quarter_patterns: dict[int, list] = defaultdict(list)
    for (fy, fq), info in confirmed.items():
        rd = info["report_date"]
        quarter_patterns[fq].append({
            "year": rd.year,
            "month": rd.month,
            "day": rd.day,
            "year_offset": rd.year - fy,
            "before_after": info["before_after"],
        })

    # Find the latest confirmed quarter
    if not confirmed:
        return 0

    latest_fy, latest_fq = max(confirmed.keys(), key=lambda k: (k[0], k[1]))

    # Get company name from any existing row
    company_name = ""
    with db_cursor() as cur:
        cur.execute(
            "SELECT company_name FROM earnings WHERE symbol = %s AND market = %s AND company_name != '' LIMIT 1",
            (symbol, market),
        )
        row = cur.fetchone()
        if row:
            company_name = row["company_name"]

    # Predict forward from the latest confirmed quarter
    predictions_made = 0
    cur_fy, cur_fq = latest_fy, latest_fq
    today = date.today()
    max_date = today + timedelta(days=MAX_FUTURE_DAYS)

    for _ in range(MAX_PREDICT_AHEAD):
        next_fy, next_fq = next_quarter(cur_fy, cur_fq)

        # Skip if we already have confirmed data for this quarter
        if (next_fy, next_fq) in confirmed:
            cur_fy, cur_fq = next_fy, next_fq
            continue

        # Need at least 1 historical data point for this quarter pattern
        history = quarter_patterns.get(next_fq, [])
        if len(history) < 1:
            cur_fy, cur_fq = next_fy, next_fq
            continue

        # Compute predicted month, day and year_offset from recent history
        recent = sorted(history, key=lambda h: h["year"])[-4:]
        pred_month = int(statistics.median([h["month"] for h in recent]))
        pred_day = int(statistics.median([h["day"] for h in recent]))
        pred_year_offset = int(statistics.median([h["year_offset"] for h in recent]))

        # Apply year_offset: e.g. Q4 fiscal_year=2025 → report year = 2026
        pred_year = next_fy + pred_year_offset

        # Clamp day for the target month/year
        max_day = calendar.monthrange(pred_year, pred_month)[1]
        pred_day = min(pred_day, max_day)

        try:
            pred_date = date(pred_year, pred_month, pred_day)
        except ValueError:
            cur_fy, cur_fq = next_fy, next_fq
            continue

        # Skip if too far future
        if pred_date > max_date:
            break

        # Determine before_after
        ba_values = [h["before_after"] for h in recent if h["before_after"]]
        pred_ba = statistics.mode(ba_values) if ba_values else None

        # Upsert: if predicted row already exists for this quarter, update it
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
                   fiscal_year, fiscal_quarter, before_after, is_predicted, date_source, date_status)
                VALUES (%s, %s, %s, %s, 'Q', %s, %s, %s, TRUE, 'algorithm', 'predicted')
                ON CONFLICT (symbol, market, report_date, report_type)
                DO UPDATE SET
                    is_predicted = CASE WHEN earnings.date_source = 'algorithm' THEN TRUE ELSE earnings.is_predicted END,
                    date_source = CASE WHEN earnings.date_source = 'algorithm' THEN 'algorithm' ELSE earnings.date_source END,
                    date_status = CASE WHEN earnings.date_source = 'algorithm' THEN 'predicted' ELSE earnings.date_status END,
                    before_after = COALESCE(EXCLUDED.before_after, earnings.before_after),
                    company_name = CASE WHEN earnings.company_name = '' THEN EXCLUDED.company_name ELSE earnings.company_name END,
                    updated_at = NOW()
                """,
                (symbol, market, company_name, pred_date.isoformat(), next_fy, next_fq, pred_ba),
            )
        predictions_made += 1

        # Also update our tracking
        predicted[(next_fy, next_fq)] = {"report_date": pred_date, "before_after": pred_ba}

        cur_fy, cur_fq = next_fy, next_fq

    return predictions_made


def mark_confirmed():
    """Clean up predictions when real data arrives."""
    with db_cursor() as cur:
        # Existing rows created before provenance support still need an explicit state.
        cur.execute("UPDATE earnings SET date_source = 'algorithm', date_status = 'predicted' WHERE is_predicted = TRUE")
        # 1) Rows that have actuals are no longer predicted
        cur.execute("UPDATE earnings SET is_predicted = FALSE, date_status = 'reported' WHERE is_predicted = TRUE AND eps_actual IS NOT NULL")
        n1 = cur.rowcount

        # 2) Delete predicted rows that overlap with a confirmed row
        #    on the same (symbol, market, fiscal_year, fiscal_quarter)
        cur.execute(
            """DELETE FROM earnings WHERE is_predicted = TRUE AND id IN (
                SELECT p.id FROM earnings p
                JOIN earnings c ON p.symbol = c.symbol AND p.market = c.market
                    AND p.fiscal_year = c.fiscal_year AND p.fiscal_quarter = c.fiscal_quarter
                    AND c.is_predicted = FALSE
            )"""
        )
        n2 = cur.rowcount

    if n1 or n2:
        logger.info(f"Confirmed {n1} rows with actuals, removed {n2} stale predictions")


def cleanup_stale_predictions():
    """Remove predicted rows that are older than 60 days (past their report date)."""
    with db_cursor() as cur:
        cur.execute(
            "DELETE FROM earnings WHERE is_predicted = TRUE AND report_date < CURRENT_DATE - INTERVAL '60 days'"
        )
        n = cur.rowcount
    if n:
        logger.info(f"Cleaned up {n} stale predictions")


@dataclass
class MergeStats:
    """Audit counters for :func:`merge_duplicate_symbols` (Issue #54).

    Before this existed the step logged a single ``Merged N 5-digit HK symbols``
    line, so a run that deleted data was indistinguishable from one that merged
    it — and the "merged" count was in fact the *candidate* count.
    """

    moved: int = 0               # rows copied onto their canonical symbol
    skipped: int = 0             # symbols whose canonical form is themselves
    deleted: int = 0             # rows removed after their data was moved
    blocked: int = 0             # rows kept because a snapshot move would collide
    snapshots_moved: int = 0     # estimate snapshots re-pointed onto the survivor

    @property
    def changed(self) -> bool:
        return bool(self.moved or self.deleted or self.snapshots_moved)

    def summary(self) -> str:
        return (f"moved={self.moved}, skipped={self.skipped}, deleted={self.deleted}, "
                f"blocked={self.blocked}, snapshots_moved={self.snapshots_moved}")


#: Columns copied when a non-canonical symbol is renamed onto its canonical code.
#: Provenance travels with the row: a rename must not strip ``date_source`` /
#: ``actual_source`` / ``estimate_as_of`` and friends (Issue #54).
_MERGE_COLUMNS = (
    "company_name, report_date, report_type, fiscal_year, fiscal_quarter, "
    "eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after, is_predicted, "
    "date_source, date_status, estimate_source, estimate_as_of, estimate_currency, "
    "estimate_basis, actual_source, actual_as_of"
)

#: Copy every row of the dirty symbol onto the canonical one, returning the id of
#: the row that survived each copy so its snapshots can follow it.
_MERGE_UPSERT = f"""
    INSERT INTO earnings (symbol, market, {_MERGE_COLUMNS})
    SELECT %s, market, {_MERGE_COLUMNS}
    FROM earnings WHERE symbol = %s AND market = %s
    ON CONFLICT (symbol, market, report_date, report_type) DO UPDATE SET
        company_name = CASE WHEN earnings.company_name = '' THEN EXCLUDED.company_name ELSE earnings.company_name END,
        eps_estimate = COALESCE(EXCLUDED.eps_estimate, earnings.eps_estimate),
        eps_actual = COALESCE(EXCLUDED.eps_actual, earnings.eps_actual),
        revenue_estimate = COALESCE(EXCLUDED.revenue_estimate, earnings.revenue_estimate),
        revenue_actual = COALESCE(EXCLUDED.revenue_actual, earnings.revenue_actual),
        before_after = COALESCE(EXCLUDED.before_after, earnings.before_after),
        fiscal_year = CASE WHEN earnings.fiscal_year IS NULL THEN EXCLUDED.fiscal_year ELSE earnings.fiscal_year END,
        fiscal_quarter = CASE WHEN earnings.fiscal_quarter IS NULL THEN EXCLUDED.fiscal_quarter ELSE earnings.fiscal_quarter END,
        is_predicted = earnings.is_predicted AND EXCLUDED.is_predicted,
        date_source = CASE WHEN earnings.date_source IN ('unknown', 'algorithm')
            AND EXCLUDED.date_source NOT IN ('unknown', 'algorithm')
            THEN EXCLUDED.date_source ELSE earnings.date_source END,
        date_status = CASE WHEN earnings.date_status IN ('scheduled', 'predicted')
            AND EXCLUDED.date_status NOT IN ('scheduled', 'predicted')
            THEN EXCLUDED.date_status ELSE earnings.date_status END,
        estimate_source = COALESCE(earnings.estimate_source, EXCLUDED.estimate_source),
        estimate_as_of = COALESCE(earnings.estimate_as_of, EXCLUDED.estimate_as_of),
        estimate_currency = COALESCE(earnings.estimate_currency, EXCLUDED.estimate_currency),
        estimate_basis = COALESCE(earnings.estimate_basis, EXCLUDED.estimate_basis),
        actual_source = COALESCE(earnings.actual_source, EXCLUDED.actual_source),
        actual_as_of = COALESCE(earnings.actual_as_of, EXCLUDED.actual_as_of),
        updated_at = NOW()
    RETURNING id, report_date, report_type
"""

#: Move one row's estimate snapshots onto the row that replaced it, skipping the
#: ones the survivor already holds under ``UNIQUE(earning_id, source, captured_at)``.
_SNAPSHOT_REPOINT = """
    UPDATE earnings_estimate_snapshots s SET earning_id = %s
    WHERE s.earning_id = %s
      AND NOT EXISTS (
          SELECT 1 FROM earnings_estimate_snapshots t
          WHERE t.earning_id = %s AND t.source = s.source AND t.captured_at = s.captured_at
      )
"""

_SNAPSHOT_COUNT = "SELECT count(*) AS n FROM earnings_estimate_snapshots WHERE earning_id = %s"

#: Only rows whose data was copied *and* whose symbol is not the canonical one may
#: go — a "merge" whose target equals its source can never delete (Issue #54).
_DELETE_ROWS = ("DELETE FROM earnings WHERE id = ANY(%s) AND symbol = %s AND market = %s "
                "AND symbol <> %s")


def merge_duplicate_symbols(dry_run: bool = False) -> "MergeStats":
    """Rename non-canonical duplicate symbols onto their canonical code.

    Two legacy spellings written by older releases are repaired:

    * ``AAPL.US`` → ``AAPL`` (US tickers are stored bare);
    * zero-padded five-digit HK codes ``00700.HK`` → ``0700.HK``.

    Five-digit HK codes are *not* dirty per se: the ``8xxxx`` RMB counters
    (``82333.HK`` is the RMB counter of ``2333.HK``, ``80000.HK`` an HSI futures
    proxy) are legitimate symbols that ``normalize()`` maps onto themselves.
    Issue #54: this step used to treat *every* five-digit code as a duplicate and
    unconditionally ``DELETE`` the rows it had "merged" — for a symbol that
    canonicalises to itself that is a pure delete, which silently removed 25
    production symbols (51 rows, 31 estimate snapshots) on every prediction run.
    Such rows are now skipped and counted instead.

    Rows are only removed after their values *and* their estimate snapshots have
    moved onto the surviving row; a snapshot whose ``(source, captured_at)``
    already exists on the survivor keeps its row instead of cascading history
    away.  ``dry_run`` reports what would change without writing anything.
    """
    stats = MergeStats()
    with db_cursor() as cur:
        # 1) Merge XXX.US → XXX for US market
        cur.execute("SELECT DISTINCT symbol FROM earnings WHERE symbol LIKE '%.US' AND market = 'US' ORDER BY symbol")
        for row in cur.fetchall():
            sym = row["symbol"]
            merge_symbol_onto_canonical(cur, sym, "US", normalize(sym, "US"), stats, dry_run=dry_run)

        # 2) Merge 5-digit HK codes (e.g. 00700.HK) → 4-digit canonical (0700.HK)
        #    Uses app.symbol.normalize — the single source of truth for HK codes.
        cur.execute(r"SELECT DISTINCT symbol FROM earnings WHERE symbol ~ '^\d{5}\.HK$' ORDER BY symbol")
        for row in cur.fetchall():
            sym = row["symbol"]
            if not is_dirty_hk_5digit(sym):
                # Legitimate five-digit code: canonical form is the symbol itself.
                stats.skipped += 1
                logger.info(f"Skipped {sym}: already canonical (Issue #54 guard)")
                continue
            merge_symbol_onto_canonical(cur, sym, "HK", normalize(sym.split(".")[0], "HK"), stats, dry_run=dry_run)

        if stats.changed:
            logger.info(f"Merged duplicate symbols: {stats.summary()}")
    return stats


def merge_symbol_onto_canonical(cur, dirty_sym: str, market: str, canonical_sym: str,
                                stats: "MergeStats", dry_run: bool = False) -> None:
    """Move every row of ``dirty_sym`` onto ``canonical_sym`` (Issue #54).

    The first line of defence is the identity check: when the canonical form is
    the symbol itself there is nothing to merge, and a ``DELETE`` would be pure
    data loss.  The ``DELETE`` also carries the same proof as a SQL predicate, so
    a "merge" whose target equals its source cannot delete anything.
    """
    if not canonical_sym or canonical_sym == dirty_sym:
        stats.skipped += 1
        return

    cur.execute(
        "SELECT id, report_date, report_type FROM earnings WHERE symbol = %s AND market = %s",
        (dirty_sym, market),
    )
    source_rows = cur.fetchall()
    if not source_rows:
        return

    if dry_run:
        logger.info(f"[dry-run] would merge {len(source_rows)} row(s) {dirty_sym} → {canonical_sym}")
        stats.moved += len(source_rows)
        return

    # Copy the rows onto the canonical symbol, then pair each source row with the
    # row that survived it (its own id when the canonical row already existed).
    cur.execute(_MERGE_UPSERT, (canonical_sym, dirty_sym, market))
    kept = {(row["report_date"], row["report_type"]): row["id"] for row in cur.fetchall()}

    movable: list[int] = []
    blocked = 0
    for row in source_rows:
        kept_id = kept.get((row["report_date"], row["report_type"]))
        if kept_id is None or kept_id == row["id"]:
            blocked += 1
            continue
        # Re-point before deleting: earnings_estimate_snapshots cascades on delete.
        cur.execute(_SNAPSHOT_REPOINT, (kept_id, row["id"], kept_id))
        stats.snapshots_moved += cur.rowcount
        cur.execute(_SNAPSHOT_COUNT, (row["id"],))
        leftover = (cur.fetchone() or {}).get("n", 0)
        if leftover:
            # A snapshot with the same (source, captured_at) already lives on the
            # survivor; deleting this row would cascade the duplicate away. Keep
            # the row and report it rather than silently losing history.
            blocked += 1
            logger.warning(
                f"Kept {dirty_sym} row id={row['id']}: {leftover} snapshot(s) collide "
                f"with {canonical_sym} id={kept_id}"
            )
            continue
        movable.append(row["id"])

    if movable:
        cur.execute(_DELETE_ROWS, (movable, dirty_sym, market, canonical_sym))
        stats.deleted += cur.rowcount
    stats.moved += len(movable)
    stats.blocked += blocked
    logger.info(f"Merged {dirty_sym} → {canonical_sym}: rows={len(movable)} blocked={blocked}")


if __name__ == "__main__":
    from app.db import init_db
    from app.sync_audit import start_run, finish_run
    init_db()
    all_symbols = []
    for mkt, syms in get_source().get_symbols_by_market().items():
        for s in syms:
            all_symbols.append((s, mkt))
    run_id = start_run("prediction", "algorithm", symbol_count=len(all_symbols))
    try:
        merge_duplicate_symbols()
        mark_confirmed()
        cleanup_stale_predictions()

        logger.info("Predicting future earnings dates...")
        total = 0
        for i, (symbol, market) in enumerate(all_symbols):
            count = predict_for_symbol(symbol, market)
            total += count
            if (i + 1) % 10 == 0:
                logger.info(f"  Processed {i+1}/{len(all_symbols)} symbols, {total} predictions so far")
    except Exception:
        finish_run(run_id, status="failed", error_code="prediction_failed")
        raise
    else:
        logger.info(f"Prediction complete: {total} future earnings dates predicted")
        finish_run(run_id, status="success", record_count=total)
