"""Issue #122: selecting an empty day must actually select it.

``selectCell`` started with

    if (cell.earnings.length === 0) return;

so tapping a weekend or a quiet weekday left ``selectedDay`` on the *previous*
day: the panel header and its list stayed on A, and nothing on screen told the
reader whether B had no reports or the click had not registered.  Calendar
navigation is a selection, not a filter — the panel already renders 「当天暂无
财报。」 for an empty day, it just never received one.

The probe runs the served ``useCalendar()`` under node with the vendored Vue
build, so it exercises the real composable (refs, the ``calendarCells``
computed, the selection helper) instead of restating its rules.
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

const sandbox = { console };
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.env.VUE_PATH, 'utf8'), sandbox);
const { ref, computed } = sandbox.Vue;

/*USE_CALENDAR*/

const earnings = ref([
  { id: 1, report_date: '2026-11-05', symbol: 'NET', market: 'US' },
]);
const cal = useCalendar(earnings, null, ref(false));
cal.currentDate.value = new Date(2026, 10, 15);

const cells = cal.calendarCells.value;
const busy = cells.find(c => c.earnings.length > 0);
const empty = cells.find(c => c.earnings.length === 0 && c.inMonth);

const sameCell = (a, b) => Boolean(a && b && a.date.getTime() === b.date.getTime());

cal.selectCell(busy);
const busyPicked = sameCell(cal.selectedDay.value, busy);
// Open a symbol's detail on the busy day, then tap the empty one: the stored
// object is a reactive proxy, so identity is not the question — the date is.
cal.selectEarning({ id: 1 }, busy, () => Promise.resolve(null));
cal.selectCell(empty);

const out = {
  busy_picked: busyPicked,
  empty_day: empty.day,
  busy_day: busy.day,
  selected_is_empty: sameCell(cal.selectedDay.value, empty),
  selected_day_number: cal.selectedDay.value ? cal.selectedDay.value.day : null,
  earning_cleared: cal.selectedEarning.value === null,
  highlight_moved: cal.isSelectedDay(empty) && !cal.isSelectedDay(busy),
  empty_day_has_no_reports: cal.selectedDay.value ? cal.selectedDay.value.earnings.length : -1,
};
console.log(JSON.stringify(out));
"""


def _run_calendar_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useCalendar")
    probe = PROBE.replace("/*USE_CALENDAR*/", source)
    completed = subprocess.run(
        ["node", "-e", probe],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "VUE_PATH": str(VENDOR_VUE)},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class EmptyDaySelectionTests(TestCase):
    def test_a_day_without_reports_becomes_the_selected_day(self):
        out = _run_calendar_probe()
        self.assertNotEqual(out["empty_day"], out["busy_day"])
        self.assertTrue(out["selected_is_empty"], "the click must move the selection")
        self.assertEqual(out["selected_day_number"], out["empty_day"])

    def test_the_previous_detail_does_not_survive_the_move(self):
        out = _run_calendar_probe()
        self.assertTrue(out["earning_cleared"], "an empty day cannot keep showing a symbol's detail")
        self.assertEqual(out["empty_day_has_no_reports"], 0)

    def test_the_highlight_follows_the_selection(self):
        out = _run_calendar_probe()
        self.assertTrue(out["highlight_moved"])


def test_the_calendar_cell_carries_a_selected_state():
    """Without it, an empty day's selection is invisible even once it is stored."""
    html = fc.read_index()
    assert "'selected': isSelectedDay(cell)" in html
    assert ".calendar-cell.selected {" in html
    # The background moved out of the inline style, or the class could not paint.
    assert 'style="background:var(--bg);"' not in html
    assert "cursor: pointer; background: var(--bg);" in html


def test_enter_and_space_take_the_same_path_as_a_click():
    html = fc.read_index()
    assert '@keydown.enter.prevent="selectCell(cell)"' in html
    assert '@keydown.space.prevent="selectCell(cell)"' in html
    # `isSelectedDay` has to be exposed, or the template resolves it to undefined.
    assert "isSelectedDay," in fc.read_app_js()
