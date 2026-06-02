"""Watchlist source: read directly from ``tsummt.watchlist`` PostgreSQL table.

This is the default source.  It connects to the tsummt database and reads
the watchlist table that the tsummt-web application manages.
"""
import logging
import psycopg2
from .base import WatchlistSource
from .. import config

logger = logging.getLogger(__name__)


class TsummtWatchlistSource(WatchlistSource):
    """Read symbols from ``tsummt.watchlist`` table."""

    def fetch_symbols(self) -> list[str]:
        try:
            conn = psycopg2.connect(
                host=config.DB_HOST,
                port=config.DB_PORT,
                database=config.TSUMMT_DB,
                user=config.DB_USER,
                password=config.DB_PASSWORD,
            )
            cur = conn.cursor()
            cur.execute("SELECT code FROM watchlist ORDER BY id")
            codes = [row[0] for row in cur.fetchall()]
            cur.close()
            conn.close()
            logger.info(f"Loaded {len(codes)} symbols from tsummt.watchlist")
            return codes
        except Exception as e:
            logger.warning(f"Failed to read tsummt.watchlist: {e}")
            return []
