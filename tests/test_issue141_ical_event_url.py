"""Issue #141: an event in the calendar must lead back to its row on the site.

The iCal feed is the busiest entrance (191 requests / 24 h, 78 events in one
subscriber's feed) and every VEVENT stopped at the event itself: ``SUMMARY``
said who and when, ``DESCRIPTION`` said how much, and there was no ``URL`` — so
opening 「NVDA 英伟达 Q3 财报」 in a phone's calendar left the reader to reopen
FinCal, page to November, find the day and then the row. The feed never emitted
a single ``URL:`` line.

Two halves, both pinned here:

* ``app.ical._event_url`` writes ``{ICAL_BASE_URL}/?tab=calendar&month=…&date=
  …&symbol=…`` on every event — and nothing at all when a self-hosted instance
  never declared a public base, so its feed keeps its exact bytes;
* the SPA's ``useUrlState()`` accepts ``symbol`` (whitelisted, dropped when
  illegal), and the page opens that row's detail panel once the day's list is
  loaded, writing the symbol back as the selection changes.

The last test feeds the *emitted* link through the SPA's own parser under node,
so the two ends cannot drift into producing links the page does not understand.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import icalendar
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from app import config  # noqa: E402
from app.ical import _event_url, generate_ical  # noqa: E402
import frontend_contract as fc  # noqa: E402

BASE = "https://fincal.kazusa.feng.moe"


# ── helpers ────────────────────────────────────────────────────────────────

def _row(**overrides) -> dict:
    row = {
        "symbol": "NVDA", "market": "US", "company_name": "英伟达",
        "company_name_en": "NVIDIA", "report_date": date(2026, 11, 17),
        "before_after": "after", "fiscal_year": 2027, "fiscal_quarter": 3,
        "eps_estimate": 2.49676, "estimate_currency": "USD",
        "date_source": "futu", "date_status": "scheduled",
    }
    row.update(overrides)
    return row


def _events(ics: str) -> list:
    cal = icalendar.Calendar.from_ical(ics)
    return [c for c in cal.walk() if c.name == "VEVENT"]


def _body(ics: str) -> str:
    """The first event's DESCRIPTION with its escaped newlines restored."""
    raw = [line for line in ics.replace("\r\n ", "").split("\r\n")
           if line.startswith("DESCRIPTION:")][0]
    return raw[len("DESCRIPTION:"):].replace("\\n", "\n")


def _url_lines(ics: str) -> list[str]:
    """``URL:`` content lines, unfolded (a long link is folded at 75 bytes)."""
    return [line[len("URL:"):] for line in ics.replace("\r\n ", "").split("\r\n")
            if line.startswith("URL:")]


@pytest.fixture
def public_base(monkeypatch):
    monkeypatch.setattr(config, "ICAL_BASE_URL", BASE)
    return BASE


# ── the link itself ────────────────────────────────────────────────────────

def test_every_event_carries_a_link_to_its_own_day_and_row(public_base):
    """Acceptance 1: the URL names the month, the day and the symbol."""
    ics = generate_ical([_row(), _row(symbol="0700.HK", market="HK",
                                      company_name="腾讯控股", report_date=date(2026, 8, 12))],
                        title_lang="zh")
    urls = _url_lines(ics)
    assert len(urls) == len(_events(ics)) == 2
    assert urls[0] == (f"{BASE}/?tab=calendar&month=2026-11&date=2026-11-17&symbol=NVDA")
    assert urls[1] == (f"{BASE}/?tab=calendar&month=2026-08&date=2026-08-12&symbol=0700.HK")


def test_the_url_property_parses_back_as_a_uri(public_base):
    """RFC 5545 §3.8.4.6 holds: the reference parser reads ``URL`` as a string."""
    ev = _events(generate_ical([_row()]))[0]
    assert str(ev["URL"]) == (f"{BASE}/?tab=calendar&month=2026-11&date=2026-11-17&symbol=NVDA")
    query = parse_qs(urlsplit(str(ev["URL"])).query)
    assert query["tab"] == ["calendar"]
    assert query["date"] == ["2026-11-17"]


def test_a_trailing_slash_on_the_base_does_not_double(monkeypatch):
    monkeypatch.setattr(config, "ICAL_BASE_URL", BASE + "//")
    assert _event_url("AAPL", date(2026, 1, 5)) == (
        f"{BASE}/?tab=calendar&month=2026-01&date=2026-01-05&symbol=AAPL")


def test_the_day_and_month_are_zero_padded(public_base):
    link = _event_url("AAPL", date(2026, 1, 5))
    assert "month=2026-01" in link and "date=2026-01-05" in link


def test_a_publisher_without_a_public_base_gets_no_link(monkeypatch):
    """Acceptance 2: an unset ``ICAL_BASE_URL`` leaves the feed's bytes alone."""
    monkeypatch.setattr(config, "ICAL_BASE_URL", "")
    ics = generate_ical([_row()], title_lang="zh")
    assert "URL:" not in ics
    assert "在 FinCal 打开" not in ics
    assert _event_url("NVDA", date(2026, 11, 17)) == ""
    # The rest of the event is exactly what it was.
    assert "SUMMARY:NVDA 英伟达 Q3 财报" in ics


def test_an_illegal_symbol_is_dropped_rather_than_carried(public_base):
    """Acceptance 3: ``symbol`` is whitelisted — the day link survives alone."""
    for hostile in ("NVDA&tab=universe", "NVDA;drop", "../../etc", "A" * 21, "英伟达", "N V"):
        link = _event_url(hostile, date(2026, 11, 17))
        assert link == f"{BASE}/?tab=calendar&month=2026-11&date=2026-11-17", hostile
    # …and the same holds through the generator, including a missing symbol.
    ics = generate_ical([_row(symbol="NVDA X")])
    assert "symbol=" not in "\n".join(_url_lines(ics))
    assert len(_url_lines(ics)) == 1


def test_the_description_repeats_the_link_in_the_feeds_language(public_base):
    """Google Calendar does not always render ``URL``; the body carries it too."""
    zh = _body(generate_ical([_row()], title_lang="zh"))
    assert zh.splitlines()[-1] == (
        f"在 FinCal 打开：{BASE}/?tab=calendar&month=2026-11&date=2026-11-17&symbol=NVDA")

    en = _body(generate_ical([_row()], title_lang="en"))
    assert en.splitlines()[-1] == (
        f"Open in FinCal: {BASE}/?tab=calendar&month=2026-11&date=2026-11-17&symbol=NVDA")


def test_adding_a_link_does_not_renumber_uid_or_sequence(monkeypatch, public_base):
    """A new property must not make clients rebuild events (``SEQUENCE``/``UID``)."""
    with_link = generate_ical([_row()], title_lang="zh")
    monkeypatch.setattr(config, "ICAL_BASE_URL", "")
    without = generate_ical([_row()], title_lang="zh")
    assert "URL:" in with_link and "URL:" not in without
    for prefix in ("UID:", "SEQUENCE:", "DTSTART", "SUMMARY:"):
        assert ([l for l in without.split("\r\n") if l.startswith(prefix)] ==
                [l for l in with_link.split("\r\n") if l.startswith(prefix)])


def test_the_folded_link_still_parses(public_base):
    """A long base + long symbol folds at 75 bytes and survives the round trip."""
    ics = generate_ical([_row(symbol="ABCDEFGHIJ-012345678", market="US")])
    assert all(len(line.encode("utf-8")) <= 75 for line in ics.split("\r\n"))
    ev = _events(ics)[0]
    assert "symbol=ABCDEFGHIJ-012345678" in str(ev["URL"])


# ── the SPA side, evaluated under node ─────────────────────────────────────

URL_PROBE = r"""
/*USE_URL_STATE*/

const calls = [];
const host = {
  history: {
    pushState: (state, title, url) => { calls.push(['push', url]); host.location.search = url.startsWith('?') ? url : ''; },
    replaceState: (state, title, url) => { calls.push(['replace', url]); host.location.search = url.startsWith('?') ? url : ''; },
  },
  location: { pathname: '/cal', search: '' },
};
const url = useUrlState(host);

const parsed = {
  from_the_feed: url.parseUrlState(process.env.LINK_QUERY || ''),
  hk_symbol: url.parseUrlState('?tab=calendar&month=2026-08&date=2026-08-12&symbol=0700.HK'),
  without: url.parseUrlState('?tab=calendar&month=2026-11&date=2026-11-17'),
  illegal: url.parseUrlState('?tab=calendar&date=2026-11-17&symbol=NVDA%26tab%3Duniverse'),
  too_long: url.parseUrlState('?tab=calendar&date=2026-11-17&symbol=' + 'A'.repeat(21)),
  padded: url.parseUrlState('?date=2026-11-17&symbol=%20NVDA%20'),
};

const built = {
  with_symbol: url.buildUrlState({ tab: 'calendar', month: '2026-11', date: '2026-11-17', symbol: 'NVDA' }),
  symbol_only: url.buildUrlState({ tab: 'calendar', symbol: '0700.HK' }),
  illegal: url.buildUrlState({ tab: 'calendar', month: '2026-11', date: '2026-11-17', symbol: 'a b' }),
  cleared: url.buildUrlState({ tab: 'calendar', month: '2026-11', date: '2026-11-17', symbol: '' }),
  old_shape: url.buildUrlState({ tab: 'calendar', month: '2026-11', date: '2026-11-17' }),
};

const keys = {
  normal: url.symbolKey('0700.HK'),
  trimmed: url.symbolKey('  NVDA  '),
  rejected: ['', null, undefined, 'A;B', 'a/b', 'A B', 'x'.repeat(21)].map(v => url.symbolKey(v)),
};

// A selection change replaces, never pushes (a click-through is not a step).
url.replaceUrl({ tab: 'calendar', month: '2026-11', date: '2026-11-17', symbol: 'NVDA' });
const afterReplace = calls.slice();

console.log(JSON.stringify({ parsed, built, keys, afterReplace }));
"""


def _run_probe(probe: str, replacements: dict[str, str], env: dict | None = None) -> dict:
    for token, value in replacements.items():
        probe = probe.replace(token, value)
    completed = subprocess.run(
        ["node", "-e", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, **(env or {})},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _url_probe(link: str = "") -> dict:
    source = fc.function_source(fc.read_app_js(), "useUrlState")
    return _run_probe(URL_PROBE, {"/*USE_URL_STATE*/": source},
                      env={"LINK_QUERY": link})


pytestmark_node = pytest.mark.skipif(not shutil.which("node"),
                                     reason="node is required for the JS behaviour check")


@pytestmark_node
class TestUrlStateSymbol:
    @classmethod
    def setup_class(cls):
        cls.out = _url_probe("?tab=calendar&month=2026-11&date=2026-11-17&symbol=NVDA")

    def test_a_symbol_link_is_readable(self):
        assert self.out["parsed"]["from_the_feed"] == {
            "tab": "calendar", "month": "2026-11", "date": "2026-11-17", "symbol": "NVDA"}
        assert self.out["parsed"]["hk_symbol"]["symbol"] == "0700.HK"

    def test_a_link_without_a_symbol_carries_no_key(self):
        """The pre-#141 state shape must stay byte-identical for old links."""
        assert self.out["parsed"]["without"] == {
            "tab": "calendar", "month": "2026-11", "date": "2026-11-17"}
        assert "symbol" not in self.out["parsed"]["without"]

    def test_an_illegal_symbol_never_enters_the_state(self):
        assert "symbol" not in self.out["parsed"]["illegal"]
        assert "symbol" not in self.out["parsed"]["too_long"]

    def test_the_symbol_is_trimmed(self):
        assert self.out["parsed"]["padded"]["symbol"] == "NVDA"
        assert self.out["keys"]["trimmed"] == "NVDA"
        assert self.out["keys"]["normal"] == "0700.HK"

    def test_the_whitelist_matches_the_feed(self):
        assert all(value == "" for value in self.out["keys"]["rejected"])

    def test_the_symbol_is_written_back(self):
        assert self.out["built"]["with_symbol"] == (
            "?tab=calendar&month=2026-11&date=2026-11-17&symbol=NVDA")
        assert self.out["built"]["symbol_only"] == "?tab=calendar&symbol=0700.HK"

    def test_an_illegal_symbol_is_not_written_and_clearing_removes_it(self):
        assert self.out["built"]["illegal"] == "?tab=calendar&month=2026-11&date=2026-11-17"
        assert self.out["built"]["cleared"] == "?tab=calendar&month=2026-11&date=2026-11-17"
        assert self.out["built"]["old_shape"] == "?tab=calendar&month=2026-11&date=2026-11-17"

    def test_a_selection_change_replaces_the_address_bar(self):
        assert self.out["afterReplace"] == [
            ["replace", "?tab=calendar&month=2026-11&date=2026-11-17&symbol=NVDA"]]


@pytestmark_node
def test_the_emitted_link_is_one_the_page_understands(public_base):
    """The producer and the consumer are pinned to each other, not to a copy."""
    ics = generate_ical([_row(symbol="0700.HK", market="HK", report_date=date(2026, 8, 12))],
                        title_lang="zh")
    link = _url_lines(ics)[0]
    assert link.startswith(BASE + "/?")
    state = _url_probe(urlsplit(link).query)["parsed"]["from_the_feed"]
    assert state == {"tab": "calendar", "month": "2026-08",
                     "date": "2026-08-12", "symbol": "0700.HK"}


# ── the page wiring ────────────────────────────────────────────────────────

class TestPageWiring:
    def _js(self) -> str:
        return fc.read_app_js()

    def test_the_link_is_applied_to_the_row_of_that_day(self):
        js = self._js()
        body = js[js.index("function applySymbolKey(key)"):]
        body = body[:body.index("\n      }")]
        assert "const cell = cal.selectedDay.value;" in body
        assert "cell.earnings.find(" in body
        assert "cal.selectEarning(earning, cell, apiFetch);" in body
        # A symbol that did not report that day falls back to the list.
        assert "if (!earning) return false;" in body
        assert "toUpperCase()" in body

    def test_the_named_row_survives_the_first_load(self):
        js = self._js()
        load = js[js.index("async function loadEarnings()"):]
        load = load[:load.index("// ── Address bar writes")]
        apply_day = load.index("if (pendingDateKey) applyDateKey(pendingDateKey);")
        apply_symbol = load.index("applySymbolKey(pendingSymbolKey);")
        assert apply_day < apply_symbol, "先定日，再开该日的那一只"
        assert "pendingSymbolKey = '';" in load, "一次性：翻月不得重新打开面板"

    def test_the_selection_is_written_and_cleared(self):
        js = self._js()
        pos = js.index("function currentUrlState()")
        state = js[pos:js.index("};", js.index("symbol: cal.selectedEarning", pos))]
        assert "symbol: cal.selectedEarning.value ? (cal.selectedEarning.value.symbol || '') : ''" in state
        watcher = js[js.index("watch(() => (cal.selectedEarning.value ?"):]
        watcher = watcher[:watcher.index("});")]
        assert "if (!restoringFromHistory) url.replaceUrl(currentUrlState());" in watcher

    def test_a_history_step_restores_the_row(self):
        js = self._js()
        handler = js[js.index("async function applyHistoryState()"):]
        handler = handler[:handler.index("window.addEventListener('popstate'")]
        assert "pendingSymbolKey = state.symbol || '';" in handler
        assert "pendingSymbolKey = '';" in handler

    def test_the_exposed_api_still_matches_the_template(self):
        js = self._js()
        exposed = fc.exposed_setup_keys(js)
        assert fc.dangling_bindings(fc.read_index(), exposed) == set()
