"""The calendar fetch must ask for a whole earnings day, not a slice of it.

Issue #90: ``fetch_calendar`` requested ``--count 300`` per call, but the
provider caps a single response at ~3,300 events and — when one date holds more
than the requested count — returns that many and advances the cursor to the next
date.  The surplus is not re-requested, so it is lost for good:

* 2026-05-07 → 3,320 events, 300 requested  (90% dropped)
* 2026-08-13 → 1,735 events
* 2026-02-12 → 2,509 events

Raising the count above the provider's ceiling returns the same totals (5,000 /
10,000 / 20,000 all returned 3,320 for 2026-05-07), which is what makes a fixed,
generous request the correct fix rather than a retry loop.  The symbols whose
release was dropped are exactly the ones that then showed neither a currency nor
a report time in the product.

Pinned here: the shipped request is large enough for the busiest measured day,
the CLI receives that number, and a page that fills the request is reported
instead of being dropped silently.
"""
import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SYNC_EARNINGS = ROOT / "scripts" / "sync_earnings.py"

#: Largest single-day event count measured in production (2026-05-07).
BUSIEST_MEASURED_DAY = 3320


def _load_sync_earnings():
    spec = importlib.util.spec_from_file_location("sync_earnings_page_size", SYNC_EARNINGS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRun:
    """Stands in for ``subprocess.run`` over the calendar CLI."""

    def __init__(self, events_per_day: int):
        self.events_per_day = events_per_day
        self.commands: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.commands.append(list(cmd))

        class Result:
            returncode = 0
            stderr = ""
            stdout = json.dumps({
                "date": "2026-05-07",
                "list": [{
                    "date": "2026-05-07",
                    "infos": [{"counter_id": f"ST/US/SYM{i}", "date": "2026.05.07 (美东)"}
                              for i in range(self.events_per_day)],
                }],
                "next_date": "",
                "result": {},
            })

        return Result()


def test_the_shipped_request_covers_the_busiest_measured_day():
    module = _load_sync_earnings()

    assert module.CALENDAR_PAGE_COUNT >= BUSIEST_MEASURED_DAY, (
        "a request smaller than the busiest day silently drops the surplus"
    )
    # Above the provider's own ceiling: asking for more does not cost a second
    # round trip, it just lets the provider return the whole day.
    assert module.CALENDAR_PAGE_COUNT >= 5000


def test_the_cli_is_asked_for_the_whole_day():
    module = _load_sync_earnings()
    fake = _FakeRun(events_per_day=1200)

    with mock.patch.object(module.subprocess, "run", fake):
        pages = module.fetch_calendar("US", "2026-05-07", "2026-05-07")

    assert fake.commands, "fetch_calendar must call the CLI"
    command = fake.commands[0]
    assert "--count" in command
    assert int(command[command.index("--count") + 1]) == module.CALENDAR_PAGE_COUNT
    # The whole day comes back: a 1,200-event day is no longer cut to 300.
    assert sum(len(page.get("infos") or []) for page in pages) == 1200


def test_a_page_that_fills_the_request_is_reported(caplog):
    module = _load_sync_earnings()
    fake = _FakeRun(events_per_day=module.CALENDAR_PAGE_COUNT)

    with mock.patch.object(module.subprocess, "run", fake), caplog.at_level("WARNING"):
        module.fetch_calendar("US", "2026-05-07", "2026-05-07")

    assert any("ceiling was reached" in record.message for record in caplog.records), \
        "a full page must say the provider's ceiling was reached, not vanish"
