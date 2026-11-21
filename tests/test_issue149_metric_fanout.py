"""Issue #149: 四个指标（EPS / 营收 / EBIT / 净利润）都走同一套 pair，历史窗 20 期。

官方文档（open.futunn.com「分析师财务预测」）写明 `estimate_type` 四个取值与
`hist_count`（默认 8、最大 20）。面板的对比表本来就有 EBIT 与净利润的位置，中概股这两行
一直是「—」；历史窗 8 期也让两年前的日历行拿不到那一对。

这里钉住：同步取四个指标且把 hist_count=20 发出去、阶段预算随之调整、读路径按指标各自
采纳、schema/导出列齐全、面板 JS 用同一对算值/涨跌/颜色。
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

from app import config, fiscal  # noqa: E402

APP_JS = ROOT / "app" / "static" / "assets" / "app-setup.js"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SYNC_FUTU = _load("sync_futu_metrics_under_test", ROOT / "scripts" / "sync_futu.py")


class _StubLimiter:
    window_seconds = 30.0

    def acquire(self):
        return 0.0

    def cool_down(self):
        pass


class _Client:
    def __init__(self, supported=None):
        self.supported = supported or set(SYNC_FUTU.PROVIDER_PAIR_METRICS)
        self.calls = []

    def get_financials_estimate(self, code, period_type="QUARTERLY",
                                estimate_type="EPS_GAAP", hist_count=None):
        self.calls.append((estimate_type, hist_count))
        if estimate_type not in self.supported:
            return -1, "Futu WebAPI HTTP 200, code -10: no data"
        return 0, {"metric": estimate_type, "currency": "CNY", "period_type": "QUARTERLY",
                   "history": [{"fiscal_year": 2019, "fiscal_quarter": 1,
                                "consensus": 100.0, "actual": 110.0,
                                "num_analysts": 9}], "future": []}


# ── 同步的扇出 ──────────────────────────────────────────────────────────
class TestMotionFanout:
    def test_four_documented_metrics_are_fetched(self):
        assert SYNC_FUTU.PROVIDER_PAIR_METRICS == (
            "EPS_GAAP", "REVENUE", "EBIT", "NET_INCOME_GAAP")

    def test_the_history_window_is_sent_and_within_the_documented_ceiling(self):
        client = _Client()
        SYNC_FUTU.fetch_provider_pairs(client, "HK.00700", _StubLimiter(),
                                       SYNC_FUTU.FutuStageStats(), 20,
                                       metric="NET_INCOME_GAAP")
        assert SYNC_FUTU.PROVIDER_PAIR_HISTORY == 20
        assert client.calls == [("NET_INCOME_GAAP", 20)]
        # The API answers -3 above 20; asking for exactly 20 is the ceiling, not a wish.
        assert 1 <= SYNC_FUTU.PROVIDER_PAIR_HISTORY <= 20

    def test_the_row_shape_carries_the_metric(self):
        rows, _outcome = SYNC_FUTU.fetch_provider_pairs(
            _Client(), "HK.00700", _StubLimiter(), SYNC_FUTU.FutuStageStats(), 20,
            metric="EBIT")
        assert rows[0][0] == "EBIT" and rows[0][1] == "CNY"
        assert rows[0][6] == 2019 and rows[0][7] == 1

    def test_each_metric_is_counted_separately(self):
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
            for metric in SYNC_FUTU.PROVIDER_PAIR_METRICS:
                rows, _outcome = SYNC_FUTU.fetch_provider_pairs(
                    _Client(), "0700.HK", _StubLimiter(), stats, 20, metric=metric)
                SYNC_FUTU.flush_provider_pairs("0700.HK", "HK", rows, stats)
        finally:
            SYNC_FUTU.db_cursor = original
        assert stats.metric_pair_rows == {"EPS_GAAP": 1, "REVENUE": 1, "EBIT": 1,
                                          "NET_INCOME_GAAP": 1}
        assert stats.pair_rows == 4
        # The details blob exposes it, so a run answers "did EBIT land?" without a query.
        import inspect

        assert "provider_metric_pairs" in inspect.getsource(SYNC_FUTU.futu_audit_details)


# ── 阶段预算 ────────────────────────────────────────────────────────────
class TestBudget:
    def test_the_futu_stage_budget_covers_five_calls_per_symbol(self):
        """Three estimate endpoints ago this was 1500s; five calls/symbol needs more."""
        assert config.STAGE_TIMEOUT_SECONDS["futu"] == 2100
        assert config.stage_timeout("futu") == 2100

    def test_the_shell_mirror_agrees(self):
        source = (ROOT / "scripts" / "sync_all.sh").read_text(encoding="utf-8")
        assert 'FINCAL_STAGE_TIMEOUT_FUTU:-2100' in source


# ── 字段表 ──────────────────────────────────────────────────────────────
class TestFieldTable:
    def test_every_metric_has_five_fields_and_a_unique_prefix(self):
        assert set(fiscal.PROVIDER_METRIC_PREFIX) == {
            "EPS_GAAP", "REVENUE", "EBIT", "NET_INCOME_GAAP"}
        names = []
        for metric in fiscal.PROVIDER_METRIC_PREFIX:
            fields = fiscal.pair_field_names(metric)
            assert len(fields) == 5
            names.extend(fields)
        assert len(names) == len(set(names)) == 20
        assert len(fiscal.ALL_PAIR_FIELDS) == 20

    def test_the_response_model_declares_every_pair_field(self):
        from app.schemas import EarningItem

        missing = [f for f in fiscal.ALL_PAIR_FIELDS if f not in EarningItem.model_fields]
        assert missing == [], (
            "response_model silently drops undeclared fields — these would never reach "
            f"the panel: {missing}")

    def test_the_export_carries_every_pair_field(self):
        source = (ROOT / "app" / "routers" / "api.py").read_text(encoding="utf-8")
        missing = [f for f in fiscal.ALL_PAIR_FIELDS if f'"{f}"' not in source]
        assert missing == []


# ── 采纳规则 ────────────────────────────────────────────────────────────
EBIT_PAIR = {"currency": "CNY", "consensus": 24328943120.0, "actual": 29116000000.0,
             "num_analysts": 14, "source": "futu"}
NET_INCOME_PAIR = {"currency": "CNY", "consensus": 22951390530.0,
                   "actual": 64535000000.0, "num_analysts": 7, "source": "futu"}


class TestAdoption:
    def _row(self, **over):
        row = {"symbol": "BABA", "market": "US", "fiscal_year": 2027, "fiscal_quarter": 2,
               "eps_estimate": 1.3589, "eps_actual": 26.96,
               "revenue_estimate": 40302317201.0, "revenue_actual": 271519000000.0,
               "consensus_ebit": 3.6e9, "consensus_net_income": 6.76e10,
               "estimate_currency": "USD", "actual_currency": "CNY"}
        row.update(over)
        row.setdefault("comparison_unavailable_reason",
                       fiscal.comparison_unavailable_reason(row))
        return row

    def test_ebit_and_net_income_are_attached_with_their_own_currency(self):
        row = fiscal.attach_metric_pair(self._row(), fiscal.METRIC_EBIT, EBIT_PAIR)
        row = fiscal.attach_metric_pair(row, fiscal.METRIC_NET_INCOME, NET_INCOME_PAIR)
        assert fiscal.row_ebit_pair(row)["actual"] == pytest.approx(29116000000.0)
        assert fiscal.row_net_income_pair(row)["estimate"] == pytest.approx(22951390530.0)
        assert fiscal.row_ebit_pair(row)["currency"] == "CNY"

    def test_they_are_rescued_even_when_the_row_has_no_actual_column_for_them(self):
        """EBIT/net income have no Longbridge-vs-Futu column pair on the calendar row.

        Their consensus comes from Longbridge with no currency of its own, so the only
        honest source of both numbers is the provider pair — whenever it exists.
        """
        row = self._row()
        assert row.get("ebit_actual") is None and row.get("net_income_actual") is None
        assert fiscal.needs_metric_pair(row, fiscal.METRIC_EBIT) is True
        assert fiscal.needs_metric_pair(row, fiscal.METRIC_NET_INCOME) is True

    def test_the_currency_gate_still_applies(self):
        row = self._row(estimate_currency="CNY")
        assert fiscal.needs_metric_pair(row, fiscal.METRIC_EPS) is False
        assert fiscal.needs_metric_pair(row, fiscal.METRIC_REVENUE) is False

    def test_one_adoption_does_not_imply_the_others(self):
        row = fiscal.attach_metric_pair(self._row(), fiscal.METRIC_EBIT, EBIT_PAIR)
        assert fiscal.row_ebit_pair(row) is not None
        assert fiscal.row_net_income_pair(row) is None

    def test_the_read_path_attaches_every_metric(self):
        from app import earnings

        rows = [self._row()]
        pairs = {("BABA", "US", 2027, 2): {
            "EPS_GAAP": {"currency": "CNY", "consensus": 9.1573, "actual": 25.94,
                         "num_analysts": 7, "source": "futu"},
            "REVENUE": {"currency": "CNY", "consensus": 269849229900.0,
                        "actual": 271519000000.0, "num_analysts": 22, "source": "futu"},
            "EBIT": EBIT_PAIR,
            "NET_INCOME_GAAP": NET_INCOME_PAIR}}

        original = earnings.load_provider_pairs
        earnings.load_provider_pairs = lambda keys: pairs
        try:
            earnings.attach_provider_pairs(rows)
        finally:
            earnings.load_provider_pairs = original
        for metric in fiscal.PROVIDER_METRIC_PREFIX:
            assert fiscal.row_metric_pair(rows[0], metric) is not None, metric


# ── 面板 JS ────────────────────────────────────────────────────────────
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


def _run_js(script: str):
    probe = f"{_formatters_source()}\nconst fmt = useFormatters();\n{script}\n"
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


ROW = """
const row = {
  symbol: 'BABA', estimate_currency: 'USD', actual_currency: 'CNY',
  comparison_unavailable_reason: 'currency_mismatch',
  consensus_ebit: 3600000000, consensus_net_income: 67600000000,
  ebit_pair_estimate: 24328943120, ebit_pair_actual: 29116000000,
  ebit_pair_currency: 'CNY', ebit_pair_analysts: 14, ebit_pair_source: 'futu',
  net_income_pair_estimate: 22951390530, net_income_pair_actual: 64535000000,
  net_income_pair_currency: 'CNY', net_income_pair_analysts: 7,
  net_income_pair_source: 'futu',
};
"""


class TestPanelJs:
    def test_ebit_and_net_income_use_the_pair(self):
        out = _run_js(ROW + """
console.log(JSON.stringify({
  ebitEstimate: fmt.ebitEstimate(row), ebitActual: fmt.ebitActual(row),
  niEstimate: fmt.netIncomeEstimate(row), niActual: fmt.netIncomeActual(row),
  ebitPct: fmt.ebitSurplus(row), niPct: fmt.netIncomeSurplus(row),
  ebitCls: fmt.surplusClass(row, 'ebit'),
  hasEbit: fmt.hasComparison(row, 'ebit'),
}));""")
        payload = json.loads(out)
        assert payload["ebitEstimate"] == pytest.approx(24328943120.0)
        assert payload["ebitActual"] == pytest.approx(29116000000.0)
        assert payload["niEstimate"] == pytest.approx(22951390530.0)
        assert payload["niActual"] == pytest.approx(64535000000.0)
        assert payload["ebitPct"] == pytest.approx(
            (29116000000 - 24328943120) / 24328943120)
        assert payload["ebitCls"] == "beat" and payload["hasEbit"] is True

    def test_without_a_pair_the_row_falls_back_to_the_consensus_and_shows_nothing(self):
        out = _run_js("""
const row = { symbol: 'BABA', consensus_ebit: 3600000000,
              consensus_net_income: 67600000000,
              comparison_unavailable_reason: 'currency_mismatch' };
console.log(JSON.stringify({
  ebitEstimate: fmt.ebitEstimate(row), ebitActual: fmt.ebitActual(row),
  ebitPct: fmt.ebitSurplus(row), hasEbit: fmt.hasComparison(row, 'ebit'),
  note: fmt.ebitPairNote(row),
}));""")
        payload = json.loads(out)
        # The estimate is still shown (it is Longbridge's), the surplus is not invented.
        assert payload["ebitEstimate"] == 3600000000
        assert payload["ebitActual"] is None and payload["ebitPct"] is None
        assert payload["hasEbit"] is False and payload["note"] == ""

    def test_the_note_names_the_metric(self):
        assert "EBIT" in _run_js(ROW + "console.log(fmt.ebitPairNote(row));")
        assert "净利润" in _run_js(ROW + "console.log(fmt.netIncomePairNote(row));")

    def test_colours_follow_each_metrics_own_sign(self):
        out = _run_js("""
const row = { revenue_pair_estimate: 100, revenue_pair_actual: 90,
              ebit_pair_estimate: 100, ebit_pair_actual: 130 };
console.log(JSON.stringify({ revenue: fmt.surplusClass(row, 'revenue'),
                             ebit: fmt.surplusClass(row, 'ebit') }));""")
        payload = json.loads(out)
        assert payload["revenue"] == "miss" and payload["ebit"] == "beat"

    def test_the_panel_renders_the_two_rows(self):
        html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        for fragment in ("ebitEstimate(selectedEarning)", "ebitActual(selectedEarning)",
                         "netIncomeEstimate(selectedEarning)",
                         "netIncomeActual(selectedEarning)",
                         ">EBIT<", ">净利润<"):
            assert fragment in html, fragment
