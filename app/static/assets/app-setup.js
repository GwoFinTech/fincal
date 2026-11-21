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

  // ── 「本周财报」 digest (Issue #133) ────────────────────────────────
  // The calendar's highest-frequency question is 「这周/下周谁要发」, which the
  // month grid answers only by scanning 30+ cells. The digest is a second *view*
  // of the batch the calendar already holds (displayed month ±1), never a second
  // request — so its rows cannot disagree with the grid's.
  const WEEK_AHEAD_DAYS = 7;
  // A season peak carries 700+ events on a single date (2026-11-13: 709 US +
  // 165 HK), so a digest that listed them all would be longer than the month it
  // summarises. Per day: the first few plus an exact 「还有 N 场」 remainder that
  // opens the day list, where the full set already renders.
  const WEEK_AHEAD_MAX_PER_DAY = 5;
  const WEEKDAY_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  // 盘前 → 盘中 → 待定 → 盘后 is the order a trading day reads in.
  const BEFORE_AFTER_RANK = { before: 0, during: 1, after: 3 };

  // `YYYY-MM-DD` → `M/D`; built from the parts, never through `Date.parse`,
  // which reads a bare date string as UTC and would shift the day (#46).
  function monthDayLabel(ymd) {
    const parts = String(ymd == null ? '' : ymd).split('-');
    if (parts.length !== 3) return String(ymd == null ? '' : ymd);
    return Number(parts[1]) + '/' + Number(parts[2]);
  }
  function monthDayWeekday(ymd) {
    const parts = String(ymd == null ? '' : ymd).split('-');
    if (parts.length !== 3) return monthDayLabel(ymd);
    const date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
    if (Number.isNaN(date.getTime())) return monthDayLabel(ymd);
    return monthDayLabel(ymd) + ' ' + WEEKDAY_LABELS[date.getDay()];
  }

  // Group the loaded rows into the next `WEEK_AHEAD_DAYS` days, one entry per
  // date that has an event. `isMine` is the account's own-symbol predicate (or
  // null for the whole system range); `gridDates` is the date set the calendar
  // is currently rendering, which a 「还有 N 场」 remainder needs: the day panel
  // it opens only exists for a rendered cell.
  function weekAheadGroups(rows, today, horizonEnd, isMine, gridDates) {
    const byDate = new Map();
    for (const e of rows || []) {
      const date = e && e.report_date;
      if (!date || date < today || date > horizonEnd) continue;
      if (isMine && !isMine(e)) continue;
      if (!byDate.has(date)) byDate.set(date, []);
      byDate.get(date).push(e);
    }
    const hasGrid = Boolean(gridDates && gridDates.has);
    return [...byDate.keys()].sort().map(date => {
      const all = byDate.get(date).slice().sort((a, b) =>
        (BEFORE_AFTER_RANK[a.before_after] ?? 2) - (BEFORE_AFTER_RANK[b.before_after] ?? 2)
        || String(a.symbol).localeCompare(String(b.symbol)));
      return {
        date,
        label: monthDayWeekday(date),
        total: all.length,
        rows: all.slice(0, WEEK_AHEAD_MAX_PER_DAY),
        more: Math.max(0, all.length - WEEK_AHEAD_MAX_PER_DAY),
        inGrid: hasGrid ? Boolean(gridDates.has(date)) : false,
      };
    });
  }

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
        // A cancelled request is not a failure: `AbortController` supersedes the
        // previous search on every keystroke (Issue #131), and nobody should see
        // a 「请求失败: aborted」 toast for their own next key.
        if (e && e.name === 'AbortError') return null;
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
    // The grid anchor. The month branch only reads its year/month; the week
    // branch reads the whole date, so entering 周 parks the exact day here
    // (Issue #138) instead of leaving it at 当月 1 日.
    const currentDate = ref(new Date());
    const selectedEarning = ref(null);
    const decision = ref(null);
    const selectedDay = ref(null);

    const weekdays = ['日', '一', '二', '三', '四', '五', '六'];

    const monthLabel = computed(() => {
      const d = currentDate.value;
      return d.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long' });
    });

    // 工具栏中间的标签（Issue #140）：周视图下 `monthLabel` 说不清是哪一周——
    // 跨月周只显示锚点所在的月份，而网格里躺着另一个月的格子。这里把首末日期
    // 拼出来，格式与「本周财报」摘要（#133）的 `11/17 → 11/23` 一致（同一个
    // `monthDayLabel`），所以两处的日期读法不会分叉。月视图仍是 `2026年11月`。
    const calendarRangeLabel = computed(() => {
      if (viewMode.value !== 'week') return monthLabel.value;
      const d = currentDate.value;
      const dayOfWeek = d.getDay();
      const start = new Date(d.getFullYear(), d.getMonth(), d.getDate() - dayOfWeek);
      const end = new Date(d.getFullYear(), d.getMonth(), d.getDate() + (6 - dayOfWeek));
      return monthDayLabel(localYmd(start)) + ' → ' + monthDayLabel(localYmd(end));
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
        // The week is built around the anchor's own date, not its month — which
        // is why `setViewMode('week')` parks the selected day here first (#138).
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

    // 周视图的锚点（Issue #138）：选中日 → 当月里的今天 → 当前锚点。
    // 「今天」只在它落在正在看的那个月时才算锚点——翻了几个月之后切「周」，
    // 用户要看的是那个月的一周，不是「今天」那一周。
    function weekAnchorDate() {
      const selected = selectedDay.value && selectedDay.value.date;
      if (selected) return new Date(selected.getFullYear(), selected.getMonth(), selected.getDate());
      const anchor = currentDate.value;
      const today = new Date();
      return today.getFullYear() === anchor.getFullYear() && today.getMonth() === anchor.getMonth()
        ? today
        : null;
    }
    // 月视图的月份导航只把锚点停在当月 1 日，用户选中的那天只活在 `selectedDay`
    // 里；两者一分叉，切「周」看到的就是 1 日那一周，而右侧面板仍停在选中日——
    // 同屏出现两个互相矛盾的「当前日期」。切换视图是两个视图对齐锚点的唯一时机：
    // 进周视图时把锚点搬到选中日，不动选中状态（翻周仍是纯导航）。
    function setViewMode(mode) {
      if (mode !== 'month' && mode !== 'week') return;
      if (mode === viewMode.value) return;
      viewMode.value = mode;
      if (mode !== 'week') return;
      const anchor = weekAnchorDate();
      if (anchor) currentDate.value = anchor;
    }

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
      weekdays, monthLabel, calendarRangeLabel, calendarCells,
      setViewMode, weekAnchorDate,
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
    const TABS = ['calendar', 'watchlist', 'universe', 'history'];
    // Issue #140: 月 / 周 is view state like the month, so it belongs in the link.
    // Only `week` is ever written: 月 is the default, so `view=month` never
    // appears and every link written before this change keeps its exact bytes.
    const VIEWS = ['month', 'week'];
    const who = host === undefined ? (typeof window !== 'undefined' ? window : null) : host;
    const MONTH = /^(\d{4})-(\d{2})$/;
    const DAY = /^\d{4}-\d{2}-\d{2}$/;
    // Issue #141: a symbol can arrive from an iCal event's link. It is the one
    // user-controlled value that reaches a request, so it is whitelisted here —
    // the same set the feed emits (letters, digits, `.`, `-`) — and anything
    // else is dropped instead of being carried into the page.
    const SYMBOL = /^[A-Za-z0-9.\-]{1,20}$/;
    function symbolKey(value) {
      const text = String(value || '').trim();
      return SYMBOL.test(text) ? text : '';
    }
    // Issue #142: 「只看自选」 changes the *request* (`watchlistOnly=true`), so it
    // is view state like the month — but only the explicit `mine=1` counts. The
    // unchecked state is the default, so the key is never written when off and
    // every link from #125/#140/#141 keeps its exact bytes.
    function mineOn(value) {
      return String(value || '') === '1';
    }

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
    // `YYYY-MM-DD` → local Date, or null when the string names no real day. The
    // regex alone accepts `2026-02-31`, which `new Date()` silently rolls over
    // into March — a link would then anchor a week nobody asked for (Issue #140).
    function dayDate(key) {
      const text = String(key || '');
      if (!DAY.test(text)) return null;
      const year = Number(text.slice(0, 4));
      const month = Number(text.slice(5, 7));
      const day = Number(text.slice(8, 10));
      const date = new Date(year, month - 1, day);
      if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return null;
      return date;
    }
    function parseUrlState(search) {
      const params = new URLSearchParams(String(search || '').replace(/^\?/, ''));
      const tab = params.get('tab');
      const month = params.get('month');
      const day = params.get('date');
      const view = params.get('view');
      // `''` means 「user's default」 — never `undefined`, so the caller can tell
      // "not in the link" from "in the link but unusable".
      const state = {
        tab: TABS.includes(tab) ? tab : '',
        month: monthDate(month) ? month : '',
        date: dayDate(day) ? day : '',
      };
      // `view` is only present when the link names one. 月 is the default, and
      // an explicit `view: ''` would make every state object — including the
      // pre-#140 shape the #125 probes pin — carry a key it never had.
      if (VIEWS.includes(view)) state.view = view;
      // Same rule for `symbol` (#141): absent means 「no row was named」, and a
      // value outside the whitelist never enters the state at all.
      const symbol = symbolKey(params.get('symbol'));
      if (symbol) state.symbol = symbol;
      // Same rule for `mine` (#142): only an explicit `1` turns the filter on,
      // and an off filter adds no key — `undefined` would compare truthy-looking
      // in a template interpolation.
      if (mineOn(params.get('mine'))) state.mine = true;
      return state;
    }
    // An unusable value is not written back: a link must not carry `?tab=nonsense`
    // for the next reader to copy.
    function buildUrlState(state) {
      const params = new URLSearchParams();
      if (state && TABS.includes(state.tab)) params.set('tab', state.tab);
      // 月 is the default view, so it is left out and old links stay byte-equal.
      if (state && VIEWS.includes(state.view) && state.view !== 'month') params.set('view', state.view);
      if (state && monthDate(state.month)) params.set('month', state.month);
      if (state && dayDate(state.date)) params.set('date', state.date);
      const symbol = symbolKey(state && state.symbol);
      if (symbol) params.set('symbol', symbol);
      // Off is the default: `mine=0` is never written, so a link that does not
      // filter keeps the byte-identical shape it had before #142.
      if (state && state.mine === true) params.set('mine', '1');
      const query = params.toString();
      return query ? '?' + query : '';
    }
    function write(mode, state) {
      const query = buildUrlState(state);
      if (!who || !who.history || !who.location) return query;
      // One user action can trip two watchers (切「周」 also re-parks the anchor,
      // which a cross-month week moves to another month). A push that changes
      // nothing is not a navigation step — writing it would cost the reader a
      // second 后退 press to undo one click (#140).
      if (mode === 'push' && who.location.search === query) return query;
      const url = query || who.location.pathname || '';
      if (mode === 'push') who.history.pushState({}, '', url);
      else who.history.replaceState({}, '', url);
      return query;
    }

    return {
      TABS, VIEWS, monthKey, monthDate, dayDate, symbolKey, parseUrlState, buildUrlState,
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
    // Issue #131: typing「0700」then「0700.HK」issues two requests, and responses
    // arrive in whatever order the network decides. Without cancellation the
    // slower, older one lands last and the list silently shows results for a
    // query the box no longer contains. So: abort the one in flight, and ignore
    // any answer whose sequence is stale even if it was already decoded.
    let searchController = null;
    let searchSeq = 0;
    async function doSearch() {
      clearTimeout(searchTimer);
      if (searchController) searchController.abort();
      const seq = ++searchSeq;
      if (searchQuery.value.length < 1) { searchResults.value = []; searchLoading.value = false; return; }
      searchLoading.value = true;
      searchTimer = setTimeout(async () => {
        const controller = new AbortController();
        searchController = controller;
        const query = searchQuery.value;
        const data = await apiFetch(`/api/search?q=${encodeURIComponent(query)}`, { signal: controller.signal });
        if (seq !== searchSeq) return; // a newer keystroke owns the results now
        if (searchController === controller) searchController = null;
        searchResults.value = data || [];
        searchLoading.value = false;
      }, 300);
    }

    // Both write paths check what came back before announcing success (Issue
    // #82): `apiFetch` returns `null` for every rejected/failed write, and the
    // failure toast it already shows must not be overwritten by a success toast
    // for a change that never happened — nor by a refresh (`loadEarnings`
    // clears the read error) racing the explanation into oblivion.
    //
    // Issue #137: `already_exists` is a *200*, so #82's `if (!added)` guard
    // cannot see it and the row count never changed while the toast said 「已添加」.
    // The candidate rows are toggles now, but a stale list (another tab, another
    // device, a watchlist written after this page loaded) still lands here — and
    // then the honest answer is 「已在自选」.
    async function addToWatchlist(symbol, market) {
      const added = await apiFetch(`/api/watchlist?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(market)}`, { method: 'POST', expectOk: true });
      if (!added) return;
      await loadWatchlist();
      await loadEarnings();
      await loadNextEarnings();
      // The refresh above is what makes the row flip to 「已自选」, so both
      // branches still refresh; only the sentence changes.
      showToast((added.status === 'already_exists' ? '已在自选 ' : '已添加 ') + symbol);
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

  // ── useSymbolHistory: one company's quarters (Issue #134) ────────
  // Every other surface answers 「这一天谁发财报」; the calendar cannot answer
  // 「这家公司过去几期怎么样」 — a reader had to walk month by month. This view
  // asks the calendar's own read for a single symbol (`/api/earnings?symbol=`),
  // so 较预期、可比性口径与详情面板走的是同一份数据与同一套 formatter，不是第二套
  // 实现；没有历史的标的显示空态，而不是一张看起来像「这家公司从不发财报」的空表。
  function useSymbolHistory(apiFetch) {
    const HISTORY_PERIODS = 12;
    // Three years back covers ~12 quarters. Forward reach is one quarter, not
    // the calendar's whole 420-day prediction horizon: the predictor places four
    // quarters ahead, and on a 12-row table those empty 「预」 rows would push the
    // real history out. One upcoming period is the useful 「下一期」 header; the
    // span stays inside `EARNINGS_HISTORY_MAX_WINDOW_DAYS`.
    const HISTORY_LOOKBACK_YEARS = 3;
    const HISTORY_FORWARD_DAYS = 120;
    const historySymbol = ref('');
    const historyRows = ref([]);
    const historyLoading = ref(false);
    const historyQuery = ref('');
    const historyResults = ref([]);
    const historySearchLoading = ref(false);
    let searchTimer = null;
    let searchController = null;
    let searchSeq = 0;

    const historyTitle = computed(() => {
      const first = historyRows.value[0];
      if (!first) return '';
      return first.company_name || first.company_name_en || '';
    });
    const historyMarket = computed(() => (historyRows.value[0] || {}).market || '');
    // Newest first and capped: the page is 「最近 N 期」, not the whole archive.
    const historyPeriods = computed(() => [...historyRows.value]
      .sort((a, b) => String(b.report_date).localeCompare(String(a.report_date)))
      .slice(0, HISTORY_PERIODS));

    function ymd(date) {
      return date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0') +
        '-' + String(date.getDate()).padStart(2, '0');
    }

    async function loadSymbolHistory(symbol) {
      const code = String(symbol || historySymbol.value || '').trim();
      historySymbol.value = code;
      if (!code) { historyRows.value = []; return; }
      historyLoading.value = true;
      try {
        const today = new Date();
        const start = new Date(today.getFullYear() - HISTORY_LOOKBACK_YEARS,
          today.getMonth(), today.getDate());
        const end = new Date(today.getFullYear(), today.getMonth(),
          today.getDate() + HISTORY_FORWARD_DAYS);
        const params = new URLSearchParams({ symbol: code, start: ymd(start), end: ymd(end) });
        const data = await apiFetch('/api/earnings?' + params.toString());
        // A slower answer for a symbol the reader has already left must not
        // replace the table they are looking at.
        if (historySymbol.value === code) historyRows.value = Array.isArray(data) ? data : [];
      } finally {
        historyLoading.value = false;
      }
    }

    // Same debounce/abort discipline as the watchlist box (Issue #137): an answer
    // to a stale keystroke must never overwrite the current one.
    async function doHistorySearch() {
      clearTimeout(searchTimer);
      if (searchController) searchController.abort();
      const seq = ++searchSeq;
      const query = historyQuery.value.trim();
      if (!query) { historyResults.value = []; historySearchLoading.value = false; return; }
      historySearchLoading.value = true;
      searchTimer = setTimeout(async () => {
        const controller = new AbortController();
        searchController = controller;
        const data = await apiFetch('/api/search?q=' + encodeURIComponent(query),
          { signal: controller.signal });
        if (seq !== searchSeq) return; // a newer keystroke owns the results now
        if (searchController === controller) searchController = null;
        historyResults.value = data || [];
        historySearchLoading.value = false;
      }, 300);
    }

    function openSymbol(item) {
      historyQuery.value = '';
      historyResults.value = [];
      return loadSymbolHistory(item && item.symbol ? item.symbol : '');
    }

    return {
      historySymbol, historyTitle, historyMarket, historyPeriods, historyLoading,
      historyQuery, historyResults, historySearchLoading,
      loadSymbolHistory, doHistorySearch, openSymbol,
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
      const reason = growthReason(decision, key);
      if (reason) return attributionNote(reason, '本期与上期');
      // Issue #146: the panel may be showing the provider's own yoy instead of a
      // ratio this app computed, and a percentage over a tiny base is a base
      // effect rather than a trend. The title therefore carries both the base and
      // which of the two sources produced the number.
      const growth = (decision && decision.actual_growth) || {};
      const base = growth.base && growth.base[key.startsWith('eps') ? 'eps' : 'revenue'];
      const parts = [];
      if (base != null) parts.push('基期 ' + (key.startsWith('eps') ? fmtNum(base) : fmtBigNum(base)));
      parts.push('来源 ' + growthSourceLabel(decision));
      return parts.join('；');
    }
    // 「provider」= 提供方在财报行上自带的同比；「statements」= 本服务用两个期间的实际值算的。
    function growthSourceLabel(decision) {
      const source = ((decision && decision.actual_growth) || {}).source;
      if (source === 'provider') return '财报提供方（同一报表行）';
      if (source === 'statements') return '本服务按已记录实际值计算';
      if (source === 'mixed') return '两者混用（提供方缺哪期就用自算补）';
      return '无';
    }
    // ── 同比趋势（Issue #146）─────────────────────────────────────────────
    // Up to 8 stored provider periods. Only periods with a number are charted; a
    // withheld period keeps its own label and reason so the gap is visible.
    function growthTrendPoints(decision) {
      const trend = decision && decision.actual_growth_trend;
      if (!trend || !Array.isArray(trend.points)) return [];
      return trend.points.map(point => ({
        label: fqShort(point.fiscal_year, point.fiscal_quarter),
        revenue: point.revenue_yoy == null ? null : Number(point.revenue_yoy),
        eps: point.eps_yoy == null ? null : Number(point.eps_yoy),
        revenueBase: point.revenue_base == null ? null : Number(point.revenue_base),
        epsBase: point.eps_base == null ? null : Number(point.eps_base),
        reason: point.reason || '',
      }));
    }
    function fqShort(year, quarter) {
      if (!year || !quarter) return '—';
      return String(year).slice(2) + 'Q' + quarter;
    }
    // Bar length as a percentage of the tallest bar, drawn from a centre axis:
    // `direction` is -1 (left) / 1 (right) / 0 (no value, nothing drawn).
    function growthBarWidth(value, maxAbs) {
      if (value == null || !isFinite(value) || !maxAbs) return 0;
      const width = Math.min(50, Math.abs(value) / maxAbs * 50);
      return Math.round(width * 10) / 10;
    }
    function growthBarDirection(value) {
      if (value == null || !isFinite(value) || value === 0) return 0;
      return value > 0 ? 1 : -1;
    }
    // Each row is scaled on its own, and values beyond this cap do not set the
    // scale: a base-effect quarter (FISV EPS -865% off a near-zero base) would
    // otherwise shrink every other bar to a hairline. The cap only decides bar
    // *length*; the number beside the bar is always the real one.
    const GROWTH_TREND_OUTLIER_CAP = 3; // 300%
    function growthTrendMaxAbs(points, series) {
      // `series` ('revenue' | 'eps') scales that row alone; without it both rows
      // share one scale.
      const raw = series
        ? points.map(point => point[series])
        : points.flatMap(point => [point.revenue, point.eps]);
      const values = raw.filter(value => value != null && isFinite(value))
        .map(value => Math.abs(value));
      if (!values.length) return 0;
      const inRange = values.filter(value => value <= GROWTH_TREND_OUTLIER_CAP);
      return inRange.length ? Math.max(...inRange) : Math.max(...values);
    }
    function growthBarClass(value) {
      // 涨绿跌红 follows the panel's own convention, and a withheld period is
      // neutral — never coloured as a fall.
      if (value == null) return 'growth-bar-empty';
      return value > 0 ? 'growth-bar-up' : value < 0 ? 'growth-bar-down' : 'growth-bar-flat';
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
    // Issue #143: the count is a named rule because `revisionPair()` has to know
    // where this formatter stops before it may append a digit.
    function numDecimals(n) {
      const v = Number(n);
      if (!Number.isFinite(v)) return 2;
      if (v === 0 || Math.abs(v) >= 0.1) return 2;
      return Math.min(6, 2 + Math.ceil(-Math.log10(Math.abs(v))));
    }
    function fmtNum(n, decimals) {
      if (n == null) return '—';
      const v = Number(n);
      if (!Number.isFinite(v)) return '—';
      return v.toFixed(decimals == null ? numDecimals(v) : decimals);
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
      const scale = bigNumberScale(v);
      if (!scale) return v.toLocaleString();
      return (v / scale).toFixed(bigNumberDecimals(scale)) + BIG_NUMBER_SUFFIX[scale];
    }
    // Issue #130: below 1e6 this used to fall through to `toLocaleString()`, so a
    // HK small cap printed `1,234,567` beside `113.35B` and `9.21M` — no unit,
    // widest cell in the column, and the magnitude had to be counted by eye. `K`
    // completes the ladder (2/2/1/1 decimals, same as its neighbours); below 1000
    // the bare number is already readable.
    // Issue #143: the ladder lives in one place so `revisionPair()` can ask which
    // divisor a value used and how many digits matching that unit already spends.
    // (A scale plus a suffix table, not descriptor objects: a brace-returning
    // helper inside this composable reads as its export literal to
    // tests/frontend_contract.py.)
    const BIG_NUMBER_SUFFIX = { 1000000000000: 'T', 1000000000: 'B', 1000000: 'M', 1000: 'K' };
    function bigNumberScale(v) {
      const abs = Math.abs(v);
      if (abs >= 1e12) return 1e12;
      if (abs >= 1e9) return 1e9;
      if (abs >= 1e6) return 1e6;
      if (abs >= 1e3) return 1e3;
      return 0;
    }
    function bigNumberDecimals(scale) {
      if (!scale) return 0;
      return scale === 1e12 || scale === 1e9 ? 2 : 1;
    }
    function fmtBigNumCompact(n, decimals) {
      if (n == null) return '—';
      const v = Number(n);
      if (!isFinite(v)) return '—';
      const scale = bigNumberScale(v);
      if (!scale) return v.toFixed(decimals);
      return (v / scale).toFixed(decimals) + BIG_NUMBER_SUFFIX[scale];
    }
    // Issue #143: 「预期修正趋势」 prints both ends of one series while its label is
    // derived from the raw difference. `fmtNum` gives two decimals from 0.1 up, so
    // AMD's `1.4197 → 1.41762` (±0.002, direction `down`) rendered 「1.42 → 1.42
    // （下修）」 — the numbers said nothing moved and the brackets said it did (960
    // EPS pairs and 1,085 revenue pairs in the 2026-11 snapshot). The direction is
    // right, so the display has to be able to express it: only when both ends
    // render identically *and* the raw values differ, step the precision up one
    // digit at a time from wherever the default stopped, capped at six decimals
    // (the precision #120 already accepts). Every other row is byte-identical.
    function revisionPair(first, latest, big) {
      const render = (value, decimals) => {
        if (big) return decimals == null ? fmtBigNum(value) : fmtBigNumCompact(value, decimals);
        return decimals == null ? fmtNum(value) : fmtNum(value, decimals);
      };
      const plain = render(first) + ' → ' + render(latest);
      const a = Number(first);
      const b = Number(latest);
      // A missing end already prints 「—」; equal raw values（持平）are *meant* to
      // print one number twice. Neither is the collision this helper exists for.
      if (first == null || latest == null || !Number.isFinite(a) || !Number.isFinite(b) || a === b) return plain;
      if (render(first) !== render(latest)) return plain;
      const base = big
        ? Math.max(bigNumberDecimals(bigNumberScale(a)), bigNumberDecimals(bigNumberScale(b)))
        : Math.max(numDecimals(a), numDecimals(b));
      for (let decimals = base + 1; decimals <= 6; decimals += 1) {
        if (render(first, decimals) !== render(latest, decimals)) {
          return render(first, decimals) + ' → ' + render(latest, decimals);
        }
      }
      return plain;
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
      growthSourceLabel, growthTrendPoints, growthBarWidth, growthBarDirection,
      growthTrendMaxAbs, growthBarClass, fqShort, 
      beatMissStreak, beatMissStreakText, beatMissStreakNote,
      metricDelta, fmtNum, fmtPct, signedPct, fmtBigNum,
      revisionPair, revisionDirectionLabel, revisionIsComparable,
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
      // Issue #140: 月 / 周 lives in the link now. A week is anchored by the day
      // the link names, so that day is applied *before* the first load — the
      // request window follows the anchor's month. A bare `?view=week` with no
      // day falls back to the same anchor rule as the 周 button (#138).
      if (urlInitial.view) cal.viewMode.value = urlInitial.view;
      const initialDay = url.dayDate(urlInitial.date);
      if (initialDay) cal.currentDate.value = initialDay;
      else if (cal.viewMode.value === 'week') {
        const anchor = cal.weekAnchorDate();
        if (anchor) cal.currentDate.value = anchor;
      }
      // The day can only be resolved once the month's cells exist (after the
      // first load), and a history step can change the month under it — so it
      // lives here as pending state that `loadEarnings()` applies, not as a
      // one-shot call after mount.
      let pendingDateKey = urlInitial.date;
      // Issue #141: an iCal event's link also names the row. The day is applied
      // first (it owns the list); the symbol then opens that row's detail panel
      // — 「该日存在这个标的时直接打开它的详情面板」 — and a symbol that is not
      // on that day leaves the reader on the day's list.
      let pendingSymbolKey = urlInitial.symbol || '';
      // Issue #142: 「只看自选」 is in the link as well. It is *account* state, so
      // it is only a request here — `onMounted` keeps it once the account is
      // known (a guest must not be handed an empty calendar that pretends to be
      // 「yours」). Set before the first load so the filter changes the request,
      // not just the checkbox.
      watchlistOnly.value = urlInitial.mine === true;
      // Set while a `popstate` is being applied: the watchers below must not
      // write the address bar back while it is being read.
      let restoringFromHistory = false;

      // 4. Watchlist (needs loadEarnings callback)
      const wl = useWatchlist(apiFetch, showToast, () => loadEarnings());
      cal.watchlist = wl.watchlist; // wire watchlist into calendar

      // 4b. 「本周财报」 digest (Issue #133) — a view of the batch the calendar
      // just loaded, scoped to the account's own symbols when it has any (the
      // issue's 「无自选则列全部」). `covered` is the honesty bit: `earnings` is
      // the *displayed month* ±1, so once the calendar is paged away from today
      // an empty digest means 「未加载」, and it must say so instead of 「本周无财报」.
      const weekAheadOpen = ref(false);
      const weekAhead = computed(() => {
        const today = localTodayYmd();
        const horizonEnd = localDaysFromTodayYmd(WEEK_AHEAD_DAYS - 1);
        const range = calendarWindowRange(cal.currentDate.value);
        // Issue #142: the digest speaks for whatever the grid is showing. It
        // used to *infer* the scope from the watchlist length, so with the
        // filter on and no symbols it fell back to 「全部标的」 while the grid was
        // empty — one screen claiming two contradictory things.
        const mineOnly = watchlistOnly.value || wl.watchlist.value.length > 0;
        const covered = range.start <= today && range.end >= horizonEnd;
        const gridDates = new Set(cal.calendarCells.value.map(c => localYmd(c.date)));
        const groups = covered
          ? weekAheadGroups(earnings.value, today, horizonEnd, mineOnly ? wl.isMine : null, gridDates)
          : [];
        return {
          covered,
          mineOnly,
          total: groups.reduce((sum, group) => sum + group.total, 0),
          rangeLabel: monthDayLabel(today) + ' → ' + monthDayLabel(horizonEnd),
          groups,
        };
      });

      // A digest row opens the same detail panel a calendar badge does. When the
      // date is in the rendered grid the day context comes with it, so the
      // panel's 「返回」 lands on that day's list rather than the previous day's.
      function selectWeekEarning(e) {
        if (!e) return;
        const cell = cal.calendarCells.value.find(c => localYmd(c.date) === e.report_date) || null;
        cal.selectEarning(e, cell, apiFetch);
      }
      // The 「还有 N 场」 remainder: the day panel already renders the full day,
      // so selecting that cell is the whole feature. A date outside the rendered
      // grid has no cell — `weekAhead.groups[].inGrid` hides the control there.
      function selectWeekDay(date) {
        const cell = cal.calendarCells.value.find(c => localYmd(c.date) === date);
        if (cell) cal.selectCell(cell);
      }

      // 5. iCal (its default scope follows the watchlist — Issue #109)
      const ical = useIcal(user, wl.watchlist, wl.watchlistLoaded);

      // 6. Admin
      const admin = useAdmin(apiFetch, showToast);

      // 6b. System universe (read-only for every user)
      const su = useSystemUniverse(apiFetch);

      // 6c. One company's quarters (Issue #134)
      const hist = useSymbolHistory(apiFetch);
      // A link may name the symbol; `appTab` is already set here, so the
      // tab watcher will not fire for it — remember it before any load.
      if (urlInitial.tab === 'history' && urlInitial.symbol) {
        hist.historySymbol.value = urlInitial.symbol;
      }

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
      // The request window is the displayed month ±1. It lives in one helper
      // because the 「本周财报」 digest (#133) has to know whether the loaded
      // batch even covers the next seven days: an empty answer outside that
      // window is 「未加载」, not 「本周无财报」.
      function calendarWindowRange(d) {
        return {
          start: localYmd(new Date(d.getFullYear(), d.getMonth() - 1, 1)),
          end: localYmd(new Date(d.getFullYear(), d.getMonth() + 2, 0)),
        };
      }

      async function loadEarnings() {
        loading.value = true;
        error.value = null;
        try {
          const { start, end } = calendarWindowRange(cal.currentDate.value);
          const params = new URLSearchParams({ start, end, watchlistOnly: watchlistOnly.value });
          const data = await apiFetch(`/api/earnings?${params}`);
          if (data) {
            earnings.value = watchlistOnly.value
              ? data.filter(e => wl.watchlist.value.some(w => w.symbol === e.symbol && w.market === e.market))
              : data;
            if (appTab.value === 'calendar') {
              // Issue #138: a reload rebuilds the grid; it must not also throw the
              // selection away. Turning the anchor towards the selected day (which
              // a cross-month 周 entry does) triggers exactly this load, and the
              // old code parked `selectedDay` on 「今天」 no matter what the reader
              // had just opened. A day that is still in the new grid wins; a day
              // that is not falls back to today, as before.
              const keepKey = cal.selectedDay.value ? localYmd(cal.selectedDay.value.date) : '';
              cal.selectedDay.value = cal.calendarCells.value.find(cell => cell.isToday) || null;
              // A link (or a history step) names a day; it wins over 「today」.
              if (pendingDateKey) applyDateKey(pendingDateKey);
              else if (keepKey) applyDateKey(keepKey);
              // Issue #141: …and it may name the row. One shot only: a later
              // month load must not re-open a panel the reader has left. A
              // symbol that is not on that day is dropped from the address bar
              // as well — the list is showing, so the link must not keep naming
              // a row nobody opened (a history step is left alone: it *is* the
              // address bar).
              if (pendingSymbolKey) {
                if (!applySymbolKey(pendingSymbolKey) && !restoringFromHistory) {
                  url.replaceUrl(currentUrlState());
                }
                pendingSymbolKey = '';
              }
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
          view: cal.viewMode.value,
          month: url.monthKey(cal.currentDate.value),
          date: cal.selectedDay.value ? localYmd(cal.selectedDay.value.date) : '',
          // Issue #141: the address bar names the row the panel is showing, so a
          // copied link reopens the same detail and 后退 cannot show A under a
          // URL that says B. `''` when the day's list is showing instead.
          symbol: cal.selectedEarning.value
            ? (cal.selectedEarning.value.symbol || '')
            // Issue #134: the history tab's subject is the symbol it names.
            : (appTab.value === 'history' ? hist.historySymbol.value : ''),
          // Issue #142: the filter is part of what the page shows「我的自选、这个月」,
          // so the link carries it; `false` writes nothing (see `buildUrlState`).
          mine: watchlistOnly.value === true,
        };
      }
      // The month is what a calendar *loads* and what a link carries. Moving the
      // anchor inside that month — entering 周 parks the selected day there
      // (Issue #138) — changes neither, so it must not refetch the window nor
      // push a second, identical history entry.
      const anchorMonth = computed(() => url.monthKey(cal.currentDate.value));
      // A tab or month change is a step the reader may want to undo, so it pushes;
      // selecting a day is a click-through inside the same view, so it replaces —
      // otherwise every cell tap would need its own 后退 press. A `popstate`
      // already *is* the history change, so it must not write one back.
      watch([appTab, anchorMonth], () => {
        if (!restoringFromHistory) url.pushUrl(currentUrlState());
      });
      // 切「月 / 周」也是一次导航（Issue #140）：它改变的是视图本身，因此与页签、
      // 月份一样 push。跨月周（如 11/29 → 12/5）同时移动锚点，上面那个 watcher
      // 会一起触发；`pushUrl` 对「与地址栏相同」的 push 不再写一遍，所以一次点击
      // 只留下一个历史步。
      watch(() => cal.viewMode.value, () => {
        if (!restoringFromHistory) url.pushUrl(currentUrlState());
      });
      // Issue #142: 「只看自选」 replaces the whole grid batch, which is exactly as
      // much of a navigation step as a month change — so it pushes, and turning
      // it off removes the key (one more step back to the full market).
      watch(watchlistOnly, () => {
        if (!restoringFromHistory) url.pushUrl(currentUrlState());
      });
      watch(() => (cal.selectedDay.value ? localYmd(cal.selectedDay.value.date) : ''),
        () => { if (!restoringFromHistory) url.replaceUrl(currentUrlState()); });
      // Issue #141: opening or closing a detail panel is the same kind of
      // selection change, so it also replaces. Selecting a row from the day's
      // list writes its symbol; 返回 (which clears `selectedEarning`) drops the
      // parameter again — the address bar never says A while the panel shows B.
      watch(() => (cal.selectedEarning.value ? (cal.selectedEarning.value.symbol || '') : ''),
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

      // Open the row an iCal link named (Issue #141). The match is against the
      // selected day's own list, so a symbol that did not report that day is not
      // 「almost right」 — it falls back to the list instead of some other period.
      // Case-insensitive because the codes are; the feed emits them upper-case.
      function applySymbolKey(key) {
        const wanted = String(key || '').trim().toUpperCase();
        if (!wanted) return false;
        const cell = cal.selectedDay.value;
        if (!cell || !cell.earnings) return false;
        const earning = cell.earnings.find(e => String(e.symbol || '').toUpperCase() === wanted);
        if (!earning) return false;
        cal.selectEarning(earning, cell, apiFetch);
        return true;
      }

      // 后退/前进 only moves the address bar; the view has to follow it, or the
      // reader sees yesterday's month under today's URL. Everything the link
      // carries is re-applied, and the write-watchers stay quiet while it happens.
      async function applyHistoryState() {
        const state = url.parseUrlState(typeof window !== 'undefined' ? window.location.search : '');
        const month = url.monthDate(state.month);
        const day = url.dayDate(state.date);
        const view = state.view || 'month';
        restoringFromHistory = true;
        try {
          appTab.value = state.tab || 'calendar';
          // Issue #140: 后退/前进 also crosses 月 ↔ 周, and the week must be the
          // one the link names — the restored anchor is the linked day, otherwise
          // the grid and the panel split exactly as #138 fixed for the 周 button.
          cal.viewMode.value = view;
          if (month) cal.currentDate.value = month;
          if (day) cal.currentDate.value = day;
          else if (view === 'week') {
            const anchor = cal.weekAnchorDate();
            if (anchor) cal.currentDate.value = anchor;
          }
          pendingDateKey = state.date;
          // Issue #141: the row is re-opened by the same load that rebuilds the
          // day, and cleared afterwards so the next month does not re-select it.
          pendingSymbolKey = state.symbol || '';
          // Issue #142: the filter is part of the history step, and it has to be
          // in place *before* the load — the request window it changes is what
          // the restored view is made of.
          watchlistOnly.value = state.mine === true;
          cal.selectedEarning.value = null;
          cal.decision.value = null;
          // One load for the restored month, and the day applied after it: the
          // month watcher would otherwise race the restore and reset the day.
          await loadEarnings();
          pendingDateKey = '';
          pendingSymbolKey = '';
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
      watch(anchorMonth, () => { if (!restoringFromHistory) loadEarnings(); });

      // The watchlist data must not follow the calendar month; it is refreshed
      // when the page is (re)entered so a long calendar session cannot show a
      // stale "下次财报" (Issue #56).
      // Factored out because a tab named by the address bar (Issue #125) reaches
      // the page without passing through the watch: `appTab` is already set when
      // the watcher is registered, so the entry load has to be callable directly.
      function loadTabData(tab) {
        // Issue #132: entering 自选 is what triggers its forward window; the
        // calendar first paint no longer pays for it. The list itself is read
        // here too so a deep link straight into the tab still fills the tables
        // (the mount only loads it when a user is present, which is the same
        // condition, but this keeps the tab self-sufficient).
        if (tab === 'watchlist' && user.value) {
          if (!wl.watchlistLoaded.value) wl.loadWatchlist();
          wl.loadNextEarnings();
        }
        // The universe is the same range for everyone and changes only when an
        // admin edits the source, so it is read on entry instead of being kept
        // in step with the calendar month.
        if (tab === 'universe' && user.value) su.loadUniverse();
        // The history read has its own window, so it is loaded on entry (and
        // only when a symbol is named — otherwise the page shows its picker).
        if (tab === 'history' && user.value && hist.historySymbol.value) {
          hist.loadSymbolHistory(hist.historySymbol.value);
        }
      }
      watch(appTab, loadTabData);
      // Choosing a company replaces (not pushes) the address bar, exactly like
      // selecting a day: it is a step inside this view, not a new one.
      watch(hist.historySymbol, () => {
        if (!restoringFromHistory) url.replaceUrl(currentUrlState());
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
        // Issue #142: `mine=1` names an account's own symbols. Without an
        // account the link falls back to the public calendar, and the key is
        // dropped from the address bar while month / day / symbol stay — a guest
        // must not land on an empty grid that looks like filtered data.
        if (!user.value && watchlistOnly.value) {
          // The ref's own watcher would push the address bar on the next tick, so
          // the guard is held across that flush — otherwise the fallback write
          // below is immediately overwritten by a step that has no `date` yet.
          restoringFromHistory = true;
          watchlistOnly.value = false;
          await nextTick();
          restoringFromHistory = false;
          // `currentUrlState()` cannot see the pending link parts yet (no cells
          // exist), so they are carried over explicitly.
          const publicState = currentUrlState();
          publicState.date = pendingDateKey;
          publicState.symbol = pendingSymbolKey;
          url.replaceUrl(publicState);
        }
        if (user.value) {
          // Issue #132: the calendar first paint used to fetch the 420-day
          // forward window (`/api/earnings?watchlistOnly=true`) that only the
          // 自选 tab renders. Most visits never open that tab, so the request is
          // now issued on entry (`loadTabData`). The symbol list itself stays on
          // first paint: `isMine()` needs it for the calendar's ☆ markers, and
          // it is two orders of magnitude smaller than the window.
          await wl.loadWatchlist();
        }
        await loadEarnings();
        loadTabData(appTab.value);
      });

      // ── Public API ───────────────────────────────────────────
      return {
        // State
        user, earnings, appTab, watchlistOnly, loading, error, writeError, toast, appConfig,
        // Calendar
        viewMode: cal.viewMode, setViewMode: cal.setViewMode, currentDate: cal.currentDate,
        selectedEarning: cal.selectedEarning, decision: cal.decision, selectedDay: cal.selectedDay,
        weekdays: cal.weekdays, monthLabel: cal.monthLabel, calendarRangeLabel: cal.calendarRangeLabel,
        calendarCells: cal.calendarCells,
        cellAriaLabel: cal.cellAriaLabel,
        icalDialog, adminDialog, trapTab,
        prevMonth: cal.prevMonth, nextMonth: cal.nextMonth, goToday: cal.goToday,
        selectCell: cal.selectCell, clearSelection: cal.clearSelection,
        isSelectedDay: cal.isSelectedDay,
        calendarWeeks: cal.calendarWeeks, cellTabIndex: cal.cellTabIndex,
        // 「本周财报」 digest (Issue #133)
        weekAhead, weekAheadOpen, selectWeekEarning, selectWeekDay,
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
        // Symbol history (Issue #134)
        historySymbol: hist.historySymbol, historyTitle: hist.historyTitle,
        historyMarket: hist.historyMarket, historyPeriods: hist.historyPeriods,
        historyLoading: hist.historyLoading, historyQuery: hist.historyQuery,
        historyResults: hist.historyResults, historySearchLoading: hist.historySearchLoading,
        openSymbol: hist.openSymbol, doHistorySearch: hist.doHistorySearch,
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
