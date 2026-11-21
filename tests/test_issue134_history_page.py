"""Issue #134: the symbol-dimension history page — structure and wiring.

Every other FinCal surface answers 「这一天谁发财报」.  「0700.HK 过去 8 个季度」
had no entry: the reader walked the calendar cell by cell.  The backend half
(`/api/earnings?symbol=`) is pinned in ``tests/test_issue134_symbol_history.py``;
this file pins the page that consumes it, because a zero-build SPA fails
silently — a view that no tab reaches, a composable nobody calls, or columns
that lost a field all render a plausible-looking table.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "app" / "static" / "index.html"
APP_JS = ROOT / "app" / "static" / "assets" / "app-setup.js"

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def _read():
    return (INDEX_HTML.read_text(encoding="utf-8"),
            APP_JS.read_text(encoding="utf-8"))


def test_the_history_page_is_reachable_from_the_navigation():
    html, js = _read()
    assert "@click=\"appTab='history'\"" in html
    assert "class=\"app-tab\">标的</button>" in html
    assert "v-if=\"appTab === 'history'\"" in html
    # A tab the address bar cannot name is a tab a link cannot open (#125).
    assert "const TABS = ['calendar', 'watchlist', 'universe', 'history'];" in js


def test_the_page_lists_the_same_fields_as_the_detail_panel():
    html, _ = _read()
    for heading in ("财季", "公布日", "EPS 预期", "EPS 实际", "较预期",
                    "营收预期", "营收实际", "不可比原因"):
        assert heading in html, heading
    # The values come from the panel's own formatters, not a second rule.
    for helper in ("fqLabel(row.fiscal_year", "fmtNum(row.eps_estimate)",
                   "fmtNum(row.eps_actual)", "signedPct(epsSurplus(row))",
                   "fmtBigNum(row.revenue_estimate)", "comparisonShortNote(row)"):
        assert helper in html, helper


def test_the_composable_reads_one_symbol_from_the_calendar_endpoint():
    _, js = _read()
    assert "function useSymbolHistory(apiFetch)" in js
    assert "const historySymbol = ref('');" in js
    # One symbol, via the existing read — no new aggregate endpoint.
    assert "/api/earnings?' + params.toString()" in js
    assert "symbol: code" in js
    # 12 periods, newest first.
    assert "const HISTORY_PERIODS = 12;" in js
    assert ".slice(0, HISTORY_PERIODS)" in js


def test_the_page_is_wired_and_shows_real_empty_states():
    html, js = _read()
    assert "const hist = useSymbolHistory(apiFetch);" in js
    assert "if (tab === 'history' && user.value && hist.historySymbol.value)" in js
    # A subject named by the link must be remembered before any load runs.
    assert "hist.historySymbol.value = urlInitial.symbol;" in js
    # Picking a company rewrites the address bar (replace, like a day selection).
    assert "watch(hist.historySymbol" in js
    assert "historySymbol: hist.historySymbol" in js
    # Three states, none of them a blank panel: loading / no symbol / no history.
    assert "正在读取…" in html
    assert "先在上方选择一个标的。" in html
    assert "该标的暂无财报历史。" in html


def test_the_history_table_ships_its_css():
    """A utility the markup uses but the committed CSS lacks renders as nothing."""
    _, js = _read()
    tailwind = (ROOT / "app" / "static" / "assets" / "tailwind.css").read_text(encoding="utf-8")
    html, _ = _read()
    assert "tabular-nums" in html        # the numeric columns ask for it
    assert ".tabular-nums" in tailwind or "tabular-nums" in tailwind
