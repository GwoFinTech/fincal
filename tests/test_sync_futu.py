"""Regression tests for Issue #39: Futu sync write paths must record
provenance so reported events are not mislabeled as ``unknown``/``scheduled``.

Root cause: ``scripts/sync_futu.py`` wrote ``date_source``/``date_status`` and
``actual_source`` nowhere, so every Futu-sourced row kept the column defaults
(``unknown`` + ``scheduled``) even when actuals were present — roughly 8% of
reported events were mislabeled and lost source attribution.

Fix: the dates upsert now writes ``date_source='futu'`` (and a correct
``date_status``), and the actuals updates write ``date_status='reported'`` +
``actual_source='futu'``.
"""
import importlib.util
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location("sync_futu", ROOT / "scripts" / "sync_futu.py")
sync_futu = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_futu)


def _db_mock(cursor):
    """A ``db_cursor`` replacement whose ``with`` block yields ``cursor``."""
    ctx = MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    return patch.object(sync_futu, "db_cursor", return_value=ctx)


class _RecordingCursor:
    """Records every ``execute(sql, params)`` call for later assertions."""

    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


class FutuDatesProvenanceTests(TestCase):
    """The earnings-dates upsert must carry Futu provenance."""

    def setUp(self):
        import pandas as pd

        self.ctx = MagicMock()
        df = pd.DataFrame(
            [{"fiscal_year": 2026, "financial_type": 2,
              "pub_trading_day_str": "2026-07-30", "pub_type": 1}]
        )
        self.ctx.get_financials_earnings_price_history.return_value = (0, df)
        self._src = MagicMock()
        self._src.get_futu_symbols.return_value = ["AAPL.US"]
        self._cursor = _RecordingCursor()
        self._batch = []
        self._sql = ""

        def fake_execute_values(cur, sql, argslist, page_size=200):
            self._batch = list(argslist)
            self._sql = sql

        self._ev_patch = patch("psycopg2.extras.execute_values",
                               side_effect=fake_execute_values)

    def test_dates_upsert_writes_futu_source(self):
        with patch.object(sync_futu, "get_source", return_value=self._src), \
             patch.object(sync_futu, "check_cancelled"), \
             _db_mock(self._cursor), self._ev_patch:
            sync_futu.sync_earnings_dates(self.ctx, 1)

        # The batch rows must carry ('futu', 'scheduled') as the last two fields.
        assert self._batch, "expected at least one upsert row"
        row = self._batch[0]
        assert row[-2:] == ("futu", "scheduled"), (
            "dates batch rows must set date_source='futu', date_status='scheduled'"
        )
        # The INSERT column list and the conflict-update must both carry provenance.
        assert "date_source" in self._sql and "date_status" in self._sql
        assert "date_source = 'futu'" in self._sql
        assert "date_status = CASE" in self._sql


class FutuActualsProvenanceTests(TestCase):
    """The actuals updates must flip status to reported and attribute Futu."""

    def setUp(self):
        self.ctx = MagicMock()
        # First call (EPS, statement_type=4) then second (revenue, statement_type=1).
        self.ctx.get_financials_statements.side_effect = [
            (0, {"report_list": [{"fiscal_year": 2026, "financial_type": 2,
                                 "item_list": [{"field_id": 14020, "data": 1.23}]}]}),
            (0, {"report_list": [{"fiscal_year": 2026, "financial_type": 2,
                                 "item_list": [{"field_id": 8002, "data": 123.0}]}]}),
        ]
        self._src = MagicMock()
        self._src.get_futu_symbols.return_value = ["AAPL.US"]
        self._cursor = _RecordingCursor()

    def test_actuals_updates_set_reported_and_futu_source(self):
        with patch.object(sync_futu, "get_source", return_value=self._src), \
             patch.object(sync_futu, "check_cancelled"), \
             _db_mock(self._cursor):
            sync_futu.sync_actuals(self.ctx, 1)

        updates = [sql for sql, _ in self._cursor.executed
                   if sql.strip().startswith("UPDATE earnings")]
        assert len(updates) >= 2, "expected EPS and revenue updates to run"
        for sql in updates:
            assert "date_status = 'reported'" in sql, (
                "actuals update must mark the event reported"
            )
            assert "actual_source = 'futu'" in sql, (
                "actuals update must attribute the actuals to Futu"
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
