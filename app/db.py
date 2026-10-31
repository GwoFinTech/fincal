import math
import os
import sys
import threading
import time

import psycopg2
import psycopg2.extras
import psycopg2.pool
import logging
from contextlib import contextmanager
from . import config
from .errors import AppError

logger = logging.getLogger(__name__)

#: Two database budgets (Issue #101). A request must fail fast and degrade; the
#: weekly sync stages run bulk upserts/reconciles that legitimately take minutes.
API_ROLE = "api"
SYNC_ROLE = "sync"
ROLES = (API_ROLE, SYNC_ROLE)

#: Role the resident connection pool belongs to. ``_pool`` stays the public
#: singleton for the API role (tests/conftest.py resets it between tests);
#: scripts get their own pool under the same lazily-created rule.
_pool = None
_sync_pool = None
_pool_lock = threading.Lock()

#: A dependency that just refused a connection is remembered for a short window
#: (Issue #101). Without it, an endpoint that performs three independent reads
#: (``/api/universe``: universe, managed codes, names) paid ``connect_timeout``
#: three times for the *same* outage, so the bounded failure grew with the number
#: of reads instead of staying one budget. Only connection establishment failures
#: trip this: a slow statement is a different problem and must not be masked.
_connect_failures: dict[str, float] = {}
_failure_lock = threading.Lock()


def _in_connect_cooldown(role: str) -> bool:
    with _failure_lock:
        until = _connect_failures.get(role)
    return until is not None and time.monotonic() < until


def _note_connect_failure(role: str) -> None:
    with _failure_lock:
        _connect_failures[role] = time.monotonic() + config.DB_CONNECT_FAILURE_COOLDOWN_SECONDS


def _note_connect_success(role: str) -> None:
    with _failure_lock:
        _connect_failures.pop(role, None)


def reset_connection_cooldowns() -> None:
    """Forget cached connect failures (tests, operator recovery)."""
    with _failure_lock:
        _connect_failures.clear()


def _budget(role: str) -> dict[str, float]:
    """Timeout budget of ``role``: connect, per-session statement/lock/idle."""
    if role == SYNC_ROLE:
        return {
            "connect_timeout": config.DB_SYNC_CONNECT_TIMEOUT_SECONDS,
            "statement_timeout": config.DB_SYNC_STATEMENT_TIMEOUT_SECONDS,
            "lock_timeout": config.DB_SYNC_LOCK_TIMEOUT_SECONDS,
            "idle_in_transaction_session_timeout": (
                config.DB_SYNC_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS
            ),
            "acquire_timeout": config.DB_SYNC_POOL_ACQUIRE_TIMEOUT_SECONDS,
        }
    return {
        "connect_timeout": config.DB_CONNECT_TIMEOUT_SECONDS,
        "statement_timeout": config.DB_STATEMENT_TIMEOUT_SECONDS,
        "lock_timeout": config.DB_LOCK_TIMEOUT_SECONDS,
        "idle_in_transaction_session_timeout": (
            config.DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS
        ),
        "acquire_timeout": config.DB_POOL_ACQUIRE_TIMEOUT_SECONDS,
    }


def _entry_point_is_script() -> bool:
    """True when this process was started as a repository script (``scripts/…``).

    The database budget belongs to the *process*, not to the call: a request
    handler must never inherit a stage's multi-minute budget and vice versa.
    ``FINCAL_DB_ROLE`` overrides the detection for anything unusual (a wrapper
    that runs a stage from elsewhere, a REPL, …).
    """
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None) or ""
    return "/scripts/" in f"/{path.replace(chr(92), '/')}"


def resolve_role(role: str | None = None) -> str:
    """Resolve the budget role for this call: explicit → env → entry point."""
    if role is not None:
        if role not in ROLES:
            raise ValueError(f"unknown database role {role!r}; expected one of {ROLES}")
        return role
    configured = (os.getenv("FINCAL_DB_ROLE") or "").strip().lower()
    if configured in ROLES:
        return configured
    return SYNC_ROLE if _entry_point_is_script() else API_ROLE


def _milliseconds(seconds: float) -> int:
    return int(math.ceil(float(seconds) * 1000))


def _session_options(budget: dict[str, float]) -> str:
    """``options`` libpq passes as ``-c name=value`` session settings.

    ``statement_timeout`` bounds one statement, ``lock_timeout`` how long it may
    wait for a lock, and ``idle_in_transaction_session_timeout`` reaps a session
    that opened a transaction and stopped talking — without the last one a single
    abandoned transaction holds a pool slot forever (restart was the only cure).
    """
    return " ".join(
        f"-c {name}={_milliseconds(budget[name])}"
        for name in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout")
    )


def connect_kwargs(
    role: str | None = None,
    *,
    database: str | None = None,
    connect_timeout: float | None = None,
    statement_timeout: float | None = None,
) -> dict:
    """Connection parameters with the timeout contract applied (Issue #101).

    The single place that builds ``psycopg2.connect`` arguments. ``role`` picks
    the budget (API vs sync); ``database``/``connect_timeout``/
    ``statement_timeout`` narrow it for a specific caller (the cross-DB watchlist
    source, a bounded probe).
    """
    resolved = resolve_role(role)
    budget = _budget(resolved)
    if connect_timeout is not None:
        budget = {**budget, "connect_timeout": connect_timeout}
    if statement_timeout is not None:
        budget = {**budget, "statement_timeout": statement_timeout}
    # libpq treats ``connect_timeout=1`` as 2 s and 0 as "wait forever"; never
    # emit 0, since that is exactly the 131 s bug this function exists to stop.
    timeout_seconds = max(2, int(round(float(budget["connect_timeout"]))))
    return {
        "host": config.DB_HOST,
        "port": config.DB_PORT,
        "database": config.DB_NAME if database is None else database,
        "user": config.DB_USER,
        "password": config.DB_PASSWORD,
        "connect_timeout": timeout_seconds,
        "application_name": f"fincal-{resolved}",
        "options": _session_options(budget),
    }


def _db_busy(details: str, *, retry_after: int = 2) -> AppError:
    """503 the error contract can render (Issue #9): busy is not an internal error."""
    return AppError("db_busy", details, 503, headers={"Retry-After": str(retry_after)})


def _db_unavailable(details: str) -> AppError:
    return AppError("db_unavailable", details, 503, headers={"Retry-After": "5"})


class _WaitedPool:
    """``ThreadedConnectionPool`` that *waits* for a slot instead of raising.

    ``psycopg2``'s pool raises ``PoolError("connection pool exhausted")`` the
    moment ``maxconn`` connections are checked out (pool.py:91). With 40 request
    threads and 10 slots that turned ordinary concurrency into a 500. The
    semaphore makes the eleventh caller queue, bounded by
    ``DB_POOL_ACQUIRE_TIMEOUT_SECONDS``, and an exhausted budget degrades to a
    503 ``db_busy`` with ``Retry-After`` instead of an internal error.
    """

    def __init__(self, inner, *, maxconn: int, acquire_timeout: float, role: str = API_ROLE):
        self._inner = inner
        self._role = role
        self._slots = threading.BoundedSemaphore(maxconn)
        self._acquire_timeout = float(acquire_timeout)

    # -- psycopg2 pool surface used by this module --------------------------
    def getconn(self, *, timeout: float | None = None):
        if _in_connect_cooldown(self._role):
            # Same outage, already bounded once: do not pay connect_timeout again.
            raise _db_unavailable("database unreachable (connect cool-down)")
        budget = self._acquire_timeout if timeout is None else float(timeout)
        if not self._slots.acquire(timeout=max(0.0, budget)):
            logger.warning("database pool saturated: no slot within %.1fs", budget)
            raise _db_busy("connection pool saturated")
        conn = None
        try:
            conn = self._inner.getconn()
            _note_connect_success(self._role)
            return conn
        except psycopg2.pool.PoolError as exc:
            self._slots.release()
            logger.warning("database pool exhausted: %s", exc)
            raise _db_busy("connection pool exhausted") from exc
        except psycopg2.OperationalError as exc:
            self._slots.release()
            _note_connect_failure(self._role)
            # Connection establishment failed (connect_timeout, host down,
            # server not accepting). Not an internal error: the dependency is
            # unavailable, and the contract says 503 + Retry-After.
            logger.warning("database connect failed: %s", exc)
            raise _db_unavailable("database unreachable") from exc
        except BaseException:
            self._slots.release()
            raise

    def putconn(self, conn, close: bool = False):
        try:
            self._inner.putconn(conn, close=close)
        finally:
            # ``BoundedSemaphore.release`` raises if the count already equals the
            # initial value, which would turn a double-put into a crash: guard it.
            try:
                self._slots.release()
            except ValueError:  # pragma: no cover - double release
                logger.warning("database pool slot released twice")

    def closeall(self):
        self._inner.closeall()


def _make_pool(role: str) -> _WaitedPool:
    budget = _budget(role)
    # ``ThreadedConnectionPool`` opens ``minconn`` connections *during
    # construction* — and it holds an internal lock while doing so, so with a
    # black-holed host every caller queued behind the same 3 s connect and then
    # paid its own (measured 6 s for the first request). Build the pool empty and
    # only restore the retention floor: ``putconn`` keeps a connection while
    # ``len(idle) < minconn``, so the two warm connections appear as the first
    # requests succeed, and an outage costs exactly one ``connect_timeout``.
    inner = psycopg2.pool.ThreadedConnectionPool(
        minconn=0,
        maxconn=config.DB_POOL_MAX_CONN,
        **connect_kwargs(role),
    )
    inner.minconn = config.DB_POOL_MIN_CONN
    return _WaitedPool(
        inner,
        maxconn=config.DB_POOL_MAX_CONN,
        acquire_timeout=budget["acquire_timeout"],
        role=role,
    )


def _get_pool(role: str | None = None):
    """Lazily created pool for ``role`` (API by default)."""
    global _pool, _sync_pool
    resolved = resolve_role(role)
    with _pool_lock:
        if resolved == SYNC_ROLE:
            if _sync_pool is None:
                _sync_pool = _make_pool(SYNC_ROLE)
            return _sync_pool
        if _pool is None:
            _pool = _make_pool(API_ROLE)
        return _pool


@contextmanager
def db_cursor(role: str | None = None):
    """Yield a pooled cursor; every acquisition carries the timeout contract.

    ``role`` picks the budget (``api`` / ``sync``) and defaults to the process's
    own role, so a request handler and a sync stage never share a budget.
    """
    pool = _get_pool(role)
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
def db_connection(role: str | None = None):
    """Yield a dedicated (non-pooled) connection, closed on exit.

    Needed for session-scoped state that must live on a single connection for
    its whole lifetime — e.g. PostgreSQL advisory locks (Issue #31). A pooled
    cursor (``db_cursor``) may hand out a *different* connection for a later
    call, so a lock taken on one session and released via another would never
    actually free. Holding one fresh connection for the full duration avoids
    that class of bug entirely.
    """
    resolved = resolve_role(role)
    if _in_connect_cooldown(resolved):
        raise _db_unavailable("database unreachable (connect cool-down)")
    try:
        conn = psycopg2.connect(**connect_kwargs(resolved))
    except psycopg2.OperationalError as exc:
        _note_connect_failure(resolved)
        logger.warning("database connect failed: %s", exc)
        raise _db_unavailable("database unreachable") from exc
    _note_connect_success(resolved)
    try:
        yield conn
    finally:
        conn.close()


def ping(timeout: float | None = None) -> None:
    """Probe PostgreSQL with a hard wall-clock budget (Issue #101).

    Raises on failure *or* timeout. The probe runs on its own thread and the
    caller stops waiting at ``timeout``: ``connect_timeout`` bounds a connect, but
    a pool that is fully checked out (or a server that accepted the socket and
    then went silent) would otherwise keep ``/api/admin/health`` hanging — which
    is precisely the state the probe exists to report as ``not_ready``.
    """
    budget = config.DB_HEALTH_PROBE_TIMEOUT_SECONDS if timeout is None else float(timeout)
    outcome: dict[str, BaseException] = {}

    def _probe() -> None:
        try:
            with db_cursor() as cur:
                cur.execute("SELECT 1")
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            outcome["error"] = exc

    worker = threading.Thread(target=_probe, name="db-probe", daemon=True)
    worker.start()
    worker.join(budget)
    if worker.is_alive():
        raise TimeoutError(f"database probe exceeded {budget:.1f}s")
    if "error" in outcome:
        raise outcome["error"]



def init_db():
    """Create tables if not exist.

    Schema work runs on the sync budget (Issue #101): a migration statement is
    not a request, and the 5 s API ``statement_timeout`` would abort an index
    build on a large installation.
    """
    with db_cursor(SYNC_ROLE) as cur:
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
                company_name_en TEXT DEFAULT '',
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
                actual_currency TEXT,
                actual_basis TEXT,
                UNIQUE(symbol, market, report_date, report_type)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_earnings_report_date ON earnings(report_date);
        """)
        # CREATE TABLE does not add fields to installations created by older releases.
        # Issue #61: ``estimate_currency``/``estimate_basis`` existed but were never
        # written by any code path, and the actual side had no columns at all — so a
        # row could not say whether its estimate and its actual were even the same
        # unit of money. ``actual_currency``/``actual_basis`` mirror the estimate
        # pair; historical rows keep NULL, which the read path reads as unknown.
        # Issue #55: the iCal feed's ``lang=en`` branch read a ``company_name_en``
        # field that no table ever had, so an English subscription silently kept
        # the Chinese name.  The English name is a separate column on purpose:
        # Kurumi supplies the display (usually Chinese) name and Longbridge
        # ``static`` supplies the Latin one, and one must not overwrite the other.
        for column, definition in (
            ("company_name_en", "TEXT"),
            ("date_source", "TEXT NOT NULL DEFAULT 'unknown'"), ("date_status", "TEXT NOT NULL DEFAULT 'scheduled'"),
            ("estimate_source", "TEXT"), ("estimate_as_of", "TIMESTAMPTZ"), ("estimate_currency", "TEXT"),
            ("estimate_basis", "TEXT"), ("actual_source", "TEXT"), ("actual_as_of", "TIMESTAMPTZ"),
            ("actual_currency", "TEXT"), ("actual_basis", "TEXT"),
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
            company_name_en TEXT NOT NULL DEFAULT '',
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(symbol, market)
        )""")
        # Issue #55: installations created before the English column existed keep
        # their rows; CREATE TABLE does not add a column to them, so migrate
        # additively (same idempotent pattern as the earnings columns above).
        cur.execute("ALTER TABLE stock_names ADD COLUMN IF NOT EXISTS company_name_en TEXT NOT NULL DEFAULT ''")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_names_lookup ON stock_names(symbol, market)")
        cur.execute("""CREATE TABLE IF NOT EXISTS earnings_guidance_status (
            id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, market TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('available', 'unavailable')), reason TEXT,
            source TEXT NOT NULL DEFAULT 'longbridge', checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            payload JSONB NOT NULL DEFAULT '{}'::jsonb, UNIQUE(symbol, market, source)
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id)")
        # Converge legacy US watchlist rows written before normalize() stripped
        # market decorations. Rank all rows by their canonical key first so the
        # update cannot collide with an already-canonical row.
        cur.execute("""
            WITH normalized AS (
                SELECT id, user_id, market,
                       CASE
                           WHEN UPPER(symbol) LIKE 'US.%' THEN SUBSTRING(symbol FROM 4)
                           WHEN UPPER(symbol) LIKE '%.US' THEN LEFT(symbol, LENGTH(symbol) - 3)
                           ELSE UPPER(symbol)
                       END AS canonical_symbol
                FROM watchlist
                WHERE UPPER(market) = 'US'
            ), ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id, UPPER(market), canonical_symbol
                           ORDER BY id
                       ) AS duplicate_rank
                FROM normalized
            )
            DELETE FROM watchlist w
            USING ranked r
            WHERE w.id = r.id AND r.duplicate_rank > 1
        """)
        removed_legacy_duplicates = cur.rowcount
        cur.execute("""
            UPDATE watchlist
            SET symbol = CASE
                    WHEN UPPER(symbol) LIKE 'US.%' THEN SUBSTRING(UPPER(symbol) FROM 4)
                    WHEN UPPER(symbol) LIKE '%.US' THEN LEFT(UPPER(symbol), LENGTH(symbol) - 3)
                    ELSE UPPER(symbol)
                END,
                market = 'US'
            WHERE UPPER(market) = 'US'
              AND (UPPER(symbol) LIKE 'US.%' OR UPPER(symbol) LIKE '%.US'
                   OR market <> 'US' OR symbol <> UPPER(symbol))
        """)
        normalized_legacy_rows = cur.rowcount
        if removed_legacy_duplicates or normalized_legacy_rows:
            logger.info(
                "normalized legacy US watchlist rows: removed=%d updated=%d",
                removed_legacy_duplicates, normalized_legacy_rows,
            )
        # Converge the *numeric* US spellings the pre-#105 write path accepted
        # (Issue #108): ``('0700', 'US')`` is an HK code stored under the wrong
        # market — it never matched an earnings row, and since #105 it made every
        # read path that touched it a 500.  The code's own shape decides the
        # market, so the row becomes the canonical ``('0700.HK', 'HK')``.
        # Rows that already occupy the canonical key win: the duplicate is
        # removed first, because the UPDATE would collide on
        # UNIQUE(user_id, symbol, market).
        cur.execute("""
            WITH ghosts AS (
                SELECT id, user_id,
                       LPAD((symbol::int)::text, 4, '0') || '.HK' AS canonical_symbol
                FROM watchlist
                WHERE UPPER(market) = 'US' AND symbol ~ '^[0-9]{1,5}$'
            ), ranked AS (
                SELECT id, user_id, canonical_symbol,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id, canonical_symbol ORDER BY id
                       ) AS duplicate_rank
                FROM ghosts
            )
            DELETE FROM watchlist w
            USING ranked r
            WHERE w.id = r.id
              AND (r.duplicate_rank > 1
                   OR EXISTS (SELECT 1 FROM watchlist h
                              WHERE h.user_id = r.user_id
                                AND h.symbol = r.canonical_symbol
                                AND UPPER(h.market) = 'HK'))
        """)
        removed_numeric_duplicates = cur.rowcount
        cur.execute("""
            UPDATE watchlist
            SET symbol = LPAD((symbol::int)::text, 4, '0') || '.HK',
                market = 'HK'
            WHERE UPPER(market) = 'US' AND symbol ~ '^[0-9]{1,5}$'
        """)
        rewritten_numeric_rows = cur.rowcount
        # A six-digit numeric code (``159326``) is a Shenzhen fund whose suffix
        # was pasted off.  No market may be invented for it, so it is left as-is
        # (read paths skip it, /api/universe reports it) and only counted here.
        cur.execute("""
            SELECT COUNT(*) AS n FROM watchlist
            WHERE UPPER(market) = 'US' AND symbol ~ '^[0-9]{6,}$'
        """)
        unservable_numeric_rows = cur.fetchone()["n"]
        if removed_numeric_duplicates or rewritten_numeric_rows or unservable_numeric_rows:
            logger.info(
                "converged legacy numeric US watchlist rows: "
                "rewritten=%d removed=%d unservable=%d",
                rewritten_numeric_rows, removed_numeric_duplicates,
                unservable_numeric_rows,
            )

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
        with db_cursor(SYNC_ROLE) as cur:
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
