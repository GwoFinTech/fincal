"""Issue #148: 营收也能用提供方自己那一对算「较预期」。

为什么会有这条：#147 的 docstring 写死「提供方没有营收一致预期」，于是中概股的营收
「较预期」一直是「—」。真相是 `financials/estimate` 有个 `estimate_type` 参数
（EPS_GAAP / REVENUE / EBIT / NET_INCOME_GAAP），#147 从来没发过它——默认值就是 EPS。
实测（2026-11-21）：

- 0700.HK 2026Q3 营收 consensus 210,239,931,880 / actual 207,336,000,000 CNY，
  与报表 `revenue_actual` 逐位相同；
- AAPL 2026Q4 113,353,533,600 / 114,477,000,000 USD；BABA 2027Q2、PDD 2026Q3 同样对得上；
- 币种是**每个指标各自**的：TSM 的 EPS 一致预期是 USD，营收是 TWD 1,494,243,000,000。

这里钉住：传输层只接受枚举内的 estimate_type；同步按指标各一次调用、两个指标都落库；
读路径只在营收被**币种**挡住时采纳营收对；`revenue_blocked()` 在采纳后不再拦；面板 JS 走
同一对；导出列齐全。
"""
import importlib.util
import re
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


SYNC_FUTU = _load("sync_futu_revenue_under_test", ROOT / "scripts" / "sync_futu.py")


def _formatters_source() -> str:
    """The real ``useFormatters()`` body, brace-matched the way the other guards do."""
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
    """Run the panel's real JS through node — the numbers users see come from here."""
    probe = f"{_formatters_source()}\nconst fmt = useFormatters();\n{script}\n"
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


ROW_WITH_REVENUE_PAIR = """
const row = {
  symbol: '0700.HK', revenue_estimate: 247536800000, revenue_actual: 207336000000,
  estimate_currency: 'HKD', actual_currency: 'CNY',
  comparison_unavailable_reason: 'currency_mismatch',
  revenue_pair_estimate: 210239931880, revenue_pair_actual: 207336000000,
  revenue_pair_currency: 'CNY', revenue_pair_analysts: 22, revenue_pair_source: 'futu',
};
"""

ROW_WITHOUT_PAIR = """
const row = {
  symbol: '0700.HK', revenue_estimate: 247536800000, revenue_actual: 207336000000,
  estimate_currency: 'HKD', actual_currency: 'CNY',
  comparison_unavailable_reason: 'currency_mismatch',
};
"""


# ── 传输 ────────────────────────────────────────────────────────────────
class TestTransport:
    def test_the_estimate_type_reaches_the_wire(self):
        request = futu_webapi.FutuWebClient.request
        captured = {}

        def fake_request(self, endpoint, params=None, **kwargs):
            captured["endpoint"] = endpoint
            captured["params"] = dict(params or {})
            return {"ret_code": 0, "data": {"currency": "TWD", "period_type": "QUARTERLY",
                                            "historical_estimates": [], "future_estimates": []}}

        futu_webapi.FutuWebClient.request = fake_request
        try:
            client = futu_webapi.FutuWebClient.__new__(futu_webapi.FutuWebClient)
            ret, payload = client.get_financials_estimate("US.TSM", estimate_type="REVENUE")
        finally:
            futu_webapi.FutuWebClient.request = request
        assert ret == 0
        assert captured["params"]["estimate_type"] == "REVENUE"
        # The metric is carried back so the sync can key its rows by it.
        assert payload["metric"] == "REVENUE" and payload["currency"] == "TWD"

    def test_an_unknown_estimate_type_is_refused_before_the_call(self):
        client = futu_webapi.FutuWebClient.__new__(futu_webapi.FutuWebClient)
        ret, message = client.get_financials_estimate("US.AAPL", estimate_type="REVENUE,EPS")
        assert ret == -1 and "unsupported estimate_type" in message

    def test_the_four_metrics_are_the_documented_set(self):
        assert futu_webapi.FutuWebClient.ESTIMATE_TYPES == (
            "EPS_GAAP", "REVENUE", "EBIT", "NET_INCOME_GAAP")


# ── 同步 ────────────────────────────────────────────────────────────────
class _StubLimiter:
    window_seconds = 30.0

    def acquire(self):
        return 0.0

    def cool_down(self):
        pass


class _MetricClient:
    """Answers per metric — and hides the revenue payload for a symbol without it."""

    def __init__(self, *, revenue=True):
        self.revenue = revenue
        self.asked = []

    def get_financials_estimate(self, code, period_type="QUARTERLY",
                                estimate_type="EPS_GAAP"):
        self.asked.append(estimate_type)
        if estimate_type == "REVENUE" and not self.revenue:
            return -1, "Futu WebAPI HTTP 200, code -10: no data"
        if estimate_type == "REVENUE":
            return 0, {"metric": "REVENUE", "currency": "CNY", "period_type": "QUARTERLY",
                       "history": [{"fiscal_year": 2026, "fiscal_quarter": 3,
                                    "consensus": 210239931880.0,
                                    "actual": 207336000000.0, "num_analysts": 22}],
                       "future": []}
        return 0, {"metric": "EPS_GAAP", "currency": "CNY", "period_type": "QUARTERLY",
                   "history": [{"fiscal_year": 2026, "fiscal_quarter": 3,
                                "consensus": 6.2873, "actual": 6.947, "num_analysts": 7}],
                   "future": []}


class TestSync:
    def test_each_metric_is_its_own_call(self):
        stats = SYNC_FUTU.FutuStageStats()
        client = _MetricClient()
        assert SYNC_FUTU.PROVIDER_PAIR_METRICS == ("EPS_GAAP", "REVENUE")
        for metric in SYNC_FUTU.PROVIDER_PAIR_METRICS:
            SYNC_FUTU.fetch_provider_pairs(client, "HK.00700", _StubLimiter(), stats, 20,
                                           metric=metric)
        assert client.asked == ["EPS_GAAP", "REVENUE"]

    def test_the_revenue_row_carries_its_own_currency_and_numbers(self):
        rows, outcome = SYNC_FUTU.fetch_provider_pairs(
            _MetricClient(), "HK.00700", _StubLimiter(), SYNC_FUTU.FutuStageStats(), 20,
            metric="REVENUE")
        assert outcome == SYNC_FUTU.OUTCOME_OK and len(rows) == 1
        metric, currency, consensus, actual, analysts, _type, fy, fq = rows[0]
        assert (metric, currency, fy, fq) == ("REVENUE", "CNY", 2026, 3)
        assert consensus == pytest.approx(210239931880.0)
        assert actual == pytest.approx(207336000000.0) and analysts == 22

    def test_a_symbol_without_revenue_consensus_still_writes_eps(self):
        stats = SYNC_FUTU.FutuStageStats()
        client = _MetricClient(revenue=False)
        written = []
        for metric in SYNC_FUTU.PROVIDER_PAIR_METRICS:
            rows, outcome = SYNC_FUTU.fetch_provider_pairs(
                client, "US.SPY", _StubLimiter(), stats, 20, metric=metric)
            if outcome == SYNC_FUTU.OUTCOME_UNSUPPORTED:
                stats.pair_unsupported += 1
                continue
            written.extend(rows)
        assert [row[0] for row in written] == ["EPS_GAAP"]
        assert stats.pair_unsupported == 1

    def test_flush_counts_the_revenue_metric_separately(self):
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
            rows, _outcome = SYNC_FUTU.fetch_provider_pairs(
                _MetricClient(), "HK.00700", _StubLimiter(), stats, 20, metric="REVENUE")
            SYNC_FUTU.flush_provider_pairs("0700.HK", "HK", rows, stats)
        finally:
            SYNC_FUTU.db_cursor = original
        assert stats.pair_rows == 1 and stats.revenue_pair_rows == 1
        assert "earnings_provider_metrics" in executed[0][0]
        assert executed[0][1][4] == "REVENUE"


# ── 采纳规则 ────────────────────────────────────────────────────────────
def _row(**over):
    row = {"symbol": "0700.HK", "market": "HK", "fiscal_year": 2026, "fiscal_quarter": 3,
           "eps_estimate": 7.3713, "eps_actual": 7.059,
           "revenue_estimate": 247536800000.0, "revenue_actual": 207336000000.0,
           "estimate_currency": "HKD", "actual_currency": "CNY",
           "estimate_source": "longbridge", "actual_source": "futu"}
    row.update(over)
    row.setdefault("comparison_unavailable_reason",
                   fiscal.comparison_unavailable_reason(row))
    return row


REVENUE_PAIR = {"currency": "CNY", "consensus": 210239931880.0, "actual": 207336000000.0,
                "num_analysts": 22, "source": "futu"}


class TestRevenueAdoption:
    def test_a_currency_blocked_row_adopts_the_revenue_pair(self):
        row = fiscal.attach_revenue_pair(_row(), REVENUE_PAIR)
        pair = fiscal.row_revenue_pair(row)
        assert pair["estimate"] == pytest.approx(210239931880.0)
        assert pair["actual"] == pytest.approx(207336000000.0)
        assert pair["currency"] == "CNY" and pair["num_analysts"] == 22
        # The row's own attribution is untouched: the pair is an extra view, not a
        # rewrite of what each column came from.
        assert row["estimate_currency"] == "HKD" and row["actual_currency"] == "CNY"

    def test_half_a_pair_is_refused(self):
        row = fiscal.attach_revenue_pair(_row(), dict(REVENUE_PAIR, actual=None))
        assert fiscal.row_revenue_pair(row) is None

    def test_revenue_is_only_rescued_when_the_currency_is_the_blocker(self):
        row = _row(estimate_currency="CNY",
                   comparison_unavailable_reason=fiscal.COMPARISON_BASIS_MISMATCH)
        assert fiscal.needs_revenue_pair(row) is False

    def test_revenue_blocked_stops_blocking_once_the_pair_is_there(self):
        blocked = _row()
        assert fiscal.revenue_blocked(blocked) is True
        assert fiscal.revenue_blocked(fiscal.attach_revenue_pair(blocked, REVENUE_PAIR)) is False

    def test_a_comparable_row_needs_no_revenue_pair(self):
        row = _row(estimate_currency="CNY")
        assert fiscal.needs_revenue_pair(row) is False
        assert fiscal.revenue_blocked(row) is False


# ── 读路径接线 ──────────────────────────────────────────────────────────
class TestReadPath:
    def test_the_loader_groups_by_metric(self):
        from app import earnings

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, sql, params=None):
                assert "earnings_provider_metrics" in sql

            def fetchall(self):
                return [
                    {"symbol": "0700.HK", "market": "HK", "fiscal_year": 2026,
                     "fiscal_quarter": 3, "source": "futu", "metric": "EPS_GAAP",
                     "currency": "CNY", "consensus": 6.2873, "actual": 6.947,
                     "num_analysts": 7, "period_type": "QUARTERLY"},
                    {"symbol": "0700.HK", "market": "HK", "fiscal_year": 2026,
                     "fiscal_quarter": 3, "source": "futu", "metric": "REVENUE",
                     "currency": "CNY", "consensus": 210239931880.0,
                     "actual": 207336000000.0, "num_analysts": 22,
                     "period_type": "QUARTERLY"},
                ]

        from app import db as db_module

        original = db_module.db_cursor
        db_module.db_cursor = lambda: _Cursor()
        try:
            pairs = earnings.load_provider_pairs([("0700.HK", "HK", 2026, 3)])
        finally:
            db_module.db_cursor = original
        by_metric = pairs[("0700.HK", "HK", 2026, 3)]
        assert set(by_metric) == {"EPS_GAAP", "REVENUE"}
        assert by_metric["REVENUE"]["actual"] == pytest.approx(207336000000.0)

    def test_attach_provider_pairs_rescues_both_metrics(self):
        from app import earnings

        rows = [_row()]
        pairs = {("0700.HK", "HK", 2026, 3): {
            "EPS_GAAP": {"currency": "CNY", "consensus": 6.2873, "actual": 6.947,
                         "num_analysts": 7, "source": "futu"},
            "REVENUE": REVENUE_PAIR}}

        original = earnings.load_provider_pairs
        earnings.load_provider_pairs = lambda keys: pairs
        try:
            earnings.attach_provider_pairs(rows)
        finally:
            earnings.load_provider_pairs = original
        assert rows[0]["eps_pair_estimate"] == pytest.approx(6.2873)
        assert rows[0]["revenue_pair_estimate"] == pytest.approx(210239931880.0)

    def test_the_revenue_pair_reaches_the_export(self):
        source = (ROOT / "app" / "routers" / "api.py").read_text(encoding="utf-8")
        for field in fiscal.REVENUE_PAIR_FIELDS:
            assert f'"{field}"' in source


# ── 面板 JS ────────────────────────────────────────────────────────────
class TestPanelJs:
    def test_the_revenue_pair_drives_the_cells(self):
        out = _run_js(ROW_WITH_REVENUE_PAIR + """
console.log(JSON.stringify({
  estimate: fmt.revenueEstimate(row), actual: fmt.revenueActual(row),
  currency: fmt.revenuePair(row).currency,
  pct: fmt.revSurplus(row), cls: fmt.revSurplusClass(row),
  has: fmt.hasComparison(row, 'revenue'),
}));""")
        payload = __import__("json").loads(out)
        assert payload["estimate"] == pytest.approx(210239931880.0)
        assert payload["actual"] == pytest.approx(207336000000.0)
        assert payload["currency"] == "CNY"
        assert payload["pct"] == pytest.approx((207336000000 - 210239931880) / 210239931880)
        assert payload["cls"] == "miss" and payload["has"] is True

    def test_without_the_pair_the_blocked_row_still_shows_nothing(self):
        out = _run_js(ROW_WITHOUT_PAIR + """
console.log(JSON.stringify({
  pair: fmt.revenuePair(row), pct: fmt.revSurplus(row),
  has: fmt.hasComparison(row, 'revenue'),
  estimate: fmt.revenueEstimate(row),
}));""")
        payload = __import__("json").loads(out)
        assert payload["pair"] is None and payload["pct"] is None
        assert payload["has"] is False
        # The raw numbers are still shown — "—" is about the surplus, not the data.
        assert payload["estimate"] == 247536800000

    def test_the_note_names_the_currency_and_the_analyst_count(self):
        out = _run_js(ROW_WITH_REVENUE_PAIR + """
console.log(fmt.revenuePairNote(row));""")
        assert "CNY" in out and "22 位分析师" in out and "营收" in out

    def test_the_eps_note_stops_claiming_revenue_is_impossible_once_it_has_a_pair(self):
        with_pair = _run_js(ROW_WITH_REVENUE_PAIR + """
const both = Object.assign({}, row, {eps_pair_estimate: 6.2873, eps_pair_actual: 6.947,
  eps_pair_currency: 'CNY', eps_pair_analysts: 7});
console.log(fmt.epsPairNote(both));""")
        without_pair = _run_js(ROW_WITHOUT_PAIR + """
const only = Object.assign({}, row, {eps_pair_estimate: 6.2873, eps_pair_actual: 6.947,
  eps_pair_currency: 'CNY', eps_pair_analysts: 7});
console.log(fmt.epsPairNote(only));""")
        assert "营收无一致预期" not in with_pair
        assert "营收无一致预期，仍不可比" in without_pair
