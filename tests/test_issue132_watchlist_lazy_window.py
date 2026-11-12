"""Issue #132: the calendar first paint must not fetch the 420-day watchlist window.

`loadNextEarnings()` asks `/api/earnings?start=today&end=today+420d&watchlistOnly=true`
— the request only the 自选 tab renders — and `onMounted` called it unconditionally.
Most visits never leave the calendar, so that query, its JSON and its memory were
paid on every first paint.

What must *not* change: the calendar marks your own symbols with ☆ (`badge-mine`)
and the day list shows the ☆ toggle state, both from `isMine()`, which reads the
symbol list. That list is 885 bytes for 18 symbols against 5.7KB for the window,
so the split is: symbols stay on first paint, the window moves to tab entry.

The measured before/after is on the issue; these pins keep the code shape honest.
"""
import re
import sys
from pathlib import Path
from unittest import TestCase

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)


def _mount_block() -> str:
    js = fc.read_app_js()
    block = js[js.index("onMounted(async () => {"):]
    return block[:block.index("});")]


def _load_tab_data() -> str:
    js = fc.read_app_js()
    block = js[js.index("function loadTabData(tab) {"):]
    return block[:block.index("watch(appTab, loadTabData);")]


class WatchlistLazyWindowTests(TestCase):
    def test_the_first_paint_does_not_request_the_forward_window(self):
        mount = _mount_block()
        assert "loadNextEarnings" not in mount, (
            "the 420-day window is the 自选 tab's data; loading it on the calendar "
            "first paint costs every visitor a request they never use"
        )
        assert "WATCHLIST_NEXT_WINDOW_DAYS" not in mount

    def test_the_first_paint_still_loads_the_symbol_list(self):
        mount = _mount_block()
        assert "await wl.loadWatchlist();" in mount, (
            "isMine() draws the calendar's ☆ markers from the symbol list"
        )
        # …and only for a signed-in reader: the endpoint is authenticated.
        assert "if (user.value) {" in mount

    def test_entering_the_tab_loads_the_window(self):
        tab = _load_tab_data()
        assert "if (tab === 'watchlist' && user.value) {" in tab
        assert "wl.loadNextEarnings();" in tab

    def test_entering_the_tab_is_also_enough_for_a_deep_link(self):
        """`?tab=watchlist` skips the watcher, so the entry load must be complete."""
        tab = _load_tab_data()
        assert "if (!wl.watchlistLoaded.value) wl.loadWatchlist();" in tab, (
            "a deep link into 自选 must not render an empty table while the list loads"
        )

    def test_a_mutation_and_a_month_change_still_refresh_what_they_must(self):
        js = fc.read_app_js()
        add = js[js.index("async function addToWatchlist("):]
        add = add[:add.index("async function removeFromWatchlist(")]
        assert "await loadWatchlist();" in add and "await loadNextEarnings();" in add
        # The calendar month keeps reloading only the calendar.
        assert "watch(cal.currentDate, () => { if (!restoringFromHistory) loadEarnings(); });" in js
        assert not re.search(r"watch\(cal\.currentDate, [^)]*loadNextEarnings", js)
