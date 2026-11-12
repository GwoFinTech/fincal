"""Issue #125: the address bar must describe the view.

``?tab=&month=&date=`` did not exist: the tab, the month and the selected day
were refs only, so a refresh returned to today, a link handed to a colleague
opened *their* default view, and 后退 left the site instead of stepping back
through the calendar. A calendar's reference action is sharing 「这只票这一天」,
and it had no representation.

The probe runs the served ``useUrlState()`` with a fake ``window`` — the same
parser/serializer the page uses — so a formatting change cannot pass while the
Python-side copy stays green.
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

PROBE = r"""
/*USE_URL_STATE*/

const calls = [];
const host = {
  history: {
    pushState: (state, title, url) => calls.push(['push', url]),
    replaceState: (state, title, url) => calls.push(['replace', url]),
  },
  location: { pathname: '/cal' },
};
const url = useUrlState(host);

const parsed = {
  full: url.parseUrlState('?tab=watchlist&month=2026-11&date=2026-11-05'),
  tab_only: url.parseUrlState('?tab=calendar'),
  empty: url.parseUrlState(''),
  garbage: url.parseUrlState('?tab=nonsense&month=2026-13&date=05/11/2026'),
  no_question_mark: url.parseUrlState('tab=universe&month=2026-01'),
};

const built = {
  full: url.buildUrlState({ tab: 'calendar', month: '2026-11', date: '2026-11-05' }),
  bad_tab: url.buildUrlState({ tab: 'nope', month: '2026-11', date: '' }),
  bad_month: url.buildUrlState({ tab: 'calendar', month: '2026-13', date: '2026-11-05' }),
  empty: url.buildUrlState({}),
};

const month = url.monthDate('2026-11');
const month_out = {
  year: month ? month.getFullYear() : null,
  month: month ? month.getMonth() : null,
  day: month ? month.getDate() : null,
};
const rejected = ['2026-13', '2026-00', 'abc', '', null, '2026-1'].map(v => url.monthDate(v) === null);

const pushResult = url.pushUrl({ tab: 'watchlist', month: '2026-11', date: '' });
const replaceResult = url.replaceUrl({ tab: 'watchlist', month: '2026-11', date: '2026-11-05' });
const blank = url.replaceUrl({});

console.log(JSON.stringify({
  tabs: url.TABS,
  parsed, built, month_out, rejected,
  month_key: url.monthKey(month),
  month_key_empty: url.monthKey(null),
  push_result: pushResult,
  replace_result: replaceResult,
  blank_url: blank,
  calls,
}));
"""


def _run_url_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useUrlState")
    probe = PROBE.replace("/*USE_URL_STATE*/", source)
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True,
                               timeout=60, env={**os.environ})
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class UrlStateTests(TestCase):
    def test_a_shared_link_carries_tab_month_and_day(self):
        out = _run_url_probe()
        self.assertEqual(out["tabs"], ["calendar", "watchlist", "universe"])
        self.assertEqual(out["parsed"]["full"],
                         {"tab": "watchlist", "month": "2026-11", "date": "2026-11-05"})
        self.assertEqual(out["parsed"]["tab_only"],
                         {"tab": "calendar", "month": "", "date": ""})
        self.assertEqual(out["parsed"]["empty"], {"tab": "", "month": "", "date": ""})
        self.assertEqual(out["parsed"]["no_question_mark"],
                         {"tab": "universe", "month": "2026-01", "date": ""})

    def test_an_illegal_parameter_falls_back_instead_of_raising(self):
        out = _run_url_probe()
        self.assertEqual(out["parsed"]["garbage"], {"tab": "", "month": "", "date": ""})
        self.assertTrue(all(out["rejected"]), "a non-existent month must not become a date")

    def test_the_month_parses_to_the_first_of_that_month(self):
        out = _run_url_probe()
        self.assertEqual(out["month_out"], {"year": 2026, "month": 10, "day": 1})
        self.assertEqual(out["month_key"], "2026-11")
        self.assertEqual(out["month_key_empty"], "")

    def test_the_serializer_never_writes_a_value_it_would_reject_on_read(self):
        out = _run_url_probe()
        self.assertEqual(out["built"]["full"], "?tab=calendar&month=2026-11&date=2026-11-05")
        self.assertEqual(out["built"]["bad_tab"], "?month=2026-11")
        self.assertEqual(out["built"]["bad_month"], "?tab=calendar&date=2026-11-05")
        self.assertEqual(out["built"]["empty"], "")

    def test_a_selection_replaces_and_a_view_change_pushes(self):
        out = _run_url_probe()
        self.assertEqual(out["push_result"], "?tab=watchlist&month=2026-11")
        self.assertEqual(out["replace_result"], "?tab=watchlist&month=2026-11&date=2026-11-05")
        self.assertEqual(out["calls"], [
            ["push", "?tab=watchlist&month=2026-11"],
            ["replace", "?tab=watchlist&month=2026-11&date=2026-11-05"],
            # No state left → the bare path, never a stale query string.
            ["replace", "/cal"],
        ])


class UrlWiringTests(TestCase):
    def test_the_link_is_read_before_the_first_load(self):
        js = fc.read_app_js()
        read = js.index("url.parseUrlState(")
        # Relative to the *first mount-time* load, not the `addToWatchlist`
        # refreshes that appear earlier in the file.
        first_load = js.index("await loadEarnings();", js.index("onMounted(async () => {"))
        self.assertLess(read, first_load)
        # The month must be applied before the request window is computed from it.
        self.assertIn("cal.currentDate.value = initialMonth;", js)
        self.assertIn("if (urlInitial.tab) appTab.value = urlInitial.tab;", js)

    def test_the_linked_day_is_applied_once_the_cells_exist(self):
        js = fc.read_app_js()
        # The day is pending state, applied by the load that builds the cells —
        # a one-shot call after mount could not survive a month switch.
        self.assertIn("let pendingDateKey = urlInitial.date;", js)
        self.assertIn("localYmd(c.date) === key", js)

    def test_the_page_writes_its_state_on_view_and_selection_changes(self):
        js = fc.read_app_js()
        self.assertIn("watch([appTab, cal.currentDate], () => {", js)
        self.assertIn("if (!restoringFromHistory) url.pushUrl(currentUrlState());", js)
        self.assertIn("if (!restoringFromHistory) url.replaceUrl(currentUrlState());", js)

    def test_a_history_step_restores_the_view_not_just_the_address(self):
        """后退 moved the URL but left the old month on screen, because nothing
        listened for `popstate` — the write-watchers had no way back."""
        js = fc.read_app_js()
        self.assertIn("window.addEventListener('popstate', () => { applyHistoryState(); });", js)
        handler = js[js.index("async function applyHistoryState()"):]
        handler = handler[:handler.index("window.addEventListener('popstate'")]
        # Whatever the link carries has to be re-applied, and the writes muted.
        for token in ("appTab.value = state.tab || 'calendar'",
                      "cal.currentDate.value = month",
                      "pendingDateKey = state.date",
                      "restoringFromHistory = true",
                      "await loadEarnings();",
                      "loadTabData(appTab.value);",
                      "restoringFromHistory = false"):
            self.assertIn(token, handler)

    def test_a_restored_day_is_not_overwritten_by_the_month_load(self):
        """`loadEarnings()` settles 「today」 first; the link's day must win."""
        js = fc.read_app_js()
        load = js[js.index("async function loadEarnings()"):]
        load = load[:load.index("// ── Address bar writes")]
        self.assertIn("if (pendingDateKey) applyDateKey(pendingDateKey);", load)

    def test_a_tab_named_by_the_link_still_loads_its_data(self):
        """`appTab` is already set when the watcher registers, so the watch alone
        would leave 系统自选 empty on a deep link."""
        js = fc.read_app_js()
        self.assertIn("watch(appTab, loadTabData);", js)
        mount = js.index("onMounted(async () => {")
        self.assertIn("loadTabData(appTab.value);", js[mount:])
