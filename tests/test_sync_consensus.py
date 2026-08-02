"""Unit coverage for Longbridge consensus and forecast-EPS transformations."""
import importlib.util
import sys
from datetime import date
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("sync_consensus", ROOT / "scripts" / "sync_consensus.py")
sync_consensus = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_consensus)


class ConsensusTransformTests(TestCase):
    def test_quarterly_consensus_keeps_gaap_and_adjusted_eps(self):
        rows = sync_consensus.consensus_rows("AAPL", "US", {
            "currency": "USD",
            "list": [{
                "fiscal_year": 2026,
                "fiscal_period": "3",
                "details": [
                    {"key": "eps", "estimate": "2.01"},
                    {"key": "normalized_eps", "estimate": "2.10"},
                    {"key": "revenue", "estimate": "109417000000"},
                    {"key": "ebit", "estimate": "35695000000"},
                    {"key": "net_income", "estimate": "29789000000"},
                ],
            }],
        })
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0][5]), "2.01")
        self.assertEqual(str(rows[0][6]), "2.10")
        self.assertEqual(str(rows[0][7]), "109417000000")

    def test_forecast_eps_persists_range_median_and_institution_counts(self):
        rows = sync_consensus.forecast_eps_rows("AAPL", "US", {"items": [{
            "forecast_start_date": "1769731200",  # 2026-01-30 UTC
            "forecast_end_date": "1769904000",
            "forecast_eps_lowest": "7.966",
            "forecast_eps_highest": "9.52",
            "forecast_eps_mean": "8.787",
            "forecast_eps_median": "8.8",
            "institution_total": 21,
            "institution_up": 12,
            "institution_down": 3,
        }]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], date(2026, 1, 30))
        self.assertEqual(rows[0][3], date(2026, 2, 1))
        self.assertEqual(str(rows[0][4]), "7.966")
        self.assertEqual(str(rows[0][5]), "9.52")
        self.assertEqual(str(rows[0][7]), "8.8")
        self.assertEqual(rows[0][8:11], (21, 12, 3))

    def test_open_ended_forecast_uses_start_for_conflict_safe_key(self):
        rows = sync_consensus.forecast_eps_rows("AAPL", "US", {"items": [{
            "forecast_start_date": "1769731200", "forecast_end_date": "0",
        }]})
        self.assertEqual(rows[0][2], rows[0][3])
