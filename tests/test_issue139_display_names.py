"""Issue #139: a Chinese reader sees the Chinese company name.

The display name is a two-column design: ``earnings.company_name`` is what the
provider wrote for that row (Futu and Longbridge only carry Latin spellings) and
``stock_names.company_name`` is the resolved display name (Kurumi first, usually
Chinese).  Nothing ever converged the two, so 38 of the 116 universe symbols
showed ``TENCENT`` / ``Boeing`` / ``PDD`` in a Chinese UI while the Chinese name
sat resolved in the cache — and a search for 波音 answered ``BA Boeing``.

Pinned here:

* ``company_name.display_name`` is the one rule every read path applies: the
  cache wins exactly when it is Chinese and the row's own spelling is not;
* the single read entry point (``fetch_earnings_from_db``) resolves both
  languages, so the calendar, the exports and the iCal feed agree: ``lang=zh``
  gets 腾讯控股, ``lang=en`` keeps TENCENT;
* the helper walks to the row's own Latin spelling — replacing the display name
  must not make an English title Chinese;
* `/api/search` and `/api/universe` use the same rule instead of their own
  ``COALESCE``;

The last test runs the rule against the local PostgreSQL inside a transaction
that is always rolled back.
"""
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import earnings as earnings_module  # noqa: E402
from app.company_name import display_name, english_name, has_cjk  # noqa: E402
from app.ical import generate_ical  # noqa: E402
from app.routers import api  # noqa: E402

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


class _RecordingCursor:
    """Records the statements a helper issues; answers via ``responses``."""

    def __init__(self, responses=None, rows=None):
        self.responses = responses or {}
        self.rows = rows or []
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.executed.append((flat, params))
        self._rows = list(self.rows)
        for marker, rows in self.responses.items():
            if marker in flat:
                self._rows = rows
                break

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


def _cursor_ctx(cursor):
    @contextmanager
    def _ctx():
        yield cursor

    return _ctx


# ── The rule ───────────────────────────────────────────────────────────────


class TestDisplayName:
    """One rule, four answers: the cache only wins when it is the Chinese one."""

    def test_a_latin_row_takes_the_cached_chinese_name(self):
        assert display_name("TENCENT", "腾讯控股") == "腾讯控股"
        assert display_name("Boeing", "波音") == "波音"

    def test_a_chinese_row_keeps_its_own_spelling(self):
        """Rows that already carry a Chinese name are not second-guessed."""
        assert display_name("智富资源投资", "腾讯控股") == "智富资源投资"
        assert display_name("智富资源投资", None) == "智富资源投资"

    def test_a_latin_cached_name_never_replaces_the_row(self):
        """KTOS-style cache entries are Latin too; nothing changes for them."""
        assert display_name("KTOS", "Kratos Defense") == "KTOS"
        assert display_name("", "Kratos Defense") == "Kratos Defense"

    def test_an_unnamed_row_falls_back_to_the_cache(self):
        assert display_name("", "腾讯控股") == "腾讯控股"
        assert display_name(None, None) == ""


class TestEnglishName:
    def test_the_stored_value_wins(self):
        assert english_name("腾讯控股", "TENCENT HOLDINGS", "TENCENT") == "TENCENT HOLDINGS"

    def test_the_cache_fills_a_gap(self):
        assert english_name("腾讯控股", "", "TENCENT") == "TENCENT"

    def test_the_row_spelling_is_the_last_latin_candidate(self):
        """The name replaced by ``display_name`` is still the English title."""
        assert english_name("TENCENT", "", "") == "TENCENT"

    def test_a_chinese_name_is_never_an_english_one(self):
        assert english_name("腾讯控股", "腾讯控股", "腾讯控股") == ""
        assert english_name("", None, None) == ""
        assert has_cjk(english_name("腾讯控股", "腾讯控股", "腾讯控股")) is False


# ── The read boundary ──────────────────────────────────────────────────────


def _row(**overrides):
    row = {
        "symbol": "0700.HK", "market": "HK",
        "company_name": "TENCENT", "company_name_en": "",
        "stock_names_company_name": "腾讯控股",
        "stock_names_company_name_en": "TENCENT",
        "report_date": date(2026, 11, 12), "report_type": "Q",
        "fiscal_year": 2026, "fiscal_quarter": 3,
        "before_after": "after", "is_predicted": False,
        "date_source": "futu", "date_status": "scheduled",
        "eps_estimate": 3.0, "eps_actual": 3.2,
        "revenue_estimate": None, "revenue_actual": None,
        "estimate_currency": "CNY", "actual_currency": "CNY",
        "estimate_as_of": None, "actual_as_of": None,
        "updated_at": datetime(2026, 10, 26, 8, 19),
    }
    row.update(overrides)
    return row


class TestReadPath:
    def _fetch(self, row):
        cursor = _RecordingCursor(responses={
            "FROM earnings e LEFT JOIN earnings_consensus": [row],
        })
        from app import db as db_module
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(db_module, "db_cursor", _cursor_ctx(cursor))
            return earnings_module.fetch_earnings_from_db(
                symbols=[row["symbol"]], markets=[row["market"]],
                start=date(2026, 11, 1), end=date(2026, 12, 1),
            )

    def test_a_latin_row_shows_the_cached_chinese_name(self):
        rows = self._fetch(_row())

        assert rows[0]["company_name"] == "腾讯控股"
        # The English title keeps working: the cached Latin name, then the very
        # spelling the display name just replaced.
        assert rows[0]["company_name_en"] == "TENCENT"
        assert "stock_names_company_name" not in rows[0]
        assert "stock_names_company_name_en" not in rows[0]

    def test_the_english_name_survives_without_a_cached_latin_one(self):
        rows = self._fetch(_row(stock_names_company_name_en=None))

        assert rows[0]["company_name"] == "腾讯控股"
        assert rows[0]["company_name_en"] == "TENCENT"

    def test_a_row_that_already_has_a_chinese_name_is_unchanged(self):
        rows = self._fetch(_row(company_name="腾讯控股", company_name_en="TENCENT"))

        assert rows[0]["company_name"] == "腾讯控股"

    def test_a_latin_cache_leaves_the_row_name_alone(self):
        """Only a *Chinese* cached name takes over (KTOS is Latin in the cache)."""
        rows = self._fetch(_row(symbol="KTOS", market="US", company_name="KTOS",
                                stock_names_company_name="Kratos Defense",
                                stock_names_company_name_en="Kratos Defense"))

        assert rows[0]["company_name"] == "KTOS"

    def test_one_symbol_one_name(self):
        """Both rows of a symbol carry the same name (#68's acceptance line)."""
        cursor = _RecordingCursor(responses={
            "FROM earnings e LEFT JOIN earnings_consensus": [
                _row(report_date=date(2026, 11, 12)),
                _row(report_date=date(2026, 8, 13), fiscal_quarter=2, date_source="algorithm",
                     is_predicted=True, eps_actual=None),
            ],
        })
        from app import db as db_module
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(db_module, "db_cursor", _cursor_ctx(cursor))
            rows = earnings_module.fetch_earnings_from_db(
                symbols=["0700.HK"], markets=["HK"],
                start=date(2026, 8, 1), end=date(2026, 12, 1),
            )

        assert {row["company_name"] for row in rows} == {"腾讯控股"}


class TestIcal:
    def _fetch(self, row):
        return TestReadPath()._fetch(row)

    def test_the_chinese_feed_names_the_company_in_chinese(self):
        ics = generate_ical(self._fetch(_row()), title_lang="zh")
        summaries = [line for line in ics.replace("\r\n ", "").split("\r\n")
                     if line.startswith("SUMMARY:")]

        assert summaries and "0700.HK 腾讯控股" in summaries[0]

    def test_the_english_feed_keeps_the_latin_title(self):
        ics = generate_ical(self._fetch(_row()), title_lang="en")
        summaries = [line for line in ics.replace("\r\n ", "").split("\r\n")
                     if line.startswith("SUMMARY:")]

        assert summaries and "TENCENT" in summaries[0]
        assert CJK_RE.search(summaries[0]) is None, summaries[0]


# ── Search and the system universe ─────────────────────────────────────────


class TestSearch:
    def test_the_projection_carries_both_spellings(self):
        where = api._SEARCH_SQL.split("FROM named", 1)[1]
        assert "row_name" in where and "cached_name" in where

    def test_a_chinese_hit_displays_the_chinese_name(self):
        cur = _RecordingCursor(rows=[{"symbol": "BA", "market": "US",
                                      "row_name": "Boeing", "cached_name": "波音"}])

        rows = api.search_stocks(cur, "波音")

        assert rows == [{"symbol": "BA", "market": "US", "company_name": "波音"}]
        # The Chinese spelling is what the query matched — the display name must
        # not fall back to the provider's spelling afterwards.
        assert cur.executed[0][1]["like"] == "%波音%"

    def test_a_symbol_without_a_chinese_cache_name_is_untouched(self):
        cur = _RecordingCursor(rows=[{"symbol": "KTOS", "market": "US",
                                      "row_name": "KTOS", "cached_name": "Kratos Defense"}])

        assert api.search_stocks(cur, "KTOS") == [
            {"symbol": "KTOS", "market": "US", "company_name": "KTOS"}]

    def test_the_universe_view_uses_the_same_rule(self):
        cursor = _RecordingCursor(rows=[
            {"symbol": "BA", "market": "US", "row_name": "Boeing", "cached_name": "波音"},
            {"symbol": "KTOS", "market": "US", "row_name": "KTOS",
             "cached_name": "Kratos Defense"},
            {"symbol": "0007.HK", "market": "HK", "row_name": "智富资源投资",
             "cached_name": None},
        ])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(api.db, "db_cursor", _cursor_ctx(cursor))
            names = api._universe_names([("BA", "US"), ("KTOS", "US"), ("0007.HK", "HK")])

        assert names == {("BA", "US"): "波音", ("KTOS", "US"): "KTOS",
                         ("0007.HK", "HK"): "智富资源投资"}


# ── Executed against the local database (skipped when unreachable) ──────────


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
def test_a_cached_chinese_name_reaches_every_read_outlet(monkeypatch):
    """The issue's evidence, on the real table, inside a rolled-back transaction.

    Two rows of one symbol carry the provider's Latin spelling while the cache
    holds the Chinese name; the search list, the calendar read and both iCal
    languages are then checked to agree.  The read boundary is pointed at this
    transaction's cursor so its statements really run and nothing outside the
    transaction ever sees the rows.
    """
    import psycopg2.extras

    symbol = "ZZDISP"
    conn = _db_connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            from app import db as db_module
            monkeypatch.setattr(db_module, "db_cursor", _cursor_ctx(cur))
            cur.execute("DELETE FROM stock_names WHERE symbol = %s", (symbol,))
            cur.execute("DELETE FROM earnings WHERE symbol = %s", (symbol,))
            for report_date, fiscal_year, fiscal_quarter, source in (
                (date(2026, 11, 12), 2026, 3, "futu"),
                (date(2026, 8, 13), 2026, 2, "longbridge"),
            ):
                cur.execute(
                    """INSERT INTO earnings (symbol, market, company_name, company_name_en,
                           report_date, report_type, fiscal_year, fiscal_quarter,
                           date_source, date_status, eps_estimate, eps_actual)
                       VALUES (%s, 'US', 'ZZ Latin Spelling', '', %s, 'Q', %s, %s,
                               %s, 'scheduled', 1.0, 1.1)""",
                    (symbol, report_date, fiscal_year, fiscal_quarter, source))
            cur.execute(
                """INSERT INTO stock_names (symbol, market, company_name, company_name_en, source)
                   VALUES (%s, 'US', 'ZZ 中文名', 'ZZ Latin Spelling', 'kurumi')""",
                (symbol,))

            # 1) search: matched by the provider spelling, displayed in Chinese
            rows = api.search_stocks(cur, "ZZ Latin")
            assert [(r["symbol"], r["company_name"]) for r in rows] == [
                (symbol, "ZZ 中文名")], rows
            # 2) the calendar read: same name, and the English title is kept
            earnings = earnings_module.fetch_earnings_from_db(
                symbols=[symbol], markets=["US"],
                start=date(2026, 8, 1), end=date(2026, 12, 1))
            assert {e["company_name"] for e in earnings} == {"ZZ 中文名"}, earnings
            assert {e["company_name_en"] for e in earnings} == {"ZZ Latin Spelling"}
            # 3) the feed in both languages
            zh = generate_ical(earnings, title_lang="zh")
            en = generate_ical(earnings, title_lang="en")
            assert "ZZ 中文名" in zh
            assert "ZZ Latin Spelling" in en
            assert CJK_RE.search(en) is None
    finally:
        conn.rollback()
        conn.close()

    verifier = _db_connect()
    try:
        with verifier.cursor() as cur:
            cur.execute("SELECT count(*) FROM earnings WHERE symbol = %s", (symbol,))
            assert cur.fetchone()[0] == 0, "the test transaction was not rolled back"
            cur.execute("SELECT count(*) FROM stock_names WHERE symbol = %s", (symbol,))
            assert cur.fetchone()[0] == 0, "the test transaction was not rolled back"
    finally:
        verifier.close()
