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
from .. import config
from .loader import get_source, invalidate_cache


def source_description() -> dict:
    """How the configured watchlist source stores the symbols.

    Shared by the admin overview and the read-only universe view so both
    describe the same configuration (``WATCHLIST_SOURCE``) the same way.
    """
    configured = config.WATCHLIST_SOURCE.strip().lower()
    return {
        "configured": configured,
        "type": "postgresql" if configured in ("tsummt", "hybrid") else configured,
        "location": f"{config.DB_HOST}:{config.DB_PORT}/{config.TSUMMT_DB}" if configured in ("tsummt", "hybrid") else "fincal.managed_watchlist",
        "transport": "database" if configured in ("tsummt", "hybrid") else "local",
        "external_dependency": configured == "tsummt",
        "local_fallback": configured in ("tsummt", "hybrid", "http"),
    }


__all__ = ["get_source", "invalidate_cache", "source_description"]
