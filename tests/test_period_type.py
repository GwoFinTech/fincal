"""Issue #75: Longbridge event sequences must not invent fiscal quarters."""
from datetime import date
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from app.fiscal import fiscal_label_consistent  # noqa: E402


def _load_sync_earnings():
    spec = importlib.util.spec_from_file_location(
        "sync_earnings_issue75", ROOT / "scripts" / "sync_earnings.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync_earnings = _load_sync_earnings()


CANONICAL = {
    ("0038.HK", "HK"): [
        {
            "symbol": "0038.HK",
            "market": "HK",
            "fiscal_year": 2026,
            "fiscal_quarter": 2,
            "report_date": "2026-08-25",
        },
    ],
}


def test_saf_period_four_does_not_create_a_false_q4_row():
    result = sync_earnings.fiscal_period_for_event(
        "0038.HK", "HK", "2026-08-26",
        {"period": "4", "period_type": "saf", "fiscal_year": "2026"},
        CANONICAL,
    )
    assert result == (None, None, True)


def test_qf_period_remains_the_canonical_fiscal_identity():
    result = sync_earnings.fiscal_period_for_event(
        "0038.HK", "HK", "2026-08-25",
        {"period": "2", "period_type": "qf", "fiscal_year": "2026"},
        CANONICAL,
    )
    assert result == (2026, 2, False)


def test_unmatched_half_year_disclosure_stays_the_half_year_event():
    """1347.HK publishes only a ``saf`` event: that *is* its interim result."""
    result = sync_earnings.fiscal_period_for_event(
        "1347.HK", "HK", "2026-08-26",
        {"period": "4", "period_type": "saf", "fiscal_year": "2027"},
        {},
    )
    assert result == (2026, 2, False)


def test_annual_report_without_a_release_event_keeps_its_quarter():
    """HSBC's annual result is published as ``af`` with no ``qf/4`` to pair."""
    canonical = {("0005.HK", "HK"): [
        {"symbol": "0005.HK", "market": "HK", "fiscal_year": 2026,
         "fiscal_quarter": 3, "report_date": "2026-10-27"},
    ]}
    result = sync_earnings.fiscal_period_for_event(
        "0005.HK", "HK", "2027-02-23",
        {"period": "4", "period_type": "af", "fiscal_year": "2026"},
        canonical,
    )
    assert result == (2026, 4, False)


def test_disclosure_ignores_its_own_fiscal_year_when_matching_the_release():
    """8033.HK's sav event claims FY2027 for an August-2026 interim."""
    canonical = {("8033.HK", "HK"): [
        {"symbol": "8033.HK", "market": "HK", "fiscal_year": 2026,
         "fiscal_quarter": 2, "report_date": "2026-08-24"},
    ]}
    result = sync_earnings.fiscal_period_for_event(
        "8033.HK", "HK", "2026-08-21",
        {"period": "4", "period_type": "saf", "fiscal_year": "2027"},
        canonical,
    )
    assert result == (None, None, True)


def test_a_period_has_one_row_even_when_the_release_is_months_away():
    """The release owns its period: distance no longer keeps a second row alive.

    The 14-day window used to let the annual report of a period whose release sat
    further away become a second row for the same identity (production: DEA
    ``qf`` 2026-02-21 + ``af`` 2026-02-23, both ``FY2025 Q4``; 200 such groups).
    """
    canonical = {("TEST.HK", "HK"): [
        {"symbol": "TEST.HK", "market": "HK", "fiscal_year": 2026,
         "fiscal_quarter": 2, "report_date": "2026-04-01"},
    ]}
    result = sync_earnings.fiscal_period_for_event(
        "TEST.HK", "HK", "2026-08-20",
        {"period": "4", "period_type": "saf", "fiscal_year": "2026"},
        canonical,
    )
    assert result == (None, None, True)


def test_report_type_records_which_event_sequence_a_row_came_from():
    assert sync_earnings.fiscal.report_type_for_period_type("qf") == "Q"
    assert sync_earnings.fiscal.report_type_for_period_type("3q") == "Q"
    assert sync_earnings.fiscal.report_type_for_period_type("saf") == "H"
    assert sync_earnings.fiscal.report_type_for_period_type("af") == "A"
    assert sync_earnings.fiscal.report_type_for_period_type(None) == "Q"


def test_disclosure_label_is_rejected_when_it_breaks_fiscal_order():
    canonical = {("TEST.HK", "HK"): [
        {"symbol": "TEST.HK", "market": "HK", "fiscal_year": 2026,
         "fiscal_quarter": 3, "report_date": "2026-11-06"},
    ]}
    result = sync_earnings.fiscal_period_for_event(
        "TEST.HK", "HK", "2026-11-20",
        {"period": "4", "period_type": "saf", "fiscal_year": "2026"},
        canonical,
    )
    assert result == (None, None, False)


def test_fiscal_year_is_derived_from_the_event_date_per_market():
    assert sync_earnings._fiscal_year_for("HK", 2, "2026-08-26") == 2026
    assert sync_earnings._fiscal_year_for("HK", 4, "2027-03-30") == 2026
    assert sync_earnings._fiscal_year_for("US", 4, "2027-02-18") == 2026
    assert sync_earnings._fiscal_year_for("US", 4, "2026-11-24") == 2026
    assert sync_earnings._fiscal_year_for("US", 1, "2026-05-11") == 2026


def test_fiscal_label_guard_rejects_quarter_before_lower_quarter():
    existing = [{
        "symbol": "0358.HK",
        "market": "HK",
        "fiscal_year": 2026,
        "fiscal_quarter": 2,
        "report_date": date(2026, 8, 25),
    }]
    assert not fiscal_label_consistent(
        "0358.HK", "HK", 2026, 3, "2026-08-20", existing
    )


def test_fiscal_label_guard_accepts_ordered_quarters_and_ignores_other_years():
    existing = [{
        "symbol": "0358.HK",
        "market": "HK",
        "fiscal_year": 2026,
        "fiscal_quarter": 2,
        "report_date": date(2026, 8, 25),
    }, {
        "symbol": "0358.HK",
        "market": "HK",
        "fiscal_year": 2025,
        "fiscal_quarter": 4,
        "report_date": date(2026, 3, 1),
    }]
    assert fiscal_label_consistent(
        "0358.HK", "HK", 2026, 3, "2026-10-25", existing
    )
