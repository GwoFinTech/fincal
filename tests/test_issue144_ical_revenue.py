"""Issue #144: the subscription feed must print the revenue pair, not only EPS.

``generate_ical()`` built its DESCRIPTION from a fixed field list — company,
fiscal, timing, the prediction notice, the date source, ``EPS Est``,
``EPS Actual``, the comparability note and the link.  Revenue was never in that
list, while the site's detail panel prints 营收 and EPS as the two rows of one
comparison table.

The magnitude is the reason this is a small rule instead of one more
interpolation: revenue is market-cap sized, so the raw column would have printed

    营收预期：108976833110.0 (USD)

next to the panel's ``108.98B``.  ``_display_big_amount`` is the Python side of
the served ``useFormatters().fmtBigNum``; the parity test below runs the *served*
JS under node (the technique ``test_issue120_small_eps_precision.py`` uses), so
the two ladders cannot drift apart with only one side updated.

Covered here:

* the four production values from the issue render to the panel's text;
* the ladder covers T/B/M/K and keeps a sub-1e3 value as stored;
* ``lang=zh`` / ``lang=en`` labels carry the value and its currency;
* an unreleased actual (#111) publishes no 营收实际 line and stays TENTATIVE;
* a row without revenue keeps its DESCRIPTION byte-identical (no blank line).
"""
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest import TestCase, skipUnless

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import icalendar  # noqa: E402

from app.ical import _display_big_amount, generate_ical  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)

#: The rows named in the issue, beside the ordinary magnitudes the same ladder
#: has to leave alone (and the sub-1e3 case the risk section pins).
PRODUCTION_VALUES = {
    "nvda": 108976833310.0,
    "tencent": 247536819838.46436,
    "xiaomi": 124831138146.40434,
    "small_hk": 3879266481.490122,
}


def _row(**overrides) -> dict:
    row = {
        "symbol": "NVDA", "market": "US",
        "company_name": "英伟达", "company_name_en": "NVIDIA",
        "report_date": date(2026, 11, 17), "before_after": "before",
        "fiscal_year": 2026, "fiscal_quarter": 3,
        "date_status": "reported", "is_predicted": False,
        "date_source": "longbridge",
        "eps_estimate": 2.49676, "eps_actual": 2.7,
        "revenue_estimate": 108976833110.0, "revenue_actual": 110500000000.0,
        "estimate_currency": "USD", "actual_currency": "USD",
    }
    row.update(overrides)
    return row


def _unfold(ics: str) -> str:
    return ics.replace("\r\n ", "")


def _body(ics: str) -> str:
    for line in _unfold(ics).split("\r\n"):
        if line.startswith("DESCRIPTION:"):
            raw = line[len("DESCRIPTION:"):]
            return raw.replace("\\n", "\n").replace("\\;", ";").replace("\\,", ",")
    raise AssertionError("no DESCRIPTION in feed")


def _lines(ics: str) -> list[str]:
    return [line for line in _body(ics).split("\n") if line]


class TestBigAmountLadder(TestCase):
    def test_production_values_match_the_panel(self):
        """Acceptance 1: the four rows from the issue render as the site shows them."""
        self.assertEqual(_display_big_amount(PRODUCTION_VALUES["nvda"]), "108.98B")
        self.assertEqual(_display_big_amount(PRODUCTION_VALUES["tencent"]), "247.54B")
        self.assertEqual(_display_big_amount(PRODUCTION_VALUES["xiaomi"]), "124.83B")
        self.assertEqual(_display_big_amount(PRODUCTION_VALUES["small_hk"]), "3.88B")

    def test_ladder_units(self):
        self.assertEqual(_display_big_amount(1.2345e12), "1.23T")
        self.assertEqual(_display_big_amount(2.3456e6), "2.3M")
        self.assertEqual(_display_big_amount(2.3456e3), "2.3K")

    def test_sub_thousand_value_is_kept_as_stored(self):
        """Risk section: a value below 1e3 must not be forced into a K unit."""
        self.assertEqual(_display_big_amount(999), "999")
        self.assertEqual(_display_big_amount(12.5), "12.5")

    def test_missing_values_render_empty(self):
        for value in (None, ""):
            self.assertEqual(_display_big_amount(value), "")

    def test_negative_growth_keeps_its_sign(self):
        self.assertEqual(_display_big_amount(-3879266481.490122), "-3.88B")


@skipUnless(shutil.which("node"), "node is required for the JS parity check")
class TestParityWithServedFormatter(TestCase):
    """The served ``fmtBigNum`` is the rule; a Python copy must agree with it."""

    def test_matches_node_formatter(self):
        source = fc.function_source(fc.read_app_js(), "useFormatters")
        probe = f"""
{source}
const fmt = useFormatters();
const values = {json.dumps(list(PRODUCTION_VALUES.values()) + [1.2345e12, 2.3456e6, 2.3456e3, 999])};
console.log(JSON.stringify(values.map((v) => fmt.fmtBigNum(v))));
"""
        completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True,
                                   timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        served = json.loads(completed.stdout.strip().splitlines()[-1])
        values = list(PRODUCTION_VALUES.values()) + [1.2345e12, 2.3456e6, 2.3456e3, 999]
        self.assertEqual([_display_big_amount(v) for v in values], served)


class TestDescriptionRevenueLines(TestCase):
    def test_zh_labels_carry_value_and_currency(self):
        """Acceptance 2: ``营收预期：108.98B (USD)`` with the site's separators."""
        lines = _lines(generate_ical([_row()], title_lang="zh"))
        self.assertIn("营收预期：108.98B (USD)", lines)
        self.assertIn("营收实际：110.50B (USD)", lines)

    def test_en_labels_carry_value_and_currency(self):
        lines = _lines(generate_ical([_row()], title_lang="en"))
        self.assertIn("Revenue Est: 108.98B (USD)", lines)
        self.assertIn("Revenue Actual: 110.50B (USD)", lines)

    def test_revenue_precedes_eps_like_the_site_table(self):
        """The panel prints 营收 above EPS; the body keeps that order."""
        lines = _lines(generate_ical([_row()], title_lang="zh"))
        self.assertLess(lines.index("营收预期：108.98B (USD)"), lines.index("EPS 预期：2.50 (USD)"))
        self.assertLess(lines.index("营收实际：110.50B (USD)"), lines.index("EPS 实际：2.70 (USD)"))

    def test_undeclared_currency_is_named_per_language(self):
        zh = _lines(generate_ical(
            [_row(estimate_currency=None, actual_currency=None)], title_lang="zh"))
        self.assertIn("营收预期：108.98B (币种未知)", zh)

    def test_unreleased_actual_withholds_the_revenue_line(self):
        """Acceptance 3: #111 applies to revenue exactly as it does to EPS."""
        ics = generate_ical(
            [_row(report_date=date(2027, 2, 20), date_status="predicted")], title_lang="zh")
        lines = _lines(ics)
        self.assertTrue(any(line.startswith("营收预期：") for line in lines))
        self.assertFalse(any(line.startswith("营收实际：") for line in lines))
        self.assertIn("STATUS:TENTATIVE", ics)
        self.assertNotIn("EPS 实际", "\n".join(lines))

    def test_row_without_revenue_is_byte_identical(self):
        """Acceptance 4: no revenue means no new line and no blank line."""
        without = generate_ical([_row(revenue_estimate=None, revenue_actual=None)],
                                title_lang="zh")
        lines = _lines(without)
        self.assertFalse([line for line in lines if "营收" in line])
        self.assertEqual(len(lines), len([line for line in _body(without).split("\n")]))
        expected = [
            "公司：英伟达",
            "财季：FY2026 Q3",
            "时间：盘前",
            "日期来源：longbridge",
            "EPS 预期：2.50 (USD)",
            "EPS 实际：2.70 (USD)",
        ]
        self.assertEqual(lines[:6], expected)

    def test_feed_still_parses(self):
        ics = generate_ical([_row()], title_lang="zh")
        calendar = icalendar.Calendar.from_ical(ics)
        events = [c for c in calendar.walk() if c.name == "VEVENT"]
        self.assertEqual(len(events), 1)
        self.assertIn("营收预期", str(events[0].get("DESCRIPTION")))
