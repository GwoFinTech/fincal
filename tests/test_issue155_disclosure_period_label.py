"""Issue #155: an annual / interim disclosure must not be labelled as a quarter.

``earnings`` rows carry ``report_type`` — ``Q`` for a quarterly release, ``H``
for an interim disclosure, ``A`` for annual results, straight from Longbridge's
``qf`` / ``saf`` / ``af``.  The consensus stored on an ``A`` row is the *whole
year's* (0700.HK 2026 年报: EPS 28.88, revenue 969.12B) while the same period's
``earnings_consensus`` row holds the quarter's (EPS 7.13, revenue 249.81B).
Every label, however, was built from ``(fiscal_year, fiscal_quarter)`` alone, so
the calendar, the detail panel, the watchlist column, the history table and the
iCal feed all printed "26Q4" over a year's number.

The checks below pin the three outcomes the label must produce — annual,
interim, quarterly — on both sides: the served helper under node, and the feed
the calendar app downloads.
"""
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest import TestCase, skipUnless

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for path in (ROOT, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.ical import generate_ical  # noqa: E402
import frontend_contract as fc  # noqa: E402  (needs the path above)

#: 0700.HK's FY2026 annual results event exactly as production stores it: the
#: provider's whole-year consensus on a row whose quarter is the 4th.
ANNUAL = {
    "symbol": "0700.HK", "market": "HK",
    "company_name": "腾讯控股", "company_name_en": "Tencent Holdings",
    "report_date": date(2027, 3, 17), "before_after": "after",
    "report_type": "A", "fiscal_year": 2026, "fiscal_quarter": 4,
    "date_status": "scheduled", "is_predicted": False, "date_source": "longbridge",
    "eps_estimate": 28.880084, "revenue_estimate": 969121727710.4298,
    "estimate_currency": "HKD",
}

#: A quarterly row of the same symbol, used as the "nothing changed" control.
QUARTERLY = {
    "symbol": "0700.HK", "market": "HK",
    "company_name": "腾讯控股", "company_name_en": "Tencent Holdings",
    "report_date": date(2026, 11, 18), "before_after": "after",
    "report_type": "Q", "fiscal_year": 2026, "fiscal_quarter": 3,
    "date_status": "reported", "is_predicted": False, "date_source": "futu",
    "eps_estimate": 7.371337, "eps_actual": 7.059,
    "revenue_estimate": 247536819838.46436, "revenue_actual": 207336000000.0,
    "estimate_currency": "HKD", "actual_currency": "HKD",
}


def _feed(row: dict, title_lang: str = "zh") -> str:
    # Unfold first: RFC 5545 splits long content lines, and a naive ``in`` test
    # misses a value that straddles a fold.
    return generate_ical([dict(row)], title_lang=title_lang).replace("\r\n ", "")


def _summary(ics: str) -> str:
    return next(line for line in ics.splitlines() if line.startswith("SUMMARY:"))


class FeedPeriodWordTests(TestCase):
    def test_an_annual_row_is_not_summarised_as_a_quarter(self):
        ics = _feed(ANNUAL)
        summary = _summary(ics)
        self.assertIn("0700.HK 腾讯控股 年报", summary)
        self.assertNotIn("Q4", summary)

    def test_the_annual_body_names_the_year_not_a_quarter(self):
        ics = _feed(ANNUAL)
        self.assertIn("财季：FY2026 年报", ics)
        self.assertNotIn("财季：FY2026 Q4", ics)
        # The figures themselves are untouched: the label changed, not the data.
        self.assertIn("969.12B", ics)
        self.assertIn("28.88", ics)

    def test_an_interim_row_says_interim(self):
        ics = _feed({**ANNUAL, "report_type": "H", "fiscal_quarter": 2})
        self.assertIn("中报", _summary(ics))
        self.assertIn("财季：FY2026 中报", ics)

    def test_a_quarterly_row_keeps_its_quarter_label(self):
        ics = _feed(QUARTERLY)
        self.assertIn("0700.HK 腾讯控股 Q3 财报", _summary(ics))
        self.assertIn("财季：FY2026 Q3", ics)

    def test_the_english_feed_reads_annual_earnings(self):
        ics = _feed({**ANNUAL, "company_name_en": "Tencent Holdings"}, title_lang="en")
        self.assertIn("Tencent Holdings Annual Earnings", _summary(ics))
        self.assertIn("Fiscal: FY2026 Annual", ics)


def _run_label_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useFormatters")
    probe = f"""
{source}
const fmt = useFormatters();
const annual = {{ fiscal_year: 2026, fiscal_quarter: 4, report_type: 'A' }};
const interim = {{ fiscal_year: 2027, fiscal_quarter: 2, report_type: 'H' }};
const quarterly = {{ fiscal_year: 2026, fiscal_quarter: 4, report_type: 'Q' }};
console.log(JSON.stringify({{
  annual: fmt.fqLabel(annual.fiscal_year, annual.fiscal_quarter, annual.report_type),
  interim: fmt.fqLabel(interim.fiscal_year, interim.fiscal_quarter, interim.report_type),
  quarterly: fmt.fqLabel(quarterly.fiscal_year, quarterly.fiscal_quarter, quarterly.report_type),
  legacy_signature: fmt.fqLabel(2026, 4),
  annual_period: fmt.periodLabel({{ ...annual, report_date: '2027-03-17' }}),
  quarterly_period: fmt.periodLabel({{ ...quarterly, report_date: '2026-11-02' }}),
}}));
"""
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class ServedLabelTests(TestCase):
    def test_the_label_follows_the_disclosure_type(self):
        out = _run_label_probe()
        self.assertEqual(out["annual"], "26年报")
        self.assertEqual(out["interim"], "27中报")
        self.assertEqual(out["quarterly"], "26Q4")

    def test_a_caller_without_a_report_type_still_gets_a_quarter(self):
        self.assertEqual(_run_label_probe()["legacy_signature"], "26Q4")

    def test_the_watchlist_period_label_uses_the_same_rule(self):
        out = _run_label_probe()
        self.assertEqual(out["annual_period"], "26年报")
        self.assertEqual(out["quarterly_period"], "26Q4")


def test_every_surface_passes_the_report_type_to_the_label():
    """A single un-migrated call site is enough to print a year as a quarter."""
    html = fc.read_index()
    for expected in (
        "fqLabel(e.fiscal_year, e.fiscal_quarter, e.report_type)",          # calendar badge + day list
        "fqLabel(selectedEarning.fiscal_year, selectedEarning.fiscal_quarter, selectedEarning.report_type)",
        "fqLabel(row.fiscal_year, row.fiscal_quarter, row.report_type)",    # history table
    ):
        assert expected in html, expected
