  const { createApp, ref, computed, onMounted, watch } = Vue;

  // ── localYmd: format a Date as a local YYYY-MM-DD string ──────────
  // Previously the date window (`start`/`end`), `today`, and the calendar
  // grid used inconsistent zones: the grid built local dates, but the
  // request window + "today" used toISOString().slice(0,10), which is UTC.
  // In UTC+8 (the product's primary market, X-WR-TIMEZONE:Asia/Shanghai)
  // that shifted every window date/today one day early. This helper formats
  // from local date parts so every caller agrees (Issue #46).
  function localYmd(d) {
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  // The single local "today" (same zone/format as every request window, #46).
  // Both the calendar window and the watchlist "next report" cut-off read it,
  // so the two pages can never disagree about what counts as future.
  function localTodayYmd() { return localYmd(new Date()); }

  function localDaysFromTodayYmd(days) {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return localYmd(d);
  }

  // ── Watchlist "next earnings" lookahead (Issue #56) ────────────────
  // The watchlist table asks for its own forward-only window instead of
  // reusing the calendar's "displayed month ±1" dataset, and the window must
  // reach as far as the predictor can place a date, otherwise a symbol whose
  // only future row sits further out would render as "—". Mirrors
  // scripts/predict_earnings.py (MAX_PREDICT_AHEAD = 4 quarters,
  // MAX_FUTURE_DAYS = 420); tests/test_frontend_structure.py pins the match.
  const WATCHLIST_NEXT_WINDOW_DAYS = 420;

  // ══════════════════════════════════════════════════════════════════
  // Composables (Issue #13 — domain logic extracted from setup)
  // ══════════════════════════════════════════════════════════════════

  // ── useApi: shared fetch wrapper + toast + error state ──────────
  function useApi() {
    const user = ref(null);
    const appConfig = ref({ auth_login_url: '' });
    const loading = ref(false);
    const error = ref(null);
    const toast = ref('');

    async function apiFetch(path, opts = {}) {
      try {
        const res = await fetch(path, { ...opts, headers: { 'Content-Type': 'application/json', ...opts.headers } });
        if (res.status === 401) { user.value = null; return null; }
        if (res.status === 404) { return null; }
        if (!res.ok) {
          const text = await res.text().catch(() => '');
          throw new Error(text || `HTTP ${res.status}`);
        }
        return res.json();
      } catch (e) {
        error.value = e.message;
        showToast('请求失败: ' + e.message);
        return null;
      }
    }

    function showToast(msg) {
      toast.value = msg;
      setTimeout(() => toast.value = '', 3000);
    }

    async function loadUser() {
      const data = await apiFetch('/api/me');
      if (data) { user.value = data; }
      return data;
    }

    function login() {
      const url = appConfig.value.auth_login_url;
      if (url) {
        window.location.href = url + (url.includes('?') ? '&' : '?') + 'redirect=' + encodeURIComponent(window.location.href);
      }
    }

    return { user, appConfig, loading, error, toast, apiFetch, showToast, loadUser, login };
  }

  // ── useCalendar: calendar navigation + cell computation ─────────
  function useCalendar(earnings, watchlist, watchlistOnly) {
    const viewMode = ref('month');
    const currentDate = ref(new Date());
    const selectedEarning = ref(null);
    const decision = ref(null);
    const selectedDay = ref(null);

    const weekdays = ['日', '一', '二', '三', '四', '五', '六'];

    const monthLabel = computed(() => {
      const d = currentDate.value;
      return d.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long' });
    });

    function sameDay(d1, d2) {
      return d1.getFullYear() === d2.getFullYear() && d1.getMonth() === d2.getMonth() && d1.getDate() === d2.getDate();
    }

    const calendarCells = computed(() => {
      const d = currentDate.value;
      const year = d.getFullYear();
      const month = d.getMonth();
      const today = new Date();

      let startDate, endDate;
      if (viewMode.value === 'month') {
        const firstDay = new Date(year, month, 1);
        const lastDay = new Date(year, month + 1, 0);
        startDate = new Date(year, month, 1 - firstDay.getDay());
        endDate = new Date(year, month + 1, 0 + (6 - lastDay.getDay()));
      } else {
        const dayOfWeek = d.getDay();
        startDate = new Date(year, month, d.getDate() - dayOfWeek);
        endDate = new Date(year, month, d.getDate() + (6 - dayOfWeek));
      }

      const cells = [];
      let cursor = new Date(startDate);
      while (cursor <= endDate) {
        const cellDate = new Date(cursor);
        const cellEarnings = earnings.value.filter(e => {
          const parts = e.report_date.split('-');
          const dd = new Date(+parts[0], +parts[1] - 1, +parts[2]);
          return sameDay(dd, cellDate);
        });
        const inMonth = cellDate.getMonth() === month;
        cells.push({
          date: cellDate,
          day: cellDate.getDate(),
          inMonth: viewMode.value === 'week' ? true : inMonth,
          isToday: sameDay(cellDate, today),
          earnings: cellEarnings,
        });
        cursor.setDate(cursor.getDate() + 1);
      }
      return cells;
    });

    function prevMonth() {
      const d = currentDate.value;
      currentDate.value = new Date(d.getFullYear(), d.getMonth() - (viewMode.value === 'week' ? 0 : 1), viewMode.value === 'week' ? d.getDate() - 7 : 1);
    }
    function nextMonth() {
      const d = currentDate.value;
      currentDate.value = new Date(d.getFullYear(), d.getMonth() + (viewMode.value === 'week' ? 0 : 1), viewMode.value === 'week' ? d.getDate() + 7 : 1);
    }
    function goToday() { currentDate.value = new Date(); }

    async function selectEarning(e, day, apiFetch) {
      if (day) selectedDay.value = day;
      selectedEarning.value = e;
      decision.value = null;
      if (e.id) {
        const data = await apiFetch(`/api/earnings/${e.id}/decision`);
        if (selectedEarning.value === e && data && data.status === 'available') decision.value = data;
      }
    }
    function selectCell(cell) {
      if (cell.earnings.length === 0) return;
      selectedEarning.value = null;
      decision.value = null;
      selectedDay.value = cell;
    }
    function clearSelection() {
      selectedEarning.value = null;
      decision.value = null;
      selectedDay.value = calendarCells.value.find(cell => cell.isToday) || null;
    }

    return {
      viewMode, currentDate, selectedEarning, decision, selectedDay,
      weekdays, monthLabel, calendarCells,
      prevMonth, nextMonth, goToday, selectEarning, selectCell, clearSelection,
    };
  }

  // ── useWatchlist: search + CRUD + own "next earnings" source ────
  function useWatchlist(apiFetch, showToast, loadEarnings) {
    const watchlist = ref([]);
    const searchQuery = ref('');
    const searchResults = ref([]);
    const searchLoading = ref(false);

    // Issue #56: the watchlist table ("下次财报 / EPS 预期 / 营收预期") owns its
    // data. It used to reuse the calendar's `earnings` ref, whose window is the
    // *displayed* month ±1, and then fell back to the earliest row of that
    // window — so 9/18 rows rendered an already-reported period in the default
    // month, 18/18 after paging the calendar back, and the values depended on
    // where the user last left the calendar. This ref is forward-only and
    // independent of cal.currentDate.
    const nextEarnings = ref([]);

    const usWatchlist = computed(() => watchlist.value.filter(item => item.market === 'US'));
    const hkWatchlist = computed(() => watchlist.value.filter(item => item.market === 'HK'));

    async function loadWatchlist() {
      const data = await apiFetch('/api/watchlist');
      if (data) watchlist.value = data;
    }

    async function loadNextEarnings() {
      // Forward-only window: a period that has already been reported is never
      // a candidate for "下次财报", so there is no reason to request one.
      const start = localTodayYmd();
      const end = localDaysFromTodayYmd(WATCHLIST_NEXT_WINDOW_DAYS);
      const params = new URLSearchParams({ start, end, watchlistOnly: true });
      const data = await apiFetch(`/api/earnings?${params}`);
      nextEarnings.value = data || [];
    }

    // One row per symbol: the earliest report still ahead of local today.
    // Computed once per data change (the table looks each row up repeatedly)
    // and a Map so the lookup cannot accidentally pick a later period.
    const nextBySymbol = computed(() => {
      const today = localTodayYmd();
      const map = new Map();
      for (const e of nextEarnings.value) {
        if (!e.report_date || e.report_date < today) continue;
        const key = e.market + ':' + e.symbol;
        if (!map.has(key)) map.set(key, e); // /api/earnings is report_date-ordered
      }
      return map;
    });

    let searchTimer;
    async function doSearch() {
      clearTimeout(searchTimer);
      if (searchQuery.value.length < 1) { searchResults.value = []; searchLoading.value = false; return; }
      searchLoading.value = true;
      searchTimer = setTimeout(async () => {
        const data = await apiFetch(`/api/search?q=${encodeURIComponent(searchQuery.value)}`);
        searchResults.value = data || [];
        searchLoading.value = false;
      }, 300);
    }

    async function addToWatchlist(symbol, market) {
      await apiFetch(`/api/watchlist?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(market)}`, { method: 'POST' });
      await loadWatchlist();
      await loadEarnings();
      await loadNextEarnings();
      showToast('已添加 ' + symbol);
      searchQuery.value = '';
      searchResults.value = [];
    }

    async function removeFromWatchlist(symbol, market) {
      await apiFetch(`/api/watchlist?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(market)}`, { method: 'DELETE' });
      await loadWatchlist();
      await loadEarnings();
      await loadNextEarnings();
      showToast('已移除 ' + symbol);
    }

    function isMine(earning) {
      return watchlist.value.some(w => w.symbol === earning.symbol && w.market === earning.market);
    }

    async function toggleWatchlist(symbol, market) {
      const existing = watchlist.value.some(w => w.symbol === symbol && w.market === market);
      if (existing) await removeFromWatchlist(symbol, market);
      else await addToWatchlist(symbol, market);
    }

    // No fallback to the earliest row of the window: when nothing is ahead of
    // today the row must render "—" rather than silently present an already
    // reported period as the next one.
    function watchlistInsight(item) {
      return nextBySymbol.value.get(item.market + ':' + item.symbol) || {};
    }

    return {
      watchlist, searchQuery, searchResults, searchLoading,
      usWatchlist, hkWatchlist, nextEarnings, nextBySymbol,
      loadWatchlist, loadNextEarnings, doSearch, addToWatchlist, removeFromWatchlist,
      isMine, toggleWatchlist, watchlistInsight,
    };
  }

  // ── useIcal: modal + URL building ───────────────────────────────
  function useIcal(user) {
    const showIcalModal = ref(false);
    const icalUrl = ref('');
    const icalOptions = ref({ lang: 'zh', scope: 'watchlist', predicted: true, markets: 'all' });
    const copied = ref(false);

    const popularSuggestions = [
      {symbol:'AAPL',market:'US'},{symbol:'NVDA',market:'US'},{symbol:'TSLA',market:'US'},
      {symbol:'0700.HK',market:'HK'},{symbol:'9988.HK',market:'HK'},{symbol:'1810.HK',market:'HK'},
    ];

    watch(icalOptions, updateIcalUrl, { deep: true });

    function updateIcalUrl() {
      if (!user.value?.ical_url) return;
      const url = new URL(user.value.ical_url, window.location.origin);
      url.searchParams.set('lang', icalOptions.value.lang);
      url.searchParams.set('scope', icalOptions.value.scope);
      url.searchParams.set('predicted', icalOptions.value.predicted ? '1' : '0');
      url.searchParams.set('markets', icalOptions.value.markets);
      icalUrl.value = url.toString();
    }

    function copyIcal() {
      updateIcalUrl();
      navigator.clipboard.writeText(icalUrl.value);
      copied.value = true;
      setTimeout(() => copied.value = false, 2000);
    }

    return { showIcalModal, icalUrl, icalOptions, copied, popularSuggestions, updateIcalUrl, copyIcal };
  }

  // ── useAdmin: admin panel + managed watchlist ───────────────────
  function useAdmin(apiFetch, showToast) {
    const showAdmin = ref(false);
    const adminLoading = ref(false);
    const adminSource = ref({});
    const managedWatchlist = ref([]);
    const syncRuns = ref([]);
    const managedForm = ref({ id: null, symbol: '', market: 'US' });

    async function openAdmin() {
      showAdmin.value = true;
      adminLoading.value = true;
      const [overview, runs] = await Promise.all([
        apiFetch('/api/admin/overview'), apiFetch('/api/admin/sync-runs?limit=50')
      ]);
      if (overview) {
        adminSource.value = overview.source || {};
        managedWatchlist.value = overview.managed_watchlist || [];
      }
      if (runs) syncRuns.value = runs;
      adminLoading.value = false;
    }

    function resetManaged() { managedForm.value = { id: null, symbol: '', market: 'US' }; }
    function editManaged(item) { managedForm.value = { id: item.id, symbol: item.symbol, market: item.market }; }
    async function saveManaged() {
      const form = managedForm.value;
      const path = form.id ? `/api/admin/watchlist/${form.id}` : '/api/admin/watchlist';
      const data = await apiFetch(path, { method: form.id ? 'PUT' : 'POST', body: JSON.stringify({ symbol: form.symbol, market: form.market }) });
      if (data) { showToast('已保存 ' + data.symbol); resetManaged(); await openAdmin(); }
    }
    async function deleteManaged(id) {
      if (!window.confirm('删除该 FinCal 自建自选？')) return;
      const data = await apiFetch(`/api/admin/watchlist/${id}`, { method: 'DELETE' });
      if (data) { showToast('已删除'); await openAdmin(); }
    }
    function fmtDateTime(value) {
      return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '进行中';
    }

    return {
      showAdmin, adminLoading, adminSource, managedWatchlist, syncRuns, managedForm,
      openAdmin, resetManaged, editManaged, saveManaged, deleteManaged, fmtDateTime,
    };
  }

  // ── useFormatters: number/percentage/date helpers ───────────────
  function useFormatters() {
    // Issue #61: the API marks a row whose estimate and actual are not the same
    // quantity (different/unknown currency, unverified basis) with a
    // language-independent reason code. Such a row must never render a surplus
    // percentage, a beat/miss colour or an up/down arrow — the two numbers are
    // not comparable, so any difference between them is an artefact.
    function comparisonUnavailable(e) {
      return !!(e && e.comparison_unavailable_reason);
    }
    // The reason codes are language-independent; the wording lives here, in one
    // place, so the row-level (预期与实际) and cross-period (本期与上期) guards
    // describe the same cause the same way.
    const ATTRIBUTION_LABELS = {
      currency_mismatch: '币种不同',
      currency_unknown: '币种未标明',
      basis_mismatch: '口径不同',
      basis_unverified: '口径未经同一来源确认',
    };
    function attributionNote(reason, subject) {
      if (!reason) return '';
      const label = ATTRIBUTION_LABELS[reason] || '口径不可比';
      return subject ? subject + label + '，无法比较' : label + '，无法比较';
    }
    function comparisonNote(e) {
      return attributionNote(e && e.comparison_unavailable_reason, '预期与实际');
    }
    // Issue #63: 同比/环比 subtract two *different* periods' actuals, so the
    // row-level guard above never covered them — production rendered "+5647.7%"
    // for TSM while the same panel showed "—" for that row's 较预期. The API now
    // returns a ratio only when both periods' actuals carry the same declared
    // currency/basis, and reports why on `<metric>_reason` otherwise.
    const GROWTH_LABELS = {
      eps_yoy: 'EPS 同比', eps_qoq: 'EPS 环比',
      revenue_yoy: '营收同比', revenue_qoq: '营收环比',
    };
    function growthValue(decision, key) {
      const growth = decision && decision.actual_growth;
      if (!growth || growth[key + '_reason']) return null;
      return growth[key] == null ? null : growth[key];
    }
    function growthReason(decision, key) {
      const growth = decision && decision.actual_growth;
      return (growth && growth[key + '_reason']) || '';
    }
    function growthNote(decision, key) {
      return attributionNote(growthReason(decision, key), '本期与上期');
    }
    // Second line of the panel's 实际值 column: the ratio, or "不可比" with the
    // reason on the cell's title — never a percentage for an incomparable pair.
    function growthCell(decision, key) {
      return growthReason(decision, key) ? '不可比' : fmtPct(growthValue(decision, key));
    }
    function growthSuppressedNote(decision) {
      const parts = Object.keys(GROWTH_LABELS)
        .filter(key => growthReason(decision, key))
        .map(key => GROWTH_LABELS[key] + '（' + attributionNote(growthReason(decision, key)) + '）');
      if (!parts.length) return '';
      return '「—」表示两个期间的实际值不可比，不是数据缺失：' + parts.join('；') + '。';
    }
    // Issue #64: 连续 was the third and last derived metric rendered without the
    // comparability rule — the panel showed "不及预期 3季" for ASML FY2026 Q2 while
    // the same row's 较预期 already said "—（预期与实际币种不同）". The API now only
    // counts a quarter whose own pair passed that rule, so a count is rendered
    // only when no reason came back; otherwise the cell shows "—" and the note
    // below says why, reusing the same reason labels as the other two guards.
    const STREAK_BREAK_LABELS = {
      missing_values: '该季预期或实际值缺失（或两者相等）',
      direction_changed: '该季方向相反',
      not_adjacent: '该季与上一季不相邻',
    };
    function beatMissStreak(decision) {
      const streak = decision && decision.beat_miss_streak;
      if (!streak || streak.reason || !streak.count) return null;
      if (streak.kind !== 'beat' && streak.kind !== 'miss') return null;
      return streak;
    }
    function beatMissStreakText(decision) {
      const streak = beatMissStreak(decision);
      if (!streak) return '—';
      return (streak.kind === 'beat' ? '超预期' : '不及预期') + ' ' + streak.count + '季';
    }
    function beatMissStreakNote(decision) {
      const streak = decision && decision.beat_miss_streak;
      if (!streak) return '';
      const boundary = streak.break_period
        ? fqLabel(streak.break_period.fiscal_year, streak.break_period.fiscal_quarter)
        : '';
      if (streak.reason) {
        return '「—」表示无法判断连续季数，不是数据缺失：本期'
          + (boundary ? '（' + boundary + '）' : '') + '的预期与实际'
          + attributionNote(streak.reason) + '。';
      }
      if (!streak.count || !streak.break_reason) return '';
      const cause = ATTRIBUTION_LABELS[streak.break_reason]
        ? '该季的预期与实际' + attributionNote(streak.break_reason)
        : (STREAK_BREAK_LABELS[streak.break_reason] || '无法继续累计');
      return '连续季数只累计相邻且可比的财季：' + (boundary ? boundary + ' ' : '') + cause + '，不计入并在此中断。';
    }
    function epsSurplus(e) {
      if (comparisonUnavailable(e)) return null;
      if (e.eps_estimate == null || e.eps_actual == null) return null;
      const diff = e.eps_actual - e.eps_estimate;
      if (Math.abs(e.eps_estimate) < 0.0001) return diff > 0 ? 1 : diff < 0 ? -1 : 0;
      return diff / Math.abs(e.eps_estimate);
    }
    function epsSurplusClass(e) {
      const s = epsSurplus(e);
      return s > 0.001 ? 'beat' : s < -0.001 ? 'miss' : '';
    }
    function revSurplus(e) {
      if (comparisonUnavailable(e)) return null;
      if (e.revenue_estimate == null || e.revenue_actual == null) return null;
      const diff = e.revenue_actual - e.revenue_estimate;
      if (Math.abs(e.revenue_estimate) < 0.0001) return diff > 0 ? 1 : diff < 0 ? -1 : 0;
      return diff / Math.abs(e.revenue_estimate);
    }
    function revSurplusClass(e) {
      const s = revSurplus(e);
      return s > 0.001 ? 'beat' : s < -0.001 ? 'miss' : '';
    }
    // A row keeps its "较预期" cell only when both values exist *and* the API did
    // not flag the pair as non-comparable; otherwise the cell shows "—".
    function hasComparison(e, metric) {
      if (!e || comparisonUnavailable(e)) return false;
      return metric === 'eps'
        ? e.eps_estimate != null && e.eps_actual != null
        : e.revenue_estimate != null && e.revenue_actual != null;
    }
    function metricDelta(actual, estimate) {
      return actual == null || estimate == null ? null : Number(actual) - Number(estimate);
    }
    function fmtNum(n) { return n == null ? '—' : Number(n).toFixed(2); }
    function fmtPct(ratio) { return ratio == null ? '—' : (ratio * 100).toFixed(1) + '%'; }
    function signedPct(ratio) {
      if (ratio == null) return '—';
      const text = fmtPct(ratio);
      return ratio > 0 ? '+' + text : text;
    }
    function fmtBigNum(n) {
      if (n == null) return '—';
      const v = Number(n);
      if (Math.abs(v) >= 1e12) return (v / 1e12).toFixed(2) + 'T';
      if (Math.abs(v) >= 1e9) return (v / 1e9).toFixed(2) + 'B';
      if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + 'M';
      return v.toLocaleString();
    }
    function estimateSourceLabel(e) {
      if (e.estimate_source === 'longbridge') return 'Longbridge';
      return '历史来源待确认';
    }
    function hasLongbridgeConsensus(e) {
      return ['consensus_eps_gaap', 'consensus_eps_adjusted', 'consensus_revenue', 'consensus_ebit', 'consensus_net_income']
        .some(key => e[key] != null);
    }
    function fqLabel(fy, fq) {
      if (!fy || !fq) return '';
      return String(fy).slice(-2) + 'Q' + fq;
    }
    // Sub-label of a watchlist "下次财报" cell: the fiscal period when the row
    // knows it, empty when there is no upcoming report at all (Issue #56).
    // Previously an empty insight still rendered "待确认" next to "—".
    function periodLabel(e) {
      if (!e || !e.report_date) return '';
      return fqLabel(e.fiscal_year, e.fiscal_quarter) || '待确认';
    }

    return {
      epsSurplus, epsSurplusClass, revSurplus, revSurplusClass,
      hasComparison, comparisonNote, comparisonUnavailable, attributionNote,
      growthValue, growthReason, growthNote, growthCell, growthSuppressedNote,
      beatMissStreak, beatMissStreakText, beatMissStreakNote,
      metricDelta, fmtNum, fmtPct, signedPct, fmtBigNum,
      estimateSourceLabel, hasLongbridgeConsensus, fqLabel, periodLabel,
    };
  }

  // ══════════════════════════════════════════════════════════════════
  // App wiring (Issue #13 — setup is now a thin composition layer)
  // ══════════════════════════════════════════════════════════════════

  createApp({
    setup() {
      // 1. Core API layer
      const api = useApi();
      const { user, appConfig, loading, error, toast, apiFetch, showToast, loadUser, login } = api;

      // 2. Earnings data (shared between calendar and watchlist)
      const earnings = ref([]);
      const appTab = ref('calendar');
      const watchlistOnly = ref(false);

      // 3. Calendar
      const cal = useCalendar(earnings, null, watchlistOnly);

      // 4. Watchlist (needs loadEarnings callback)
      const wl = useWatchlist(apiFetch, showToast, () => loadEarnings());
      cal.watchlist = wl.watchlist; // wire watchlist into calendar

      // 5. iCal
      const ical = useIcal(user);

      // 6. Admin
      const admin = useAdmin(apiFetch, showToast);

      // 7. Formatters
      const fmt = useFormatters();

      // ── Earnings loading ─────────────────────────────────────
      async function loadEarnings() {
        loading.value = true;
        error.value = null;
        try {
          const d = cal.currentDate.value;
          const start = localYmd(new Date(d.getFullYear(), d.getMonth() - 1, 1));
          const end = localYmd(new Date(d.getFullYear(), d.getMonth() + 2, 0));
          const params = new URLSearchParams({ start, end, watchlistOnly: watchlistOnly.value });
          const data = await apiFetch(`/api/earnings?${params}`);
          if (data) {
            earnings.value = watchlistOnly.value
              ? data.filter(e => wl.watchlist.value.some(w => w.symbol === e.symbol && w.market === e.market))
              : data;
            if (appTab.value === 'calendar') {
              cal.selectedDay.value = cal.calendarCells.value.find(cell => cell.isToday) || null;
            }
          }
        } finally {
          loading.value = false;
        }
      }

      // ── Lifecycle ────────────────────────────────────────────
      watch(cal.currentDate, loadEarnings);

      // The watchlist data must not follow the calendar month; it is refreshed
      // when the page is (re)entered so a long calendar session cannot show a
      // stale "下次财报" (Issue #56).
      watch(appTab, (tab) => {
        if (tab === 'watchlist' && user.value) wl.loadNextEarnings();
      });

      onMounted(async () => {
        try {
          const resp = await fetch('/api/config');
          if (resp.ok) appConfig.value = await resp.json();
        } catch {}
        const userData = await loadUser();
        if (userData) {
          ical.updateIcalUrl();
        }
        if (user.value) {
          await wl.loadWatchlist();
          await wl.loadNextEarnings();
        }
        await loadEarnings();
      });

      // ── Public API ───────────────────────────────────────────
      return {
        // State
        user, earnings, appTab, watchlistOnly, loading, error, toast, appConfig,
        // Calendar
        viewMode: cal.viewMode, currentDate: cal.currentDate,
        selectedEarning: cal.selectedEarning, decision: cal.decision, selectedDay: cal.selectedDay,
        weekdays: cal.weekdays, monthLabel: cal.monthLabel, calendarCells: cal.calendarCells,
        prevMonth: cal.prevMonth, nextMonth: cal.nextMonth, goToday: cal.goToday,
        selectCell: cal.selectCell, clearSelection: cal.clearSelection,
        selectEarning: (e, day) => cal.selectEarning(e, day, apiFetch),
        // Watchlist
        watchlist: wl.watchlist, searchQuery: wl.searchQuery, searchResults: wl.searchResults,
        searchLoading: wl.searchLoading, usWatchlist: wl.usWatchlist, hkWatchlist: wl.hkWatchlist,
        doSearch: wl.doSearch, addToWatchlist: wl.addToWatchlist, removeFromWatchlist: wl.removeFromWatchlist,
        toggleWatchlist: wl.toggleWatchlist, isMine: wl.isMine,
        watchlistInsight: wl.watchlistInsight,
        // iCal
        showIcalModal: ical.showIcalModal, icalUrl: ical.icalUrl, icalOptions: ical.icalOptions,
        copied: ical.copied, popularSuggestions: ical.popularSuggestions, copyIcal: ical.copyIcal,
        // Admin
        showAdmin: admin.showAdmin, adminLoading: admin.adminLoading, adminSource: admin.adminSource,
        managedWatchlist: admin.managedWatchlist, syncRuns: admin.syncRuns, managedForm: admin.managedForm,
        openAdmin: admin.openAdmin, saveManaged: admin.saveManaged, editManaged: admin.editManaged,
        deleteManaged: admin.deleteManaged, resetManaged: admin.resetManaged, fmtDateTime: admin.fmtDateTime,
        // Formatters
        ...fmt,
        // Shared
        showToast, login,
      };
    }
  }).mount('#app');
