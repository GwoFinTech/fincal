"""Issue #157: 表格级的一致预期年龄——自选表与「本周财报」摘要也要说自己有多旧。

`#153` 只给详情面板加上了年龄，读者要安排下一两周时用的却是另外两个表面：

* 「自选」页两张表（`#watchlist`）逐行印 `eps_estimate` / `revenue_estimate`，
  表头写着「下次财报 · 一致预期」，没有任何日期；
* 「本周财报」摘要的每一行印 `row.eps_estimate`。

生产库 2026-11-28 的快照：未来事件带 EPS 预期的 3,913 行里 `estimate_as_of`
最新是 **2026-11-08**（20 天前），另有 10-26 / 10-29 / 10-31 / 11-02 的数——
`longbridge` / `consensus` 两个阶段已经连续三周 failed，所以一张表里可能同时
存在几个日期的行。这里钉住：

* 报**屏上这些行**的区间（最早/最新），同一天就只印那一天；
* 超过 `ESTIMATE_STALE_DAYS`（与 `#153` 同一个常量、与后端 192h 同源）才追加
  「每周同步近期失败」，8 天以内只陈述年龄；
* 只统计真的带了预期的行：算法预测日期、没有一致预期的事件不会拉低年龄；
* 一行带预期的都没有时返回空串，模板整行不渲染——不印「0 天前」这种假年龄。

行为断言跑 node 里**已服务**的 `useFormatters()`（`frontend_contract.function_source`
抽出），不是在 Python 里重写一遍规则。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import TestCase, skipUnless

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)

ROOT = fc.ROOT
sys.path.insert(0, str(ROOT))

#: 2026-11-28 08:50 CST——生产快照最新 11-08（20 天前），最老 10-26（33 天前）.
NOW = "2026-11-28T08:50:00+08:00"


def _row(stamp, eps=1.0, revenue=100.0):
    return {"eps_estimate": eps, "revenue_estimate": revenue, "estimate_as_of": stamp}


CASES = {
    # 生产自选页（18 个标的）第一行未来事件的真实分布：14 行 11-08，ORCL 一行 10-31。
    "watchlist": [
        _row("2026-11-08T20:58:28.039047+08:00"),
        _row("2026-11-08T12:55:33.473308+08:00", eps=-0.15, revenue=2.0),
        _row("2026-10-31T02:41:12.620218+08:00", eps=1.52648, revenue=21325007560.0),
    ],
    # 同一次同步写入的一整页：只印那一天。
    "same_day": [
        _row("2026-11-08T20:58:28+08:00"),
        _row("2026-11-08T13:09:33+08:00"),
    ],
    # 8 天以内：陈述年龄，不追加告警。
    "fresh": [_row("2026-11-22T09:00:00+08:00")],
    # 边界：第 8 天已经算过期（与 #153 的 ESTIMATE_STALE_DAYS 同一条线）。
    "exactly_threshold": [_row("2026-11-20T09:00:00+08:00")],
    # 算法预测日期：没有一致预期，也不该被当成「很旧的快照」。
    "predicted_only": [
        {"eps_estimate": None, "revenue_estimate": None, "estimate_as_of": None},
        {"eps_estimate": None, "revenue_estimate": None, "estimate_as_of": "2026-10-01T00:00:00+08:00"},
    ],
    # 有预期但没有时间戳：不猜年龄，直接不参与统计。
    "no_stamp": [{"eps_estimate": 1.0, "revenue_estimate": 2.0, "estimate_as_of": None}],
    "empty": [],
}

PROBE = r"""
{factory}
const fmt = useFormatters();
const now = new Date({now});
const cases = {cases};
const out = {{}};
for (const [key, rows] of Object.entries(cases)) {{
  out[key] = {{
    note: fmt.estimateSnapshotNote(rows, now),
    stale: fmt.estimateSnapshotIsStale(rows, now),
  }};
}}
console.log(JSON.stringify(out));
"""


def _run_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useFormatters")
    probe = PROBE.format(factory=source, now=json.dumps(NOW), cases=json.dumps(CASES))
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class EstimateSnapshotNoteTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _run_probe()

    def test_the_watchlist_note_reports_the_span_of_the_rows_on_screen(self):
        """14 行 11-08 + 1 行 10-31：只报最新的那天会把 ORCL 那一行说轻 8 天。"""
        self.assertEqual(
            self.out["watchlist"]["note"],
            "一致预期为 2026/10/31 至 2026/11/8 的快照（最早 28 天前）"
            "：每周同步近期失败，这些数可能不是最新的",
        )
        self.assertTrue(self.out["watchlist"]["stale"])

    def test_one_snapshot_day_prints_that_day_only(self):
        """同一次同步写入的一页印「10/31 至 11/8」是没有的事。"""
        self.assertEqual(
            self.out["same_day"]["note"],
            "一致预期为 2026/11/8 的快照（20 天前）：每周同步近期失败，这些数可能不是最新的",
        )

    def test_fresh_rows_state_the_age_without_a_warning(self):
        """8 天以内多喊一句「可能过时」= 每周都狼来了（#153 的同一条取舍）。"""
        note = self.out["fresh"]["note"]
        self.assertEqual(note, "一致预期为 2026/11/22 的快照（6 天前）")
        self.assertNotIn("同步", note)
        self.assertFalse(self.out["fresh"]["stale"])

    def test_the_stale_threshold_is_the_one_153_uses(self):
        self.assertEqual(
            self.out["exactly_threshold"]["note"],
            "一致预期为 2026/11/20 的快照（8 天前）：每周同步近期失败，这些数可能不是最新的",
        )
        self.assertTrue(self.out["exactly_threshold"]["stale"])

    def test_rows_without_an_estimate_do_not_join_the_span(self):
        """预测日期没有一致预期：它既不是「今天的数」，也不该被说成很旧。"""
        self.assertEqual(self.out["predicted_only"]["note"], "")
        self.assertFalse(self.out["predicted_only"]["stale"])

    def test_no_timestamp_means_no_note(self):
        for key in ("no_stamp", "empty"):
            self.assertEqual(self.out[key]["note"], "", key)
            self.assertFalse(self.out[key]["stale"], key)


class TemplateWiringTests(TestCase):
    """静态断言：两个表面真的渲染了这一行，且带 `--warning` 分支。"""

    @classmethod
    def setUpClass(cls):
        cls.html = fc.read_index()
        cls.js = fc.read_app_js()

    def test_the_watchlist_tables_render_the_note(self):
        self.assertIn('v-if="watchlistSnapshotNote"', self.html)

    def test_the_week_digest_renders_the_note(self):
        self.assertIn('v-if="weekAheadSnapshotNote"', self.html)

    def test_both_notes_switch_to_the_warning_token_when_stale(self):
        """不新增颜色 token：仓库既有的 `--warning` 就是「注意，不是涨跌」。"""
        self.assertIn(
            ":style=\"watchlistSnapshotStale ? 'color:var(--warning);' : 'color:var(--text-muted);'\"",
            self.html,
        )
        self.assertIn(
            ":style=\"weekAheadSnapshotStale ? 'color:var(--warning);' : 'color:var(--text-muted);'\"",
            self.html,
        )

    def test_the_notes_are_exposed_to_the_template(self):
        exposed = fc.exposed_setup_keys(self.js)
        for name in (
            "watchlistSnapshotNote", "watchlistSnapshotStale",
            "weekAheadSnapshotNote", "weekAheadSnapshotStale",
            "estimateSnapshotNote", "estimateSnapshotIsStale",
        ):
            self.assertIn(name, exposed, name)

    def test_the_watchlist_note_reads_the_rows_the_tables_render(self):
        """表格渲染 `watchlistInsight`（= `nextBySymbol` 的第一条未来事件），
        年龄必须来自同一批行，而不是另一份 420 天窗口里的全部事件。"""
        self.assertIn(
            "computed(() => [...wl.nextBySymbol.value.values()])",
            self.js,
        )
        self.assertIn("watchlistInsight(w).eps_estimate", self.html)
