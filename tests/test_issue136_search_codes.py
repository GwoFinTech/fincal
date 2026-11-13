"""Issue #136: `/api/search` accepts the code spellings the write path accepts.

`/api/search` is the only way to add a symbol the calendar does not already show.
It matched the query text with a bare ``ILIKE``, while ``POST /api/watchlist``
goes through ``symbol.normalize_user_symbol`` and happily stores ``00700``,
``09988``, ``AAPL.US`` and ``US.AAPL``.  So the spellings Futu and Longbridge
*display* — five-digit HK codes, ``.US`` suffixes, Futu's ``US.`` prefix —
searched to zero rows, and the empty state told the user their keyword was wrong
instead of that the format had never been read.

Covered here:

* the contract of ``symbol.canonical_code``: one definition of "canonical",
  the same chain the write path uses, and ``None`` for text that is not a code;
* the search statement matches that canonical spelling and ranks it above every
  substring tier, while a name query keeps the ladder it had;
* an executable check against the local PostgreSQL: the four zero-result
  spellings from the issue now find their symbol, and ``q=700`` puts ``0700.HK``
  first.  It runs inside a transaction that is always rolled back.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.routers.api import _SEARCH_SQL, search_stocks  # noqa: E402
from app.symbol import canonical_code, normalize_user_symbol  # noqa: E402


class _RecordingCursor:
    """Minimal cursor that records the statement and its parameters."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self.rows)


# ── The canonical spelling of a typed code ──────────────────────────


@pytest.mark.parametrize("typed,canon", [
    ("00700", "0700.HK"),          # Futu's five-digit display format
    ("09988", "9988.HK"),
    ("700", "0700.HK"),
    ("0700", "0700.HK"),
    ("0700.HK", "0700.HK"),
    ("82333", "82333.HK"),         # a real five-digit HKEX code is not padded away
    ("AAPL.US", "AAPL"),           # Longbridge's suffix spelling
    ("US.AAPL", "AAPL"),           # Futu's prefix spelling
    ("aapl", "AAPL"),
    ("brk.a", "BRK.A"),            # a class share keeps its suffix
])
def test_a_typed_code_becomes_the_symbol_the_write_path_stores(typed, canon):
    """Search and write must read one code the same way."""
    assert canonical_code(typed) == canon
    # The write path's own contract, for the pairs where it accepts the code.
    market = canon.rsplit(".", 1)[1] if canon.endswith(".HK") else "US"
    assert normalize_user_symbol(typed, market)[0] == canon


def test_the_canonical_form_agrees_with_the_write_path_for_both_markets():
    """Every spelling the user API stores has to be findable afterwards."""
    for typed, market in (("00700", "HK"), ("09988", "HK"), ("700", "HK"),
                          ("AAPL.US", "US"), ("US.AAPL", "US"), ("NVDA", "US")):
        stored, _ = normalize_user_symbol(typed, market)
        assert canonical_code(typed) == stored, (typed, stored)


def test_a_futu_prefix_spelling_is_understood_even_though_the_write_path_refuses_it():
    """`HK.00700` is code-shaped: searching it must not be a dead end.

    ``normalize_user_symbol`` refuses the ``HK.`` prefix for a HK code (no
    user-facing write sends it), but a code pasted from a Futu screen still has
    to find its symbol, so the search reads it through ``market_of`` /
    ``bare_ticker`` rather than inheriting the write path's narrower guard.
    """
    assert canonical_code("HK.00700") == "0700.HK"


@pytest.mark.parametrize("typed", [
    "", " ", "腾讯", "TENCENT HOLDINGS", "%", "_", "a_b",
    "600028.SH",     # an exchange FinCal does not serve
    "159326",        # a Shenzhen fund code whose suffix was pasted off
    "0700.US",       # servable-looking, but the market cannot be invented
    "THISISNOTATICKER",  # longer than any ticker
])
def test_text_that_is_not_a_code_is_not_canonicalised(typed):
    """Refusing here is what keeps a name search from growing a bogus predicate."""
    assert canonical_code(typed) is None


# ── The statement ──────────────────────────────────────────────────


def test_the_statement_matches_the_canonical_spelling():
    where = _SEARCH_SQL.split("FROM named", 1)[1].split("ORDER BY", 1)[0]
    assert "symbol = %(canon)s" in where


def test_the_canonical_hit_outranks_every_substring_tier():
    """`q=700` must not rank `7001.HK` above the exact `0700.HK` (Issue #136)."""
    ladder = _SEARCH_SQL.split("ORDER BY", 1)[1]
    assert ladder.index("symbol = %(canon)s THEN 0") < \
        ladder.index("upper(symbol) = upper(%(exact)s) THEN 1") < \
        ladder.index("symbol ILIKE %(prefix)s THEN 2") < \
        ladder.index("symbol ILIKE %(like)s THEN 3") < ladder.index("ELSE 4")


def test_a_name_query_still_travels_with_a_null_canon():
    """`symbol = NULL` is never true, so the name ladder is unchanged."""
    cur = _RecordingCursor(rows=[])
    search_stocks(cur, "腾讯")
    assert cur.executed[0][1]["canon"] is None
    assert cur.executed[0][1]["like"] == "%腾讯%"


def test_the_search_wires_the_canonical_spelling():
    cur = _RecordingCursor(rows=[])
    search_stocks(cur, "00700")
    params = cur.executed[0][1]
    assert params["canon"] == "0700.HK"
    # The literal query still reaches the name tiers untouched.
    assert params["like"] == "%00700%"


# ── Executed against the local database (skipped when unreachable) ──


def _db_connect():
    import psycopg2

    from app import config

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


@pytest.mark.integration
@pytest.mark.skipif(not _db_reachable(), reason="local fincal PostgreSQL not available")
def test_the_spellings_from_the_issue_find_their_symbol():
    """The four zero-result queries now hit, inside a rolled-back transaction."""
    import psycopg2.extras

    conn = _db_connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # A universe of the codes the issue's table used, plus a prefix
            # neighbour (7001.HK) that used to outrank the exact hit.  The rows
            # land in a fiscal year no real row uses, so a symbol that is already
            # in production (0700.HK, 9988.HK) does not collide with it.
            for symbol, market, name in (
                ("0700.HK", "HK", "ZZ Search TENCENT"),
                ("7001.HK", "HK", "ZZ Prefix Neighbour"),
                ("9988.HK", "HK", "ZZ Search ALIBABA"),
                ("82333.HK", "HK", "ZZ RMB Counter"),
                ("AAPL", "US", "ZZ Search Apple"),
            ):
                cur.execute(
                    """INSERT INTO earnings (symbol, market, company_name, report_date,
                           report_type, fiscal_year, fiscal_quarter, date_source, date_status)
                       VALUES (%s, %s, %s, date '1900-01-01', 'Q', 1900, 1, 'futu',
                               'scheduled')
                       ON CONFLICT DO NOTHING""",
                    (symbol, market, name))

            def hits(q):
                return [r["symbol"] for r in search_stocks(cur, q)]

            # The four spellings that returned [] before the fix.
            assert hits("00700")[:1] == ["0700.HK"], hits("00700")
            assert hits("09988")[:1] == ["9988.HK"], hits("09988")
            assert hits("AAPL.US")[:1] == ["AAPL"], hits("AAPL.US")
            assert hits("US.AAPL")[:1] == ["AAPL"], hits("US.AAPL")
            # The literal spellings keep working, and the exact hit now leads.
            assert hits("0700")[:1] == ["0700.HK"], hits("0700")
            assert hits("0700.HK")[:1] == ["0700.HK"], hits("0700.HK")
            assert hits("700")[:1] == ["0700.HK"], hits("700")
            assert "7001.HK" in hits("700"), hits("700")
            # A real five-digit HKEX code is not collapsed into its neighbour.
            assert hits("82333")[:1] == ["82333.HK"], hits("82333")
            # Name search is untouched: an old row's spelling still finds the
            # symbol (the displayed name stays the newest provider row's).
            assert hits("ZZ Prefix Neighbour") == ["7001.HK"]
            assert hits("TENCENT").count("0700.HK") == 1, hits("TENCENT")
    finally:
        conn.rollback()
        conn.close()
