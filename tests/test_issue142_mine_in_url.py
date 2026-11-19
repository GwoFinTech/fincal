"""Issue #142: 「只看自选」 must be one state, and it must be the one on screen.

The toolbar checkbox changed the *request* (`/api/earnings?watchlistOnly=true`,
the grid's whole symbol set) while living only in a Vue ref: a refresh or a
bookmark silently dropped the filter, so 「我的自选、这个月」 had no link and no
way to survive an F5. The 「本周财报」 digest made it worse by *inferring* its
scope from the watchlist length — with the filter on and no symbols it kept
saying 「全部标的 · 本周无财报」 above a grid of 0 badges, and the page offered no
clue about why it was empty.

What is pinned here:

* the served ``useUrlState()`` (parsed/serialized under node, so a formatting
  change cannot pass while a Python copy stays green),
* the wiring that reads the key before the first load, restores it on 后退, and
  drops it for a guest,
* the digest rule (scope follows the switch, not the list length), and
* the empty-watchlist hint that replaces the silence.
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

PROBE = r"""
/*USE_URL_STATE*/

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
  on: url.parseUrlState('?tab=calendar&month=2026-11&date=2026-11-19&mine=1'),
  off: url.parseUrlState('?mine=0'),
  word: url.parseUrlState('?mine=yes'),
  blank: url.parseUrlState('?mine='),
  absent: url.parseUrlState('?tab=calendar&month=2026-11&date=2026-11-19'),
};

const built = {
  on: url.buildUrlState({ tab: 'calendar', month: '2026-11', date: '2026-11-19', mine: true }),
  off: url.buildUrlState({ tab: 'calendar', month: '2026-11', date: '2026-11-19', mine: false }),
  off_empty: url.buildUrlState({ tab: 'calendar', month: '2026-11', date: '2026-11-19', mine: '' }),
  absent: url.buildUrlState({ tab: 'calendar', month: '2026-11', date: '2026-11-19' }),
  with_symbol: url.buildUrlState({ tab: 'calendar', month: '2026-11', date: '2026-11-19', symbol: 'NVDA', mine: true }),
};

// A link with the key has to survive read → write unchanged, otherwise 后退
// would rewrite the reader's URL into a different byte string.
const round_trip = url.buildUrlState(url.parseUrlState('?tab=calendar&month=2026-11&date=2026-11-19&mine=1'));

// One click = one history step: the page is already at the unfiltered link, so
// only the toggles write, and the second write of the same state is a no-op.
calls.length = 0;
host.location.search = '?tab=calendar&month=2026-11&date=2026-11-19';
url.pushUrl({ tab: 'calendar', month: '2026-11', date: '2026-11-19' });
url.pushUrl({ tab: 'calendar', month: '2026-11', date: '2026-11-19', mine: true });
const after_on = calls.length;
url.pushUrl({ tab: 'calendar', month: '2026-11', date: '2026-11-19', mine: true });
const after_duplicate = calls.length;
url.pushUrl({ tab: 'calendar', month: '2026-11', date: '2026-11-19', mine: false });
const after_off = calls.length;

console.log(JSON.stringify({
  parsed, built, round_trip, calls,
  after_on, after_duplicate, after_off,
}));
"""


def _run_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useUrlState")
    probe = PROBE.replace("/*USE_URL_STATE*/", source)
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True,
                               timeout=60, env={**os.environ})
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class MineInUrlTests(TestCase):
    def test_the_link_names_the_filter_only_when_it_is_on(self):
        out = _run_probe()["parsed"]
        self.assertEqual(out["on"], {"tab": "calendar", "month": "2026-11",
                                     "date": "2026-11-19", "mine": True})
        # `mine=0`, `mine=yes`, `mine=` are not the filter being on — the key is
        # only honoured as the literal `1` (the same whitelist idea as `symbol`).
        for key in ("off", "word", "blank"):
            self.assertNotIn("mine", out[key], key)
        self.assertNotIn("mine", out["absent"])

    def test_the_serializer_keeps_older_links_byte_identical(self):
        built = _run_probe()["built"]
        old = "?tab=calendar&month=2026-11&date=2026-11-19"
        self.assertEqual(built["off"], old)
        self.assertEqual(built["off_empty"], old)
        self.assertEqual(built["absent"], old)
        self.assertEqual(built["on"], old + "&mine=1")
        self.assertEqual(built["with_symbol"], old + "&symbol=NVDA&mine=1")

    def test_a_filtered_link_survives_the_read_write_round_trip(self):
        self.assertEqual(_run_probe()["round_trip"],
                         "?tab=calendar&month=2026-11&date=2026-11-19&mine=1")

    def test_toggling_is_one_history_step_each_way(self):
        out = _run_probe()
        self.assertEqual(out["after_on"], 1, "the click is one step")
        self.assertEqual(out["after_duplicate"], 1, "a repeated write is not a step")
        self.assertEqual(out["after_off"], 2)
        self.assertEqual(out["calls"], [
            ["push", "?tab=calendar&month=2026-11&date=2026-11-19&mine=1"],
            ["push", "?tab=calendar&month=2026-11&date=2026-11-19"],
        ])


class MineWiringTests(TestCase):
    def setUp(self):
        self.js = fc.read_app_js()
        self.html = fc.read_index()

    def test_the_link_is_applied_before_the_first_request(self):
        init = self.js.index("watchlistOnly.value = urlInitial.mine === true;")
        mount = self.js.index("onMounted(async () => {")
        first_load = self.js.index("await loadEarnings();", mount)
        self.assertLess(init, mount)
        self.assertLess(init, first_load)
        # The request, not just the checkbox: it goes through `watchlistOnly.value`.
        loader = self.js[self.js.index("async function loadEarnings()"):]
        loader = loader[:loader.index("} finally {")]
        self.assertIn("watchlistOnly: watchlistOnly.value", loader)

    def test_a_toggle_pushes_and_a_history_step_restores(self):
        self.assertIn("mine: watchlistOnly.value === true,", self.js)
        watcher = self.js[self.js.index("watch(watchlistOnly, () => {"):]
        watcher = watcher[:watcher.index("});")]
        self.assertIn("if (!restoringFromHistory) url.pushUrl(currentUrlState());", watcher)
        handler = self.js[self.js.index("async function applyHistoryState()"):]
        handler = handler[:handler.index("window.addEventListener('popstate'")]
        # The filter has to be in place *before* the load it changes.
        self.assertIn("watchlistOnly.value = state.mine === true;", handler)
        self.assertLess(handler.index("watchlistOnly.value = state.mine === true;"),
                        handler.index("await loadEarnings();"))

    def test_a_guest_gets_the_public_calendar_and_a_link_without_the_key(self):
        mount = self.js[self.js.index("onMounted(async () => {"):]
        fallback = mount.index("if (!user.value && watchlistOnly.value) {")
        self.assertLess(mount.index("const userData = await loadUser();"), fallback)
        self.assertLess(fallback, mount.index("await loadEarnings();"))
        block = mount[fallback:mount.index("if (user.value) {", fallback)]
        self.assertIn("watchlistOnly.value = false;", block)
        self.assertIn("url.replaceUrl(publicState);", block)
        # The ref's watcher flushes on the next tick; the guard has to cover it,
        # or a push with an empty `date` overwrites the preserved link.
        self.assertLess(block.index("await nextTick();"), block.index("url.replaceUrl(publicState);"))
        # The rest of the link is preserved: only the account-bound key is dropped.
        self.assertIn("publicState.date = pendingDateKey;", block)
        self.assertIn("publicState.symbol = pendingSymbolKey;", block)

    def test_the_digest_scope_follows_the_switch_not_the_list_length(self):
        block = self.js[self.js.index("const weekAhead = computed(() => {"):]
        block = block[:block.index("      });")]
        self.assertIn("const mineOnly = watchlistOnly.value || wl.watchlist.value.length > 0;", block)
        self.assertIn("mineOnly ? wl.isMine : null", block)

    def test_an_empty_watchlist_explains_itself_above_the_grid(self):
        hint = self.html[self.html.index("watchlistOnly && watchlistLoaded && !watchlist.length"):]
        hint = hint[:hint.index("</div>")]
        self.assertIn("自选还是空的", hint)
        self.assertIn("@click=\"appTab = 'watchlist'\"", hint)
        self.assertIn("去「自选」添加", hint)
        # It belongs in the same layer as the error banner: above the digest and
        # the grid, so it is read before the empty cells are.
        hint_at = self.html.index("watchlistOnly && watchlistLoaded && !watchlist.length")
        self.assertLess(hint_at, self.html.index("<!-- Calendar grid -->"))
        self.assertLess(self.html.index('v-if="error || writeError"'), hint_at)

    def test_the_hint_has_no_new_template_binding(self):
        exposed = fc.exposed_setup_keys(self.js)
        self.assertEqual(fc.dangling_bindings(self.html, exposed), set())

    def test_the_hint_reuses_existing_tokens_instead_of_a_new_one(self):
        block = self.html[self.html.index("watchlistOnly && watchlistLoaded && !watchlist.length"):]
        block = block[:block.index("<!-- 「本周财报」 digest")]
        self.assertIn("var(--surface-raised)", block)
        self.assertIn("var(--border)", block)
        self.assertNotIn("--mine-", block)
        # 390px: the sentence wraps and the button drops below it instead of
        # widening the page.
        self.assertIn("flex-wrap", block)
