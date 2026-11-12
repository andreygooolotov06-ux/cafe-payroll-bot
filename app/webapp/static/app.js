(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    try {
      tg.ready();
      tg.expand();
      if (typeof tg.setHeaderColor === "function") tg.setHeaderColor("#FFFCF6");
      if (typeof tg.setBackgroundColor === "function") tg.setBackgroundColor("#FFFCF6");
    } catch (_) {}
  }

  const UK_MONTHS = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"
  ];
  const UK_WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"];

  const SWR_TTL_MS = 24 * 60 * 60 * 1000;
  const TAB_FRESH_MS = 20 * 1000;

  const state = {
    me: null,
    homeScreenStatus: "unknown",
    bannerDismissed: false,
    workspace: "auto", // "admin" | "personal"
    tab: "",
    tabHistory: [],
    _cacheStamp: {},
    _userNavigated: false,
    // Employee state
    empPeriod: "month",
    calYear: null,
    calMonth: null,
    myDash: null,
    shiftForm: {
      dateMode: "today",
      customDate: "",
      startTime: "10:00",
      endTime: "22:00",
      isBanquet: false,
      banquetGuests: 12,
      editingShiftId: null,
      idemKey: makeIdemKey(),
    },
    salarySubtab: "shifts",
    // Owner state
    ownerPeriod: "month",
    ownerCustomFrom: "",
    ownerCustomTo: "",
    analytics: null,
    chartHoverIdx: null,
    staffList: null,
    staffSearch: "",
    staffFilter: "all",
    payouts: null,
    adminPerms: null,
  };

  function makeIdemKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "id-" + Date.now() + "-" + Math.random().toString(36).slice(2, 10);
  }

  function haptic(type = "light") {
    try {
      if (!tg || !tg.HapticFeedback) return;
      if (type === "success" || type === "error" || type === "warning") {
        tg.HapticFeedback.notificationOccurred(type);
      } else {
        tg.HapticFeedback.impactOccurred(type);
      }
    } catch (_) {}
  }

  function esc(val) {
    return String(val ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function roundKop(v) {
    return Math.round((Number(v) || 0) * 100) / 100;
  }

  function fmtMoney(val) {
    const r = roundKop(val);
    const fixed = Math.abs(r) < 0.005 ? 0 : r;
    const isInt = Math.abs(fixed - Math.round(fixed)) < 1e-9;
    const formatted = isInt
      ? Math.round(fixed).toLocaleString("uk-UA")
      : fixed.toLocaleString("uk-UA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return `${formatted} грн`;
  }

  function fmtNum(val) {
    const r = roundKop(val);
    if (Math.abs(r - Math.round(r)) < 1e-9) return String(Math.round(r));
    return r.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
  }

  function fmtHours(val) {
    return `${fmtNum(val)} год`;
  }

  function fmtPct(val) {
    return `${fmtNum(val)}%`;
  }

  function calcDuration(start, end) {
    if (!/^\d{2}:\d{2}$/.test(start) || !/^\d{2}:\d{2}$/.test(end)) return null;
    const [sh, sm] = start.split(":").map(Number);
    const [eh, em] = end.split(":").map(Number);
    if (sh > 23 || sm > 59 || eh > 23 || em > 59) return null;
    let sMin = sh * 60 + sm;
    let eMin = eh * 60 + em;
    if (sMin === eMin) return null;
    if (eMin < sMin) eMin += 1440;
    const diff = eMin - sMin;
    if (diff > 1440) return null;
    return Math.round((diff / 60) * 100) / 100;
  }

  function addDaysIso(iso, delta) {
    const [y, m, d] = iso.split("-").map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d + delta));
    return dt.toISOString().slice(0, 10);
  }

  function formatShortDate(iso) {
    const [y, m, d] = iso.split("-");
    return `${d}.${m}.${y}`;
  }

  function showToast(text, isErr = false) {
    const host = document.getElementById("toast-host");
    const el = document.createElement("div");
    el.className = "toast" + (isErr ? " err" : "");
    el.textContent = text;
    host.appendChild(el);
    setTimeout(() => {
      el.remove();
    }, 3400);
  }

  async function copyToClipboard(text, successMsg = "Скопійовано! 📋") {
    const val = String(text || "");
    if (!val) return false;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(val);
        haptic("success");
        showToast(successMsg);
        return true;
      }
    } catch (_) {}
    try {
      const ta = document.createElement("textarea");
      ta.value = val;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      if (ok) {
        haptic("success");
        showToast(successMsg);
        return true;
      }
    } catch (_) {}
    showToast("Скопіюй вручну: " + val, true);
    return false;
  }

  function formatCardInputLive(raw) {
    const digits = String(raw || "").replace(/\D+/g, "").slice(0, 19);
    return digits.replace(/(.{4})/g, "$1 ").trim();
  }

  function getOpenPayoutBadgeCount() {
    if (state.payouts && Array.isArray(state.payouts.payout_requests)) {
      return state.payouts.payout_requests.filter(
        (r) => r.status === "pending" || r.status === "approved"
      ).length;
    }
    if (state.analytics && state.analytics.kpis) {
      return Number(state.analytics.kpis.pending_payout_requests || 0);
    }
    return Number((state.me && state.me.pending_payout_requests) || 0);
  }

  function getInitData() {
    if (tg && tg.initData) return tg.initData;
    const qs = new URLSearchParams(window.location.search);
    if (qs.get("tgWebAppData")) return qs.get("tgWebAppData");
    if (window.location.hash && window.location.hash.length > 1) {
      const hs = new URLSearchParams(window.location.hash.slice(1));
      if (hs.get("tgWebAppData")) return hs.get("tgWebAppData");
    }
    return "";
  }

  function getSwrStorageKey() {
    const uid = tg && tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user.id : null;
    return uid ? `cafe_cab_swr_v1_${uid}` : "cafe_cab_swr_v1_anon";
  }

  function readSwrCache() {
    try {
      if (!window.localStorage) return null;
      const uid = tg && tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user.id : null;
      const raw = window.localStorage.getItem(getSwrStorageKey());
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || !parsed.ts || !parsed.me) return null;
      if (Date.now() - Number(parsed.ts) > SWR_TTL_MS) return null;
      if (uid && parsed.me.telegram_id && Number(parsed.me.telegram_id) !== Number(uid)) return null;
      return parsed;
    } catch (_) {
      return null;
    }
  }

  function writeSwrCache() {
    try {
      if (!window.localStorage || !state.me) return;
      window.localStorage.setItem(
        getSwrStorageKey(),
        JSON.stringify({
          ts: Date.now(),
          workspace: state.workspace,
          tab: state.tab,
          me: state.me,
          analytics: state.analytics,
          staffList: state.staffList,
          payouts: state.payouts,
          adminPerms: state.adminPerms,
          myDash: state.myDash,
        })
      );
    } catch (_) {}
  }

  function brandEmblemSvg(size = 28) {
    return `<svg viewBox="0 0 512 512" width="${size}" height="${size}" aria-hidden="true" style="display:block;border-radius:22%"><defs><linearGradient id="cbBg" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#FFFCF6"/><stop offset="100%" stop-color="#F7EAD0"/></linearGradient><linearGradient id="cbGold" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#E7C96B"/><stop offset="52%" stop-color="#C89522"/><stop offset="100%" stop-color="#9E700B"/></linearGradient></defs><rect width="512" height="512" rx="116" fill="url(#cbBg)"/><rect x="20" y="20" width="472" height="472" rx="102" fill="none" stroke="url(#cbGold)" stroke-width="6" opacity="0.5"/><circle cx="256" cy="242" r="126" fill="none" stroke="url(#cbGold)" stroke-width="14"/><path d="M180 226h136v26c0 40-30 70-68 70s-68-30-68-70v-26z" fill="url(#cbGold)"/><path d="M316 238h20c16 0 28 12 28 26s-12 26-28 26h-22" fill="none" stroke="url(#cbGold)" stroke-width="14" stroke-linecap="round"/><path d="M168 338h168" stroke="url(#cbGold)" stroke-width="14" stroke-linecap="round"/><path d="M222 194c-6-16 10-24 4-40M256 190c-6-16 10-24 4-40M290 194c-6-16 10-24 4-40" fill="none" stroke="url(#cbGold)" stroke-width="10" stroke-linecap="round"/></svg>`;
  }

  async function api(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    headers["X-Telegram-Init-Data"] = getInitData();
    const startParam = tg && tg.initDataUnsafe ? tg.initDataUnsafe.start_param : "";
    if (startParam) headers["X-Start-Param"] = startParam;
    if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    const resp = await fetch(path, Object.assign({}, options, { headers }));
    const contentType = resp.headers.get("content-type") || "";
    const data = contentType.includes("application/json") ? await resp.json() : await resp.text();
    if (!resp.ok) {
      const msg = (data && data.error) ? data.error : `Помилка ${resp.status}`;
      const err = new Error(msg);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  // Bottom Sheet Modal
  const sheetBackdrop = document.getElementById("sheet-backdrop");
  const sheetTitle = document.getElementById("sheet-title");
  const sheetBody = document.getElementById("sheet-body");
  document.getElementById("sheet-close").addEventListener("click", closeSheet);
  sheetBackdrop.addEventListener("click", (e) => {
    if (e.target === sheetBackdrop) closeSheet();
  });

  function openSheet(title, htmlContent, onMount) {
    sheetTitle.textContent = title;
    sheetBody.innerHTML = htmlContent;
    sheetBackdrop.classList.remove("hidden");
    syncBackButton();
    if (typeof onMount === "function") onMount(sheetBody);
  }

  function closeSheet() {
    sheetBackdrop.classList.add("hidden");
    sheetBody.innerHTML = "";
    syncBackButton();
  }

  function syncBackButton() {
    if (!tg || !tg.BackButton) return;
    try {
      const sheetOpen = !sheetBackdrop.classList.contains("hidden");
      const hasHistory = state.tabHistory.length > 0;
      const adminInPersonal =
        state.me &&
        state.me.role === "admin" &&
        state.workspace === "personal" &&
        getAdminManagementTabs().length > 0;
      if (sheetOpen || hasHistory || adminInPersonal) {
        tg.BackButton.show();
      } else {
        tg.BackButton.hide();
      }
    } catch (_) {}
  }

  async function handleBackButton() {
    haptic("light");
    if (!sheetBackdrop.classList.contains("hidden")) {
      closeSheet();
      return;
    }
    if (state.tabHistory.length > 0) {
      const prev = state.tabHistory.pop();
      await navigateTo(prev.workspace, prev.tab, false);
      return;
    }
    if (state.me && state.me.role === "admin" && state.workspace === "personal") {
      const admTabs = getAdminManagementTabs();
      if (admTabs.length > 0) {
        await navigateTo("admin", admTabs[0].id, false);
      }
    }
  }

  if (tg && tg.BackButton && typeof tg.BackButton.onClick === "function") {
    try {
      tg.BackButton.onClick(handleBackButton);
    } catch (_) {}
  }

  // Home Screen integration
  function checkHomeScreen() {
    if (!tg || typeof tg.checkHomeScreenStatus !== "function") return;
    try {
      tg.checkHomeScreenStatus((status) => {
        state.homeScreenStatus = status || "unknown";
        renderApp();
      });
    } catch (_) {}
  }

  function triggerAddToHomeScreen() {
    haptic("light");
    if (tg && typeof tg.addToHomeScreen === "function" && state.homeScreenStatus !== "unsupported") {
      try {
        tg.addToHomeScreen();
        return;
      } catch (_) {}
    }
    openSheet(
      "📲 Винести на головний екран",
      `
      <div class="card card-gold" style="margin-bottom:12px">
        <div style="font-weight:800;margin-bottom:6px">Як закріпити Кафе Кабінет як додаток:</div>
        <div class="text-muted" style="line-height:1.55">
          <b>Android / iPhone у Telegram:</b> натисни <b>⋮</b> (або <b>⋯</b> у правому верхньому куті вікна Mini App) → <b>«Додати на екран»</b>.<br><br>
          <b>iPhone Safari:</b> натисни «Поділитися» ⬆ → <b>«На початковий екран»</b>.<br><br>
          <b>Android Chrome:</b> меню <b>⋮</b> → <b>«Додати на головний екран»</b>.
        </div>
      </div>
      <button class="btn btn-gold btn-full" id="close-guide-btn" type="button">Зрозуміло ✨</button>
      `,
      (root) => {
        root.querySelector("#close-guide-btn").addEventListener("click", closeSheet);
      }
    );
  }

  if (tg && typeof tg.onEvent === "function") {
    try {
      tg.onEvent("homeScreenAdded", () => {
        state.homeScreenStatus = "added";
        haptic("success");
        showToast("Іконку додано на головний екран! ✨");
        renderApp();
      });
    } catch (_) {}
  }

  // Available tabs based on role & permissions
  function getAdminManagementTabs() {
    if (!state.me) return [];
    const p = state.me.permissions || {};
    const tabs = [];
    if (p.can_view_salaries_reports) tabs.push({ id: "pulse", label: "Пульс", icon: "chart" });
    if (p.can_open_staff_tab) tabs.push({ id: "staff", label: "Персонал", icon: "users" });
    if (p.can_payout) tabs.push({ id: "payouts", label: "Виплати", icon: "wallet" });
    if (p.can_configure_admin) tabs.push({ id: "perms", label: "Доступи", icon: "shield" });
    return tabs;
  }

  function getPersonalTabs() {
    return [
      { id: "shift", label: "Зміна", icon: "clock" },
      { id: "calendar", label: "Календар", icon: "cal" },
      { id: "salary", label: "Зарплата", icon: "coin" },
    ];
  }

  function getActiveTabs() {
    if (!state.me) return [];
    const role = state.me.role;
    const adminTabs = getAdminManagementTabs();
    if (role === "owner") return adminTabs;
    if (role === "admin") {
      if (state.workspace === "admin" && adminTabs.length > 0) return adminTabs;
      return getPersonalTabs();
    }
    return getPersonalTabs();
  }

  function navIconSvg(kind) {
    switch (kind) {
      case "chart":
        return `<svg viewBox="0 0 24 24"><path d="M4 19h16M7 16v-5M12 16V7M17 16v-3"/></svg>`;
      case "cash":
        return `<svg viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/></svg>`;
      case "users":
        return `<svg viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M3 19c0-3 2.7-5 6-5s6 2 6 5"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14.2c2.8.4 5 2.1 5 4.8"/></svg>`;
      case "wallet":
        return `<svg viewBox="0 0 24 24"><path d="M4 7h15a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z"/><path d="M16 12h5"/></svg>`;
      case "shield":
        return `<svg viewBox="0 0 24 24"><path d="M12 3l7 3v5c0 5-3.2 8.7-7 10-3.8-1.3-7-5-7-10V6l7-3z"/><path d="M9.5 12.2l1.8 1.8 3.4-3.6"/></svg>`;
      case "clock":
        return `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></svg>`;
      case "cal":
        return `<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="16" rx="2.5"/><path d="M8 3v4M16 3v4M3 10h18"/></svg>`;
      case "coin":
      default:
        return `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v9M14.5 9.8c0-1-1.1-1.6-2.5-1.6s-2.5.6-2.5 1.6 1 1.5 2.5 1.8 2.5.8 2.5 1.9-1.1 1.7-2.5 1.7-2.5-.6-2.5-1.7"/></svg>`;
    }
  }

  function initDatesFromMe(me) {
    if (!me || !me.today) return;
    const [ty, tm] = me.today.split("-").map(Number);
    if (!state.calYear) state.calYear = ty;
    if (!state.calMonth) state.calMonth = tm;
    if (!state.shiftForm.customDate) state.shiftForm.customDate = me.today;
    if (!state.ownerCustomFrom) state.ownerCustomFrom = me.today.slice(0, 8) + "01";
    if (!state.ownerCustomTo) state.ownerCustomTo = me.today;
  }

  function applyMyDashState(dash) {
    state.myDash = dash;
    if (dash && dash.patterns && dash.patterns.last_time && !state.shiftForm._touchedTime) {
      state.shiftForm.startTime = dash.patterns.last_time.start_time;
      state.shiftForm.endTime = dash.patterns.last_time.end_time;
    }
    syncSelectedDateShiftState();
  }

  function getTabCacheGroup(tabId) {
    if (tabId === "shift" || tabId === "calendar" || tabId === "salary") return "myDash";
    return tabId;
  }

  function getTabCacheKey(tabId) {
    const grp = getTabCacheGroup(tabId);
    if (grp === "myDash") return `${state.empPeriod}|${state.calYear}|${state.calMonth}`;
    if (grp === "pulse") {
      return state.ownerPeriod === "custom"
        ? `custom|${state.ownerCustomFrom}|${state.ownerCustomTo}`
        : state.ownerPeriod;
    }
    if (grp === "staff") return state.ownerPeriod;
    return "default";
  }

  function hasTabPayload(tabId) {
    const grp = getTabCacheGroup(tabId);
    if (grp === "myDash") return !!state.myDash;
    if (grp === "pulse") return !!state.analytics;
    if (grp === "staff") return !!state.staffList;
    if (grp === "payouts") return !!state.payouts;
    if (grp === "perms") return !!state.adminPerms;
    return false;
  }

  function isTabCacheFresh(tabId) {
    if (!hasTabPayload(tabId)) return false;
    const grp = getTabCacheGroup(tabId);
    const entry = state._cacheStamp[grp];
    if (!entry) return false;
    if (entry.key !== getTabCacheKey(tabId)) return false;
    return Date.now() - entry.ts < TAB_FRESH_MS;
  }

  function markTabFresh(tabId) {
    const grp = getTabCacheGroup(tabId);
    state._cacheStamp[grp] = {
      key: getTabCacheKey(tabId),
      ts: Date.now(),
    };
  }

  function invalidateCache(...groups) {
    if (!groups.length) {
      state._cacheStamp = {};
      return;
    }
    groups.forEach((g) => {
      delete state._cacheStamp[g];
    });
  }

  function applyBootstrapPayload(bootResp) {
    if (!bootResp || !bootResp.me) return;
    state.me = bootResp.me;
    initDatesFromMe(bootResp.me);

    const allowedTabs = getActiveTabs();
    if (!state._userNavigated || !allowedTabs.some((t) => t.id === state.tab)) {
      state.workspace = bootResp.workspace;
      state.tab = bootResp.initial_tab;
    }

    if (bootResp.analytics) {
      state.analytics = bootResp.analytics;
      if (bootResp.analytics.kpis) {
        state.me.pending_payout_requests = bootResp.analytics.kpis.pending_payout_requests || 0;
      }
      markTabFresh("pulse");
    }
    if (bootResp.staff_list) {
      state.staffList = bootResp.staff_list;
      markTabFresh("staff");
    }
    if (bootResp.payouts) {
      state.payouts = bootResp.payouts;
      if (Array.isArray(bootResp.payouts.payout_requests)) {
        state.me.pending_payout_requests = bootResp.payouts.payout_requests.filter(
          (r) => r.status === "pending"
        ).length;
      }
      markTabFresh("payouts");
    }
    if (bootResp.admin_perms) {
      state.adminPerms = bootResp.admin_perms;
      markTabFresh("perms");
    }
    if (bootResp.my_dash) {
      applyMyDashState(bootResp.my_dash);
      markTabFresh("shift");
    }
    writeSwrCache();
  }

  async function boot() {
    const appEl = document.getElementById("app");
    let hydratedFromCache = false;

    const cached = readSwrCache();
    if (cached && cached.me) {
      state.me = cached.me;
      initDatesFromMe(cached.me);
      state.analytics = cached.analytics || null;
      state.staffList = cached.staffList || null;
      state.payouts = cached.payouts || null;
      state.adminPerms = cached.adminPerms || null;
      if (cached.myDash) applyMyDashState(cached.myDash);

      const admTabs = getAdminManagementTabs();
      if (cached.me.role === "owner") {
        state.workspace = "admin";
        state.tab = "pulse";
      } else if (cached.me.role === "admin") {
        state.workspace = admTabs.length > 0 ? "admin" : "personal";
        state.tab = admTabs.length > 0 ? admTabs[0].id : "shift";
      } else {
        state.workspace = "personal";
        state.tab = "shift";
      }
      hydratedFromCache = true;
      syncBackButton();
      renderApp();
    }

    checkHomeScreen();

    const nowDt = new Date();
    const initY = state.calYear || nowDt.getFullYear();
    const initM = state.calMonth || (nowDt.getMonth() + 1);
    try {
      const bootResp = await api(
        `/api/bootstrap?preset=month&year=${initY}&month=${initM}`
      );
      applyBootstrapPayload(bootResp);
      syncBackButton();
      renderApp();
    } catch (err) {
      if (hydratedFromCache && err && err.status !== 401 && err.status !== 403) {
        showToast(err.message, true);
        return;
      }
      const isNoInit = !getInitData();
      appEl.innerHTML = `
        <div class="boot-screen">
          <div class="boot-emblem">${brandEmblemSvg(64)}</div>
          <div class="boot-title">${isNoInit ? "Відкрий через Telegram" : "Доступ обмежено"}</div>
          <div class="card" style="max-width:380px;text-align:center">
            <div style="font-weight:700;margin-bottom:6px">${esc(err.message)}</div>
            <div class="text-muted">
              ${isNoInit
                ? "Щоб увійти безпечно за твоїм Telegram ID, введи команду <b>/app</b> у боті та натисни кнопку <b>«✨ Відкрити Mini App»</b>."
                : "Якщо ти новий співробітник, попроси в Ані персональне посилання-запрошення та натисни Start у боті."}
            </div>
            <button class="btn btn-gold btn-full mt-12" type="button" onclick="location.reload()">Оновити</button>
          </div>
        </div>
      `;
    }
  }

  async function loadCurrentTabData(force = false) {
    if (!state.me) return false;
    const t = state.tab;
    if (!force && isTabCacheFresh(t)) return false;

    if (t === "shift" || t === "calendar" || t === "salary") {
      const dash = await api(
        `/api/my/dashboard?preset=${encodeURIComponent(state.empPeriod)}&year=${state.calYear}&month=${state.calMonth}`
      );
      applyMyDashState(dash);
    } else if (t === "pulse") {
      const q = state.ownerPeriod === "custom"
        ? `from=${encodeURIComponent(state.ownerCustomFrom)}&to=${encodeURIComponent(state.ownerCustomTo)}`
        : `preset=${encodeURIComponent(state.ownerPeriod)}`;
      state.analytics = await api(`/api/owner/analytics?${q}`);
      if (state.me && state.analytics && state.analytics.kpis) {
        state.me.pending_payout_requests = state.analytics.kpis.pending_payout_requests || 0;
      }
    } else if (t === "staff") {
      const q = `preset=${encodeURIComponent(state.ownerPeriod)}`;
      state.staffList = await api(`/api/owner/staff?${q}`);
    } else if (t === "payouts") {
      state.payouts = await api("/api/owner/payouts");
      if (state.me && state.payouts && Array.isArray(state.payouts.payout_requests)) {
        state.me.pending_payout_requests = state.payouts.payout_requests.filter((r) => r.status === "pending").length;
      }
    } else if (t === "perms") {
      state.adminPerms = await api("/api/owner/admin-perms");
    }
    markTabFresh(t);
    writeSwrCache();
    return true;
  }

  function getSelectedShiftDateIso() {
    if (!state.me) return "";
    if (state.shiftForm.dateMode === "today") return state.me.today;
    if (state.shiftForm.dateMode === "yesterday") return addDaysIso(state.me.today, -1);
    return state.shiftForm.customDate || state.me.today;
  }

  function syncSelectedDateShiftState() {
    if (!state.myDash) return;
    const targetIso = getSelectedShiftDateIso();
    const calMatch = state.myDash.calendar && state.myDash.calendar.days
      ? state.myDash.calendar.days.find((d) => d.date === targetIso && d.has_shift)
      : null;
    const sumMatch = state.myDash.summary && state.myDash.summary.shifts
      ? state.myDash.summary.shifts.find((s) => s.work_date === targetIso)
      : null;
    const match = calMatch || sumMatch;
    if (match) {
      state.shiftForm.editingShiftId = calMatch ? calMatch.shift_id : sumMatch.id;
      if (!state.shiftForm._touchedTime) {
        state.shiftForm.startTime = match.start_time;
        state.shiftForm.endTime = match.end_time;
      }
      state.shiftForm.isBanquet = !!match.is_banquet;
      state.shiftForm.banquetGuests = match.banquet_guests || 12;
    } else {
      state.shiftForm.editingShiftId = null;
    }
  }

  async function navigateTo(targetWorkspace, tabId, pushHistory = true) {
    haptic("light");
    state._userNavigated = true;
    const prevWs = state.workspace;
    const prevTab = state.tab;
    if (pushHistory && prevTab && (prevWs !== targetWorkspace || prevTab !== tabId)) {
      const top = state.tabHistory[state.tabHistory.length - 1];
      if (!top || top.workspace !== prevWs || top.tab !== prevTab) {
        state.tabHistory.push({ workspace: prevWs, tab: prevTab });
        if (state.tabHistory.length > 20) state.tabHistory.shift();
      }
    }
    state.workspace = targetWorkspace;
    state.tab = tabId;
    syncBackButton();
    renderApp();
    try {
      const fetched = await loadCurrentTabData(false);
      if (fetched) renderApp();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function switchTab(tabId) {
    await navigateTo(state.workspace, tabId, true);
  }

  async function refreshCurrent() {
    haptic("light");
    try {
      invalidateCache();
      const isPersonalTab = state.tab === "shift" || state.tab === "calendar" || state.tab === "salary";
      let q = `tab=${encodeURIComponent(state.tab)}&year=${state.calYear}&month=${state.calMonth}`;
      if (!isPersonalTab && state.ownerPeriod === "custom") {
        q += `&from=${encodeURIComponent(state.ownerCustomFrom)}&to=${encodeURIComponent(state.ownerCustomTo)}`;
      } else {
        q += `&preset=${encodeURIComponent(isPersonalTab ? state.empPeriod : state.ownerPeriod)}`;
      }
      const bootResp = await api(`/api/bootstrap?${q}`);
      applyBootstrapPayload(bootResp);
      syncBackButton();
      renderApp();
      showToast("Дані оновлено ✨");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function renderApp() {
    if (!state.me) return;
    const appEl = document.getElementById("app");
    const me = state.me;
    const tabs = getActiveTabs();
    if (!tabs.some((t) => t.id === state.tab) && tabs.length > 0) {
      state.tab = tabs[0].id;
    }

    const roleDisplay =
      me.role === "owner"
        ? "👑 Власниця кафе"
        : me.role === "admin"
        ? `👑 Адмін • ${me.employee ? me.employee.role_name : ""}`
        : `🌸 ${me.employee ? me.employee.role_name : "Працівник"}`;

    const displayName = me.role === "owner" ? "Аня" : me.employee ? me.employee.full_name : "Кабінет";

    const showPinBanner =
      !state.bannerDismissed &&
      state.homeScreenStatus !== "added" &&
      tg &&
      typeof tg.addToHomeScreen === "function";

    const adminTabs = getAdminManagementTabs();
    const showAdminSwitch = me.role === "admin" && adminTabs.length > 0;

    let bodyHtml = "";
    if (state.tab === "shift") bodyHtml = renderEmployeeShiftTab();
    else if (state.tab === "calendar") bodyHtml = renderEmployeeCalendarTab();
    else if (state.tab === "salary") bodyHtml = renderEmployeeSalaryTab();
    else if (state.tab === "pulse") bodyHtml = renderOwnerPulseTab();
    else if (state.tab === "staff") bodyHtml = renderOwnerStaffTab();
    else if (state.tab === "payouts") bodyHtml = renderOwnerPayoutsTab();
    else if (state.tab === "perms") bodyHtml = renderOwnerPermsTab();

    const payBadgeCnt = getOpenPayoutBadgeCount();

    appEl.innerHTML = `
      <header class="top-header">
        <div class="brand-wrap">
          <div class="brand-avatar">
            ${brandEmblemSvg(28)}
          </div>
          <div class="brand-meta">
            <div class="brand-name">${esc(displayName)}</div>
            <div class="brand-role">${esc(roleDisplay)}</div>
          </div>
        </div>
        <div class="header-actions">
          <button class="pin-pill" id="btn-pin-home" type="button" title="Перенести на екран">📲 На екран</button>
          <button class="icon-btn" id="btn-refresh" type="button" title="Оновити">↻</button>
        </div>
      </header>

      ${showPinBanner ? `
        <div class="install-banner">
          <div class="install-banner-text">
            <strong>Перенести на екран телефону?</strong>
            <span>Ярлик поруч з іншими додатками — вхід в 1 дотик ✨</span>
          </div>
          <div class="install-banner-btns">
            <button class="btn btn-gold btn-sm" id="banner-add-home" type="button">Додати</button>
            <button class="icon-btn" id="banner-dismiss" type="button" style="width:30px;height:30px;font-size:12px">✕</button>
          </div>
        </div>
      ` : ""}

      ${showAdminSwitch ? `
        <div class="mode-switch">
          <button class="mode-btn ${state.workspace === "admin" ? "active" : ""}" id="ws-admin" type="button">👑 Адмін-панель</button>
          <button class="mode-btn ${state.workspace === "personal" ? "active" : ""}" id="ws-personal" type="button">🌸 Мої зміни</button>
        </div>
      ` : ""}

      <main id="tab-content">${bodyHtml}</main>

      <nav class="bottom-nav">
        ${tabs.map((t) => `
          <button class="nav-item ${state.tab === t.id ? "active" : ""}" data-tab="${t.id}" type="button">
            ${navIconSvg(t.icon)}
            <span>${esc(t.label)}</span>
            ${t.id === "payouts" && payBadgeCnt > 0 ? `<span class="nav-badge">${payBadgeCnt}</span>` : ""}
          </button>
        `).join("")}
      </nav>
    `;

    bindGlobalEvents(appEl);
    bindTabEvents(appEl);
  }

  function bindGlobalEvents(root) {
    const refreshBtn = root.querySelector("#btn-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", refreshCurrent);

    const pinBtn = root.querySelector("#btn-pin-home");
    if (pinBtn) pinBtn.addEventListener("click", triggerAddToHomeScreen);

    const bannerAdd = root.querySelector("#banner-add-home");
    if (bannerAdd) bannerAdd.addEventListener("click", triggerAddToHomeScreen);

    const bannerDismiss = root.querySelector("#banner-dismiss");
    if (bannerDismiss) {
      bannerDismiss.addEventListener("click", () => {
        state.bannerDismissed = true;
        renderApp();
      });
    }

    const wsAdmin = root.querySelector("#ws-admin");
    if (wsAdmin) {
      wsAdmin.addEventListener("click", async () => {
        const admTabs = getAdminManagementTabs();
        await navigateTo("admin", admTabs[0] ? admTabs[0].id : "shift", true);
      });
    }
    const wsPers = root.querySelector("#ws-personal");
    if (wsPers) {
      wsPers.addEventListener("click", async () => {
        await navigateTo("personal", "shift", true);
      });
    }

    root.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });
  }

  // ==================== 1. EMPLOYEE: SHIFT ENTRY ====================

  function renderEmployeeShiftTab() {
    const dash = state.myDash;
    const emp = state.me.employee;
    const f = state.shiftForm;
    const targetIso = getSelectedShiftDateIso();
    const hours = calcDuration(f.startTime, f.endTime);
    const estBase = hours && emp ? roundKop(hours * emp.hourly_rate) : 0;
    const estBanquet =
      f.isBanquet && emp && emp.banquet_rate_per_person
        ? roundKop(Number(f.banquetGuests || 0) * emp.banquet_rate_per_person)
        : 0;
    const patterns = (dash && dash.patterns && dash.patterns.suggested_times) || [];

    const existingShift =
      dash && dash.calendar && dash.calendar.days
        ? dash.calendar.days.find((d) => d.date === targetIso && d.has_shift)
        : null;

    return `
      <div class="card card-gold">
        <div class="card-title-row">
          <div>
            <div class="hero-label">Твій поточний баланс</div>
            <div class="hero-amount">${dash ? fmtMoney(dash.summary.balance_due) : "—"}</div>
          </div>
          <span class="badge badge-gold">Ставка ${emp ? fmtMoney(emp.hourly_rate) : "0"}/год</span>
        </div>
        <div class="card-sub">
          ${dash ? `Цього місяця: <b>${dash.calendar.month_shifts} зм.</b> (${fmtHours(dash.calendar.month_hours)}) • Нараховано <b>${fmtMoney(dash.calendar.month_earned)}</b>` : ""}
        </div>
      </div>

      <div class="card">
        <div class="card-title-row">
          <h2 class="card-title">${existingShift ? "✏️ Редагування зміни" : "🕒 Внести зміну"}</h2>
          <span class="badge ${existingShift ? "badge-warn" : "badge-muted"}">${formatShortDate(targetIso)}</span>
        </div>

        <label class="field-label">Дата зміни</label>
        <div class="seg-group mb-12">
          <button class="seg-btn ${f.dateMode === "today" ? "active" : ""}" data-dmode="today" type="button">Сьогодні</button>
          <button class="seg-btn ${f.dateMode === "yesterday" ? "active" : ""}" data-dmode="yesterday" type="button">Вчора</button>
          <button class="seg-btn ${f.dateMode === "custom" ? "active" : ""}" data-dmode="custom" type="button">Інша дата</button>
        </div>

        ${f.dateMode === "custom" ? `
          <div class="mb-12">
            <input class="input" type="date" id="emp-custom-date" value="${esc(f.customDate || targetIso)}" />
          </div>
        ` : ""}

        ${patterns.length > 0 ? `
          <label class="field-label">Швидкі підказки</label>
          <div class="chips-wrap">
            ${patterns.map((p) => {
              const active = p.start_time === f.startTime && p.end_time === f.endTime;
              return `
                <button class="chip-btn ${active ? "active" : ""}" data-preset-time="${esc(p.start_time)}-${esc(p.end_time)}" type="button">
                  ⚡ ${esc(p.title)}: ${esc(p.label)}
                </button>
              `;
            }).join("")}
          </div>
        ` : ""}

        <div class="row-2 mb-12">
          <div>
            <label class="field-label">Початок</label>
            <input class="input num" type="time" id="emp-start-time" value="${esc(f.startTime)}" />
          </div>
          <div>
            <label class="field-label">Кінець</label>
            <input class="input num" type="time" id="emp-end-time" value="${esc(f.endTime)}" />
          </div>
        </div>

        <div class="card" style="background:var(--bg-champagne);padding:11px 13px;margin-bottom:12px">
          <div class="flex-between">
            <span class="text-muted">Тривалість зміни</span>
            <strong class="num">${hours ? fmtHours(hours) : "Перевір час ⚠️"}</strong>
          </div>
          <div class="flex-between mt-8">
            <span class="text-muted">Орієнтовний розрахунок</span>
            <strong class="num" style="color:var(--gold-deep)">${hours ? fmtMoney(estBase + estBanquet) : "—"}</strong>
          </div>
        </div>

        <div class="card" style="background:var(--bg-canvas);padding:12px 13px;margin-bottom:14px">
          <div class="flex-between">
            <div>
              <div style="font-weight:800;font-size:14px">🎉 Чи був банкет?</div>
              <div class="text-muted">
                ${emp && emp.banquet_rate_per_person
                  ? `Тариф: ${fmtMoney(emp.banquet_rate_per_person)} / люд.`
                  : "Тариф банкету встановлює Аня"}
              </div>
            </div>
            <label class="switch">
              <input type="checkbox" id="emp-banquet-toggle" ${f.isBanquet ? "checked" : ""} />
              <span class="slider"></span>
            </label>
          </div>

          ${f.isBanquet ? `
            <div class="divider"></div>
            <div class="flex-between">
              <span class="field-label" style="margin:0">Кількість гостей:</span>
              <div class="stepper">
                <button type="button" id="guests-dec">−</button>
                <input type="number" id="guests-input" min="0" max="2000" value="${f.banquetGuests ?? ""}" placeholder="—" />
                <button type="button" id="guests-inc">+</button>
              </div>
            </div>
            <div class="text-muted mt-8">Заповни зараз або залиш 0/порожнім і вкажи пізніше</div>
          ` : ""}
        </div>

        <button class="btn btn-gold btn-full" id="btn-save-shift" type="button" ${hours ? "" : "disabled"}>
          ${existingShift ? "💾 Оновити зміну" : "✨ Зберегти зміну"}
        </button>
      </div>
    `;
  }

  // ==================== 2. EMPLOYEE: CALENDAR ====================

  function renderEmployeeCalendarTab() {
    const dash = state.myDash;
    if (!dash) return `<div class="card">Завантаження календаря…</div>`;
    const cal = dash.calendar;
    const firstDt = new Date(Date.UTC(cal.year, cal.month - 1, 1));
    const leadingBlanks = (firstDt.getUTCDay() + 6) % 7;

    return `
      <div class="card card-gold">
        <div class="flex-between mb-8">
          <button class="icon-btn" id="cal-prev" type="button">‹</button>
          <div style="text-align:center">
            <div style="font-size:16px;font-weight:800">${UK_MONTHS[cal.month - 1]} ${cal.year}</div>
            <div class="text-muted">Змін: ${cal.month_shifts} • Годин: ${fmtHours(cal.month_hours)}</div>
          </div>
          <button class="icon-btn" id="cal-next" type="button">›</button>
        </div>

        <div class="kpi-grid" style="margin-bottom:0">
          <div class="kpi-card">
            <div class="kpi-label">Зароблено в місяці</div>
            <div class="kpi-value">${fmtMoney(cal.month_earned)}</div>
            <div class="kpi-hint">Середня зміна: ${fmtMoney(cal.avg_per_shift)}</div>
          </div>
          <div class="kpi-card gold">
            <div class="kpi-label">Прогноз до кінця міс.</div>
            <div class="kpi-value">${fmtMoney(cal.projected_month_total)}</div>
            <div class="kpi-hint">${fmtMoney(cal.avg_per_hour)} / год</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="cal-weekdays">${UK_WD.map((w) => `<div>${w}</div>`).join("")}</div>
        <div class="cal-grid">
          ${Array.from({ length: leadingBlanks }).map(() => `<div class="cal-cell empty"></div>`).join("")}
          ${cal.days.map((d) => {
            const cls = [
              "cal-cell",
              d.is_today ? "today" : "",
              d.has_shift ? "worked" : "",
            ].join(" ");
            let dots = "";
            if (d.has_shift) {
              dots += `<span class="cal-dot" style="background:var(--gold-main)"></span>`;
              if (d.is_banquet) dots += `<span class="cal-dot" style="background:#8A4FFF"></span>`;
              if (d.missing_banquet_rate || d.missing_banquet_guests) {
                dots += `<span class="cal-dot" style="background:var(--warn-text)"></span>`;
              }
            }
            return `
              <button class="${cls}" data-cal-day="${d.date}" type="button">
                <span class="cal-day-num">${d.day}</span>
                <span class="cal-hrs">${d.has_shift ? fmtNum(d.hours) + "г" : ""}</span>
                <span class="cal-dots">${dots}</span>
              </button>
            `;
          }).join("")}
        </div>
        <div class="legend-row mt-12">
          <span><span class="legend-dot" style="background:var(--gold-main)"></span>Зміна</span>
          <span><span class="legend-dot" style="background:#8A4FFF"></span>Банкет</span>
          <span><span class="legend-dot" style="background:var(--warn-text)"></span>Очікує даних</span>
        </div>
      </div>
    `;
  }

  // ==================== 3. EMPLOYEE: SALARY ====================

  function reqStatusBadge(status) {
    switch (status) {
      case "pending":
        return `<span class="badge badge-warn">⏳ Очікує Аню</span>`;
      case "approved":
        return `<span class="badge badge-ok">✅ Схвалено Анею</span>`;
      case "paid":
        return `<span class="badge badge-ok">💸 Виплачено</span>`;
      case "rejected":
        return `<span class="badge badge-danger">❌ Відхилено</span>`;
      case "cancelled":
      default:
        return `<span class="badge badge-muted">✖️ Скасовано</span>`;
    }
  }

  function renderEmployeeSalaryTab() {
    const dash = state.myDash;
    if (!dash) return `<div class="card">Завантаження зарплати…</div>`;
    const s = dash.summary;
    const emp = dash.employee;
    const reqs = dash.payout_requests || [];
    const pendingReq = reqs.find((r) => r.status === "pending");
    const approvedReq = reqs.find((r) => r.status === "approved");

    const showPct = s.percent_pay > 0 || (emp && emp.revenue_percent > 0);
    const totalComp = Math.max(0.01, s.base_pay + s.percent_pay + s.banquet_pay + s.bonuses);
    const wBase = (s.base_pay / totalComp) * 100;
    const wPct = (s.percent_pay / totalComp) * 100;
    const wBanq = (s.banquet_pay / totalComp) * 100;
    const wBon = (s.bonuses / totalComp) * 100;

    return `
      <div class="pills-scroll">
        ${[
          ["today", "Сьогодні"],
          ["week", "Тиждень"],
          ["month", "Цей місяць"],
          ["last_month", "Мин. місяць"],
        ].map(([p, lbl]) => `
          <button class="pill-btn ${state.empPeriod === p ? "active" : ""}" data-emp-period="${p}" type="button">${lbl}</button>
        `).join("")}
      </div>

      <div class="card card-gold">
        <div class="hero-label">Залишок до видачі</div>
        <div class="hero-amount">${fmtMoney(s.balance_due)}</div>
        <div class="flex-between text-muted mb-12">
          <span>Зароблено за період: <b>${fmtMoney(s.earned_period)}</b></span>
          <span>Виплачено: <b>${fmtMoney(s.paid_period)}</b></span>
        </div>

        ${pendingReq ? `
          <div class="card" style="background:var(--warn-bg);border-color:rgba(165,99,15,.26);padding:11px 12px;margin-bottom:0">
            <div class="flex-between">
              <div>
                <div style="font-weight:800;font-size:13.5px;color:var(--warn-text)">⏳ Запит на ${fmtMoney(pendingReq.amount)} надіслано Ані</div>
                <div class="text-muted">Щойно Аня прийме його, отримаєш сповіщення в боті ✨</div>
              </div>
              <button class="btn btn-danger-soft btn-sm" data-cancel-my-req="${pendingReq.id}" type="button">Скасувати</button>
            </div>
          </div>
        ` : `
          ${approvedReq ? `
            <div class="card" style="background:var(--ok-bg);border-color:rgba(34,109,77,.24);padding:10px 12px;margin-bottom:10px">
              <div style="font-weight:800;font-size:13px;color:var(--ok-text)">✅ Запит на ${fmtMoney(approvedReq.amount)} прийнято Анею!</div>
              <div class="text-muted">Очікуй переказ на картку ${esc(approvedReq.card_display || emp.card_masked || "")} 💳</div>
            </div>
          ` : ""}
          <button class="btn btn-gold btn-full" id="btn-emp-req-payout" type="button">💸 Запросити виплату в Ані</button>
        `}
      </div>

      <div class="card">
        <div class="card-title-row">
          <h3 class="card-title">💳 Картка для виплат</h3>
          <button class="btn btn-outline btn-sm" id="btn-emp-edit-card" type="button">${emp.card_digits ? "✏️ Змінити" : "➕ Додати"}</button>
        </div>
        ${emp.card_display ? `
          <div class="card-number-box">
            <div>
              <div class="text-muted" style="font-size:11.5px">Збережений номер картки</div>
              <div class="card-digits-big num">${esc(emp.card_display)}</div>
            </div>
            <span class="badge badge-ok">Готово ✓</span>
          </div>
        ` : `
          <div class="text-muted">Вкажи свій номер картки (16 цифр), щоб Аня могла в 1 клік скопіювати його та скинути тобі виплату.</div>
        `}
      </div>

      ${reqs.length ? `
        <div class="card">
          <div class="card-title-row">
            <h3 class="card-title">📬 Мої запити на виплату</h3>
            <span class="badge badge-muted">${reqs.length}</span>
          </div>
          ${reqs.slice(0, 6).map((r) => `
            <div class="list-item">
              <div class="list-main">
                <div class="list-title">${reqStatusBadge(r.status)} <span class="num">${fmtMoney(r.amount)}</span></div>
                <div class="list-sub">${esc(r.created_at_label || "")}${r.comment ? " • " + esc(r.comment) : ""}</div>
              </div>
              <div class="list-right">
                ${r.status === "pending"
                  ? `<button class="btn btn-danger-soft btn-sm" data-cancel-my-req="${r.id}" type="button">Скасувати</button>`
                  : `<span class="text-muted">${esc(r.card_masked || "")}</span>`}
              </div>
            </div>
          `).join("")}
        </div>
      ` : ""}

      <div class="card">
        <div class="card-title-row">
          <h3 class="card-title">✨ Структура нарахувань</h3>
          <span class="badge badge-gold">${s.shifts_count} зм. • ${fmtHours(s.total_hours)}</span>
        </div>
        <div class="stacked-bar">
          <div class="stacked-seg" style="width:${wBase.toFixed(1)}%;background:#C59220"></div>
          ${showPct ? `<div class="stacked-seg" style="width:${wPct.toFixed(1)}%;background:#2D7D5B"></div>` : ""}
          <div class="stacked-seg" style="width:${wBanq.toFixed(1)}%;background:#E3C261"></div>
          <div class="stacked-seg" style="width:${wBon.toFixed(1)}%;background:#6D83F2"></div>
        </div>
        <div class="legend-row mb-8">
          <span><span class="legend-dot" style="background:#C59220"></span>Години: <b>${fmtMoney(s.base_pay)}</b></span>
          ${showPct ? `<span><span class="legend-dot" style="background:#2D7D5B"></span>%: <b>${fmtMoney(s.percent_pay)}</b></span>` : ""}
          <span><span class="legend-dot" style="background:#E3C261"></span>Банкет: <b>${fmtMoney(s.banquet_pay)}</b></span>
          ${s.bonuses > 0 ? `<span><span class="legend-dot" style="background:#6D83F2"></span>Премії: <b>+${fmtMoney(s.bonuses)}</b></span>` : ""}
          ${s.fines > 0 ? `<span class="badge badge-danger">Штрафи: −${fmtMoney(s.fines)}</span>` : ""}
        </div>

        ${(s.missing_banquet_rate_dates.length || s.missing_banquet_guests_dates.length) ? `
          <div class="divider"></div>
          ${s.missing_banquet_rate_dates.length ? `<div class="badge badge-warn mb-8">🙏 Звернись до Ані (немає тарифу банкету: ${s.missing_banquet_rate_dates.map(formatShortDate).join(", ")})</div>` : ""}
          ${s.missing_banquet_guests_dates.length ? `<div class="badge badge-warn mb-8">👥 Вкажи кількість гостей за банкет: ${s.missing_banquet_guests_dates.map(formatShortDate).join(", ")}</div>` : ""}
        ` : ""}
      </div>

      <div class="card">
        <div class="card-title-row">
          <h3 class="card-title">🏷 Мої умови оплати</h3>
        </div>
        <div class="row-2" style="gap:8px">
          <div class="kpi-card">
            <div class="kpi-label">Ставка</div>
            <div class="kpi-value" style="font-size:16px">${fmtMoney(emp.hourly_rate)}/год</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Банкетний тариф</div>
            <div class="kpi-value" style="font-size:16px">${emp.banquet_rate_per_person ? fmtMoney(emp.banquet_rate_per_person) + " / люд." : "Не вказано ⚠️"}</div>
          </div>
        </div>
        ${emp.revenue_percent > 0 ? `
          <div class="kpi-card mt-8">
            <div class="kpi-label">% від каси</div>
            <div class="kpi-value" style="font-size:16px">${fmtPct(emp.revenue_percent)}</div>
          </div>
        ` : ""}
      </div>

      <div class="card">
        <div class="seg-group mb-12">
          <button class="seg-btn ${state.salarySubtab === "shifts" ? "active" : ""}" data-sal-sub="shifts" type="button">Зміни (${s.shifts.length})</button>
          <button class="seg-btn ${state.salarySubtab === "adj" ? "active" : ""}" data-sal-sub="adj" type="button">Премії/Штрафи</button>
          <button class="seg-btn ${state.salarySubtab === "pay" ? "active" : ""}" data-sal-sub="pay" type="button">Виплати</button>
        </div>
        ${renderSalarySubtabBody(dash)}
      </div>
    `;
  }

  function openCardEditSheet(onSaved) {
    const curDisplay = (state.myDash && state.myDash.employee && state.myDash.employee.card_display)
      || (state.me && state.me.employee && state.me.employee.card_display)
      || "";
    openSheet(
      "💳 Номер картки для виплат",
      `
      <div class="card card-gold mb-12">
        <div class="text-muted">Введи номер банківської картки (16 цифр). Коли ти подаси запит на виплату й Аня його прийме, вона зможе скопіювати цей номер одним дотиком.</div>
      </div>
      <div class="mb-12">
        <label class="field-label">Номер картки</label>
        <input class="input num" type="text" inputmode="numeric" id="modal-card-input" value="${esc(curDisplay)}" placeholder="4149 4991 1234 5678" maxlength="24" autofocus />
      </div>
      <button class="btn btn-gold btn-full" id="modal-card-save" type="button">💾 Зберегти картку</button>
      `,
      (root) => {
        const inp = root.querySelector("#modal-card-input");
        const saveBtn = root.querySelector("#modal-card-save");
        inp.addEventListener("input", () => {
          inp.value = formatCardInputLive(inp.value);
        });
        saveBtn.addEventListener("click", async () => {
          saveBtn.disabled = true;
          try {
            const res = await api("/api/my/card", {
              method: "PUT",
              body: { card_number: inp.value },
            });
            if (state.me && state.me.employee) {
              state.me.employee.card_digits = res.card_digits;
              state.me.employee.card_display = res.card_display;
              state.me.employee.card_masked = res.card_masked;
            }
            haptic("success");
            closeSheet();
            showToast("Номер картки збережено! 💳");
            invalidateCache("myDash", "staff", "payouts");
            await loadCurrentTabData(true);
            renderApp();
            if (typeof onSaved === "function") onSaved(res);
          } catch (err) {
            haptic("error");
            showToast(err.message, true);
            saveBtn.disabled = false;
          }
        });
      }
    );
  }

  function openPayoutRequestSheet() {
    const dash = state.myDash;
    if (!dash) return;
    const emp = dash.employee;
    const bal = Math.max(0, roundKop(dash.summary.balance_due));
    const suggestedAmt = bal > 0 ? String(bal) : "";
    const quickAmounts = [];
    if (bal >= 100) {
      quickAmounts.push({ label: `Весь залишок (${fmtMoney(bal)})`, val: bal });
      if (bal >= 400) quickAmounts.push({ label: `Половина (${fmtMoney(roundKop(bal / 2))})`, val: roundKop(bal / 2) });
    }
    if (bal !== 1000) quickAmounts.push({ label: "1 000 грн", val: 1000 });
    if (bal !== 2000) quickAmounts.push({ label: "2 000 грн", val: 2000 });

    const idemKey = makeIdemKey();
    openSheet(
      "💸 Запит на виплату Ані",
      `
      <div class="card card-gold mb-12">
        <div class="flex-between">
          <div>
            <div class="hero-label" style="font-size:11px">Доступний залишок</div>
            <div class="hero-amount" style="font-size:24px;margin:2px 0">${fmtMoney(dash.summary.balance_due)}</div>
          </div>
          ${emp.card_display ? `<span class="badge badge-ok">💳 Картка є</span>` : `<span class="badge badge-warn">Потрібна картка</span>`}
        </div>
      </div>

      ${emp.card_display ? `
        <div class="card-number-box mb-12">
          <div>
            <div class="text-muted" style="font-size:11px">Картка для переказу</div>
            <div class="card-digits-big num" style="font-size:15px">${esc(emp.card_display)}</div>
          </div>
          <button class="btn btn-outline btn-sm" id="req-change-card" type="button">Змінити</button>
        </div>
      ` : `
        <div class="mb-12">
          <label class="field-label">Номер твоєї картки (16 цифр)</label>
          <input class="input num" type="text" inputmode="numeric" id="req-card-input" placeholder="4149 4991 1234 5678" maxlength="24" />
        </div>
      `}

      <div class="mb-12">
        <label class="field-label">Сума виплати (грн)</label>
        <input class="input num" type="number" step="0.01" min="1" id="req-amt-input" value="${esc(suggestedAmt)}" placeholder="Наприклад, 2500" />
      </div>

      ${quickAmounts.length ? `
        <div class="chips-wrap mb-12">
          ${quickAmounts.map((q) => `
            <button class="chip-btn" data-req-quick="${q.val}" type="button">${esc(q.label)}</button>
          `).join("")}
        </div>
      ` : ""}

      <div class="mb-12">
        <label class="field-label">Коментар (необов'язково)</label>
        <input class="input" id="req-comment-input" placeholder="Наприклад: аванс або за цей тиждень" maxlength="200" />
      </div>

      <button class="btn btn-gold btn-full" id="req-submit-btn" type="button">📨 Надіслати запит Ані</button>
      `,
      (root) => {
        const cardInp = root.querySelector("#req-card-input");
        if (cardInp) {
          cardInp.addEventListener("input", () => {
            cardInp.value = formatCardInputLive(cardInp.value);
          });
        }
        root.querySelector("#req-change-card")?.addEventListener("click", () => {
          closeSheet();
          openCardEditSheet(() => openPayoutRequestSheet());
        });
        const amtInp = root.querySelector("#req-amt-input");
        root.querySelectorAll("[data-req-quick]").forEach((b) => {
          b.addEventListener("click", () => {
            amtInp.value = b.dataset.reqQuick;
          });
        });

        const subBtn = root.querySelector("#req-submit-btn");
        subBtn.addEventListener("click", async () => {
          subBtn.disabled = true;
          try {
            const body = {
              amount: amtInp.value,
              comment: root.querySelector("#req-comment-input").value,
            };
            if (cardInp) body.card_number = cardInp.value;
            await api("/api/my/payout-requests", {
              method: "POST",
              headers: { "X-Idempotency-Key": idemKey },
              body,
            });
            haptic("success");
            closeSheet();
            showToast("Запит надіслано Ані! Чекай підтвердження 🌸");
            invalidateCache("myDash", "payouts", "pulse");
            await loadCurrentTabData(true);
            renderApp();
          } catch (err) {
            haptic("error");
            showToast(err.message, true);
            subBtn.disabled = false;
          }
        });
      }
    );
  }

  function renderSalarySubtabBody(dash) {
    if (state.salarySubtab === "shifts") {
      if (!dash.summary.shifts.length) return `<div class="text-muted">За цей період змін ще немає.</div>`;
      return dash.summary.shifts.map((sh) => `
        <div class="list-item clickable" data-edit-my-shift="${sh.work_date}">
          <div class="list-main">
            <div class="list-title">
              ${esc(sh.date_label)}
              ${sh.is_banquet ? `<span class="badge badge-gold">🎉 ${sh.banquet_guests ? sh.banquet_guests + " люд." : "гості?"}</span>` : ""}
              ${sh.missing_banquet_rate ? `<span class="badge badge-warn">Тариф?</span>` : ""}
            </div>
            <div class="list-sub">${esc(sh.start_time)}–${esc(sh.end_time)} (${fmtHours(sh.hours)}) • База ${fmtMoney(sh.base_pay)}${sh.percent_pay > 0 ? " + % " + fmtMoney(sh.percent_pay) : ""}</div>
          </div>
          <div class="list-right">
            <div class="list-val">${fmtMoney(sh.total_pay)}</div>
          </div>
        </div>
      `).join("");
    }
    if (state.salarySubtab === "adj") {
      if (!dash.adjustments.length) return `<div class="text-muted">Премій та штрафів поки що немає.</div>`;
      return dash.adjustments.map((a) => `
        <div class="list-item">
          <div class="list-main">
            <div class="list-title">
              <span class="badge ${a.kind === "bonus" ? "badge-ok" : "badge-danger"}">${a.kind === "bonus" ? "🎁 Премія" : "📌 Штраф"}</span>
              ${esc(a.date_label)}
            </div>
            ${a.comment ? `<div class="list-sub">${esc(a.comment)}</div>` : ""}
          </div>
          <div class="list-right">
            <div class="list-val" style="color:${a.kind === "bonus" ? "var(--ok-text)" : "var(--danger-text)"}">${a.kind === "bonus" ? "+" : "−"}${fmtMoney(a.amount)}</div>
          </div>
        </div>
      `).join("");
    }
    if (!dash.payouts.length) return `<div class="text-muted">Виплат поки що не було.</div>`;
    return dash.payouts.map((p) => `
      <div class="list-item">
        <div class="list-main">
          <div class="list-title">💸 ${esc(p.date_label)}</div>
          ${p.comment ? `<div class="list-sub">${esc(p.comment)}</div>` : ""}
        </div>
        <div class="list-right">
          <div class="list-val" style="color:var(--ok-text)">${fmtMoney(p.amount)}</div>
        </div>
      </div>
    `).join("");
  }

  // ==================== 4. OWNER: PULSE & ANALYTICS ====================

  function buildPayrollSvgChart(series) {
    const pts = (series || []).slice(-21).map((d) => ({
      label: d.label,
      wd: d.weekday_short,
      date: d.date,
      payroll: d.payroll || 0,
      hours: d.hours || 0,
      shifts: d.shifts_count || 0,
    }));
    if (!pts.length) return `<div class="text-muted">Немає даних для графіка.</div>`;

    const maxPay = Math.max(100, ...pts.map((p) => p.payroll));
    const maxHours = Math.max(4, ...pts.map((p) => p.hours));
    const W = Math.max(340, pts.length * 26 + 34);
    const H = 190;
    const padL = 12, padR = 12, padT = 18, padB = 30;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const step = plotW / pts.length;
    const barW = Math.max(8, Math.min(18, step * 0.56));
    const yPay = (v) => padT + plotH - (v / maxPay) * plotH;
    const yHrs = (h) => padT + plotH - (h / maxHours) * (plotH * 0.9);

    const hoursLinePts = pts.map(
      (p, i) => `${(padL + i * step + step / 2).toFixed(1)},${yHrs(p.hours).toFixed(1)}`
    );

    const barsSvg = pts
      .map((p, i) => {
        const cx = padL + i * step + step / 2;
        const x = cx - barW / 2;
        const y = yPay(p.payroll);
        const h = Math.max(p.payroll > 0 ? 4 : 2, padT + plotH - y);
        const fill = p.payroll > 0 ? "url(#barGold)" : "#EFE7D6";
        const showLabel = pts.length <= 14 || i % 2 === 0 || i === pts.length - 1;
        return `
          <g class="chart-col" data-cidx="${i}" style="cursor:pointer">
            <rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="4" fill="${fill}" />
            <circle cx="${cx.toFixed(1)}" cy="${yHrs(p.hours).toFixed(1)}" r="2.6" fill="#1F1A14" />
            ${showLabel ? `<text x="${cx.toFixed(1)}" y="${H - 10}" text-anchor="middle" font-size="9.5" font-weight="700" fill="#8B7D6B">${esc(p.label)}</text>` : ""}
          </g>
        `;
      })
      .join("");

    const hoverPt =
      state.chartHoverIdx !== null && pts[state.chartHoverIdx]
        ? pts[state.chartHoverIdx]
        : pts[pts.length - 1];

    return `
      <div class="chart-wrap">
        <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" style="display:block">
          <defs>
            <linearGradient id="barGold" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#E5C463"/>
              <stop offset="100%" stop-color="#C59220"/>
            </linearGradient>
          </defs>
          <line x1="${padL}" y1="${padT + plotH}" x2="${W - padR}" y2="${padT + plotH}" stroke="rgba(197,146,32,0.25)"/>
          ${barsSvg}
          ${hoursLinePts.length >= 2 ? `<polyline fill="none" stroke="#1F1A14" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" points="${hoursLinePts.join(" ")}" />` : ""}
        </svg>
      </div>
      <div class="chart-tooltip">
        <span><b>📅 ${esc(hoverPt.label)} (${esc(hoverPt.wd)})</b></span>
        <span>ФОП: <b>${fmtMoney(hoverPt.payroll)}</b></span>
        <span>Години: <b>${fmtHours(hoverPt.hours)}</b> (${hoverPt.shifts} зм.)</span>
      </div>
      <div class="legend-row mt-8">
        <span><span class="legend-dot" style="background:#C59220"></span>Нарахована ЗП (грн)</span>
        <span><span class="legend-dot" style="background:#1F1A14"></span>Відпрацьовані години</span>
      </div>
    `;
  }

  function renderOwnerPulseTab() {
    const a = state.analytics;
    if (!a) return `<div class="card">Рахуємо аналітику…</div>`;
    const k = a.kpis;
    const p = state.me.permissions || {};
    const openReqs = (k.pending_payout_requests || 0) + (k.approved_payout_requests || 0);

    return `
      <div class="pills-scroll">
        ${[
          ["today", "Сьогодні"],
          ["week", "Тиждень"],
          ["two_weeks", "14 днів"],
          ["month", "Цей місяць"],
          ["last_month", "Мин. місяць"],
          ["custom", "Період…"],
        ].map(([pid, lbl]) => `
          <button class="pill-btn ${state.ownerPeriod === pid ? "active" : ""}" data-own-period="${pid}" type="button">${lbl}</button>
        `).join("")}
      </div>

      ${state.ownerPeriod === "custom" ? `
        <div class="card" style="padding:12px">
          <div class="row-2">
            <div>
              <label class="field-label">Від</label>
              <input class="input" type="date" id="own-from-date" value="${esc(state.ownerCustomFrom)}" />
            </div>
            <div>
              <label class="field-label">До</label>
              <input class="input" type="date" id="own-to-date" value="${esc(state.ownerCustomTo)}" />
            </div>
          </div>
          <button class="btn btn-gold btn-sm btn-full mt-8" id="own-apply-custom" type="button">Застосувати період</button>
        </div>
      ` : ""}

      ${p.can_payout && openReqs > 0 ? `
        <div class="pulse-alert">
          <div>
            <div style="font-weight:800;font-size:13.5px">📬 Запити на виплату: ${k.pending_payout_requests || 0} нових${k.approved_payout_requests ? ` • ${k.approved_payout_requests} на переказ` : ""}</div>
            <div class="text-muted">Натисни, щоб прийняти, скопіювати картку й виплатити</div>
          </div>
          <button class="btn btn-gold btn-sm" id="pulse-go-payouts" type="button">Відкрити ›</button>
        </div>
      ` : ""}

      <div class="card card-gold">
        <div class="flex-between mb-8">
          <div>
            <div class="hero-label">Загальний залишок до видачі команді</div>
            <div class="hero-amount">${fmtMoney(k.total_balance_due)}</div>
          </div>
          <span class="badge badge-gold">✅ ${k.connected_staff_count}/${k.active_staff_count} у боті</span>
        </div>
        <div class="flex-between text-muted">
          <span>Нараховано (ФОП): <b>${fmtMoney(k.total_payroll)}</b></span>
          <span>Виплачено за період: <b>${fmtMoney(k.paid_period)}</b></span>
        </div>
      </div>

      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">Відпрацьовано</div>
          <div class="kpi-value">${fmtHours(k.total_hours)}</div>
          <div class="kpi-hint">${k.shifts_count} змін за період</div>
        </div>
        <div class="kpi-card gold">
          <div class="kpi-label">Середня вартість / год</div>
          <div class="kpi-value">${k.cost_per_hour ? fmtMoney(k.cost_per_hour) : "—"}</div>
          <div class="kpi-hint">Активних працівників: ${k.active_staff_count}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-title-row">
          <h3 class="card-title">📈 Нарахування та години по днях</h3>
        </div>
        ${buildPayrollSvgChart(a.daily_series)}
      </div>

      <div class="card">
        <div class="card-title-row">
          <h3 class="card-title">🔍 Аудитор змін та аномалій</h3>
          <span class="badge ${a.anomalies.length ? "badge-warn" : "badge-ok"}">${a.anomalies.length ? a.anomalies.length + " знайдено" : "Все чисто ✓"}</span>
        </div>
        ${!a.anomalies.length ? `<div class="text-muted">Аномальних змін або пропущених даних не виявлено ✨</div>` : a.anomalies.slice(0, 8).map((item, idx) => `
          <div class="anomaly-item ${esc(item.severity)}">
            <div style="min-width:0;flex:1">
              <div style="font-weight:800;font-size:13px">${esc(item.title)}</div>
              <div class="text-muted">${esc(item.description)}</div>
            </div>
            <button class="btn btn-outline btn-sm" data-anom-idx="${idx}" type="button">${esc(item.action_label)}</button>
          </div>
        `).join("")}
      </div>

      ${a.roles_breakdown.length ? `
        <div class="card">
          <div class="card-title-row"><h3 class="card-title">🏷 Розподіл ФОП по посадах</h3></div>
          ${a.roles_breakdown.map((r) => `
            <div class="list-item">
              <div class="list-main">
                <div class="list-title">${esc(r.role)} <span class="badge badge-muted">${r.employees} ос. • ${r.shifts} зм.</span></div>
                <div class="list-sub">${fmtHours(r.hours)} • ${fmtPct(r.share_pct)} бюджету</div>
              </div>
              <div class="list-right"><div class="list-val">${fmtMoney(r.payroll)}</div></div>
            </div>
          `).join("")}
        </div>
      ` : ""}

      <div class="card">
        <div class="card-title-row">
          <h3 class="card-title">📊 Зведена таблиця та Excel</h3>
        </div>
        <div class="row-2 mb-12">
          <button class="btn btn-gold btn-sm" id="btn-excel-chat" type="button">📥 Excel у Telegram</button>
          <button class="btn btn-outline btn-sm" id="btn-excel-dl" type="button">⬇️ Скачати .xlsx</button>
        </div>
        ${a.staff.map((st) => `
          <div class="list-item clickable" data-open-emp="${st.id}">
            <div class="list-main">
              <div class="list-title">
                ${esc(st.full_name)}
                <span class="badge badge-muted">${esc(st.role_name)}</span>
                ${st.has_card ? `<span class="badge badge-gold">💳</span>` : ""}
                ${st.has_warnings ? `<span class="badge badge-warn">⚠️</span>` : ""}
              </div>
              <div class="list-sub">${st.shifts_count} зм. • ${fmtHours(st.total_hours)} • До видачі: <b>${fmtMoney(st.balance_due)}</b></div>
            </div>
            <div class="list-right"><div class="list-val">${fmtMoney(st.earned_period)}</div></div>
          </div>
        `).join("")}
      </div>
    `;
  }

  // ==================== 6. OWNER / ADMIN: STAFF ====================

  function renderOwnerStaffTab() {
    const data = state.staffList;
    if (!data) return `<div class="card">Завантаження команди…</div>`;
    const p = state.me.permissions || {};
    const q = state.staffSearch.trim().toLowerCase();
    const list = (data.staff || []).filter((st) => {
      if (q && !st.full_name.toLowerCase().includes(q) && !st.role_name.toLowerCase().includes(q)) {
        return false;
      }
      if (state.staffFilter === "connected" && !st.connected) return false;
      if (state.staffFilter === "pending" && st.connected) return false;
      if (state.staffFilter === "warnings" && !st.has_warnings) return false;
      return true;
    });

    return `
      <div class="card">
        <div class="flex-between mb-12">
          <h2 class="card-title">👥 Працівники (${data.staff.length})</h2>
          ${p.can_manage_staff ? `<button class="btn btn-gold btn-sm" id="btn-add-staff" type="button">+ Додати</button>` : ""}
        </div>
        <input class="input mb-12" type="search" id="staff-search" value="${esc(state.staffSearch)}" placeholder="Пошук за ім'ям або посадою…" />
        <div class="pills-scroll" style="margin-bottom:0">
          ${[
            ["all", "Усі"],
            ["connected", "🟢 Підключені"],
            ["pending", "⏳ Очікують"],
            ["warnings", "⚠️ Потребують уваги"],
          ].map(([fid, lbl]) => `
            <button class="pill-btn ${state.staffFilter === fid ? "active" : ""}" data-sfilter="${fid}" type="button">${lbl}</button>
          `).join("")}
        </div>
      </div>

      <div class="card">
        ${!list.length ? `<div class="text-muted">Нікого не знайдено за фільтром.</div>` : list.map((st) => `
          <div class="list-item clickable" data-open-emp="${st.id}">
            <div class="list-main">
              <div class="list-title">
                ${st.connected ? "🟢" : "⏳"} ${esc(st.full_name)}
                <span class="badge badge-muted">${esc(st.role_name)}</span>
                ${st.has_card ? `<span class="badge badge-gold">💳</span>` : ""}
                ${st.has_warnings ? `<span class="badge badge-warn">⚠️</span>` : ""}
              </div>
              <div class="list-sub">
                ${st.shifts_count} зм. (${fmtHours(st.total_hours)})
                ${data.can_view_money ? ` • ${fmtMoney(st.hourly_rate)}/год${st.revenue_percent > 0 ? " • " + fmtPct(st.revenue_percent) : ""}` : ""}
              </div>
            </div>
            <div class="list-right">
              ${data.can_view_money ? `<div class="list-val">${fmtMoney(st.balance_due)}</div><div class="text-muted">до видачі</div>` : `<span>›</span>`}
            </div>
          </div>
        `).join("")}
      </div>
    `;
  }

  function openAddStaffModal() {
    const roles = (state.me && state.me.roles) || [];
    const allowedRoles = roles.filter(
      (r) => state.me.role === "owner" || !["адмін", "админ", "адміністратор", "администратор"].includes(r.name.toLowerCase())
    );

    openSheet(
      "➕ Новий працівник",
      `
      <div class="mb-12">
        <label class="field-label">Ім'я працівника</label>
        <input class="input" id="add-name" placeholder="Наприклад, Олена Коваленко" />
      </div>
      <div class="mb-12">
        <label class="field-label">Швидка посада</label>
        <div class="chips-wrap" id="add-role-chips">
          ${allowedRoles.map((r, i) => `
            <button class="chip-btn ${i === 0 ? "active" : ""}" data-role-id="${r.id}" type="button">${esc(r.name)}</button>
          `).join("")}
          <button class="chip-btn" data-role-id="custom" type="button">+ Інша посада</button>
        </div>
        <input class="input mt-8 hidden" id="add-role-custom" style="display:none" placeholder="Введи назву посади" />
      </div>
      <div class="row-2 mb-12">
        <div>
          <label class="field-label">Ставка (грн/год)</label>
          <input class="input num" type="number" step="0.01" min="0" id="add-rate" value="100" />
        </div>
        <div>
          <label class="field-label">% від каси</label>
          <input class="input num" type="number" step="0.1" min="0" id="add-pct" value="0" />
        </div>
      </div>
      <div class="mb-12">
        <label class="field-label">Тариф банкету за 1 людину (необов'язково)</label>
        <input class="input num" type="number" step="0.01" min="0.01" id="add-banq" placeholder="Наприклад, 50 або залиш порожнім" />
      </div>
      <button class="btn btn-gold btn-full" id="add-staff-submit" type="button">✨ Створити та отримати посилання</button>
      `,
      (root) => {
        let selectedRoleId = allowedRoles[0] ? String(allowedRoles[0].id) : "custom";
        const customInp = root.querySelector("#add-role-custom");
        if (selectedRoleId === "custom") customInp.style.display = "block";

        root.querySelectorAll("#add-role-chips .chip-btn").forEach((b) => {
          b.addEventListener("click", () => {
            root.querySelectorAll("#add-role-chips .chip-btn").forEach((x) => x.classList.remove("active"));
            b.classList.add("active");
            selectedRoleId = b.dataset.roleId;
            customInp.style.display = selectedRoleId === "custom" ? "block" : "none";
          });
        });

        const btn = root.querySelector("#add-staff-submit");
        const idemKey = makeIdemKey();
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            const payload = {
              full_name: root.querySelector("#add-name").value,
              hourly_rate: root.querySelector("#add-rate").value,
              revenue_percent: root.querySelector("#add-pct").value,
              banquet_rate_per_person: root.querySelector("#add-banq").value || null,
            };
            if (selectedRoleId === "custom") payload.role_name = customInp.value;
            else payload.role_id = Number(selectedRoleId);

            const res = await api("/api/owner/staff", {
              method: "POST",
              headers: { "X-Idempotency-Key": idemKey },
              body: payload,
            });
            haptic("success");
            closeSheet();
            showToast(`Працівника ${res.employee.full_name} створено! 🎉`);
            invalidateCache("staff", "pulse", "payouts");
            await loadCurrentTabData(true);
            renderApp();
            openEmployeeSheet(res.employee.id);
          } catch (err) {
            haptic("error");
            showToast(err.message, true);
            btn.disabled = false;
          }
        });
      }
    );
  }

  async function openEmployeeSheet(empId, focusAction = null) {
    openSheet("👤 Картка працівника", `<div class="text-muted">Завантаження…</div>`);
    try {
      const detail = await api(`/api/owner/staff/${empId}?preset=${encodeURIComponent(state.ownerPeriod)}`);
      const emp = detail.employee;
      const p = state.me.permissions || {};
      const canMoney = detail.can_view_money;
      const sum = detail.summary;

      const shareUrl = emp.invite_link
        ? `https://t.me/share/url?url=${encodeURIComponent(emp.invite_link)}&text=${encodeURIComponent(`Привіт, ${emp.full_name}! 🌸 Ось твоє персональне посилання для підключення до бота кафе. Натисни Start (Розпочати):`)}`
        : null;

      sheetTitle.textContent = `${emp.full_name} • ${emp.role_name}`;
      sheetBody.innerHTML = `
        <div class="card card-gold" style="margin-bottom:12px">
          <div class="flex-between mb-8">
            <span class="badge ${emp.connected ? "badge-ok" : "badge-warn"}">
              ${emp.connected ? "🟢 Підключено" + (emp.telegram_username ? " (@" + esc(emp.telegram_username) + ")" : "") : "⏳ Очікує підключення"}
            </span>
            <span class="badge badge-muted">${sum.shifts_count} зм. • ${fmtHours(sum.total_hours)}</span>
          </div>
          ${canMoney ? `
            <div class="flex-between">
              <span class="text-muted">Ставка: <b>${fmtMoney(emp.hourly_rate)}/год</b>${emp.revenue_percent > 0 ? " • <b>" + fmtPct(emp.revenue_percent) + "</b>" : ""}</span>
              <strong style="font-size:16px">До видачі: ${fmtMoney(sum.balance_due)}</strong>
            </div>
            <div class="text-muted mt-8">Банкетний тариф: <b>${emp.banquet_rate_per_person !== null ? fmtMoney(emp.banquet_rate_per_person) + " / люд." : "Не вказано ⚠️"}</b></div>
          ` : ""}
          ${emp.card_display ? `
            <div class="card-number-box mt-8">
              <div>
                <div class="text-muted" style="font-size:11px">Картка працівника</div>
                <div class="card-digits-big num" style="font-size:15px">${esc(emp.card_display)}</div>
              </div>
              <button class="btn btn-outline btn-sm" id="emp-sheet-copy-card" type="button">📋 Копіювати</button>
            </div>
          ` : ""}
        </div>

        ${emp.invite_link ? `
          <div class="card" style="background:var(--bg-champagne);margin-bottom:12px">
            <div style="font-weight:800;font-size:13px;margin-bottom:6px">🔗 Посилання-запрошення</div>
            <div style="word-break:break-all;font-size:12.5px;color:var(--gold-deep);margin-bottom:8px">${esc(emp.invite_link)}</div>
            <div class="row-2">
              <button class="btn btn-outline btn-sm" id="copy-invite-btn" type="button">📋 Копіювати</button>
              <button class="btn btn-gold btn-sm" id="share-invite-btn" type="button">📤 Надіслати в TG</button>
            </div>
          </div>
        ` : ""}

        <div class="chips-wrap mb-12">
          ${p.can_edit_hours ? `<button class="btn btn-soft btn-sm" id="act-edit-hours" type="button">🕒 Змінити години</button>` : ""}
          ${p.can_adjust_money ? `<button class="btn btn-soft btn-sm" id="act-bonus" type="button">➕ Премія</button>` : ""}
          ${p.can_adjust_money ? `<button class="btn btn-soft btn-sm" id="act-fine" type="button">➖ Штраф</button>` : ""}
          ${p.can_payout ? `<button class="btn btn-gold btn-sm" id="act-payout" type="button">💸 Виплата</button>` : ""}
          ${emp.can_manage_target ? `<button class="btn btn-outline btn-sm" id="act-rates" type="button">⚙️ Ставки і посада</button>` : ""}
          ${emp.can_manage_target && emp.connected ? `<button class="btn btn-soft btn-sm" id="act-relink" type="button">🔄 Скинути TG</button>` : ""}
          ${emp.can_manage_target ? `<button class="btn btn-danger-soft btn-sm" id="act-delete" type="button">🗑 Видалити</button>` : ""}
        </div>

        <div id="emp-action-box"></div>

        <div class="divider"></div>
        <div style="font-weight:800;font-size:14px;margin-bottom:8px">Останні зміни</div>
        ${!detail.shifts.length ? `<div class="text-muted">Змін поки що немає.</div>` : detail.shifts.map((sh) => `
          <div class="list-item">
            <div class="list-main">
              <div class="list-title">${esc(sh.date_label)} ${sh.is_banquet ? `<span class="badge badge-gold">🎉 ${sh.banquet_guests || "?"} люд.</span>` : ""}</div>
              <div class="list-sub">${esc(sh.start_time)}–${esc(sh.end_time)} (${fmtHours(sh.hours)})</div>
            </div>
            ${canMoney ? `<div class="list-right"><div class="list-val">${fmtMoney(sh.total_pay)}</div></div>` : ""}
          </div>
        `).join("")}
      `;

      const box = sheetBody.querySelector("#emp-action-box");

      const sheetCopyCard = sheetBody.querySelector("#emp-sheet-copy-card");
      if (sheetCopyCard && emp.card_digits) {
        sheetCopyCard.addEventListener("click", () => {
          copyToClipboard(emp.card_digits, "Номер картки скопійовано! 💳");
        });
      }

      const copyBtn = sheetBody.querySelector("#copy-invite-btn");
      if (copyBtn) {
        copyBtn.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(emp.invite_link);
            showToast("Посилання скопійовано! 📋");
          } catch (_) {
            showToast("Скопіюй посилання вручну зверху.");
          }
        });
      }
      const shareBtn = sheetBody.querySelector("#share-invite-btn");
      if (shareBtn && shareUrl) {
        shareBtn.addEventListener("click", () => {
          if (tg && typeof tg.openTelegramLink === "function") tg.openTelegramLink(shareUrl);
          else window.open(shareUrl, "_blank");
        });
      }

      const bHours = sheetBody.querySelector("#act-edit-hours");
      if (bHours) bHours.addEventListener("click", () => renderEmpActionHours(box, emp));

      const bBonus = sheetBody.querySelector("#act-bonus");
      if (bBonus) bBonus.addEventListener("click", () => renderEmpActionAdjustment(box, emp, "bonus"));

      const bFine = sheetBody.querySelector("#act-fine");
      if (bFine) bFine.addEventListener("click", () => renderEmpActionAdjustment(box, emp, "fine"));

      const bPayout = sheetBody.querySelector("#act-payout");
      if (bPayout) bPayout.addEventListener("click", () => openPayoutSheetForEmployee(emp.id));

      const bRates = sheetBody.querySelector("#act-rates");
      if (bRates) bRates.addEventListener("click", () => renderEmpActionRates(box, emp));

      const bRelink = sheetBody.querySelector("#act-relink");
      if (bRelink) {
        bRelink.addEventListener("click", () => {
          box.innerHTML = `
            <div class="card card-gold">
              <div style="font-weight:800;margin-bottom:6px">Скинути Telegram-прив'язку?</div>
              <div class="text-muted mb-12">Старий акаунт втратить доступ одразу, а ти отримаєш нове посилання.</div>
              <button class="btn btn-gold btn-sm btn-full" id="confirm-relink-btn" type="button">🔄 Так, скинути зараз</button>
            </div>
          `;
          box.querySelector("#confirm-relink-btn").addEventListener("click", async () => {
            await api(`/api/owner/staff/${emp.id}/relink`, { method: "POST" });
            showToast("Прив'язку скинуто! Нове посилання готове 🔄");
            invalidateCache("staff", "pulse");
            await loadCurrentTabData(true);
            openEmployeeSheet(emp.id);
          });
        });
      }

      const bDel = sheetBody.querySelector("#act-delete");
      if (bDel) {
        bDel.addEventListener("click", () => {
          box.innerHTML = `
            <div class="card" style="background:var(--danger-bg)">
              <div style="font-weight:800;color:var(--danger-text);margin-bottom:6px">Точно видалити ${esc(emp.full_name)}?</div>
              <div class="text-muted mb-12">Доступ до бота закриється миттєво, але історія виплат збережеться у звітах.</div>
              <button class="btn btn-danger-soft btn-sm btn-full" id="confirm-del-btn" type="button">🗑 Так, видалити</button>
            </div>
          `;
          box.querySelector("#confirm-del-btn").addEventListener("click", async () => {
            await api(`/api/owner/staff/${emp.id}`, { method: "DELETE" });
            closeSheet();
            showToast(`Працівника ${emp.full_name} видалено.`);
            invalidateCache("staff", "pulse", "payouts");
            await loadCurrentTabData(true);
            renderApp();
          });
        });
      }

      if (focusAction === "set_banquet_rate" && bRates) {
        renderEmpActionRates(box, emp);
      }
    } catch (err) {
      sheetBody.innerHTML = `<div class="badge badge-danger">${esc(err.message)}</div>`;
    }
  }

  function renderEmpActionHours(box, emp) {
    box.innerHTML = `
      <div class="card card-gold">
        <div style="font-weight:800;margin-bottom:8px">🕒 Внести або змінити години</div>
        <div class="mb-8"><label class="field-label">Дата</label><input class="input" type="date" id="mgr-shift-date" value="${esc(state.me.today)}" /></div>
        <div class="row-2 mb-8">
          <div><label class="field-label">Початок</label><input class="input num" type="time" id="mgr-shift-start" value="10:00" /></div>
          <div><label class="field-label">Кінець</label><input class="input num" type="time" id="mgr-shift-end" value="22:00" /></div>
        </div>
        <button class="btn btn-gold btn-sm btn-full" id="mgr-shift-save" type="button">💾 Зберегти години</button>
      </div>
    `;
    box.querySelector("#mgr-shift-save").addEventListener("click", async () => {
      try {
        await api(`/api/owner/staff/${emp.id}/shifts`, {
          method: "PUT",
          body: {
            work_date: box.querySelector("#mgr-shift-date").value,
            start_time: box.querySelector("#mgr-shift-start").value,
            end_time: box.querySelector("#mgr-shift-end").value,
          },
        });
        haptic("success");
        showToast("Години оновлено, працівнику надіслано пуш! ✨");
        invalidateCache("staff", "pulse", "myDash");
        await loadCurrentTabData(true);
        openEmployeeSheet(emp.id);
      } catch (err) {
        showToast(err.message, true);
      }
    });
  }

  function renderEmpActionAdjustment(box, emp, kind) {
    const title = kind === "bonus" ? "🎁 Нарахувати премію" : "📌 Зафіксувати штраф";
    const idemKey = makeIdemKey();
    box.innerHTML = `
      <div class="card card-gold">
        <div style="font-weight:800;margin-bottom:8px">${title}</div>
        <div class="row-2 mb-8">
          <div><label class="field-label">Сума (грн)</label><input class="input num" type="number" step="0.01" min="1" id="adj-amount" placeholder="200" /></div>
          <div><label class="field-label">Коментар</label><input class="input" id="adj-comment" placeholder="За чудову роботу" /></div>
        </div>
        <button class="btn btn-gold btn-sm btn-full" id="adj-save" type="button">✨ Підтвердити</button>
      </div>
    `;
    const btn = box.querySelector("#adj-save");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api(`/api/owner/staff/${emp.id}/adjustments`, {
          method: "POST",
          headers: { "X-Idempotency-Key": idemKey },
          body: {
            kind,
            amount: box.querySelector("#adj-amount").value,
            comment: box.querySelector("#adj-comment").value,
          },
        });
        haptic("success");
        showToast(kind === "bonus" ? "Премію додано! 🎁" : "Штраф зафіксовано 📌");
        invalidateCache("staff", "pulse", "payouts", "myDash");
        await loadCurrentTabData(true);
        openEmployeeSheet(emp.id);
      } catch (err) {
        showToast(err.message, true);
        btn.disabled = false;
      }
    });
  }

  function renderEmpActionRates(box, emp) {
    const roles = (state.me && state.me.roles) || [];
    const allowedRoles = roles.filter(
      (r) => state.me.role === "owner" || !["адмін", "админ", "адміністратор", "администратор"].includes(r.name.toLowerCase())
    );
    box.innerHTML = `
      <div class="card card-gold">
        <div style="font-weight:800;margin-bottom:8px">⚙️ Ставки та посада</div>
        <div class="mb-8">
          <label class="field-label">Посада</label>
          <select class="select" id="edit-role-id">
            ${allowedRoles.map((r) => `<option value="${r.id}" ${r.id === emp.role_id ? "selected" : ""}>${esc(r.name)}</option>`).join("")}
          </select>
        </div>
        <div class="row-2 mb-8">
          <div><label class="field-label">Ставка (грн/год)</label><input class="input num" type="number" step="0.01" min="0" id="edit-rate" value="${esc(emp.hourly_rate)}" /></div>
          <div><label class="field-label">% від каси</label><input class="input num" type="number" step="0.1" min="0" id="edit-pct" value="${esc(emp.revenue_percent)}" /></div>
        </div>
        <div class="mb-8">
          <label class="field-label">Тариф банкету за 1 людину (автоперерахунок старих банкетів!)</label>
          <input class="input num" type="number" step="0.01" min="0.01" id="edit-banq" value="${emp.banquet_rate_per_person !== null ? esc(emp.banquet_rate_per_person) : ""}" placeholder="Наприклад, 50" />
        </div>
        <button class="btn btn-gold btn-sm btn-full" id="save-rates-btn" type="button">💾 Зберегти умови</button>
      </div>
    `;
    box.querySelector("#save-rates-btn").addEventListener("click", async () => {
      try {
        const res = await api(`/api/owner/staff/${emp.id}`, {
          method: "PATCH",
          body: {
            role_id: Number(box.querySelector("#edit-role-id").value),
            hourly_rate: box.querySelector("#edit-rate").value,
            revenue_percent: box.querySelector("#edit-pct").value,
            banquet_rate_per_person: box.querySelector("#edit-banq").value || null,
          },
        });
        haptic("success");
        const extra = res.recalculated_banquet_dates && res.recalculated_banquet_dates.length
          ? ` Перераховано банкети (${res.recalculated_banquet_dates.length}) 🎉`
          : "";
        showToast("Умови оплати оновлено!" + extra);
        invalidateCache("staff", "pulse", "payouts", "myDash");
        await loadCurrentTabData(true);
        openEmployeeSheet(emp.id);
      } catch (err) {
        showToast(err.message, true);
      }
    });
  }

  // ==================== 7. OWNER / ADMIN: PAYOUTS ====================

  function renderPayoutRequestCard(r) {
    if (r.status === "approved") {
      return `
        <div class="req-card approved mb-8">
          <div class="flex-between mb-8">
            <div style="min-width:0">
              <div style="font-weight:800;font-size:14.5px">${esc(r.employee_name)} <span class="badge badge-muted">${esc(r.role_name)}</span></div>
              <div class="text-muted">${esc(r.created_at_label || "")} • Баланс: <b>${fmtMoney(r.current_balance_due ?? 0)}</b></div>
            </div>
            <div style="text-align:right">
              <div class="num" style="font-weight:800;font-size:18px;color:var(--gold-deep)">${fmtMoney(r.amount)}</div>
              <span class="badge badge-ok">✅ Прийнято</span>
            </div>
          </div>
          ${r.comment ? `<div class="text-muted mb-8">💬 ${esc(r.comment)}</div>` : ""}
          ${r.card_digits ? `
            <div class="card-number-box mb-8">
              <div style="min-width:0">
                <div class="text-muted" style="font-size:11px">Номер картки для переказу</div>
                <div class="card-digits-big num">${esc(r.card_display || r.card_digits)}</div>
              </div>
              <button class="btn btn-gold btn-sm" data-copy-card="${esc(r.card_digits)}" type="button">📋 Копіювати</button>
            </div>
          ` : `<div class="badge badge-warn mb-8">⚠️ Номер картки не вказаний</div>`}
          <div class="row-2">
            <button class="btn btn-gold btn-sm" data-req-pay="${r.id}" type="button">💸 Виплачено (${fmtMoney(r.amount)})</button>
            <button class="btn btn-outline btn-sm" data-req-reject="${r.id}" data-req-name="${esc(r.employee_name)}" data-req-amt="${r.amount}" type="button">❌ Відхилити</button>
          </div>
        </div>
      `;
    }

    return `
      <div class="req-card mb-8">
        <div class="flex-between mb-8">
          <div style="min-width:0">
            <div style="font-weight:800;font-size:14.5px">${esc(r.employee_name)} <span class="badge badge-muted">${esc(r.role_name)}</span></div>
            <div class="text-muted">${esc(r.created_at_label || "")} • Баланс: <b>${fmtMoney(r.current_balance_due ?? 0)}</b></div>
          </div>
          <div style="text-align:right">
            <div class="num" style="font-weight:800;font-size:18px;color:var(--gold-deep)">${fmtMoney(r.amount)}</div>
            <span class="badge badge-warn">⏳ Новий запит</span>
          </div>
        </div>
        ${r.comment ? `<div class="text-muted mb-8">💬 ${esc(r.comment)}</div>` : ""}
        <div class="text-muted mb-8">💳 Картка: <b>${r.has_card ? esc(r.card_masked) : "не вказано"}</b>${r.has_card ? " (відкриється після прийняття)" : ""}</div>
        <div class="row-2">
          <button class="btn btn-gold btn-sm" data-req-accept="${r.id}" type="button">✅ Прийняти</button>
          <button class="btn btn-danger-soft btn-sm" data-req-reject="${r.id}" data-req-name="${esc(r.employee_name)}" data-req-amt="${r.amount}" type="button">❌ Відхилити</button>
        </div>
      </div>
    `;
  }

  function renderOwnerPayoutsTab() {
    const data = state.payouts;
    if (!data) return `<div class="card">Завантаження виплат…</div>`;

    const allReqs = data.payout_requests || [];
    const openReqs = allReqs.filter((r) => r.status === "pending" || r.status === "approved");
    const doneReqs = allReqs.filter((r) => r.status === "paid" || r.status === "rejected");

    const requestsSection = openReqs.length ? `
      <div class="card">
        <div class="card-title-row">
          <h3 class="card-title">📬 Запити на виплату</h3>
          <span class="badge badge-gold">${openReqs.length} активних</span>
        </div>
        ${openReqs.map(renderPayoutRequestCard).join("")}
      </div>
    ` : `
      <div class="card">
        <div class="card-title-row">
          <h3 class="card-title">📬 Запити на виплату</h3>
          <span class="badge badge-ok">Немає нових</span>
        </div>
        <div class="text-muted">Коли хтось із команди надішле запит, ти зможеш тут прийняти його та в 1 дотик скопіювати номер картки.</div>
      </div>
    `;

    const heroSection = `
      <div class="card card-gold">
        <div class="hero-label">Загальний залишок до видачі команді</div>
        <div class="hero-amount">${fmtMoney(data.total_balance_due)}</div>
        <div class="text-muted">Натисни на працівника нижче, щоб подивитись картку або оформити виплату вручну.</div>
      </div>
    `;

    return `
      ${openReqs.length ? requestsSection + heroSection : heroSection + requestsSection}

      <div class="card">
        <div class="card-title-row"><h3 class="card-title">👛 Баланси команди</h3></div>
        ${data.employees.map((e) => `
          <div class="list-item clickable" data-payout-emp="${e.id}">
            <div class="list-main">
              <div class="list-title">
                ${esc(e.full_name)}
                <span class="badge badge-muted">${esc(e.role_name)}</span>
                ${e.has_card ? `<span class="badge badge-gold">💳 ${esc(e.card_masked)}</span>` : ""}
              </div>
              <div class="list-sub">Зароблено: ${fmtMoney(e.all_time_earned)} • Видано: ${fmtMoney(e.all_time_paid)}</div>
            </div>
            <div class="list-right">
              <div class="list-val" style="color:${e.balance_due > 0 ? "var(--gold-deep)" : "var(--ok-text)"}">${fmtMoney(e.balance_due)}</div>
              <div class="text-muted">Виплатити ›</div>
            </div>
          </div>
        `).join("")}
      </div>

      ${doneReqs.length ? `
        <div class="card">
          <div class="card-title-row"><h3 class="card-title">🗂 Оброблені запити</h3></div>
          ${doneReqs.slice(0, 6).map((r) => `
            <div class="list-item">
              <div class="list-main">
                <div class="list-title">${esc(r.employee_name)} ${reqStatusBadge(r.status)}</div>
                <div class="list-sub">${esc(r.paid_at_label || r.reviewed_at_label || r.created_at_label || "")}${r.comment ? " • " + esc(r.comment) : ""}</div>
              </div>
              <div class="list-right"><div class="list-val">${fmtMoney(r.amount)}</div></div>
            </div>
          `).join("")}
        </div>
      ` : ""}

      ${data.recent_payouts.length ? `
        <div class="card">
          <div class="card-title-row"><h3 class="card-title">📜 Останні виплати</h3></div>
          ${data.recent_payouts.slice(0, 10).map((p) => `
            <div class="list-item">
              <div class="list-main">
                <div class="list-title">${esc(p.full_name)}</div>
                <div class="list-sub">${esc(p.date_label)}${p.comment ? " • " + esc(p.comment) : ""}</div>
              </div>
              <div class="list-right"><div class="list-val" style="color:var(--ok-text)">${fmtMoney(p.amount)}</div></div>
            </div>
          `).join("")}
        </div>
      ` : ""}
    `;
  }

  async function openPayoutSheetForEmployee(empId) {
    if (!state.payouts) state.payouts = await api("/api/owner/payouts");
    const emp = state.payouts.employees.find((x) => x.id === empId);
    if (!emp) return;

    const idemKey = makeIdemKey();
    openSheet(
      `💸 Виплата: ${emp.full_name}`,
      `
      <div class="card card-gold mb-12">
        <div class="hero-label">Поточний залишок до видачі</div>
        <div class="hero-amount" style="font-size:26px">${fmtMoney(emp.balance_due)}</div>
      </div>

      ${emp.card_digits ? `
        <div class="card-number-box mb-12">
          <div style="min-width:0">
            <div class="text-muted" style="font-size:11px">Картка працівника</div>
            <div class="card-digits-big num" style="font-size:15px">${esc(emp.card_display || emp.card_digits)}</div>
          </div>
          <button class="btn btn-gold btn-sm" id="sheet-payout-copy-card" type="button">📋 Копіювати</button>
        </div>
      ` : ""}

      ${emp.balance_due > 0 ? `
        <button class="btn btn-gold btn-full mb-12" id="pay-full-btn" type="button">
          💯 Закрити весь залишок (${fmtMoney(emp.balance_due)})
        </button>
      ` : ""}

      <div class="card mb-12">
        <div style="font-weight:800;margin-bottom:8px">📅 Порахувати і видати за період</div>
        <div class="row-2 mb-8">
          <input class="input" type="date" id="pay-p-from" value="${esc(state.me.today)}" />
          <input class="input" type="date" id="pay-p-to" value="${esc(state.me.today)}" />
        </div>
        <button class="btn btn-outline btn-sm btn-full" id="pay-calc-period" type="button">🧮 Порахувати суму</button>
        <div id="pay-period-preview" class="mt-8"></div>
      </div>

      <div class="card">
        <div style="font-weight:800;margin-bottom:8px">💵 Вільна виплата / аванс</div>
        <div class="row-2 mb-8">
          <input class="input num" type="number" step="0.01" min="1" id="pay-custom-amt" placeholder="Сума (грн)" />
          <input class="input" id="pay-custom-comment" placeholder="Коментар (аванс…)" />
        </div>
        <button class="btn btn-soft btn-full" id="pay-custom-btn" type="button">✨ Зафіксувати виплату</button>
      </div>
      `,
      (root) => {
        const copyCardBtn = root.querySelector("#sheet-payout-copy-card");
        if (copyCardBtn && emp.card_digits) {
          copyCardBtn.addEventListener("click", () => {
            copyToClipboard(emp.card_digits, "Номер картки скопійовано! 💳");
          });
        }

        async function doPayout(amount, comment) {
          const res = await api("/api/owner/payouts", {
            method: "POST",
            headers: { "X-Idempotency-Key": idemKey },
            body: { employee_id: emp.id, amount, comment },
          });
          haptic("success");
          closeSheet();
          state.analytics = null;
          state.staffList = null;
          invalidateCache("payouts", "pulse", "staff", "myDash");
          showToast(`Виплату проведено: ${fmtMoney(res.amount)}! Залишок: ${fmtMoney(res.balance_due)} ✅`);
          await loadCurrentTabData(true);
          renderApp();
        }

        const fullBtn = root.querySelector("#pay-full-btn");
        if (fullBtn) {
          fullBtn.addEventListener("click", async () => {
            fullBtn.disabled = true;
            try { await doPayout(emp.balance_due, "повний розрахунок"); }
            catch (err) { showToast(err.message, true); fullBtn.disabled = false; }
          });
        }

        const calcBtn = root.querySelector("#pay-calc-period");
        calcBtn.addEventListener("click", async () => {
          const d1 = root.querySelector("#pay-p-from").value;
          const d2 = root.querySelector("#pay-p-to").value;
          const prevEl = root.querySelector("#pay-period-preview");
          try {
            const p = await api(`/api/owner/payouts/${emp.id}/preview?from=${d1}&to=${d2}`);
            if (p.earned_period <= 0) {
              prevEl.innerHTML = `<div class="text-muted">За вибрані дні нараховано 0 грн.</div>`;
              return;
            }
            prevEl.innerHTML = `
              <div class="text-muted mb-8">Нараховано за ці дні: <b>${fmtMoney(p.earned_period)}</b> (${p.shifts_count} зм., ${fmtHours(p.total_hours)})</div>
              <button class="btn btn-gold btn-sm btn-full" id="confirm-period-pay" type="button">💸 Виплатити ${fmtMoney(p.earned_period)}</button>
            `;
            prevEl.querySelector("#confirm-period-pay").addEventListener("click", async () => {
              await doPayout(p.earned_period, p.suggested_comment);
            });
          } catch (err) {
            showToast(err.message, true);
          }
        });

        const custBtn = root.querySelector("#pay-custom-btn");
        custBtn.addEventListener("click", async () => {
          custBtn.disabled = true;
          try {
            await doPayout(
              root.querySelector("#pay-custom-amt").value,
              root.querySelector("#pay-custom-comment").value
            );
          } catch (err) {
            showToast(err.message, true);
            custBtn.disabled = false;
          }
        });
      }
    );
  }

  function openRejectRequestSheet(reqId, empName, amount) {
    openSheet(
      "❌ Відхилити запит",
      `
      <div class="card mb-12" style="background:var(--danger-bg)">
        <div style="font-weight:800;color:var(--danger-text)">${esc(empName || "Працівник")} • ${fmtMoney(amount || 0)}</div>
        <div class="text-muted mt-8">Працівник отримає сповіщення у Telegram.</div>
      </div>
      <div class="mb-12">
        <label class="field-label">Причина (необов'язково)</label>
        <input class="input" id="reject-reason-input" placeholder="Наприклад: уточнимо після зміни" maxlength="200" />
      </div>
      <button class="btn btn-danger-soft btn-full" id="reject-confirm-btn" type="button">❌ Підтвердити відхилення</button>
      `,
      (sr) => {
        const confirmBtn = sr.querySelector("#reject-confirm-btn");
        confirmBtn.addEventListener("click", async () => {
          confirmBtn.disabled = true;
          try {
            const reason = sr.querySelector("#reject-reason-input").value.trim();
            await api(`/api/owner/payout-requests/${reqId}/reject`, {
              method: "POST",
              body: { reason: reason || null },
            });
            haptic("warning");
            closeSheet();
            showToast("Запит відхилено");
            invalidateCache("payouts", "pulse", "myDash");
            await loadCurrentTabData(true);
            renderApp();
          } catch (err) {
            showToast(err.message, true);
            confirmBtn.disabled = false;
          }
        });
      }
    );
  }

  // ==================== 8. OWNER ONLY: ADMIN PERMS ====================

  function renderOwnerPermsTab() {
    const data = state.adminPerms;
    if (!data) return `<div class="card">Завантаження налаштувань…</div>`;
    return `
      <div class="card card-gold">
        <div class="card-title-row"><h2 class="card-title">⚙️ Права доступу ролі «Адмін»</h2></div>
        <div class="text-muted">Увімкнені розділи миттєво стають доступні всім підключеним адміністраторам як у боті, так і в Mini App.</div>
      </div>

      <div class="card">
        ${data.permissions.map((perm) => `
          <div class="list-item">
            <div class="list-main">
              <div class="list-title" style="font-size:14px">${esc(perm.label)}</div>
            </div>
            <label class="switch">
              <input type="checkbox" class="perm-toggle-chk" data-perm-key="${esc(perm.key)}" ${perm.enabled ? "checked" : ""} />
              <span class="slider"></span>
            </label>
          </div>
        `).join("")}
      </div>
    `;
  }

  // ==================== EVENT BINDINGS PER TAB ====================

  function bindTabEvents(root) {
    // Employee Shift Tab
    if (state.tab === "shift") {
      root.querySelectorAll("[data-dmode]").forEach((b) => {
        b.addEventListener("click", () => {
          state.shiftForm.dateMode = b.dataset.dmode;
          state.shiftForm._touchedTime = false;
          syncSelectedDateShiftState();
          renderApp();
        });
      });

      const custDate = root.querySelector("#emp-custom-date");
      if (custDate) {
        custDate.addEventListener("change", () => {
          state.shiftForm.customDate = custDate.value;
          state.shiftForm._touchedTime = false;
          syncSelectedDateShiftState();
          renderApp();
        });
      }

      root.querySelectorAll("[data-preset-time]").forEach((b) => {
        b.addEventListener("click", () => {
          const [s, e] = b.dataset.presetTime.split("-");
          state.shiftForm.startTime = s;
          state.shiftForm.endTime = e;
          state.shiftForm._touchedTime = true;
          haptic("light");
          renderApp();
        });
      });

      const sInp = root.querySelector("#emp-start-time");
      const eInp = root.querySelector("#emp-end-time");
      if (sInp) sInp.addEventListener("change", () => {
        state.shiftForm.startTime = sInp.value;
        state.shiftForm._touchedTime = true;
        renderApp();
      });
      if (eInp) eInp.addEventListener("change", () => {
        state.shiftForm.endTime = eInp.value;
        state.shiftForm._touchedTime = true;
        renderApp();
      });

      const banqTog = root.querySelector("#emp-banquet-toggle");
      if (banqTog) {
        banqTog.addEventListener("change", () => {
          state.shiftForm.isBanquet = banqTog.checked;
          haptic("light");
          renderApp();
        });
      }

      const gDec = root.querySelector("#guests-dec");
      const gInc = root.querySelector("#guests-inc");
      const gInp = root.querySelector("#guests-input");
      if (gDec) gDec.addEventListener("click", () => {
        state.shiftForm.banquetGuests = Math.max(0, Number(state.shiftForm.banquetGuests || 0) - 1);
        renderApp();
      });
      if (gInc) gInc.addEventListener("click", () => {
        state.shiftForm.banquetGuests = Math.min(2000, Number(state.shiftForm.banquetGuests || 0) + 1);
        renderApp();
      });
      if (gInp) gInp.addEventListener("change", () => {
        state.shiftForm.banquetGuests = gInp.value ? Math.max(0, Number(gInp.value)) : 0;
      });

      const saveBtn = root.querySelector("#btn-save-shift");
      if (saveBtn) {
        saveBtn.addEventListener("click", async () => {
          saveBtn.disabled = true;
          const f = state.shiftForm;
          const workDate = getSelectedShiftDateIso();
          const payload = {
            work_date: workDate,
            start_time: f.startTime,
            end_time: f.endTime,
            is_banquet: f.isBanquet,
            banquet_guests: f.isBanquet && Number(f.banquetGuests) > 0 ? Number(f.banquetGuests) : null,
          };
          try {
            if (f.editingShiftId) {
              await api(`/api/my/shifts/${f.editingShiftId}`, { method: "PATCH", body: payload });
              showToast("Зміну оновлено! 🌸");
            } else {
              const res = await api("/api/my/shifts", {
                method: "POST",
                headers: { "X-Idempotency-Key": f.idemKey },
                body: payload,
              });
              f.idemKey = makeIdemKey();
              showToast(res.needs_owner_banquet_rate ? "Збережено! Звернись до Ані щодо тарифу банкету 🙏" : "Зміну збережено! ✨");
            }
            haptic("success");
            state.shiftForm._touchedTime = false;
            invalidateCache("myDash", "pulse", "staff");
            await loadCurrentTabData(true);
            renderApp();
          } catch (err) {
            haptic("error");
            showToast(err.message, true);
            saveBtn.disabled = false;
          }
        });
      }
    }

    // Calendar Tab
    if (state.tab === "calendar") {
      root.querySelector("#cal-prev")?.addEventListener("click", async () => {
        state.calMonth -= 1;
        if (state.calMonth < 1) { state.calMonth = 12; state.calYear -= 1; }
        await loadCurrentTabData();
        renderApp();
      });
      root.querySelector("#cal-next")?.addEventListener("click", async () => {
        state.calMonth += 1;
        if (state.calMonth > 12) { state.calMonth = 1; state.calYear += 1; }
        await loadCurrentTabData();
        renderApp();
      });
      root.querySelectorAll("[data-cal-day]").forEach((cell) => {
        cell.addEventListener("click", () => {
          const iso = cell.dataset.calDay;
          const dayObj = state.myDash.calendar.days.find((d) => d.date === iso);
          if (!dayObj || !dayObj.has_shift) {
            state.shiftForm.dateMode = "custom";
            state.shiftForm.customDate = iso;
            state.shiftForm._touchedTime = false;
            switchTab("shift");
            return;
          }
          const showPctRow = dayObj.percent_pay > 0 || (state.me.employee && state.me.employee.revenue_percent > 0);
          openSheet(
            `📅 ${formatShortDate(iso)}`,
            `
            <div class="card card-gold mb-12">
              <div class="list-item"><span class="text-muted">Час роботи</span><strong>${esc(dayObj.start_time)}–${esc(dayObj.end_time)} (${fmtHours(dayObj.hours)})</strong></div>
              <div class="list-item"><span class="text-muted">За годинами</span><strong>${fmtMoney(dayObj.base_pay)}</strong></div>
              ${showPctRow ? `<div class="list-item"><span class="text-muted">% від каси</span><strong>${fmtMoney(dayObj.percent_pay)}</strong></div>` : ""}
              ${dayObj.is_banquet ? `<div class="list-item"><span class="text-muted">Банкет (${dayObj.banquet_guests || "гості?"})</span><strong>${dayObj.missing_banquet_rate ? "Звернись до Ані 🙏" : fmtMoney(dayObj.banquet_pay)}</strong></div>` : ""}
              <div class="list-item"><span style="font-weight:800">Разом за зміну</span><strong style="color:var(--gold-deep);font-size:17px">${fmtMoney(dayObj.total_pay)}</strong></div>
            </div>
            <button class="btn btn-gold btn-full" id="jump-edit-shift" type="button">✏️ Редагувати цю зміну</button>
            `,
            (sr) => {
              sr.querySelector("#jump-edit-shift").addEventListener("click", () => {
                closeSheet();
                state.shiftForm.dateMode = "custom";
                state.shiftForm.customDate = iso;
                state.shiftForm._touchedTime = false;
                syncSelectedDateShiftState();
                switchTab("shift");
              });
            }
          );
        });
      });
    }

    // Salary Tab
    if (state.tab === "salary") {
      root.querySelector("#btn-emp-req-payout")?.addEventListener("click", openPayoutRequestSheet);
      root.querySelector("#btn-emp-edit-card")?.addEventListener("click", () => openCardEditSheet());

      root.querySelectorAll("[data-cancel-my-req]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            await api(`/api/my/payout-requests/${btn.dataset.cancelMyReq}/cancel`, { method: "POST" });
            haptic("warning");
            showToast("Запит на виплату скасовано");
            invalidateCache("myDash", "payouts", "pulse");
            await loadCurrentTabData(true);
            renderApp();
          } catch (err) {
            showToast(err.message, true);
            btn.disabled = false;
          }
        });
      });

      root.querySelectorAll("[data-emp-period]").forEach((b) => {
        b.addEventListener("click", async () => {
          state.empPeriod = b.dataset.empPeriod;
          await loadCurrentTabData();
          renderApp();
        });
      });
      root.querySelectorAll("[data-sal-sub]").forEach((b) => {
        b.addEventListener("click", () => {
          state.salarySubtab = b.dataset.salSub;
          renderApp();
        });
      });
      root.querySelectorAll("[data-edit-my-shift]").forEach((row) => {
        row.addEventListener("click", () => {
          state.shiftForm.dateMode = "custom";
          state.shiftForm.customDate = row.dataset.editMyShift;
          state.shiftForm._touchedTime = false;
          switchTab("shift");
        });
      });
    }

    // Owner Pulse Tab
    if (state.tab === "pulse") {
      root.querySelector("#pulse-go-payouts")?.addEventListener("click", () => switchTab("payouts"));

      root.querySelectorAll("[data-own-period]").forEach((b) => {
        b.addEventListener("click", async () => {
          state.ownerPeriod = b.dataset.ownPeriod;
          if (state.ownerPeriod !== "custom") await loadCurrentTabData();
          renderApp();
        });
      });
      root.querySelector("#own-apply-custom")?.addEventListener("click", async () => {
        state.ownerCustomFrom = root.querySelector("#own-from-date").value;
        state.ownerCustomTo = root.querySelector("#own-to-date").value;
        await loadCurrentTabData();
        renderApp();
      });

      root.querySelectorAll("[data-cidx]").forEach((col) => {
        col.addEventListener("click", () => {
          state.chartHoverIdx = Number(col.dataset.cidx);
          haptic("light");
          renderApp();
        });
      });

      root.querySelectorAll("[data-anom-idx]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const anom = state.analytics.anomalies[Number(btn.dataset.anomIdx)];
          if (!anom) return;
          if (anom.action_type === "set_banquet_rate") {
            await openEmployeeSheet(anom.action_payload.employee_id, "set_banquet_rate");
          } else if (anom.action_payload && anom.action_payload.employee_id) {
            await openEmployeeSheet(anom.action_payload.employee_id);
          }
        });
      });

      root.querySelector("#btn-excel-chat")?.addEventListener("click", async (e) => {
        const b = e.currentTarget;
        b.disabled = true;
        try {
          await api("/api/owner/reports/send_excel", {
            method: "POST",
            body: { from: state.analytics.period.from, to: state.analytics.period.to },
          });
          haptic("success");
          showToast("Excel-файл надіслано у чат з ботом! 📥");
        } catch (err) {
          showToast(err.message, true);
        } finally {
          b.disabled = false;
        }
      });

      root.querySelector("#btn-excel-dl")?.addEventListener("click", () => {
        const p = state.analytics.period;
        const url = `/api/owner/reports/excel?from=${encodeURIComponent(p.from)}&to=${encodeURIComponent(p.to)}&initData=${encodeURIComponent(getInitData())}`;
        window.open(url, "_blank");
      });

      root.querySelectorAll("[data-open-emp]").forEach((row) => {
        row.addEventListener("click", () => openEmployeeSheet(Number(row.dataset.openEmp)));
      });
    }

    // Owner Staff Tab
    if (state.tab === "staff") {
      const searchEl = root.querySelector("#staff-search");
      if (searchEl) {
        searchEl.addEventListener("input", (e) => {
          state.staffSearch = e.target.value;
          const caret = e.target.selectionStart;
          renderApp();
          const newEl = document.querySelector("#staff-search");
          if (newEl) { newEl.focus(); newEl.setSelectionRange(caret, caret); }
        });
      }
      root.querySelectorAll("[data-sfilter]").forEach((b) => {
        b.addEventListener("click", () => {
          state.staffFilter = b.dataset.sfilter;
          renderApp();
        });
      });
      root.querySelector("#btn-add-staff")?.addEventListener("click", openAddStaffModal);
      root.querySelectorAll("[data-open-emp]").forEach((row) => {
        row.addEventListener("click", () => openEmployeeSheet(Number(row.dataset.openEmp)));
      });
    }

    // Owner Payouts Tab
    if (state.tab === "payouts") {
      root.querySelectorAll("[data-copy-card]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          copyToClipboard(btn.dataset.copyCard, "Номер картки скопійовано! 💳");
        });
      });

      root.querySelectorAll("[data-req-accept]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            await api(`/api/owner/payout-requests/${btn.dataset.reqAccept}/accept`, { method: "POST" });
            haptic("success");
            showToast("Запит прийнято! Картку відкрито — натисни «Копіювати» 💳");
            invalidateCache("payouts", "pulse", "myDash");
            await loadCurrentTabData(true);
            renderApp();
          } catch (err) {
            haptic("error");
            showToast(err.message, true);
            btn.disabled = false;
          }
        });
      });

      root.querySelectorAll("[data-req-pay]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            const res = await api(`/api/owner/payout-requests/${btn.dataset.reqPay}/pay`, {
              method: "POST",
              headers: { "X-Idempotency-Key": makeIdemKey() },
            });
            haptic("success");
            state.analytics = null;
            state.staffList = null;
            invalidateCache("payouts", "pulse", "staff", "myDash");
            showToast(`Виплату ${fmtMoney(res.amount)} проведено! Залишок: ${fmtMoney(res.balance_due)} ✅`);
            await loadCurrentTabData(true);
            renderApp();
          } catch (err) {
            haptic("error");
            showToast(err.message, true);
            btn.disabled = false;
          }
        });
      });

      root.querySelectorAll("[data-req-reject]").forEach((btn) => {
        btn.addEventListener("click", () => {
          openRejectRequestSheet(
            Number(btn.dataset.reqReject),
            btn.dataset.reqName,
            btn.dataset.reqAmt
          );
        });
      });

      root.querySelectorAll("[data-payout-emp]").forEach((row) => {
        row.addEventListener("click", () => openPayoutSheetForEmployee(Number(row.dataset.payoutEmp)));
      });
    }

    // Owner Admin Perms Tab
    if (state.tab === "perms") {
      root.querySelectorAll(".perm-toggle-chk").forEach((chk) => {
        chk.addEventListener("change", async () => {
          chk.disabled = true;
          try {
            state.adminPerms = await api("/api/owner/admin-perms/toggle", {
              method: "POST",
              body: { key: chk.dataset.permKey },
            });
            markTabFresh("perms");
            writeSwrCache();
            haptic("success");
            showToast("Права доступу оновлено ✅");
            renderApp();
          } catch (err) {
            showToast(err.message, true);
            chk.checked = !chk.checked;
            chk.disabled = false;
          }
        });
      });
    }
  }

  boot();
})();
