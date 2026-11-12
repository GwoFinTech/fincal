"""Issue #133: a 「本周财报」 digest at the top of the calendar.

The calendar already holds the next seven days' events (the default view loads
the displayed month ±1) and already knows whether a symbol is the reader's own.
What was missing was a view that answers 「这周谁要发」 without scanning 30+ cells.

Constraints this file defends:

* the digest is a second *view* of the loaded batch — no request of its own, so
  it cannot disagree with the grid;
* the scope is the reader's own symbols when the account has any, otherwise the
  whole system range (the issue's 「无自选则列全部」);
* an empty digest outside the loaded window says 「不在当前视图」, never
  「本周无财报」: the batch is the *displayed* month ±1, and paging the calendar
  away from today empties it without meaning anything about the week;
* a season peak carries 700+ events on one date, so each day is capped and the
  exact remainder is offered as a drill-down into the day panel.

The grouping runs under node against the served `weekAheadGroups()`, so these
cases exercise the shipped function instead of a copy of its rules.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import TestCase, skipUnless

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)

TODAY = "2026-11-13"          # a Friday
HORIZON_END = "2026-11-19"

ROWS = [
    {"id": 1, "symbol": "AAPL", "market": "US", "company_name": "Apple",
     "report_date": "2026-11-13", "before_after": "after", "eps_estimate": 1.5,
     "fiscal_year": 2026, "fiscal_quarter": 4},
    {"id": 2, "symbol": "0388.HK", "market": "HK", "company_name": "HKEX",
     "report_date": "2026-11-13", "before_after": "before", "eps_estimate": 3.88864,
     "fiscal_year": 2026, "fiscal_quarter": 3},
    {"id": 3, "symbol": "0291.HK", "market": "HK", "company_name": "CHINA RES BEER",
     "report_date": "2026-11-13", "before_after": None, "eps_estimate": None,
     "fiscal_year": 2026, "fiscal_quarter": 3},
    {"id": 4, "symbol": "MSFT", "market": "US", "company_name": "Microsoft",
     "report_date": "2026-11-16", "before_after": "after", "eps_estimate": 3.1,
     "fiscal_year": 2027, "fiscal_quarter": 1},
    {"id": 5, "symbol": "NVDA", "market": "US", "company_name": "NVIDIA",
     "report_date": "2026-11-19", "before_after": "after", "eps_estimate": 1.2,
     "fiscal_year": 2027, "fiscal_quarter": 3},
    # Outside the seven-day horizon on both sides: the digest must not claim them.
    {"id": 6, "symbol": "OLD", "market": "US", "company_name": "Yesterday",
     "report_date": "2026-11-12", "before_after": "after", "eps_estimate": 1.0,
     "fiscal_year": 2026, "fiscal_quarter": 3},
    {"id": 7, "symbol": "LATE", "market": "US", "company_name": "Next week",
     "report_date": "2026-11-20", "before_after": "before", "eps_estimate": 1.0,
     "fiscal_year": 2026, "fiscal_quarter": 3},
]

PROBE = r"""
const fs = require('fs');
const js = fs.readFileSync(process.env.APP_JS, 'utf8');
const pick = (re) => { const m = js.match(re); if (!m) { throw new Error('missing: ' + re); } return m[0]; };
const source = [
  pick(/^ *const WEEK_AHEAD_MAX_PER_DAY = \d+;$/m),
  pick(/^ *const WEEKDAY_LABELS = \[[^\]]*\];$/m),
  pick(/^ *const BEFORE_AFTER_RANK = \{[^}]*\};$/m),
  pick(/^ *function monthDayLabel\([\s\S]*?\n  \}$/m),
  pick(/^ *function monthDayWeekday\([\s\S]*?\n  \}$/m),
  pick(/^ *function weekAheadGroups\([\s\S]*?\n  \}$/m),
].join('\n');
const { weekAheadGroups } = new Function(source + '\nreturn { weekAheadGroups };')();

const rows = JSON.parse(process.env.ROWS);
const groups = (opts) => weekAheadGroups(
  opts.rows === undefined ? rows : opts.rows,
  opts.today || '2026-11-13',
  opts.end || '2026-11-19',
  opts.isMine === undefined ? null : opts.isMine,
  opts.grid === undefined ? new Set() : new Set(opts.grid),
);

// Seven events on one date: the cap plus the exact remainder.
const crowded = [];
for (let i = 0; i < 7; i++) {
  crowded.push({ id: 100 + i, symbol: 'S' + i, market: 'US', report_date: '2026-11-17',
                 before_after: i % 2 ? 'before' : 'after', eps_estimate: i });
}

const out = {
  full: groups({}),
  mine: groups({ isMine: (e) => e.symbol === 'AAPL' }),
  empty: groups({ rows: [] }),
  crowded: groups({ rows: crowded, today: '2026-11-13', end: '2026-11-19' }),
  grid: groups({ grid: ['2026-11-13'] }),
  outside: groups({ rows: rows.filter(r => r.report_date > '2026-11-19') }),
};
console.log(JSON.stringify(out));
"""


def _probe() -> dict:
    completed = subprocess.run(
        ["node", "-e", PROBE],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "APP_JS": str(fc.APP_JS), "ROWS": json.dumps(ROWS)},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class WeekAheadGroupingTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _probe()

    def test_only_the_next_seven_days_are_included(self):
        dates = [g["date"] for g in self.out["full"]]
        self.assertEqual(dates, ["2026-11-13", "2026-11-16", "2026-11-19"])
        symbols = [r["symbol"] for g in self.out["full"] for r in g["rows"]]
        self.assertNotIn("OLD", symbols, "yesterday is not 「本周」")
        self.assertNotIn("LATE", symbols, "the eighth day is outside the horizon")
        self.assertEqual(self.out["outside"], [], "no rows in the horizon → no groups")

    def test_the_heading_carries_the_local_weekday(self):
        self.assertEqual(self.out["full"][0]["label"], "11/13 周五")
        self.assertEqual(self.out["full"][1]["label"], "11/16 周一")

    def test_a_trading_day_reads_premarket_first(self):
        first = self.out["full"][0]
        self.assertEqual([r["symbol"] for r in first["rows"]], ["0388.HK", "0291.HK", "AAPL"])

    def test_the_scope_follows_the_account_watchlist(self):
        mine = self.out["mine"]
        self.assertEqual([g["date"] for g in mine], ["2026-11-13"])
        self.assertEqual([r["symbol"] for r in mine[0]["rows"]], ["AAPL"])

    def test_a_crowded_day_is_capped_and_the_remainder_is_exact(self):
        group = self.out["crowded"][0]
        self.assertEqual(group["total"], 7)
        self.assertEqual(len(group["rows"]), 5, "the digest must stay a summary")
        self.assertEqual(group["more"], 2, "the remainder has to be the real count")

    def test_out_of_grid_dates_are_marked_so_no_drill_down_is_offered(self):
        by_date = {g["date"]: g for g in self.out["grid"]}
        self.assertTrue(by_date["2026-11-13"]["inGrid"])
        self.assertFalse(by_date["2026-11-16"]["inGrid"])
        self.assertTrue(all(not g["inGrid"] for g in self.out["full"]))

    def test_an_empty_week_has_no_groups(self):
        self.assertEqual(self.out["empty"], [])


class DigestSourcePins(TestCase):
    def _js(self) -> str:
        return fc.read_app_js()

    def _computed(self) -> str:
        js = self._js()
        block = js[js.index("const weekAhead = computed(() => {"):]
        return block[:block.index("      });")]

    def _template(self) -> str:
        html = fc.read_index()
        start = html.index('<section class="surface mb-2" aria-label="本周财报">')
        return html[start:html.index("<!-- Calendar grid -->")]

    def test_the_digest_reuses_the_calendar_batch_instead_of_fetching(self):
        body = self._computed()
        self.assertIn("earnings.value", body, "the rows must come from the loaded batch")
        self.assertIn("weekAheadGroups(", body)
        self.assertNotIn("apiFetch", body)
        self.assertNotIn("fetch(", body)
        self.assertNotIn("/api/earnings", self._template())

    def test_the_scope_is_the_watchlist_when_there_is_one(self):
        body = self._computed()
        self.assertIn("const mineOnly = wl.watchlist.value.length > 0;", body)
        self.assertIn("mineOnly ? wl.isMine : null", body)

    def test_an_uncovered_window_is_reported_as_such(self):
        body = self._computed()
        self.assertIn("const covered = range.start <= today && range.end >= horizonEnd;", body)
        template = self._template()
        self.assertIn("不在当前视图", template)
        self.assertIn("本周无财报", template)
        # The page-level banner and the digest's own failure state are distinct.
        self.assertIn("本周财报读取失败", template)

    def test_the_window_helper_is_shared_with_the_request(self):
        js = self._js()
        loader = js[js.index("async function loadEarnings()"):]
        loader = loader[:loader.index("} finally {")]
        self.assertIn("calendarWindowRange(cal.currentDate.value)", loader), (
            "the digest's coverage check and the request window must not drift"
        )
        helper = fc.function_source(js, "calendarWindowRange")
        self.assertIn("d.getMonth() - 1, 1", helper)
        self.assertIn("d.getMonth() + 2, 0", helper)

    def test_it_starts_collapsed_and_sits_above_the_grid(self):
        self.assertIn("const weekAheadOpen = ref(false);", self._js())
        html = fc.read_index()
        self.assertLess(
            html.index('aria-label="本周财报"'), html.index('aria-label="财报日历"'),
            "the digest is a summary of the grid below it, not a footer",
        )
        template = self._template()
        self.assertIn('v-show="weekAheadOpen"', template)
        self.assertIn(":aria-expanded=\"weekAheadOpen ? 'true' : 'false'\"", template)

    def test_rows_open_the_existing_detail_panel(self):
        template = self._template()
        js = self._js()
        self.assertIn("@click=\"selectWeekEarning(row)\"", template)
        self.assertIn("fmtNum(row.eps_estimate)", template)
        self.assertIn("periodLabel(row)", template, "reuse the existing period label")
        self.assertIn("cal.selectEarning(e, cell, apiFetch);", js)
        # …and the remainder selects the day whose list already renders in full.
        self.assertIn("@click=\"selectWeekDay(group.date)\"", template)
        self.assertIn("if (cell) cal.selectCell(cell);", js)

    def test_the_bindings_are_exposed_to_the_runtime_template(self):
        exposed = fc.exposed_setup_keys(self._js())
        for key in ("weekAhead", "weekAheadOpen", "selectWeekEarning", "selectWeekDay"):
            self.assertIn(key, exposed)
        self.assertEqual(fc.dangling_bindings(fc.read_index(), exposed), set())

    def test_the_row_truncates_instead_of_pushing_the_figure_off_a_390px_screen(self):
        html = fc.read_index()
        self.assertIn(".week-row .week-company { max-width: 150px; }", html)
        self.assertIn("@media (max-width: 420px) { .week-row .week-company { max-width: 96px; } }", html)
        self.assertIn(".week-eps { width: 52px; text-align: right; flex-shrink: 0; }", html)
        template = self._template()
        self.assertIn('class="week-company text-[11px] truncate"', template)
