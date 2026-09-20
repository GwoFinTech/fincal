"""fincal application configuration.

All values are read from environment variables with sensible defaults.
For production, set at minimum: DB_HOST, DB_PASSWORD, ICAL_BASE_URL.
"""
import os

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

# Kurumi (tsummt) API — preferred company-name source for FinCal symbols.
KURUMI_API_URL = os.getenv("KURUMI_API_URL", "http://localhost:8000")
KURUMI_API_TIMEOUT = float(os.getenv("KURUMI_API_TIMEOUT", "5"))

# Sync freshness monitoring (Issue #53). The pipeline declared by
# scripts/sync_all.sh runs weekly, so a stage (or the derived table it owns)
# older than this is reported as stale by app/freshness.py, /api/admin/health
# and scripts/check_sync_freshness.py. Default 192h = weekly + 1 day of grace.
SYNC_STAGE_STALE_AFTER_HOURS = float(os.getenv("SYNC_STAGE_STALE_AFTER_HOURS", "192"))
# Window used by /api/admin/diagnostics for the sync-run summary. A fixed 24h
# window is empty ~6 of 7 days for a weekly pipeline; 14 days covers at least
# one full cycle. The legacy `sync_runs_24h` field is kept for compatibility.
SYNC_RUNS_WINDOW_HOURS = int(os.getenv("SYNC_RUNS_WINDOW_HOURS", "336"))
