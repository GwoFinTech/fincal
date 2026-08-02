"""Regression coverage for Longbridge calendar pagination."""
import importlib.util
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("sync_earnings", ROOT / "scripts" / "sync_earnings.py")
sync_earnings = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_earnings)


class LongbridgePaginationTests(TestCase):
    def test_empty_next_date_advances_past_last_returned_calendar_day(self):
        self.assertEqual(
            sync_earnings.next_calendar_cursor("", "2026.02.04 (美东)", "2026-02-03"),
            "2026-02-05",
        )

    def test_provider_cursor_cannot_repeat_or_move_backward(self):
        self.assertEqual(
            sync_earnings.next_calendar_cursor("2026-02-03", "2026.02.04 (美东)", "2026-02-03"),
            "2026-02-05",
        )
