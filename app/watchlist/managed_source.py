"""Local FinCal-managed global watchlist source."""
from .base import WatchlistSource
from .. import db


class ManagedWatchlistSource(WatchlistSource):
    def fetch_symbols(self) -> list[str]:
        with db.db_cursor() as cur:
            cur.execute("SELECT symbol FROM managed_watchlist ORDER BY market, symbol")
            return [row["symbol"] for row in cur.fetchall()]


class CombinedWatchlistSource(WatchlistSource):
    """Union an optional upstream source with the local managed universe."""
    def __init__(self, upstream: WatchlistSource | None, local: WatchlistSource | None = None):
        self.upstream = upstream
        self.local = local or ManagedWatchlistSource()

    def fetch_symbols(self) -> list[str]:
        merged: dict[str, None] = {}
        if self.upstream is not None:
            for symbol in self.upstream.get_symbols(force_refresh=True):
                merged[symbol.strip().upper()] = None
        for symbol in self.local.get_symbols(force_refresh=True):
            merged[symbol.strip().upper()] = None
        return sorted(merged)
