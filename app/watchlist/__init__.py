"""Watchlist data source abstraction.

Usage:
    from app.watchlist import get_source
    source = get_source()
    symbols = source.get_symbols()
    by_market = source.get_symbols_by_market()
    futu_codes = source.get_futu_symbols()

Configure via environment variables:
    WATCHLIST_SOURCE=tsummt|http  (default: tsummt)
    WATCHLIST_HTTP_URL=...        (required when source=http)
    WATCHLIST_HTTP_FIELD=code     (JSON field name, default: code)
"""
from .loader import get_source, invalidate_cache

__all__ = ["get_source", "invalidate_cache"]
