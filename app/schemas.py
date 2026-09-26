"""Pydantic response models for FinCal API (OpenAPI schema).

All endpoints return typed response models so /openapi.json has full
schema coverage. Frontend TypeScript types are auto-generated from this.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
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
    portal_user_id: int
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
    # Attribution of the estimate/actual pair (Issue #61): which currency and
    # which base each side is stated in, and — when they are not comparable — the
    # language-independent reason the UI must render "—" instead of a surplus.
    estimate_currency: str | None = None
    estimate_basis: str | None = None
    actual_currency: str | None = None
    actual_basis: str | None = None
    comparison_unavailable_reason: str | None = None
    consensus_eps_gaap: float | None = None
    consensus_eps_adjusted: float | None = None
    consensus_revenue: float | None = None
    consensus_ebit: float | None = None
    consensus_net_income: float | None = None
    consensus_currency: str | None = None
    consensus_fetched_at: datetime | None = None
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
    # Issue #64: the streak is derived from the same rows as actual_growth, so its
    # provenance states the same comparability contract.
    beat_miss_streak: str = ""

class ActualGrowth(BaseModel):
    """Cross-period growth of a row's own actuals (Issue #63).

    Each ratio is only present when the two periods' actuals were attributed to
    the same currency and basis; otherwise it is ``None`` and ``<metric>_reason``
    carries the language-independent reason the UI renders "—" for.  A ratio and
    a reason are never both set.
    """
    eps_yoy: Decimal | None = None
    eps_yoy_reason: str | None = None
    eps_qoq: Decimal | None = None
    eps_qoq_reason: str | None = None
    revenue_yoy: Decimal | None = None
    revenue_yoy_reason: str | None = None
    revenue_qoq: Decimal | None = None
    revenue_qoq_reason: str | None = None


class BeatMissPeriod(BaseModel):
    """The fiscal period a beat/miss run stopped at (Issue #64)."""
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None


class BeatMissStreak(BaseModel):
    """Contiguous EPS beat/miss run of one quarter (Issue #64).

    ``kind`` is ``beat``/``miss`` only while every counted quarter's own
    estimate/actual pair passed :func:`app.fiscal.comparison_unavailable_reason`
    (and was adjacent, decidable and in the same direction); a run is counted
    backwards from the quarter the panel is open on.  When no run can be stated —
    the opened quarter's own pair is not comparable — ``kind`` is ``unavailable``,
    ``count`` is ``0`` and ``reason`` carries the language-independent code the UI
    renders "—" for.  ``break_period``/``break_reason`` name the quarter the count
    stopped at and why (a ``COMPARISON_*`` code, or ``missing_values`` /
    ``direction_changed`` / ``not_adjacent``).  A meaningful ``count`` and a
    ``reason`` are never both set.
    """
    kind: str = "unavailable"
    count: int = 0
    reason: str | None = None
    break_period: BeatMissPeriod | None = None
    break_reason: str | None = None


class DecisionResponse(BaseModel):
    status: str
    revision_trend: dict | None = None
    institution_rating: InstitutionRating | dict | None = None
    guidance: GuidanceStatus | dict | None = None
    provenance: Provenance | dict | None = None
    # Derived from two fiscal periods' actuals (Issue #63), declared here so the
    # generated TypeScript client types the ratio and its reason instead of
    # treating the whole object as opaque.
    actual_growth: ActualGrowth | None = None
    # The streak was the last derived metric the client received untrusted
    # (Issue #64): it is declared here for the same reason as actual_growth.
    beat_miss_streak: BeatMissStreak | None = None
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

class SyncFreshnessCheck(BaseModel):
    """Aggregate-only sync freshness (Issue #53).

    Returned on the unauthenticated ``/api/admin/health``: stage names and
    statuses only — never SQL, timestamps per stage or credentials.
    """
    status: str
    error_code: str | None = None
    threshold_hours: float | None = None
    stale_stages: list[str] = []
    never_run_stages: list[str] = []
    stale_data: list[str] = []
    checked_at: str | None = None

class FreshnessEntry(BaseModel):
    """Per-stage / per-derived-table freshness (admin diagnostics only)."""
    stage: str
    kind: str = "stage"
    last_success_at: str | None = None
    age_hours: float | None = None
    status: str
    error_code: str | None = None

class FreshnessResponse(BaseModel):
    status: str
    error_code: str | None = None
    threshold_hours: float | None = None
    checked_at: str | None = None
    stale_stages: list[str] = []
    never_run_stages: list[str] = []
    stale_data: list[str] = []
    entries: list[FreshnessEntry] = []

class HealthResponse(BaseModel):
    status: str
    version: str = "dev"
    checks: dict[str, SourceCheck | SyncFreshnessCheck | dict] = {}

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

class UniverseStatus(BaseModel):
    """State of the live default calendar/export universe (Issue #58)."""
    symbol_count: int = 0
    us_count: int = 0
    hk_count: int = 0
    source: str = ""
    stale: bool = False
    error_code: str | None = None
    last_success_at: str | None = None
    fetched_at: str | None = None
    ttl_seconds: float = 0.0

class DiagnosticsResponse(BaseModel):
    providers: dict[str, ProviderStats | dict] = {}
    cache: CacheStats = CacheStats()
    sync_runs_24h: list[SyncRunSummary | dict] = []
    # Issue #53: the pipeline is weekly, so the 24h window above is empty most
    # days. `sync_runs_window` covers the configurable window instead (kept
    # alongside the legacy field for backward compatibility).
    sync_runs_window: list[SyncRunSummary | dict] = []
    sync_runs_window_hours: int = 24
    recent_syncs: list[RecentSync | dict] = []
    freshness: FreshnessResponse | None = None
    universe: UniverseStatus | None = None

class OverviewSource(BaseModel):
    configured: str = ""
    type: str = ""
    location: str = ""
    transport: str = ""
    external_dependency: bool = False
    local_fallback: bool = False
    symbol_count: int = 0
    error_code: str | None = None
    stale: bool = False
    last_success_at: str | None = None

class OverviewResponse(BaseModel):
    source: OverviewSource
    external_symbols: list[str] = []
    managed_watchlist: list[ManagedWatchlistItem] = []
