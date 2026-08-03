"""Regression: watchlist-derived symbol lists must use canonical HK padding."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.watchlist.base import WatchlistSource  # noqa: E402


class FakeSource(WatchlistSource):
    def __init__(self, codes):
        self.codes = codes

    def fetch_symbols(self) -> list[str]:
        return self.codes


def test_get_symbols_by_market_pads_hk_codes():
    src = FakeSource(["700.HK", "1.HK", "293.HK", "AAPL.US", "NVDA"])
    by_market = src.get_symbols_by_market(force_refresh=True)
    assert "0700.HK" in by_market["HK"]
    assert "0001.HK" in by_market["HK"]
    assert "0293.HK" in by_market["HK"]
    assert "700.HK" not in by_market["HK"]
    assert "AAPL" in by_market["US"]
    assert "NVDA" in by_market["US"]


def test_get_futu_symbols_pads_hk_codes():
    src = FakeSource(["700.HK", "1.HK", "AAPL.US", "NVDA"])
    futu = src.get_futu_symbols(force_refresh=True)
    assert "0700.HK" in futu
    assert "0001.HK" in futu
    assert "AAPL.US" in futu
    assert "NVDA.US" in futu
