"""Issue #130: `fmtBigNum` must keep a unit below 1e6, not print a bare number.

The formatter had tiers for T/B/M and then fell through to `toLocaleString()`,
so the revenue column rendered `113.35B` · `9.21M` · `1,234,567`. HK small caps
and small US tickers live in that last band: no unit, widest cell in the column,
and the reader has to count digits to learn the magnitude.

The probe runs the served `useFormatters()` under node, so the assertions are on
shipped code (and `fmtNum` — the price formatter from Issue #120 — is checked to
be untouched by this change).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import TestCase, skipUnless

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)

VENDOR_VUE = fc.ROOT / "app" / "static" / "assets" / "vendor" / "vue.global.prod.js"

PROBE = r"""
const fs = require('fs');
const vm = require('vm');

const sandbox = { console, setTimeout, clearTimeout };
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.env.VUE_PATH, 'utf8'), sandbox);
const { ref, computed } = sandbox.Vue;

/*USE_FORMATTERS*/

const f = useFormatters();
const values = {
  zero: 0,
  below_1k: 999,
  exactly_1k: 1000,
  hundreds_of_k: 123456,
  exactly_1m: 1e6,
  one_and_half_m: 1.5e6,
  billions: 113.35e9,
  trillions: 1e12,
  negative_k: -1234,
  small_negative: -999,
  missing: null,
  not_a_number: NaN,
  string_number: '2500',
};
const big = {};
for (const [k, v] of Object.entries(values)) big[k] = f.fmtBigNum(v);

console.log(JSON.stringify({
  big,
  prices: {
    tiny: f.fmtNum(-0.00263),
    normal: f.fmtNum(12.5),
    zero: f.fmtNum(0),
  },
}));
"""


def _run_formatter_probe() -> dict:
    probe = PROBE.replace("/*USE_FORMATTERS*/", fc.function_source(fc.read_app_js(), "useFormatters"))
    completed = subprocess.run(
        ["node", "-e", probe], capture_output=True, text=True, timeout=60,
        env={**os.environ, "VUE_PATH": str(VENDOR_VUE)},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class BigNumberTierTests(TestCase):
    def test_a_thousand_and_above_carries_a_unit(self):
        big = _run_formatter_probe()["big"]
        self.assertEqual(big["exactly_1k"], "1.0K")
        self.assertEqual(big["hundreds_of_k"], "123.5K")

    def test_the_existing_tiers_keep_their_decimals(self):
        big = _run_formatter_probe()["big"]
        self.assertEqual(big["exactly_1m"], "1.0M")
        self.assertEqual(big["one_and_half_m"], "1.5M")
        self.assertEqual(big["billions"], "113.35B")
        self.assertEqual(big["trillions"], "1.00T")

    def test_below_a_thousand_and_missing_values_are_unchanged(self):
        big = _run_formatter_probe()["big"]
        self.assertEqual(big["zero"], "0")
        self.assertEqual(big["below_1k"], "999")
        self.assertEqual(big["missing"], "—")
        self.assertEqual(big["not_a_number"], "—", "a non-finite value is missing data, not the string NaN")

    def test_negatives_use_the_same_ladder(self):
        big = _run_formatter_probe()["big"]
        self.assertEqual(big["negative_k"], "-1.2K")
        self.assertEqual(big["small_negative"], "-999")

    def test_a_numeric_string_still_formats(self):
        # API payloads occasionally arrive as strings; the old code coerced too.
        self.assertEqual(_run_formatter_probe()["big"]["string_number"], "2.5K")

    def test_the_price_formatter_is_not_touched_by_this_change(self):
        prices = _run_formatter_probe()["prices"]
        self.assertEqual(prices["tiny"], "-0.00263", "Issue #120's precision rule must survive")
        self.assertEqual(prices["normal"], "12.50")
        self.assertEqual(prices["zero"], "0.00")
