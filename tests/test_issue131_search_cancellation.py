"""Issue #131: a slow search response must not answer a query you have replaced.

`doSearch()` awaited `/api/search` with no cancellation, and every response wrote
`searchResults` in arrival order. Type 「0700」 then 「0700.HK」 and, if the first
request is the slower one, it lands last and the list shows results for a keyword
the box no longer holds — no spinner, no hint, just the wrong answer.

Two guards now: the in-flight request is aborted on the next keystroke, and a
response whose sequence is already stale is discarded even if the transport —
or a browser that had already decoded the body — still delivers it. A cancelled
request is not an error, so `apiFetch` returns quietly instead of raising the
「请求失败」 banner with `aborted`.

The probes run the served composables under node with a fake transport, so both
orderings (honours `signal` / ignores it) are exercised.
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

PRELUDE = r"""
const fs = require('fs');
const vm = require('vm');

const sandbox = { console, setTimeout, clearTimeout, Promise };
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.env.VUE_PATH, 'utf8'), sandbox);
const { ref, computed, watch, nextTick } = sandbox.Vue;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const localYmd = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');

function makeTransport(delays, opts) {
  const calls = [];
  const aborts = [];
  const fn = (path, init) => {
    const query = decodeURIComponent((path.split('q=')[1] || '').replace(/&.*$/, ''));
    const signal = init && init.signal;
    const delay = delays[query] === undefined ? 10 : delays[query];
    calls.push({ query, has_signal: !!signal });
    return new Promise((resolve) => {
      // Mimic `apiFetch`: an abort resolves as "no data", it does not throw —
      // that is exactly the contract the composable relies on.
      const abort = () => {
        aborts.push(query);
        clearTimeout(timer);
        resolve(null);
      };
      const timer = setTimeout(() => {
        resolve([{ symbol: query.toUpperCase(), market: 'US' }]);
      }, delay);
      if (signal && !(opts && opts.ignore_abort)) signal.addEventListener('abort', abort);
    });
  };
  fn.calls = calls;
  fn.aborts = aborts;
  return fn;
}

async function searchRace(source, delays, opts) {
  const useWatchlist = new Function('ref', 'computed', 'watch', 'nextTick', source + '\nreturn useWatchlist;')(ref, computed, watch, nextTick);
  const transport = makeTransport(delays, opts);
  const wl = useWatchlist(transport, () => {}, async () => {});
  wl.searchQuery.value = '0700';
  wl.doSearch();
  await sleep(340);                 // the debounce has fired: 「0700」 is in flight
  wl.searchQuery.value = '0700.HK';
  wl.doSearch();                    // supersedes it
  await sleep(1200);                // both have had time to land, in any order
  return {
    results: wl.searchResults.value.map(r => r.symbol),
    loading: wl.searchLoading.value,
    calls: transport.calls,
    aborts: transport.aborts,
  };
}
"""

PROBE_WATCHLIST = PRELUDE + r"""
(async () => {
  /*USE_WATCHLIST*/
  const source = WATCHLIST_SOURCE;
  const honours = await searchRace(source, { '0700': 700, '0700.HK': 60 }, { ignore_abort: false });
  const ignores = await searchRace(source, { '0700': 700, '0700.HK': 60 }, { ignore_abort: true });
  console.log(JSON.stringify({ honours, ignores }));
})();
"""

PROBE_APIFETCH = PRELUDE + r"""
(async () => {
  /*USE_APIFETCH*/
  const toasts = [];
  const writeError = ref(null);
  const error = ref(null);
  const user = ref({ id: 1 });
  const apiErrorText = (text, status) => 'HTTP ' + status;
  const fetch = () => Promise.reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
  const call = new Function(
    'ref', 'fetch', 'writeError', 'error', 'user', 'apiErrorText', 'showToast',
    API_FETCH_SOURCE + '\nreturn apiFetch;'
  )(ref, fetch, writeError, error, user, apiErrorText, (m) => toasts.push(m));
  const out = await call('/api/search?q=0700', { signal: { aborted: true } });
  console.log(JSON.stringify({ out, error: error.value, write_error: writeError.value, toasts }));
})();
"""


def _run(probe: str, replacements: dict) -> dict:
    script = probe
    for placeholder, source in replacements.items():
        script = script.replace(placeholder, source)
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=120,
        env={**os.environ, "VUE_PATH": str(VENDOR_VUE)},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _watchlist_probe() -> dict:
    js = fc.read_app_js()
    source = fc.function_source(js, "useWatchlist")
    # The composable is injected into a fresh scope; keep it as a literal string.
    script = PROBE_WATCHLIST.replace("/*USE_WATCHLIST*/", "const WATCHLIST_SOURCE = " + json.dumps(source) + ";")
    script = script.replace("WATCHLIST_SOURCE", "WATCHLIST_SOURCE")
    return _run(script, {})


def _apifetch_probe() -> dict:
    js = fc.read_app_js()
    source = fc.function_source(js, "apiFetch")
    script = PROBE_APIFETCH.replace("/*USE_APIFETCH*/", "const API_FETCH_SOURCE = " + json.dumps(source) + ";")
    return _run(script, {})


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class SearchCancellationTests(TestCase):
    def test_the_superseded_request_is_aborted(self):
        out = _watchlist_probe()["honours"]
        self.assertEqual([c["query"] for c in out["calls"]], ["0700", "0700.HK"])
        self.assertTrue(out["calls"][0]["has_signal"], "the request must carry an AbortSignal")
        self.assertEqual(out["aborts"], ["0700"], "the older query must be cancelled")

    def test_the_last_query_owns_the_results(self):
        honours = _watchlist_probe()["honours"]
        self.assertEqual(honours["results"], ["0700.HK"])
        self.assertFalse(honours["loading"], "the spinner must not be left on")

    def test_a_late_answer_is_discarded_even_when_it_cannot_be_aborted(self):
        """A transport (or a decoded body) that ignores `signal` must still lose."""
        ignores = _watchlist_probe()["ignores"]
        self.assertEqual(ignores["aborts"], [], "this transport does not honour the signal")
        self.assertEqual(ignores["results"], ["0700.HK"], "the stale response overwrote the newer one")

    def test_a_cancelled_request_is_not_reported_as_a_failure(self):
        out = _apifetch_probe()
        self.assertIsNone(out["out"], "a cancelled read yields no data")
        self.assertIsNone(out["error"], "no page-level error for the reader's own next keystroke")
        self.assertIsNone(out["write_error"])
        self.assertEqual(out["toasts"], [], "no 「请求失败」 toast for an abort")


class SourcePins(TestCase):
    def test_api_fetch_swallows_aborts_before_the_error_paths(self):
        js = fc.read_app_js()
        catch = js[js.index("} catch (e) {", js.index("async function apiFetch")):]
        catch = catch[:catch.index("return null;") + len("return null;")]
        self.assertIn("if (e && e.name === 'AbortError') return null;", catch)

    def test_the_search_aborts_before_it_debounces(self):
        js = fc.read_app_js()
        body = js[js.index("async function doSearch()"):]
        body = body[:body.index("}, 300);")]
        self.assertLess(body.index("searchController.abort()"), body.index("setTimeout"))
        self.assertIn("if (seq !== searchSeq) return;", body)
        self.assertIn("{ signal: controller.signal }", body)
