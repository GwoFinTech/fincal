"""Issue #121: a metric with no comparable snapshots must not be called 「持平」.

``app/phase3.py::_series_change()`` returns ``direction: "unavailable"`` when a
metric has fewer than two non-null snapshots, while ``revision_trend().status``
is ``available`` as soon as the *snapshot list* has two rows.  The panel gated
the whole block on ``status`` and then mapped the per-metric direction with a
ternary that only knew ``up``/``down``:

    direction === 'up' ? '上修' : direction === 'down' ? '下修' : '持平'

so ``unavailable`` came out as 「持平」 — a claim that sell-side estimates did
not move, made about a comparison that never happened.  The revenue half of the
same row printed 「—」, contradicting its neighbour.

Two checks: the served helper's four outputs, and that the template no longer
reaches 「持平」 through a fallback branch.
"""
import json
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

DIRECTIONS = ("up", "down", "flat", "unavailable")


def _run_direction_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useFormatters")
    probe = f"""
{source}
const fmt = useFormatters();
const out = {{}};
for (const d of {json.dumps(list(DIRECTIONS))}) out[d] = fmt.revisionDirectionLabel(d);
out.comparable_up = fmt.revisionIsComparable({{ direction: 'up' }});
out.comparable_flat = fmt.revisionIsComparable({{ direction: 'flat' }});
out.comparable_unavailable = fmt.revisionIsComparable({{ direction: 'unavailable' }});
out.comparable_missing = fmt.revisionIsComparable(null);
console.log(JSON.stringify(out));
"""
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class RevisionDirectionLabelTests(TestCase):
    def test_the_three_measured_directions_keep_their_words(self):
        out = _run_direction_probe()
        self.assertEqual(out["up"], "上修")
        self.assertEqual(out["down"], "下修")
        self.assertEqual(out["flat"], "持平")

    def test_an_unmeasured_metric_never_reads_as_unchanged(self):
        out = _run_direction_probe()
        self.assertNotIn("持平", out["unavailable"])
        self.assertIn("无历史样本", out["unavailable"])

    def test_only_a_measured_series_is_rendered_as_a_pair(self):
        out = _run_direction_probe()
        self.assertTrue(out["comparable_up"])
        self.assertTrue(out["comparable_flat"])
        self.assertFalse(out["comparable_unavailable"])
        self.assertFalse(out["comparable_missing"])


def test_the_panel_does_not_fall_back_to_unchanged_for_an_unknown_direction():
    """The old ternary chain sent every non-``up``/``down`` value to 「持平」."""
    html = fc.read_index()
    assert "? '上修' :" not in html, "the ternary chain that swallowed `unavailable` is back"
    assert re.search(r"\?\s*'上修'", html) is None

    # Both metrics of the panel must route through the helper, each with its own
    # direction — the previous markup read only `...eps.direction`.
    for metric in ("eps", "revenue"):
        assert f"revisionDirectionLabel(decision.revision_trend.{metric}.direction)" in html, metric
        assert f"revisionIsComparable(decision.revision_trend.{metric})" in html, metric
