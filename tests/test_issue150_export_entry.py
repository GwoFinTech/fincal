"""Issue #150: 「导出 CSV」 必须真的能点到，且导出的是屏幕上那段区间。

`/api/export` 从 #47 起就在跑（55 列、UTF-8 BOM、attachment 文件名），README 与
系统自选页的文案都写着「导出」，但 ``grep -c "api/export"`` 在模板和 JS 里都是 0：
一个测过、文档承诺过的能力，用户只能自己拼 start/end 再手写带 forwardAuth 会话的
请求。

这里钉住四件事：

* 工具栏上有一个入口（在「订阅日历」左侧，登录才渲染）；
* 区间跟着当前视图：月 = 当月 1 日→月末，周 = 网格里那 7 天（与 `calendarCells`
  用同一套周锚点，不是另写一份公式）；
* 文件名与区间取自服务端的 ``Content-Disposition``，字节与直接请求接口一致；
* 401/422 只留一条 toast，不导航到 JSON 错误页。

行为用真实的 ``exportRange`` / ``useExport`` / ``useCalendar``（从服务的
``app-setup.js`` 里按函数名抽出）在 node vm + vendored Vue 里驱动，而不是在
Python 里重写一遍公式——重写的公式在服务代码改掉之后仍然会通过。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)

ROOT = fc.ROOT
APP_JS = fc.APP_JS
INDEX_HTML = fc.INDEX_HTML
VENDOR_VUE = ROOT / "app" / "static" / "assets" / "vendor" / "vue.global.prod.js"

CSV_NAME = "fincal-earnings-2026-11-01-2026-11-30.csv"
WEEK_EXPORT_URL = "/api/export?start=2026-11-15&end=2026-11-21&format=csv"

PROBE = r"""
const fs = require('fs');
const vm = require('vm');

const sandbox = {
  console, Promise, JSON, Math, Date, String, Number, Object, Array, Boolean, Intl,
  Error, TypeError, decodeURIComponent, encodeURIComponent, isNaN, parseInt, parseFloat,
  __timers: [],
};
sandbox.window = { location: { origin: 'https://fincal.test' } };
sandbox.setTimeout = (fn, ms) => { sandbox.__timers.push(ms); return sandbox.__timers.length; };
sandbox.clearTimeout = () => {};
sandbox.URL = {
  createObjectURL: (blob) => 'blob:mock/' + (blob && blob.size),
  revokeObjectURL: () => {},
};
sandbox.__clicks = [];
sandbox.document = {
  body: { appendChild() {} },
  createElement(tag) {
    return {
      tagName: tag, href: '', download: '',
      click() { sandbox.__clicks.push({ href: this.href, download: this.download }); },
      remove() {},
    };
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.env.VUE_PATH, 'utf8'), sandbox);
// The bundle's own first line does this; the extracted functions expect it.
vm.runInContext('const { ref, computed, watch, nextTick, onMounted } = Vue;', sandbox);

const js = fs.readFileSync(process.env.APP_JS, 'utf8');

// Extract the served functions by name (Python side, via frontend_contract's
// brace-aware slicer). A renamed/removed function fails there instead of
// probing a stale hand-written copy.
vm.runInContext(Object.values(JSON.parse(process.env.APP_FUNCS)).join('\n\n'),
  sandbox, { filename: 'app-setup.js' });

const driver = `
(async () => {
  function response({ status = 200, disposition = null, pending = null } = {}) {
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: { get: (name) => String(name).toLowerCase() === 'content-disposition' ? disposition : null },
      blob: () => pending || Promise.resolve({ size: 23491 }),
      text: () => Promise.resolve('{"detail":"bad request"}'),
    };
  }

  async function run(mode, y, m, d, resp, clicksBefore) {
    __clicks.length = 0;
    const calls = [];
    const fetched = [];
    globalThis.fetch = async (url) => { calls.push(url); return resp(); };
    const toasts = [];
    let unauthorized = 0;
    const viewMode = ref(mode);
    const currentDate = ref(new Date(y, m - 1, d, 12, 0, 0));
    const exp = useExport(viewMode, currentDate, (msg) => toasts.push(msg), () => { unauthorized += 1; });
    const label = exp.exportRangeLabel.value;
    const disabledBefore = exp.exporting.value;
    await exp.downloadExport();
    return {
      url: calls[0] || null,
      calls: calls.length,
      label,
      disabled_before: disabledBefore,
      disabled_after: exp.exporting.value,
      toasts,
      unauthorized,
      clicks: __clicks.slice(),
      timers: __timers.slice(),
    };
  }

  const out = {};
  // Month view: 11 月 19 日 → 当月 1 日..月末
  out.month = await run('month', 2026, 11, 19,
    () => response({ disposition: 'attachment; filename=fincal-earnings-2026-11-01-2026-11-30.csv' }));
  // Week view: 11 月 19 日（周四）→ 该周 11/15..11/21；没有头就用同一命名规则
  out.week = await run('week', 2026, 11, 19, () => response({ disposition: null }));
  // 跨月的一周：12 月 1 日（周二）→ 11/29..12/05
  out.week_cross = await run('week', 2026, 12, 1, () => response({ disposition: null }));
  // 401 / 422：只有 toast，没有下载，也没有导航
  out.unauthorized = await run('month', 2026, 11, 19, () => response({ status: 401 }));
  out.invalid = await run('month', 2026, 11, 19, () => response({ status: 422 }));

  // 连点两次只发一个请求（第二次在第一次的 Blob 落地前就被挡掉）
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const calls = [];
  globalThis.fetch = async (url) => { calls.push(url); return response({ pending: gate }); };
  const viewMode = ref('month');
  const currentDate = ref(new Date(2026, 10, 19));
  const exp = useExport(viewMode, currentDate, () => {}, () => {});
  const first = exp.downloadExport();
  const second = exp.downloadExport();
  release({ size: 1 });
  await Promise.all([first, second]);
  out.double_click = { calls: calls.length, disabled_after: exp.exporting.value };

  // 网格交叉验证：同一份 useCalendar 的周/月格子 vs exportRange 的区间
  const earnings = ref([]);
  const watchlistOnly = ref(false);
  const cal = useCalendar(earnings, null, watchlistOnly);
  function gridRange(mode, y, m, d) {
    cal.viewMode.value = mode;
    cal.currentDate.value = new Date(y, m - 1, d);
    const cells = cal.calendarCells.value;
    const shown = mode === 'week' ? cells : cells.filter((c) => c.inMonth);
    return {
      first: localYmd(shown[0].date),
      last: localYmd(shown[shown.length - 1].date),
      count: cells.length,
      label: cal.calendarRangeLabel.value,
    };
  }
  out.grid = {
    week: gridRange('week', 2026, 11, 19),
    week_cross: gridRange('week', 2026, 12, 1),
    month: gridRange('month', 2026, 11, 19),
  };
  out.exported = {
    week: exportRange(ref('week'), ref(new Date(2026, 10, 19))),
    week_cross: exportRange(ref('week'), ref(new Date(2026, 11, 1))),
    month: exportRange(ref('month'), ref(new Date(2026, 10, 19))),
  };
  return out;
})()
`;

vm.runInContext(driver, sandbox, { filename: 'app-setup.js' })
  .then((out) => console.log(JSON.stringify(out)))
  .catch((error) => {
    console.error(String((error && error.stack) || error));
    process.exit(1);
  });
"""

requires_node = pytest.mark.skipif(
    not shutil.which("node") or not VENDOR_VUE.exists(),
    reason="node and the vendored Vue build are needed for the composable probe",
)


PROBED_FUNCTIONS = ("localYmd", "monthDayLabel", "exportRange", "exportFilename",
                    "useExport", "useCalendar")


def _run_probe(app_js: Path = APP_JS) -> dict:
    js = app_js.read_text(encoding="utf-8")
    funcs = {name: fc.function_source(js, name) for name in PROBED_FUNCTIONS}
    env = {**os.environ, "VUE_PATH": str(VENDOR_VUE), "APP_JS": str(app_js),
           "APP_FUNCS": json.dumps(funcs)}
    completed = subprocess.run(
        ["node", "-e", PROBE], capture_output=True, text=True, timeout=120, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def probe() -> dict:
    return _run_probe()


# ── 结构：入口存在、位置正确、登录才渲染 ──────────────────────────────────

def _export_button_tag(html: str) -> str:
    """The ``<button>`` that carries ``@click="downloadExport"``, as served."""
    at = html.index('@click="downloadExport"')
    open_tag = html.rindex("<button", 0, at)
    return html[open_tag:html.index(">", at) + 1]


def test_the_toolbar_has_an_export_entry_left_of_the_subscription_button():
    html = INDEX_HTML.read_text(encoding="utf-8")
    tag = _export_button_tag(html)
    assert 'class="ui-btn ui-btn-outline"' in tag
    assert ":disabled=\"downloadingExport\"" in tag
    assert ":title=\"'导出 ' + exportRangeLabel" in tag
    # 下载图标，样式与「订阅日历」一致
    assert 'd="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 4v11"' in html
    assert "{{ downloadingExport ? '导出中…' : '导出 CSV' }}" in html
    # 说明文案承诺的「导出」现在站内可达：按钮就在「订阅日历」左侧。
    assert html.index('@click="downloadExport"') < html.index('@click="openIcal()"')
    # 未登录不渲染：日历工具栏的其它动作同样是 `v-if="user"`。
    assert 'v-if="user"' in tag


def test_the_setup_exposes_the_download_handler():
    js = APP_JS.read_text(encoding="utf-8")
    exposed = fc.exposed_setup_keys(js)
    for key in ("downloadExport", "downloadingExport", "exportRangeLabel"):
        assert key in exposed, key
    assert "const exp = useExport(cal.viewMode, cal.currentDate" in js
    # forwardAuth 下导航过去只会得到一页 JSON，所以下载必须走 fetch + Blob。
    # Comment text may name it; only real code counts.
    body = fc.function_source(js, "useExport")
    assert "window.location" not in fc.mask_code(body)
    assert "URL.createObjectURL(blob)" in body
    assert 'format=csv' in body


def test_the_template_has_no_dangling_bindings_with_the_new_button():
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    assert fc.dangling_bindings(html, fc.exposed_setup_keys(js)) == set()


# ── 行为：区间 = 屏幕上的区间 ────────────────────────────────────────────

@requires_node
def test_month_view_exports_the_whole_displayed_month(probe):
    month = probe["month"]
    assert month["url"] == "/api/export?start=2026-11-01&end=2026-11-30&format=csv"
    assert month["label"] == "2026-11-01 → 2026-11-30"
    assert month["clicks"] == [{"href": "blob:mock/23491", "download": CSV_NAME}]
    assert month["toasts"] == ["已导出 2026-11-01 → 2026-11-30"]
    assert month["disabled_before"] is False and month["disabled_after"] is False
    # Blob 先落地再 revoke，否则部分浏览器落一个空文件。
    assert month["timers"] == [1000]


@requires_node
def test_week_view_exports_the_seven_days_on_screen(probe):
    week = probe["week"]
    assert week["url"] == WEEK_EXPORT_URL
    assert week["clicks"][0]["download"] == "fincal-earnings-2026-11-15-2026-11-21.csv"
    assert week["toasts"] == ["已导出 2026-11-15 → 2026-11-21"]


@requires_node
def test_a_week_that_crosses_a_month_boundary_follows_the_anchor(probe):
    cross = probe["week_cross"]
    assert cross["url"] == "/api/export?start=2026-11-29&end=2026-12-05&format=csv"
    assert cross["label"] == "2026-11-29 → 2026-12-05"


@requires_node
def test_export_range_matches_the_grid_the_reader_is_looking_at(probe):
    """导出的区间必须与同一次 `useCalendar` 渲染出来的格子是同一段。"""
    grid, exported = probe["grid"], probe["exported"]
    assert (exported["month"]["start"], exported["month"]["end"]) == (grid["month"]["first"], grid["month"]["last"])
    for key in ("week", "week_cross"):
        cell_range = (grid[key]["first"], grid[key]["last"])
        assert (exported[key]["start"], exported[key]["end"]) == cell_range, key
    # 周视图的 7 格正好是那 7 天，不是整月；标签也是同一段。
    assert grid["week"]["count"] == 7
    assert grid["week"]["label"] == "11/15 → 11/21"
    assert grid["week_cross"]["label"] == "11/29 → 12/5"
    assert probe["week"]["label"] == exported["week"]["start"] + " → " + exported["week"]["end"]


@requires_node
def test_an_expired_session_shows_a_toast_instead_of_navigating(probe):
    expired = probe["unauthorized"]
    assert expired["clicks"] == []
    assert expired["toasts"] == ["登录状态已失效，未导出（HTTP 401），请重新登录后重试"]
    assert expired["unauthorized"] == 1
    assert expired["disabled_after"] is False


@requires_node
def test_a_rejected_range_reports_the_status_without_a_download(probe):
    invalid = probe["invalid"]
    assert invalid["clicks"] == []
    assert invalid["toasts"] == ["导出失败：当前区间不被接口接受（HTTP 422）"]
    assert invalid["unauthorized"] == 0


@requires_node
def test_a_double_click_does_not_queue_a_second_file(probe):
    double = probe["double_click"]
    assert double["calls"] == 1
    assert double["disabled_after"] is False


@requires_node
def test_the_probe_fails_when_the_week_branch_is_lost(tmp_path):
    """Reverse validation: 周视图退回「整月」时，区间检查必须真的变红。

    同一份源码只改一处分支条件（`viewMode.value !== 'week'` → `true`），周视图
    就又导出一整月——这正是 Issue 里说的「周视图导出的是屏幕上那 7 天，不是整月」。
    没有这条，上面的断言也可能在一个从不看 `viewMode` 的实现上通过。
    """
    source = APP_JS.read_text(encoding="utf-8")
    marker = "if (viewMode.value !== 'week') {"
    assert marker in source, "the week branch moved — update this test's marker"
    mutated = tmp_path / "app-setup.js"
    mutated.write_text(source.replace(marker, "if (true) {", 1), encoding="utf-8")

    broken = _run_probe(mutated)
    assert broken["week"]["url"].endswith("start=2026-11-01&end=2026-11-30&format=csv")
    assert broken["week"]["url"] != WEEK_EXPORT_URL
    # 网格仍然是那 7 天：两者一分叉，`export_range_matches_the_grid` 就失败了。
    assert broken["grid"]["week"]["count"] == 7
