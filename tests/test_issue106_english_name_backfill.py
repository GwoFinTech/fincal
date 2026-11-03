"""Issue #106: a prediction row must carry the symbol's Latin name.

``earnings.company_name_en`` is what a ``lang=en`` subscription renders, but the
prediction stage wrote only ``company_name`` and the ``stock_names`` stage judged
its backlog per *symbol*: once a symbol had any row with a Latin name it was no
longer a target, so every prediction the weekly run created kept an empty
``company_name_en`` forever (production: 28 of 491 events fell back to the
Chinese name).  Pinned here:

* the prediction INSERT binds the name and its conflict branch is fill-only —
  a provider's own Latin name is never replaced and an empty resolution never
  blanks one;
* ``_company_name_en_for`` reads the cache first, then the newest provider row;
* ``sync_stock_names`` converges every row the cache can name, without touching
  ``updated_at`` (that column is the iCal ``SEQUENCE``, Issue #103), and its
  backlog is judged per row;
* the single read entry point fills a missing ``company_name_en`` from
  ``stock_names``, so an ``en`` feed has no CJK title even before the backfill
  runs, and the extra join column never leaks into the response dict.
"""
import importlib.util
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


predict_earnings = _load("predict_earnings_issue106", "scripts/predict_earnings.py")
sync_stock_names = _load("sync_stock_names_issue106", "scripts/sync_stock_names.py")

from app import earnings as earnings_module  # noqa: E402
from app.ical import generate_ical  # noqa: E402

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


class _RecordingCursor:
    """Records the statements a helper issues; answers via ``responses``.

    ``responses`` maps a substring of the statement to ``(rows, rowcount)`` so a
    test can tell the three SELECTs of ``english_name_targets`` apart.
    """

    def __init__(self, responses=None, rowcount=0):
        self.responses = responses or {}
        self.executed = []
        self.rowcount = rowcount
        self._rows = []

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.executed.append((flat, params))
        self._rows = []
        for marker, (rows, rowcount) in self.responses.items():
            if marker in flat:
                self._rows = rows
                self.rowcount = rowcount
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


# ── the prediction write ───────────────────────────────────────────────────

class PredictionWriteTests(TestCase):
    def test_the_insert_binds_the_latin_name(self):
        cursor = _RecordingCursor()
        predict_earnings.upsert_prediction(
            cursor, symbol="0700.HK", market="HK", company_name="腾讯控股",
            company_name_en="TENCENT", report_date="2027-11-04",
            fiscal_year=2027, fiscal_quarter=3, before_after="after",
        )

        sql, params = cursor.executed[0]
        columns = sql.split("VALUES", 1)[0]
        self.assertIn("company_name_en", columns)
        self.assertEqual(params[:4], ("0700.HK", "HK", "腾讯控股", "TENCENT"))

    def test_the_upsert_is_fill_only_and_owned_rows_only(self):
        expression = predict_earnings._PREDICTION_UPSERT["company_name_en"]

        self.assertIn("earnings.date_source = 'algorithm'", expression,
                      "a provider's own Latin name must never be rewritten")
        self.assertIn("COALESCE(earnings.company_name_en, '') = ''", expression,
                      "fill-only: an existing name is not replaced")
        self.assertIn("EXCLUDED.company_name_en <> ''", expression,
                      "an unresolved name must not blank the stored one")
        self.assertIn("ELSE earnings.company_name_en END", expression)
        self.assertIn("company_name_en", predict_earnings._PREDICTION_REVISION_COLUMNS,
                      "a filled name changes the body, so it is a real revision")

    def test_the_name_is_read_from_the_cache_first(self):
        sql = predict_earnings._COMPANY_NAME_EN_SQL

        self.assertTrue(sql.strip().startswith("SELECT COALESCE("))
        self.assertLess(sql.index("FROM stock_names"), sql.index("FROM earnings"),
                        "the cache is the canonical source; a row is the fallback")

        cursor = _RecordingCursor(responses={
            "FROM stock_names": ([{"company_name_en": "TENCENT"}], 1),
        })
        self.assertEqual(
            predict_earnings._company_name_en_for(cursor, "0700.HK", "HK"), "TENCENT")
        self.assertEqual(cursor.executed[0][1], ("0700.HK", "HK", "0700.HK", "HK"))


class _PredictionCursor(_RecordingCursor):
    """Minimal in-memory model of the statements one prediction issues."""

    def __init__(self, latin_name):
        super().__init__()
        self.latin_name = latin_name
        self.rowcount = 0

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.executed.append((flat, params))
        self._rows = []
        if flat.startswith("SELECT id, report_date, report_type"):
            self._rows = [
                {"id": 1, "report_date": date(2025, 1, 30), "report_type": "Q",
                 "fiscal_year": 2025, "fiscal_quarter": 1, "before_after": "after",
                 "is_predicted": False, "date_source": "longbridge",
                 "eps_actual": 2.4, "revenue_actual": None},
                {"id": 2, "report_date": date(2026, 1, 29), "report_type": "Q",
                 "fiscal_year": 2026, "fiscal_quarter": 1, "before_after": "after",
                 "is_predicted": False, "date_source": "longbridge",
                 "eps_actual": 2.5, "revenue_actual": None},
            ]
        elif flat.startswith("SELECT company_name FROM earnings"):
            self._rows = [{"company_name": "腾讯控股"}]
        elif flat.startswith("SELECT COALESCE("):
            self._rows = [{"company_name_en": self.latin_name}]
        elif flat.startswith("INSERT INTO earnings"):
            self.rowcount = 1

    def fetchall(self):
        return list(self._rows)


class PredictionRunTests(TestCase):
    def test_a_new_prediction_carries_the_cached_latin_name(self):
        cursor = _PredictionCursor(latin_name="TENCENT")
        with mock.patch.object(predict_earnings, "db_cursor", _cursor_ctx(cursor)):
            predict_earnings.predict_for_symbol("0700.HK", "HK")

        inserts = [params for sql, params in cursor.executed
                   if sql.startswith("INSERT INTO earnings")]
        self.assertTrue(inserts, "the run wrote no prediction to inspect")
        self.assertEqual({params[2] for params in inserts}, {"腾讯控股"})
        self.assertEqual({params[3] for params in inserts}, {"TENCENT"})


# ── the sync stage's convergence ───────────────────────────────────────────

class EnglishBackfillTests(TestCase):
    def test_the_backfill_is_fill_only_and_leaves_the_stamp_alone(self):
        cursor = _RecordingCursor(rowcount=28)
        with mock.patch.object(sync_stock_names, "db_cursor", _cursor_ctx(cursor)):
            self.assertEqual(sync_stock_names.backfill_earnings_english_names(), 28)

        sql = cursor.executed[0][0]
        self.assertIn("SET company_name_en = sn.company_name_en", sql)
        self.assertIn("COALESCE(e.company_name_en, '') = ''", sql)
        self.assertIn("COALESCE(sn.company_name_en, '') <> ''", sql)
        self.assertNotIn("updated_at", sql,
                         "the backfill is not a revision (Issue #103)")

    def test_the_backlog_is_judged_per_row_not_per_symbol(self):
        """A symbol with one filled row and one gap row is still a target."""
        scope = [
            {"symbol": "AAPL", "market": "US"},
            {"symbol": "MSFT", "market": "US"},
            {"symbol": "0700.HK", "market": "HK"},
            {"symbol": "NEW", "market": "US"},
        ]
        cursor = _RecordingCursor(responses={
            "FROM stock_names": ([{"symbol": "AAPL", "market": "US",
                                   "company_name_en": "APPLE INC"}], 1),
            "COALESCE(company_name_en, '') = ''": (
                [{"symbol": "MSFT", "market": "US"},
                 {"symbol": "0700.HK", "market": "HK"}], 2),
            "SELECT DISTINCT symbol, market FROM earnings": (
                [{"symbol": "AAPL", "market": "US"},
                 {"symbol": "MSFT", "market": "US"},
                 {"symbol": "0700.HK", "market": "HK"}], 3),
        })
        with mock.patch.object(sync_stock_names, "name_scope", lambda: scope), \
                mock.patch.object(sync_stock_names, "db_cursor", _cursor_ctx(cursor)):
            targets = sync_stock_names.english_name_targets()

        self.assertEqual(
            [(t["symbol"], t["market"]) for t in targets],
            [("MSFT", "US"), ("0700.HK", "HK"), ("NEW", "US")],
            "the cached symbol is skipped; the row-level gap keeps 0700.HK a target "
            "and a symbol with no earnings row keeps its old coverage",
        )


# ── the read path ──────────────────────────────────────────────────────────

def _row(**overrides):
    row = {
        "symbol": "0700.HK", "market": "HK",
        "company_name": "腾讯控股", "company_name_en": "",
        "stock_names_company_name_en": "TENCENT",
        "report_date": date(2027, 11, 4), "report_type": "Q",
        "fiscal_year": 2027, "fiscal_quarter": 3,
        "before_after": "after", "is_predicted": True,
        "date_source": "algorithm", "date_status": "predicted",
        "eps_estimate": None, "eps_actual": None,
        "revenue_estimate": None, "revenue_actual": None,
        "estimate_currency": None, "actual_currency": None,
        "estimate_as_of": None, "actual_as_of": None,
        "updated_at": datetime(2026, 10, 26, 8, 19),
    }
    row.update(overrides)
    return row


class ReadPathTests(TestCase):
    def _fetch(self, row):
        cursor = _RecordingCursor(responses={
            "FROM earnings e LEFT JOIN earnings_consensus": ([row], 1),
        })
        from app import db as db_module
        with mock.patch.object(db_module, "db_cursor", _cursor_ctx(cursor)):
            return earnings_module.fetch_earnings_from_db(
                symbols=["0700.HK"], markets=["HK"],
                start=date(2026, 10, 28), end=date(2028, 1, 1),
            )

    def test_a_row_without_a_latin_name_is_filled_from_the_cache(self):
        rows = self._fetch(_row())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company_name_en"], "TENCENT")
        self.assertNotIn("stock_names_company_name_en", rows[0],
                         "the join's helper column must not reach the response")

    def test_a_stored_latin_name_wins_over_the_cache(self):
        rows = self._fetch(_row(company_name_en="TENCENT HOLDINGS"))

        self.assertEqual(rows[0]["company_name_en"], "TENCENT HOLDINGS")

    def test_an_english_feed_has_no_chinese_title(self):
        rows = self._fetch(_row())

        ics = generate_ical(rows, title_lang="en")
        summaries = [line for line in ics.replace("\r\n ", "").split("\r\n")
                     if line.startswith("SUMMARY:")]

        self.assertTrue(summaries)
        for summary in summaries:
            self.assertIsNone(CJK_RE.search(summary), summary)
            self.assertIn("TENCENT", summary)

    def test_a_chinese_feed_is_byte_identical_with_or_without_the_fallback(self):
        """``lang=zh`` reads ``company_name`` only, so the fill must not touch it."""
        with_fallback = generate_ical(self._fetch(_row()), title_lang="zh")
        without_fallback = generate_ical(
            self._fetch(_row(company_name_en="TENCENT",
                             stock_names_company_name_en=None)),
            title_lang="zh",
        )

        self.assertEqual(with_fallback, without_fallback)
        self.assertIn("腾讯控股", with_fallback)
