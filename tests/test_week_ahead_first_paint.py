"""「本周财报」首屏不能先替读者宣布一个它还不知道的范围，也不能先宣布「没有财报」。

挂载顺序是 `loadWatchlist()` → `loadEarnings()`，而摘要的范围来自
`mineOnly = watchlistOnly || wl.watchlist.value.length > 0`、条数来自日历那一批：

* 自选请求回来之前 `wl.watchlist` 是空的，于是一个有自选的账号会先被写成
  「全部标的」；
* `earnings` 初始为空、`loading` 只有在请求**开始**时才置真，于是首帧同时宣布
  「本周无财报」。

浏览器实测（1440px，本地副本，`performance.now()` 采样）：

    308ms  11/28 → 12/4 · 全部标的  本周无财报
    442ms  11/28 → 12/4 · 你的自选  读取中…
    614ms  11/28 → 12/4 · 你的自选  本周无财报

在慢网络里那两句话会停留整个自选往返。这里钉住修正后的契约：

* 身份未知（`/api/me` 未回答）时不写范围；已登录但自选未回答时也不写；
* 日历那一批未回答时写「读取中…」，不写「本周无财报」；
* 登录前的访客仍是「全部标的」（没有自选，范围本来就是全部）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)


class WeekAheadFirstPaintTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = fc.read_index()
        cls.js = fc.read_app_js()
        cls.header = cls.html[cls.html.index('aria-label="本周财报"'):]
        cls.header = cls.header[:cls.header.index('</section>')]

    def test_the_scope_is_withheld_until_it_is_known(self):
        self.assertIn(
            '{{ weekAhead.rangeLabel }}<template v-if="weekAheadScopeKnown"> · ', self.header,
        )
        # The old form stated 「全部标的」 on the very first paint, before `/api/me`
        # or the watchlist had answered.
        self.assertNotIn(
            '{{ weekAhead.rangeLabel }} · {{ weekAhead.mineOnly', self.header,
        )

    def test_unknown_data_reads_as_loading_not_as_an_empty_week(self):
        self.assertIn('v-else-if="!earningsLoaded"', self.header)
        self.assertLess(
            self.header.index('v-else-if="!earningsLoaded"'),
            self.header.index('本周无财报'),
            "「读取中…」 must win over the empty-week claim",
        )

    def test_the_expanded_body_also_waits(self):
        self.assertIn('v-else-if="!earningsLoaded"', self.html)
        body = self.html[self.html.index('id="week-ahead-body"'):]
        self.assertLess(
            body.index('v-else-if="!earningsLoaded"'),
            body.index('本周无财报'),
        )

    def test_the_scope_needs_identity_and_the_watchlist(self):
        start = self.js.index('const weekAheadScopeKnown = computed(')
        helper = self.js[start:start + 240]
        self.assertIn('userLoaded', helper)
        self.assertIn('watchlistLoaded', helper)
        # A visitor with no account still sees the whole universe: nothing else can
        # be meant by 「全部标的」.
        self.assertIn('!user.value', helper)

    def test_identity_and_the_batch_each_report_when_they_settle(self):
        loader = fc.function_source(self.js, 'loadUser')
        self.assertIn('userLoaded.value = true', loader)
        earnings = fc.function_source(self.js, 'loadEarnings')
        self.assertIn('earningsLoaded.value = true', earnings)
        # A failed read must not leave the digest stuck on 「读取中…」.
        tail = earnings[earnings.index('} finally {'):]
        self.assertIn('earningsLoaded.value = true', tail)

    def test_the_digest_still_says_all_symbols_for_a_visitor_without_a_watchlist(self):
        self.assertIn("'你的自选' : '全部标的'", self.header)
