"""Pydantic response models for FinCal API (OpenAPI schema).

All endpoints return typed response models so /openapi.json has full
schema coverage. Frontend TypeScript types are auto-generated from this.
"""
from __future__ import annotations

from datetime import date, datetime
from pydantic import BaseModel


# ── Common ─────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    error: ErrorDetail

class ErrorDetail(BaseModel):
    code: str
    details: str = ""


# ── User & Config ──────────────────────────────────────────────────

class AppConfig(BaseModel):
    auth_login_url: str = ""

class UserResponse(BaseModel):
    id: int
    portal_user_id: str
    email: str
    name: str
    role: str
    is_admin: bool
    ical_token: str
    ical_url: str


# ── Watchlist ──────────────────────────────────────────────────────

class WatchlistItem(BaseModel):
    symbol: str
    market: str

class WatchlistAddResult(BaseModel):
    symbol: str | None = None
    market: str | None = None
    status: str | None = None

class WatchlistRemoveResult(BaseModel):
    status: str

class SearchItem(BaseModel):
    symbol: str
    market: str
    company_name: str = ""


# ── Earnings ───────────────────────────────────────────────────────

class EarningItem(BaseModel):
    id: int
    symbol: str
    market: str
    company_name: str = ""
    report_date: date
    report_type: str = "Q"
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    before_after: str | None = None
    eps_estimate: float | None = None
    eps_actual: float | None = None
    revenue_estimate: float | None = None
    revenue_actual: float | None = None
    is_predicted: bool = False
    date_source: str | None = None
    date_status: str | None = None
    estimate_source: str | None = None
    actual_source: str | None = None
    consensus_eps_gaap: float | None = None
    consensus_eps_adjusted: float | None = None
    consensus_revenue: float | None = None
    consensus_ebit: float | None = None
    consensus_net_income: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

class InstitutionRating(BaseModel):
    currency_symbol: str | None = None
    target_price: float | None = None
    strong_buy: int | None = None
    buy: int | None = None
    hold: int | None = None
    underperform: int | None = None
    sell: int | None = None
    recommendation: str | None = None
    provider_updated_at: str | None = None
    fetched_at: datetime | None = None
    source: str = ""
    status: str | None = None

class GuidanceStatus(BaseModel):
    status: str
    reason: str | None = None
    source: str = ""
    checked_at: datetime | None = None

class Provenance(BaseModel):
    revision_trend: str = ""
    institution_rating: str = ""
    actual_growth: str = ""
    price_reaction: str = ""

class DecisionResponse(BaseModel):
    status: str
    revision_trend: list[dict] | None = None
    institution_rating: InstitutionRating | dict | None = None
    guidance: GuidanceStatus | dict | None = None
    provenance: Provenance | dict | None = None
    # Additional dynamic fields from build_decision_metrics
    model_config = {"extra": "allow"}


# ── Popular ────────────────────────────────────────────────────────

class PopularStocks(BaseModel):
    US: list[str]
    HK: list[str]


# ── Admin ──────────────────────────────────────────────────────────

class ManagedWatchlistItem(BaseModel):
    id: int
    symbol: str
    market: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

class ManagedWatchlistInput(BaseModel):
    symbol: str
    market: str = "US"

class SyncRun(BaseModel):
    id: int
    stage: str
    source: str
    status: str
    symbol_count: int = 0
    record_count: int = 0
    error_code: str | None = None
    details: dict | None = None
    attempt: int = 1
    timeout_seconds: int = 3600
    phase: str | None = None
    current: int | None = None
    total: int | None = None
    idempotency_key: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None

class SyncRunCancelResult(BaseModel):
    status: str
    run_id: int

class SyncRunRecoverResult(BaseModel):
    recovered: int

class SyncRunRetryResult(BaseModel):
    status: str
    original_run_id: int
    new_run_id: int

class AuditLogEntry(BaseModel):
    id: int
    action: str
    actor_id: str | None = None
    actor_email: str | None = None
    target: str | None = None
    details: dict = {}
    created_at: datetime | None = None

class SourceCheck(BaseModel):
    status: str
    error: str | None = None

class HealthResponse(BaseModel):
    status: str
    version: str = "dev"
    checks: dict[str, SourceCheck | dict] = {}

class ReadyResponse(BaseModel):
    status: str

class ProviderErrorStats(BaseModel):
    timeout: int = 0
    rate_limited: int = 0
    connection: int = 0
    invalid_response: int = 0

class ProviderStats(BaseModel):
    calls: int = 0
    success: int = 0
    success_rate: float = 0.0
    avg_ms: float = 0.0
    errors: ProviderErrorStats = ProviderErrorStats()

class CacheStats(BaseModel):
    hits: int = 0
    misses: int = 0
    hit_rate: float = 0.0
    stale_returns: int = 0
    refresh_ok: int = 0
    refresh_fail: int = 0

class SyncRunSummary(BaseModel):
    status: str
    cnt: int

class RecentSync(BaseModel):
    stage: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None

class SourceDistribution(BaseModel):
    source: str | None = None
    date_source: str | None = None
    estimate_source: str | None = None
    count: int

class DiagnosticsResponse(BaseModel):
    providers: dict[str, ProviderStats | dict] = {}
    cache: CacheStats = CacheStats()
    sync_runs_24h: list[SyncRunSummary | dict] = []
    recent_syncs: list[RecentSync | dict] = []

class OverviewSource(BaseModel):
    managed_watchlist_count: int = 0
    last_sync: dict = {}
    source_status: dict = {}

class OverviewResponse(BaseModel):
    source: OverviewSource | dict = {}
    managed_watchlist: list[ManagedWatchlistItem] = []
