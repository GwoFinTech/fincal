  const { createApp, ref, computed, onMounted, watch, nextTick } = Vue;

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
    // Two separate surfaces (Issue #82): a *read* failure is transient — the next
    // refresh may clear it — while a *write* failure must stay visible until the
    // user acts on it. They used to share one ref, so the `loadEarnings()` that
    // every write path calls right after the request wiped the explanation the
    // failure had just written.
    const error = ref(null);
    const writeError = ref(null);
    const toast = ref('');

    // Language-neutral error codes from `app/errors.py` → the one wording this
    // SPA shows for them. A rejected write must read as a sentence instead of
    // raw JSON (Issue #66: saving an A-share code as a US symbol).
    const API_ERROR_LABELS = {
      symbol_market_mismatch: '代码后缀与所选市场不一致（如 600519.SH 不是美股）',
    };

    function apiErrorText(body, status) {
      try {
        const code = JSON.parse(body)?.error?.code;
        if (code && API_ERROR_LABELS[code]) return API_ERROR_LABELS[code];
      } catch (e) { /* not our JSON error envelope — show the body as-is */ }
      return body || `HTTP ${status}`;
    }

    // ``expectOk: true`` marks a *write* (Issue #82). A 401/404 answers with a
    // visible, attributable ``writeError`` instead of a silent ``null``, the
    // message never lands in the read surface, and the caller is handed the same
    // ``null`` it already knows how to check — so the return shape stays
    // compatible with the dozen read call sites.
    async function apiFetch(path, opts = {}) {
      const { expectOk, ...init } = opts;
      if (expectOk) writeError.value = null; // a new attempt supersedes the last failure
      try {
        const res = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...init.headers } });
        if (res.status === 401) {
          user.value = null;
          if (expectOk) writeError.value = '登录状态已失效（HTTP 401），本次改动未生效，请重新登录后重试';
          return null;
        }
        if (res.status === 404) {
          if (expectOk) writeError.value = '请求的接口不存在（HTTP 404），本次改动未生效';
          return null;
        }
        if (!res.ok) {
          const text = await res.text().catch(() => '');
          throw new Error(apiErrorText(text, res.status));
        }
        return res.json();
      } catch (e) {
        if (expectOk) writeError.value = '本次改动未生效：' + e.message;
        else error.value = e.message;
        showToast('请求失败: ' + e.message);
        return null;
      }
    }

    function dismissErrors() {
      error.value = null;
      writeError.value = null;
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

    return { user, appConfig, loading, error, writeError, toast, apiFetch, showToast, dismissErrors, loadUser, login };
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
      if (day) { selectedDay.value = day; focusedDate.value = day.date; }
      selectedEarning.value = e;
      decision.value = null;
      revealSelectionPanel();
      if (e.id) {
        const data = await apiFetch(`/api/earnings/${e.id}/decision`);
        if (selectedEarning.value === e && data && data.status === 'available') decision.value = data;
      }
    }
    // Under 1024px the detail panel is rendered *after* the whole month grid, so a
    // tap on a day or a symbol looked like it did nothing — the list was more than
    // a screen below. Desktop keeps both columns in view, so nothing moves there.
    const NARROW_VIEWPORT = '(max-width: 1023px)';
    function revealSelectionPanel() {
      if (typeof window === 'undefined' || !window.matchMedia) return;
      if (!window.matchMedia(NARROW_VIEWPORT).matches) return;
      const panel = document.querySelector('.selection-panel');
      if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    function selectCell(cell) {
      // Issue #122: an empty day used to return here, so `selectedDay` kept the
      // previous day's value — the panel header and list stayed on A after
      // tapping B, and nothing on screen distinguished 「B 日没有财报」 from
      // 「点击没生效」. Selecting a day is navigation, not a filter: every day
      // is selectable and the panel renders its own empty state.
      if (!cell || !cell.date) return;
      selectedEarning.value = null;
      decision.value = null;
      selectedDay.value = cell;
      focusedDate.value = cell.date;
      revealSelectionPanel();
    }
    // `calendarCells` is recomputed whenever the month changes, so the stored
    // `selectedDay` object is no longer the live one; identity would stop
    // matching after a single navigation. Compare the date instead.
    function isSelectedDay(cell) {
      return Boolean(cell && cell.date && selectedDay.value && selectedDay.value.date)
        && sameDay(selectedDay.value.date, cell.date);
    }
    // ── Roving tabindex (Issue #126) ────────────────────────────────
    // #95 made a cell keyboard-operable by giving *every* cell `tabindex="0"`,
    // so a month grid became ~35 tab stops and the next control was 35 presses
    // away. A grid keeps exactly one tab stop: the cell that currently owns the
    // focus, moved with the arrow keys. The badges inside a cell are taken out
    // of the tab order for the same reason — the cell opens the day's list, and
    // that list's rows are real buttons.
    const focusedDate = ref(null);
    const calendarWeeks = computed(() => {
      const cells = calendarCells.value;
      const weeks = [];
      for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
      return weeks;
    });
    // Which cell owns the tab stop: the focused one while it is in the rendered
    // window, otherwise the selection, then today, then the first in-month day.
    // A history step or a month change can move the window under the focus, and
    // falling back through this chain keeps exactly one cell at 0.
    const focusIndex = computed(() => {
      const cells = calendarCells.value;
      if (!cells.length) return -1;
      const key = focusedDate.value ? localYmd(focusedDate.value) : '';
      const focused = key ? cells.findIndex(c => localYmd(c.date) === key) : -1;
      if (focused >= 0) return focused;
      const selected = cells.findIndex(c => isSelectedDay(c));
      if (selected >= 0) return selected;
      const today = cells.findIndex(c => c.isToday);
      if (today >= 0) return today;
      const inMonth = cells.findIndex(c => c.inMonth);
      return inMonth >= 0 ? inMonth : 0;
    });
    function cellTabIndex(cell) {
      const anchor = calendarCells.value[focusIndex.value];
      return anchor && localYmd(anchor.date) === localYmd(cell.date) ? 0 : -1;
    }
    function focusCellDom(date) {
      if (!date || typeof document === 'undefined') return;
      nextTick(() => {
        const el = document.querySelector('.calendar-cell[data-date="' + localYmd(date) + '"]');
        if (el && el.focus) el.focus();
      });
    }
    function moveFocus(targetIndex) {
      const cells = calendarCells.value;
      const index = focusIndex.value;
      if (index < 0) return false;
      const days = targetIndex - index;
      if (!days) return true;
      if (targetIndex >= 0 && targetIndex < cells.length) {
        const cell = cells[targetIndex];
        focusedDate.value = cell.date;
        focusCellDom(cell.date);
        return true;
      }
      // Past the rendered window: step the month, keep the weekday slot.
      const anchor = cells[index].date;
      const next = new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate() + days);
      focusedDate.value = next;
      currentDate.value = viewMode.value === 'week'
        ? next
        : new Date(next.getFullYear(), next.getMonth(), 1);
      focusCellDom(next);
      return true;
    }
    // PageUp/PageDown keep the day of the month, clamped to the target month's
    // length (Jan 31 → Feb 28), instead of overflowing into March.
    function moveFocusByMonth(delta) {
      const cells = calendarCells.value;
      const index = focusIndex.value;
      const anchor = index >= 0 ? cells[index].date : new Date();
      const year = anchor.getFullYear();
      const month = anchor.getMonth() + delta;
      const lastDay = new Date(year, month + 1, 0).getDate();
      const next = new Date(year, month, Math.min(anchor.getDate(), lastDay));
      focusedDate.value = next;
      currentDate.value = viewMode.value === 'week'
        ? next
        : new Date(next.getFullYear(), next.getMonth(), 1);
      focusCellDom(next);
      return true;
    }
    const GRID_KEYS = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'];
    function handleGridKey(key) {
      const cells = calendarCells.value;
      if (!cells.length || !GRID_KEYS.includes(key)) return false;
      const index = focusIndex.value;
      const row = 7;
      if (key === 'ArrowLeft') return moveFocus(index - 1);
      if (key === 'ArrowRight') return moveFocus(index + 1);
      if (key === 'ArrowUp') return moveFocus(index - row);
      if (key === 'ArrowDown') return moveFocus(index + row);
      if (key === 'Home') return moveFocus(index - (index % row));
      if (key === 'End') return moveFocus(index + (row - 1 - (index % row)));
      if (key === 'PageUp') return moveFocusByMonth(-1);
      return moveFocusByMonth(1);
    }
    function clearSelection() {
      selectedEarning.value = null;
      decision.value = null;
      selectedDay.value = calendarCells.value.find(cell => cell.isToday) || null;
    }
    // A cell is a control now (`tabindex="0"`, Enter/Space), so it needs a name:
    // "10月21日周三，5 个财报" is what a screen reader announces instead of a bare day.
    function cellAriaLabel(cell) {
      if (!cell || !cell.date) return '';
      const when = cell.date.toLocaleDateString('zh-CN',
        { month: 'long', day: 'numeric', weekday: 'short' });
      const count = (cell.earnings || []).length;
      return count ? `${when}，${count} 个财报` : `${when}，无财报`;
    }

    return {
      viewMode, currentDate, selectedEarning, decision, selectedDay,
      weekdays, monthLabel, calendarCells,
      prevMonth, nextMonth, goToday, selectEarning, selectCell, clearSelection,
      cellAriaLabel, isSelectedDay,
      calendarWeeks, cellTabIndex, handleGridKey, focusIndex,
    };
  }

  // ── useUrlState: the address bar carries the view (Issue #125) ─────
  // Everything the page showed — tab, month, selected day — lived only in refs.
  // A refresh dropped the reader back to today, a shared link landed the
  // recipient on *their* default view, and 后退 left the site instead of
  // stepping back through the calendar. `?tab=&month=&date=` is written with
  // `pushState` for a tab/month change (a real navigation step) and
  // `replaceState` for the selection (a click-through must not fill the history).
  function useUrlState(host) {
    const TABS = ['calendar', 'watchlist', 'universe'];
    const who = host === undefined ? (typeof window !== 'undefined' ? window : null) : host;
    const MONTH = /^(\d{4})-(\d{2})$/;
    const DAY = /^\d{4}-\d{2}-\d{2}$/;

    function monthKey(date) {
      if (!date || typeof date.getFullYear !== 'function') return '';
      return date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0');
    }
    // Returns null for anything the calendar cannot render — a 13th month, a
    // truncated year, an empty string — so a bad link falls back instead of
    // producing `new Date(NaN)`.
    function monthDate(key) {
      const match = MONTH.exec(String(key || ''));
      if (!match) return null;
      const year = Number(match[1]);
      const month = Number(match[2]);
      if (month < 1 || month > 12 || year < 1970 || year > 2999) return null;
      return new Date(year, month - 1, 1);
    }
    function parseUrlState(search) {
      const params = new URLSearchParams(String(search || '').replace(/^\?/, ''));
      const tab = params.get('tab');
      const month = params.get('month');
      const day = params.get('date');
      // `''` means 「user's default」 — never `undefined`, so the caller can tell
      // "not in the link" from "in the link but unusable".
      return {
        tab: TABS.includes(tab) ? tab : '',
        month: monthDate(month) ? month : '',
        date: DAY.test(String(day || '')) ? day : '',
      };
    }
    // An unusable value is not written back: a link must not carry `?tab=nonsense`
    // for the next reader to copy.
    function buildUrlState(state) {
      const params = new URLSearchParams();
      if (state && TABS.includes(state.tab)) params.set('tab', state.tab);
      if (state && monthDate(state.month)) params.set('month', state.month);
      if (state && DAY.test(String(state.date || ''))) params.set('date', state.date);
      const query = params.toString();
      return query ? '?' + query : '';
    }
    function write(mode, state) {
      const query = buildUrlState(state);
      if (!who || !who.history || !who.location) return query;
      const url = query || who.location.pathname || '';
      if (mode === 'push') who.history.pushState({}, '', url);
      else who.history.replaceState({}, '', url);
      return query;
    }

    return {
      TABS, monthKey, monthDate, parseUrlState, buildUrlState,
      pushUrl: (state) => write('push', state),
      replaceUrl: (state) => write('replace', state),
    };
  }

  // ── useWatchlist: search + CRUD + own "next earnings" source ────
  function useWatchlist(apiFetch, showToast, loadEarnings) {
    const watchlist = ref([]);
    // Issue #109: "the watchlist is empty" and "the watchlist has not been read
    // yet" are different answers, but both look like `length === 0`. The iCal
    // dialog picks its default scope from this fact, so it needs the flag: an
    // unloaded list must never be read as an empty one. Set after every attempt
    // (success or failure) — a failed read that leaves `watchlist` empty is the
    // first-load case, where the non-empty calendar is still the better default
    // than the empty one this dialog used to hand out.
    const watchlistLoaded = ref(false);
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
      try {
        const data = await apiFetch('/api/watchlist');
        if (data) watchlist.value = data;
      } finally {
        // Both outcomes count as "we asked": the iCal dialog must not treat a
        // slow request as an empty watchlist (Issue #109).
        watchlistLoaded.value = true;
      }
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

    // Both write paths check what came back before announcing success (Issue
    // #82): `apiFetch` returns `null` for every rejected/failed write, and the
    // failure toast it already shows must not be overwritten by a success toast
    // for a change that never happened — nor by a refresh (`loadEarnings`
    // clears the read error) racing the explanation into oblivion.
    async function addToWatchlist(symbol, market) {
      const added = await apiFetch(`/api/watchlist?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(market)}`, { method: 'POST', expectOk: true });
      if (!added) return;
      await loadWatchlist();
      await loadEarnings();
      await loadNextEarnings();
      showToast('已添加 ' + symbol);
      searchQuery.value = '';
      searchResults.value = [];
    }

    async function removeFromWatchlist(symbol, market) {
      const removed = await apiFetch(`/api/watchlist?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(market)}`, { method: 'DELETE', expectOk: true });
      if (!removed) return;
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
      watchlist, watchlistLoaded, searchQuery, searchResults, searchLoading,
      usWatchlist, hkWatchlist, nextEarnings, nextBySymbol,
      loadWatchlist, loadNextEarnings, doSearch, addToWatchlist, removeFromWatchlist,
      isMine, toggleWatchlist, watchlistInsight,
    };
  }

  // ── useIcal: modal + URL building ───────────────────────────────
  function useIcal(user, watchlist, watchlistLoaded) {
    const showIcalModal = ref(false);
    const icalUrl = ref('');
    const icalOptions = ref({ lang: 'zh', scope: 'watchlist', predicted: true, markets: 'all' });
    const copied = ref(false);
    // The scope default below is a *default*: once the reader has chosen a
    // scope by hand it must survive closing and reopening the dialog, or the
    // URL they copy would silently contradict the choice they just made.
    let scopeChosen = false;

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

    // Issue #109: `scope=watchlist` on an empty watchlist is a valid feed with
    // zero events (Issue #25 keeps that contract), so the dialog's old hardcoded
    // default handed a first-time subscriber an empty calendar — the exact
    // opposite of the 491 events they had just seen in the app. The default now
    // follows the watchlist: empty (and *known* empty) → the system universe,
    // non-empty → 仅自选, i.e. today's address byte for byte.
    function openIcal() {
      if (!scopeChosen && watchlistLoaded.value) {
        icalOptions.value.scope = watchlist.value.length === 0 ? 'all' : 'watchlist';
      }
      showIcalModal.value = true;
      updateIcalUrl();
    }

    function markScopeChosen() { scopeChosen = true; }

    function copyIcal() {
      updateIcalUrl();
      navigator.clipboard.writeText(icalUrl.value);
      copied.value = true;
      setTimeout(() => copied.value = false, 2000);
    }

    return { showIcalModal, icalUrl, icalOptions, copied, popularSuggestions,
             updateIcalUrl, copyIcal, openIcal, markScopeChosen };
  }

  // ── useAdmin: admin panel + managed watchlist ───────────────────
  function useAdmin(apiFetch, showToast) {
    const showAdmin = ref(false);
    const adminLoading = ref(false);
    const adminSource = ref({});
    const managedWatchlist = ref([]);
    const syncRuns = ref([]);
    const managedForm = ref({ id: null, symbol: '', market: 'US' });

    // Issue #124: the panel only ever listed sync runs, although the backend has
    // served the audit trail (`GET /api/admin/audit-log`) and the diagnostics
    // snapshot (`GET /api/admin/diagnostics`) all along — answering 「谁改过自选」
    // or 「缓存有没有命中」 meant SSH + SQL. Three panes, loaded on demand: opening
    // the dialog must not pay for two extra reads nobody asked for.
    const ADMIN_TABS = [
      { value: 'managed', label: '自选与同步' },
      { value: 'audit', label: '审计' },
      { value: 'diagnostics', label: '诊断' },
    ];
    const adminTab = ref('managed');
    const auditEntries = ref([]);
    const auditLoading = ref(false);
    const auditError = ref(null);
    const diagnostics = ref(null);
    const diagnosticsLoading = ref(false);
    const diagnosticsError = ref(null);

    // ── Audit details: render-time redaction (Issue #124, same rule as #114) ──
    // `details` is free-form JSONB written by several call sites. Rendering it
    // verbatim would put whatever a future caller stored on screen, so the keys
    // that name a credential are blanked and an iCal path keeps only its prefix.
    const REDACTED = '[REDACTED]';
    const SECRET_KEY = /(token|secret|passw|credential|api[_-]?key|private)/i;
    const ICAL_PATH = /(\/ical\/)[A-Za-z0-9_-]{16,}/g;

    function redactDetails(details) {
      if (!details || typeof details !== 'object') return {};
      const walk = (value) => {
        if (typeof value === 'string') return value.replace(ICAL_PATH, '$1' + REDACTED);
        if (Array.isArray(value)) return value.map(walk);
        if (value && typeof value === 'object') {
          const nested = {};
          for (const [key, inner] of Object.entries(value)) {
            nested[key] = SECRET_KEY.test(key) ? REDACTED : walk(inner);
          }
          return nested;
        }
        return value;
      };
      const out = {};
      for (const [key, value] of Object.entries(details)) {
        out[key] = SECRET_KEY.test(key) ? REDACTED : walk(value);
      }
      return out;
    }
    function auditDetailsText(entry) {
      try { return JSON.stringify(redactDetails(entry && entry.details)); } catch (e) { return '—'; }
    }

    async function loadAudit() {
      auditLoading.value = true;
      auditError.value = null;
      try {
        const data = await apiFetch('/api/admin/audit-log?limit=50');
        if (data === null || data === undefined) {
          auditError.value = '接口未返回数据';
          return;
        }
        auditEntries.value = data;
      } finally {
        auditLoading.value = false;
      }
    }

    // Four read-only blocks in the shape the operator asks the question:
    // what the caches did, how the providers behaved, whether the pipeline is
    // fresh, and how many future-dated rows still carry actuals (#111).
    function diagnosticsSections(diag) {
      if (!diag) return [];
      const pct = (value) => (typeof value === 'number' ? (value * 100).toFixed(1) + '%' : '—');
      const cache = diag.cache || {};
      const cacheRows = [
        { label: '命中 / 未命中', value: (cache.hits ?? 0) + ' / ' + (cache.misses ?? 0) },
        { label: '命中率', value: pct(cache.hit_rate) },
        { label: '返回旧值次数', value: String(cache.stale_returns ?? 0) },
        { label: '刷新成功 / 失败', value: (cache.refresh_ok ?? 0) + ' / ' + (cache.refresh_fail ?? 0) },
      ];
      for (const [name, stats] of Object.entries(diag.cache_by_name || {})) {
        cacheRows.push({
          label: name + ' 命中率',
          value: pct(stats.hit_rate) + '（' + (stats.hits ?? 0) + ' / ' + ((stats.hits ?? 0) + (stats.misses ?? 0)) + '）',
        });
      }
      const providerRows = Object.entries(diag.providers || {}).map(([name, stats]) => ({
        label: name,
        value: (stats.calls ?? 0) + ' 次 · 成功率 ' + pct(stats.success_rate) + ' · ' + (stats.avg_ms ?? 0) + 'ms'
          + (stats.errors && Object.values(stats.errors).some(Boolean)
            ? ' · 错误 ' + Object.entries(stats.errors).filter(([, n]) => n).map(([kind, n]) => kind + ' ' + n).join(', ')
            : ''),
      }));
      // `app/freshness.check_freshness()` names its buckets `stale_stages` /
      // `failed_stages` / `never_run_stages` / `stale_data`, and the derived
      // tables are a separate list from the stages — reading the wrong key here
      // would render zeros while the payload says two stages failed.
      const fresh = diag.freshness || {};
      const names = (value) => (Array.isArray(value) && value.length ? value.join('、') : '无');
      const freshRows = [
        { label: '总体', value: (fresh.status || '—') + (fresh.error_code ? ' · ' + fresh.error_code : '') },
        { label: '判定阈值', value: (fresh.threshold_hours ?? '—') + ' 小时' },
        { label: '失败阶段', value: names(fresh.failed_stages) },
        { label: '落后阶段', value: names(fresh.stale_stages) },
        { label: '从未运行阶段', value: names(fresh.never_run_stages) },
        { label: '落后数据表', value: names(fresh.stale_data) },
        { label: '检查时间', value: fresh.checked_at ? new Date(fresh.checked_at).toLocaleString('zh-CN', { hour12: false }) : '—' },
      ];
      return [
        { key: 'cache', title: '缓存', rows: cacheRows },
        { key: 'providers', title: '外部提供方', rows: providerRows },
        { key: 'freshness', title: '数据新鲜度', rows: freshRows },
        { key: 'future_reported', title: '未来日期带实际值', rows: [
          { label: '行数', value: String(diag.future_reported ?? 0) },
          { label: '说明', value: '应为 0；大于 0 表示有未发生的事件已带实际值' },
        ] },
      ];
    }

    async function loadDiagnostics() {
      diagnosticsLoading.value = true;
      diagnosticsError.value = null;
      try {
        const data = await apiFetch('/api/admin/diagnostics');
        if (data === null || data === undefined) {
          diagnosticsError.value = '接口未返回数据';
          return;
        }
        diagnostics.value = data;
      } finally {
        diagnosticsLoading.value = false;
      }
    }

    function selectAdminTab(tab) {
      adminTab.value = tab;
      if (tab === 'audit') loadAudit();
      if (tab === 'diagnostics') loadDiagnostics();
    }

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
      if (adminTab.value === 'audit') await loadAudit();
      else if (adminTab.value === 'diagnostics') await loadDiagnostics();
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
      // Issue #124
      adminTabs: ADMIN_TABS, adminTab, selectAdminTab,
      auditEntries, auditLoading, auditError, loadAudit, auditDetailsText, redactDetails,
      diagnostics, diagnosticsLoading, diagnosticsError, diagnosticsSections, loadDiagnostics,
    };
  }

  // ── useSystemUniverse: read-only view of the default watchlist ──────
  // The calendar, the export and the iCal feed all render one symbol range —
  // the configured watchlist source (Kurumi's shared list + FinCal's managed
  // list). Until now only the admin panel could show what that range *is*, so
  // a user could not tell whether a symbol was never tracked or simply had no
  // upcoming report. This tab answers 「系统到底追踪哪些标的」 for every
  // user and has no write path at all: the editable list stays admin-only
  // (`/api/admin/watchlist`), which is what makes it safe for everyone.
  function useSystemUniverse(apiFetch) {
    const EMPTY = {
      source: '', configured: '', transport: '',
      external_dependency: false, stale: false, error_code: null,
      updated_at: null, counts: { US: 0, HK: 0, total: 0 }, items: [], skipped: [],
    };
    const systemUniverse = ref({ ...EMPTY, counts: { ...EMPTY.counts } });
    const universeLoading = ref(false);

    const usUniverse = computed(() => (systemUniverse.value.items || []).filter(i => i.market === 'US'));
    const hkUniverse = computed(() => (systemUniverse.value.items || []).filter(i => i.market === 'HK'));

    // The configured source, spelled the way the rest of the product talks
    // about it (`app/watchlist/loader.py`: tsummt+managed / managed / http).
    const universeSourceLabel = computed(() => {
      const src = (systemUniverse.value.source || '').toLowerCase();
      if (src.includes('fallback')) return '内置兜底清单（配置来源暂不可用）';
      const parts = [];
      if (src.includes('tsummt')) parts.push('Kurumi 共享清单');
      if (src.includes('managed')) parts.push('FinCal 本地自选');
      if (src.includes('http')) parts.push('外部 HTTP 清单');
      return parts.length ? parts.join(' + ') : (systemUniverse.value.configured || '—');
    });

    function universeOriginLabel(origin) {
      if (origin === 'managed') return 'FinCal 自选';
      if (origin === 'shared') return 'Kurumi 共享';
      return '—';
    }

    // Not `fmtDateTime`: that one renders an empty value as 「进行中」 (a sync
    // run's state), which would read as a lie for "never read successfully".
    const universeUpdatedLabel = computed(() => {
      const value = systemUniverse.value.updated_at;
      return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '尚未成功读取';
    });

    // The dropped codes are worth showing (they answer 「为什么搜不到这只票」)
    // but the list is A-shares and ETFs, ~25 long: show the count and a sample
    // instead of a wall of codes.
    const universeSkippedLabel = computed(() => {
      const skipped = systemUniverse.value.skipped || [];
      if (!skipped.length) return '';
      const sample = skipped.slice(0, 8).join('、');
      return skipped.length > 8 ? `${sample} 等 ${skipped.length} 个` : sample;
    });

    async function loadUniverse() {
      universeLoading.value = true;
      try {
        const data = await apiFetch('/api/universe');
        if (data) systemUniverse.value = { ...EMPTY, ...data, counts: data.counts || EMPTY.counts };
      } finally {
        universeLoading.value = false;
      }
    }

    return {
      systemUniverse, universeLoading, usUniverse, hkUniverse,
      universeSourceLabel, universeOriginLabel, universeUpdatedLabel,
      universeSkippedLabel, loadUniverse,
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
      // Issue #111: the API withheld an actual whose report date is still ahead —
      // the event has not been disclosed, so there is nothing to compare yet.
      report_date_in_future: '报告尚未发生',
    };
    function attributionNote(reason, subject) {
      if (!reason) return '';
      const label = ATTRIBUTION_LABELS[reason] || '口径不可比';
      return subject ? subject + label + '，无法比较' : label + '，无法比较';
    }
    function comparisonNote(e) {
      return attributionNote(e && e.comparison_unavailable_reason, '预期与实际');
    }
    // The 「较预期」 cell is ~110px wide: print the cause (`币种不同`), not the whole
    // sentence — `预期与实际币种不同，无法比较` wrapped onto three lines inside the
    // row it was explaining. The full wording stays in the cell's title and in the
    // footnote below the table.
    function comparisonShortNote(e) {
      const reason = e && e.comparison_unavailable_reason;
      return reason ? (ATTRIBUTION_LABELS[reason] || '口径不可比') : '';
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
    // Issue #121: `revision_trend.<metric>.direction` is `unavailable` when that
    // metric had fewer than two non-null snapshots, but the panel's ternary chain
    // only knew `up`/`down` and sent everything else to 「持平」 — a verdict about
    // a comparison that was never made (the revenue half of the same row printed
    // 「—」, so the row contradicted itself). 「持平」 belongs to `flat` alone.
    function revisionDirectionLabel(direction) {
      if (direction === 'up') return '上修';
      if (direction === 'down') return '下修';
      if (direction === 'flat') return '持平';
      return '无历史样本（可比快照不足 2 次）';
    }
    function revisionIsComparable(series) {
      return Boolean(series) && series.direction !== 'unavailable';
    }
    // Issue #120: two decimals cannot carry a sub-cent EPS. `2068.HK` reported
    // -0.0038 and `NET` -0.00263, both rendered `-0.00`, while the same row's
    // 「较预期」 was computed from the real value — a row that contradicted
    // itself. Keep two decimals from 0.1 up (the normal case, and the trailing
    // zero convention the prices already follow); below that keep at least three
    // significant digits, capped at six decimals. Zero is still `0.00`.
    function fmtNum(n) {
      if (n == null) return '—';
      const v = Number(n);
      if (!Number.isFinite(v)) return '—';
      if (v === 0 || Math.abs(v) >= 0.1) return v.toFixed(2);
      return v.toFixed(Math.min(6, 2 + Math.ceil(-Math.log10(Math.abs(v)))));
    }
    // The month cell is ~50px wide on a phone, where `2068.HK 26Q3` was simply
    // sliced by the cell's overflow. The suffix is redundant inside the calendar —
    // the badge is colour-coded per market and the legend says so — so narrow
    // screens print `2068`; the title attribute still carries the full code.
    function compactSymbol(symbol) {
      return String(symbol == null ? '' : symbol).replace(/\.(HK|US)$/i, '');
    }
    // A stored label can be the literal string "unknown" (Longbridge's calendar
    // never states a currency, and the sync writes that sentinel rather than NULL),
    // which must read as "not stated" — printing "unknown" as a unit is noise.
    function currencyOf(value) {
      const text = value == null ? '' : String(value).trim();
      return text === '' || text.toLowerCase() === 'unknown' ? '' : text;
    }
    // Estimate and actual in different currencies is exactly why 「较预期」 is
    // withheld, so the unit has to be visible: same code → muted, different → warn.
    function currenciesDiffer(e) {
      const estimate = currencyOf(e && e.estimate_currency);
      const actual = currencyOf(e && e.actual_currency);
      return Boolean(estimate && actual && estimate !== actual);
    }
    function fmtPct(ratio) { return ratio == null ? '—' : (ratio * 100).toFixed(1) + '%'; }
    function signedPct(ratio) {
      if (ratio == null) return '—';
      const text = fmtPct(ratio);
      return ratio > 0 ? '+' + text : text;
    }
    function fmtBigNum(n) {
      if (n == null) return '—';
      const v = Number(n);
      if (!isFinite(v)) return '—';
      if (Math.abs(v) >= 1e12) return (v / 1e12).toFixed(2) + 'T';
      if (Math.abs(v) >= 1e9) return (v / 1e9).toFixed(2) + 'B';
      if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + 'M';
      // Issue #130: below 1e6 this used to fall through to `toLocaleString()`,
      // so a HK small cap printed `1,234,567` beside `113.35B` and `9.21M` — no
      // unit, widest cell in the column, and the magnitude had to be counted by
      // eye. `K` completes the ladder (2/2/1/1 decimals, same as its neighbours);
      // below 1000 the bare number is already readable.
      if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + 'K';
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
      revisionDirectionLabel, revisionIsComparable,
      compactSymbol, currenciesDiffer, currencyOf, comparisonShortNote,
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
      const { user, appConfig, loading, error, writeError, toast, apiFetch, showToast, dismissErrors, loadUser, login } = api;

      // 2. Earnings data (shared between calendar and watchlist)
      const earnings = ref([]);
      const appTab = ref('calendar');
      const watchlistOnly = ref(false);

      // 3. Calendar
      const cal = useCalendar(earnings, null, watchlistOnly);

      // 3b. Address bar (Issue #125). Read once, before the first load, so a
      // shared link renders the view it names; the month is applied to
      // `currentDate` immediately because the earnings request window follows it.
      const url = useUrlState();
      const urlInitial = url.parseUrlState(typeof window !== 'undefined' ? window.location.search : '');
      if (urlInitial.tab) appTab.value = urlInitial.tab;
      const initialMonth = url.monthDate(urlInitial.month);
      if (initialMonth) cal.currentDate.value = initialMonth;
      // The day can only be resolved once the month's cells exist (after the
      // first load), and a history step can change the month under it — so it
      // lives here as pending state that `loadEarnings()` applies, not as a
      // one-shot call after mount.
      let pendingDateKey = urlInitial.date;
      // Set while a `popstate` is being applied: the watchers below must not
      // write the address bar back while it is being read.
      let restoringFromHistory = false;

      // 4. Watchlist (needs loadEarnings callback)
      const wl = useWatchlist(apiFetch, showToast, () => loadEarnings());
      cal.watchlist = wl.watchlist; // wire watchlist into calendar

      // 5. iCal (its default scope follows the watchlist — Issue #109)
      const ical = useIcal(user, wl.watchlist, wl.watchlistLoaded);

      // 6. Admin
      const admin = useAdmin(apiFetch, showToast);

      // 6b. System universe (read-only for every user)
      const su = useSystemUniverse(apiFetch);

      // 7. Formatters
      const fmt = useFormatters();

      // ── Dialog focus management ──────────────────────────────
      // A dialog needs a focus owner: without one Esc never reaches its handler, Tab
      // walks out through the backdrop into the page behind it, and closing strands
      // focus on <body>. Both dialogs (订阅日历, 管理) go through the same two helpers.
      const icalDialog = ref(null);
      const adminDialog = ref(null);
      let dialogReturnFocus = null;

      function trapTab(event) {
        const dialog = event.currentTarget;
        const focusables = [...dialog.querySelectorAll(
          'a[href], button:not([disabled]), input:not([disabled]), ' +
          'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )].filter(el => el.offsetWidth || el.offsetHeight || el === document.activeElement);
        if (!focusables.length) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        } else if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        }
      }

      watch([() => ical.showIcalModal.value, () => admin.showAdmin.value],
        async ([icalOpen, adminOpen]) => {
          const dialog = icalOpen ? icalDialog.value : adminOpen ? adminDialog.value : null;
          if (dialog) {
            dialogReturnFocus = document.activeElement;
            document.body.style.overflow = 'hidden';
            await nextTick();
            const firstControl = dialog.querySelector(
              'input, select, textarea, button:not([disabled]), a[href]');
            (firstControl || dialog).focus();
            return;
          }
          document.body.style.overflow = '';
          if (dialogReturnFocus && dialogReturnFocus.focus) dialogReturnFocus.focus();
          dialogReturnFocus = null;
        }, { flush: 'post' });  // the dialog ref only exists after the DOM update

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
              // A link (or a history step) names a day; it wins over 「today」.
              if (pendingDateKey) applyDateKey(pendingDateKey);
            }
          }
        } finally {
          loading.value = false;
        }
      }

      // ── Address bar writes (Issue #125) ──────────────────────
      function currentUrlState() {
        return {
          tab: appTab.value,
          month: url.monthKey(cal.currentDate.value),
          date: cal.selectedDay.value ? localYmd(cal.selectedDay.value.date) : '',
        };
      }
      // A tab or month change is a step the reader may want to undo, so it pushes;
      // selecting a day is a click-through inside the same view, so it replaces —
      // otherwise every cell tap would need its own 后退 press. A `popstate`
      // already *is* the history change, so it must not write one back.
      watch([appTab, cal.currentDate], () => {
        if (!restoringFromHistory) url.pushUrl(currentUrlState());
      });
      watch(() => (cal.selectedDay.value ? localYmd(cal.selectedDay.value.date) : ''),
        () => { if (!restoringFromHistory) url.replaceUrl(currentUrlState()); });

      // Point the selection at a day of the currently rendered month; a key that
      // is not in the window keeps whatever the load already chose (today).
      function applyDateKey(key) {
        if (!key) return false;
        const cell = cal.calendarCells.value.find(c => localYmd(c.date) === key);
        if (!cell) return false;
        cal.selectedEarning.value = null;
        cal.decision.value = null;
        cal.selectedDay.value = cell;
        return true;
      }

      // 后退/前进 only moves the address bar; the view has to follow it, or the
      // reader sees yesterday's month under today's URL. Everything the link
      // carries is re-applied, and the write-watchers stay quiet while it happens.
      async function applyHistoryState() {
        const state = url.parseUrlState(typeof window !== 'undefined' ? window.location.search : '');
        const month = url.monthDate(state.month);
        restoringFromHistory = true;
        try {
          appTab.value = state.tab || 'calendar';
          if (month) cal.currentDate.value = month;
          pendingDateKey = state.date;
          cal.selectedEarning.value = null;
          cal.decision.value = null;
          // One load for the restored month, and the day applied after it: the
          // month watcher would otherwise race the restore and reset the day.
          await loadEarnings();
          pendingDateKey = '';
          loadTabData(appTab.value);
          await nextTick();
        } finally {
          restoringFromHistory = false;
        }
      }
      if (typeof window !== 'undefined' && window.addEventListener) {
        window.addEventListener('popstate', () => { applyHistoryState(); });
      }

      // ── Lifecycle ────────────────────────────────────────────
      watch(cal.currentDate, () => { if (!restoringFromHistory) loadEarnings(); });

      // The watchlist data must not follow the calendar month; it is refreshed
      // when the page is (re)entered so a long calendar session cannot show a
      // stale "下次财报" (Issue #56).
      // Factored out because a tab named by the address bar (Issue #125) reaches
      // the page without passing through the watch: `appTab` is already set when
      // the watcher is registered, so the entry load has to be callable directly.
      function loadTabData(tab) {
        if (tab === 'watchlist' && user.value) wl.loadNextEarnings();
        // The universe is the same range for everyone and changes only when an
        // admin edits the source, so it is read on entry instead of being kept
        // in step with the calendar month.
        if (tab === 'universe' && user.value) su.loadUniverse();
      }
      watch(appTab, loadTabData);

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
        loadTabData(appTab.value);
      });

      // ── Public API ───────────────────────────────────────────
      return {
        // State
        user, earnings, appTab, watchlistOnly, loading, error, writeError, toast, appConfig,
        // Calendar
        viewMode: cal.viewMode, currentDate: cal.currentDate,
        selectedEarning: cal.selectedEarning, decision: cal.decision, selectedDay: cal.selectedDay,
        weekdays: cal.weekdays, monthLabel: cal.monthLabel, calendarCells: cal.calendarCells,
        cellAriaLabel: cal.cellAriaLabel,
        icalDialog, adminDialog, trapTab,
        prevMonth: cal.prevMonth, nextMonth: cal.nextMonth, goToday: cal.goToday,
        selectCell: cal.selectCell, clearSelection: cal.clearSelection,
        isSelectedDay: cal.isSelectedDay,
        calendarWeeks: cal.calendarWeeks, cellTabIndex: cal.cellTabIndex,
        onGridKeydown: (event) => {
          // The cell keeps its own Enter/Space handler; this one owns navigation.
          if (event.metaKey || event.ctrlKey || event.altKey) return;
          if (cal.handleGridKey(event.key)) event.preventDefault();
        },
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
        // The dialog's default scope depends on the watchlist (Issue #109), and
        // the empty-watchlist hint in the markup reads `watchlistLoaded` too.
        openIcal: ical.openIcal, markScopeChosen: ical.markScopeChosen,
        watchlistLoaded: wl.watchlistLoaded,
        // Admin
        showAdmin: admin.showAdmin, adminLoading: admin.adminLoading, adminSource: admin.adminSource,
        managedWatchlist: admin.managedWatchlist, syncRuns: admin.syncRuns, managedForm: admin.managedForm,
        openAdmin: admin.openAdmin, saveManaged: admin.saveManaged, editManaged: admin.editManaged,
        deleteManaged: admin.deleteManaged, resetManaged: admin.resetManaged, fmtDateTime: admin.fmtDateTime,
        // Admin panes (Issue #124): the audit trail and the diagnostics snapshot
        // were reachable only through curl before.
        adminTabs: admin.adminTabs, adminTab: admin.adminTab, selectAdminTab: admin.selectAdminTab,
        auditEntries: admin.auditEntries, auditLoading: admin.auditLoading, auditError: admin.auditError,
        loadAudit: admin.loadAudit, auditDetailsText: admin.auditDetailsText,
        redactDetails: admin.redactDetails,
        diagnostics: admin.diagnostics, diagnosticsLoading: admin.diagnosticsLoading,
        diagnosticsError: admin.diagnosticsError, diagnosticsSections: admin.diagnosticsSections,
        loadDiagnostics: admin.loadDiagnostics,
        // System universe (read-only)
        systemUniverse: su.systemUniverse, universeLoading: su.universeLoading,
        usUniverse: su.usUniverse, hkUniverse: su.hkUniverse,
        universeSourceLabel: su.universeSourceLabel, universeOriginLabel: su.universeOriginLabel,
        universeUpdatedLabel: su.universeUpdatedLabel, universeSkippedLabel: su.universeSkippedLabel,
        // Formatters
        ...fmt,
        // Shared
        showToast, login, dismissErrors,
        // The calendar's 「只看自选」 checkbox calls this from the template
        // (Issue #80): an internal function is invisible to the runtime-compiled
        // template, and `@change` then silently attaches no listener at all.
        loadEarnings,
      };
    }
  }).mount('#app');
