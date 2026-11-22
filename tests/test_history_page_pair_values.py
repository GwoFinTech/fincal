"""The history page must print the same numbers it computes from.

Issue #147/#148 taught every *detail-panel* reader to take the provider's own
same-currency pair when one exists, but the 标的 history table (Issue #134)
kept interpolating the raw row fields while its 「较预期」 cell went through the
pair-aware `epsSurplus()`.  One row therefore contradicted itself: on
0700.HK 26Q3 it printed `EPS 预期 7.37 / EPS 实际 7.06` (Longbridge's HKD
estimate beside the CNY statement actual — a 4.2% *miss*) and `+10.5%` beside
them (the CNY pair 6.2873 → 6.947 — a beat).  The detail panel shows the same
quarter as `6.29 CNY → 6.95 CNY, +10.5%`; the page below it has to agree.

The probe evaluates the exact template expressions the row carries, in the
served `useFormatters()` scope, so a row that goes back to `row.eps_estimate`
both fails the markup check and prints `7.37` instead of `6.29`.
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

# 0700.HK 26Q3 as the API returns it: Longbridge's estimate is HKD, the Futu
# statement actual is CNY, and the provider's own pair is CNY on both sides.
ROW_0700 = {
    "symbol": "0700.HK",
    "market": "HK",
    "report_date": "2026-11-18",
    "eps_estimate": 7.371337,
    "eps_actual": 7.059,
    "revenue_estimate": 247536819838.46436,
    "revenue_actual": 207336000000.0,
    "estimate_currency": "HKD",
    "actual_currency": "CNY",
    "comparison_unavailable_reason": "currency_mismatch",
    "eps_pair_estimate": 6.2873,
    "eps_pair_actual": 6.947,
    "eps_pair_currency": "CNY",
    "eps_pair_analysts": 7,
    "eps_pair_source": "futu",
    "revenue_pair_estimate": 210239931880.0,
    "revenue_pair_actual": 207336000000.0,
    "revenue_pair_currency": "CNY",
    "revenue_pair_analysts": 22,
    "revenue_pair_source": "futu",
}

# A row whose comparison is genuinely impossible: no pair, currencies differ.
ROW_BLOCKED = {
    "symbol": "X",
    "market": "US",
    "eps_estimate": 1.0,
    "eps_actual": 2.0,
    "estimate_currency": "USD",
    "actual_currency": "CNY",
    "comparison_unavailable_reason": "currency_mismatch",
}

# 0700.HK 26Q2: same currency on both sides, so no pair is adopted and the row's
# own columns are what the table prints.
ROW_PLAIN = {
    "symbol": "0700.HK",
    "market": "HK",
    "eps_estimate": 7.217469,
    "eps_actual": 7.050358,
    "estimate_currency": "HKD",
    "actual_currency": "HKD",
}

# The expressions the row's six cells are required to carry, in table order.
CELL_EXPRESSIONS = (
    "fmtNum(epsEstimate(row))",
    "fmtNum(epsActual(row))",
    "signedPct(epsSurplus(row))",
    "fmtBigNum(revenueEstimate(row))",
    "fmtBigNum(revenueActual(row))",
)

PROBE = r"""
const fs = require('fs');
const vm = require('vm');

const sandbox = { console, setTimeout, clearTimeout };
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.env.VUE_PATH, 'utf8'), sandbox);

/*USE_FORMATTERS*/

const f = useFormatters();
const row = JSON.parse(process.env.ROW);
const exprs = JSON.parse(process.env.EXPRS);
const out = {};
for (const e of exprs) {
  try { out[e] = Function('f', 'row',
    'const {' + Object.keys(f).join(',') + '} = f; return (' + e + ');')(f, row); }
  catch (err) { out[e] = 'ERR:' + err.message; }
}
console.log(JSON.stringify({
  cells: out,
  row_only_surplus: (row.eps_actual - row.eps_estimate) / Math.abs(row.eps_estimate),
  blocked_surplus: f.epsSurplus(JSON.parse(process.env.BLOCKED)),
  currencies: {
  pair_row: f.rowCurrencyLabel(row),
  plain_row: f.rowCurrencyLabel(JSON.parse(process.env.PLAIN)),
  blocked_row: f.rowCurrencyLabel(JSON.parse(process.env.BLOCKED)),
  none_known: f.rowCurrencyLabel({ estimate_currency: 'unknown' }),
  estimate_only: f.rowCurrencyLabel({ estimate_currency: 'HKD' }),
  actual_only: f.rowCurrencyLabel({ actual_currency: 'CNY' }),
  },
}));
"""


def _run_probe() -> dict:
    probe = PROBE.replace("/*USE_FORMATTERS*/", fc.function_source(fc.read_app_js(), "useFormatters"))
    completed = subprocess.run(
        ["node", "-e", probe], capture_output=True, text=True, timeout=60,
        env={**os.environ, "VUE_PATH": str(VENDOR_VUE),
             "ROW": json.dumps(ROW_0700), "BLOCKED": json.dumps(ROW_BLOCKED),
             "PLAIN": json.dumps(ROW_PLAIN),
             "EXPRS": json.dumps(list(CELL_EXPRESSIONS))},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _history_row_markup() -> str:
    """The `<tr>` the history table renders per period, template source included."""
    html = fc.INDEX_HTML.read_text(encoding="utf-8")
    start = html.index('v-for="row in historyPeriods"')
    return html[start:html.index("</tr>", start)]


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class TheRowPrintsThePair(TestCase):
    def test_the_five_cells_render_the_pair_values(self):
        cells = _run_probe()["cells"]
        self.assertEqual(cells["fmtNum(epsEstimate(row))"], "6.29")
        self.assertEqual(cells["fmtNum(epsActual(row))"], "6.95")
        self.assertEqual(cells["fmtBigNum(revenueEstimate(row))"], "210.24B")
        self.assertEqual(cells["fmtBigNum(revenueActual(row))"], "207.34B")

    def test_the_surplus_matches_the_numbers_beside_it(self):
        got = _run_probe()
        self.assertAlmostEqual(got["row_only_surplus"], (7.059 - 7.371337) / 7.371337, places=9)
        cells = got["cells"]
        self.assertGreater(cells["signedPct(epsSurplus(row))"], "+0")
        # The row-level subtraction the old template sat beside has the other sign,
        # which is exactly the contradiction this pins shut.
        self.assertIn("+10.5", cells["signedPct(epsSurplus(row))"])
        self.assertLess(got["row_only_surplus"], 0)

    def test_a_row_with_no_pair_still_refuses(self):
        self.assertIsNone(_run_probe()["blocked_surplus"])

    def test_every_row_states_the_currency_its_figures_are_in(self):
        currencies = _run_probe()["currencies"]
        # The pair's currency when the pair is what the cells show…
        self.assertEqual(currencies["pair_row"], "CNY")
        # …the row's own column currency for a period with no pair…
        self.assertEqual(currencies["plain_row"], "HKD")
        # …and both sides when they disagree, so nobody subtracts across them.
        self.assertEqual(currencies["blocked_row"], "USD → CNY")
        # `unknown` is a placeholder, not a currency: print nothing.
        self.assertEqual(currencies["none_known"], "")
        # An arrow claims two currencies were in play. A forecast with no actual (and a
        # quarter with no consensus) has only one — production read `HKD → —` and
        # `— → CNY`, which suggests a conversion that never happened.
        self.assertEqual(currencies["estimate_only"], "HKD")
        self.assertEqual(currencies["actual_only"], "CNY")


class TheWiring(TestCase):
    def test_the_value_cells_use_the_pair_aware_accessors(self):
        row = _history_row_markup()
        for expr in CELL_EXPRESSIONS:
            self.assertIn("{{ " + expr + " }}", row, expr)
        # The raw fields may not appear beside a pair-aware surplus again.
        for raw in ("row.eps_estimate", "row.eps_actual",
                    "row.revenue_estimate", "row.revenue_actual"):
            self.assertNotIn(raw, row, raw)

    def test_the_currency_is_stated_once_per_row(self):
        row = _history_row_markup()
        self.assertIn('v-if="rowCurrencyLabel(row)"', row)
        self.assertIn("{{ rowCurrencyLabel(row) }}", row)
        # The four numeric cells stay bare numbers: a label on each one costs the
        # 390 px layout its last column (measured: 不可比原因 wrapped to 3 lines).
        for cell in ("fmtNum(epsEstimate(row))", "fmtNum(epsActual(row))",
                     "fmtBigNum(revenueEstimate(row))", "fmtBigNum(revenueActual(row))"):
            start = row.index("{{ " + cell + " }}")
            self.assertNotIn("ccy", row[start:start + 260], cell)

    def test_the_reason_column_only_speaks_when_the_cell_is_empty(self):
        row = _history_row_markup()
        self.assertIn("epsSurplus(row) == null ? (comparisonShortNote(row)", row)

    def test_the_footnote_mentions_the_pair_exception(self):
        """The disclosure must describe the rule the table now follows."""
        html = fc.INDEX_HTML.read_text(encoding="utf-8")
        start = html.index("标的财报历史")
        footnote = html[html.index("「较预期」只在预期与实际都标明币种且相同时计算", start):]
        footnote = footnote[:footnote.index("</p>")]
        self.assertIn("一对「一致预期 + 实际」", footnote)
        self.assertIn("币种", footnote)
