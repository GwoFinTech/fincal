"""Issue #50: one fiscal period is one earnings row, everywhere.

``earnings`` is keyed by ``(symbol, market, report_date, report_type)`` — a
display date that changes whenever a provider reschedules an event — while the
persistent identity of an event is ``(symbol, market, fiscal_year,
fiscal_quarter)``.  Production held 439 confirmed duplicate groups (913 rows):
the calendar, the CSV/JSON export and the API rendered one period twice with
contradictory actuals, iCal silently collapsed it (Issue #40) so the three
outlets disagreed, and ``phase3`` derived growth from whichever duplicate came
last in ``ORDER BY report_date``.

Covered here:

* the identity/authority rules and the read-path collapse (API + export + iCal +
  derived metrics all pick the same row);
* the write-path guards that stop new duplicates (Longbridge and Futu batches,
  prediction upserts);
* the reconciliation plan (``scripts/reconcile_fiscal_rows.py``) and the guarded
  fiscal-identity unique index in ``app/db.py``.

Issue #52 coverage (same module): the write-path guard itself crashed in
production.  ``reschedule_confirmed_rows`` pre-checked only the fiscal period,
while the table is unique on ``(symbol, market, report_date, report_type)``, so
re-dating a quarter onto a date another quarter already owns raised
``UniqueViolation`` and aborted the whole Longbridge run after ~10.8k of ~17.8k
rows.  The tests below pin the natural-key pre-check, the savepoint fallback,
the batch-level isolation, the truthful run counters and the rule that a
provider candidate may never rewrite an existing row's fiscal identity.
"""
import importlib.util
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, fiscal  # noqa: E402
from app.earnings import fetch_earnings_from_db  # noqa: E402
from app.ical import generate_ical  # noqa: E402
from app.phase3 import build_decision_metrics  # noqa: E402


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: dataclasses (PEP 563 annotations) look the module
    # up in sys.modules while processing the class body.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync_earnings = _load("sync_earnings_issue50", "scripts/sync_earnings.py")
sync_futu = _load("sync_futu_issue50", "scripts/sync_futu.py")
predict_earnings = _load("predict_earnings_issue50", "scripts/predict_earnings.py")
reconcile = _load("reconcile_fiscal_rows", "scripts/reconcile_fiscal_rows.py")


# ── fixtures ────────────────────────────────────────────────────────────────

def _ts(day: int) -> datetime:
    return datetime(2026, 9, day, tzinfo=timezone.utc)


def _uuuu_rows():
    """UUUU FY2026 Q2 exactly as reported in the Issue (two contradictory rows)."""
    return [
        {
            "id": 15346, "symbol": "UUUU", "market": "US", "fiscal_year": 2026,
            "fiscal_quarter": 2, "report_date": date(2026, 8, 6), "report_type": "Q",
            "is_predicted": False, "date_source": "futu", "date_status": "reported",
            "actual_source": "futu", "eps_actual": 27.898188, "eps_estimate": -0.04333,
            "revenue_actual": None, "revenue_estimate": None, "updated_at": _ts(1),
        },
        {
            "id": 98493, "symbol": "UUUU", "market": "US", "fiscal_year": 2026,
            "fiscal_quarter": 2, "report_date": date(2026, 8, 5), "report_type": "Q",
            "is_predicted": False, "date_source": "longbridge", "date_status": "reported",
            "actual_source": "longbridge", "eps_actual": -0.13, "eps_estimate": -0.04333,
            "revenue_actual": None, "revenue_estimate": None, "updated_at": _ts(2),
        },
    ]


def _hk_reported_plus_scheduled():
    """2600.HK FY2026 Q2: a reported row with an actual next to a scheduled one."""
    return [
        {
            "id": 13923, "symbol": "2600.HK", "market": "HK", "fiscal_year": 2026,
            "fiscal_quarter": 2, "report_date": date(2026, 8, 28), "report_type": "Q",
            "is_predicted": False, "date_source": "longbridge", "date_status": "scheduled",
            "eps_actual": None, "revenue_actual": None, "eps_estimate": 0.764756,
            "revenue_estimate": None, "updated_at": _ts(5),
        },
        {
            "id": 141987, "symbol": "2600.HK", "market": "HK", "fiscal_year": 2026,
            "fiscal_quarter": 2, "report_date": date(2026, 8, 27), "report_type": "Q",
            "is_predicted": False, "date_source": "futu", "date_status": "reported",
            "eps_actual": 0.42703, "revenue_actual": None, "eps_estimate": 0.415489,
            "revenue_estimate": None, "updated_at": _ts(3),
        },
    ]


class _FakeCursor:
    """Records every statement and replays canned result sets in order."""

    def __init__(self, results=None, raise_on=None):
        self.executed = []
        self._results = list(results or [])
        self._raise_on = raise_on or {}
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((" ".join(str(sql).split()), params))
        for marker, exc in self._raise_on.items():
            if marker in str(sql):
                raise exc

    def fetchone(self):
        return self._results.pop(0) if self._results else None

    def fetchall(self):
        return self._results.pop(0) if self._results else []

    def sql_calls(self):
        return [sql for sql, _ in self.executed]

    def executed_values_calls(self):
        return [call for call in self.executed if "VALUES" in call[0]]


class _RecordingExecuteValues:
    """Stand-in for psycopg2.extras.execute_values."""

    def __init__(self):
        self.calls = []

    def __call__(self, cur, sql, argslist, page_size=None, fetch=False):
        self.calls.append({"sql": " ".join(str(sql).split()), "rows": list(argslist)})


def _fake_db_cursor(cursor):
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    return ctx


# ── identity + authority ────────────────────────────────────────────────────

class FiscalIdentityTests(TestCase):
    def test_fiscal_key_requires_symbol_market_year_and_quarter(self):
        self.assertIsNone(fiscal.fiscal_key({"symbol": "AAPL", "market": "US"}))
        self.assertIsNone(fiscal.fiscal_key({"symbol": "AAPL", "market": "US", "fiscal_year": None, "fiscal_quarter": 2}))
        self.assertEqual(
            fiscal.fiscal_key({"symbol": "UUUU", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 2}),
            ("UUUU", "US", 2026, 2),
        )

    def test_collapse_keeps_one_row_per_fiscal_period(self):
        collapsed = fiscal.collapse_fiscal_duplicates(_uuuu_rows())
        self.assertEqual(len(collapsed), 1)
        # newest report date wins among two confirmed rows that both carry actuals
        self.assertEqual(collapsed[0]["id"], 15346)

    def test_authority_prefers_the_row_that_carries_actuals(self):
        """A scheduled row must not hide the period's real, reported actual."""
        collapsed = fiscal.collapse_fiscal_duplicates(_hk_reported_plus_scheduled())
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0]["id"], 141987)
        self.assertEqual(float(collapsed[0]["eps_actual"]), 0.42703)

    def test_authority_prefers_confirmed_over_predicted(self):
        rows = [
            {"id": 2, "symbol": "AMD", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 3,
             "report_date": date(2026, 10, 30), "is_predicted": True, "eps_actual": None,
             "updated_at": _ts(9)},
            {"id": 1, "symbol": "AMD", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 3,
             "report_date": date(2026, 10, 28), "is_predicted": False, "eps_actual": None,
             "updated_at": _ts(1)},
        ]
        collapsed = fiscal.collapse_fiscal_duplicates(rows)
        self.assertEqual([row["id"] for row in collapsed], [1])

    def test_tie_break_is_deterministic_on_identical_timestamps(self):
        rows = [
            {"id": 7, "symbol": "INTC", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 1,
             "report_date": date(2026, 4, 23), "is_predicted": False, "eps_actual": 1, "updated_at": _ts(1)},
            {"id": 9, "symbol": "INTC", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 1,
             "report_date": date(2026, 4, 23), "is_predicted": False, "eps_actual": 2, "updated_at": _ts(1)},
        ]
        self.assertEqual(fiscal.collapse_fiscal_duplicates(rows)[0]["id"], 9)

    def test_rows_without_a_fiscal_period_pass_through_untouched(self):
        rows = [
            {"id": 1, "symbol": "NVDA", "market": "US", "report_date": date(2026, 8, 26)},
            {"id": 2, "symbol": "NVDA", "market": "US", "report_date": date(2026, 8, 27)},
        ]
        self.assertEqual([row["id"] for row in fiscal.collapse_fiscal_duplicates(rows)], [1, 2])

    def test_output_is_sorted_by_report_date_market_symbol(self):
        rows = _uuuu_rows() + [{
            "id": 3, "symbol": "AAPL", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 3,
            "report_date": date(2026, 7, 30), "is_predicted": False, "eps_actual": 1,
        }]
        collapsed = fiscal.collapse_fiscal_duplicates(rows)
        self.assertEqual([row["id"] for row in collapsed], [3, 15346])


# ── read paths ──────────────────────────────────────────────────────────────

class ReadPathTests(TestCase):
    def test_fetch_earnings_from_db_collapses_duplicate_periods(self):
        cursor = _FakeCursor(results=[_uuuu_rows() + [{
            "id": 1, "symbol": "AAPL", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 3,
            "report_date": date(2026, 7, 30), "is_predicted": False, "eps_actual": 1,
        }]])
        with mock.patch("app.db.db_cursor", return_value=_fake_db_cursor(cursor)):
            rows = fetch_earnings_from_db(symbols=["UUUU", "AAPL"], markets=["US"],
                                          start=date(2026, 1, 1), end=date(2026, 12, 31))
        self.assertEqual([row["id"] for row in rows], [1, 15346])

    def test_api_and_ical_show_the_same_row_for_a_period(self):
        rows = _hk_reported_plus_scheduled()
        collapsed = fiscal.collapse_fiscal_duplicates(rows)
        ics = generate_ical(rows)
        self.assertEqual(collapsed[0]["id"], 141987)
        self.assertEqual(ics.count("BEGIN:VEVENT"), 1)   # iCal still emits one event
        # The single event is the surviving row: its date, its reported actual.
        self.assertIn("DTSTART;VALUE=DATE:20260827", ics)
        self.assertIn("EPS Actual: 0.42703", ics.replace("\r\n ", ""))

    def test_growth_uses_the_row_the_user_opened(self):
        """The panel's actual and its growth must come from the same row."""
        rows = [
            {"id": 10, "symbol": "UUUU", "market": "US", "fiscal_year": 2025, "fiscal_quarter": 2,
             "report_date": date(2025, 8, 6), "eps_actual": 1, "eps_estimate": 1, "revenue_actual": None},
            {"id": 11, "symbol": "UUUU", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 2,
             "report_date": date(2026, 8, 5), "eps_actual": -0.13, "eps_estimate": -0.04,
             "revenue_actual": None},
            {"id": 12, "symbol": "UUUU", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 2,
             "report_date": date(2026, 8, 6), "eps_actual": 27.898188, "eps_estimate": -0.04,
             "revenue_actual": None},
        ]
        # The user opened row 12 (the one the API shows for the period).
        metrics = build_decision_metrics(rows, earning_id=12)
        self.assertEqual(metrics["actual_growth"]["eps_yoy"], Decimal("27.898188") - Decimal("1"))
        # A duplicated period must not be counted twice in the streak.
        self.assertEqual(metrics["beat_miss_streak"]["kind"], "beat")
        self.assertEqual(metrics["beat_miss_streak"]["count"], 1)


# ── write paths ─────────────────────────────────────────────────────────────

class WritePathTests(TestCase):
    def test_reschedule_updates_the_period_row_instead_of_inserting_a_twin(self):
        cursor = _FakeCursor(results=[[{
            "id": 15247, "symbol": "MARA", "market": "US", "fiscal_year": 2025, "fiscal_quarter": 4,
            "report_date": date(2026, 2, 26), "report_type": "Q", "is_predicted": False,
            "eps_actual": 1.272849, "revenue_actual": None, "updated_at": _ts(1),
        }]])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_earnings, "db_cursor", return_value=_fake_db_cursor(cursor)):
            sync_earnings.flush_batch(cursor, [
                lb_batch_row("MARA", "US", "", "2026-03-05", 2025, 4, before_after="after"),
            ])
        updates = [call for call in cursor.executed if "UPDATE earnings SET report_date" in call[0]]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][1], (date(2026, 3, 5), 15247, date(2026, 2, 26)))

    def test_reschedule_skips_when_another_row_already_holds_the_new_date(self):
        cursor = _FakeCursor(results=[[
            {"id": 20, "symbol": "MARA", "market": "US", "fiscal_year": 2025, "fiscal_quarter": 4,
             "report_date": date(2026, 2, 26), "report_type": "Q", "is_predicted": False,
             "eps_actual": None, "revenue_actual": None, "updated_at": _ts(1)},
            {"id": 21, "symbol": "MARA", "market": "US", "fiscal_year": 2025, "fiscal_quarter": 4,
             "report_date": date(2026, 3, 5), "report_type": "Q", "is_predicted": False,
             "eps_actual": None, "revenue_actual": None, "updated_at": _ts(2)},
        ], {"id": 21, "fiscal_year": 2025, "fiscal_quarter": 4, "is_predicted": False}])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_earnings, "db_cursor", return_value=_fake_db_cursor(cursor)):
            sync_earnings.flush_batch(cursor, [
                lb_batch_row("MARA", "US", "", "2026-03-05", 2025, 4, before_after="after"),
            ])
        self.assertEqual([call for call in cursor.executed if "UPDATE earnings SET report_date" in call[0]], [])

    def test_reschedule_never_moves_a_predicted_row(self):
        """A prediction is for the coming period; only confirmed rows carry the identity."""
        cursor = _FakeCursor(results=[[]])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_earnings, "db_cursor", return_value=_fake_db_cursor(cursor)):
            sync_earnings.flush_batch(cursor, [
                lb_batch_row("AMD", "US", "", "2026-10-30", 2026, 3, before_after="after"),
            ])
        self.assertEqual([call for call in cursor.executed if "UPDATE earnings SET report_date" in call[0]], [])
        insert = [call for call in recorder.calls if "INSERT INTO earnings " in call["sql"]]
        self.assertEqual(len(insert), 1)

    def test_longbridge_batch_keeps_one_row_per_period_in_a_single_response(self):
        cursor = _FakeCursor(results=[[]])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_earnings, "db_cursor", return_value=_fake_db_cursor(cursor)):
            sync_earnings.flush_batch(cursor, [
                lb_batch_row("2880.HK", "HK", "", "2026-08-27", 2026, 4),
                lb_batch_row("2880.HK", "HK", "", "2026-08-31", 2026, 4),
            ])
        insert = [call for call in recorder.calls if "INSERT INTO earnings " in call["sql"]][0]
        self.assertEqual([row[3] for row in insert["rows"]], ["2026-08-31"])

    def test_futu_date_batch_applies_the_same_guards(self):
        cursor = _FakeCursor(results=[[{
            "id": 500, "symbol": "0700.HK", "market": "HK", "fiscal_year": 2026, "fiscal_quarter": 3,
            "report_date": date(2026, 11, 12), "report_type": "Q", "is_predicted": False,
            "eps_actual": None, "revenue_actual": None, "updated_at": _ts(1),
        }]])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_futu, "db_cursor", return_value=_fake_db_cursor(cursor)):
            written = sync_futu.flush_date_batch([
                ("0700.HK", "HK", "", "2026-11-12", "Q", 2026, 3, "after", "futu", "scheduled"),
                ("0700.HK", "HK", "", "2026-11-13", "Q", 2026, 3, "after", "futu", "scheduled"),
            ])
        self.assertEqual(written, 1)
        updates = [call for call in cursor.executed if "UPDATE earnings SET report_date" in call[0]]
        self.assertEqual(updates[0][1], (date(2026, 11, 13), 500, date(2026, 11, 12)))
        insert = [call for call in recorder.calls if "INSERT INTO earnings " in call["sql"]][0]
        self.assertEqual([row[3] for row in insert["rows"]], ["2026-11-13"])

    def test_prediction_upsert_does_not_downgrade_a_confirmed_row(self):
        """Issue #50: predicting a date must not relabel provider-confirmed data."""
        self.assertTrue(hasattr(predict_earnings, "predict_for_symbol"))
        source = (ROOT / "scripts" / "predict_earnings.py").read_text()
        # The conflict branch only marks a row predicted while it is still
        # algorithm-owned; a provider row at the same date keeps its provenance.
        self.assertIn(
            "CASE WHEN earnings.date_source = 'algorithm' THEN TRUE ELSE earnings.is_predicted END",
            source,
        )
        self.assertNotIn("is_predicted = TRUE,\n                    date_source = 'algorithm'", source)


# ── Issue #52: a reschedule may not collide with the display key ────────────

def _reschedule(cursor, rows):
    """Run the reschedule guard against a fake cursor (``execute_values`` stubbed)."""
    recorder = _RecordingExecuteValues()
    with mock.patch("psycopg2.extras.execute_values", recorder):
        return fiscal.reschedule_confirmed_rows(cursor, rows)


def _date_updates(cursor):
    return [call for call in cursor.executed if "UPDATE earnings SET report_date" in call[0]]


def lb_batch_row(symbol, market, company_name, report_date, fiscal_year, fiscal_quarter,
                 eps_estimate=None, eps_actual=None, revenue_estimate=None,
                 revenue_actual=None, before_after=None, estimate_currency=None,
                 actual_currency=None):
    """One Longbridge batch tuple in the layout ``flush_batch`` expects.

    Issue #61 added the four attribution fields to the batch (the calendar's
    declared currency plus the explicit ``unknown`` basis), so the tuple width has
    to match the INSERT column list exactly — see
    ``BatchColumnContractTests`` for the assertion that keeps the two in step.
    """
    return (symbol, market, company_name, report_date, "Q", fiscal_year, fiscal_quarter,
            eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after,
            estimate_currency, actual_currency, "unknown", "unknown")


#: The production collision: Longbridge reports QBIEY FY2026 Q1 on 2026-08-13,
#: a date already held by that symbol's FY2026 Q2 row (sync_runs id=66).
_QBIEY_CANDIDATE = lb_batch_row("QBIEY", "US", "QBIEY", "2026-08-13", 2026, 1,
                                before_after="after")


def _qbiey_q1_row():
    return {"id": 26494, "symbol": "QBIEY", "market": "US", "fiscal_year": 2026,
            "fiscal_quarter": 1, "report_date": date(2026, 5, 7), "report_type": "Q",
            "is_predicted": False, "eps_actual": None, "revenue_actual": None,
            "updated_at": _ts(1)}


class RescheduleConflictTests(TestCase):
    """The pre-check must cover the key the table is actually unique on."""

    def test_another_fiscal_quarter_holding_the_date_skips_the_move(self):
        """Issue #52: the fiscal group says "free", the unique index says no."""
        cursor = _FakeCursor(results=[
            [_qbiey_q1_row()],
            {"id": 32173, "fiscal_year": 2026, "fiscal_quarter": 2, "is_predicted": False},
        ])
        outcome = _reschedule(cursor, [_QBIEY_CANDIDATE])
        self.assertEqual(_date_updates(cursor), [])          # no UPDATE, no UniqueViolation
        self.assertEqual(outcome.moves, [])
        self.assertEqual(len(outcome.skipped), 1)
        skip = outcome.skipped[0]
        self.assertEqual(skip["reason"], "target_occupied")
        self.assertEqual((skip["symbol"], skip["from"], skip["to"]),
                         ("QBIEY", "2026-05-07", "2026-08-13"))
        self.assertEqual(skip["holder"]["id"], 32173)
        self.assertEqual(skip["holder"]["fiscal_quarter"], 2)

    def test_predicted_row_holding_the_date_skips_the_move(self):
        """A prediction owns the same unique key, so it blocks a move too."""
        cursor = _FakeCursor(results=[
            [dict(_qbiey_q1_row(), id=100447, fiscal_year=2027, fiscal_quarter=4,
                  report_date=date(2027, 2, 18))],
            {"id": 370, "fiscal_year": 2027, "fiscal_quarter": 1, "is_predicted": True},
        ])
        outcome = _reschedule(cursor, [
            lb_batch_row("QBIEY", "US", "QBIEY", "2026-08-13", 2027, 4, before_after="after"),
        ])
        self.assertEqual(_date_updates(cursor), [])
        self.assertEqual(outcome.moves, [])
        self.assertTrue(outcome.skipped[0]["holder"]["is_predicted"])

    def test_a_conflict_slipping_past_the_check_is_contained_by_the_savepoint(self):
        """The savepoint turns a UniqueViolation into one skipped period."""
        from psycopg2 import errors
        cursor = _FakeCursor(
            results=[[_qbiey_q1_row()]],
            raise_on={"UPDATE earnings SET report_date": errors.UniqueViolation("duplicate key")},
        )
        outcome = _reschedule(cursor, [_QBIEY_CANDIDATE])   # must not raise
        self.assertEqual(outcome.moves, [])
        self.assertEqual([skip["reason"] for skip in outcome.skipped], ["unique_violation"])
        statements = cursor.sql_calls()
        self.assertIn(f"SAVEPOINT {fiscal._RESCHEDULE_SAVEPOINT}", statements)
        self.assertIn(f"ROLLBACK TO SAVEPOINT {fiscal._RESCHEDULE_SAVEPOINT}", statements)
        self.assertIn(f"RELEASE SAVEPOINT {fiscal._RESCHEDULE_SAVEPOINT}", statements)

    def test_a_free_date_is_still_moved_inside_a_savepoint(self):
        cursor = _FakeCursor(results=[[_qbiey_q1_row()], None])
        outcome = _reschedule(cursor, [_QBIEY_CANDIDATE])
        self.assertEqual(len(_date_updates(cursor)), 1)
        self.assertEqual([move["id"] for move in outcome.moves], [26494])
        self.assertEqual(outcome.skipped, [])
        self.assertIn(f"RELEASE SAVEPOINT {fiscal._RESCHEDULE_SAVEPOINT}", cursor.sql_calls())

    def test_flush_batch_reports_what_it_wrote_and_what_it_skipped(self):
        """The batch keeps writing the rest of its rows after a conflict."""
        cursor = _FakeCursor(results=[
            [_qbiey_q1_row()],
            {"id": 32173, "fiscal_year": 2026, "fiscal_quarter": 2, "is_predicted": False},
        ])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_earnings, "db_cursor", return_value=_fake_db_cursor(cursor)):
            stats = sync_earnings.flush_batch(cursor, [
                _QBIEY_CANDIDATE,
                lb_batch_row("AAPL", "US", "Apple", "2026-10-29", 2026, 4, before_after="after"),
            ])
        self.assertEqual((stats.rows, stats.moves, stats.skipped), (2, 0, 1))
        insert = [call for call in recorder.calls if "INSERT INTO earnings " in call["sql"]][0]
        self.assertEqual([row[0] for row in insert["rows"]], ["QBIEY", "AAPL"])

    def test_longbridge_upsert_never_rewrites_an_existing_fiscal_identity(self):
        """Issue #52: the collision must not relabel the row that holds the date."""
        cursor = _FakeCursor(results=[[], None])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_earnings, "db_cursor", return_value=_fake_db_cursor(cursor)):
            sync_earnings.flush_batch(cursor, [_QBIEY_CANDIDATE])
        sql = [call["sql"] for call in recorder.calls if "INSERT INTO earnings " in call["sql"]][0]
        self.assertIn(
            "fiscal_year = CASE WHEN earnings.fiscal_year IS NULL"
            " THEN EXCLUDED.fiscal_year ELSE earnings.fiscal_year END",
            sql,
        )
        self.assertIn(
            "fiscal_quarter = CASE WHEN earnings.fiscal_quarter IS NULL"
            " THEN EXCLUDED.fiscal_quarter ELSE earnings.fiscal_quarter END",
            sql,
        )
        self.assertNotIn("fiscal_year = EXCLUDED.fiscal_year,", sql)

    def test_futu_upsert_applies_the_same_identity_freeze(self):
        cursor = _FakeCursor(results=[[], None])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_futu, "db_cursor", return_value=_fake_db_cursor(cursor)):
            sync_futu.flush_date_batch([
                ("QBIEY", "US", "", "2026-08-13", "Q", 2026, 1, "after", "futu", "scheduled"),
            ])
        sql = [call["sql"] for call in recorder.calls if "INSERT INTO earnings " in call["sql"]][0]
        self.assertIn("fiscal_year = CASE WHEN earnings.fiscal_year IS NULL", sql)


class RunAccountingTests(TestCase):
    """Issue #52: one bad batch costs 200 records, not the whole run."""

    def _fetch(self, pages):
        """Serve the fixture for the US market only (``sync_earnings`` loops both)."""
        return mock.patch.object(
            sync_earnings, "fetch_calendar",
            side_effect=lambda market, start, end: pages if market == "US" else [],
        )

    def _pages(self, count: int):
        infos = [
            {
                "counter_id": f"ST/US/AAA{i}",
                "date": "2026-08-13",
                "counter_name": f"AAA{i}",
                "date_type": "after",
                "data_kv": [],
                "ext": {"financial_report": {"period": "2", "fiscal_year": "2026"}},
            }
            for i in range(count)
        ]
        return [{"infos": infos}]

    def test_a_failed_batch_is_isolated_and_the_counts_stay_truthful(self):
        pages = self._pages(sync_earnings.BATCH_SIZE * 2)
        calls = []

        def fake_flush(cur, batch):
            calls.append(len(batch))
            if len(calls) == 1:
                raise RuntimeError("unique_violation")
            return sync_earnings.FlushStats(rows=len(batch), moves=1)

        with self._fetch(pages), \
             mock.patch.object(sync_earnings, "check_cancelled"), \
             mock.patch.object(sync_earnings, "flush_batch", side_effect=fake_flush), \
             mock.patch.object(sync_earnings, "db_cursor", return_value=_fake_db_cursor(_FakeCursor())):
            stats = sync_earnings.sync_earnings(42)

        self.assertEqual(calls, [sync_earnings.BATCH_SIZE] * 2)   # the run kept going
        self.assertEqual(stats.fetched, sync_earnings.BATCH_SIZE * 2)
        self.assertEqual(stats.written, sync_earnings.BATCH_SIZE)  # only the good batch
        self.assertEqual(stats.failed_batches, 1)
        self.assertEqual(stats.rescheduled, 1)
        # A partial run is reported as partial, never as a clean success.
        self.assertEqual(stats.quality().to_dict()["status"], "partial")

    def test_a_clean_run_reports_written_equal_to_fetched(self):
        pages = self._pages(sync_earnings.BATCH_SIZE)

        def fake_flush(cur, batch):
            return sync_earnings.FlushStats(rows=len(batch))

        with self._fetch(pages), \
             mock.patch.object(sync_earnings, "check_cancelled"), \
             mock.patch.object(sync_earnings, "flush_batch", side_effect=fake_flush), \
             mock.patch.object(sync_earnings, "db_cursor", return_value=_fake_db_cursor(_FakeCursor())):
            stats = sync_earnings.sync_earnings(42)

        self.assertEqual((stats.fetched, stats.written, stats.failed_batches),
                         (sync_earnings.BATCH_SIZE,) * 2 + (0,))
        self.assertEqual(stats.quality().to_dict()["status"], "success")

    def test_a_run_that_wrote_nothing_is_not_recorded_as_success(self):
        """Isolation must not turn a fully failed run into a green one."""
        self.assertEqual(
            sync_earnings.run_terminal_state(sync_earnings.SyncStats(written=0, failed_batches=3)),
            ("failed", "longbridge_batch_failed"),
        )
        self.assertEqual(
            sync_earnings.run_terminal_state(sync_earnings.SyncStats(written=400, failed_batches=1)),
            ("success", None),
        )
        self.assertEqual(
            sync_earnings.run_terminal_state(sync_earnings.SyncStats()),
            ("success", None),
        )


# ── reconciliation plan ─────────────────────────────────────────────────────

class ReconcilePlanTests(TestCase):
    def test_plan_keeps_the_authority_row_and_counts_snapshots_to_move(self):
        rows = _uuuu_rows()
        snapshots = {98493: [{"source": "longbridge", "captured_at": _ts(1)}]}
        plan = reconcile.plan_group(("UUUU", "US", 2026, 2), rows, snapshots)
        self.assertEqual(plan.survivor["id"], 15346)
        self.assertEqual([row["id"] for row in plan.dropped], [98493])
        self.assertEqual(plan.snapshots_to_move, 1)
        self.assertEqual(plan.snapshot_collision, [])
        self.assertTrue(any("eps_actual" in conflict for conflict in plan.value_conflicts))
        self.assertIn("kept row 15346", plan.reason)

    def test_plan_flags_snapshot_collisions_that_would_lose_history(self):
        rows = _uuuu_rows()
        captured = _ts(1)
        snapshots = {
            15346: [{"source": "futu", "captured_at": captured}],
            98493: [{"source": "futu", "captured_at": captured}],
        }
        plan = reconcile.plan_group(("UUUU", "US", 2026, 2), rows, snapshots)
        self.assertEqual(len(plan.snapshot_collision), 1)

    def test_dry_run_report_summarises_what_would_be_removed(self):
        """The plan is pure: it never issues an UPDATE/DELETE/INSERT."""
        plans = [reconcile.plan_group(("UUUU", "US", 2026, 2), _uuuu_rows(), {}),
                 reconcile.plan_group(("2600.HK", "HK", 2026, 2), _hk_reported_plus_scheduled(), {})]
        report = reconcile.render(plans, duplicates=2, snapshots=76626, orphans=0)
        self.assertIn("rows to delete                    : 2", report)
        self.assertIn("2600.HK", report)
        self.assertIn("earnings_estimate_snapshots total : 76626", report)

    def test_apply_backs_up_before_deleting_and_repoints_snapshots(self):
        cursor = _FakeCursor()
        plan = reconcile.plan_group(("UUUU", "US", 2026, 2), _uuuu_rows(), {})
        reconcile.apply_plan(cursor, plan)
        statements = cursor.sql_calls()
        backup = next(i for i, sql in enumerate(statements) if f"INSERT INTO {reconcile.BACKUP_TABLE}" in sql)
        repoint = next(i for i, sql in enumerate(statements) if "UPDATE earnings_estimate_snapshots" in sql)
        delete = next(i for i, sql in enumerate(statements) if "DELETE FROM earnings" in sql)
        self.assertLess(backup, repoint)
        self.assertLess(repoint, delete)
        self.assertEqual(cursor.executed[delete][1][0], [98493])


# ── guarded fiscal-identity index ───────────────────────────────────────────

class FiscalIdentityIndexTests(TestCase):
    def test_index_is_created_when_no_duplicates_remain(self):
        cursor = _FakeCursor(results=[{"groups": 0}])
        with mock.patch("app.db.db_cursor", return_value=_fake_db_cursor(cursor)):
            created = db.ensure_fiscal_identity_index()
        self.assertTrue(created)
        self.assertTrue(any("CREATE UNIQUE INDEX IF NOT EXISTS idx_earnings_fiscal_identity" in sql
                            for sql in cursor.sql_calls()))

    def test_index_is_skipped_with_a_warning_while_duplicates_exist(self):
        """Startup must not abort on an installation that still has duplicates."""
        cursor = _FakeCursor(results=[{"groups": 439}])
        with mock.patch("app.db.db_cursor", return_value=_fake_db_cursor(cursor)):
            created = db.ensure_fiscal_identity_index()
        self.assertFalse(created)
        self.assertFalse(any("CREATE UNIQUE INDEX" in sql for sql in cursor.sql_calls()))

    def test_a_race_that_breaks_the_build_is_reported_not_raised(self):
        from psycopg2 import errors
        cursor = _FakeCursor(results=[{"groups": 0}],
                             raise_on={"CREATE UNIQUE INDEX": errors.UniqueViolation("duplicate key")})
        with mock.patch("app.db.db_cursor", return_value=_fake_db_cursor(cursor)):
            created = db.ensure_fiscal_identity_index()
        self.assertFalse(created)
