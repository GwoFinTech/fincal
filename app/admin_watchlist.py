"""FinCal-managed global watchlist helpers.

This list is independent of users' personal watchlists and of the optional
external tsummt source.  It is the local fallback/override universe managed by
FinCal administrators.
"""
from .symbol import market_mismatch, normalize


VALID_MARKETS = {"US", "HK"}


def normalize_managed_symbol(symbol: str, market: str) -> tuple[str, str]:
    market = market.strip().upper()
    if market not in VALID_MARKETS:
        raise ValueError("market_unsupported")
    value = symbol.strip().upper()
    if not value:
        raise ValueError("symbol_required")
    # A managed symbol joins the default calendar/export universe, so it must be
    # a symbol FinCal can actually serve: storing ``600028.SH`` as US would add a
    # permanent empty entry (the sync skips it, no earnings row can match it)
    # and hide the mistake behind an apparently successful save (Issue #66).
    if market_mismatch(value, market):
        raise ValueError("symbol_market_mismatch")
    return normalize(value, market), market
