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


def group_symbols_by_market(codes: list[str]) -> dict[str, list[str]]:
    """Bucket canonical ``TICKER.MARKET`` codes into ``{'US': [...], 'HK': [...]}``.

    HK codes are normalized to the canonical 4-digit zero-padded form so they
    match the earnings table keys (``700.HK`` -> ``0700.HK``); codes without a
    known suffix are treated as US tickers.
    """
    result: dict[str, list[str]] = {"US": [], "HK": []}
    for code in codes:
        code = str(code).strip().upper()
        if code.endswith(".HK"):
            result["HK"].append(normalize(code[:-3], "HK"))
        elif code.endswith(".US"):
            result["US"].append(code[:-3])  # strip .US suffix
        else:
            result["US"].append(code)
    return result


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
        by_market, _status = self.get_symbols_by_market_with_status(force_refresh=force_refresh)
        return by_market

    def get_symbols_by_market_with_status(
        self, *, force_refresh: bool = False
    ) -> tuple[dict[str, list[str]], FetchResult]:
        """``(by_market, status)`` — :meth:`get_symbols_by_market` plus the
        staleness/error metadata callers need to keep a cached copy of the
        universe honest (Issue #58)."""
        status = self.get_symbols_with_status(force_refresh=force_refresh)
        return group_symbols_by_market(status.symbols), status

    def get_futu_symbols(self, *, force_refresh: bool = False) -> list[str]:
        """Symbols in fincal canonical format (``AAPL.US``, ``0700.HK``).

        Codes that OpenD cannot route (A-shares, other exchanges) are dropped
        rather than suffixed with ``.US`` — see
        :meth:`get_futu_symbols_with_skipped` (Issue #49).
        """
        symbols, _skipped = self.get_futu_symbols_with_skipped(force_refresh=force_refresh)
        return symbols

    def get_futu_symbols_with_skipped(
        self, *, force_refresh: bool = False
    ) -> tuple[list[str], list[str]]:
        """Return ``(futu_symbols, skipped_codes)`` in fincal canonical format.

        Only codes that map onto a Futu market FinCal can sync (US / HK) are
        returned. Any other exchange suffix (``000651.SZ``, ``600519.SH``, …)
        is skipped and reported: blindly appending ``.US`` used to produce
        impossible codes such as ``US.000651.SZ``, which failed on every Futu
        run and inflated the sync's failure counts (Issue #49).
        """
        codes = self.get_symbols(force_refresh=force_refresh)
        symbols: list[str] = []
        skipped: list[str] = []
        for code in codes:
            code = code.strip().upper()
            if code.endswith(".HK"):
                symbols.append(normalize(code[:-3], "HK"))
            elif code.endswith(".US"):
                symbols.append(code)
            elif "." in code:
                skipped.append(code)
            else:
                symbols.append(f"{code}.US")
        if skipped:
            logger.info(
                "%d watchlist code(s) skipped for Futu sync (not US/HK): %s",
                len(skipped), ", ".join(skipped[:10]),
            )
        return symbols, skipped

    def refresh(self) -> None:
        """Bust the cache."""
        self._raw_cache = None
