"""Issue #134: a symbol-scoped earnings read, so one company's quarters can be listed.

The product is organised by date: every surface answers「这一天/这个月谁发财报」.
「0700.HK 过去 8 个季度 EPS 预期与实际」had no entry at all — the reader had to
walk the calendar cell by cell.  The read that answers it already exists
(``fetch_earnings_from_db(symbols=…)``); what was missing is the route letting a
client ask for one symbol instead of making it download the universe and filter.

Pinned here:

* the parameter resolves to the two columns the query filters on, for the
  spellings users type (``0700.HK`` / ``700`` / ``AAPL``), and a code this
  product cannot serve is refused with ``invalid_symbol`` rather than silently
  returning an empty calendar;
* the request reaches the fetch with exactly one symbol+market;
* a history window wider than the calendar bound is accepted *only* when a
  symbol scopes it (the payload is a dozen rows, not ~2 MiB);
* two symbols never share a cache entry.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, earnings as earnings_mod  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.routers import api as api_mod  # noqa: E402

USER = {"id": 1, "email": "probe@local", "name": "probe"}


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    """Auth, the DB read and the layer cache are this route's external edges.

    The cache is a module global, so a fresh one per test keeps a cached entry
    from one case from answering the next (the key is symbol + window, and the
    window default is “today”).
    """
    monkeypatch.setattr(api_mod, "_earnings_cache", api_mod.LayerCache(
        default_ttl=120.0, stale_ttl=1800.0, name="earnings-test"))
    monkeypatch.setattr(api_mod, "ensure_user", lambda *a, **k: {"id": 1})
    calls = []

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(earnings_mod, "fetch_earnings_from_db", fake_fetch)
    return calls


class TestSymbolScope:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0700.HK", ("0700.HK", "HK")),
            ("700", ("0700.HK", "HK")),
            ("9988.hk", ("9988.HK", "HK")),
            ("AAPL", ("AAPL", "US")),
            ("aapl.us", ("AAPL", "US")),
        ],
    )
    def test_resolves_the_spellings_users_type(self, raw, expected):
        assert api_mod._symbol_scope(raw) == expected

    @pytest.mark.parametrize("raw", ["600519.SH", "not a symbol", ""])
    def test_refuses_codes_it_cannot_serve(self, raw):
        """An unservable code must not look like “this company never reports”."""
        with pytest.raises(AppError) as excinfo:
            api_mod._symbol_scope(raw)
        assert excinfo.value.code == "invalid_symbol"


class TestSymbolScopedRequest:
    def test_filters_the_existing_read_to_one_symbol(self, _stubs):
        api_mod.api_earnings(symbol="0700.HK", user=USER)
        assert len(_stubs) == 1
        assert _stubs[0]["symbols"] == ["0700.HK"]
        assert _stubs[0]["markets"] == ["HK"]

    def test_two_symbols_do_not_share_a_cache_entry(self, _stubs):
        api_mod.api_earnings(symbol="AAPL", user=USER)
        api_mod.api_earnings(symbol="NVDA", user=USER)
        assert [c["symbols"][0] for c in _stubs] == ["AAPL", "NVDA"]

    def test_repeat_read_uses_the_cache(self, _stubs):
        api_mod.api_earnings(symbol="AAPL", user=USER)
        api_mod.api_earnings(symbol="AAPL", user=USER)
        assert len(_stubs) == 1


class TestHistoryWindowBound:
    """A single symbol's quarters are a dozen rows; the universe bound is not theirs."""

    def _window(self, days: int) -> tuple[date, date]:
        start = date.today() - timedelta(days=days)
        return start, date.today()

    def test_history_window_beyond_the_calendar_bound_is_accepted(self, _stubs):
        start, end = self._window(900)
        assert (end - start).days > config.EARNINGS_MAX_WINDOW_DAYS
        api_mod.api_earnings(start=start, end=end, symbol="0700.HK", user=USER)
        assert len(_stubs) == 1
        assert _stubs[0]["start"] == start and _stubs[0]["end"] == end

    def test_calendar_request_keeps_the_universe_bound(self, _stubs):
        start, end = self._window(900)
        with pytest.raises(AppError) as excinfo:
            api_mod.api_earnings(start=start, end=end, user=USER)
        assert excinfo.value.code == "invalid_window"

    def test_history_bound_is_still_bounded(self, _stubs):
        start, end = self._window(config.EARNINGS_HISTORY_MAX_WINDOW_DAYS + 1)
        with pytest.raises(AppError) as excinfo:
            api_mod.api_earnings(start=start, end=end, symbol="0700.HK", user=USER)
        assert excinfo.value.code == "invalid_window"

    def test_inverted_window_still_rejected(self, _stubs):
        with pytest.raises(AppError):
            api_mod.api_earnings(start=date.today(), end=date.today() - timedelta(days=5),
                                 symbol="0700.HK", user=USER)
