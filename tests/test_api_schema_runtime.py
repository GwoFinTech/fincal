"""Regression tests for API response schema/runtime compatibility."""
from datetime import date


def test_earning_item_accepts_postgres_date_value():
    """PostgreSQL returns DATE as datetime.date; FastAPI must serialize it."""
    from app.schemas import EarningItem

    item = EarningItem.model_validate({
        "id": 1,
        "symbol": "AAPL",
        "market": "US",
        "company_name": "Apple",
        "report_date": date(2026, 7, 14),
    })
    assert item.report_date == date(2026, 7, 14)
    assert item.model_dump(mode="json")["report_date"] == "2026-07-14"
