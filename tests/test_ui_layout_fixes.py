"""UI fixes from the layout review: clipped cells, a sliced column, missing units.

Four defects that were visible in the running app on production data (desktop 1440
and phone 390, driven through CDP):

* the month cell is ~50px wide on a phone, and `2068.HK 26Q3` was sliced in half by
  the cell's `overflow: hidden` — HK codes were unreadable;
* the 「业绩对比」 table demanded `min-w-[390px]` inside a 340–400px panel, so its
  last column (「较预期」, the point of the section) was cut off;
* neither currency was printed anywhere in that table, although the API carries
  `estimate_currency` / `actual_currency` for it — `31.94 → 32.87` with no unit is
  unreadable for TSM-style rows where providers report different currencies;
* the footnote still described the pre-#92 rule ("口径未标明 … 显示—"), i.e. the page
  claimed a stricter contract than `app.fiscal.comparison_unavailable_reason` now
  applies.

Plus the mobile interaction gap: the detail panel renders below the whole month
grid under 1024px, so tapping a day scrolled nothing and looked broken.

These are static-shape assertions on purpose — the SPA has no build step, so the
markup and the stylesheet *are* the contract. The behaviour they describe (no
horizontal clipping, panel scrolled into view) is verified in a real browser; this
file keeps a regression from silently undoing the shape.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INDEX = ROOT / "app" / "static" / "index.html"
APP_JS = ROOT / "app" / "static" / "assets" / "app-setup.js"


def page() -> str:
    return INDEX.read_text(encoding="utf-8")


def setup_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


# ── 1. phone cells must not slice the symbol ────────────────────────────────

def test_a_narrow_cell_prints_the_code_without_the_market_suffix():
    html = page()

    assert ".calendar-cell .sym-short { display: none; }" in html, (
        "the short form must be the exception, not the default"
    )
    narrow = html[html.index("@media (max-width: 640px)"):]
    assert ".calendar-cell .sym-full, .calendar-cell .fq-tag { display: none; }" in narrow
    assert ".calendar-cell .sym-short { display: inline; }" in narrow
    assert ".calendar-cell { padding: 4px; }" in narrow, (
        "the 7px cell padding alone eats a fifth of a 50px cell"
    )


def test_the_calendar_renders_both_forms_of_the_symbol():
    html = page()

    assert '<span class="sym-full">{{ e.symbol }}</span>' in html
    assert '<span class="sym-short">{{ compactSymbol(e.symbol) }}</span>' in html


def test_the_short_form_drops_only_a_market_suffix():
    js = setup_js()

    assert "function compactSymbol(symbol)" in js
    assert r".replace(/\.(HK|US)$/i, '')" in js
    assert "compactSymbol, currenciesDiffer," in js, "the template can only use exported helpers"


# ── 2. the comparison table must fit the panel it lives in ──────────────────

def test_the_comparison_table_no_longer_demands_more_width_than_the_panel():
    html = page()
    section = html[html.index("业绩对比"):html.index("Longbridge 一致预期")]

    assert "min-w-[390px]" not in section, (
        "the panel offers 340–400px minus padding; a 390px minimum sliced the column"
    )
    assert "table-fixed" in section, "fixed layout is what keeps the four columns inside"
    assert 'style="width:17%"' in section and 'style="width:31%"' in section
    assert "overflow-x-auto" not in section


def test_the_withheld_reason_fits_the_column_it_sits_in():
    html = page()
    js = setup_js()
    section = html[html.index("业绩对比"):html.index("Longbridge 一致预期")]

    # `预期与实际币种不同，无法比较` wrapped onto three lines inside the 110px cell.
    assert "comparisonShortNote(selectedEarning)" in section
    assert "comparisonNote(selectedEarning) || '—'" not in section
    assert ':title="comparisonNote(selectedEarning)"' in section, (
        "the full sentence must remain reachable on hover"
    )
    assert "function comparisonShortNote(e)" in js
    assert "comparisonShortNote," in js, "the template can only call exported helpers"


def test_every_figure_cell_stays_on_one_line():
    html = page()
    section = html[html.index("业绩对比"):html.index("Longbridge 一致预期")]

    # One row per metric with two lines of numbers: wrapping would reflow the
    # comparison column out of line with the values it compares.
    assert section.count("text-right whitespace-nowrap") >= 6


# ── 3. the unit is part of the number ──────────────────────────────────────

def test_the_comparison_headers_name_the_currency_of_each_side():
    html = page()
    section = html[html.index("业绩对比"):html.index("Longbridge 一致预期")]

    assert "selectedEarning.estimate_currency" in section
    assert "selectedEarning.actual_currency" in section
    assert "currenciesDiffer(selectedEarning)" in section, (
        "a mismatch is the reason the comparison is withheld — it must be visible"
    )


def test_a_currency_mismatch_has_its_own_emphasis():
    html = page()

    assert ".ccy { font-size: 9.5px;" in html
    assert ".ccy-warn { color: #fbbf24; }" in html


def test_currencies_differ_needs_two_stated_currencies():
    js = setup_js()
    block = js[js.index("function currenciesDiffer(e)"):js.index("function fmtPct(ratio)")]

    # Missing on either side is "unknown", not "different": only a stated pair of
    # different codes may raise the warning.
    assert "Boolean(estimate && actual && estimate !== actual)" in block
    assert "currencyOf(e && e.estimate_currency)" in block


def test_the_unknown_sentinel_never_renders_as_a_currency():
    js = setup_js()

    normalizer = js[js.index("function currencyOf(value)"):js.index("function currenciesDiffer(e)")]
    assert "text.toLowerCase() === 'unknown'" in normalizer, (
        "the sync stores the literal 'unknown'; as a unit it is noise, not a currency"
    )
    html = page()
    section = html[html.index("业绩对比"):html.index("Longbridge 一致预期")]
    assert "currencyOf(selectedEarning.estimate_currency) || '—'" in section
    assert "currencyOf(selectedEarning.actual_currency) || '—'" in section
    assert "currencyOf," in js, "the template can only call exported helpers"


# ── 4. the footnote has to describe the rule the code applies ──────────────

def test_the_footnote_states_the_current_comparison_rule():
    html = page()
    footnote = html[html.index("「较预期」需要两侧"):]
    footnote = footnote[:footnote.index("</p>")]

    assert "都标明币种且相同" in footnote
    assert "币种缺失" in footnote and "两币种不同" in footnote
    # The #92 decision: an unstated basis no longer blocks the comparison, so the
    # old wording would send users looking for a problem that is not there.
    assert "口径未标明" not in footnote
    assert "口径未经同一来源确认" not in footnote


# ── 5. tapping a day has to show the days ──────────────────────────────────

def test_a_selection_scrolls_the_panel_into_view_on_narrow_screens():
    js = setup_js()

    assert "const NARROW_VIEWPORT = '(max-width: 1023px)';" in js
    reveal = js[js.index("function revealSelectionPanel()"):js.index("function selectCell(cell)")]
    assert "window.matchMedia(NARROW_VIEWPORT).matches" in reveal
    assert "scrollIntoView({ behavior: 'smooth', block: 'start' })" in reveal


def test_both_selection_paths_ask_for_the_reveal():
    js = setup_js()

    cell_block = js[js.index("function selectCell(cell)"):js.index("function clearSelection()")]
    assert "revealSelectionPanel();" in cell_block

    earning_block = js[js.index("async function selectEarning("):js.index("function selectCell(cell)")]
    assert "revealSelectionPanel();" in earning_block


def test_the_panel_leaves_room_for_the_sticky_header():
    html = page()

    assert ".selection-panel { scroll-margin-top: 52px; }" in html, (
        "without this the scrolled panel lands under the sticky header"
    )

# ── the day list's figures ──────────────────────────────────────────────────

def test_the_day_row_figures_share_one_fixed_column():
    """Value and beat/miss percentage used to be two auto-width flex items.

    The percentage therefore floated further right than the number it belongs to,
    and both moved whenever a digit count changed — the list read as ragged.
    """
    html = page()
    css = html[html.index(".day-row-num {"):html.index("}", html.index(".day-row-num {"))]

    assert "width: 74px" in css, "the column must be fixed width to line up across rows"
    assert "text-align: right" in css
    assert "font-variant-numeric: tabular-nums" in css, (
        "proportional digits defeat the alignment the fixed column provides"
    )


def test_the_percentage_sits_under_the_value_in_the_same_cell():
    html = page()
    row = html[html.index('class="day-row-main day-row-num'):html.index('class="day-row-action')]

    # Both lines belong to the numeric button; a third flex sibling would drift again.
    assert "fmtNum(e.eps_actual)" in row
    assert "fmtPct(epsSurplus(e))" in row
    assert row.count('class="block') >= 2


def test_the_watchlist_toggle_is_an_icon_not_a_word():
    html = page()
    row = html[html.index('class="day-row-action'):html.index("</button>", html.index('class="day-row-action'))]

    assert "☆ 加自选" not in row and "★ 已自选" not in row, (
        "the label made the star as wide as the numbers column and pushed them apart"
    )
    assert 'aria-label="' in row, "an icon-only button still needs an accessible name"


def test_the_estimate_is_labelled_when_no_actual_exists():
    html = page()
    row = html[html.index('class="day-row-main day-row-num'):html.index('class="day-row-action')]

    assert "预期 {{ fmtNum(e.eps_estimate) }}" in row
