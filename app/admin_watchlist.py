"""FinCal-managed global watchlist helpers.

This list is independent of users' personal watchlists and of the optional
external tsummt source.  It is the local fallback/override universe managed by
FinCal administrators.
"""
from .symbol import normalize


VALID_MARKETS = {"US", "HK"}


def normalize_managed_symbol(symbol: str, market: str) -> tuple[str, str]:
    market = market.strip().upper()
    if market not in VALID_MARKETS:
        raise ValueError("market_unsupported")
    value = symbol.strip().upper()
    if not value:
        raise ValueError("symbol_required")
    if market == "US":
        value = value.removesuffix(".US")
    return normalize(value, market), market
