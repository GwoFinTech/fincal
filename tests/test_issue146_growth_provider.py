"""Issue #146: provider-reported growth (``yoy``/``qoq``) from the statement to the panel.

Why this exists at all: growth used to be computed only from two *stored* periods,
so it required this app to already hold the prior-year quarter — measured 348 of
9,758 symbols (3.3%). The provider returns ``yoy``/``qoq`` on every statement line
and it equals our own ratio wherever both exist, so those values are stored in
``earnings_growth`` and shown instead, with the base they were taken against and a
cross-check that keeps the two from drifting apart silently.

Pinned here:

* the percent→ratio boundary (``earnings_growth`` stores percent, the API/panel/JS
  speak ratios) — the single conversion in :func:`merge_provider_growth`;
* a pair whose currency or accounting basis changed produces **no percentage** and
  a reason, never a cross-currency ratio;
* the cross-check tolerance and the run counters that make a disagreement visible;
* the trend shape (≤8 periods, oldest→newest, withheld periods keep their slot);
* the panel's JS: the chart's bar geometry, the 涨绿跌红 mapping, and the tooltip
  that has to name the base and which source produced the number.
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

from app import futu_webapi, phase3  # noqa: E402

APP_JS = ROOT / "app" / "static" / "assets" / "app-setup.js"
INDEX_HTML = ROOT / "app" / "static" / "index.html"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SYNC_FUTU = _load("sync_futu_growth_under_test", ROOT / "scripts" / "sync_futu.py")


def _report(*items, currency="USD", basis="US_GAAP", fy=2026, ft=4):
    return {
        "fiscal_year": fy, "financial_type": ft,
        "currency_code": currency, "accounting_standards": basis,
        "item_list": [
            {"field_id": index, "display_name": label, "data": value, "yoy": yoy, "qoq": qoq}
            for index, (label, value, yoy, qoq) in enumerate(items, start=1)
        ],
    }


def _fields(report):
    return (futu_webapi.select_statement_field(report, futu_webapi.REVENUE_LABELS),
            futu_webapi.select_statement_field(report, futu_webapi.EPS_LABELS))


# ── the transport carries the provider's own growth ──────────────────────
class TestStatementFieldCarriesGrowth:
    def test_yoy_and_qoq_are_read_off_the_item(self):
        revenue, eps = _fields(_report(
            ("Total Revenue", 1000.0, 11.72, 3.1),
            ("Basic EPS", 2.5, 14.05, 1.2),
        ))
        assert revenue.yoy == pytest.approx(11.72)
        assert revenue.qoq == pytest.approx(3.1)
        assert eps.yoy == pytest.approx(14.05)

    def test_a_null_comparison_stays_null_not_zero(self):
        """The provider uses null for 「no comparison」; 0 would mean 「flat」."""
        revenue, _eps = _fields(_report(("Total Revenue", 1000.0, None, None)))
        assert revenue.yoy is None and revenue.qoq is None


# ── storing a row ────────────────────────────────────────────────────────
class TestGrowthRow:
    def test_provider_percent_is_stored_as_is_with_its_base(self):
        current = _report(("Total Revenue", 1116.0, 11.6, 2.0), ("Basic EPS", 2.8, 12.0, 1.0))
        prior = _report(("Total Revenue", 1000.0, None, None), ("Basic EPS", 2.5, None, None),
                        fy=2025)
        revenue, eps = _fields(current)
        row = SYNC_FUTU.growth_row("AAPL", "US", 2026, 4, 4, current, prior, revenue, eps)
        assert row[5] == pytest.approx(11.6)          # revenue_yoy, percent
        assert row[6] == pytest.approx(12.0)          # eps_yoy, percent
        assert row[9] == pytest.approx(1000.0)        # revenue_base
        assert row[10] == pytest.approx(2.5)          # eps_base
        assert row[13] is None                        # reason

    def test_a_currency_change_withholds_every_percentage(self):
        current = _report(("Total Revenue", 1116.0, 11.6, 2.0), currency="USD")
        prior = _report(("Total Revenue", 1000.0, None, None), currency="CNY", fy=2025)
        revenue, eps = _fields(current)
        row = SYNC_FUTU.growth_row("TSM", "US", 2026, 4, 4, current, prior, revenue, eps)
        assert row[5] is None and row[6] is None and row[7] is None and row[8] is None
        assert row[13] == "currency_changed"

    def test_a_basis_change_withholds_every_percentage(self):
        current = _report(("Total Revenue", 1116.0, 11.6, 2.0), basis="US_GAAP")
        prior = _report(("Total Revenue", 1000.0, None, None), basis="IFRS", fy=2025)
        revenue, eps = _fields(current)
        row = SYNC_FUTU.growth_row("X", "US", 2026, 4, 4, current, prior, revenue, eps)
        assert row[5] is None and row[13] == "basis_changed"

    def test_without_a_prior_period_the_percentage_still_stands_but_the_base_is_null(self):
        """The provider can compare against a period we did not receive."""
        current = _report(("Total Revenue", 1116.0, 11.6, 2.0), ("Basic EPS", 2.8, 12.0, 1.0))
        revenue, eps = _fields(current)
        row = SYNC_FUTU.growth_row("AAPL", "US", 2026, 4, 4, current, None, revenue, eps)
        assert row[5] == pytest.approx(11.6)
        assert row[9] is None and row[10] is None

    def test_flush_counts_written_and_incomparable_rows(self):
        stats = SYNC_FUTU.FutuStageStats()
        good = ("A", "US", 2026, 4, 4, 10.0, 11.0, None, None, 100.0, 1.0, "USD", "gaap", None)
        withheld = ("B", "US", 2026, 4, 4, None, None, None, None, None, None, "USD", "gaap",
                    "currency_changed")
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
            written = SYNC_FUTU.flush_growth_rows([good, withheld], stats)
        finally:
            SYNC_FUTU.db_cursor = original
        assert written == 2 and stats.growth_rows == 2
        assert stats.growth_incomparable == 1
        assert all("ON CONFLICT (symbol, market, fiscal_year, fiscal_quarter)" in sql
                   for sql, _ in executed)


# ── cross-check tolerance ────────────────────────────────────────────────
class TestCrossCheckTolerance:
    def test_the_tolerance_is_a_tenth_of_a_point(self):
        assert SYNC_FUTU.GROWTH_CROSSCHECK_TOLERANCE_PP == pytest.approx(0.1)

    def test_a_negative_base_is_measured_against_its_magnitude(self):
        """The provider's convention, pinned with the run that exposed it.

        XPEV FY2026 Q3: prior-year EPS -0.4, this year's -0.9 → the provider says
        -125% (loss grew by 1.25× the base's magnitude). Dividing by the *signed*
        base gives +125%, which is how the cross-check first reported 146
        disagreements that were all the same sign bug.
        """
        prior, current = -0.4, -0.9
        ours = ((current - prior) / abs(prior)) * 100
        assert ours == pytest.approx(-125.0)
        assert (current / prior - 1) * 100 == pytest.approx(125.0), (
            "the signed-base formula is the wrong one; it stays here as the pin")

    def test_the_stage_uses_the_magnitude_convention(self):
        source = (ROOT / "scripts" / "sync_futu.py").read_text(encoding="utf-8")
        assert "/ abs(prior_read.value)) * 100" in source

    def test_the_callers_count_a_disagreement_against_the_statements(self):
        """The stage compares the provider's ``yoy`` with the two statements' ratio.

        Same payload, same periods: a difference above the tolerance is counted
        (and logged) rather than silently overwriting either number.
        """
        source = (ROOT / "scripts" / "sync_futu.py").read_text(encoding="utf-8")
        assert "stats.growth_discrepancies += 1" in source
        assert "GROWTH_CROSSCHECK_TOLERANCE_PP" in source
        assert "provider %.4f%% vs " in source


# ── merging into the panel's payload ─────────────────────────────────────
class TestMergeProviderGrowth:
    def _computed(self):
        growth = phase3._unavailable_growth()
        growth.update({"eps_yoy": 0.5, "eps_yoy_reason": None,
                       "revenue_yoy": None, "revenue_yoy_reason": "currency_unknown"})
        return growth

    def test_provider_percent_becomes_a_ratio_and_wins(self):
        merged = phase3.merge_provider_growth(
            self._computed(),
            {"eps_yoy": 14.05, "revenue_yoy": 11.72, "revenue_qoq": None, "eps_qoq": None,
             "eps_base": 2.5, "revenue_base": 1000.0, "reason": None})
        assert merged["eps_yoy"] == pytest.approx(0.1405)      # 14.05% → ratio
        assert merged["revenue_yoy"] == pytest.approx(0.1172)  # overrides the withheld one
        assert merged["revenue_yoy_reason"] is None
        assert merged["source"] == "provider"
        assert merged["base"]["eps"] == pytest.approx(2.5)

    def test_missing_provider_value_keeps_the_computed_ratio_and_reason(self):
        merged = phase3.merge_provider_growth(
            self._computed(), {"eps_yoy": None, "revenue_yoy": None, "reason": None})
        assert merged["eps_yoy"] == pytest.approx(0.5)
        assert merged["revenue_yoy_reason"] == "currency_unknown"
        assert merged["source"] == "statements"

    def test_no_provider_row_leaves_the_computed_contract_untouched(self):
        merged = phase3.merge_provider_growth(self._computed(), None)
        assert merged["eps_yoy"] == pytest.approx(0.5)
        assert merged["base"] == {"eps": None, "revenue": None}

    def test_a_withheld_provider_row_reports_its_reason(self):
        merged = phase3.merge_provider_growth(
            self._computed(), {"eps_yoy": None, "revenue_yoy": None,
                               "reason": "currency_changed"})
        assert merged["provider_reason"] == "currency_changed"


# ── trend shape ─────────────────────────────────────────────────────────
class TestGrowthTrend:
    def _rows(self, count=10):
        return [{"fiscal_year": 2026 - index // 4, "fiscal_quarter": 4 - index % 4,
                 "revenue_yoy": 10.0 + index, "eps_yoy": None,
                 "revenue_base": 100.0 + index, "eps_base": None,
                 "reason": None, "source": "futu"} for index in range(count)]

    def test_at_most_eight_points_oldest_first(self):
        trend = phase3.growth_trend(self._rows(10))
        assert trend["sample_count"] == 8
        assert trend["status"] == "available"
        assert trend["points"][0]["fiscal_year"] == self._rows(10)[7]["fiscal_year"]

    def test_percentages_are_converted_to_ratios(self):
        trend = phase3.growth_trend(self._rows(2))
        assert trend["points"][-1]["revenue_yoy"] == pytest.approx(0.10)

    def test_a_period_with_nothing_is_skipped_but_a_withheld_one_is_kept(self):
        rows = self._rows(2) + [
            {"fiscal_year": 2024, "fiscal_quarter": 4, "revenue_yoy": None, "eps_yoy": None,
             "reason": None, "source": "futu"},
            {"fiscal_year": 2024, "fiscal_quarter": 3, "revenue_yoy": None, "eps_yoy": None,
             "reason": "basis_changed", "source": "futu"},
        ]
        trend = phase3.growth_trend(rows)
        quarters = [(p["fiscal_year"], p["fiscal_quarter"]) for p in trend["points"]]
        assert (2024, 4) not in quarters, "a period with no number and no reason is dropped"
        assert (2024, 3) in quarters, "a withheld period keeps its slot"
        withheld = next(p for p in trend["points"] if p["fiscal_quarter"] == 3)
        assert withheld["reason"] == "basis_changed" and withheld["revenue_yoy"] is None

    def test_one_point_is_not_a_trend(self):
        assert phase3.growth_trend(self._rows(1))["status"] == "insufficient_history"


# ── the panel's JS ──────────────────────────────────────────────────────
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


def _run_probe(payload: dict) -> dict:
    probe = f"""
{_formatters_source()}
const fmt = useFormatters();
const decision = {json.dumps(payload)};
const points = fmt.growthTrendPoints(decision);
const out = {{
  labels: points.map(p => p.label),
  max: fmt.growthTrendMaxAbs(points),
  revenueWidths: points.map(p => fmt.growthBarWidth(p.revenue, fmt.growthTrendMaxAbs(points))),
  directions: points.map(p => fmt.growthBarDirection(p.revenue)),
  classes: points.map(p => fmt.growthBarClass(p.revenue)),
  note: fmt.growthNote(decision, 'eps_yoy'),
  source: fmt.growthSourceLabel(decision),
  no_points: fmt.growthTrendPoints({{}}),
}};
console.log(JSON.stringify(out));
"""
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


DECISION = {
    "actual_growth": {"eps_yoy": 0.1405, "eps_yoy_reason": None,
                      "base": {"eps": 2.5, "revenue": 1000.0}, "source": "provider"},
    "actual_growth_trend": {"status": "available", "sample_count": 3, "points": [
        {"fiscal_year": 2026, "fiscal_quarter": 1, "revenue_yoy": -0.05, "eps_yoy": 0.2,
         "revenue_base": 1000.0, "eps_base": 1.0, "reason": None, "source": "futu"},
        {"fiscal_year": 2026, "fiscal_quarter": 2, "revenue_yoy": 0.3, "eps_yoy": None,
         "revenue_base": 900.0, "eps_base": None, "reason": None, "source": "futu"},
        {"fiscal_year": 2026, "fiscal_quarter": 3, "revenue_yoy": None, "eps_yoy": None,
         "revenue_base": None, "eps_base": None, "reason": "currency_changed", "source": "futu"},
    ]},
}


class TestPanelJs:
    def test_labels_use_a_short_fiscal_form(self):
        result = _run_probe(DECISION)
        assert result["labels"] == ["26Q1", "26Q2", "26Q3"]

    def test_bars_scale_against_the_tallest_and_grow_from_the_centre(self):
        result = _run_probe(DECISION)
        # 30% is the tallest (max abs), so it takes the full half-width; -5% is
        # one sixth of it, and a withheld period draws nothing.
        assert result["max"] == pytest.approx(0.3)
        assert result["revenueWidths"][1] == pytest.approx(50.0)
        assert result["revenueWidths"][0] == pytest.approx(8.3, abs=0.1)
        assert result["revenueWidths"][2] == 0
        assert result["directions"] == [-1, 1, 0]

    def test_colours_follow_the_panel_convention(self):
        result = _run_probe(DECISION)
        assert result["classes"] == ["growth-bar-down", "growth-bar-up", "growth-bar-empty"]

    def test_the_tooltip_names_the_base_and_the_source(self):
        result = _run_probe(DECISION)
        assert "基期 2.5" in result["note"]
        assert "财报提供方" in result["note"]
        assert result["source"] == "财报提供方（同一报表行）"

    def test_no_decision_means_no_chart(self):
        assert _run_probe(DECISION)["no_points"] == []


class TestPageMarkup:
    def test_the_trend_section_and_its_styles_are_shipped(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert "营收 / EPS 同比趋势" in html
        for needle in ("growthTrendPoints(decision)", "growthTrendMaxAbs",
                       ".growth-trend {", ".growth-bar-up { background: var(--success); }",
                       ".growth-bar-down { background: var(--destructive); }"):
            assert needle in html, needle

    def test_the_mobile_rule_keeps_eight_columns_readable(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert ".growth-trend-value { display: none; }" in html


# ── the endpoint ────────────────────────────────────────────────────────
class _ScriptedCursor:
    """Serves one prepared result set per statement (the decision endpoint's queries)."""

    def __init__(self, pages):
        self._pages = list(pages)
        self._current = None
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(" ".join(str(sql).split()))
        self._current = self._pages.pop(0) if self._pages else []

    def fetchone(self):
        return self._current[0] if isinstance(self._current, list) and self._current else None

    def fetchall(self):
        return self._current if isinstance(self._current, list) else []

    def close(self):
        pass


def _decision_payload(growth_rows, earning=None):
    """Call the decision endpoint with the DB scripted page by page."""
    from datetime import date

    from fastapi.testclient import TestClient
    from unittest.mock import MagicMock, patch as mock_patch

    from app import db as app_db
    from app.auth import get_current_user
    from app.main import app

    row = {
        "id": 42, "symbol": "0700.HK", "market": "HK", "company_name": "腾讯控股",
        "fiscal_year": 2026, "fiscal_quarter": 3, "report_date": date(2026, 11, 18),
        "report_type": "Q", "is_predicted": False, "date_status": "reported",
        "eps_estimate": 7.37, "eps_actual": 7.059,
        "revenue_estimate": 200000.0, "revenue_actual": 207336.0,
        "estimate_currency": "CNY", "actual_currency": "CNY",
        "estimate_basis": None, "actual_basis": "ifrs",
        "estimate_source": "longbridge", "actual_source": "futu",
        "comparison_unavailable_reason": None,
        **(earning or {}),
    }
    history = [row, dict(row, id=41, fiscal_year=2025, fiscal_quarter=3,
                         eps_actual=6.952, revenue_actual=192869.0,
                         actual_currency="CNY", actual_basis="ifrs")]
    # The endpoint's own queries in order: snapshots, institution rating, guidance
    # status, then the growth rows (the row/history read is patched above).
    cursor = _ScriptedCursor([[], None, None, growth_rows])
    ctx = MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "email": "t@t.com", "name": "T", "role": "user"}
    # ``fetch_earning_with_history`` is imported inside the endpoint, so the patch
    # has to target its own module rather than the router's namespace.
    try:
        with mock_patch.object(app_db, "db_cursor", lambda: ctx), \
             mock_patch("app.routers.api.ensure_user", return_value={"id": 1}), \
             mock_patch("app.earnings.fetch_earning_with_history",
                        return_value=(row, history)):
            response = TestClient(app).get("/api/earnings/42/decision")
    finally:
        app.dependency_overrides = {}
    assert response.status_code == 200, response.text
    return response.json()


class TestDecisionEndpoint:
    def test_a_provider_row_becomes_the_panels_source(self):
        payload = _decision_payload([
            {"fiscal_year": 2026, "fiscal_quarter": 3, "revenue_yoy": 7.5, "eps_yoy": 1.54,
             "revenue_qoq": 1.2, "eps_qoq": 0.9, "revenue_base": 192869.0, "eps_base": 6.952,
             "reason": None, "source": "futu"},
        ])
        growth = payload["actual_growth"]
        # FastAPI serialises a NUMERIC as a string; compare numerically.
        assert float(growth["eps_yoy"]) == pytest.approx(0.0154)
        assert float(growth["revenue_yoy"]) == pytest.approx(0.075)
        assert growth["source"] == "provider"
        assert growth["base"]["revenue"] == pytest.approx(192869.0)

    def test_the_trend_ships_with_the_same_request(self):
        payload = _decision_payload([
            {"fiscal_year": 2026, "fiscal_quarter": 3, "revenue_yoy": 7.5, "eps_yoy": 1.54,
             "revenue_base": 192869.0, "eps_base": 6.952, "reason": None, "source": "futu"},
            {"fiscal_year": 2026, "fiscal_quarter": 2, "revenue_yoy": 10.99, "eps_yoy": 1.5,
             "revenue_base": 184504.0, "eps_base": 6.115, "reason": None, "source": "futu"},
        ])
        trend = payload["actual_growth_trend"]
        assert trend["status"] == "available"
        assert [p["fiscal_quarter"] for p in trend["points"]] == [2, 3]
        assert float(trend["points"][-1]["revenue_yoy"]) == pytest.approx(0.075)

    def test_no_stored_growth_leaves_the_computed_ratio_in_place(self):
        payload = _decision_payload([])
        growth = payload["actual_growth"]
        # 6.952 → 7.059 from the two stored actuals, and the payload says so.
        assert float(growth["eps_yoy"]) == pytest.approx(7.059 / 6.952 - 1, rel=1e-6)
        assert growth["source"] == "statements"
        assert payload["actual_growth_trend"]["status"] == "insufficient_history"
