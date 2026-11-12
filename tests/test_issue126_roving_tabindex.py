"""Issue #126: a month grid is one tab stop, not thirty-five.

#95 made every cell keyboard-operable by giving each one `tabindex="0"`, and
each cell can hold four badge buttons on top of that. A month view therefore put
~35 tab stops between the calendar and the next control, and a keyboard or
screen-reader user had to press Tab ~35 times to reach 下个月 — then up to four
more per day to walk its badges.

The WAI-ARIA grid pattern answers this with **roving tabindex**: the grid holds
exactly one tab stop, and the arrow keys move it. The probe runs the served
`useCalendar()` under node with the vendored Vue build, so the anchor chain and
the key handling are the shipped code.
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
const localYmd = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');

/*USE_CALENDAR*/

const earnings = ref([
  { id: 1, report_date: '2026-11-05', symbol: 'NET', market: 'US' },
  { id: 2, report_date: '2026-11-06', symbol: 'IRM', market: 'US' },
]);
const cal = useCalendar(earnings, null, ref(false));
cal.currentDate.value = new Date(2026, 10, 15);

// `calendarCells` is a computed: it must be re-read after every month change,
// otherwise the probe looks for dates in a window that no longer exists.
const cells = () => cal.calendarCells.value;
const zeroStops = () => cells().filter(c => cal.cellTabIndex(c) === 0).length;
const anchorKey = () => {
  const cell = cells().find(c => cal.cellTabIndex(c) === 0);
  return cell ? localYmd(cell.date) : null;
};
const weekday = (key) => new Date(key + 'T00:00:00').getDay();

const initial = {
  weeks: cal.calendarWeeks.value.map(w => w.length),
  cells: cells().length,
  zero_stops: zeroStops(),
  anchor: anchorKey(),
};

// ArrowRight / ArrowDown / ArrowLeft move the single stop by 1 / 7 / back.
cal.handleGridKey('ArrowRight');
const afterRight = anchorKey();
cal.handleGridKey('ArrowDown');
const afterDown = anchorKey();
cal.handleGridKey('ArrowLeft');
cal.handleGridKey('ArrowUp');
const afterBack = anchorKey();

// Home / End snap to the ends of the row the anchor is on.
cal.handleGridKey('Home');
const afterHome = anchorKey();
cal.handleGridKey('End');
const afterEnd = anchorKey();
const homeWeekday = weekday(afterHome);
const endWeekday = weekday(afterEnd);

const stopsAfterKeys = zeroStops();

// PageDown / PageUp change the month and keep the day of the month.
cal.handleGridKey('PageDown');
const afterPageDown = anchorKey();
const monthAfterPageDown = cal.currentDate.value.getMonth();
cal.handleGridKey('PageUp');
const afterPageUp = anchorKey();
const monthAfterPageUp = cal.currentDate.value.getMonth();

// A short month clamps instead of overflowing (Jan 31 -> Feb 28).
cal.currentDate.value = new Date(2026, 0, 1);
const jan31 = cells().find(c => localYmd(c.date) === '2026-01-31');
cal.selectCell(jan31);
const anchorOnJan31 = anchorKey();
cal.handleGridKey('PageDown');
const clamped = anchorKey();
const clampedMonth = cal.currentDate.value.getMonth();

// Selection drives the anchor too (mouse click / watchlist toggle).
const jan20 = cells().find(c => localYmd(c.date) === '2026-02-20');
cal.selectCell(jan20);
const anchorFollowsSelection = anchorKey() === localYmd(jan20.date);

// Enter/Space and any unrelated key are not navigation.
const enterHandled = cal.handleGridKey('Enter');
const letterHandled = cal.handleGridKey('a');

console.log(JSON.stringify({
  initial, afterRight, afterDown, afterBack, afterHome, afterEnd,
  home_weekday: homeWeekday, end_weekday: endWeekday,
  stops_after_keys: stopsAfterKeys,
  afterPageDown, month_after_page_down: monthAfterPageDown,
  afterPageUp, month_after_page_up: monthAfterPageUp,
  anchor_on_jan31: anchorOnJan31, clamped, clamped_month: clampedMonth,
  anchor_follows_selection: anchorFollowsSelection,
  enter_handled: enterHandled, letter_handled: letterHandled,
}));
"""


def _run_grid_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useCalendar")
    probe = PROBE.replace("/*USE_CALENDAR*/", source)
    completed = subprocess.run(
        ["node", "-e", probe], capture_output=True, text=True, timeout=60,
        env={**os.environ, "VUE_PATH": str(VENDOR_VUE)},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _shift(ymd: str, days: int) -> str:
    year, month, day = (int(part) for part in ymd.split("-"))
    from datetime import date, timedelta
    return (date(year, month, day) + timedelta(days=days)).isoformat()


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class RovingTabindexTests(TestCase):
    def test_the_month_grid_has_exactly_one_tab_stop(self):
        out = _run_grid_probe()
        self.assertEqual(out["initial"]["cells"], 35)
        self.assertEqual(out["initial"]["weeks"], [7] * 5)
        self.assertEqual(out["initial"]["zero_stops"], 1)
        self.assertEqual(out["stops_after_keys"], 1, "every key press must leave exactly one stop")

    def test_the_anchor_lands_on_today_before_anything_is_selected(self):
        out = _run_grid_probe()
        # The probe pins the month to 2026-11-15; no selection exists yet, so the
        # anchor is the first in-month day rather than an arbitrary cell.
        self.assertTrue(out["initial"]["anchor"].startswith("2026-11-"))

    def test_arrow_keys_move_the_single_stop(self):
        out = _run_grid_probe()
        start = out["initial"]["anchor"]
        self.assertEqual(out["afterRight"], _shift(start, 1))
        self.assertEqual(out["afterDown"], _shift(out["afterRight"], 7))
        self.assertEqual(out["afterBack"], start)

    def test_home_and_end_snap_to_the_row_ends(self):
        out = _run_grid_probe()
        # Sunday → Saturday of the anchor's own week, wherever the window starts.
        self.assertEqual(out["home_weekday"], 0)
        self.assertEqual(out["end_weekday"], 6)
        self.assertEqual(out["afterEnd"], _shift(out["afterHome"], 6))

    def test_page_up_and_down_change_the_month(self):
        out = _run_grid_probe()
        self.assertEqual(out["month_after_page_down"], 11)   # December (0-based)
        self.assertEqual(out["month_after_page_up"], 10)     # November
        # The day of the month survives the month change (the anchor before the
        # page keys was the row end, 「End」 having just been pressed).
        self.assertEqual(out["afterPageDown"].split("-")[2], out["afterEnd"].split("-")[2])
        self.assertEqual(out["afterPageUp"].split("-")[2], out["afterEnd"].split("-")[2])

    def test_a_short_month_clamps_instead_of_overflowing(self):
        out = _run_grid_probe()
        self.assertEqual(out["anchor_on_jan31"], "2026-01-31")
        self.assertEqual(out["clamped"], "2026-02-28")
        self.assertEqual(out["clamped_month"], 1)

    def test_selecting_a_day_moves_the_anchor_with_it(self):
        out = _run_grid_probe()
        self.assertTrue(out["anchor_follows_selection"])

    def test_enter_space_and_letters_are_not_navigation(self):
        out = _run_grid_probe()
        self.assertFalse(out["enter_handled"])
        self.assertFalse(out["letter_handled"])


class GridMarkupTests(TestCase):
    def test_the_grid_is_one_tab_stop_and_the_badges_are_not_stops_at_all(self):
        html = fc.read_index()
        assert 'role="grid" aria-label="财报日历"' in html
        assert ":tabindex=\"cellTabIndex(cell)\"" in html, "the cell's stop must be roving"
        assert 'tabindex="0"' not in html.split('class="calendar-cell"')[1].split(">")[0], (
            "a cell must not hard-code its tabindex"
        )
        # Four badges per day would be four more stops each; the cell opens the
        # day's list, and that list's rows are real buttons.
        badge = html[html.index('@click.stop="selectEarning(e, cell)"'):]
        opening = html.rindex("<button", 0, html.index('@click.stop="selectEarning(e, cell)"'))
        assert 'tabindex="-1"' in html[opening:html.index(">", opening)], (
            "an in-cell badge must not be its own tab stop"
        )

    def test_the_grid_carries_rows_without_changing_the_layout(self):
        html = fc.read_index()
        assert 'role="row" style="display:contents;"' in html
        assert "v-for=\"(week, wi) in calendarWeeks\"" in html
        assert 'role="gridcell"' in html
        assert 'role="columnheader"' in html
        # `display:contents` is what keeps the two visual grids (and their 1px
        # gap trick) exactly as they were.
        assert 'role="presentation"' in html

    def test_navigation_keys_are_handled_on_the_grid(self):
        html = fc.read_index()
        assert '@keydown="onGridKeydown"' in html
        js = fc.read_app_js()
        assert "onGridKeydown: (event) => {" in js
        assert "if (cal.handleGridKey(event.key)) event.preventDefault();" in js
        for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"):
            assert f"'{key}'" in js, key
