"""Accessibility and consistency guard rails for the single-file SPA.

The layout review found four P2 problems, all invisible to a click test but real:

* the dark theme never declared `color-scheme`, so every native widget (checkbox,
  select popup, scrollbars) was painted by the light theme — the 只看自选 checkbox
  was a white square on a near-black bar;
* `--text-muted` was `#71717a`: 3.67:1 on a surface and 3.40:1 on a raised card — below
  AA for the 10–12px text it is used for in 50 places, while `zinc-600`/`zinc-700`
  came in at 2.29:1 and 1.91:1;
* the calendar grid and the day list were mouse-only (div + inline
  `@mouseenter` style mutation), and both dialogs opened with no role, no Esc, no
  focus trap and no focus owner at all;
* `.ui-tab` was used in the markup but never defined, two button systems coexisted, and
  `.skeleton` was defined and never used (so the first paint was an empty grid).

Contrast is computed here rather than eyeballed, so lowering the token again fails.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INDEX = ROOT / "app" / "static" / "index.html"
APP_JS = ROOT / "app" / "static" / "assets" / "app-setup.js"

SURFACE = "#18181b"        # --surface
SURFACE_RAISED = "#1f1f23"  # --surface-raised


def page() -> str:
    return INDEX.read_text(encoding="utf-8")


def setup_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _token(name: str) -> str:
    match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", page())
    assert match, f"--{name} must be a literal hex token so its contrast is checkable"
    return match.group(1)


# ── 6. the dark theme has to declare itself ─────────────────────────────────

def test_the_document_tells_the_browser_it_is_dark():
    html = page()
    root = html[html.index(":root {"):html.index("}", html.index(":root {"))]

    assert "color-scheme: dark;" in root, (
        "without it every native widget (checkbox, select popup, scrollbar) is drawn "
        "by the light theme on a near-black page"
    )


# ── 7. contrast ────────────────────────────────────────────────────────────

def test_muted_text_reaches_aa_on_both_surfaces_it_sits_on():
    muted = _token("text-muted")

    for background in (SURFACE, SURFACE_RAISED):
        ratio = _contrast(muted, background)
        assert ratio >= 4.5, (
            f"--text-muted {muted} on {background} is {ratio:.2f}:1 — used at 10–12px "
            "in 50 places, so it has to clear 4.5:1"
        )


def test_secondary_text_stays_easy_to_read():
    secondary = _token("text-secondary")

    # Secondary is the "normal" text colour next to --text; it must not drift down
    # towards muted (7:1 is the AAA bar and it already clears it).
    assert _contrast(secondary, SURFACE_RAISED) >= 6.0


def test_the_pale_tailwind_greys_are_pointed_at_the_token():
    html = page()

    # Tailwind's own zinc-500/600/700 skip the palette; the override redirects them
    # instead of leaving a second, unreadable grey scale in the page.
    assert ".text-zinc-500, .text-zinc-600, .text-zinc-700 { color: var(--text-muted); }" in html
    # zinc-700 on the page background was 1.91:1 — nothing may reintroduce it.
    assert "text-zinc-700" not in html.replace(
        ".text-zinc-500, .text-zinc-600, .text-zinc-700 { color: var(--text-muted); }", "")
    # zinc-600 may stay in the markup: the grouped override above is what makes it
    # readable, and avoiding a rewrite of every class attribute is its whole point.
    assert "zinc-600" in html and ".text-zinc-600, .text-zinc-700 { color: var(--text-muted); }" in html


# ── 8. keyboard operation ──────────────────────────────────────────────────

def test_a_calendar_cell_is_a_named_keyboard_control():
    html = page()
    start = html.index('class="calendar-cell"')
    cell = html[start:html.index(">", html.index('@keydown.space.prevent="selectCell(cell)"', start))]

    assert 'tabindex="0"' in cell
    assert ':aria-label="cellAriaLabel(cell)"' in cell
    assert '@keydown.enter.prevent="selectCell(cell)"' in cell
    assert '@keydown.space.prevent="selectCell(cell)"' in cell


def test_a_cell_announces_its_date_and_how_many_reports_it_holds():
    js = setup_js()
    block = js[js.index("function cellAriaLabel(cell)"):js.index("return {\n      viewMode")]

    assert "toLocaleDateString" in block
    assert "个财报" in block
    # Exported, or the template cannot call it.
    assert "cellAriaLabel," in js


def test_calendar_badges_are_real_buttons():
    html = page()
    block = html[html.index('class="cal-scroll'):html.index("<!-- Legend -->")]

    assert '<button v-for="e in cell.earnings.slice(0, 4)"' in block
    assert 'type="button"' in block
    assert ":aria-label=\"e.symbol" in block
    assert block.count("</button>") >= 1


def test_a_day_row_is_two_buttons_not_a_div_with_inline_hover():
    html = page()
    block = html[html.index('class="day-row flex items-center'):html.index("当天暂无财报")]

    assert "@mouseenter" not in block, (
        "faking hover by mutating $event.target.style repaints whatever child the "
        "pointer landed on, and leaves the row keyboard-unreachable"
    )
    assert "@mouseleave" not in block
    assert 'class="day-row flex items-center' in block
    assert block.count('class="day-row-main') == 2, "open-details is offered on the left and on the numbers"
    assert 'class="day-row-action' in block, "the watchlist toggle stays its own control"


def test_the_rows_and_cells_show_a_focus_ring():
    html = page()

    for selector in (".calendar-cell:focus-visible", ".calendar-cell .badge:focus-visible",
                     ".day-row-main:focus-visible, .day-row-action:focus-visible"):
        assert selector in html, f"{selector} needs an outline — a focus ring is not optional"


# ── 8b. dialogs ────────────────────────────────────────────────────────────

def test_both_dialogs_declare_their_role_and_name():
    html = page()

    assert html.count('role="dialog"') == 2
    assert html.count('aria-modal="true"') == 2
    assert 'aria-labelledby="ical-dialog-title"' in html
    assert 'aria-labelledby="admin-dialog-title"' in html
    assert 'id="ical-dialog-title"' in html
    assert 'id="admin-dialog-title"' in html
    # Focusable so it can hold focus when it has no control to land on.
    assert html.count('tabindex="-1"') >= 2


def test_escape_closes_each_dialog_and_tab_stays_inside():
    html = page()

    assert html.count('@keydown.esc.stop') == 2
    assert html.count('@keydown.tab="trapTab"') == 2
    assert 'aria-label="关闭管理面板"' in html, "an ✕ without a name is announced as 'button'"


def test_the_focus_owner_is_wired_up():
    js = setup_js()
    block = js[js.index("function trapTab(event)"):js.index("// ── Earnings loading")]

    assert "dialog.querySelectorAll(" in block
    assert "event.preventDefault();" in block, "Tab at the last control must wrap, not escape"
    assert "document.body.style.overflow = 'hidden';" in block, "the page behind must not scroll"
    assert "if (dialogReturnFocus && dialogReturnFocus.focus) dialogReturnFocus.focus();" in block, (
        "closing has to return focus to whatever opened the dialog"
    )
    # The ref only exists after Vue has patched the DOM, so a pre-flush watcher would
    # see `null`, treat it as "closed" and never focus anything (that bug shipped once).
    assert "{ flush: 'post' }" in block


# ── 9. dead code ───────────────────────────────────────────────────────────

def test_the_undefined_tab_class_is_gone():
    assert "ui-tab" not in page()


def test_only_one_button_system_remains():
    html = page()

    for legacy in ('class="btn ', 'class="btn-primary', 'class="btn-ghost', 'class="btn-outline'):
        assert legacy not in html
    assert ".btn {" not in html
    assert ".ui-tab {" not in html


def test_the_skeleton_is_used_not_just_defined():
    html = page()
    grid = html[html.index('class="cal-scroll'):html.index("<!-- Legend -->")]

    assert ".skeleton {" in html, "the style must still exist"
    assert 'class="skeleton"' in grid, (
        "the first paint used to be an empty grid: the skeleton was defined and unused"
    )
    assert 'v-if="loading && !earnings.length"' in grid
