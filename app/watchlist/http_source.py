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

Issue #4: raises on failure instead of returning empty, so
stale-while-error can preserve previous data.
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
            raise ValueError("WATCHLIST_SOURCE=http but WATCHLIST_HTTP_URL is empty")

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
                raise ValueError(
                    f"HTTP source: no recognised key in response, got {list(payload.keys())}"
                )

        if not isinstance(items, list):
            raise TypeError(f"HTTP source: expected list, got {type(items).__name__}")

        # Extract codes
        if items and isinstance(items[0], dict):
            codes = [str(item[field]) for item in items if item.get(field)]
        else:
            codes = [str(s) for s in items]

        logger.info("Fetched %d symbols from %s", len(codes), url)
        return codes

    @property
    def source_name(self) -> str:
        return "http"
