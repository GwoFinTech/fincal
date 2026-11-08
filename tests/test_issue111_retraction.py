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
        # A withdrawal names the row it belongs to: without the report type, a
        # blank on one event could clear a same-day row of another type.
        assert "AND e.report_type = v.report_type" in sql
        assert "revenue_actual = CASE WHEN v.drop_revenue THEN NULL" in sql

    def test_the_whole_attribution_goes_when_the_last_actual_goes(self):
        # A cleared row must not keep saying which currency its (gone) actual was in.
        sql = " ".join(sync_earnings._RETRACTION_SQL.split())

        for column in ("actual_as_of", "actual_currency", "actual_basis", "actual_source"):
            assert f"{column} = CASE WHEN v.drop_eps AND v.drop_revenue THEN NULL" in sql, column

    def test_the_withdrawal_is_settled_after_the_whole_payload_is_read(self):
        # Deciding per batch let an event in one batch blank a value another batch
        # had just stated: the row flapped between reported and scheduled every run.
        source = Path(sync_earnings.__file__).read_text(encoding="utf-8")
        loop = source[source.index("for market in [\"US\", \"HK\"]:"):source.index("if run_stats.unresolved:")]
        final_flush = loop.index('f"{market} final batch"')
        retract_at = loop.index("_retract(run_stats, retractions, stated)")

        assert final_flush < retract_at


class WiringTests(TestCase):
    """The market's withdrawals are applied through one statement after its pages."""

    class _Cursor:
        def __init__(self, cleared):
            self.cleared = cleared
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.calls.append(" ".join(str(sql).split()))

        def fetchone(self):
            return {"n": 0}

    def test_the_run_records_what_was_cleared(self):
        stats = sync_earnings.SyncStats()
        cursor = self._Cursor([
            {"symbol": "BBAR", "market": "US",
             "report_date": "2026-11-24", "report_type": "Q"},
        ])
        withdrawn = {("BBAR", "US", "2026-11-24", "Q"): {"eps": False, "revenue": True}}

        with patch.object(sync_earnings, "db_cursor", lambda *a, **k: cursor), \
                patch.object(sync_earnings, "apply_retractions",
                             side_effect=lambda cur, effective: [
                                 "BBAR.US 2026-11-24 Q"]):
            sync_earnings._retract(stats, withdrawn, {})

        assert stats.retracted == 1
        assert stats.retracted_keys == ["BBAR.US 2026-11-24 Q"]

    def test_a_contradicted_withdrawal_issues_no_statement(self):
        stats = sync_earnings.SyncStats()
        withdrawn = {("GLRE", "US", "2026-08-04", "Q"): {"eps": True, "revenue": True}}
        stated = {("GLRE", "US", "2026-08-04", "Q"): {"eps": True, "revenue": True}}

        with patch.object(sync_earnings, "db_cursor") as cursor:
            sync_earnings._retract(stats, withdrawn, stated)

        cursor.assert_not_called()
        assert stats.retracted == 0

    def test_the_details_carry_the_count(self):
        details = sync_earnings.SyncStats(written=10, retracted=2).quality().to_dict()

        assert details["retracted"] == 2


class TheCrossProviderGuardTests(TestCase):
    """A provider may only withdraw the value it supplied (Issue #91's principle).

    The first production run cleared one Futu-written US actual because
    Longbridge served the field blank: the blank says nothing about Futu's figure.
    """

    def test_the_statement_restricts_retraction_to_the_same_supplier(self):
        sql = " ".join(sync_earnings._RETRACTION_SQL.split())

        assert "e.actual_source IS NULL OR e.actual_source = 'longbridge'" in sql

    def test_the_regression_is_named_in_the_sql(self):
        # A guard without a reason gets deleted by the next cleanup; keep both.
        assert "#91" in sync_earnings._RETRACTION_SQL

class ReturnedRowShapeTests(TestCase):
    """`db_cursor()` is a RealDictCursor, so a RETURNING row is a mapping."""

    def test_a_mapping_row_renders_its_key(self):
        key = sync_earnings._display_key({
            "symbol": "BBAR", "market": "US",
            "report_date": "2026-11-24", "report_type": "Q",
        })

        assert key == "BBAR.US 2026-11-24 Q"

    def test_a_tuple_row_still_renders(self):
        # Regression: indexing a dict positionally raised KeyError(0), and the
        # batch isolation reported it as a failure whose message was "0".
        assert sync_earnings._display_key(("BBAR", "US", "2026-11-24", "Q")) == "BBAR.US 2026-11-24 Q"

class ContradictionTests(TestCase):
    """A payload that states a value cannot also be read as withdrawing it."""

    def test_a_stated_value_vetoes_the_blank_for_the_same_row(self):
        withdrawn = {("GLRE", "US", "2026-08-04", "Q"): {"eps": True, "revenue": True}}
        stated = {("GLRE", "US", "2026-08-04", "Q"): {"eps": True, "revenue": True}}

        assert sync_earnings.effective_retractions(withdrawn, stated) == {}

    def test_a_side_nothing_contradicts_is_still_withdrawn(self):
        withdrawn = {("GLRE", "US", "2026-08-04", "Q"): {"eps": True, "revenue": True}}
        stated = {("GLRE", "US", "2026-08-04", "Q"): {"eps": True, "revenue": False}}

        assert sync_earnings.effective_retractions(withdrawn, stated) == {
            ("GLRE", "US", "2026-08-04", "Q"): {"eps": False, "revenue": True}
        }

    def test_another_row_stating_a_value_vetoes_nothing(self):
        withdrawn = {("GLRE", "US", "2026-08-04", "Q"): {"eps": True, "revenue": False}}
        stated = {("GLRE", "US", "2026-08-04", "H"): {"eps": True, "revenue": True}}

        assert sync_earnings.effective_retractions(withdrawn, stated) == withdrawn

    def test_an_uncontradicted_withdrawal_passes_through(self):
        withdrawn = {("BBAR", "US", "2026-11-24", "Q"): {"eps": False, "revenue": True}}

        assert sync_earnings.effective_retractions(withdrawn, {}) == withdrawn
