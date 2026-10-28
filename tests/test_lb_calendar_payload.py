"""Issue #104: the calendar event's identity comes from ``symbol``, not only ``counter_id``.

``longbridge`` 0.31.0 (installed 2026-10-26 10:21) stopped emitting
``counter_id`` (``ST/US/AAPL``) on ``finance-calendar report`` events; an event
now carries ``symbol`` (``AAPL.US`` / ``1081.HK``) and ``market``.  The stage
read only ``counter_id``, so every event was skipped: the 2026-10-29 run fetched
599 pages, wrote 0 rows, and still recorded ``success`` (``record_count=0``),
leaving the calendar frozen at the 2026-10-26 snapshot.

These cases pin both payload shapes — the new one as the primary source and the
old one as the fallback — so a future provider/CLI change cannot silently zero
the ingest again, and HK codes stay canonical either way.
"""
import importlib.util
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.symbol import from_lb_calendar_info  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "sync_earnings", ROOT / "scripts" / "sync_earnings.py")
sync_earnings = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_earnings)


class CalendarEventIdentityTests(TestCase):
    """One event → one canonical ``(symbol, market)``, whichever shape arrives."""

    def test_the_current_payload_resolves_from_symbol_and_market(self):
        # Captured verbatim from production on 2026-10-29.
        self.assertEqual(
            from_lb_calendar_info({"symbol": "IMMP.US", "market": "US",
                                   "counter_name": "Immutep"}),
            ("IMMP", "US"))
        self.assertEqual(
            from_lb_calendar_info({"symbol": "1081.HK", "market": "HK",
                                   "counter_name": "DAJIN"}),
            ("1081.HK", "HK"))

    def test_a_short_hk_code_is_padded_to_the_canonical_form(self):
        # 763.HK in the payload is 0763.HK everywhere else in the system; string
        # surgery would split one company into two symbols.
        self.assertEqual(from_lb_calendar_info({"symbol": "763.HK", "market": "HK"}),
                         ("0763.HK", "HK"))

    def test_the_market_suffix_alone_is_enough(self):
        self.assertEqual(from_lb_calendar_info({"symbol": "AAPL.US"}), ("AAPL", "US"))
        self.assertEqual(from_lb_calendar_info({"symbol": "0700.HK"}), ("0700.HK", "HK"))

    def test_a_bare_symbol_uses_the_declared_market(self):
        self.assertEqual(from_lb_calendar_info({"symbol": "AAPL", "market": "us"}),
                         ("AAPL", "US"))

    def test_the_old_counter_id_payload_still_resolves(self):
        self.assertEqual(from_lb_calendar_info({"counter_id": "ST/HK/700"}), ("0700.HK", "HK"))
        self.assertEqual(from_lb_calendar_info({"counter_id": "ST/US/AAPL"}), ("AAPL", "US"))

    def test_an_event_with_neither_field_is_unresolvable(self):
        self.assertEqual(from_lb_calendar_info({"counter_name": "???"}), ("", ""))
        self.assertEqual(from_lb_calendar_info({}), ("", ""))


class CalendarSyncResolvesTheCurrentPayloadTests(TestCase):
    """The stage must write rows from a payload that only carries ``symbol``."""

    @staticmethod
    def _event(symbol: str, market: str) -> dict:
        return {
            "symbol": symbol, "market": market, "counter_name": "Example Co",
            "currency": "USD", "date": "2026.08.04 (美东)", "date_type": "盘后",
            "data_kv": [{"type": "estimate_eps", "value_raw": "1.05"}],
            "ext": {"financial_report": {"fiscal_year": "2026", "period": "2",
                                         "period_type": "qf"}},
        }

    def _flush(self, pages):
        captured = {"rows": [], "fetched": 0}

        def fake_flush(stats, batch, label):
            captured["rows"].extend(batch)

        def fake_fetch(market, start, end):
            return pages if market == "US" else []

        with patch.object(sync_earnings, "fetch_calendar", side_effect=fake_fetch), \
             patch.object(sync_earnings, "check_cancelled"), \
             patch("app.sync_audit.heartbeat"), \
             patch.object(sync_earnings, "_flush_batch", side_effect=fake_flush):
            stats = sync_earnings.SyncStats()
            sync_earnings.sync_earnings(1, stats)
        captured["fetched"] = stats.fetched
        return captured

    def test_a_symbol_only_calendar_still_writes_every_event(self):
        captured = self._flush([{"infos": [self._event("AAPL.US", "US"),
                                           self._event("MSFT.US", "US")]}])

        self.assertEqual(captured["fetched"], 2,
                         "a payload without counter_id must not be skipped")
        self.assertEqual([(row[0], row[1]) for row in captured["rows"]],
                         [("AAPL", "US"), ("MSFT", "US")])
        self.assertEqual(captured["rows"][0][7], 1.05)

    def test_the_old_counter_id_calendar_still_writes_every_event(self):
        legacy = {"counter_id": "ST/US/KTOS", "counter_name": "Kratos",
                  "currency": "USD", "date": "2026.08.04 (美东)", "date_type": "盘后",
                  "data_kv": [], "ext": {"financial_report": {"fiscal_year": "2026",
                                                              "period": "2",
                                                              "period_type": "qf"}}}
        captured = self._flush([{"infos": [legacy]}])

        self.assertEqual(captured["fetched"], 1)
        self.assertEqual((captured["rows"][0][0], captured["rows"][0][1]), ("KTOS", "US"))

    def test_the_fiscal_period_index_resolves_both_shapes(self):
        pages = [{"infos": [self._event("AAPL.US", "US"),
                            {"counter_id": "ST/US/MSFT", "date": "2026.08.05 (美东)",
                             "ext": {"financial_report": {"fiscal_year": "2026",
                                                          "period": "2",
                                                          "period_type": "qf"}}}]}]
        index = sync_earnings.build_fiscal_period_index(pages)

        keys = {key for key in index}
        self.assertIn(("AAPL", "US"), keys)
        self.assertIn(("MSFT", "US"), keys)
