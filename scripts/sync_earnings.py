#!/usr/bin/env python3
"""Full sync of earnings data from Longbridge finance-calendar into fincal DB.
Covers wide date ranges and uses pagination to get all records.
Uses batch inserts for performance."""
import subprocess
import json
import logging
import sys
import os
from dataclasses import dataclass
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor
from app.symbol import from_lb_counter_id, normalize
from app.sync_audit import check_cancelled, SyncCancelledError
from app.sync_quality import SyncQuality
from app import fiscal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 200


@dataclass
class FlushStats:
    """Outcome of one batch upsert (Issue #52).

    ``rows`` is what the batch actually committed; ``moves`` are the fiscal
    periods re-dated onto the provider's date, ``skipped`` the periods whose
    provider date was already owned by another row (a data conflict to
    reconcile, not a failure).
    """

    rows: int = 0
    moves: int = 0
    skipped: int = 0


@dataclass
class SyncStats:
    """Entry counts for one Longbridge run.

    The counts are accumulated as batches commit, so a run that dies mid-way —
    or loses single batches to a data conflict — still reports what it really
    wrote.  Before Issue #52 the failure path hard-coded ``fetched=0,
    written=0`` and recorded ~0 for a run that had already committed ~10.8k rows.
    """

    fetched: int = 0
    written: int = 0
    rescheduled: int = 0
    skipped: int = 0
    failed_batches: int = 0

    def quality(self) -> SyncQuality:
        return SyncQuality(
            fetched=self.fetched,
            written=self.written,
            skipped=self.skipped,
            failed=self.failed_batches,
        )


def next_calendar_cursor(api_next_date: str, last_report_date: str, current_start: str) -> str | None:
    """Advance pagination even when Longbridge omits ``next_date``.

    The calendar endpoint can return a full page for only one or two days but
    leave ``next_date`` empty.  Stopping there silently drops every later
    earnings release and its consensus estimates.
    """
    candidates: list[str] = []
    try:
        candidate = api_next_date.strip()
        if candidate and date.fromisoformat(candidate) > date.fromisoformat(current_start):
            candidates.append(candidate)
    except ValueError:
        pass
    try:
        last_day = last_report_date.split(" ", 1)[0].replace(".", "-")
        candidates.append((date.fromisoformat(last_day) + timedelta(days=1)).isoformat())
    except (AttributeError, ValueError):
        pass
    return max(candidates) if candidates else None


def fetch_calendar(market: str, start: str, end: str) -> list[dict]:
    """Fetch all earnings calendar pages from Longbridge, paginating via next_date."""
    all_pages = []
    cursor_start = start
    # Empty calendar days must be advanced explicitly: this endpoint does not
    # seek to the next non-empty day when ``start`` itself has no releases.
    max_iterations = 800

    for i in range(max_iterations):
        cmd = [
            "longbridge", "finance-calendar", "report",
            "--market", market,
            "--start", cursor_start,
            "--end", end,
            "--count", "300",
            "--format", "json",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                raise RuntimeError(f"longbridge_cli_failed:{market}:{i}:{result.stderr[:200]}")
            data = json.loads(result.stdout)
        except Exception as exc:
            logger.error(f"Fetch error iteration {i}: {exc}")
            raise RuntimeError(f"longbridge_fetch_failed:{market}:{i}") from exc

        pages = data.get("list", [])
        if not pages:
            next_cursor = (date.fromisoformat(cursor_start) + timedelta(days=1)).isoformat()
            if next_cursor > end:
                break
            logger.debug("%s no releases on %s; advancing to %s", market, cursor_start, next_cursor)
            cursor_start = next_cursor
            continue
        all_pages.extend(pages)

        next_date = data.get("next_date", "")
        last_page_date = pages[-1].get("date", "")
        next_cursor = next_calendar_cursor(next_date, last_page_date, cursor_start)
        if not next_cursor or next_cursor > end:
            break
        if next_cursor <= cursor_start:
            raise RuntimeError(f"longbridge_pagination_stalled:{market}:{cursor_start}")
        cursor_start = next_cursor

        logger.info(f"  {market} iteration {i}: got {len(pages)} pages, last_date={last_page_date}, next={cursor_start}")
    else:
        raise RuntimeError(f"longbridge_pagination_limit:{market}:{max_iterations}")

    return all_pages


def parse_date_type(date_type: str) -> str | None:
    for k, v in {"盘前": "before", "盘后": "after", "盘中": "during",
                 "Before Open": "before", "After Close": "after"}.items():
        if k in (date_type or ""):
            return v
    return None


def extract_kv(data_kv: list[dict]) -> dict:
    result = {}
    for kv in data_kv:
        t = kv.get("type", "")
        raw = kv.get("value_raw")
        val = None
        if raw is not None and raw != "" and raw != "0.000000":
            try:
                val = float(raw)
            except (ValueError, TypeError):
                pass
        if t == "estimate_eps":
            result["eps_estimate"] = val
        elif t == "actual_eps":
            result["eps_actual"] = val
        elif t == "estimate_revenue":
            result["revenue_estimate"] = val
        elif t == "actual_revenue":
            result["revenue_actual"] = val
    return result


def parse_report_date(date_str: str) -> str | None:
    """Normalize provider dates, rejecting relative/non-ISO display labels."""
    try:
        parsed = date_str.split(" ")[0].replace(".", "-")
        return date.fromisoformat(parsed).isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def dedupe_batch(rows: list[tuple]) -> list[tuple]:
    """Collapse provider duplicates before a bulk UPSERT.

    Longbridge can repeat one calendar event across adjacent result windows.
    PostgreSQL rejects duplicate conflict keys within one ``execute_values``
    statement; retain the copy carrying the most financial metadata.
    """
    unique: dict[tuple[str, str, str, str], tuple] = {}
    for row in rows:
        key = (row[0], row[1], row[3], row[4])
        existing = unique.get(key)
        score = sum(value not in (None, "") for value in row[2:])
        existing_score = sum(value not in (None, "") for value in existing[2:]) if existing else -1
        if score > existing_score:
            unique[key] = row
    return list(unique.values())


def flush_batch(cur, rows: list[tuple]) -> FlushStats:
    """Batch upsert using execute_values.

    Two Issue #50 guards run before the upsert, because the table's unique key is
    the report date — not the fiscal period:

    * a provider response that carries one fiscal period at two dates is
      collapsed to the newest date (adjacent calendar windows overlap);
    * a period that already has a confirmed row on a different date is re-dated
      onto the incoming one instead of being inserted as a second row.

    Issue #52: the re-dating step can be impossible (another fiscal period, or a
    prediction, already owns the provider's date).  Those periods are reported
    back in ``FlushStats`` instead of aborting the batch, and the upsert below
    no longer rewrites an existing row's ``fiscal_year``/``fiscal_quarter`` —
    the fiscal period is the row's persistent identity, so letting a provider
    candidate of a *different* period overwrite it would re-create exactly the
    duplicate-period disease #50 removed.
    """
    rows = dedupe_batch(rows)
    rows = fiscal.collapse_rows_by_period(
        rows,
        identity_of=lambda r: fiscal.fiscal_key_from_parts(r[0], r[1], r[5], r[6]),
        date_of=lambda r: r[3],
    )
    if not rows:
        return FlushStats()
    outcome = fiscal.reschedule_confirmed_rows(cur, rows)
    for move in outcome.moves:
        logger.info(
            "rescheduled %s.%s FY%s Q%s: %s → %s (row %s, Issue #50)",
            move["symbol"], move["market"], move["fiscal_year"], move["fiscal_quarter"],
            move["from"], move["to"], move["id"],
        )
    from psycopg2.extras import execute_values
    execute_values(
        cur,
        """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
           fiscal_year, fiscal_quarter,
           eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after)
        VALUES %s
        ON CONFLICT (symbol, market, report_date, report_type)
        DO UPDATE SET
            company_name = EXCLUDED.company_name,
            fiscal_year = CASE WHEN earnings.fiscal_year IS NULL THEN EXCLUDED.fiscal_year ELSE earnings.fiscal_year END,
            fiscal_quarter = CASE WHEN earnings.fiscal_quarter IS NULL THEN EXCLUDED.fiscal_quarter ELSE earnings.fiscal_quarter END,
            eps_estimate = COALESCE(EXCLUDED.eps_estimate, earnings.eps_estimate),
            eps_actual = COALESCE(EXCLUDED.eps_actual, earnings.eps_actual),
            revenue_estimate = COALESCE(EXCLUDED.revenue_estimate, earnings.revenue_estimate),
            revenue_actual = COALESCE(EXCLUDED.revenue_actual, earnings.revenue_actual),
            before_after = COALESCE(EXCLUDED.before_after, earnings.before_after),
            is_predicted = FALSE,
            updated_at = NOW()
        """,
        rows,
        page_size=BATCH_SIZE,
    )
    keys = [(r[0], r[1], r[3]) for r in rows]
    execute_values(cur, """UPDATE earnings AS e SET
        date_source = 'longbridge', date_status = CASE WHEN e.eps_actual IS NOT NULL OR e.revenue_actual IS NOT NULL THEN 'reported' ELSE 'scheduled' END,
        estimate_source = CASE WHEN e.eps_estimate IS NOT NULL OR e.revenue_estimate IS NOT NULL THEN 'longbridge' ELSE e.estimate_source END,
        estimate_as_of = CASE WHEN e.eps_estimate IS NOT NULL OR e.revenue_estimate IS NOT NULL THEN NOW() ELSE e.estimate_as_of END,
        actual_source = CASE WHEN e.eps_actual IS NOT NULL OR e.revenue_actual IS NOT NULL THEN 'longbridge' ELSE e.actual_source END,
        actual_as_of = CASE WHEN e.eps_actual IS NOT NULL OR e.revenue_actual IS NOT NULL THEN NOW() ELSE e.actual_as_of END
        FROM (VALUES %s) AS v(symbol, market, report_date)
        WHERE (e.symbol,e.market,e.report_date)=(v.symbol,v.market,(v.report_date)::date)""", keys)
    execute_values(cur, """INSERT INTO earnings_estimate_snapshots (earning_id, source, eps_estimate, revenue_estimate, payload)
        SELECT e.id, 'longbridge', e.eps_estimate, e.revenue_estimate, '{"endpoint":"finance-calendar"}'::jsonb
        FROM earnings e JOIN (VALUES %s) AS v(symbol,market,report_date) ON (e.symbol,e.market,e.report_date)=(v.symbol,v.market,(v.report_date)::date)
        WHERE e.eps_estimate IS NOT NULL OR e.revenue_estimate IS NOT NULL""", keys)
    return FlushStats(rows=len(rows), moves=len(outcome.moves), skipped=len(outcome.skipped))


def _flush_batch(run_stats: SyncStats, batch: list[tuple], label: str) -> None:
    """Commit one batch, isolating its failure from the rest of the run.

    Issue #52: one unmovable row (a data conflict inside a 200-row batch) used to
    raise out of ``flush_batch`` and abort the whole run — killing the Longbridge
    stage, the remaining pages and every later stage of ``sync_all.sh``.  A batch
    that still fails (e.g. the database rejected something unforeseen) is counted
    and logged, then the run continues with the next batch so a single bad batch
    costs 200 records instead of the whole calendar.
    """
    try:
        with db_cursor() as cur:
            flushed = flush_batch(cur, batch)
    except SyncCancelledError:
        raise
    except Exception as exc:
        run_stats.failed_batches += 1
        logger.error(
            "  batch failed %s (%d records, fetched: %d, written: %d): %s",
            label, len(batch), run_stats.fetched, run_stats.written, exc,
        )
        return
    run_stats.written += flushed.rows
    run_stats.rescheduled += flushed.moves
    run_stats.skipped += flushed.skipped
    logger.info(
        "  Flushed %d records (written: %d, fetched: %d, skipped: %d)",
        flushed.rows, run_stats.written, run_stats.fetched, run_stats.skipped,
    )


def run_terminal_state(stats: SyncStats) -> tuple[str, str | None]:
    """Terminal ``sync_runs`` status/error_code for a finished Longbridge run.

    Issue #52: batch-level isolation means a run can finish *without* raising and
    still have written nothing (every batch failed).  Such a run must not be
    recorded as ``success``.  A run where only some batches failed stays
    ``success`` with ``details.status='partial'`` (see :meth:`SyncStats.quality`)
    so the remaining stages of ``sync_all.sh`` still run.
    """
    if stats.failed_batches and stats.written == 0:
        return ("failed", "longbridge_batch_failed")
    return ("success", None)


def sync_earnings(run_id: int, stats: SyncStats | None = None) -> SyncStats:
    """Full sync with wide date range, batched inserts.

    ``run_id`` is polled at each market/batch checkpoint so an admin cancel
    stops the job promptly instead of burning the full API budget (Issue #28).

    Returns the run's :class:`SyncStats`: what was fetched, what each committed
    batch actually wrote, and the reschedule conflicts the identity guards
    skipped (Issue #52) — so the audit row reflects the run instead of a
    hard-coded zero.  ``stats`` may be supplied by the caller to keep the
    counters of a run that ends in an exception.
    """
    today = date.today()
    start = (today - timedelta(days=180)).isoformat()
    end = (today + timedelta(days=365)).isoformat()

    run_stats = stats if stats is not None else SyncStats()

    for market in ["US", "HK"]:
        check_cancelled(run_id)
        logger.info(f"=== Fetching {market} earnings [{start} → {end}] ===")
        pages = fetch_calendar(market, start, end)
        logger.info(f"  Total pages received: {len(pages)}")

        batch = []
        for page in pages:
            check_cancelled(run_id)
            for info in page.get("infos", []):
                symbol, mkt = from_lb_counter_id(info.get("counter_id", ""))
                if not symbol:
                    continue

                report_date = parse_report_date(info.get("date", ""))
                if not report_date:
                    continue

                company_name = info.get("counter_name", "")
                date_type = parse_date_type(info.get("date_type", ""))
                kv = extract_kv(info.get("data_kv", []))

                ext = info.get("ext", {}).get("financial_report", {})
                fiscal_quarter = None
                try:
                    fq = int(ext.get("period", "0") or "0")
                    if 1 <= fq <= 4:
                        fiscal_quarter = fq
                except (ValueError, TypeError):
                    pass

                # Try to get fiscal_year from API response
                fiscal_year = None
                fy_from_api = ext.get("fiscal_year") or ext.get("year")
                if fy_from_api:
                    try:
                        fiscal_year = int(fy_from_api)
                    except (ValueError, TypeError):
                        pass

                # Fallback: heuristic from report date
                if not fiscal_year and fiscal_quarter:
                    rd_month = int(report_date[5:7])
                    if mkt == "US":
                        if fiscal_quarter <= 2:
                            fiscal_year = int(report_date[:4])
                        else:
                            fiscal_year = int(report_date[:4]) - 1 if rd_month <= 6 else int(report_date[:4])
                    else:
                        if fiscal_quarter in (1, 2):
                            fiscal_year = int(report_date[:4])
                        else:
                            fiscal_year = int(report_date[:4]) - 1 if rd_month <= 3 else int(report_date[:4])

                batch.append((
                    symbol, mkt, company_name, report_date, "Q",
                    fiscal_year, fiscal_quarter,
                    kv.get("eps_estimate"), kv.get("eps_actual"),
                    kv.get("revenue_estimate"), kv.get("revenue_actual"),
                    date_type,
                ))
                run_stats.fetched += 1

                # Flush when batch is full
                if len(batch) >= BATCH_SIZE:
                    _flush_batch(run_stats, batch, f"{market} page batch")
                    batch = []
                    check_cancelled(run_id)

        # Flush remaining
        if batch:
            _flush_batch(run_stats, batch, f"{market} final batch")

    logger.info(
        "=== Sync complete: %d records fetched, %d written, %d rescheduled, "
        "%d reschedule conflicts, %d failed batches ===",
        run_stats.fetched, run_stats.written, run_stats.rescheduled,
        run_stats.skipped, run_stats.failed_batches,
    )
    return run_stats


if __name__ == "__main__":
    from app.db import init_db
    from app.sync_audit import (
        start_run, finish_run, heartbeat, advisory_lock,
        SyncCancelledError, LOCK_LONGBRIDGE_EARNINGS,
    )
    init_db()
    with advisory_lock(LOCK_LONGBRIDGE_EARNINGS) as acquired:
        if not acquired:
            logger.info("longbridge earnings sync locked by another process, skipping")
            sys.exit(0)
        run_id = start_run("longbridge", "longbridge",
                           idempotency_key="longbridge:earnings:full",
                           symbol_count=0)
        if run_id is None:
            logger.info("longbridge earnings sync already running, skipping")
            sys.exit(0)
        run_stats = SyncStats()
        try:
            sync_earnings(run_id, run_stats)
            status, error_code = run_terminal_state(run_stats)
            finish_run(run_id, status=status, error_code=error_code,
                       record_count=run_stats.written,
                       details=run_stats.quality().to_dict())
            if status == "failed":
                # Every batch died: the stage is recorded as failed even though no
                # exception escaped sync_earnings, while the exit code stays 0 so
                # sync_all.sh still runs the Futu/prediction stages (Issue #52) —
                # one broken stage must not cost the whole weekly refresh.
                logger.error(
                    "longbridge earnings sync wrote nothing (%d failed batch(es))",
                    run_stats.failed_batches,
                )
            elif run_stats.failed_batches:
                # A partial run is not a green run: the details carry
                # status='partial' (SyncQuality.classify) for the admin.
                logger.warning(
                    "longbridge earnings sync finished with %d failed batch(es): "
                    "%d written, %d skipped",
                    run_stats.failed_batches, run_stats.written, run_stats.skipped,
                )
        except SyncCancelledError:
            # Admin cancelled this run; keep the terminal 'cancelled' state.
            finish_run(run_id, status="cancelled", error_code="cancelled_by_admin")
            logger.warning("longbridge earnings sync cancelled by admin; stopping")
            sys.exit(1)
        except Exception:
            # Report what the run did before it died.  Issue #52's incident wrote
            # ~10.8k rows and recorded ``fetched:0, written:0`` — the counters now
            # follow the batches that actually committed, so a mid-run failure
            # stays auditable instead of looking like a no-op.
            run_stats.failed_batches = max(run_stats.failed_batches, 1)
            finish_run(run_id, status="failed", error_code="longbridge_sync_failed",
                       record_count=run_stats.written,
                       details=run_stats.quality().to_dict())
            raise
