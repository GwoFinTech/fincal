"""Issue #147: 中概股/ADR 的「币种不一致」不再让 EPS 对比变「—」。

背景（生产实测 2026-11-21）：日历的预期来自 Longbridge，港股按挂牌币种 HKD 报价、美股按
USD 报价（且经过换算），而实际值来自 Futu 报表、是**记账**币种（中资发行人是 CNY）。
行级规则 `fiscal.comparison_unavailable_reason` 正确地拒绝相减，于是 0700.HK
2026Q3、9988.HK 2027Q2、9868.HK、0981.HK、BABA、PDD 全部显示「—」。

解法不是汇率换算：把 Longbridge 的 HKD 值与 Futu 的 CNY 值逐期对照，隐含汇率在
1.11 / 1.15 / 1.155 之间漂移（同期市场汇率只有一个值），说明两边不只差币种。真正同源的是
提供方自己的 `financials/estimate`：它返回**同一记账币种**下的 `consensus_mean` 与
`actual_value`（按挂牌单位，ADR 为每 ADS），才是一对可以相减的数。

这里钉住：那一对的解析、写库、只在该用的时候被采纳、营收不被牵连，以及面板 JS 的口径。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fiscal, futu_webapi  # noqa: E402

APP_JS = ROOT / "app" / "static" / "assets" / "app-setup.js"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SYNC_FUTU = _load("sync_futu_pairs_under_test", ROOT / "scripts" / "sync_futu.py")


# ── 传输：提供方那一对的形状 ──────────────────────────────────────────────
class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_financials_estimate(self, code, period_type="QUARTERLY"):
        self.calls.append((code, period_type))
        return 0, self.payload


ESTIMATE_PAYLOAD = {
    "currency": "CNY",
    "period_type": "QUARTERLY",
    "history": [
        {"fiscal_year": 2026, "fiscal_quarter": 3, "consensus": 6.2873, "actual": 6.947,
         "num_analysts": 7, "period_str": "2026/Q3"},
        {"fiscal_year": 2026, "fiscal_quarter": 2, "consensus": 6.2487, "actual": 6.104,
         "num_analysts": 7, "period_str": "2026/Q2"},
    ],
    "future": [{"fiscal_year": 2026, "fiscal_quarter": 4, "consensus": 5.1936,
                "actual": None, "num_analysts": 7, "period_str": "2026/Q4"}],
}


class _StubLimiter:
    """``futu_call`` paces then despatches; this stub records nothing and waits never."""

    def acquire(self):
        return 0.0

    def cool_down(self):
        pass


class TestEstimateTransport:
    def test_numbers_are_coerced_and_null_stays_null(self):
        raw = {"currency": "cny", "historical_estimates": [
            {"fiscal_year": 2026, "fiscal_quarter": 3, "consensus_mean": "6.2873",
             "actual_value": "6.947", "num_analysts": 7, "period_str": "2026/Q3"},
        ], "future_estimates": [
            {"fiscal_year": 2026, "fiscal_quarter": 4, "consensus_mean": "5.19",
             "num_analysts": 2},
        ]}
        period = futu_webapi._estimate_period(raw["historical_estimates"][0])
        assert period["consensus"] == pytest.approx(6.2873)
        assert period["actual"] == pytest.approx(6.947)
        assert period["num_analysts"] == 7
        assert futu_webapi._estimate_period({"fiscal_year": 2026})["consensus"] is None
        assert futu_webapi._estimate_period({"fiscal_year": 2026})["actual"] is None

    def test_a_missing_actual_does_not_become_zero(self):
        assert futu_webapi._estimate_period({"actual_value": None})["actual"] is None
        assert futu_webapi._estimate_period({"actual_value": ""})["actual"] is None


class TestFetchPairs:
    def _call(self, client, stats=None):
        stats = stats or SYNC_FUTU.FutuStageStats()
        return SYNC_FUTU.fetch_provider_pairs(client, "HK.00700", _StubLimiter(), stats, 20)

    def test_a_complete_pair_is_emitted_per_period(self):
        rows, outcome = self._call(_FakeClient(ESTIMATE_PAYLOAD))
        assert outcome == SYNC_FUTU.OUTCOME_OK
        assert [row[:2] for row in rows] == [(2026, 3), (2026, 2)]
        assert rows[0][2] == "CNY" and rows[0][3] == pytest.approx(6.2873)
        assert rows[0][4] == pytest.approx(6.947) and rows[0][5] == 7

    def test_a_future_period_without_an_actual_is_not_a_pair(self):
        """`future_estimates` carry no actual — writing them would fake a comparison."""
        payload = {"currency": "CNY", "history": [], "future": ESTIMATE_PAYLOAD["future"]}
        rows, outcome = self._call(_FakeClient(payload))
        assert rows == [] and outcome == SYNC_FUTU.OUTCOME_OK

    def test_without_a_currency_the_pair_is_withheld(self):
        payload = dict(ESTIMATE_PAYLOAD, currency=None)
        rows, _outcome = self._call(_FakeClient(payload))
        assert rows == []

    def test_an_unsupported_symbol_is_counted_not_failed(self):
        class _Unsupported:
            def get_financials_estimate(self, code, period_type="QUARTERLY"):
                return -1, "Futu WebAPI HTTP 200, code -10: no data"

        stats = SYNC_FUTU.FutuStageStats()
        rows, outcome = SYNC_FUTU.fetch_provider_pairs(
            _Unsupported(), "US.SPY", _StubLimiter(), stats, 20)
        assert rows == [] and outcome == SYNC_FUTU.OUTCOME_UNSUPPORTED

    def test_flush_counts_rows_and_symbols(self):
        stats = SYNC_FUTU.FutuStageStats()
        executed = []

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, sql, params=None):
                executed.append((sql, params))

        original = SYNC_FUTU.db_cursor
        SYNC_FUTU.db_cursor = lambda: _Cursor()
        try:
            rows, _outcome = self._call(_FakeClient(ESTIMATE_PAYLOAD))
            written = SYNC_FUTU.flush_provider_pairs("0700.HK", "HK", rows, stats)
        finally:
            SYNC_FUTU.db_cursor = original
        assert written == 2 and stats.pair_rows == 2 and stats.pair_symbols == 1
        assert all("earnings_provider_pairs" in sql for sql, _ in executed)
        assert executed[0][1][:4] == ("0700.HK", "HK", 2026, 3)


# ── 采纳规则 ─────────────────────────────────────────────────────────────
def _row(**over):
    row = {"symbol": "0700.HK", "market": "HK", "fiscal_year": 2026, "fiscal_quarter": 3,
           "eps_estimate": 7.3713, "eps_actual": 7.059,
           "estimate_currency": "HKD", "actual_currency": "CNY",
           "revenue_estimate": 247536800000.0, "revenue_actual": 207336000000.0,
           "estimate_source": "longbridge", "actual_source": "futu"}
    row.update(over)
    row.setdefault("comparison_unavailable_reason",
                   fiscal.comparison_unavailable_reason(row))
    return row


PAIR = {"currency": "CNY", "eps_consensus": 6.2873, "eps_actual": 6.947,
        "num_analysts": 7, "source": "futu"}


class TestAdoptionRule:
    def test_a_currency_mismatch_asks_for_the_provider_pair(self):
        row = _row()
        assert row["comparison_unavailable_reason"] == fiscal.COMPARISON_CURRENCY_MISMATCH
        assert fiscal.needs_provider_pair(row) is True

    def test_a_matching_currency_keeps_the_calendar_estimate(self):
        row = _row(actual_currency="HKD")
        assert fiscal.needs_provider_pair(row) is False

    def test_a_basis_mismatch_is_not_rescued_by_the_pair(self):
        row = _row(actual_currency="HKD", estimate_basis="adjusted", actual_basis="gaap")
        assert row["comparison_unavailable_reason"] == fiscal.COMPARISON_BASIS_MISMATCH
        assert fiscal.needs_provider_pair(row) is False

    def test_the_pair_is_adopted_with_its_currency_and_analysts(self):
        row = fiscal.attach_eps_pair(_row(), PAIR)
        assert row["eps_pair_estimate"] == pytest.approx(6.2873)
        assert row["eps_pair_actual"] == pytest.approx(6.947)
        assert row["eps_pair_currency"] == "CNY"
        assert row["eps_pair_analysts"] == 7
        # The row's own attribution is untouched: the revenue side still has to be
        # refused, and it is refused by the same code it was refused with before.
        assert row["estimate_currency"] == "HKD" and row["actual_currency"] == "CNY"
        assert fiscal.revenue_blocked(row) is True

    def test_half_a_pair_is_refused(self):
        row = _row()
        fiscal.attach_eps_pair(row, dict(PAIR, eps_actual=None))
        assert fiscal.row_eps_pair(row) is None

    def test_no_pair_leaves_the_row_untouched(self):
        row = _row()
        before = dict(row)
        fiscal.attach_eps_pair(row, None)
        assert row == before

    def test_row_eps_pair_is_the_one_shape_readers_use(self):
        pair = fiscal.row_eps_pair(fiscal.attach_eps_pair(_row(), PAIR))
        assert pair == {"estimate": pytest.approx(6.2873), "actual": pytest.approx(6.947),
                        "currency": "CNY", "num_analysts": 7, "source": "futu"}


# ── 面板 JS ─────────────────────────────────────────────────────────────
def _formatters_source() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function useFormatters()")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError("could not locate useFormatters()")


def _probe(rows: list[dict]) -> dict:
    probe = f"""
{_formatters_source()}
const fmt = useFormatters();
const rows = {json.dumps(rows)};
console.log(JSON.stringify(rows.map(row => ({{
  estimate: fmt.epsEstimate(row),
  actual: fmt.epsActual(row),
  surplus: fmt.epsSurplus(row),
  cls: fmt.epsSurplusClass(row),
  comparable: fmt.hasComparison(row, 'eps'),
  revenueComparable: fmt.hasComparison(row, 'revenue'),
  note: fmt.epsPairNote(row),
}}))));
"""
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


BLOCKED = {"eps_estimate": 7.3713, "eps_actual": 7.059,
           "estimate_currency": "HKD", "actual_currency": "CNY",
           "comparison_unavailable_reason": "currency_mismatch",
           "revenue_estimate": 247536800000.0, "revenue_actual": 207336000000.0,
           "eps_pair_estimate": 6.2873, "eps_pair_actual": 6.947,
           "eps_pair_currency": "CNY", "eps_pair_analysts": 7, "eps_pair_source": "futu"}


class TestPanelJs:
    def test_the_pair_replaces_both_sides_of_the_eps_cell(self):
        out = _probe([BLOCKED])[0]
        assert out["estimate"] == pytest.approx(6.2873)
        assert out["actual"] == pytest.approx(6.947)
        assert out["comparable"] is True
        assert out["cls"] == "beat"
        assert out["surplus"] == pytest.approx(0.1049, abs=1e-3)

    def test_revenue_stays_unavailable_because_the_provider_has_no_revenue_consensus(self):
        assert _probe([BLOCKED])[0]["revenueComparable"] is False

    def test_the_note_names_the_currency_the_analysts_and_the_revenue_gap(self):
        note = _probe([BLOCKED])[0]["note"]
        assert "CNY" in note and "7 位分析师" in note and "营收" in note

    def test_a_row_without_a_pair_behaves_exactly_as_before(self):
        row = {k: v for k, v in BLOCKED.items() if not k.startswith("eps_pair_")}
        out = _probe([row])[0]
        assert out["surplus"] is None and out["comparable"] is False


class TestStreakUsesThePair:
    def _period(self, fy, fq, *, estimate, actual, pair):
        row = {"id": fy * 10 + fq, "symbol": "0700.HK", "market": "HK",
               "fiscal_year": fy, "fiscal_quarter": fq,
               "eps_estimate": estimate, "eps_actual": actual,
               "estimate_currency": "HKD", "actual_currency": "CNY",
               "estimate_source": "longbridge", "actual_source": "futu"}
        if pair:
            row.update({"eps_pair_estimate": 6.2873, "eps_pair_actual": 6.947,
                        "eps_pair_currency": "CNY", "eps_pair_analysts": 7,
                        "eps_pair_source": "futu"})
        return row

    def test_a_blocked_quarter_counts_as_a_beat_once_it_has_a_pair(self):
        from app import phase3

        rows = [self._period(2026, 3, estimate=7.3713, actual=7.059, pair=True),
                self._period(2026, 2, estimate=7.2175, actual=7.0504, pair=True)]
        streak = phase3._beat_miss_streak(rows, 2026, 3)
        assert streak["kind"] == "beat" and streak["count"] == 2
        assert streak["reason"] is None and streak["break_reason"] is None

    def test_without_the_pair_the_same_quarter_is_still_withheld(self):
        from app import phase3

        rows = [self._period(2026, 3, estimate=7.3713, actual=7.059, pair=False)]
        streak = phase3._beat_miss_streak(rows, 2026, 3)
        assert streak["kind"] == "unavailable"
        assert streak["reason"] == fiscal.COMPARISON_CURRENCY_MISMATCH

    def test_the_history_projection_carries_symbol_and_market(self):
        """Without them the pair lookup keys on (None, None, …) and silently misses.

        Found in production: the panel printed an EPS surplus from the pair while the
        streak right below it still said 「连续 —（币种不同）」.
        """
        source = (ROOT / "app" / "earnings.py").read_text(encoding="utf-8")
        query = next(line for line in source.splitlines()
                     if "FROM earnings WHERE symbol=%s AND market=%s" in line)
        projection = query.split("SELECT", 1)[1].split(" FROM", 1)[0]
        fields = {field.strip() for field in projection.split(",")}
        assert {"symbol", "market", "fiscal_year", "fiscal_quarter"} <= fields


class TestReadPathWiring:
    def test_the_single_read_entry_point_attaches_pairs(self):
        source = (ROOT / "app" / "earnings.py").read_text(encoding="utf-8")
        assert "attach_provider_pairs(rows)" in source
        assert "attach_provider_pairs([earning] + history)" in source

    def test_the_streak_prefers_the_pair_over_the_row_level_gate(self):
        source = (ROOT / "app" / "phase3.py").read_text(encoding="utf-8")
        assert "pair = fiscal.row_eps_pair(row)" in source
        assert "actual, estimate = as_decimal(pair[\"actual\"])" in source

    def test_the_response_model_declares_the_pair(self):
        from app.schemas import EarningItem

        assert set(fiscal.EPS_PAIR_FIELDS) <= set(EarningItem.model_fields)
