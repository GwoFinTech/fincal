"""Issue #153: 读者必须看得出「一致预期」有多旧，管理员之外也一样。

`longbridge` / `consensus` 两个每周同步阶段连续三次失败（生产库 `sync_runs`
最后一次 success 分别是 2026-11-08 与 2026-11-02）之后，详情面板上的预期仍然
和当天采集的长得一模一样：只有一个静态时间戳，没有年龄、没有过期样式，而降级
信号只出现在管理员「诊断」页。读者会拿三周前的快照判断「超预期/不及预期」。

这里钉住四件事：

* 阈值 `ESTIMATE_STALE_DAYS` 与后端 `SYNC_STAGE_STALE_AFTER_HOURS` 同源
  （同一张表的做法同 `test_issue78_stage_timeouts`），防止两边漂移；
* `estimateAgeDays` 按**日历天**算（11-08 21:01 → 11-24 08:40 是 16 天，不是
  15 天），未来时间戳算 0 天，缺时间戳返回 None；
* 对比表下的那行小字与一致预期块：8 天以内只说「多久之前」，超过阈值才追加
  「已 N 天未更新」并改用 `--warning` 色——狼来了会让告警失效；
* 没有时间戳时整行不渲染（`v-if` 在空串上挡掉），不猜年龄也不印「—」。

行为断言跑 node 里**已服务**的 `useFormatters()`（`frontend_contract.function_source`
抽出），不是在 Python 里重写一遍公式。
"""
from __future__ import annotations

import json
import re
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

from app import config  # noqa: E402

#: The two production timestamps from the issue, read at 2026-11-24 08:40 CST.
NOW = "2026-11-24T08:40:00+08:00"
ESTIMATE_AS_OF = "2026-11-08T21:01:00+08:00"
CONSENSUS_FETCHED_AT = "2026-11-02T08:20:00+08:00"

CASES = {
    # key: [value, expected age, expected stale]
    "issue_row": [ESTIMATE_AS_OF, 16, True],
    "issue_consensus": [CONSENSUS_FETCHED_AT, 22, True],
    # Boundary: the 8th day is already late (192h / 24), the 7th is not.
    "exactly_threshold": ["2026-11-16T00:00:00+08:00", 8, True],
    "just_inside": ["2026-11-17T00:00:00+08:00", 7, False],
    "same_day": ["2026-11-24T01:00:00+08:00", 0, False],
    "future_clock": ["2026-11-25T09:00:00+08:00", 0, False],
    "missing": [None, None, False],
    "empty": ["", None, False],
    "garbage": ["not-a-date", None, False],
}

PROBE = r"""
{factory}
const fmt = useFormatters();
const now = new Date({now});
const cases = {cases};
const out = {{}};
for (const [key, value] of Object.entries(cases)) {{
  out[key] = {{
    age: fmt.estimateAgeDays(value, now),
    ageByDefaultNow: fmt.estimateAgeDays(value),
    stale: fmt.estimateIsStale(value, now),
    suffix: fmt.estimateAgeSuffix(value, now),
    note: fmt.estimateFreshnessNote(value, now),
    staleNote: fmt.estimateStaleNote(value, now),
  }};
}}
console.log(JSON.stringify(out));
"""


def _run_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useFormatters")
    probe = PROBE.format(
        factory=source,
        now=json.dumps(NOW),
        cases=json.dumps({key: value for key, value in
                          ((k, v[0]) for k, v in CASES.items())}),
    )
    completed = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _constant_days() -> int:
    js = fc.read_app_js()
    match = re.search(r"(?m)^[ \t]*const ESTIMATE_STALE_DAYS = (\d+);", js)
    assert match, "ESTIMATE_STALE_DAYS is missing from useFormatters()"
    return int(match.group(1))


class ThresholdParityTests(TestCase):
    def test_the_frontend_threshold_is_the_backend_one_in_days(self):
        """`SYNC_STAGE_STALE_AFTER_HOURS` 192h and 8 days must stay one number."""
        hours = float(config.SYNC_STAGE_STALE_AFTER_HOURS)
        self.assertEqual(
            _constant_days(), int(hours / 24),
            "ESTIMATE_STALE_DAYS drifted from SYNC_STAGE_STALE_AFTER_HOURS",
        )


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class EstimateAgeTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _run_probe()

    def test_calendar_days_match_the_issue_observation(self):
        """16 / 22 天是读者对日期的结论，不是满 24 小时的块数。"""
        self.assertEqual(self.out["issue_row"]["age"], 16)
        self.assertEqual(self.out["issue_consensus"]["age"], 22)

    def test_the_threshold_is_inclusive_and_only_there(self):
        self.assertEqual(self.out["exactly_threshold"]["age"], 8)
        self.assertTrue(self.out["exactly_threshold"]["stale"])
        self.assertEqual(self.out["just_inside"]["age"], 7)
        self.assertFalse(self.out["just_inside"]["stale"])
        self.assertFalse(self.out["same_day"]["stale"])

    def test_no_timestamp_means_no_text(self):
        """提供方自带的那一对没有采集时间——不渲染年龄，也不印占位符。"""
        for key in ("missing", "empty", "garbage"):
            self.assertIsNone(self.out[key]["age"], key)
            self.assertEqual(self.out[key]["suffix"], "", key)
            self.assertEqual(self.out[key]["note"], "", key)
            self.assertEqual(self.out[key]["staleNote"], "", key)
            self.assertFalse(self.out[key]["stale"], key)

    def test_a_future_stamp_never_prints_a_negative_age(self):
        self.assertEqual(self.out["future_clock"]["age"], 0)
        self.assertEqual(self.out["future_clock"]["suffix"], "（0 天前）")
        self.assertFalse(self.out["future_clock"]["stale"])

    def test_the_table_note_says_the_date_and_the_age(self):
        self.assertEqual(
            self.out["issue_row"]["note"],
            "预期值采集于 2026/11/8（16 天前）"
            "，已 16 天未更新，每周同步近期失败，这个数可能不是最新的",
        )

    def test_inside_the_threshold_the_note_carries_no_warning(self):
        """8 天以内多加一句「可能过时」= 每周都喊一次狼来了。"""
        inside = self.out["just_inside"]["note"]
        self.assertEqual(inside, "预期值采集于 2026/11/17（7 天前）")
        self.assertNotIn("未更新", inside)
        self.assertEqual(self.out["just_inside"]["staleNote"], "")

    def test_the_consensus_row_gets_a_parenthesised_suffix(self):
        self.assertEqual(self.out["issue_consensus"]["suffix"], "（22 天前）")
        self.assertEqual(
            self.out["issue_consensus"]["staleNote"],
            "已 22 天未更新：每周同步近期失败，这个数可能不是最新的",
        )

    def test_the_default_now_argument_still_works(self):
        """模板只传时间戳：不传 `now` 时也得是个可比较的整数。"""
        self.assertIsInstance(self.out["issue_row"]["ageByDefaultNow"], int)
        self.assertGreater(self.out["issue_row"]["ageByDefaultNow"], 0)


class TemplateWiringTests(TestCase):
    """静态断言：模板真的调用了这些函数，且调用点带着 warning 分支。"""

    @classmethod
    def setUpClass(cls):
        cls.html = fc.read_index()
        cls.js = fc.read_app_js()

    def test_the_comparison_table_reads_its_own_timestamp(self):
        self.assertIn(
            'v-if="estimateFreshnessNote(selectedEarning.estimate_as_of)"',
            self.html,
        )

    def test_the_consensus_block_ages_its_fetch_time(self):
        self.assertIn(
            'estimateAgeSuffix(selectedEarning.consensus_fetched_at)',
            self.html,
        )
        self.assertIn(
            'v-if="estimateStaleNote(selectedEarning.consensus_fetched_at)"',
            self.html,
        )

    def test_stale_lines_use_the_existing_warning_token(self):
        """不新增颜色 token：仓库既有的 `--warning` 就是「注意，不是涨跌」。"""
        # 对比表的年龄行、一致预期块的年龄后缀（都是绑定式），以及块尾的告警行。
        self.assertGreaterEqual(self.html.count("color:var(--warning);"), 3)
        self.assertIn('style="color:var(--warning);"', self.html)

    def test_the_helpers_are_exposed_to_the_template(self):
        exposed = fc.exposed_setup_keys(self.js)
        for name in (
            "estimateAgeDays", "estimateIsStale", "estimateAgeSuffix",
            "estimateFreshnessNote", "estimateStaleNote",
        ):
            self.assertIn(name, exposed, name)
