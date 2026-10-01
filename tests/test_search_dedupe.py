"""Issue #69: `/api/search` returns one candidate per symbol, most relevant first.

`/api/search` is the watchlist page's only add-candidate source.  It used to select
`DISTINCT symbol, market, company_name` straight off `earnings`, whose `company_name`
is a *row-level* display field: the provider that wrote a row decides the spelling,
and a rename leaves the older rows behind.  So one symbol came back once per spelling
(`0700.HK TENCENT` **and** `0700.HK 腾讯控股` — 43 of the 112 visible symbols), the
front end rendered duplicate `v-for` keys, and `ORDER BY market, symbol` put an
exact code hit in the same tier as a name substring hit under one `LIMIT 20`, so
`q=META` was buried under `MetaLight` / `Ardagh Metal Packaging` / … at row 15.

Covered here:

* the statement's contract — one row per `(symbol, market)`, the authoritative name
  (`stock_names` first, then the newest *provider* row, never an algorithm row's
  copied name), and the relevance ladder;
* LIKE metacharacters in the query are escaped rather than treated as wildcards;
* the Longbridge CLI fallback cannot re-introduce a symbol already listed;
* an executable check against the local PostgreSQL: two spellings of one test symbol
  come back as a single row carrying the newest name.  It runs inside a transaction
  that is always rolled back, so no row is ever committed.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.routers.api import _SEARCH_SQL, _search_pattern, search_stocks  # noqa: E402


class _RecordingCursor:
    """Minimal cursor that records the statement and its parameters."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self.rows)


# ── The statement's contract ────────────────────────────────────────


def test_statement_keys_the_result_by_symbol_and_market():
    """One row per symbol: the dedupe must be on `(symbol, market)`, not the name."""
    assert "DISTINCT ON (symbol, market)" in _SEARCH_SQL
    assert "SELECT DISTINCT symbol, market FROM earnings" in _SEARCH_SQL
    # The old statement's key included the display name, which is what split a symbol
    # into one row per provider spelling.
    assert "DISTINCT symbol, market, company_name" not in _SEARCH_SQL


def test_statement_prefers_the_cached_name_then_the_newest_provider_row():
    assert "LEFT JOIN stock_names" in _SEARCH_SQL
    assert "COALESCE(NULLIF(sn.company_name, ''), a.company_name, '')" in _SEARCH_SQL
    # Algorithm rows only copy a provider name, and a symbol's newest rows *are* its
    # predictions — ranking them last is what keeps the copy from being circular
    # (the ordering rule `predict_earnings._COMPANY_NAME_SQL` uses, Issue #68).
    assert "(COALESCE(date_source, '') = 'algorithm')" in _SEARCH_SQL
    assert "report_date DESC, id DESC" in _SEARCH_SQL


def test_statement_still_matches_a_name_the_symbol_no_longer_uses():
    """A rename must not take the old spelling out of the search index."""
    assert "EXISTS (" in _SEARCH_SQL


def test_statement_ranks_an_exact_code_before_a_name_substring():
    """The ladder is exact code → code prefix → code substring → name only."""
    ladder = _SEARCH_SQL.split("ORDER BY", 1)[1]
    assert ladder.index("upper(symbol) = upper(%(exact)s) THEN 0") < \
        ladder.index("symbol ILIKE %(prefix)s THEN 1") < \
        ladder.index("symbol ILIKE %(like)s THEN 2") < ladder.index("ELSE 3")
    # Each tier stays stable, and the page is still 20 rows.
    assert "market, symbol" in ladder
    assert "LIMIT %(limit)s" in _SEARCH_SQL


def test_search_wires_the_parameters_the_statement_declares():
    cur = _RecordingCursor(rows=[])
    search_stocks(cur, "0700")

    sql, params = cur.executed[0]
    assert sql is _SEARCH_SQL
    assert params["like"] == "%0700%"
    assert params["prefix"] == "0700%"
    assert params["exact"] == "0700"
    assert params["limit"] == 20


def test_search_trims_the_exact_match_and_returns_dict_rows():
    # RealDictCursor (the read path's cursor) yields mapping rows; `search_stocks`
    # copies them into plain dicts so FastAPI can serialise them into `SearchItem`.
    cur = _RecordingCursor(rows=[{"symbol": "AAPL", "market": "US", "company_name": "苹果"}])
    rows = search_stocks(cur, " AAPL ")
    assert cur.executed[0][1]["exact"] == "AAPL"
    assert rows == [{"symbol": "AAPL", "market": "US", "company_name": "苹果"}]


# ── Query text is matched literally ─────────────────────────────────


def test_like_metacharacters_are_escaped():
    assert _search_pattern("a_b") == "%a\\_b%"
    assert _search_pattern("10%") == "%10\\%%"
    assert _search_pattern("a\\b") == "%a\\\\b%"


def test_the_metacharacter_escapes_reach_the_statement():
    """`q=%` must search for a percent sign, not return the first page of the table."""
    cur = _RecordingCursor(rows=[])
    search_stocks(cur, "%")
    assert cur.executed[0][1] == {"like": "%\\%%", "prefix": "\\%%", "exact": "%", "limit": 20}
    search_stocks(cur, "_")
    assert cur.executed[1][1]["like"] == "%\\_%"


# ── The CLI fallback ────────────────────────────────────────────────


class _FakeConn:
    def __enter__(self):
        return _RecordingCursor(rows=[])

    def __exit__(self, *a):
        return False


def _cli_result(items):
    import json

    class _Proc:
        returncode = 0
        stdout = json.dumps({"list": items})

    return _Proc()


def test_cli_fallback_dedupes_by_symbol():
    """The second source answers the same list, so it cannot add a duplicate."""
    from app.routers import api

    items = [
        {"counter_id": "ST/HK/700", "name": "腾讯控股"},
        {"counter_id": "ST/HK/0700", "name": "TENCENT"},
        {"counter_id": "ST/HK/80700", "name": "腾讯控股-R"},
    ]
    with mock.patch.object(api.db, "db_cursor", lambda: _FakeConn()), \
            mock.patch("subprocess.run", return_value=_cli_result(items)):
        rows = api.api_search_stocks("0700", user={"id": 1})

    assert [r["symbol"] for r in rows] == ["0700.HK", "80700.HK"]
    assert len({(r["symbol"], r["market"]) for r in rows}) == len(rows)


# ── Executed against the local database (skipped when unreachable) ──

TEST_SYMBOL = "ZZSEARCH"
TEST_OTHER = "ZZSEARCH2"


def _db_connect():
    from app import config
    import psycopg2

    return psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
        user=config.DB_USER, password=config.DB_PASSWORD, connect_timeout=3,
    )


def _db_reachable():
    try:
        _db_connect().close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _db_reachable(), reason="local fincal PostgreSQL not available")
def test_one_row_per_symbol_on_a_real_database():
    """Two spellings of one symbol are one candidate, named by the newest row.

    Everything happens inside one transaction that is rolled back in `finally`, so
    the production table is never modified by running the suite.
    """
    import psycopg2.extras
    from datetime import date

    conn = _db_connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "DELETE FROM stock_names WHERE symbol IN (%s, %s)", (TEST_SYMBOL, TEST_OTHER))
            cur.execute(
                "DELETE FROM earnings WHERE symbol IN (%s, %s)", (TEST_SYMBOL, TEST_OTHER))
            # The provider renamed the company: the oldest row keeps the old spelling.
            for report_date, fiscal_year, name, source in (
                (date(2026, 3, 4), 2025, "ZZ Old Spelling", "futu"),
                (date(2026, 6, 3), 2026, "ZZ New Spelling", "futu"),
            ):
                cur.execute(
                    """INSERT INTO earnings (symbol, market, company_name, report_date,
                           report_type, fiscal_year, fiscal_quarter, date_source, date_status)
                       VALUES (%s, 'US', %s, %s, 'Q', %s, 1, %s, 'scheduled')""",
                    (TEST_SYMBOL, name, report_date, fiscal_year, source))
            # A prediction copies the name; it must never become the displayed one.
            cur.execute(
                """INSERT INTO earnings (symbol, market, company_name, report_date,
                       report_type, fiscal_year, fiscal_quarter, date_source, date_status,
                       is_predicted)
                   VALUES (%s, 'US', 'ZZ Stale Copy', %s, 'Q', 2027, 1, 'algorithm',
                           'scheduled', TRUE)""",
                (TEST_SYMBOL, date(2027, 3, 4)))
            # The other symbol is not renamed; its only name lives in the cache.
            cur.execute(
                """INSERT INTO earnings (symbol, market, company_name, report_date,
                       report_type, fiscal_year, fiscal_quarter, date_source, date_status)
                   VALUES (%s, 'US', '', %s, 'Q', 2026, 1, 'longbridge', 'scheduled')""",
                (TEST_OTHER, date(2026, 5, 5)))
            cur.execute(
                """INSERT INTO stock_names (symbol, market, company_name, source)
                   VALUES (%s, 'US', 'ZZ Cached Name', 'kurumi')""", (TEST_OTHER,))

            rows = search_stocks(cur, TEST_SYMBOL.lower())

            assert [(r["symbol"], r["company_name"]) for r in rows] == [
                (TEST_SYMBOL, "ZZ New Spelling"),
                (TEST_OTHER, "ZZ Cached Name"),
            ], f"expected one row per symbol with its authoritative name, got {rows}"
            # The old spelling still finds the symbol, under the current name.
            assert [r["symbol"] for r in search_stocks(cur, "Old Spelling")] == [TEST_SYMBOL]
    finally:
        conn.rollback()
        conn.close()

    verifier = _db_connect()
    try:
        with verifier.cursor() as cur:
            cur.execute("SELECT count(*) FROM earnings WHERE symbol LIKE 'ZZSEARCH%'")
            assert cur.fetchone()[0] == 0, "the test transaction was not rolled back"
    finally:
        verifier.close()
