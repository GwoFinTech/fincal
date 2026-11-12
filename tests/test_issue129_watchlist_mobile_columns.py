"""Issue #129: the watchlist tables must fit a phone, with 移除 still on screen.

Both tables were `class="w-full min-w-[620px]"` inside `overflow-x-auto`. On a
390px viewport the rightmost 「操作」 column — the only way to remove a symbol —
started outside the viewport, so the phone UI read as 「没有删除入口」: the
control existed, but you had to guess that the table scrolled sideways.

The fix drops the fixed 620px floor, hides the 营收预期 column below `sm` (the
information-vs-operability trade recorded in the issue) and tightens the gutters,
so 代码/公司 · 下次财报 · EPS 预期 · 操作 fit in 390px. Desktop keeps every column.

Layout itself can only be proven in a browser, so these are the structural pins;
the 390×844 pass is recorded on the issue.
"""
import re
import sys
from pathlib import Path
from unittest import TestCase

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)


def _tables(html: str) -> list[str]:
    return [html[pos:] for pos in _find_all(html, '<table class="w-full watchlist-table')]


def _find_all(haystack: str, needle: str) -> list[int]:
    positions, start = [], 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return positions
        positions.append(index)
        start = index + 1


class WatchlistPhoneColumnsTests(TestCase):
    def setUp(self):
        self.html = fc.read_index()

    def test_neither_table_keeps_a_width_below_the_phone_viewport(self):
        # The CSS comment quotes the old value on purpose; what must not come back
        # is the class on the table itself.
        assert "min-w-[620px]" not in self.html.replace("`min-w-[620px]`", ""), (
            "a 620px floor inside overflow-x-auto puts the 操作 column off-screen on a phone"
        )
        self.assertEqual(len(_tables(self.html)), 2, "both markets must be changed, not just one")

    def test_the_revenue_column_is_desktop_only(self):
        # Product trade recorded in the issue: revenue is the least actionable of
        # the four columns, so it is the one that goes on a narrow screen.
        self.assertEqual(self.html.count('<th class="hidden sm:table-cell text-right px-3 py-2 font-medium">营收预期</th>'), 2)
        self.assertEqual(
            self.html.count('<td class="hidden sm:table-cell px-3 py-2.5 text-right">{{ fmtBigNum(watchlistInsight(w).revenue_estimate) }}</td>'),
            2,
        )

    def test_the_remove_control_is_never_hidden(self):
        banner = self.html[self.html.index("我的自选"):]
        remove_cell = 'class="px-4 py-2.5 text-right"><button @click="removeFromWatchlist'
        self.assertEqual(banner.count(remove_cell), 2)
        cell = banner[banner.index(remove_cell):]
        self.assertNotIn("hidden", cell[:cell.index(">")])

    def test_the_phone_rule_drops_the_floor_and_tightens_the_gutters(self):
        css = self.html[:self.html.index("</style>")]
        assert ".watchlist-table { min-width: 0; }" in css
        media = css[css.index("@media (max-width: 639px)"):]
        media = media[:media.index("}\n    }") + 1]
        assert ".watchlist-table th, .watchlist-table td { padding-left: 5px; padding-right: 5px; }" in media
        # Same four columns still fit: the company sub-line and the button shrink.
        assert ".watchlist-table .wl-company { max-width: 92px; }" in media
        assert ".watchlist-table .watchlist-remove { padding: 4px 6px; }" in media

    def test_the_desktop_column_set_is_unchanged(self):
        header = '<th class="text-left px-4 py-2 font-medium">代码 / 公司</th>'
        self.assertEqual(self.html.count(header), 2)
        for label, klass in (
            ("下次财报", '<th class="text-left px-3 py-2 font-medium">下次财报</th>'),
            ("EPS 预期", '<th class="text-right px-3 py-2 font-medium">EPS 预期</th>'),
            ("操作", '<th class="text-right px-4 py-2 font-medium">操作</th>'),
        ):
            self.assertEqual(self.html.count(klass), 2, label)


# ── The class has to exist in the *shipped* stylesheet ───────────────────
# `hidden sm:table-cell` hides the column on a phone, but `sm:table-cell` is a
# Tailwind utility that only exists in the committed `tailwind.css`. That file is
# pre-rendered (`scripts/build_frontend.sh`) and is the source of truth for
# deploys, so a markup change without a rebuild is invisible in code review and
# in the markup tests — and the column stays hidden at *every* width. That is
# exactly what happened to this issue's fix on its first deploy.

ESCAPED = '.[]/%:#(),!*+~<>="'


def _escape(token: str) -> str:
    return "".join("\\" + ch if ch in ESCAPED else ch for ch in token)


def _shipped_css() -> str:
    return (fc.ROOT / "app" / "static" / "assets" / "tailwind.css").read_text(encoding="utf-8")


def _local_style_block() -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", fc.read_index(), re.S))


def _static_classes() -> set[str]:
    tokens: set[str] = set()
    for attr in re.findall(r'(?<!:)class="([^"]*)"', fc.read_index()):
        if "{{" in attr or "$" in attr:
            continue
        tokens.update(attr.split())
    return {t for t in tokens if not t.startswith("{") and not t.endswith("}")}


class ShippedStylesheetCoversTheMarkupTests(TestCase):
    def test_every_static_class_is_in_the_prerendered_tailwind_css(self):
        css = _shipped_css()
        local = _local_style_block()
        missing = sorted(
            token for token in _static_classes()
            if f".{_escape(token)}" not in css
            and not re.search(r"\." + re.escape(token) + r"[\s,{:.]", local)
        )
        self.assertEqual(
            missing, [],
            "these classes are used in index.html but missing from "
            "app/static/assets/tailwind.css — run `bash scripts/build_frontend.sh`: " + ", ".join(missing),
        )

    def test_the_phone_rule_utility_exists(self):
        css = _shipped_css()
        assert ".sm\\:table-cell" in css and "@media (min-width:640px)" in css, (
            "without `sm:table-cell` in the shipped CSS, `hidden` wins at every width "
            "and the revenue column disappears on desktop too"
        )

    def test_the_build_script_runs_tailwind_from_the_repo_root(self):
        script = (fc.ROOT / "scripts" / "build_frontend.sh").read_text(encoding="utf-8")
        # The content globs are relative to the repo root, so the CLI must run
        # there; from the scratch build dir it matches nothing and emits a
        # utility-less stylesheet.
        assert 'cd "$ROOT"' in script
        assert '"$BUILD/node_modules/.bin/tailwindcss"' in script
        assert "contains no utilities" in script, "a scan that matched nothing must fail the build"

