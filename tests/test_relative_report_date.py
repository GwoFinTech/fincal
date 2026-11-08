"""Issue #107: a relative date label still carries its absolute date.

Longbridge prefixes *same-day* events with a word: ``"Today 2026.10.30 (EST)"``.
`parse_report_date` did `split(" ")[0]`, got ``"Today"``, failed `fromisoformat`
and returned `None`, so the event was dropped — 114 of 26,247 events in run 152,
every run, silently until Issue #104 added the counter.  The same payload carries
the absolute date three separate ways; none of them was being read.

The resolver takes the most reliable source first (the provider's own
``ext.local_date``), then the date inside the label, then the epoch timestamp in
the zone the label names, and only then gives up — at which point the caller counts
the event as ``unresolved`` (Issue #104's signal) instead of dropping it.
"""
import importlib.util
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync_earnings = _load("sync_earnings_relative_dates", "scripts/sync_earnings.py")


#: A same-day event exactly as production serves it: relative label, absolute
#: date inside the label, epoch, and the market's local day.
TODAY_EVENT = {
    "symbol": "WT.US", "market": "US", "counter_name": "WisdomTree",
    "currency": "USD", "date": "Today 2026.10.30 (EST)",
    "datetime": "1793377800", "date_type": "盘后",
    "data_kv": [{"type": "estimate_eps", "value_raw": "0.12"}],
    "ext": {"date_zone": "(EST)", "local_date": "2026-10-30",
            "financial_report": {"fiscal_year": "2026", "period": "4",
                                 "period_type": "qf"}},
}


class ParsingTests(TestCase):
    def test_a_relative_label_still_yields_its_absolute_date(self):
        self.assertEqual(sync_earnings.parse_report_date("Today 2026.10.30 (EST)"), "2026-10-30")

    def test_yesterdays_label_too(self):
        self.assertEqual(sync_earnings.parse_report_date("Yesterday 2026.10.29 (EST)"), "2026-10-29")

    def test_the_ordinary_shape_is_unchanged(self):
        self.assertEqual(sync_earnings.parse_report_date("2026.08.04 (美东)"), "2026-08-04")

    def test_a_label_with_no_date_at_all_is_unparseable(self):
        self.assertIsNone(sync_earnings.parse_report_date("今天"))
        self.assertIsNone(sync_earnings.parse_report_date(None))


class ResolutionTests(TestCase):
    def test_the_providers_own_local_date_wins(self):
        # Both sources disagree; the ISO local date is the authoritative one.
        info = {"date": "2026.10.30 (EST)", "ext": {"local_date": "2026-10-31"}}

        self.assertEqual(sync_earnings.resolve_report_date(info), "2026-10-31")

    def test_the_label_is_used_when_there_is_no_local_date(self):
        info = {"date": "Today 2026.10.30 (EST)", "ext": {}}

        self.assertEqual(sync_earnings.resolve_report_date(info), "2026-10-30")

    def test_the_epoch_is_the_last_resort_and_uses_the_named_zone(self):
        # 1793377800 = 2026-10-30 13:30 UTC = 09:30 EST — the same day either way;
        # the zone matters for an after-hours event just before local midnight.
        info = {"date": "Yesterday", "datetime": "1793377800", "ext": {"date_zone": "(EST)"}}

        self.assertEqual(sync_earnings.resolve_report_date(info), "2026-10-30")

    def test_an_event_with_no_absolute_date_resolves_to_nothing(self):
        self.assertIsNone(sync_earnings.resolve_report_date({"date": "Today", "ext": {}}))


class SyncTests(TestCase):
    """The stage must write the same-day event, not count it as unresolved."""

    def _run(self, event: dict) -> tuple[list[tuple], object]:
        captured: list[tuple] = []

        def fake_flush(stats, batch, label):
            captured.extend(batch)

        def fake_fetch(market, start, end):
            return [{"infos": [event]}] if market == "US" else []

        with patch.object(sync_earnings, "fetch_calendar", side_effect=fake_fetch), \
                patch.object(sync_earnings, "check_cancelled"), \
                patch("app.sync_audit.heartbeat"), \
                patch.object(sync_earnings, "_flush_batch", side_effect=fake_flush):
            stats = sync_earnings.SyncStats()
            sync_earnings.sync_earnings(1, stats)
        return captured, stats

    def test_a_same_day_event_is_written_with_its_absolute_date(self):
        rows, stats = self._run(TODAY_EVENT)

        self.assertEqual(stats.unresolved, 0, "a relative label is no longer a reason to drop an event")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "WT")
        self.assertEqual(rows[0][3], "2026-10-30")
        self.assertEqual(rows[0][7], 0.12, "the payload's estimate must survive the date fix")

    def test_an_event_with_no_date_is_still_counted_not_dropped_silently(self):
        undated = dict(TODAY_EVENT)
        undated.pop("datetime")
        undated["date"] = "Today"
        undated["ext"] = {"date_zone": "(EST)",
                          "financial_report": {"fiscal_year": "2026", "period": "4",
                                               "period_type": "qf"}}

        rows, stats = self._run(undated)

        self.assertEqual(rows, [])
        self.assertEqual(stats.unresolved, 1)
        self.assertEqual(stats.pages, 1, "the run resolved no rows from a real page — #104's signal")
