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

/** Whether the agent-logs modal is currently visible. */
let logsModalOpen = false;

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

/** Metadata map (market → {base, quote, status}) loaded from /api/markets. */
const marketMeta = new Map();
let marketOptions = [];
let marketHighlightIndex = -1;
let lastFeeSnapshot = null;
const cryptoLogoBySymbol = new Map();
let coinMapLoadPromise = null;
let marketIconObserver = null;
const MAX_DECIMALS = 8;

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
        if (!key || !url) continue;
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
  const headers = { "Content-Type": "application/json", ...options.headers };
  if (authToken) headers["Authorization"] = "Bearer " + authToken;

  const res = await fetch(url, { ...options, headers });

  // Session expired or invalid → redirect to login
  if (res.status === 401) {
    localStorage.removeItem("cryptobot_token");
    globalThis.location.href = "/login";
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const txt = await res.text();
      try {
        const body = JSON.parse(txt);
        msg = body.detail || JSON.stringify(body);
      } catch {
        if (txt) msg = txt;
      }
    } catch { /* empty */ }
    throw new Error(msg);
  }
  return res.headers.get("content-type")?.includes("application/json") ? res.json() : res.text();
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
    quoteEl.textContent = "n/a";
    baseEl.textContent = "n/a";
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

/**
 * Fetch all bots from the API and render them into the bots table.
 * Start/Stop buttons are hidden for viewer-role users.
 */
async function loadBots() {
  const bots = await api("/api/v1/bots");
  const agents = await api("/api/v1/agents");
  const moveCandidates = agents.filter((a) => a.approval_status === "approved");
  const body = document.getElementById("bots_body");
  const isViewer = currentUser?.role === "viewer";

  // Populate the equity chart bot selector (preserve selection)
  const chartSelect = document.getElementById("equity_chart_bot");
  if (chartSelect) {
    const currentVal = chartSelect.value;
    chartSelect.innerHTML = `<option value="">${t("lbl_select_bot")}</option><option value="__total__"${currentVal === "__total__" ? " selected" : ""}>${t("lbl_total_all_bots")}</option>`;
    for (const bot of bots) {
      const opt = document.createElement("option");
      opt.value = bot.id;
      opt.textContent = bot.name;
      if (bot.id === currentVal) opt.selected = true;
      chartSelect.appendChild(opt);
    }
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
    const pnl = Number(m.dashboard_pnl_quote ?? m.realized_pnl_quote ?? 0);
    const trades = Number(m.trade_count || 0);
    const runtime = formatUptime(m.runtime_seconds || 0);
    const lastPrice = Number(m.price || 0);
    const market = bot.config?.market || "-";
    const lowerPrice = Number(bot.config?.grid?.lower_price || 0);
    const upperPrice = Number(bot.config?.grid?.upper_price || 0);
    const isOutsideGrid = lastPrice > 0 && lowerPrice > 0 && upperPrice > 0 && (lastPrice < lowerPrice || lastPrice > upperPrice);
    const nameHtml = isOutsideGrid
      ? `<span class="bot-name-cell bot-grid-outside">${bot.name}<span class="bot-grid-warning" title="${t("bot_grid_warning")}">&#9888;</span></span>`
      : bot.name;

    const modeLabel = bot.mode === "live" ? "Live" : "Sim";
    const priceStr = lastPrice > 0 ? formatNumber(lastPrice) : "-";
    let statusHtml;
    if (bot.status === "initializing") {
      statusHtml = `<span class="status-badge status-initializing">${t("lbl_initializing")}</span>`;
    } else if (bot.status === "running") {
      statusHtml = `<span class="status-badge status-running">${t("lbl_running")}</span>`;
    } else if (bot.status === "queued") {
      statusHtml = `<span class="status-badge status-queued">${t("lbl_queued")}</span>`;
    } else if (bot.status === "stopped") {
      statusHtml = `<span class="status-badge status-stopped">${t("lbl_stopped")}</span>`;
    } else {
      statusHtml = bot.status;
    }
    const agentHtml = buildAgentCellHtml(bot, moveCandidates, isViewer);
    const agentTitle = bot.assigned_agent_id || "";

    let tr = body.querySelector(`tr[data-bot-id="${bot.id}"]`);
    if (tr) {
      // Update existing row cells in-place (skip actions column to preserve dropdown state)
      const cells = tr.children;
      cells[0].innerHTML = nameHtml;
      cells[1].textContent = market;
      cells[2].innerHTML = modeLabel;
      cells[3].innerHTML = statusHtml;
      cells[4].innerHTML = agentHtml;
      cells[4].title = agentTitle;
      cells[5].textContent = priceStr;
      cells[6].textContent = formatNumber(m.total_equity_quote || 0);
      cells[7].className = pnl >= 0 ? "pnl-positive" : "pnl-negative";
      cells[7].textContent = formatNumber(pnl);
      cells[8].textContent = trades;
      cells[9].textContent = runtime;
      // cells[10] is the actions dropdown — leave it untouched
    } else {
      // Create new row
      tr = document.createElement("tr");
      tr.dataset.botId = bot.id;
      const acts = isViewer
        ? "<td>-</td>"
        : `<td><div class="action-dropdown" data-bot-id="${bot.id}"><button class="action-toggle">${t("btn_actions")} ▾</button><div class="action-menu"><button data-action="start">${t("btn_start")}</button><button data-action="stop">${t("btn_stop")}</button><button data-action="chart">${t("btn_chart")}</button><button data-action="orders">${t("btn_orders")}</button><button data-action="bot_logs">${t("btn_bot_log")}</button><button data-action="delete" class="danger">${t("btn_delete")}</button></div></div></td>`;
      tr.innerHTML = `<td>${nameHtml}</td><td>${market}</td><td>${modeLabel}</td><td>${statusHtml}</td><td title="${agentTitle}">${agentHtml}</td><td>${priceStr}</td><td>${formatNumber(m.total_equity_quote || 0)}</td><td class="${pnl >= 0 ? "pnl-positive" : "pnl-negative"}">${formatNumber(pnl)}</td><td>${trades}</td><td>${runtime}</td>${acts}`;
      body.appendChild(tr);
      _wireUpBotRow(tr, bots);
    }
  }
}

/** Wire action dropdown handlers for a single bot row. */
function _wireUpBotRow(tr, bots) {
  const isViewer = currentUser?.role === "viewer";
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
      const bot = bots.find((x) => x.id === botId);
      btn.closest(".action-menu").classList.remove("open");
      if (!action || !bot) return;

      try {
        if (action === "start") {
          await api(`/api/v1/bots/${botId}/start`, { method: "POST", body: JSON.stringify({}) });
        } else if (action === "stop") {
          await api(`/api/v1/bots/${botId}/stop`, { method: "POST" });
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
          await api(`/api/v1/bots/${botId}`, {
            method: "DELETE",
            body: JSON.stringify({ delete_mode: deleteMode }),
          });
        }
      } catch (err) {
        showToast(t("btn_" + action) || action, err.message || String(err), "warn", 5000);
      }
      await loadBots();
      await loadOrders();
      await loadTradeEvents();
      await loadEquityChart();
    };
  });
}

// Close menus on outside click (register once)
document.addEventListener("click", (e) => {
  closeAllAppSelects();
  document.querySelectorAll(".action-menu.open").forEach((m) => m.classList.remove("open"));
  document.querySelectorAll(".agent-move-dropdown.open").forEach((m) => m.classList.remove("open"));
  if (!e.target.closest(".combo-wrap")) {
    closeMarketSuggestions();
  }
});

document.addEventListener("click", async (e) => {
  const trigger = e.target.closest(".agent-move-pill");
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

  const moveBtn = e.target.closest(".agent-move-item");
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
  if (e.target?.closest?.(".combo-wrap") || e.target?.closest?.("#market_suggestions")) {
    return;
  }
  if (e.target.closest(".modal") || e.target === document) {
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
    const runtime = formatUptime(bot.latest_metrics?.runtime_seconds ?? 0);
    html += `<tr><td>${bot.name}</td><td>${bot.market}</td><td>${bot.status}</td><td>${bot.trade_count}</td><td>${runtime}</td><td>${formatNumber(bot.quote_balance)}</td><td>${formatNumber(bot.base_balance)}</td></tr>`;
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
// Agent events (notification feed)
// ──────────────────────────────────────────────────────────────

/**
 * Fetch agent lifecycle events and render them into the notifications
 * panel. Newly discovered agents trigger a popup notification once.
 */
async function loadEvents() {
  const events = await api("/api/v1/agent-events");
  const list = document.getElementById("events_list");
  list.innerHTML = "";

  for (const event of events) {
    // Show a toast for newly discovered or dead agents (only once)
    if (!seenEventIds.has(event.id)) {
      seenEventIds.add(event.id);
      if (event.event_type === "discovered") {
        showToast(t("toast_agent_discovered"), event.message, "info", 5000, switchToAgentsTab);
      } else if (event.event_type === "offline") {
        showToast(t("toast_agent_dead"), event.message, "warn", 5000, switchToAgentsTab);
      }
    }
    const item = document.createElement("div");
    item.className = "event-item";
    const timeLabel = new Date(event.timestamp).toLocaleString();
    item.innerHTML = `<div><strong>[${event.event_type}]</strong> <strong>[${event.agent_id}]</strong> ${event.message}</div><div class="event-time">${timeLabel}</div>`;
    list.appendChild(item);
  }
}

// ──────────────────────────────────────────────────────────────
// Trade events
// ──────────────────────────────────────────────────────────────

/** Set of trade event IDs already shown as toasts. */
const seenTradeIds = new Set();
/** Whether the initial trade-event load has completed (suppress toasts on first load). */
let _tradeEventsInitialised = false;

/**
 * Fetch trade events from the API and render them into the
 * trade notifications panel. New trades trigger a toast.
 */
async function loadTradeEvents() {
  const events = await api("/api/v1/trade-events");
  const list = document.getElementById("trade_events_list");
  if (!list) return;
  list.innerHTML = "";

  // On first load, seed the seen-set without firing toasts
  if (!_tradeEventsInitialised) {
    for (const ev of events) seenTradeIds.add(ev.id);
    _tradeEventsInitialised = true;
  }

  for (const ev of events) {
    const evType = ev.event_type || "trade";
    const isPlacement = evType === "order_placed";
    const isFill = evType === "order_filled";

    // Toast for new events
    if (!seenTradeIds.has(ev.id)) {
      seenTradeIds.add(ev.id);
      if (isPlacement) {
        showToast(
          t("toast_order_placed"),
          `${ev.bot_name} ${ev.side.toUpperCase()} ${formatNumber(ev.quote_amount)} @ ${formatNumber(ev.price)}`,
          "info",
          3000,
        );
      } else {
        const pnlStr = ev.trade_pnl >= 0 ? `+${formatNumber(ev.trade_pnl)}` : formatNumber(ev.trade_pnl);
        showToast(
          isFill ? t("toast_order_filled") : `${t("toast_trade")} #${ev.trade_number}`,
          `${ev.bot_name} ${ev.side.toUpperCase()} @ ${formatNumber(ev.price)} | PnL: ${pnlStr}`,
          ev.trade_pnl >= 0 ? "info" : "warn",
          4000,
        );
      }
    }

    const item = document.createElement("div");
    const sideClass = ev.side === "buy" ? "trade-buy" : ev.side === "sell" ? "trade-sell" : "";
    const timeLabel = new Date(ev.timestamp).toLocaleString();

    if (isPlacement) {
      const levelStr = ev.level_index != null ? ` L${ev.level_index}` : "";
      item.className = `event-item ${sideClass} event-placement`;
      item.innerHTML = `<div><strong>📋 ${ev.side.toUpperCase()}</strong>${levelStr} ${ev.bot_name} — ${formatNumber(ev.quote_amount)} @ ${formatNumber(ev.price)}</div><div class="event-time">${timeLabel}</div>`;
    } else {
      const pnlClass = ev.trade_pnl >= 0 ? "trade-pnl-pos" : "trade-pnl-neg";
      const pnlStr = ev.trade_pnl >= 0 ? `+${formatNumber(ev.trade_pnl)}` : formatNumber(ev.trade_pnl);
      const icon = isFill ? "✅" : "🔄";
      const label = isFill ? t("toast_order_filled") : `#${ev.trade_number}`;
      const fillParts = isFill && Number(ev.fill_count || 0) > 0 ? ` | ${t("lbl_fill_parts")}: ${Number(ev.fill_count)}` : "";
      item.className = `event-item ${sideClass}`;
      item.innerHTML = `<div><strong>${icon} ${label}</strong> ${ev.bot_name} ${ev.side.toUpperCase()} @ ${formatNumber(ev.price)} &mdash; <span class="${pnlClass}">${pnlStr}</span>${fillParts}</div><div class="event-time">${timeLabel}</div>`;
    }
    list.appendChild(item);
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

// ──────────────────────────────────────────────────────────────
// Equity trend chart (pure Canvas 2D)
// ──────────────────────────────────────────────────────────────

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

  // Size canvas to CSS size at device pixel ratio
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width;
  const H = rect.height;

  ctx.clearRect(0, 0, W, H);

  if (!data || data.length < 2) {
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
    ctx.fillText(formatNumber(v), pad.left - 6, y + 4);
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
  const infoDiv = document.getElementById("equity_chart_info");
  if (!botId) {
    drawEquityChart([]);
    if (infoDiv) infoDiv.style.display = "none";
    return;
  }
  try {
    const url = botId === "__total__"
      ? "/api/v1/bots/equity-history/total"
      : `/api/v1/bots/${botId}/equity-history`;
    const resp = await api(url);
    const points = resp.points || [];
    const startingBudget = resp.starting_budget || 0;
    const pnl = resp.pnl || 0;
    const totalEquity = resp.total_equity || 0;

    // Update info labels above chart
    if (infoDiv) {
      infoDiv.style.display = "";
      document.getElementById("equity_info_budget").textContent = formatNumber(startingBudget);
      const pnlEl = document.getElementById("equity_info_pnl");
      pnlEl.textContent = (pnl >= 0 ? "+" : "") + formatNumber(pnl);
      pnlEl.className = pnl >= 0 ? "pnl-positive" : "pnl-negative";
      document.getElementById("equity_info_total").textContent = formatNumber(totalEquity);
    }

    drawEquityChart(points, startingBudget);
  } catch {
    drawEquityChart([]);
    if (infoDiv) infoDiv.style.display = "none";
  }
}

// Redraw chart when the bot selector changes
document.getElementById("equity_chart_bot")?.addEventListener("change", loadEquityChart);

// ──────────────────────────────────────────────────────────────
// Orders overview table
// ──────────────────────────────────────────────────────────────

/** Load all order events into the Orders tab table. */
async function loadOrders() {
  const body = document.getElementById("orders_body");
  if (!body) return;
  try {
    const events = await api("/api/v1/trade-events");
    body.innerHTML = "";
    for (const ev of events) {
      const tr = document.createElement("tr");
      tr.style.cursor = "pointer";
      const ts = new Date(ev.timestamp).toLocaleString();
      const typeClass = ev.event_type === "order_placed" ? "order-type-placed"
        : ev.event_type === "order_filled" ? "order-type-filled"
        : "order-type-cancelled";
      const typeLabel = ev.event_type === "order_placed" ? `📋 ${t("lbl_placed")}`
        : ev.event_type === "order_filled" ? `✅ ${t("lbl_filled")}`
        : ev.event_type === "order_cancelled" ? `❌ ${t("lbl_cancelled")}`
        : `🔄 ${t("toast_trade")}`;
      const pnlStr = ev.trade_pnl !== 0 ? ((ev.trade_pnl >= 0 ? "+" : "") + formatNumber(ev.trade_pnl)) : "-";
      const pnlClass = ev.trade_pnl > 0 ? "pnl-positive" : ev.trade_pnl < 0 ? "pnl-negative" : "";
      const sideClass = ev.side === "buy" ? "order-buy" : ev.side === "sell" ? "order-sell" : "";
      const marketLabel = ev.market || ev.bot_name || "-";
      const feeStr = Number(ev.fee_paid_quote || 0) > 0 ? formatNumber(ev.fee_paid_quote) : "-";
      tr.innerHTML = `<td>${ts}</td><td>${marketLabel}</td><td class="${typeClass}">${typeLabel}</td><td class="${sideClass}">${ev.side.toUpperCase()}</td><td>${formatNumber(ev.price)}</td><td>${formatNumber(ev.quote_amount)}</td><td>${feeStr}</td><td class="${pnlClass}">${pnlStr}</td>`;
      tr.onclick = () => openOrderDetail(ev.id);
      body.appendChild(tr);
    }
  } catch { /* ignore */ }
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
    const typeLabel = ev.event_type === "order_placed" ? `📋 ${t("lbl_placed")}`
      : ev.event_type === "order_filled" ? `✅ ${t("lbl_filled")}`
      : ev.event_type === "order_cancelled" ? `❌ ${t("lbl_cancelled")}`
      : `🔄 ${t("toast_trade")}`;
    const sideClass = ev.side === "buy" ? "order-buy" : "order-sell";
    const pnlClass = ev.trade_pnl > 0 ? "pnl-positive" : ev.trade_pnl < 0 ? "pnl-negative" : "";
    const pnlStr = ev.trade_pnl !== 0 ? ((ev.trade_pnl >= 0 ? "+" : "") + formatNumber(ev.trade_pnl)) : "-";

    let html = `<div class="order-detail-grid">`;
    html += `<div class="od-row"><span class="od-label">${t("th_type")}</span><span>${typeLabel}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("th_side")}</span><span class="${sideClass}">${ev.side.toUpperCase()}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("th_market")}</span><span>${ev.market || ev.bot_name || "-"}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("th_price")}</span><span>${formatNumber(ev.price)}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("th_amount")}</span><span>${formatNumber(ev.quote_amount)}</span></div>`;
    if (ev.event_type === "order_filled") {
      html += `<div class="od-row"><span class="od-label">${t("lbl_fill_parts")}</span><span>${Number(ev.fill_count || 0) > 0 ? Number(ev.fill_count) : "-"}</span></div>`;
    }
    html += `<div class="od-row"><span class="od-label">${t("th_fee_amount")}</span><span>${Number(ev.fee_paid_quote || 0) > 0 ? formatNumber(ev.fee_paid_quote) : "-"}</span></div>`;
    html += `<div class="od-row"><span class="od-label">${t("th_time")}</span><span>${new Date(ev.timestamp).toLocaleString()}</span></div>`;
    if (ev.side !== "buy") {
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
    } else if (ev.side === "buy") {
      html += `<p style="color:#94a3b8;margin-top:12px;font-size:0.9rem;">${t("lbl_pair_pnl_pending_sell_fill")}</p>`;
    }

    // Linked order section
    if (ev.linked_order) {
      const lo = ev.linked_order;
      const loSideClass = lo.side === "buy" ? "order-buy" : "order-sell";
      const loTypeLabel = lo.event_type === "order_filled" ? `✅ ${t("lbl_filled")}` : lo.event_type;
      const loPnlStr = lo.trade_pnl !== 0 ? ((lo.trade_pnl >= 0 ? "+" : "") + formatNumber(lo.trade_pnl)) : "-";
      const linkedLabel = ev.side === "sell" ? t("lbl_linked_buy") : t("lbl_linked_sell");
      html += `<h4 style="margin:14px 0 6px;">${linkedLabel}</h4>`;
      html += `<div class="order-detail-grid">`;
      html += `<div class="od-row"><span class="od-label">${t("th_type")}</span><span>${loTypeLabel}</span></div>`;
      html += `<div class="od-row"><span class="od-label">${t("th_side")}</span><span class="${loSideClass}">${lo.side.toUpperCase()}</span></div>`;
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
        const sideClass = o.side === "buy" ? "order-buy" : "order-sell";
        const amt = formatNumber(o.quote_amount || 0);
        const filled = formatNumber(o.filled_quote || 0);
        tr.innerHTML = `<td>${o.level}</td><td>${formatNumber(o.price)}</td><td class="${sideClass}">${o.side.toUpperCase()}</td><td>${amt}</td><td>${filled}</td>`;
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
        const cls = order.side === "buy" ? "order-buy" : "order-sell";
        statusHtml = `<span class="${cls}">${order.side.toUpperCase()}</span>`;
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
  if (typeof EventSource === "undefined") return;
  if (dashboardEventSource) {
    try { dashboardEventSource.close(); } catch { /* already closed */ }
  }

  dashboardEventSource = new EventSource("/api/v1/stream/dashboard");
  const onUpdate = () => scheduleRealtimeRefresh();

  dashboardEventSource.addEventListener("trade_event", onUpdate);
  dashboardEventSource.addEventListener("equity_point", onUpdate);
  dashboardEventSource.addEventListener("agent_event", async () => {
    await loadAgents();
    await loadEvents();
    scheduleRealtimeRefresh();
  });
  dashboardEventSource.onmessage = onUpdate;
  dashboardEventSource.onerror = () => {
    // Browser EventSource auto-reconnects; keep polling fallback active.
  };
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
