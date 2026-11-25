"""Issue #154: the subscription's EPS must read like the panel's EPS.

The body interpolated the raw column: ``EPS 预期：7.371337 (HKD)`` beside the
panel's ``7.37`` for the same period, and 21 of the 78 events in one production
watchlist feed carried three or more decimals — six decimals filling a phone
calendar row, reading like unprocessed data.

``_display_eps`` is the Python side of the served ``fmtNum`` (Issue #120: two
decimals, one more per decade below 0.1, capped at six).  The parity test runs the
*served* ``useFormatters()`` under node on the same values, so the two formatters
cannot drift apart with only one side updated — the technique
``test_issue120_small_eps_precision`` and ``test_issue144_ical_revenue`` use.

Covered here:

* the six values the issue quotes render to the panel's text;
* sub-cent values keep their significant digits (``-0.0151``, ``-0.00263``);
* rounding matches the served formatter, ties included;
* a whole feed carries at most one 3+-decimal EPS line, and it is ``-0.0151``.
"""
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ical import _display_eps, generate_ical  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)

#: The rows the issue quotes, beside the sub-cent cases #120 added and the
#: ordinary magnitudes the same formatter must leave alone.
PRODUCTION_VALUES = {
    "0700_estimate": 7.371337,
    "9988_estimate": 1.380084,
    "PDD_estimate": 2.656448,
    "MU_estimate": 37.91036,
    "INTC_estimate": -2.05793,
    "3896_estimate": -0.015096,
    "CRWD_estimate": 0.01733,
    "NET_estimate": -0.00263,
    "tenth": 0.1,
    "plain": 1.99,
    "rounded": 4.38908,
    "integer": 21.0,
    "zero": 0,
    "tie_sub_cent": 0.125,
    "tie_cent": 2.675,
}


def _run_fmt_num_probe(values: dict) -> dict:
    source = fc.function_source(fc.read_app_js(), "useFormatters")
    probe = f"""
{source}
const fmt = useFormatters();
const values = {json.dumps(values)};
const out = {{}};
for (const [key, value] of Object.entries(values)) out[key] = fmt.fmtNum(value);
console.log(JSON.stringify(out));
"""
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


# ── the formatter itself ───────────────────────────────────────────────────

def test_the_figures_the_issue_quotes_match_the_panel():
    assert _display_eps(7.371337) == "7.37"
    assert _display_eps(1.380084) == "1.38"
    assert _display_eps(2.656448) == "2.66"
    assert _display_eps(37.91036) == "37.91"
    assert _display_eps(-2.05793) == "-2.06"
    assert _display_eps(-0.015096) == "-0.0151"


def test_sub_cent_values_keep_their_significant_digits():
    assert _display_eps(0.01733) == "0.0173"
    assert _display_eps(-0.00263) == "-0.00263"


def test_ordinary_magnitudes_keep_two_decimals():
    assert _display_eps(1.99) == "1.99"
    assert _display_eps(4.38908) == "4.39"
    assert _display_eps(0.1) == "0.10"
    assert _display_eps(21.0) == "21.00"
    assert _display_eps(0) == "0.00"


def test_missing_values_render_empty():
    for value in (None, ""):
        assert _display_eps(value) == ""
    assert _display_eps("not-a-number") == ""


def test_decimal_columns_are_formatted_like_floats():
    """The read path hands psycopg Decimals to the feed, not floats."""
    from decimal import Decimal

    assert _display_eps(Decimal("7.371337")) == "7.37"
    assert _display_eps(Decimal("-0.015096")) == "-0.0151"


@pytest.mark.skipif(not shutil.which("node"), reason="node is required for the JS parity check")
def test_matches_the_served_formatter_value_by_value():
    """One rule, two languages: the served ``fmtNum`` decides."""
    served = _run_fmt_num_probe(PRODUCTION_VALUES)
    produced = {key: _display_eps(value) for key, value in PRODUCTION_VALUES.items()}
    assert produced == served


@pytest.mark.skipif(not shutil.which("node"), reason="node is required for the JS parity check")
def test_ties_round_the_way_toFixed_does():
    """``toFixed`` rounds the binary value, so 2.675 is 2.67; Decimal(str()) is not."""
    assert _display_eps(2.675) == "2.67"
    assert _display_eps(0.125) == "0.13"


# ── the feed ───────────────────────────────────────────────────────────────

def _row(symbol: str, estimate, actual=None, **overrides) -> dict:
    row = {
        "symbol": symbol, "market": "US", "company_name": symbol,
        "company_name_en": symbol, "report_date": date(2026, 11, 18),
        "before_after": "after", "fiscal_year": 2026, "fiscal_quarter": 3,
        "date_status": "reported", "is_predicted": False, "date_source": "futu",
        "eps_estimate": estimate, "eps_actual": actual,
        "estimate_currency": "USD", "actual_currency": "USD",
    }
    row.update(overrides)
    return row


def _eps_lines(ics: str) -> list[str]:
    flat = ics.replace("\r\n ", "")
    bodies = [line[len("DESCRIPTION:"):].replace("\\n", "\n")
              for line in flat.split("\r\n") if line.startswith("DESCRIPTION:")]
    return [line for body in bodies for line in body.split("\n")
            if line.startswith("EPS ")]


def test_a_feed_of_the_production_rows_keeps_one_three_decimal_line():
    """Acceptance 1: 21 raw lines → 1, and it is the sub-cent one."""
    raw = [7.371337, 1.380084, 2.656448, 37.91036, -2.05793, 28.880084, 1.111496,
           -0.17943, 1.52648, 5.14754, 29.12343, 0.98655, 2.91805, 4.86747,
           1.67824, 6.15646, 19.52654, 12.80865, 0.187818, -0.015096]
    rows = [_row(f"T{index:02d}", value) for index, value in enumerate(raw)]
    rows.append(_row("TAIL", 2.7, 7.059))

    lines = _eps_lines(generate_ical(rows, title_lang="zh"))
    long_lines = [line for line in lines
                  if len(re.search(r"：(-?\d+\.\d+)", line).group(1).split(".")[1]) >= 3]
    assert long_lines == ["EPS 预期：-0.0151 (USD)"]


def test_the_body_reads_like_the_panel_for_the_quoted_rows():
    ics = generate_ical([_row("0700.HK", 7.371337, 7.059,
                              market="HK", estimate_currency="HKD",
                              actual_currency="CNY")], title_lang="zh")
    lines = _eps_lines(ics)
    assert "EPS 预期：7.37 (HKD)" in lines
    assert "EPS 实际：7.06 (CNY)" in lines


def test_the_feed_still_parses_after_the_narrower_numbers():
    import icalendar

    ics = generate_ical([_row("0700.HK", 7.371337, 7.059, market="HK")], title_lang="zh")
    events = [c for c in icalendar.Calendar.from_ical(ics).walk() if c.name == "VEVENT"]
    assert len(events) == 1
    assert "EPS 预期：7.37" in str(events[0]["DESCRIPTION"])
