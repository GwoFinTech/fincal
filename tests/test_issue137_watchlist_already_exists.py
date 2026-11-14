"""Issue #137: `already_exists` is 「没变」, not 「已添加」.

`POST /api/watchlist` answers **200** `{"status": "already_exists"}` when the row
is already there (`ON CONFLICT ... DO NOTHING RETURNING *` returns nothing), and
the row count does not change.  #82's `if (!added) return;` guard cannot see
that: the request succeeded, so the SPA announced 「已添加 AAPL」 for a change that
never happened — and the candidate list, the only way to add a symbol, rendered
「添加」 for rows that were already in the watchlist, so an existing subscriber
could click 「热门」 six times and get six false successes.

Two contracts replace it:

* the write path reads the body's ``status``: ``already_exists`` → 「已在自选」,
  every other 2xx → 「已添加」 (pinned as an exact string, so a backend rename of
  the success code fails here instead of silently regressing to a false claim);
* the candidate rows and the 热门 chips are membership-aware toggles, i.e. the
  same ☆/★ convention the calendar already uses (``isMine()`` +
  ``toggleWatchlist()``).

The behaviour checks run the served `setup()` under node against a stubbed
`fetch`; the reverse validation replays the pre-fix shape of `addToWatchlist` and
must observe the old false toast, otherwise the probe proves nothing.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, skipUnless

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)

VENDOR_VUE = fc.ROOT / "app" / "static" / "assets" / "vendor" / "vue.global.prod.js"

WATCHLIST_MAIN = re.compile(r'<main v-if="appTab === \'watchlist\'".*?</main>', re.S)

# Executes the real bundle in a vm with the vendored Vue build: the component
# options are captured instead of mounted (no DOM), the real setup() runs, and
# one POST /api/watchlist is performed against a stubbed transport.
PROBE = r"""
const fs = require('fs');
const vm = require('vm');

const sandbox = {
  console, setTimeout, clearTimeout, URLSearchParams,
  JSON, Math, Date, Object, Array, String, Number, Boolean, Map, Set, Error, RegExp,
  encodeURIComponent, encodeURI, decodeURIComponent, parseInt, parseFloat, isNaN,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.env.VUE_PATH, 'utf8'), sandbox);

const realVue = sandbox.Vue;
let captured = null;
const fakeVue = Object.create(realVue);
fakeVue.createApp = options => { captured = options; return { mount: () => ({}) }; };
sandbox.Vue = fakeVue;

vm.runInContext(fs.readFileSync(process.env.APP_PATH, 'utf8'), sandbox);
const api = captured.setup();

const calls = [];
const write = JSON.parse(process.env.WRITE);   // the POST answer
const list = JSON.parse(process.env.WATCHLIST || '[]');  // GET /api/watchlist
function respond(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => JSON.parse(JSON.stringify(body)),
    text: async () => JSON.stringify(body),
  };
}
sandbox.fetch = async (path, init = {}) => {
  const method = init.method || 'GET';
  calls.push({ method, path });
  if (path.startsWith('/api/watchlist') && method !== 'GET') return respond(write.status, write.body);
  if (path === '/api/watchlist') return respond(200, list);
  if (path === '/api/me') return respond(200, { id: 7, name: 'probe', ical_url: '/ical/x' });
  return respond(200, []);
};

(async () => {
  await api.loadEarnings();      // settle the read surface first
  calls.length = 0;              // only the write path is under test
  await api.addToWatchlist('AAPL', 'US');
  console.log('RESULT ' + JSON.stringify({
    toast: api.toast.value,
    writeError: api.writeError.value,
    watchlist: api.watchlist.value,
    isMine: api.isMine({ symbol: 'AAPL', market: 'US' }),
    calls: calls.map(c => c.method),
  }));
})();
"""


def run_probe(plan: dict, watchlist=None) -> dict:
    env = {
        **os.environ,
        "VUE_PATH": str(VENDOR_VUE),
        "APP_PATH": str(APP_JS_PATH[0]),
        "WRITE": json.dumps(plan),
        "WATCHLIST": json.dumps(watchlist or []),
    }
    completed = subprocess.run(
        ["node", "-e", PROBE], capture_output=True, text=True, timeout=120, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    for line in completed.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    raise AssertionError(f"probe printed no result:\n{completed.stdout}\n{completed.stderr}")


# The probe reads the file it executes from this single-element list so the
# reverse validation can point it at the pre-fix copy without globals.
APP_JS_PATH = [fc.APP_JS]

ALREADY_EXISTS = {"status": 200, "body": {"symbol": None, "market": None,
                                          "status": "already_exists"}}


def prefix_source() -> str:
    """`addToWatchlist` with #137's status branch removed (the pre-fix shape)."""
    js = fc.read_app_js()
    js, n = re.subn(
        r"\(added\.status === 'already_exists' \? '已在自选 ' : '已添加 '\)",
        "'已添加 '",
        js,
    )
    assert n == 1, f"expected 1 status branch in addToWatchlist, replaced {n}"
    return js


def watchlist_page() -> str:
    match = WATCHLIST_MAIN.search(fc.read_index())
    assert match, "the watchlist page <main> is missing from index.html"
    return match.group(0)


class TestSourceContract(TestCase):
    def test_the_write_path_reads_the_returned_status(self):
        body = fc.function_source(fc.read_app_js(), "addToWatchlist")
        self.assertIn("added.status === 'already_exists'", body,
                      "a 200 already_exists is not a failure, so the null guard misses it")
        self.assertIn("已在自选", body)
        self.assertIn("已添加", body)
        # The honest sentence must be *inside* the toast argument, not just in a
        # comment: one statement decides both branches.
        toast = re.search(r"showToast\(([^;]*)\);", body)
        self.assertIsNotNone(toast, "no toast in the write path")
        assert toast is not None
        self.assertIn("added.status === 'already_exists'", toast.group(1))
        self.assertIn("已在自选", toast.group(1))

    def test_the_already_exists_code_is_pinned(self):
        """The backend's success code is the only judge; renaming it must fail here."""
        src = (fc.ROOT / "app" / "routers" / "api.py").read_text(encoding="utf-8")
        start = src.index("def api_add_watchlist(")
        block = src[start:src.index("def api_remove_watchlist(")]
        self.assertIn('"status": "already_exists"', block)


class TestWatchlistPageMarkup(TestCase):
    def test_candidate_rows_are_toggles(self):
        page = watchlist_page()
        self.assertIn('@click="toggleWatchlist(r.symbol, r.market)"', page)
        self.assertNotIn('@click="addToWatchlist(r.symbol', page,
                         "an unconditional add is the defect: the row cannot say 已自选")

    def test_candidate_rows_render_the_membership_state(self):
        page = watchlist_page()
        badge = page[page.index('v-for="r in searchResults"'):]
        badge = badge[:badge.index("</button>")]
        self.assertIn("isMine(r)", badge)
        self.assertIn("已自选", badge, "the badge must name the state before the click")
        self.assertIn("badge-mine", badge, "same outline the calendar grid uses")
        self.assertIn("添加", badge, "a non-member still needs its add affordance")

    def test_popular_chips_show_they_are_already_mine(self):
        page = watchlist_page()
        chips = page[page.index("热门："):]
        chips = chips[:chips.index("</div>", chips.index("popularSuggestions"))]
        self.assertIn('@click="toggleWatchlist(s.symbol, s.market)"', chips)
        self.assertIn("isMine(s)", chips)
        self.assertNotIn('addToWatchlist(s.symbol', chips)

    def test_the_convention_is_shared_with_the_calendar(self):
        """Reused helpers, not a new one-off state: both are exposed by setup()."""
        keys = fc.exposed_setup_keys(fc.read_app_js())
        self.assertIn("isMine", keys)
        self.assertIn("toggleWatchlist", keys)
        self.assertEqual(sorted(fc.dangling_bindings(fc.read_index(), keys)), [])


@skipUnless(shutil.which("node"), "node is required for the setup() probe")
class TestAlreadyExistsInTheRealSetup(TestCase):
    def tearDown(self):
        APP_JS_PATH[0] = fc.APP_JS

    def test_already_exists_does_not_claim_a_new_row(self):
        result = run_probe(ALREADY_EXISTS)
        self.assertEqual(result["toast"], "已在自选 AAPL")
        self.assertNotIn("已添加", result["toast"])
        self.assertIsNone(result["writeError"])

    def test_already_exists_still_refreshes_so_the_row_can_flip(self):
        result = run_probe(ALREADY_EXISTS, watchlist=[{"symbol": "AAPL", "market": "US"}])
        self.assertEqual(result["calls"][0], "POST")
        self.assertIn("GET", result["calls"], "the lists must reload after the answer")
        self.assertTrue(result["isMine"], "the refreshed list is what the ★ badge reads")
        self.assertEqual(result["watchlist"], [{"symbol": "AAPL", "market": "US"}])

    def test_a_real_insert_still_says_added(self):
        result = run_probe({"status": 200, "body": {"symbol": "AAPL", "market": "US"}},
                           watchlist=[{"symbol": "AAPL", "market": "US"}])
        self.assertEqual(result["toast"], "已添加 AAPL")
        self.assertIsNone(result["writeError"])

    def test_the_probe_sees_the_pre_fix_behaviour(self):
        """Reverse validation: without the branch, `already_exists` reads 「已添加」."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app-setup-prefix.js"
            path.write_text(prefix_source(), encoding="utf-8")
            APP_JS_PATH[0] = path
            result = run_probe(ALREADY_EXISTS)
        self.assertEqual(result["toast"], "已添加 AAPL",
                         "the probe must observe the false success it prevents")
