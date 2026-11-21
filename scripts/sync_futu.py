#!/usr/bin/env python3
"""Fetch earnings calendar dates + actual EPS/revenue from the Futu WebAPI.

Uses batched DB writes. One shared signed client for all symbols (Issue #145:
the OpenD SDK/daemon this used to require is gone; the transport is the same
signed REST API tsummt-web runs on).
"""
import contextlib
import signal
import logging
import sys
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor
from app import config
from app import fiscal
from app import futu_webapi
from app.provenance import (
    REPLACEABLE_ACTUAL_SOURCES,
    normalize_basis,
    normalize_currency,
)
from app.symbol import normalize, to_futu_code
from app.sync_audit import check_cancelled, HeartbeatThrottle
from app.watchlist import get_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

F10_TO_QUARTER = {1: 1, 2: 2, 3: 3, 4: 4}
PUB_TYPE_MAP = {1: "before", 2: "after", 3: "during"}

# ── Income-statement metrics, resolved by label (Issues #62 and #145) ──
#
# The #62 incident: "EPS" was read from a *key-metrics* field (statement_type=4)
# whose id means 流动比率 (current ratio), so 312 rows over 76 symbols stored a
# liquidity ratio as ``eps_actual``. The fix moved EPS into the income statement
# (statement_type=1, next to revenue) and checked every value against the
# provider's own ``display_name``.
#
# #145 removes the daemon, and with it the ids this table used to hold (US 8002 /
# 8047 / 8048, HK 5001 / 5054 / 5055). The signed WebAPI numbers its income
# statement 1..N and **the numbering moves with the issuer's ``structure``**:
# ``NORMAL_HK`` puts revenue at 1 and EPS at 54, ``BANK_HK`` puts operating
# revenue at 15 and EPS at 61, and on a bank's statement id 1 is 净利息收入. Fixed
# ids are therefore no longer expressible; a metric is resolved by its label in a
# preference order, which keeps the #62 guard in the only form that still has
# meaning — an unrecognised label yields **no value**, never a neighbouring one.
FUTU_STATEMENT_INCOME = 1

#: Label preference order for the two metric columns, shared by both markets
#: (the labels are the same in every ``structure``; only the ids move).
FUTU_REVENUE_LABELS = futu_webapi.REVENUE_LABELS
FUTU_EPS_LABELS = futu_webapi.EPS_LABELS

#: The provider's ``yoy`` is a percent for income-statement rows. The cross-check
#: allows this much absolute difference (percentage points) between it and the
#: ratio of the two statements before the run reports a discrepancy: the provider
#: rounds its inputs, so exact equality is not a fair bar, while 0.1 pp is far
#: below any difference that would change a displayed sign or digit.
GROWTH_CROSSCHECK_TOLERANCE_PP = 0.1

#: SQL list of the sources a new Futu actual may replace (Issue #45, extended by
#: #62 with the retracted label so a wrong actual stays repairable).  Built from
#: the shared constant rather than repeated in both UPDATEs; the values are
#: module constants, never user input.
_REPLACEABLE_SOURCES_SQL = "(" + ", ".join(
    f"'{source}'" for source in REPLACEABLE_ACTUAL_SOURCES
) + ")"

#: A same-provider pair must stay a pair (Issue #91).  ``sync_all.sh`` runs the
#: Longbridge stage first and this stage second, so writing Futu's actual over a
#: Longbridge one left almost every row in the default universe as
#: "Longbridge estimate × Futu actual": different providers, neither of them
#: stating an accounting basis, so ``comparison_unavailable_reason`` had to
#: refuse the subtraction and the panel showed 「—」 for AAPL, AMD, AMZN …
#: The two providers also quote different units — Longbridge USD-converts both
#: sides (TSM 4.31/ADS against a 3.94 estimate) while OpenD reports the statement
#: currency (TSM 136.25 TWD) — so replacing one side of a pair mixes units even
#: when the currency happens to line up afterwards.  Futu now fills the *gap*
#: (no actual yet) and leaves a provider-consistent pair alone.
_KEEP_SAME_PROVIDER_PAIR_SQL = """
    AND NOT (
        estimate_source IS NOT NULL
        AND actual_source IS NOT NULL
        AND LOWER(actual_source) = LOWER(estimate_source)
    )"""

#: One actual-side update (EPS or revenue).  Issue #103: ``actual_as_of`` answers
#: "when did *this* number change" (#73) and ``updated_at`` is the feed's revision
#: counter, so both are conditional — a statement re-fetched unchanged must not
#: restamp the row.  The comparison reads the *stored* value, which in an UPDATE
#: is what an unqualified column name refers to.
_ACTUAL_UPDATE_SQL = f"""UPDATE earnings SET {{column}} = %s,
   date_status = CASE WHEN report_date <= CURRENT_DATE THEN 'reported' ELSE date_status END,
   actual_source = 'futu',
   actual_as_of = CASE WHEN {{column}} IS DISTINCT FROM %s::numeric
                       THEN NOW() ELSE actual_as_of END,
   actual_currency = %s, actual_basis = %s,
   updated_at = CASE WHEN ({{column}}, actual_currency, actual_basis)
                          IS DISTINCT FROM (%s::numeric, %s::text, %s::text)
                     THEN NOW() ELSE updated_at END
WHERE symbol = %s AND market = %s AND fiscal_year = %s
AND fiscal_quarter = %s
AND COALESCE(actual_source, 'unknown') IN {_REPLACEABLE_SOURCES_SQL}
{_KEEP_SAME_PROVIDER_PAIR_SQL}
"""


def read_statement_field(report: dict, labels) -> futu_webapi.StatementField:
    """Resolve one metric from a statement report by label (Issue #145).

    Thin wrapper so the existing callers (the sync's actuals stage and
    ``scripts/fix_futu_eps_field.py --verify``) keep one entry point while the
    resolution rule lives next to the transport that needs it.
    """
    return futu_webapi.select_statement_field(report, labels)


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
# Provider messages, both transports: the OpenD SDK phrased these in Chinese, the
# signed WebAPI answers with HTTP 429/439 and ret codes -11 (rate limit), -7
# (invalid symbol) and -10 (no data). The sync keeps one vocabulary either way.
RATE_LIMIT_MARKERS = ("频率太高", "频率限制", "每30秒最多", "too frequent",
                      "rate limit", "HTTP 429", "HTTP 439")
UNSUPPORTED_MARKERS = ("仅支持正股", "invalid symbol", "no data", "code -7", "code -10")

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

    Issue #81 added the write-side counters so the run's ``details`` can tell an
    operator what the database accepted from the batch:

    * ``written_rows`` — date rows the upsert actually stored (``total`` counts
      the rows *fetched*, which is what it always did);
    * ``skipped_rows`` — rows a unique violation rejected even after the per-row
      retry (a data conflict, reported instead of aborting the stage);
    * ``cleared_predictions`` — stale predictions removed so the provider's date
      could land on the period's confirmed row;
    * ``reschedule_skipped`` — periods whose provider date another fiscal period
      already owns (the #52 case).
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
    written_rows: int = 0
    skipped_rows: int = 0
    cleared_predictions: int = 0
    reschedule_skipped: int = 0
    #: Issue #115: symbols whose income statement arrived but carried none of the
    #: expected field ids.  A single such symbol is not a failure (an HK interim
    #: may only publish revenue), but if *every* fetched symbol is missing the
    #: fields the provider's schema changed and the run must fail loudly instead
    #: of reporting a clean stage that wrote nothing.
    missing_field_symbols: list[str] = field(default_factory=list)
    #: Symbols that returned at least one statement report — the denominator for
    #: the "all symbols lost their fields" check.
    report_symbols: int = 0

    #: ``earnings_growth`` rows this run wrote / withheld because the two periods
    #: were not comparable (currency or accounting basis changed).
    growth_rows: int = 0
    growth_incomparable: int = 0
    #: Provider-native consensus/actual pairs written this run (Issue #147), the
    #: symbols whose estimate read came back empty/unsupported, and — the counter
    #: that exists because the first version of this hook was silently broken — how
    #: many symbols raised instead of answering.
    pair_rows: int = 0
    pair_symbols: int = 0
    pair_unsupported: int = 0
    pair_errors: int = 0

    #: Periods where the provider's own ``yoy`` disagreed with the ratio of the
    #: two statements by more than ``GROWTH_CROSSCHECK_TOLERANCE_PP``. Never
    #: stored as a discrepancy — the provider value wins — but every one is
    #: logged and counted, because the two numbers are computed from the same
    #: payload and a drift means one of them changed meaning.
    growth_discrepancies: int = 0

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
    """Return a signed WebAPI client, or ``None`` when Futu is unavailable.

    Issue #145: the OpenD daemon (and its TCP preflight) is gone. The equivalent
    preflight is one signed read: it exercises the credential, the route and the
    upstream in the same request the stages will make, so a missing key or a
    blocked network degrades to 「stage skipped」 instead of five minutes of
    per-symbol failures.
    """
    client = futu_webapi.create_client()
    if client is None:
        return None
    probe = client.probe()
    if not probe["ok"]:
        logger.warning(
            "Futu WebAPI preflight failed (%s, %sms); skipping optional Futu sync",
            probe["code"], probe["latency_ms"],
        )
        client.close()
        return None
    logger.info("Futu WebAPI ready (%sms, %s)", probe["latency_ms"], config.FUTU_API_BASE)
    return client


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
    if any(
        stage.report_symbols and len(stage.missing_field_symbols) == stage.report_symbols
        for stage in stages
    ):
        # Issue #115: every symbol returned a statement and none of them carried
        # an expected field id.  That is not "these instruments have no data" but
        # "the provider's field schema changed", and the stage must not report a
        # clean success while writing nothing.
        return "failed", "futu_actual_fields_missing"
    if any(stage.failed_symbols for stage in stages):
        return "failed", "futu_symbol_fetch_failed"
    return "success", None


def futu_audit_details(date_stats: FutuStageStats, actual_stats: FutuStageStats,
                       skipped_symbols: list[str]) -> dict:
    """Build ``sync_runs.details``: the historical keys plus the Issue #49
    failure classification, so an operator can tell a rate limit from an
    unsupported instrument from a real symbol failure without probing OpenD.

    Issue #81 added what the *write* did (``written_rows``/``skipped_rows``/
    ``cleared_predictions``/``reschedule_skipped``): the failed 2026-10-12 run
    recorded ``details={}`` and ``record_count=0``, so the batch it had already
    fetched was invisible, and a conflict that degrades to a skip would otherwise
    leave no trace at all.
    """
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
        # Issue #81 write-side counters.
        "date_rows_written": date_stats.written_rows,
        "date_rows_skipped": date_stats.skipped_rows,
        "date_predictions_cleared": date_stats.cleared_predictions,
        "date_reschedule_skipped": date_stats.reschedule_skipped,
        # Issue #115: symbols that returned an income statement without any of the
        # expected field ids.  Written even when zero so a provider schema change
        # is visible in the run instead of looking like "no HK actuals exist".
        "actual_missing_fields": len(actual_stats.missing_field_symbols),
        # Provider growth (percent, income statement rows) and its quality counters:
        # ``growth_discrepancies`` is the cross-check against the ratio of the two
        # statements, ``growth_incomparable`` the periods withheld because currency
        # or accounting basis changed between the pair.
        "growth_rows": actual_stats.growth_rows,
        "growth_incomparable": actual_stats.growth_incomparable,
        "growth_discrepancies": actual_stats.growth_discrepancies,
        # Issue #147: periods where the provider's consensus and its own actual
        # share one currency and one unit — the only pair a beat/miss may use for an
        # issuer whose calendar estimate is quoted in another currency.
        "provider_pairs": actual_stats.pair_rows,
        "provider_pair_symbols": actual_stats.pair_symbols,
        "provider_pair_unsupported": actual_stats.pair_unsupported,
        # Non-zero means the pair read threw (a wiring bug, not a provider answer) —
        # the first version passed a name the loop never bound and the broad
        # `except` made a total outage look like a clean run.
        "provider_pair_errors": actual_stats.pair_errors,
    }


def flush_date_batch(batch: list[tuple], stats: "FutuStageStats | None" = None) -> int:
    """Upsert fetched Futu dates, one row per fiscal period (Issue #50).

    The ``earnings`` unique key is ``(symbol, market, report_date, report_type)``
    — the *display* date.  A provider that moves an announcement would therefore
    be inserted as a second row for a fiscal period that already has one.  Two
    guards keep the identity intact before the upsert:

    * a single response carrying one period at two dates is collapsed to the
      newest date (adjacent calendar windows overlap);
    * the period's existing confirmed row is re-dated onto the incoming date
      instead of gaining a twin — including when the incoming date is held by that
      very period's own, now stale, prediction (Issue #81).

    Both live in ``app/fiscal.py`` so the Longbridge sync behaves identically.

    Issue #81 blast radius: the batch used to be one ``execute_values`` call, so a
    single period's conflict rolled back every row of the batch *and* the stage
    raised — 2026-10-12 lost all 188 dates plus the whole actuals stage of the
    week.  The write is now one savepoint per symbol, and a symbol that still
    fails is retried row by row: a conflict costs exactly the rows it affects and
    is reported in ``stats.skipped_rows`` (surfaced in ``sync_runs.details``), so
    the stage finishes and ``sync_actuals`` still runs.  Only unique violations —
    data conflicts — are absorbed that way; any other database error still
    propagates, so a broken connection cannot be mistaken for a quiet skip.

    Returns the number of rows written.
    """
    from psycopg2 import errors as pg_errors
    from psycopg2.extras import execute_values

    batch = fiscal.collapse_rows_by_period(
        batch,
        identity_of=lambda r: fiscal.fiscal_key_from_parts(r[0], r[1], r[5], r[6]),
        date_of=lambda r: r[3],
    )
    if not batch:
        return 0
    flush_stats = stats if stats is not None else FutuStageStats()
    with db_cursor() as cur:
        # A Futu row carries a date and actuals, not a period sequence: let it
        # inherit the report type its period already stores (instead of inserting
        # a second row for one period, Issue #50) and leave the stored row's
        # report type/values alone (`takeover=False`, no figures in this layout).
        outcome = fiscal.reschedule_confirmed_rows(
            cur, batch, value_fields=(), takeover=False, align_report_type=True)
        batch = outcome.rows
        for move in outcome.moves:
            logger.info(
                "rescheduled %s.%s FY%s Q%s: %s → %s (row %s, Issue #50)",
                move["symbol"], move["market"], move["fiscal_year"], move["fiscal_quarter"],
                move["from"], move["to"], move["id"],
            )
        flush_stats.cleared_predictions += len(outcome.predictions_cleared)
        flush_stats.reschedule_skipped += len(outcome.skipped)
        written = 0
        if batch:
            written = _upsert_date_rows(cur, batch, flush_stats, execute_values, pg_errors)
    if outcome.skipped:
        # A provider date that another fiscal period already owns: no row is
        # re-dated for that period (Issue #52).
        logger.warning(
            "futu: %d reschedule(s) skipped (target date owned by another row)",
            len(outcome.skipped),
        )
    if flush_stats.skipped_rows:
        logger.warning(
            "futu: %d date row(s) skipped after the per-row retry (Issue #81)",
            flush_stats.skipped_rows,
        )
    flush_stats.written_rows += written
    logger.info(f"Flushed {written} earnings dates")
    return written


#: Savepoint names for the per-symbol date upsert (Issue #81).
_DATE_SYMBOL_SAVEPOINT = "fincal_futu_symbol"
_DATE_ROW_SAVEPOINT = "fincal_futu_row"

#: The date upsert.  ``is_predicted`` is *not* a bare ``FALSE``: a provider row
#: may only confirm a row that carries its own fiscal identity
#: (``fiscal.CONFIRM_IDENTITY_PROMOTION``, Issue #81).
#:
#: Issue #103: ``updated_at`` is the feed's revision counter (``SEQUENCE`` /
#: ``LAST-MODIFIED`` in ``app/ical.py``, and ``authority_key``'s tie-break), so a
#: re-delivered date moves it only when one of these columns actually changes.
#: ``date_source``/``date_status`` are rewritten unconditionally — they are
#: provenance, and the Longbridge stage rewrites them back on every run, so
#: counting them would bump the stamp twice a week for no content change.
_FUTU_DATE_UPSERT: dict[str, str] = {
    "fiscal_year": "CASE WHEN earnings.fiscal_year IS NULL THEN EXCLUDED.fiscal_year ELSE earnings.fiscal_year END",
    "fiscal_quarter": "CASE WHEN earnings.fiscal_quarter IS NULL THEN EXCLUDED.fiscal_quarter ELSE earnings.fiscal_quarter END",
    "before_after": "COALESCE(EXCLUDED.before_after, earnings.before_after)",
    "is_predicted": fiscal.IDENTITY_PROMOTION_VALUE,
    "company_name": "CASE WHEN earnings.company_name = '' THEN EXCLUDED.company_name ELSE earnings.company_name END",
}

#: Every column above is content (nothing in the conflict branch is provenance),
#: so the revision predicate covers the whole mapping.
_DATE_REVISION_COLUMNS = tuple(_FUTU_DATE_UPSERT)

#: Rendered SET list, one assignment per line.
_DATE_ASSIGNMENTS = ",\n    ".join(
    f"{column} = {expression}" for column, expression in _FUTU_DATE_UPSERT.items()
)

_DATE_UPSERT_SQL = f"""INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
   fiscal_year, fiscal_quarter, before_after, date_source, date_status)
VALUES %s
ON CONFLICT (symbol, market, report_date, report_type)
DO UPDATE SET
    {_DATE_ASSIGNMENTS},
    date_source = 'futu',
    date_status = CASE WHEN (earnings.eps_actual IS NOT NULL OR earnings.revenue_actual IS NOT NULL) AND earnings.report_date <= CURRENT_DATE THEN 'reported' ELSE 'scheduled' END,
    updated_at = CASE WHEN {fiscal.revision_predicate(_FUTU_DATE_UPSERT, _DATE_REVISION_COLUMNS)}
                      THEN NOW() ELSE earnings.updated_at END
"""


def _upsert_date_rows(cur, batch: list[tuple], stats: FutuStageStats,
                      execute_values, pg_errors) -> int:
    """Write ``batch`` one symbol at a time, containing a conflict to its rows.

    See :func:`flush_date_batch` for why: one conflict must not cost the batch,
    the stage, or the week's actuals.  A symbol whose statement fails on a unique
    violation is retried per row, and only the rows that still fail are counted as
    skipped (``stats.skipped_rows``); any other database error propagates
    unchanged.
    """
    groups: dict[tuple, list[tuple]] = {}
    for row in batch:
        groups.setdefault((row[0], row[1]), []).append(row)

    written = 0
    for (symbol, market), rows in groups.items():
        cur.execute(f"SAVEPOINT {_DATE_SYMBOL_SAVEPOINT}")
        try:
            execute_values(cur, _DATE_UPSERT_SQL, rows, page_size=len(rows))
        except pg_errors.UniqueViolation as exc:
            cur.execute(f"ROLLBACK TO SAVEPOINT {_DATE_SYMBOL_SAVEPOINT}")
            logger.warning(
                "futu: %s.%s date batch rejected (%d row(s)), retrying per row: %s",
                symbol, market, len(rows), exc,
            )
            written += _upsert_date_rows_individually(cur, rows, stats, execute_values, pg_errors)
        else:
            written += len(rows)
        cur.execute(f"RELEASE SAVEPOINT {_DATE_SYMBOL_SAVEPOINT}")
    return written


def _upsert_date_rows_individually(cur, rows: list[tuple], stats: FutuStageStats,
                                   execute_values, pg_errors) -> int:
    """Retry each rejected row under its own savepoint, counting the survivors."""
    written = 0
    for row in rows:
        cur.execute(f"SAVEPOINT {_DATE_ROW_SAVEPOINT}")
        try:
            execute_values(cur, _DATE_UPSERT_SQL, [row], page_size=1)
        except pg_errors.UniqueViolation as exc:
            cur.execute(f"ROLLBACK TO SAVEPOINT {_DATE_ROW_SAVEPOINT}")
            stats.skipped_rows += 1
            logger.warning(
                "futu: date row not written %s.%s %s FY%s Q%s: %s (Issue #81)",
                row[0], row[1], row[3], row[5], row[6], exc,
            )
        else:
            written += 1
        cur.execute(f"RELEASE SAVEPOINT {_DATE_ROW_SAVEPOINT}")
    return written


_GROWTH_UPSERT_SQL = """
    INSERT INTO earnings_growth
        (symbol, market, fiscal_year, fiscal_quarter, fiscal_type,
         revenue_yoy, eps_yoy, revenue_qoq, eps_qoq,
         revenue_base, eps_base, currency, basis, reason, source, as_of, updated_at)
    VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s,'futu',NOW(),NOW())
    ON CONFLICT (symbol, market, fiscal_year, fiscal_quarter) DO UPDATE SET
        fiscal_type = EXCLUDED.fiscal_type,
        revenue_yoy = EXCLUDED.revenue_yoy,
        eps_yoy = EXCLUDED.eps_yoy,
        revenue_qoq = EXCLUDED.revenue_qoq,
        eps_qoq = EXCLUDED.eps_qoq,
        revenue_base = EXCLUDED.revenue_base,
        eps_base = EXCLUDED.eps_base,
        currency = EXCLUDED.currency,
        basis = EXCLUDED.basis,
        reason = EXCLUDED.reason,
        source = EXCLUDED.source,
        as_of = EXCLUDED.as_of,
        updated_at = NOW()
"""


def growth_row(symbol: str, market: str, fy: int, fq: int, fiscal_type: int,
               report: dict, prior: dict | None,
               revenue, eps) -> tuple:
    """One ``earnings_growth`` row from a statement (and its prior-year peer).

    The provider's percent is kept as-is. The *base* is the prior-year statement's
    own value, so a base-effect quarter shows the number it grew from. When the
    pair's currency or accounting basis differs, the percentages are withheld with
    a reason: a cross-currency or cross-basis ratio is not a growth rate.
    """
    currency = normalize_currency(report.get("currency_code"))
    basis = normalize_basis(report.get("accounting_standards"))
    reason = None
    if prior is not None:
        prior_currency = normalize_currency(prior.get("currency_code"))
        prior_basis = normalize_basis(prior.get("accounting_standards"))
        if prior_currency != currency:
            reason = "currency_changed"
        elif prior_basis != basis:
            reason = "basis_changed"
    revenue_base = prices = None
    eps_base = None
    if prior is not None:
        revenue_base = read_statement_field(prior, FUTU_REVENUE_LABELS).value
        eps_base = read_statement_field(prior, FUTU_EPS_LABELS).value
    keep = reason is None
    return (
        symbol, market, fy, fq, fiscal_type,
        revenue.yoy if keep else None,
        eps.yoy if keep else None,
        revenue.qoq if keep else None,
        eps.qoq if keep else None,
        revenue_base, eps_base, currency, basis, reason,
    )


def flush_growth_rows(rows: list[tuple], stats: FutuStageStats) -> int:
    """Upsert growth rows; returns how many were written."""
    written = 0
    for row in rows:
        try:
            with db_cursor() as cur:
                cur.execute(_GROWTH_UPSERT_SQL, row)
            written += 1
            if row[13]:
                stats.growth_incomparable += 1
        except Exception as exc:  # noqa: BLE001 - one bad period must not stop the stage
            logger.warning("growth row skipped for %s FY%s Q%s: %s",
                           row[0], row[2], row[3], exc)
    stats.growth_rows = written
    return written


_PAIR_UPSERT_SQL = """
    INSERT INTO earnings_provider_pairs
        (symbol, market, fiscal_year, fiscal_quarter, source, currency,
         eps_consensus, eps_actual, num_analysts, period_type, as_of, updated_at)
    VALUES (%s,%s,%s,%s,'futu',%s,%s,%s,%s,%s,NOW(),NOW())
    ON CONFLICT (symbol, market, fiscal_year, fiscal_quarter, source) DO UPDATE SET
        currency = EXCLUDED.currency,
        eps_consensus = EXCLUDED.eps_consensus,
        eps_actual = EXCLUDED.eps_actual,
        num_analysts = EXCLUDED.num_analysts,
        period_type = EXCLUDED.period_type,
        as_of = NOW(),
        updated_at = NOW()
"""


def fetch_provider_pairs(client, code: str, limiter, stats,
                         timeout_seconds: int) -> tuple[list[tuple], str]:
    """Provider-native consensus/actual pairs for one symbol (Issue #147).

    Returns ``(rows, outcome)`` with ``outcome`` ``None`` on success or
    ``"unsupported"`` when the provider has no estimates for the symbol (ETFs,
    indices, uncovered small caps).  A pair is only emitted when both numbers and
    the period are present: half a pair cannot compare anything, and writing it
    would make the read path believe a comparison exists.
    """
    outcome, payload = futu_call(
        code, "Estimate", lambda: client.get_financials_estimate(code),
        limiter, stats, timeout_seconds=timeout_seconds)
    if outcome != OUTCOME_OK:
        return [], outcome
    currency = payload.get("currency")
    rows = []
    for period in payload.get("history") or []:
        fy, fq = period.get("fiscal_year"), period.get("fiscal_quarter")
        consensus, actual = period.get("consensus"), period.get("actual")
        if not fy or not fq or consensus is None or actual is None or not currency:
            continue
        rows.append((int(fy), int(fq), currency, consensus, actual,
                     period.get("num_analysts"), payload.get("period_type")))
    return rows, OUTCOME_OK


def flush_provider_pairs(symbol: str, market: str, rows: list[tuple],
                         stats: FutuStageStats) -> int:
    """Upsert one symbol's pairs; returns how many were written."""
    written = 0
    for row in rows:
        try:
            with db_cursor() as cur:
                cur.execute(_PAIR_UPSERT_SQL, (symbol, market) + row)
            written += 1
        except Exception as exc:  # noqa: BLE001 - one bad period must not stop the stage
            logger.warning("provider pair skipped for %s FY%s Q%s: %s",
                           symbol, row[0], row[1], exc)
    stats.pair_rows += written
    if written:
        stats.pair_symbols += 1
    return written


def sync_earnings_dates(ctx, run_id: int, symbols: list[str],
                        stats: FutuStageStats | None = None) -> FutuStageStats:
    """Fetch earnings calendar dates from the Futu WebAPI, one shared client.

    ``symbols`` are the Futu-routable watchlist codes resolved once by the
    caller; request pacing and failure classification live in :func:`futu_call`.

    ``stats`` may be supplied by the caller so a stage that ends in an exception
    still reports what it had counted (Issue #81: the failed 2026-10-12 run
    recorded ``details={}``/``record_count=0``).
    """
    stats = stats if stats is not None else FutuStageStats()
    limiter = get_rate_limiter()
    batch = []
    cutoff = date.today() - timedelta(days=config.SYNC_FUTU_LOOKBACK_DAYS)
    # Issue #78: the stage is budgeted 1500s and its row must reflect real
    # progress, so the reaper ages it from a live heartbeat rather than from its
    # start (throttled to one write per minute, not one per symbol).
    beats = HeartbeatThrottle()

    for source_symbol in symbols:
        check_cancelled(run_id)
        beats.maybe(run_id, phase="dates",
                    current=stats.symbols_attempted, total=len(symbols))
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

            # One row per fiscal period: the provider lists a period again when a
            # date is rescheduled, and the first occurrence is the one the stage
            # has always kept. Plain dicts — the client returns rows, not a frame
            # (pandas only ever shipped as an OpenD SDK dependency).
            seen: set[tuple[int, int]] = set()
            for row in data:
                try:
                    fy = int(row["fiscal_year"])
                    ft = int(row["financial_type"])
                except (KeyError, TypeError, ValueError):
                    continue
                if (fy, ft) in seen:
                    continue
                seen.add((fy, ft))
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
        flush_date_batch(batch, stats)

    logger.info(
        "Futu earnings dates: %s records; %s symbol failures "
        "(%s unsupported, %s rate limited)",
        stats.total, stats.failed_symbols, stats.unsupported_symbols,
        stats.rate_limited_symbols,
    )
    return stats


def sync_actuals(ctx, run_id: int, symbols: list[str],
                 stats: FutuStageStats | None = None) -> FutuStageStats:
    """Fetch actual EPS and revenue from the market's income statement.

    Both figures live in ``statement_type=1``, so one request per symbol carries
    them: the stage used to spend two calls per symbol, one of which read a
    liquidity ratio and stored it as the actual EPS (Issue #62).  The metrics are
    resolved by label (#145) because the WebAPI's income-statement numbering
    differs per issuer structure, and a period whose statement carries neither
    label is reported as a missing field instead of a clean write (#115).  The
    symbol is counted once, using the most severe outcome, so ``failed_symbols``
    keeps its historical per-symbol meaning (Issue #49).
    ``stats`` may be supplied by the caller (see :func:`sync_earnings_dates`).
    """
    stats = stats if stats is not None else FutuStageStats()
    limiter = get_rate_limiter()
    # Issue #78: heartbeat the actuals loop as well (the phase-level heartbeat in
    # run_sync() is written once, before the loop starts).
    beats = HeartbeatThrottle()
    growth_batch: list[tuple] = []

    for source_symbol in symbols:
        check_cancelled(run_id)
        beats.maybe(run_id, phase="actuals",
                    current=stats.symbols_attempted, total=len(symbols))
        stats.symbols_attempted += 1
        symbol, market = canonical_earnings_symbol(source_symbol)
        futu_code = to_futu_code(source_symbol)
        outcome = OUTCOME_OK
        try:
            outcome, income_data = futu_call(
                futu_code, "IncomeStatement",
                lambda: ctx.get_financials_statements(
                    futu_code, statement_type=FUTU_STATEMENT_INCOME,
                    financial_type=9, num=config.SYNC_FUTU_STATEMENT_PERIODS,
                ),
                limiter, stats,
                timeout_seconds=config.FUTU_ACTUALS_TIMEOUT_SECONDS,
            )
            if outcome == OUTCOME_OK and income_data.get("report_list"):
                stats.report_symbols += 1
                saw_expected_field = False
                # Prior-year peers for the growth base: the provider's own ``yoy``
                # is authoritative, but the base it was taken against comes from
                # the statement one fiscal year earlier in this same payload.
                by_period = {
                    (r.get("fiscal_year"), r.get("financial_type")): r
                    for r in income_data["report_list"]
                }
                for report in income_data["report_list"]:
                    fy = report.get("fiscal_year")
                    ft = report.get("financial_type")
                    fq = F10_TO_QUARTER.get(ft)
                    if not fy or not fq:
                        continue
                    # Issue #145: resolved by label — the WebAPI's income-statement
                    # numbering differs per issuer structure, so an id table cannot
                    # express this any more (see FUTU_REVENUE_LABELS above).
                    eps = read_statement_field(report, FUTU_EPS_LABELS)
                    revenue = read_statement_field(report, FUTU_REVENUE_LABELS)
                    if eps.field_id is not None or revenue.field_id is not None:
                        saw_expected_field = True
                    for name, read in (("EPS", eps), ("Revenue", revenue)):
                        if read.value is None:
                            # No item carried a label this metric owns. Nothing is
                            # written for the period; a period that carries neither
                            # metric is counted as a missing field below (#115).
                            logger.info(
                                "%s: no %s label in FY%s Q%s income statement "
                                "(structure %s) — nothing written",
                                futu_code, name, fy, fq, report.get("structure_name", "?"),
                            )
                    # Issue #61: a written actual must say which currency and
                    # which accounting base it is in — OpenD reports the
                    # *reporting* currency (TSM: TWD, BABA/PDD/NIO: CNY) for a
                    # listing whose consensus estimate is in its quote currency,
                    # and this is the evidence the read path needs to refuse the
                    # subtraction.  Never guessed: an undeclared value is stored
                    # as ``unknown``.  Both figures now come from this one
                    # statement, so they share its declared currency and basis.
                    currency = normalize_currency(report.get("currency_code"))
                    basis = normalize_basis(report.get("accounting_standards"))
                    if eps.value is not None:
                        with db_cursor() as cur:
                            cur.execute(
                                _ACTUAL_UPDATE_SQL.format(column="eps_actual"),
                                (eps.value, eps.value, currency, basis,
                                 eps.value, currency, basis,
                                 symbol, market, fy, fq),
                            )
                    if revenue.value is not None:
                        with db_cursor() as cur:
                            cur.execute(
                                _ACTUAL_UPDATE_SQL.format(column="revenue_actual"),
                                (revenue.value, revenue.value, currency, basis,
                                 revenue.value, currency, basis,
                                 symbol, market, fy, fq),
                            )
                    # Provider growth for this period, plus the base it grew from.
                    prior = by_period.get((fy - 1, ft))
                    growth_batch.append(
                        growth_row(symbol, market, fy, fq, ft, report, prior, revenue, eps))
                    for name, read, prior_read in (
                        ("EPS", eps, read_statement_field(prior, FUTU_EPS_LABELS)
                         if prior is not None else None),
                        ("Revenue", revenue, read_statement_field(prior, FUTU_REVENUE_LABELS)
                         if prior is not None else None),
                    ):
                        if read.yoy is None or prior_read is None or not prior_read.value:
                            continue
                        try:
                            # Sign convention: growth over a **negative** base is
                            # measured against its magnitude, exactly like the
                            # provider and like ``phase3._growth`` — dividing by the
                            # signed base flips the sign of every loss-making
                            # quarter (XPEV base -0.4: -125% instead of +125%).
                            # The first version of this check did that and reported
                            # 146 false disagreements in one run.
                            ours = ((read.value - prior_read.value)
                                    / abs(prior_read.value)) * 100
                        except (TypeError, ZeroDivisionError):
                            continue
                        if abs(ours - read.yoy) > GROWTH_CROSSCHECK_TOLERANCE_PP:
                            stats.growth_discrepancies += 1
                            logger.warning(
                                "%s: %s yoy disagreement FY%s Q%s — provider %.4f%% vs "
                                "statement ratio %.4f%% (base %s)",
                                futu_code, name, fy, fq, read.yoy, ours, prior_read.value,
                            )
                if not saw_expected_field:
                    # Issue #115: the report arrived but carried none of the ids
                    # this market's schema defines.  Nothing was written; say so
                    # instead of letting the stage look clean (all-market miss
                    # fails the run — see futu_audit_outcome).
                    stats.missing_field_symbols.append(source_symbol)
                    logger.warning(
                        "%s: %d income-statement report(s) but none carried the "
                        "revenue (%s) or EPS (%s) label — no actual written "
                        "(Issues #115, #145)",
                        futu_code, len(income_data["report_list"]),
                        "/".join(FUTU_REVENUE_LABELS[:2]), "/".join(FUTU_EPS_LABELS[:2]),
                    )
        except Exception as e:
            outcome = merge_outcome(outcome, OUTCOME_FAILED)
            logger.warning("Actuals failed %s: %s", futu_code, e)
        finally:
            stats.total += 1
            stats.count_symbol(outcome)

        # Issue #147: the provider's own consensus/actual pair for the same periods.
        # One extra read per symbol; it is enrichment (beat/miss for an issuer quoted
        # in another currency), so a failure is counted and logged but never turns
        # the symbol — or the run — red. Deliberately outside the block above: the
        # pair is per-symbol independent of whether the statements landed.
        try:
            pair_rows, pair_outcome = fetch_provider_pairs(
                ctx, futu_code, limiter, stats,
                timeout_seconds=config.FUTU_ACTUALS_TIMEOUT_SECONDS)
            if pair_outcome == OUTCOME_UNSUPPORTED:
                stats.pair_unsupported += 1
            elif pair_outcome == OUTCOME_OK and pair_rows:
                # The canonical symbol, not the provider's ``BABA.US`` form: the read
                # path joins this table on ``earnings.symbol``, which is bare. Writing
                # the provider form matched only HK names (whose suffix is kept) and
                # left every US row without a pair.
                flush_provider_pairs(symbol, market, pair_rows, stats)
        except Exception as e:  # noqa: BLE001 - enrichment must not fail the stage
            # Never silent: a raised exception here is a defect in this hook (a
            # misspelled client, a schema drift), not a provider answer, so it is
            # counted into `details` where a run summary shows it.
            stats.pair_errors += 1
            logger.warning("Provider pairs unavailable for %s: %s", futu_code, e)

        if stats.rate_limited:
            logger.warning(
                "Actuals stage stopped: %d consecutive rate-limit rejections "
                "(OpenD quota), %d symbol(s) left unfetched",
                stats.consecutive_rate_limited, len(symbols) - stats.symbols_attempted,
            )
            break

    # Growth is written after the loop so one slow period cannot lengthen a
    # symbol's watchdog window; the numbers were fetched in the same request.
    if growth_batch:
        flush_growth_rows(growth_batch, stats)

    logger.info(
        "Futu actuals synced: %s symbols; %s symbol failures "
        "(%s unsupported, %s rate limited); growth rows %s "
        "(%s withheld as incomparable, %s provider/statement disagreements)",
        stats.total, stats.failed_symbols, stats.unsupported_symbols,
        stats.rate_limited_symbols, stats.growth_rows,
        stats.growth_incomparable, stats.growth_discrepancies,
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
                       idempotency_key="futu:earnings:full",
                       timeout_seconds=config.stage_timeout("futu"))
    if run_id is None:
        logger.info("futu sync already running, skipping")
        return None
    date_stats = FutuStageStats()
    actual_stats = FutuStageStats()
    terminal = False
    stage = "dates"
    try:
        try:
            heartbeat(run_id, phase="dates", current=0, total=len(symbols))
            date_stats = sync_earnings_dates(ctx, run_id, symbols, date_stats)
            stage = "actuals"
            heartbeat(run_id, phase="actuals", current=0, total=len(symbols))
            actual_stats = sync_actuals(ctx, run_id, symbols, actual_stats)
        except SyncCancelledError:
            # Admin cancelled this run; keep the terminal 'cancelled' state.
            terminal = finish_run(run_id, status="cancelled",
                                  error_code="cancelled_by_admin")
            logger.warning("futu sync cancelled by admin; stopping")
            raise
        except Exception:
            # Issue #81: report what the stage had already done.  The failed
            # 2026-10-12 run recorded ``details={}`` and ``record_count=0`` for a
            # batch it had fully fetched, so its 188 dates, its per-symbol
            # classification and the stage it died in were all invisible.
            terminal = finish_run(
                run_id, status="failed", error_code="futu_sync_failed",
                record_count=date_stats.total,
                details={
                    **futu_audit_details(date_stats, actual_stats, skipped),
                    "failure_stage": stage,
                },
            )
            raise
        else:
            status, error_code = futu_audit_outcome(date_stats, actual_stats)
            terminal = finish_run(
                run_id, status=status, record_count=date_stats.total,
                details=futu_audit_details(date_stats, actual_stats, skipped),
                error_code=error_code,
            )
        return run_id
    finally:
        # Belt and braces, but only for a path that could not record its own
        # terminal state: ``finish_run`` on a run that already left 'running' is
        # a no-op that now warns (Issue #78), and forcing "interrupted" over an
        # already-recorded success would be a false alarm.
        if not terminal:
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
            run_id = start_run("futu", "futu", idempotency_key="futu:earnings:full",
                               timeout_seconds=config.stage_timeout("futu"))
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
