import psycopg2
import psycopg2.extras
from psycopg_pool import ConnectionPool
from contextlib import contextmanager
from . import config

_pool = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=f"host={config.DB_HOST} port={config.DB_PORT} dbname={config.DB_NAME} user={config.DB_USER} password={config.DB_PASSWORD}",
            min_size=2,
            max_size=10,
            kwargs={"cursor_factory": psycopg2.extras.RealDictCursor},
        )
    return _pool


def get_conn():
    """Get a connection from the pool (for backward compat with scripts)."""
    return _get_pool().getconn()


@contextmanager
def db_cursor():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        pool.putconn(conn)


def init_db():
    """Create tables if not exist."""
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                portal_user_id INTEGER UNIQUE NOT NULL,
                email TEXT NOT NULL,
                name TEXT DEFAULT '',
                ical_token TEXT UNIQUE NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT 'US',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, symbol, market)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS earnings (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT 'US',
                company_name TEXT DEFAULT '',
                report_date DATE NOT NULL,
                report_type TEXT DEFAULT 'Q',
                fiscal_year INTEGER,
                fiscal_quarter INTEGER,
                eps_estimate NUMERIC,
                eps_actual NUMERIC,
                revenue_estimate NUMERIC,
                revenue_actual NUMERIC,
                before_after TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                is_predicted BOOLEAN DEFAULT FALSE,
                UNIQUE(symbol, market, report_date, report_type)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_earnings_report_date ON earnings(report_date);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id);
        """)
