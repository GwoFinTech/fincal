"""Issue #55: an ``lang=en`` subscription must actually be English.

The feed's language switch looked implemented — ``/ical/{token}?lang=en`` is
validated, the SUMMARY suffix says ``Earnings`` — but the en branch read a
``company_name_en`` field that no table, schema or read query ever had, and the
status markers were hard-coded Chinese.  Production therefore served
``ORCL 甲骨文 Q1 Earnings`` plus ``[预测]``: a switch that silently did nothing for
47% of ``earnings`` rows (96.5% of HK) and mixed Chinese into every English
subscriber's calendar.

Covered here:

* the column exists on both tables and ``init_db()`` adds it to an existing
  installation idempotently (executed against a throwaway schema, rolled back);
* resolution keeps the display name Kurumi-based and resolves the Latin name
  separately — a Chinese name is never accepted as the English one;
* the iCal generator uses the English name and English markers for ``en``, falls
  back to the display name without empty titles or double spaces, and leaves the
  ``zh`` output byte-identical to the pre-change wording;
* the cache writer stores each name in its own column and never lets an empty
  value clear the other.
"""
import importlib.util
import re
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import company_name as cn  # noqa: E402
from app import db  # noqa: E402
from app.ical import generate_ical  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "sync_stock_names", ROOT / "scripts" / "sync_stock_names.py")
assert _SPEC is not None and _SPEC.loader is not None
sync_stock_names = importlib.util.module_from_spec(_SPEC)
sys.modules["sync_stock_names"] = sync_stock_names
_SPEC.loader.exec_module(sync_stock_names)

CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


# ── helpers ────────────────────────────────────────────────────────────────

def _unfold(ics: str) -> str:
    """Undo RFC 5545 line folding so assertions can look at whole lines."""
    return ics.replace("\r\n ", "")


def _lines(ics: str, prefix: str) -> list[str]:
    return [line for line in _unfold(ics).split("\r\n") if line.startswith(prefix)]


def _event(**overrides) -> dict:
    row = {
        "symbol": "0700.HK", "market": "HK",
        "company_name": "腾讯控股", "company_name_en": "TENCENT",
        "report_date": date(2026, 11, 12),
        "fiscal_year": 2026, "fiscal_quarter": 3,
        "date_status": "predicted", "is_predicted": True,
    }
    row.update(overrides)
    return row


# ── the iCal outlet ────────────────────────────────────────────────────────

def test_english_feed_uses_the_english_name_and_english_markers():
    ics = generate_ical([_event()], title_lang="en")
    body = _unfold(ics)

    summary = _lines(ics, "SUMMARY:")[0]
    assert summary == "SUMMARY:0700.HK TENCENT Q3 Earnings [Predicted]"
    assert not CJK_RE.search(summary), summary
    # Acceptance 3 also covers DESCRIPTION: the company line must not stay Chinese.
    assert "Company: TENCENT" in body
    description = _lines(ics, "DESCRIPTION:")[0]
    assert not CJK_RE.search(description), description


def test_english_feed_localizes_the_unconfirmed_marker():
    ics = generate_ical([_event(date_status="unavailable", is_predicted=False)],
                        title_lang="en")
    assert _lines(ics, "SUMMARY:")[0].endswith(" [Unconfirmed]")
    assert "[未确认]" not in _unfold(ics)


def test_english_feed_without_an_english_name_falls_back_cleanly():
    """Documented rule: fall back to the display name, never to an empty title."""
    ics = generate_ical([_event(company_name_en="")], title_lang="en")
    summary = _lines(ics, "SUMMARY:")[0]

    assert summary == "SUMMARY:0700.HK 腾讯控股 Q3 Earnings [Predicted]"
    assert "  " not in summary, "a missing English name must not leave a double space"
    assert "None" not in summary


def test_chinese_feed_is_byte_identical_and_ignores_the_english_name():
    ics = generate_ical([_event()], title_lang="zh")
    body = _unfold(ics)

    assert _lines(ics, "SUMMARY:")[0] == "SUMMARY:0700.HK 腾讯控股 Q3 财报 [预测]"
    # Issue #97: the body follows the summary's language, so the label is 公司.
    assert "公司：腾讯控股" in body
    assert "TENCENT" not in body


def test_missing_company_names_do_not_break_the_title():
    ics = generate_ical([_event(company_name="", company_name_en="")], title_lang="en")
    summary = _lines(ics, "SUMMARY:")[0]
    assert summary == "SUMMARY:0700.HK Q3 Earnings [Predicted]"
    assert "  " not in summary


# ── name resolution: two languages, two providers, two columns ─────────────

def test_english_name_comes_from_longbridge_while_the_display_name_stays_kurumi(monkeypatch):
    monkeypatch.setattr(cn, "fetch_from_kurumi", lambda symbol, market: "腾讯控股")
    monkeypatch.setattr(cn, "fetch_from_longbridge", lambda symbol, market: "TENCENT")

    result = cn.resolve_company_name_result("0700.HK", "HK")

    assert (result.name, result.source) == ("腾讯控股", "kurumi")
    assert result.name_en == "TENCENT"


def test_a_latin_display_name_is_its_own_english_name_without_another_call(monkeypatch):
    calls = []

    def longbridge(symbol, market):
        calls.append(symbol)
        return "Adobe Inc"

    monkeypatch.setattr(cn, "fetch_from_kurumi", lambda symbol, market: "Adobe")
    monkeypatch.setattr(cn, "fetch_from_longbridge", longbridge)

    result = cn.resolve_company_name_result("ADBE", "US")

    assert result.name_en == "Adobe"
    assert calls == [], "no upstream call is needed when the display name is already Latin"


def test_a_chinese_name_is_never_used_as_the_english_name(monkeypatch):
    """The bug was a Chinese name rendered under `lang=en`; returning "" makes
    the fallback explicit instead of pretending the name is English."""
    monkeypatch.setattr(cn, "fetch_from_kurumi", lambda symbol, market: "某某科技")
    monkeypatch.setattr(cn, "fetch_from_longbridge", lambda symbol, market: "某某科技有限公司")

    def futu(symbol, market):
        raise ConnectionError("opend down")

    monkeypatch.setattr(cn, "fetch_from_futu", futu)

    result = cn.resolve_company_name_result("0999.HK", "HK")

    assert result.name == "某某科技"
    assert result.name_en == ""


def test_resolution_still_falls_back_when_kurumi_fails(monkeypatch):
    def kurumi(symbol, market):
        raise ConnectionError("kurumi down")

    monkeypatch.setattr(cn, "fetch_from_kurumi", kurumi)
    monkeypatch.setattr(cn, "fetch_from_longbridge", lambda symbol, market: "APPLE INC")

    result = cn.resolve_company_name_result("AAPL", "US")

    assert (result.name, result.source) == ("APPLE INC", "longbridge")
    assert result.name_en == "APPLE INC"


# ── cache writer ───────────────────────────────────────────────────────────

class _RecordingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def close(self):
        pass


def _with_cursor():
    cursor = _RecordingCursor()

    @contextmanager
    def _ctx():
        yield cursor

    return cursor, _ctx


def test_cache_writes_each_name_into_its_own_column():
    cursor, ctx = _with_cursor()
    with mock.patch.object(sync_stock_names, "db_cursor", ctx):
        sync_stock_names.cache_name("0700.HK", "HK", "腾讯控股", "kurumi", "TENCENT")

    statements = " ".join(sql for sql, _ in cursor.calls)
    assert "company_name_en" in statements
    params = [p for _, p in cursor.calls if p]
    assert ("0700.HK", "HK", "腾讯控股", "TENCENT", "kurumi") in params
    # The English column is written on the fill-only rule of Issue #4, so a later
    # round that resolves no English name cannot blank it out.
    update_en = [sql for sql, _ in cursor.calls
                 if "SET company_name_en" in sql]
    assert update_en and "company_name_en IS NULL OR company_name_en = ''" in update_en[0]


def test_cache_refuses_to_write_nothing():
    cursor, ctx = _with_cursor()
    with mock.patch.object(sync_stock_names, "db_cursor", ctx):
        sync_stock_names.cache_name("0700.HK", "HK", "", "", "")
    assert cursor.calls == []


def test_english_targets_are_the_render_scope_not_the_whole_table():
    """The ``earnings`` table holds thousands of symbols; the sync must not call
    three providers for every one of them."""
    source = (ROOT / "scripts" / "sync_stock_names.py").read_text(encoding="utf-8")
    body = source.split("def name_scope", 1)[1].split("def cache_name", 1)[0]
    assert "popular_stocks" in body
    assert "FROM stock_names" in body
    assert "FROM watchlist" in body
    # The scope query reads only the cache/watchlist tables; the expensive
    # "which symbols exist at all" scan stays out of it.
    assert "FROM earnings" not in body.split("def english_name_targets")[0]


# ── migration (executed against a throwaway schema, rolled back) ───────────

TEST_SCHEMA = "fincal_issue55_selftest"


def _db_connect():
    import psycopg2

    from app import config

    return psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
        user=config.DB_USER, password=config.DB_PASSWORD, connect_timeout=3,
    )


def _db_reachable() -> bool:
    try:
        _db_connect().close()
        return True
    except Exception:  # noqa: BLE001
        return False


def test_db_py_declares_the_column_in_both_places():
    """A fresh install (CREATE TABLE) and an existing one (ALTER) both need it."""
    source = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
    assert "company_name_en TEXT DEFAULT ''" in source
    assert '("company_name_en", "TEXT")' in source
    assert "ALTER TABLE stock_names ADD COLUMN IF NOT EXISTS company_name_en" in source


@pytest.mark.skipif(not _db_reachable(), reason="local fincal PostgreSQL not available")
def test_init_db_adds_the_english_name_columns_idempotently():
    """Acceptance 1: running ``init_db()`` twice must not raise or drop data."""
    with db.db_connection() as conn:
        conn.autocommit = False

        @contextmanager
        def _cursor():
            with conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
                yield cur

        try:
            with conn.cursor() as setup:
                setup.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
                setup.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
                setup.execute(f"SET search_path TO {TEST_SCHEMA}")

            def columns(table):
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT column_name FROM information_schema.columns
                           WHERE table_schema = %s AND table_name = %s""",
                        (TEST_SCHEMA, table),
                    )
                    return {row[0] for row in cur.fetchall()}

            with mock.patch.object(db, "db_cursor", _cursor):
                db.init_db()
                db.init_db()

            assert "company_name_en" in columns("earnings")
            assert "company_name_en" in columns("stock_names")
        finally:
            conn.rollback()

        with conn.cursor() as check:
            check.execute(
                "SELECT count(*) FROM information_schema.schemata WHERE schema_name=%s",
                (TEST_SCHEMA,))
            assert check.fetchone()[0] == 0, "the self-test schema must not survive"
