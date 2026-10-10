"""Regression tests for Issue #33: enforce each run's own ``timeout_seconds``.

Before this, ``reap_timeout_runs()`` had no caller anywhere in the repository
(``grep -rn reap_timeout_runs`` matched only its definition), so the timeout
recovery described by Issues #3/#19 did not exist in practice: a run whose
owning process died — a stage killed by the cron wrapper's ``timeout``, a
container restart mid-run — kept ``status='running'`` and was rendered as a
running job by ``/api/admin/sync-runs`` and the admin panel until the container
started again.  Both helpers also ignored the per-run ``timeout_seconds`` column
that ``start_run()`` writes for every run, judging everything against a global
3600s cutoff ("configuration illusion").

Covers:
- the reaper's cutoff is each row's own ``timeout_seconds`` (SQL contract);
- against real PostgreSQL (opt-in, ``FINCAL_TEST_DB=1``): a run past its own
  timeout is interrupted while a longer-budget run is left alone;
- the background loop keeps reaping after a failed pass and stops on request;
- the application lifespan starts the reaper and stops it on shutdown.
"""
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, db, sync_audit, timeout_reaper
from app.sync_audit import reap_timeout_runs

# The stage budgets the pipeline really enforces (Issue #78): a row is only
# reclaimable once its *declared* budget — the one `start_run()` now writes from
# the same variable `sync_all.sh` uses — has elapsed.
stage_timeout = config.stage_timeout


# ── SQL contract ───────────────────────────────────────────────────────────

class _RecordingCursor:
    """Captures the SQL and parameters of the reaper's single statement."""

    def __init__(self, rowcount: int = 0):
        self.rowcount = rowcount
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def close(self):
        pass


@contextmanager
def _cursor(cursor):
    yield cursor


def _run_reaper(cursor, timeout_seconds=None):
    with patch.object(db, "db_cursor", return_value=_cursor(cursor)):
        if timeout_seconds is None:
            return reap_timeout_runs()
        return reap_timeout_runs(timeout_seconds)


class ReaperCutoffTests(TestCase):
    """The per-run column decides, not a global cutoff."""

    def test_the_cutoff_is_the_rows_own_timeout_not_a_global_timestamp(self):
        cursor = _RecordingCursor()
        _run_reaper(cursor)
        sql, params = cursor.executed[-1]

        self.assertIn("timeout_seconds", sql,
                      "the cutoff must come from the row's own column")
        self.assertIn("NULLIF(timeout_seconds, 0)", sql,
                      "a zero timeout must fall back instead of reaping instantly")
        # The old shape: a Python-computed global cutoff compared to heartbeat_at.
        self.assertNotIn("heartbeat_at < %s", sql)
        self.assertEqual(params, (sync_audit._DEFAULT_TIMEOUT_SECONDS,),
                         "the bound parameter is only the fallback, not the cutoff")

    def test_a_missing_heartbeat_is_aged_from_the_start_time(self):
        cursor = _RecordingCursor()
        _run_reaper(cursor)
        sql, _ = cursor.executed[-1]
        self.assertIn("COALESCE(heartbeat_at, started_at)", sql,
                      "a row that never wrote a heartbeat must still be reapable")

    def test_a_reaped_run_is_recorded_as_a_reaper_timeout(self):
        cursor = _RecordingCursor()
        _run_reaper(cursor)
        sql, _ = cursor.executed[-1]
        self.assertIn("status = 'interrupted'", sql)
        self.assertIn("error_code = 'timeout_reaper'", sql)
        self.assertIn('"recovered_by": "reaper"', sql)

    def test_it_reports_how_many_runs_it_reaped(self):
        self.assertEqual(_run_reaper(_RecordingCursor(rowcount=3)), 3)
        self.assertEqual(_run_reaper(_RecordingCursor(rowcount=0)), 0)


# ── opt-in: the real statement on real PostgreSQL ──────────────────────────
@skipUnless(os.environ.get("FINCAL_TEST_DB") == "1",
            "set FINCAL_TEST_DB=1 with a reachable fincal DB (the reaper's own "
            "UPDATE runs against a temp table inside a rolled-back transaction)")
class PerRunTimeoutTests(TestCase):
    """A long-budget run must survive while a short-budget one is reaped.

    The budgets are the declared per-stage ones (Issue #78), so this class also
    fails if a stage's declared budget stops being a usable cutoff.
    """

    ROWS = (
        # label, heartbeat age, started age, timeout_seconds, expected reaped
        # `prediction` is budgeted 600s in the pipeline: a row whose heartbeat
        # stopped at its start is reclaimable once 700s have passed.
        ("expired_vs_own_timeout", "700 seconds", "700 seconds",
         stage_timeout("prediction"), True),
        # `consensus` gets the widest budget (2400s) and — like every stage since
        # Issue #78 — keeps heartbeating while it works: a live run must not be
        # reaped at its budget.
        ("long_budget_still_healthy", "1 minute", "1 minute",
         stage_timeout("consensus"), False),
        ("expired_vs_short_budget", "2 hours", "2 hours",
         stage_timeout("stock_names"), True),
        ("no_heartbeat_expired", None, "2 hours", stage_timeout("stock_names"), True),
        ("no_heartbeat_fresh", None, "0 seconds", stage_timeout("stock_names"), False),
        ("zero_timeout_falls_back", "2 hours", "2 hours", 0, True),
    )

    def test_only_the_runs_past_their_own_timeout_are_interrupted(self):
        with db.db_connection() as conn:
            conn.autocommit = False

            # A temp table named sync_runs shadows the real audit table for this
            # session only: the production statement is executed unchanged and
            # every write is rolled back below.
            with conn.cursor() as setup:
                setup.execute("""
                    CREATE TEMP TABLE sync_runs (
                        id SERIAL PRIMARY KEY,
                        stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        heartbeat_at TIMESTAMPTZ,
                        timeout_seconds INTEGER NOT NULL DEFAULT 3600,
                        details JSONB NOT NULL DEFAULT '{}'::jsonb,
                        finished_at TIMESTAMPTZ,
                        error_code TEXT
                    )
                """)
                for label, heartbeat, started, timeout, _ in self.ROWS:
                    setup.execute(
                        """INSERT INTO sync_runs
                               (stage, status, started_at, heartbeat_at, timeout_seconds)
                           VALUES (%s, 'running', NOW() - %s::interval,
                                   CASE WHEN %s IS NULL THEN NULL
                                        ELSE NOW() - %s::interval END,
                                   %s)""",
                        (label, started, heartbeat, heartbeat, timeout),
                    )
                setup.execute("SELECT count(*) FROM sync_runs WHERE status='running'")
                running_before = setup.fetchone()[0]

            @contextmanager
            def _transaction_cursor():
                with conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
                    yield cur

            try:
                with patch.object(db, "db_cursor", _transaction_cursor):
                    reaped = reap_timeout_runs()

                self.assertEqual(running_before, len(self.ROWS))
                self.assertEqual(reaped, sum(1 for *_, expected in self.ROWS if expected))

                with conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT stage, status, error_code, finished_at "
                                "FROM sync_runs ORDER BY id")
                    rows = {row["stage"]: row for row in cur.fetchall()}
                for label, _, _, _, expected in self.ROWS:
                    row = rows[label]
                    self.assertEqual(
                        row["status"], "interrupted" if expected else "running", label)
                    if expected:
                        self.assertEqual(row["error_code"], "timeout_reaper", label)
                        self.assertIsNotNone(row["finished_at"], label)

                # The real audit table is untouched: nothing in this session wrote
                # to it (the temp table shadowed it), and the transaction is
                # rolled back anyway.
                with conn.cursor() as probe:
                    probe.execute("SELECT count(*) FROM pg_catalog.pg_class "
                                  "WHERE relname='sync_runs' AND relpersistence='t'")
                    self.assertEqual(probe.fetchone()[0], 1)
            finally:
                conn.rollback()


# ── the background loop ────────────────────────────────────────────────────
class ReaperLoopTests(TestCase):
    def setUp(self):
        timeout_reaper.stop_timeout_reaper()

    tearDown = setUp

    def test_the_loop_keeps_reaping_after_a_failed_pass(self):
        calls = []
        stop = threading.Event()

        def flaky_reap():
            calls.append(len(calls))
            if len(calls) == 1:
                raise RuntimeError("connection reset")
            if len(calls) >= 3:
                stop.set()
            return 1

        with patch.object(timeout_reaper, "reap_timeout_runs", flaky_reap), \
             self.assertLogs("app.timeout_reaper", level="WARNING") as logs:
            timeout_reaper.run_reaper_loop(0.005, stop)

        self.assertGreaterEqual(len(calls), 3,
                               "the loop stopped after one failed pass")
        self.assertIn("connection reset", "\n".join(logs.output))

    def test_the_loop_returns_when_the_stop_event_is_set(self):
        stop = threading.Event()
        stop.set()
        start = time.monotonic()
        with patch.object(timeout_reaper, "reap_timeout_runs", lambda: 0):
            timeout_reaper.run_reaper_loop(30, stop)   # must not wait out the 30s
        self.assertLess(time.monotonic() - start, 5)

    def test_starting_the_reaper_runs_one_daemon_thread_that_stops(self):
        with patch.object(timeout_reaper, "reap_timeout_runs", lambda: 2):
            self.assertIsNotNone(timeout_reaper.start_timeout_reaper(0.01))
            thread = timeout_reaper.reaper_thread()
            self.assertIsNotNone(thread)
            self.assertEqual(thread.name, "sync-run-reaper")
            self.assertTrue(thread.daemon)
            time.sleep(0.05)
            timeout_reaper.stop_timeout_reaper()

        self.assertIsNotNone(thread)
        self.assertFalse(thread.is_alive(), "the reaper thread outlived shutdown")
        self.assertIsNone(timeout_reaper.reaper_thread())

    def test_a_second_start_reuses_the_running_thread(self):
        with patch.object(timeout_reaper, "reap_timeout_runs", lambda: 0):
            timeout_reaper.start_timeout_reaper(0.01)
            thread = timeout_reaper.reaper_thread()
            timeout_reaper.start_timeout_reaper(0.01)
            self.assertIs(timeout_reaper.reaper_thread(), thread)

    def test_a_disabled_reaper_starts_no_thread(self):
        with patch.object(config, "SYNC_RUN_REAPER_ENABLED", False), \
             self.assertLogs("app.timeout_reaper", level="INFO") as logs:
            self.assertIsNone(timeout_reaper.start_timeout_reaper(0.01))
        self.assertIsNone(timeout_reaper.reaper_thread())
        self.assertIn("timeout reaper disabled", "\n".join(logs.output))

    def test_the_configured_interval_is_the_one_that_is_used(self):
        passes = []
        with patch.object(config, "SYNC_RUN_REAPER_INTERVAL_SECONDS", 0.02), \
             patch.object(config, "SYNC_RUN_REAPER_ENABLED", True), \
             patch.object(timeout_reaper, "reap_timeout_runs",
                          lambda: passes.append(1) or 0), \
             self.assertLogs("app.timeout_reaper", level="INFO") as logs:
            timeout_reaper.start_timeout_reaper()
            time.sleep(0.09)
            timeout_reaper.stop_timeout_reaper()
        self.assertGreaterEqual(
            len(passes), 2, "the reaper did not use the configured interval")
        self.assertIn("every 0s", "\n".join(logs.output))


# ── lifespan wiring ────────────────────────────────────────────────────────
class LifespanWiringTests(TestCase):
    """The reaper is only useful if the running application actually starts it."""

    def test_the_lifespan_starts_the_reaper_and_stops_it_on_shutdown(self):
        from fastapi.testclient import TestClient

        from app import earnings, main

        timeout_reaper.stop_timeout_reaper()
        reap_calls = []

        with patch.object(db, "init_db"), \
             patch.object(earnings, "seed_earnings_if_empty"), \
             patch.object(sync_audit, "recover_stale_runs") as recover, \
             patch.object(timeout_reaper, "reap_timeout_runs",
                          lambda: reap_calls.append(1) or 0):
            with TestClient(main.app):
                self.assertIsNotNone(
                    timeout_reaper.reaper_thread(),
                    "the application lifespan did not start the timeout reaper")
                self.assertTrue(timeout_reaper.reaper_thread().is_alive())
            self.assertIsNone(timeout_reaper.reaper_thread(),
                              "the reaper was not stopped on shutdown")

        recover.assert_called_once()
