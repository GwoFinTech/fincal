"""Issue #154: the body must print the pair the panel prints, and say so.

``attach_provider_pairs`` has been on the iCal read path since #147/#148, but
``app/ical.py`` never read the adopted fields: it interpolated
``eps_estimate``/``revenue_estimate`` and the row-level
``comparison_unavailable_reason``, which is computed *before* adoption and still
says ``currency_mismatch``.  So one 0700.HK period read

    营收预期：247.54B (HKD)   EPS 预期：7.371337 (HKD)
    对比：不可用（币种不同；currency_mismatch）

in the subscription while the panel, from the same row, printed
``210.24B CNY → 207.34B CNY``, ``6.29 CNY → 6.95 CNY (+10.5%)``.

These tests pin the panel's rule on the Python side (``fiscal.metric_value`` /
``metric_currency`` — pair first, row column second) and the body-level
consequences: the pair's figures, the pair's currency, and a 「对比」 line that
describes what is printed instead of denying it.
"""
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fiscal  # noqa: E402
from app.ical import generate_ical  # noqa: E402

#: 0700.HK 2026Q3 exactly as production hands it to the feed: the calendar columns
#: in the quote currency (HKD) against the CNY statement, plus the provider's own
#: CNY pair that #147 attached.
TENCENT = {
    "symbol": "0700.HK", "market": "HK",
    "company_name": "腾讯控股", "company_name_en": "Tencent Holdings",
    "report_date": date(2026, 11, 18), "before_after": "after",
    "fiscal_year": 2026, "fiscal_quarter": 3,
    "date_status": "reported", "is_predicted": False, "date_source": "futu",
    "eps_estimate": 7.371337, "eps_actual": 7.059,
    "revenue_estimate": 247536819838.46436, "revenue_actual": 207336000000.0,
    "estimate_currency": "HKD", "actual_currency": "CNY",
    "comparison_unavailable_reason": "currency_mismatch",
    "eps_pair_estimate": 6.2873, "eps_pair_actual": 6.947,
    "eps_pair_currency": "CNY", "eps_pair_analysts": 7, "eps_pair_source": "futu",
    "revenue_pair_estimate": 210239931880.0, "revenue_pair_actual": 207336000000.0,
    "revenue_pair_currency": "CNY", "revenue_pair_analysts": 30,
    "revenue_pair_source": "futu",
}


def _row(**overrides) -> dict:
    row = dict(TENCENT)
    row.update(overrides)
    return row


def _lines(ics: str) -> list[str]:
    flat = ics.replace("\r\n ", "")
    bodies = [line[len("DESCRIPTION:"):].replace("\\n", "\n")
              .replace("\\;", ";").replace("\\,", ",")
              for line in flat.split("\r\n") if line.startswith("DESCRIPTION:")]
    return [line for body in bodies for line in body.split("\n") if line]


# ── the rule itself ────────────────────────────────────────────────────────

class TestPairFirstValues:
    def test_the_pair_is_what_the_panel_shows(self):
        assert fiscal.metric_pair_values(_row(), fiscal.METRIC_EPS) == {
            "estimate": 6.2873, "actual": 6.947, "currency": "CNY",
            "num_analysts": 7, "source": "futu"}
        assert fiscal.metric_value(_row(), fiscal.METRIC_EPS, "estimate") == 6.2873
        assert fiscal.metric_value(_row(), fiscal.METRIC_EPS, "actual") == 6.947
        assert fiscal.metric_currency(_row(), fiscal.METRIC_EPS, "estimate") == "CNY"

    def test_without_a_pair_the_row_column_is_the_figure(self):
        row = {key: value for key, value in _row().items() if not key.startswith("eps_pair_")}
        assert fiscal.metric_value(row, fiscal.METRIC_EPS, "estimate") == 7.371337
        assert fiscal.metric_currency(row, fiscal.METRIC_EPS, "actual") == "CNY"
        assert fiscal.metric_currency(row, fiscal.METRIC_EPS, "estimate") == "HKD"

    def test_use_pair_false_keeps_the_row_column(self):
        """The withheld-actual path (#111) must not adopt half a pair."""
        assert fiscal.metric_value(_row(), fiscal.METRIC_EPS, "estimate",
                                   use_pair=False) == 7.371337
        assert fiscal.metric_currency(_row(), fiscal.METRIC_EPS, "estimate",
                                      use_pair=False) == "HKD"

    def test_an_undeclared_row_currency_stays_undeclared(self):
        row = _row(estimate_currency="unknown")
        assert fiscal.metric_currency(row, fiscal.METRIC_EPS, "estimate",
                                      use_pair=False) is None

    def test_revenue_and_eps_have_their_own_currency(self):
        """TSM: revenue TWD, EPS consensus USD — one pair never answers for both."""
        row = _row(revenue_pair_currency="TWD")
        assert fiscal.paired_currencies(row, fiscal.BODY_METRICS) == ["CNY", "TWD"]

    def test_unknown_side_is_a_programming_error(self):
        with pytest.raises(ValueError):
            fiscal.metric_value(_row(), fiscal.METRIC_EPS, "consensus")

    def test_a_partly_paired_row_does_not_claim_a_comparison(self):
        row = {key: value for key, value in _row().items()
               if not key.startswith("revenue_pair_")}
        assert fiscal.metric_has_figures(row, fiscal.METRIC_REVENUE) is True
        assert fiscal.paired_figures(row) is None

    def test_a_fully_paired_row_claims_both_metrics(self):
        assert fiscal.paired_figures(_row()) == [fiscal.METRIC_REVENUE, fiscal.METRIC_EPS]

    def test_a_row_without_figures_claims_nothing(self):
        empty = _row(eps_estimate=None, eps_actual=None,
                     revenue_estimate=None, revenue_actual=None,
                     eps_pair_estimate=None, eps_pair_actual=None,
                     revenue_pair_estimate=None, revenue_pair_actual=None)
        assert fiscal.paired_figures(empty) is None


# ── the body ───────────────────────────────────────────────────────────────

class TestDescription:
    def test_the_four_figures_are_the_panel_s(self):
        lines = _lines(generate_ical([_row()], title_lang="zh"))
        assert "营收预期：210.24B (CNY)" in lines
        assert "营收实际：207.34B (CNY)" in lines
        assert "EPS 预期：6.29 (CNY)" in lines
        assert "EPS 实际：6.95 (CNY)" in lines
        # Nothing of the old, cross-currency body survives.
        assert not any("HKD" in line for line in lines)

    def test_the_comparability_line_describes_the_pair(self):
        lines = _lines(generate_ical([_row()], title_lang="zh"))
        assert "对比：采用提供方同一币种的一致预期与实际（CNY）" in lines
        assert not any(line.startswith("对比：不可用") for line in lines)

    def test_the_english_feed_says_the_same_thing(self):
        lines = _lines(generate_ical([_row()], title_lang="en"))
        assert "EPS Est: 6.29 (CNY)" in lines
        assert "Comparability: estimate and actual from the same provider, one currency (CNY)" in lines

    def test_one_paired_metric_keeps_the_unavailable_notice(self):
        """Risk section: a row rescued for EPS alone is still cross-currency on revenue."""
        row = {key: value for key, value in _row().items()
               if not key.startswith("revenue_pair_")}
        lines = _lines(generate_ical([row], title_lang="zh"))
        assert "EPS 预期：6.29 (CNY)" in lines          # the rescue still applies
        assert "营收预期：247.54B (HKD)" in lines        # revenue has no pair of its own
        assert "对比：不可用（币种不同；currency_mismatch）" in lines

    def test_a_pair_on_the_only_printed_metric_is_enough(self):
        """A row with no revenue at all prints one metric, and it is paired."""
        row = _row(revenue_estimate=None, revenue_actual=None,
                   revenue_pair_estimate=None, revenue_pair_actual=None)
        lines = _lines(generate_ical([row], title_lang="zh"))
        assert not any("营收" in line for line in lines)
        assert "对比：采用提供方同一币种的一致预期与实际（CNY）" in lines

    def test_a_row_without_pairs_is_byte_identical_to_the_old_body(self):
        row = {key: value for key, value in _row().items()
               if not key.startswith(("eps_pair_", "revenue_pair_"))}
        lines = _lines(generate_ical([row], title_lang="en"))
        assert lines[4:9] == [
            "Revenue Est: 247.54B (HKD)",
            "Revenue Actual: 207.34B (CNY)",
            "EPS Est: 7.37 (HKD)",
            "EPS Actual: 7.06 (CNY)",
            "Comparability: unavailable (different currencies; currency_mismatch)",
        ]

    def test_an_unreleased_actual_keeps_the_row_columns(self):
        """#111: nothing is comparable before the report; the pair is not printed."""
        row = _row(report_date=date(2027, 3, 17), date_status="predicted")
        ics = generate_ical([row], title_lang="zh")
        lines = _lines(ics)
        assert "EPS 预期：7.37 (HKD)" in lines
        assert not any(line.startswith("EPS 实际") for line in lines)
        assert not any("CNY" in line for line in lines)
        assert "对比：不可用（币种不同；currency_mismatch）" in lines
        assert "STATUS:TENTATIVE" in ics

    def test_a_comparable_row_gains_no_note(self):
        """The pair note replaces the 「不可用」 line; it never adds a new one."""
        row = _row(estimate_currency="CNY", actual_currency="CNY",
                   comparison_unavailable_reason=None)
        lines = _lines(generate_ical([row], title_lang="zh"))
        assert "EPS 预期：6.29 (CNY)" in lines
        assert not any(line.startswith("对比") for line in lines)

    def test_the_feed_still_parses(self):
        import icalendar

        ics = generate_ical([_row()], title_lang="zh")
        events = [c for c in icalendar.Calendar.from_ical(ics).walk() if c.name == "VEVENT"]
        assert len(events) == 1
        assert "210.24B" in str(events[0]["DESCRIPTION"])


# ── the new label is bilingual like the rest of the table ──────────────────

def test_the_pair_note_label_carries_both_languages():
    entry = __import__("app.ical", fromlist=["_LABELS"])._LABELS["comparability_provider_pair"]
    assert set(entry) == {"en", "zh"}
    assert "{currency}" in entry["en"] and "{currency}" in entry["zh"]
