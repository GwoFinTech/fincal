"""Issue #110: a prediction run must not restamp a period it did not change.

``earnings.updated_at`` is the revision counter behind the iCal ``SEQUENCE`` /
``LAST-MODIFIED`` and the feed's ``Last-Modified`` floor (Issue #103).  The two
sync stages already compare a row's effective values before stamping it, but the
per-period prediction upsert — the write path most of a subscription window comes
from — still ended its conflict branch with ``updated_at = NOW()``.  A weekly run
therefore stamped ~345 unchanged predicted rows with the run clock: 70% of a
491-event feed was announced as revised every week, and the feed's
``Last-Modified`` advanced although nothing had changed.

The tests below pin the contract offline (the statement says what a revision is,
and a run reports examined vs written) and, opt-in with ``--run-integration`` +
``FINCAL_TEST_DB=1``, exercise the real statement against a temporary table
inside a rolled-back transaction (the isolation convention of Issue #98).
"""
import contextlib
import importlib.util
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


predict_earnings = _load("predict_earnings_110", "scripts/predict_earnings.py")

from app import db, ical  # noqa: E402


# ── offline: the write path says what a revision is ────────────────────────
class PredictionRevisionContractTests(TestCase):
    """The upsert carries its own proof that a period actually changed."""

    def setUp(self):
        self.sql = predict_earnings._PREDICTION_INSERT_SQL
        self.conflict = self.sql.split("ON CONFLICT", 1)[1]
        self.revision = self.conflict.split("WHERE (", 1)[1]

    def test_an_unchanged_period_writes_nothing(self):
        self.assertIn("DO UPDATE SET", self.conflict)
        self.assertIn("WHERE (", self.conflict,
                      "without the guard the row is rewritten on every run")
        self.assertIn("IS DISTINCT FROM", self.revision,
                      "NULL IS NOT DISTINCT FROM NULL must not count as a revision")

    def test_the_stamp_is_inside_the_guarded_update(self):
        set_clause = self.conflict.split("DO UPDATE SET", 1)[1]
        self.assertIn("updated_at = NOW()", set_clause)
        self.assertIn("WHERE (", set_clause,
                      "the DO UPDATE's WHERE is what keeps an unchanged row untouched")
        self.assertNotIn("updated_at = CASE", self.sql,
                         "the WHERE already proves the change; a CASE would re-decide it")

    def test_the_comparison_covers_exactly_the_columns_this_path_owns(self):
        for column in predict_earnings._PREDICTION_REVISION_COLUMNS:
            self.assertIn(f"earnings.{column}", self.revision,
                          f"{column} changes the event body, so it must bump the stamp")
        for column in ("symbol", "market", "report_date", "fiscal_year", "fiscal_quarter"):
            self.assertNotIn(f"earnings.{column}", self.revision,
                             f"{column} is identity, not a value this path rewrites")

    def test_the_effective_value_is_compared_not_the_raw_field(self):
        """A re-delivery without a value must not read as a change."""
        self.assertIn("COALESCE(EXCLUDED.before_after, earnings.before_after)", self.revision)
        self.assertIn("EXCLUDED.company_name <> ''", self.revision)

    def test_the_assignment_table_is_the_only_definition_of_a_write(self):
        for column, expression in predict_earnings._PREDICTION_UPSERT.items():
            self.assertIn(f"{column} = {expression}", self.sql,
                          "the SET and the revision predicate must come from one table")
        self.assertEqual(set(predict_earnings._PREDICTION_UPSERT),
                         set(predict_earnings._PREDICTION_REVISION_COLUMNS))

    def test_a_new_period_is_still_inserted_as_a_prediction(self):
        insert = self.sql.split("ON CONFLICT", 1)[0]
        self.assertIn("INSERT INTO earnings", insert)
        self.assertIn("TRUE, 'algorithm', 'predicted'", insert)
        self.assertNotIn("updated_at", insert,
                         "a first write must take the column default, not a literal")

    def test_the_bound_parameters_still_match_the_insert(self):
        self.assertEqual(self.sql.count("%s"), 8, "one placeholder per bound field")
        self.assertNotIn("%s", self.conflict, "the guard compares stored values, not new binds")


class _StampCursor:
    """A cursor whose ``rowcount`` is the upsert's yes/no answer."""

    def __init__(self, changed: bool):
        self.changed = changed
        self.rowcount = 0
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.rowcount = 1 if self.changed else 0


def _run_predict_with_cursor(cursor):
    """``predict_for_symbol`` against the minimal model it needs, one period."""
    history = _StampHistoryCursor(cursor)

    @contextlib.contextmanager
    def fake_db_cursor():
        yield history

    stats = predict_earnings.PredictionStats()
    with patch.object(predict_earnings, "db_cursor", fake_db_cursor):
        count = predict_earnings.predict_for_symbol("AAPL", "US", stats)
    return count, stats, history


class _StampHistoryCursor(_StampCursor):
    """Answers the two reads ``predict_for_symbol`` does before the walk."""

    def __init__(self, upsert_cursor: _StampCursor):
        super().__init__(changed=True)
        self.upsert_cursor = upsert_cursor
        self._result = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.executed.append((flat, params))
        self._result = []
        if flat.startswith("SELECT id, report_date, report_type"):
            self._result = [
                {"id": 1, "report_date": date(2025, 1, 30), "report_type": "Q",
                 "fiscal_year": 2025, "fiscal_quarter": 1, "before_after": "after",
                 "is_predicted": False, "date_source": "longbridge",
                 "eps_actual": 2.4, "revenue_actual": None},
                {"id": 2, "report_date": date(2026, 1, 29), "report_type": "Q",
                 "fiscal_year": 2026, "fiscal_quarter": 1, "before_after": "after",
                 "is_predicted": False, "date_source": "longbridge",
                 "eps_actual": 2.5, "revenue_actual": None},
            ]
        elif flat.startswith("SELECT company_name FROM earnings"):
            self._result = [{"company_name": "Apple"}]
        elif flat.startswith("SELECT COALESCE("):
            self._result = [{"company_name_en": "APPLE"}]
        elif flat.startswith("INSERT INTO earnings"):
            self.rowcount = 1 if self.upsert_cursor.changed else 0
        else:
            raise AssertionError(f"predict_for_symbol issued an unexpected statement: {flat}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class PredictionRunCounterTests(TestCase):
    """``details`` must separate "periods examined" from "rows written" (#110)."""

    def test_a_converged_period_is_reported_as_unchanged(self):
        _, stats, _ = _run_predict_with_cursor(_StampCursor(changed=False))

        details = stats.details()
        self.assertEqual(details["examined"], 1)
        self.assertEqual(details["written"], 0)
        self.assertEqual(details["unchanged"], 1)
        self.assertEqual(details["predicted"], 1,
                         "the historical key keeps its meaning (periods examined)")

    def test_a_changed_period_is_reported_as_written(self):
        _, stats, _ = _run_predict_with_cursor(_StampCursor(changed=True))

        details = stats.details()
        self.assertEqual((details["examined"], details["written"], details["unchanged"]),
                         (1, 1, 0))


# ── opt-in: the statement on real PostgreSQL ───────────────────────────────
_SHADOW_DDL = """
CREATE TEMP TABLE earnings (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'US',
    company_name TEXT DEFAULT '',
    company_name_en TEXT DEFAULT '',
    report_date DATE NOT NULL,
    report_type TEXT DEFAULT 'Q',
    fiscal_year INTEGER,
    fiscal_quarter INTEGER,
    before_after TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    is_predicted BOOLEAN DEFAULT FALSE,
    date_source TEXT NOT NULL DEFAULT 'unknown',
    date_status TEXT NOT NULL DEFAULT 'scheduled',
    UNIQUE(symbol, market, report_date, report_type)
);
"""

#: Inside one transaction ``NOW()`` is the transaction start, so an untouched
#: stamp is only distinguishable from a re-stamped one by a sentinel.
_SENTINEL = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@skipUnless(os.environ.get("FINCAL_TEST_DB") == "1",
            "set FINCAL_TEST_DB=1 with a reachable fincal DB (temp tables inside "
            "a rolled-back transaction)")
@pytest.mark.integration
class PredictionStampIntegrationTests(TestCase):
    """The real statement: same values → same stamps and SEQUENCE."""

    def setUp(self):
        self._connection_cm = db.db_connection()
        self.conn = self._connection_cm.__enter__()
        self.conn.autocommit = False
        with self.conn.cursor() as setup:
            setup.execute(_SHADOW_DDL)

    def tearDown(self):
        self.conn.rollback()
        self._connection_cm.__exit__(None, None, None)

    # ── helpers ────────────────────────────────────────────────────────────
    @contextlib.contextmanager
    def _cursor(self):
        with self.conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
            yield cur

    def _upsert(self, **overrides):
        values = dict(symbol="AAPL", market="US", company_name="Apple",
                      company_name_en="",
                      report_date="2027-01-29", fiscal_year=2027,
                      fiscal_quarter=1, before_after="after")
        values.update(overrides)
        with self._cursor() as cur:
            return predict_earnings.upsert_prediction(cur, **values)

    def _stored(self):
        with self._cursor() as cur:
            cur.execute("SELECT *, xmin::text AS row_version FROM earnings")
            rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _freeze_stamp(self):
        with self._cursor() as cur:
            cur.execute("UPDATE earnings SET updated_at = %s", (_SENTINEL,))
        return self._stored()

    def _sequence(self, row):
        return ical._event_sequence(dict(row), row["report_date"])

    # ── the three acceptance cases ─────────────────────────────────────────
    def test_a_new_period_is_written_and_stamped(self):
        self.assertTrue(self._upsert())

        row = self._stored()
        self.assertTrue(row["is_predicted"])
        self.assertEqual(row["date_source"], "algorithm")
        self.assertEqual(row["date_status"], "predicted")
        self.assertIsNotNone(row["updated_at"])

    def test_the_same_period_recomputed_writes_nothing(self):
        self.assertTrue(self._upsert())
        before = self._freeze_stamp()

        self.assertFalse(self._upsert(), "an unchanged period is not a write")

        after = self._stored()
        self.assertEqual(after["updated_at"], _SENTINEL)
        self.assertEqual(after["row_version"], before["row_version"],
                         "no row version means no dead tuple and no index churn")
        self.assertEqual(self._sequence(after), self._sequence(before),
                         "SEQUENCE and LAST-MODIFIED must be identical (#37/#110)")

    def test_a_missing_before_after_is_not_a_change(self):
        """The conflict branch's COALESCE would keep the stored value anyway."""
        self._upsert(before_after="after")
        before = self._freeze_stamp()

        self.assertFalse(self._upsert(before_after=None))

        after = self._stored()
        self.assertEqual(after["before_after"], "after")
        self.assertEqual(after["updated_at"], _SENTINEL)

    def test_a_changed_before_after_is_a_revision(self):
        self._upsert(before_after="after")
        before = self._freeze_stamp()

        self.assertTrue(self._upsert(before_after="before"))

        after = self._stored()
        self.assertEqual(after["before_after"], "before")
        self.assertGreater(after["updated_at"], _SENTINEL)
        self.assertGreater(self._sequence(after), self._sequence(before),
                           "a real revision must still advance SEQUENCE (#37)")

    def test_a_changed_name_is_a_revision(self):
        self._upsert(company_name="Apple")
        before = self._freeze_stamp()

        self.assertTrue(self._upsert(company_name="Apple Inc"))

        after = self._stored()
        self.assertEqual(after["company_name"], "Apple Inc")
        self.assertGreater(after["updated_at"], _SENTINEL)
        self.assertGreater(self._sequence(after), self._sequence(before))

    def test_a_missing_latin_name_is_filled_and_is_a_revision(self):
        """Issue #106: the first Latin name to arrive changes the event body."""
        self._upsert(company_name_en="")
        before = self._freeze_stamp()

        self.assertTrue(self._upsert(company_name_en="APPLE"))

        after = self._stored()
        self.assertEqual(after["company_name_en"], "APPLE")
        self.assertGreater(after["updated_at"], _SENTINEL)
        self.assertGreater(self._sequence(after), self._sequence(before))

    def test_a_stored_latin_name_is_neither_replaced_nor_blanked(self):
        """Issue #106: fill-only in both directions, so no fake revision either."""
        self._upsert(company_name_en="APPLE")
        before = self._freeze_stamp()

        self.assertFalse(self._upsert(company_name_en="APPLE INC"))
        self.assertFalse(self._upsert(company_name_en=""))

        after = self._stored()
        self.assertEqual(after["company_name_en"], "APPLE")
        self.assertEqual(after["updated_at"], _SENTINEL)
        self.assertEqual(self._sequence(after), self._sequence(before))

    def test_a_provider_row_on_the_same_key_is_not_rewritten_when_nothing_changed(self):
        """The guard also protects a confirmed row the prediction date happens to hit."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO earnings (symbol, market, company_name, report_date,"
                " report_type, fiscal_year, fiscal_quarter, before_after,"
                " is_predicted, date_source, date_status, updated_at)"
                " VALUES ('AAPL', 'US', 'Apple', '2027-01-29', 'Q', 2027, 1,"
                " 'after', FALSE, 'longbridge', 'scheduled', %s)",
                (_SENTINEL,),
            )
        before = self._stored()

        self.assertFalse(self._upsert())

        after = self._stored()
        self.assertEqual(after["updated_at"], _SENTINEL)
        self.assertFalse(after["is_predicted"], "a provider row is never made predicted")
        self.assertEqual(after["date_source"], "longbridge")

    def test_a_full_second_pass_writes_none_of_the_converged_periods(self):
        """The production shape: ~350 periods, one run after another, nothing changed."""
        periods = [(2027 + quarter // 4, quarter % 4 + 1) for quarter in range(12)]
        for index, (fiscal_year, quarter) in enumerate(periods):
            self.assertTrue(self._upsert(symbol=f"SYM{index}", fiscal_year=fiscal_year,
                                         fiscal_quarter=quarter,
                                         report_date="2027-03-15"))
        with self._cursor() as cur:
            cur.execute("UPDATE earnings SET updated_at = %s", (_SENTINEL,))

        written = sum(
            self._upsert(symbol=f"SYM{index}", fiscal_year=fiscal_year,
                         fiscal_quarter=quarter,
                         report_date="2027-03-15")
            for index, (fiscal_year, quarter) in enumerate(periods)
        )

        self.assertEqual(written, 0, "a converged run must not write a single row")
        with self._cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM earnings WHERE updated_at > %s", (_SENTINEL,))
            self.assertEqual(cur.fetchone()["n"], 0,
                             "no row may carry the run clock after a no-op pass")
