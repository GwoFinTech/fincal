"""Issue #109: the subscription dialog must not hand out an empty calendar.

``scope=watchlist`` on an empty watchlist is a *valid* feed with zero events
(Issue #25 keeps that contract), and the dialog used to hardcode that scope as
its opening default.  Every first-time subscriber therefore copied
``?scope=watchlist`` — a 248-byte VCALENDAR with 0 VEVENTs — minutes after
seeing 491 events in the app, and a calendar client polling it every 12 h
would never show an event, an error, or a hint.  Production had 0/4 real users
with a watchlist at the time.

What is pinned here:

* the opening default follows the watchlist — empty (and *known* empty) →
  ``all``, non-empty → ``watchlist`` with the exact address today's code
  produced;
* "not read yet" is not "empty": the decision waits for ``watchlistLoaded``;
* a scope chosen by hand survives closing and reopening the dialog;
* the empty-watchlist warning exists, is announced (``aria-live``) and cannot
  cover the copy button;
* nothing about the feed itself changed (``scope=watchlist`` + empty watchlist
  still yields the empty calendar — ``tests/test_ical_subscription.py``).

The behaviour is exercised by loading the real ``useWatchlist`` / ``useIcal``
composables from ``app/static/assets/app-setup.js`` into a node vm with the
vendored Vue build, then driving the same calls the template makes.  A
mutated copy of the file (the scope decision removed) must reproduce the old
address, so the check is known to be able to fail.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)

ROOT = fc.ROOT
APP_JS = fc.APP_JS
INDEX_HTML = fc.INDEX_HTML
VENDOR_VUE = ROOT / "app" / "static" / "assets" / "vendor" / "vue.global.prod.js"

TOKEN_URL = "https://fincal.test/ical/tok-109"
EXPECTED_WATCHLIST_URL = TOKEN_URL + "?lang=zh&scope=watchlist&predicted=1&markets=all"
EXPECTED_ALL_URL = TOKEN_URL + "?lang=zh&scope=all&predicted=1&markets=all"

# The one line the default depends on; removed in the reverse-validation copy.
SCOPE_DECISION = (
    "icalOptions.value.scope = watchlist.value.length === 0 ? 'all' : 'watchlist';"
)

PROBE = r"""
const fs = require('fs');
const vm = require('vm');

const sandbox = {
  console, setTimeout, clearTimeout, URL, URLSearchParams,
  Promise, JSON, Math, Date, String, Number, Object, Array, Boolean, Intl,
  Error, TypeError,
  ICAL_TOKEN_URL: process.env.ICAL_TOKEN_URL,
};
sandbox.window = { location: { origin: 'https://fincal.test' } };
sandbox.__copied = [];
sandbox.navigator = { clipboard: { writeText: (t) => sandbox.__copied.push(t) } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.env.VUE_PATH, 'utf8'), sandbox);

const js = fs.readFileSync(process.env.APP_JS, 'utf8');
const marker = '\n  createApp({';
const cut = js.indexOf(marker);
if (cut < 0) throw new Error('app wiring marker not found in app-setup.js');
// Everything before the wiring block: the composables, verbatim.
const prefix = js.slice(0, cut);

// Driven exactly like the template does: the button calls openIcal(), the
// 复制 button calls copyIcal(), the scope select calls markScopeChosen().
const driver = `
(async () => {
  function harness(rows) {
    const fetched = [];
    const apiFetch = async (path) => {
      fetched.push(path);
      if (rows === 'null') return null;   // apiFetch's failure shape, not a throw
      return rows;
    };
    const wl = useWatchlist(apiFetch, () => {}, async () => {});
    const user = ref({ ical_url: ICAL_TOKEN_URL });
    const ical = useIcal(user, wl.watchlist, wl.watchlistLoaded);
    return { wl, ical, fetched };
  }

  const out = {};
  for (const [name, rows] of [['empty', []], ['one', [{ symbol: 'AAPL', market: 'US' }]], ['failed', 'null']]) {
    const h = harness(rows);
    const rawDefault = h.ical.icalOptions.value.scope;   // before openIcal(): the old default
    const loadedBefore = h.wl.watchlistLoaded.value;     // must start false
    h.ical.openIcal();                                   // click before the read returns
    const scopeBeforeLoad = h.ical.icalOptions.value.scope;
    h.ical.showIcalModal.value = false;
    await h.wl.loadWatchlist();
    h.ical.openIcal();                                   // click after the read returns
    h.ical.copyIcal();                                   // the 复制 button
    out[name] = {
      raw_default: rawDefault,
      loaded_before_load: loadedBefore,
      loaded: h.wl.watchlistLoaded.value,
      scope_before_load: scopeBeforeLoad,
      scope: h.ical.icalOptions.value.scope,
      url: h.ical.icalUrl.value,
      copied: __copied[__copied.length - 1],
      rows: h.wl.watchlist.value.length,
      fetched: h.fetched,
    };
  }

  // A hand-made choice must survive close/reopen.
  const empty = harness([]);
  await empty.wl.loadWatchlist();
  empty.ical.openIcal();
  out.explicit = { auto: empty.ical.icalOptions.value.scope };
  empty.ical.icalOptions.value.scope = 'watchlist';
  empty.ical.markScopeChosen();
  empty.ical.showIcalModal.value = false;
  empty.ical.openIcal();
  out.explicit.after_reopen = empty.ical.icalOptions.value.scope;

  const filled = harness([{ symbol: 'AAPL', market: 'US' }]);
  await filled.wl.loadWatchlist();
  filled.ical.openIcal();
  out.explicit_all = { auto: filled.ical.icalOptions.value.scope };
  filled.ical.icalOptions.value.scope = 'all';
  filled.ical.markScopeChosen();
  filled.ical.showIcalModal.value = false;
  filled.ical.openIcal();
  out.explicit_all.after_reopen = filled.ical.icalOptions.value.scope;

  return out;
})()
`;

vm.runInContext(prefix + '\n' + driver, sandbox, { filename: 'app-setup.js' })
  .then((out) => console.log(JSON.stringify(out)))
  .catch((error) => {
    console.error(String((error && error.message) || error));
    process.exit(1);
  });
"""

requires_node = pytest.mark.skipif(
    not shutil.which("node") or not VENDOR_VUE.exists(),
    reason="node and the vendored Vue build are needed for the composable probe",
)


def _run_probe(app_js: Path = APP_JS) -> dict:
    env = {**os.environ, "VUE_PATH": str(VENDOR_VUE), "APP_JS": str(app_js),
           "ICAL_TOKEN_URL": TOKEN_URL}
    completed = subprocess.run(
        ["node", "-e", PROBE], capture_output=True, text=True, timeout=120, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def probe() -> dict:
    return _run_probe()


# ── Acceptance 1/2: the opening default follows the watchlist ──────────────

@requires_node
def test_empty_watchlist_opens_on_the_system_universe(probe):
    """Empty + loaded → ``scope=all``, and the copied address says so."""
    empty = probe["empty"]
    assert empty["raw_default"] == "watchlist", "the old hardcoded default must still be visible pre-open"
    assert empty["rows"] == 0
    assert empty["scope_before_load"] == "watchlist", (
        "a watchlist that has not been read must not be treated as empty"
    )
    assert empty["loaded_before_load"] is False
    assert empty["loaded"] is True
    assert empty["scope"] == "all"
    assert empty["url"] == EXPECTED_ALL_URL
    assert empty["copied"] == EXPECTED_ALL_URL, "the 复制 button must hand out the non-empty scope"
    assert empty["fetched"] == ["/api/watchlist"]


@requires_node
def test_watchlist_users_keep_todays_address_byte_for_byte(probe):
    """Non-empty → ``watchlist``, same bytes as before the change."""
    one = probe["one"]
    assert one["rows"] == 1
    assert one["scope"] == "watchlist"
    assert one["url"] == EXPECTED_WATCHLIST_URL
    assert one["copied"] == EXPECTED_WATCHLIST_URL


@requires_node
def test_a_failed_watchlist_read_is_not_treated_as_a_choice(probe):
    """``apiFetch`` failure leaves an empty list; the flag still flips.

    The first read is the one that matters: a client with a real watchlist but
    a failed ``/api/watchlist`` would otherwise copy the empty feed — the exact
    symptom this issue is about — instead of the calendar it just displayed.
    """
    failed = probe["failed"]
    assert failed["rows"] == 0
    assert failed["loaded"] is True
    assert failed["scope"] == "all"
    assert failed["copied"] == EXPECTED_ALL_URL


@requires_node
def test_a_hand_made_scope_survives_reopening(probe):
    """Auto-defaulting must never overwrite the reader's own choice."""
    assert probe["explicit"] == {"auto": "all", "after_reopen": "watchlist"}
    assert probe["explicit_all"] == {"auto": "watchlist", "after_reopen": "all"}


@requires_node
def test_the_probe_fails_on_the_prefix_shape(tmp_path):
    """Reverse validation: dropping the decision reproduces the old address.

    Same source, one line removed → the empty-watchlist user is back on the
    0-event calendar.  Without this the assertions above could pass on code
    that never consults the watchlist at all.
    """
    source = APP_JS.read_text(encoding="utf-8")
    assert SCOPE_DECISION in source, "the default decision moved — update this test's marker"
    mutated = tmp_path / "app-setup-prefix.js"
    mutated.write_text(source.replace(SCOPE_DECISION, "/* pre-fix */"), encoding="utf-8")

    prefix = _run_probe(mutated)
    assert prefix["empty"]["scope"] == "watchlist"
    assert prefix["empty"]["copied"] == EXPECTED_WATCHLIST_URL
    # ... and only the empty case differs; the non-empty default is unchanged.
    assert prefix["one"]["copied"] == EXPECTED_WATCHLIST_URL


# ── Acceptance 3: the empty-watchlist warning ──────────────────────────────

def _modal_markup() -> str:
    """The scope selector down to (and including) the copy button."""
    html = fc.read_index()
    start = html.index('<select v-model="icalOptions.scope"')
    end = html.index("</button>", html.index("{{ copied", start)) + len("</button>")
    return html[start:end]


def test_the_warning_sits_between_the_scope_and_the_address():
    markup = _modal_markup()
    assert 'aria-live="polite"' in markup
    assert "0 个事件" in markup
    assert "您的自选为空" in markup
    # Gated on "known empty", not on "no rows yet".
    assert 'v-if="icalOptions.scope === \'watchlist\' && watchlistLoaded && watchlist.length === 0"' in markup
    # DOM order: below the select, above the 订阅地址 input + 复制 button, so it
    # can never cover the copy control.
    assert markup.index("aria-live") < markup.index("订阅地址")
    assert "复制" in markup


def test_the_warning_is_absent_from_the_prefix_markup():
    """Reverse validation: the pre-fix dialog had no warning at all."""
    body = _modal_markup()
    before = re.sub(r"\s*<!-- Issue #109.*?-->", "", body, flags=re.S)
    before = re.sub(r'<p v-if="icalOptions\.scope.*?</p>', "", before, flags=re.S)
    assert "aria-live" not in before
    assert "0 个事件" not in before


# ── Wiring: the template can actually reach the new functions ──────────────

def test_the_subscribe_button_goes_through_open_ical():
    html = fc.read_index()
    button = re.search(
        r"<button[^>]*>(?:(?!</button>).)*订阅日历(?:(?!</button>).)*</button>", html, re.S)
    assert button, "the 订阅日历 button is missing"
    assert '@click="openIcal()"' in button.group(0)
    assert "showIcalModal = true" not in button.group(0)


def test_the_scope_selector_marks_a_hand_made_choice():
    markup = _modal_markup()
    assert '@change="markScopeChosen"' in markup


def test_the_new_bindings_resolve():
    keys = fc.exposed_setup_keys(fc.read_app_js())
    for name in ("openIcal", "markScopeChosen", "watchlistLoaded"):
        assert name in keys, name
    assert fc.dangling_bindings(fc.read_index(), keys) == set()


def test_the_composables_are_wired_to_the_watchlist():
    js = fc.read_app_js()
    assert "function useIcal(user, watchlist, watchlistLoaded)" in js
    assert "useIcal(user, wl.watchlist, wl.watchlistLoaded)" in js
    assert "watchlistLoaded.value = true" in js


# ── Acceptance 4: the server contract is untouched ────────────────────────

def test_the_feed_router_is_unchanged_by_this_issue():
    """No server-side fallback: an empty watchlist keeps yielding an empty feed."""
    src = (ROOT / "app" / "routers" / "ical.py").read_text(encoding="utf-8")
    assert "if not syms" in src
    assert "generate_ical([]" in src
    # The dialog decides; the feed does not silently widen its own scope.
    assert "scope == \"watchlist\" and not syms" not in src
