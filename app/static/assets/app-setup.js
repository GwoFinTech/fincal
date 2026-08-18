  const { createApp, ref, computed, onMounted, watch } = Vue;

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

  // ── useWatchlist: search + CRUD ─────────────────────────────────
  function useWatchlist(apiFetch, showToast, loadEarnings) {
    const watchlist = ref([]);
    const searchQuery = ref('');
    const searchResults = ref([]);
    const searchLoading = ref(false);

    const usWatchlist = computed(() => watchlist.value.filter(item => item.market === 'US'));
    const hkWatchlist = computed(() => watchlist.value.filter(item => item.market === 'HK'));

    async function loadWatchlist() {
      const data = await apiFetch('/api/watchlist');
      if (data) watchlist.value = data;
    }

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
      showToast('已添加 ' + symbol);
      searchQuery.value = '';
      searchResults.value = [];
    }

    async function removeFromWatchlist(symbol, market) {
      await apiFetch(`/api/watchlist?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(market)}`, { method: 'DELETE' });
      await loadWatchlist();
      await loadEarnings();
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

    function watchlistInsight(item, earningsData) {
      const today = new Date().toISOString().slice(0, 10);
      const matches = earningsData.filter(e => e.symbol === item.symbol && e.market === item.market);
      return matches.find(e => e.report_date >= today) || matches[0] || {};
    }

    return {
      watchlist, searchQuery, searchResults, searchLoading,
      usWatchlist, hkWatchlist,
      loadWatchlist, doSearch, addToWatchlist, removeFromWatchlist,
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
    function epsSurplus(e) {
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
      if (e.revenue_estimate == null || e.revenue_actual == null) return null;
      const diff = e.revenue_actual - e.revenue_estimate;
      if (Math.abs(e.revenue_estimate) < 0.0001) return diff > 0 ? 1 : diff < 0 ? -1 : 0;
      return diff / Math.abs(e.revenue_estimate);
    }
    function revSurplusClass(e) {
      const s = revSurplus(e);
      return s > 0.001 ? 'beat' : s < -0.001 ? 'miss' : '';
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

    return {
      epsSurplus, epsSurplusClass, revSurplus, revSurplusClass,
      metricDelta, fmtNum, fmtPct, signedPct, fmtBigNum,
      estimateSourceLabel, hasLongbridgeConsensus, fqLabel,
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
          const start = new Date(d.getFullYear(), d.getMonth() - 1, 1).toISOString().slice(0, 10);
          const end = new Date(d.getFullYear(), d.getMonth() + 2, 0).toISOString().slice(0, 10);
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

      onMounted(async () => {
        try {
          const resp = await fetch('/api/config');
          if (resp.ok) appConfig.value = await resp.json();
        } catch {}
        const userData = await loadUser();
        if (userData) {
          ical.updateIcalUrl();
        }
        if (user.value) await wl.loadWatchlist();
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
        watchlistInsight: (item) => wl.watchlistInsight(item, earnings.value),
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
