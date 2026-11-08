"""The offline backfill for Issue #113: label what can be proven, merge what it hid.

The script's planning half is pure (rows in, plan out), so it is tested without a
database.  The rule under test is the same one the sync now uses, and the guards
are the interesting part: a bare ``Q`` row is never labelled (its quarter is
unrecoverable), a label another row already holds is never taken over, and a row
that reports an actual value is never merged away.
"""
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import backfill_fiscal_identity as backfill  # noqa: E402


def row(**overrides):
    base = {
        "id": 1, "symbol": "0303.HK", "market": "HK", "company_name": "VTECH",
        "report_date": dt.date(2026, 11, 20), "report_type": "H",
        "fiscal_year": None, "fiscal_quarter": None, "is_predicted": False,
        "eps_actual": None, "revenue_actual": None, "date_source": "longbridge",
    }
    base.update(overrides)
    return base


ANNUAL = row(id=90, report_date=dt.date(2026, 5, 21), report_type="A",
             fiscal_year=2026, fiscal_quarter=4, eps_actual=1.23)


# ── labelling ───────────────────────────────────────────────────────────────

def test_a_half_year_report_is_labelled_from_the_symbols_annual_report():
    labels, report = backfill.plan_labels([ANNUAL, row()])

    assert report["labelled"] == 1
    assert labels[0]["fiscal_year"] == 2027
    assert labels[0]["fiscal_quarter"] == 2, "H discloses Q2 by the #75 sequence contract"


def test_a_bare_quarter_row_is_never_labelled():
    # Nothing in the row says which quarter it is, and guessing would invent an
    # identity — the count is what makes it visible instead.
    labels, report = backfill.plan_labels([ANNUAL, row(report_type="Q")])

    assert labels == []
    assert report["quarter_unknown"] == 1


def test_a_label_another_row_already_holds_is_not_taken_over():
    holder = row(id=77, report_type="Q", fiscal_year=2027, fiscal_quarter=2)

    labels, report = backfill.plan_labels([ANNUAL, holder, row()])

    assert labels == []
    assert report["held_by_other"] == 1


def test_a_symbol_with_no_annual_report_keeps_its_row_unlabelled():
    labels, report = backfill.plan_labels([row(symbol="NEW.HK")])

    assert labels == []
    assert report["no_closing"] == 1


def test_a_label_that_contradicts_a_stored_date_is_refused():
    # A FY2027 Q2 cannot be dated before the FY2027 Q1 already stored.
    q1 = row(id=55, report_date=dt.date(2027, 3, 1), report_type="Q",
             fiscal_year=2027, fiscal_quarter=1)

    labels, report = backfill.plan_labels([ANNUAL, q1, row()])

    assert labels == []
    assert report["inconsistent"] == 1


def test_labelled_rows_are_not_planned_again():
    labelled = row(fiscal_year=2027, fiscal_quarter=2)

    labels, report = backfill.plan_labels([ANNUAL, labelled])

    assert labels == [] and report["unlabelled"] == 0


# ── duplicate reschedules ───────────────────────────────────────────────────

def test_a_rescheduled_half_year_keeps_only_the_latest_date():
    plans = backfill.plan_duplicates([
        row(id=1, report_date=dt.date(2026, 11, 11)),
        row(id=2, report_date=dt.date(2026, 11, 12)),
    ])

    assert len(plans) == 1
    assert plans[0]["keep"]["report_date"] == "2026-11-12"
    assert [d["id"] for d in plans[0]["drop"]] == [1]


def test_a_row_carrying_actuals_is_never_merged_away():
    plans = backfill.plan_duplicates([
        row(id=1, report_date=dt.date(2026, 11, 11)),
        row(id=2, report_date=dt.date(2026, 11, 12), eps_actual=2.5),
    ])

    assert plans == []


def test_announcements_further_apart_than_the_window_stay_separate():
    plans = backfill.plan_duplicates([
        row(id=1, report_date=dt.date(2026, 1, 5)),
        row(id=2, report_date=dt.date(2026, 11, 12)),
    ])

    assert plans == [], "two不同 announcements of the same type must not be folded"


def test_a_different_report_type_is_a_different_announcement():
    plans = backfill.plan_duplicates([
        row(id=1, report_date=dt.date(2026, 11, 11), report_type="H"),
        row(id=2, report_date=dt.date(2026, 11, 12), report_type="A"),
    ])

    assert plans == []


def test_three_dates_in_one_window_collapse_to_one_row():
    plans = backfill.plan_duplicates([
        row(id=1, report_date=dt.date(2026, 10, 30)),
        row(id=2, report_date=dt.date(2026, 11, 6)),
        row(id=3, report_date=dt.date(2026, 11, 27)),
    ])

    assert len(plans) == 1
    assert plans[0]["keep"]["id"] == 3
    assert [d["id"] for d in plans[0]["drop"]] == [1, 2]

def test_consecutive_quarters_are_not_duplicates():
    # The first version of this script merged any two rows within 90 days and
    # proposed deleting 298 rows of consecutive quarters (JPM 2027-01-14 and
    # 2027-04-13 are two different periods, 89 days apart).
    plans = backfill.plan_duplicates([
        row(id=1, report_date=dt.date(2027, 1, 14), report_type="Q",
            fiscal_year=2026, fiscal_quarter=4),
        row(id=2, report_date=dt.date(2027, 4, 13), report_type="Q",
            fiscal_year=2027, fiscal_quarter=1),
    ])

    assert plans == []


def test_a_half_year_and_an_annual_report_within_a_month_are_separate():
    plans = backfill.plan_duplicates([
        row(id=1, report_date=dt.date(2026, 11, 11), report_type="H"),
        row(id=2, report_date=dt.date(2026, 11, 20), report_type="A"),
    ])

    assert plans == []
