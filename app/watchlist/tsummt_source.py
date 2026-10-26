"""Watchlist source: read directly from ``tsummt.watchlist`` PostgreSQL table.

This is the default source.  It connects to the tsummt database and reads
the watchlist table that the tsummt-web application manages.

Issue #4: raises on failure instead of returning empty, so
stale-while-error can preserve previous data.

Issue #101: the connection goes through ``app.db.connect_kwargs`` so this
cross-database read carries the same ``connect_timeout`` / ``statement_timeout``
contract as every other database access — it used to be a bare
``psycopg2.connect`` with no upper bound at all.
"""
import logging
import psycopg2
from .base import WatchlistSource
from .. import config, db

logger = logging.getLogger(__name__)


class TsummtWatchlistSource(WatchlistSource):
    """Read symbols from ``tsummt.watchlist`` table."""

    def fetch_symbols(self) -> list[str]:
        conn = psycopg2.connect(**db.connect_kwargs(database=config.TSUMMT_DB))
        try:
            cur = conn.cursor()
            cur.execute("SELECT code FROM watchlist ORDER BY id")
            codes = [row[0] for row in cur.fetchall()]
            cur.close()
            logger.info("Loaded %d symbols from tsummt.watchlist", len(codes))
            return codes
        finally:
            conn.close()

    @property
    def source_name(self) -> str:
        return "tsummt"
