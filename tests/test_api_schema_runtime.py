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


def test_earning_item_preserves_consensus_currency_and_fetched_at():
    """Issue #44: consensus_currency/consensus_fetched_at must survive schema roundtrip.

    SQL layer selects c.currency AS consensus_currency and c.fetched_at AS
    consensus_fetched_at, but the EarningItem response model dropped them so the
    Longbridge consensus detail panel showed placeholder values.
    """
    from app.schemas import EarningItem

    item = EarningItem.model_validate({
        "id": 1,
        "symbol": "AAPL",
        "market": "US",
        "report_date": date(2026, 8, 1),
        "consensus_eps_gaap": 1.5,
        "consensus_currency": "USD",
        "consensus_fetched_at": "2026-08-01T00:00:00Z",
    })
    dumped = item.model_dump()
    assert dumped["consensus_currency"] == "USD"
    assert dumped["consensus_fetched_at"] is not None
    assert dumped["consensus_eps_gaap"] == 1.5
