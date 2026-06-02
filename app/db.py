import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from . import config


def get_conn():
    return psycopg2.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
    )


@contextmanager
def db_cursor():
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


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
                UNIQUE(symbol, market, report_date, report_type, fiscal_year, fiscal_quarter)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_earnings_report_date ON earnings(report_date);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id);
        """)
