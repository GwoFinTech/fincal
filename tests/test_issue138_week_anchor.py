"""Issue #138: the week view must contain the day the reader selected.

The month grid's anchor (`currentDate`) and the selection (`selectedDay`) were two
pieces of state that nothing ever reconciled: month navigation parks the anchor
on 当月 1 日, while `selectCell` only writes the selection. Once the reader had
pressed ‹ once, switching to 周 therefore rendered 1 日's week while the detail
panel still showed the day they had opened — two contradictory 「current dates」
on one screen, and the day they wanted was not even in the grid.

The probe runs the served ``useCalendar()`` under node with the vendored Vue
build, so the anchor resolution and the cell window are the shipped code. The
page-level wiring (selection survives a reload, the month key drives the load
and the address bar) is pinned from the served sources below.
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
const { ref, computed, nextTick } = sandbox.Vue;
const pad = (n) => String(n).padStart(2, '0');
const localYmd = (d) => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());

/*USE_CALENDAR*/

const earnings = ref([
  { id: 1, report_date: '2026-11-24', symbol: 'NVDA', market: 'US' },
  { id: 2, report_date: '2026-11-15', symbol: 'NET', market: 'US' },
]);

// Fresh calendar per case: the probe must not inherit an anchor or a selection.
const fresh = (anchor) => {
  const cal = useCalendar(earnings, null, ref(false));
  if (anchor) cal.currentDate.value = anchor;
  return cal;
};
const keys = (cal) => cal.calendarCells.value.map(c => localYmd(c.date));
const spans = (cal) => {
  const dates = keys(cal);
  return dates.length ? [dates[0], dates[dates.length - 1]] : [];
};
const cellFor = (cal, key) => cal.calendarCells.value.find(c => localYmd(c.date) === key) || null;
const selectedKey = (cal) => (cal.selectedDay.value ? localYmd(cal.selectedDay.value.date) : null);

// ── 路径一：深链 ?month=2026-11&date=2026-11-24 ─────────────────────
// The month branch parks the anchor on 11-01 and the link's day only reaches
// `selectedDay`, which is exactly the split state the issue describes.
const deepLink = fresh(new Date(2026, 10, 1));
deepLink.selectCell(cellFor(deepLink, '2026-11-24'));
const deepSelected = selectedKey(deepLink);
const monthSpans = spans(deepLink);
const monthFirstGrid = keys(deepLink);
deepLink.setViewMode('week');
const deepWeek = {
  cells: keys(deepLink),
  contains_selected: keys(deepLink).includes('2026-11-24'),
  selected_after: selectedKey(deepLink),
  selected_before: deepSelected,
  in_month_flags: deepLink.calendarCells.value.map(c => c.inMonth),
  month_spans_before: monthSpans,
  first_grid_before: monthFirstGrid.slice(0, 7),
  anchor: localYmd(deepLink.currentDate.value),
};

// ── 路径二：纯 UI（11-15 → ‹ 到 10 月 → › 回 11 月 → 点 15 日 → 周）──────
const uiPath = fresh(new Date(2026, 10, 15));
uiPath.prevMonth();                 // 10-01
uiPath.nextMonth();                 // 11-01
uiPath.selectCell(cellFor(uiPath, '2026-11-15'));
const uiMonthSpan = spans(uiPath);
uiPath.setViewMode('week');
const uiWeek = {
  cells: keys(uiPath),
  contains_selected: keys(uiPath).includes('2026-11-15'),
  selected_after: selectedKey(uiPath),
  anchor: localYmd(uiPath.currentDate.value),
  month_span_before: uiMonthSpan,
};

// ── 周视图的 ‹ / › 仍是 7 天一步，且跨月不丢锚点的星期 ───────────────
// 锚点取自选中日（而不是「今天」），所以这三步与真实日期无关。
const stepper = fresh(new Date(2026, 10, 1));
stepper.selectCell(cellFor(stepper, '2026-11-24'));
stepper.setViewMode('week');
const stepStart = spans(stepper);
stepper.nextMonth();
const nextWeek = { span: spans(stepper), anchor: localYmd(stepper.currentDate.value) };
stepper.nextMonth();
const crossMonth = { span: spans(stepper), anchor: localYmd(stepper.currentDate.value) };
stepper.prevMonth();
stepper.prevMonth();
const backHome = { span: spans(stepper), anchor: localYmd(stepper.currentDate.value) };

// ── 月视图未被改动 ────────────────────────────────────────────────
const monthOnly = fresh(new Date(2026, 10, 1));
const monthView = {
  cells: monthOnly.calendarCells.value.length,
  span: spans(monthOnly),
  first_weekday: monthOnly.calendarCells.value[0].date.getDay(),
  last_weekday: monthOnly.calendarCells.value[monthOnly.calendarCells.value.length - 1].date.getDay(),
  in_month: monthOnly.calendarCells.value.filter(c => c.inMonth).length,
  in_month_flags_all_true_in_week_view: null,
};
// Switching back to 月 from an anchored week keeps rendering that month.
const back = fresh(new Date(2026, 10, 1));
back.selectCell(cellFor(back, '2026-11-24'));
back.setViewMode('week');
back.setViewMode('month');
const monthAfterWeek = { cells: back.calendarCells.value.length, span: spans(back), selected: selectedKey(back) };

// ── 没有选中日时的回落：「当月里的今天」→「当前锚点」──────────────────
const todayKey = localYmd(new Date());
const now = new Date();
const noSelectionThisMonth = fresh(new Date(now.getFullYear(), now.getMonth(), 1));
noSelectionThisMonth.setViewMode('week');
const noSel = { cells: keys(noSelectionThisMonth), contains_today: keys(noSelectionThisMonth).includes(todayKey) };

// Browsing a month that is not today's: the week belongs to the month on screen,
// not to 「今天」.
const farMonth = fresh(new Date(2020, 2, 1));
farMonth.setViewMode('week');
const farSel = {
  cells: keys(farMonth),
  contains_anchor: keys(farMonth).includes('2020-03-01'),
  contains_today: keys(farMonth).includes(todayKey),
};

// An idempotent click (already in 周) must not move anything.
const again = fresh(new Date(2026, 10, 1));
again.selectCell(cellFor(again, '2026-11-24'));
again.setViewMode('week');
const anchorAfterFirst = localYmd(again.currentDate.value);
again.setViewMode('week');
const idempotent = localYmd(again.currentDate.value) === anchorAfterFirst;

// ── 跨月对齐不丢选中日（唯一会触发取数的路径）────────────────────────
// 选中 12-02（11 月网格里的灰格）后切「周」：锚点跨到 12 月，选中日必须还在。
const cross = fresh(new Date(2026, 10, 1));
const decemberGrey = cellFor(cross, '2026-12-02');
cross.selectCell(decemberGrey);
const crossBefore = selectedKey(cross);
cross.setViewMode('week');
const crossMonthSelect = {
  selected_before: crossBefore,
  selected_after: selectedKey(cross),
  contains_selected: keys(cross).includes('2026-12-02'),
  anchor: localYmd(cross.currentDate.value),
};

console.log(JSON.stringify({
  deep_week: deepWeek, ui_week: uiWeek, stepper: { step_start: stepStart, next: nextWeek, cross: crossMonth, back: backHome },
  month_view: monthView, month_after_week: monthAfterWeek, no_selection: noSel, far_month: farSel,
  idempotent, cross_month_select: crossMonthSelect,
}));
"""


def _run_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useCalendar")
    probe = PROBE.replace("/*USE_CALENDAR*/", source)
    completed = subprocess.run(
        ["node", "-e", probe],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "VUE_PATH": str(VENDOR_VUE)},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class WeekAnchorTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _run_probe()

    def test_a_deep_link_opens_the_week_of_its_day(self):
        week = self.out["deep_week"]
        self.assertEqual(len(week["cells"]), 7, "a week is seven cells")
        self.assertTrue(week["contains_selected"], "选中的 11-24 必须出现在周网格里")
        self.assertEqual(week["selected_before"], "2026-11-24")
        self.assertEqual(week["selected_after"], "2026-11-24", "切视图不改选中状态")
        self.assertEqual(week["anchor"], "2026-11-24")

    def test_the_plain_ui_order_also_lands_on_the_selected_day(self):
        """2026-11-15 首屏 → ‹ 到 10 月 → › 回 11 月 → 点 15 日 → 周。

        月视图导航是让两个日期分叉的最常见动作；这里复现的就是那条路径。
        """
        week = self.out["ui_week"]
        self.assertTrue(week["contains_selected"], "选中的 11-15 必须出现在周网格里")
        self.assertEqual(week["selected_after"], "2026-11-15")
        self.assertEqual(week["anchor"], "2026-11-15")
        # The month grid it came from was the bug's shape: it started on 11-01.
        self.assertEqual(week["month_span_before"][0], "2026-11-01")

    def test_every_week_cell_is_in_month(self):
        flags = self.out["deep_week"]["in_month_flags"]
        self.assertTrue(all(flags), "周视图没有「跨月灰格」，7 格都是本周")

    def test_the_week_view_still_steps_seven_days_and_crosses_months(self):
        stepper = self.out["stepper"]
        self.assertEqual(stepper["step_start"], ["2026-11-22", "2026-11-28"])
        self.assertEqual(stepper["next"]["span"], ["2026-11-29", "2026-12-05"])
        self.assertEqual(stepper["next"]["anchor"], "2026-12-01")
        self.assertEqual(stepper["cross"]["span"], ["2026-12-06", "2026-12-12"])
        self.assertEqual(stepper["back"]["span"], stepper["step_start"], "‹ ‹ 回到原周")

    def test_the_month_view_is_unchanged(self):
        month = self.out["month_view"]
        self.assertEqual(month["span"], ["2026-11-01", "2026-12-05"])
        self.assertEqual(month["first_weekday"], 0, "首格是周日")
        self.assertEqual(month["last_weekday"], 6, "末格是周六")
        self.assertEqual(month["cells"], 35)
        self.assertEqual(month["in_month"], 30, "11 月 30 天，其余 5 格是跨月灰格")

    def test_switching_back_to_month_keeps_the_selection_and_the_month(self):
        back = self.out["month_after_week"]
        self.assertEqual(back["span"], ["2026-11-01", "2026-12-05"])
        self.assertEqual(back["selected"], "2026-11-24")

    def test_without_a_selection_the_week_falls_back_to_todays_month(self):
        self.assertTrue(self.out["no_selection"]["contains_today"])

    def test_a_month_that_is_not_todays_week_stays_on_screen(self):
        """翻了几个月之后切「周」，「今天」不是用户正在看的那一周。"""
        far = self.out["far_month"]
        self.assertTrue(far["contains_anchor"])
        self.assertFalse(far["contains_today"])

    def test_clicking_周_twice_does_not_move_the_anchor(self):
        self.assertTrue(self.out["idempotent"])

    def test_a_cross_month_selection_survives_the_move(self):
        cross = self.out["cross_month_select"]
        self.assertEqual(cross["selected_before"], "2026-12-02")
        self.assertEqual(cross["selected_after"], "2026-12-02", "跨月对齐不得丢掉选中日")
        self.assertTrue(cross["contains_selected"])
        self.assertEqual(cross["anchor"], "2026-12-02")


class WeekAnchorWiringTests(TestCase):
    def _js(self) -> str:
        return fc.read_app_js()

    def _loader(self) -> str:
        js = self._js()
        load = js[js.index("async function loadEarnings()"):]
        return load[:load.index("// ── Address bar writes")]

    def test_the_loader_keeps_a_selection_that_is_still_in_the_grid(self):
        """A reload rebuilds the cells; it must not silently re-park the panel."""
        load = self._loader()
        self.assertIn("const keepKey = cal.selectedDay.value ? localYmd(cal.selectedDay.value.date) : '';", load)
        self.assertIn("else if (keepKey) applyDateKey(keepKey);", load)
        # The link (or a history step) still wins over the carried-over day.
        self.assertLess(load.index("if (pendingDateKey) applyDateKey(pendingDateKey);"),
                        load.index("else if (keepKey) applyDateKey(keepKey);"))

    def test_the_month_key_drives_the_load_and_the_address_bar(self):
        js = self._js()
        self.assertIn("const anchorMonth = computed(() => url.monthKey(cal.currentDate.value));", js)
        self.assertIn("watch(anchorMonth, () => { if (!restoringFromHistory) loadEarnings(); });", js)
        self.assertIn("watch([appTab, anchorMonth], () => {", js)

    def test_the_switch_goes_through_the_alignment_hook(self):
        """`viewMode='week'` assigned straight from the template skips the fix."""
        html = fc.read_index()
        self.assertIn("@click=\"setViewMode('week')\"", html)
        self.assertIn("@click=\"setViewMode('month')\"", html)
        self.assertNotIn("@click=\"viewMode='week'\"", html)
        self.assertIn("if (mode === viewMode.value) return;", self._js())

    def test_the_hook_is_exposed_to_the_runtime_template(self):
        exposed = fc.exposed_setup_keys(self._js())
        self.assertIn("setViewMode", exposed)
        self.assertEqual(fc.dangling_bindings(fc.read_index(), exposed), set())
