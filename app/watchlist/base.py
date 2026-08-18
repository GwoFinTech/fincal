"""Abstract base class for watchlist data sources.

Subclasses implement `fetch_symbols()` only.  Derived accessors
(get_symbols_by_market, get_futu_symbols) are provided by the base
with an in-memory cache that callers can bust via `refresh()`.

Issue #4: stale-while-error — when upstream fails, return last
successful data with explicit stale/error metadata.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..symbol import normalize

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Result of a symbol fetch, carrying staleness metadata."""
    symbols: list[str]
    stale: bool = False
    source: str = ""
    last_success_at: datetime | None = None
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None

    @property
    def unavailable(self) -> bool:
        return not self.symbols and self.error_code is not None


class WatchlistSource(ABC):
    """Base watchlist source.  Only ``fetch_symbols`` needs implementation."""

    _raw_cache: list[str] | None = None
    _last_success_at: datetime | None = None
    _last_error_code: str | None = None

    # -- abstract ----------------------------------------------------------
    @abstractmethod
    def fetch_symbols(self) -> list[str]:
        """Return raw symbol codes from the upstream source.

        Codes should be in TICKER.MARKET format (e.g. ``AAPL.US``, ``0700.HK``).
        Bare tickers (e.g. ``AAPL``) are treated as US stocks.
        """
        ...

    @property
    def source_name(self) -> str:
        return type(self).__name__

    # -- concrete helpers --------------------------------------------------
    def get_symbols(self, *, force_refresh: bool = False) -> list[str]:
        """Raw codes with stale-while-error cache (issue #4)."""
        result = self.get_symbols_with_status(force_refresh=force_refresh)
        return result.symbols

    def get_symbols_with_status(self, *, force_refresh: bool = False) -> FetchResult:
        """Fetch symbols and return with staleness metadata.

        On upstream failure, returns the last cached data with stale=True
        if available, or empty with error_code if never succeeded.
        """
        if self._raw_cache is not None and not force_refresh:
            return FetchResult(
                symbols=list(self._raw_cache),
                stale=self._last_error_code is not None,
                source=self.source_name,
                last_success_at=self._last_success_at,
                error_code=self._last_error_code,
            )
        try:
            symbols = self.fetch_symbols()
            self._raw_cache = symbols
            self._last_success_at = datetime.now(timezone.utc)
            self._last_error_code = None
            return FetchResult(
                symbols=list(symbols),
                stale=False,
                source=self.source_name,
                last_success_at=self._last_success_at,
            )
        except Exception as exc:
            logger.warning("fetch_symbols failed for %s: %s", self.source_name, exc)
            self._last_error_code = f"{self.source_name}_fetch_failed"
            if self._raw_cache is not None:
                # Return stale cached data
                return FetchResult(
                    symbols=list(self._raw_cache),
                    stale=True,
                    source=self.source_name,
                    last_success_at=self._last_success_at,
                    error_code=self._last_error_code,
                )
            # Never succeeded — return empty
            return FetchResult(
                symbols=[],
                stale=False,
                source=self.source_name,
                last_success_at=None,
                error_code="unavailable",
            )

    def get_symbols_by_market(self, *, force_refresh: bool = False) -> dict[str, list[str]]:
        """``{'US': ['AAPL', …], 'HK': ['0700.HK', …]}`` — HK codes are
        normalized to the canonical 4-digit zero-padded form so they match
        the earnings table keys (700.HK -> 0700.HK)."""
        codes = self.get_symbols(force_refresh=force_refresh)
        result: dict[str, list[str]] = {"US": [], "HK": []}
        for code in codes:
            code = code.strip().upper()
            if code.endswith(".HK"):
                result["HK"].append(normalize(code[:-3], "HK"))
            elif code.endswith(".US"):
                result["US"].append(code[:-3])  # strip .US suffix
            else:
                result["US"].append(code)
        return result

    def get_futu_symbols(self, *, force_refresh: bool = False) -> list[str]:
        """Symbols in fincal canonical format (``AAPL.US``, ``0700.HK``)."""
        codes = self.get_symbols(force_refresh=force_refresh)
        result: list[str] = []
        for code in codes:
            code = code.strip().upper()
            if code.endswith(".HK"):
                result.append(normalize(code[:-3], "HK"))
            elif code.endswith(".US"):
                result.append(code)
            else:
                result.append(f"{code}.US")
        return result

    def refresh(self) -> None:
        """Bust the cache."""
        self._raw_cache = None
