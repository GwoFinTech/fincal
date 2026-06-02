"""Abstract base class for watchlist data sources.

Subclasses implement `fetch_symbols()` only.  Derived accessors
(get_symbols_by_market, get_futu_symbols) are provided by the base
with an in-memory cache that callers can bust via `refresh()`.
"""
from abc import ABC, abstractmethod


class WatchlistSource(ABC):
    """Base watchlist source.  Only ``fetch_symbols`` needs implementation."""

    _raw_cache: list[str] | None = None

    # -- abstract ----------------------------------------------------------
    @abstractmethod
    def fetch_symbols(self) -> list[str]:
        """Return raw symbol codes from the upstream source.

        Codes should be in TICKER.MARKET format (e.g. ``AAPL.US``, ``0700.HK``).
        Bare tickers (e.g. ``AAPL``) are treated as US stocks.
        """
        ...

    # -- concrete helpers --------------------------------------------------
    def get_symbols(self, *, force_refresh: bool = False) -> list[str]:
        """Raw codes with single-flight in-memory cache."""
        if self._raw_cache is None or force_refresh:
            self._raw_cache = self.fetch_symbols()
        return list(self._raw_cache)

    def get_symbols_by_market(self, *, force_refresh: bool = False) -> dict[str, list[str]]:
        """``{'US': ['AAPL', …], 'HK': ['0700.HK', …]}``"""
        codes = self.get_symbols(force_refresh=force_refresh)
        result: dict[str, list[str]] = {"US": [], "HK": []}
        for code in codes:
            code = code.strip().upper()
            if code.endswith(".HK"):
                result["HK"].append(code)
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
            if code.endswith(".HK") or code.endswith(".US"):
                result.append(code)
            else:
                result.append(f"{code}.US")
        return result

    def refresh(self) -> None:
        """Bust the cache."""
        self._raw_cache = None
