"""Issue #140: 月 / 周 must be part of the view, and the week must name itself.

#125 wrote 页签 / 月份 / 选中日期 into the address bar and #138 made 切「周」 land
on the selected day, but the *view mode* itself still lived in one Vue ref: the
toolbar label showed 「2026年11月」 above a 7-cell week, a refresh fell back to
月, 后退 had no 月↔周 step, and `?month=2026-11` was the only thing a colleague
could be sent. This module pins the three pieces that fix it:

* the served ``useUrlState()`` (parsed/serialized under node, so a formatting
  change cannot pass while a Python copy stays green),
* the served ``useCalendar()``'s ``calendarRangeLabel`` (same node harness as
  #138, so the label is computed from the shipped anchor rule), and
* the page wiring that reads/writes/restores the view.
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

URL_PROBE = r"""
/*USE_URL_STATE*/

// A fake window that behaves like the browser after each write: the address bar
// reflects what was just written, which is what makes a repeated push a no-op.
const calls = [];
const host = {
  history: {
    pushState: (state, title, url) => { calls.push(['push', url]); host.location.search = url.startsWith('?') ? url : ''; },
    replaceState: (state, title, url) => { calls.push(['replace', url]); host.location.search = url.startsWith('?') ? url : ''; },
  },
  location: { pathname: '/cal', search: '' },
};
const url = useUrlState(host);

const parsed = {
  week_deep_link: url.parseUrlState('?tab=calendar&view=week&date=2026-11-18'),
  month_explicit: url.parseUrlState('?view=month&month=2026-11'),
  illegal_view: url.parseUrlState('?tab=calendar&view=quarter&month=2026-11'),
  no_view: url.parseUrlState('?tab=calendar&month=2026-11&date=2026-11-18'),
  impossible_day: url.parseUrlState('?view=week&date=2026-02-31'),
  week_without_day: url.parseUrlState('?view=week&month=2026-11'),
};

const built = {
  week: url.buildUrlState({ tab: 'calendar', view: 'week', date: '2026-11-18' }),
  month_is_default: url.buildUrlState({ tab: 'calendar', view: 'month', month: '2026-11', date: '2026-11-18' }),
  illegal_view: url.buildUrlState({ tab: 'calendar', view: 'quarter', month: '2026-11' }),
  impossible_day: url.buildUrlState({ tab: 'calendar', view: 'week', date: '2026-02-31' }),
  week_cross_month: url.buildUrlState({ tab: 'calendar', view: 'week', month: '2026-12', date: '2026-12-02' }),
};

const days = {
  valid: url.dayDate('2026-11-18'),
  valid_out: (() => { const d = url.dayDate('2026-11-18'); return d ? [d.getFullYear(), d.getMonth(), d.getDate()] : null; })(),
  rejected: ['2026-02-31', '2026-13-01', '2026-11-32', '2026-00-10', '18/11/2026', '', null].map(v => url.dayDate(v) === null),
};

// ── push de-dup: one click, one history step ─────────────────────
const weekState = () => ({ tab: 'calendar', view: 'week', month: '2026-11', date: '2026-11-18' });
url.pushUrl(weekState());          // the click
const afterFirst = calls.length;
url.pushUrl(weekState());          // the second watcher for the same click
const afterDuplicate = calls.length;
url.pushUrl({ tab: 'calendar', view: 'week', month: '2026-11', date: '2026-11-20' });
const afterRealChange = calls.length;

console.log(JSON.stringify({
  views: url.VIEWS,
  parsed, built, days,
  dedupe: { after_first: afterFirst, after_duplicate: afterDuplicate, after_real_change: afterRealChange },
  calls,
}));
"""

CALENDAR_PROBE = r"""
const fs = require('fs');
const vm = require('vm');

const sandbox = { console, setTimeout, clearTimeout };
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.env.VUE_PATH, 'utf8'), sandbox);
const { ref, computed, nextTick } = sandbox.Vue;
const pad = (n) => String(n).padStart(2, '0');
const localYmd = (d) => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());

/*MONTH_DAY_LABEL*/

/*USE_CALENDAR*/

const earnings = ref([{ id: 1, report_date: '2026-11-18', symbol: 'NVDA', market: 'US' }]);

// A deep link sets the anchor and then the view — the order the page uses in
// `setup()`, not `setViewMode()` (which re-anchors on the 周 button's rule).
const deepLink = (anchor) => {
  const cal = useCalendar(earnings, null, ref(false));
  cal.currentDate.value = anchor;
  cal.viewMode.value = 'week';
  return cal;
};
const keys = (cal) => cal.calendarCells.value.map(c => localYmd(c.date));

const monthCal = useCalendar(earnings, null, ref(false));
monthCal.currentDate.value = new Date(2026, 10, 18);

const week = deepLink(new Date(2026, 10, 18));           // 2026-11-15 … 11-21
const crossMonth = deepLink(new Date(2026, 11, 2));       // 2026-11-29 … 12-05
const crossYear = deepLink(new Date(2027, 0, 1));         // 2026-12-27 … 2027-01-02
const weekCells = keys(week);
const labelOfCells = monthDayLabel(weekCells[0]) + ' → ' + monthDayLabel(weekCells[weekCells.length - 1]);

// Stepping a week must move the label with the grid, not just the month name.
const stepper = deepLink(new Date(2026, 10, 18));
const stepLabels = [stepper.calendarRangeLabel.value];
stepper.nextMonth();
stepLabels.push(stepper.calendarRangeLabel.value);
stepper.nextMonth();
stepLabels.push(stepper.calendarRangeLabel.value);
const steppedSpans = [[keys(stepper)[0], keys(stepper)[6]]];

// 周 → 月 goes back to the month wording.
const backToMonth = deepLink(new Date(2026, 10, 18));
backToMonth.setViewMode('month');
const backLabel = backToMonth.calendarRangeLabel.value;
const backMonthLabel = backToMonth.monthLabel.value;

console.log(JSON.stringify({
  month_label: monthCal.calendarRangeLabel.value,
  month_label_equals_month: monthCal.calendarRangeLabel.value === monthCal.monthLabel.value,
  week_label: week.calendarRangeLabel.value,
  week_cells: weekCells,
  week_cells_label: labelOfCells,
  cross_month_label: crossMonth.calendarRangeLabel.value,
  cross_month_cells: keys(crossMonth),
  cross_year_label: crossYear.calendarRangeLabel.value,
  cross_year_cells: keys(crossYear),
  step_labels: stepLabels,
  stepped_span: steppedSpans[0],
  back_label: backLabel,
  back_month_label: backMonthLabel,
}));
"""


def _run(probe: str, replacements: dict[str, str], env: dict | None = None) -> dict:
    for token, value in replacements.items():
        probe = probe.replace(token, value)
    completed = subprocess.run(
        ["node", "-e", probe],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, **(env or {})},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _run_url_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useUrlState")
    return _run(URL_PROBE, {"/*USE_URL_STATE*/": source})


def _run_calendar_probe() -> dict:
    js = fc.read_app_js()
    return _run(
        CALENDAR_PROBE,
        {
            "/*MONTH_DAY_LABEL*/": fc.function_source(js, "monthDayLabel"),
            "/*USE_CALENDAR*/": fc.function_source(js, "useCalendar"),
        },
        env={"VUE_PATH": str(VENDOR_VUE)},
    )


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class ViewUrlStateTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _run_url_probe()

    def test_a_week_link_is_readable_on_its_own(self):
        self.assertEqual(self.out["views"], ["month", "week"])
        self.assertEqual(self.out["parsed"]["week_deep_link"],
                         {"tab": "calendar", "view": "week", "month": "", "date": "2026-11-18"})
        self.assertEqual(self.out["parsed"]["week_without_day"],
                         {"tab": "", "view": "week", "month": "2026-11", "date": ""})

    def test_an_illegal_view_falls_back_to_month(self):
        self.assertNotIn("view", self.out["parsed"]["illegal_view"])
        self.assertEqual(self.out["built"]["illegal_view"], "?tab=calendar&month=2026-11")

    def test_an_impossible_day_never_anchors_a_week(self):
        """`2026-02-31` passes the regex but `new Date()` would roll it into March."""
        self.assertTrue(all(self.out["days"]["rejected"]), "不存在的日期必须回落")
        self.assertEqual(self.out["days"]["valid_out"], [2026, 10, 18])
        self.assertEqual(self.out["parsed"]["impossible_day"],
                         {"tab": "", "view": "week", "month": "", "date": ""})
        # …and it is not written back for the next reader to copy.
        self.assertEqual(self.out["built"]["impossible_day"], "?tab=calendar&view=week")

    def test_month_is_the_default_so_old_links_keep_their_bytes(self):
        self.assertEqual(self.out["built"]["month_is_default"],
                         "?tab=calendar&month=2026-11&date=2026-11-18")
        self.assertEqual(self.out["built"]["week"], "?tab=calendar&view=week&date=2026-11-18")
        # A cross-month week keeps its month: the link must rebuild the same window.
        self.assertEqual(self.out["built"]["week_cross_month"],
                         "?tab=calendar&view=week&month=2026-12&date=2026-12-02")

    def test_one_click_is_one_history_step(self):
        dedupe = self.out["dedupe"]
        self.assertEqual(dedupe["after_first"], 1)
        self.assertEqual(dedupe["after_duplicate"], 1, "与地址栏相同的 push 不是一步导航")
        self.assertEqual(dedupe["after_real_change"], 2)


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class RangeLabelTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _run_calendar_probe()

    def test_the_month_view_keeps_the_month_wording(self):
        self.assertTrue(self.out["month_label_equals_month"])
        self.assertTrue(self.out["month_label"].endswith("11月"))

    def test_the_week_view_names_its_first_and_last_day(self):
        self.assertEqual(len(self.out["week_cells"]), 7)
        self.assertEqual(self.out["week_label"], "11/15 → 11/21")
        # The label describes the grid on screen, not a second, hand-kept rule.
        self.assertEqual(self.out["week_label"], self.out["week_cells_label"])

    def test_a_cross_month_week_reads_as_a_range(self):
        """The bug's own example: 「2026年11月」 above cells that end in December."""
        self.assertEqual(self.out["cross_month_cells"][0], "2026-11-29")
        self.assertEqual(self.out["cross_month_cells"][-1], "2026-12-05")
        self.assertEqual(self.out["cross_month_label"], "11/29 → 12/5")

    def test_a_cross_year_week_reads_as_a_range(self):
        self.assertEqual(self.out["cross_year_cells"][0], "2026-12-27")
        self.assertEqual(self.out["cross_year_cells"][-1], "2027-01-02")
        self.assertEqual(self.out["cross_year_label"], "12/27 → 1/2")

    def test_stepping_a_week_moves_the_label_with_the_grid(self):
        self.assertEqual(self.out["step_labels"], ["11/15 → 11/21", "11/22 → 11/28", "11/29 → 12/5"])
        self.assertEqual(self.out["stepped_span"], ["2026-11-29", "2026-12-05"])

    def test_switching_back_to_month_restores_the_month_wording(self):
        self.assertEqual(self.out["back_label"], self.out["back_month_label"])


class ViewWiringTests(TestCase):
    def _js(self) -> str:
        return fc.read_app_js()

    def test_the_template_shows_the_range_label(self):
        html = fc.read_index()
        self.assertIn("{{ calendarRangeLabel }}", html)
        # The digest's 「当前日历视图（…）」 sentence still describes the month.
        self.assertIn("{{ monthLabel }}", html)

    def test_the_label_is_exposed_to_the_runtime_template(self):
        exposed = fc.exposed_setup_keys(self._js())
        self.assertIn("calendarRangeLabel", exposed)
        self.assertEqual(fc.dangling_bindings(fc.read_index(), exposed), set())

    def test_the_linked_view_is_applied_before_the_first_load(self):
        js = self._js()
        self.assertIn("if (urlInitial.view) cal.viewMode.value = urlInitial.view;", js)
        # Anchored by the linked day, so the request window is that day's month.
        self.assertIn("const initialDay = url.dayDate(urlInitial.date);", js)
        read = js.index("if (urlInitial.view) cal.viewMode.value = urlInitial.view;")
        first_load = js.index("await loadEarnings();", js.index("onMounted(async () => {"))
        self.assertLess(read, first_load)

    def test_the_view_is_written_and_read_back(self):
        js = self._js()
        pos = js.index("function currentUrlState()")
        state = js[pos:js.index("}", js.index("date: cal.selectedDay", pos))]
        self.assertIn("view: cal.viewMode.value,", state)
        # A view switch is a navigation step (push), not a click-through (replace).
        self.assertIn("watch(() => cal.viewMode.value, () => {", js)
        handler = js[js.index("watch(() => cal.viewMode.value, () => {"):]
        handler = handler[:handler.index("});")]
        self.assertIn("if (!restoringFromHistory) url.pushUrl(currentUrlState());", handler)

    def test_a_history_step_restores_the_view_and_the_anchor(self):
        js = self._js()
        handler = js[js.index("async function applyHistoryState()"):]
        handler = handler[:handler.index("window.addEventListener('popstate'")]
        for token in ("const view = state.view || 'month';",
                      "cal.viewMode.value = view;",
                      "const day = url.dayDate(state.date);",
                      "if (day) cal.currentDate.value = day;",
                      "cal.weekAnchorDate()"):
            self.assertIn(token, handler)
        # Muted writes while the address bar is being read.
        self.assertIn("restoringFromHistory = true;", handler)
        self.assertIn("restoringFromHistory = false", handler)

    def test_the_named_day_survives_the_restored_window(self):
        js = self._js()
        load = js[js.index("async function loadEarnings()"):]
        load = load[:load.index("// ── Address bar writes")]
        self.assertIn("if (pendingDateKey) applyDateKey(pendingDateKey);", load)
