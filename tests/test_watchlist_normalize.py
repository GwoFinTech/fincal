"""Regression: watchlist-derived symbol lists must use canonical HK padding, and
user-pasted US codes with a stray ``.US`` suffix / Futu ``US.`` prefix must
normalize to the canonical bare ticker (Issue #43).

Also pins the single market decision shared by the read path, the sync path and
the write paths (Issue #66): a code from an exchange FinCal has no calendar for
must never be presented as a US ticker, and the write paths must refuse to
persist one under a market it does not belong to.
"""
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.symbol import bare_ticker, market_mismatch, market_of, normalize  # noqa: E402
from app.admin_watchlist import normalize_managed_symbol  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.main import app  # noqa: E402
from app.watchlist.base import (  # noqa: E402
    WatchlistSource, group_symbols_by_market, group_symbols_by_market_with_skipped,
)


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


# ── Issue #49: codes OpenD cannot route must be skipped, not suffixed ────

def test_get_futu_symbols_skips_non_us_hk_codes():
    src = FakeSource(["AAPL.US", "0700.HK", "000651.SZ", "600519.SH", "NVDA"])
    symbols, skipped = src.get_futu_symbols_with_skipped(force_refresh=True)
    assert symbols == ["AAPL.US", "0700.HK", "NVDA.US"]
    assert skipped == ["000651.SZ", "600519.SH"]


def test_get_futu_symbols_never_fabricates_malformed_codes():
    """``000651.SZ`` + ".US" produced ``US.000651.SZ``, which OpenD can never serve."""
    src = FakeSource(["000651.SZ", "159326.SZ"])
    futu = src.get_futu_symbols(force_refresh=True)
    assert futu == [], "no symbol may be invented for an unsupported exchange"
    assert src.get_futu_symbols_with_skipped(force_refresh=True)[1] == ["000651.SZ", "159326.SZ"]


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


def test_managed_watchlist_uses_same_us_normalization():
    assert normalize_managed_symbol("AAPL.US", "US") == ("AAPL", "US")
    assert normalize_managed_symbol("US.AAPL", "US") == ("AAPL", "US")


def test_legacy_watchlist_migration_is_present():
    """Guard the startup migration that converges pre-existing decorated rows."""
    from app import db
    source = db.init_db.__code__.co_consts
    sql = " ".join(str(value) for value in source)
    assert "canonical_symbol" in sql
    assert "ROW_NUMBER" in sql


# ── Issue #66: one market decision for read, sync and write paths ────────

# The production watchlist offers these; only the first two are servable.
_PRODUCTION_CODES = [
    "AAPL.US", "MSFT.US", "0700.HK", "9988.HK",
    "000651.SZ", "001389.SZ", "399006.SZ", "512890.SH", "600028.SH", "601988.SH",
]


def test_market_of_accepts_every_us_spelling():
    for code in ("AAPL", "aapl.us", "US.AAPL", " BRK.A ", "BF.B", "MKC.V", "ETSS.U"):
        assert market_of(code) == "US", code


def test_market_of_only_claims_the_markets_fincal_serves():
    assert market_of("700.HK") == "HK"
    assert market_of("HK.00700") == "HK"
    for code in ("000651.SZ", "600519.SH", "430047.BJ", "2330.TW", "ABC.RT", "", "  ", "."):
        assert market_of(code) is None, code


def test_bare_ticker_strips_a_market_but_not_a_class_share_suffix():
    assert bare_ticker("AAPL.US") == "AAPL"
    assert bare_ticker("US.AAPL") == "AAPL"
    assert bare_ticker("700.HK") == "700"
    assert bare_ticker("BRK.A") == "BRK.A"


def test_group_symbols_by_market_drops_other_exchange_codes():
    """The US bucket used to carry 25 A-share codes that can never have a row."""
    by_market, skipped = group_symbols_by_market_with_skipped(_PRODUCTION_CODES)

    assert by_market["US"] == ["AAPL", "MSFT"]
    assert by_market["HK"] == ["0700.HK", "9988.HK"]
    assert skipped == ["000651.SZ", "001389.SZ", "399006.SZ", "512890.SH", "600028.SH", "601988.SH"]
    # The single-market accessor keeps returning just the mapping.
    assert group_symbols_by_market(["600519.SH"]) == {"US": [], "HK": []}


def test_futu_sync_skip_set_is_unchanged():
    """Issue #49's skip set must not grow or shrink (Issue #66 regression)."""
    src = FakeSource(["AAPL.US", "0700.HK", "NVDA", "000651.SZ", "600519.SH", "BRK.A"])
    symbols, skipped = src.get_futu_symbols_with_skipped(force_refresh=True)
    assert symbols == ["AAPL.US", "0700.HK", "NVDA.US"]
    assert skipped == ["000651.SZ", "600519.SH", "BRK.A"]


def test_market_mismatch_flags_a_code_from_another_market():
    assert market_mismatch("600028.SH", "US") is True
    assert market_mismatch("0700.HK", "US") is True
    assert market_mismatch("AAPL", "HK") is True
    assert market_mismatch("AAPL.US", "HK") is True


def test_market_mismatch_allows_the_canonical_spellings():
    assert market_mismatch("AAPL.US", "US") is False
    assert market_mismatch("US.AAPL", "US") is False
    assert market_mismatch("BRK.A", "US") is False
    assert market_mismatch("0700.HK", "HK") is False
    assert market_mismatch("700", "HK") is False
    assert market_mismatch("00700", "HK") is False
    # Empty symbol is reported by a different code, not as a mismatch.
    assert market_mismatch("  ", "US") is False


def test_managed_watchlist_rejects_a_symbol_market_mismatch():
    import pytest

    with pytest.raises(ValueError, match="symbol_market_mismatch"):
        normalize_managed_symbol("600519.SH", "US")
    with pytest.raises(ValueError, match="symbol_market_mismatch"):
        normalize_managed_symbol("AAPL.US", "HK")


# ── Issue #66: the write path must not persist a dead entry ──────────────

class _RecordingCursor:
    """Cursor that records the SQL it is handed and answers with a user row."""

    rowcount = 1

    def __init__(self, executed):
        self._executed = executed

    def execute(self, sql, params=None):
        self._executed.append(" ".join(str(sql).split()))

    def fetchone(self):
        return {
            "id": 1, "portal_user_id": 1, "email": "t@t.com", "name": "T",
            "ical_token": "tok-abc", "symbol": "AAPL", "market": "US",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        }

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RecordingConn:
    def __init__(self, executed):
        self._executed = executed

    def cursor(self, **kw):
        return _RecordingCursor(self._executed)

    def __enter__(self):
        return _RecordingCursor(self._executed)

    def __exit__(self, *a):
        return False


def test_add_watchlist_rejects_a_code_that_is_not_the_requested_market():
    """``POST /api/watchlist?symbol=600028.SH&market=US`` → 400, no row written."""
    executed: list[str] = []
    app.dependency_overrides = {get_current_user: lambda: {"id": 1, "email": "t@t.com", "name": "T", "role": "user"}}
    try:
        with patch.object(db, "db_cursor", lambda: _RecordingConn(executed)):
            client = TestClient(app, raise_server_exceptions=False)

            resp = client.post("/api/watchlist?symbol=600028.SH&market=US")
            assert resp.status_code == 400, resp.text
            assert resp.json()["error"]["code"] == "symbol_market_mismatch"
            assert not [s for s in executed if "INSERT INTO watchlist" in s], executed

            # The canonical spellings still reach the INSERT.
            assert client.post("/api/watchlist?symbol=AAPL.US&market=US").status_code == 200
            assert [s for s in executed if "INSERT INTO watchlist" in s], executed

            assert client.post("/api/watchlist?symbol=700&market=HK").status_code == 200
    finally:
        app.dependency_overrides = {}
