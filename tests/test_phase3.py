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
        {"id": 1, "fiscal_year": 2025, "fiscal_quarter": 4, "report_date": "2025-01-30", "eps_actual": Decimal("1.00"), "revenue_actual": Decimal("100"), "eps_estimate": Decimal("0.90")},
        {"id": 2, "fiscal_year": 2026, "fiscal_quarter": 1, "report_date": "2025-04-30", "eps_actual": Decimal("1.10"), "revenue_actual": Decimal("110"), "eps_estimate": Decimal("1.00")},
        {"id": 3, "fiscal_year": 2026, "fiscal_quarter": 2, "report_date": "2025-07-30", "eps_actual": Decimal("1.20"), "revenue_actual": Decimal("120"), "eps_estimate": Decimal("1.10")},
        {"id": 4, "fiscal_year": 2026, "fiscal_quarter": 3, "report_date": "2025-10-30", "eps_actual": Decimal("1.32"), "revenue_actual": Decimal("132"), "eps_estimate": Decimal("1.20")},
        {"id": 5, "fiscal_year": 2026, "fiscal_quarter": 4, "report_date": "2026-01-30", "eps_actual": Decimal("1.44"), "revenue_actual": Decimal("144"), "eps_estimate": Decimal("1.30")},
    ]
    metrics = build_decision_metrics(rows, earning_id=5)
    assert metrics["actual_growth"]["eps_yoy"] == Decimal("0.44")
    assert metrics["actual_growth"]["revenue_qoq"] == Decimal("12") / Decimal("132")
    assert metrics["beat_miss_streak"] == {"kind": "beat", "count": 5}
    assert metrics["price_reaction"]["status"] == "unavailable"
