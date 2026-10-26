"""fincal application configuration.

All values are read from environment variables with sensible defaults.
For production, set at minimum: DB_HOST, DB_PASSWORD, ICAL_BASE_URL.
"""
import os
from urllib.parse import urlsplit

APP_NAME = "fincal"
PORT = int(os.getenv("PORT", "8000"))

# Auth - headers injected by Traefik forwardAuth
HEADER_USER_ID = "X-User-Id"
HEADER_USER_EMAIL = "X-User-Email"
HEADER_USER_NAME = "X-User-Name"
HEADER_USER_ROLE = "X-User-Role"

# Auth login URL - set to your auth provider login page
# Leave empty to hide login button (single-user setups)
AUTH_LOGIN_URL=os.getenv("AUTH_LOGIN_URL", "")

# Database
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "fincal")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD=os.getenv("DB_PASSWORD", "")

# External DB for shared watchlist (only used when WATCHLIST_SOURCE=tsummt)
TSUMMT_DB = os.getenv("TSUMMT_DB", "tsummt")

# Database access budgets (Issue #101).
#
# Neither the connection pool nor the cross-DB watchlist source had any timeout,
# so a dependency that *drops* packets instead of answering with RST held a
# request thread for as long as the kernel kept retrying (measured: 131 s for a
# single libpq connect), and every concurrent request burned its own 30 s budget
# behind it. These values are the contract `app/db.py` enforces at the only
# place where a connection is opened, so no call site has to remember them.
#
# Two budgets on purpose: the request path must fail fast and degrade, while the
# weekly sync stages run bulk upserts / reconciles that legitimately take minutes
# — a single API-sized statement_timeout would kill them.
#
# ``connect_timeout`` is API 3 s: a slow-but-healthy cross-DB read must not be
# misread as an outage, and 3 s still keeps the worst case of a request path
# reload inside the 5 s degradation budget.
DB_CONNECT_TIMEOUT_SECONDS = float(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "3"))
DB_STATEMENT_TIMEOUT_SECONDS = float(os.getenv("DB_STATEMENT_TIMEOUT_SECONDS", "5"))
DB_LOCK_TIMEOUT_SECONDS = float(os.getenv("DB_LOCK_TIMEOUT_SECONDS", "2"))
DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS = float(
    os.getenv("DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS", "15")
)
# Script-role budgets: a stage is already wrapped in ``timeout`` by
# scripts/sync_all.sh, so the session budgets only need to be generous enough to
# never cut a healthy bulk statement.
DB_SYNC_CONNECT_TIMEOUT_SECONDS = float(os.getenv("DB_SYNC_CONNECT_TIMEOUT_SECONDS", "10"))
DB_SYNC_STATEMENT_TIMEOUT_SECONDS = float(os.getenv("DB_SYNC_STATEMENT_TIMEOUT_SECONDS", "300"))
DB_SYNC_LOCK_TIMEOUT_SECONDS = float(os.getenv("DB_SYNC_LOCK_TIMEOUT_SECONDS", "10"))
DB_SYNC_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS = float(
    os.getenv("DB_SYNC_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS", "600")
)

# Pool sizing (Issue #101). FastAPI's sync endpoints run on a 40-thread limiter,
# and the pool used to cap at 10 *without waiting*: the 11th concurrent request
# raised ``PoolError: connection pool exhausted``, which the error contract
# reported as a 500. The pool is now the same order as the thread limit and
# ``db_cursor`` waits for a slot instead of failing (see ``db.getconn``);
# production PostgreSQL has max_connections=100 with ~27 in use, so 20 stays
# well inside the remaining headroom.
#
# ``DB_POOL_MIN_CONN`` is the number of idle connections kept warm. The pool is
# built empty and warmed on a daemon thread: ``ThreadedConnectionPool`` otherwise
# opens ``minconn`` connections *during construction*, so with a black-holed host
# whoever creates the pool paid ``minconn × connect_timeout`` before the first
# caller could report a degraded answer.
DB_POOL_MIN_CONN = int(os.getenv("DB_POOL_MIN_CONN", "2"))
DB_POOL_MAX_CONN = int(os.getenv("DB_POOL_MAX_CONN", "20"))
# How long a request thread may wait for a pool slot before it degrades to
# 503 db_busy (+ Retry-After) instead of queueing forever.
DB_POOL_ACQUIRE_TIMEOUT_SECONDS = float(os.getenv("DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "5"))
DB_SYNC_POOL_ACQUIRE_TIMEOUT_SECONDS = float(
    os.getenv("DB_SYNC_POOL_ACQUIRE_TIMEOUT_SECONDS", "60")
)
# Health/readiness probe budget (Issue #101). The probe used to be a plain
# ``SELECT 1`` on the same pool: with the database unreachable it hung instead of
# answering ``postgresql: not_ready``, so monitoring could not tell "hanging"
# from "down".
DB_HEALTH_PROBE_TIMEOUT_SECONDS = float(os.getenv("DB_HEALTH_PROBE_TIMEOUT_SECONDS", "1.5"))
# Cool-down after a *failed connection*, not a failed statement (Issue #101).
# One outage must cost at most one ``connect_timeout``: an endpoint with three
# independent reads used to pay it three times in a row. Kept short so recovery
# needs no restart; a successful connect clears it immediately.
DB_CONNECT_FAILURE_COOLDOWN_SECONDS = float(
    os.getenv("DB_CONNECT_FAILURE_COOLDOWN_SECONDS", "5")
)

# Watchlist data source: hybrid (tsummt DB + local fallback), tsummt, local, or http
WATCHLIST_SOURCE = os.getenv("WATCHLIST_SOURCE", "hybrid")
WATCHLIST_HTTP_URL = os.getenv("WATCHLIST_HTTP_URL", "")
WATCHLIST_HTTP_FIELD = os.getenv("WATCHLIST_HTTP_FIELD", "code")

# Default calendar/export universe freshness (Issue #58). The universe is the
# configured watchlist source, read through a short TTL cache instead of once at
# import time: the cross-DB read stays off the request path, while an upstream
# add/remove still lands without a container restart.
UNIVERSE_CACHE_TTL_SECONDS = float(os.getenv("UNIVERSE_CACHE_TTL_SECONDS", "120"))
# A failed read must not be parked for the whole TTL: the previous (or fallback)
# universe is kept, the error is exposed in /api/admin/diagnostics, and the
# source is retried after this bounded backoff — so recovery needs no restart,
# and an outage does not become one source query per request.
UNIVERSE_ERROR_RETRY_SECONDS = float(os.getenv("UNIVERSE_ERROR_RETRY_SECONDS", "15"))
# How long a caller on the request path may wait for a reload it did not start
# (Issue #101). The reload runs on its own thread so a hanging cross-DB source
# can never hold a request thread: the caller waits at most this long, then gets
# the current snapshot and the refresh lands when it lands. 1 s is ~100x a normal
# (local, indexed) watchlist read, so a healthy source is indistinguishable from
# the old synchronous behaviour, while the worst case of an endpoint that performs
# several reads stays well inside the 5 s degradation budget. The DB layer bounds
# the reload itself via connect_timeout + the connect cool-down.
UNIVERSE_REQUEST_WAIT_SECONDS = float(os.getenv("UNIVERSE_REQUEST_WAIT_SECONDS", "1"))

# Futu OpenD connection (optional - used for earnings date sync)
FUTU_HOST = os.getenv("FUTU_HOST", "127.0.0.1")
# The host's OpenD relay listens on 11112.  Production containers override
# this through .env, but the default must also work for host-run sync scripts.
FUTU_PORT = int(os.getenv("FUTU_PORT", "11112"))
# Per-symbol call watchdog for scripts/sync_futu.py (Issue #48). Each OpenD call
# is bounded by wall clock so a wedged symbol is counted as a failure instead of
# hanging the batch; the windows stay configurable because a slow-but-healthy
# response must not be misread as a timeout.
FUTU_DATES_TIMEOUT_SECONDS = int(os.getenv("FUTU_DATES_TIMEOUT_SECONDS", "15"))
FUTU_ACTUALS_TIMEOUT_SECONDS = int(os.getenv("FUTU_ACTUALS_TIMEOUT_SECONDS", "20"))
# OpenD pacing for scripts/sync_futu.py (Issue #49). OpenD rejects both
# financials interfaces with "…频率太高，请求失败，每30秒最多30次。" once the
# window budget is spent, and a full sync used to fire ~3 unpaced calls per
# symbol and trip the quota within seconds. The defaults keep headroom under
# the documented "30 calls / 30 s" limit; one limiter is shared by every Futu
# call in the process, and a rejected call waits out a whole window before its
# bounded retry.
FUTU_MAX_CALLS_PER_30S = int(os.getenv("FUTU_MAX_CALLS_PER_30S", "28"))
FUTU_RATE_LIMIT_WINDOW_SECONDS = float(os.getenv("FUTU_RATE_LIMIT_WINDOW_SECONDS", "30"))
FUTU_RATE_LIMIT_MAX_RETRIES = int(os.getenv("FUTU_RATE_LIMIT_MAX_RETRIES", "2"))
# Consecutive identical quota rejections after which a stage stops hammering
# OpenD and reports the provider outage instead of per-symbol failures.
FUTU_RATE_LIMIT_CIRCUIT_BREAKER = int(os.getenv("FUTU_RATE_LIMIT_CIRCUIT_BREAKER", "10"))

# Longbridge CLI (optional - primary earnings data source)
LONGBRIDGE_APP_KEY = os.getenv("LONGBRIDGE_APP_KEY", "")
LONGBRIDGE_APP_SECRET=os.getenv("LONGBRIDGE_APP_SECRET", "")
LONGBRIDGE_ACCESS_TOKEN=os.getenv("LONGBRIDGE_ACCESS_TOKEN", "")

# iCal subscription base URL (must be publicly accessible)
ICAL_BASE_URL = os.getenv("ICAL_BASE_URL", "")

# Forward horizon (days) of the calendar read outlets (Issue #59).  This is the
# single source of truth for "how far ahead does the app look": the iCal feed
# (app/routers/ical.py) and the default window of /api/earnings
# (app/routers/api.py, app/earnings.py) all read it, so no outlet can silently
# stop short of another.
#
# The invariant that matters: the predictor places dates up to
# scripts/predict_earnings.py::MAX_FUTURE_DAYS ahead (4 quarters, ~1 year), so
# any outlet that is meant to show the calendar must reach at least that far.
# The feed used to stop at a hardcoded 120 days while the app UI (and the
# predictor) used 420: 80% of the already-computed predictions were visible in
# the app and permanently missing from the subscription, with no error anywhere.
# The default is pinned to MAX_FUTURE_DAYS by tests/test_ical_subscription.py;
# narrowing it through the environment variable re-introduces that gap, because
# a prediction outside the window has no other channel to reach the subscriber.
CALENDAR_FORWARD_DAYS = int(os.getenv("CALENDAR_FORWARD_DAYS", "420"))

# Capacity and window bounds for the `/api/earnings` response cache (Issue #86).
#
# `LayerCache` used to be an unbounded dict: every distinct `start`/`end` a
# caller sent stayed resident forever together with its payload (measured
# against the production universe: 0.80 MiB for the default window, 1.05 MiB
# for the widest accepted one), so an authenticated caller could grow the
# process without limit just by varying the dates. Eviction is LRU, which is
# harmless for the UI — one displayed month is one key.
#
# The cap is 128, not the 512 slots the iCal feed cache uses: at 1.05 MiB per
# payload, 512 keys would allow ~537 MiB of retained bodies for a service whose
# baseline RSS is ~52 MiB. The real working set is "users x months browsed":
# 6 users x 14 windows (12 browsed months + watchlist + default) = 84, so 128
# keeps every user's recent months cached while capping the route at ~134 MiB.
# Raise it through the environment if a deployment serves many more users.
#
# `EARNINGS_MAX_WINDOW_DAYS` bounds how far apart `start` and `end` may be.
# The default leaves room for the shipped horizons (default view:
# 7 + CALENDAR_FORWARD_DAYS; watchlist view: CALENDAR_FORWARD_DAYS) plus 90
# days, so no legitimate outlet is affected while a decade-long request is
# rejected with `invalid_window` instead of being cached. Bulk history stays
# available through `/api/export`, which is a separate path.
EARNINGS_CACHE_MAX_ENTRIES = int(os.getenv("EARNINGS_CACHE_MAX_ENTRIES", "128"))
EARNINGS_MAX_WINDOW_DAYS = int(os.getenv("EARNINGS_MAX_WINDOW_DAYS", str(CALENDAR_FORWARD_DAYS + 90)))

# Kurumi (tsummt) API — preferred company-name source for FinCal symbols.
# Issue #70: the default is FinCal's *own* listen address inside the production
# container, and FinCal answers the old probe path (`/api/config`) itself, so a
# reachability probe against it could never fail. `kurumi_target_is_self()`
# below lets the dependency probe recognise that self-reference (and an unset
# value) instead of reporting a false `healthy`.
KURUMI_API_URL = os.getenv("KURUMI_API_URL", "http://localhost:8000")
KURUMI_API_TIMEOUT = float(os.getenv("KURUMI_API_TIMEOUT", "5"))

# Hosts that mean "this machine". A base URL pointing at one of them *and* at
# this service' own port cannot be a reachable external dependency.
_SELF_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def kurumi_target_is_self(value: str | None = None) -> bool:
    """True when ``value`` (default ``KURUMI_API_URL``) points back at FinCal.

    Guards the Kurumi dependency probe (Issue #70): the shipped default
    ``http://localhost:8000`` matches ``PORT``, so probing it only proves that
    this process is alive. An empty/unset value is treated the same way —
    "no address configured" is not a healthy dependency either.
    """
    base = (KURUMI_API_URL if value is None else value) or ""
    base = base.strip()
    if not base:
        return True
    try:
        parsed = urlsplit(base if "://" in base else f"//{base}")
    except ValueError:
        return False
    if (parsed.hostname or "").lower() not in _SELF_HOSTS:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return port == PORT


# Sync freshness monitoring (Issue #53). The pipeline declared by
# scripts/sync_all.sh runs weekly, so a stage (or the derived table it owns)
# older than this is reported as stale by app/freshness.py, /api/admin/health
# and scripts/check_sync_freshness.py. Default 192h = weekly + 1 day of grace.
SYNC_STAGE_STALE_AFTER_HOURS = float(os.getenv("SYNC_STAGE_STALE_AFTER_HOURS", "192"))
# Window used by /api/admin/diagnostics for the sync-run summary. A fixed 24h
# window is empty ~6 of 7 days for a weekly pipeline; 14 days covers at least
# one full cycle. The legacy `sync_runs_24h` field is kept for compatibility.
SYNC_RUNS_WINDOW_HOURS = int(os.getenv("SYNC_RUNS_WINDOW_HOURS", "336"))

# Sync-run timeout reaper (Issue #33). `sync_runs.timeout_seconds` is written per
# run by start_run() but nothing ever read it, and the periodic reaper meant to
# enforce it had no caller — so a run whose owning process died (stage killed by
# the cron wrapper's `timeout`, container restarted mid-run) stayed `running`
# until the container started again, and the admin panel showed a job that was
# not running. The application lifespan now starts one daemon thread that
# reaps runs past their own `timeout_seconds`; disable it with
# SYNC_RUN_REAPER_ENABLED=false (startup/manual recovery is unaffected).
SYNC_RUN_REAPER_ENABLED = os.getenv("SYNC_RUN_REAPER_ENABLED", "true").strip().lower() not in (
    "0", "false", "no", "off",
)
# Reaping pass interval. A run is interrupted within its own timeout plus this
# interval; startup recovery already covers the previous process's runs.
SYNC_RUN_REAPER_INTERVAL_SECONDS = float(os.getenv("SYNC_RUN_REAPER_INTERVAL_SECONDS", "300"))

# Per-stage wall-clock budget of the weekly pipeline (Issue #78).
#
# `scripts/sync_all.sh` wraps every stage in `timeout <budget>`, and the reaper
# judges a run by `sync_runs.timeout_seconds` — so the stage script must write
# *that same number* into its own audit row. Before this, only the reader half
# existed: `start_run()` always wrote its 3600s default while the real budget
# lived in the shell, so a stage killed by cron kept a `running` row until the
# 3600s cutoff (predictions are budgeted 600s → ~55 minutes of ghost job, during
# which a manual re-run returned "already running, skipping" and refreshed
# nothing), and a budget raised above 3600 made the reaper interrupt a *live* run
# and silently discard its real terminal state.
#
# This table is the value `sync_all.sh` mirrors (its `FINCAL_STAGE_TIMEOUT_*`
# defaults); `tests/test_issue78_stage_timeouts.py` runs the contract —
# changing either side alone fails the suite.
STAGE_TIMEOUT_SECONDS = {
    "longbridge": 900,
    "futu": 1500,
    "stock_names": 900,
    "consensus": 2400,
    "prediction": 600,
    # Not a pipeline stage: the on-demand EPS field refill re-reads the same
    # Futu interface as the `futu` stage, with the same budget.
    "futu_eps_field_fix": 1500,
}


def stage_timeout(stage: str) -> int:
    """Wall-clock budget, in seconds, that the pipeline enforces for ``stage``.

    Reads ``FINCAL_STAGE_TIMEOUT_<STAGE>`` — the very variable
    ``scripts/sync_all.sh`` reads — so a stage's ``sync_runs`` row always
    declares the budget the stage really runs under (Issue #78).
    """
    try:
        default = STAGE_TIMEOUT_SECONDS[stage]
    except KeyError:
        raise KeyError(
            f"unknown sync stage {stage!r}; register it in "
            "app.config.STAGE_TIMEOUT_SECONDS"
        ) from None
    return int(os.getenv(f"FINCAL_STAGE_TIMEOUT_{stage.upper()}", str(default)))
