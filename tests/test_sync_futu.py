"""Regression tests for Issue #39: Futu sync write paths must record
provenance so reported events are not mislabeled as ``unknown``/``scheduled``.

Root cause: ``scripts/sync_futu.py`` wrote ``date_source``/``date_status`` and
``actual_source`` nowhere, so every Futu-sourced row kept the column defaults
(``unknown`` + ``scheduled``) even when actuals were present — roughly 8% of
reported events were mislabeled and lost source attribution.

Fix: the dates upsert now writes ``date_source='futu'`` (and a correct
``date_status``), and the actuals updates write ``date_status='reported'`` +
``actual_source='futu'``.

Issue #48 coverage (same module): the per-symbol OpenD watchdog must raise a
catchable ``TimeoutError`` instead of letting the default SIGALRM disposition
kill the whole process, and the audited run must always end in a terminal state.

Issue #49 coverage (same module): OpenD refuses both financials interfaces
above 30 calls / 30 s, so the sync must pace its calls, retry a quota rejection
after waiting out the window, keep the provider's message, and classify a
rejection apart from an unsupported instrument (ETF) and a real symbol failure
— otherwise 91% of a run failed for one systemic reason under a single opaque
``ret=-1``.

Issue #62 coverage (same module): the actual EPS must come from the income
statement's 基本每股收益 field, not from a key-metrics id whose label is
流动比率, and a field whose provider label contradicts its id must fail the
symbol instead of being written under the EPS column.
"""
import contextlib
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location("sync_futu", ROOT / "scripts" / "sync_futu.py")
sync_futu = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_futu)


def _db_mock(cursor):
    """A ``db_cursor`` replacement whose ``with`` block yields ``cursor``."""
    ctx = MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    return patch.object(sync_futu, "db_cursor", return_value=ctx)


class _RecordingCursor:
    """Records every ``execute(sql, params)`` call for later assertions.

    ``fetchall`` returns an empty result set: the pre-upsert fiscal-period guard
    (Issue #50, ``app.fiscal.reschedule_confirmed_rows``) asks for the period's
    existing confirmed rows, and "none exist" is exactly the state under which
    these tests assert the upsert SQL itself.
    """

    def __init__(self):
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []


class _FakeClock:
    """Deterministic ``time.monotonic`` replacement for pacing tests."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _RecordingSleep:
    """Records requested sleeps and advances the fake clock instead of waiting."""

    def __init__(self, clock):
        self.clock = clock
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)
        self.clock.now += seconds


def _fake_limiter(clock, sleep, *, max_calls=10_000, window_seconds=30):
    """A limiter that never blocks unless the test asks it to."""
    return sync_futu.FutuRateLimiter(
        max_calls=max_calls, window_seconds=window_seconds, clock=clock, sleep=sleep
    )


@contextlib.contextmanager
def _stage_env(limiter, *, retries=None, breaker=None, cursor=None):
    """Patch a Futu stage's collaborators (limiter, config knobs, DB, cancel check).

    Keeps the stage tests hermetic: no real OpenD calls, no real DB writes and
    no real waiting.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(sync_futu, "get_rate_limiter", return_value=limiter))
        stack.enter_context(patch.object(sync_futu, "check_cancelled"))
        stack.enter_context(_db_mock(cursor or _RecordingCursor()))
        stack.enter_context(patch("psycopg2.extras.execute_values"))
        if retries is not None:
            stack.enter_context(
                patch.object(sync_futu.config, "FUTU_RATE_LIMIT_MAX_RETRIES", retries))
        if breaker is not None:
            stack.enter_context(
                patch.object(sync_futu.config, "FUTU_RATE_LIMIT_CIRCUIT_BREAKER", breaker))
        yield


class FutuDatesProvenanceTests(TestCase):
    """The earnings-dates upsert must carry Futu provenance."""

    def setUp(self):
        import pandas as pd

        self.ctx = MagicMock()
        df = pd.DataFrame(
            [{"fiscal_year": 2026, "financial_type": 2,
              "pub_trading_day_str": "2026-07-30", "pub_type": 1}]
        )
        self.ctx.get_financials_earnings_price_history.return_value = (0, df)
        self._cursor = _RecordingCursor()
        self._batch = []
        self._sql = ""

        def fake_execute_values(cur, sql, argslist, page_size=200):
            self._batch = list(argslist)
            self._sql = sql

        self._ev_patch = patch("psycopg2.extras.execute_values",
                               side_effect=fake_execute_values)

    def test_dates_upsert_writes_futu_source(self):
        with patch.object(sync_futu, "check_cancelled"), \
             _db_mock(self._cursor), self._ev_patch:
            sync_futu.sync_earnings_dates(self.ctx, 1, ["AAPL.US"])

        # The batch rows must carry ('futu', 'scheduled') as the last two fields.
        assert self._batch, "expected at least one upsert row"
        row = self._batch[0]
        assert row[-2:] == ("futu", "scheduled"), (
            "dates batch rows must set date_source='futu', date_status='scheduled'"
        )
        # The INSERT column list and the conflict-update must both carry provenance.
        assert "date_source" in self._sql and "date_status" in self._sql
        assert "date_source = 'futu'" in self._sql
        assert "date_status = CASE" in self._sql


def _income_response(*, fy=2026, ft=2, eps=1.23, revenue=123.0,
                     eps_field_id=8047, eps_label="基本每股收益",
                     revenue_label="营业总收入", currency="USD",
                     standard="US_GAAP", extra_items=()):
    """One OpenD income-statement response in the layout the stage reads.

    Since Issue #62 the actuals stage takes both figures from
    ``statement_type=1``: EPS from ``fid=8047`` (``fid=8048`` as fallback) and
    revenue from ``fid=8002``, each carrying the provider's own ``display_name``.
    """
    items = list(extra_items)
    if eps is not None:
        items.append({"field_id": eps_field_id, "display_name": eps_label, "data": eps})
    if revenue is not None:
        items.append({"field_id": 8002, "display_name": revenue_label, "data": revenue})
    return {"report_list": [{"fiscal_year": fy, "financial_type": ft,
                             "currency_code": currency, "accounting_standards": standard,
                             "item_list": items}]}


class FutuActualsProvenanceTests(TestCase):
    """The actuals updates must flip status to reported and attribute Futu."""

    def setUp(self):
        self.ctx = MagicMock()
        # One income statement now carries EPS and revenue (Issue #62).
        self.ctx.get_financials_statements.return_value = (0, _income_response())
        self._cursor = _RecordingCursor()

    def test_actuals_updates_set_reported_and_futu_source(self):
        with patch.object(sync_futu, "check_cancelled"), \
             _db_mock(self._cursor):
            sync_futu.sync_actuals(self.ctx, 1, ["AAPL.US"])

        updates = [sql for sql, _ in self._cursor.executed
                   if sql.strip().startswith("UPDATE earnings")]
        assert len(updates) >= 2, "expected EPS and revenue updates to run"
        for sql in updates:
            assert "date_status = 'reported'" in sql, (
                "actuals update must mark the event reported"
            )
            assert "actual_source = 'futu'" in sql, (
                "actuals update must attribute the actuals to Futu"
            )

    def test_futu_updates_higher_priority_actuals_and_records_timestamp(self):
        """Issue #45: Futu must replace Longbridge actuals, not only fill NULL."""
        with patch.object(sync_futu, "check_cancelled"), \
             _db_mock(self._cursor):
            sync_futu.sync_actuals(self.ctx, 1, ["AAPL.US"])

        updates = [sql for sql, _ in self._cursor.executed
                   if sql.strip().startswith("UPDATE earnings")]
        assert len(updates) >= 2
        for sql in updates:
            assert "actual_as_of = NOW()" in sql
            assert "COALESCE(actual_source, 'unknown') IN" in sql
            assert "'longbridge'" in sql
            assert "IS NULL" not in sql
            assert "ABS(" not in sql


class FutuEpsFieldTests(TestCase):
    """Issue #62: the actual EPS must be the income statement's EPS field.

    The stage used to read a key-metrics field id whose ``display_name`` is
    流动比率 (current ratio), so every ``eps_actual`` Futu wrote measured
    liquidity.  These tests pin the corrected field ids, the provider-label
    check that makes a drifted id fail the symbol instead of writing another
    metric, and the fact that one call now carries both figures.
    """

    def _run(self, response, symbols=("AAPL.US",)):
        ctx = MagicMock()
        ctx.get_financials_statements.return_value = (0, response)
        cursor = _RecordingCursor()
        with patch.object(sync_futu, "check_cancelled"), _db_mock(cursor):
            stats = sync_futu.sync_actuals(ctx, 1, list(symbols))
        updates = [params for sql, params in cursor.executed
                   if sql.strip().startswith("UPDATE earnings")]
        return ctx, stats, updates

    def test_eps_is_read_from_the_income_statement_eps_field(self):
        ctx, stats, updates = self._run(_income_response(eps=2.03, revenue=109417000000.0))

        assert ctx.get_financials_statements.call_count == 1, (
            "EPS and revenue live in the same statement — one call per symbol"
        )
        _args, kwargs = ctx.get_financials_statements.call_args
        assert kwargs["statement_type"] == sync_futu.FUTU_STATEMENT_INCOME
        assert [params[0] for params in updates] == [2.03, 109417000000.0], (
            "the EPS update must carry 基本每股收益, not a key-metrics field"
        )
        assert stats.failed_symbols == 0

    def test_the_current_ratio_field_is_never_read_as_eps(self):
        """The wrong field may still arrive in the payload — it must be ignored."""
        response = _income_response(
            eps=2.03,
            extra_items=[{"field_id": 14020, "display_name": "流动比率", "data": 1.003295}],
        )
        _ctx, _stats, updates = self._run(response)

        assert [params[0] for params in updates] == [2.03, 123.0]
        assert all(1.003295 not in params for params in updates), (
            "a liquidity ratio must never reach eps_actual"
        )

    def test_diluted_eps_is_the_fallback_when_basic_is_absent(self):
        response = _income_response(eps=2.02, eps_field_id=sync_futu.FUTU_FID_INCOME_EPS_DILUTED,
                                    eps_label="稀释每股收益")
        _ctx, stats, updates = self._run(response)

        assert updates[0][0] == 2.02
        assert stats.failed_symbols == 0

    def test_a_drifted_eps_field_fails_the_symbol_instead_of_writing(self):
        """A drifted label must not become a value in the EPS column."""
        response = _income_response(eps=1.003295, eps_label="流动比率")
        with self.assertLogs("sync_futu", level="WARNING") as logs:
            _ctx, stats, updates = self._run(response)

        assert stats.failed_symbols == 1, (
            "a field whose label contradicts its id must be audited as a symbol failure"
        )
        assert [params[0] for params in updates] == [123.0], (
            "only the verified revenue figure may be written"
        )
        assert "Issue #62" in "\n".join(logs.output)

    def test_a_drifted_revenue_field_is_refused_too(self):
        response = _income_response(revenue=1.003295, revenue_label="流动比率")
        _ctx, stats, updates = self._run(response)

        assert [params[0] for params in updates] == [1.23]
        assert stats.failed_symbols == 1

    def test_an_unlabelled_field_is_written_but_reported_as_unverified(self):
        response = _income_response(eps_label="", revenue_label="")
        with self.assertLogs("sync_futu", level="WARNING") as logs:
            _ctx, stats, updates = self._run(response)

        assert [params[0] for params in updates] == [1.23, 123.0]
        assert stats.failed_symbols == 0, "a missing label is not drift"
        assert "semantics unverified" in "\n".join(logs.output)

    def test_retracted_actuals_stay_replaceable_by_the_corrected_sync(self):
        """Issue #62 criterion 3: voiding a wrong actual must not freeze the row."""
        ctx = MagicMock()
        ctx.get_financials_statements.return_value = (0, _income_response())
        cursor = _RecordingCursor()
        with patch.object(sync_futu, "check_cancelled"), _db_mock(cursor):
            sync_futu.sync_actuals(ctx, 1, ["AAPL.US"])

        sql = "\n".join(sql for sql, _ in cursor.executed)
        assert "'futu_invalid_field'" in sql
        assert "futu_invalid_field" in sync_futu.REPLACEABLE_ACTUAL_SOURCES


class FutuWatchdogTests(TestCase):
    """Issue #48: an expired watchdog must be catchable, not fatal."""

    def test_default_disposition_kills_process_but_handler_does_not(self):
        """The Issue's repro: SIG_DFL terminates the process (exit 142 / -14).

        With the watchdog handler installed the same overrun raises a catchable
        ``FutuCallTimeout`` and the process survives to finish the batch.
        """
        baseline = subprocess.run(
            [sys.executable, "-c", "import signal,time; signal.alarm(1); time.sleep(5)"],
            capture_output=True, text=True,
        )
        assert baseline.returncode == -signal.SIGALRM, (
            "baseline repro changed: SIGALRM no longer terminates the process"
        )

        child = (
            "import importlib.util, time\n"
            "spec = importlib.util.spec_from_file_location('sync_futu', r'%s')\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "try:\n"
            "    with m.futu_call_timeout(1):\n"
            "        time.sleep(5)\n"
            "except m.FutuCallTimeout:\n"
            "    print('timeout-caught')\n"
            "print('process-alive')\n"
        ) % (ROOT / "scripts" / "sync_futu.py")

        proc = subprocess.run([sys.executable, "-c", child],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert "timeout-caught" in proc.stdout
        assert "process-alive" in proc.stdout

    def test_handler_registered_and_watchdog_disarmed(self):
        assert sync_futu._install_alarm_handler() is True
        assert signal.getsignal(signal.SIGALRM) is not signal.SIG_DFL
        with sync_futu.futu_call_timeout(30):
            pass
        assert signal.alarm(0) == 0, "watchdog must be disarmed after the call"

    def test_zero_window_skips_alarm(self):
        with sync_futu.futu_call_timeout(0) as _:
            pass
        assert signal.alarm(0) == 0


class FutuTimeoutIsolationTests(TestCase):
    """Issue #48: one wedged symbol must be counted as failed, not abort the batch."""

    def _run_dates(self, *, hang_time, timeout):
        import pandas as pd

        ctx = MagicMock()

        def fetch(futu_code):
            if futu_code == "US.AAPL":
                time.sleep(hang_time)
            df = pd.DataFrame(
                [{"fiscal_year": 2026, "financial_type": 2,
                  "pub_trading_day_str": "2026-07-30", "pub_type": 1}]
            )
            return 0, df

        ctx.get_financials_earnings_price_history.side_effect = fetch
        captured = {}

        def fake_execute_values(cur, sql, argslist, page_size=200):
            captured["batch"] = list(argslist)

        with patch.object(sync_futu, "check_cancelled"), \
             patch.object(sync_futu.config, "FUTU_DATES_TIMEOUT_SECONDS", timeout), \
             _db_mock(_RecordingCursor()), \
             patch("psycopg2.extras.execute_values", side_effect=fake_execute_values):
            stats = sync_futu.sync_earnings_dates(ctx, 1, ["AAPL.US", "MSFT.US"])
        return stats, captured.get("batch", [])

    def test_hanging_symbol_is_counted_and_loop_continues(self):
        stats, batch = self._run_dates(hang_time=5, timeout=1)
        assert stats.failed_symbols == 1, "the wedged symbol must be counted as a failed symbol"
        assert stats.total == 1, "the remaining symbols must still be fetched in this batch"
        assert [row[0] for row in batch] == ["MSFT"]


class FutuRunTerminalStateTests(TestCase):
    """Issue #48: the audited run must always end terminal, never 'running'."""

    def _run_sync(self, *, dates_result, actuals_result, finished):
        ctx = MagicMock()
        src = MagicMock()
        src.get_futu_symbols_with_skipped.return_value = (["AAPL.US"], [])

        def dates(ctx_, run_id, symbols):
            if isinstance(dates_result, BaseException):
                raise dates_result
            return dates_result

        def actuals(ctx_, run_id, symbols):
            if isinstance(actuals_result, BaseException):
                raise actuals_result
            return actuals_result

        def fake_finish(run_id, **kwargs):
            finished.append(kwargs["status"])
            return True

        with patch.object(sync_futu, "get_source", return_value=src), \
             patch("app.sync_audit.start_run", return_value=42), \
             patch("app.sync_audit.finish_run", side_effect=fake_finish), \
             patch("app.sync_audit.heartbeat"), \
             patch.object(sync_futu, "sync_earnings_dates", side_effect=dates), \
             patch.object(sync_futu, "sync_actuals", side_effect=actuals):
            return sync_futu.run_sync(ctx)

    def test_success_path_is_terminal(self):
        finished = []
        assert self._run_sync(
            dates_result=sync_futu.FutuStageStats(total=2),
            actuals_result=sync_futu.FutuStageStats(total=1),
            finished=finished) == 42
        assert finished[0] == "success"
        assert "running" not in finished

    def test_failed_stage_is_terminal(self):
        finished = []
        with self.assertRaises(RuntimeError):
            self._run_sync(dates_result=RuntimeError("boom"),
                           actuals_result=sync_futu.FutuStageStats(), finished=finished)
        assert finished[0] == "failed"
        assert "running" not in finished

    def test_base_exception_still_reaches_terminal_state(self):
        """A watchdog/SIGTERM-style ``BaseException`` must not leak a running row."""
        finished = []
        with self.assertRaises(KeyboardInterrupt):
            self._run_sync(dates_result=KeyboardInterrupt(),
                           actuals_result=sync_futu.FutuStageStats(), finished=finished)
        assert finished == ["interrupted"], (
            "an unhandled BaseException must still force a terminal state"
        )


class FutuIdempotencyRecoveryTests(TestCase):
    """Issue #48 criterion 3: a forced interruption must not block the next sync.

    Runs against the real schema inside a transaction that is rolled back, so it
    proves the unique-index/idempotency interaction without persisting anything.
    Opt in with ``FINCAL_TEST_DB=1`` plus reachable DB_* settings.
    """

    @skipUnless(os.environ.get("FINCAL_TEST_DB") == "1",
                "set FINCAL_TEST_DB=1 with a reachable fincal DB")
    def test_interrupted_run_does_not_block_next_start(self):
        from app import db as db_mod
        from app import sync_audit

        key = "futu:earnings:full:issue48-selftest"
        with db_mod.db_connection() as conn:
            conn.autocommit = False

            @contextlib.contextmanager
            def _cursor():
                with conn.cursor(cursor_factory=db_mod.psycopg2.extras.RealDictCursor) as cur:
                    yield cur

            try:
                with patch.object(db_mod, "db_cursor", _cursor):
                    first = sync_audit.start_run("futu", "futu", idempotency_key=key)
                    assert first is not None, "first attempt must create a run"
                    # Simulate the interrupted path (alarm/SIGTERM before finish).
                    assert sync_audit.finish_run(first, status="interrupted",
                                                 error_code="futu_sync_interrupted")
                    second = sync_audit.start_run("futu", "futu", idempotency_key=key)
                    assert second is not None and second != first, (
                        "a terminal (non-running) row must not block the next attempt"
                    )
                    assert sync_audit.finish_run(second, status="failed",
                                                 error_code="futu_sync_failed")
            finally:
                conn.rollback()

            with patch.object(db_mod, "db_cursor", _cursor):
                with db_mod.db_cursor() as cur:
                    cur.execute("SELECT count(*) AS n FROM sync_runs WHERE idempotency_key=%s",
                                (key,))
                    row = cur.fetchone() or {}
                    assert row.get("n") == 0, "self-test must not persist rows"


class FutuRateLimiterTests(TestCase):
    """Issue #49: OpenD calls must be paced, not fired as fast as possible."""

    def test_calls_within_budget_do_not_wait(self):
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        limiter = _fake_limiter(clock, sleep, max_calls=3, window_seconds=30)
        waited = [limiter.acquire() for _ in range(3)]
        assert waited == [0.0, 0.0, 0.0]
        assert sleep.slept == []

    def test_exceeding_the_budget_waits_out_the_window(self):
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        limiter = _fake_limiter(clock, sleep, max_calls=3, window_seconds=30)
        for _ in range(3):
            limiter.acquire()
        waited = limiter.acquire()
        assert waited == 30.0, "the 4th call in the same window must wait"
        assert sleep.slept == [30.0]

    def test_budget_recovers_after_the_window_slides(self):
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        limiter = _fake_limiter(clock, sleep, max_calls=2, window_seconds=30)
        limiter.acquire()
        limiter.acquire()
        clock.now += 30  # oldest calls leave the window
        assert limiter.acquire() == 0.0
        assert sleep.slept == []

    def test_cool_down_clears_the_window(self):
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        limiter = _fake_limiter(clock, sleep, max_calls=2, window_seconds=30)
        limiter.acquire()
        assert limiter.cool_down() == 30.0
        assert sleep.slept == [30.0]
        # The window was spent when OpenD refused us, so it is empty afterwards.
        assert limiter.acquire() == 0.0


class FutuFailureClassificationTests(TestCase):
    """Issue #49: a quota rejection, an unsupported instrument and a real
    failure must not collapse into the same opaque ``ret=-1``."""

    RATE_LIMIT_MSG = "获取财务报表频率太高，请求失败，每30秒最多30次。"

    def _env(self, clock, sleep, *, retries=0, breaker=99):
        return _stage_env(_fake_limiter(clock, sleep), retries=retries, breaker=breaker)

    def test_provider_message_is_kept_in_the_log(self):
        clock = _FakeClock()
        stats = sync_futu.FutuStageStats()
        with self.assertLogs("sync_futu", level="WARNING") as logs:
            outcome, _ = sync_futu.futu_call(
                "US.AAPL", "EPS", lambda: (-1, self.RATE_LIMIT_MSG),
                _fake_limiter(clock, _RecordingSleep(clock)), stats,
                timeout_seconds=0,
            )
        assert outcome == sync_futu.OUTCOME_RATE_LIMITED
        assert self.RATE_LIMIT_MSG in "\n".join(logs.output), (
            "the provider's reason must survive into the logs (Issue #49)"
        )

    def test_rate_limited_call_is_retried_then_counted(self):
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        ctx = MagicMock()
        ctx.get_financials_earnings_price_history.return_value = (-1, self.RATE_LIMIT_MSG)

        with self._env(clock, sleep, retries=2), \
             self.assertLogs("sync_futu", level="WARNING") as logs:
            stats = sync_futu.sync_earnings_dates(ctx, 1, ["AAPL.US"])

        assert ctx.get_financials_earnings_price_history.call_count == 3, (
            "a quota rejection must be retried (1 call + FUTU_RATE_LIMIT_MAX_RETRIES)"
        )
        assert stats.retries == 2
        assert stats.rate_limited_calls == 1
        assert stats.rate_limited_symbols == 1
        assert stats.failed_symbols == 0, "a quota rejection is not a symbol failure"
        assert sleep.slept == [30.0, 30.0], "each rejection waits out one quota window"
        assert self.RATE_LIMIT_MSG in "\n".join(logs.output)

    def test_unsupported_symbol_is_not_retried_or_fatal(self):
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        ctx = MagicMock()
        ctx.get_financials_earnings_price_history.return_value = (-1, "该接口仅支持正股")

        with self._env(clock, sleep, retries=2), \
             self.assertLogs("sync_futu", level="INFO") as logs:
            stats = sync_futu.sync_earnings_dates(ctx, 1, ["SPY.US"])

        assert ctx.get_financials_earnings_price_history.call_count == 1, (
            "an unsupported instrument fails permanently — retrying wastes quota"
        )
        assert stats.unsupported_symbols == 1
        assert stats.failed_symbols == 0
        assert stats.rate_limited_calls == 0
        assert sleep.slept == []
        assert sync_futu.futu_audit_outcome(stats) == ("success", None), (
            "structurally unsupported instruments must not fail the run"
        )
        assert "该接口仅支持正股" in "\n".join(logs.output)

    def test_unknown_provider_error_stays_a_symbol_failure(self):
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        ctx = MagicMock()
        ctx.get_financials_earnings_price_history.return_value = (-1, "some unknown OpenD error")

        with self._env(clock, sleep, retries=2):
            stats = sync_futu.sync_earnings_dates(ctx, 1, ["AAPL.US"])

        assert stats.failed_symbols == 1
        assert stats.unsupported_symbols == 0
        assert stats.rate_limited_calls == 0
        assert sync_futu.futu_audit_outcome(stats) == ("failed", "futu_symbol_fetch_failed")

    def test_circuit_breaker_stops_hammering_the_provider(self):
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        symbols = [f"SYM{i}.US" for i in range(40)]
        ctx = MagicMock()
        ctx.get_financials_earnings_price_history.return_value = (-1, self.RATE_LIMIT_MSG)

        with self._env(clock, sleep, retries=0, breaker=3):
            stats = sync_futu.sync_earnings_dates(ctx, 1, symbols)

        calls = ctx.get_financials_earnings_price_history.call_count
        assert stats.rate_limited is True, "the breaker must be flagged in the stats"
        assert stats.symbols_attempted == 3
        assert calls == 3 and calls < len(symbols), (
            "a persistent quota rejection must short-circuit the stage, not brute-force "
            "every remaining symbol"
        )

    def test_actuals_symbols_are_counted_once_each(self):
        clock = _FakeClock()
        sleep = _RecordingSleep(clock)
        ctx = MagicMock()
        ctx.get_financials_statements.return_value = (-1, "该接口仅支持正股")

        with self._env(clock, sleep):
            stats = sync_futu.sync_actuals(ctx, 1, ["SPY.US"])

        assert stats.total == 1
        assert stats.unsupported_symbols == 1, "one rejected call must not double-count the symbol"
        assert sync_futu.futu_audit_outcome(stats) == ("success", None)

    def test_pacing_wait_is_not_counted_as_a_watchdog_timeout(self):
        """The watchdog bounds one provider call, not the wait for a quota slot.

        With the watchdog armed around the pacer, a 1.5 s pacing sleep inside a
        1 s window raised ``FutuCallTimeout`` and the symbol was recorded as a
        failed symbol — throttling masqueraded as wedged symbols (observed on
        the first production run of the Issue #49 change).
        """
        import pandas as pd

        limiter = sync_futu.FutuRateLimiter(max_calls=1, window_seconds=1.5,
                                            clock=time.monotonic, sleep=time.sleep)
        ctx = MagicMock()
        ctx.get_financials_earnings_price_history.return_value = (
            0, pd.DataFrame([{"fiscal_year": 2026, "financial_type": 2,
                              "pub_trading_day_str": "2026-07-30", "pub_type": 1}]),
        )

        with _stage_env(limiter), \
             patch.object(sync_futu.config, "FUTU_DATES_TIMEOUT_SECONDS", 1):
            stats = sync_futu.sync_earnings_dates(ctx, 1, ["AAPL.US", "MSFT.US"])

        assert stats.failed_symbols == 0, "waiting for a quota slot must not look like a timeout"
        assert stats.total == 2


class FutuAuditDetailsTests(TestCase):
    """Issue #49: ``sync_runs.details`` must separate the failure kinds."""

    def test_details_keep_historical_keys_and_add_classification(self):
        date_stats = sync_futu.FutuStageStats(
            total=3, failed_symbols=1, unsupported_symbols=2, rate_limited_calls=4, retries=8,
        )
        actual_stats = sync_futu.FutuStageStats(
            total=9, unsupported_symbols=1, rate_limited_symbols=2,
        )
        details = sync_futu.futu_audit_details(date_stats, actual_stats, ["000651.SZ"])

        assert details["actual_symbols"] == 9
        assert details["date_failed_symbols"] == 1
        assert details["actual_failed_symbols"] == 0
        assert details["unsupported_symbols"] == 3
        assert details["rate_limited_calls"] == 4
        assert details["rate_limited_symbols"] == 2
        assert details["rate_limit_retries"] == 8
        assert details["rate_limited"] is False
        assert details["skipped_symbols"] == 1

    def test_rate_limit_outranks_symbol_failures(self):
        stats = sync_futu.FutuStageStats(failed_symbols=7, rate_limited_calls=1)
        assert sync_futu.futu_audit_outcome(stats) == ("failed", "futu_rate_limited")


class FutuRateLimitAuditTests(TestCase):
    """Issue #49 criterion 1: the audited run must report the provider refusal."""

    def _run(self, date_stats, actual_stats, skipped=()):
        finished = []
        ctx = MagicMock()
        src = MagicMock()
        src.get_futu_symbols_with_skipped.return_value = (["AAPL.US"], list(skipped))

        def fake_finish(run_id, **kwargs):
            finished.append(kwargs)
            return True

        with patch.object(sync_futu, "get_source", return_value=src), \
             patch("app.sync_audit.start_run", return_value=11), \
             patch("app.sync_audit.finish_run", side_effect=fake_finish), \
             patch("app.sync_audit.heartbeat"), \
             patch.object(sync_futu, "sync_earnings_dates", return_value=date_stats), \
             patch.object(sync_futu, "sync_actuals", return_value=actual_stats):
            sync_futu.run_sync(ctx)
        return finished

    def test_rate_limited_run_is_audited_as_futu_rate_limited(self):
        finished = self._run(
            sync_futu.FutuStageStats(total=35, rate_limited_calls=12,
                                     rate_limited_symbols=12, rate_limited=True, retries=24),
            sync_futu.FutuStageStats(total=4, rate_limited_symbols=4),
            skipped=["000651.SZ"],
        )
        assert finished[0]["status"] == "failed"
        assert finished[0]["error_code"] == "futu_rate_limited"
        assert finished[0]["details"]["rate_limited_calls"] == 12
        assert finished[0]["details"]["rate_limited"] is True
        assert finished[0]["details"]["skipped_symbols"] == 1

    def test_unsupported_only_run_stays_successful(self):
        finished = self._run(
            sync_futu.FutuStageStats(total=0, unsupported_symbols=4),
            sync_futu.FutuStageStats(total=5, unsupported_symbols=4),
        )
        assert finished[0]["status"] == "success"
        assert finished[0]["error_code"] is None
        assert finished[0]["details"]["unsupported_symbols"] == 8


if __name__ == "__main__":
    import unittest

    unittest.main()
