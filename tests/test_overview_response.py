"""Regression tests for admin overview response preservation (Issue #26)."""


def test_overview_response_preserves_source_metadata_and_external_symbols():
    from app.schemas import OverviewResponse

    result = OverviewResponse.model_validate({
        "source": {
            "configured": "hybrid",
            "type": "postgresql",
            "location": "tsummt-db:5432/tsummt",
            "transport": "database",
            "external_dependency": False,
            "local_fallback": True,
            "symbol_count": 25,
            "error_code": None,
            "stale": False,
            "last_success_at": "2026-08-21T12:00:00+00:00",
        },
        "external_symbols": ["AAPL", "0700.HK"],
        "managed_watchlist": [],
    })
    payload = result.model_dump()

    assert payload["source"]["configured"] == "hybrid"
    assert payload["source"]["symbol_count"] == 25
    assert payload["source"]["stale"] is False
    assert payload["external_symbols"] == ["AAPL", "0700.HK"]
