"""Issue #120: one formatter, one decimal policy — and it must fit an EPS.

Every EPS figure in the panel went through ``fmtNum``, which was a single
``toFixed(2)``.  Two decimals do not exist below five mills: production
rendered ``NET``'s estimate ``-0.00263`` as ``-0.00`` and ``2068.HK``'s
``-0.0038`` the same way, in the same row where 「较预期」 was computed from the
*real* denominator (``-3702.3%``).  The reader saw a zero that was used as a
nonzero, and in a year of data 17 rows had ``0 < |EPS| < 0.02``.

The assertion runs the served ``useFormatters()`` under node rather than
restating the formula here: a Python copy of ``fmtNum`` would keep passing long
after the bundle changed.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import TestCase, skipUnless

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)

#: Values taken from the production rows named in the issue, beside the
#: ordinary magnitudes the same formatter must not move.
PROBE_VALUES = {
    "net_estimate": -0.00263,
    "detail_estimate": -0.0038,
    "hk_small": -0.015096,
    "crwd_upcoming": 0.01733,
    "ktos_estimate": -0.0058,
    "zero": 0,
    "null": None,
    "plain": 1.99,
    "rounded": 4.38908,
    "wide": 12.1701,
    "tenth": 0.1,
}


def _run_fmt_num_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useFormatters")
    literals = json.dumps(PROBE_VALUES)
    probe = f"""
{source}
const fmt = useFormatters();
const values = {literals};
const out = {{}};
for (const [key, value] of Object.entries(values)) out[key] = fmt.fmtNum(value);
console.log(JSON.stringify(out));
"""
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class SmallEpsPrecisionTests(TestCase):
    def test_a_sub_cent_eps_keeps_its_significant_digits(self):
        out = _run_fmt_num_probe()
        self.assertEqual(out["net_estimate"], "-0.00263")
        self.assertEqual(out["detail_estimate"], "-0.00380")
        self.assertEqual(out["hk_small"], "-0.0151")
        self.assertEqual(out["crwd_upcoming"], "0.0173")
        self.assertEqual(out["ktos_estimate"], "-0.00580")

    def test_ordinary_magnitudes_keep_two_decimals(self):
        out = _run_fmt_num_probe()
        self.assertEqual(out["plain"], "1.99")
        self.assertEqual(out["rounded"], "4.39")
        self.assertEqual(out["wide"], "12.17")
        self.assertEqual(out["tenth"], "0.10")

    def test_zero_stays_two_decimals_and_missing_stays_a_dash(self):
        out = _run_fmt_num_probe()
        self.assertEqual(out["zero"], "0.00")
        self.assertEqual(out["null"], "—")

    def test_the_rendered_denominator_is_never_the_string_the_surplus_contradicts(self):
        """The row's own two cells must agree: `-0.00` beside `-3702.3%` cannot."""
        out = _run_fmt_num_probe()
        self.assertNotEqual(out["net_estimate"].lstrip("-").strip("0."), "")
        self.assertIn("0.00263", out["net_estimate"])


def test_the_small_value_needs_no_more_width_than_the_percentage_it_sits_beside():
    """The table is `table-fixed`; the longest cell was already a percentage."""
    out = _run_fmt_num_probe() if shutil.which("node") else {"net_estimate": "-0.00263"}
    assert len(out["net_estimate"]) <= len("-3702.3%")
