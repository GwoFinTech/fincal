"""Issue #103: a re-delivered, unchanged row must not be announced as revised.

``earnings.updated_at`` is the revision counter three consumers read
(``SEQUENCE`` and ``LAST-MODIFIED`` in ``app/ical.py``, the feed's
``Last-Modified`` floor in ``app/routers/ical.py``, and ``authority_key``'s
tie-break).  Both sync stages used to write it unconditionally, so one weekly
Longbridge run stamped 25,875 unchanged rows with a batch time and the 497
events of a generated feed collapsed into 12 ``SEQUENCE`` values — every
subscriber saw a "full revision" every week (RFC 5545 §3.8.7.4).

The stamps now follow the *values*: this module covers, offline, that the write
paths say so, and — opt-in against real PostgreSQL (``FINCAL_TEST_DB=1``) — that
the statements behave that way, using temp-table shadows of ``earnings`` inside
a rolled-back transaction (the isolation convention of Issue #98).
"""
import contextlib
import importlib.util
import os
import re
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


sync_earnings = _load("sync_earnings", "scripts/sync_earnings.py")
sync_futu = _load("sync_futu", "scripts/sync_futu.py")

from app import db, fiscal, ical  # noqa: E402


# ── offline: the write paths say what a revision is ────────────────────────
class _Recorder:
    """Capture every ``execute_values`` statement instead of running it."""

    def __init__(self):
        self.calls = []

    def __call__(self, cur, sql, argslist, page_size=200):
        self.calls.append({"sql": sql, "rows": list(argslist)})

    def statements(self):
        return [call["sql"] for call in self.calls]

    def insert(self) -> str:
        return [sql for sql in self.statements() if "INSERT INTO earnings " in sql][0]


class _Cursor:
    """A cursor that answers the pre-upsert guards with "no stored row"."""

    def __init__(self):
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []


@contextlib.contextmanager
def _fake_db():
    ctx = MagicMock()
    ctx.__enter__.return_value = _Cursor()
    ctx.__exit__.return_value = False
    with patch.object(sync_earnings, "db_cursor", return_value=ctx):
        yield


def _row(**overrides) -> tuple:
    """One Longbridge batch tuple in ``sync_earnings.flush_batch``'s layout."""
    values = dict(symbol="KTOS", market="US", company_name="Kratos",
                  report_date="2026-08-04", report_type="Q", fiscal_year=2026,
                  fiscal_quarter=2, eps_estimate=1.05, eps_actual=None,
                  revenue_estimate=11.3, revenue_actual=None, before_after="after",
                  estimate_currency="USD", actual_currency=None,
                  estimate_basis="unknown", actual_basis="unknown")
    values.update(overrides)
    return tuple(values[column] for column in sync_earnings._BATCH_VALUE_COLUMNS)


def _longbridge_sql() -> str:
    recorder = _Recorder()
    cursor = _Cursor()
    with patch("psycopg2.extras.execute_values", recorder), _fake_db():
        sync_earnings.flush_batch(cursor, [_row()])
    return recorder.insert()


class LongbridgeRevisionStampTests(TestCase):
    """The Longbridge upsert must not carry a run clock."""

    def test_updated_at_is_conditional_on_the_row_content(self):
        sql = _longbridge_sql()
        self.assertIn("updated_at = CASE WHEN", sql)
        self.assertIn("IS DISTINCT FROM", sql)
        self.assertNotIn("updated_at = NOW()", sql,
                         "a re-delivery must not restamp the row with the run time")

    def test_the_comparison_covers_every_column_a_client_can_see(self):
        sql = _longbridge_sql()
        for column in sync_earnings._REVISION_COLUMNS:
            self.assertIn(f"earnings.{column}", sql,
                          f"{column} changes the event body, so it must bump the stamp")

    def test_provenance_is_not_part_of_the_revision(self):
        """Both stages rewrite provenance every run; counting it is the original bug."""
        for column in ("date_source", "date_status", "estimate_source", "actual_source"):
            self.assertNotIn(column, sync_earnings._REVISION_COLUMNS)

    def test_estimate_as_of_follows_only_the_estimate_side(self):
        sql = _longbridge_sql()
        estimate = re.search(r"estimate_as_of = CASE WHEN \((.*?)\) IS DISTINCT FROM \((.*?)\) THEN",
                             sql, re.S)
        actual = re.search(r"actual_as_of = CASE WHEN \((.*?)\) IS DISTINCT FROM \((.*?)\) THEN",
                           sql, re.S)
        self.assertIsNotNone(estimate)
        self.assertIsNotNone(actual)
        self.assertIn("earnings.eps_estimate", estimate.group(2))
        self.assertIn("earnings.revenue_estimate", estimate.group(2))
        self.assertNotIn("eps_actual", estimate.group(2))
        self.assertIn("earnings.eps_actual", actual.group(2))
        self.assertIn("earnings.revenue_actual", actual.group(2))
        self.assertNotIn("eps_estimate", actual.group(2))

    def test_a_first_write_stamps_the_numbers_it_inserts(self):
        sql = _longbridge_sql()
        self.assertIn("estimate_as_of, actual_as_of", sql)
        self.assertIn("CASE WHEN v.eps_estimate IS NOT NULL OR v.revenue_estimate IS NOT NULL", sql)
        self.assertIn("THEN NOW() END", sql)

    def test_the_provenance_update_no_longer_writes_a_timestamp(self):
        """#89/#91 convergence still runs — without touching the revision stamps."""
        recorder = _Recorder()
        cursor = _Cursor()
        with patch("psycopg2.extras.execute_values", recorder), _fake_db():
            sync_earnings.flush_batch(cursor, [_row()])
        follow_up = [sql for sql in recorder.statements() if sql.startswith("UPDATE earnings AS e")]
        self.assertEqual(len(follow_up), 1)
        self.assertNotIn("NOW()", follow_up[0])
        self.assertIn("date_source = 'longbridge'", follow_up[0])
        self.assertIn("estimate_source = CASE", follow_up[0])

    def test_the_batch_values_and_the_insert_columns_stay_one_contract(self):
        recorder = _Recorder()
        cursor = _Cursor()
        with patch("psycopg2.extras.execute_values", recorder), _fake_db():
            sync_earnings.flush_batch(cursor, [_row()])
        sql = recorder.insert()
        batch = [call["rows"] for call in recorder.calls if "INSERT INTO earnings " in call["sql"]][0]
        alias = re.search(r"FROM \(VALUES %s\) AS v\(([^)]*)\)", sql, re.S).group(1)
        names = [name.strip() for name in alias.split(",")]
        self.assertEqual(names, list(sync_earnings._BATCH_VALUE_COLUMNS))
        self.assertEqual(len(names), len(batch[0]),
                         "one VALUES field per batch tuple field")
        inserted = re.search(r"INSERT INTO earnings \(([^)]*)\)", sql, re.S).group(1)
        self.assertEqual([column.strip() for column in inserted.split(",")],
                         names + ["estimate_as_of", "actual_as_of"])


class FutuRevisionStampTests(TestCase):
    """The Futu write paths follow the same rule."""

    def test_the_dates_upsert_only_moves_the_stamp_on_a_change(self):
        sql = sync_futu._DATE_UPSERT_SQL
        self.assertIn("updated_at = CASE WHEN", sql)
        self.assertNotIn("updated_at = NOW()", sql)
        for column in sync_futu._DATE_REVISION_COLUMNS:
            self.assertIn(f"earnings.{column}", sql)

    def test_the_actuals_update_moves_its_stamp_only_on_a_change(self):
        sql = sync_futu._ACTUAL_UPDATE_SQL.format(column="eps_actual")
        self.assertIn("actual_as_of = CASE WHEN eps_actual IS DISTINCT FROM %s::numeric", sql)
        self.assertIn("THEN NOW() ELSE actual_as_of END", sql)
        self.assertIn("updated_at = CASE WHEN (eps_actual, actual_currency, actual_basis)", sql)
        self.assertNotIn("NOW(),", sql)
        # #45/#91 guards are untouched: a Futu actual still replaces a
        # lower-priority one, but never one half of a same-provider pair.
        self.assertIn("COALESCE(actual_source, 'unknown') IN", sql)
        self.assertIn(sync_futu._KEEP_SAME_PROVIDER_PAIR_SQL, sql)

    def test_the_actuals_update_binds_one_value_per_placeholder(self):
        for column in ("eps_actual", "revenue_actual"):
            sql = sync_futu._ACTUAL_UPDATE_SQL.format(column=column)
            self.assertEqual(sql.count("%s"), 11,
                             "5 assignments + 4 key columns + 2 revision comparisons")

    def test_the_revision_predicate_is_a_null_safe_row_comparison(self):
        predicate = fiscal.revision_predicate(
            {"a": "COALESCE(EXCLUDED.a, earnings.a)", "b": "EXCLUDED.b"}, ("a", "b"))
        self.assertEqual(
            predicate,
            "((COALESCE(EXCLUDED.a, earnings.a)), (EXCLUDED.b))"
            " IS DISTINCT FROM (earnings.a, earnings.b)")
        self.assertIn("IS DISTINCT FROM", predicate,
                      "NULL IS NOT DISTINCT FROM NULL must not count as a revision")


# ── opt-in: the statements on real PostgreSQL ──────────────────────────────
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
    eps_estimate NUMERIC,
    eps_actual NUMERIC,
    revenue_estimate NUMERIC,
    revenue_actual NUMERIC,
    before_after TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    is_predicted BOOLEAN DEFAULT FALSE,
    date_source TEXT NOT NULL DEFAULT 'unknown',
    date_status TEXT NOT NULL DEFAULT 'scheduled',
    estimate_source TEXT,
    estimate_as_of TIMESTAMPTZ,
    estimate_currency TEXT,
    estimate_basis TEXT,
    actual_source TEXT,
    actual_as_of TIMESTAMPTZ,
    actual_currency TEXT,
    actual_basis TEXT,
    UNIQUE(symbol, market, report_date, report_type)
);
CREATE UNIQUE INDEX idx_earnings_fiscal_identity ON earnings
    (symbol, market, fiscal_year, fiscal_quarter)
    WHERE is_predicted = FALSE AND fiscal_year IS NOT NULL AND fiscal_quarter IS NOT NULL;
CREATE TEMP TABLE earnings_estimate_snapshots (
    id BIGSERIAL PRIMARY KEY,
    earning_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    eps_estimate NUMERIC,
    revenue_estimate NUMERIC,
    currency TEXT,
    basis TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);
"""

#: A stored row's stamps are moved here so that a rewritten stamp is detectable:
#: inside one transaction ``NOW()`` is the transaction's start time, so the
#: sentinel is the only way to tell "left alone" from "stamped again".
_SENTINEL = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@skipUnless(os.environ.get("FINCAL_TEST_DB") == "1",
            "set FINCAL_TEST_DB=1 with a reachable fincal DB (temp tables inside "
            "a rolled-back transaction)")
@pytest.mark.integration
class RevisionStampIntegrationTests(TestCase):
    """The real statements: same values → same stamps; changed value → new ones."""

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

    def _flush(self, rows):
        with self._cursor() as cur:
            return sync_earnings.flush_batch(cur, rows)

    def _stored(self, symbol="KTOS", report_date=date(2026, 8, 4)):
        with self._cursor() as cur:
            cur.execute("SELECT * FROM earnings WHERE symbol = %s AND report_date = %s",
                        (symbol, report_date))
            rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _freeze_stamps(self):
        with self._cursor() as cur:
            cur.execute("UPDATE earnings SET updated_at = %s, estimate_as_of = %s,"
                        " actual_as_of = %s", (_SENTINEL, _SENTINEL, _SENTINEL))
        return _SENTINEL

    def _sequence(self, row):
        return ical._event_sequence(dict(row), row["report_date"])

    # ── the three acceptance cases ─────────────────────────────────────────
    def test_same_values_re_delivered_leave_every_stamp_alone(self):
        self._flush([_row()])
        first = self._stored()
        self.assertIsNotNone(first["updated_at"])
        self.assertIsNotNone(first["estimate_as_of"], "a first write stamps its number")

        frozen = self._freeze_stamps()
        before = self._stored()
        # Empty fields included: an event that repeats only the date must be a
        # non-change too, which is what the COALESCE-aware comparison buys.
        self._flush([_row(company_name="Kratos", eps_estimate=1.05, revenue_estimate=11.3,
                          estimate_currency="USD", before_after="after")])
        again = self._stored()

        self.assertEqual(again["updated_at"], frozen)
        self.assertEqual(again["estimate_as_of"], frozen)
        self.assertEqual(again["actual_as_of"], frozen)
        self.assertEqual(self._sequence(again), self._sequence(before),
                         "an unchanged event must not be announced as a revision")

    def test_a_re_delivery_without_the_numbers_is_not_a_change(self):
        self._flush([_row()])
        frozen = self._freeze_stamps()

        self._flush([_row(eps_estimate=None, revenue_estimate=None, estimate_currency=None)])
        row = self._stored()

        self.assertEqual(float(row["eps_estimate"]), 1.05, "COALESCE keeps the stored value")
        self.assertEqual(row["updated_at"], frozen)
        self.assertEqual(row["estimate_as_of"], frozen)

    def test_a_changed_number_moves_all_three_and_the_sequence(self):
        self._flush([_row()])
        frozen = self._freeze_stamps()
        before = self._stored()

        self._flush([_row(eps_estimate=1.09)])
        after = self._stored()

        self.assertEqual(float(after["eps_estimate"]), 1.09)
        self.assertGreater(after["updated_at"], frozen)
        self.assertGreater(after["estimate_as_of"], frozen)
        self.assertEqual(after["actual_as_of"], frozen,
                         "the actual side did not change, so its stamp must not move")
        self.assertGreater(self._sequence(after), self._sequence(before),
                           "SEQUENCE must advance on a real revision (#37)")

    def test_provenance_and_status_still_converge(self):
        self._flush([_row(eps_actual=0.02, actual_currency=None)])
        row = self._stored()
        self.assertEqual(row["date_source"], "longbridge")
        self.assertEqual(row["date_status"], "reported")
        self.assertEqual(row["estimate_source"], "longbridge")
        self.assertEqual(row["actual_source"], "longbridge")

    def test_a_futu_actual_that_did_not_change_is_not_restamped(self):
        self._flush([_row()])
        self._freeze_stamps()

        with patch.object(sync_futu, "check_cancelled"), \
             patch.object(sync_futu, "get_rate_limiter",
                          return_value=sync_futu.FutuRateLimiter(
                              max_calls=10_000, window_seconds=30,
                              clock=lambda: 0.0, sleep=lambda _s: None)), \
             patch("app.sync_audit.heartbeat"), \
             patch.object(sync_futu, "db_cursor", side_effect=self._cursor):
            sync_futu.sync_actuals(self._opend(1.23), 1, ["KTOS.US"])
            first = self._stored()
            self.assertEqual(float(first["eps_actual"]), 1.23)
            self.assertGreater(first["actual_as_of"], _SENTINEL,
                               "a new actual must be stamped")
            self.assertEqual(first["actual_source"], "futu")

            frozen = self._freeze_stamps()
            sync_futu.sync_actuals(self._opend(1.23), 1, ["KTOS.US"])
            again = self._stored()

        self.assertEqual(again["actual_as_of"], frozen)
        self.assertEqual(again["updated_at"], frozen)

    def test_a_futu_date_that_did_not_change_is_not_restamped(self):
        self._flush([_row()])
        frozen = self._freeze_stamps()
        batch = [("KTOS", "US", "", "2026-08-04", "Q", 2026, 2, "after", "futu", "scheduled")]

        with patch.object(sync_futu, "db_cursor", side_effect=self._cursor):
            sync_futu.flush_date_batch(list(batch))
        unchanged = self._stored()
        self.assertEqual(unchanged["updated_at"], frozen,
                         "Futu re-confirming the same date is not a revision")
        self.assertEqual(unchanged["date_source"], "futu",
                         "provenance still converges on every run")

        frozen = self._freeze_stamps()
        with patch.object(sync_futu, "db_cursor", side_effect=self._cursor):
            sync_futu.flush_date_batch([("KTOS", "US", "", "2026-08-04", "Q", 2026, 2,
                                         "before", "futu", "scheduled")])
        changed = self._stored()
        self.assertEqual(changed["before_after"], "before")
        self.assertGreater(changed["updated_at"], frozen,
                           "the session changed, so the event body changed")

    @staticmethod
    def _opend(eps: float) -> MagicMock:
        """One OpenD income statement (EPS + revenue), as ``sync_actuals`` reads it."""
        ctx = MagicMock()
        ctx.get_financials_statements.return_value = (0, {"report_list": [{
            "fiscal_year": 2026, "financial_type": 2,
            "currency_code": "USD", "accounting_standards": "US_GAAP",
            "item_list": [
                {"field_id": 8047, "display_name": "基本每股收益", "data": eps},
                {"field_id": 8002, "display_name": "营业总收入", "data": 123.0},
            ],
        }]})
        return ctx
