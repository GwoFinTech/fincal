"""One announcement is one row, whatever fiscal label the two events carry.

Issue #87: Longbridge emits a release (``Q``) and a half-year/annual disclosure
(``H``/``A``) for the same announcement.  The merge passes in ``app.fiscal`` group
by *fiscal identity*, and these pairs need not share one — the release can still
carry a pre-#75 ``Q4`` label while the disclosure is the corrected ``Q2``, and ADR
symbols carry no identity at all — so the pair reached the table as two rows: 85
duplicate ``(symbol, market, report_date)`` groups in production, each rendered
twice by the calendar/export/iCal and continuing to be minted every sync.

Pinned here:

* the read-side fold (``collapse_fiscal_duplicates``) keeps the release and leaves
  two same-kind rows alone;
* the write-side pass (``settle_same_date_rows``) drops an incoming disclosure when
  the date already has a release, and promotes a stored disclosure when the batch
  brings the release, so the upsert merges instead of inserting a twin;
* the repair script (``scripts/reconcile_same_date_rows.py``) plans only mixed
  groups and never lets a disclosure's figures overwrite the release's.
"""
import importlib.util
import sys
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import fiscal  # noqa: E402

RECONCILE = ROOT / "scripts" / "reconcile_same_date_rows.py"


def _row(report_type, quarter=2, symbol="0090.HK", market="HK", eps_actual=1.0,
         revenue_actual=100.0, currency="HKD", ident=True):
    return {
        "id": abs(hash((symbol, report_type, quarter))) % 10_000,
        "symbol": symbol,
        "market": market,
        "report_date": date(2026, 8, 28),
        "report_type": report_type,
        "fiscal_year": 2026 if ident else None,
        "fiscal_quarter": quarter if ident else None,
        "is_predicted": False,
        "eps_estimate": 0.9,
        "eps_actual": eps_actual,
        "revenue_estimate": 90.0,
        "revenue_actual": revenue_actual,
        "estimate_currency": currency,
        "actual_currency": currency,
        "estimate_basis": "unknown",
        "actual_basis": "unknown",
    }


class _Cursor:
    def __init__(self, rows=(), executed=None):
        self._rows = list(rows)
        self.executed = executed if executed is not None else []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return (list(self._rows) or [None])[0]


# ── read side ────────────────────────────────────────────────────────────

def test_a_release_and_a_disclosure_on_one_date_read_as_one_row():
    rows = [_row("Q", quarter=4), _row("H", quarter=2)]

    kept = fiscal.collapse_fiscal_duplicates(rows)

    assert len(kept) == 1, "the same announcement must not be rendered twice"
    assert kept[0]["report_type"] == "Q", "the release is the period's own event"


def test_two_disclosures_on_one_date_are_left_alone():
    rows = [_row("H", quarter=2), _row("A", quarter=4)]

    kept = fiscal.collapse_fiscal_duplicates(rows)

    assert len(kept) == 2, "two same-kind rows are not provably one announcement"


def test_adr_rows_without_a_fiscal_identity_fold_too():
    rows = [_row("Q", ident=False, symbol="FJTSY", market="US"),
            _row("H", ident=False, symbol="FJTSY", market="US")]

    kept = fiscal.collapse_fiscal_duplicates(rows)

    assert len(kept) == 1 and kept[0]["report_type"] == "Q"


def test_different_dates_stay_separate():
    first = _row("Q", quarter=1)
    second = dict(_row("H", quarter=2), report_date=date(2026, 11, 19))

    kept = fiscal.collapse_fiscal_duplicates([first, second])

    assert len(kept) == 2, "two different announcements on different dates stay"


# ── write side ───────────────────────────────────────────────────────────

def _batch_row(report_type, quarter=2, symbol="0090.HK"):
    """A raw sync tuple in the batch layout ``flush_batch`` uses."""
    return (symbol, "HK", "Test Ltd", "2026-08-28", report_type, 2026, quarter,
            0.9, 1.0, 90.0, 100.0, "after", "HKD", "HKD", "unknown", "unknown")


def test_an_incoming_disclosure_is_dropped_when_the_batch_has_the_release():
    outcome = fiscal.settle_same_date_rows(
        _Cursor(), [_batch_row("Q"), _batch_row("H")]
    )

    assert [row[4] for row in outcome.rows] == ["Q"]
    assert len(outcome.dropped) == 1 and outcome.dropped[0][4] == "H"


def test_a_stored_release_on_that_date_keeps_the_disclosure_out():
    stored = [{"id": 11, "symbol": "0090.HK", "market": "HK",
               "report_date": date(2026, 8, 28), "report_type": "Q",
               "is_predicted": False}]
    outcome = fiscal.settle_same_date_rows(_Cursor(stored), [_batch_row("H")])

    assert outcome.rows == [], "the date already shows this announcement"
    assert len(outcome.dropped) == 1


def test_a_batch_release_promotes_the_stored_disclosure_instead_of_twinning():
    stored = [{"id": 12, "symbol": "0090.HK", "market": "HK",
               "report_date": date(2026, 8, 28), "report_type": "H",
               "is_predicted": False}]
    cursor = _Cursor(stored)

    outcome = fiscal.settle_same_date_rows(cursor, [_batch_row("Q")])

    assert len(outcome.rows) == 1, "the release still goes through the upsert"
    assert outcome.promoted and outcome.promoted[0]["id"] == 12
    assert outcome.promoted[0]["from"] == "H" and outcome.promoted[0]["to"] == "Q"
    update = [sql for sql, _ in cursor.executed if sql.strip().startswith("UPDATE earnings")]
    assert update and "report_type" in update[0]


# ── repair script ────────────────────────────────────────────────────────

def _load_reconcile():
    spec = importlib.util.spec_from_file_location("reconcile_same_date_rows", RECONCILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_repair_never_lets_a_disclosure_overwrite_the_release():
    module = _load_reconcile()
    survivor = _row("Q", eps_actual=1.0, revenue_actual=100.0)
    dropped = _row("H", eps_actual=None, revenue_actual=334.0)

    values = module.merged_values(survivor, [dropped])

    assert "eps_actual" not in values, "the release's own figure must win"
    assert "revenue_actual" not in values, (
        "the disclosure's half-year/annual total must never replace the quarter"
    )


def test_the_repair_fills_only_what_the_release_lacks():
    module = _load_reconcile()
    survivor = _row("Q", eps_actual=None, currency="HKD")
    dropped = _row("H", eps_actual=2.5, currency="HKD")

    values = module.merged_values(survivor, [dropped])

    assert values.get("eps_actual") == 2.5
    assert "actual_currency" not in values
