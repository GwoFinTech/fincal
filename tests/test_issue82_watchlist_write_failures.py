"""Issue #82: a rejected watchlist write must not be reported as success.

``apiFetch`` returned ``null`` for 401/404 and for any failed request, and the
write paths ignored the return value: the user saw 「已添加 AAPL」 / 「已移除
AAPL」 while the database never changed.  The failure had nowhere to show up
either — the only error banner lived inside ``v-if="appTab === 'calendar'"``,
the failure toast was overwritten by the success toast, and the ``loadEarnings()``
that every write path calls first thing cleared ``error``.

The checks below follow Issue #80's lesson: asserting that a string exists in
the source proves nothing.  So the real file is executed under node (same
vendored Vue build, same ``setup()``), a stubbed ``fetch`` answers 401 / 500 /
200, and what the *user would see* is read off the component state.  A
reverse-validation runs the same probe against the pre-fix shape of the source
(guard statements and ``expectOk`` removed) and must observe the old 「谎报成功」
behaviour — otherwise the probe proves nothing.
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

WRITE_ERROR_BANNER = 'v-if="error || writeError"'
WATCHLIST_MAIN = re.compile(r'<main v-if="appTab === \'watchlist\'".*?</main>', re.S)


def function_body(js: str, name: str) -> str:
    """Source of ``async function <name>(...) { ... }`` (brace-matched).

    The parameter list is matched first: ``opts = {}`` would otherwise close
    the depth-0 brace right away and truncate the body.
    """
    start = js.index(f"function {name}(")
    depth = 0
    open_brace = -1
    for i in range(js.index("(", start), len(js)):
        if js[i] == "(":
            depth += 1
        elif js[i] == ")":
            depth -= 1
            if depth == 0:
                open_brace = js.index("{", i)
                break
    assert open_brace > 0, f"unbalanced parameter list in {name}"
    depth = 0
    for i in range(open_brace, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def watchlist_page() -> str:
    match = WATCHLIST_MAIN.search(fc.read_index())
    assert match, "the watchlist page <main> is missing from index.html"
    return match.group(0)


# Runs the real app-setup.js in a vm with the vendored Vue build, captures the
# component options instead of mounting (no DOM needed), calls the real
# setup(), then performs one watchlist write against a stubbed fetch and
# reports what the component state holds afterwards.
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
const plan = JSON.parse(process.env.PLAN);          // write answer
const list = JSON.parse(process.env.WATCHLIST || '[]');  // GET /api/watchlist answer
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
  if (path.startsWith('/api/watchlist') && method !== 'GET') {
    return plan.throw ? Promise.reject(new Error(plan.throw)) : respond(plan.status, plan.body);
  }
  if (path === '/api/watchlist') return respond(200, list);
  if (path === '/api/me') return respond(200, { id: 9999, name: 'probe', ical_url: '/ical/x' });
  return respond(200, []);
};

(async () => {
  const action = process.env.ACTION;
  await api.loadEarnings();               // settles the read surface first
  if (process.env.SEED) api.watchlist.value = JSON.parse(process.env.SEED);
  calls.length = 0;                       // only the write path is under test
  if (action === 'toggle') await api.toggleWatchlist('AAPL', 'US');
  else if (action === 'remove') await api.removeFromWatchlist('AAPL', 'US');
  else await api.addToWatchlist('AAPL', 'US');
  console.log('RESULT ' + JSON.stringify({
    toast: api.toast.value,
    writeError: api.writeError.value,
    readError: api.error.value,
    user: api.user.value,
    watchlist: api.watchlist.value,
    searchQuery: api.searchQuery.value,
    searchResults: api.searchResults.value,
    calls,
  }));
})();
"""


def run_probe(action: str, plan: dict, watchlist=None) -> dict:
    env = {
        **os.environ,
        "VUE_PATH": str(VENDOR_VUE),
        "APP_PATH": str(APP_JS_PATH[0]),
        "PLAN": json.dumps(plan),
        "ACTION": action,
        "WATCHLIST": json.dumps(watchlist or []),
        "SEED": json.dumps(watchlist) if watchlist else "",
    }
    completed = subprocess.run(
        ["node", "-e", PROBE], capture_output=True, text=True, timeout=120, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    for line in completed.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    raise AssertionError(f"probe printed no result:\n{completed.stdout}\n{completed.stderr}")


# The probe reads the file it must execute from this single-element list so the
# reverse-validation can point it at the pre-fix copy without globals.
APP_JS_PATH = [fc.APP_JS]


def prefix_source() -> str:
    """The same file with #82's two fixes textually reverted.

    Pins that the probe can actually see the bug: without the ``if (!ok)``
    guards and without ``expectOk``, a 401 has to render 「已添加」 again.
    """
    js = fc.read_app_js()
    js, n_guards = re.subn(r"^\s*if \(!(?:added|removed)\) return;\n", "", js, flags=re.M)
    assert n_guards == 2, f"expected 2 write guards, removed {n_guards}"
    js, n_flags = re.subn(r", expectOk: true", "", js)
    assert n_flags == 2, f"expected 2 expectOk flags, removed {n_flags}"
    return js


class TestSourceContract(TestCase):
    def test_write_paths_check_the_result_before_toasting_success(self):
        """Acceptance 1: the guard must precede the 「已添加 / 已移除」 toast."""
        js = fc.read_app_js()
        for name, toast in (("addToWatchlist", "showToast('已添加"), ("removeFromWatchlist", "showToast('已移除")):
            body = function_body(js, name)
            guard = re.search(r"if \(!(\w+)\) return;", body)
            self.assertIsNotNone(guard, f"{name} has no return-value guard")
            assert guard is not None
            self.assertLess(guard.start(), body.index(toast),
                            f"{name}: the guard must run before the success toast")

    def test_write_requests_are_marked_as_writes(self):
        """Both write paths opt into the visible failure surface."""
        js = fc.read_app_js()
        for name in ("addToWatchlist", "removeFromWatchlist"):
            self.assertIn("expectOk: true", function_body(js, name))

    def test_api_fetch_keeps_its_return_shape(self):
        """`null` still means "not 2xx" for the dozen read call sites."""
        body = function_body(fc.read_app_js(), "apiFetch")
        self.assertEqual(body.count("return null;"), 3)  # 401, 404, catch
        self.assertIn("return res.json();", body)

    def test_read_error_is_not_the_write_surface(self):
        """`loadEarnings` clears `error`; it must not touch `writeError`."""
        js = fc.read_app_js()
        self.assertNotIn("writeError", function_body(js, "loadEarnings"))
        fetch_body = function_body(js, "apiFetch")
        self.assertIn("writeError.value =", fetch_body)

    def test_watchlist_page_has_an_error_surface_outside_the_user_branch(self):
        """A 401 clears `user`, so the explanation must render above that branch."""
        page = watchlist_page()
        self.assertIn(WRITE_ERROR_BANNER, page)
        self.assertLess(page.index(WRITE_ERROR_BANNER), page.index('<template v-if="user">'))
        self.assertIn("dismissErrors", page)

    def test_calendar_banner_shows_write_failures_too(self):
        """The calendar detail panel writes through the same paths."""
        html = fc.read_index()
        calendar = html[html.index('<!-- Calendar and selection workspace -->'):]
        self.assertIn(WRITE_ERROR_BANNER, calendar[:4000])

    def test_template_bindings_resolve(self):
        """`writeError` / `dismissErrors` must be exposed by setup()."""
        keys = fc.exposed_setup_keys(fc.read_app_js())
        self.assertIn("writeError", keys)
        self.assertIn("dismissErrors", keys)
        self.assertEqual(sorted(fc.dangling_bindings(fc.read_index(), keys)), [])


@skipUnless(shutil.which("node"), "node is required for the write-failure probe")
class TestWriteFailuresInTheRealSetup(TestCase):
    """The real setup() + a stubbed fetch: what the user would actually see."""

    def tearDown(self):
        APP_JS_PATH[0] = fc.APP_JS

    def test_401_add_does_not_claim_success(self):
        result = run_probe("toggle", {"status": 401, "body": {"detail": "Not authenticated"}})
        self.assertNotIn("已添加", result["toast"])
        self.assertIn("401", result["writeError"] or "")
        self.assertIsNone(result["user"], "a 401 must still log the SPA out")
        self.assertEqual([c["method"] for c in result["calls"]],
                         ["POST"], "a rejected write must not be followed by a refresh")

    def test_500_add_keeps_the_failure_and_stays_attributable(self):
        result = run_probe("add", {"status": 500, "body": "Internal Server Error"})
        self.assertNotIn("已添加", result["toast"])
        self.assertIn("请求失败", result["toast"], "the failure toast must survive the write path")
        self.assertIn("未生效", result["writeError"] or "")
        self.assertEqual(result["watchlist"], [])
        self.assertEqual(result["searchResults"], [], "a failed add must not clear the results")

    def test_network_failure_is_reported_too(self):
        result = run_probe("add", {"throw": "Failed to fetch"})
        self.assertNotIn("已添加", result["toast"])
        self.assertIn("Failed to fetch", result["writeError"] or "")

    def test_401_remove_does_not_claim_success(self):
        result = run_probe("remove", {"status": 401, "body": {"detail": "Not authenticated"}},
                           watchlist=[{"symbol": "AAPL", "market": "US"}])
        self.assertNotIn("已移除", result["toast"])
        self.assertIn("401", result["writeError"] or "")
        self.assertEqual(result["watchlist"], [{"symbol": "AAPL", "market": "US"}])

    def test_a_successful_write_behaves_exactly_as_before(self):
        """Acceptance 2: 2xx keeps toast + refresh, and clears the write error."""
        result = run_probe("toggle", {"status": 200, "body": {"symbol": "AAPL", "market": "US"}})
        self.assertEqual(result["toast"], "已添加 AAPL")
        self.assertIsNone(result["writeError"])
        methods = [c["method"] for c in result["calls"]]
        self.assertEqual(methods[0], "POST")
        self.assertIn("GET", methods, "a successful write still refreshes the lists")
        self.assertEqual(result["searchQuery"], "")

    def test_the_probe_sees_the_pre_fix_behaviour(self):
        """Reverse validation: un-fixed source must reproduce 「谎报成功」."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app-setup-prefix.js"
            path.write_text(prefix_source(), encoding="utf-8")
            APP_JS_PATH[0] = path
            result = run_probe("toggle", {"status": 401, "body": {"detail": "Not authenticated"}})
        self.assertEqual(result["toast"], "已添加 AAPL",
                         "the probe must observe the bug it is meant to prevent")
        self.assertIsNone(result["writeError"])
        self.assertIn("GET", [c["method"] for c in result["calls"]],
                      "the pre-fix path refreshed (and cleared `error`) after a 401")
