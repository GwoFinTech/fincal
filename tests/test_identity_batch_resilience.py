"""One colliding fiscal identity must not discard the rest of its batch.

The Longbridge batch is a single ``execute_values`` statement, and the table
carries ``idx_earnings_fiscal_identity`` — UNIQUE (symbol, market, fiscal_year,
fiscal_quarter) on confirmed rows.  A row whose period is already owned by a
stored row at another ``(report_date, report_type)`` therefore violates a
constraint the INSERT's own ``ON CONFLICT (symbol, market, report_date,
report_type)`` target does not cover, and *the whole statement* used to fail.

Once Issue #90 made the sync fetch whole earnings days, that stopped being a
corner case: a production run lost 14 batches of 200 records (2,800 rows) and
reported them only as "batch failed".  The root data problem is Issue #87 (the
same period written as two rows on one day); this pins the write-side
containment — per-row savepoints, so the colliding rows are skipped by name and
the other ~199 land.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2.errors

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SYNC_EARNINGS = ROOT / "scripts" / "sync_earnings.py"

INSERT_WIDTH = 16


def _load_sync_earnings():
    spec = importlib.util.spec_from_file_location("sync_earnings_batch_resilience", SYNC_EARNINGS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(symbol: str, quarter: int = 2):
    """A batch row in the exact tuple layout ``flush_batch`` writes."""
    row = [
        symbol, "US", f"{symbol} Inc", "2026-08-20", "Q",
        2026, quarter,
        1.0, 1.2, 100.0, 110.0, "after",
        "USD", "USD", "unknown", "unknown",
    ]
    assert len(row) == INSERT_WIDTH
    return tuple(row)


def _cursor():
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = None
    return cursor


def test_a_poison_row_only_costs_itself():
    module = _load_sync_earnings()
    cursor = _cursor()
    rows = [_row("GOODA"), _row("POISON"), _row("GOODB")]
    attempted = []

    def fake_execute_values(cur, sql, values, page_size=None):
        values = list(values)
        attempted.append(len(values))
        if "INSERT INTO earnings " not in sql:
            return
        # The real constraint: this batch statement collides as a whole, and
        # only the poison row collides when written alone.
        if len(values) > 1 or values[0][0] == "POISON":
            raise psycopg2.errors.UniqueViolation()

    with patch("psycopg2.extras.execute_values", fake_execute_values):
        stats = module.flush_batch(cursor, rows)

    assert attempted[0] == 3, "the batch is attempted as one statement first"
    assert stats.rows == 2, "the two clean rows must still be written"
    assert stats.identity_skipped == 1, "the colliding row is counted, not hidden"


def test_the_skipped_row_is_named_in_the_log():
    module = _load_sync_earnings()
    cursor = _cursor()

    def fake_execute_values(cur, sql, values, page_size=None):
        values = list(values)
        if "INSERT INTO earnings " in sql and (len(values) > 1 or values[0][0] == "POISON"):
            raise psycopg2.errors.UniqueViolation()

    with patch("psycopg2.extras.execute_values", fake_execute_values), \
         patch.object(module.logger, "warning") as warned:
        module.flush_batch(cursor, [_row("GOODA"), _row("POISON")])

    messages = " ".join(str(call.args) % call.args if False else str(call.args)
                        for call in warned.call_args_list)
    assert "POISON" in messages, "an operator must be able to find the colliding period"
    assert "another confirmed row already holds this fiscal period" in messages


def test_a_clean_batch_still_takes_the_single_statement_path():
    module = _load_sync_earnings()
    cursor = _cursor()
    sizes = []

    def fake_execute_values(cur, sql, values, page_size=None):
        values = list(values)
        if "INSERT INTO earnings " in sql:
            sizes.append(len(values))

    with patch("psycopg2.extras.execute_values", fake_execute_values):
        stats = module.flush_batch(cursor, [_row("AA"), _row("BB")])

    assert sizes == [2], "no per-row retry when nothing collides"
    assert stats.rows == 2 and stats.identity_skipped == 0
