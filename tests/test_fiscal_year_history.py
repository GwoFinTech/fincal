"""A fiscal year is closed by the annual report, not by the calendar month.

Issue #113: `_fiscal_year_for` derived the year from the event's calendar month,
which is only true for a December year-end.  For a March year-end company (VTECH,
and most Japanese ADRs) the November half-year belongs to the *next* fiscal year;
the month rule said the current one, the order check then rejected the label, and
the row was written anyway with `fiscal_year`/`fiscal_quarter` NULL — 487 rows on
481 symbols, outside the identity contract (no unique-index protection, no
reschedule move, no merge, date-based iCal UID).

These tests pin the rule itself, the derivation that uses it, and the counter that
makes a remaining unlabelled row visible in `sync_runs.details`.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import fiscal  # noqa: E402
from app.sync_quality import SyncQuality  # noqa: E402
from scripts import sync_earnings  # noqa: E402


MARCH_YEAR_END = [{"fiscal_year": 2026, "report_date": "2026-05-21"}]
DECEMBER_YEAR_END = [{"fiscal_year": 2025, "report_date": "2026-02-10"}]


# ── the rule ────────────────────────────────────────────────────────────────

def test_the_event_after_an_annual_report_belongs_to_the_next_fiscal_year():
    # VTECH (0303.HK), 31 March year-end: FY2026 closed on 2026-05-21.
    assert fiscal.fiscal_year_after_closing(MARCH_YEAR_END, "2026-11-20") == 2027


def test_a_december_year_end_still_derives_the_same_year_as_before():
    # FY2025 closed on 2026-02-10 → the April 2026 quarter is FY2026, which is
    # what the calendar-month heuristic also said.  The fix must not move it.
    assert fiscal.fiscal_year_after_closing(DECEMBER_YEAR_END, "2026-04-22") == 2026


def test_the_annual_report_itself_keeps_the_year_it_closes():
    # Re-syncing the FY2026 annual report must not derive FY2027 for it: a closing
    # on the same day is not "earlier", so it provides no evidence and the caller
    # keeps its fallback (which derives FY2026 here).
    same_day = [{"fiscal_year": 2026, "report_date": "2026-05-21"}]
    assert fiscal.fiscal_year_after_closing(same_day, "2026-05-21") is None

    # With the previous year's annual report in history it derives FY2026, i.e.
    # the year this annual report closes — not FY2027.
    with_previous = same_day + [{"fiscal_year": 2025, "report_date": "2025-05-22"}]
    assert fiscal.fiscal_year_after_closing(with_previous, "2026-05-21") == 2026


def test_no_annual_report_means_no_derived_year():
    assert fiscal.fiscal_year_after_closing([], "2026-11-20") is None
    assert fiscal.fiscal_year_after_closing(None, "2026-11-20") is None


def test_a_closing_is_only_evidence_for_later_events():
    assert fiscal.fiscal_year_after_closing(MARCH_YEAR_END, "2026-01-05") is None


# ── the derivation that uses it ─────────────────────────────────────────────

def test_the_history_outranks_the_calendar_month_rule():
    closings = {("0303.HK", "HK"): MARCH_YEAR_END}

    assert sync_earnings._fiscal_year_for(
        "HK", 2, "2026-11-20", symbol="0303.HK", closings=closings) == 2027
    # Without the history the month rule still answers — it is the fallback, and
    # its answer for a March year-end is the wrong year.
    assert sync_earnings._fiscal_year_for("HK", 2, "2026-11-20") == 2026


def test_the_month_fallback_still_serves_a_symbol_with_no_history():
    assert sync_earnings._fiscal_year_for("US", 1, "2026-04-22") == 2026
    assert sync_earnings._fiscal_year_for("US", 4, "2027-02-05") == 2026


def test_a_march_year_end_half_year_keeps_its_fiscal_identity():
    """The exact production case: VTECH's November half-year report.

    The window index holds the FY2026 Q4 release of 2026-05-21, so the month
    rule's FY2026 Q2 label is dated after a Q4 of the same year and is rejected —
    the row is written with no identity.  With the annual report as history the
    label is FY2027 Q2 and the order check passes.
    """
    window = {
        ("0303.HK", "HK"): [{
            "symbol": "0303.HK", "market": "HK",
            "fiscal_year": 2026, "fiscal_quarter": 4,
            "report_date": "2026-05-21",
        }],
    }
    ext = {"period_type": "saf", "period": "2"}

    rejected = sync_earnings.fiscal_period_for_event(
        "0303.HK", "HK", "2026-11-20", ext, window)
    assert rejected == (None, None, False), (
        "the month rule labels it FY2026 Q2, which is dated after the FY2026 Q4 "
        "release and has to be rejected"
    )

    fixed = sync_earnings.fiscal_period_for_event(
        "0303.HK", "HK", "2026-11-20", ext, window,
        {("0303.HK", "HK"): MARCH_YEAR_END})
    assert fixed == (2027, 2, False)


def test_a_japanese_adr_quarter_is_not_one_year_early():
    # FJTSY reports a March year-end: its Nov quarter is FY2027 Q2, not FY2026.
    closings = {("FJTSY", "US"): [{"fiscal_year": 2026, "report_date": "2026-04-28"}]}

    year, quarter, skip = sync_earnings.fiscal_period_for_event(
        "FJTSY", "US", "2026-11-10", {"period_type": "qf", "period": "2"}, {}, closings)

    assert (year, quarter, skip) == (2027, 2, False)


# ── observability ───────────────────────────────────────────────────────────

def test_a_run_reports_how_many_rows_it_wrote_without_an_identity():
    quality = SyncQuality(written=500, fetched=500, unlabeled=12).to_dict()

    assert quality["unlabeled"] == 12
    assert quality["status"] == "success", "an unlabelled row is a count, not a failure"


def test_the_batch_counter_counts_rows_the_provider_gave_no_period_for():
    stats = sync_earnings.SyncStats()
    batch = [
        ("AAPL", "US", "Apple", "2026-11-01", "Q", 2026, 4, None, None, None, None, "after", "USD", None, "longbridge", "longbridge"),
        ("0303.HK", "HK", "VTECH", "2026-11-20", "H", None, None, None, None, None, None, "after", "HKD", None, "longbridge", "longbridge"),
        ("0700.HK", "HK", "Tencent", "2026-11-12", "Q", 2026, 3, None, None, None, None, "after", "HKD", None, "longbridge", "longbridge"),
    ]

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    original_cursor = sync_earnings.db_cursor
    original_flush = sync_earnings.flush_batch
    sync_earnings.db_cursor = lambda *a, **k: _Cursor()
    sync_earnings.flush_batch = lambda cur, rows: sync_earnings.FlushStats(rows=len(rows))
    try:
        sync_earnings._flush_batch(stats, batch, "test batch")
    finally:
        sync_earnings.db_cursor = original_cursor
        sync_earnings.flush_batch = original_flush

    assert stats.written == 3
    assert stats.unlabeled == 1, "only the row with no fiscal identity is counted"


# ── the identity is what makes a reschedule an update, not a new event ─────

def test_a_labelled_row_keeps_its_calendar_uid_when_the_date_moves():
    """Acceptance 3 of Issue #113: a reschedule must keep the UID.

    Without a fiscal identity the UID falls back to ``report_date``, so the
    provider moving an announcement by a day looked like a *new* event to every
    subscriber (and the old date stayed behind).  With the backfilled identity the
    UID is ``FY{fy}-Q{fq}`` and survives the move — which is the whole point of
    the identity contract (#40, #50, #87).
    """
    import datetime as dt

    from app.ical import _event_uid

    before = {"symbol": "0303.HK", "market": "HK", "fiscal_year": 2027, "fiscal_quarter": 2}
    after = dict(before)

    uid_before = _event_uid(before, dt.date(2026, 11, 11))
    uid_after = _event_uid(after, dt.date(2026, 11, 12))

    assert uid_before == uid_after == "fincal-0303.HK-HK-FY2027-Q2@fincal"

    # And the unlabelled row is exactly the case that could not hold a UID:
    assert _event_uid({"symbol": "0303.HK", "market": "HK"},
                      dt.date(2026, 11, 11)) != _event_uid({"symbol": "0303.HK", "market": "HK"},
                                                           dt.date(2026, 11, 12))
