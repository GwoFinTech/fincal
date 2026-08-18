"""Regression checks for the non-overlay FinCal selection workspace.

After Issue #13 refactor, JS logic is in assets/app-setup.js.
Tests check both index.html (template) and app-setup.js (logic).
"""
from pathlib import Path

INDEX_HTML = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "assets" / "app-setup.js"


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
