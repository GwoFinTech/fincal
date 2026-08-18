"""Watchlist source: fetch from a remote HTTP/JSON API (Issue #6).

Uses unified provider_client for timeout, retry, error classification.
"""
import logging
from .base import WatchlistSource
from .. import config
from ..provider_client import ProviderConfig, http_get_json

logger = logging.getLogger(__name__)

_WRAP_KEYS = ("symbols", "data", "items", "list")
_HTTP_CFG = ProviderConfig(name="watchlist_http", timeout=15, max_retries=1)


class HttpWatchlistSource(WatchlistSource):
    """Fetch watchlist symbols from an HTTP JSON endpoint."""

    def fetch_symbols(self) -> list[str]:
        url = config.WATCHLIST_HTTP_URL
        if not url:
            raise ValueError("WATCHLIST_SOURCE=http but WATCHLIST_HTTP_URL is empty")

        payload = http_get_json(url, cfg=_HTTP_CFG)
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

        if items and isinstance(items[0], dict):
            codes = [str(item[field]) for item in items if item.get(field)]
        else:
            codes = [str(s) for s in items]

        logger.info("Fetched %d symbols from %s", len(codes), url)
        return codes

    @property
    def source_name(self) -> str:
        return "http"
