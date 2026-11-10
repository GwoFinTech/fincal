"""Issue #118: the decision panel reads through the same boundary as the list.

Issue #111 put the rule "an event dated in the future has no disclosed actual"
in one read entry point (``app.earnings.fetch_earnings_from_db``) and the
calendar, both exports and the iCal feed all inherit it.  ``/api/earnings/{id}/decision``
never went through it: it ran its own ``SELECT * FROM earnings`` and handed the
unmasked row to ``phase3.build_decision_metrics``, so the same panel printed
「报告尚未发生」 in the row and 「超预期 4 季 / +185.7% 环比」 underneath it.  On
2026-11-10, 29 of the 30 stored rows that carried an actual for a future report
date produced such a number.

Covered here:

* the metrics rule — an unreleased quarter yields four ``null`` ratios and a
  ``count=0`` streak, each carrying ``report_date_in_future``;
* the entry point — ``fetch_earning_with_history`` masks the opened row *and* the
  history it is compared against, from the database's own day;
* the regression pin — a disclosed row's ``actual_growth`` / ``beat_miss_streak``
  are unchanged, because a masked future row is never a predecessor of it;
* the consistency assertion — every row the list withholds (``comparison_unavailable_reason
  == 'report_date_in_future'``) has a decision payload without any growth value,
  so "the panel cannot contradict itself" is executable.
"""
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, fiscal  # noqa: E402
from app.phase3 import GROWTH_METRICS, build_decision_metrics  # noqa: E402

FUTURE = date.today() + timedelta(days=3)
# Fixed past dates: a disclosed row must stay disclosed whenever the suite runs.
PAST_QUARTER = date(2024, 8, 13)
PAST_YEAR = date(2023, 11, 13)
PAST_LATEST = date(2024, 11, 13)


def _row(**overrides) -> dict:
    """One stored ``earnings`` row, shaped like psycopg returns it."""
    values = {
        "id": 1, "symbol": "0388.HK", "market": "HK", "company_name": "0388.HK",
        "fiscal_year": 2026, "fiscal_quarter": 3, "report_date": FUTURE,
        "report_type": "Q", "is_predicted": False, "date_status": "reported",
        "eps_estimate": 3.0, "eps_actual": 12.1701,
        "revenue_estimate": 1000.0, "revenue_actual": 2000.0,
        "estimate_currency": "HKD", "actual_currency": "HKD",
        "estimate_basis": None, "actual_basis": None,
        "estimate_source": "longbridge", "actual_source": "longbridge",
        "comparison_unavailable_reason": None,
    }
    values.update(overrides)
    return values


def _future_history() -> list[dict]:
    """The panel's row (future) plus the two comparable periods it would use."""
    return [
        _row(id=1, fiscal_year=2024, fiscal_quarter=4, report_date=PAST_YEAR,
             eps_actual=1.0, eps_estimate=1.2, revenue_actual=100.0, revenue_estimate=110.0),
        _row(id=2, fiscal_year=2025, fiscal_quarter=2, report_date=PAST_QUARTER,
             eps_actual=2.0, eps_estimate=1.9, revenue_actual=190.0, revenue_estimate=180.0),
        _row(id=3, fiscal_year=2025, fiscal_quarter=3, report_date=PAST_LATEST,
             eps_actual=2.5, eps_estimate=2.9, revenue_actual=240.0, revenue_estimate=230.0),
        _row(id=4, fiscal_year=2026, fiscal_quarter=3, report_date=FUTURE),
    ]


# ── the metrics rule ──────────────────────────────────────────────────────


class UnreleasedMetricsTests(TestCase):
    ID = 4

    def test_an_unreleased_quarter_states_why_every_ratio_is_missing(self):
        rows = _future_history()
        fiscal.hide_unreleased_actuals(rows, date.today())

        metrics = build_decision_metrics(rows, self.ID)
        growth = metrics["actual_growth"]
        for key, _metric, _span in GROWTH_METRICS:
            self.assertIsNone(growth[key], f"{key} was computed from an unreleased actual")
            self.assertEqual(growth[f"{key}_reason"],
                             fiscal.COMPARISON_REPORT_DATE_IN_FUTURE)

        streak = metrics["beat_miss_streak"]
        self.assertEqual((streak["kind"], streak["count"]), ("unavailable", 0))
        self.assertEqual(streak["reason"], fiscal.COMPARISON_REPORT_DATE_IN_FUTURE)
        self.assertEqual(streak["break_period"],
                         {"fiscal_year": 2026, "fiscal_quarter": 3})

    def test_an_unmasked_future_row_cannot_publish_a_number_either(self):
        """The rule is read from the row, so a caller that skips masking is caught too."""
        metrics = build_decision_metrics(_future_history(), self.ID)
        growth = metrics["actual_growth"]
        self.assertTrue(all(growth[key] is None for key, _m, _s in GROWTH_METRICS))
        self.assertEqual(metrics["beat_miss_streak"]["count"], 0)

    def test_the_numbers_were_reachable_before_the_rule(self):
        """Control: the same history at a disclosed date does produce a ratio."""
        rows = [dict(row, report_date=PAST_LATEST) for row in _future_history()]
        metrics = build_decision_metrics(rows, self.ID)
        self.assertIsNotNone(metrics["actual_growth"]["eps_yoy"])

    def test_a_disclosed_row_keeps_its_output(self):
        """Issue #111 never masks a past row, so the regression pin must not move."""
        history = [
            _row(id=11, fiscal_year=2024, fiscal_quarter=1, report_date=date(2024, 2, 20),
                 eps_actual=40.0, eps_estimate=38.0),
            _row(id=12, fiscal_year=2024, fiscal_quarter=2, report_date=date(2024, 5, 20),
                 eps_actual=45.0, eps_estimate=42.0),
        ]
        masked = fiscal.hide_unreleased_actuals(history, date.today())
        self.assertEqual(masked, 0, "a disclosed history must not be masked")

        metrics = build_decision_metrics(history, 12)
        growth = metrics["actual_growth"]
        self.assertEqual(Decimal(str(growth["eps_qoq"])), Decimal("0.125"))
        self.assertIsNone(growth["eps_qoq_reason"])
        self.assertIsNone(growth["eps_yoy"], "no prior-year quarter is stored")
        self.assertEqual((metrics["beat_miss_streak"]["kind"],
                          metrics["beat_miss_streak"]["count"]), ("beat", 2))


# ── the entry point ───────────────────────────────────────────────────────


class _ScriptedCursor:
    """A cursor that serves one prepared result set per statement."""

    def __init__(self, pages):
        self._pages = list(pages)
        self._current = None
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(" ".join(str(sql).split()))
        self._current = self._pages.pop(0) if self._pages else []

    def fetchone(self):
        return self._current[0] if isinstance(self._current, list) and self._current else None

    def fetchall(self):
        return self._current if isinstance(self._current, list) else []

    def close(self):
        pass


def _patched_db(pages) -> tuple[MagicMock, _ScriptedCursor]:
    cursor = _ScriptedCursor(pages)
    ctx = MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    return ctx, cursor


class DecisionReadEntryPointTests(TestCase):
    def test_the_row_and_its_history_are_masked_at_the_boundary(self):
        from app.earnings import fetch_earning_with_history

        stored = _future_history()
        ctx, cursor = _patched_db([
            [dict(stored[-1], read_today=date.today())],
            list(stored),
        ])
        with patch.object(db, "db_cursor", lambda: ctx):
            earning, history = fetch_earning_with_history(stored[-1]["id"])

        self.assertIsNotNone(earning)
        assert earning is not None
        self.assertIsNone(earning["eps_actual"])
        self.assertIsNone(earning["revenue_actual"])
        self.assertEqual(earning["date_status"], "scheduled")
        self.assertEqual(earning["comparison_unavailable_reason"],
                         fiscal.COMPARISON_REPORT_DATE_IN_FUTURE)
        self.assertNotIn("read_today", earning, "the read clock is not part of the row")

        by_id = {row["id"]: row for row in history}
        self.assertIsNone(by_id[4]["eps_actual"])
        self.assertEqual(by_id[3]["eps_actual"], 2.5, "a disclosed quarter keeps its actual")
        self.assertNotIn("read_today", by_id[3])
        self.assertEqual(len(history), len(stored))

    def test_the_read_clock_comes_from_the_database(self):
        """The clock is the row's ``read_today``, not ``date.today()``."""
        from app.earnings import fetch_earning_with_history

        stored = _future_history()
        # A pinned clock *behind* the report date masks the future row; a clock
        # beyond it does not. Only a read_today taken from the database explains
        # both, so the plumbing is asserted by behaviour and by the statement.
        ctx, cursor = _patched_db([
            [dict(stored[-1], read_today=FUTURE + timedelta(days=30))],
            list(stored),
        ])
        with patch.object(db, "db_cursor", lambda: ctx):
            earning, _history = fetch_earning_with_history(stored[-1]["id"])

        self.assertIn("CURRENT_DATE AS read_today", cursor.statements[0])
        assert earning is not None
        self.assertEqual(earning["eps_actual"], 12.1701,
                         "a report date before the database's day is disclosed")

        ctx, _cursor = _patched_db([
            [dict(stored[-1], read_today=date.today())],
            list(stored),
        ])
        with patch.object(db, "db_cursor", lambda: ctx):
            earning, history = fetch_earning_with_history(stored[-1]["id"])

        assert earning is not None
        self.assertIsNone(earning["eps_actual"])
        self.assertIsNone({row["id"]: row for row in history}[4]["eps_actual"],
                          "the history shares the row's day")

    def test_a_missing_row_is_reported_as_missing(self):
        from app.earnings import fetch_earning_with_history

        ctx, _cursor = _patched_db([[]])
        with patch.object(db, "db_cursor", lambda: ctx):
            earning, history = fetch_earning_with_history(999999)

        self.assertIsNone(earning)
        self.assertEqual(history, [])


# ── the endpoint ──────────────────────────────────────────────────────────


def _decision(earning, history, snapshots=(), rating=None, guidance=None):
    from app.auth import get_current_user
    from app.main import app as fastapi_app
    from app.routers import api as api_router

    ctx, cursor = _patched_db([
        [earning], list(history), list(snapshots),
        [rating] if rating else [], [guidance] if guidance else [],
    ])
    fastapi_app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "email": "t@t.com", "name": "T", "role": "user"}
    try:
        with patch.object(db, "db_cursor", lambda: ctx), \
                patch.object(api_router, "ensure_user", return_value={}):
            client = TestClient(fastapi_app, raise_server_exceptions=False)
            response = client.get(f"/api/earnings/{earning['id']}/decision")
    finally:
        fastapi_app.dependency_overrides = {}
    assert response.status_code == 200, response.text
    return response.json(), cursor


class DecisionEndpointTests(TestCase):
    def test_the_panel_of_a_future_quarter_carries_no_growth(self):
        stored = _future_history()
        body, _cursor = _decision(dict(stored[-1]), stored)

        growth = body["actual_growth"]
        for key, _metric, _span in GROWTH_METRICS:
            self.assertIsNone(growth[key])
            self.assertEqual(growth[f"{key}_reason"], "report_date_in_future")
        streak = body["beat_miss_streak"]
        self.assertEqual((streak["kind"], streak["count"]), ("unavailable", 0))
        self.assertEqual(streak["reason"], "report_date_in_future")
        # No ratio may reach the wire at all: the panel's "较预期" cell reads
        # 「报告尚未发生」 from the same table row, so anything numeric below it
        # is the contradiction this issue is about.
        self.assertNotIn("超预期", str(body))

    def test_the_panel_of_a_disclosed_quarter_is_unchanged(self):
        history = [
            _row(id=12, fiscal_year=2024, fiscal_quarter=2, report_date=date(2024, 5, 20),
                 eps_actual=45.0, eps_estimate=42.0),
            _row(id=11, fiscal_year=2024, fiscal_quarter=1, report_date=date(2024, 2, 20),
                 eps_actual=40.0, eps_estimate=38.0),
        ]
        body, _cursor = _decision(dict(history[0]), history)

        self.assertEqual(Decimal(str(body["actual_growth"]["eps_qoq"])), Decimal("0.125"))
        self.assertIsNone(body["actual_growth"]["eps_qoq_reason"])
        streak = body["beat_miss_streak"]
        self.assertEqual((streak["kind"], streak["count"]), ("beat", 2))
        self.assertIsNone(streak["reason"])

    def test_the_endpoint_reads_the_row_through_the_boundary(self):
        stored = _future_history()
        _body, cursor = _decision(dict(stored[-1]), stored)
        self.assertIn("CURRENT_DATE AS read_today", cursor.statements[0],
                      "the decision endpoint must not SELECT * on its own")

    def test_a_disclosed_row_is_still_reachable_when_a_later_quarter_is_not(self):
        """#111's risk note: masking a future row must not change another row's count."""
        history = _future_history()
        body, _cursor = _decision(dict(history[2]), history)
        # The opened quarter (2025 Q3, 2.5 against 2.9) is a miss whose
        # predecessor (2025 Q2) went the other way: the run is 1, unchanged by
        # the masked 2026 Q3 row further down the history.
        streak = body["beat_miss_streak"]
        self.assertEqual((streak["kind"], streak["count"]), ("miss", 1))
        self.assertIsNone(streak["reason"])


# ── the consistency assertion (acceptance 3) ──────────────────────────────


class ListAndPanelAgreeTests(TestCase):
    def test_every_withheld_row_has_a_withheld_panel(self):
        from app.earnings import fetch_earnings_from_db

        stored = _future_history()
        # What the calendar query returns for this symbol: the same rows plus the
        # columns ``fetch_earnings_from_db`` reads (its own day, the name cache).
        listed = [dict(row, read_today=date.today(), stock_names_company_name_en=None)
                  for row in stored]
        ctx, _cursor = _patched_db([listed])
        with patch.object(db, "db_cursor", lambda: ctx):
            served = fetch_earnings_from_db(symbols=["0388.HK"], markets=["HK"])

        withheld = [row for row in served
                    if row.get("comparison_unavailable_reason") == "report_date_in_future"]
        self.assertEqual(len(withheld), 1, "the future row must be withheld by the list")

        for row in withheld:
            body, _cursor = _decision(dict(stored[-1]), stored)
            growth = body["actual_growth"]
            self.assertTrue(all(growth[key] is None for key, _m, _s in GROWTH_METRICS),
                            f"{row['id']}: the panel published a value the list withholds")
            self.assertEqual(body["beat_miss_streak"]["count"], 0)
