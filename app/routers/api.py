"""API routes for watchlist management and earnings data.

Issue #7: layer cache for earnings and popular stocks.
OpenAPI: all endpoints have response_model for schema generation.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from datetime import date, timedelta
from ..auth import get_current_user, ensure_user
from .. import db, config
from ..symbol import normalize, sort_key, market_mismatch, resolve_stored_rows
from ..layer_cache import LayerCache
from ..errors import AppError, NotFoundError, ForbiddenError
from ..schemas import (
    AppConfig, UserResponse, WatchlistItem, WatchlistAddResult,
    WatchlistRemoveResult, EarningItem, DecisionResponse, PopularStocks,
    SearchItem, SystemUniverse,
)

router = APIRouter(prefix="/api", tags=["api"])

logger = logging.getLogger(__name__)

# Per-endpoint caches. The default symbol universe lives in app.universe (Issue
# #58) with its own TTL, so /api/popular no longer wraps it in a second,
# hour-long cache that could outlive an upstream change.
# `name` is the metrics label this cache reports under (Issue #83); it is a
# constant, never the per-user key used below.
_earnings_cache = LayerCache(
    default_ttl=120.0,
    stale_ttl=1800.0,
    name="earnings",
    # Bounded, LRU (Issue #86): the key contains caller-chosen dates, so without
    # a cap every distinct window would pin its payload (up to ~2 MiB) in the
    # process forever. The bound matches the iCal feed cache's slot count.
    max_entries=config.EARNINGS_CACHE_MAX_ENTRIES,
)


def invalidate_calendar_caches() -> None:
    """Drop the cached ``/api/earnings`` responses (Issues #58, #72).

    Two writers change the symbol set a cached default-calendar response was
    built from, and both must be visible on the next request instead of after
    the 120s TTL — the cached body alone would keep the old set:

    * :func:`app.universe.invalidate_symbol_universe` after an admin watchlist
      mutation (the universe changed, Issue #58);
    * the user watchlist write paths below (the default view folds the caller's
      own watchlist symbols in, Issue #72).
    """
    _earnings_cache.invalidate()


def _validate_window(start: date, end: date) -> None:
    """Reject a calendar window that is inverted or wider than the bound.

    The endpoint's ``start``/``end`` are raw ``date`` query parameters, so the
    caller controls both the cache key and the size of the cached payload
    (Issue #86). ``EARNINGS_MAX_WINDOW_DAYS`` keeps a single request inside the
    horizons the product actually shows (default view:
    ``CALENDAR_FORWARD_DAYS`` ahead; watchlist view: the same), and spans the
    one legitimate backwards reach — the calendar's previous month.

    Both failures answer 422 with the language-neutral code ``invalid_window``
    so the client can render its own wording.
    """
    if start > end:
        raise AppError(
            "invalid_window",
            "start must not be after end",
            422,
        )
    span_days = (end - start).days
    if span_days > config.EARNINGS_MAX_WINDOW_DAYS:
        raise AppError(
            "invalid_window",
            f"window spans {span_days} days, limit is {config.EARNINGS_MAX_WINDOW_DAYS}",
            422,
        )


@router.get("/config", response_model=AppConfig)
def api_config():
    """Public config (no auth required)."""
    return {
        "auth_login_url": config.AUTH_LOGIN_URL,
    }


@router.get("/me", response_model=UserResponse)
def api_me(user=Depends(get_current_user)):
    """Get current user info + ical token."""
    fincal_user = ensure_user(user["id"], user["email"], user["name"])
    return {
        "id": fincal_user["id"],
        "portal_user_id": fincal_user["portal_user_id"],
        "email": fincal_user["email"],
        "name": fincal_user["name"],
        "role": user["role"],
        "is_admin": user["role"].strip().lower() == "admin",
        "ical_token": fincal_user["ical_token"],
        "ical_url": f"{config.ICAL_BASE_URL}/ical/{fincal_user['ical_token']}",
    }


@router.get("/watchlist", response_model=list[WatchlistItem])
def api_watchlist(user=Depends(get_current_user)):
    """Get user's watchlist."""
    fincal_user = ensure_user(user["id"], user["email"], user["name"])
    with db.db_cursor() as cur:
        cur.execute(
            "SELECT symbol, market FROM watchlist WHERE user_id = %s ORDER BY market, symbol",
            (fincal_user["id"],),
        )
        return [dict(row) for row in cur.fetchall()]


@router.post("/watchlist", response_model=WatchlistAddResult)
def api_add_watchlist(symbol: str, market: str = "US", user=Depends(get_current_user)):
    """Add a stock to watchlist."""
    fincal_user = ensure_user(user["id"], user["email"], user["name"])
    market = market.strip().upper()
    if market not in ("US", "HK"):
        raise AppError("invalid_market", "market must be US or HK", 400)
    # A code carrying another exchange's suffix cannot be stored under ``US``:
    # it would never match an earnings row (the sync skips it every week), so the
    # watchlist entry would show an empty row forever (Issue #66).
    if market_mismatch(symbol, market):
        raise AppError(
            "symbol_market_mismatch",
            "symbol suffix does not match the requested market",
            400,
        )
    normalized = normalize(symbol, market)
    with db.db_cursor() as cur:
        cur.execute(
            """INSERT INTO watchlist (user_id, symbol, market) VALUES (%s, %s, %s)
            ON CONFLICT (user_id, symbol, market) DO NOTHING RETURNING *""",
            (fincal_user["id"], normalized, market),
        )
        row = cur.fetchone()
    from .ical import invalidate_ical_cache
    invalidate_ical_cache(fincal_user.get("ical_token"))
    # The default calendar view folds this user's own watchlist into its symbol
    # set (see _fetch below), so the response cached for the caller has to go as
    # well — the watchlist page reads live and would otherwise disagree with the
    # calendar about the symbol just added for up to 120s (Issue #72).
    invalidate_calendar_caches()
    return dict(row) if row else {"status": "already_exists"}


@router.delete("/watchlist", response_model=WatchlistRemoveResult)
def api_remove_watchlist(symbol: str, market: str = "US", user=Depends(get_current_user)):
    """Remove a stock from watchlist."""
    fincal_user = ensure_user(user["id"], user["email"], user["name"])
    market = market.strip().upper()
    # Same guard order as api_add_watchlist (Issues #66/#108): a request that
    # cannot name a stored row is a client error (400), not a 500 from
    # ``normalize()`` raising ``ValueError``, and not a silent 200 either.
    if market not in ("US", "HK"):
        raise AppError("invalid_market", "market must be US or HK", 400)
    if not symbol.strip():
        raise AppError("symbol_required", "symbol is required", 400)
    if market_mismatch(symbol, market):
        raise AppError(
            "symbol_market_mismatch",
            "symbol suffix does not match the requested market",
            400,
        )
    normalized = normalize(symbol, market)
    with db.db_cursor() as cur:
        cur.execute(
            "DELETE FROM watchlist WHERE user_id = %s AND symbol = %s AND market = %s",
            (fincal_user["id"], normalized, market),
        )
        # ``rowcount`` decides the answer: this used to return "removed" for a
        # symbol that was never there, which is the same false success Issue #82
        # removed from the UI, one layer down.
        removed = cur.rowcount
    if not removed:
        raise AppError("watchlist_entry_not_found", "watchlist entry not found", 404)
    from .ical import invalidate_ical_cache
    invalidate_ical_cache(fincal_user.get("ical_token"))
    # Same reason as in api_add_watchlist: a removed symbol must not linger on
    # the calendar (Issue #72).
    invalidate_calendar_caches()
    return {"status": "removed"}


@router.get("/earnings", response_model=list[EarningItem])
def api_earnings(
    start: date | None = None,
    end: date | None = None,
    watchlistOnly: bool = False,
    user=Depends(get_current_user),
):
    """Get earnings calendar data with layer cache (Issue #7)."""
    from ..earnings import fetch_earnings_from_db
    from ..universe import popular_stocks

    fincal_user = ensure_user(user["id"], user["email"], user["name"])

    if start is None:
        start = date.today() - timedelta(days=7)
    if end is None:
        # Keep the default window on the shared calendar horizon instead of a
        # local literal: a caller that omits `end` used to get 90 days while the
        # watchlist view and the iCal feed reached 420 (Issue #59).
        end = date.today() + timedelta(days=config.CALENDAR_FORWARD_DAYS)

    # The window is both cache-key material and the size of the payload, so it
    # is validated before either is used (Issue #86): an unbounded
    # ``start``/``end`` pair let one authenticated caller create an unlimited
    # number of ~2 MiB cache entries (a ten-year window was accepted and cached
    # as-is). Rejections carry the language-neutral code ``invalid_window``;
    # bulk history remains available through ``/api/export``.
    _validate_window(start, end)

    cache_key = f"earnings:{start}:{end}:{watchlistOnly}:{fincal_user['id']}"
    # The key is per user, so any writer that changes this user's symbol set must
    # drop it (invalidate_calendar_caches) or the calendar keeps the old body
    # (Issue #72).

    if watchlistOnly:
        with db.db_cursor() as cur:
            cur.execute(
                "SELECT symbol, market FROM watchlist WHERE user_id = %s",
                (fincal_user["id"],),
            )
            wl = cur.fetchall()
        # A stored row that cannot be served is skipped (and logged once), never
        # raised: one legacy spelling used to 500 the whole view (Issue #108).
        resolved = resolve_stored_rows(wl, context="GET /api/earnings?watchlistOnly=true")
        if not resolved:
            return []
        symbols = [sym for sym, _ in resolved]
        markets = list(dict.fromkeys(mkt for _, mkt in resolved))
        return fetch_earnings_from_db(symbols=symbols, markets=markets, start=start, end=end)
    else:
        def _fetch():
            universe_us, universe_hk = popular_stocks()
            all_symbols = list(set(universe_us + universe_hk))
            all_markets = ["US", "HK"]
            with db.db_cursor() as cur:
                cur.execute(
                    "SELECT symbol, market FROM watchlist WHERE user_id = %s",
                    (fincal_user["id"],),
                )
                for norm, mkt in resolve_stored_rows(
                    cur.fetchall(), context="GET /api/earnings"
                ):
                    if norm not in all_symbols:
                        all_symbols.append(norm)
                    if mkt not in all_markets:
                        all_markets.append(mkt)
            return fetch_earnings_from_db(symbols=all_symbols, markets=all_markets, start=start, end=end)

        data, entry = _earnings_cache.get_or_refresh(cache_key, _fetch, ttl=120.0)
        return data


@router.get("/earnings/{earning_id}/decision", response_model=DecisionResponse)
def api_earning_decision(earning_id: int, user=Depends(get_current_user)):
    """Decision-support facts with source-specific unavailable states, never synthetic values."""
    from ..phase3 import build_decision_metrics, revision_trend

    ensure_user(user["id"], user["email"], user["name"])
    with db.db_cursor() as cur:
        cur.execute("SELECT * FROM earnings WHERE id=%s", (earning_id,))
        earning = cur.fetchone()
        if not earning:
            raise NotFoundError("earning")
        earning = dict(earning)
        # The decision metrics judge comparability from the row's own attribution
        # (Issues #61/#63/#64), so the history query must carry both sides of it:
        # without estimate_* the row-level rule degrades to currency_unknown and
        # every streak would read as unmatched (Issue #64 §4).
        cur.execute("SELECT id,fiscal_year,fiscal_quarter,report_date,eps_actual,revenue_actual,eps_estimate,estimate_currency,estimate_basis,estimate_source,actual_currency,actual_basis,actual_source FROM earnings WHERE symbol=%s AND market=%s ORDER BY report_date", (earning["symbol"], earning["market"]))
        history = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT captured_at,eps_estimate,revenue_estimate,source FROM earnings_estimate_snapshots WHERE earning_id=%s ORDER BY captured_at", (earning_id,))
        snapshots = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT currency_symbol,target_price,strong_buy,buy,hold,underperform,sell,recommendation,provider_updated_at,fetched_at,source FROM earnings_institution_ratings WHERE symbol=%s AND market=%s AND source='longbridge'", (earning["symbol"], earning["market"]))
        rating = cur.fetchone()
        cur.execute("SELECT status,reason,source,checked_at FROM earnings_guidance_status WHERE symbol=%s AND market=%s AND source='longbridge'", (earning["symbol"], earning["market"]))
        guidance = cur.fetchone()
    return {
        "status": "available", "revision_trend": revision_trend(snapshots),
        "institution_rating": dict(rating) if rating else {"status": "unavailable", "source": "longbridge"},
        "guidance": dict(guidance) if guidance else {"status": "unavailable", "reason": "longbridge_guidance_endpoint_unavailable", "source": "longbridge"},
        **build_decision_metrics(history, earning_id),
        "provenance": {"revision_trend": "earnings_estimate_snapshots / Longbridge finance-calendar", "institution_rating": "Longbridge institution-rating", "actual_growth": "earnings actuals (Longbridge/Futu as recorded); a period pair is only compared when both actuals share a declared currency and basis", "price_reaction": "unavailable: no reliable provider configured", "beat_miss_streak": "earnings actuals + the estimate recorded at the time; a quarter is only counted when its own estimate/actual pair is comparably attributed"},
    }


@router.get("/popular", response_model=PopularStocks)
def api_popular(user=Depends(get_current_user)):
    """Get the list of popular stocks shown by default (Issue #7).

    Served straight from the live universe accessor (Issue #58): its TTL is the
    one that governs how fresh this answer is, so an upstream add/remove cannot
    hide behind a second, longer-lived cache.
    """
    from ..universe import popular_stocks_by_market

    return popular_stocks_by_market()


# ── System universe: read-only view of the default watchlist ────────────────
#
# Every user's calendar, export and iCal feed is built from one symbol range:
# the configured watchlist source (``WATCHLIST_SOURCE``).  Until now only an
# admin could see what that range *is* — ``/api/admin/overview`` — so a normal
# user could not tell whether a missing symbol was never tracked or simply had
# no upcoming report, and could not explain 「为什么日历里只有这些公司」.
#
# This endpoint answers that for any signed-in user, and it is read-only by
# construction: there is no mutation counterpart under ``/api`` (the editable
# list stays admin-only at ``/api/admin/watchlist``).  It reads the same
# ``app.universe`` accessor the calendar uses, so it cannot drift from what is
# actually served, and it reports each symbol's origin so a user can tell
# FinCal's own additions apart from the shared upstream list.
#
# One display name per ``(symbol, market)``, shared by the search list and the
# read-only system-universe view: the newest *provider* row's name (algorithm
# rows only copy a provider name), with the ``stock_names`` cache as fallback.
_AUTHORITATIVE_NAME_CTE = """authoritative_name AS (
    SELECT DISTINCT ON (symbol, market) symbol, market, company_name
    FROM earnings
    WHERE company_name IS NOT NULL AND company_name <> ''
    ORDER BY symbol, market, (COALESCE(date_source, '') = 'algorithm'),
             report_date DESC, id DESC
)"""

_UNIVERSE_NAME_SQL = f"""
WITH {_AUTHORITATIVE_NAME_CTE}
SELECT p.symbol, p.market,
       COALESCE(a.company_name, NULLIF(sn.company_name, ''), '') AS company_name
FROM unnest(%(symbols)s::text[], %(markets)s::text[]) AS p(symbol, market)
LEFT JOIN authoritative_name a ON a.symbol = p.symbol AND a.market = p.market
LEFT JOIN stock_names sn ON sn.symbol = p.symbol AND sn.market = p.market
"""


def _managed_codes() -> set[str] | None:
    """``{'US:AAPL', 'HK:0700.HK'}`` of the FinCal-managed symbols.

    ``None`` when the managed table cannot be read: the universe itself is still
    answerable, so the origin column degrades to "unknown" instead of failing the
    whole view.
    """
    from ..symbol import bare_ticker

    try:
        with db.db_cursor() as cur:
            cur.execute("SELECT symbol, market FROM managed_watchlist")
            rows = cur.fetchall()
    except Exception as exc:
        logger.warning("system universe: managed watchlist unreadable: %s", exc)
        return None
    codes: set[str] = set()
    for row in rows:
        market = (row["market"] or "").strip().upper()
        raw = (row["symbol"] or "").strip().upper()
        if not raw:
            continue
        codes.add(f"{market}:{normalize(bare_ticker(raw), market) if market == 'HK' else bare_ticker(raw)}")
    return codes


def _universe_names(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Display names for the universe, one per ``(symbol, market)``.

    Same precedence the search list uses (``_AUTHORITATIVE_NAME_CTE``): the
    newest provider row's name, then the ``stock_names`` cache.  A failure here
    only costs the names — the range itself is what the page is for.
    """
    if not pairs:
        return {}
    try:
        with db.db_cursor() as cur:
            cur.execute(_UNIVERSE_NAME_SQL, {
                "symbols": [pair[0] for pair in pairs],
                "markets": [pair[1] for pair in pairs],
            })
            rows = cur.fetchall()
    except Exception as exc:
        logger.warning("system universe: name lookup failed: %s", exc)
        return {}
    return {(row["symbol"], row["market"]): row["company_name"] or "" for row in rows}


@router.get("/universe", response_model=SystemUniverse)
def api_universe(user=Depends(get_current_user)):
    """The default watchlist range the whole system tracks (read-only)."""
    from ..universe import popular_stocks, universe_status
    from ..watchlist import source_description

    us, hk = popular_stocks()
    status = universe_status()
    # Issue #93: this endpoint serves every signed-in user, so it describes the
    # source in coarse terms — the admin overview keeps the exact host/port/name.
    description = source_description(include_location=False)

    pairs = sorted([(symbol, "US") for symbol in us] + [(symbol, "HK") for symbol in hk],
                   key=lambda pair: (pair[1], sort_key(pair[0])))
    managed = _managed_codes()
    names = _universe_names(pairs)

    items = []
    for symbol, market in pairs:
        origin = None
        if managed is not None:
            origin = "managed" if f"{market}:{symbol}" in managed else "shared"
        items.append({
            "symbol": symbol,
            "market": market,
            "company_name": names.get((symbol, market), ""),
            "origin": origin,
        })

    return {
        **description,
        "source": status.get("source") or "",
        "stale": bool(status.get("stale")),
        "error_code": status.get("error_code"),
        "updated_at": status.get("last_success_at"),
        "fetched_at": status.get("fetched_at"),
        "counts": {"US": len(us), "HK": len(hk), "total": len(us) + len(hk)},
        "items": items,
        "skipped": list(status.get("skipped_symbols") or []),
    }


# ── Watchlist search (Issue #69) ────────────────────────────────────────────
#
# `/api/search` is the watchlist page's only add-candidate source, so one code has
# to come back as one candidate.  The previous statement selected
# `DISTINCT symbol, market, company_name` straight off `earnings`, and that name is
# a *row-level* display field: Longbridge and Futu each write their own spelling and
# a rename leaves the older rows behind, so `DISTINCT` emitted the same symbol once
# per spelling (`0700.HK TENCENT` and `0700.HK 腾讯控股`, 43/112 of the visible
# universe).  Worse, `ORDER BY market, symbol` ranked a name-substring hit exactly
# like an exact code hit under the same `LIMIT 20`, so `q=META` was drowned by
# `MetaLight` / `Ardagh Metal Packaging` / … and the symbol itself fell out of view.
#
# The statement now:
#   * keeps one row per `(symbol, market)`;
#   * displays the name the rest of the product already shows for that symbol — the
#     newest *provider* row's name.  Algorithm-owned rows are ranked last because they
#     merely copy a provider name, and a symbol's newest rows are its predictions (the
#     ordering rule `predict_earnings._COMPANY_NAME_SQL` uses, Issue #68).  The
#     `stock_names` cache (Kurumi > Longbridge > Futu, `app/company_name.py`) is the
#     fallback for a symbol whose own rows never carried a name: naming this list from
#     the cache first instead would print a second spelling beside the one the calendar
#     and the watchlist use — 23 of today's 65 multi-spelling symbols differ that way,
#     and 21 of the 112 visible symbols would answer a code search with a name their
#     calendar events never show;
#   * still *finds* a symbol by any spelling that is attached to it anywhere — its own
#     rows (including a rename it no longer uses) or its cached name — so narrowing the
#     display name does not narrow the search;
#   * orders the matches by relevance — exact code, code prefix, code substring,
#     then name-only — with `market, symbol` keeping each tier stable.
_SEARCH_SQL = f"""
WITH {_AUTHORITATIVE_NAME_CTE},
symbols AS (
    SELECT DISTINCT symbol, market FROM earnings
),
named AS (
    SELECT s.symbol, s.market,
           a.company_name AS row_name,
           NULLIF(sn.company_name, '') AS cached_name
    FROM symbols s
    LEFT JOIN authoritative_name a ON a.symbol = s.symbol AND a.market = s.market
    LEFT JOIN stock_names sn ON sn.symbol = s.symbol AND sn.market = s.market
)
SELECT symbol, market, COALESCE(row_name, cached_name, '') AS company_name
FROM named
WHERE symbol ILIKE %(like)s
   OR COALESCE(row_name, cached_name) ILIKE %(like)s
   OR cached_name ILIKE %(like)s
   OR EXISTS (
        SELECT 1 FROM earnings e
        WHERE e.symbol = named.symbol AND e.market = named.market
          AND e.company_name ILIKE %(like)s
      )
ORDER BY
    CASE
        WHEN upper(symbol) = upper(%(exact)s) THEN 0
        WHEN symbol ILIKE %(prefix)s THEN 1
        WHEN symbol ILIKE %(like)s THEN 2
        ELSE 3
    END,
    market, symbol
LIMIT %(limit)s
"""


def _search_pattern(q: str) -> str:
    """The user's text as a literal LIKE pattern (Issue #69).

    `%` and `_` are LIKE metacharacters; unescaped, typing `_` in the search box
    would match any character and a bare `%` would return the first page of the
    whole table.  Backslash is PostgreSQL's default LIKE escape character.
    """
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_stocks(cur, q: str, limit: int = 20) -> list[dict]:
    """One row per symbol matching ``q``, most relevant first (Issue #69)."""
    like = _search_pattern(q)
    cur.execute(_SEARCH_SQL, {
        "like": like,
        "prefix": like[1:] if len(like) > 1 else like,   # strip the leading wildcard
        "exact": q.strip(),
        "limit": limit,
    })
    return [dict(row) for row in cur.fetchall()]


@router.get("/search", response_model=list[SearchItem])
def api_search_stocks(q: str, user=Depends(get_current_user)):
    """Search the earnings universe for stocks to add to the watchlist.

    One source only: the `earnings` table (Issue #71).  The endpoint used to
    fall back to `longbridge stock-search` when the table had no match, but no
    such subcommand exists in the Longbridge CLI — the installed one exposes
    `finance-calendar`, `static`, `consensus`, `forecast-eps` and
    `institution-rating`, none of which search by keyword.  The branch could
    therefore never succeed (inside the service image the binary is not even
    present) and its failure was swallowed by a bare ``except: pass``, so "the
    fallback failed" and "the universe really has no match" were
    indistinguishable from the outside.  Removing it keeps the endpoint honest
    about where its answers come from; `tests/test_search_dedupe.py` now checks
    every ``longbridge <subcommand>`` the code calls against the real CLI, so a
    command name can no longer drift out of existence unnoticed.

    A miss is logged instead of silently returning ``[]``, which is what made
    the old dead branch invisible: `logger.info` carries the query (and its
    length), never the caller.
    """
    with db.db_cursor() as cur:
        results = search_stocks(cur, q)

    if not results:
        logger.info("search: no universe match for q=%r (len=%d)", q, len(q or ""))

    return results


@router.get("/export")
def api_export(start: date, end: date, format: str = "csv", user=Depends(get_current_user)):
    """Export earnings data as CSV or JSON."""
    from ..earnings import fetch_earnings_from_db
    from ..universe import popular_stocks
    from fastapi.responses import StreamingResponse
    import csv, io, json as json_mod

    ensure_user(user["id"], user["email"], user["name"])
    universe_us, universe_hk = popular_stocks()
    symbols = universe_us + universe_hk
    markets = ["US", "HK"]
    data = fetch_earnings_from_db(symbols=symbols, markets=markets, start=start, end=end)

    if format == "json":
        return data

    output = io.StringIO()
    writer = csv.writer(output)
    # Derive the column set from the actual rows so the CSV field set stays in
    # lockstep with the JSON export (which returns `data` verbatim). This keeps
    # provenance/status (date_source, date_status, estimate_source, actual_source)
    # and consensus_* fields from drifting out of sync with the EarningItem
    # contract (Issue #47). Columns follow first-appearance order across rows;
    # empty data falls back to a canonical list so the header is still emitted.
    fields: list[str] = []
    seen: set[str] = set()
    for r in data:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                fields.append(k)
    if not fields:
        fields = [
            "symbol", "market", "company_name", "report_date", "report_type",
            "fiscal_year", "fiscal_quarter", "before_after", "eps_estimate",
            "eps_actual", "revenue_estimate", "revenue_actual", "is_predicted",
            "date_source", "date_status", "estimate_source", "estimate_as_of",
            "estimate_currency", "estimate_basis", "actual_source", "actual_as_of",
            "actual_currency", "actual_basis", "comparison_unavailable_reason",
            "updated_at", "consensus_currency", "consensus_eps_gaap",
            "consensus_eps_adjusted", "consensus_revenue", "consensus_ebit",
            "consensus_net_income", "consensus_normalized_net_income",
            "consensus_fetched_at",
        ]
    writer.writerow(fields)
    for r in data:
        writer.writerow([r.get(f) for f in fields])
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=fincal-earnings-{start}-{end}.csv"},
    )
