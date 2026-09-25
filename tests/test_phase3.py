"""Phase 3 pure transformations: ratings, revisions, and earnings decision metrics."""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.phase3 import build_decision_metrics, rating_row, revision_trend


def test_rating_row_normalizes_longbridge_distribution_and_target():
    row = rating_row("AAPL", "US", {
        "instratings": {
            "ccy_symbol": "$", "recommend": "buy", "target": "323.28195", "updated_at": "2026 年 7 月 31 日",
            "evaluate": {"strong_buy": 22, "buy": 6, "hold": 14, "under": 2, "sell": 2},
        }
    })
    assert row[:3] == ("AAPL", "US", "$")
    assert row[3] == Decimal("323.28195")
    assert row[4:9] == (22, 6, 14, 2, 2)
    assert row[9] == "buy"


def test_revision_trend_uses_first_and_latest_snapshot_without_inventing_missing_metric():
    trend = revision_trend([
        {"eps_estimate": Decimal("2.00"), "revenue_estimate": Decimal("100"), "captured_at": "2026-01-01"},
        {"eps_estimate": Decimal("2.20"), "revenue_estimate": None, "captured_at": "2026-02-01"},
    ])
    assert trend["sample_count"] == 2
    assert trend["eps"]["change"] == Decimal("0.20")
    assert trend["eps"]["direction"] == "up"
    assert trend["revenue"]["change"] is None
    assert trend["revenue"]["direction"] == "unavailable"


def test_decision_metrics_calculates_growth_and_latest_beat_streak():
    rows = [
        {"id": 1, "fiscal_year": 2025, "fiscal_quarter": 4, "report_date": "2025-01-30", "eps_actual": Decimal("1.00"), "revenue_actual": Decimal("100"), "eps_estimate": Decimal("0.90"), "actual_currency": "USD", "actual_source": "longbridge"},
        {"id": 2, "fiscal_year": 2026, "fiscal_quarter": 1, "report_date": "2025-04-30", "eps_actual": Decimal("1.10"), "revenue_actual": Decimal("110"), "eps_estimate": Decimal("1.00"), "actual_currency": "USD", "actual_source": "longbridge"},
        {"id": 3, "fiscal_year": 2026, "fiscal_quarter": 2, "report_date": "2025-07-30", "eps_actual": Decimal("1.20"), "revenue_actual": Decimal("120"), "eps_estimate": Decimal("1.10"), "actual_currency": "USD", "actual_source": "longbridge"},
        {"id": 4, "fiscal_year": 2026, "fiscal_quarter": 3, "report_date": "2025-10-30", "eps_actual": Decimal("1.32"), "revenue_actual": Decimal("132"), "eps_estimate": Decimal("1.20"), "actual_currency": "USD", "actual_source": "longbridge"},
        {"id": 5, "fiscal_year": 2026, "fiscal_quarter": 4, "report_date": "2026-01-30", "eps_actual": Decimal("1.44"), "revenue_actual": Decimal("144"), "eps_estimate": Decimal("1.30"), "actual_currency": "USD", "actual_source": "longbridge"},
    ]
    metrics = build_decision_metrics(rows, earning_id=5)
    assert metrics["actual_growth"]["eps_yoy"] == Decimal("0.44")
    assert metrics["actual_growth"]["eps_yoy_reason"] is None
    assert metrics["actual_growth"]["revenue_qoq"] == Decimal("12") / Decimal("132")
    assert metrics["beat_miss_streak"] == {"kind": "beat", "count": 5}
    assert metrics["price_reaction"]["status"] == "unavailable"


# ── Issue #63: a derived ratio needs two comparably attributed actuals ─────

def _attributed(symbol, year, quarter, eps, currency="USD", source="longbridge"):
    return {
        "id": year * 10 + quarter, "symbol": symbol, "market": "US",
        "fiscal_year": year, "fiscal_quarter": quarter, "report_date": f"{year - 1}-11-01",
        "eps_actual": Decimal(eps), "revenue_actual": None, "eps_estimate": None,
        "actual_currency": currency, "actual_source": source,
    }


def test_growth_is_withheld_when_the_two_periods_do_not_declare_a_currency():
    """The unattributed prior row is exactly the production TSM/PDD pairing."""
    rows = [
        {"id": 1, "fiscal_year": 2025, "fiscal_quarter": 2, "report_date": "2025-07-15", "eps_actual": Decimal("2.3704")},
        {"id": 2, "fiscal_year": 2026, "fiscal_quarter": 2, "report_date": "2026-07-15", "eps_actual": Decimal("136.25"), "actual_currency": "TWD", "actual_source": "futu"},
    ]
    growth = build_decision_metrics(rows, earning_id=2)["actual_growth"]
    assert growth["eps_yoy"] is None
    assert growth["eps_yoy_reason"] == "currency_unknown"


def test_growth_is_withheld_when_the_two_periods_disagree_on_currency():
    rows = [
        _attributed("PDD", 2025, 2, "2.35", currency="USD"),
        _attributed("PDD", 2026, 2, "19.32", currency="CNY"),
    ]
    growth = build_decision_metrics(rows, earning_id=rows[1]["id"])["actual_growth"]
    assert growth["eps_yoy"] is None
    assert growth["eps_yoy_reason"] == "currency_mismatch"


def test_growth_is_withheld_across_providers_without_a_stated_basis():
    rows = [
        _attributed("LITE", 2025, 4, "4.371945", source="longbridge"),
        _attributed("LITE", 2026, 4, "-84.65", source="futu"),
    ]
    growth = build_decision_metrics(rows, earning_id=rows[1]["id"])["actual_growth"]
    assert growth["eps_yoy"] is None
    assert growth["eps_yoy_reason"] == "basis_unverified"


def test_growth_survives_across_providers_when_both_declare_the_same_basis():
    rows = [
        dict(_attributed("MU", 2025, 3, "2.754711", source="longbridge"), actual_basis="gaap"),
        dict(_attributed("MU", 2026, 3, "25.03", source="futu"), actual_basis="gaap"),
    ]
    growth = build_decision_metrics(rows, earning_id=rows[1]["id"])["actual_growth"]
    assert growth["eps_yoy"] == (Decimal("25.03") - Decimal("2.754711")) / Decimal("2.754711")
    assert growth["eps_yoy_reason"] is None


def test_growth_reason_is_scoped_to_the_metric_and_the_comparison_that_failed():
    """A missing prior period is not a comparability problem, and never leaks."""
    rows = [_attributed("AAPL", 2026, 3, "1.5")]
    growth = build_decision_metrics(rows, earning_id=rows[0]["id"])["actual_growth"]
    assert growth["eps_yoy"] is None and growth["eps_yoy_reason"] is None
    assert growth["eps_qoq"] is None and growth["eps_qoq_reason"] is None
    assert growth["revenue_yoy"] is None and growth["revenue_yoy_reason"] is None
