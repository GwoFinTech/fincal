"""Issue #74: a brand-new database must start, and report honestly if it cannot.

``app/db.py::init_db()`` ends by creating the partial unique index
``idx_earnings_fiscal_identity`` over confirmed rows, and the start-up sequence
is ``init_db()`` → ``seed_earnings_if_empty()`` → ``recover_stale_runs()``.  On
an empty database the index therefore exists *before* the demo rows are written,
so the demo data is subject to the same fiscal-period invariant as production
data.  Two literal demo rows violated it — ``MSFT/US/FY2026 Q4`` and
``GOOGL/US/FY2026 Q2`` each appeared twice with different display dates — and
the consequences were: ``UniqueViolation`` on the first start-up, the seeding
transaction rolled back (0 rows), and every following start hit the same index
and the same rows.  Under ``restart: unless-stopped`` that is a restart loop
with no self-healing path, and the failure was invisible in production because
a database that already holds duplicates takes the "skip the index" branch.

Covered here:

* the demo data satisfies the fiscal-period invariant (and the display key) —
  this test goes red if the duplicate rows come back;
* the demo seed is *best effort*: a rejected seed logs a warning and start-up
  continues, because real data arrives through the sync pipeline and "will not
  boot" is far worse than "no demo rows";
* an opt-in integration test (``FINCAL_TEST_DB=1``) drives the real
  ``init_db()`` + seed + second cold start against an empty schema inside a
  transaction that is rolled back.
"""
import ast
import inspect
import os
import re
import sys
import unittest
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from unittest import TestCase, mock, skipUnless

from psycopg2 import errors

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, earnings  # noqa: E402
from app.earnings import DEMO_EARNINGS_ROWS, seed_earnings_if_empty  # noqa: E402

#: The tuple layout documented above ``DEMO_EARNINGS_ROWS`` and used by
#: ``_seed_demo_data()``'s INSERT column list.
ROW_LAYOUT = [
    "symbol", "market", "company_name", "report_date", "report_type",
    "fiscal_year", "fiscal_quarter", "eps_estimate", "eps_actual",
    "revenue_estimate", "revenue_actual", "before_after",
]


# ── the demo data must satisfy the invariant the index enforces ────────────

class DemoSeedDataTests(TestCase):
    def test_one_confirmed_row_per_fiscal_period(self):
        """The regression: seeding these rows on a fresh DB raised UniqueViolation."""
        seen = {}
        for symbol, market, _name, _date, _type, year, quarter, *_rest in DEMO_EARNINGS_ROWS:
            seen.setdefault((symbol, market, year, quarter), 0)
            seen[(symbol, market, year, quarter)] += 1
        duplicated = {key: count for key, count in seen.items() if count > 1}
        self.assertEqual(
            duplicated, {},
            f"demo rows would violate idx_earnings_fiscal_identity: {duplicated}",
        )

    def test_the_two_june_rows_own_a_period_of_their_own(self):
        """Named explicitly: these are the two rows the Issue reported."""
        periods = {
            (symbol, market, year, quarter)
            for symbol, market, _n, _d, _t, year, quarter, *_r in DEMO_EARNINGS_ROWS
        }
        self.assertIn(("MSFT", "US", 2026, 3), periods)     # June row
        self.assertIn(("MSFT", "US", 2026, 4), periods)     # July row
        self.assertIn(("GOOGL", "US", 2026, 1), periods)    # June row
        self.assertIn(("GOOGL", "US", 2026, 2), periods)    # July row
        msft_q4 = [r for r in DEMO_EARNINGS_ROWS if r[0] == "MSFT" and r[5] == 2026 and r[6] == 4]
        googl_q2 = [r for r in DEMO_EARNINGS_ROWS if r[0] == "GOOGL" and r[5] == 2026 and r[6] == 2]
        self.assertEqual((len(msft_q4), len(googl_q2)), (1, 1))

    def test_demo_rows_are_unique_on_the_display_key(self):
        """``ON CONFLICT (symbol, market, report_date, report_type)`` must not clobber."""
        keys = [(r[0], r[1], r[3], r[4]) for r in DEMO_EARNINGS_ROWS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_demo_row_matches_the_insert_column_list(self):
        source = inspect.getsource(earnings._seed_demo_data)
        columns = re.search(r"INSERT INTO earnings\s*\(([^)]*)\)", source)
        self.assertIsNotNone(columns, "the demo INSERT column list moved")
        parsed = [name.strip() for name in columns.group(1).split(",") if name.strip()]
        self.assertEqual(parsed, ROW_LAYOUT)
        for row in DEMO_EARNINGS_ROWS:
            self.assertEqual(len(row), len(parsed), f"width mismatch: {row!r}")


class SeedScriptDataTests(TestCase):
    """``scripts/seed_earnings.py`` seeds the same kind of demo data.

    It calls ``init_db()`` first, so an environment that is bootstrapped through
    the script hits the very same index — and there the failure is an uncaught
    traceback instead of a warning.  The demo list is a local literal, so it is
    read statically (the same way the Issue confirmed the duplicates).
    """

    SCRIPT = ROOT / "scripts" / "seed_earnings.py"

    def _rows(self):
        tree = ast.parse(self.SCRIPT.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "demo_earnings":
                return ast.literal_eval(node.value)
        self.fail("demo_earnings literal not found in scripts/seed_earnings.py")

    def test_script_demo_rows_keep_one_confirmed_row_per_fiscal_period(self):
        rows = self._rows()
        self.assertTrue(rows, "the parsed demo list must not be empty")
        seen = Counter((r[0], r[1], r[5], r[6]) for r in rows)
        self.assertEqual(
            {key: count for key, count in seen.items() if count > 1}, {},
            "script demo rows would violate idx_earnings_fiscal_identity",
        )

    def test_script_demo_rows_are_unique_on_the_display_key(self):
        keys = Counter((r[0], r[1], r[3], r[4]) for r in self._rows())
        self.assertEqual({k: c for k, c in keys.items() if c > 1}, {})

    def test_script_demo_rows_match_the_insert_column_list(self):
        rows = self._rows()
        expected = re.search(
            r"INSERT INTO earnings\s*\(([^)]*)\)", self.SCRIPT.read_text())
        self.assertIsNotNone(expected, "the demo INSERT column list moved")
        parsed = [name.strip() for name in expected.group(1).split(",") if name.strip()]
        self.assertEqual(parsed, ROW_LAYOUT)
        self.assertEqual({len(row) for row in rows}, {len(parsed)})


# ── the seed must never be the reason the process cannot start ─────────────

class _SeedCursor:
    """Answers the row count, then either records the demo INSERTs or raises."""

    def __init__(self, count=0, raise_on_insert=None, raise_on_count=None):
        self._count = count
        self._raise_on_insert = raise_on_insert
        self._raise_on_count = raise_on_count
        self.inserts = []
        self.statements = []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.statements.append(text)
        if "INSERT INTO earnings" in text:
            if self._raise_on_insert is not None:
                raise self._raise_on_insert
            self.inserts.append(params)
        elif "COUNT" in text.upper() and self._raise_on_count is not None:
            raise self._raise_on_count

    def fetchone(self):
        return {"cnt": self._count}


def _cursor_ctx(cursor):
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    return ctx


class SeedIsBestEffortTests(TestCase):
    def _run(self, cursor):
        with mock.patch.object(db, "db_cursor", return_value=_cursor_ctx(cursor)):
            with self.assertLogs("app.earnings", level="WARNING") as captured:
                seed_earnings_if_empty()          # must not raise
            return captured

    def test_a_rejected_seed_warns_instead_of_aborting_startup(self):
        cursor = _SeedCursor(count=0, raise_on_insert=errors.UniqueViolation(
            'duplicate key value violates unique constraint "idx_earnings_fiscal_identity"'))
        captured = self._run(cursor)
        self.assertIn("demo seed skipped", captured.output[0])
        self.assertIn("UniqueViolation", captured.output[0])

    def test_a_seed_that_raises_permission_errors_is_also_non_fatal(self):
        """Any DB-side rejection of the demo data is a warning, not a crash."""
        cursor = _SeedCursor(count=0, raise_on_insert=errors.InsufficientPrivilege("denied"))
        captured = self._run(cursor)
        self.assertIn("demo seed skipped", captured.output[0])

    def test_an_unreachable_table_probe_is_not_fatal_either(self):
        """The whole seeding helper is best effort, including its row-count probe."""
        cursor = _SeedCursor(raise_on_count=errors.OperationalError("connection reset"))
        captured = self._run(cursor)
        self.assertIn("demo seed skipped", captured.output[0])
        self.assertIn("OperationalError", captured.output[0])
        self.assertEqual(cursor.inserts, [])

    def test_a_healthy_empty_table_still_gets_every_demo_row(self):
        cursor = _SeedCursor(count=0)
        with mock.patch.object(db, "db_cursor", return_value=_cursor_ctx(cursor)), \
             self.assertLogs("app.earnings", level="INFO") as captured:
            seed_earnings_if_empty()
        self.assertEqual(len(cursor.inserts), len(DEMO_EARNINGS_ROWS))
        self.assertIn("seeding demo data", "\n".join(captured.output))

    def test_a_populated_table_is_left_alone(self):
        cursor = _SeedCursor(count=1)
        with mock.patch.object(db, "db_cursor", return_value=_cursor_ctx(cursor)):
            seed_earnings_if_empty()          # no exception, no INSERT
        self.assertEqual(cursor.inserts, [])
        self.assertEqual(len(cursor.statements), 1)     # only the row-count probe
        self.assertIn("COUNT", cursor.statements[0])


# ── opt-in: the real thing, on a real empty schema ─────────────────────────

SCHEMA = "fincal_issue74_selftest"


@skipUnless(os.environ.get("FINCAL_TEST_DB") == "1",
            "set FINCAL_TEST_DB=1 with a reachable fincal DB (writes only to a "
            "throwaway schema inside a rolled-back transaction)")
class FreshDatabaseStartupTests(TestCase):
    """Acceptance 1-3 of Issue #74 against a real, empty schema.

    Everything runs inside one transaction on a dedicated connection using a
    temporary ``search_path``, and is rolled back: no database is created or
    dropped and no row outside the transaction is ever touched.
    """

    def test_init_db_then_seed_succeeds_twice_on_an_empty_schema(self):
        from app.sync_audit import recover_stale_runs

        with db.db_connection() as conn:
            conn.autocommit = False

            @contextmanager
            def _cursor():
                with conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
                    yield cur

            try:
                with conn.cursor() as setup:
                    setup.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
                    setup.execute(f"CREATE SCHEMA {SCHEMA}")
                    setup.execute(f"SET search_path TO {SCHEMA}")

                with conn.cursor() as guard:
                    guard.execute("SELECT current_schema() AS s")
                    self.assertEqual(guard.fetchone()[0], SCHEMA)

                with mock.patch.object(db, "db_cursor", _cursor), \
                     self.assertLogs("app.db", level="INFO") as db_logs:
                    # First cold start: empty schema, index gets created, seed runs.
                    db.init_db()
                    seed_earnings_if_empty()
                    recover_stale_runs()

                    def scalar(sql, params=None):
                        with conn.cursor() as cur:
                            cur.execute(sql, params)
                            return cur.fetchone()[0]

                    self.assertEqual(
                        scalar("SELECT count(*) FROM earnings"), len(DEMO_EARNINGS_ROWS))
                    self.assertEqual(scalar(
                        "SELECT count(*) FROM pg_indexes WHERE tablename='earnings' "
                        "AND indexname='idx_earnings_fiscal_identity'"), 1)
                    self.assertEqual(scalar("""
                        SELECT count(*) FROM (
                            SELECT 1 FROM earnings
                            WHERE is_predicted = FALSE AND fiscal_year IS NOT NULL
                              AND fiscal_quarter IS NOT NULL
                            GROUP BY symbol, market, fiscal_year, fiscal_quarter
                            HAVING count(*) > 1) duplicated_periods
                    """), 0)

                    # Second cold start on the same (still empty-of-duplicates)
                    # schema: must not raise and must not change the row count.
                    db.init_db()
                    seed_earnings_if_empty()
                    self.assertEqual(
                        scalar("SELECT count(*) FROM earnings"), len(DEMO_EARNINGS_ROWS))

                joined = "\n".join(db_logs.output)
                self.assertIn("fiscal identity index present", joined)
                self.assertNotIn("UniqueViolation", joined)
            finally:
                conn.rollback()

            with conn.cursor() as check:
                check.execute(
                    "SELECT count(*) FROM information_schema.schemata WHERE schema_name=%s",
                    (SCHEMA,))
                self.assertEqual(check.fetchone()[0], 0,
                                 "the self-test schema must not survive the rollback")


if __name__ == "__main__":
    unittest.main()
