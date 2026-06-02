"""Factory: instantiate the configured watchlist source once.

Set ``WATCHLIST_SOURCE`` to choose the backend (default: ``tsummt``).
"""
import logging
from .. import config
from .base import WatchlistSource

logger = logging.getLogger(__name__)

_source: WatchlistSource | None = None


def _create_source() -> WatchlistSource:
    src = config.WATCHLIST_SOURCE.lower().strip()
    if src == "http":
        from .http_source import HttpWatchlistSource
        logger.info("Watchlist source: HTTP (%s)", config.WATCHLIST_HTTP_URL)
        return HttpWatchlistSource()
    # Default / explicit tsummt
    from .tsummt_source import TsummtWatchlistSource
    logger.info("Watchlist source: tsummt DB")
    return TsummtWatchlistSource()


def get_source() -> WatchlistSource:
    """Return the singleton watchlist source (created on first call)."""
    global _source
    if _source is None:
        _source = _create_source()
    return _source


def invalidate_cache() -> None:
    """Drop the singleton so the next ``get_source()`` recreates it."""
    global _source
    if _source is not None:
        _source.refresh()
    _source = None
