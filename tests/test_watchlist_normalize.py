"""Regression: watchlist-derived symbol lists must use canonical HK padding, and
user-pasted US codes with a stray ``.US`` suffix / Futu ``US.`` prefix must
normalize to the canonical bare ticker (Issue #43)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.symbol import normalize  # noqa: E402
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


# ── Issue #43: US suffix / Futu prefix must normalize to bare ticker ──────

def test_normalize_us_strips_suffix():
    assert normalize("AAPL.US", "US") == "AAPL"
    assert normalize("aapl.us", "US") == "AAPL"


def test_normalize_us_strips_futu_prefix():
    assert normalize("US.AAPL", "US") == "AAPL"
    assert normalize("us.aapl", "US") == "AAPL"


def test_normalize_us_lefts_bare_ticker():
    assert normalize("AAPL", "US") == "AAPL"
    assert normalize(" NVDA ", "US") == "NVDA"


def test_normalize_hk_still_pads_and_keeps_suffix():
    # Existing HK contract must be unchanged (Issue #2 / #43 scope).
    assert normalize("700.HK", "HK") == "0700.HK"
    assert normalize("00700", "HK") == "0700.HK"
    assert normalize("9988.HK", "HK") == "9988.HK"
