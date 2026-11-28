"""管理弹窗的同步记录不该给一个「不按标的跑」的阶段印「标的 0」。

`longbridge` 与 `stock_names` 两个阶段不是按标的执行：前者拉整个财报日历，后者只补
空名字。它们的 `sync_runs.symbol_count` 因此一直是 0，而管理弹窗把它和记录数并列：

    2026/11/23 08:01:04 · 标的 0 · 记录 3      （stock_names success）
    2026/11/8 20:55:54  · 标的 0 · 记录 25138  （longbridge success，生产库原值）

「跑了 0 个标的却写入 25,138 条记录」读起来像审计数据坏了，实际是这个字段对这两类
阶段没有意义。有标的范围的阶段（futu / prediction / consensus = 116）保持原样。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)


class AdminSyncRunScopeTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = fc.read_index()
        start = cls.html.index('v-for="run in syncRuns"')
        cls.row = cls.html[start:start + 1200]

    def test_a_stage_without_a_symbol_scope_does_not_claim_one(self):
        self.assertIn('<template v-if="run.symbol_count > 0"> · 标的 {{ run.symbol_count }}</template>', self.row)

    def test_the_old_unconditional_label_is_gone(self):
        self.assertNotIn(' · 标的 {{ run.symbol_count }} · 记录', self.row)

    def test_the_record_count_still_renders(self):
        """记录数是每个阶段都有的真实数字，不能顺手一起藏掉。"""
        self.assertIn('· 记录 {{ run.record_count }}', self.row)
