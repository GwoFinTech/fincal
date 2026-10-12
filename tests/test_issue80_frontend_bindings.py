"""Issue #80: the calendar's 「只看自选」 checkbox must really re-request.

``index.html`` binds ``@change="loadEarnings"``, but ``loadEarnings`` lived
only inside ``setup()`` and was never returned, so the runtime-compiled
template resolved it to ``undefined``: ``@change`` attached no listener and
the filter looked broken.  Vue's public-instance proxy answers ``true`` to
``has`` for every key that does not start with ``_``, so this fails silently —
no exception, no console output — which is why the old test asserting the
binding *string* stayed green.

Two checks replace that assertion:

* the template's root identifiers are compared with the set ``setup()``
  exposes (``tests/frontend_contract.py``); and
* the real markup is mounted with the vendored Vue 3.4.38 build under node,
  counting the ``change`` listeners Vue actually attaches to the ``<input>``.
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

VENDOR_VUE = fc.ROOT / "app" / "static" / "assets" / "vendor" / "vue.global.prod.js"

CHECKBOX = re.compile(r'<input class="ui-checkbox"[^>]*/>')

# Mounts the real markup with the vendored Vue build and counts the change
# listeners on the <input>.  A custom renderer replaces the DOM so the probe
# needs no browser and no headless shell, while the compilation path (runtime
# compiler + the `with (_ctx)` proxy) is exactly the browser's.
PROBE = r"""
const fs = require('fs');
const vm = require('vm');

const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.env.VUE_PATH, 'utf8'), sandbox);
const Vue = sandbox.Vue;

const created = [];
function makeEl(tag) {
  const el = { tag, listeners: {} };
  el.addEventListener = (type, fn) => {
    (el.listeners[type] = el.listeners[type] || []).push(fn);
  };
  el.removeEventListener = (type, fn) => {
    const all = el.listeners[type] || [];
    const index = all.indexOf(fn);
    if (index >= 0) all.splice(index, 1);
  };
  created.push(el);
  return el;
}

const patchCalls = [];
const nodeOps = {
  patchProp(el, key, prev, next) {
    patchCalls.push({
      key: String(key),
      functions: typeof next === 'function'
        ? 1
        : (Array.isArray(next) ? next.filter(f => typeof f === 'function').length : 0),
    });
  },
  insert() {}, remove() {}, setText() {}, setElementText() {},
  createElement: makeEl, createText: text => ({ text }), createComment: text => ({ text }),
  parentNode: () => null, nextSibling: () => null,
  querySelector: () => null, setScopeId() {},
};

const setupState = Object.fromEntries(
  JSON.parse(process.env.KEYS).map(key => [key, () => 'stub'])
);

let thrown = null;
try {
  Vue.createRenderer(nodeOps)
    .createApp({ template: process.env.SNIPPET, setup: () => setupState })
    .mount({});
} catch (error) {
  thrown = String((error && error.message) || error);
}

const input = created.find(el => el.tag === 'input');
const changeProps = patchCalls.filter(call => call.key.toLowerCase() === 'onchange');
const changeListeners = input ? (input.listeners.change || []).length : 0;
console.log(JSON.stringify({
  thrown,
  patch_keys: patchCalls.map(call => call.key),
  change_prop_handlers: changeProps.reduce((total, call) => total + call.functions, 0),
  element_change_listeners: changeListeners,
  total_change_handlers:
    changeListeners + changeProps.reduce((total, call) => total + call.functions, 0),
}));
"""


def checkbox_markup() -> str:
    """The calendar checkbox exactly as the served page renders it."""
    match = CHECKBOX.search(fc.read_index())
    assert match, "the 「只看自选」 checkbox markup is missing from index.html"
    return match.group(0)


def _run_probe(snippet: str, keys: list[str]) -> dict:
    env = {**os.environ, "VUE_PATH": str(VENDOR_VUE), "SNIPPET": snippet,
           "KEYS": json.dumps(keys)}
    completed = subprocess.run(
        ["node", "-e", PROBE], capture_output=True, text=True, timeout=60, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


class TestExposedBindings(TestCase):
    def test_setup_exposes_the_checkbox_change_handler(self):
        """Acceptance 1: `loadEarnings` is in the returned object."""
        keys = fc.exposed_setup_keys(fc.read_app_js())
        self.assertIn("loadEarnings", keys)
        self.assertIn('@change="loadEarnings"', checkbox_markup())

    def test_the_exposed_set_includes_the_spread_formatters(self):
        """The set is complete: `...fmt` resolves to `useFormatters()`' keys.

        Without this the dangling-binding check could pass simply because the
        formatters were missing from the exposed side too.
        """
        keys = fc.exposed_setup_keys(fc.read_app_js())
        for name in ("fmtNum", "fmtPct", "periodLabel", "growthCell"):
            self.assertIn(name, keys)

    def test_the_checkbox_binding_resolves_against_the_exposed_set(self):
        keys = fc.exposed_setup_keys(fc.read_app_js())
        roots: set[str] = set()
        for root_set in fc.template_roots(checkbox_markup()).values():
            roots |= root_set
        self.assertIn("loadEarnings", roots)
        self.assertTrue(roots <= keys | fc.TEMPLATE_GLOBALS, sorted(roots - keys))

    def test_no_template_binding_is_dangling(self):
        """Every template root is exposed, a v-for local, or a Vue/JS global."""
        keys = fc.exposed_setup_keys(fc.read_app_js())
        self.assertEqual(sorted(fc.dangling_bindings(fc.read_index(), keys)), [])

    def test_the_check_fails_when_a_binding_is_not_exposed(self):
        """Reverse validation: the same check must catch the pre-fix shape.

        Dropping `loadEarnings` from the exposed set (which is what HEAD was)
        must be reported, and a template naming an unknown root must be
        reported too — otherwise the check above proves nothing.
        """
        html = fc.read_index()
        keys = fc.exposed_setup_keys(fc.read_app_js())
        self.assertEqual(
            sorted(fc.dangling_bindings(html, keys - {"loadEarnings"})),
            ["loadEarnings"],
        )
        self.assertEqual(
            sorted(fc.dangling_bindings('<div @change="notExposed"></div>', keys)),
            ["notExposed"],
        )

    def test_v_for_aliases_and_globals_do_not_mask_drift(self):
        html = fc.read_index()
        keys = fc.exposed_setup_keys(fc.read_app_js())
        self.assertFalse(
            fc.v_for_locals(html) & keys,
            "a v-for alias shadowing an exposed name would hide real drift",
        )
        self.assertFalse(keys & fc.TEMPLATE_GLOBALS)


@skipUnless(shutil.which("node"), "node is required for the binding check")
class TestMountingTheRealMarkup(TestCase):
    def test_the_checkbox_gets_both_change_handlers(self):
        """Acceptance 2: 2 change handlers with the fix, 1 without it."""
        keys = sorted(fc.exposed_setup_keys(fc.read_app_js()))
        snippet = checkbox_markup()

        with_fix = _run_probe(snippet, keys)
        self.assertIsNone(with_fix["thrown"], with_fix["thrown"])
        self.assertIn("onUpdate:modelValue", with_fix["patch_keys"])
        self.assertEqual(with_fix["change_prop_handlers"], 1)
        self.assertEqual(with_fix["element_change_listeners"], 1)
        self.assertEqual(
            with_fix["total_change_handlers"], 2,
            "the @change handler must join the v-model listener on the input",
        )

        without_fix = _run_probe(snippet, [k for k in keys if k != "loadEarnings"])
        self.assertIsNone(without_fix["thrown"], without_fix["thrown"])
        self.assertEqual(
            without_fix["total_change_handlers"], 1,
            "the probe must be able to observe the pre-fix shape (v-model only)",
        )
        self.assertLess(without_fix["total_change_handlers"],
                        with_fix["total_change_handlers"])
