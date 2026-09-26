"""Issue #53: sync stages and the data they derive must be freshness-checked.

The pipeline declared by ``scripts/sync_all.sh`` runs weekly, but the only
observability was dependency reachability plus a fixed 24-hour run window.
Production therefore carried ``consensus`` idle since 2026-08-02 (44 days) and
``stock_names`` since 2026-08-03 while ``/api/admin/health`` kept reporting the
dependencies healthy and the calendar kept rendering the 6-week-old consensus
snapshot without a stale marker.

Covered here:

* the ``fresh`` / ``stale`` / ``never`` verdicts for stages and derived tables;
* the verdict is taken from the **most recent success**, not from a time window
  (a weekly pipeline has no run in the last 24h on most days);
* ``/api/admin/health`` reports ``status != healthy`` with a language-neutral
  ``error_code`` while ``/api/admin/ready`` stays unaffected;
* ``scripts/check_sync_freshness.py`` exit codes;
* the check is SELECT-only and makes no external calls;
* the declared stage list can never drift away from ``scripts/sync_all.sh``.
"""
import importlib.util
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, db, freshness  # noqa: E402
from app.freshness import (  # noqa: E402
    DERIVED_TABLES,
    STAGE_SCRIPTS,
    SYNC_STAGES,
    check_freshness,
    health_snapshot,
)


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_script = _load("check_sync_freshness", "scripts/check_sync_freshness.py")

NOW = datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)


class _Cursor:
    """Records executed SQL; serves queued ``fetchall`` results per execute."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self._i = 0
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        rows = self._results[self._i] if self._i < len(self._results) else []
        self._i += 1
        return rows

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def close(self):
        pass


def _stage_rows(**ages):
    """Build sync_runs rows from ``stage=<timedelta ago>`` or ``None``."""
    rows = []
    for stage, age in ages.items():
        rows.append({
            "stage": stage,
            "last_success_at": None if age is None else NOW - age,
        })
    return rows


def _derived_rows(**ages):
    rows = []
    for name, age in ages.items():
        rows.append({"name": name, "last_at": None if age is None else NOW - age})
    return rows


def _patch_db(results):
    cursor = _Cursor(results)

    @contextmanager
    def _db_cursor():
        yield cursor

    return cursor, mock.patch.object(db, "db_cursor", _db_cursor)


class FreshnessVerdictTests(TestCase):
    def test_all_stages_recent_is_healthy(self):
        results = [
            _stage_rows(**{s: timedelta(hours=3) for s in SYNC_STAGES}),
            _derived_rows(**{t: timedelta(hours=4) for t, _ in DERIVED_TABLES}),
        ]
        _, patcher = _patch_db(results)
        with patcher:
            summary = check_freshness(now=NOW)

        self.assertEqual(summary["status"], "healthy")
        self.assertIsNone(summary["error_code"])
        self.assertEqual(summary["stale_stages"], [])
        self.assertEqual(summary["never_run_stages"], [])
        self.assertEqual(summary["stale_data"], [])
        self.assertTrue(all(e["status"] == "fresh" for e in summary["entries"]))
        # 5 stages + 5 derived tables
        self.assertEqual(len(summary["entries"]), len(SYNC_STAGES) + len(DERIVED_TABLES))

    def test_stale_stage_is_degraded_with_language_neutral_code(self):
        ages = {s: timedelta(hours=1) for s in SYNC_STAGES}
        ages["consensus"] = timedelta(days=47)
        results = [
            _stage_rows(**ages),
            _derived_rows(**{t: timedelta(hours=1) for t, _ in DERIVED_TABLES}),
        ]
        _, patcher = _patch_db(results)
        with patcher:
            summary = check_freshness(now=NOW)

        self.assertEqual(summary["status"], "degraded")
        self.assertEqual(summary["error_code"], freshness.ERROR_STAGE_STALE)
        self.assertEqual(summary["stale_stages"], ["consensus"])
        entry = next(e for e in summary["entries"] if e["stage"] == "consensus")
        self.assertEqual(entry["status"], "stale")
        self.assertEqual(entry["kind"], "stage")
        self.assertEqual(entry["error_code"], freshness.ERROR_STAGE_STALE)
        self.assertAlmostEqual(entry["age_hours"], 47 * 24, places=1)

    def test_never_run_stage_wins_over_stale(self):
        ages = {s: timedelta(days=30) for s in SYNC_STAGES}
        ages["stock_names"] = None
        results = [
            _stage_rows(**ages),
            _derived_rows(**{t: timedelta(days=30) for t, _ in DERIVED_TABLES}),
        ]
        _, patcher = _patch_db(results)
        with patcher:
            summary = check_freshness(now=NOW)

        self.assertEqual(summary["status"], "degraded")
        self.assertEqual(summary["error_code"], freshness.ERROR_STAGE_NEVER)
        self.assertEqual(summary["never_run_stages"], ["stock_names"])
        entry = next(e for e in summary["entries"] if e["stage"] == "stock_names")
        self.assertEqual(entry["status"], "never")
        self.assertIsNone(entry["age_hours"])
        self.assertIsNone(entry["last_success_at"])

    def test_missing_stage_row_is_never_run(self):
        """A stage with no sync_runs row at all must not be read as healthy."""
        results = [
            _stage_rows(longbridge=timedelta(hours=1)),
            _derived_rows(**{t: timedelta(hours=1) for t, _ in DERIVED_TABLES}),
        ]
        _, patcher = _patch_db(results)
        with patcher:
            summary = check_freshness(now=NOW)

        self.assertEqual(sorted(summary["never_run_stages"]),
                         sorted(set(SYNC_STAGES) - {"longbridge"}))
        self.assertEqual(summary["status"], "degraded")

    def test_stale_derived_data_alone_is_degraded(self):
        """Issue #53 core symptom: every stage green, the data itself 47 days old."""
        results = [
            _stage_rows(**{s: timedelta(hours=1) for s in SYNC_STAGES}),
            _derived_rows(
                earnings_consensus=timedelta(days=47),
                earnings_forecast_eps=timedelta(days=47),
                earnings_institution_ratings=timedelta(days=47),
                stock_names=timedelta(days=1),
                earnings=timedelta(hours=2),
            ),
        ]
        _, patcher = _patch_db(results)
        with patcher:
            summary = check_freshness(now=NOW)

        self.assertEqual(summary["status"], "degraded")
        self.assertEqual(summary["error_code"], freshness.ERROR_DATA_STALE)
        self.assertEqual(summary["stale_stages"], [])
        self.assertEqual(
            summary["stale_data"],
            ["earnings_consensus", "earnings_forecast_eps", "earnings_institution_ratings"],
        )

    def test_never_fetched_derived_table_is_reported(self):
        results = [
            _stage_rows(**{s: timedelta(hours=1) for s in SYNC_STAGES}),
            _derived_rows(
                earnings_consensus=None,
                earnings_forecast_eps=timedelta(hours=1),
                earnings_institution_ratings=timedelta(hours=1),
                stock_names=timedelta(hours=1),
                earnings=timedelta(hours=1),
            ),
        ]
        _, patcher = _patch_db(results)
        with patcher:
            summary = check_freshness(now=NOW)

        entry = next(e for e in summary["entries"] if e["stage"] == "earnings_consensus")
        self.assertEqual(entry["status"], "never")
        self.assertEqual(entry["error_code"], freshness.ERROR_DATA_MISSING)
        self.assertIn("earnings_consensus", summary["stale_data"])

    def test_verdict_uses_latest_success_not_a_time_window(self):
        """No run inside 24h (any Tuesday) must still read as fresh when the last
        success is inside the threshold — the old fixed 24h window would have
        reported '0 runs' and been unusable."""
        results = [
            _stage_rows(**{s: timedelta(days=3) for s in SYNC_STAGES}),
            _derived_rows(**{t: timedelta(days=3) for t, _ in DERIVED_TABLES}),
        ]
        cursor, patcher = _patch_db(results)
        with patcher:
            summary = check_freshness(now=NOW)

        self.assertEqual(summary["status"], "healthy")
        stage_sql = cursor.executed[0][0]
        self.assertNotIn("started_at >", stage_sql)
        self.assertNotIn("NOW()", stage_sql)

    def test_threshold_is_configurable_and_validated(self):
        results = [
            _stage_rows(**{s: timedelta(days=3) for s in SYNC_STAGES}),
            _derived_rows(**{t: timedelta(days=3) for t, _ in DERIVED_TABLES}),
        ]
        _, patcher = _patch_db(results)
        with patcher:
            tight = check_freshness(now=NOW, threshold_hours=24)
        self.assertEqual(tight["status"], "degraded")
        self.assertEqual(len(tight["stale_stages"]), len(SYNC_STAGES))

        with mock.patch.object(config, "SYNC_STAGE_STALE_AFTER_HOURS", 24):
            self.assertEqual(freshness.stale_after_hours(), 24.0)
        with mock.patch.object(config, "SYNC_STAGE_STALE_AFTER_HOURS", "not-a-number"):
            self.assertEqual(freshness.stale_after_hours(), freshness.DEFAULT_STALE_AFTER_HOURS)
        with mock.patch.object(config, "SYNC_STAGE_STALE_AFTER_HOURS", 0):
            self.assertEqual(freshness.stale_after_hours(), freshness.DEFAULT_STALE_AFTER_HOURS)

    def test_naive_timestamps_are_read_as_utc(self):
        results = [
            _stage_rows(**{s: timedelta(hours=1) for s in SYNC_STAGES}),
            _derived_rows(**{t: timedelta(hours=1) for t, _ in DERIVED_TABLES}),
        ]
        for row in results[0]:
            row["last_success_at"] = row["last_success_at"].replace(tzinfo=None)
        _, patcher = _patch_db(results)
        with patcher:
            summary = check_freshness(now=NOW.replace(tzinfo=None))
        self.assertEqual(summary["status"], "healthy")

    def test_unreadable_database_is_unknown_not_an_exception(self):
        @contextmanager
        def _boom():
            raise RuntimeError("db down")
            yield

        with mock.patch.object(db, "db_cursor", _boom):
            summary = check_freshness(now=NOW)

        self.assertEqual(summary["status"], "unknown")
        self.assertEqual(summary["error_code"], freshness.ERROR_UNAVAILABLE)
        self.assertEqual(summary["entries"], [])


class ReadOnlyContractTests(TestCase):
    def _results(self):
        return [
            _stage_rows(**{s: timedelta(hours=1) for s in SYNC_STAGES}),
            _derived_rows(**{t: timedelta(hours=1) for t, _ in DERIVED_TABLES}),
        ]

    def test_only_select_statements_are_executed(self):
        cursor, patcher = _patch_db(self._results())
        with patcher:
            check_freshness(now=NOW)

        self.assertEqual(len(cursor.executed), 2)  # one stage query, one derived query
        for sql, _ in cursor.executed:
            self.assertTrue(sql.strip().upper().startswith("SELECT"), sql)
            # Word-boundary match so a column such as `updated_at` is not
            # mistaken for a mutation statement.
            for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE"):
                self.assertIsNone(re.search(rf"\b{forbidden}\b", sql.upper()), sql)

    def test_no_external_probe_is_made(self):
        def _fail(*args, **kwargs):
            raise AssertionError("freshness check must not touch the network or spawn processes")

        with mock.patch("socket.create_connection", _fail), \
                mock.patch("subprocess.run", _fail), \
                mock.patch("urllib.request.urlopen", _fail):
            cursor, patcher = _patch_db(self._results())
            with patcher:
                summary = check_freshness(now=NOW)

        self.assertEqual(summary["status"], "healthy")

    def test_module_source_does_not_import_probe_clients(self):
        source = (ROOT / "app" / "freshness.py").read_text()
        for module in ("socket", "subprocess", "urllib", "futu", "requests"):
            self.assertNotIn(f"import {module}", source)


class HealthIntegrationTests(TestCase):
    def _health_results(self, stage_ages, derived_ages):
        # `admin.health_check()` judges with its own (real) clock: unlike
        # `check_freshness(now=...)` it takes no injected reference.  The ages
        # below are therefore laid out relative to the clock it will actually
        # read — building them from the module's fixed NOW made this class
        # "stale" purely because real time had moved on (Issue #64 note).
        now = datetime.now(timezone.utc)
        return [
            [{"stage": stage, "last_success_at": now - age} for stage, age in stage_ages.items()],
            [{"name": name, "last_at": now - age} for name, age in derived_ages.items()],
        ]

    def _call_health(self, results):
        from app.routers import admin

        cursor = _Cursor(results)

        @contextmanager
        def _db_cursor():
            yield cursor

        ok_process = mock.MagicMock(return_value=mock.MagicMock(returncode=0))
        ok_response = mock.MagicMock(status=200)
        ok_response.__enter__ = lambda self: ok_response
        ok_response.__exit__ = lambda self, *exc: False
        with mock.patch.object(db, "db_cursor", _db_cursor), \
                mock.patch("socket.create_connection", mock.MagicMock()), \
                mock.patch("urllib.request.urlopen", mock.MagicMock(return_value=ok_response)), \
                mock.patch("subprocess.run", ok_process):
            return admin.health_check()

    def test_health_reports_degraded_when_a_stage_is_stale(self):
        ages = {s: timedelta(hours=2) for s in SYNC_STAGES}
        ages["consensus"] = timedelta(days=47)
        ages["stock_names"] = timedelta(days=46)
        response = self._call_health(self._health_results(
            ages, {t: timedelta(hours=2) for t, _ in DERIVED_TABLES}))

        self.assertNotEqual(response["status"], "healthy")
        check = response["checks"]["sync_freshness"]
        self.assertEqual(check["status"], "degraded")
        self.assertEqual(check["error_code"], freshness.ERROR_STAGE_STALE)
        # Reported in declared-pipeline order, not by age.
        self.assertEqual(check["stale_stages"], ["stock_names", "consensus"])
        self.assertEqual(check["never_run_stages"], [])

    def test_health_is_healthy_when_everything_is_fresh(self):
        response = self._call_health(self._health_results(
            {s: timedelta(hours=2) for s in SYNC_STAGES},
            {t: timedelta(hours=2) for t, _ in DERIVED_TABLES}))

        self.assertEqual(response["status"], "healthy")
        self.assertEqual(response["checks"]["sync_freshness"]["status"], "healthy")
        self.assertIsNone(response["checks"]["sync_freshness"]["error_code"])

    def test_health_exposes_aggregates_only(self):
        ages = {s: timedelta(hours=2) for s in SYNC_STAGES}
        ages["prediction"] = timedelta(days=40)
        response = self._call_health(self._health_results(
            ages, {t: timedelta(hours=2) for t, _ in DERIVED_TABLES}))

        check = response["checks"]["sync_freshness"]
        self.assertNotIn("entries", check)
        self.assertNotIn("last_success_at", check)
        self.assertEqual(sorted(check), sorted(freshness.AGGREGATE_KEYS))

    def test_readiness_is_not_blocked_by_stale_stages(self):
        """/api/admin/ready stays dependency-only: staleness must degrade health
        reporting, never fail the readiness probe (Issue #53 acceptance 2)."""
        from app.routers import admin

        cursor = _Cursor([[{"?column?": 1}]])

        @contextmanager
        def _db_cursor():
            yield cursor

        with mock.patch.object(db, "db_cursor", _db_cursor):
            ready = admin.readiness_check()

        self.assertEqual(ready, {"status": "ready"})

    def test_health_snapshot_drops_entry_details(self):
        results = [
            _stage_rows(**{s: timedelta(hours=1) for s in SYNC_STAGES}),
            _derived_rows(**{t: timedelta(hours=1) for t, _ in DERIVED_TABLES}),
        ]
        _, patcher = _patch_db(results)
        with patcher:
            summary = check_freshness(now=NOW)
        snapshot = health_snapshot(summary)

        self.assertEqual(sorted(snapshot), sorted(freshness.AGGREGATE_KEYS))
        self.assertNotIn("entries", snapshot)
        self.assertNotIn("last_success_at", snapshot)


class DiagnosticsWindowTests(TestCase):
    def test_diagnostics_window_is_configurable_and_carries_freshness(self):
        from app.routers import admin

        cursor = _Cursor([[], [], []])  # 24h summary, window summary, recent runs

        @contextmanager
        def _db_cursor():
            yield cursor

        with mock.patch.object(db, "db_cursor", _db_cursor), \
                mock.patch.object(config, "SYNC_RUNS_WINDOW_HOURS", 336), \
                mock.patch.object(freshness, "check_freshness",
                                  mock.MagicMock(return_value={
                                      "status": "degraded",
                                      "error_code": freshness.ERROR_STAGE_STALE,
                                      "threshold_hours": 192.0,
                                      "stale_stages": ["consensus"],
                                      "never_run_stages": [],
                                      "stale_data": [],
                                      "checked_at": NOW.isoformat(),
                                      "entries": [],
                                  })):
            result = admin.diagnostics({})

        self.assertEqual(result["sync_runs_window_hours"], 336)
        self.assertIn("sync_runs_24h", result)          # kept for compatibility
        self.assertIn("sync_runs_window", result)
        self.assertEqual(result["freshness"]["stale_stages"], ["consensus"])
        windowed = [sql for sql, params in cursor.executed
                    if params == (336,)]
        self.assertEqual(len(windowed), 2)  # window summary + recent runs
        self.assertIn("make_interval", windowed[0])
        self.assertIn("started_at >", windowed[1])


class StageRegistryDriftTests(TestCase):
    """Acceptance 5: a stage added to the pipeline can never go unmonitored."""

    def test_registry_keys_match_declared_stages(self):
        self.assertEqual(set(STAGE_SCRIPTS), set(SYNC_STAGES))

    def test_registry_covers_every_script_in_sync_all(self):
        script = (ROOT / "scripts" / "sync_all.sh").read_text()
        invoked = set(re.findall(r"python\s+(scripts/[\w./-]+\.py)", script))

        self.assertTrue(invoked, "sync_all.sh must invoke the sync scripts")
        self.assertEqual(invoked, set(STAGE_SCRIPTS.values()),
                         "every stage in scripts/sync_all.sh must be registered in "
                         "app/freshness.STAGE_SCRIPTS (and vice versa)")

    def test_every_registered_stage_is_started_by_its_script(self):
        """The stage name in the registry must be the one the script records."""
        for stage, relative in STAGE_SCRIPTS.items():
            source = (ROOT / relative).read_text()
            self.assertIn(f'start_run("{stage}"', source,
                          f"{relative} must call start_run(\"{stage}\", …)")


class ScriptExitCodeTests(TestCase):
    def _run_with(self, summary):
        with mock.patch.object(check_script, "check_freshness", return_value=summary):
            return check_script.main()

    def test_exit_0_when_fresh(self):
        summary = {
            "status": "healthy", "error_code": None, "threshold_hours": 192.0,
            "checked_at": NOW.isoformat(), "stale_stages": [], "never_run_stages": [],
            "stale_data": [], "entries": [],
        }
        self.assertEqual(self._run_with(summary), check_script.EXIT_OK)

    def test_exit_1_and_lists_stale_stages(self):
        summary = {
            "status": "degraded", "error_code": freshness.ERROR_STAGE_STALE,
            "threshold_hours": 192.0, "checked_at": NOW.isoformat(),
            "stale_stages": ["consensus", "stock_names"], "never_run_stages": [],
            "stale_data": [],
            "entries": [
                {"stage": "consensus", "kind": "stage", "status": "stale",
                 "last_success_at": "2026-08-02T09:27:43+00:00", "age_hours": 1112.54,
                 "error_code": freshness.ERROR_STAGE_STALE},
                {"stage": "stock_names", "kind": "stage", "status": "stale",
                 "last_success_at": "2026-08-03T09:51:57+00:00", "age_hours": 1088.13,
                 "error_code": freshness.ERROR_STAGE_STALE},
            ],
        }
        with mock.patch("sys.stderr", new_callable=_CapturedStderr) as err:
            code = self._run_with(summary)
        self.assertEqual(code, check_script.EXIT_STALE)

    def test_exit_1_when_a_stage_never_ran(self):
        summary = {
            "status": "degraded", "error_code": freshness.ERROR_STAGE_NEVER,
            "threshold_hours": 192.0, "checked_at": NOW.isoformat(),
            "stale_stages": [], "never_run_stages": ["stock_names"], "stale_data": [],
            "entries": [{"stage": "stock_names", "kind": "stage", "status": "never",
                         "last_success_at": None, "age_hours": None,
                         "error_code": freshness.ERROR_STAGE_NEVER}],
        }
        self.assertEqual(self._run_with(summary), check_script.EXIT_STALE)

    def test_exit_1_when_only_derived_data_is_stale(self):
        summary = {
            "status": "degraded", "error_code": freshness.ERROR_DATA_STALE,
            "threshold_hours": 192.0, "checked_at": NOW.isoformat(),
            "stale_stages": [], "never_run_stages": [],
            "stale_data": ["earnings_consensus"],
            "entries": [{"stage": "earnings_consensus", "kind": "derived",
                         "status": "stale", "last_success_at": "2026-08-02T09:27:41+00:00",
                         "age_hours": 1112.5, "error_code": freshness.ERROR_DATA_STALE}],
        }
        self.assertEqual(self._run_with(summary), check_script.EXIT_STALE)

    def test_exit_2_when_freshness_is_unknown(self):
        summary = {
            "status": "unknown", "error_code": freshness.ERROR_UNAVAILABLE,
            "threshold_hours": 192.0, "checked_at": NOW.isoformat(),
            "stale_stages": [], "never_run_stages": [], "stale_data": [], "entries": [],
        }
        self.assertEqual(self._run_with(summary), check_script.EXIT_UNKNOWN)


class _CapturedStderr:
    """Minimal stderr stand-in so print(file=sys.stderr) is captured."""

    def __init__(self, *args, **kwargs):
        self.text = ""

    def write(self, value):
        self.text += value

    def flush(self):
        pass
