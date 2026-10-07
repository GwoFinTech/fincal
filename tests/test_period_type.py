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


def test_unmatched_disclosure_keeps_date_but_not_invented_identity():
    result = sync_earnings.fiscal_period_for_event(
        "1347.HK", "HK", "2026-08-26",
        {"period": "4", "period_type": "saf", "fiscal_year": "2026"},
        {},
    )
    assert result == (None, None, False)


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
