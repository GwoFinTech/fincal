"""Regression checks for the non-overlay FinCal selection workspace.

After Issue #13 refactor, JS logic is in assets/app-setup.js.
Tests check both index.html (template) and app-setup.js (logic).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "app" / "static" / "index.html"
APP_JS = ROOT / "app" / "static" / "assets" / "app-setup.js"
PREDICT_EARNINGS = ROOT / "scripts" / "predict_earnings.py"


def _read_all():
    """Read both HTML template and JS logic."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8") if APP_JS.exists() else ""
    return html, js


def test_calendar_selection_uses_a_responsive_non_overlay_side_panel():
    html, js = _read_all()

    assert ".calendar-workspace" in html
    assert "grid-template-columns: minmax(0, 1fr) minmax(340px, 390px)" in html
    assert "grid-template-columns: minmax(0, 1fr) 400px" in html
    assert 'class="selection-panel surface"' in html
    assert 'v-else-if="selectedDay"' in html
    assert "if (appTab.value === 'calendar')" in js
    assert "calendarCells.value.find(cell => cell.isToday)" in js
    assert "Day Detail Modal" not in html
    assert 'class="fixed inset-0 z-50 flex justify-end"' not in html


def test_selection_keeps_day_context_and_lazy_detail_loading():
    html, js = _read_all()

    assert '@click.stop="selectEarning(e, cell)"' in html
    assert "async function selectEarning(e, day" in js
    assert "if (day) selectedDay.value = day;" in js
    assert "selectedEarning.value = null;" in js
    assert "decision.value = null;" in js
    assert "selectedDay.value = cell;" in js
    assert "/api/earnings/" in js and "/decision" in js
    assert "function clearSelection()" in js
    assert "selectCell" in js and "selectEarning" in js and "clearSelection" in js


def test_watchlist_is_a_dedicated_top_level_page_with_market_groups():
    html, js = _read_all()

    assert "appTab = ref('calendar')" in js
    assert "@click=\"appTab='calendar'\"" in html
    assert "@click=\"appTab='watchlist'\"" in html
    assert "v-if=\"appTab === 'calendar'\"" in html
    assert "v-if=\"appTab === 'watchlist'\"" in html
    assert "const usWatchlist = computed" in js
    assert "const hkWatchlist = computed" in js
    assert "登录后管理自选" in html


def test_watchlist_page_reuses_search_and_mutation_apis_without_calendar_pills():
    html, js = _read_all()

    assert "@input=\"doSearch\"" in html
    assert "@click=\"addToWatchlist(r.symbol, r.market)\"" in html
    assert "@click=\"removeFromWatchlist(w.symbol, w.market)\"" in html
    assert "async function addToWatchlist(" in js
    assert "async function removeFromWatchlist(" in js
    assert "<!-- Watchlist pills -->" not in html


def test_controls_use_shadcn_compatible_primitives():
    html, _ = _read_all()
    assert "ui-btn" in html
    assert "ui-input" in html
    assert "ui-select" in html
    assert "ui-checkbox" in html
    assert "ui-dialog-backdrop" in html
    assert "ui-dialog" in html
    assert "@click=\"appTab='calendar'\"" in html
    assert '<input type="checkbox"' not in html.replace('<input class="ui-checkbox" type="checkbox"', '')
    assert 'v-model="icalOptions.lang" class="ui-select"' in html
    assert 'v-model="icalOptions.scope" class="ui-select"' in html
    assert 'v-model="icalOptions.markets" class="ui-select"' in html


def test_watchlist_only_reloads_and_guards_calendar_rows():
    html, js = _read_all()
    assert 'v-model="watchlistOnly" @change="loadEarnings"' in html
    assert "watchlistOnly.value" in js and "data.filter" in js
    assert "watchlist.value.some(w => w.symbol === e.symbol && w.market === e.market)" in js
    assert "calendarCells.value.find(cell => cell.isToday)" in js


def test_date_window_uses_local_ymd_not_utc_toiso_string():
    """Issue #46: window dates and 'today' must be built from local date
    parts, not toISOString().slice(0,10) (UTC), which shifts early by one
    day for UTC+8 (primary CN/HK market). Guards a regression."""
    html, js = _read_all()

    # The UTC-based date-string generation must be gone.
    assert ".toISOString().slice(0, 10)" not in js

    # localYmd helper exists and formats the local date.
    assert "function localYmd(d)" in js
    assert "d.getFullYear()" in js and "padStart(2, '0')" in js

    # Both call sites use localYmd: today in watchlistInsight and the
    # start/end window in loadEarnings.
    assert js.count("localYmd(new Date())") == 1
    assert js.count("localYmd(new Date(d.getFullYear(), d.getMonth() - 1, 1))") == 1
    assert js.count("localYmd(new Date(d.getFullYear(), d.getMonth() + 2, 0))") == 1

    # ... and that one call site is the shared local "today" helper, so the
    # watchlist lookahead and the calendar window cannot drift apart (#56).
    assert "function localTodayYmd() { return localYmd(new Date()); }" in js


def test_watchlist_next_report_has_its_own_forward_only_window():
    """Issue #56: the watchlist table's 「下次财报 / EPS 预期 / 营收预期」 must be
    fed by the watchlist's own forward-only request, never by the calendar's
    "displayed month ±1" dataset, and must never fall back to a reported row.

    The previous shape rendered 9/18 rows of already-reported periods in the
    default month (18/18 after paging the calendar back) with no stale hint.
    """
    html, js = _read_all()

    # The composable owns a dedicated ref plus its loader.
    assert "const nextEarnings = ref([])" in js
    assert "async function loadNextEarnings()" in js
    assert "for (const e of nextEarnings.value)" in js

    # Forward-only, watchlist-scoped request anchored on local today.
    assert "const start = localTodayYmd();" in js
    assert "localDaysFromTodayYmd(WATCHLIST_NEXT_WINDOW_DAYS)" in js
    assert "const WATCHLIST_NEXT_WINDOW_DAYS = " in js
    assert "watchlistOnly: true" in js
    assert "if (!e.report_date || e.report_date < today) continue;" in js

    # The old "earliest row of the calendar window" fallback is gone, and the
    # insight no longer receives the calendar dataset at all.
    assert "matches[0]" not in js
    assert "matches.find(" not in js
    assert "watchlistInsight(item, earningsData)" not in js
    assert "watchlistInsight(item, earnings.value)" not in js
    assert "watchlistInsight: wl.watchlistInsight," in js

    # Lifecycle: first paint, entering the tab, and watchlist mutations all
    # refresh the watchlist's own data (the calendar month must not be a
    # trigger for it).
    assert "if (tab === 'watchlist' && user.value) wl.loadNextEarnings();" in js
    assert js.count("await loadNextEarnings();") == 2
    assert "await wl.loadNextEarnings();" in js
    assert "watch(cal.currentDate, loadEarnings)" in js

    # Template: reflected values, an algorithmic-date marker and an empty state
    # that no longer claims a period is "待确认" when there is no report date.
    assert "watchlistInsight(w).report_date || '—'" in html
    assert 'v-if="watchlistInsight(w).is_predicted"' in html
    assert "periodLabel(watchlistInsight(w))" in html
    assert "fqLabel(watchlistInsight(w).fiscal_year" not in html


def test_watchlist_lookahead_covers_the_whole_prediction_horizon():
    """The watchlist window must be at least as long as the horizon the
    predictor can place a date in (scripts/predict_earnings.py
    MAX_FUTURE_DAYS), otherwise a symbol whose only future row sits further
    out would render "—" (Issue #56)."""
    js = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"^MAX_FUTURE_DAYS\s*=\s*(\d+)\s*$", PREDICT_EARNINGS.read_text(encoding="utf-8"), re.M)
    assert match, "MAX_FUTURE_DAYS not found in scripts/predict_earnings.py"

    assert f"const WATCHLIST_NEXT_WINDOW_DAYS = {int(match.group(1))};" in js
