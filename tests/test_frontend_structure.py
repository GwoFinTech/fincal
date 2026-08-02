"""Regression checks for the non-overlay FinCal selection workspace."""
from pathlib import Path


INDEX_HTML = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"


def test_calendar_selection_uses_a_responsive_non_overlay_side_panel():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert ".calendar-workspace" in html
    assert "grid-template-columns: minmax(0, 1fr) minmax(340px, 390px)" in html
    assert "grid-template-columns: minmax(0, 1fr) 400px" in html
    assert 'class="selection-panel surface"' in html
    assert 'v-else-if="selectedDay" class="selection-panel surface p-4"' in html
    assert "Day Detail Modal" not in html
    assert 'class="fixed inset-0 z-50 flex justify-end"' not in html


def test_selection_keeps_day_context_and_lazy_detail_loading():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '@click.stop="selectEarning(e, cell)"' in html
    assert "async function selectEarning(e, day = null)" in html
    assert "if (day) selectedDay.value = day;" in html
    assert "selectedEarning.value = null;\n        decision.value = null;\n        selectedDay.value = cell;" in html
    assert "const data = await apiFetch(`/api/earnings/${e.id}/decision`);" in html
    assert "function clearSelection()" in html
    assert "selectCell, selectEarning, clearSelection," in html
