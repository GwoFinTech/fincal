#!/usr/bin/env python3
"""Fetch earnings calendar dates + actual EPS/revenue from Futu OpenD.
Uses batched DB writes. One shared OpenQuoteContext for all symbols.
"""
import contextlib
import signal
import logging
import sys
import os
import socket
import threading
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor
from app import config
from app.symbol import normalize, to_futu_code
from app.sync_audit import check_cancelled
from app.watchlist import get_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

F10_TO_QUARTER = {1: 1, 2: 2, 3: 3, 4: 4}
PUB_TYPE_MAP = {1: "before", 2: "after", 3: "during"}


# ── Futu call watchdog (Issue #48) ──────────────────────────────────
#
# ``signal.alarm`` only produces a catchable exception when a SIGALRM handler
# is installed.  With the default disposition (SIG_DFL) the kernel terminates
# the whole process (exit 128 + 14 = 142) the moment a per-symbol OpenD call
# overruns its window: the rest of the batch is never fetched, ``finish_run``
# never runs, and the leftover ``running`` row makes every later sync with the
# fixed key ``futu:earnings:full`` take the idempotent skip — Futu data stalls
# until someone restarts the service or manually recovers the run.
#
# The handler below turns an expired watchdog into a catchable ``TimeoutError``
# inside the per-symbol ``except`` blocks, so one wedged symbol is recorded as a
# failed symbol and the loop moves on to the next one.

class FutuCallTimeout(TimeoutError):
    """Raised when a single Futu OpenD call exceeds its watchdog window."""


def _raise_futu_timeout(signum, frame):
    raise FutuCallTimeout(f"futu call exceeded watchdog window (signal {signum})")


_alarm_handler_installed = False


def _install_alarm_handler() -> bool:
    """Install the SIGALRM watchdog handler once, on the main thread only.

    Returns ``False`` when alarms cannot be used (non-main thread, or a
    platform without ``SIGALRM``), so the caller degrades to an unbounded call
    instead of arming a watchdog whose "handler" would never fire.
    """
    global _alarm_handler_installed
    if _alarm_handler_installed:
        return True
    if not hasattr(signal, "SIGALRM"):
        return False
    if threading.current_thread() is not threading.main_thread():
        # CPython only allows installing signal handlers from the main thread.
        return False
    signal.signal(signal.SIGALRM, _raise_futu_timeout)
    _alarm_handler_installed = True
    return True


@contextlib.contextmanager
def futu_call_timeout(seconds: int):
    """Bound one blocking Futu OpenD call by wall clock (Issue #48).

    On expiry the wrapped call raises :class:`FutuCallTimeout`, which the
    per-symbol handlers count as a symbol failure before continuing with the
    rest of the batch. ``seconds <= 0`` disables the watchdog.
    """
    if seconds <= 0 or not _install_alarm_handler():
        yield
        return
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)


def create_futu_context():
    """Return a connected OpenD context, or ``None`` when it is unavailable.

    ``OpenQuoteContext`` retries a refused connection indefinitely.  A cheap
    TCP preflight prevents a weekly sync from consuming the scheduler's full
    one-hour allowance when OpenD is down or pointed at the wrong port.
    """
    try:
        with socket.create_connection((config.FUTU_HOST, config.FUTU_PORT), timeout=3):
            pass
    except OSError as exc:
        logger.warning(
            "Futu OpenD unavailable at %s:%s; skipping optional Futu sync: %s",
            config.FUTU_HOST,
            config.FUTU_PORT,
            exc,
        )
        return None

    from futu import OpenQuoteContext
    try:
        ctx = OpenQuoteContext(host=config.FUTU_HOST, port=config.FUTU_PORT)
        logger.info("Connected to Futu OpenD at %s:%s (shared context)", config.FUTU_HOST, config.FUTU_PORT)
        return ctx
    except Exception as exc:
        logger.warning("Failed to create Futu OpenD context: %s", exc)
        return None


def canonical_earnings_symbol(symbol: str) -> tuple[str, str]:
    """Convert a watchlist symbol to the earnings table's canonical key.

    The shared watchlist keeps US tickers as ``AAPL.US`` while the earnings
    table's established US convention is the bare ticker (``AAPL``). Writing
    the watchlist spelling directly made every Futu run create transient
    ``*.US`` duplicates, which prediction cleanup then had to merge.
    """
    raw = symbol.strip().upper()
    market = raw.rsplit(".", 1)[-1] if "." in raw else "US"
    if market == "HK":
        return normalize(raw, "HK"), market
    if market == "US":
        return raw.removesuffix(".US"), market
    raise ValueError(f"unsupported_market:{market}")


def futu_audit_outcome(date_failures: int, actual_failures: int) -> tuple[str, str | None]:
    """Classify a stage honestly while allowing other stages to continue."""
    if date_failures or actual_failures:
        return "failed", "futu_symbol_fetch_failed"
    return "success", None


def sync_earnings_dates(ctx, run_id: int) -> tuple[int, int]:
    """Fetch earnings calendar dates from Futu, single shared context."""
    batch = []
    total = 0
    failed_symbols = 0
    cutoff = date.today() - timedelta(days=365)
    symbols = get_source().get_futu_symbols()

    for source_symbol in symbols:
        check_cancelled(run_id)
        symbol, market = canonical_earnings_symbol(source_symbol)
        futu_code = to_futu_code(source_symbol)
        try:
            with futu_call_timeout(config.FUTU_DATES_TIMEOUT_SECONDS):
                ret, data = ctx.get_financials_earnings_price_history(futu_code)
            if ret != 0:  # RET_OK = 0
                failed_symbols += 1
                logger.warning("Dates request failed for %s: ret=%s", futu_code, ret)
                continue

            df = data.drop_duplicates(subset=["fiscal_year", "financial_type"], keep="first")
            for _, row in df.iterrows():
                fy = int(row["fiscal_year"])
                ft = int(row["financial_type"])
                fq = F10_TO_QUARTER.get(ft)
                if fq is None:
                    continue
                pub_date_str = row.get("pub_trading_day_str", "")
                if not pub_date_str:
                    continue
                report_date = date.fromisoformat(pub_date_str)
                if report_date < cutoff:
                    continue
                pub_type = PUB_TYPE_MAP.get(int(row.get("pub_type", 0)))
                # Futu confirms the date; no actuals fetched here yet, so status
                # stays 'scheduled' until sync_actuals() marks it reported.
                batch.append((symbol, market, "", pub_date_str, "Q", fy, fq, pub_type, "futu", "scheduled"))
                total += 1
        except Exception as e:
            failed_symbols += 1
            logger.warning("Dates failed %s: %s", futu_code, e)
            continue

    # Batch upsert all earnings dates
    if batch:
        from psycopg2.extras import execute_values
        with db_cursor() as cur:
            execute_values(
                cur,
                """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
                   fiscal_year, fiscal_quarter, before_after, date_source, date_status)
                VALUES %s
                ON CONFLICT (symbol, market, report_date, report_type)
                DO UPDATE SET
                    fiscal_year = EXCLUDED.fiscal_year,
                    fiscal_quarter = EXCLUDED.fiscal_quarter,
                    before_after = COALESCE(EXCLUDED.before_after, earnings.before_after),
                    is_predicted = FALSE,
                    company_name = CASE WHEN earnings.company_name = '' THEN EXCLUDED.company_name ELSE earnings.company_name END,
                    date_source = 'futu',
                    date_status = CASE WHEN earnings.eps_actual IS NOT NULL OR earnings.revenue_actual IS NOT NULL THEN 'reported' ELSE 'scheduled' END,
                    updated_at = NOW()
                """,
                batch,
                page_size=200,
            )
        logger.info(f"Flushed {len(batch)} earnings dates")

    logger.info("Futu earnings dates: %s records; %s symbol failures", total, failed_symbols)
    return total, failed_symbols


def sync_actuals(ctx, run_id: int) -> tuple[int, int]:
    """Fetch actual EPS (fid=14020) and revenue (fid=8002) via shared context."""
    total = 0
    failed_symbols = 0
    symbols = get_source().get_futu_symbols()

    for source_symbol in symbols:
        check_cancelled(run_id)
        symbol, market = canonical_earnings_symbol(source_symbol)
        futu_code = to_futu_code(source_symbol)
        try:
            symbol_failed = False
            # MainIndex for EPS (fid=14020)
            with futu_call_timeout(config.FUTU_ACTUALS_TIMEOUT_SECONDS):
                ret, main_data = ctx.get_financials_statements(
                    futu_code, statement_type=4, financial_type=9, num=4
                )
            if ret != 0:
                symbol_failed = True
                logger.warning("EPS request failed for %s: ret=%s", futu_code, ret)
            if ret == 0 and main_data.get("report_list"):
                for report in main_data["report_list"]:
                    fy = report.get("fiscal_year")
                    ft = report.get("financial_type")
                    fq = F10_TO_QUARTER.get(ft)
                    if not fy or not fq:
                        continue
                    eps_val = None
                    for item in report.get("item_list", []):
                        if item["field_id"] == 14020 and item.get("data") is not None:
                            try:
                                eps_val = float(item["data"])
                            except (ValueError, TypeError):
                                pass
                            break
                    if eps_val is not None:
                        with db_cursor() as cur:
                            cur.execute(
                                """UPDATE earnings SET eps_actual = %s, date_status = 'reported',
                                   actual_source = 'futu', updated_at = NOW()
                                WHERE symbol = %s AND market = %s AND fiscal_year = %s
                                AND fiscal_quarter = %s AND (eps_actual IS NULL OR ABS(eps_actual) > 1000)
                                """,
                                (eps_val, symbol, market, fy, fq),
                            )

            # Income Statement for revenue (fid=8002)
            with futu_call_timeout(config.FUTU_ACTUALS_TIMEOUT_SECONDS):
                ret, income_data = ctx.get_financials_statements(
                    futu_code, statement_type=1, financial_type=9, num=4
                )
            if ret != 0:
                symbol_failed = True
                logger.warning("Revenue request failed for %s: ret=%s", futu_code, ret)
            if ret == 0 and income_data.get("report_list"):
                for report in income_data["report_list"]:
                    fy = report.get("fiscal_year")
                    ft = report.get("financial_type")
                    fq = F10_TO_QUARTER.get(ft)
                    if not fy or not fq:
                        continue
                    rev_val = None
                    for item in report.get("item_list", []):
                        if item["field_id"] == 8002 and item.get("data") is not None:
                            try:
                                rev_val = float(item["data"])
                            except (ValueError, TypeError):
                                pass
                            break
                    if rev_val is not None:
                        with db_cursor() as cur:
                            cur.execute(
                                """UPDATE earnings SET revenue_actual = %s, date_status = 'reported',
                                   actual_source = 'futu', updated_at = NOW()
                                WHERE symbol = %s AND market = %s AND fiscal_year = %s
                                AND fiscal_quarter = %s AND revenue_actual IS NULL
                                """,
                                (rev_val, symbol, market, fy, fq),
                            )
            if symbol_failed:
                failed_symbols += 1
            total += 1
        except Exception as e:
            failed_symbols += 1
            logger.warning("Actuals failed %s: %s", futu_code, e)
            continue

    logger.info("Futu actuals synced: %s symbols; %s symbol failures", total, failed_symbols)
    return total, failed_symbols


def run_sync(ctx) -> int | None:
    """Run the dates + actuals stages under one audited, always-terminal run.

    Returns the run id, or ``None`` when the fixed idempotency key was already
    running. Every exit path — success, failure, admin cancel, or an unexpected
    ``BaseException`` (a watchdog expiry outside a guarded window, SIGTERM,
    KeyboardInterrupt) — must leave ``sync_runs`` out of the ``running`` state.
    A leftover ``running`` row makes ``futu:earnings:full`` take the idempotent
    skip on every later sync, stalling Futu data until a service restart or a
    manual recover (Issue #48).
    """
    from app.sync_audit import (
        start_run, finish_run, heartbeat, SyncCancelledError,
    )

    symbols = get_source().get_futu_symbols()
    run_id = start_run("futu", "futu", symbol_count=len(symbols),
                       idempotency_key="futu:earnings:full")
    if run_id is None:
        logger.info("futu sync already running, skipping")
        return None
    try:
        try:
            heartbeat(run_id, phase="dates", current=0, total=len(symbols))
            date_count, date_failures = sync_earnings_dates(ctx, run_id)
            heartbeat(run_id, phase="actuals", current=0, total=len(symbols))
            actual_count, actual_failures = sync_actuals(ctx, run_id)
        except SyncCancelledError:
            # Admin cancelled this run; keep the terminal 'cancelled' state.
            finish_run(run_id, status="cancelled", error_code="cancelled_by_admin")
            logger.warning("futu sync cancelled by admin; stopping")
            raise
        except Exception:
            finish_run(run_id, status="failed", error_code="futu_sync_failed")
            raise
        else:
            status, error_code = futu_audit_outcome(date_failures, actual_failures)
            finish_run(
                run_id, status=status, record_count=date_count,
                details={
                    "actual_symbols": actual_count,
                    "date_failed_symbols": date_failures,
                    "actual_failed_symbols": actual_failures,
                },
                error_code=error_code,
            )
        return run_id
    finally:
        # Belt and braces: ``finish_run`` only transitions rows that are still
        # 'running', so this is a no-op after a normal terminal transition and
        # guarantees no path can leave the row blocking the next sync.
        try:
            finish_run(run_id, status="interrupted",
                       error_code="futu_sync_interrupted")
        except Exception:
            logger.exception("could not force terminal state for run %s", run_id)


if __name__ == "__main__":
    from app.db import init_db
    from app.sync_audit import (
        start_run, finish_run, advisory_lock,
        SyncCancelledError, LOCK_FUTU_EARNINGS,
    )
    init_db()

    with advisory_lock(LOCK_FUTU_EARNINGS) as acquired:
        if not acquired:
            logger.info("futu sync locked by another process, skipping")
            sys.exit(0)

        ctx = create_futu_context()
        if ctx is None:
            run_id = start_run("futu", "futu", idempotency_key="futu:earnings:full")
            if run_id is not None:
                finish_run(run_id, status="skipped", error_code="opend_unavailable")
            sys.exit(0)  # Non-fatal — skip Futu sync

        try:
            try:
                run_sync(ctx)
            except SyncCancelledError:
                sys.exit(1)
        finally:
            ctx.close()
            logger.info("Futu context closed")
