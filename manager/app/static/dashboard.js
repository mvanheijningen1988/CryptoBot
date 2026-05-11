/**
 * CryptoBot Manager – Dashboard Application
 *
 * Handles authentication, i18n translations, RBAC visibility,
 * bot management, agent management, market data via WebSocket,
 * grid profitability checks, backtesting, and agent log streaming.
 */

// ──────────────────────────────────────────────────────────────
// Auth & i18n globals
// ──────────────────────────────────────────────────────────────

/** JWT token retrieved from localStorage (set during login). */
let authToken = localStorage.getItem("cryptobot_token") || "";

/** Manager UI websocket connection used for RPC + realtime updates. */
let managerUiSocket = null;
let managerUiSocketConnectPromise = null;
let managerUiSocketReconnectTimer = null;
let managerUiRpcCounter = 0;
const managerUiPendingRpc = new Map();

/** Currently opened bot detail modal target bot ID. */
let activeBotDetailId = "";

/** The currently authenticated user object (populated on init). */
let currentUser = null;

/** Active UI language code ("en" | "nl"). */
let lang = localStorage.getItem("cryptobot_lang") || "en";

/**
 * Translate a key using the I18N dictionary loaded from i18n.js.
 * Falls back to the English value, then to the raw key.
 *
 * @param {string} key - Translation key (e.g. "btn_start").
 * @returns {string} Translated string.
 */
function t(key) {
  return I18N[lang]?.[key] || I18N.en?.[key] || key;
}

/**
 * Walk the DOM and apply translations to all elements carrying
 * data-i18n (text content) or data-tip-key (tooltip text) attributes.
 */
function applyTranslations() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-tip-key]").forEach((el) => {
    el.dataset.tip = t(el.dataset.tipKey);
  });
}

/**
 * Hide or show UI elements based on the current user's role.
 * Viewers cannot create bots or perform write actions.
 */
function applyRBAC() {
  if (!currentUser) return;
  const isViewer = currentUser.role === "viewer";

  // Hide "New Bot" button for view-only users
  const w = document.getElementById("new_bot_wrapper");
  if (w) w.style.display = isViewer ? "none" : "block";

  const settingsBtn = document.querySelector('.tab-btn[data-tab="tab_settings"]');
  const settingsPane = document.getElementById("tab_settings");
  const isAdmin = currentUser.role === "admin";
  if (settingsBtn) settingsBtn.style.display = isAdmin ? "inline-flex" : "none";
  if (settingsPane) settingsPane.style.display = isAdmin ? "" : "none";

  if (!isAdmin && settingsPane?.classList.contains("active")) {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
    const dashboardBtn = document.querySelector('.tab-btn[data-tab="tab_dashboard"]');
    if (dashboardBtn) dashboardBtn.classList.add("active");
    document.getElementById("tab_dashboard")?.classList.add("active");
  }
}

// ──────────────────────────────────────────────────────────────
// State
// ──────────────────────────────────────────────────────────────

/** Set of event IDs already shown as popups (prevents duplicates). */
const seenEventIds = new Set();

/** Cached result of the last grid profitability check. */
let lastGridPreview = null;

/** Currently selected agent ID for the logs modal. */
let selectedAgentId = null;

/** Optional selected bot ID when the logs modal is opened from bot actions. */
let selectedLogBotId = null;

/** Optional bot name shown in the logs modal title for bot logs. */
let selectedLogBotName = "";

/** Latest bot list cache used by multiple dashboard sections. */
let latestBots = [];

/** Prevent redundant settings file fetches when tab is reopened. */
let settingsLoadedOnce = false;
let settingsPayloadCache = { sections: [], exchanges: [] };
let activeSettingsSubtab = "General";
let editingExchangeId = null;

/** Whether the agent-logs modal is currently visible. */
let logsModalOpen = false;

/** Whether diagnostics tab is active (controls polling). */
let diagnosticsTabActive = false;

/** Active Bitvavo WebSocket connection for live market data. */
let marketSocket = null;

/** The market string the WebSocket is currently subscribed to. */
let marketSocketMarket = null;

/** Timer handle for WebSocket reconnection back-off. */
let marketReconnectTimer = null;

/** Periodic REST refresh timer to keep 24h summary aligned with exchange values. */
let marketSummaryRefreshTimer = null;

/** Latest aggregated market snapshot used by renderMarketSummary(). */
let marketSnapshot = null;
const botMarketSummaryCache = new Map();
let botMarketTooltipEl = null;

/** Metadata map (market → {base, quote, status}) loaded from /api/markets. */
const marketMeta = new Map();
let marketOptions = [];
let marketHighlightIndex = -1;
let lastFeeSnapshot = null;
const cryptoLogoBySymbol = new Map();
let coinMapLoadPromise = null;
let marketIconObserver = null;
const MAX_DECIMALS = 8;
const dashboardBalanceByAsset = new Map();

function roundToDecimals(value, decimals = MAX_DECIMALS) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  const factor = 10 ** decimals;
  return Math.round(n * factor) / factor;
}

function clampInputDecimals(inputEl, decimals = MAX_DECIMALS) {
  if (!inputEl) return;
  const raw = String(inputEl.value ?? "");
  const dotIndex = raw.indexOf(".");
  if (dotIndex < 0) return;
  const intPart = raw.slice(0, dotIndex);
  const fracPart = raw.slice(dotIndex + 1);
  if (fracPart.length <= decimals) return;
  inputEl.value = `${intPart}.${fracPart.slice(0, decimals)}`;
}

function bindMaxDecimalInput(id, decimals = MAX_DECIMALS) {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener("input", () => clampInputDecimals(el, decimals));
}

function placeFloatingMenu(anchorEl, menuEl, maxHeight = 240) {
  if (!anchorEl || !menuEl) return;
  const rect = anchorEl.getBoundingClientRect();
  const viewportH = window.innerHeight;
  const spaceBelow = viewportH - rect.bottom - 8;
  const spaceAbove = rect.top - 8;
  const openUp = spaceBelow < 180 && spaceAbove > spaceBelow;
  const menuHeight = Math.max(120, Math.min(maxHeight, openUp ? spaceAbove : spaceBelow));

  menuEl.style.left = `${Math.max(8, rect.left)}px`;
  menuEl.style.width = `${Math.max(180, rect.width)}px`;
  menuEl.style.maxHeight = `${menuHeight}px`;
  if (openUp) {
    menuEl.style.top = `${Math.max(8, rect.top - menuHeight - 4)}px`;
  } else {
    menuEl.style.top = `${Math.min(viewportH - 8, rect.bottom + 4)}px`;
  }
}

function closeAllAppSelects() {
  document.querySelectorAll(".app-select.open").forEach((el) => el.classList.remove("open"));
}

function refreshAppSelect(selectEl) {
  if (!selectEl) return;
  const wrapper = selectEl.nextElementSibling;
  if (!wrapper || !wrapper.classList.contains("app-select")) return;
  const label = wrapper.querySelector(".app-select-label");
  const menu = wrapper.querySelector(".app-select-menu");
  if (!label || !menu) return;

  const options = [...selectEl.options];
  const current = options.find((o) => o.value === selectEl.value) || options[0];
  label.textContent = current ? current.textContent : "";
  menu.innerHTML = "";

  options.forEach((opt) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `app-select-item${opt.value === selectEl.value ? " current" : ""}${opt.disabled ? " disabled" : ""}`;
    btn.textContent = opt.textContent;
    if (opt.disabled) {
      btn.disabled = true;
    } else {
      btn.onclick = () => {
        selectEl.value = opt.value;
        selectEl.dispatchEvent(new Event("change", { bubbles: true }));
        refreshAppSelect(selectEl);
        wrapper.classList.remove("open");
      };
    }
    menu.appendChild(btn);
  });
}

function initAppSelect(selectEl) {
  if (!selectEl || selectEl.dataset.customized === "1") return;
  if (selectEl.id === "market" || selectEl.id === "profit_mode") return;

  selectEl.dataset.customized = "1";
  selectEl.classList.add("app-select-native");

  const wrap = document.createElement("div");
  wrap.className = "app-select";
  wrap.innerHTML = `<button type="button" class="app-select-trigger"><span class="app-select-label"></span><span class="app-select-caret">▾</span></button><div class="app-select-menu"></div>`;
  selectEl.insertAdjacentElement("afterend", wrap);

  const trigger = wrap.querySelector(".app-select-trigger");
  trigger.onclick = (e) => {
    e.stopPropagation();
    const isOpen = wrap.classList.contains("open");
    closeAllAppSelects();
    if (!isOpen) {
      wrap.classList.add("open");
      placeFloatingMenu(trigger, wrap.querySelector(".app-select-menu"), 260);
    }
  };

  selectEl.addEventListener("change", () => refreshAppSelect(selectEl));
  refreshAppSelect(selectEl);
}

function initAllAppSelects() {
  document.querySelectorAll("select").forEach((sel) => initAppSelect(sel));
}

function refreshAllAppSelects() {
  document.querySelectorAll("select").forEach((sel) => refreshAppSelect(sel));
}

function normalizeMarketValue(raw) {
  return String(raw || "")
    .trim()
    .toUpperCase()
    .replace(/\//g, "-")
    .replace(/\s+/g, "");
}

function normalizeSymbol(raw) {
  return String(raw || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

async function loadCoinMap() {
  if (cryptoLogoBySymbol.size > 0) return;
  if (coinMapLoadPromise) {
    await coinMapLoadPromise;
    return;
  }

  coinMapLoadPromise = (async () => {
    try {
      const res = await fetch("/static/assets/coin_map.json", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      if (!Array.isArray(data)) return;
      for (const item of data) {
        if (!item || typeof item !== "object") continue;
        const key = normalizeSymbol(item.symbol || "");
        const url = String(item.img_url || "").trim();
        if (!cryptoLogoBySymbol.has(key)) {
          cryptoLogoBySymbol.set(key, url);
        }
      }
    } catch {
      // Best effort: keep dropdown working even if icon index is unavailable.
    } finally {
      coinMapLoadPromise = null;
    }
  })();

  await coinMapLoadPromise;
}

function getMarketIconPath(market) {
  const meta = marketMeta.get(market) || {};
  const base = String(meta.base || market.split("-")[0] || "");
  return cryptoLogoBySymbol.get(normalizeSymbol(base)) || "";
}

function getAssetIconPath(asset) {
  return cryptoLogoBySymbol.get(normalizeSymbol(asset)) || "";
}

function renderMarketInputIcon() {
  const input = getMarketInput();
  const icon = document.getElementById("market_input_icon");
  const wrap = input?.closest(".combo-wrap");
  if (!input || !icon || !wrap) return;

  const market = normalizeMarketValue(input.value);
  const iconPath = getMarketIconPath(market);
  if (iconPath) {
    icon.src = iconPath;
    icon.hidden = false;
    wrap.classList.add("has-icon");
  } else {
    icon.hidden = true;
    icon.removeAttribute("src");
    wrap.classList.remove("has-icon");
  }
}

function getMarketInput() {
  return document.getElementById("market");
}

function closeMarketSuggestions() {
  const menu = document.getElementById("market_suggestions");
  if (!menu) return;
  if (marketIconObserver) {
    marketIconObserver.disconnect();
    marketIconObserver = null;
  }
  menu.classList.remove("open");
  menu.innerHTML = "";
  marketHighlightIndex = -1;
}

function setupMarketIconLazyLoad(menu) {
  if (!menu) return;
  const icons = [...menu.querySelectorAll(".combo-icon[data-src]")];
  if (!icons.length) return;

  if (marketIconObserver) {
    marketIconObserver.disconnect();
    marketIconObserver = null;
  }

  if (!("IntersectionObserver" in window)) {
    icons.forEach((icon) => {
      icon.src = icon.dataset.src;
      icon.removeAttribute("data-src");
    });
    return;
  }

  marketIconObserver = new IntersectionObserver(
    (entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const icon = entry.target;
        const src = icon.dataset.src;
        if (src) {
          icon.src = src;
          icon.removeAttribute("data-src");
        }
        observer.unobserve(icon);
      });
    },
    {
      root: menu,
      rootMargin: "32px 0px",
      threshold: 0.01,
    }
  );

  icons.forEach((icon) => marketIconObserver.observe(icon));
}

function renderMarketSuggestions(query = "") {
  const menu = document.getElementById("market_suggestions");
  const input = getMarketInput();
  if (!menu) return;
  const q = normalizeMarketValue(query);
  const filtered = marketOptions.filter((m) => !q || m.includes(q)).slice(0, 30);
  menu.innerHTML = "";
  if (!filtered.length) {
    menu.innerHTML = `<div class="combo-empty">No markets found</div>`;
    menu.classList.add("open");
    if (input) placeFloatingMenu(input, menu, 260);
    marketHighlightIndex = -1;
    return;
  }
  filtered.forEach((market, idx) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "combo-item";
    btn.dataset.market = market;
    btn.dataset.idx = String(idx);

    const iconPath = getMarketIconPath(market);
    if (iconPath) {
      const icon = document.createElement("img");
      icon.className = "combo-icon";
      icon.dataset.src = iconPath;
      icon.alt = "";
      icon.decoding = "async";
      icon.onerror = () => {
        icon.remove();
      };
      btn.appendChild(icon);
    }

    const label = document.createElement("span");
    label.className = "combo-label";
    label.textContent = market;
    btn.appendChild(label);

    btn.onclick = () => {
      const input = getMarketInput();
      if (!input) return;
      input.value = market;
      closeMarketSuggestions();
      onMarketValueCommitted();
    };
    menu.appendChild(btn);
  });
  marketHighlightIndex = 0;
  menu.classList.add("open");
  setupMarketIconLazyLoad(menu);
  if (input) placeFloatingMenu(input, menu, 260);
}

function updateMarketHighlight(delta) {
  const menu = document.getElementById("market_suggestions");
  if (!menu || !menu.classList.contains("open")) return;
  const items = [...menu.querySelectorAll(".combo-item")];
  if (!items.length) return;
  marketHighlightIndex = (marketHighlightIndex + delta + items.length) % items.length;
  items.forEach((el, idx) => el.classList.toggle("active", idx === marketHighlightIndex));
  items[marketHighlightIndex]?.scrollIntoView({ block: "nearest" });
}

function onMarketValueCommitted() {
  const input = getMarketInput();
  if (!input) return;
  input.value = normalizeMarketValue(input.value);
  renderMarketInputIcon();
  closeMarketSuggestions();
  startMarketRealtime();
  loadBalances();
  loadMarketFees(true);
  renderMinimumOrderHint();
  scheduleGridCheck();
}

function formatPctFromRate(rate) {
  const n = Number(rate || 0) * 100;
  return `${n.toFixed(4)}%`;
}

function tr(key, fallback) {
  const value = t(key);
  return value === key ? fallback : value;
}

function fillTemplate(template, values) {
  let output = String(template || "");
  for (const [key, value] of Object.entries(values || {})) {
    output = output.replaceAll(`{${key}}`, String(value));
  }
  return output;
}

function getNumberLocale() {
  const override = String(localStorage.getItem("cryptobot_number_locale") || "").trim();
  if (override && override.toLowerCase() !== "auto") {
    return override;
  }
  return navigator.language || "en-US";
}

function setNumberLocaleOverride(locale) {
  const value = String(locale || "").trim();
  if (!value || value.toLowerCase() === "auto") {
    localStorage.removeItem("cryptobot_number_locale");
  } else {
    localStorage.setItem("cryptobot_number_locale", value);
  }
  if (marketSnapshot) {
    renderMarketSummary(marketSnapshot);
  }
}

globalThis.setNumberLocaleOverride = setNumberLocaleOverride;

function getNumberLocaleForLanguage(languageCode) {
  return languageCode === "nl" ? "nl-NL" : "en-US";
}

function formatSignedNumber(value, digits = 2) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  const locale = getNumberLocale();
  const sign = n >= 0 ? "+" : "";
  return `${sign}${new Intl.NumberFormat(locale, {
    useGrouping: true,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(n)}`;
}

function renderFeeInfo(snapshot) {
  const box = document.getElementById("fee_info_box");
  if (!box) return;
  const title = tr("lbl_fee_info_title", "Bitvavo fees");
  const tierLabel = tr("lbl_fee_tier", "Tier");
  const volumeLabel = tr("lbl_fee_volume_30d", "30d volume");
  const marketRatesLabel = tr("lbl_fee_market_rates", "Market rates");
  const accountRatesLabel = tr("lbl_fee_account_rates", "Account rates");
  const appliedLabelText = tr("lbl_fee_applied", "Applied to this bot");
  const makerLabel = tr("lbl_maker", "Maker");
  const takerLabel = tr("lbl_taker", "Taker");
  const unavailableLabel = tr("lbl_fee_info_unavailable", "Bitvavo fees are currently unavailable.");

  if (!snapshot || !snapshot.available) {
    box.classList.add("warn");
    const msg = snapshot?.message || unavailableLabel;
    box.innerHTML = `<strong>${title}</strong><br>${msg}`;
    return;
  }

  box.classList.remove("warn");
  const tier = snapshot.tier ?? "-";
  const volume = formatNumber(snapshot.volume_30d_eur ?? 0);
  const marketMaker = formatPctFromRate(snapshot.market_maker_fee_rate);
  const marketTaker = formatPctFromRate(snapshot.market_taker_fee_rate);
  const accountMaker = formatPctFromRate(snapshot.account_maker_fee_rate);
  const accountTaker = formatPctFromRate(snapshot.account_taker_fee_rate);
  const applied = formatPctFromRate(snapshot.applied_fee_rate);
  const appliedLabel = snapshot.applied_fee_type === "taker" ? takerLabel : makerLabel;

  box.innerHTML = [
    `<strong>${title}</strong>`,
    `${tierLabel}: ${tier} | ${volumeLabel}: ${volume} EUR`,
    `${marketRatesLabel}: ${makerLabel} ${marketMaker} / ${takerLabel} ${marketTaker}`,
    `${accountRatesLabel}: ${makerLabel} ${accountMaker} / ${takerLabel} ${accountTaker}`,
    `${appliedLabelText}: ${applied} (${appliedLabel})`,
  ].join("<br>");
}

async function loadMarketFees(forceSet = false) {
  const market = normalizeMarketValue(getMarketInput()?.value);
  const box = document.getElementById("fee_info_box");
  if (!box || !market) return;

  try {
    const data = await api(`/api/v1/market/fees?market=${encodeURIComponent(market)}`);
    lastFeeSnapshot = data;
    renderFeeInfo(data);
    if (forceSet) scheduleGridCheck();
  } catch (err) {
    lastFeeSnapshot = { available: false, message: String(err.message || err) };
    renderFeeInfo(lastFeeSnapshot);
  }
}

// ──────────────────────────────────────────────────────────────
// Authenticated API helper
// ──────────────────────────────────────────────────────────────

/**
 * Fetch wrapper that injects the JWT Bearer token and handles 401
 * by redirecting to the login page.
 *
 * @param {string} url - API endpoint path (e.g. "/api/v1/bots").
 * @param {RequestInit} [options] - Standard fetch options.
 * @returns {Promise<any>} Parsed JSON body or plain text.
 */
async function api(url, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const parsed = new URL(url, globalThis.location.origin);

  if (!parsed.pathname.startsWith("/api/v1/")) {
    const headers = { "Content-Type": "application/json", ...options.headers };
    if (authToken) headers["Authorization"] = "Bearer " + authToken;

    const res = await fetch(parsed.pathname + parsed.search, { ...options, headers });
    if (res.status === 401) {
      localStorage.removeItem("cryptobot_token");
      globalThis.location.href = "/login";
      throw new Error("Unauthorized");
    }
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try {
        const txt = await res.text();
        if (txt) msg = txt;
      } catch { /* empty */ }
      throw new Error(msg);
    }
    return res.headers.get("content-type")?.includes("application/json") ? res.json() : res.text();
  }

  let body = null;
  if (typeof options.body === "string" && options.body.trim()) {
    try {
      body = JSON.parse(options.body);
    } catch {
      body = null;
    }
  } else if (options.body && typeof options.body === "object") {
    body = options.body;
  }

  const query = {};
  parsed.searchParams.forEach((value, key) => {
    query[key] = value;
  });

  const result = await sendManagerUiRpc({
    method,
    path: parsed.pathname,
    query,
    body,
  });
  return result;
}

function _managerWsUrl() {
  const scheme = globalThis.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${globalThis.location.host}/api/v1/ui/ws?token=${encodeURIComponent(authToken)}`;
}

function _rejectAllPendingRpc(reason) {
  const error = reason instanceof Error ? reason : new Error(String(reason || "WebSocket disconnected"));
  for (const [, pending] of managerUiPendingRpc.entries()) {
    clearTimeout(pending.timer);
    pending.reject(error);
  }
  managerUiPendingRpc.clear();
}

function _scheduleManagerUiReconnect() {
  if (managerUiSocketReconnectTimer || !authToken) return;
  managerUiSocketReconnectTimer = setTimeout(() => {
    managerUiSocketReconnectTimer = null;
    ensureManagerUiSocket().catch(() => {
      _scheduleManagerUiReconnect();
    });
  }, 1500);
}

function _handleManagerUiRealtimeEvent(eventName) {
  if (eventName === "agent_event") {
    loadAgents().catch(() => {});
    loadEvents().catch(() => {});
  }
  scheduleRealtimeRefresh();
}

function _onManagerUiSocketMessage(raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    return;
  }

  const msgType = String(msg?.type || "");
  if (msgType === "rpc_result") {
    const id = String(msg.id || "");
    const pending = managerUiPendingRpc.get(id);
    if (!pending) return;
    managerUiPendingRpc.delete(id);
    clearTimeout(pending.timer);

    if (Number(msg.status) === 401) {
      localStorage.removeItem("cryptobot_token");
      globalThis.location.href = "/login";
      pending.reject(new Error("Unauthorized"));
      return;
    }

    if (!msg.ok) {
      pending.reject(new Error(String(msg.error || `HTTP ${msg.status || 500}`)));
      return;
    }

    pending.resolve(msg.data);
    return;
  }

  if (msgType === "dashboard_update") {
    _handleManagerUiRealtimeEvent(String(msg.event || "update"));
  }
}

function ensureManagerUiSocket() {
  if (managerUiSocket && managerUiSocket.readyState === WebSocket.OPEN) {
    return Promise.resolve(managerUiSocket);
  }
  if (managerUiSocketConnectPromise) {
    return managerUiSocketConnectPromise;
  }
  if (!authToken) {
    return Promise.reject(new Error("Missing auth token"));
  }

  managerUiSocketConnectPromise = new Promise((resolve, reject) => {
    const ws = new WebSocket(_managerWsUrl());

    ws.onopen = () => {
      managerUiSocket = ws;
      managerUiSocketConnectPromise = null;
      resolve(ws);
    };

    ws.onmessage = (event) => {
      _onManagerUiSocketMessage(event.data);
    };

    ws.onerror = () => {
      // onclose handles reconnect and pending request rejection.
    };

    ws.onclose = () => {
      if (managerUiSocket === ws) {
        managerUiSocket = null;
      }
      managerUiSocketConnectPromise = null;
      _rejectAllPendingRpc(new Error("WebSocket disconnected"));
      _scheduleManagerUiReconnect();
    };

    setTimeout(() => {
      if (ws.readyState !== WebSocket.OPEN) {
        try { ws.close(); } catch { /* ignore */ }
        if (managerUiSocketConnectPromise) {
          managerUiSocketConnectPromise = null;
          reject(new Error("WebSocket connect timeout"));
        }
      }
    }, 7000);
  });

  return managerUiSocketConnectPromise;
}

async function sendManagerUiRpc({ method, path, query = {}, body = null }) {
  const ws = await ensureManagerUiSocket();
  const id = `rpc-${Date.now()}-${++managerUiRpcCounter}`;
  const payload = {
    type: "rpc",
    id,
    method: String(method || "GET").toUpperCase(),
    path: String(path || ""),
    query: query && typeof query === "object" ? query : {},
    body,
  };

  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      managerUiPendingRpc.delete(id);
      reject(new Error("RPC timeout"));
    }, 20000);
    managerUiPendingRpc.set(id, { resolve, reject, timer });

    try {
      ws.send(JSON.stringify(payload));
    } catch (err) {
      clearTimeout(timer);
      managerUiPendingRpc.delete(id);
      reject(err instanceof Error ? err : new Error(String(err)));
    }
  });
}

// ──────────────────────────────────────────────────────────────
// Bot configuration builder
// ──────────────────────────────────────────────────────────────

/**
 * Read all form fields in the "Create Bot" modal and return a
 * BotConfig-shaped object ready to POST to /api/bots.
 *
 * @returns {object} Configuration payload.
 */
function currentConfig() {
  const market = normalizeMarketValue(document.getElementById("market").value);
  const marketInfo = marketMeta.get(market) || {};
  const split = market.split("-");
  return {
    market,
    base_currency: marketInfo.base || split[0] || "",
    quote_currency: marketInfo.quote || split[1] || "",
    mode: document.getElementById("mode").value,
    strategy: "static_grid",
    fee_rate: roundToDecimals(Number(lastFeeSnapshot?.applied_fee_rate || 0)),
    start_price: 0,
    grid: {
      lower_price: roundToDecimals(Number(document.getElementById("lower_price").value)),
      upper_price: roundToDecimals(Number(document.getElementById("upper_price").value)),
      levels: Number(document.getElementById("levels").value),
      order_size_quote: roundToDecimals(Number(document.getElementById("order_size_quote").value)),
    },
    budget: {
      quote_budget: roundToDecimals(Number(document.getElementById("quote_budget").value)),
      base_budget: 0,
      profit_mode: document.getElementById("profit_mode").value,
      skim_ratio: roundToDecimals(Number(document.getElementById("skim_ratio").value)),
    },
  };
}

function getMinimumRequiredOrderSizeQuote(config) {
  if (!config || config.mode !== "live") return null;
  const meta = marketMeta.get(config.market);
  if (!meta) return null;
  const minQuote = Number(meta.min_order_in_quote_asset || 0);
  const minBase = Number(meta.min_order_in_base_asset || 0);
  const maxGridPrice = Math.max(Number(config.grid?.lower_price || 0), Number(config.grid?.upper_price || 0));
  const minQuoteFromBase = minBase > 0 && maxGridPrice > 0 ? minBase * maxGridPrice : 0;
  const requiredQuote = Math.max(minQuote, minQuoteFromBase);
  if (!(requiredQuote > 0)) return null;
  return {
    requiredQuote,
    minQuote,
    minBase,
    minQuoteFromBase,
    quoteCurrency: config.quote_currency,
    baseCurrency: config.base_currency,
    maxGridPrice,
  };
}

function renderMinimumOrderHint(config = currentConfig()) {
  const hintEl = document.getElementById("min_order_hint");
  if (!hintEl) return;

  hintEl.classList.remove("warn", "ok");

  if (config.mode !== "live") {
    hintEl.textContent = t("lbl_min_order_hint_live_only");
    return;
  }

  const limits = getMinimumRequiredOrderSizeQuote(config);
  if (!limits) {
    hintEl.textContent = t("lbl_min_order_hint_unavailable");
    return;
  }

  const values = {
    required: formatNumber(limits.requiredQuote),
    quote: limits.quoteCurrency,
    current: "-",
  };
  const orderSizeInput = document.getElementById("order_size_quote");
  const currentOrderSize = Number(orderSizeInput?.value ?? config.grid?.order_size_quote ?? 0);
  values.current = formatNumber(currentOrderSize);
  const isValid = currentOrderSize + 1e-12 >= limits.requiredQuote;

  hintEl.textContent = fillTemplate(
    t(isValid ? "lbl_min_order_hint_ok" : "lbl_min_order_hint_warn"),
    values,
  );
  hintEl.classList.add(isValid ? "ok" : "warn");
}

// ──────────────────────────────────────────────────────────────
// Formatting helpers
// ──────────────────────────────────────────────────────────────

/**
 * Locale-aware number formatter with configurable decimal places.
 *
 * @param {number|string|null} value - The value to format.
 * @param {number} [digits=6]       - Maximum fraction digits.
 * @returns {string} Formatted number or "-" for invalid input.
 */
function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const n = Number(value);
  const maxDigits = Number.isInteger(arguments[1]) && arguments[1] >= 0 ? Number(arguments[1]) : 8;
  const minDigits = Number.isInteger(arguments[2]) && arguments[2] >= 0 ? Number(arguments[2]) : 0;
  return new Intl.NumberFormat(getNumberLocale(), {
    useGrouping: true,
    minimumFractionDigits: minDigits,
    maximumFractionDigits: maxDigits,
  }).format(n);
}

function formatPnlWithPercent(pnlValue, startBudget) {
  const pnl = Number(pnlValue || 0);
  const start = Number(startBudget || 0);
  const amount = formatNumber(pnl);
  if (!(start > 0)) return amount;
  const pct = (pnl / start) * 100;
  const pctStr = `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
  return `${amount} (${pctStr})`;
}

/** Format seconds into human-readable uptime string. */
function formatUptime(seconds) {
  if (!seconds || seconds <= 0) return "-";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

/**
 * Return a human-readable relative time string (e.g. "3 minutes ago").
 *
 * @param {Date} date - The date to format.
 * @returns {string} Relative time string.
 */
function timeAgo(date) {
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 0) return date.toLocaleString();
  const intervals = [
    { label: lang === "nl" ? "maand" : "month", plural: lang === "nl" ? "maanden" : "months", seconds: 2592000 },
    { label: lang === "nl" ? "week" : "week", plural: lang === "nl" ? "weken" : "weeks", seconds: 604800 },
    { label: lang === "nl" ? "dag" : "day", plural: lang === "nl" ? "dagen" : "days", seconds: 86400 },
    { label: lang === "nl" ? "uur" : "hour", plural: lang === "nl" ? "uur" : "hours", seconds: 3600 },
    { label: lang === "nl" ? "minuut" : "minute", plural: lang === "nl" ? "minuten" : "minutes", seconds: 60 },
  ];
  for (const i of intervals) {
    const count = Math.floor(seconds / i.seconds);
    if (count >= 1) {
      const ago = lang === "nl" ? "geleden" : "ago";
      return `${count} ${count === 1 ? i.label : i.plural} ${ago}`;
    }
  }
  const ago = lang === "nl" ? "geleden" : "ago";
  return `${seconds} ${seconds === 1 ? (lang === "nl" ? "seconde" : "second") : (lang === "nl" ? "seconden" : "seconds")} ${ago}`;
}

// ──────────────────────────────────────────────────────────────
// Market summary (REST + WebSocket)
// ──────────────────────────────────────────────────────────────

/**
 * Render a market-data snapshot into the four market-stats DOM elements
 * (last price, 24h change, volume base, volume quote).
 *
 * @param {object} summary - Market data with last_price / open_24h / etc.
 */
function renderMarketSummary(summary) {
  const lastEl = document.getElementById("market_last_price");
  const changeEl = document.getElementById("market_change");
  const volQuoteEl = document.getElementById("market_volume_quote");

  const last = Number(summary.last_price ?? summary.last ?? 0);
  const open = Number(summary.open_24h ?? summary.open ?? 0);
  const volumeBase = Number(summary.volume_24h_base ?? summary.volume ?? 0);
  const volumeQuote = Number(summary.volume_24h_quote ?? summary.volumeQuote ?? 0);

  // Calculate 24h change; use pre-computed values if available
  let diffAbs = Number(summary.diff_24h_abs ?? Number.NaN);
  let diffPct = Number(summary.diff_24h_pct ?? Number.NaN);
  if (Number.isNaN(diffAbs) || Number.isNaN(diffPct)) {
    diffAbs = open > 0 ? last - open : 0;
    diffPct = open > 0 ? (diffAbs / open) * 100 : 0;
  }

  lastEl.textContent = formatNumber(last);
  changeEl.textContent = `${formatSignedNumber(diffAbs, 6)} (${formatSignedNumber(diffPct, 2)}%)`;

  // Colour-code positive / negative
  changeEl.classList.remove("market-positive", "market-negative");
  changeEl.classList.add(diffAbs >= 0 ? "market-positive" : "market-negative");
  lastEl.classList.remove("market-positive", "market-negative");
  lastEl.classList.add(diffAbs >= 0 ? "market-positive" : "market-negative");

  volQuoteEl.textContent = formatNumber(volumeQuote, 0);
}

function ensureBotMarketTooltip() {
  if (botMarketTooltipEl) return botMarketTooltipEl;
  const tip = document.createElement("div");
  tip.id = "bot_market_tooltip";
  tip.className = "tip-popup info-popup bot-market-tooltip";
  tip.style.display = "none";
  document.body.appendChild(tip);
  botMarketTooltipEl = tip;
  return tip;
}

function hideBotMarketTooltip() {
  const tip = botMarketTooltipEl || document.getElementById("bot_market_tooltip");
  if (tip) tip.style.display = "none";
}

function positionBotMarketTooltip(anchorRect) {
  const tip = ensureBotMarketTooltip();
  const margin = 10;
  const rect = tip.getBoundingClientRect();
  let left = anchorRect.left;
  let top = anchorRect.bottom + 8;

  if (left + rect.width > globalThis.innerWidth - margin) {
    left = Math.max(margin, globalThis.innerWidth - rect.width - margin);
  }
  if (top + rect.height > globalThis.innerHeight - margin) {
    top = Math.max(margin, anchorRect.top - rect.height - 8);
  }

  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function renderBotMarketTooltip(target, market, summary, errorText = "") {
  const tip = ensureBotMarketTooltip();
  if (!target || !market) {
    hideBotMarketTooltip();
    return;
  }

  const marketName = String(market || "-");
  const title = marketName;
  if (!summary) {
    tip.innerHTML = `
      <div class="tip-title">${title}</div>
      <div class="tip-body">
        <div class="tip-row">${errorText || t("loading")}</div>
      </div>
    `;
    tip.style.display = "block";
    positionBotMarketTooltip(target.getBoundingClientRect());
    return;
  }

  const last = Number(summary.last_price ?? summary.last ?? 0);
  const open = Number(summary.open_24h ?? summary.open ?? 0);
  const volumeQuote = Number(summary.volume_24h_quote ?? summary.volumeQuote ?? 0);
  const suppliedDiffPct = Number(summary.change_24h_pct ?? 0);
  const diffAbs = open > 0 ? last - open : 0;
  const diffPct = open > 0 ? (diffAbs / open) * 100 : suppliedDiffPct;

  tip.innerHTML = `
    <div class="tip-title">${title}</div>
    <div class="tip-body">
      <div class="tip-row">${t("lbl_last_price")}: ${formatNumber(last)}</div>
      <div class="tip-row ${diffAbs >= 0 ? "market-positive" : "market-negative"}">${t("th_24h_change")}: ${formatSignedNumber(diffAbs, 6)} (${formatSignedNumber(diffPct, 2)}%)</div>
      <div class="tip-row">${t("lbl_24h_vol_quote")}: ${formatNumber(volumeQuote, 0)}</div>
    </div>
  `;
  tip.style.display = "block";
  positionBotMarketTooltip(target.getBoundingClientRect());
}

function getBotSummaryFromCell(target) {
  if (!(target instanceof Element)) return null;
  const dataset = target.dataset || {};
  const last = Number(dataset.lastPrice || 0);
  const open = Number(dataset.open24h || 0);
  const changePct = Number(dataset.change24hPct || 0);
  const volumeQuote = Number(dataset.volume24hQuote || 0);
  if (last <= 0 && open <= 0 && Math.abs(changePct) <= 0 && volumeQuote <= 0) {
    return null;
  }
  return {
    last_price: last,
    open_24h: open,
    change_24h_pct: changePct,
    volume_24h_quote: volumeQuote,
  };
}

function enrichMarketCell(cellEl, market, metrics) {
  if (!(cellEl instanceof Element)) return;
  const safeMarket = String(market || "-");
  const m = metrics || {};
  const last = Number(m.market_last_price ?? m.price ?? 0);
  const open = Number(m.market_open_24h ?? 0);
  const changePct = Number(m.market_change_24h_pct ?? 0);
  const volumeQuote = Number(m.market_volume_24h_quote ?? 0);
  if (safeMarket.includes("-")) {
    cellEl.dataset.market = safeMarket;
  } else {
    delete cellEl.dataset.market;
  }
  cellEl.dataset.lastPrice = String(Number.isFinite(last) ? last : 0);
  cellEl.dataset.open24h = String(Number.isFinite(open) ? open : 0);
  cellEl.dataset.change24hPct = String(Number.isFinite(changePct) ? changePct : 0);
  cellEl.dataset.volume24hQuote = String(Number.isFinite(volumeQuote) ? volumeQuote : 0);

  const cacheKey = safeMarket.trim().toUpperCase();
  if (cacheKey && cacheKey.includes("-") && (last > 0 || open > 0 || volumeQuote > 0 || Math.abs(changePct) > 0)) {
    botMarketSummaryCache.set(cacheKey, {
      data: {
        last_price: last,
        open_24h: open,
        change_24h_pct: changePct,
        volume_24h_quote: volumeQuote,
      },
      ts: Date.now(),
      promise: null,
    });
  }
}

async function getBotMarketSummary(market) {
  const key = String(market || "").trim().toUpperCase();
  if (!key) return null;

  const now = Date.now();
  const cached = botMarketSummaryCache.get(key);
  if (cached && cached.data && now - Number(cached.ts || 0) < 15000) {
    return cached.data;
  }
  if (cached && cached.promise) {
    return cached.promise;
  }

  const pending = api(`/api/v1/market/summary?market=${encodeURIComponent(key)}`)
    .then((data) => {
      botMarketSummaryCache.set(key, { data, ts: Date.now(), promise: null });
      return data;
    })
    .catch((err) => {
      botMarketSummaryCache.set(key, { data: null, ts: 0, promise: null });
      throw err;
    });

  botMarketSummaryCache.set(key, { data: cached?.data || null, ts: Number(cached?.ts || 0), promise: pending });
  return pending;
}

function wireBotMarketTooltips() {
  const body = document.getElementById("bots_body");
  if (!body || body.dataset.marketTooltipBound === "1") return;
  body.dataset.marketTooltipBound = "1";

  body.addEventListener("mouseover", async (event) => {
    const target = event.target instanceof Element ? event.target.closest(".bot-market-cell[data-market]") : null;
    if (!target) return;
    const market = String(target.dataset.market || "").trim().toUpperCase();
    if (!market) return;

    const rowSummary = getBotSummaryFromCell(target);
    if (rowSummary) {
      renderBotMarketTooltip(target, market, rowSummary);
      return;
    }

    renderBotMarketTooltip(target, market, null, t("loading"));
    try {
      const summary = await getBotMarketSummary(market);
      if (!target.isConnected) return;
      renderBotMarketTooltip(target, market, summary);
    } catch {
      if (!target.isConnected) return;
      renderBotMarketTooltip(target, market, null, t("lbl_fee_info_unavailable"));
    }
  });

  body.addEventListener("mousemove", (event) => {
    const target = event.target instanceof Element ? event.target.closest(".bot-market-cell[data-market]") : null;
    if (!target || !botMarketTooltipEl || botMarketTooltipEl.style.display === "none") return;
    positionBotMarketTooltip(target.getBoundingClientRect());
  });

  body.addEventListener("mouseout", (event) => {
    const from = event.target instanceof Element ? event.target.closest(".bot-market-cell[data-market]") : null;
    if (!from) return;
    const to = event.relatedTarget instanceof Element ? event.relatedTarget.closest(".bot-market-cell[data-market]") : null;
    if (to === from) return;
    hideBotMarketTooltip();
  });

  document.addEventListener("scroll", hideBotMarketTooltip, true);
  window.addEventListener("resize", hideBotMarketTooltip);
}

/**
 * Reset all four market-stat elements to "N/A" and remove colour classes.
 */
function resetMarketSummaryToNA() {
  for (const id of ["market_last_price", "market_change", "market_volume_quote"]) {
    const el = document.getElementById(id);
    el.textContent = "N/A";
    el.classList.remove("market-positive", "market-negative");
  }
}

/**
 * Fetch the list of trading markets from the manager API and
 * populate the <select id="market"> dropdown.
 */
async function loadMarkets() {
  const input = getMarketInput();
  if (!input) return;
  try {
    await loadCoinMap();
    const markets = await api("/api/v1/markets?status=trading");
    const current = normalizeMarketValue(input.value);
    marketMeta.clear();
    marketOptions = [];
    for (const item of markets) {
      marketMeta.set(item.market, item);
      marketOptions.push(item.market);
    }
    marketOptions.sort();
    // Restore previous selection or default to BTC-EUR
    if (current && marketMeta.has(current)) input.value = current;
    else if (marketMeta.has("BTC-EUR")) input.value = "BTC-EUR";
    else if (marketOptions.length) input.value = marketOptions[0];
    renderMarketInputIcon();
    renderMarketSuggestions(input.value);
    closeMarketSuggestions();
    renderMinimumOrderHint();
  } catch (err) {
    console.error("Failed to load markets", err);
  }
}

/**
 * Fetch the latest 24h market summary from the REST API
 * and update the market stats panel.
 */
async function loadMarketSummary() {
  const market = normalizeMarketValue(getMarketInput()?.value);
  if (!market) { resetMarketSummaryToNA(); return; }
  try {
    const s = await api(`/api/v1/market/summary?market=${encodeURIComponent(market)}`);
    marketSnapshot = {
      market: s.market,
      last: Number(s.last_price ?? 0),
      open: Number(s.open_24h ?? 0),
      volume: Number(s.volume_24h_base ?? 0),
      volumeQuote: Number(s.volume_24h_quote ?? 0),
    };
    renderMarketSummary(s);
  } catch (err) {
    console.error("Failed to load market summary", err);
    resetMarketSummaryToNA();
  }
}

/**
 * Fetch available balances for the base and quote currencies of the
 * currently selected market and display them in the balance hint.
 * Also auto-fills the quote_budget field.
 */
async function loadBalances() {
  const market = normalizeMarketValue(getMarketInput()?.value);
  const quoteEl = document.getElementById("avail_quote");
  const baseEl = document.getElementById("avail_base");
  const budgetInputEl = document.getElementById("quote_budget");
  const budgetSliderEl = document.getElementById("quote_budget_slider");
  const budgetSliderMaxEl = document.getElementById("quote_budget_slider_max");
  quoteEl.textContent = "-";
  baseEl.textContent = "-";
  if (!market) return;

  const parts = market.split("-");
  if (parts.length !== 2) return;
  const [baseSym, quoteSym] = parts;

  try {
    const [baseData, quoteData] = await Promise.all([
      api(`/api/v1/balance?symbol=${encodeURIComponent(baseSym)}`),
      api(`/api/v1/balance?symbol=${encodeURIComponent(quoteSym)}`),
    ]);
    const availQuote = Number(quoteData.available);
    baseEl.textContent = `${formatNumber(Number(baseData.available))} ${baseSym}`;
    quoteEl.textContent = `${formatNumber(availQuote)} ${quoteSym}`;
    if (budgetSliderEl) {
      budgetSliderEl.min = "0";
      budgetSliderEl.max = String(Math.max(availQuote, 0));
      budgetSliderEl.step = "0.00000001";
      const currentBudget = Number(budgetInputEl?.value || 0);
      budgetSliderEl.value = String(Math.min(Math.max(currentBudget, 0), availQuote));
    }
    if (budgetSliderMaxEl) budgetSliderMaxEl.textContent = formatNumber(availQuote, 0);

    // Pre-fill budget with full available balance
    if (budgetInputEl) budgetInputEl.value = String(availQuote);
    syncBudgetAndOrderSize("budget");
  } catch (err) {
    console.error("Failed to load balances", err);
    const cachedQuote = Number(dashboardBalanceByAsset.get(String(quoteSym || "").toUpperCase())?.available || Number.NaN);
    const cachedBase = Number(dashboardBalanceByAsset.get(String(baseSym || "").toUpperCase())?.available || Number.NaN);
    const fallbackQuote = Number.isFinite(cachedQuote) && cachedQuote >= 0 ? cachedQuote : 0;
    const fallbackBase = Number.isFinite(cachedBase) && cachedBase >= 0 ? cachedBase : 0;
    const currentBudget = Number(budgetInputEl?.value || 0);
    quoteEl.textContent = `${formatNumber(fallbackQuote)} ${quoteSym}`;
    baseEl.textContent = `${formatNumber(fallbackBase)} ${baseSym}`;

    if (budgetSliderEl) {
      budgetSliderEl.min = "0";
      budgetSliderEl.max = String(Math.max(currentBudget, fallbackQuote, 0));
      budgetSliderEl.step = "0.00000001";
      budgetSliderEl.value = String(Math.max(Math.min(currentBudget, Math.max(currentBudget, fallbackQuote, 0)), 0));
    }
    if (budgetSliderMaxEl) budgetSliderMaxEl.textContent = formatNumber(Math.max(currentBudget, fallbackQuote, 0), 0);

    // Keep manual budget entry possible when exchange balance lookup fails.
    if (budgetInputEl && !(Number(budgetInputEl.value) > 0)) {
      if (fallbackQuote > 0) {
        budgetInputEl.value = String(roundToDecimals(fallbackQuote));
      } else {
        budgetInputEl.value = String(roundToDecimals(Number(document.getElementById("order_size_quote")?.value || 0) * (Number(document.getElementById("levels")?.value || 0) || 1)));
      }
    }
    syncBudgetAndOrderSize("budget");
  }
}

// ──────────────────────────────────────────────────────────────
// Bitvavo WebSocket (real-time ticker)
// ──────────────────────────────────────────────────────────────

/**
 * Cleanly tear down the active market WebSocket and cancel any
 * pending reconnection timer.
 */
function closeMarketSocket() {
  if (marketReconnectTimer) { clearTimeout(marketReconnectTimer); marketReconnectTimer = null; }
  if (marketSummaryRefreshTimer) {
    clearInterval(marketSummaryRefreshTimer);
    marketSummaryRefreshTimer = null;
  }
  if (marketSocket) {
    try { marketSocket.onopen = null; marketSocket.onmessage = null; marketSocket.onclose = null; marketSocket.onerror = null; marketSocket.close(); } catch { /* socket already closed */ }
  }
  marketSocket = null;
}

/**
 * Parse a single WebSocket message from Bitvavo's ticker24h channel
 * and merge it into the local marketSnapshot, then re-render.
 *
 * @param {string} raw - Raw JSON string from the WebSocket.
 */
function handleMarketSocketMessage(raw) {
  let msg;
  try { msg = JSON.parse(raw); } catch { return; }
  if (!msg || typeof msg !== "object") return;

  // Ignore updates for a different market (can happen during switching)
  if (msg.market && marketSocketMarket && msg.market !== marketSocketMarket) return;

  // Only process messages that carry market payload fields
  if (!(msg.market || msg.last || msg.open || msg.volume || msg.volumeQuote || msg.price || msg.bestBid || msg.bestAsk)) return;

  if (!marketSnapshot) marketSnapshot = { market: marketSocketMarket, last: 0, open: 0, volume: 0, volumeQuote: 0 };

  // Update last price from whichever field is present
  if (msg.last !== undefined) marketSnapshot.last = Number(msg.last);
  else if (msg.price !== undefined) marketSnapshot.last = Number(msg.price);
  else if (msg.bestBid !== undefined && msg.bestAsk !== undefined) marketSnapshot.last = (Number(msg.bestBid) + Number(msg.bestAsk)) / 2;

  if (msg.open !== undefined) marketSnapshot.open = Number(msg.open);
  if (msg.volume !== undefined) marketSnapshot.volume = Number(msg.volume);
  if (msg.volumeQuote !== undefined) marketSnapshot.volumeQuote = Number(msg.volumeQuote);

  renderMarketSummary(marketSnapshot);
}

/**
 * Open a WebSocket to Bitvavo's ticker24h channel for the currently
 * selected market. Automatically reconnects on close after 1.5 s.
 */
function startMarketRealtime() {
  const market = normalizeMarketValue(getMarketInput()?.value);
  closeMarketSocket();
  marketSocketMarket = market;
  if (!market) { resetMarketSummaryToNA(); return; }

  marketSocket = new WebSocket("wss://ws.bitvavo.com/v2/");
  marketSocket.onopen = () => {
    marketSocket.send(JSON.stringify({ action: "subscribe", channels: [{ name: "ticker24h", markets: [market] }] }));
  };
  marketSocket.onmessage = (e) => handleMarketSocketMessage(e.data);
  marketSocket.onerror = () => loadMarketSummary();  // Fallback to REST on WS error
  marketSocket.onclose = () => {
    if (marketSocketMarket !== market) return;  // Market changed, don't reconnect
    marketReconnectTimer = setTimeout(() => {
      if (normalizeMarketValue(getMarketInput()?.value) === market) startMarketRealtime();
    }, 1500);
  };

  // Also load REST summary immediately (WS may take a moment)
  loadMarketSummary();
  // Keep 24h open/volume aligned with exchange by periodic REST refresh.
  marketSummaryRefreshTimer = setInterval(() => {
    if (normalizeMarketValue(getMarketInput()?.value) !== market) return;
    loadMarketSummary();
  }, 15000);
}

// ──────────────────────────────────────────────────────────────
// Grid profitability
// ──────────────────────────────────────────────────────────────

/**
 * POST the current grid parameters to the profitability preview
 * endpoint and update the inline summary with a "View details" link.
 */
async function checkGridProfitability() {
  try {
    const basePayload = {
      grid: {
        lower_price: Number(document.getElementById("lower_price").value),
        upper_price: Number(document.getElementById("upper_price").value),
        levels: Number(document.getElementById("levels").value),
        order_size_quote: Number(document.getElementById("order_size_quote").value),
      },
    };
    const makerRate = Number(lastFeeSnapshot?.market_maker_fee_rate ?? lastFeeSnapshot?.account_maker_fee_rate ?? 0);
    const takerRate = Number(lastFeeSnapshot?.market_taker_fee_rate ?? lastFeeSnapshot?.account_taker_fee_rate ?? 0);
    const [makerPreview, takerPreview] = await Promise.all([
      api("/api/v1/strategy/static-grid/preview", { method: "POST", body: JSON.stringify({ ...basePayload, fee_rate: makerRate }) }),
      api("/api/v1/strategy/static-grid/preview", { method: "POST", body: JSON.stringify({ ...basePayload, fee_rate: takerRate }) }),
    ]);

    const appliedType = lastFeeSnapshot?.applied_fee_type === "maker" ? "maker" : "taker";
    const appliedPreview = appliedType === "maker" ? makerPreview : takerPreview;
    const combinedPreview = {
      ...appliedPreview,
      fee_context: {
        maker_rate: makerRate,
        taker_rate: takerRate,
        applied_type: appliedType,
        maker: makerPreview,
        taker: takerPreview,
      },
    };

    lastGridPreview = combinedPreview;

    const cls = combinedPreview.is_profitable ? "profit-ok" : "profit-warn";
    const txt = combinedPreview.is_profitable ? t("grid_profitable") : t("grid_not_profitable");

    const inlineEl = document.getElementById("grid_profit_summary");
    if (inlineEl) {
      inlineEl.innerHTML = `<span class="${cls}"><strong>${txt}</strong></span> — <a href="#" id="grid_preview_link" data-action="open-grid-preview" style="color:var(--accent)">${t("btn_view_details")}</a>`;
      const linkEl = inlineEl.querySelector("#grid_preview_link");
      if (linkEl) {
        linkEl.onclick = (e) => {
          e.preventDefault();
          openGridPreviewModal(combinedPreview);
        };
      }
    }

    // If the preview modal is already open, refresh its content in-place
    const previewModal = document.getElementById("grid_preview_modal");
    if (previewModal && previewModal.open) {
      openGridPreviewModal(combinedPreview, true);
    }
  } catch (err) {
    lastGridPreview = null;
    const inlineEl = document.getElementById("grid_profit_summary");
    if (inlineEl) inlineEl.innerHTML = `<div class="profit-warn">${t("grid_calc_error")}: ${String(err.message || err)}</div>`;
  }
}

/**
 * Render the grid preview modal with profitability summary
 * and the full list of scheduled trades.
 */
function openGridPreviewModal(r, skipShowModal = false) {
  const modal = document.getElementById("grid_preview_modal");
  const tbody = document.getElementById("grid_trades_body");
  if (!modal || !tbody || !r) return;

  tbody.innerHTML = "";
  for (const trade of (r.trades || [])) {
    const row = document.createElement("tr");
    const displayLevel = Number(trade.level) + 1;
    const pcls = trade.profitable ? "grid-trade-ok" : "grid-trade-bad";
    const icon = trade.profitable ? "✓" : "✗";
    const feeRatePct = Number(r.fee_rate || 0) * 100;
    const buyFee = Number(trade.buy_fee_quote || 0);
    const sellFee = Number(trade.sell_fee_quote || 0);
    const totalFees = Number(trade.total_fees_quote || 0);
    const feeTitle = [
      `${tr("lbl_fee_rate", "Fee rate")} ${formatNumber(feeRatePct)}%`,
      `${tr("lbl_fee_tooltip_buy", "Buy fee")}: ${formatNumber(buyFee)} (${formatNumber(feeRatePct)}% ${tr("lbl_fee_tooltip_of_order_size", "of order size")})`,
      `${tr("lbl_fee_tooltip_sell", "Sell fee")}: ${formatNumber(sellFee)} (${formatNumber(feeRatePct)}% ${tr("lbl_fee_tooltip_of_sell_value", "of sell value")})`,
      `${tr("lbl_total_fees", "Total Fees")}: ${formatNumber(totalFees)}`,
    ].join("\n");
    const safeFeeTip = feeTitle.replace(/"/g, "&quot;");
    row.innerHTML = `<td>${displayLevel}</td><td>${formatNumber(trade.buy_price)}</td><td>${formatNumber(trade.sell_price)}</td><td>${formatNumber(trade.order_size_quote)}</td><td class="grid-fee-cell" data-fee-tip="${safeFeeTip}">${formatNumber(totalFees)}</td><td class="${pcls}">${formatNumber(trade.net_profit)}</td><td class="${pcls}">${icon}</td>`;
    tbody.appendChild(row);
  }

  if (!skipShowModal) modal.showModal();
}

document.getElementById("grid_profit_summary")?.addEventListener("click", (e) => {
  const target = e.target instanceof Element ? e.target : null;
  const link = target ? target.closest("[data-action='open-grid-preview']") : null;
  if (!link) return;
  e.preventDefault();
  if (lastGridPreview) openGridPreviewModal(lastGridPreview);
});

document.getElementById("close_grid_preview")?.addEventListener("click", () => {
  document.getElementById("grid_preview_modal").close();
});

// ──────────────────────────────────────────────────────────────
// Bi-directional order size ↔ quote budget calculation
// ──────────────────────────────────────────────────────────────

/** Track which field the user last edited to determine sync direction. */
let _budgetCalcSource = "";

/**
 * When quote_budget changes, compute order_size_quote = budget / levels.
 * When order_size_quote changes, compute quote_budget = size × levels.
 * The levels field triggers a recalc based on whichever was last edited.
 */
function syncBudgetAndOrderSize(source) {
  const levels = Number(document.getElementById("levels").value) || 1;
  const budgetEl = document.getElementById("quote_budget");
  const budgetSliderEl = document.getElementById("quote_budget_slider");
  const sizeEl = document.getElementById("order_size_quote");

  const syncSlider = () => {
    if (!budgetSliderEl) return;
    const sliderMax = Number(budgetSliderEl.max || 0) || 0;
    const budget = Number(budgetEl.value) || 0;
    budgetSliderEl.value = String(Math.min(Math.max(budget, 0), sliderMax || budget));
  };

  if (source === "budget") {
    _budgetCalcSource = "budget";
    const budget = Number(budgetEl.value) || 0;
    if (budget > 0 && levels > 0) {
      sizeEl.value = String(roundToDecimals(budget / levels));
    }
    syncSlider();
  } else if (source === "size") {
    _budgetCalcSource = "size";
    const size = Number(sizeEl.value) || 0;
    if (size > 0 && levels > 0) {
      budgetEl.value = String(roundToDecimals(size * levels));
    }
    syncSlider();
  } else if (source === "levels") {
    // Recalculate based on whichever the user last touched
    if (_budgetCalcSource === "budget") {
      const budget = Number(budgetEl.value) || 0;
      if (budget > 0 && levels > 0) {
        sizeEl.value = String(roundToDecimals(budget / levels));
      }
    } else if (_budgetCalcSource === "size") {
      const size = Number(sizeEl.value) || 0;
      if (size > 0 && levels > 0) {
        budgetEl.value = String(roundToDecimals(size * levels));
      }
    }
    syncSlider();
  }
  scheduleGridCheck();
}

document.getElementById("quote_budget").addEventListener("input", () => syncBudgetAndOrderSize("budget"));
document.getElementById("quote_budget_slider")?.addEventListener("input", (e) => {
  const budgetEl = document.getElementById("quote_budget");
  if (!budgetEl) return;
  budgetEl.value = String(Number(e.target.value || 0));
  syncBudgetAndOrderSize("budget");
});
document.getElementById("order_size_quote").addEventListener("input", () => syncBudgetAndOrderSize("size"));
document.getElementById("levels").addEventListener("input", () => syncBudgetAndOrderSize("levels"));
bindMaxDecimalInput("lower_price");
bindMaxDecimalInput("upper_price");
bindMaxDecimalInput("order_size_quote");
bindMaxDecimalInput("quote_budget");
bindMaxDecimalInput("skim_ratio");

// ──────────────────────────────────────────────────────────────
// Bot list
// ──────────────────────────────────────────────────────────────

function buildAgentCellHtml(bot, candidateAgents, isViewer) {
  const assignedId = bot.assigned_agent_id || "";
  const label = assignedId ? assignedId.slice(0, 8) : "-";
  if (isViewer || candidateAgents.length === 0) {
    return label;
  }
  const options = candidateAgents
    .map((agent) => {
      const short = agent.id.slice(0, 8);
      const isCurrent = agent.id === assignedId;
      const isUnavailable = agent.status !== "online" || agent.approval_status !== "approved";
      const disabled = isCurrent || isUnavailable;
      const suffix = isUnavailable ? " (offline)" : "";
      return `<button type="button" class="agent-move-item${isCurrent ? " current" : ""}" data-move-bot-id="${bot.id}" data-target-agent-id="${agent.id}" data-bot-name="${bot.name}" ${disabled ? "disabled" : ""}>${isCurrent ? "✓ " : ""}${short}${suffix}</button>`;
    })
    .join("");
  return `<div class="agent-move-dropdown" data-bot-id="${bot.id}"><span class="agent-move-pill">${label}<span class="agent-move-caret">▾</span></span><div class="agent-move-menu">${options}</div></div>`;
}

function _modeLabel(bot) {
  return bot?.mode === "live" ? "Live" : "Sim";
}

function _statusBadgeHtml(bot) {
  if (bot.status === "initializing") {
    return `<span class="status-badge status-initializing">${t("lbl_initializing")}</span>`;
  }
  if (bot.status === "running") {
    return `<span class="status-badge status-running">${t("lbl_running")}</span>`;
  }
  if (bot.status === "queued") {
    return `<span class="status-badge status-queued">${t("lbl_queued")}</span>`;
  }
  if (bot.status === "stopped") {
    return `<span class="status-badge status-stopped">${t("lbl_stopped")}</span>`;
  }
  return String(bot.status || "-");
}

function _renderBotDetailModal(bot) {
  const title = document.getElementById("bot_detail_title");
  const content = document.getElementById("bot_detail_content");
  if (!content) return;

  const metrics = bot?.latest_metrics || {};
  const grid = bot?.config?.grid || {};
  const budget = bot?.config?.budget || {};
  const sectionGeneral = t("bot_detail_section_general");
  const sectionBudget = t("lbl_budget");
  const sectionGrid = t("lbl_grid");
  const sectionPerformance = t("bot_detail_section_performance");

  const rows = [];
  const addRow = (label, value) => {
    rows.push(`<div class="bd-row"><span class="bd-label">${label}</span><span class="bd-value">${value}</span></div>`);
  };

  rows.push(`<h4 class="bot-detail-section">${sectionGeneral}</h4>`);
  rows.push("<div class=\"bot-detail-grid\">");
  addRow(t("th_name"), String(bot?.name || "-"));
  addRow(t("th_market"), String(bot?.config?.market || "-"));
  addRow(t("th_mode"), _modeLabel(bot));
  addRow(t("th_status"), _statusBadgeHtml(bot));
  addRow(t("th_agent"), String(bot?.assigned_agent_id || "-"));
  rows.push("</div>");

  rows.push(`<h4 class="bot-detail-section">${sectionBudget}</h4>`);
  rows.push("<div class=\"bot-detail-grid\">");
  addRow(t("lbl_quote_budget"), formatNumber(Number(budget.quote_budget || 0)));
  addRow(t("bot_detail_base_budget"), formatNumber(Number(budget.base_budget || 0)));
  addRow(t("lbl_profit_mode"), String(budget.profit_mode || "-"));
  if (String(budget.profit_mode || "").toLowerCase() === "skim") {
    addRow(t("lbl_skim_ratio"), budget.skim_ratio != null ? formatNumber(Number(budget.skim_ratio || 0), 4, 4) : "-");
  }
  rows.push("</div>");

  rows.push(`<h4 class="bot-detail-section">${sectionGrid}</h4>`);
  rows.push("<div class=\"bot-detail-grid\">");
  addRow(t("lbl_lower"), formatNumber(Number(grid.lower_price || 0)));
  addRow(t("lbl_upper"), formatNumber(Number(grid.upper_price || 0)));
  addRow(t("lbl_levels"), Number.isFinite(Number(grid.levels)) ? String(Number(grid.levels)) : "-");
  addRow(t("lbl_order_size"), formatNumber(Number(grid.order_size_quote || 0)));
  rows.push("</div>");

  rows.push(`<h4 class="bot-detail-section">${sectionPerformance}</h4>`);
  rows.push("<div class=\"bot-detail-grid\">");
  addRow(t("th_last_price"), Number(metrics.price || 0) > 0 ? formatNumber(Number(metrics.price || 0)) : "-");
  addRow(t("th_equity"), formatNumber(Number(metrics.total_equity_quote || 0)));
  addRow(t("th_pnl"), formatPnlWithPercent(Number(metrics.dashboard_pnl_quote ?? metrics.realized_pnl_quote ?? 0), Number(budget.quote_budget || 0)));
  addRow(t("th_trades"), String(Number(metrics.trade_count || 0)));
  addRow(t("th_runtime"), formatUptime(Number(metrics.runtime_seconds || 0)));
  rows.push("</div>");

  if (title) {
    title.textContent = `${t("bot_detail_title")} - ${String(bot?.name || "")}`;
  }
  content.innerHTML = rows.join("");
}

function openBotDetailModal(bot) {
  const modal = document.getElementById("bot_detail_modal");
  if (!modal) return;

  activeBotDetailId = String(bot?.id || "");
  _renderBotDetailModal(bot);
  modal.showModal();
}

/**
 * Fetch all bots from the API and render them into the bots table.
 * Start/Stop buttons are hidden for viewer-role users.
 */
async function loadBots() {
  wireBotMarketTooltips();
  const bots = await api("/api/v1/bots");
  latestBots = Array.isArray(bots) ? bots : [];
  const body = document.getElementById("bots_body");
  const isViewer = currentUser?.role === "viewer";

  // Populate the equity chart bot selector (preserve selection)
  const chartSelect = document.getElementById("equity_chart_bot");
  if (chartSelect) {
    const savedVal = localStorage.getItem("cryptobot_equity_chart_bot") || "";
    const currentVal = chartSelect.value || savedVal || (bots.length ? "__total__" : "");
    chartSelect.innerHTML = `<option value="">${t("lbl_select_bot")}</option><option value="__total__"${currentVal === "__total__" ? " selected" : ""}>${t("lbl_total_all_bots")}</option>`;
    for (const bot of bots) {
      const opt = document.createElement("option");
      opt.value = bot.id;
      opt.textContent = bot.name;
      if (bot.id === currentVal) opt.selected = true;
      chartSelect.appendChild(opt);
    }
    if (![...chartSelect.options].some((opt) => opt.value === currentVal)) {
      chartSelect.value = bots.length ? "__total__" : "";
    }
    localStorage.setItem("cryptobot_equity_chart_bot", chartSelect.value || "");
    refreshAppSelect(chartSelect);
  }

  // Track which bot IDs are in the new data
  const newBotIds = new Set(bots.map((b) => b.id));

  // Remove rows for bots that no longer exist
  body.querySelectorAll("tr[data-bot-id]").forEach((tr) => {
    if (!newBotIds.has(tr.dataset.botId)) tr.remove();
  });

  for (const bot of bots) {
    const m = bot.latest_metrics || {};
    const lastPrice = Number(m.price || 0);
    const runtime = formatUptime(m.runtime_seconds || 0);
    const market = bot.config?.market || "-";
    const lowerPrice = Number(bot.config?.grid?.lower_price || 0);
    const upperPrice = Number(bot.config?.grid?.upper_price || 0);
    const isOutsideGrid = lastPrice > 0 && lowerPrice > 0 && upperPrice > 0 && (lastPrice < lowerPrice || lastPrice > upperPrice);
    const nameHtml = isOutsideGrid
      ? `<span class="bot-name-cell bot-grid-outside">${bot.name}<span class="bot-grid-warning" title="${t("bot_grid_warning")}">&#9888;</span></span>`
      : bot.name;

    const statusHtml = _statusBadgeHtml(bot);

    let tr = body.querySelector(`tr[data-bot-id="${bot.id}"]`);
    if (tr) {
      // Update existing row cells in-place (skip actions column to preserve dropdown state)
      const cells = tr.children;
      cells[0].innerHTML = nameHtml;
      const safeMarket = String(market || "-");
      cells[1].innerHTML = `<span class="bot-market-cell">${safeMarket}</span>`;
      enrichMarketCell(cells[1].querySelector(".bot-market-cell"), safeMarket, m);
      cells[2].innerHTML = statusHtml;
      cells[3].textContent = runtime;
      // cells[4] is the actions dropdown — leave it untouched
    } else {
      // Create new row
      tr = document.createElement("tr");
      tr.dataset.botId = bot.id;
      const acts = isViewer
        ? "<td>-</td>"
        : `<td><div class="action-dropdown" data-bot-id="${bot.id}"><button class="action-toggle">${t("btn_actions")} ▾</button><div class="action-menu"><button data-action="start">${t("btn_start")}</button><button data-action="stop">${t("btn_stop")}</button><button data-action="sync">${t("btn_sync_exchange")}</button><button data-action="chart">${t("btn_chart")}</button><button data-action="orders">${t("btn_orders")}</button><button data-action="bot_logs">${t("btn_bot_log")}</button><button data-action="delete" class="danger">${t("btn_delete")}</button></div></div></td>`;
      const safeMarket = String(market || "-");
      tr.innerHTML = `<td>${nameHtml}</td><td><span class="bot-market-cell">${safeMarket}</span></td><td>${statusHtml}</td><td>${runtime}</td>${acts}`;
      enrichMarketCell(tr.querySelector(".bot-market-cell"), safeMarket, m);
      body.appendChild(tr);
      _wireUpBotRow(tr);
    }
  }

  const modal = document.getElementById("bot_detail_modal");
  if (modal?.open && activeBotDetailId) {
    const updatedBot = latestBots.find((bot) => bot.id === activeBotDetailId);
    if (updatedBot) {
      _renderBotDetailModal(updatedBot);
    } else {
      modal.close();
      activeBotDetailId = "";
    }
  }
}

/** Wire action dropdown handlers for a single bot row. */
function _wireUpBotRow(tr) {
  const isViewer = currentUser?.role === "viewer";

  const resolveDeleteErrorMessage = (err, deleteMode) => {
    const raw = String(err?.message || err || "");
    const normalized = raw.toLowerCase();

    if (deleteMode === "delete_open_orders") {
      if (normalized.includes("no assigned agent") && normalized.includes("cancel open orders")) {
        return lang === "nl"
          ? "Verwijderen gestopt: manager kan geen actieve host-agent voor deze bot vinden om alleen de bot-orders op de exchange te annuleren."
          : "Delete stopped: manager cannot find a hosting agent for this bot to cancel only this bot's exchange orders.";
      }
      if (normalized.includes("delete preparation failed")) {
        return lang === "nl"
          ? "Verwijderen gestopt: voorbereiding op de agent voor bot-specifieke order-cancel is mislukt."
          : "Delete stopped: agent-side preparation for bot-scoped order cancellation failed.";
      }
    }

    return raw;
  };

  tr.addEventListener("click", (e) => {
    const target = e.target;
    const targetEl = target instanceof Element ? target : null;
    if (!targetEl) return;
    if (targetEl.closest(".action-dropdown") || targetEl.closest(".agent-move-dropdown")) {
      return;
    }
    const botId = tr.dataset.botId;
    const bot = latestBots.find((x) => x.id === botId);
    if (!bot) return;
    openBotDetailModal(bot);
  });

  if (isViewer) return;

  tr.querySelectorAll(".action-toggle").forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      const menu = btn.nextElementSibling;
      document.querySelectorAll(".action-menu.open").forEach((m) => {
        if (m !== menu) m.classList.remove("open");
      });
      // Position the fixed menu below the toggle button
      const rect = btn.getBoundingClientRect();
      menu.style.top = (rect.bottom + 4) + "px";
      menu.style.right = (window.innerWidth - rect.right) + "px";
      menu.classList.toggle("open");
    };
  });
  tr.querySelectorAll(".action-menu button").forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const action = btn.dataset.action;
      const dropdown = btn.closest(".action-dropdown");
      const botId = dropdown.dataset.botId;
      const bot = latestBots.find((x) => x.id === botId);
      btn.closest(".action-menu").classList.remove("open");
      if (!action || !bot) return;
      let chosenDeleteMode = "";

      try {
        if (action === "start") {
          await api(`/api/v1/bots/${botId}/start`, { method: "POST", body: JSON.stringify({}) });
        } else if (action === "stop") {
          await api(`/api/v1/bots/${botId}/stop`, { method: "POST" });
        } else if (action === "sync") {
          await api(`/api/v1/bots/${botId}/sync`, { method: "POST" });
          showToast(t("btn_sync_exchange"), t("toast_sync_done"), "info", 3000);
        } else if (action === "chart") {
          openTradeChart(bot);
          return;
        } else if (action === "orders") {
          openOrdersModal(bot);
          return;
        } else if (action === "bot_logs") {
          openLogsModal(bot.assigned_agent_id || "", bot.id, bot.name);
          await loadAgentLogs();
          return;
        } else if (action === "delete") {
          const deleteMode = await showDeleteBotModeModal(bot.name || botId);
          if (!deleteMode) return;
          chosenDeleteMode = String(deleteMode || "");
          await api(`/api/v1/bots/${botId}`, {
            method: "DELETE",
            body: JSON.stringify({ delete_mode: deleteMode }),
          });
        }
      } catch (err) {
        const displayMessage = action === "delete"
          ? resolveDeleteErrorMessage(err, chosenDeleteMode)
          : String(err?.message || err);
        showToast(t("btn_" + action) || action, displayMessage, "warn", 5000);
      }
      await loadBots();
      await loadAgents();
      await loadOrders();
      await loadTradeEvents();
      await loadEquityChart();
      await loadMarketSummary();
    };
  });
}

// Close menus on outside click (register once)
document.addEventListener("click", (e) => {
  const target = e.target;
  const targetEl = target instanceof Element ? target : null;
  closeAllAppSelects();
  document.querySelectorAll(".action-menu.open").forEach((m) => m.classList.remove("open"));
  document.querySelectorAll(".agent-move-dropdown.open").forEach((m) => m.classList.remove("open"));
  if (!targetEl?.closest(".combo-wrap")) {
    closeMarketSuggestions();
  }
});

document.addEventListener("click", async (e) => {
  const target = e.target;
  const targetEl = target instanceof Element ? target : null;
  const trigger = targetEl?.closest(".agent-move-pill");
  if (trigger) {
    const wrap = trigger.closest(".agent-move-dropdown");
    if (!wrap) return;
    e.stopPropagation();
    document.querySelectorAll(".agent-move-dropdown.open").forEach((m) => {
      if (m !== wrap) m.classList.remove("open");
    });
    wrap.classList.toggle("open");
    return;
  }

  const moveBtn = targetEl?.closest(".agent-move-item");
  if (!moveBtn || moveBtn.disabled) return;
  e.stopPropagation();
  const botId = moveBtn.dataset.moveBotId;
  const targetAgentId = moveBtn.dataset.targetAgentId;
  const botName = moveBtn.dataset.botName || botId;
  if (!botId || !targetAgentId) return;

  try {
    await api(`/api/v1/bots/${botId}/move`, {
      method: "POST",
      body: JSON.stringify({ agent_id: targetAgentId }),
    });
    showToast(t("th_agent"), `${botName} moved to ${targetAgentId.slice(0, 8)}`, "info", 3500);
    await loadBots();
    await loadAgents();
    await loadEvents();
  } catch (err) {
    showToast(t("th_agent"), err.message || String(err), "warn", 4500);
  }
});

window.addEventListener("resize", () => {
  closeAllAppSelects();
  closeMarketSuggestions();
});

document.addEventListener("scroll", (e) => {
  const target = e.target;
  const targetEl = target instanceof Element ? target : null;
  if (targetEl?.closest(".combo-wrap") || targetEl?.closest("#market_suggestions")) {
    return;
  }
  if (targetEl?.closest(".modal") || target === document) {
    closeAllAppSelects();
    closeMarketSuggestions();
  }
}, true);

// ──────────────────────────────────────────────────────────────
// Toast notifications
// ──────────────────────────────────────────────────────────────

/**
 * Display a toast notification that auto-dismisses after a delay.
 *
 * @param {string} title   - Bold heading text.
 * @param {string} message - Body text.
 * @param {"info"|"warn"} [type="info"] - Visual style.
 * @param {number} [duration=5000]      - Time in ms before removal.
 * @param {Function} [onclick]          - Optional click handler.
 */
function showToast(title, message, type = "info", duration = 5000, onclick = null) {
  const container = document.getElementById("toast_container");
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.innerHTML = `<strong>${title}</strong><span>${message}</span>`;
  if (onclick) {
    el.style.cursor = "pointer";
    el.onclick = () => { onclick(); el.remove(); };
  }
  el.style.animationDuration = "0.3s, 0.4s";
  el.style.animationDelay = `0s, ${duration - 400}ms`;
  container.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

// ──────────────────────────────────────────────────────────────
// Agent list
// ──────────────────────────────────────────────────────────────

/**
 * Fetch all agents from the API and render them into the agents table.
 * Action buttons (approve / reject / unapprove / logs) are hidden for viewers,
 * except "Open logs" which is always visible for approved agents.
 */
async function loadAgents() {
  const agents = await api("/api/v1/agents");
  const body = document.getElementById("agents_body");
  const isViewer = currentUser?.role === "viewer";

  // Remember which agents are expanded
  const expandedIds = new Set();
  body.querySelectorAll("tr.expandable-row").forEach((tr) => {
    const detailRow = tr.nextElementSibling;
    if (detailRow && detailRow.classList.contains("bot-detail-row") && detailRow.style.display !== "none") {
      expandedIds.add(tr.dataset.agentId);
    }
  });

  // Track new agent IDs
  const newAgentIds = new Set(agents.map((a) => a.id));

  // Remove rows for agents no longer present
  body.querySelectorAll("tr[data-agent-id]").forEach((tr) => {
    if (!newAgentIds.has(tr.dataset.agentId)) {
      // Also remove the detail row if present
      const next = tr.nextElementSibling;
      if (next && next.classList.contains("bot-detail-row")) next.remove();
      tr.remove();
    }
  });

  for (const agent of agents) {
    const isDead = agent.status === "offline";
    const isRunning = agent.status === "online" && agent.approval_status === "approved" && (agent.bot_count ?? 0) > 0;
    const isStopped = agent.status === "stopped";
    let displayStatus;
    if (isDead) {
      displayStatus = '<span style="color:#ef4444">dead</span>';
    } else if (isStopped) {
      displayStatus = `online (<span style="color:#ef4444">${t("lbl_stopped")}</span>)`;
    } else if (isRunning) {
      displayStatus = `online (<span style="color:#10b981">${t("lbl_running")}</span>)`;
    } else if (agent.status === "online") {
      displayStatus = 'online';
    } else {
      displayStatus = agent.status;
    }

    let ah = "<span>-</span>";
    if (isDead) {
      if (!isViewer)
        ah = `<div class="bot-actions"><button class="icon-btn icon-remove" data-agent-remove="${agent.id}" title="${t("btn_remove")}"><span class="remove-x">✕</span></button></div>`;
    } else if (isViewer) {
      if (agent.approval_status === "approved")
        ah = `<button class="icon-btn" data-logs="${agent.id}" title="${t("btn_open_logs")}">📋</button>`;
    } else if (agent.approval_status === "pending") {
      ah = `<div class="bot-actions"><button class="btn-approve" data-approve="${agent.id}">${t("btn_approve")}</button><button class="btn-reject" data-reject="${agent.id}">${t("btn_reject")}</button></div>`;
    } else if (agent.approval_status === "approved") {
      ah = `<div class="bot-actions">`
        + (isStopped ? `<button class="icon-btn icon-start" data-agent-start="${agent.id}" title="${t("btn_start")}">▶</button>`
                     : `<button class="icon-btn icon-stop" data-agent-stop="${agent.id}" title="${t("btn_stop")}">⏹</button>`)
        + `<button class="icon-btn" data-logs="${agent.id}" title="${t("btn_open_logs")}">📋</button>`
        + `<button class="icon-btn icon-remove" data-agent-remove="${agent.id}" title="${t("btn_remove")}"><span class="remove-x">✕</span></button>`
        + `</div>`;
    } else if (agent.approval_status === "rejected") {
      ah = `<button class="btn-approve" data-approve="${agent.id}">${t("btn_approve")}</button>`;
    }

    const botCount = agent.bot_count ?? 0;
    const version = agent.version || "-";
    const address = agent.base_url ? agent.base_url.replace(/^https?:\/\//, "") : "-";
    const uptime = formatUptime(agent.uptime_seconds || 0);
    const heartbeat = agent.last_heartbeat ? timeAgo(new Date(agent.last_heartbeat)) : "-";
    const heartbeatAttr = agent.last_heartbeat ? ` data-heartbeat="${agent.last_heartbeat}"` : "";
    const hasExpand = botCount > 0;

    let tr = body.querySelector(`tr[data-agent-id="${agent.id}"]`);
    let detailTr = tr ? tr.nextElementSibling : null;
    if (detailTr && !detailTr.classList.contains("bot-detail-row")) detailTr = null;

    const wasExpanded = expandedIds.has(agent.id);

    if (tr) {
      // Update cells in-place
      const classes = [hasExpand ? "expandable-row" : "", isDead ? "dead-row" : ""].filter(Boolean).join(" ");
      tr.className = classes;
      const cells = tr.children;
      cells[0].innerHTML = `${hasExpand ? '<span class="expand-arrow">' + (wasExpanded ? '▼' : '▶') + '</span> ' : ""}${agent.id}`;
      cells[1].textContent = address;
      cells[2].innerHTML = displayStatus;
      cells[3].textContent = botCount;
      cells[4].textContent = version;
      cells[5].textContent = uptime;
      cells[6].textContent = heartbeat;
      if (agent.last_heartbeat) cells[6].dataset.heartbeat = agent.last_heartbeat;
      cells[7].textContent = agent.approval_status;
      cells[8].innerHTML = ah;

      // Update detail row content
      if (hasExpand) {
        if (!detailTr) {
          detailTr = document.createElement("tr");
          detailTr.className = "bot-detail-row";
          detailTr.style.display = wasExpanded ? "table-row" : "none";
          tr.after(detailTr);
        }
        detailTr.innerHTML = `<td colspan="9">${_buildAgentBotTable(agent)}</td>`;
        detailTr.style.display = wasExpanded ? "table-row" : "none";
      } else if (detailTr) {
        detailTr.remove();
        detailTr = null;
      }
    } else {
      // Create new row
      tr = document.createElement("tr");
      tr.dataset.agentId = agent.id;
      const classes = [hasExpand ? "expandable-row" : "", isDead ? "dead-row" : ""].filter(Boolean).join(" ");
      tr.className = classes;
      tr.innerHTML = `<td>${hasExpand ? '<span class="expand-arrow">▶</span> ' : ""}${agent.id}</td><td>${address}</td><td>${displayStatus}</td><td>${botCount}</td><td>${version}</td><td>${uptime}</td><td${heartbeatAttr}>${heartbeat}</td><td>${agent.approval_status}</td><td>${ah}</td>`;
      body.appendChild(tr);

      if (hasExpand) {
        detailTr = document.createElement("tr");
        detailTr.className = "bot-detail-row";
        detailTr.style.display = "none";
        detailTr.innerHTML = `<td colspan="9">${_buildAgentBotTable(agent)}</td>`;
        body.appendChild(detailTr);
      }
    }

    // Wire expand toggle
    if (hasExpand && tr && detailTr) {
      const finalDetailTr = detailTr;
      tr.onclick = (e) => {
        if (e.target.closest("button")) return;
        const arrow = tr.querySelector(".expand-arrow");
        const open = finalDetailTr.style.display !== "none";
        finalDetailTr.style.display = open ? "none" : "table-row";
        arrow.textContent = open ? "▶" : "▼";
      };
    }

    // Wire action buttons for this row
    _wireUpAgentRow(tr, agent);
  }
}

/** Build the sub-table HTML for bots under an agent. */
function _buildAgentBotTable(agent) {
  let html = `<table class="sub-table"><thead><tr><th>${t("th_name")}</th><th>${t("th_market")}</th><th>${t("th_status")}</th><th>${t("th_trades")}</th><th>Runtime</th><th>${t("th_quote_balance")}</th><th>${t("th_base_balance")}</th></tr></thead><tbody>`;
  for (const bot of agent.bots) {
    const runtimeSeconds = bot.runtime_seconds ?? bot.latest_metrics?.runtime_seconds ?? 0;
    const runtime = formatUptime(runtimeSeconds);
    const tradeCount = bot.trade_count ?? bot.latest_metrics?.trade_count ?? 0;
    const quoteBalance = bot.quote_balance ?? bot.latest_metrics?.quote_balance ?? null;
    const baseBalance = bot.base_balance ?? bot.latest_metrics?.base_balance ?? null;
    html += `<tr><td>${bot.name}</td><td>${bot.market}</td><td>${bot.status}</td><td>${tradeCount}</td><td>${runtime}</td><td>${formatNumber(quoteBalance)}</td><td>${formatNumber(baseBalance)}</td></tr>`;
  }
  html += `</tbody></table>`;
  return html;
}

/** Wire action button handlers for a single agent row. */
function _wireUpAgentRow(tr, agent) {
  tr.querySelectorAll("button[data-approve]").forEach((b) => {
    b.onclick = async () => { await api(`/api/v1/agents/${b.dataset.approve}/approve`, { method: "POST" }); await loadAgents(); await loadEvents(); };
  });
  tr.querySelectorAll("button[data-reject]").forEach((b) => {
    b.onclick = async () => { await api(`/api/v1/agents/${b.dataset.reject}/reject`, { method: "POST" }); await loadAgents(); await loadEvents(); };
  });
  tr.querySelectorAll("button[data-agent-start]").forEach((b) => {
    b.onclick = async () => { await api(`/api/v1/agents/${b.dataset.agentStart}/approve`, { method: "POST" }); await loadAgents(); await loadEvents(); };
  });
  tr.querySelectorAll("button[data-agent-stop]").forEach((b) => {
    b.onclick = async () => { await api(`/api/v1/agents/${b.dataset.agentStop}/stop`, { method: "POST" }); await loadAgents(); await loadEvents(); };
  });
  tr.querySelectorAll("button[data-agent-remove]").forEach((b) => {
    b.onclick = async () => {
      if (!await showConfirm(t("confirm_remove_agent"))) return;
      await api(`/api/v1/agents/${b.dataset.agentRemove}`, { method: "DELETE" });
      if (selectedAgentId === b.dataset.agentRemove) { selectedAgentId = null; closeLogsModal(); }
      await loadAgents(); await loadEvents();
    };
  });
  tr.querySelectorAll("button[data-logs]").forEach((b) => {
    b.onclick = async () => { selectedAgentId = b.dataset.logs; openLogsModal(selectedAgentId); await loadAgentLogs(); };
  });
}

// ──────────────────────────────────────────────────────────────
// Agent logs modal
// ──────────────────────────────────────────────────────────────

/**
 * Open the logs modal for a specific agent.
 *
 * @param {string} agentId - The agent whose logs to display.
 * @param {string|null} botId - Optional bot id to filter logs.
 * @param {string} botName - Optional bot name for the modal title.
 */
function refreshLogsModalTitle() {
  const titleEl = document.querySelector("#agent_logs_modal .modal-title");
  if (!titleEl) return;
  titleEl.textContent = selectedLogBotId
    ? `${t("bot_logs_title")}: ${selectedLogBotName || selectedLogBotId}`
    : t("logs_title");
}

function openLogsModal(agentId, botId = null, botName = "") {
  logsModalOpen = true;
  selectedAgentId = agentId;
  selectedLogBotId = botId;
  selectedLogBotName = botName || "";
  if (botId) {
    const categoryEl = document.getElementById("modal_log_category");
    if (categoryEl) categoryEl.value = "";
  }
  document.getElementById("modal_selected_agent_id").value = agentId;
  refreshLogsModalTitle();
  document.getElementById("agent_logs_modal").showModal();
}

/** Close the agent-logs modal and stop polling. */
function closeLogsModal() {
  logsModalOpen = false;
  selectedLogBotId = null;
  selectedLogBotName = "";
  document.getElementById("agent_logs_modal").close();
}

/**
 * Fetch logs for the selected agent (with category and limit filters)
 * and render them into the logs modal. Newest entries appear at the bottom.
 */
async function loadAgentLogs() {
  const list = document.getElementById("modal_agent_logs_list");
  if (!selectedAgentId && !selectedLogBotId) { list.innerHTML = `<div class="log-item">${t("logs_no_agent")}</div>`; return; }

  const limit = Number(document.getElementById("modal_agent_logs_limit").value || "200");
  const categoryEl = document.getElementById("modal_log_category");
  let category = categoryEl.value;
  if (category === "trading") {
    category = "";
    categoryEl.value = "";
  }
  const qs = new URLSearchParams({ limit: String(Math.max(1, Math.min(limit, 1000))) });
  if (category === "system") qs.set("category", category);

  try {
    const url = selectedLogBotId
      ? `/api/v1/bots/${selectedLogBotId}/logs?${qs.toString()}`
      : `/api/v1/agents/${selectedAgentId}/logs?${qs.toString()}`;
    const payload = await api(url);
    if (payload?.agent_id) {
      selectedAgentId = payload.agent_id;
      document.getElementById("modal_selected_agent_id").value = payload.agent_id;
    }
    const logs = payload.logs || [];
    if (!logs.length) { list.innerHTML = `<div class="log-item">${t("logs_none")}</div>`; return; }

    // Preserve scroll position if user has scrolled up
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;

    list.innerHTML = "";
    // Reverse so newest is at the bottom (natural scroll direction)
    for (const log of [...logs].reverse()) {
      const item = document.createElement("div");
      item.className = "log-item";
      const timeLabel = new Date(log.timestamp).toLocaleString();
      const botLabel = log.bot_id ? ` | bot ${log.bot_id}` : "";
      item.textContent = `${timeLabel}  [${log.event_type}]${botLabel}  ${log.message}`;
      list.appendChild(item);
    }
    if (atBottom) list.scrollTop = list.scrollHeight;
  } catch (err) {
    list.innerHTML = `<div class="log-item">${t("logs_error")}: ${String(err.message || err)}</div>`;
  }
}

// ──────────────────────────────────────────────────────────────
// Diagnostics tab (instances + debug logs)
// ──────────────────────────────────────────────────────────────

function buildDiagnosticsQuery() {
  const params = new URLSearchParams();
  params.set("kind", "debug");

  const instanceType = String(document.getElementById("diag_filter_instance_type")?.value || "").trim();
  const instanceId = String(document.getElementById("diag_filter_instance_id")?.value || "").trim();
  const component = String(document.getElementById("diag_filter_component")?.value || "").trim();
  const limit = Number(document.getElementById("diag_filter_limit")?.value || 500);

  if (instanceType) params.set("instance_type", instanceType);
  if (instanceId) params.set("instance_id", instanceId);
  if (component) params.set("component", component);
  params.set("limit", String(Math.max(10, Math.min(limit, 5000))));
  return params;
}

async function loadDiagnosticsInstances() {
  const body = document.getElementById("diag_instances_body");
  const retentionInfo = document.getElementById("diag_retention_info");
  if (!body) return;

  try {
    const payload = await api("/api/v1/debug/instances");
    const instances = payload.instances || [];
    const retentionHours = Number(payload.retention_hours || 48);
    if (retentionInfo) retentionInfo.textContent = `Retention: ${retentionHours}h`;
    body.innerHTML = "";

    for (const instance of instances) {
      const tr = document.createElement("tr");
      const firstSeen = instance.first_seen ? new Date(instance.first_seen).toLocaleString() : "-";
      const lastSeen = instance.last_seen ? new Date(instance.last_seen).toLocaleString() : "-";
      tr.innerHTML = `<td>${instance.instance_type || "-"}</td><td>${instance.instance_id || "-"}</td><td>${instance.status || "-"}</td><td>${firstSeen}</td><td>${lastSeen}</td><td>${instance.source || "-"}</td>`;
      body.appendChild(tr);
    }
  } catch (err) {
    body.innerHTML = `<tr><td colspan="6">${t("logs_error")}: ${String(err.message || err)}</td></tr>`;
  }
}

async function loadDiagnosticsLogs() {
  const list = document.getElementById("diag_debug_logs");
  if (!list) return;
  const qs = buildDiagnosticsQuery();
  const maxInlineJsonChars = 1200;

  function stringifyForInline(value) {
    try {
      const text = JSON.stringify(value);
      if (text.length <= maxInlineJsonChars) return text;
      return `${text.slice(0, maxInlineJsonChars)}… [truncated in UI; full payload available in download]`;
    } catch {
      return "[unserializable]";
    }
  }

  try {
    const payload = await api(`/api/v1/debug/logs?${qs.toString()}`);
    const logs = payload.logs || [];
    list.innerHTML = "";
    if (!logs.length) {
      list.textContent = t("logs_none");
      return;
    }

    for (const log of logs) {
      const line = document.createElement("div");
      line.className = "diag-log-item";
      const ts = log.timestamp ? new Date(log.timestamp).toLocaleString() : "-";
      const inst = `${log.instance_type || "-"}:${log.instance_id_resolved || log.instance_id || "-"}`;
      const corr = log.correlation_id ? ` | corr=${log.correlation_id}` : "";
      const comp = log.component ? ` | ${log.component}` : "";
      const fields = log.fields && typeof log.fields === "object" ? log.fields : {};
      const payload = fields.payload && typeof fields.payload === "object" ? fields.payload : null;
      const payloadText = payload ? ` | payload=${stringifyForInline(payload)}` : "";
      const fieldsWithoutPayload = { ...fields };
      delete fieldsWithoutPayload.payload;
      const fieldsText = Object.keys(fieldsWithoutPayload).length
        ? ` | fields=${stringifyForInline(fieldsWithoutPayload)}`
        : "";
      line.textContent = `${ts} [${log.event || "event"}] [${inst}]${comp}${corr} ${log.message || ""}${fieldsText}${payloadText}`;
      list.appendChild(line);
    }
    list.scrollTop = 0;
  } catch (err) {
    list.textContent = `${t("logs_error")}: ${String(err.message || err)}`;
  }
}

async function downloadDiagnosticsLogs() {
  const qs = buildDiagnosticsQuery();
  const resp = await fetch(`/api/v1/debug/logs/download?${qs.toString()}`, {
    headers: { Authorization: `Bearer ${authToken}` },
  });
  if (!resp.ok) throw new Error(await resp.text());
  const blob = await resp.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = "cryptobot_debug_logs.ndjson";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(href);
}

// ──────────────────────────────────────────────────────────────
// Settings tab
// ──────────────────────────────────────────────────────────────

async function loadSettingsPage() {
  const status = document.getElementById("settings_status");
  if (!document.getElementById("settings_fields")) return;

  if (status) status.textContent = t("settings_loading");
  try {
    const payload = await api("/api/v1/settings");
    settingsPayloadCache = {
      sections: Array.isArray(payload?.sections) ? payload.sections : [],
      exchanges: Array.isArray(payload?.exchanges) ? payload.exchanges : [],
    };
    _renderSettingsSubtab(activeSettingsSubtab);
    if (status) status.textContent = t("settings_loaded");
    settingsLoadedOnce = true;
  } catch (err) {
    if (status) status.textContent = `${t("settings_load_error")}: ${String(err?.message || err)}`;
  }
}

async function saveSettingsPage() {
  const status = document.getElementById("settings_status");
  const wrap = document.getElementById("settings_fields");
  if (!wrap) return;

  if (status) status.textContent = t("settings_saving");
  try {
    const updates = [...wrap.querySelectorAll("input[data-setting-id]")].map((input) => ({
      id: Number(input.getAttribute("data-setting-id") || 0),
      value: input.value,
    }));

    const payload = await api("/api/v1/settings", {
      method: "POST",
      body: JSON.stringify({
        items: updates,
      }),
    });
    if (status) status.textContent = String(payload?.message || t("settings_saved"));
    showToast(t("settings_title"), t("settings_saved"), "info", 3500);
    await loadSettingsPage();
  } catch (err) {
    if (status) status.textContent = `${t("settings_save_error")}: ${String(err?.message || err)}`;
    showToast(t("settings_title"), String(err?.message || err), "warn", 5000);
  }
}

function _settingsSectionBySource(source) {
  return (settingsPayloadCache.sections || []).find((section) => String(section?.source || "") === String(source || ""));
}

function _renderScalarSettingsSection(source) {
  const wrap = document.getElementById("settings_fields");
  const exchangesWrap = document.getElementById("settings_exchanges");
  const exchangeForm = document.getElementById("settings_exchange_form");
  if (!wrap) return;
  if (exchangesWrap) exchangesWrap.style.display = "none";
  if (exchangeForm) exchangeForm.style.display = "none";
  wrap.style.display = "block";

  const section = _settingsSectionBySource(source);
  const items = Array.isArray(section?.items) ? section.items : [];
  const html = `
    <div class="settings-group">
      <h3>${_notificationEscapeHtml(source)}</h3>
      <div class="settings-grid">
        ${items
          .map(
            (row) => `
              <label class="settings-field">
                <span class="settings-key-row">
                  <span class="settings-key">${_notificationEscapeHtml(String(row?.name || row?.key || ""))}</span>
                  <span class="settings-desc-tip" title="${_notificationEscapeHtml(String(row?.description || ""))}">i</span>
                </span>
                <input type="text" data-setting-id="${Number(row?.id || 0)}" value="${_notificationEscapeHtml(String(row?.value || ""))}" />
              </label>
            `
          )
          .join("")}
      </div>
    </div>
  `;
  wrap.innerHTML = html;
}

function _renderExchangeList() {
  const wrap = document.getElementById("settings_fields");
  const exchangesWrap = document.getElementById("settings_exchanges");
  const exchangeForm = document.getElementById("settings_exchange_form");
  if (!exchangesWrap) return;
  if (wrap) wrap.style.display = "none";
  exchangesWrap.style.display = "block";
  if (exchangeForm) exchangeForm.style.display = "block";

  const rows = Array.isArray(settingsPayloadCache.exchanges) ? settingsPayloadCache.exchanges : [];
  if (!rows.length) {
    exchangesWrap.innerHTML = `<div class="settings-group"><div class="diag-hint">${t("notif_no_data")}</div></div>`;
    return;
  }

  exchangesWrap.innerHTML = rows
    .map(
      (row) => `
        <div class="settings-group" data-exchange-id="${Number(row.id || 0)}">
          <div class="settings-exchange-header">
            <div>
              <h3>${_notificationEscapeHtml(String(row.name || ""))}</h3>
              <div class="diag-hint">${_notificationEscapeHtml(String(row.description || ""))}</div>
            </div>
            <div class="settings-exchange-actions">
              <button class="secondary" data-action="edit" data-exchange-id="${Number(row.id || 0)}">${t("settings_exchange_update")}</button>
              <button data-action="delete" data-exchange-id="${Number(row.id || 0)}">${t("settings_exchange_delete")}</button>
            </div>
          </div>
          <div class="settings-grid">
            <label class="settings-field"><span class="settings-key">${t("settings_exchange_base_url")}</span><input type="text" disabled value="${_notificationEscapeHtml(String(row.base_url || ""))}" /></label>
            <label class="settings-field"><span class="settings-key">${t("settings_exchange_ws_url")}</span><input type="text" disabled value="${_notificationEscapeHtml(String(row.ws_url || ""))}" /></label>
            <label class="settings-field"><span class="settings-key">${t("settings_exchange_endpoints_key")}</span><input type="text" disabled value="${_notificationEscapeHtml(String(row.endpoints_key || ""))}" /></label>
            <label class="settings-field"><span class="settings-key">${t("settings_exchange_secret")}</span><input type="text" disabled value="********" /></label>
            <label class="settings-field"><span class="settings-key">${t("settings_exchange_default_market")}</span><input type="text" disabled value="${_notificationEscapeHtml(String(row.default_market || ""))}" /></label>
          </div>
        </div>
      `
    )
    .join("");
}

function _resetExchangeForm() {
  editingExchangeId = null;
  ["exchange_name", "exchange_description", "exchange_base_url", "exchange_ws_url", "exchange_endpoints_key", "exchange_secret", "exchange_default_market"].forEach((id) => {
    const input = document.getElementById(id);
    if (input) input.value = "";
  });
  const addBtn = document.getElementById("settings_exchange_add_btn");
  const updateBtn = document.getElementById("settings_exchange_update_btn");
  const cancelBtn = document.getElementById("settings_exchange_cancel_btn");
  const title = document.getElementById("settings_exchange_form_title");
  if (addBtn) addBtn.style.display = "inline-flex";
  if (updateBtn) updateBtn.style.display = "none";
  if (cancelBtn) cancelBtn.style.display = "none";
  if (title) title.textContent = t("settings_exchange_add");
}

function _loadExchangeIntoForm(exchangeId) {
  const row = (settingsPayloadCache.exchanges || []).find((item) => Number(item?.id || 0) === Number(exchangeId || 0));
  if (!row) return;
  editingExchangeId = Number(row.id || 0);
  const mappings = {
    exchange_name: row.name || "",
    exchange_description: row.description || "",
    exchange_base_url: row.base_url || "",
    exchange_ws_url: row.ws_url || "",
    exchange_endpoints_key: row.endpoints_key || "",
    exchange_secret: row.secret || "",
    exchange_default_market: row.default_market || "",
  };
  Object.entries(mappings).forEach(([id, value]) => {
    const input = document.getElementById(id);
    if (input) input.value = String(value || "");
  });
  const addBtn = document.getElementById("settings_exchange_add_btn");
  const updateBtn = document.getElementById("settings_exchange_update_btn");
  const cancelBtn = document.getElementById("settings_exchange_cancel_btn");
  const title = document.getElementById("settings_exchange_form_title");
  if (addBtn) addBtn.style.display = "none";
  if (updateBtn) updateBtn.style.display = "inline-flex";
  if (cancelBtn) cancelBtn.style.display = "inline-flex";
  if (title) title.textContent = t("settings_exchange_update");
}

function _exchangeFormPayload() {
  return {
    name: document.getElementById("exchange_name")?.value || "",
    description: document.getElementById("exchange_description")?.value || "",
    base_url: document.getElementById("exchange_base_url")?.value || "",
    ws_url: document.getElementById("exchange_ws_url")?.value || "",
    endpoints_key: document.getElementById("exchange_endpoints_key")?.value || "",
    secret: document.getElementById("exchange_secret")?.value || "",
    default_market: document.getElementById("exchange_default_market")?.value || "",
    provider: "bitvavo",
  };
}

async function _createExchangeFromForm() {
  await api("/api/v1/settings/exchanges", {
    method: "POST",
    body: JSON.stringify(_exchangeFormPayload()),
  });
  _resetExchangeForm();
  await loadSettingsPage();
  showToast(t("settings_title"), t("settings_saved"), "info", 3000);
}

async function _updateExchangeFromForm() {
  if (!editingExchangeId) return;
  await api(`/api/v1/settings/exchanges/${editingExchangeId}`, {
    method: "PUT",
    body: JSON.stringify(_exchangeFormPayload()),
  });
  _resetExchangeForm();
  await loadSettingsPage();
  showToast(t("settings_title"), t("settings_saved"), "info", 3000);
}

async function _deleteExchange(exchangeId) {
  await api(`/api/v1/settings/exchanges/${Number(exchangeId || 0)}`, { method: "DELETE" });
  if (Number(editingExchangeId || 0) === Number(exchangeId || 0)) {
    _resetExchangeForm();
  }
  await loadSettingsPage();
}

function _renderSettingsSubtab(source) {
  activeSettingsSubtab = source || "General";
  document.querySelectorAll(".settings-subtab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.settingsTab === activeSettingsSubtab);
  });
  if (activeSettingsSubtab === "Exchange") {
    _renderExchangeList();
    return;
  }
  _renderScalarSettingsSection(activeSettingsSubtab);
}

function _bindSettingsSubtabs() {
  document.querySelectorAll(".settings-subtab-btn").forEach((btn) => {
    btn.onclick = () => {
      _renderSettingsSubtab(btn.dataset.settingsTab || "General");
    };
  });
}

// ──────────────────────────────────────────────────────────────
// Notification tables (balance/orders/trades)
// ──────────────────────────────────────────────────────────────

const _notificationOrderCache = new Map();
const _notificationTradeCache = new Map();

function _formatDateTime(value) {
  if (!value) return "-";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return String(value);
  return dt.toLocaleString();
}

function _formatDateTimeCompact(value) {
  if (!value) return "-";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return String(value);
  const dd = String(dt.getDate()).padStart(2, "0");
  const mm = String(dt.getMonth() + 1).padStart(2, "0");
  const yyyy = String(dt.getFullYear());
  const hh = String(dt.getHours()).padStart(2, "0");
  const min = String(dt.getMinutes()).padStart(2, "0");
  return `${dd}-${mm}-${yyyy} ${hh}:${min}`;
}

function _titleCaseWord(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (!raw) return "-";
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

function _normalizeSideValue(side) {
  return String(side || "").trim().toLowerCase();
}

function _valueWithCurrency(value, currency) {
  const unit = String(currency || "").trim();
  if (!unit) return formatNumber(value || 0);
  return `${formatNumber(value || 0)}<br>${_notificationEscapeHtml(unit)}`;
}

function _sideClass(side) {
  const normalized = _normalizeSideValue(side);
  if (normalized === "buy") return "notification-side-buy";
  if (normalized === "sell") return "notification-side-sell";
  return "";
}

function _renderNotificationEmpty(tbody, colSpan, messageKey = "notif_no_data") {
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="${colSpan}">${t(messageKey)}</td></tr>`;
}

function _notificationEscapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function _ensureRowCellCount(tr, count) {
  while (tr.children.length < count) {
    tr.appendChild(document.createElement("td"));
  }
  while (tr.children.length > count) {
    tr.lastElementChild?.remove();
  }
}

function _removePlaceholderRows(tbody, attrName) {
  tbody.querySelectorAll("tr").forEach((tr) => {
    if (!tr.hasAttribute(attrName)) tr.remove();
  });
}

function _syncRowsByKey(tbody, rows, attrName, keyFn, updateRow) {
  const existing = new Map();
  tbody.querySelectorAll(`tr[${attrName}]`).forEach((tr) => {
    const key = tr.getAttribute(attrName);
    if (key) existing.set(key, tr);
  });

  const ordered = [];
  const seen = new Set();
  rows.forEach((row, index) => {
    const key = String(keyFn(row, index) || "");
    if (!key || seen.has(key)) return;
    seen.add(key);

    let tr = existing.get(key);
    if (!tr) {
      tr = document.createElement("tr");
      tr.setAttribute(attrName, key);
    }
    updateRow(tr, row, key);
    ordered.push(tr);
  });

  existing.forEach((tr, key) => {
    if (!seen.has(key)) tr.remove();
  });

  ordered.forEach((tr, index) => {
    const anchor = tbody.children[index] || null;
    if (anchor !== tr) tbody.insertBefore(tr, anchor);
  });
}

function _openNotificationDetail(record, titleKey, infoTitleKey) {
  const modal = document.getElementById("notification_detail_modal");
  const title = document.getElementById("notification_detail_title");
  const content = document.getElementById("notification_detail_content");
  if (!modal || !content) return;

  title.textContent = t(infoTitleKey || titleKey);
  const totalValue = Number(record.total_amount ?? record.total ?? 0);
  const rows = [
    [t("lbl_date_created"), _formatDateTimeCompact(record.date_created)],
    [t("lbl_date_updated"), _formatDateTimeCompact(record.date_updated)],
    [t("th_order_type"), record.order_type || "-"],
    [t("th_side"), `<span class="${_sideClass(record.side)}">${_titleCaseWord(record.side)}</span>`],
    [t("th_status"), `<span class="notification-status">${_titleCaseWord(record.status)}</span>`],
    [t("th_amount"), _valueWithCurrency(record.amount || 0, record.base_currency)],
    [t("th_price"), _valueWithCurrency(record.price || 0, record.quote_currency)],
    [t("th_filled_amount"), _valueWithCurrency(record.filled_amount || 0, record.base_currency)],
    [t("th_total"), _valueWithCurrency(totalValue, record.quote_currency)],
  ];

  if (record.transaction_fee || record.fee) {
    rows.splice(8, 0, [t("th_fee"), _valueWithCurrency(record.transaction_fee || record.fee || 0, record.fee_currency || record.quote_currency)]);
  }
  rows.push([t("lbl_order_id"), record.order_id || record.id || "-"]);

  content.innerHTML = `
    <div class="notification-detail-grid">
      ${rows.map(([label, value]) => `<div class="od-label">${label}</div><div class="od-value">${value}</div>`).join("")}
    </div>
  `;
  modal.showModal();
}

async function loadNotificationBalance() {
  const tbody = document.getElementById("balance_body");
  if (!tbody) return;
  try {
    await loadCoinMap();
    const resp = await api("/api/v1/market/notifications/balance");
    const rows = Array.isArray(resp?.rows) ? resp.rows : [];
    dashboardBalanceByAsset.clear();
    rows.forEach((row) => {
      const asset = String(row?.asset || "").toUpperCase();
      if (!asset) return;
      dashboardBalanceByAsset.set(asset, {
        available: Number(row?.available_balance || 0),
        balance: Number(row?.balance || 0),
      });
    });
    if (!rows.length) {
      _renderNotificationEmpty(tbody, 7, "notif_no_balance_data");
      return;
    }
    _removePlaceholderRows(tbody, "data-notif-balance-id");
    _syncRowsByKey(
      tbody,
      rows,
      "data-notif-balance-id",
      (row) => String(row?.asset || "").toUpperCase(),
      (tr, row) => {
        const asset = String(row.asset || "-").toUpperCase();
        const iconPath = getAssetIconPath(asset);

        _ensureRowCellCount(tr, 7);
        const cells = tr.children;

        let assetCell = cells[0].querySelector(".notif-asset-cell");
        if (!assetCell) {
          assetCell = document.createElement("span");
          assetCell.className = "notif-asset-cell";
          const label = document.createElement("span");
          label.className = "notif-asset-label";
          assetCell.appendChild(label);
          cells[0].textContent = "";
          cells[0].appendChild(assetCell);
        }

        let icon = assetCell.querySelector("img.notif-asset-icon");
        if (iconPath) {
          if (!icon) {
            icon = document.createElement("img");
            icon.className = "notif-asset-icon";
            icon.alt = "";
            icon.loading = "lazy";
            icon.decoding = "async";
            assetCell.prepend(icon);
          }
          if (icon.getAttribute("src") !== iconPath) {
            icon.setAttribute("src", iconPath);
          }
        } else if (icon) {
          icon.remove();
        }

        const label = assetCell.querySelector(".notif-asset-label");
        if (label) label.textContent = asset;

        cells[1].textContent = formatNumber(row.price || 0);
        cells[2].className = Number(row.change_24h || 0) >= 0 ? "pnl-positive" : "pnl-negative";
        cells[2].textContent = `${Number(row.change_24h || 0) >= 0 ? "+" : ""}${formatNumber(row.change_24h || 0, 2, 2)}%`;
        cells[3].textContent = formatNumber(row.euro_value || 0);
        cells[4].textContent = formatNumber(row.balance || 0);
        cells[5].textContent = formatNumber(row.available_balance || 0);
        cells[6].textContent = formatNumber(row.in_orders || 0);
      }
    );
  } catch {
    _renderNotificationEmpty(tbody, 7, "notif_no_balance_data");
  }
}

async function loadOrderHistory() {
  const tbody = document.getElementById("order_history_body");
  if (!tbody) return;
  try {
    const bots = Array.isArray(latestBots) && latestBots.length ? latestBots : await api("/api/v1/bots");
    const markets = [...new Set((Array.isArray(bots) ? bots : [])
      .map((bot) => String(bot?.config?.market || "").trim().toUpperCase())
      .filter((market) => market.includes("-")))];
    const qs = new URLSearchParams();
    if (markets.length) qs.set("markets", markets.join(","));
    const url = qs.size ? `/api/v1/market/notifications/order-history?${qs.toString()}` : "/api/v1/market/notifications/order-history";
    const resp = await api(url);
    const rows = Array.isArray(resp?.rows) ? resp.rows : [];
    _notificationOrderCache.clear();
    rows.forEach((row) => {
      const id = row.id || `${row.order_id || ""}-${row.date_time || ""}`;
      _notificationOrderCache.set(id, row);
    });
    if (!rows.length) {
      _renderNotificationEmpty(tbody, 8);
      return;
    }
    _removePlaceholderRows(tbody, "data-notif-order-id");
    _syncRowsByKey(
      tbody,
      rows,
      "data-notif-order-id",
      (row) => row.id || `${row.order_id || ""}-${row.date_time || ""}`,
      (tr, row) => {
        _ensureRowCellCount(tr, 8);
        const cells = tr.children;
        cells[0].textContent = row.market || "-";
        cells[1].textContent = row.order_type || "-";
        cells[2].className = _sideClass(row.side);
        cells[2].textContent = String(row.side || "-").toUpperCase();
        cells[3].textContent = formatNumber(row.total || 0);
        cells[4].textContent = formatNumber(row.amount || 0);
        cells[5].textContent = formatNumber(row.price || 0);
        cells[6].className = "notification-status";
        cells[6].textContent = row.status || "-";
        cells[7].textContent = _formatDateTime(row.date_time);
      }
    );
  } catch {
    _renderNotificationEmpty(tbody, 8);
  }
}

/**
 * Backward-compatible entrypoint used throughout the app refresh flow.
 * Now loads balance + order history notification panes.
 */
async function loadEvents() {
  await Promise.all([loadNotificationBalance(), loadOrderHistory()]);
}

/**
 * Backward-compatible entrypoint used throughout the app refresh flow.
 * Now loads trade history notification pane.
 */
async function loadTradeEvents() {
  const tbody = document.getElementById("trade_history_body");
  if (!tbody) return;
  try {
    const resp = await api("/api/v1/market/notifications/trade-history");
    const rows = Array.isArray(resp?.rows) ? resp.rows : [];
    _notificationTradeCache.clear();
    rows.forEach((row) => {
      const id = row.id || `${row.order_id || ""}-${row.date_time || ""}`;
      _notificationTradeCache.set(id, row);
    });
    if (!rows.length) {
      _renderNotificationEmpty(tbody, 7);
      return;
    }
    _removePlaceholderRows(tbody, "data-notif-trade-id");
    _syncRowsByKey(
      tbody,
      rows,
      "data-notif-trade-id",
      (row) => row.id || `${row.order_id || ""}-${row.date_time || ""}`,
      (tr, row) => {
        _ensureRowCellCount(tr, 7);
        const cells = tr.children;
        cells[0].textContent = row.market || "-";
        cells[1].className = _sideClass(row.side);
        cells[1].textContent = String(row.side || "-").toUpperCase();
        cells[2].textContent = formatNumber(row.total || 0);
        cells[3].textContent = formatNumber(row.amount || 0);
        cells[4].textContent = formatNumber(row.price || 0);
        cells[5].textContent = formatNumber(row.fee || row.transaction_fee || 0);
        cells[6].textContent = _formatDateTime(row.date_time);
      }
    );
  } catch {
    _renderNotificationEmpty(tbody, 7);
  }
}

// ──────────────────────────────────────────────────────────────
// Notification tabs
// ──────────────────────────────────────────────────────────────

document.querySelectorAll(".ntab-btn").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".ntab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".ntab-pane").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.ntab)?.classList.add("active");
  };
});

document.getElementById("open_orders_body")?.addEventListener("click", (e) => {
  const target = e.target;
  const targetEl = target instanceof Element ? target : null;
  const row = targetEl?.closest("tr[data-notif-order-id]");
  if (!row) return;
  const order = _notificationOrderCache.get(row.dataset.notifOrderId || "");
  if (!order) return;
  _openNotificationDetail(order, "notif_detail_title_order", "lbl_order_information");
});

document.getElementById("order_history_body")?.addEventListener("click", (e) => {
  const target = e.target;
  const targetEl = target instanceof Element ? target : null;
  const row = targetEl?.closest("tr[data-notif-order-id]");
  if (!row) return;
  const order = _notificationOrderCache.get(row.dataset.notifOrderId || "");
  if (!order) return;
  _openNotificationDetail(order, "notif_detail_title_order", "lbl_order_information");
});

document.getElementById("trade_history_body")?.addEventListener("click", (e) => {
  const target = e.target;
  const targetEl = target instanceof Element ? target : null;
  const row = targetEl?.closest("tr[data-notif-trade-id]");
  if (!row) return;
  const trade = _notificationTradeCache.get(row.dataset.notifTradeId || "");
  if (!trade) return;
  _openNotificationDetail(trade, "notif_detail_title_trade", "lbl_trade_information");
});

// ──────────────────────────────────────────────────────────────
// Equity trend chart (pure Canvas 2D)
// ──────────────────────────────────────────────────────────────

let _equityChartMarkers = [];

const _defaultEquityAggregation = "5m";
const _equityAggregationOptions = new Set(["1m", "5m", "10m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "1w", "1mo"]);

function getSelectedEquityAggregation() {
  const select = document.getElementById("equity_chart_aggregation");
  const value = String(select?.value || "").trim();
  return _equityAggregationOptions.has(value) ? value : _defaultEquityAggregation;
}

function initEquityAggregationSelector() {
  const select = document.getElementById("equity_chart_aggregation");
  if (!select) return;

  const saved = String(localStorage.getItem("cryptobot_equity_chart_aggregation") || _defaultEquityAggregation).trim();
  select.value = _equityAggregationOptions.has(saved) ? saved : _defaultEquityAggregation;
  localStorage.setItem("cryptobot_equity_chart_aggregation", select.value || _defaultEquityAggregation);
  refreshAppSelect(select);
}

function hideEquityChartTooltip() {
  const tip = document.getElementById("equity_chart_tooltip");
  if (tip) tip.style.display = "none";
}

function ensureEquityChartTooltip() {
  let tip = document.getElementById("equity_chart_tooltip");
  if (tip) return tip;
  tip = document.createElement("div");
  tip.id = "equity_chart_tooltip";
  tip.className = "tip-popup info-popup";
  tip.style.display = "none";
  document.body.appendChild(tip);
  return tip;
}

function formatTooltipExactNumber(value, maxDecimals = 12) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";

  const abs = Math.abs(n);
  let raw = String(abs);
  if (/[eE]/.test(raw)) raw = abs.toFixed(maxDecimals + 2);

  let [intPart, fracPart = ""] = raw.split(".");
  if (fracPart.length > maxDecimals) fracPart = fracPart.slice(0, maxDecimals);
  fracPart = fracPart.replace(/0+$/, "");

  const intFormatted = new Intl.NumberFormat(getNumberLocale(), {
    useGrouping: true,
    maximumFractionDigits: 0,
  }).format(Number(intPart || "0"));
  const decimalSep = (1.1).toLocaleString(getNumberLocale()).replace(/[0-9]/g, "").charAt(0) || ".";
  const sign = n < 0 ? "-" : "";

  return fracPart ? `${sign}${intFormatted}${decimalSep}${fracPart}` : `${sign}${intFormatted}`;
}

function updateEquityChartTooltip(marker, event) {
  const tip = ensureEquityChartTooltip();
  if (!marker || !event) {
    hideEquityChartTooltip();
    return;
  }

  const pointDate = new Date(marker.t);
  const pointTime = Number.isNaN(pointDate.getTime()) ? String(marker.t || "-") : pointDate.toLocaleString();
  const pointPnl = Number(marker.v || 0) - Number(marker.startingBudget || 0);
  const timeLabel = lang === "nl" ? "Tijd" : "Time";
  const equityLabel = lang === "nl" ? "Equity" : "Equity";
  const pnlLabel = "PnL";
  const priceLabel = lang === "nl" ? "Prijs" : "Price";
  const seriesLabel = lang === "nl" ? "Bot" : "Bot";
  const currencyLabel = lang === "nl" ? "Valuta" : "Currency";

  const rows = [
    `<div class="tip-row">${timeLabel}: ${pointTime}</div>`,
    marker.seriesLabel ? `<div class="tip-row">${seriesLabel}: ${marker.seriesLabel}</div>` : "",
    marker.quoteCurrency ? `<div class="tip-row">${currencyLabel}: ${marker.quoteCurrency}</div>` : "",
    `<div class="tip-row">${equityLabel}: ${formatTooltipExactNumber(marker.v)}</div>`,
    `<div class="tip-row" style="color:${pointPnl >= 0 ? "#22c55e" : "#ef4444"}">${pnlLabel}: ${pointPnl >= 0 ? "+" : ""}${formatTooltipExactNumber(pointPnl)}</div>`,
  ].filter(Boolean);
  if (Number(marker.price || 0) > 0) {
    rows.push(`<div class="tip-row">${priceLabel}: ${formatTooltipExactNumber(marker.price)}</div>`);
  }

  tip.innerHTML = `
    <div class="tip-title">${formatTooltipExactNumber(marker.v)}</div>
    <div class="tip-body">${rows.join("")}</div>
  `;
  tip.style.display = "block";

  const margin = 12;
  const tipRect = tip.getBoundingClientRect();
  let left = event.clientX + 14;
  let top = event.clientY - tipRect.height - 14;

  if (left + tipRect.width > globalThis.innerWidth - margin) {
    left = event.clientX - tipRect.width - 14;
  }
  if (left < margin) left = margin;
  if (top < margin) top = event.clientY + 14;

  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function drawEquityChartSeries(seriesEntries, startingBudget = null) {
  const canvas = document.getElementById("equity_chart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  _equityChartMarkers = [];

  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width;
  const H = rect.height;

  ctx.clearRect(0, 0, W, H);

  const cleanedSeries = (Array.isArray(seriesEntries) ? seriesEntries : [])
    .map((entry) => ({
      ...entry,
      points: Array.isArray(entry?.points) ? entry.points.filter((point) => point?.t != null && Number.isFinite(Number(point?.v))) : [],
    }))
    .filter((entry) => entry.points.length >= 2);

  if (!cleanedSeries.length) {
    hideEquityChartTooltip();
    ctx.fillStyle = "#94a3b8";
    ctx.font = "14px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(t("chart_no_data"), W / 2, H / 2);
    return;
  }

  const pad = { top: 20, right: 20, bottom: 30, left: 60 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;

  const allValues = [];
  const allTimes = [];
  for (const entry of cleanedSeries) {
    for (const point of entry.points) {
      allValues.push(Number(point.v || 0));
      allTimes.push(new Date(point.t).getTime());
    }
  }

  let minV = Math.min(...allValues);
  let maxV = Math.max(...allValues);
  if (startingBudget != null) {
    minV = Math.min(minV, Number(startingBudget));
    maxV = Math.max(maxV, Number(startingBudget));
  }
  const range = maxV - minV || 1;
  const validTimes = allTimes.filter((v) => Number.isFinite(v));
  const minTs = validTimes.length ? Math.min(...validTimes) : 0;
  const maxTs = validTimes.length ? Math.max(...validTimes) : minTs + 1;
  const tsRange = maxTs - minTs || 1;

  const currencyHues = [210, 28, 145, 355, 265, 190, 42, 325, 85, 15];
  const currencyHueMap = new Map();
  const botIndexPerCurrency = new Map();
  const dashStyles = [[], [8, 4], [4, 3], [2, 2], [10, 3, 2, 3]];

  const styleBySeries = cleanedSeries.map((entry) => {
    const currencyKey = String(entry.quote_currency || "UNKNOWN").toUpperCase();
    if (!currencyHueMap.has(currencyKey)) {
      currencyHueMap.set(currencyKey, currencyHues[currencyHueMap.size % currencyHues.length]);
    }
    const hue = currencyHueMap.get(currencyKey);
    const botIndex = botIndexPerCurrency.get(currencyKey) || 0;
    botIndexPerCurrency.set(currencyKey, botIndex + 1);

    // Keep a stable hue per currency, vary lightness and dash for bots in that currency.
    const lightness = 44 + ((botIndex % 4) * 10);
    const color = `hsl(${hue} 78% ${lightness}%)`;
    const dash = dashStyles[Math.floor(botIndex / 4) % dashStyles.length];
    return { color, dash };
  });

  ctx.strokeStyle = "#334155";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top);
  ctx.lineTo(pad.left, pad.top + plotH);
  ctx.lineTo(pad.left + plotW, pad.top + plotH);
  ctx.stroke();

  ctx.fillStyle = "#94a3b8";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const v = minV + (range * i) / 4;
    const y = pad.top + plotH - (i / 4) * plotH;
    ctx.fillText(formatNumber(v, 2, 2), pad.left - 6, y + 4);
    if (i > 0 && i < 4) {
      ctx.save();
      ctx.strokeStyle = "#1e293b";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + plotW, y);
      ctx.stroke();
      ctx.restore();
    }
  }

  ctx.textAlign = "center";
  const firstDate = new Date(minTs);
  const lastDate = new Date(maxTs);
  const midDate = new Date(minTs + tsRange / 2);
  const timeFmt = (d) => d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  ctx.fillText(timeFmt(firstDate), pad.left, pad.top + plotH + 18);
  ctx.fillText(timeFmt(midDate), pad.left + plotW / 2, pad.top + plotH + 18);
  ctx.fillText(timeFmt(lastDate), pad.left + plotW, pad.top + plotH + 18);

  cleanedSeries.forEach((entry, index) => {
    const color = styleBySeries[index].color;
    const dash = styleBySeries[index].dash;
    const mappedPoints = entry.points.map((point) => {
      const ts = new Date(point.t).getTime();
      const x = pad.left + ((ts - minTs) / tsRange) * plotW;
      const y = pad.top + plotH - ((Number(point.v || 0) - minV) / range) * plotH;
      return { x, y, ts, raw: point };
    });

    ctx.beginPath();
    ctx.moveTo(mappedPoints[0].x, mappedPoints[0].y);
    for (let i = 1; i < mappedPoints.length; i++) {
      ctx.lineTo(mappedPoints[i].x, mappedPoints[i].y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash(dash);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = color;
    for (const point of mappedPoints) {
      ctx.beginPath();
      ctx.arc(point.x, point.y, 2, 0, Math.PI * 2);
      ctx.fill();
      _equityChartMarkers.push({
        x: point.x,
        y: point.y,
        t: point.raw.t,
        v: Number(point.raw.v || 0),
        price: Number(point.raw.p || 0),
        startingBudget: Number(entry.starting_budget || 0),
        seriesLabel: String(entry.bot_name || entry.bot_id || ""),
        quoteCurrency: String(entry.quote_currency || ""),
      });
    }
  });

  const legendX = pad.left + 8;
  let legendY = pad.top + 6;
  ctx.font = "11px sans-serif";
  ctx.textAlign = "left";

  if (startingBudget != null) {
    const budgetY = pad.top + plotH - ((Number(startingBudget) - minV) / range) * plotH;
    ctx.save();
    ctx.strokeStyle = "#22c55e";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, budgetY);
    ctx.lineTo(pad.left + plotW, budgetY);
    ctx.stroke();
    ctx.restore();

    // Keep the start-budget marker visible in the legend.
    ctx.strokeStyle = "#22c55e";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(legendX, legendY + 7);
    ctx.lineTo(legendX + 24, legendY + 7);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#cbd5e1";
    ctx.fillText(t("lbl_starting_budget"), legendX + 28, legendY + 8);
    legendY += 14;
  }

  for (let i = 0; i < cleanedSeries.length; i++) {
    const color = styleBySeries[i].color;
    const dash = styleBySeries[i].dash;
    const entry = cleanedSeries[i];
    const label = `${String(entry.bot_name || entry.bot_id || "Bot")}${entry.quote_currency ? ` (${entry.quote_currency})` : ""}`;
    ctx.fillStyle = color;
    ctx.fillRect(legendX, legendY + 3, 8, 8);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(legendX + 14, legendY + 7);
    ctx.lineTo(legendX + 28, legendY + 7);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#cbd5e1";
    ctx.fillText(label, legendX + 32, legendY + 8);
    legendY += 14;
    if (legendY > pad.top + plotH - 10) break;
  }
}

/**
 * Draw a line chart of equity over time on the canvas element.
 *
 * @param {Array<{t: string, v: number}>} data - Equity data-points.
 * @param {number} [startingBudget] - Base budget to draw as reference line.
 */
function drawEquityChart(data, startingBudget) {
  const canvas = document.getElementById("equity_chart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  _equityChartMarkers = [];

  // Size canvas to CSS size at device pixel ratio
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width;
  const H = rect.height;

  ctx.clearRect(0, 0, W, H);

  if (!data || data.length < 2) {
    hideEquityChartTooltip();
    ctx.fillStyle = "#94a3b8";
    ctx.font = "14px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(t("chart_no_data"), W / 2, H / 2);
    return;
  }

  const pad = { top: 20, right: 20, bottom: 30, left: 60 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;

  const values = data.map((d) => d.v);
  let minV = Math.min(...values);
  let maxV = Math.max(...values);
  if (startingBudget != null) {
    minV = Math.min(minV, startingBudget);
    maxV = Math.max(maxV, startingBudget);
  }
  const range = maxV - minV || 1;

  // Map data to pixel coordinates
  const points = data.map((d, i) => ({
    x: pad.left + (i / (data.length - 1)) * plotW,
    y: pad.top + plotH - ((d.v - minV) / range) * plotH,
  }));

  // Draw axes
  ctx.strokeStyle = "#334155";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top);
  ctx.lineTo(pad.left, pad.top + plotH);
  ctx.lineTo(pad.left + plotW, pad.top + plotH);
  ctx.stroke();

  // Y-axis labels
  ctx.fillStyle = "#94a3b8";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const v = minV + (range * i) / 4;
    const y = pad.top + plotH - (i / 4) * plotH;
    ctx.fillText(formatNumber(v, 2, 2), pad.left - 6, y + 4);
    if (i > 0 && i < 4) {
      ctx.save();
      ctx.strokeStyle = "#1e293b";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + plotW, y);
      ctx.stroke();
      ctx.restore();
    }
  }

  // X-axis labels (first, middle, last)
  ctx.textAlign = "center";
  const timestamps = data.map((d) => new Date(d.t));
  const timeFmt = (d) => d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  ctx.fillText(timeFmt(timestamps[0]), points[0].x, pad.top + plotH + 18);
  const mid = Math.floor(data.length / 2);
  ctx.fillText(timeFmt(timestamps[mid]), points[mid].x, pad.top + plotH + 18);
  ctx.fillText(timeFmt(timestamps[timestamps.length - 1]), points[points.length - 1].x, pad.top + plotH + 18);

  // Draw gradient fill
  const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
  gradient.addColorStop(0, "rgba(59, 130, 246, 0.25)");
  gradient.addColorStop(1, "rgba(59, 130, 246, 0.02)");
  ctx.beginPath();
  ctx.moveTo(points[0].x, pad.top + plotH);
  for (const p of points) ctx.lineTo(p.x, p.y);
  ctx.lineTo(points[points.length - 1].x, pad.top + plotH);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  // Draw line
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (let i = 1; i < points.length; i++) ctx.lineTo(points[i].x, points[i].y);
  ctx.strokeStyle = "#3b82f6";
  ctx.lineWidth = 2;
  ctx.stroke();

  // Draw small points so every datapoint can be hovered.
  ctx.fillStyle = "rgba(96, 165, 250, 0.45)";
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const d = data[i] || {};
    ctx.beginPath();
    ctx.arc(p.x, p.y, i === 0 || i === points.length - 1 ? 3 : 2, 0, Math.PI * 2);
    ctx.fill();
    _equityChartMarkers.push({
      x: p.x,
      y: p.y,
      t: d.t,
      v: Number(d.v || 0),
      price: Number(d.p || 0),
      startingBudget: Number(startingBudget || 0),
    });
  }

  // Draw starting budget reference line (green dotted)
  if (startingBudget != null) {
    const budgetY = pad.top + plotH - ((startingBudget - minV) / range) * plotH;
    ctx.save();
    ctx.strokeStyle = "#22c55e";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, budgetY);
    ctx.lineTo(pad.left + plotW, budgetY);
    ctx.stroke();
    ctx.restore();
  }

  // Draw dots at endpoints
  for (const p of [points[0], points[points.length - 1]]) {
    ctx.beginPath();
    ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
    ctx.fillStyle = "#3b82f6";
    ctx.fill();
  }
}

/** Fetch equity history for the selected bot and redraw the chart. */
async function loadEquityChart() {
  const botId = document.getElementById("equity_chart_bot")?.value;
  const aggregation = getSelectedEquityAggregation();
  const infoDiv = document.getElementById("equity_chart_info");
  if (!botId) {
    drawEquityChart([]);
    if (infoDiv) infoDiv.style.display = "none";
    return;
  }
  try {
    const params = new URLSearchParams({ aggregation });
    const url = botId === "__total__"
      ? `/api/v1/bots/equity-history/total?${params.toString()}`
      : `/api/v1/bots/${botId}/equity-history?${params.toString()}`;
    const resp = await api(url);
    const totalSeries = botId === "__total__" && Array.isArray(resp.series) ? resp.series : null;
    const points = Array.isArray(resp.points) ? [...resp.points] : [];
    const startingBudget = resp.starting_budget || 0;
    const pnl = resp.pnl || 0;
    const totalEquity = resp.total_equity || 0;

    // Keep the trend endpoint anchored to the same normalized equity that is
    // shown in the bots table/info panel.
    if (points.length > 0) {
      const last = points[points.length - 1] || {};
      points[points.length - 1] = { ...last, v: Number(totalEquity || 0) };
    }

    // Update info labels above chart
    if (infoDiv) {
      infoDiv.style.display = "";
      document.getElementById("equity_info_budget").textContent = formatNumber(startingBudget);
      const pnlEl = document.getElementById("equity_info_pnl");
      pnlEl.textContent = (pnl >= 0 ? "+" : "") + formatNumber(pnl);
      pnlEl.className = pnl >= 0 ? "pnl-positive" : "pnl-negative";
      document.getElementById("equity_info_total").textContent = formatNumber(totalEquity);
    }

    if (totalSeries && totalSeries.length > 0) {
      drawEquityChartSeries(totalSeries, startingBudget);
    } else {
      drawEquityChart(points, startingBudget);
    }
  } catch {
    drawEquityChart([]);
    if (infoDiv) infoDiv.style.display = "none";
  }
}

// Redraw chart when the bot selector changes
document.getElementById("equity_chart_bot")?.addEventListener("change", () => {
  const chartSelect = document.getElementById("equity_chart_bot");
  localStorage.setItem("cryptobot_equity_chart_bot", chartSelect?.value || "");
  loadEquityChart();
});
document.getElementById("equity_chart_aggregation")?.addEventListener("change", () => {
  const aggregationSelect = document.getElementById("equity_chart_aggregation");
  localStorage.setItem("cryptobot_equity_chart_aggregation", aggregationSelect?.value || _defaultEquityAggregation);
  loadEquityChart();
});
document.getElementById("equity_chart")?.addEventListener("mousemove", (e) => {
  const canvas = e.target instanceof HTMLCanvasElement ? e.target : null;
  if (!canvas || !_equityChartMarkers.length) {
    hideEquityChartTooltip();
    return;
  }

  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  let best = null;
  let bestDistance = Number.POSITIVE_INFINITY;

  for (const marker of _equityChartMarkers) {
    const dx = marker.x - mx;
    const dy = marker.y - my;
    const distance = Math.hypot(dx, dy);
    if (distance < bestDistance) {
      best = marker;
      bestDistance = distance;
    }
  }

  if (best && bestDistance <= 12) updateEquityChartTooltip(best, e);
  else hideEquityChartTooltip();
});
document.getElementById("equity_chart")?.addEventListener("mouseleave", hideEquityChartTooltip);

// ──────────────────────────────────────────────────────────────
// Orders overview table
// ──────────────────────────────────────────────────────────────

const _ordersTableState = {
  events: [],
  sortField: "timestamp",
  sortDir: "desc",
  filters: {},
};

const _ordersCategoricalFields = new Set(["market", "event_type", "side"]);
const _ordersNumericFields = new Set(["price", "quote_amount", "fee_paid_quote", "trade_pnl"]);

function _ordersEscapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function _ordersFilterType(field) {
  if (_ordersCategoricalFields.has(field)) return "categorical";
  if (_ordersNumericFields.has(field)) return "numeric";
  return "none";
}

function _ordersParseNumberInput(raw) {
  const text = String(raw || "").trim();
  if (!text) return null;
  const normalized = text.replace(",", ".");
  const value = Number(normalized);
  return Number.isFinite(value) ? value : null;
}

function _ordersIsFilterActive(filter) {
  if (!filter || typeof filter !== "object") return false;
  if (filter.type === "multi") return Array.isArray(filter.values) && filter.values.length > 0;
  if (filter.type === "range") return filter.min !== null || filter.max !== null;
  return false;
}

function _ordersOptionLabel(field, value) {
  const str = String(value || "");
  if (field === "event_type") {
    if (str === "order_placed") return t("lbl_placed");
    if (str === "order_filled") return t("lbl_filled");
    if (str === "order_cancelled") return t("lbl_cancelled");
    return str;
  }
  if (field === "side") return str.toUpperCase();
  return str;
}

function _ordersUniqueFieldValues(field) {
  const values = new Set();
  for (const ev of _ordersTableState.events) {
    const raw = _ordersFieldValue(ev, field);
    const value = String(raw || "").trim();
    if (value) values.add(value);
  }
  return Array.from(values).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
}

function _ordersFieldValue(ev, field) {
  if (!ev) return null;
  if (field === "market") return ev.market || ev.bot_name || "";
  return ev[field];
}

function _ordersFieldDisplayValue(ev, field) {
  const value = _ordersFieldValue(ev, field);
  if (field === "timestamp") return value ? new Date(value).toLocaleString() : "";
  if (["price", "quote_amount", "fee_paid_quote", "trade_pnl"].includes(field)) {
    if (field === "trade_pnl" && Number(value || 0) === 0) return "-";
    if (field === "fee_paid_quote" && Number(value || 0) <= 0) return "-";
    return formatNumber(value);
  }
  if (field === "side") return String(value || "").toUpperCase();
  if (field === "event_type") {
    return value === "order_placed" ? t("lbl_placed")
      : value === "order_filled" ? t("lbl_filled")
      : value === "order_cancelled" ? t("lbl_cancelled")
      : String(value || "");
  }
  return String(value || "");
}

function _ordersCompare(a, b, field) {
  const av = _ordersFieldValue(a, field);
  const bv = _ordersFieldValue(b, field);
  if (field === "timestamp") return new Date(av || 0).getTime() - new Date(bv || 0).getTime();
  if (["price", "quote_amount", "fee_paid_quote", "trade_pnl"].includes(field)) {
    return Number(av || 0) - Number(bv || 0);
  }
  return String(av || "").localeCompare(String(bv || ""), undefined, { sensitivity: "base" });
}

function _setOrderSorter(field, dir) {
  _ordersTableState.sortField = field;
  _ordersTableState.sortDir = dir;
}

function _clearOrderSorter(field) {
  if (_ordersTableState.sortField === field) {
    _ordersTableState.sortField = "timestamp";
    _ordersTableState.sortDir = "desc";
  }
}

function _filteredAndSortedOrderEvents() {
  const entries = _ordersTableState.events.filter((ev) => {
    for (const [field, filter] of Object.entries(_ordersTableState.filters)) {
      if (!_ordersIsFilterActive(filter)) continue;
      if (filter.type === "multi") {
        const value = String(_ordersFieldValue(ev, field) || "");
        if (!filter.values.includes(value)) return false;
      }
      if (filter.type === "range") {
        const value = Number(_ordersFieldValue(ev, field) || 0);
        if (filter.min !== null && value < filter.min) return false;
        if (filter.max !== null && value > filter.max) return false;
      }
    }
    return true;
  });

  entries.sort((left, right) => {
    const cmp = _ordersCompare(left, right, _ordersTableState.sortField);
    return _ordersTableState.sortDir === "asc" ? cmp : -cmp;
  });
  return entries;
}

function _closeOrdersHeaderMenu() {
  const menu = document.getElementById("orders_header_menu");
  if (menu) menu.classList.remove("open");
}

function _ensureOrdersHeaderMenu() {
  let menu = document.getElementById("orders_header_menu");
  if (menu) return menu;
  menu = document.createElement("div");
  menu.id = "orders_header_menu";
  menu.className = "orders-header-menu";
  document.body.appendChild(menu);
  return menu;
}

function _updateOrdersHeaderState() {
  document.querySelectorAll(".orders-table th[data-order-field]").forEach((th) => {
    const field = th.dataset.orderField || "";
    const isSortField = field === _ordersTableState.sortField;
    th.classList.toggle("orders-sort-asc", isSortField && _ordersTableState.sortDir === "asc");
    th.classList.toggle("orders-sort-desc", isSortField && _ordersTableState.sortDir === "desc");
    th.classList.toggle("orders-filtered", _ordersIsFilterActive(_ordersTableState.filters[field]));
  });
}

function _renderOrdersActiveChips() {
  const container = document.getElementById("orders_active_chips");
  const resetBtn = document.getElementById("orders_reset_all");
  if (!container) return;

  const chips = [];
  const isDefaultSort = _ordersTableState.sortField === "timestamp" && _ordersTableState.sortDir === "desc";

  if (!isDefaultSort) {
    const th = document.querySelector(`.orders-table th[data-order-field="${_ordersTableState.sortField}"]`);
    const label = (th?.textContent || _ordersTableState.sortField).trim();
    chips.push(`<span class="orders-chip">Sort: ${_ordersEscapeHtml(label)} ${_ordersTableState.sortDir === "asc" ? "↑" : "↓"}<button type="button" data-chip-type="sort" data-field="${_ordersTableState.sortField}">×</button></span>`);
  }
  for (const [field, filter] of Object.entries(_ordersTableState.filters)) {
    if (!_ordersIsFilterActive(filter)) continue;
    const th = document.querySelector(`.orders-table th[data-order-field="${field}"]`);
    const label = (th?.textContent || field).trim();
    if (filter.type === "multi") {
      const selected = filter.values.map((v) => _ordersOptionLabel(field, v)).join(", ");
      chips.push(`<span class="orders-chip">Filter: ${_ordersEscapeHtml(label)} = ${_ordersEscapeHtml(selected)}<button type="button" data-chip-type="filter" data-field="${field}">×</button></span>`);
    } else if (filter.type === "range") {
      const minText = filter.min === null ? "-∞" : formatNumber(filter.min);
      const maxText = filter.max === null ? "+∞" : formatNumber(filter.max);
      chips.push(`<span class="orders-chip">Filter: ${_ordersEscapeHtml(label)} ${_ordersEscapeHtml(minText)} .. ${_ordersEscapeHtml(maxText)}<button type="button" data-chip-type="filter" data-field="${field}">×</button></span>`);
    }
  }

  container.innerHTML = chips.join("");
  if (resetBtn) {
    resetBtn.disabled = chips.length === 0;
  }

  container.querySelectorAll("button[data-chip-type]").forEach((btn) => {
    btn.onclick = (event) => {
      event.stopPropagation();
      const field = btn.dataset.field || "";
      if (btn.dataset.chipType === "sort") {
        _clearOrderSorter(field);
      } else {
        delete _ordersTableState.filters[field];
      }
      _renderOrdersTable();
    };
  });
}

function _openOrdersHeaderMenu(th) {
  const field = th?.dataset?.orderField;
  if (!field) return;

  const menu = _ensureOrdersHeaderMenu();
  const filterType = _ordersFilterType(field);
  const currentFilter = _ordersTableState.filters[field];

  let filterMarkup = "";
  if (filterType === "categorical") {
    const selectedValues = currentFilter?.type === "multi" ? new Set(currentFilter.values) : new Set();
    const options = _ordersUniqueFieldValues(field);
    const optionsMarkup = options.length
      ? options.map((value) => {
        const id = `orders_filter_${field}_${value.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
        const checked = selectedValues.has(value) ? "checked" : "";
        return `<label for="${id}" class="orders-filter-option"><input id="${id}" type="checkbox" value="${_ordersEscapeHtml(value)}" ${checked} data-filter-option="1" /><span>${_ordersEscapeHtml(_ordersOptionLabel(field, value))}</span></label>`;
      }).join("")
      : `<div class="orders-filter-empty">${lang === "nl" ? "Geen opties" : "No options"}</div>`;
    filterMarkup = `
      <div class="orders-filter-section">
        <span>${lang === "nl" ? "Selecteer opties" : "Select options"}</span>
        <div class="orders-filter-options">${optionsMarkup}</div>
      </div>
    `;
  } else if (filterType === "numeric") {
    const currentMin = currentFilter?.type === "range" && currentFilter.min !== null ? String(currentFilter.min) : "";
    const currentMax = currentFilter?.type === "range" && currentFilter.max !== null ? String(currentFilter.max) : "";
    filterMarkup = `
      <div class="orders-filter-section orders-filter-range">
        <span>${lang === "nl" ? "Range" : "Range"}</span>
        <div class="orders-filter-range-grid">
          <input id="orders_header_filter_min" type="text" inputmode="decimal" placeholder="${lang === "nl" ? "Min" : "Min"}" value="${_ordersEscapeHtml(currentMin)}" />
          <input id="orders_header_filter_max" type="text" inputmode="decimal" placeholder="${lang === "nl" ? "Max" : "Max"}" value="${_ordersEscapeHtml(currentMax)}" />
        </div>
      </div>
    `;
  }

  const sortMarkup = `<div class="orders-header-actions"><button type="button" data-sort="asc">${lang === "nl" ? "Oplopend" : "Ascending"}</button><button type="button" data-sort="desc">${lang === "nl" ? "Aflopend" : "Descending"}</button></div>`;

  menu.innerHTML = `
    <div class="orders-header-title">${th.textContent.trim()}</div>
    ${sortMarkup}
    ${filterMarkup}
    <div class="orders-header-actions">
      ${filterType !== "none" ? `<button type="button" data-action="clear-filter">${lang === "nl" ? "Filter wissen" : "Clear filter"}</button>` : ""}
      <button type="button" data-action="clear-sort">${lang === "nl" ? "Sortering wissen" : "Clear sort"}</button>
    </div>
  `;

  const rect = th.getBoundingClientRect();
  menu.classList.add("open");
  const menuRect = menu.getBoundingClientRect();
  let left = rect.left;
  if (left + menuRect.width > globalThis.innerWidth - 8) {
    left = globalThis.innerWidth - menuRect.width - 8;
  }
  left = Math.max(8, left);
  menu.style.left = `${left}px`;
  menu.style.top = `${Math.min(globalThis.innerHeight - menuRect.height - 8, rect.bottom + 6)}px`;

  menu.querySelectorAll("button[data-sort]").forEach((btn) => {
    btn.onclick = () => {
      _setOrderSorter(field, btn.dataset.sort === "asc" ? "asc" : "desc");
      _renderOrdersTable();
      _closeOrdersHeaderMenu();
    };
  });

  const applyLiveFilter = () => {
    if (filterType === "categorical") {
      const selected = Array.from(menu.querySelectorAll("input[data-filter-option='1']:checked"))
        .map((input) => String(input.value || "").trim())
        .filter(Boolean);
      if (selected.length) {
        _ordersTableState.filters[field] = { type: "multi", values: selected };
      } else {
        delete _ordersTableState.filters[field];
      }
    }
    if (filterType === "numeric") {
      const min = _ordersParseNumberInput(menu.querySelector("#orders_header_filter_min")?.value || "");
      const max = _ordersParseNumberInput(menu.querySelector("#orders_header_filter_max")?.value || "");
      if (min !== null || max !== null) {
        _ordersTableState.filters[field] = { type: "range", min, max };
      } else {
        delete _ordersTableState.filters[field];
      }
    }
    _renderOrdersTable();
  };

  if (filterType === "categorical") {
    menu.querySelectorAll("input[data-filter-option='1']").forEach((input) => {
      input.addEventListener("change", applyLiveFilter);
    });
  }

  if (filterType === "numeric") {
    const minInput = menu.querySelector("#orders_header_filter_min");
    const maxInput = menu.querySelector("#orders_header_filter_max");
    minInput?.addEventListener("input", applyLiveFilter);
    maxInput?.addEventListener("input", applyLiveFilter);
  }

  const clearFilterBtn = menu.querySelector("button[data-action='clear-filter']");
  if (clearFilterBtn) {
    clearFilterBtn.onclick = () => {
      delete _ordersTableState.filters[field];
      _renderOrdersTable();
      _closeOrdersHeaderMenu();
    };
  }

  menu.querySelector("button[data-action='clear-sort']").onclick = () => {
    _clearOrderSorter(field);
    _renderOrdersTable();
    _closeOrdersHeaderMenu();
  };

  const firstInput = menu.querySelector("input");
  firstInput?.focus();
  if (firstInput instanceof HTMLInputElement && firstInput.type === "text") firstInput.select();
}

function _renderOrdersTable() {
  const body = document.getElementById("orders_body");
  if (!body) return;

  const events = _filteredAndSortedOrderEvents();
  body.innerHTML = "";
  for (const ev of events) {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    const normalizedSide = _normalizeSideValue(ev.side);
    const ts = new Date(ev.timestamp).toLocaleString();
    const typeClass = ev.event_type === "order_placed" ? "order-type-placed"
      : ev.event_type === "order_filled" ? "order-type-filled"
      : "order-type-cancelled";
    const typeLabel = ev.event_type === "order_placed" ? `📋 ${t("lbl_placed")}`
      : ev.event_type === "order_filled" ? `✅ ${t("lbl_filled")}`
      : ev.event_type === "order_cancelled" ? `❌ ${t("lbl_cancelled")}`
      : String(ev.event_type || "");
    const pnlStr = ev.trade_pnl !== 0 ? ((ev.trade_pnl >= 0 ? "+" : "") + formatNumber(ev.trade_pnl)) : "-";
    const pnlClass = ev.trade_pnl > 0 ? "pnl-positive" : ev.trade_pnl < 0 ? "pnl-negative" : "";
    const sideClass = normalizedSide === "buy" ? "order-buy" : normalizedSide === "sell" ? "order-sell" : "";
    const marketLabel = ev.market || ev.bot_name || "-";
    const feeStr = Number(ev.fee_paid_quote || 0) > 0 ? formatNumber(ev.fee_paid_quote) : "-";
    tr.innerHTML = `<td>${ts}</td><td>${marketLabel}</td><td class="${typeClass}">${typeLabel}</td><td class="${sideClass}">${normalizedSide ? normalizedSide.toUpperCase() : "-"}</td><td>${formatNumber(ev.price)}</td><td>${formatNumber(ev.quote_amount)}</td><td>${feeStr}</td><td class="${pnlClass}">${pnlStr}</td>`;
    tr.onclick = () => {
      if (ev.event_source === "open_order_snapshot") {
        openOrderDetailFromData(ev);
        return;
      }
      openOrderDetail(ev.id);
    };
    body.appendChild(tr);
  }

  _updateOrdersHeaderState();
  _renderOrdersActiveChips();
}

function _initOrdersHeaderControls() {
  document.querySelectorAll(".orders-table th[data-order-field]").forEach((th) => {
    if (th.dataset.orderHeaderBound === "1") return;
    th.dataset.orderHeaderBound = "1";
    th.addEventListener("click", (event) => {
      event.stopPropagation();
      _openOrdersHeaderMenu(th);
    });
  });

  const resetBtn = document.getElementById("orders_reset_all");
  if (resetBtn && resetBtn.dataset.bound !== "1") {
    resetBtn.dataset.bound = "1";
    resetBtn.addEventListener("click", () => {
      _ordersTableState.filters = {};
      _ordersTableState.sortField = "timestamp";
      _ordersTableState.sortDir = "desc";
      _renderOrdersTable();
      _closeOrdersHeaderMenu();
    });
  }
}

document.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (target?.closest("#orders_header_menu") || target?.closest(".orders-table th[data-order-field]")) return;
  _closeOrdersHeaderMenu();
});

/** Load all order events into the Orders tab table. */
async function loadOrders() {
  const tbody = document.getElementById("open_orders_body");
  const countEl = document.getElementById("open_orders_count");
  if (!tbody) return;
  try {
    const bots = Array.isArray(latestBots) && latestBots.length ? latestBots : await api("/api/v1/bots");
    const markets = [...new Set((Array.isArray(bots) ? bots : [])
      .map((bot) => String(bot?.config?.market || "").trim().toUpperCase())
      .filter((market) => market.includes("-")))];
    const qs = new URLSearchParams();
    if (markets.length) qs.set("markets", markets.join(","));
    const url = qs.size ? `/api/v1/market/notifications/open-orders?${qs.toString()}` : "/api/v1/market/notifications/open-orders";
    const resp = await api(url);
    const rows = Array.isArray(resp?.rows) ? resp.rows : [];
    if (countEl) countEl.textContent = `(${rows.length})`;
    _notificationOrderCache.clear();
    rows.forEach((row) => {
      const id = row.id || `${row.order_id || ""}-${row.date_time || ""}`;
      _notificationOrderCache.set(id, row);
    });
    if (!rows.length) {
      _renderNotificationEmpty(tbody, 10);
      return;
    }
    _removePlaceholderRows(tbody, "data-notif-order-id");
    _syncRowsByKey(
      tbody,
      rows,
      "data-notif-order-id",
      (row) => row.id || `${row.order_id || ""}-${row.date_time || ""}`,
      (tr, row) => {
        _ensureRowCellCount(tr, 10);
        const cells = tr.children;
        cells[0].textContent = row.market || "-";
        cells[1].textContent = row.order_type || "-";
        cells[2].className = _sideClass(row.side);
        cells[2].textContent = String(row.side || "-").toUpperCase();
        cells[3].textContent = formatNumber(row.total || 0);
        cells[4].textContent = Number(row.trigger_price || 0) > 0 ? formatNumber(row.trigger_price || 0) : "-";
        cells[5].textContent = formatNumber(row.limit_price || 0);
        cells[6].textContent = formatNumber(row.total_amount || 0);
        cells[7].textContent = formatNumber(row.open_amount || 0);
        cells[8].textContent = formatNumber(row.filled_amount || 0);
        cells[9].textContent = _formatDateTime(row.date_time);
      }
    );
  } catch {
    if (countEl) countEl.textContent = "(0)";
    _renderNotificationEmpty(tbody, 10);
  }
}

function openOrderDetailFromData(ev) {
  const modal = document.getElementById("order_detail_modal");
  const content = document.getElementById("order_detail_content");
  if (!modal || !content) return;

  const normalizedSide = _normalizeSideValue(ev.side);
  const sideClass = normalizedSide === "buy" ? "order-buy" : normalizedSide === "sell" ? "order-sell" : "";
  const typeLabel = `📋 ${t("lbl_placed")}`;
  let html = `<div class="order-detail-grid">`;
  html += `<div class="od-row"><span class="od-label">${t("th_type")}</span><span>${typeLabel}</span></div>`;
  html += `<div class="od-row"><span class="od-label">${t("lbl_local_order_id")}</span><span>${ev.order_id || "-"}</span></div>`;
  html += `<div class="od-row"><span class="od-label">${t("lbl_exchange_order_id")}</span><span>${ev.exchange_order_id || "-"}</span></div>`;
  html += `<div class="od-row"><span class="od-label">${t("th_side")}</span><span class="${sideClass}">${normalizedSide ? normalizedSide.toUpperCase() : "-"}</span></div>`;
  html += `<div class="od-row"><span class="od-label">${t("th_market")}</span><span>${ev.market || ev.bot_name || "-"}</span></div>`;
  html += `<div class="od-row"><span class="od-label">${t("th_price")}</span><span>${formatNumber(ev.price)}</span></div>`;
  html += `<div class="od-row"><span class="od-label">${t("th_amount")}</span><span>${formatNumber(ev.quote_amount)}</span></div>`;
  html += `<div class="od-row"><span class="od-label">${t("th_time")}</span><span>${new Date(ev.timestamp).toLocaleString()}</span></div>`;
  html += `<div class="od-row"><span class="od-label">${t("th_level")}</span><span>${ev.level_index != null ? ev.level_index : "-"}</span></div>`;
  html += `</div>`;
  content.innerHTML = html;
  modal.showModal();
}

// ──────────────────────────────────────────────────────────────
// Order detail modal
// ──────────────────────────────────────────────────────────────

async function openOrderDetail(eventId) {
  const modal = document.getElementById("order_detail_modal");
  const content = document.getElementById("order_detail_content");
  content.innerHTML = `<p>${t("loading")}</p>`;
  modal.showModal();

  try {
    const ev = await api(`/api/v1/trade-events/${eventId}`);
    const normalizedSide = _normalizeSideValue(ev.side);
    const typeLabel = ev.event_type === "order_placed" ? `📋 ${t("lbl_placed")}`
      : ev.event_type === "order_filled" ? `✅ ${t("lbl_filled")}`
      : ev.event_type === "order_cancelled" ? `❌ ${t("lbl_cancelled")}`
      : `🔄 ${t("toast_trade")}`;
    const sideClass = normalizedSide === "buy" ? "order-buy" : normalizedSide === "sell" ? "order-sell" : "";
    const pnlClass = ev.trade_pnl > 0 ? "pnl-positive" : ev.trade_pnl < 0 ? "pnl-negative" : "";
    const pnlStr = ev.trade_pnl !== 0 ? ((ev.trade_pnl >= 0 ? "+" : "") + formatNumber(ev.trade_pnl)) : "-";

    let html = `<div class="order-detail-grid">`;
    html += `<div class="od-row"><span class="od-label">${t("th_type")}</span><span>${typeLabel}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("lbl_local_order_id")}</span><span>${ev.order_id || "-"}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("lbl_exchange_order_id")}</span><span>${ev.exchange_order_id || "-"}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("th_side")}</span><span class="${sideClass}">${normalizedSide ? normalizedSide.toUpperCase() : "-"}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("th_market")}</span><span>${ev.market || ev.bot_name || "-"}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("th_price")}</span><span>${formatNumber(ev.price)}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("th_amount")}</span><span>${formatNumber(ev.quote_amount)}</span></div>`;
    if (ev.event_type === "order_filled") {
      html += `<div class="od-row"><span class="od-label">${t("lbl_fill_parts")}</span><span>${Number(ev.fill_count || 0) > 0 ? Number(ev.fill_count) : "-"}</span></div>`;
    }
    html += `<div class="od-row"><span class="od-label">${t("th_fee_amount")}</span><span>${Number(ev.fee_paid_quote || 0) > 0 ? formatNumber(ev.fee_paid_quote) : "-"}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("th_time")}</span><span>${new Date(ev.timestamp).toLocaleString()}</span></div>`;
    if (normalizedSide !== "buy") {
      html += `<div class="od-row"><span class="od-label">${t("lbl_pnl")}</span><span class="${pnlClass}">${pnlStr}</span></div>`;
    }
    html += `<div class="od-row"><span class="od-label">${t("th_level")}</span><span>${ev.level_index != null ? ev.level_index : "-"}</span></div>`;
    html += `</div>`;

    if (ev.pair_metrics) {
      const pm = ev.pair_metrics;
      const pairPnlClass = pm.realized_pnl_quote > 0 ? "pnl-positive" : pm.realized_pnl_quote < 0 ? "pnl-negative" : "";
      const pairPnlStr = `${pm.realized_pnl_quote >= 0 ? "+" : ""}${formatNumber(pm.realized_pnl_quote)}`;
      const grossProfitStr = `${pm.gross_profit_quote >= 0 ? "+" : ""}${formatNumber(pm.gross_profit_quote)}`;
      html += `<h4 style="margin:14px 0 6px;">${t("lbl_realized_pair_pnl")}</h4>`;
      html += `<div class="order-detail-grid">`;
      html += `<div class="od-row"><span class="od-label">${t("lbl_realized_pair_pnl")}</span><span class="${pairPnlClass}">${pairPnlStr}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("lbl_gross_profit")}</span><span>${grossProfitStr}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("lbl_total_fees")}</span><span>${formatNumber(pm.total_fees_quote)}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("lbl_quantity")}</span><span>${formatNumber(pm.quantity_base)}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("lbl_fee_rate")}</span><span>${(pm.fee_rate * 100).toFixed(2)}%</span></div>`;
      html += `</div>`;
    } else if (normalizedSide === "buy") {
      html += `<p style="color:#94a3b8;margin-top:12px;font-size:0.9rem;">${t("lbl_pair_pnl_pending_sell_fill")}</p>`;
    }

    // Linked order section
    if (ev.linked_order) {
      const lo = ev.linked_order;
      const loSide = _normalizeSideValue(lo.side);
      const loSideClass = loSide === "buy" ? "order-buy" : loSide === "sell" ? "order-sell" : "";
      const loTypeLabel = lo.event_type === "order_filled" ? `✅ ${t("lbl_filled")}` : lo.event_type;
      const loPnlStr = lo.trade_pnl !== 0 ? ((lo.trade_pnl >= 0 ? "+" : "") + formatNumber(lo.trade_pnl)) : "-";
      const linkedLabel = normalizedSide === "sell" ? t("lbl_linked_buy") : t("lbl_linked_sell");
      html += `<h4 style="margin:14px 0 6px;">${linkedLabel}</h4>`;
      html += `<div class="order-detail-grid">`;
      html += `<div class="od-row"><span class="od-label">${t("th_type")}</span><span>${loTypeLabel}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("lbl_local_order_id")}</span><span>${lo.order_id || "-"}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("lbl_exchange_order_id")}</span><span>${lo.exchange_order_id || "-"}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("th_side")}</span><span class="${loSideClass}">${loSide ? loSide.toUpperCase() : "-"}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("th_price")}</span><span>${formatNumber(lo.price)}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("th_amount")}</span><span>${formatNumber(lo.quote_amount)}</span></div>`;
      if (lo.event_type === "order_filled") {
        html += `<div class="od-row"><span class="od-label">${t("lbl_fill_parts")}</span><span>${Number(lo.fill_count || 0) > 0 ? Number(lo.fill_count) : "-"}</span></div>`;
      }
      html += `<div class="od-row"><span class="od-label">${t("th_fee_amount")}</span><span>${Number(lo.fee_paid_quote || 0) > 0 ? formatNumber(lo.fee_paid_quote) : "-"}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("th_time")}</span><span>${new Date(lo.timestamp).toLocaleString()}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("lbl_pnl")}</span><span>${loPnlStr}</span></div>`;
      html += `</div>`;
    } else {
      html += `<p style="color:#94a3b8;margin-top:12px;font-size:0.9rem;">${t("lbl_no_linked_order")}</p>`;
    }

    content.innerHTML = html;
  } catch (e) {
    content.innerHTML = `<p style="color:#ef4444;">${e.message}</p>`;
  }
}

document.getElementById("close_order_detail")?.addEventListener("click", () => {
  document.getElementById("order_detail_modal").close();
});

document.getElementById("close_notification_detail")?.addEventListener("click", () => {
  document.getElementById("notification_detail_modal")?.close();
});

document.getElementById("close_bot_detail_modal")?.addEventListener("click", () => {
  document.getElementById("bot_detail_modal").close();
  activeBotDetailId = "";
});

// ──────────────────────────────────────────────────────────────
// Trade levels chart (modal, per-bot)
// ──────────────────────────────────────────────────────────────

// ──────────────────────────────────────────────────────────────
// Open orders modal
// ──────────────────────────────────────────────────────────────

async function openOrdersModal(bot) {
  const modal = document.getElementById("orders_modal");
  document.getElementById("orders_title").textContent = `${t("orders_modal_title")} — ${bot.name}`;
  const tbody = document.getElementById("modal_orders_body");
  const gridBody = document.getElementById("grid_body");
  tbody.innerHTML = `<tr><td colspan="5">${t("loading")}</td></tr>`;
  gridBody.innerHTML = "";
  modal.showModal();

  // Build full grid levels from config
  const grid = bot.config?.grid || {};
  const levels = [];
  if (grid.lower_price && grid.upper_price && grid.levels > 1) {
    const step = (grid.upper_price - grid.lower_price) / (grid.levels - 1);
    for (let i = 0; i < grid.levels; i++) {
      levels.push({ index: i, price: grid.lower_price + i * step });
    }
  }

  try {
    const data = await api(`/api/v1/bots/${bot.id}/open-orders`);
    const orders = data.orders || [];
    if (orders.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5">${t("no_open_orders")}</td></tr>`;
    } else {
      tbody.innerHTML = "";
      for (const o of orders) {
        const tr = document.createElement("tr");
        const normalizedSide = _normalizeSideValue(o.side);
        const sideClass = normalizedSide === "buy" ? "order-buy" : normalizedSide === "sell" ? "order-sell" : "";
        const amt = formatNumber(o.quote_amount || 0);
        const filled = formatNumber(o.filled_quote || 0);
        tr.innerHTML = `<td>${o.level}</td><td>${formatNumber(o.price)}</td><td class="${sideClass}">${normalizedSide ? normalizedSide.toUpperCase() : "-"}</td><td>${amt}</td><td>${filled}</td>`;
        tbody.appendChild(tr);
      }
    }

    // Build order lookup by level index
    const orderMap = {};
    for (const o of orders) orderMap[o.level] = o;

    // Render full grid
    for (const lv of levels) {
      const tr = document.createElement("tr");
      const order = orderMap[lv.index];
      let statusHtml;
      if (order) {
        const orderSide = _normalizeSideValue(order.side);
        const cls = orderSide === "buy" ? "order-buy" : orderSide === "sell" ? "order-sell" : "";
        statusHtml = `<span class="${cls}">${orderSide ? orderSide.toUpperCase() : "-"}</span>`;
      } else {
        statusHtml = `<span class="grid-idle">—</span>`;
      }
      tr.innerHTML = `<td>${lv.index}</td><td>${formatNumber(lv.price)}</td><td>${statusHtml}</td>`;
      gridBody.appendChild(tr);
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5">${e.message}</td></tr>`;
  }
}

/** Cached pixel points and trade markers for tooltip hit-testing. */
let _tradeChartMarkers = [];
let _tradeChartGridMarkers = [];
let _tradeChartBot = null;

/**
 * Open the trade chart modal for a bot, fetch its price history
 * and trades, then draw the chart.
 */
async function openTradeChart(bot) {
  _tradeChartBot = bot;
  const modal = document.getElementById("trade_chart_modal");
  document.getElementById("trade_chart_title").textContent = `${t("chart_modal_title")} — ${bot.name}`;
  modal.showModal();

  let history = [];
  let trades = [];
  let openOrders = [];
  const grid = bot.config?.grid || {};
  const fallbackPrice = Number(bot.latest_metrics?.price || grid.lower_price || grid.upper_price || 0) || 0;
  try {
    const resp = await api(`/api/v1/bots/${bot.id}/equity-history`);
    history = resp.points || [];
  } catch {}
  try {
    const all = await api("/api/v1/trade-events");
    trades = all.filter((e) => e.bot_id === bot.id);
  } catch {}
  try {
    const resp = await api(`/api/v1/bots/${bot.id}/open-orders`);
    openOrders = resp.orders || [];
  } catch {}

  drawTradeChart(history, trades, grid, openOrders, fallbackPrice);
}

document.getElementById("close_trade_chart")?.addEventListener("click", () => {
  document.getElementById("trade_chart_modal").close();
  _tradeChartMarkers = [];
});

document.getElementById("close_orders_modal")?.addEventListener("click", () => {
  document.getElementById("orders_modal").close();
});

/**
 * Draw the trade chart: price line, grid levels, and trade markers.
 *
 * @param {Array<{t:string,v:number,p:number}>} history - equity/price points
 * @param {Array} trades - trade events for this bot
 * @param {{lower_price:number,upper_price:number,levels:number}} grid
 * @param {Array<{level:number,price:number,side:string}>} openOrders
 * @param {number} fallbackPrice
 */
function drawTradeChart(history, trades, grid, openOrders = [], fallbackPrice = 0) {
  const canvas = document.getElementById("trade_chart_canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width;
  const H = rect.height;
  ctx.clearRect(0, 0, W, H);

  _tradeChartMarkers = [];
  _tradeChartGridMarkers = [];

  // Filter to points that have a price. If the bot has no history yet,
  // synthesize a short flat series so the grid and hover still render.
  let priceData = history.filter((d) => d.p && d.p > 0);
  if (priceData.length === 0) {
    const derivedFallback = fallbackPrice > 0
      ? fallbackPrice
      : (grid.lower_price > 0 && grid.upper_price > 0)
        ? (grid.lower_price + grid.upper_price) / 2
        : (grid.lower_price > 0 || grid.upper_price > 0)
          ? (grid.lower_price || grid.upper_price)
          : 1;
    const now = new Date();
    priceData = [
      { t: new Date(now.getTime() - 1000).toISOString(), p: derivedFallback },
      { t: now.toISOString(), p: derivedFallback },
    ];
  } else if (priceData.length === 1) {
    const single = priceData[0];
    priceData = [
      { t: new Date(new Date(single.t).getTime() - 1000).toISOString(), p: single.p },
      { t: single.t, p: single.p },
    ];
  }

  if (priceData.length < 2) {
    ctx.fillStyle = "#94a3b8";
    ctx.font = "14px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(t("chart_no_data"), W / 2, H / 2);
    return;
  }

  const pad = { top: 20, right: 20, bottom: 30, left: 70 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;

  // Compute price range (include grid bounds if available)
  const prices = priceData.map((d) => d.p);
  let minP = Math.min(...prices);
  let maxP = Math.max(...prices);
  if (grid.lower_price) minP = Math.min(minP, grid.lower_price);
  if (grid.upper_price) maxP = Math.max(maxP, grid.upper_price);
  const margin = (maxP - minP) * 0.05 || 0.001;
  minP -= margin;
  maxP += margin;
  const rangeP = maxP - minP || 1;

  const tMin = new Date(priceData[0].t).getTime();
  const tMax = new Date(priceData[priceData.length - 1].t).getTime();
  const tRange = tMax - tMin || 1;
  const currentPrice = priceData[priceData.length - 1].p || 0;
  const openOrderMap = new Map();
  for (const order of openOrders || []) {
    if (order && Number.isInteger(Number(order.level))) {
      openOrderMap.set(Number(order.level), order);
    }
  }

  const toX = (ts) => pad.left + ((new Date(ts).getTime() - tMin) / tRange) * plotW;
  const toY = (p) => pad.top + plotH - ((p - minP) / rangeP) * plotH;

  // Draw axes
  ctx.strokeStyle = "#334155";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top);
  ctx.lineTo(pad.left, pad.top + plotH);
  ctx.lineTo(pad.left + plotW, pad.top + plotH);
  ctx.stroke();

  // Y-axis labels
  ctx.fillStyle = "#94a3b8";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const v = minP + (rangeP * i) / 4;
    const y = pad.top + plotH - (i / 4) * plotH;
    ctx.fillText(formatNumber(v), pad.left - 6, y + 4);
  }

  // X-axis labels
  ctx.textAlign = "center";
  const timeFmt = (d) => d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  ctx.fillText(timeFmt(new Date(priceData[0].t)), toX(priceData[0].t), pad.top + plotH + 18);
  const midIdx = Math.floor(priceData.length / 2);
  ctx.fillText(timeFmt(new Date(priceData[midIdx].t)), toX(priceData[midIdx].t), pad.top + plotH + 18);
  ctx.fillText(timeFmt(new Date(priceData[priceData.length - 1].t)), toX(priceData[priceData.length - 1].t), pad.top + plotH + 18);

  // Draw grid levels using order state: open buys green, open sells red,
  // and future sells above the current price as dark red.
  if (grid.lower_price && grid.upper_price && grid.levels >= 2) {
    const step = (grid.upper_price - grid.lower_price) / (grid.levels - 1);
    ctx.save();
    ctx.setLineDash([6, 4]);
    ctx.lineWidth = 1;
    for (let i = 0; i < grid.levels; i++) {
      const lvl = grid.lower_price + i * step;
      const y = toY(lvl);
      if (y < pad.top || y > pad.top + plotH) continue;
      const order = openOrderMap.get(i);
      let strokeStyle = "rgba(250,204,21,0.35)";
      let fillStyle = "rgba(250,204,21,0.55)";
      if (order?.side === "buy") {
        strokeStyle = "rgba(34,197,94,0.95)";
        fillStyle = "rgba(34,197,94,0.9)";
      } else if (order?.side === "sell") {
        strokeStyle = "rgba(239,68,68,0.95)";
        fillStyle = "rgba(239,68,68,0.9)";
      } else if (currentPrice > 0 && lvl > currentPrice) {
        strokeStyle = "rgba(127,29,29,0.95)";
        fillStyle = "rgba(127,29,29,0.75)";
      }
      ctx.strokeStyle = strokeStyle;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + plotW, y);
      ctx.stroke();

      const isOpenBuy = order?.side === "buy";
      const isOpenSell = order?.side === "sell";
      const isFutureSell = !order && currentPrice > 0 && lvl > currentPrice;
      const expectedPnl = isOpenSell && i > 0 ? lvl - (grid.lower_price + (i - 1) * step) : null;
      _tradeChartGridMarkers.push({
        x1: pad.left,
        x2: pad.left + plotW,
        y,
        level: i,
        price: lvl,
        side: order?.side || (isFutureSell ? "sell" : "buy"),
        isOpenBuy,
        isOpenSell,
        isFutureSell,
        expectedPnl,
      });
    }
    ctx.restore();
  }

  // Draw price line
  ctx.beginPath();
  ctx.moveTo(toX(priceData[0].t), toY(priceData[0].p));
  for (let i = 1; i < priceData.length; i++) {
    ctx.lineTo(toX(priceData[i].t), toY(priceData[i].p));
  }
  ctx.strokeStyle = "#3b82f6";
  ctx.lineWidth = 2;
  ctx.setLineDash([]);
  ctx.stroke();

  // Draw trade level lines
  for (const tr of trades) {
    const y = toY(tr.price);
    if (y < pad.top || y > pad.top + plotH) continue;
    const isBuy = tr.side === "buy" || tr.trade_pnl < 0;
    const lineColor = isBuy ? "rgba(34,197,94,0.9)" : "rgba(239,68,68,0.9)";
    ctx.save();
    ctx.setLineDash([8, 5]);
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + plotW, y);
    ctx.stroke();
    ctx.restore();
    _tradeChartMarkers.push({ x1: pad.left, x2: pad.left + plotW, y, trade: tr });
  }
}

function _updateTradeChartTooltip(marker, canvas, mx) {
  const tip = document.getElementById("trade_chart_tooltip");
  if (!tip || !marker) return;

  if (marker.trade) {
    const tr = marker.trade;
    const time = new Date(tr.timestamp).toLocaleString();
    const pnl = Number(tr.trade_pnl || 0);
    const pnlCls = pnl >= 0 ? "color:#22c55e" : "color:#ef4444";
    tip.innerHTML = `<div><strong>#${tr.trade_number}</strong> @ ${formatNumber(tr.price)}</div><div>${time}</div><div style="${pnlCls}">PnL: ${pnl >= 0 ? "+" : ""}${formatNumber(pnl)}</div>`;
  } else {
    const parts = [];
    const sideLabel = marker.isOpenBuy ? "Open buy order" : marker.isOpenSell ? "Open sell order" : marker.isFutureSell ? "Future sell order" : marker.side === "buy" ? "Buy level" : "Sell level";
    parts.push(`<div><strong>${sideLabel}</strong> #${marker.level + 1}</div>`);
    parts.push(`<div>Price: ${formatNumber(marker.price)}</div>`);
    if (marker.isOpenSell) {
      parts.push(`<div><strong>verwacht PnL</strong>: ${marker.expectedPnl >= 0 ? "+" : ""}${formatNumber(marker.expectedPnl ?? 0)}</div>`);
    }
    tip.innerHTML = parts.join("");
  }
  tip.style.display = "block";
  tip.style.left = Math.min(mx + 12, canvas.clientWidth - 180) + "px";
  tip.style.top = (marker.y - 10) + "px";
}

// Tooltip on hover over trade and grid markers
document.getElementById("trade_chart_canvas")?.addEventListener("mousemove", (e) => {
  const canvas = e.target;
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;

  let hit = null;
  for (const m of _tradeChartGridMarkers) {
    const withinX = mx >= m.x1 && mx <= m.x2;
    const withinY = Math.abs(my - m.y) <= 6;
    if (withinX && withinY) { hit = m; break; }
  }
  if (!hit) {
    for (const m of _tradeChartMarkers) {
      const withinX = mx >= m.x1 && mx <= m.x2;
      const withinY = Math.abs(my - m.y) <= 6;
      if (withinX && withinY) { hit = m; break; }
    }
  }
  if (hit) _updateTradeChartTooltip(hit, canvas, mx);
  else document.getElementById("trade_chart_tooltip").style.display = "none";
});
document.getElementById("trade_chart_canvas")?.addEventListener("mouseleave", () => {
  document.getElementById("trade_chart_tooltip").style.display = "none";
});

// ──────────────────────────────────────────────────────────────
// Event handlers
// ──────────────────────────────────────────────────────────────

/** Create-bot button: validate grid, confirm if unprofitable, then POST. */
document.getElementById("create").onclick = async () => {
  try {
    const config = currentConfig();
    const limits = getMinimumRequiredOrderSizeQuote(config);
    if (limits && Number(config.grid.order_size_quote || 0) + 1e-12 < limits.requiredQuote) {
      const parts = [];
      if (limits.minQuote > 0) parts.push(`min quote: ${formatNumber(limits.minQuote)} ${limits.quoteCurrency}`);
      if (limits.minBase > 0) {
        parts.push(`min base: ${formatNumber(limits.minBase)} ${limits.baseCurrency} (= ${formatNumber(limits.minQuoteFromBase)} ${limits.quoteCurrency} at max grid price ${formatNumber(limits.maxGridPrice)})`);
      }
      throw new Error(`Order size ${formatNumber(config.grid.order_size_quote)} ${limits.quoteCurrency} is below the Bitvavo minimum. Minimum required order size is ${formatNumber(limits.requiredQuote)} ${limits.quoteCurrency}${parts.length ? ` (${parts.join("; ")})` : ""}`);
    }

    await checkGridProfitability();
    if (lastGridPreview && !lastGridPreview.is_profitable) {
      if (!await showConfirm(t("grid_confirm_unprofitable"))) return;
    }
    await api("/api/v1/bots", {
      method: "POST",
      body: JSON.stringify({
        name: document.getElementById("name").value,
        config,
      }),
    });
    await loadBots();
    document.getElementById("create_bot_modal").close();
  } catch (err) {
    showToast(t("grid_calc_error"), err.message || String(err), "warn", 5000);
  }
};

/**
 * Show a styled confirm modal and return a promise that resolves to true/false.
 *
 * @param {string} message - The confirmation message to display.
 * @param {string} [title] - Optional title for the modal header.
 * @returns {Promise<boolean>} True if confirmed, false if cancelled.
 */
function showConfirm(message, title = "") {
  return new Promise((resolve) => {
    const dialog = document.getElementById("confirm_modal");
    document.getElementById("confirm_modal_title").textContent = title || t("btn_confirm");
    document.getElementById("confirm_modal_message").textContent = message;
    const okBtn = document.getElementById("confirm_modal_ok");
    const cancelBtn = document.getElementById("confirm_modal_cancel");
    function cleanup(result) {
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      dialog.close();
      resolve(result);
    }
    okBtn.onclick = () => cleanup(true);
    cancelBtn.onclick = () => cleanup(false);
    dialog.showModal();
  });
}

function showDeleteBotModeModal(botName = "") {
  return new Promise((resolve) => {
    const dialog = document.getElementById("delete_bot_modal");
    const titleEl = document.getElementById("delete_bot_modal_title");
    const messageEl = document.getElementById("delete_bot_modal_message");
    const confirmBtn = document.getElementById("delete_bot_modal_confirm");
    const cancelBtn = document.getElementById("delete_bot_modal_cancel");
    const modeButtons = [...dialog.querySelectorAll("[data-delete-mode]")];
    let selectedMode = null;

    titleEl.textContent = t("delete_bot_modal_title");
    const baseMsg = t("delete_bot_modal_message");
    messageEl.textContent = botName ? `${baseMsg} (${botName})` : baseMsg;
    confirmBtn.disabled = true;
    modeButtons.forEach((btn) => btn.classList.remove("selected"));

    function cleanup(result) {
      confirmBtn.onclick = null;
      cancelBtn.onclick = null;
      modeButtons.forEach((btn) => { btn.onclick = null; });
      dialog.close();
      resolve(result);
    }

    confirmBtn.onclick = () => cleanup(selectedMode);
    cancelBtn.onclick = () => cleanup(null);
    modeButtons.forEach((btn) => {
      btn.onclick = () => {
        selectedMode = btn.dataset.deleteMode || null;
        modeButtons.forEach((el) => el.classList.toggle("selected", el === btn));
        confirmBtn.disabled = !selectedMode;
      };
    });
    dialog.showModal();
  });
}

/** Switch to the Agents tab programmatically. */
function switchToAgentsTab() {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
  document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
  const agentsBtn = document.querySelector('.tab-btn[data-tab="tab_agents"]');
  if (agentsBtn) agentsBtn.classList.add("active");
  document.getElementById("tab_agents")?.classList.add("active");
}

/** Tab switching: toggle active class on both buttons and panes. */
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab).classList.add("active");
    diagnosticsTabActive = btn.dataset.tab === "tab_diagnostics";
    if (diagnosticsTabActive) {
      loadDiagnosticsInstances();
      loadDiagnosticsLogs();
    }
    if (btn.dataset.tab === "tab_settings" && currentUser?.role === "admin" && !settingsLoadedOnce) {
      loadSettingsPage();
    }
  };
});

/** Open the "Create Bot" modal and load market data + balances. */
document.getElementById("open_create_bot").onclick = () => {
  document.getElementById("create_bot_modal").showModal();
  loadMarkets().then(() => {
    startMarketRealtime();
    loadBalances();
    loadMarketFees(true);
    renderMinimumOrderHint();
  });
};

document.getElementById("cancel_create_bot").onclick = () => { document.getElementById("create_bot_modal").close(); };

/** Debounce timer for auto grid profitability check. */
let _gridCheckTimer = null;

/** Auto-check grid profitability when any grid parameter changes. */
function scheduleGridCheck() {
  clearTimeout(_gridCheckTimer);
  _gridCheckTimer = setTimeout(() => checkGridProfitability(), 400);
}
["lower_price", "upper_price", "levels", "order_size_quote"].forEach(
  (id) => document.getElementById(id)?.addEventListener("input", scheduleGridCheck)
);
document.getElementById("order_size_quote")?.addEventListener("input", () => renderMinimumOrderHint());
document.getElementById("lower_price")?.addEventListener("input", () => renderMinimumOrderHint());
document.getElementById("upper_price")?.addEventListener("input", () => renderMinimumOrderHint());
document.getElementById("mode")?.addEventListener("change", () => renderMinimumOrderHint());

/**
 * Fetch the average high/low for the selected market over the
 * configured lookback period and fill in the lower/upper fields.
 */
document.getElementById("btn_suggest_range").onclick = async () => {
  const market = normalizeMarketValue(getMarketInput()?.value);
  const days = Number(document.getElementById("lookback_days").value) || 7;
  const btn = document.getElementById("btn_suggest_range");
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const r = await api(`/api/v1/market/price-range?market=${encodeURIComponent(market)}&days=${days}`);
    document.getElementById("lower_price").value = formatNumber(r.avg_low);
    document.getElementById("upper_price").value = formatNumber(r.avg_high);
    syncBudgetAndOrderSize("levels");
  } catch (err) {
    showToast(t("grid_calc_error"), String(err.message || err), "warn", 4000);
  } finally {
    btn.disabled = false;
    btn.textContent = t("btn_suggest_range");
  }
};

/** Run a quick backtest using the current form parameters. */
document.getElementById("backtest").onclick = async () => {
  const result = await api("/api/v1/backtest", { method: "POST", body: JSON.stringify({ config: currentConfig() }) });
  document.getElementById("backtest_result").textContent = JSON.stringify(result, null, 2);
};

/** Market combobox interactions: type to filter, enter to select, arrows to navigate. */
const marketInputEl = getMarketInput();
marketInputEl?.addEventListener("focus", () => {
  renderMarketSuggestions(marketInputEl.value);
});
marketInputEl?.addEventListener("input", () => {
  marketInputEl.value = normalizeMarketValue(marketInputEl.value);
  renderMarketInputIcon();
  renderMarketSuggestions(marketInputEl.value);
  if (marketMeta.has(marketInputEl.value)) {
    onMarketValueCommitted();
  }
});
marketInputEl?.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") {
    e.preventDefault();
    updateMarketHighlight(1);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    updateMarketHighlight(-1);
  } else if (e.key === "Enter") {
    const menu = document.getElementById("market_suggestions");
    if (menu?.classList.contains("open")) {
      const items = [...menu.querySelectorAll(".combo-item")];
      if (items.length && marketHighlightIndex >= 0) {
        e.preventDefault();
        items[marketHighlightIndex].click();
        return;
      }
    }
    onMarketValueCommitted();
  } else if (e.key === "Escape") {
    closeMarketSuggestions();
  }
});
marketInputEl?.addEventListener("blur", () => {
  setTimeout(() => {
    closeMarketSuggestions();
    onMarketValueCommitted();
  }, 120);
});

/**
 * Show/hide the "Skim ratio" field based on profit mode.
 * Only "skim" mode uses a ratio.
 */
function toggleSkimRatio() {
  const createModalBody = document.querySelector("#create_bot_modal .modal-body");
  const prevScrollTop = createModalBody ? createModalBody.scrollTop : 0;

  const skimRatioLabel = document.getElementById("skim_ratio_label");
  const skimRatioInput = document.getElementById("skim_ratio");
  const isSkim = document.getElementById("profit_mode").value === "skim";
  skimRatioLabel.classList.toggle("is-hidden", !isSkim);
  skimRatioInput.disabled = !isSkim;

  if (createModalBody) {
    requestAnimationFrame(() => {
      createModalBody.scrollTop = prevScrollTop;
    });
  }
}
document.getElementById("profit_mode").addEventListener("change", toggleSkimRatio);
document.getElementById("mode")?.addEventListener("change", () => {
  loadMarketFees(false);
});
toggleSkimRatio();

document.getElementById("modal_refresh_agent_logs").onclick = async () => { await loadAgentLogs(); };
document.getElementById("modal_close_agent_logs").onclick = () => { closeLogsModal(); };
document.getElementById("modal_log_category").onchange = async () => { if (logsModalOpen) await loadAgentLogs(); };
document.getElementById("diag_refresh_btn")?.addEventListener("click", async () => {
  await loadDiagnosticsInstances();
  await loadDiagnosticsLogs();
});
document.getElementById("diag_download_btn")?.addEventListener("click", async () => {
  try {
    await downloadDiagnosticsLogs();
  } catch (err) {
    showToast(t("logs_error"), String(err.message || err), "warn", 4000);
  }
});
document.getElementById("settings_reload_btn")?.addEventListener("click", async () => {
  await loadSettingsPage();
});
document.getElementById("settings_save_btn")?.addEventListener("click", async () => {
  await saveSettingsPage();
});
document.getElementById("settings_exchange_add_btn")?.addEventListener("click", async () => {
  await _createExchangeFromForm();
});
document.getElementById("settings_exchange_update_btn")?.addEventListener("click", async () => {
  await _updateExchangeFromForm();
});
document.getElementById("settings_exchange_cancel_btn")?.addEventListener("click", () => {
  _resetExchangeForm();
});
document.getElementById("settings_exchanges")?.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const action = target.getAttribute("data-action");
  const exchangeId = Number(target.getAttribute("data-exchange-id") || 0);
  if (!action || !exchangeId) return;
  if (action === "edit") {
    _loadExchangeIntoForm(exchangeId);
    return;
  }
  if (action === "delete") {
    await _deleteExchange(exchangeId);
  }
});
_bindSettingsSubtabs();

// ──────────────────────────────────────────────────────────────
// Language switcher (flag buttons)
// ──────────────────────────────────────────────────────────────

/**
 * Highlight the active language flag and dim the rest.
 */
function updateLangFlags() {
  document.querySelectorAll(".lang-flag").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.lang === lang);
  });
}

updateLangFlags();
document.querySelectorAll(".lang-flag").forEach((btn) => {
  btn.onclick = async () => {
    lang = btn.dataset.lang;
    localStorage.setItem("cryptobot_lang", lang);
    setNumberLocaleOverride(getNumberLocaleForLanguage(lang));
    updateLangFlags();
    applyTranslations();
    refreshLogsModalTitle();
    renderFeeInfo(lastFeeSnapshot);
    if (marketSnapshot) renderMarketSummary(marketSnapshot);
    refreshAllAppSelects();
    try { await api("/api/v1/auth/locale", { method: "POST", body: JSON.stringify({ locale: lang }) }); } catch { /* best-effort locale sync */ }
  };
});

// ──────────────────────────────────────────────────────────────
// Logout
// ──────────────────────────────────────────────────────────────

document.getElementById("btn_logout").onclick = () => {
  localStorage.removeItem("cryptobot_token");
  localStorage.removeItem("cryptobot_session_start");
  localStorage.removeItem("cryptobot_session_max");
  globalThis.location.href = "/login";
};

/** Check if the session has expired and log out automatically. */
function checkSessionExpiry() {
  const start = Number(localStorage.getItem("cryptobot_session_start") || 0);
  const max = Number(localStorage.getItem("cryptobot_session_max") || 0);
  if (start && max && Date.now() > start + max * 1000) {
    localStorage.removeItem("cryptobot_token");
    localStorage.removeItem("cryptobot_session_start");
    localStorage.removeItem("cryptobot_session_max");
    globalThis.location.href = "/login";
  }
}

let dashboardEventSource = null;
let realtimeRefreshTimer = null;
let realtimeRefreshInFlight = false;
let realtimeRefreshQueued = false;

async function runRealtimeRefresh() {
  if (realtimeRefreshInFlight) {
    realtimeRefreshQueued = true;
    return;
  }
  realtimeRefreshInFlight = true;
  try {
    await loadBots();
    await loadEvents();
    await loadTradeEvents();
    await loadOrders();
    await loadEquityChart();
    if (logsModalOpen) await loadAgentLogs();
  } catch {
    // Keep fallback polling as safety net when realtime updates fail.
  } finally {
    realtimeRefreshInFlight = false;
    if (realtimeRefreshQueued) {
      realtimeRefreshQueued = false;
      await runRealtimeRefresh();
    }
  }
}

function scheduleRealtimeRefresh() {
  if (realtimeRefreshTimer) clearTimeout(realtimeRefreshTimer);
  realtimeRefreshTimer = setTimeout(() => {
    realtimeRefreshTimer = null;
    runRealtimeRefresh();
  }, 150);
}

function connectDashboardRealtimeStream() {
  ensureManagerUiSocket().catch(() => {
    // Keep interval fallback active when websocket connect fails.
  });
}

// ──────────────────────────────────────────────────────────────
// Initialisation
// ──────────────────────────────────────────────────────────────

(async () => {
  // Redirect to login if there is no stored token
  if (!authToken) { globalThis.location.href = "/login"; return; }

  // Check session expiry before doing anything else
  checkSessionExpiry();

  // Validate the token and fetch the current user profile
  try {
    currentUser = await api("/api/v1/auth/me");
    // If the user still needs to change their password, send them back
    if (currentUser.must_change_password) { globalThis.location.href = "/login"; return; }
  } catch {
    globalThis.location.href = "/login";
    return;
  }

  // Apply server-side locale preference
  if (currentUser.locale) {
    lang = currentUser.locale;
    localStorage.setItem("cryptobot_lang", lang);
    updateLangFlags();
  }

  // Render user info in the header bar
  document.getElementById("user_display").textContent = currentUser.username;
  document.getElementById("role_display").textContent = currentUser.role;

  applyTranslations();
  initAllAppSelects();
  initEquityAggregationSelector();
  applyRBAC();

  // Initial data load
  await loadMarkets();
  await loadBots();
  await loadAgents();
  await loadEvents();
  await loadTradeEvents();
  await loadEquityChart();
  await loadOrders();
  connectDashboardRealtimeStream();
})();

// ──────────────────────────────────────────────────────────────
// Polling intervals
// ──────────────────────────────────────────────────────────────

/** Fallback refresh when realtime SSE is temporarily unavailable. */
setInterval(async () => { checkSessionExpiry(); await loadBots(); await loadAgents(); await loadEvents(); await loadTradeEvents(); await loadEquityChart(); await loadOrders(); if (logsModalOpen) await loadAgentLogs(); }, 5000);

/** Refresh market summary from REST every 60 seconds (WebSocket handles real-time). */
setInterval(async () => { await loadMarketSummary(); }, 60000);

/** Poll agent logs every 2 seconds while the modal is open. */
setInterval(async () => { if (logsModalOpen) await loadAgentLogs(); }, 2000);

/** Refresh diagnostics panel while active. */
setInterval(async () => {
  if (!diagnosticsTabActive) return;
  await loadDiagnosticsInstances();
  await loadDiagnosticsLogs();
}, 8000);

// ──────────────────────────────────────────────────────────────
// Tooltip positioning (hover-triggered info tips)
// ──────────────────────────────────────────────────────────────

(function () {
  // Create a single shared tooltip element
  const tipEl = document.createElement("div");
  tipEl.className = "tip-popup";
  tipEl.style.display = "none";
  document.body.appendChild(tipEl);

  const hoverSelector = ".info-tip, .grid-fee-cell";

  function renderTipContent(trigger, text) {
    const isFeeTip = trigger.classList.contains("grid-fee-cell");
    tipEl.classList.toggle("fee-popup", isFeeTip);
    tipEl.classList.toggle("info-popup", !isFeeTip);

    if (!isFeeTip) {
      tipEl.textContent = text;
      return;
    }

    const lines = String(text)
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);

    const title = lines[0] || t("lbl_total_fees");
    const rows = lines.slice(1);
    tipEl.innerHTML = `
      <div class="tip-title">${title}</div>
      <div class="tip-body">
        ${rows.map((line) => `<div class="tip-row">${line}</div>`).join("")}
      </div>
    `;
  }

  document.addEventListener("mouseover", (e) => {
    const target = e.target instanceof Element ? e.target : null;
    const trigger = target ? target.closest(hoverSelector) : null;
    if (!trigger) return;
    const text = trigger.dataset.tip || trigger.dataset.feeTip;
    if (!text) return;

    // Move tooltip into the open dialog (top-layer) so it renders above it
    const openDialog = trigger.closest("dialog[open]");
    const tipParent = openDialog || document.body;
    if (tipEl.parentNode !== tipParent) tipParent.appendChild(tipEl);

    renderTipContent(trigger, text);
    tipEl.style.display = "block";

    // Position above the trigger icon, horizontally centred
    const rect = trigger.getBoundingClientRect();
    const tipRect = tipEl.getBoundingClientRect();
    let left = rect.left + rect.width / 2 - tipRect.width / 2;
    let top = rect.top - tipRect.height - 6;

    // Clamp horizontally to viewport
    if (left < 4) left = 4;
    if (left + tipRect.width > globalThis.innerWidth - 4) left = globalThis.innerWidth - tipRect.width - 4;

    // If it overflows the top, show below instead
    if (top < 4) top = rect.bottom + 6;

    tipEl.style.left = left + "px";
    tipEl.style.top = top + "px";
  });

  document.addEventListener("mouseout", (e) => {
    const target = e.target instanceof Element ? e.target : null;
    const related = e.relatedTarget instanceof Element ? e.relatedTarget : null;
    if (!target) return;
    const fromTrigger = target.closest(hoverSelector);
    const toTrigger = related ? related.closest(hoverSelector) : null;
    if (fromTrigger && !toTrigger) tipEl.style.display = "none";
  });
})();
