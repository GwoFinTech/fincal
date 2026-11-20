"""Issue #143: 「预期修正趋势」 must not print one number twice beside a direction.

``app/phase3.py::_series_change()`` derives the direction from the raw difference,
while the panel rendered the two ends through ``fmtNum`` / ``fmtBigNum`` — two
decimals from 0.1 up.  A revision smaller than that resolution printed as the
same string at both ends while the brackets still claimed 上修/下调:

    EPS（首笔 → 最新）   1.42 → 1.42（下修）      # AMD 26Q3, 1.4197 → 1.41762

960 EPS pairs and 1,085 revenue pairs in the 2026-11 snapshot read that way.
#120 fixed a single value collapsing to zero; this is two values collapsing
into each other.

The rule pinned here is the served one: ``revisionPair()`` adds one digit at a
time — starting where the default formatter stopped, capped at six decimals —
and *only* when both ends render identically while the raw values differ.  Every
other row must come out byte-identical to the old two-call rendering, so the
probe asserts the plain rendering as hard-coded strings, not by recomputing it
with the same helper.

The assertions run ``useFormatters()`` under node (the bundle the browser
actually receives) instead of restating the formula in Python.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import TestCase, skipUnless

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)

#: ``(first, latest, big)`` — the AMD pair from the issue, pairs whose default
#: rendering already differs (must not move), and the two non-collision cases.
CASES = {
    "amd_eps": ["1.4197", "1.41762", False],
    "amd_revenue": ["13060000000", "13090000000", True],
    "b_tier_collision": ["1234567890", "1234999000", True],
    "m_tier_collision": ["1234000", "1249000", True],
    "missing_first": [None, "1.23", False],
    "missing_latest": ["1.23", None, False],
    "flat": ["1.5", "1.5", False],
    "flat_big": ["13060000000", "13060000000", True],
    "ordinary_eps": ["1.99", "2.15", False],
    "sub_cent_collision": ["-0.0026301", "-0.0026302", False],
    "bare_number_collision": ["123.2421", "123.2422", True],
}


def _run_pair_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useFormatters")
    literals = json.dumps(CASES)
    probe = f"""
{source}
const fmt = useFormatters();
const cases = {literals};
const out = {{}};
for (const [key, [first, latest, big]] of Object.entries(cases)) {{
  out[key] = {{
    pair: fmt.revisionPair(first, latest, big),
    plain: (big ? fmt.fmtBigNum(first) : fmt.fmtNum(first)) + ' → '
         + (big ? fmt.fmtBigNum(latest) : fmt.fmtNum(latest)),
  }};
}}
console.log(JSON.stringify(out));
"""
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class RevisionPairTests(TestCase):
    def test_the_amd_row_stops_contradicting_itself(self):
        """The premise first: the default formatter really does collide here."""
        out = _run_pair_probe()
        self.assertEqual(out["amd_eps"]["plain"], "1.42 → 1.42")
        first, latest = out["amd_eps"]["pair"].split(" → ")
        self.assertNotEqual(first, latest)
        # Three decimals are the first that separate the two ends — the smallest
        # step that makes the 「下修」 label true.
        self.assertEqual(out["amd_eps"]["pair"], "1.420 → 1.418")

    def test_a_revenue_pair_keeps_its_old_rendering(self):
        out = _run_pair_probe()
        self.assertEqual(out["amd_revenue"]["pair"], "13.06B → 13.09B")
        self.assertEqual(out["amd_revenue"]["pair"], out["amd_revenue"]["plain"])

    def test_a_collision_steps_past_whatever_precision_the_tier_already_used(self):
        """B tier starts at two decimals, so the *third* digit may still collide."""
        out = _run_pair_probe()
        self.assertEqual(out["b_tier_collision"]["plain"], "1.23B → 1.23B")
        first, latest = out["b_tier_collision"]["pair"].split(" → ")
        self.assertNotEqual(first, latest)
        self.assertEqual(out["b_tier_collision"]["pair"], "1.2346B → 1.2350B")

    def test_an_m_tier_collision_also_gets_a_digit(self):
        out = _run_pair_probe()
        self.assertEqual(out["m_tier_collision"]["plain"], "1.2M → 1.2M")
        self.assertEqual(out["m_tier_collision"]["pair"], "1.23M → 1.25M")

    def test_an_ordinary_row_is_byte_identical_to_the_old_two_calls(self):
        out = _run_pair_probe()
        self.assertEqual(out["ordinary_eps"]["pair"], out["ordinary_eps"]["plain"])

    def test_a_missing_end_keeps_its_dash(self):
        out = _run_pair_probe()
        self.assertEqual(out["missing_first"]["pair"], "— → 1.23")
        self.assertEqual(out["missing_latest"]["pair"], "1.23 → —")

    def test_flat_is_allowed_to_print_the_same_number_twice(self):
        """The label is 「持平」 there — one number twice is the truth."""
        out = _run_pair_probe()
        self.assertEqual(out["flat"]["pair"], "1.50 → 1.50")
        self.assertEqual(out["flat_big"]["pair"], "13.06B → 13.06B")

    def test_the_cap_is_six_decimals_and_a_pair_below_it_stays_as_printed(self):
        """Sub-cent values are already at five decimals; 6 is where #120 stops.

        Two values that still collide at six decimals keep the plain rendering:
        the cap is deliberate (another digit would be noise everywhere else).
        """
        out = _run_pair_probe()
        self.assertEqual(out["sub_cent_collision"]["plain"], "-0.00263 → -0.00263")
        self.assertEqual(out["sub_cent_collision"]["pair"], "-0.00263 → -0.00263")
        # Below 1e3 the big formatter has no unit and no fixed precision, so the
        # ladder starts at one digit and does resolve this pair.
        first, latest = out["bare_number_collision"]["pair"].split(" → ")
        self.assertNotEqual(first, latest)


class TemplateWiringTests(TestCase):
    def setUp(self):
        self.html = fc.read_index()

    def test_both_metrics_render_through_the_paired_formatter(self):
        self.assertIn(
            "revisionPair(decision.revision_trend.eps.first, decision.revision_trend.eps.latest)",
            self.html,
        )
        self.assertIn(
            "revisionPair(decision.revision_trend.revenue.first, decision.revision_trend.revenue.latest, true)",
            self.html,
        )

    def test_the_two_independent_calls_are_gone(self):
        """Printing the ends through two separate calls is the defect itself."""
        for metric, fn in (("eps", "fmtNum"), ("revenue", "fmtBigNum")):
            self.assertNotIn(f"{fn}(decision.revision_trend.{metric}.first)", self.html)
            self.assertNotIn(f"{fn}(decision.revision_trend.{metric}.latest)", self.html)

    def test_the_helper_is_exposed_to_the_template(self):
        js = fc.read_app_js()
        self.assertIn("revisionPair, revisionDirectionLabel", js)
        self.assertIn("revisionPair", fc.exposed_setup_keys(js))
        self.assertEqual(fc.dangling_bindings(self.html, fc.exposed_setup_keys(js)), set())
