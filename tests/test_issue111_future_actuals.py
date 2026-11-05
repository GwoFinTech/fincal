"""Issue #111: a report that has not happened cannot be "reported".

``earnings.date_status`` answers "has this event been disclosed?", but both write
paths derived it from the presence of an actual alone: ``eps_actual IS NOT NULL
OR revenue_actual IS NOT NULL``.  ``report_date`` came from the provider
untouched, and an actual, once written, was never withdrawn.  The combination
produced rows dated in the future that read ``reported`` — on 2026-11-03,
18 production rows, e.g. PFIS with ``eps_actual=1.90`` against ``1.71`` three
days before its own report date, which the calendar, the CSV/JSON export, the
iCal feed (``STATUS:CONFIRMED`` + ``EPS Actual``) and the front end ("较预期"
+11.1%) all published as fact.

Covered here:

* the stored rule — every write path that can set ``reported`` now requires
  ``report_date <= CURRENT_DATE``;
* the proxy rule — ``predict_earnings.mark_confirmed`` no longer promotes a
  future-dated prediction just because a provider shipped an actual;
* the read rule — one boundary (``fetch_earnings_from_db``) masks an unreleased
  actual, drops the status to ``scheduled``/``predicted`` and reports
  ``report_date_in_future`` as the comparability reason, which switches off
  "较预期" in the API, both exports and the decision panel;
* the feed rule — such an event is ``STATUS:TENTATIVE`` without an ``EPS Actual``
  line, so a subscriber's tentative copy is not overwritten by a fabricated one.
"""
import contextlib
import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sync_earnings = _load("sync_earnings", "scripts/sync_earnings.py")
sync_futu = _load("sync_futu", "scripts/sync_futu.py")
predict_earnings = _load("predict_earnings", "scripts/predict_earnings.py")

from app import db, fiscal, ical  # noqa: E402


FUTURE = date.today() + timedelta(days=5)
PAST = date.today() - timedelta(days=5)


def _row(**overrides) -> dict:
    values = {
        "symbol": "BBAR", "market": "US", "company_name": "BBAR",
        "report_date": FUTURE, "report_type": "Q",
        "fiscal_year": 2026, "fiscal_quarter": 3,
        "eps_estimate": 1.71, "eps_actual": 1.90,
        "revenue_estimate": 847280367.0, "revenue_actual": 1369164371.0,
        "before_after": "after", "is_predicted": False,
        "date_source": "longbridge", "date_status": "reported",
        "estimate_currency": "USD", "actual_currency": "USD",
        "estimate_basis": None, "actual_basis": None,
        "actual_as_of": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "comparison_unavailable_reason": None,
    }
    values.update(overrides)
    return values


# ── the stored status rule ─────────────────────────────────────────────────


class _Cursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []

    def fetchone(self) -> dict | None:
        return None


def _ctx(cursor):
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    return ctx


def _lb_row(**overrides) -> tuple:
    """One Longbridge batch tuple in ``flush_batch``'s column layout."""
    values = dict(symbol="BBAR", market="US", company_name="BBAR",
                  report_date=FUTURE.isoformat(), report_type="Q",
                  fiscal_year=2026, fiscal_quarter=3,
                  eps_estimate=1.71, eps_actual=1.90,
                  revenue_estimate=847280367.0, revenue_actual=1369164371.0,
                  before_after="after", estimate_currency="USD",
                  actual_currency="USD", estimate_basis="unknown",
                  actual_basis="unknown")
    values.update(overrides)
    return tuple(values[column] for column in sync_earnings._BATCH_VALUE_COLUMNS)


class WritePathStatusTests(TestCase):
    """Every statement that can write ``reported`` must look at the date."""

    def test_longbridge_status_update_requires_the_report_date(self):
        cursor = _Cursor()
        recorded = []

        def _execute_values(cur, sql, argslist, page_size=200):
            recorded.append(sql)

        with mock.patch("psycopg2.extras.execute_values", _execute_values), \
                mock.patch.object(sync_earnings, "db_cursor", return_value=_ctx(cursor)):
            sync_earnings.flush_batch(cursor, [_lb_row()])

        status = [sql for sql in recorded if "date_status = CASE" in sql]
        self.assertTrue(status, "the provenance UPDATE was not issued")
        self.assertIn("e.report_date <= CURRENT_DATE", status[0],
                      "a future-dated row with an actual must not read 'reported'")

    def test_futu_actuals_update_requires_the_report_date(self):
        sql = sync_futu._ACTUAL_UPDATE_SQL.format(column="eps_actual")
        self.assertIn("report_date <= CURRENT_DATE", sql)
        self.assertNotIn("date_status = 'reported'", sql)

    def test_futu_date_upsert_requires_the_report_date(self):
        self.assertIn("earnings.report_date <= CURRENT_DATE", sync_futu._DATE_UPSERT_SQL)

    def test_prediction_promotion_requires_the_report_date(self):
        cursor = _Cursor()
        with mock.patch.object(predict_earnings, "db_cursor", return_value=_ctx(cursor)):
            predict_earnings.mark_confirmed()
        promote = [sql for sql, _ in cursor.executed if "date_status = 'reported'" in sql]
        self.assertTrue(promote)
        self.assertIn("report_date <= CURRENT_DATE", promote[0],
                      "a future-dated prediction must stay predicted")


# ── the read rule ──────────────────────────────────────────────────────────


class UnreleasedActualsTests(TestCase):
    def test_report_date_shapes_all_compare(self):
        self.assertTrue(fiscal.report_date_is_future({"report_date": FUTURE}))
        self.assertTrue(fiscal.report_date_is_future(
            {"report_date": datetime.combine(FUTURE, datetime.min.time())}))
        self.assertTrue(fiscal.report_date_is_future({"report_date": FUTURE.isoformat()}))
        self.assertFalse(fiscal.report_date_is_future({"report_date": PAST}))
        self.assertFalse(fiscal.report_date_is_future({"report_date": None}))
        self.assertFalse(fiscal.report_date_is_future(
            {"report_date": PAST}, today=date.today()),
            "a pinned clock must be honoured")

    def test_a_future_actual_is_unreleased(self):
        self.assertTrue(fiscal.has_unreleased_actuals(_row()))
        self.assertFalse(fiscal.has_unreleased_actuals(_row(report_date=PAST)))
        self.assertFalse(fiscal.has_unreleased_actuals(
            _row(eps_actual=None, revenue_actual=None)),
            "a row without an actual has nothing to withhold")

    def test_masking_keeps_the_estimate_and_the_date(self):
        row = _row()
        masked = fiscal.hide_unreleased_actuals([row], date.today())

        self.assertEqual(masked, 1)
        self.assertIsNone(row["eps_actual"])
        self.assertIsNone(row["revenue_actual"])
        self.assertIsNone(row["actual_as_of"])
        self.assertEqual(row["eps_estimate"], 1.71)
        self.assertEqual(row["revenue_estimate"], 847280367.0)
        self.assertEqual(row["report_date"], FUTURE)
        self.assertEqual(row["date_status"], "scheduled")
        self.assertEqual(row["comparison_unavailable_reason"],
                         fiscal.COMPARISON_REPORT_DATE_IN_FUTURE)

    def test_masking_leaves_a_disclosed_row_untouched(self):
        row = _row(report_date=PAST)
        self.assertEqual(fiscal.hide_unreleased_actuals([row], date.today()), 0)
        self.assertEqual(row["eps_actual"], 1.90)
        self.assertEqual(row["date_status"], "reported")
        self.assertIsNone(row["comparison_unavailable_reason"])

    def test_a_predicted_row_keeps_its_own_status(self):
        row = _row(is_predicted=True, date_status="predicted")
        fiscal.hide_unreleased_actuals([row], date.today())
        self.assertEqual(row["date_status"], "predicted")

    def test_the_reason_travels_with_the_masked_row(self):
        row = _row()
        fiscal.hide_unreleased_actuals([row], date.today())
        # A second consumer (the feed) reads the already-masked row: the verdict
        # must survive the empty actuals.
        self.assertTrue(fiscal.has_unreleased_actuals(row))


class _FetchCursor:
    """A cursor that serves one fixed result set to ``fetch_earnings_from_db``."""

    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self.rows)


@contextlib.contextmanager
def _patched_db(rows):
    cursor = _FetchCursor(rows)
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    with mock.patch.object(db, "db_cursor", return_value=ctx):
        yield cursor


class ReadBoundaryTests(TestCase):
    def test_the_single_read_entry_point_withholds_an_unreleased_actual(self):
        from app.earnings import fetch_earnings_from_db

        rows = [_row(read_today=date.today()),
                _row(symbol="PFIS", report_date=PAST, read_today=date.today())]
        with _patched_db(rows):
            served = fetch_earnings_from_db(symbols=["BBAR", "PFIS"])

        by_symbol = {row["symbol"]: row for row in served}
        self.assertIsNone(by_symbol["BBAR"]["eps_actual"])
        self.assertIsNone(by_symbol["BBAR"]["revenue_actual"])
        self.assertEqual(by_symbol["BBAR"]["date_status"], "scheduled")
        self.assertEqual(by_symbol["BBAR"]["comparison_unavailable_reason"],
                         fiscal.COMPARISON_REPORT_DATE_IN_FUTURE)
        # The disclosed row keeps its numbers and answers the attribution question.
        self.assertEqual(by_symbol["PFIS"]["eps_actual"], 1.90)
        self.assertEqual(by_symbol["PFIS"]["date_status"], "reported")
        self.assertIsNone(by_symbol["PFIS"]["comparison_unavailable_reason"])

    def test_the_read_clock_comes_from_the_database(self):
        from app.earnings import fetch_earnings_from_db

        cursor = None
        with _patched_db([_row(read_today=date.today())]) as cursor:
            fetch_earnings_from_db(symbols=["BBAR"])
        self.assertIn("CURRENT_DATE AS read_today", cursor.executed[0][0],
                      "the read rule must share the database's day with the sync")


# ── the feed rule ─────────────────────────────────────────────────────────


class IcalStatusTests(TestCase):
    def _event(self, row):
        ics = ical.generate_ical([row], title_lang="en")
        block = ics.split("BEGIN:VEVENT")[1]
        return block

    def test_a_future_actual_is_not_published_as_confirmed(self):
        block = self._event(_row())
        self.assertIn("STATUS:TENTATIVE", block)
        self.assertNotIn("STATUS:CONFIRMED", block)
        self.assertNotIn("EPS Actual", block)
        self.assertIn("EPS Est", block)

    def test_a_disclosed_row_stays_confirmed(self):
        block = self._event(_row(report_date=PAST))
        self.assertIn("STATUS:CONFIRMED", block)
        self.assertIn("EPS Actual", block)

    def test_a_masked_row_reaches_the_same_verdict(self):
        row = _row()
        fiscal.hide_unreleased_actuals([row], date.today())
        block = self._event(row)
        self.assertIn("STATUS:TENTATIVE", block)
        self.assertNotIn("EPS Actual", block)


# ── the counter that keeps it observable ──────────────────────────────────


class DiagnosticsCounterTests(TestCase):
    def test_admin_diagnostics_reports_the_future_reported_count(self):
        from app.routers import admin

        class _CountCursor(_Cursor):
            def fetchone(self):
                return {"cnt": 18}

        cursor = _CountCursor()
        with mock.patch.object(db, "db_cursor", return_value=_ctx(cursor)), \
                mock.patch("app.freshness.check_freshness",
                           mock.MagicMock(return_value={"status": "healthy"})):
            result = admin.diagnostics({})

        self.assertEqual(result["future_reported"], 18)
        self.assertTrue(any("report_date > CURRENT_DATE" in sql
                            for sql, _ in cursor.executed))
