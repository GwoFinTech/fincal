"""Issue #111, second half: a value the provider withdrew must stop being shown.

`_LONGBRIDGE_UPSERT` is all `COALESCE(EXCLUDED.x, earnings.x)` — additive by design,
so a provider that stops serving a figure cannot take it back.  BBAR's FY2026Q3 row
kept `revenue_actual = 1,369,164,371` (vs an 847,280,367 estimate, +61.6%) for weeks
after Longbridge stopped returning it, and `date_status` kept saying `reported`.

The fix reads the withdrawal the way the payload states it: the field is *present*
with an empty `value_raw`.  Everything else — a field that is merely absent because
the window does not cover the event — must leave the stored value alone, otherwise a
shrunken window would wipe the table.
"""
import importlib.util
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync_earnings = _load("sync_earnings_retraction", "scripts/sync_earnings.py")


class ReadingTheWithdrawalTests(TestCase):
    def test_an_explicitly_blank_actual_is_a_withdrawal(self):
        # The exact shape from the BBAR payload.
        self.assertEqual(
            sync_earnings.extract_retractions([{"type": "actual_revenue", "value_raw": ""}]),
            {"eps": False, "revenue": True},
        )

    def test_whitespace_counts_as_blank(self):
        self.assertEqual(
            sync_earnings.extract_retractions([{"type": "actual_eps", "value_raw": "  "}]),
            {"eps": True, "revenue": False},
        )

    def test_an_absent_field_is_not_a_withdrawal(self):
        # A window that does not cover the event simply has no field for it.
        self.assertEqual(
            sync_earnings.extract_retractions(
                [{"type": "estimate_revenue", "value_raw": "847280367.5"}]),
            {"eps": False, "revenue": False},
        )
        self.assertEqual(sync_earnings.extract_retractions([]),
                         {"eps": False, "revenue": False})
        self.assertEqual(sync_earnings.extract_retractions(None),
                         {"eps": False, "revenue": False})

    def test_a_real_zero_is_a_value_not_a_withdrawal(self):
        # 2068.HK legitimately reported 0.00 EPS; "0.000000" must never clear it.
        self.assertEqual(
            sync_earnings.extract_retractions([{"type": "actual_eps", "value_raw": "0.000000"}]),
            {"eps": False, "revenue": False},
        )

    def test_a_real_value_is_not_a_withdrawal(self):
        self.assertEqual(
            sync_earnings.extract_retractions([{"type": "actual_eps", "value_raw": "1.9"}]),
            {"eps": False, "revenue": False},
        )


class TheStatementTests(TestCase):
    def test_the_retraction_only_touches_confirmed_rows_that_hold_a_value(self):
        sql = " ".join(sync_earnings._RETRACTION_SQL.split())

        assert "e.is_predicted = FALSE" in sql
        assert "AND e.eps_actual IS NOT NULL" in sql
        assert "AND e.revenue_actual IS NOT NULL" in sql
        assert "eps_actual = CASE WHEN v.drop_eps THEN NULL" in sql
        assert "revenue_actual = CASE WHEN v.drop_revenue THEN NULL" in sql

    def test_the_whole_attribution_goes_when_the_last_actual_goes(self):
        # A cleared row must not keep saying which currency its (gone) actual was in.
        sql = " ".join(sync_earnings._RETRACTION_SQL.split())

        for column in ("actual_as_of", "actual_currency", "actual_basis", "actual_source"):
            assert f"{column} = CASE WHEN v.drop_eps AND v.drop_revenue THEN NULL" in sql, column

    def test_it_runs_before_the_status_is_recomputed(self):
        # The provenance UPDATE derives `date_status` from the stored actuals, so a
        # retraction applied after it would leave the row saying `reported`.
        source = Path(sync_earnings.__file__).read_text(encoding="utf-8")
        body = source[source.index("def flush_batch("):source.index("def _count_unlabeled_keys")]
        retraction_at = body.index("_RETRACTION_SQL")
        status_at = body.index("date_status = CASE WHEN (e.eps_actual IS NOT NULL")

        assert retraction_at < status_at


class WiringTests(TestCase):
    """The batch's withdrawals have to reach the flush that writes the batch."""

    def test_the_run_counters_see_what_the_flush_retracted(self):
        stats = sync_earnings.SyncStats()
        seen = {}

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                self.sql = " ".join(str(sql).split())

            def fetchone(self):
                return {"n": 0}

        def fake_flush(cur, rows, retractions=None):
            seen["retractions"] = retractions
            return sync_earnings.FlushStats(rows=len(rows), retracted=1)

        batch = [
            ("BBAR", "US", "BBAR", "2026-11-24", "Q", 2026, 3, None, 1.9, None, 1_369_164_371,
             "after", "USD", "USD", "longbridge", "longbridge"),
        ]
        withdrawals = {("BBAR", "US", "2026-11-24"): {"eps": False, "revenue": True}}

        with patch.object(sync_earnings, "db_cursor", lambda *a, **k: _Cursor()), \
                patch.object(sync_earnings, "flush_batch", side_effect=fake_flush):
            sync_earnings._flush_batch(stats, batch, "test batch", withdrawals)

        assert seen["retractions"] == withdrawals, "the withdrawal must reach the flush"
        assert stats.retracted == 1
        assert stats.written == 1

    def test_the_details_carry_the_count(self):
        details = sync_earnings.SyncStats(written=10, retracted=2).quality().to_dict()

        assert details["retracted"] == 2
