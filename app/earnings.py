"""Earnings data fetching from Futu OpenD + Longbridge fallback."""
import logging
from datetime import date, timedelta
from . import config

logger = logging.getLogger(__name__)

# The default universe (POPULAR_STOCKS_*) used to be read here, at import time,
# which froze an upstream add/remove — or a source outage during startup — for
# the whole process lifetime (Issue #58).  It now lives in ``app.universe`` as a
# live, TTL-cached accessor; ``POPULAR_STOCKS_US`` / ``POPULAR_STOCKS_HK`` are
# kept as deprecated shims (below) for any out-of-tree importer.


def __getattr__(name: str):
    """Deprecated compatibility shim for the removed module-level constants.

    Prefer :func:`app.universe.popular_stocks`, which reads the universe live
    instead of returning the value captured at import time (Issue #58).
    """
    if name in ("POPULAR_STOCKS_US", "POPULAR_STOCKS_HK"):
        from .universe import popular_stocks

        us, hk = popular_stocks()
        return us if name == "POPULAR_STOCKS_US" else hk
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def fetch_earnings_from_db(
    symbols: list[str] | None = None,
    markets: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[dict]:
    """Fetch earnings from our database. symbols are bare (no .US/.HK suffix).
    If markets is provided, filter by market. Otherwise return all."""
    from . import db

    if start is None:
        start = date.today() - timedelta(days=7)
    if end is None:
        # Same horizon as the iCal feed and the watchlist view (Issue #59): a
        # shorter fallback here truncated the very predictions the rest of the
        # app shows for callers that omit the window.
        end = date.today() + timedelta(days=config.CALENDAR_FORWARD_DAYS)

    with db.db_cursor() as cur:
        conditions = ["e.report_date BETWEEN %s AND %s"]
        params: list = [start, end]

        if symbols:
            placeholders = ",".join(["%s"] * len(symbols))
            conditions.append(f"e.symbol IN ({placeholders})")
            params.extend(symbols)

        if markets:
            placeholders = ",".join(["%s"] * len(markets))
            conditions.append(f"e.market IN ({placeholders})")
            params.extend(markets)

        where = " AND ".join(conditions)
        cur.execute(
            f"""SELECT e.*, CURRENT_DATE AS read_today,
                   sn.company_name AS stock_names_company_name,
                   sn.company_name_en AS stock_names_company_name_en,
                   c.currency AS consensus_currency, c.eps_gaap AS consensus_eps_gaap,
                   c.eps_adjusted AS consensus_eps_adjusted, c.revenue AS consensus_revenue,
                   c.ebit AS consensus_ebit, c.net_income AS consensus_net_income,
                   c.normalized_net_income AS consensus_normalized_net_income, c.fetched_at AS consensus_fetched_at
            FROM earnings e LEFT JOIN earnings_consensus c ON c.symbol=e.symbol AND c.market=e.market
              AND c.fiscal_year=e.fiscal_year AND c.fiscal_quarter=e.fiscal_quarter AND c.source='longbridge'
            LEFT JOIN stock_names sn ON sn.symbol=e.symbol AND sn.market=e.market
            WHERE {where} ORDER BY e.report_date, e.market, e.symbol""",
            params,
        )
        rows = [dict(row) for row in cur.fetchall()]

    # Issue #111: the read boundary is where "already disclosed" is decided for
    # every consumer at once.  ``date_status`` is written from the presence of an
    # actual alone, so a future-dated row can claim to be disclosed (and publish
    # the number as fact); the database's own ``CURRENT_DATE`` is selected above so
    # this rule and the sync's use the same day.
    from . import fiscal
    read_today = None
    for row in rows:
        read_today = row.pop("read_today", read_today)
    masked = fiscal.hide_unreleased_actuals(rows, read_today)
    if masked:
        logger.debug("masked %d future-dated actual(s) on the read path", masked)

    # Issue #139 (and #106 for the other language): a row's own ``company_name``
    # is the provider's spelling — Futu and Longbridge only have Latin ones —
    # while ``stock_names`` holds the resolved display name (Kurumi first, usually
    # Chinese).  Both languages are settled here, at the single read entry point,
    # so the calendar, the two exports and the iCal feed cannot disagree:
    # ``company_name`` is the name a reader sees, ``company_name_en`` the Latin
    # title of an ``en`` subscription.  In memory only — the database keeps what
    # the providers wrote — and neither join column reaches a response.
    from .company_name import display_name, english_name

    for row in rows:
        cached = row.pop("stock_names_company_name", None)
        cached_en = row.pop("stock_names_company_name_en", None)
        row_name = str(row.get("company_name") or "").strip()
        display = display_name(row_name, cached)
        if display:
            row["company_name"] = display
        latin = english_name(row_name, row.get("company_name_en"), cached_en)
        if latin:
            row["company_name_en"] = latin

    # One fiscal period is one event: collapsing here (the single read entry point
    # of the API, the CSV/JSON export and the iCal feed) keeps the three outlets
    # in agreement instead of letting each of them pick its own row (Issue #50).
    rows = fiscal.collapse_fiscal_duplicates(rows)
    # Issue #61: the same single entry point decides whether each row's estimate
    # and actual may be subtracted, so the calendar, the exports and the iCal feed
    # cannot disagree about comparability either. The reason is a code, never
    # prose — the UI owns the wording.
    for row in rows:
        # A verdict already reached at the read boundary (the unreleased-actuals
        # rule of Issue #111) wins: it explains a masked pair, which by then has no
        # values left for the attribution check to look at.
        row["comparison_unavailable_reason"] = (
            row.get("comparison_unavailable_reason")
            or fiscal.comparison_unavailable_reason(row)
        )
    # Issue #147: rows whose *quote* currency differs from the *reporting* currency
    # (every Chinese issuer listed in HK or as a US ADR) cannot subtract the
    # calendar estimate from the statement actual. Where the provider published its
    # own consensus and actual for the same period in one currency, that pair is
    # attached here — one entry point, so the calendar, both exports and the iCal
    # feed keep agreeing.
    attach_provider_pairs(rows)
    return rows


PROVIDER_PAIR_COLUMNS = ("symbol, market, fiscal_year, fiscal_quarter, source, "
                         "metric, currency, consensus, actual, num_analysts")


def load_provider_pairs(keys) -> dict[tuple, dict[str, dict]]:
    """Provider-native metric pairs for ``(symbol, market, fiscal_year, quarter)`` keys.

    Returns ``{key: {metric: row}}`` (Issue #148): one query for the whole result set,
    because the calendar spans ~100 symbols and a lookup per row would put a hundred
    round-trips in front of every page load for an enrichment most rows never use.
    """
    from . import db

    symbols = {symbol for symbol, _market, _fy, _fq in keys if symbol}
    if not symbols:
        return {}
    with db.db_cursor() as cur:
        cur.execute(
            f"SELECT {PROVIDER_PAIR_COLUMNS}, period_type FROM earnings_provider_metrics "
            "WHERE symbol = ANY(%s)", (sorted(symbols),))
        rows = [dict(row) for row in cur.fetchall()]
    pairs: dict[tuple, dict[str, dict]] = {}
    for row in rows:
        key = (row["symbol"], row["market"], row["fiscal_year"], row["fiscal_quarter"])
        # ``metric`` is part of the key since #148; a row from a caller/legacy query
        # that predates it is the EPS row (the only metric that existed then) rather
        # than a KeyError at read time.
        metric = row.get("metric") or "EPS_GAAP"
        # The newest source wins if two providers ever write the same period+metric.
        pairs.setdefault(key, {}).setdefault(metric, row)
    return pairs


def attach_provider_pairs(rows) -> list[dict]:
    """Attach the provider's own pairs where the row's own pair is currency-blocked.

    EPS and revenue are judged separately: a row can have a HKD estimate against a CNY
    actual for *both*, and each is rescued by its own metric row (or not at all).
    """
    from . import fiscal

    candidates = [row for row in rows
                  if any(fiscal.needs_metric_pair(row, metric)
                         for metric in fiscal.PROVIDER_METRIC_PREFIX)]
    if not candidates:
        return rows
    pairs = load_provider_pairs([
        (row.get("symbol"), row.get("market"), row.get("fiscal_year"),
         row.get("fiscal_quarter")) for row in candidates])
    adopted = 0
    for row in candidates:
        by_metric = pairs.get((row.get("symbol"), row.get("market"),
                               row.get("fiscal_year"), row.get("fiscal_quarter"))) or {}
        before = tuple(row.get(field) for field in fiscal.ALL_PAIR_FIELDS)
        for metric in fiscal.PROVIDER_METRIC_PREFIX:
            if fiscal.needs_metric_pair(row, metric):
                fiscal.attach_metric_pair(row, metric, by_metric.get(metric))
        after = tuple(row.get(field) for field in fiscal.ALL_PAIR_FIELDS)
        if after != before:
            adopted += 1
    if adopted:
        logger.debug("adopted a provider pair on %d row(s)", adopted)
    return rows


def fetch_earning_with_history(earning_id: int) -> tuple[dict | None, list[dict]]:
    """Read one earning **and** the symbol's period history through the read boundary.

    Issue #118: ``/api/earnings/{id}/decision`` used to run its own
    ``SELECT * FROM earnings WHERE id=%s`` and hand the raw row to
    :func:`app.phase3.build_decision_metrics`, so it was the one outlet that never
    met :func:`app.fiscal.hide_unreleased_actuals` — the panel printed
    「报告尚未发生」 in the row and 「超预期 4 季 / +185.7% 环比」 underneath it.

    Both the row the panel is open on and the history it is compared against now
    take the same rule as the calendar, the two exports and the iCal feed, from
    the same database day (``CURRENT_DATE``), so a derived metric can never read
    a number the list withholds.
    """
    from . import db, fiscal

    with db.db_cursor() as cur:
        cur.execute(
            "SELECT e.*, CURRENT_DATE AS read_today FROM earnings e WHERE e.id=%s",
            (earning_id,),
        )
        earning = cur.fetchone()
        if not earning:
            return None, []
        earning = dict(earning)
        # The decision metrics judge comparability from the row's own attribution
        # (Issues #61/#63/#64), so the history query must carry both sides of it:
        # without estimate_* the row-level rule degrades to currency_unknown and
        # every streak would read as unmatched (Issue #64 §4).
        # ``symbol``/``market`` are selected as well as filtered on: the provider-pair
        # lookup keys on them (Issue #147), and a history row without them silently
        # matched no pair — the panel then printed 「连续 —（币种不同）」 next to an
        # EPS surplus it had just computed from that very pair.
        cur.execute("SELECT id,symbol,market,fiscal_year,fiscal_quarter,report_date,eps_actual,revenue_actual,eps_estimate,estimate_currency,estimate_basis,estimate_source,actual_currency,actual_basis,actual_source FROM earnings WHERE symbol=%s AND market=%s ORDER BY report_date", (earning["symbol"], earning["market"]))
        history = [dict(row) for row in cur.fetchall()]

    # One clock for both reads: the row's own ``CURRENT_DATE``, the same day the
    # calendar path and the sync use (Issues #111/#118).
    read_today = earning.pop("read_today", None)
    masked = fiscal.hide_unreleased_actuals([earning] + history, read_today)
    if masked:
        logger.debug("masked %d future-dated actual(s) on the decision read path", masked)
    # Issue #147: the panel shows the same beat/miss the calendar does, so it goes
    # through the same pair rule — for the row it is open on *and* for the history,
    # because the beat/miss streak counts each period under the same rule. Without
    # the history, a quarter whose surplus is printed from the provider pair would
    # sit next to 「连续 —（币种不同）」.
    for row in [earning] + history:
        row["comparison_unavailable_reason"] = (
            row.get("comparison_unavailable_reason")
            or fiscal.comparison_unavailable_reason(row)
        )
    attach_provider_pairs([earning] + history)
    return earning, history


def seed_earnings_if_empty():
    """Seed DB with earnings data if empty — best effort, never fatal (Issue #74).

    The demo rows exist so a brand-new installation shows something instead of an
    empty calendar.  They are *not* the app's data (that comes from the sync
    pipeline), so a rejected seed must not take the process down: on a fresh
    database ``init_db()`` has already created ``idx_earnings_fiscal_identity``
    before this runs, and any seeded row that violates the fiscal-period
    invariant would otherwise abort start-up — under
    ``restart: unless-stopped`` that is a restart loop with no self-healing
    path (every subsequent start hits the same index and the same rows).

    Failures are therefore logged at warning level (visible in the container
    logs) and start-up continues; the transaction is rolled back by
    ``db_cursor`` so the database is left untouched.
    """
    from . import db

    try:
        with db.db_cursor() as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM earnings")
            row = cur.fetchone() or {}
            if (row.get("cnt") or 0) > 0:
                return

        logger.info("Earnings table empty, seeding demo data...")
        _seed_demo_data()
    except Exception as exc:  # noqa: BLE001 - demo data must not block start-up
        logger.warning(
            "demo seed skipped: %s: %s — the earnings table stays empty until "
            "the sync pipeline populates it (Issue #74)",
            type(exc).__name__, exc,
        )


#: Demo rows: ``(symbol, market, company_name, report_date, report_type,
#: fiscal_year, fiscal_quarter, eps_estimate, eps_actual, revenue_estimate,
#: revenue_actual, before_after)``.
#:
#: Every confirmed demo row must satisfy the fiscal-period invariant that
#: ``app.db.ensure_fiscal_identity_index()`` enforces — one confirmed row per
#: ``(symbol, market, fiscal_year, fiscal_quarter)`` — because on a fresh
#: database the index exists before this data is written (Issue #74).  The two
#: June rows below are dated for immediate visibility, so they carry a *different*
#: fiscal period than the same symbol's July/August row rather than duplicating
#: it.  ``tests/test_fresh_start_seed.py`` fails if that invariant is broken
#: again.
DEMO_EARNINGS_ROWS = [
    ("AAPL", "US", "Apple Inc.", "2026-07-30", "Q", 2026, 3, None, None, None, None, "after"),
    ("MSFT", "US", "Microsoft Corp.", "2026-07-22", "Q", 2026, 4, None, None, None, None, "after"),
    ("GOOGL", "US", "Alphabet Inc.", "2026-07-28", "Q", 2026, 2, None, None, None, None, "after"),
    ("AMZN", "US", "Amazon.com Inc.", "2026-08-04", "Q", 2026, 2, None, None, None, None, "after"),
    ("NVDA", "US", "NVIDIA Corp.", "2026-08-26", "Q", 2026, 2, None, None, None, None, "after"),
    ("META", "US", "Meta Platforms", "2026-07-30", "Q", 2026, 2, None, None, None, None, "after"),
    ("TSLA", "US", "Tesla Inc.", "2026-07-23", "Q", 2026, 2, None, None, None, None, "after"),
    ("NFLX", "US", "Netflix Inc.", "2026-07-17", "Q", 2026, 2, None, None, None, None, "after"),
    ("AMD", "US", "AMD Inc.", "2026-07-29", "Q", 2026, 2, None, None, None, None, "after"),
    ("INTC", "US", "Intel Corp.", "2026-07-24", "Q", 2026, 2, None, None, None, None, "after"),
    ("JPM", "US", "JPMorgan Chase", "2026-07-15", "Q", 2026, 2, None, None, None, None, "before"),
    ("V", "US", "Visa Inc.", "2026-07-23", "Q", 2026, 3, None, None, None, None, "after"),
    ("JNJ", "US", "Johnson & Johnson", "2026-07-16", "Q", 2026, 2, None, None, None, None, "before"),
    ("WMT", "US", "Walmart Inc.", "2026-08-14", "Q", 2026, 2, None, None, None, None, "before"),
    ("PG", "US", "Procter & Gamble", "2026-07-31", "Q", 2026, 4, None, None, None, None, "before"),
    # June dates for immediate visibility
    ("AAPL", "US", "Apple Inc.", "2026-06-10", "Q", 2026, 2, 1.45, None, 95000, None, "after"),
    ("NVDA", "US", "NVIDIA Corp.", "2026-06-11", "Q", 2026, 1, 0.85, None, 43000, None, "after"),
    # FY2026 Q3, not Q4: the July row above already owns (MSFT, US, 2026, 4).
    ("MSFT", "US", "Microsoft Corp.", "2026-06-18", "Q", 2026, 3, 2.95, None, 64000, None, "after"),
    # FY2026 Q1, not Q2: the July row above already owns (GOOGL, US, 2026, 2).
    ("GOOGL", "US", "Alphabet Inc.", "2026-06-20", "Q", 2026, 1, 1.89, None, 74000, None, "after"),
    # HK Stocks
    ("0700.HK", "HK", "Tencent", "2026-08-15", "Q", 2026, 2, None, None, None, None, None),
    ("9988.HK", "HK", "Alibaba", "2026-08-20", "Q", 2026, 1, None, None, None, None, None),
    ("0005.HK", "HK", "HSBC Holdings", "2026-08-05", "Q", 2026, 2, None, None, None, None, None),
    ("1810.HK", "HK", "Xiaomi Corp", "2026-08-25", "Q", 2026, 2, None, None, None, None, None),
    ("0700.HK", "HK", "Tencent", "2026-06-16", "Q", 2026, 1, None, None, None, None, None),
    ("9988.HK", "HK", "Alibaba", "2026-06-12", "Q", 2026, 4, None, None, None, None, None),
]


def _seed_demo_data():
    """Insert demo earnings data for testing (see ``DEMO_EARNINGS_ROWS``)."""
    from . import db

    with db.db_cursor() as cur:
        for row in DEMO_EARNINGS_ROWS:
            cur.execute(
                """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
                   fiscal_year, fiscal_quarter, eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, market, report_date, report_type)
                DO UPDATE SET company_name=EXCLUDED.company_name, eps_estimate=EXCLUDED.eps_estimate,
                   before_after=EXCLUDED.before_after, updated_at=NOW()
                """,
                row,
            )
    logger.info(f"Seeded {len(DEMO_EARNINGS_ROWS)} demo earnings records")
