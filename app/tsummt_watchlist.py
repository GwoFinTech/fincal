"""Read symbols from tsummt watchlist for sync universe."""
import psycopg2
import psycopg2.extras
import logging
from . import config

logger = logging.getLogger(__name__)


def get_tsummt_symbols() -> list[str]:
    """Read tsummt.watchlist and return all codes as-is (e.g. 'AAPL.US', '700.HK')."""
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


def get_symbols_by_market() -> dict[str, list[str]]:
    """Split tsummt symbols into {'US': [...], 'HK': [...]}."""
    codes = get_tsummt_symbols()
    result = {"US": [], "HK": []}
    for code in codes:
        if code.endswith(".HK"):
            result["HK"].append(code)
        elif code.endswith(".US"):
            # Strip .US suffix for fincal internal format
            result["US"].append(code.replace(".US", ""))
        else:
            # Bare ticker — assume US
            result["US"].append(code)
    return result


def get_futu_symbols() -> list[str]:
    """Return symbols in fincal internal format for Futu sync (AAPL.US, 0700.HK)."""
    codes = get_tsummt_symbols()
    result = []
    for code in codes:
        if code.endswith(".HK"):
            result.append(code)
        elif code.endswith(".US"):
            result.append(code)  # Keep .US for futu sync (to_futu_code handles it)
        else:
            result.append(f"{code}.US")
    return result
