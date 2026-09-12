import psycopg2
import psycopg2.extras
import psycopg2.pool
import logging
from contextlib import contextmanager
from . import config

logger = logging.getLogger(__name__)

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=config.DB_HOST,
            port=config.DB_PORT,
            database=config.DB_NAME,
            user=config.DB_USER,
            password=config.DB_PASSWORD,
        )
    return _pool


@contextmanager
def db_cursor():
    pool = _get_pool()
    conn = pool.getconn()
    cur = None
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if cur is not None:
            cur.close()
        pool.putconn(conn)


@contextmanager
def db_connection():
    """Yield a dedicated (non-pooled) connection, closed on exit.

    Needed for session-scoped state that must live on a single connection for
    its whole lifetime — e.g. PostgreSQL advisory locks (Issue #31). A pooled
    cursor (``db_cursor``) may hand out a *different* connection for a later
    call, so a lock taken on one session and released via another would never
    actually free. Holding one fresh connection for the full duration avoids
    that class of bug entirely.
    """
    conn = psycopg2.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
    )
    try:
        yield conn
    finally:
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
                is_predicted BOOLEAN DEFAULT FALSE,
                date_source TEXT NOT NULL DEFAULT 'unknown',
                date_status TEXT NOT NULL DEFAULT 'scheduled',
                estimate_source TEXT,
                estimate_as_of TIMESTAMPTZ,
                estimate_currency TEXT,
                estimate_basis TEXT,
                actual_source TEXT,
                actual_as_of TIMESTAMPTZ,
                UNIQUE(symbol, market, report_date, report_type)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_earnings_report_date ON earnings(report_date);
        """)
        # CREATE TABLE does not add fields to installations created by older releases.
        for column, definition in (
            ("date_source", "TEXT NOT NULL DEFAULT 'unknown'"), ("date_status", "TEXT NOT NULL DEFAULT 'scheduled'"),
            ("estimate_source", "TEXT"), ("estimate_as_of", "TIMESTAMPTZ"), ("estimate_currency", "TEXT"),
            ("estimate_basis", "TEXT"), ("actual_source", "TEXT"), ("actual_as_of", "TIMESTAMPTZ"),
        ):
            cur.execute(f"ALTER TABLE earnings ADD COLUMN IF NOT EXISTS {column} {definition}")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS earnings_estimate_snapshots (
                id BIGSERIAL PRIMARY KEY, earning_id INTEGER NOT NULL REFERENCES earnings(id) ON DELETE CASCADE,
                source TEXT NOT NULL, captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), eps_estimate NUMERIC,
                revenue_estimate NUMERIC, currency TEXT, basis TEXT, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                UNIQUE(earning_id, source, captured_at)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_estimate_snapshots_earning_time ON earnings_estimate_snapshots(earning_id, captured_at DESC)")
        cur.execute("""CREATE TABLE IF NOT EXISTS earnings_consensus (
            id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, market TEXT NOT NULL, fiscal_year INTEGER NOT NULL, fiscal_quarter INTEGER NOT NULL,
            currency TEXT, eps_gaap NUMERIC, eps_adjusted NUMERIC, revenue NUMERIC, ebit NUMERIC, net_income NUMERIC, normalized_net_income NUMERIC,
            source TEXT NOT NULL DEFAULT 'longbridge', fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE(symbol, market, fiscal_year, fiscal_quarter, source)
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_earnings_consensus_lookup ON earnings_consensus(symbol, market, fiscal_year, fiscal_quarter)")
        cur.execute("""CREATE TABLE IF NOT EXISTS earnings_forecast_eps (
            id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, market TEXT NOT NULL,
            forecast_start_date DATE NOT NULL, forecast_end_date DATE,
            eps_low NUMERIC, eps_high NUMERIC, eps_mean NUMERIC, eps_median NUMERIC,
            institution_total INTEGER, institution_up INTEGER, institution_down INTEGER,
            source TEXT NOT NULL DEFAULT 'longbridge', fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE(symbol, market, forecast_start_date, forecast_end_date, source)
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_earnings_forecast_eps_lookup ON earnings_forecast_eps(symbol, market, forecast_start_date DESC)")
        cur.execute("""CREATE TABLE IF NOT EXISTS earnings_institution_ratings (
            id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, market TEXT NOT NULL, currency_symbol TEXT,
            target_price NUMERIC, strong_buy INTEGER NOT NULL DEFAULT 0, buy INTEGER NOT NULL DEFAULT 0,
            hold INTEGER NOT NULL DEFAULT 0, underperform INTEGER NOT NULL DEFAULT 0, sell INTEGER NOT NULL DEFAULT 0,
            recommendation TEXT, provider_updated_at TEXT, source TEXT NOT NULL DEFAULT 'longbridge',
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE(symbol, market, source)
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_institution_ratings_lookup ON earnings_institution_ratings(symbol, market)")
        cur.execute("""CREATE TABLE IF NOT EXISTS stock_names (
            id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, market TEXT NOT NULL,
            company_name TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(symbol, market)
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_names_lookup ON stock_names(symbol, market)")
        cur.execute("""CREATE TABLE IF NOT EXISTS earnings_guidance_status (
            id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, market TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('available', 'unavailable')), reason TEXT,
            source TEXT NOT NULL DEFAULT 'longbridge', checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            payload JSONB NOT NULL DEFAULT '{}'::jsonb, UNIQUE(symbol, market, source)
        )""")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id);
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS managed_watchlist (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(symbol, market)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sync_runs (
                id BIGSERIAL PRIMARY KEY,
                stage TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'skipped', 'interrupted', 'cancelled')),
                source TEXT NOT NULL,
                symbol_count INTEGER NOT NULL DEFAULT 0,
                record_count INTEGER NOT NULL DEFAULT 0,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                error_code TEXT,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ,
                heartbeat_at TIMESTAMPTZ,
                attempt INTEGER NOT NULL DEFAULT 1,
                idempotency_key TEXT,
                phase TEXT,
                current INTEGER,
                total INTEGER,
                timeout_seconds INTEGER NOT NULL DEFAULT 3600
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sync_runs_started_at ON sync_runs(started_at DESC);
        """)
        # Add new columns to existing installations
        for column, definition in (
            ("heartbeat_at", "TIMESTAMPTZ"),
            ("attempt", "INTEGER NOT NULL DEFAULT 1"),
            ("idempotency_key", "TEXT"),
            ("phase", "TEXT"),
            ("current", "INTEGER"),
            ("total", "INTEGER"),
            ("timeout_seconds", "INTEGER NOT NULL DEFAULT 3600"),
        ):
            cur.execute(f"ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS {column} {definition}")
        # Update CHECK constraint for new statuses
        cur.execute("ALTER TABLE sync_runs DROP CONSTRAINT IF EXISTS sync_runs_status_check")
        cur.execute("ALTER TABLE sync_runs ADD CONSTRAINT sync_runs_status_check CHECK (status IN ('running', 'success', 'failed', 'skipped', 'interrupted', 'cancelled'))")
        # Idempotency key uniqueness is enforced only among *running* rows.
        # A scheduled sync that already reached a terminal state must be able to
        # start a fresh attempt (new row) with the same fixed key on the next
        # cron run, while still preventing two concurrent running attempts
        # (Issue #38). The old all-time-unique index (previously created here)
        # is dropped first so existing installations converge on this definition.
        cur.execute("DROP INDEX IF EXISTS idx_sync_runs_idempotency_key")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_runs_idempotency_key "
            "ON sync_runs(idempotency_key) WHERE idempotency_key IS NOT NULL AND status='running'"
        )

        # Audit log for admin operations (Issue #11)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id BIGSERIAL PRIMARY KEY,
                action TEXT NOT NULL,
                actor_id TEXT,
                actor_email TEXT,
                target TEXT,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_log_created_at ON admin_audit_log(created_at DESC)")

    ensure_fiscal_identity_index()


def ensure_fiscal_identity_index() -> bool:
    """Enforce one confirmed row per fiscal period (Issue #50).

    ``UNIQUE(symbol, market, report_date, report_type)`` keys a row by its
    *display* date, so a provider that moves an announcement (or a second
    provider with a different date) inserts a duplicate row for the same fiscal
    period — 439 confirmed duplicate groups in production, 913 rows.  The fiscal
    identity ``(symbol, market, fiscal_year, fiscal_quarter)`` is the real key;
    a partial unique index over confirmed rows makes a recurrence impossible.

    The index can only be built once the historical duplicates are gone, so the
    creation is attempted *after* the schema body and skipped (with a warning
    naming the reconciliation script) while duplicates remain — otherwise
    ``init_db()`` would abort at startup on any pre-existing installation.  The
    check runs in its own transaction, so a failed build cannot poison the
    schema transaction above.  It converges automatically: after
    ``scripts/reconcile_fiscal_rows.py --apply`` the next startup creates it.
    """
    from psycopg2 import errors

    try:
        with db_cursor() as cur:
            cur.execute("""
                SELECT count(*) AS groups FROM (
                    SELECT 1 FROM earnings
                    WHERE is_predicted = FALSE AND fiscal_year IS NOT NULL AND fiscal_quarter IS NOT NULL
                    GROUP BY symbol, market, fiscal_year, fiscal_quarter
                    HAVING count(*) > 1
                ) duplicated_periods
            """)
            row = cur.fetchone()
            duplicates = row["groups"] if row else 0
            if duplicates:
                logger.warning(
                    "fiscal identity index not created: %d confirmed duplicate "
                    "fiscal period(s) still present — run "
                    "scripts/reconcile_fiscal_rows.py (dry-run first, then --apply) "
                    "to merge them (Issue #50)",
                    duplicates,
                )
                return False
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_earnings_fiscal_identity "
                "ON earnings(symbol, market, fiscal_year, fiscal_quarter) "
                "WHERE is_predicted = FALSE AND fiscal_year IS NOT NULL AND fiscal_quarter IS NOT NULL"
            )
        logger.info("fiscal identity index present (idx_earnings_fiscal_identity)")
        return True
    except (errors.UniqueViolation, errors.DuplicateObject) as exc:
        logger.warning("fiscal identity index not created this startup: %s", exc)
        return False
