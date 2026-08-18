"""Local FinCal-managed global watchlist source.

Issue #4: CombinedWatchlistSource preserves stale upstream data
when the external source fails, instead of returning empty.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from .base import WatchlistSource, FetchResult
from .. import db

logger = logging.getLogger(__name__)


class ManagedWatchlistSource(WatchlistSource):
    def fetch_symbols(self) -> list[str]:
        with db.db_cursor() as cur:
            cur.execute("SELECT symbol FROM managed_watchlist ORDER BY market, symbol")
            return [row["symbol"] for row in cur.fetchall()]

    @property
    def source_name(self) -> str:
        return "managed"


class CombinedWatchlistSource(WatchlistSource):
    """Union an optional upstream source with the local managed universe.

    Issue #4: when upstream fails, preserve the last successful upstream
    data (stale-while-error) rather than clearing it.
    """
    def __init__(self, upstream: WatchlistSource | None, local: WatchlistSource | None = None):
        self.upstream = upstream
        self.local = local or ManagedWatchlistSource()
        self._upstream_stale_cache: list[str] | None = None
        self._last_upstream_error: str | None = None

    @property
    def source_name(self) -> str:
        parts = []
        if self.upstream:
            parts.append(self.upstream.source_name)
        parts.append(self.local.source_name)
        return "+".join(parts)

    def fetch_symbols(self) -> list[str]:
        merged: dict[str, None] = {}

        if self.upstream is not None:
            result = self.upstream.get_symbols_with_status(force_refresh=True)
            if result.ok:
                for symbol in result.symbols:
                    merged[symbol.strip().upper()] = None
                self._upstream_stale_cache = list(result.symbols)
                self._last_upstream_error = None
            else:
                # Upstream failed — use stale cached data
                logger.warning(
                    "upstream %s failed (%s); using stale cache with %d symbols",
                    self.upstream.source_name, result.error_code,
                    len(self._upstream_stale_cache or []),
                )
                self._last_upstream_error = result.error_code
                for symbol in (self._upstream_stale_cache or []):
                    merged[symbol.strip().upper()] = None

        for symbol in self.local.get_symbols(force_refresh=True):
            merged[symbol.strip().upper()] = None
        return sorted(merged)

    def get_symbols_with_status(self, *, force_refresh: bool = False) -> FetchResult:
        """Override to combine upstream status with local.

        Calls fetch_symbols() directly to avoid recursion through get_symbols().
        """
        if self._raw_cache is not None and not force_refresh:
            return FetchResult(
                symbols=list(self._raw_cache),
                stale=self._last_upstream_error is not None,
                source=self.source_name,
                last_success_at=self._last_success_at,
                error_code=self._last_upstream_error,
            )
        try:
            symbols = self.fetch_symbols()
            self._raw_cache = symbols
            self._last_success_at = datetime.now(timezone.utc)
            return FetchResult(
                symbols=list(symbols),
                stale=self._last_upstream_error is not None,
                source=self.source_name,
                last_success_at=self._last_success_at,
                error_code=self._last_upstream_error,
            )
        except Exception as exc:
            logger.warning("CombinedWatchlistSource fetch failed: %s", exc)
            self._last_upstream_error = str(exc)
            if self._raw_cache is not None:
                return FetchResult(
                    symbols=list(self._raw_cache),
                    stale=True,
                    source=self.source_name,
                    last_success_at=self._last_success_at,
                    error_code=str(exc),
                )
            return FetchResult(
                symbols=[],
                stale=False,
                source=self.source_name,
                last_success_at=None,
                error_code="unavailable",
            )
