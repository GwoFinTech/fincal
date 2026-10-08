from datetime import date
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from reconcile_fiscal_labels import Release, build_proposals  # noqa: E402


def test_qf_release_corrects_stale_fiscal_year_without_deleting_row():
    rows = [{
        "id": 9, "symbol": "DLTH", "market": "US",
        "fiscal_year": 2026, "fiscal_quarter": 4,
        "report_date": date(2026, 3, 19), "date_source": "longbridge",
        "is_predicted": False,
    }, {
        "id": 10, "symbol": "DLTH", "market": "US",
        "fiscal_year": 2026, "fiscal_quarter": 2,
        "report_date": date(2026, 9, 4), "date_source": "longbridge",
        "is_predicted": False,
    }]
    releases = [Release("DLTH", "US", 2027, 2, "2026-09-03")]
    proposals = build_proposals(rows, releases)
    assert len(proposals) == 1
    assert (proposals[0].after_fiscal_year, proposals[0].after_fiscal_quarter) == (2027, 2)
    assert proposals[0].source_id == 10


def test_legacy_q4_between_q1_and_q3_becomes_q2():
    rows = [{
        "id": 18, "symbol": "0323.HK", "market": "HK",
        "fiscal_year": 2026, "fiscal_quarter": 1,
        "report_date": date(2026, 4, 24), "date_source": "longbridge",
        "is_predicted": False,
    }, {
        "id": 19, "symbol": "0323.HK", "market": "HK",
        "fiscal_year": 2026, "fiscal_quarter": 3,
        "report_date": date(2026, 10, 30), "date_source": "longbridge",
        "is_predicted": False,
    }, {
        "id": 20, "symbol": "0323.HK", "market": "HK",
        "fiscal_year": 2026, "fiscal_quarter": 4,
        "report_date": date(2026, 8, 28), "date_source": "longbridge",
        "is_predicted": False,
    }]
    releases = [
        Release("0323.HK", "HK", 2026, 1, "2026-04-24"),
        Release("0323.HK", "HK", 2026, 3, "2026-10-30"),
    ]
    proposals = build_proposals(rows, releases)
    assert len(proposals) == 1
    assert (proposals[0].after_fiscal_year, proposals[0].after_fiscal_quarter) == (2026, 2)


def test_non_longbridge_and_predictions_are_untouched():
    rows = [{
        "id": 30, "symbol": "X", "market": "US",
        "fiscal_year": 2026, "fiscal_quarter": 4,
        "report_date": date(2026, 3, 1), "date_source": "futu",
        "is_predicted": False,
    }, {
        "id": 31, "symbol": "Y", "market": "US",
        "fiscal_year": 2026, "fiscal_quarter": 4,
        "report_date": date(2026, 3, 1), "date_source": "longbridge",
        "is_predicted": True,
    }]
    releases = [Release("X", "US", 2026, 3, "2026-03-02")]
    assert build_proposals(rows, releases) == []
