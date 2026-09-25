"""Issue #61: an estimate and an actual may only be compared when they are the
same quantity.

Production rendered ``(actual - estimate) / |estimate|`` unconditionally, while the
two figures come from providers that disagree about the unit: the estimate is
Longbridge's calendar figure in the listing's *quote* currency and the actual is
often Futu's statement in the *reporting* currency (TSM → ×32 = TWD/USD,
BABA/PDD/NIO/XPEV → ×6.8 = CNY/USD).  In the default universe window 50 of 184
EPS pairs exceeded +500%, the largest +108319%, and every one of them was a
Longbridge-estimate × Futu-actual row.  The attribution columns that could have
said so (``estimate_currency``/``estimate_basis``) had never been written and had
no actual-side counterpart at all.

Covered here:

* the comparability rule (``app.fiscal.comparison_unavailable_reason``) and the
  attribution normalizers that never guess a currency;
* the single read entry point (``app.earnings.fetch_earnings_from_db``) tagging
  every row, so the API, the CSV/JSON export and the iCal feed agree;
* the write paths stamping the provider's own attribution (Longbridge calendar
  currency, OpenD ``currency_code``/``accounting_standards``);
* the display guards: the calendar/panel never renders a surplus (or a
  beat/miss colour, or an up/down arrow) for a flagged row, and the iCal
  description labels each figure with its currency;
* the one-time backfill script (dry-run by default, attribution columns only).
"""
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, fiscal  # noqa: E402
from app.earnings import fetch_earnings_from_db  # noqa: E402
from app.ical import generate_ical  # noqa: E402
from app.schemas import EarningItem  # noqa: E402


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync_earnings = _load("sync_earnings_issue61", "scripts/sync_earnings.py")
sync_futu = _load("sync_futu_issue61", "scripts/sync_futu.py")
backfill = _load("backfill_comparison_attribution", "scripts/backfill_comparison_attribution.py")

APP_JS = ROOT / "app" / "static" / "assets" / "app-setup.js"
INDEX_HTML = ROOT / "app" / "static" / "index.html"

#: The production KTOS row: same currency code, two different per-share bases.
KTOS_ROW = {
    "symbol": "KTOS", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 2,
    "eps_estimate": -0.00512, "eps_actual": 5.540795,
    "estimate_currency": "USD", "actual_currency": "USD",
    "estimate_basis": "unknown", "actual_basis": "unknown",
    "estimate_source": "longbridge", "actual_source": "futu",
}

#: The production TSM row: quote currency against reporting currency.
TSM_ROW = {
    "symbol": "TSM", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 2,
    "eps_estimate": 3.94329, "eps_actual": 2.457635,
    "revenue_estimate": 39681347466.10591, "revenue_actual": 1270380250000.0,
    "estimate_currency": "USD", "actual_currency": "TWD",
    "estimate_basis": "unknown", "actual_basis": "unknown",
    "estimate_source": "longbridge", "actual_source": "futu",
}

#: A healthy row: one provider produced both numbers.
LONGBRIDGE_ROW = {
    "symbol": "AZO", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 4,
    "eps_estimate": 53.88695, "eps_actual": 56.05,
    "estimate_currency": "USD", "actual_currency": "USD",
    "estimate_basis": "unknown", "actual_basis": "unknown",
    "estimate_source": "longbridge", "actual_source": "longbridge",
}


class ComparisonRuleTests(TestCase):
    """The rule itself: attributed-and-equal is comparable, everything else is not."""

    def test_one_provider_with_the_same_currency_is_comparable(self):
        self.assertIsNone(fiscal.comparison_unavailable_reason(LONGBRIDGE_ROW))

    def test_quote_currency_against_reporting_currency_is_a_mismatch(self):
        self.assertEqual(
            fiscal.comparison_unavailable_reason(TSM_ROW),
            fiscal.COMPARISON_CURRENCY_MISMATCH,
        )

    def test_unattributed_currency_is_not_assumed_to_match(self):
        for value in (None, "", "unknown", "UNKNOWN"):
            row = dict(LONGBRIDGE_ROW, actual_currency=value)
            self.assertEqual(
                fiscal.comparison_unavailable_reason(row),
                fiscal.COMPARISON_CURRENCY_UNKNOWN,
                f"currency {value!r} must not be treated as attributed",
            )

    def test_different_stated_bases_are_blocked(self):
        row = dict(LONGBRIDGE_ROW, estimate_basis="adjusted", actual_basis="gaap")
        self.assertEqual(
            fiscal.comparison_unavailable_reason(row),
            fiscal.COMPARISON_BASIS_MISMATCH,
        )

    def test_same_stated_basis_across_providers_is_comparable(self):
        row = dict(KTOS_ROW, estimate_basis="gaap", actual_basis="gaap")
        self.assertIsNone(fiscal.comparison_unavailable_reason(row))

    def test_cross_provider_without_a_stated_basis_is_unverified(self):
        # KTOS: same currency code, 1083× apart — nothing proves the two figures
        # share a base, so the row must not be subtracted.
        self.assertEqual(
            fiscal.comparison_unavailable_reason(KTOS_ROW),
            fiscal.COMPARISON_BASIS_UNVERIFIED,
        )

    def test_an_unattributed_source_is_not_treated_as_the_same_provider(self):
        row = dict(LONGBRIDGE_ROW, actual_source=None)
        self.assertEqual(
            fiscal.comparison_unavailable_reason(row),
            fiscal.COMPARISON_BASIS_UNVERIFIED,
        )

    def test_a_row_without_a_complete_pair_is_not_flagged(self):
        row = dict(LONGBRIDGE_ROW, eps_estimate=None, eps_actual=None)
        self.assertIsNone(fiscal.comparison_unavailable_reason(row))
        self.assertFalse(fiscal.has_comparable_values(row))

    def test_declared_attribution_normalises_labels(self):
        self.assertEqual(fiscal.declared_attribution({"x": " usd "}, "x"), "USD")
        for value in (None, "", "unknown", "Unknown"):
            self.assertIsNone(fiscal.declared_attribution({"x": value}, "x"))


class AttributionNormalizerTests(TestCase):
    """Providers' own labels are kept; nothing is guessed."""

    def test_currency_requires_a_three_letter_code(self):
        from app.provenance import normalize_currency

        self.assertEqual(normalize_currency(" twd "), "TWD")
        for value in (None, "", "T", "US dollars", "123", "EURO"):
            self.assertEqual(normalize_currency(value), "unknown")

    def test_basis_maps_the_providers_words(self):
        from app.provenance import normalize_basis

        self.assertEqual(normalize_basis("US_GAAP"), "gaap")
        self.assertEqual(normalize_basis("国际会计准则"), "ifrs")
        self.assertEqual(normalize_basis("IFRS"), "ifrs")
        for value in (None, "", "whatever"):
            self.assertEqual(normalize_basis(value), "unknown")


class _FakeCursor:
    """Minimal cursor double returning canned rows for ``SELECT e.*`` reads."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    """Context-manager connection double around :class:`_FakeCursor`."""

    def __init__(self, rows):
        self._rows = rows

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self._rows)

    def __enter__(self):
        return _FakeCursor(self._rows)

    def __exit__(self, *args):
        return False


class _RecordingExecuteValues:
    """Stand-in for ``psycopg2.extras.execute_values`` (same shape as Issue #50's)."""

    def __init__(self):
        self.calls = []

    def __call__(self, cur, sql, argslist, page_size=None, fetch=False):
        self.calls.append({"sql": " ".join(str(sql).split()), "rows": list(argslist)})


def _fake_db_cursor(cursor):
    ctx = MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    return ctx


class ReadPathTests(TestCase):
    """One entry point tags the rows: API, exports and the feed cannot disagree."""

    def test_fetch_earnings_attaches_the_reason_to_every_row(self):
        rows = [dict(TSM_ROW, report_date=date(2026, 7, 15), id=1),
                dict(LONGBRIDGE_ROW, report_date=date(2026, 9, 22), id=2)]
        with patch.object(db, "db_cursor", lambda: _FakeConn(rows)):
            result = fetch_earnings_from_db(symbols=["TSM", "AZO"], start=date(2026, 1, 1),
                                            end=date(2026, 12, 31))
        by_symbol = {row["symbol"]: row for row in result}
        self.assertEqual(by_symbol["TSM"]["comparison_unavailable_reason"], "currency_mismatch")
        self.assertIsNone(by_symbol["AZO"]["comparison_unavailable_reason"])

    def test_earning_item_exposes_the_attribution_contract(self):
        fields = set(EarningItem.model_fields)
        for field in ("estimate_currency", "estimate_basis", "actual_currency",
                      "actual_basis", "comparison_unavailable_reason"):
            self.assertIn(field, fields, f"EarningItem must expose {field}")

    def test_export_field_set_covers_the_attribution_contract(self):
        """The CSV fallback column list must mention the new fields (Issue #47 rule)."""
        source = (ROOT / "app" / "routers" / "api.py").read_text(encoding="utf-8")
        for field in ("actual_currency", "actual_basis", "comparison_unavailable_reason"):
            self.assertIn(f'"{field}"', source)


class _FakeResult:
    def __init__(self, pages):
        self.pages = pages


class LongbridgeAttributionWriteTests(TestCase):
    """The calendar states the currency of the figures it delivers."""

    def _batch_for(self, pages):
        """Collect the batch ``sync_earnings`` hands to ``_flush_batch`` (US only)."""
        captured = {}

        def fake_flush(stats, batch, label):
            captured.setdefault("rows", []).extend(batch)

        def fake_fetch(market, start, end):
            return pages if market == "US" else []

        with patch.object(sync_earnings, "fetch_calendar", side_effect=fake_fetch), \
             patch.object(sync_earnings, "check_cancelled"), \
             patch.object(sync_earnings, "_flush_batch", side_effect=fake_flush):
            stats = sync_earnings.SyncStats()
            sync_earnings.sync_earnings(1, stats)
        return captured["rows"]

    @staticmethod
    def _info(currency="USD", eps_estimate="-0.00512", eps_actual="0.02"):
        return {
            "counter_id": "ST/US/KTOS", "counter_name": "Kratos",
            "currency": currency, "date": "2026.08.04 (美东)", "date_type": "盘后",
            "data_kv": [
                {"type": "estimate_eps", "value_raw": eps_estimate},
                {"type": "actual_eps", "value_raw": eps_actual},
            ],
            "ext": {"financial_report": {"fiscal_year": "2026", "period": "2"}},
        }

    def test_batch_carries_the_declared_currency_on_both_sides(self):
        rows = self._batch_for([{"infos": [self._info()]}])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[12], "USD", "estimate currency must travel with the estimate")
        self.assertEqual(row[13], "USD", "a Longbridge actual is in the same currency")
        self.assertEqual(row[14], "unknown")
        self.assertEqual(row[15], "unknown")

    def test_an_unstated_currency_is_recorded_as_unknown_not_assumed(self):
        rows = self._batch_for([{"infos": [self._info(currency=None)]}])
        self.assertEqual(rows[0][12], "unknown")
        self.assertEqual(rows[0][13], "unknown")

    def test_the_actual_side_is_only_labelled_when_this_event_carries_it(self):
        info = self._info(eps_actual="")
        rows = self._batch_for([{"infos": [info]}])
        self.assertEqual(rows[0][12], "USD")
        self.assertIsNone(rows[0][13],
                          "an event without actuals must not label the row's Futu actual")

    def test_upsert_labels_the_actual_side_only_for_the_values_it_wrote(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        recorder = _RecordingExecuteValues()
        with patch("psycopg2.extras.execute_values", recorder), \
             patch.object(sync_earnings, "db_cursor", return_value=_fake_db_cursor(cursor)):
            sync_earnings.flush_batch(cursor, [
                sync_earnings_tuple(estimate_currency="USD", actual_currency="USD",
                                    eps_actual=0.02),
            ])
        sql = _insert_sql(recorder)
        self.assertIn("estimate_currency = COALESCE(EXCLUDED.estimate_currency", sql)
        self.assertIn("CASE WHEN EXCLUDED.eps_actual IS NOT NULL "
                      "OR EXCLUDED.revenue_actual IS NOT NULL", sql)

    def test_insert_columns_match_the_batch_width(self):
        """The batch layout and the INSERT column list are one contract (Issue #61)."""
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        recorder = _RecordingExecuteValues()
        with patch("psycopg2.extras.execute_values", recorder), \
             patch.object(sync_earnings, "db_cursor", return_value=_fake_db_cursor(cursor)):
            sync_earnings.flush_batch(cursor, [sync_earnings_tuple()])
        call = [call for call in recorder.calls if "INSERT INTO earnings " in call["sql"]][0]
        columns = re.findall(r"\(([^()]*)\)\s*VALUES", call["sql"], re.S)[0]
        column_count = len([c for c in columns.split(",") if c.strip()])
        self.assertEqual(column_count, len(call["rows"][0]),
                         "flush_batch must pass exactly one value per INSERT column")


def _insert_sql(recorder) -> str:
    return [call["sql"] for call in recorder.calls if "INSERT INTO earnings " in call["sql"]][0]


def sync_earnings_tuple(estimate_currency=None, actual_currency=None, eps_actual=None,
                        eps_estimate=None):
    """One Longbridge batch tuple in the current layout (see ``flush_batch``)."""
    return ("KTOS", "US", "Kratos", "2026-08-04", "Q", 2026, 2,
            eps_estimate, eps_actual, None, None, "after",
            estimate_currency, actual_currency, "unknown", "unknown")


class _FakeFutuContext:
    """OpenD double returning one statement per period with a declared currency."""

    def __init__(self, currency="TWD", standard="US_GAAP", eps=2.457635, revenue=1270380250000.0):
        self.currency = currency
        self.standard = standard
        self.eps = eps
        self.revenue = revenue

    def get_financials_statements(self, code, statement_type=1, financial_type=9, num=4):
        # Since Issue #62 both figures come from the income statement, each with
        # the provider's own label: 基本每股收益 (fid=8047) and 营业总收入
        # (fid=8002).
        return 0, {"report_list": [{
            "fiscal_year": 2026, "financial_type": 2,
            "currency_code": self.currency, "accounting_standards": self.standard,
            "item_list": [
                {"field_id": 8047, "display_name": "基本每股收益", "data": self.eps},
                {"field_id": 8002, "display_name": "营业总收入", "data": self.revenue},
            ],
        }]}

    def close(self):
        pass


class FutuAttributionWriteTests(TestCase):
    """OpenD's own words go on the row; nothing is inferred from the listing."""

    def _updates(self, ctx):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        with patch.object(sync_futu, "check_cancelled"), \
             patch.object(sync_futu, "db_cursor", return_value=MagicMock(
                 __enter__=MagicMock(return_value=cursor), __exit__=MagicMock(return_value=False))):
            sync_futu.sync_actuals(ctx, 1, ["TSM.US"])
        return [call.args for call in cursor.execute.call_args_list
                if str(call.args[0]).strip().startswith("UPDATE earnings")]

    def test_every_actuals_update_records_currency_and_basis(self):
        updates = self._updates(_FakeFutuContext(currency="TWD", standard="US_GAAP"))
        self.assertEqual(len(updates), 2, "EPS and revenue updates are expected")
        for sql, params in updates:
            self.assertIn("actual_currency = %s", sql)
            self.assertIn("actual_basis = %s", sql)
            self.assertIn("TWD", params, "the provider's reporting currency must be stored")

    def test_an_undeclared_currency_is_stored_as_unknown(self):
        updates = self._updates(_FakeFutuContext(currency=None, standard=""))
        for _sql, params in updates:
            self.assertIn("unknown", params)
            self.assertNotIn("USD", params,
                             "the listing's quote currency must never be assumed")


class FeedAndUiTests(TestCase):
    """No outlet may present a surplus for a flagged row."""

    @staticmethod
    def _flat(ics: str) -> str:
        """Unfold the ICS and undo RFC 5545 escaping of ';'."""
        return ics.replace("\r\n ", "").replace("\\;", ";")

    def test_ical_description_labels_each_figure_with_its_currency(self):
        ics = generate_ical([dict(TSM_ROW, report_date=date(2026, 7, 15))])
        flat = self._flat(ics)
        self.assertIn("EPS Est: 3.94329 (USD)", flat)
        self.assertIn("EPS Actual: 2.457635 (TWD)", flat)
        self.assertIn("Comparability: unavailable (different currencies; currency_mismatch)", flat)

    def test_ical_description_marks_an_undeclared_currency(self):
        row = dict(KTOS_ROW, report_date=date(2026, 8, 4),
                   estimate_currency=None, actual_currency=None)
        flat = self._flat(generate_ical([row]))
        self.assertIn("(currency unknown)", flat)
        self.assertIn("Comparability: unavailable (currency undeclared; currency_unknown)", flat)
        # The summary language also switches the comparability wording.
        zh = self._flat(generate_ical([dict(TSM_ROW, report_date=date(2026, 7, 15))], title_lang="zh"))
        self.assertIn("Comparability: unavailable (币种不同; currency_mismatch)", zh)

    def test_comparable_row_gets_no_comparability_warning(self):
        ics = generate_ical([dict(LONGBRIDGE_ROW, report_date=date(2026, 9, 22))])
        self.assertNotIn("Comparability:", ics)

    def test_calendar_templates_gate_every_surplus_on_the_guard(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        # The arrow in the month grid and the percentage in the day list must use
        # the guard instead of "both values exist".
        self.assertNotIn("v-if=\"e.eps_estimate != null && e.eps_actual != null\"", html)
        self.assertEqual(html.count("hasComparison(e, 'eps')"), 2)
        self.assertIn("hasComparison(selectedEarning, 'eps')", html)
        self.assertIn("hasComparison(selectedEarning, 'revenue')", html)
        self.assertIn("comparison_unavailable_reason", js)

    @skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
    def test_formatters_refuse_to_compute_a_surplus_for_a_flagged_row(self):
        """Run the real formatter code from app-setup.js under node."""
        result = _run_formatters_probe()
        self.assertIsNone(result["flagged_eps"], "a flagged row must not yield a surplus")
        self.assertIsNone(result["flagged_rev"], "a flagged row must not yield a surplus")
        self.assertEqual(result["flagged_class"], "", "no beat/miss colour for a flagged row")
        self.assertFalse(result["flagged_has_eps"])
        self.assertTrue(result["has_eps"])
        self.assertEqual(result["eps"], 1.0)
        self.assertEqual(result["note"], "预期与实际币种不同，无法比较")

    @skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
    def test_formatters_withhold_a_period_ratio_the_api_did_not_attest(self):
        """Issue #63: the cross-period guard, in the real formatter code."""
        result = _run_growth_probe()
        self.assertIsNone(result["flagged_eps"], "a flagged pair must not yield a ratio")
        self.assertEqual(result["flagged_eps_cell"], "不可比")
        self.assertEqual(result["flagged_eps_note"], "本期与上期币种未标明，无法比较")
        # The comparable 环比 of the same row still renders.
        self.assertEqual(result["flagged_eps_qoq"], 0.25)
        self.assertEqual(result["healthy_eps_cell"], "56.5%")
        self.assertEqual(result["healthy_eps"], 0.56477)
        # No value at all is a missing period, not a comparability problem.
        self.assertEqual(result["missing"], "—")
        self.assertIn("EPS 同比", result["note"])
        self.assertIn("不是数据缺失", result["note"])
        self.assertEqual(result["no_note"], "")


def _formatters_function_source() -> str:
    """Extract ``useFormatters`` from the served app-setup.js."""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function useFormatters()")
    depth = 0
    end = None
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    assert end, "could not locate the end of useFormatters()"
    return source[start:end]


def _run_formatters_probe() -> dict:
    """Evaluate ``useFormatters`` extracted from app-setup.js and probe the guard."""
    function_source = _formatters_function_source()
    probe = f"""
{function_source}
const fmt = useFormatters();
const flagged = {{ eps_estimate: -0.00512, eps_actual: 5.540795,
                   revenue_estimate: 1, revenue_actual: 2,
                   comparison_unavailable_reason: 'currency_mismatch' }};
const healthy = {{ eps_estimate: 1, eps_actual: 2 }};
const out = {{
  flagged_eps: fmt.epsSurplus(flagged),
  flagged_rev: fmt.revSurplus(flagged),
  flagged_class: fmt.epsSurplusClass(flagged),
  flagged_has_eps: fmt.hasComparison(flagged, 'eps'),
  has_eps: fmt.hasComparison(healthy, 'eps'),
  eps: fmt.epsSurplus(healthy),
  note: fmt.comparisonNote(flagged),
}};
console.log(JSON.stringify(out));
"""
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _run_growth_probe() -> dict:
    """Probe the cross-period guard with a decision payload shaped like the API's."""
    probe = f"""
{_formatters_function_source()}
const fmt = useFormatters();
const flagged = {{ actual_growth: {{
  eps_yoy: null, eps_yoy_reason: 'currency_unknown',
  eps_qoq: 0.25, eps_qoq_reason: null,
  revenue_yoy: 3.6, revenue_yoy_reason: null,
  revenue_qoq: null, revenue_qoq_reason: null }} }};
const healthy = {{ actual_growth: {{
  eps_yoy: 0.56477, eps_yoy_reason: null,
  eps_qoq: null, eps_qoq_reason: null,
  revenue_yoy: 0.1, revenue_yoy_reason: null,
  revenue_qoq: null, revenue_qoq_reason: null }} }};
const out = {{
  flagged_eps: fmt.growthValue(flagged, 'eps_yoy'),
  flagged_eps_cell: fmt.growthCell(flagged, 'eps_yoy'),
  flagged_eps_note: fmt.growthNote(flagged, 'eps_yoy'),
  flagged_eps_qoq: fmt.growthValue(flagged, 'eps_qoq'),
  flagged_rev_cell: fmt.growthCell(flagged, 'revenue_yoy'),
  healthy_eps: fmt.growthValue(healthy, 'eps_yoy'),
  healthy_eps_cell: fmt.growthCell(healthy, 'eps_yoy'),
  missing: fmt.growthCell({{}}, 'eps_yoy'),
  note: fmt.growthSuppressedNote(flagged),
  no_note: fmt.growthSuppressedNote(healthy),
}};
console.log(JSON.stringify(out));
"""
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


class BackfillPlanTests(TestCase):
    """The backfill writes attribution only, and only where it is not claimed yet."""

    def _row(self, **kwargs):
        row = {
            "id": 1, "symbol": "TSM", "market": "US", "report_date": date(2026, 7, 15),
            "fiscal_year": 2026, "fiscal_quarter": 2,
            "eps_estimate": 3.94329, "eps_actual": 2.457635,
            "revenue_estimate": 39681347466.1, "revenue_actual": 1270380250000.0,
            "estimate_source": "longbridge", "actual_source": "futu",
            "estimate_currency": None, "estimate_basis": None,
            "actual_currency": None, "actual_basis": None,
        }
        row.update(kwargs)
        return row

    def test_plan_labels_both_sides_from_provider_declarations_only(self):
        rows = [self._row(), self._row(id=2, symbol="AZO", actual_source="longbridge")]
        plan = backfill.build_plan(
            rows,
            by_date={("AZO", "US", "2026-07-15"): "USD"},
            by_period={("TSM", "US", 2026, 2): "USD"},
            futu={("TSM", "US", 2026, 2): ("TWD", "gaap")},
        )
        by_id = {update["id"]: update for update in plan}
        self.assertEqual(by_id[1]["estimate_currency"], "USD")
        self.assertEqual(by_id[1]["actual_currency"], "TWD")
        self.assertEqual(by_id[1]["actual_basis"], "gaap")
        self.assertEqual(by_id[2]["actual_currency"], "USD")
        for update in plan:
            self.assertNotIn("eps_actual", update)
            self.assertNotIn("revenue_actual", update)

    def test_an_unattributed_actual_is_left_alone(self):
        row = self._row(actual_source=None)
        plan = backfill.build_plan([row], {}, {("TSM", "US", 2026, 2): "USD"}, {})
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["estimate_currency"], "USD")
        self.assertNotIn("actual_currency", plan[0],
                         "a value of unknown origin must not be attributed to a currency")

    def test_existing_attribution_is_never_overwritten(self):
        row = self._row(estimate_currency="USD", actual_currency="TWD", estimate_basis="gaap")
        plan = backfill.build_plan([row], {}, {("TSM", "US", 2026, 2): "USD"},
                                   {("TSM", "US", 2026, 2): ("TWD", "gaap")})
        self.assertEqual(plan, [])

    def test_dry_run_does_not_write(self):
        executed = []
        cursor = MagicMock()
        cursor.execute.side_effect = lambda *args, **kwargs: executed.append(args)

        with patch.object(backfill, "longbridge_currency_map",
                          return_value=({}, {("TSM", "US", 2026, 2): "USD"})), \
             patch.object(backfill, "load_rows", return_value=[self._row()]), \
             patch.object(backfill, "futu_attribution", return_value={}), \
             patch.object(backfill, "db_cursor", return_value=MagicMock(
                 __enter__=MagicMock(return_value=cursor), __exit__=MagicMock(return_value=False))), \
             patch.object(sys, "argv", ["backfill"]):
            backfill.main()
        self.assertEqual(executed, [], "the default run must not touch the database")


# ── Issue #63: the same contract, across two fiscal periods ────────────────
#
# The derived 同比/环比 ratios subtract two *different* periods' actuals, so the
# row-level rule above never reached them: production rendered "+5647.7%" for TSM
# (row 29078) while the same panel marked the same row's 较预期 as unavailable.
# The rule is now shared, and the ratio a client receives is either a real number
# or nothing plus a reason.

#: The production TSM pair: unattributed prior-year actual against a Futu TWD one.
TSM_GROWTH_PAIR = (
    {"id": 1, "symbol": "TSM", "market": "US", "fiscal_year": 2025, "fiscal_quarter": 2,
     "report_date": date(2025, 7, 17), "eps_actual": 2.370496, "revenue_actual": None,
     "actual_currency": None, "actual_basis": None, "actual_source": None},
    {"id": 2, "symbol": "TSM", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 2,
     "report_date": date(2026, 7, 15), "eps_actual": 136.25, "revenue_actual": None,
     "actual_currency": "TWD", "actual_basis": "gaap", "actual_source": "futu"},
)

#: Two quarters one provider wrote in one currency and one basis.
COMPARABLE_GROWTH_PAIR = (
    {"id": 3, "symbol": "AZO", "market": "US", "fiscal_year": 2025, "fiscal_quarter": 4,
     "report_date": date(2025, 9, 23), "eps_actual": 40.0, "revenue_actual": None,
     "actual_currency": "USD", "actual_basis": None, "actual_source": "longbridge"},
    {"id": 4, "symbol": "AZO", "market": "US", "fiscal_year": 2026, "fiscal_quarter": 4,
     "report_date": date(2026, 9, 22), "eps_actual": 50.0, "revenue_actual": None,
     "actual_currency": "USD", "actual_basis": None, "actual_source": "longbridge"},
)


class CrossPeriodGrowthRuleTests(TestCase):
    """Two periods' actuals may only be subtracted when both are attributed."""

    def test_an_unattributed_period_is_not_assumed_to_match(self):
        self.assertEqual(
            fiscal.growth_unavailable_reason(*TSM_GROWTH_PAIR, metric="eps"),
            fiscal.COMPARISON_CURRENCY_UNKNOWN,
        )

    def test_two_currencies_are_a_mismatch(self):
        pair = (dict(TSM_GROWTH_PAIR[0], actual_currency="USD", actual_source="futu"),
                TSM_GROWTH_PAIR[1])
        self.assertEqual(
            fiscal.growth_unavailable_reason(*pair, metric="eps"),
            fiscal.COMPARISON_CURRENCY_MISMATCH,
        )

    def test_cross_provider_without_a_stated_basis_is_unverified(self):
        pair = (dict(COMPARABLE_GROWTH_PAIR[0], actual_source="longbridge", actual_basis=None),
                dict(COMPARABLE_GROWTH_PAIR[1], actual_source="futu", actual_basis=None))
        self.assertEqual(
            fiscal.growth_unavailable_reason(*pair, metric="eps"),
            fiscal.COMPARISON_BASIS_UNVERIFIED,
        )

    def test_one_provider_needs_no_stated_basis(self):
        self.assertIsNone(fiscal.growth_unavailable_reason(*COMPARABLE_GROWTH_PAIR, metric="eps"))

    def test_two_different_stated_bases_are_blocked(self):
        pair = (dict(COMPARABLE_GROWTH_PAIR[0], actual_basis="adjusted"),
                dict(COMPARABLE_GROWTH_PAIR[1], actual_basis="gaap"))
        self.assertEqual(
            fiscal.growth_unavailable_reason(*pair, metric="eps"),
            fiscal.COMPARISON_BASIS_MISMATCH,
        )

    def test_a_missing_side_is_not_a_comparability_problem(self):
        self.assertIsNone(fiscal.growth_unavailable_reason(None, TSM_GROWTH_PAIR[1], metric="eps"))
        self.assertIsNone(fiscal.growth_unavailable_reason(TSM_GROWTH_PAIR[1], None, metric="eps"))
        missing = dict(TSM_GROWTH_PAIR[1], eps_actual=None)
        self.assertIsNone(fiscal.growth_unavailable_reason(missing, TSM_GROWTH_PAIR[0], metric="eps"))

    def test_each_metric_reads_its_own_column(self):
        """Revenue is compared on ``revenue_actual``, never on the EPS column."""
        pair = (dict(TSM_GROWTH_PAIR[0], eps_actual=None, revenue_actual=100.0),
                dict(TSM_GROWTH_PAIR[1], revenue_actual=200.0))
        self.assertIsNone(fiscal.growth_unavailable_reason(*pair, metric="eps"),
                          "the unattributed revenue pair must not block the EPS comparison")
        self.assertEqual(
            fiscal.growth_unavailable_reason(*pair, metric="revenue"),
            fiscal.COMPARISON_CURRENCY_UNKNOWN,
        )

    def test_an_unknown_metric_is_refused(self):
        with self.assertRaises(ValueError):
            fiscal.growth_unavailable_reason(*COMPARABLE_GROWTH_PAIR, metric="eps_growth")


class DecisionGrowthContractTests(TestCase):
    """``/decision`` never hands a client a ratio it must not render."""

    def _history(self, pair):
        return [dict(row, eps_estimate=None) for row in pair]

    def test_ratio_and_reason_are_mutually_exclusive(self):
        from app.phase3 import build_decision_metrics

        for pair, expected_reason in ((TSM_GROWTH_PAIR, "currency_unknown"),
                                      (COMPARABLE_GROWTH_PAIR, None)):
            growth = build_decision_metrics(self._history(pair), earning_id=pair[1]["id"])["actual_growth"]
            self.assertEqual(growth["eps_yoy_reason"], expected_reason)
            if expected_reason:
                self.assertIsNone(growth["eps_yoy"])
            else:
                self.assertEqual(growth["eps_yoy"], Decimal("0.25"))
                self.assertIsNotNone(growth["eps_yoy"])

    def test_every_metric_ships_a_reason_key(self):
        from app.phase3 import build_decision_metrics, GROWTH_METRICS

        growth = build_decision_metrics(self._history(TSM_GROWTH_PAIR), earning_id=2)["actual_growth"]
        for key, _metric, _span in GROWTH_METRICS:
            self.assertIn(key, growth)
            self.assertIn(f"{key}_reason", growth)

    def test_the_response_model_declares_the_growth_and_its_reason(self):
        from app.schemas import ActualGrowth, DecisionResponse

        self.assertIn("actual_growth", DecisionResponse.model_fields)
        self.assertEqual(set(ActualGrowth.model_fields), {
            "eps_yoy", "eps_yoy_reason", "eps_qoq", "eps_qoq_reason",
            "revenue_yoy", "revenue_yoy_reason", "revenue_qoq", "revenue_qoq_reason",
        })

    def test_the_endpoint_reads_the_attribution_it_judges(self):
        """The history query must carry the columns the rule reads."""
        source = (ROOT / "app" / "routers" / "api.py").read_text(encoding="utf-8")
        history_query = next(line for line in source.splitlines()
                             if "FROM earnings WHERE symbol=%s AND market=%s" in line)
        for field in ("actual_currency", "actual_basis", "actual_source"):
            self.assertIn(field, history_query, f"the growth rule needs {field} in the history query")

    def test_the_detailed_panel_never_renders_a_ratio_for_a_flagged_pair(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertNotIn("decision.actual_growth.eps_yoy", html)
        self.assertNotIn("decision.actual_growth.revenue_yoy", html)
        # The panel and the 业绩对比 table both go through the guard.
        self.assertIn("growthValue(decision, 'eps_yoy')", html)
        self.assertIn("growthValue(decision, 'eps_qoq')", html)
        self.assertIn("growthCell(decision, 'eps_yoy')", html)
        self.assertIn("growthCell(decision, 'revenue_yoy')", html)
        self.assertIn("growthSuppressedNote(decision)", html)


class DecisionEndpointTests(TestCase):
    """The wire response of the production repro (TSM row 29078)."""

    class _ScriptedCursor:
        def __init__(self, pages):
            self._pages = list(pages)
            self._current = None
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append(" ".join(str(sql).split()))
            self._current = self._pages.pop(0) if self._pages else []

        def fetchone(self):
            return self._current[0] if isinstance(self._current, list) and self._current else None

        def fetchall(self):
            return self._current if isinstance(self._current, list) else []

        def close(self):
            pass

    def _decision(self, earning, history):
        from app.main import app as fastapi_app
        from app.auth import get_current_user
        from app.routers import api as api_router

        cursor = self._ScriptedCursor([[earning], history, [], [], []])
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor
        ctx.__exit__.return_value = False
        fastapi_app.dependency_overrides[get_current_user] = lambda: {
            "id": 1, "email": "t@t.com", "name": "T", "role": "user"}
        try:
            with patch.object(db, "db_cursor", lambda: ctx), \
                 patch.object(api_router, "ensure_user", return_value={}):
                client = TestClient(fastapi_app, raise_server_exceptions=False)
                response = client.get(f"/api/earnings/{earning['id']}/decision")
        finally:
            fastapi_app.dependency_overrides = {}
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_cross_currency_pair_returns_no_ratio_and_a_reason(self):
        earning = dict(TSM_GROWTH_PAIR[1], id=2, report_type="Q", is_predicted=False,
                       eps_estimate=3.94329, estimate_currency="USD",
                       revenue_actual=None, revenue_estimate=None)
        body = self._decision(earning, [dict(r, eps_estimate=None) for r in TSM_GROWTH_PAIR])
        growth = body["actual_growth"]
        self.assertIsNone(growth["eps_yoy"])
        self.assertEqual(growth["eps_yoy_reason"], "currency_unknown")

    def test_an_attributed_pair_still_returns_the_ratio(self):
        earning = dict(COMPARABLE_GROWTH_PAIR[1], id=4, report_type="Q", is_predicted=False,
                       eps_estimate=45.0, estimate_currency="USD",
                       revenue_actual=None, revenue_estimate=None)
        body = self._decision(earning, [dict(r, eps_estimate=None) for r in COMPARABLE_GROWTH_PAIR])
        growth = body["actual_growth"]
        self.assertEqual(Decimal(str(growth["eps_yoy"])), Decimal("0.25"))
        self.assertIsNone(growth["eps_yoy_reason"])
