"""Live view of the default calendar/export symbol universe (Issue #58).

``/api/earnings`` (default view), ``/api/export`` and ``/api/popular`` share one
symbol universe: the configured watchlist source.  That universe used to be read
once at import time, so a symbol added upstream (or through the admin watchlist)
was synced and predicted but invisible in the default calendar/export until the
next container restart — and a source that happened to be unavailable at startup
pinned the whole process to the hardcoded fallback.

This module keeps the read off the request path with a TTL cache while letting
both sources of change land without a restart:

* the admin watchlist endpoints call :func:`invalidate_symbol_universe`, so a
  UI change is visible on the very next request;
* an upstream change (or an outage during startup) is picked up within
  ``config.UNIVERSE_CACHE_TTL_SECONDS`` instead of never.

A failed read keeps the last known (or fallback) universe, exposes the error in
``/api/admin/diagnostics`` and is retried after a short bounded backoff rather
than being cached for the full TTL: recovery needs no restart, and an outage
does not turn into one source query per request.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from . import config
from .singleflight import Singleflight

logger = logging.getLogger(__name__)

# Last-resort universe.  Only reachable while the configured source has never
# been read successfully; it is never presented as fresh (see ``stale`` /
# ``error_code`` in ``SymbolUniverse.status``).
FALLBACK_US = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
FALLBACK_HK = ["0700.HK", "9988.HK", "1810.HK"]

NOT_LOADED = "universe_not_loaded"
SOURCE_ERROR = "watchlist_source_error"
EMPTY_SOURCE = "watchlist_empty"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class SymbolUniverse:
    """TTL-cached universe of default symbols, read from the watchlist source.

    ``ttl_seconds`` bounds how long a successful read is reused;
    ``error_retry_seconds`` bounds how long a failed read is remembered before
    the source is retried.  ``source_factory`` is injectable so tests can drive
    the cache without a database.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float | None = None,
        error_retry_seconds: float | None = None,
        fallback_us: list[str] | None = None,
        fallback_hk: list[str] | None = None,
        source_factory=None,
    ):
        self._ttl = float(config.UNIVERSE_CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds)
        self._error_retry = float(
            config.UNIVERSE_ERROR_RETRY_SECONDS if error_retry_seconds is None else error_retry_seconds
        )
        self._fallback_us = list(FALLBACK_US if fallback_us is None else fallback_us)
        self._fallback_hk = list(FALLBACK_HK if fallback_hk is None else fallback_hk)
        self._source_factory = source_factory
        self._flight = Singleflight()
        self._flight_key = f"symbol-universe:{id(self)}"
        self._mutex = threading.Lock()

        self._us = list(self._fallback_us)
        self._hk = list(self._fallback_hk)
        self._source = "unloaded"
        self._error_code: str | None = NOT_LOADED
        self._stale = True
        self._last_success_at: datetime | None = None
        self._fetched_at: datetime | None = None
        self._expires_at = 0.0  # monotonic; 0 forces the first read

    # -- public API --------------------------------------------------------
    def get(self, *, force_refresh: bool = False) -> tuple[list[str], list[str]]:
        """Return ``(US, HK)``, reading the source at most once per TTL."""
        with self._mutex:
            if not force_refresh and time.monotonic() < self._expires_at:
                return list(self._us), list(self._hk)
        try:
            self._flight.do(self._flight_key, self._reload)
        except Exception as exc:  # pragma: no cover - _reload swallows failures
            logger.warning("symbol universe refresh failed: %s", exc)
        with self._mutex:
            return list(self._us), list(self._hk)

    def status(self) -> dict:
        """Operator-visible snapshot.  Never triggers a read, so an admin
        diagnostics call cannot block on the cross-database source."""
        with self._mutex:
            return {
                "symbol_count": len(self._us) + len(self._hk),
                "us_count": len(self._us),
                "hk_count": len(self._hk),
                "source": self._source,
                "stale": self._stale,
                "error_code": self._error_code,
                "last_success_at": _iso(self._last_success_at),
                "fetched_at": _iso(self._fetched_at),
                "ttl_seconds": self._ttl,
            }

    def invalidate(self) -> None:
        """Force the next :meth:`get` to re-read the source."""
        with self._mutex:
            self._expires_at = 0.0

    # -- internals ---------------------------------------------------------
    def _source_obj(self):
        if self._source_factory is not None:
            return self._source_factory()
        from .watchlist import get_source

        return get_source()

    def _reload(self) -> None:
        by_market: dict[str, list[str]] = {}
        status = None
        try:
            by_market, status = self._source_obj().get_symbols_by_market_with_status(force_refresh=True)
        except Exception as exc:
            logger.warning("Failed to load watchlist source: %s", exc)

        us = list(by_market.get("US", []))
        hk = list(by_market.get("HK", []))

        with self._mutex:
            now = time.monotonic()
            self._fetched_at = datetime.now(timezone.utc)
            if us or hk:
                self._us, self._hk = us, hk
                self._source = (status.source if status is not None else "") or "watchlist"
                self._error_code = status.error_code if status is not None else None
                self._stale = bool(self._error_code) or bool(status and status.stale)
                if status is not None and status.last_success_at:
                    self._last_success_at = status.last_success_at
            else:
                # Nothing usable upstream: keep the last good universe, or fall
                # back to the hardcoded one if the source never worked, but
                # never present either as fresh.
                if self._last_success_at is None:
                    self._us, self._hk = list(self._fallback_us), list(self._fallback_hk)
                    self._source = "fallback"
                self._error_code = (
                    (status.error_code if status is not None else None)
                    or (EMPTY_SOURCE if status is not None else SOURCE_ERROR)
                )
                self._stale = True
            healthy = self._error_code is None and bool(us or hk)
            self._expires_at = now + (self._ttl if healthy else self._error_retry)


_universe = SymbolUniverse()


def popular_stocks(*, force_refresh: bool = False) -> tuple[list[str], list[str]]:
    """``(US, HK)`` symbols of the default calendar/export universe."""
    return _universe.get(force_refresh=force_refresh)


def popular_stocks_by_market(*, force_refresh: bool = False) -> dict[str, list[str]]:
    """The universe as ``{"US": [...], "HK": [...]}`` (``/api/popular``)."""
    us, hk = popular_stocks(force_refresh=force_refresh)
    return {"US": us, "HK": hk}


def universe_status() -> dict:
    """Cached universe metadata for admin diagnostics (no read triggered)."""
    return _universe.status()


def invalidate_symbol_universe() -> None:
    """Drop the cached universe and everything derived from it (Issue #58).

    Called after an admin watchlist mutation so a symbol added/removed through
    the UI shows up on the next request instead of after the TTL — including the
    response caches that embed the universe.
    """
    _universe.invalidate()
    try:
        from .watchlist import invalidate_cache

        invalidate_cache()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("watchlist source cache invalidation failed: %s", exc)
    try:
        # Owned by the API router, which knows which response caches embed the
        # universe; imported lazily to keep this module import-cycle free.
        from .routers.api import invalidate_universe_caches

        invalidate_universe_caches()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("derived cache invalidation failed: %s", exc)
