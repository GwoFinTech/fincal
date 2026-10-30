"""Issue #104 (remaining item): "fetched pages, resolved no rows" is a failure.

The 2026-10-29 incident had two halves:

* the identity field moved from ``counter_id`` to ``symbol`` — fixed in
  ``6a1424a`` (see ``tests/test_lb_calendar_payload.py``); and
* a run that fetched 599 calendar pages and resolved **no** event was recorded
  as ``success`` with ``record_count=0``, so ``/api/admin/health`` reported
  ``sync_freshness: healthy`` for the three days the calendar was frozen.

These cases pin the second half:

* the stage counts what it skipped (``events`` / ``unresolved``) instead of
  ``continue``-ing silently;
* such a run is terminal ``failed`` with ``error_code=longbridge_no_rows``;
* freshness reads the stage's **most recent run**, so a failed run degrades
  ``/api/admin/health`` immediately instead of waiting out the 192h threshold;
* ``scripts/check_sync_freshness.py`` exits 1 and names the stage.
"""
import importlib.util
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase, mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, freshness  # noqa: E402
from app.freshness import (  # noqa: E402
    DERIVED_TABLES,
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


sync_earnings = _load("sync_earnings_no_rows", "scripts/sync_earnings.py")
check_script = _load("check_sync_freshness_no_rows", "scripts/check_sync_freshness.py")

NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)


# ── the stage's own counters ────────────────────────────────────────────────

def _event(**overrides) -> dict:
    event = {
        "symbol": "AAPL.US", "market": "US", "counter_name": "Apple",
        "currency": "USD", "date": "2026.08.04 (美东)", "date_type": "盘后",
        "data_kv": [],
        "ext": {"financial_report": {"fiscal_year": "2026", "period": "2",
                                     "period_type": "qf"}},
    }
    event.update(overrides)
    return event


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(pages):
    """Run ``sync_earnings`` against a fake calendar; return its SyncStats."""

    def fake_flush(stats, batch, label):
        # The write path itself is covered elsewhere; here only the counters of
        # what the stage saw and skipped matter.
        return None

    def fake_fetch(market, start, end):
        return pages if market == "US" else []

    with patch.object(sync_earnings, "fetch_calendar", side_effect=fake_fetch), \
            patch.object(sync_earnings, "check_cancelled"), \
            patch("app.sync_audit.heartbeat"), \
            patch.object(sync_earnings, "_flush_batch", side_effect=fake_flush):
        stats = sync_earnings.SyncStats()
        sync_earnings.sync_earnings(1, stats)
    return stats


class StageCountsWhatItSkippedTests(TestCase):
    def test_a_resolvable_payload_counts_no_unresolved_events(self):
        stats = _run([{"infos": [_event(), _event(symbol="MSFT.US")]}])

        self.assertEqual((stats.pages, stats.events, stats.unresolved), (1, 2, 0))
        self.assertEqual(stats.resolved(), 2)
        self.assertEqual(stats.fetched, 2)

    def test_events_without_an_identity_are_counted_not_silently_dropped(self):
        """The 2026-10-29 shape: pages arrive, every event is unresolvable."""
        stats = _run([{"infos": [
            {"counter_name": "Immutep", "date": "2026.10.25 (EST)"},
            {"counter_name": "DAJIN", "date": "2026.10.26"},
        ]}])

        self.assertEqual(stats.pages, 1)
        self.assertEqual(stats.events, 2)
        self.assertEqual(stats.unresolved, 2)
        self.assertEqual(stats.resolved(), 0)
        self.assertEqual(stats.fetched, 0)
        self.assertEqual(stats.written, 0)

    def test_events_with_an_unparseable_date_are_counted_too(self):
        stats = _run([{"infos": [_event(date="tomorrow")]}])

        self.assertEqual((stats.events, stats.unresolved), (1, 1))
        self.assertEqual(stats.fetched, 0)

    def test_the_audit_details_carry_the_unresolved_count(self):
        stats = _run([{"infos": [{"counter_name": "???", "date": "2026.08.04"}]}])
        details = stats.quality().to_dict()

        self.assertEqual(details["unresolved"], 1)
        self.assertEqual(details["fetched"], 0)
        self.assertEqual(details["status"], "unavailable")


class TerminalStateTests(TestCase):
    def test_pages_without_a_single_resolved_event_is_a_failure(self):
        stats = sync_earnings.SyncStats(pages=599, events=25115, unresolved=25115)
        self.assertEqual(sync_earnings.run_terminal_state(stats),
                         ("failed", "longbridge_no_rows"))

    def test_pages_that_carry_no_events_at_all_are_also_a_failure(self):
        """A provider answering with day buckets but no events synced nothing."""
        stats = sync_earnings.SyncStats(pages=3, events=0, unresolved=0)
        self.assertEqual(sync_earnings.run_terminal_state(stats),
                         ("failed", "longbridge_no_rows"))

    def test_a_partially_resolved_payload_stays_a_success(self):
        stats = sync_earnings.SyncStats(pages=2, events=100, unresolved=3, written=97)
        self.assertEqual(sync_earnings.run_terminal_state(stats), ("success", None))

    def test_an_empty_calendar_window_is_not_a_failure(self):
        """No pages at all means "the window had no releases", not a fault."""
        self.assertEqual(sync_earnings.run_terminal_state(sync_earnings.SyncStats()),
                         ("success", None))

    def test_a_dead_batch_still_wins(self):
        stats = sync_earnings.SyncStats(pages=1, events=10, unresolved=10,
                                        failed_batches=1, written=0)
        self.assertEqual(sync_earnings.run_terminal_state(stats),
                         ("failed", "longbridge_batch_failed"))


# ── freshness reads the most recent run ─────────────────────────────────────

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

    def close(self):
        pass


def _stage_row(stage, *, success_age=None, run_age=None, run_status="success",
               run_error_code=None):
    return {
        "stage": stage,
        "last_success_at": None if success_age is None else NOW - success_age,
        "last_run_at": None if run_age is None else NOW - run_age,
        "last_run_status": run_status,
        "last_run_error_code": run_error_code,
    }


def _derived_rows(age=timedelta(hours=1)):
    return [{"name": name, "last_at": NOW - age} for name, _ in DERIVED_TABLES]


def _check(stage_rows):
    cursor = _Cursor([stage_rows, _derived_rows()])

    @contextmanager
    def _db_cursor():
        yield cursor

    with mock.patch.object(db, "db_cursor", _db_cursor):
        return check_freshness(now=NOW)


class FailedRunDegradesHealthTests(TestCase):
    def _rows(self, **overrides):
        rows = [_stage_row(s, success_age=timedelta(hours=2), run_age=timedelta(hours=2))
                for s in SYNC_STAGES]
        for stage, values in overrides.items():
            rows = [row for row in rows if row["stage"] != stage]
            rows.append(_stage_row(stage, **values))
        return rows

    def test_a_failed_most_recent_run_is_degraded_immediately(self):
        """Two hours after the last success: inside the threshold, still not fine."""
        summary = _check(self._rows(longbridge=dict(
            success_age=timedelta(hours=2), run_age=timedelta(hours=1),
            run_status="failed", run_error_code="longbridge_no_rows")))

        self.assertEqual(summary["status"], "degraded")
        self.assertEqual(summary["error_code"], freshness.ERROR_STAGE_FAILED)
        self.assertEqual(summary["failed_stages"], ["longbridge"])
        self.assertEqual(summary["stale_stages"], [])
        # The other stages are untouched by the failure.
        other = next(e for e in summary["entries"] if e["stage"] == "futu")
        self.assertEqual(other["status"], "fresh")
        self.assertIsNone(other["error_code"])

        entry = next(e for e in summary["entries"] if e["stage"] == "longbridge")
        self.assertEqual(entry["status"], "failed")
        # The run's own language-neutral code wins over the generic one.
        self.assertEqual(entry["error_code"], "longbridge_no_rows")
        self.assertEqual(entry["last_run_status"], "failed")
        self.assertEqual(entry["last_run_error_code"], "longbridge_no_rows")
        self.assertAlmostEqual(entry["age_hours"], 2.0, places=2)

    def test_a_generic_failed_run_without_a_code_is_still_reported(self):
        summary = _check(self._rows(futu=dict(
            success_age=timedelta(hours=2), run_age=timedelta(hours=1),
            run_status="failed")))
        entry = next(e for e in summary["entries"] if e["stage"] == "futu")

        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["error_code"], freshness.ERROR_STAGE_FAILED)
        self.assertEqual(summary["failed_stages"], ["futu"])

    def test_a_success_after_the_failure_clears_it(self):
        summary = _check(self._rows(longbridge=dict(
            success_age=timedelta(hours=1), run_age=timedelta(minutes=30),
            run_status="success")))

        self.assertEqual(summary["status"], "healthy")
        self.assertEqual(summary["failed_stages"], [])
        self.assertIsNone(summary["error_code"])

    def test_a_running_or_cancelled_run_does_not_fail_the_stage(self):
        """Only a *failed* run refreshed nothing for a reason worth alarming on."""
        for status in ("running", "cancelled", "skipped", "interrupted"):
            summary = _check(self._rows(longbridge=dict(
                success_age=timedelta(hours=2), run_age=timedelta(minutes=5),
                run_status=status)))
            self.assertEqual(summary["status"], "healthy", status)
            self.assertEqual(summary["failed_stages"], [], status)

    def test_a_failed_run_reports_failed_not_stale(self):
        summary = _check(self._rows(longbridge=dict(
            success_age=timedelta(days=30), run_age=timedelta(days=1),
            run_status="failed", run_error_code="longbridge_no_rows")))
        entry = next(e for e in summary["entries"] if e["stage"] == "longbridge")

        self.assertEqual(entry["status"], "failed")
        self.assertEqual(summary["failed_stages"], ["longbridge"])
        self.assertEqual(summary["stale_stages"], [])

    def test_health_snapshot_exposes_the_failed_stage_names_only(self):
        summary = _check(self._rows(longbridge=dict(
            success_age=timedelta(hours=2), run_age=timedelta(minutes=30),
            run_status="failed", run_error_code="longbridge_no_rows")))
        snapshot = health_snapshot(summary)

        self.assertEqual(snapshot["failed_stages"], ["longbridge"])
        self.assertEqual(sorted(snapshot), sorted(freshness.AGGREGATE_KEYS))
        self.assertNotIn("entries", snapshot)

    def test_a_successful_run_is_fresh_even_with_no_run_timestamp_columns(self):
        """Old fixtures carried only ``last_success_at``; keep tolerating that."""
        rows = [{"stage": s, "last_success_at": NOW - timedelta(hours=1)}
                for s in SYNC_STAGES]
        summary = _check(rows)

        self.assertEqual(summary["status"], "healthy")
        self.assertEqual(summary["failed_stages"], [])
        self.assertTrue(all(e["last_run_status"] is None for e in summary["entries"]))


class GateTests(TestCase):
    def _summary(self, **extra):
        summary = {
            "status": "degraded", "error_code": freshness.ERROR_STAGE_FAILED,
            "threshold_hours": 192.0, "checked_at": NOW.isoformat(),
            "stale_stages": [], "never_run_stages": [], "failed_stages": ["longbridge"],
            "stale_data": [],
            "entries": [{
                "stage": "longbridge", "kind": "stage", "status": "failed",
                "last_success_at": "2026-10-26T00:19:00+00:00", "age_hours": 74.7,
                "error_code": "longbridge_no_rows",
                "last_run_at": "2026-10-29T03:02:13+00:00",
                "last_run_status": "failed",
                "last_run_error_code": "longbridge_no_rows",
            }],
        }
        summary.update(extra)
        return summary

    def test_the_gate_fails_and_names_the_stage(self):
        stderr = mock.MagicMock()
        with mock.patch.object(check_script, "check_freshness",
                               return_value=self._summary()), \
                mock.patch("sys.stderr", stderr):
            self.assertEqual(check_script.main(), check_script.EXIT_STALE)

        printed = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("FAILED: stage 'longbridge'", printed)
        self.assertIn("longbridge_no_rows", printed)

    def test_a_healthy_summary_still_exits_zero(self):
        summary = {
            "status": "healthy", "error_code": None, "threshold_hours": 192.0,
            "checked_at": NOW.isoformat(), "stale_stages": [], "never_run_stages": [],
            "failed_stages": [], "stale_data": [], "entries": [],
        }
        with mock.patch.object(check_script, "check_freshness", return_value=summary):
            self.assertEqual(check_script.main(), check_script.EXIT_OK)


class ResponseContractTests(TestCase):
    def test_the_health_contract_names_failed_stages(self):
        from app import schemas

        self.assertIn("failed_stages", schemas.SyncFreshnessCheck.model_fields)
        self.assertIn("failed_stages", schemas.FreshnessResponse.model_fields)
        for field in ("last_run_at", "last_run_status", "last_run_error_code"):
            self.assertIn(field, schemas.FreshnessEntry.model_fields)

    def test_the_stage_query_keeps_one_round_trip(self):
        """The verdict must not cost a second query against ``sync_runs``."""
        self.assertIn("last_run_status", freshness._STAGE_QUERY)
        self.assertIn("FILTER (WHERE status = 'success')", freshness._STAGE_QUERY)

        cursor = _Cursor([[], _derived_rows()])

        @contextmanager
        def _db_cursor():
            yield cursor

        with mock.patch.object(db, "db_cursor", _db_cursor):
            check_freshness(now=NOW)

        self.assertEqual(len(cursor.executed), 2)
        for sql, _ in cursor.executed:
            self.assertTrue(sql.strip().upper().startswith("SELECT"), sql)
