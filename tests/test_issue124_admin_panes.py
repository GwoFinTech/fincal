"""Issue #124: the admin panel must show the audit trail and the diagnostics.

The backend served both all along — ``GET /api/admin/audit-log`` (``app/audit.py``)
and ``GET /api/admin/diagnostics`` (provider/cache metrics, sync freshness,
``future_reported``) — while the SPA had zero references to either: the 管理
dialog listed sync runs and nothing else.  Answering 「谁改过自选」 or 「缓存有没有
命中」 therefore meant SSH + SQL, which is exactly the kind of number that then
never gets looked at.

The probe runs the served ``useAdmin()`` under node with the vendored Vue build
and a fake ``apiFetch``: what is asserted is the composable's real behaviour
(demand-loading, error surface, redaction), not a copy of it.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import TestCase, skipUnless

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import frontend_contract as fc  # noqa: E402  (needs the path above)

VENDOR_VUE = fc.ROOT / "app" / "static" / "assets" / "vendor" / "vue.global.prod.js"

PROBE = r"""
const fs = require('fs');
const vm = require('vm');

const sandbox = { console, setTimeout, clearTimeout };
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.env.VUE_PATH, 'utf8'), sandbox);
const { ref } = sandbox.Vue;

/*USE_ADMIN*/

// A real audit row, plus the two credential shapes the renderer must not print.
const AUDIT = [
  { id: 3, action: 'add_managed_watchlist', actor_id: '42', actor_email: 'admin@example.com',
    target: 'managed_watchlist:9', created_at: '2026-11-12T03:04:05Z',
    details: { symbol: '0700.HK', market: 'HK' } },
  { id: 2, action: 'rotate_ical_token', actor_id: '42', actor_email: 'admin@example.com',
    target: 'user:7', created_at: '2026-11-11T03:04:05Z',
    details: { ical_token: 'abcdefghijklmnopqrstuvwxyz012345',
               url: 'https://fincal.example/ical/abcdefghijklmnopqrstuvwxyz012345',
               api_key: 'sk-live-123' } },
];

const DIAG = {
  cache: { hits: 12, misses: 3, hit_rate: 0.8, stale_returns: 1, refresh_ok: 5, refresh_fail: 2 },
  cache_by_name: { earnings: { hits: 10, misses: 2, hit_rate: 0.833 } },
  providers: { kurumi: { calls: 4, success: 3, success_rate: 0.75, avg_ms: 120.5,
                         errors: { timeout: 1, rate_limited: 0, connection: 0, invalid_response: 0 } } },
  // The real shape served by `app/freshness.check_freshness()`: the buckets are
  // `stale_stages` / `failed_stages` / `never_run_stages` / `stale_data`, and the
  // derived tables are listed apart from the stages.
  freshness: { status: 'degraded', error_code: 'sync_stage_run_failed',
               threshold_hours: 192, checked_at: '2026-11-12T03:50:49.717382+00:00',
               stale_stages: [], never_run_stages: [],
               failed_stages: ['longbridge', 'consensus'],
               stale_data: ['earnings_consensus', 'earnings_forecast_eps'] },
  future_reported: 2,
};

function makeAdmin(respond) {
  const paths = [];
  const apiFetch = async (path) => { paths.push(path); return respond(path); };
  const admin = useAdmin(apiFetch, () => {});
  return { admin, paths };
}

const tick = () => new Promise(resolve => setTimeout(resolve, 0));

(async () => {
  const ok = {
    '/api/admin/audit-log?limit=50': AUDIT,
    '/api/admin/diagnostics': DIAG,
  };
  const healthy = makeAdmin((path) => {
    if (path in ok) return ok[path];
    return null;
  });

  // Opening the dialog must not fetch the two heavy panes.
  await healthy.admin.openAdmin();
  const fetchedOnOpen = healthy.paths.slice();

  healthy.admin.selectAdminTab('audit');
  await tick();
  const auditCount = healthy.admin.auditEntries.value.length;
  const auditText = healthy.admin.auditDetailsText(healthy.admin.auditEntries.value[1]);
  const firstText = healthy.admin.auditDetailsText(healthy.admin.auditEntries.value[0]);

  healthy.admin.selectAdminTab('diagnostics');
  await tick();
  const sections = healthy.admin.diagnosticsSections(healthy.admin.diagnostics.value);

  // A failing endpoint must leave an error surface, not an empty table.
  const failing = makeAdmin(() => null);
  failing.admin.selectAdminTab('audit');
  await tick();
  const failureAuditError = failing.admin.auditError.value;
  const failureAuditRows = failing.admin.auditEntries.value.length;
  failing.admin.selectAdminTab('diagnostics');
  await tick();
  const failureDiagError = failing.admin.diagnosticsError.value;

  const out = {
    fetched_on_open: fetchedOnOpen,
    fetched_all: healthy.paths,
    audit_count: auditCount,
    audit_first: firstText,
    audit_secret_row: auditText,
    section_keys: sections.map(s => s.key),
    section_titles: sections.map(s => s.title),
    cache_rows: sections.find(s => s.key === 'cache').rows,
    provider_rows: sections.find(s => s.key === 'providers').rows,
    freshness_rows: sections.find(s => s.key === 'freshness').rows,
    future_rows: sections.find(s => s.key === 'future_reported').rows,
    empty_sections: healthy.admin.diagnosticsSections(null).length,
    failure_audit_error: failureAuditError,
    failure_audit_rows: failureAuditRows,
    failure_diag_error: failureDiagError,
    tabs: healthy.admin.adminTabs.map(t => t.value),
  };
  console.log(JSON.stringify(out));
})().catch(err => { console.error(err); process.exit(1); });
"""


def _run_admin_probe() -> dict:
    source = fc.function_source(fc.read_app_js(), "useAdmin")
    probe = PROBE.replace("/*USE_ADMIN*/", source)
    completed = subprocess.run(
        ["node", "-e", probe], capture_output=True, text=True, timeout=60,
        env={**os.environ, "VUE_PATH": str(VENDOR_VUE)},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class AdminPaneTests(TestCase):
    def test_the_dialog_opens_without_paying_for_the_two_extra_reads(self):
        out = _run_admin_probe()
        self.assertEqual(out["tabs"], ["managed", "audit", "diagnostics"])
        self.assertFalse([p for p in out["fetched_on_open"] if "/audit-log" in p or "/diagnostics" in p],
                         "the audit/diagnostics panes must load when they are opened")
        self.assertIn("/api/admin/audit-log?limit=50", out["fetched_all"])
        self.assertIn("/api/admin/diagnostics", out["fetched_all"])

    def test_the_audit_pane_reads_the_real_records(self):
        out = _run_admin_probe()
        self.assertEqual(out["audit_count"], 2)
        # `auditDetailsText` is only the details column; the row's action, actor
        # and target are rendered by the template from the record itself.
        self.assertEqual(out["audit_first"], '{"symbol":"0700.HK","market":"HK"}')
        html = fc.read_index()
        for binding in ("{{ entry.action }}", "entry.actor_email", "entry.target", "fmtDateTime(entry.created_at)"):
            assert binding in html, binding

    def test_secrets_never_reach_the_screen(self):
        out = _run_admin_probe()
        row = out["audit_secret_row"]
        self.assertIn("[REDACTED]", row)
        for secret in ("abcdefghijklmnopqrstuvwxyz012345", "sk-live-123"):
            self.assertNotIn(secret, row)

    def test_the_four_diagnostic_blocks_carry_their_values(self):
        out = _run_admin_probe()
        self.assertEqual(out["section_keys"], ["cache", "providers", "freshness", "future_reported"])
        cache = {row["label"]: row["value"] for row in out["cache_rows"]}
        self.assertEqual(cache["命中 / 未命中"], "12 / 3")
        self.assertEqual(cache["命中率"], "80.0%")
        self.assertEqual(cache["刷新成功 / 失败"], "5 / 2")
        self.assertIn("earnings 命中率", cache)

        provider = {row["label"]: row["value"] for row in out["provider_rows"]}
        self.assertIn("4 次", provider["kurumi"])
        self.assertIn("timeout 1", provider["kurumi"])

        fresh = {row["label"]: row["value"] for row in out["freshness_rows"]}
        self.assertIn("degraded", fresh["总体"])
        self.assertEqual(fresh["判定阈值"], "192 小时")
        # The names, not zeros: reading `stale`/`failed`/`never` (which the payload
        # never had) printed "0 / 0" beside a degraded status.
        self.assertEqual(fresh["失败阶段"], "longbridge、consensus")
        self.assertEqual(fresh["落后阶段"], "无")
        self.assertEqual(fresh["落后数据表"], "earnings_consensus、earnings_forecast_eps")

        future = {row["label"]: row["value"] for row in out["future_rows"]}
        self.assertEqual(future["行数"], "2")

    def test_a_failed_read_shows_an_error_instead_of_an_empty_table(self):
        out = _run_admin_probe()
        self.assertTrue(out["failure_audit_error"], "a failed audit read must be visible")
        self.assertTrue(out["failure_diag_error"], "a failed diagnostics read must be visible")

    def test_no_payload_yields_no_sections_rather_than_fake_zeros(self):
        out = _run_admin_probe()
        self.assertEqual(out["empty_sections"], 0)


class AdminPaneMarkupTests(TestCase):
    def test_the_dialog_has_three_panes_and_the_panes_are_reachable(self):
        html = fc.read_index()
        assert 'role="tablist" aria-label="管理面板分区"' in html
        for pane in ("adminTab === 'managed'", "adminTab === 'audit'", "<template v-else>"):
            assert pane in html, pane
        # Each pane owns its loading, error and empty state.
        for text in ("加载审计日志", "审计日志加载失败", "暂无审计记录", "加载诊断", "诊断加载失败", "暂无数据。"):
            assert text in html, text

    def test_the_entry_point_stays_admin_only(self):
        html = fc.read_index()
        # The 管理 button is behind `user.is_admin`, and every endpoint behind
        # `require_admin()` — the pane must not add a second, weaker gate.
        assert 'v-if="user && user.is_admin" @click="openAdmin"' in html, (
            "the 管理 entry point must still be rendered from the admin role only"
        )
