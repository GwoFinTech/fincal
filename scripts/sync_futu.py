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
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor
from app import config
from app import fiscal
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


# ── OpenD pacing, error classification, circuit breaker (Issue #49) ──
#
# OpenD refuses both ``get_financials_statements`` and
# ``get_financials_earnings_price_history`` once the window budget is spent
# ("…频率太高，请求失败，每30秒最多30次。"). A full sync used to issue ~3
# unpaced calls per symbol (~6.5 calls/s), tripped the quota within seconds,
# and then failed every remaining symbol with ``ret=-1`` while throwing the
# provider's message away — so a rate limit, a structurally unsupported
# instrument and a genuine bug all looked identical in ``sync_runs``.
#
# The pieces below fix that at the source:
#   * :class:`FutuRateLimiter` paces every OpenD call through one shared
#     sliding window (``FUTU_MAX_CALLS_PER_30S`` per
#     ``FUTU_RATE_LIMIT_WINDOW_SECONDS``);
#   * :func:`futu_call` keeps the provider message, retries a quota rejection
#     after waiting out one window (bounded by ``FUTU_RATE_LIMIT_MAX_RETRIES``)
#     and classifies the outcome;
#   * :class:`FutuStageStats` separates quota rejections from unsupported
#     instruments and real symbol failures, so the audit can report them
#     individually;
#   * a stage that keeps getting refused stops early
#     (``FUTU_RATE_LIMIT_CIRCUIT_BREAKER``) and is audited as
#     ``futu_rate_limited`` instead of blaming the symbols.

# Provider wording that identifies each failure mode. Messages are matched as
# substrings because OpenD localises them (and may append extra detail).
RATE_LIMIT_MARKERS = ("频率太高", "频率限制", "每30秒最多", "too frequent")
UNSUPPORTED_MARKERS = ("仅支持正股",)

OUTCOME_OK = "ok"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_UNSUPPORTED = "unsupported"
OUTCOME_FAILED = "failed"

_OUTCOME_PRIORITY = {
    OUTCOME_OK: 0,
    OUTCOME_UNSUPPORTED: 1,
    OUTCOME_FAILED: 2,
    OUTCOME_RATE_LIMITED: 3,
}


def futu_provider_message(data) -> str:
    """Return OpenD's human-readable reason for a ``ret != 0`` response.

    On failure the second tuple slot carries the reason (e.g. ``该接口仅支持正股``)
    rather than a frame. It used to be discarded, which made the production
    failure impossible to diagnose from the logs (Issue #49).
    """
    if data is None:
        return ""
    return " ".join(str(data).split())[:200]


def classify_futu_error(message: str) -> str:
    """Map a provider message to ``rate_limited`` / ``unsupported`` / ``failed``."""
    if any(marker in message for marker in RATE_LIMIT_MARKERS):
        return OUTCOME_RATE_LIMITED
    if any(marker in message for marker in UNSUPPORTED_MARKERS):
        return OUTCOME_UNSUPPORTED
    return OUTCOME_FAILED


class FutuRateLimiter:
    """Sliding-window pacer shared by every OpenD call in this process.

    Both financials interfaces draw on the same OpenD session quota, so one
    limiter is shared by the dates and actuals stages. ``clock``/``sleep`` are
    injectable so tests can exercise the pacing without real waiting.
    """

    def __init__(self, max_calls: int | None = None, window_seconds: float | None = None,
                 *, clock=time.monotonic, sleep=time.sleep):
        self.max_calls = int(config.FUTU_MAX_CALLS_PER_30S if max_calls is None else max_calls)
        self.window_seconds = float(
            config.FUTU_RATE_LIMIT_WINDOW_SECONDS if window_seconds is None else window_seconds
        )
        self.waited_seconds = 0.0
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def _evict(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self.window_seconds:
            self._calls.popleft()

    def acquire(self) -> float:
        """Block until a call slot is free; return the seconds spent waiting."""
        with self._lock:
            slept = 0.0
            now = self._clock()
            self._evict(now)
            if len(self._calls) >= self.max_calls:
                wait = self.window_seconds - (now - self._calls[0])
                if wait > 0:
                    self._sleep(wait)
                    slept = wait
                    now = self._clock()
                    self._evict(now)
            self._calls.append(now)
            self.waited_seconds += slept
            return slept

    def cool_down(self) -> float:
        """Wait out a whole quota window after the provider refused a call.

        A rejection means the window is already spent, so retrying sooner would
        just be refused again. Sleeping a full window empties the tracker.
        """
        with self._lock:
            wait = self.window_seconds
            if wait > 0:
                self._sleep(wait)
            self._calls.clear()
            self.waited_seconds += max(wait, 0.0)
            return wait


_rate_limiter: FutuRateLimiter | None = None


def get_rate_limiter() -> FutuRateLimiter:
    """Return the process-wide OpenD pacer, creating it on first use.

    Lazy creation (rather than an import-time singleton) keeps the configured
    budget patchable in tests and honours ``FUTU_*`` overrides.
    """
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = FutuRateLimiter()
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Drop the process-wide pacer (used by tests and by long-lived callers)."""
    global _rate_limiter
    _rate_limiter = None


@dataclass
class FutuStageStats:
    """Outcome counters for one Futu stage, classified by failure kind.

    ``total`` keeps its historical per-stage meaning (dates: rows upserted,
    actuals: symbols processed) because existing ``sync_runs.details``
    consumers read it; the remaining fields are the Issue #49 classification.
    """

    total: int = 0
    symbols_attempted: int = 0
    failed_symbols: int = 0
    unsupported_symbols: int = 0
    rate_limited_symbols: int = 0
    rate_limited_calls: int = 0
    retries: int = 0
    consecutive_rate_limited: int = 0
    rate_limited: bool = False  # circuit breaker tripped

    def count_symbol(self, outcome: str) -> None:
        """Count one symbol-level outcome (``ok`` outcomes count nothing)."""
        if outcome == OUTCOME_FAILED:
            self.failed_symbols += 1
        elif outcome == OUTCOME_UNSUPPORTED:
            self.unsupported_symbols += 1
        elif outcome == OUTCOME_RATE_LIMITED:
            self.rate_limited_symbols += 1


def merge_outcome(current: str, new: str) -> str:
    """Return the more severe of two outcomes for the same symbol."""
    return new if _OUTCOME_PRIORITY[new] > _OUTCOME_PRIORITY[current] else current


def futu_call(futu_code: str, description: str, call, limiter, stats,
              *, timeout_seconds: int) -> tuple[str, Any]:
    """Pace, retry and classify one OpenD call (Issue #49).

    Returns ``(outcome, data)`` where ``outcome`` is ``ok`` / ``rate_limited`` /
    ``unsupported`` / ``failed``. A quota rejection waits out one window and is
    retried at most ``FUTU_RATE_LIMIT_MAX_RETRIES`` times; the provider's
    message is always logged, so an operator can tell a rate limit from an
    unsupported instrument or an unknown error without extra probing.

    The quota slot is acquired (and a back-off waited out) *outside* the
    ``timeout_seconds`` watchdog window: the watchdog bounds one provider call,
    and waiting for the pacer is not a call. Arming it around the wait made
    every pacing sleep look like a wedged symbol and turned throttling into
    per-symbol failures.
    """
    retries = max(config.FUTU_RATE_LIMIT_MAX_RETRIES, 0)
    for attempt in range(retries + 1):
        limiter.acquire()
        with futu_call_timeout(timeout_seconds):
            ret, data = call()
        if ret == 0:  # RET_OK = 0
            stats.consecutive_rate_limited = 0
            return OUTCOME_OK, data

        message = futu_provider_message(data)
        kind = classify_futu_error(message)
        if kind == OUTCOME_RATE_LIMITED:
            if attempt < retries:
                stats.retries += 1
                logger.warning(
                    "%s request rate limited for %s (retry %d/%d): ret=%s msg=%s; "
                    "waiting one quota window (%.0fs)",
                    description, futu_code, attempt + 1, retries, ret, message,
                    limiter.window_seconds,
                )
                limiter.cool_down()
                continue
            stats.rate_limited_calls += 1
            stats.consecutive_rate_limited += 1
            if stats.consecutive_rate_limited >= config.FUTU_RATE_LIMIT_CIRCUIT_BREAKER:
                stats.rate_limited = True
            logger.warning(
                "%s request rate limited for %s after %d retries: ret=%s msg=%s",
                description, futu_code, retries, ret, message,
            )
        elif kind == OUTCOME_UNSUPPORTED:
            logger.info(
                "%s request unsupported for %s: ret=%s msg=%s",
                description, futu_code, ret, message,
            )
        else:
            logger.warning(
                "%s request failed for %s: ret=%s msg=%s",
                description, futu_code, ret, message,
            )
        return kind, data
    raise AssertionError("unreachable: retry loop must return")  # pragma: no cover


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


def futu_audit_outcome(*stages: FutuStageStats) -> tuple[str, str | None]:
    """Classify a run honestly while allowing other stages to continue.

    A provider quota rejection is not a symbol-level failure: OpenD refused the
    request because the window budget was spent, so it gets its own error code
    instead of the catch-all ``futu_symbol_fetch_failed`` that made every
    weekly run look like the same permanent regression (Issue #49).
    Structurally unsupported instruments (ETFs) are expected and never fail a
    run by themselves.
    """
    if any(stage.rate_limited or stage.rate_limited_calls for stage in stages):
        return "failed", "futu_rate_limited"
    if any(stage.failed_symbols for stage in stages):
        return "failed", "futu_symbol_fetch_failed"
    return "success", None


def futu_audit_details(date_stats: FutuStageStats, actual_stats: FutuStageStats,
                       skipped_symbols: list[str]) -> dict:
    """Build ``sync_runs.details``: the historical keys plus the Issue #49
    failure classification, so an operator can tell a rate limit from an
    unsupported instrument from a real symbol failure without probing OpenD."""
    return {
        # Historical keys — unchanged so existing consumers keep working.
        "actual_symbols": actual_stats.total,
        "date_failed_symbols": date_stats.failed_symbols,
        "actual_failed_symbols": actual_stats.failed_symbols,
        # Issue #49 classification.
        "date_symbols": date_stats.total,
        "unsupported_symbols": date_stats.unsupported_symbols + actual_stats.unsupported_symbols,
        "rate_limited_calls": date_stats.rate_limited_calls + actual_stats.rate_limited_calls,
        "rate_limited_symbols": date_stats.rate_limited_symbols + actual_stats.rate_limited_symbols,
        "rate_limit_retries": date_stats.retries + actual_stats.retries,
        "rate_limited": bool(date_stats.rate_limited or actual_stats.rate_limited),
        "skipped_symbols": len(skipped_symbols),
    }


def flush_date_batch(batch: list[tuple]) -> int:
    """Upsert fetched Futu dates, one row per fiscal period (Issue #50).

    The ``earnings`` unique key is ``(symbol, market, report_date, report_type)``
    — the *display* date.  A provider that moves an announcement would therefore
    be inserted as a second row for a fiscal period that already has one.  Two
    guards keep the identity intact before the upsert:

    * a single response carrying one period at two dates is collapsed to the
      newest date (adjacent calendar windows overlap);
    * the period's existing confirmed row is re-dated onto the incoming date
      instead of gaining a twin.

    Both live in ``app/fiscal.py`` so the Longbridge sync behaves identically.
    """
    from psycopg2.extras import execute_values

    batch = fiscal.collapse_rows_by_period(
        batch,
        identity_of=lambda r: fiscal.fiscal_key_from_parts(r[0], r[1], r[5], r[6]),
        date_of=lambda r: r[3],
    )
    if not batch:
        return 0
    with db_cursor() as cur:
        for move in fiscal.reschedule_confirmed_rows(cur, batch):
            logger.info(
                "rescheduled %s.%s FY%s Q%s: %s → %s (row %s, Issue #50)",
                move["symbol"], move["market"], move["fiscal_year"], move["fiscal_quarter"],
                move["from"], move["to"], move["id"],
            )
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
    return len(batch)


def sync_earnings_dates(ctx, run_id: int, symbols: list[str]) -> FutuStageStats:
    """Fetch earnings calendar dates from Futu, single shared context.

    ``symbols`` are the Futu-routable watchlist codes resolved once by the
    caller; OpenD pacing and failure classification live in :func:`futu_call`.
    """
    stats = FutuStageStats()
    limiter = get_rate_limiter()
    batch = []
    cutoff = date.today() - timedelta(days=365)

    for source_symbol in symbols:
        check_cancelled(run_id)
        stats.symbols_attempted += 1
        symbol, market = canonical_earnings_symbol(source_symbol)
        futu_code = to_futu_code(source_symbol)
        try:
            outcome, data = futu_call(
                futu_code, "Dates",
                lambda: ctx.get_financials_earnings_price_history(futu_code),
                limiter, stats,
                timeout_seconds=config.FUTU_DATES_TIMEOUT_SECONDS,
            )
            if outcome != OUTCOME_OK:
                stats.count_symbol(outcome)
                if stats.rate_limited:
                    logger.warning(
                        "Dates stage stopped: %d consecutive rate-limit rejections "
                        "(OpenD quota), %d symbol(s) left unfetched",
                        stats.consecutive_rate_limited, len(symbols) - stats.symbols_attempted,
                    )
                    break
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
                stats.total += 1
        except Exception as e:
            stats.count_symbol(OUTCOME_FAILED)
            logger.warning("Dates failed %s: %s", futu_code, e)
            continue

    # Batch upsert all earnings dates
    if batch:
        flush_date_batch(batch)

    logger.info(
        "Futu earnings dates: %s records; %s symbol failures "
        "(%s unsupported, %s rate limited)",
        stats.total, stats.failed_symbols, stats.unsupported_symbols,
        stats.rate_limited_symbols,
    )
    return stats


def sync_actuals(ctx, run_id: int, symbols: list[str]) -> FutuStageStats:
    """Fetch actual EPS (fid=14020) and revenue (fid=8002) via shared context.

    Each symbol issues two OpenD calls; the symbol is counted once, using the
    most severe of the two outcomes, so ``failed_symbols`` keeps its historical
    per-symbol meaning (Issue #49).
    """
    stats = FutuStageStats()
    limiter = get_rate_limiter()

    for source_symbol in symbols:
        check_cancelled(run_id)
        stats.symbols_attempted += 1
        symbol, market = canonical_earnings_symbol(source_symbol)
        futu_code = to_futu_code(source_symbol)
        outcome = OUTCOME_OK
        try:
            # MainIndex for EPS (fid=14020)
            eps_outcome, main_data = futu_call(
                futu_code, "EPS",
                lambda: ctx.get_financials_statements(
                    futu_code, statement_type=4, financial_type=9, num=4
                ),
                limiter, stats,
                timeout_seconds=config.FUTU_ACTUALS_TIMEOUT_SECONDS,
            )
            outcome = merge_outcome(outcome, eps_outcome)
            if eps_outcome == OUTCOME_OK and main_data.get("report_list"):
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
                                   actual_source = 'futu', actual_as_of = NOW(), updated_at = NOW()
                                WHERE symbol = %s AND market = %s AND fiscal_year = %s
                                AND fiscal_quarter = %s
                                AND COALESCE(actual_source, 'unknown') IN
                                    ('unknown', 'algorithm', 'longbridge', 'futu')
                                """,
                                (eps_val, symbol, market, fy, fq),
                            )

            # Income Statement for revenue (fid=8002)
            rev_outcome, income_data = futu_call(
                futu_code, "Revenue",
                lambda: ctx.get_financials_statements(
                    futu_code, statement_type=1, financial_type=9, num=4
                ),
                limiter, stats,
                timeout_seconds=config.FUTU_ACTUALS_TIMEOUT_SECONDS,
            )
            outcome = merge_outcome(outcome, rev_outcome)
            if rev_outcome == OUTCOME_OK and income_data.get("report_list"):
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
                                   actual_source = 'futu', actual_as_of = NOW(), updated_at = NOW()
                                WHERE symbol = %s AND market = %s AND fiscal_year = %s
                                AND fiscal_quarter = %s
                                AND COALESCE(actual_source, 'unknown') IN
                                    ('unknown', 'algorithm', 'longbridge', 'futu')
                                """,
                                (rev_val, symbol, market, fy, fq),
                            )
        except Exception as e:
            outcome = merge_outcome(outcome, OUTCOME_FAILED)
            logger.warning("Actuals failed %s: %s", futu_code, e)
        finally:
            stats.total += 1
            stats.count_symbol(outcome)

        if stats.rate_limited:
            logger.warning(
                "Actuals stage stopped: %d consecutive rate-limit rejections "
                "(OpenD quota), %d symbol(s) left unfetched",
                stats.consecutive_rate_limited, len(symbols) - stats.symbols_attempted,
            )
            break

    logger.info(
        "Futu actuals synced: %s symbols; %s symbol failures "
        "(%s unsupported, %s rate limited)",
        stats.total, stats.failed_symbols, stats.unsupported_symbols,
        stats.rate_limited_symbols,
    )
    return stats


def run_sync(ctx) -> int | None:
    """Run the dates + actuals stages under one audited, always-terminal run.

    Returns the run id, or ``None`` when the fixed idempotency key was already
    running. Every exit path — success, failure, admin cancel, or an unexpected
    ``BaseException`` (a watchdog expiry outside a guarded window, SIGTERM,
    KeyboardInterrupt) — must leave ``sync_runs`` out of the ``running`` state.
    A leftover ``running`` row makes ``futu:earnings:full`` take the idempotent
    skip on every later sync, stalling Futu data until a service restart or a
    manual recover (Issue #48).

    The watchlist is resolved once, filtering codes OpenD cannot route (Issue
    #49): both stages then share the same symbol list, and the skipped codes are
    recorded in the run's ``details`` instead of being turned into impossible
    ``US.000651.SZ`` requests.
    """
    from app.sync_audit import (
        start_run, finish_run, heartbeat, SyncCancelledError,
    )

    symbols, skipped = get_source().get_futu_symbols_with_skipped()
    run_id = start_run("futu", "futu", symbol_count=len(symbols),
                       idempotency_key="futu:earnings:full")
    if run_id is None:
        logger.info("futu sync already running, skipping")
        return None
    date_stats = FutuStageStats()
    actual_stats = FutuStageStats()
    try:
        try:
            heartbeat(run_id, phase="dates", current=0, total=len(symbols))
            date_stats = sync_earnings_dates(ctx, run_id, symbols)
            heartbeat(run_id, phase="actuals", current=0, total=len(symbols))
            actual_stats = sync_actuals(ctx, run_id, symbols)
        except SyncCancelledError:
            # Admin cancelled this run; keep the terminal 'cancelled' state.
            finish_run(run_id, status="cancelled", error_code="cancelled_by_admin")
            logger.warning("futu sync cancelled by admin; stopping")
            raise
        except Exception:
            finish_run(run_id, status="failed", error_code="futu_sync_failed")
            raise
        else:
            status, error_code = futu_audit_outcome(date_stats, actual_stats)
            finish_run(
                run_id, status=status, record_count=date_stats.total,
                details=futu_audit_details(date_stats, actual_stats, skipped),
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
