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
    """Records every ``execute(sql, params)`` call for later assertions."""

    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


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
        self._src = MagicMock()
        self._src.get_futu_symbols.return_value = ["AAPL.US"]
        self._cursor = _RecordingCursor()
        self._batch = []
        self._sql = ""

        def fake_execute_values(cur, sql, argslist, page_size=200):
            self._batch = list(argslist)
            self._sql = sql

        self._ev_patch = patch("psycopg2.extras.execute_values",
                               side_effect=fake_execute_values)

    def test_dates_upsert_writes_futu_source(self):
        with patch.object(sync_futu, "get_source", return_value=self._src), \
             patch.object(sync_futu, "check_cancelled"), \
             _db_mock(self._cursor), self._ev_patch:
            sync_futu.sync_earnings_dates(self.ctx, 1)

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


class FutuActualsProvenanceTests(TestCase):
    """The actuals updates must flip status to reported and attribute Futu."""

    def setUp(self):
        self.ctx = MagicMock()
        # First call (EPS, statement_type=4) then second (revenue, statement_type=1).
        self.ctx.get_financials_statements.side_effect = [
            (0, {"report_list": [{"fiscal_year": 2026, "financial_type": 2,
                                 "item_list": [{"field_id": 14020, "data": 1.23}]}]}),
            (0, {"report_list": [{"fiscal_year": 2026, "financial_type": 2,
                                 "item_list": [{"field_id": 8002, "data": 123.0}]}]}),
        ]
        self._src = MagicMock()
        self._src.get_futu_symbols.return_value = ["AAPL.US"]
        self._cursor = _RecordingCursor()

    def test_actuals_updates_set_reported_and_futu_source(self):
        with patch.object(sync_futu, "get_source", return_value=self._src), \
             patch.object(sync_futu, "check_cancelled"), \
             _db_mock(self._cursor):
            sync_futu.sync_actuals(self.ctx, 1)

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
        src = MagicMock()
        src.get_futu_symbols.return_value = ["AAPL.US", "MSFT.US"]
        captured = {}

        def fake_execute_values(cur, sql, argslist, page_size=200):
            captured["batch"] = list(argslist)

        with patch.object(sync_futu, "get_source", return_value=src), \
             patch.object(sync_futu, "check_cancelled"), \
             patch.object(sync_futu.config, "FUTU_DATES_TIMEOUT_SECONDS", timeout), \
             _db_mock(_RecordingCursor()), \
             patch("psycopg2.extras.execute_values", side_effect=fake_execute_values):
            total, failures = sync_futu.sync_earnings_dates(ctx, 1)
        return total, failures, captured.get("batch", [])

    def test_hanging_symbol_is_counted_and_loop_continues(self):
        total, failures, batch = self._run_dates(hang_time=5, timeout=1)
        assert failures == 1, "the wedged symbol must be counted as a failed symbol"
        assert total == 1, "the remaining symbols must still be fetched in this batch"
        assert [row[0] for row in batch] == ["MSFT"]


class FutuRunTerminalStateTests(TestCase):
    """Issue #48: the audited run must always end terminal, never 'running'."""

    def _run_sync(self, *, dates_result, actuals_result, finished):
        ctx = MagicMock()
        src = MagicMock()
        src.get_futu_symbols.return_value = ["AAPL.US"]

        def dates(ctx_, run_id):
            if isinstance(dates_result, BaseException):
                raise dates_result
            return dates_result

        def actuals(ctx_, run_id):
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
        assert self._run_sync(dates_result=(2, 0), actuals_result=(1, 0),
                              finished=finished) == 42
        assert finished[0] == "success"
        assert "running" not in finished

    def test_failed_stage_is_terminal(self):
        finished = []
        with self.assertRaises(RuntimeError):
            self._run_sync(dates_result=RuntimeError("boom"),
                           actuals_result=(0, 0), finished=finished)
        assert finished[0] == "failed"
        assert "running" not in finished

    def test_base_exception_still_reaches_terminal_state(self):
        """A watchdog/SIGTERM-style ``BaseException`` must not leak a running row."""
        finished = []
        with self.assertRaises(KeyboardInterrupt):
            self._run_sync(dates_result=KeyboardInterrupt(),
                           actuals_result=(0, 0), finished=finished)
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


if __name__ == "__main__":
    import unittest

    unittest.main()
