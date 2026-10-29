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


# ── Issue #105: a bare numeric code is an HK code, not a US one ──────────

def test_market_of_reads_a_bare_numeric_code_as_hk():
    """The admin form's own placeholder (``0700``) is a valid HK code."""
    for code in ("1", "700", "0700", "00700", "08233", "82333"):
        assert market_of(code) == "HK", code
    # Spellings of the same company stay HK, so the universe can collapse them.
    assert market_of("0700.HK") == "HK"
    assert market_of("HK.00700") == "HK"


def test_market_of_refuses_to_guess_a_market_for_a_bare_six_digit_code():
    """``159326`` / ``512890`` are Shenzhen funds whose suffix was pasted off."""
    for code in ("159326", "512890", "1234567"):
        assert market_of(code) is None, code
    # The US contract and class shares are untouched.
    assert market_of("AAPL") == "US"
    assert market_of("BRK.A") == "US"
    assert market_of("AAPL.US") == "US"
    assert market_of("000651.SZ") is None


def test_group_symbols_by_market_collapses_the_spellings_of_one_hk_code():
    """``700`` / ``0700`` / ``0700.HK`` used to occupy three universe slots."""
    by_market, skipped = group_symbols_by_market_with_skipped(["700", "0700", "0700.HK"])

    assert by_market["US"] == [], by_market
    assert by_market["HK"] == ["0700.HK"], by_market
    assert skipped == []


def test_group_symbols_by_market_sends_bare_numbers_to_hk_and_drops_funds():
    by_market, skipped = group_symbols_by_market_with_skipped(
        ["0700", "82333", "AAPL", "159326", "BRK.A"]
    )

    # No numeric code may be bucketed as US any more.
    assert by_market["US"] == ["AAPL", "BRK.A"], by_market
    # The 5-digit 8xxxx RMB counter keeps its own canonical spelling (#54).
    assert by_market["HK"] == ["0700.HK", "82333.HK"], by_market
    # A bare six-digit fund code reaches the reported skip channel (a class
    # share is skipped by the Futu sync, not by the universe).
    assert skipped == ["159326"], skipped


def test_futu_sync_routes_a_bare_numeric_code_to_hk():
    """``0700`` used to be suffixed ``.US`` and fail on every Futu run."""
    src = FakeSource(["0700", "700", "0700.HK", "AAPL", "159326"])
    symbols, skipped = src.get_futu_symbols_with_skipped(force_refresh=True)

    assert symbols == ["0700.HK", "AAPL.US"], symbols
    assert skipped == ["159326"], skipped


def test_normalize_never_fabricates_a_numeric_us_symbol():
    import pytest

    with pytest.raises(ValueError, match="symbol_market_mismatch"):
        normalize("0700", "US")
    # The HK branch is unchanged, padding to the canonical four digits.
    assert normalize("0700", "HK") == "0700.HK"
    assert normalize("700", "HK") == "0700.HK"
    assert normalize("82333", "HK") == "82333.HK"


def test_managed_symbol_accepts_the_bare_hk_placeholder():
    import pytest

    assert normalize_managed_symbol("0700", "HK") == ("0700.HK", "HK")
    assert normalize_managed_symbol("700", "HK") == ("0700.HK", "HK")
    for symbol, market in (("0700", "US"), ("159326", "US"), ("159326", "HK")):
        with pytest.raises(ValueError, match="symbol_market_mismatch"):
            normalize_managed_symbol(symbol, market)


def test_write_paths_refuse_a_bare_numeric_code_as_us():
    """Both write paths reject it before touching the database (Issue #105)."""
    executed: list[str] = []
    app.dependency_overrides = {get_current_user: lambda: {"id": 1, "email": "t@t.com", "name": "T", "role": "user"}}
    try:
        with patch.object(db, "db_cursor", lambda: _RecordingConn(executed)):
            client = TestClient(app, raise_server_exceptions=False)

            rejected = client.post("/api/watchlist?symbol=0700&market=US")
            assert rejected.status_code == 400, rejected.text
            assert rejected.json()["error"]["code"] == "symbol_market_mismatch"
            assert not [s for s in executed if "INSERT INTO watchlist" in s], executed

            # The same code under HK is accepted, canonicalized to 0700.HK.
            assert client.post("/api/watchlist?symbol=0700&market=HK").status_code == 200
    finally:
        app.dependency_overrides = {}

    app.dependency_overrides = {get_current_user: lambda: {"id": 1, "email": "t@t.com", "name": "T", "role": "admin"}}
    try:
        with patch.object(db, "db_cursor", lambda: _RecordingConn([])):
            client = TestClient(app, raise_server_exceptions=False)

            rejected = client.post("/api/admin/watchlist", json={"symbol": "0700", "market": "US"})
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["error"]["code"] == "symbol_market_mismatch"
    finally:
        app.dependency_overrides = {}
