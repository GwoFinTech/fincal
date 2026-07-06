"""Watchlist source: fetch from a remote HTTP/JSON API.

Supports two response shapes:
  1. Array of strings:       ``["AAPL.US", "0700.HK"]``
  2. Array of objects:       ``[{"code": "AAPL.US"}, …]``  (field configurable)

Also handles wrapped payloads::

    {"symbols": [...]}   {"data": [...]}
    {"items": [...]}     {"list": [...]}

Configure with:

* ``WATCHLIST_HTTP_URL``   -- endpoint URL (required)
* ``WATCHLIST_HTTP_FIELD`` -- object key for symbol code (default: ``code``)
"""
import json
import logging
import urllib.request
from .base import WatchlistSource
from .. import config

logger = logging.getLogger(__name__)

_WRAP_KEYS = ("symbols", "data", "items", "list")


class HttpWatchlistSource(WatchlistSource):
    """Fetch watchlist symbols from an HTTP JSON endpoint."""

    def fetch_symbols(self) -> list[str]:
        url = config.WATCHLIST_HTTP_URL
        if not url:
            logger.error("WATCHLIST_SOURCE=http but WATCHLIST_HTTP_URL is empty")
            return []

        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode())

            field = config.WATCHLIST_HTTP_FIELD

            # Unwrap top-level object if needed
            items = payload
            if isinstance(payload, dict):
                for key in _WRAP_KEYS:
                    if key in payload:
                        items = payload[key]
                        break
                else:
                    logger.error(
                        "HTTP source: no recognised key in response, got %s",
                        list(payload.keys()),
                    )
                    return []

            if not isinstance(items, list):
                logger.error("HTTP source: expected list, got %s", type(items).__name__)
                return []

            # Extract codes
            if items and isinstance(items[0], dict):
                codes = [str(item[field]) for item in items if item.get(field)]
            else:
                codes = [str(s) for s in items]

            logger.info(f"Fetched {len(codes)} symbols from {url}")
            return codes

        except Exception as e:
            logger.error(f"Failed to fetch watchlist from {url}: {e}")
            return []
