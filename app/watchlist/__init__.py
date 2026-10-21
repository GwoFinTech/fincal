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

#: Coarse identifiers the public (non-admin) views carry instead of the
#: configured database's host, port and name (Issue #93).  ``/api/universe`` is a
#: read-only page for every signed-in user, so it must not tell them the internal
#: topology; the admin overview keeps the full value.
PUBLIC_LOCATION_SHARED_DATABASE = "shared_database"
PUBLIC_LOCATION_LOCAL_MANAGED = "local_managed"
PUBLIC_LOCATION_EXTERNAL_FEED = "external_feed"


def source_description(*, include_location: bool = True) -> dict:
    """How the configured watchlist source stores the symbols.

    Shared by the admin overview and the read-only universe view so both
    describe the same configuration (``WATCHLIST_SOURCE``) the same way.

    ``include_location=False`` replaces ``host:port/database`` with a coarse
    identifier (``shared_database`` / ``local_managed`` / ``external_feed``): the
    public endpoint has no use for infrastructure details and used to hand them to
    every signed-in user (Issue #93).  The admin path keeps the exact value.
    """
    configured = config.WATCHLIST_SOURCE.strip().lower()
    shared_database = configured in ("tsummt", "hybrid")
    if include_location:
        location = (f"{config.DB_HOST}:{config.DB_PORT}/{config.TSUMMT_DB}"
                    if shared_database else "fincal.managed_watchlist")
    elif shared_database:
        location = PUBLIC_LOCATION_SHARED_DATABASE
    elif configured == "http":
        location = PUBLIC_LOCATION_EXTERNAL_FEED
    else:
        location = PUBLIC_LOCATION_LOCAL_MANAGED
    return {
        "configured": configured,
        "type": "postgresql" if shared_database else configured,
        "location": location,
        "transport": "database" if shared_database else "local",
        "external_dependency": configured == "tsummt",
        "local_fallback": configured in ("tsummt", "hybrid", "http"),
    }


__all__ = ["get_source", "invalidate_cache", "source_description"]
