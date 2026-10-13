"""Issue #81: a provider date may never promote a prediction of another period.

Production incident (2026-10-12, ``sync_runs`` id 113): the ``futu`` stage of the
weekly pipeline died after fetching every symbol, on its single batch upsert:

    psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint
    "idx_earnings_fiscal_identity"
    DETAIL: Key (symbol, market, fiscal_year, fiscal_quarter)=(1888.HK, HK, 2026, 2)

``longbridge`` had just written that period's confirmed row (2026-07-15); the
period still had a stale *prediction* one season later (2026-08-24), which is
exactly the date OpenD reported.  ``reschedule_confirmed_rows`` recognised the
collision (``target_occupied``), skipped the move, and — per Issue #52 — left the
incoming row to the caller's ``ON CONFLICT ... DO UPDATE SET is_predicted = FALSE``.
That merge promoted the *prediction* into a second confirmed row of the period,
straight into the fiscal-identity partial unique index of Issue #50.  The whole
``execute_values`` batch rolled back (188 rows) and the stage raised, so the
week's ``sync_actuals`` never ran, and the failed run recorded ``details={}`` with
``record_count=0``.

Covered here:

* the provider's date now wins: the period's stale prediction is removed and its
  confirmed row moves onto the provider's date, so the batch row merges into the
  confirmed row by natural key;
* both provider write paths promote a conflicting row only while it carries the
  incoming row's **own** fiscal identity, so no upsert can mint a second
  confirmed identity (the Longbridge path carries the same pattern);
* one conflict no longer costs the batch or the stage: the date upsert is written
  per symbol inside a savepoint, a rejected symbol is retried row by row, the
  losses are counted in ``sync_runs.details``, and ``sync_actuals`` still runs;
* a stage that dies mid-way still reports what it had counted.
"""
import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import fiscal  # noqa: E402
from psycopg2 import errors as pg_errors  # noqa: E402


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync_futu = _load("sync_futu_issue81", "scripts/sync_futu.py")
sync_earnings = _load("sync_earnings_issue81", "scripts/sync_earnings.py")


def _ts(day: int) -> datetime:
    return datetime(2026, 9, day, tzinfo=timezone.utc)


# ── fixtures: the incident's state ──────────────────────────────────────────

#: ``1888.HK`` FY2026 Q2 as ``longbridge`` confirmed it at 08:03 (row 159143).
def _confirmed_1888():
    return {
        "id": 159143, "symbol": "1888.HK", "market": "HK", "fiscal_year": 2026,
        "fiscal_quarter": 2, "report_date": date(2026, 7, 15), "report_type": "Q",
        "is_predicted": False, "eps_actual": None, "revenue_actual": None,
        "updated_at": _ts(1),
    }


#: The same period's stale prediction — the season's median month, which is the
#: date OpenD reports (row 324155 in the incident).
def _stale_prediction_1888():
    return {"id": 324155, "fiscal_year": 2026, "fiscal_quarter": 2, "is_predicted": True}


#: The OpenD response row for FY2026 Q2 (batch layout of ``sync_futu``).
def _futu_row(symbol, market, report_date, fiscal_year, fiscal_quarter,
              before_after="before"):
    return (symbol, market, "", report_date, "Q", fiscal_year, fiscal_quarter,
            before_after, "futu", "scheduled")


#: One Longbridge batch tuple (the layout ``sync_earnings.flush_batch`` expects).
def _lb_row(symbol, market, company_name, report_date, fiscal_year, fiscal_quarter):
    return (symbol, market, company_name, report_date, "Q", fiscal_year, fiscal_quarter,
            None, None, None, None, "after", None, None, "unknown", "unknown")


class _FakeCursor:
    """Records statements and replays canned result sets in order."""

    def __init__(self, results=None, rowcount=1):
        self.executed = []
        self._results = list(results or [])
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.executed.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return self._results.pop(0) if self._results else None

    def fetchall(self):
        return self._results.pop(0) if self._results else []

    def sql_calls(self):
        return [sql for sql, _ in self.executed]


class _RecordingExecuteValues:
    def __init__(self):
        self.calls = []

    def __call__(self, cur, sql, argslist, page_size=None, fetch=False):
        self.calls.append({"sql": " ".join(str(sql).split()), "rows": list(argslist)})


def _fake_db_cursor(cursor):
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    return ctx


def _identity_query_calls(cursor):
    return [sql for sql in cursor.sql_calls() if sql.startswith("DELETE FROM earnings WHERE id")]


# ── root cause: the period's own stale prediction must not hold its date ────

class StalePredictionClearedTests(TestCase):
    """The provider's date wins: the stale prediction is removed, the row moves."""

    def _reschedule(self, results):
        cursor = _FakeCursor(results=results)
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder):
            outcome = fiscal.reschedule_confirmed_rows(
                cursor,
                [_futu_row("1888.HK", "HK", "2026-08-24", 2026, 2)],
                value_fields=(), takeover=False, align_report_type=True,
            )
        return outcome, cursor, recorder

    def test_the_period_reclaims_the_provider_date(self):
        outcome, cursor, _ = self._reschedule([[_confirmed_1888()], _stale_prediction_1888()])

        # 1) the stale prediction was deleted, scoped to the period's own identity
        deletes = [call for call in cursor.executed if call[0].startswith("DELETE FROM earnings WHERE id")]
        self.assertEqual(len(deletes), 1)
        self.assertIn("is_predicted = TRUE", deletes[0][0])
        self.assertEqual(deletes[0][1], (324155, "1888.HK", "HK", 2026, 2))

        # 2) the confirmed row — not the prediction — was moved onto the provider date
        updates = [call for call in cursor.executed if "UPDATE earnings SET report_date" in call[0]]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][1], (date(2026, 8, 24), 159143, date(2026, 7, 15)))
        self.assertEqual([move["id"] for move in outcome.moves], [159143])

        # 3) no #52 skip is left behind: the natural key is free when the move runs
        self.assertEqual(outcome.skipped, [])
        self.assertEqual(len(outcome.predictions_cleared), 1)
        cleared = outcome.predictions_cleared[0]
        self.assertEqual((cleared["id"], cleared["holder"]["id"]), (159143, 324155))
        self.assertEqual((cleared["from"], cleared["to"]), ("2026-07-15", "2026-08-24"))
        self.assertEqual(cleared["reason"], "stale_prediction")

    def test_the_delete_runs_before_the_move(self):
        """Order matters: the move needs the key the delete frees."""
        _, cursor, _ = self._reschedule([[_confirmed_1888()], _stale_prediction_1888()])
        statements = cursor.sql_calls()
        delete_at = next(i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM earnings WHERE id"))
        move_at = next(i for i, sql in enumerate(statements) if "UPDATE earnings SET report_date" in sql)
        self.assertLess(delete_at, move_at)

    def test_a_foreign_periods_prediction_is_left_alone(self):
        """Only the period's *own* prediction may be removed (Issue #54 lesson)."""
        outcome, cursor, _ = self._reschedule([
            [_confirmed_1888()],
            {"id": 370, "fiscal_year": 2027, "fiscal_quarter": 2, "is_predicted": True},
        ])
        self.assertEqual(_identity_query_calls(cursor), [])
        updates = [call for call in cursor.executed if "UPDATE earnings SET report_date" in call[0]]
        self.assertEqual(updates, [])
        self.assertEqual([skip["reason"] for skip in outcome.skipped], ["target_occupied"])
        self.assertEqual(outcome.predictions_cleared, [])

    def test_a_holder_that_is_no_longer_a_prediction_keeps_the_skip(self):
        """A concurrent writer wins the race; the delete is a no-op, not a blind move."""
        cursor = _FakeCursor(results=[[_confirmed_1888()], _stale_prediction_1888()], rowcount=0)
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder):
            outcome = fiscal.reschedule_confirmed_rows(
                cursor, [_futu_row("1888.HK", "HK", "2026-08-24", 2026, 2)],
                value_fields=(), takeover=False, align_report_type=True)
        self.assertEqual(outcome.predictions_cleared, [])
        self.assertEqual([skip["reason"] for skip in outcome.skipped], ["target_occupied"])
        self.assertEqual([sql for sql in cursor.sql_calls() if "UPDATE earnings SET report_date" in sql], [])


class FlushDateBatchIncidentTests(TestCase):
    """The incident's reproduction, through the real ``flush_date_batch``."""

    def test_the_batch_is_written_and_the_period_ends_up_on_the_provider_date(self):
        cursor = _FakeCursor(results=[[_confirmed_1888()], _stale_prediction_1888()])
        recorder = _RecordingExecuteValues()
        stats = sync_futu.FutuStageStats()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_futu, "db_cursor", return_value=_fake_db_cursor(cursor)):
            written = sync_futu.flush_date_batch([
                _futu_row("1888.HK", "HK", "2026-08-24", 2026, 2),
                _futu_row("1888.HK", "HK", "2025-03-16", 2025, 4),
            ], stats)

        self.assertEqual(written, 2)
        self.assertEqual(stats.skipped_rows, 0)
        self.assertEqual(stats.cleared_predictions, 1)
        upsert = [call for call in recorder.calls if call["sql"].startswith("INSERT INTO earnings")][0]
        # The whole batch survives: the incident's failure lost all of it.
        self.assertEqual([row[3] for row in upsert["rows"]], ["2026-08-24", "2025-03-16"])
        moves = [call for call in cursor.executed if "UPDATE earnings SET report_date" in call[0]]
        self.assertEqual(moves[0][1][1], 159143)

    def test_the_upsert_cannot_promote_a_row_of_another_fiscal_period(self):
        """``is_predicted = FALSE`` is gone from both provider write paths."""
        self.assertIn(fiscal.CONFIRM_IDENTITY_PROMOTION, sync_futu._DATE_UPSERT_SQL)
        self.assertNotIn("is_predicted = FALSE", sync_futu._DATE_UPSERT_SQL)

        cursor = _FakeCursor(results=[[]])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_earnings, "db_cursor", return_value=_fake_db_cursor(cursor)):
            sync_earnings.flush_batch(cursor, [
                _lb_row("1888.HK", "HK", "ALUMINIUM CORP", "2026-08-24", 2026, 2),
            ])
        insert = [call["sql"] for call in recorder.calls
                  if call["sql"].startswith("INSERT INTO earnings")][0]
        self.assertIn(fiscal.CONFIRM_IDENTITY_PROMOTION, insert)
        self.assertNotIn("is_predicted = FALSE", insert)
        # The guard is an identity comparison, not a blanket refusal to confirm:
        # a same-period provider row must still be able to take over a prediction.
        self.assertIn("earnings.fiscal_quarter IS NOT DISTINCT FROM EXCLUDED.fiscal_quarter", insert)


# ── blast radius: one conflict costs its rows, not the batch or the stage ───

class DateBatchIsolationTests(TestCase):
    """Issue #81 item 3: a single unmovable row must not abort the date batch."""

    def _flush(self, batch, failing_symbol="BAD.HK", failing_date="2026-08-01"):
        cursor = _FakeCursor(results=[[]])
        written_rows = []

        def fake_execute_values(cur, sql, argslist, page_size=None, fetch=False):
            rows = list(argslist)
            if sql.startswith("INSERT INTO earnings"):
                if len(rows) > 1 and rows[0][0] == failing_symbol:
                    raise pg_errors.UniqueViolation("duplicate key value ... idx_earnings_fiscal_identity")
                for row in rows:
                    if row[0] == failing_symbol and row[3] == failing_date:
                        raise pg_errors.UniqueViolation("duplicate key value ... idx_earnings_fiscal_identity")
                written_rows.extend(rows)
            return None

        stats = sync_futu.FutuStageStats()
        with mock.patch("psycopg2.extras.execute_values", side_effect=fake_execute_values), \
             mock.patch.object(sync_futu, "db_cursor", return_value=_fake_db_cursor(cursor)):
            written = sync_futu.flush_date_batch(batch, stats)
        return written, stats, cursor, written_rows

    def test_one_conflicting_row_costs_only_that_row(self):
        written, stats, cursor, written_rows = self._flush([
            _futu_row("BAD.HK", "HK", "2026-08-01", 2026, 1),
            _futu_row("BAD.HK", "HK", "2026-05-01", 2026, 2),
            _futu_row("GOOD.HK", "HK", "2026-08-20", 2026, 2),
            _futu_row("GOOD.HK", "HK", "2026-11-20", 2026, 3),
        ])
        # The clean symbol is written by its own statement, the rejected symbol's
        # other row by the per-row retry, and exactly one row is lost.
        self.assertEqual(written, 3)
        self.assertEqual(stats.skipped_rows, 1)
        self.assertEqual(sorted(row[0] for row in written_rows if row[0] == "GOOD.HK"),
                         ["GOOD.HK", "GOOD.HK"])
        statements = cursor.sql_calls()
        self.assertIn(f"SAVEPOINT {sync_futu._DATE_SYMBOL_SAVEPOINT}", statements)
        self.assertIn(f"ROLLBACK TO SAVEPOINT {sync_futu._DATE_SYMBOL_SAVEPOINT}", statements)
        self.assertIn(f"SAVEPOINT {sync_futu._DATE_ROW_SAVEPOINT}", statements)
        self.assertIn(f"ROLLBACK TO SAVEPOINT {sync_futu._DATE_ROW_SAVEPOINT}", statements)

    def test_a_non_conflict_error_is_not_turned_into_a_quiet_skip(self):
        """A broken connection must fail the stage, not look like a data conflict."""
        cursor = _FakeCursor(results=[[]])

        def fake_execute_values(cur, sql, argslist, page_size=None, fetch=False):
            if sql.startswith("INSERT INTO earnings"):
                raise pg_errors.OperationalError("server closed the connection unexpectedly")

        with mock.patch("psycopg2.extras.execute_values", side_effect=fake_execute_values), \
             mock.patch.object(sync_futu, "db_cursor", return_value=_fake_db_cursor(cursor)):
            with self.assertRaises(pg_errors.OperationalError):
                sync_futu.flush_date_batch([_futu_row("AAPL", "US", "2026-08-01", 2026, 3)])

    def test_the_details_carry_the_skip_and_clear_counters(self):
        details = sync_futu.futu_audit_details(
            sync_futu.FutuStageStats(total=188, written_rows=187, skipped_rows=1,
                                     cleared_predictions=2, reschedule_skipped=3),
            sync_futu.FutuStageStats(total=137), [],
        )
        self.assertEqual(details["date_rows_written"], 187)
        self.assertEqual(details["date_rows_skipped"], 1)
        self.assertEqual(details["date_predictions_cleared"], 2)
        self.assertEqual(details["date_reschedule_skipped"], 3)
        self.assertEqual(details["date_symbols"], 188)      # historical key kept
        self.assertEqual(details["actual_symbols"], 137)


class RunAccountingTests(TestCase):
    """Issue #81 item 4: the run's details must describe the run."""

    def _run_sync(self, *, dates, actuals, finished):
        ctx = mock.MagicMock()
        src = mock.MagicMock()
        src.get_futu_symbols_with_skipped.return_value = (["AAPL.US"], [])
        calls = {"actuals": 0}

        def dates_stage(ctx_, run_id, symbols, stats=None):
            return dates(stats)

        def actuals_stage(ctx_, run_id, symbols, stats=None):
            calls["actuals"] += 1
            return actuals(stats)

        def fake_finish(run_id, **kwargs):
            finished.append(kwargs)
            return True

        with mock.patch.object(sync_futu, "get_source", return_value=src), \
             mock.patch("app.sync_audit.start_run", return_value=42), \
             mock.patch("app.sync_audit.finish_run", side_effect=fake_finish), \
             mock.patch("app.sync_audit.heartbeat"), \
             mock.patch.object(sync_futu, "sync_earnings_dates", side_effect=dates_stage), \
             mock.patch.object(sync_futu, "sync_actuals", side_effect=actuals_stage):
            result = sync_futu.run_sync(ctx)
        return result, calls

    def test_a_batch_conflict_does_not_stop_the_actuals_stage(self):
        """Criterion 3: the conflict is a counted skip and the run stays green."""
        def dates(stats):
            stats.total = 188
            stats.written_rows = 187
            stats.skipped_rows = 1
            stats.cleared_predictions = 1
            return stats

        def actuals(stats):
            stats.total = 137
            return stats

        finished = []
        result, calls = self._run_sync(dates=dates, actuals=actuals, finished=finished)

        self.assertEqual(result, 42)
        self.assertEqual(calls["actuals"], 1)
        self.assertEqual([entry["status"] for entry in finished], ["success"])
        details = finished[0]["details"]
        self.assertEqual(details["date_rows_skipped"], 1)
        self.assertEqual(details["date_predictions_cleared"], 1)

    def test_a_stage_that_dies_still_reports_what_it_counted(self):
        """The incident recorded ``details={}`` and ``record_count=0``."""
        def dates(stats):
            stats.total = 188
            raise RuntimeError("futu stage exploded")

        def actuals(stats):  # pragma: no cover - never reached
            return stats

        finished = []
        with self.assertRaises(RuntimeError):
            self._run_sync(dates=dates, actuals=actuals, finished=finished)

        self.assertEqual(finished[0]["status"], "failed")
        self.assertEqual(finished[0]["error_code"], "futu_sync_failed")
        self.assertEqual(finished[0]["record_count"], 188)
        self.assertEqual(finished[0]["details"]["failure_stage"], "dates")
        self.assertEqual(finished[0]["details"]["date_symbols"], 188)
