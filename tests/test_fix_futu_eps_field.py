"""Issue #62: the one-time retraction of the wrong Futu ``eps_actual`` values.

The fix in ``scripts/sync_futu.py`` stops writing the key-metrics 流动比率 field
as the actual EPS; this covers ``scripts/fix_futu_eps_field.py``, which retracts
the values already in the table (312 rows over 76 symbols in production) and
refills them from the corrected field, reusing the real sync stage under its own
audited run.
"""
import contextlib
import importlib.util
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "fix_futu_eps_field", ROOT / "scripts" / "fix_futu_eps_field.py")
assert SPEC is not None and SPEC.loader is not None
fix_fix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fix_fix)

sync_futu = fix_fix.sync_futu


@contextlib.contextmanager
def process_args(args):
    """Run the script's ``main()`` with a synthetic ``sys.argv``."""
    with patch.object(sys, "argv", ["fix_futu_eps_field.py", *args]):
        yield


class _RecordingCursor:
    """Records ``execute`` calls and answers the script's single-row SELECTs."""

    def __init__(self, row=None):
        self.executed = []
        self.rowcount = 3
        self._row = row or {"n": 312}

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []

    def fetchone(self):
        return dict(self._row)


def _db_mock(cursor):
    ctx = MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    return patch.object(fix_fix, "db_cursor", return_value=ctx)


def _row(symbol="AAPL", market="US", fy=2026, fq=3, eps="1.003295"):
    return {"id": 1, "symbol": symbol, "market": market, "fiscal_year": fy,
            "fiscal_quarter": fq, "report_date": None, "eps_actual": eps,
            "eps_estimate": "1.89243", "actual_source": "futu", "actual_as_of": None}


class AffectedScopeTests(TestCase):
    """Only rows whose EPS actually came from the wrong field are touched."""

    def test_the_predicate_selects_futu_written_eps_only(self):
        predicate = fix_fix.AFFECTED_PREDICATE
        assert "actual_source = 'futu'" in predicate
        assert "eps_actual IS NOT NULL" in predicate
        assert "revenue_actual" not in predicate, (
            "the revenue field was correct — it must not be part of the retraction"
        )

    def test_symbols_use_the_watchlist_spelling_each_market_expects(self):
        rows = [_row(), _row(symbol="0700.HK", market="HK")]
        assert fix_fix.affected_symbols(rows) == ["0700.HK", "AAPL.US"]


class ProviderComparisonTests(TestCase):
    """``--verify`` must compare against the EPS field, not another metric."""

    def test_statement_map_reads_basic_eps_and_falls_back_to_diluted(self):
        payload = {"report_list": [
            {"fiscal_year": 2026, "financial_type": 3, "item_list": [
                {"field_id": 8047, "display_name": "基本每股收益", "data": 2.03},
                {"field_id": 14020, "display_name": "流动比率", "data": 1.003295},
            ]},
            {"fiscal_year": 2026, "financial_type": 2, "item_list": [
                {"field_id": 8048, "display_name": "稀释每股收益", "data": 2.01},
            ]},
        ]}
        assert fix_fix.statement_eps_map(payload) == {(2026, 3): 2.03, (2026, 2): 2.01}

    def test_statement_map_ignores_a_drifted_label(self):
        payload = {"report_list": [{
            "fiscal_year": 2026, "financial_type": 3,
            "item_list": [{"field_id": 8047, "display_name": "流动比率", "data": 1.003295}],
        }]}
        assert fix_fix.statement_eps_map(payload) == {}

    def test_compare_rows_separates_matches_from_mismatches(self):
        rows = [_row(eps="2.03"), _row(symbol="OKLO", fy=2026, fq=1, eps="59.932626")]
        provider = {
            ("AAPL", "US"): {(2026, 3): 2.03},
            ("OKLO", "US"): {(2026, 1): -0.19},
        }
        matched, mismatched, unverified = fix_fix.compare_rows(rows, provider)

        assert [row["symbol"] for row in matched] == ["AAPL"]
        assert [row["symbol"] for row in mismatched] == ["OKLO"]
        assert mismatched[0]["reason"] == "value_differs"
        assert mismatched[0]["provider_eps"] == -0.19
        assert unverified == []

    def test_a_period_the_provider_does_not_report_is_a_mismatch(self):
        matched, mismatched, _unverified = fix_fix.compare_rows(
            [_row(fy=2019, fq=1)], {("AAPL", "US"): {(2026, 3): 2.03}})

        assert matched == []
        assert mismatched[0]["reason"] == "provider_has_no_eps_for_period"

    def test_a_refused_symbol_is_unverified_not_a_mismatch(self):
        """A quota rejection must not be reported as a data difference."""
        rows = [_row()]
        matched, mismatched, unverified = fix_fix.compare_rows(
            rows, provider={}, unreadable=(("AAPL", "US"),))

        assert matched == [] and mismatched == []
        assert unverified[0]["reason"] == "provider_unavailable_for_symbol"

    def test_fetch_provider_eps_marks_a_refused_symbol_unreadable(self):
        ctx = MagicMock()
        ctx.get_financials_statements.return_value = (
            -1, "获取财务报表频率太高，请求失败，每30秒最多30次。")
        with patch.object(sync_futu, "create_futu_context", return_value=ctx), \
             patch.object(sync_futu.config, "FUTU_RATE_LIMIT_MAX_RETRIES", 0):
            provider, unreadable = fix_fix.fetch_provider_eps(["AAPL.US"])

        assert provider == {}
        assert unreadable == [("AAPL", "US")]
        assert ctx.close.called, "the read-only probe must release its OpenD session"

    def test_verify_reports_mismatches_without_writing(self):
        rows = [_row()]
        with patch.object(fix_fix, "fetch_provider_eps",
                          return_value=({("AAPL", "US"): {(2026, 3): 2.03}}, [])), \
             patch.object(fix_fix, "db_cursor") as db:
            code = fix_fix.verify(rows)

        assert code == 1, "a poisoned value must not verify against 基本每股收益"
        db.assert_not_called()

    def test_verify_passes_once_the_rows_match(self):
        rows = [_row(eps="2.03")]
        with patch.object(fix_fix, "fetch_provider_eps",
                          return_value=({("AAPL", "US"): {(2026, 3): 2.03}}, [])), \
             patch.object(fix_fix, "db_cursor") as db:
            code = fix_fix.verify(rows)

        assert code == 0
        db.assert_not_called()

    def test_verify_cannot_pass_without_provider_values(self):
        with patch.object(fix_fix, "fetch_provider_eps", return_value=({}, [])):
            assert fix_fix.verify([_row()]) == 2, (
                "an unreachable provider must not be reported as a pass"
            )

    def test_verify_cannot_pass_on_a_partial_read(self):
        with patch.object(fix_fix, "fetch_provider_eps",
                          return_value=({}, [("AAPL", "US")])):
            assert fix_fix.verify([_row()]) == 2, (
                "a row the provider refused is unknown, not verified"
            )


class RetractTests(TestCase):
    """Retraction is snapshot-first, value-clearing and reversible."""

    def _retract(self):
        cursor = _RecordingCursor(row={"n": 2})
        with _db_mock(cursor):
            result = fix_fix.retract([1, 2])
        return cursor, result

    def test_the_value_is_cleared_and_the_reason_recorded(self):
        cursor, result = self._retract()
        updates = [(sql, params) for sql, params in cursor.executed
                   if sql.strip().startswith("UPDATE earnings")]

        assert result == (2, 3)
        assert len(updates) == 1
        sql, params = updates[0]
        assert "eps_actual = NULL" in sql
        assert fix_fix.RETRACTED_ACTUAL_SOURCE in params
        assert "revenue_actual" not in sql, "the correct revenue value stays untouched"
        assert "date_status" not in sql, "the reported status is not rewritten"

    def test_rows_are_snapshotted_before_they_change(self):
        cursor, _result = self._retract()
        statements = [sql for sql, _ in cursor.executed]

        assert statements[0].startswith(f"DROP TABLE IF EXISTS {fix_fix.BACKUP_TABLE}")
        assert f"CREATE TABLE {fix_fix.BACKUP_TABLE} AS" in statements[1]
        assert "eps_actual" in statements[1]

    def test_the_retracted_marker_stays_replaceable_by_the_corrected_sync(self):
        from app.provenance import REPLACEABLE_ACTUAL_SOURCES

        assert fix_fix.RETRACTED_ACTUAL_SOURCE in REPLACEABLE_ACTUAL_SOURCES
        assert f"'{fix_fix.RETRACTED_ACTUAL_SOURCE}'" in sync_futu._REPLACEABLE_SOURCES_SQL


class RefillTests(TestCase):
    """The refill reuses the corrected stage, audited as its own run."""

    def test_refill_calls_the_real_stage_and_finishes_the_run(self):
        ctx = MagicMock()
        stats = sync_futu.FutuStageStats(total=2, failed_symbols=1)
        finished = []

        with patch.object(sync_futu, "create_futu_context", return_value=ctx), \
             patch.object(fix_fix, "start_run", return_value=77) as start, \
             patch.object(fix_fix, "finish_run",
                          side_effect=lambda run_id, **kw: finished.append((run_id, kw))), \
             patch.object(sync_futu, "sync_actuals", return_value=stats) as actuals:
            result = fix_fix.refill(["AAPL.US", "OKLO.US"])

        assert result is stats
        actuals.assert_called_once_with(ctx, 77, ["AAPL.US", "OKLO.US"])
        assert ctx.close.called
        run_id, kwargs = finished[0]
        assert run_id == 77
        assert kwargs["status"] == "failed", "the audit follows the stage's own outcome"
        assert kwargs["details"]["issue"] == 62
        assert kwargs["details"]["actual_failed_symbols"] == 1
        assert start.call_args.args[0] == fix_fix.REFILL_STAGE != "futu", (
            "a partial repair run must not refresh the weekly stage's freshness"
        )

    def test_refill_skips_without_opend(self):
        with patch.object(sync_futu, "create_futu_context", return_value=None), \
             patch.object(fix_fix, "start_run") as start:
            assert fix_fix.refill(["AAPL.US"]) is None

        start.assert_not_called()

    def test_refill_is_a_noop_without_symbols(self):
        with patch.object(sync_futu, "create_futu_context") as create:
            assert fix_fix.refill([]) is None

        create.assert_not_called()


class CliTests(TestCase):
    """Flag handling: dry run reads, --apply writes, --verify is read-only."""

    def test_a_dry_run_never_writes(self):
        cursor = _RecordingCursor()
        with _db_mock(cursor), patch.object(fix_fix, "init_db"), process_args([]):
            code = fix_fix.main()

        assert code == 0
        assert not [sql for sql, _ in cursor.executed if sql.strip().startswith("UPDATE")]

    def test_verify_only_reads(self):
        with patch.object(fix_fix, "init_db"), \
             patch.object(fix_fix, "load_affected", return_value=[_row()]), \
             patch.object(fix_fix, "fetch_provider_eps",
                          return_value=({("AAPL", "US"): {(2026, 3): 2.03}}, [])), \
             patch.object(fix_fix, "db_cursor") as db, \
             process_args(["--verify"]):
            code = fix_fix.main()

        assert code == 1, "the poisoned value does not match 基本每股收益"
        db.assert_not_called()

    def test_apply_retracts_under_the_futu_advisory_lock(self):
        cursor = _RecordingCursor(row={"n": 0})
        locked = []

        @contextlib.contextmanager
        def fake_lock(key, **kwargs):
            locked.append(key)
            yield True

        with _db_mock(cursor), patch.object(fix_fix, "init_db"), \
             patch.object(fix_fix, "advisory_lock", fake_lock), \
             patch.object(fix_fix, "load_affected", return_value=[]), \
             patch.object(fix_fix, "state", return_value={}), \
             patch.object(fix_fix, "refill") as refill, \
             process_args(["--apply"]):
            code = fix_fix.main()

        assert code == 0
        assert locked, "the retraction must not race a scheduled Futu sync"
        refill.assert_called_once_with([])

    def test_apply_aborts_when_another_futu_sync_holds_the_lock(self):
        @contextlib.contextmanager
        def busy_lock(key, **kwargs):
            yield False

        with patch.object(fix_fix, "init_db"), \
             patch.object(fix_fix, "advisory_lock", busy_lock), \
             patch.object(fix_fix, "retract") as retract, process_args(["--apply"]):
            code = fix_fix.main()

        assert code == 1
        retract.assert_not_called()
