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

/** Whether the agent-logs modal is currently visible. */
let logsModalOpen = false;

/** Active Bitvavo WebSocket connection for live market data. */
let marketSocket = null;

/** The market string the WebSocket is currently subscribed to. */
let marketSocketMarket = null;

/** Timer handle for WebSocket reconnection back-off. */
let marketReconnectTimer = null;

/** Latest aggregated market snapshot used by renderMarketSummary(). */
let marketSnapshot = null;

/** Metadata map (market → {base, quote, status}) loaded from /api/markets. */
const marketMeta = new Map();

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
    const txt = await res.text();
    throw new Error(txt || `HTTP ${res.status}`);
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
  const market = document.getElementById("market").value;
  const marketInfo = marketMeta.get(market) || {};
  const split = market.split("-");
  return {
    market,
    base_currency: marketInfo.base || split[0] || "",
    quote_currency: marketInfo.quote || split[1] || "",
    mode: document.getElementById("mode").value,
    strategy: "static_grid",
    start_price: 0,
    grid: {
      lower_price: Number(document.getElementById("lower_price").value),
      upper_price: Number(document.getElementById("upper_price").value),
      levels: Number(document.getElementById("levels").value),
      order_size_quote: Number(document.getElementById("order_size_quote").value),
    },
    budget: {
      quote_budget: Number(document.getElementById("quote_budget").value),
      base_budget: 0,
      profit_mode: document.getElementById("profit_mode").value,
      skim_ratio: Number(document.getElementById("skim_ratio").value),
    },
  };
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
function formatNumber(value, digits = 6) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: digits });
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
  const volBaseEl = document.getElementById("market_volume_base");
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

  lastEl.textContent = formatNumber(last, 8);
  changeEl.textContent = `${diffAbs >= 0 ? "+" : ""}${formatNumber(diffAbs, 8)} (${diffPct >= 0 ? "+" : ""}${formatNumber(diffPct, 4)}%)`;

  // Colour-code positive / negative
  changeEl.classList.remove("market-positive", "market-negative");
  changeEl.classList.add(diffAbs >= 0 ? "market-positive" : "market-negative");
  lastEl.classList.remove("market-positive", "market-negative");
  lastEl.classList.add(diffAbs >= 0 ? "market-positive" : "market-negative");

  volBaseEl.textContent = formatNumber(volumeBase, 8);
  volQuoteEl.textContent = formatNumber(volumeQuote, 2);
}

/**
 * Reset all four market-stat elements to "N/A" and remove colour classes.
 */
function resetMarketSummaryToNA() {
  for (const id of ["market_last_price", "market_change", "market_volume_base", "market_volume_quote"]) {
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
  const sel = document.getElementById("market");
  try {
    const markets = await api("/api/v1/markets?status=trading");
    const cur = sel.value;
    sel.innerHTML = "";
    marketMeta.clear();
    for (const item of markets) {
      marketMeta.set(item.market, item);
      const o = document.createElement("option");
      o.value = item.market;
      o.textContent = item.market;
      sel.appendChild(o);
    }
    // Restore previous selection or default to BTC-EUR
    if (cur && marketMeta.has(cur)) sel.value = cur;
    else if (marketMeta.has("BTC-EUR")) sel.value = "BTC-EUR";
  } catch (err) {
    console.error("Failed to load markets", err);
  }
}

/**
 * Fetch the latest 24h market summary from the REST API
 * and update the market stats panel.
 */
async function loadMarketSummary() {
  const market = document.getElementById("market").value.trim();
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
  const market = document.getElementById("market").value.trim();
  const quoteEl = document.getElementById("avail_quote");
  const baseEl = document.getElementById("avail_base");
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
    baseEl.textContent = `${Number(baseData.available).toFixed(8)} ${baseSym}`;
    quoteEl.textContent = `${availQuote.toFixed(2)} ${quoteSym}`;

    // Pre-fill budget with full available balance
    document.getElementById("quote_budget").value = availQuote.toFixed(2);
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
  const market = document.getElementById("market").value.trim();
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
      if (document.getElementById("market").value.trim() === market) startMarketRealtime();
    }, 1500);
  };

  // Also load REST summary immediately (WS may take a moment)
  loadMarketSummary();
}

// ──────────────────────────────────────────────────────────────
// Grid profitability
// ──────────────────────────────────────────────────────────────

/**
 * POST the current grid parameters to the profitability preview
 * endpoint and render the result (or an error) into the
 * #grid_profit_result element.
 */
async function checkGridProfitability() {
  const el = document.getElementById("grid_profit_result");
  try {
    const payload = {
      grid: {
        lower_price: Number(document.getElementById("lower_price").value),
        upper_price: Number(document.getElementById("upper_price").value),
        levels: Number(document.getElementById("levels").value),
        order_size_quote: Number(document.getElementById("order_size_quote").value),
      },
      fee_rate: Number(document.getElementById("fee_rate_percent").value) / 100,
    };
    const r = await api("/api/v1/strategy/static-grid/preview", { method: "POST", body: JSON.stringify(payload) });
    lastGridPreview = r;

    const cls = r.is_profitable ? "profit-ok" : "profit-warn";
    const txt = r.is_profitable ? t("grid_profitable") : t("grid_not_profitable");
    el.innerHTML = `
      <div class="${cls}"><strong>${txt}</strong></div>
      <div>${t("grid_step_size")}: ${r.step_size.toFixed(6)} (${r.step_percent.toFixed(3)}%)</div>
      <div>${t("grid_profit_per_trade")} ${r.profit_per_trade_quote_min.toFixed(6)}, ${t("grid_avg")} ${r.profit_per_trade_quote_avg.toFixed(6)}, ${t("grid_max")} ${r.profit_per_trade_quote_max.toFixed(6)}</div>
      <div>${t("grid_profitable_paths")}: ${r.profitable_trades}/${r.total_trade_paths}</div>
      <div>${t("grid_used_fee")}: ${(r.fee_rate * 100).toFixed(3)}%</div>
    `;
  } catch (err) {
    lastGridPreview = null;
    el.innerHTML = `<div class="profit-warn">${t("grid_calc_error")}: ${String(err.message || err)}</div>`;
  }
}

// ──────────────────────────────────────────────────────────────
// Bot list
// ──────────────────────────────────────────────────────────────

/**
 * Fetch all bots from the API and render them into the bots table.
 * Start/Stop buttons are hidden for viewer-role users.
 */
async function loadBots() {
  const bots = await api("/api/v1/bots");
  const body = document.getElementById("bots_body");
  body.innerHTML = "";
  const isViewer = currentUser?.role === "viewer";

  // Populate the equity chart bot selector
  const chartSelect = document.getElementById("equity_chart_bot");
  if (chartSelect) {
    const currentVal = chartSelect.value;
    chartSelect.innerHTML = `<option value="">${t("lbl_select_bot")}</option>`;
    for (const bot of bots) {
      const opt = document.createElement("option");
      opt.value = bot.id;
      opt.textContent = bot.name;
      if (bot.id === currentVal) opt.selected = true;
      chartSelect.appendChild(opt);
    }
  }

  for (const bot of bots) {
    const m = bot.latest_metrics || {};
    const pnl = Number(m.unrealized_pnl_quote || 0);
    const trades = Number(m.trade_count || 0);
    const tr = document.createElement("tr");

    // Viewers see no action buttons
    const acts = isViewer
      ? "<td>-</td>"
      : `<td><div class="bot-actions"><button data-start="${bot.id}">${t("btn_start")}</button><button class="secondary" data-stop="${bot.id}">${t("btn_stop")}</button></div></td>`;

    tr.innerHTML = `<td>${bot.name}</td><td>${bot.status}</td><td>${Number(m.total_equity_quote || 0).toFixed(2)}</td><td class="${pnl >= 0 ? "pnl-positive" : "pnl-negative"}">${pnl.toFixed(2)}</td><td>${trades}</td>${acts}`;
    body.appendChild(tr);
  }

  // Wire up action buttons (only present for non-viewers)
  if (!isViewer) {
    body.querySelectorAll("button[data-start]").forEach((b) => {
      b.onclick = async () => { await api(`/api/v1/bots/${b.dataset.start}/start`, { method: "POST", body: JSON.stringify({}) }); await loadBots(); };
    });
    body.querySelectorAll("button[data-stop]").forEach((b) => {
      b.onclick = async () => { await api(`/api/v1/bots/${b.dataset.stop}/stop`, { method: "POST" }); await loadBots(); };
    });
  }
}

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
  body.innerHTML = "";
  const isViewer = currentUser?.role === "viewer";

  for (const agent of agents) {
    const tr = document.createElement("tr");
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
      // Dead agents can only be removed
      if (!isViewer)
        ah = `<div class="bot-actions"><button class="icon-btn icon-remove" data-agent-remove="${agent.id}" title="${t("btn_remove")}"><span class="remove-x">✕</span></button></div>`;
    } else if (isViewer) {
      // Viewers can only open logs for approved agents
      if (agent.approval_status === "approved")
        ah = `<button class="icon-btn" data-logs="${agent.id}" title="${t("btn_open_logs")}">📋</button>`;
    } else if (agent.approval_status === "pending") {
      ah = `<div class="bot-actions"><button class="btn-approve" data-approve="${agent.id}">${t("btn_approve")}</button><button class="btn-reject" data-reject="${agent.id}">${t("btn_reject")}</button></div>`;
    } else if (agent.approval_status === "approved") {
      const isOnline = agent.status === "online" || agent.status === "stopped";
      const isStopped = agent.status === "stopped";
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
    const heartbeat = agent.last_heartbeat ? timeAgo(new Date(agent.last_heartbeat)) : "-";
    const heartbeatAttr = agent.last_heartbeat ? ` data-heartbeat="${agent.last_heartbeat}"` : "";
    const hasExpand = botCount > 0;
    const classes = [hasExpand ? "expandable-row" : "", isDead ? "dead-row" : ""].filter(Boolean).join(" ");
    tr.className = classes;
    tr.innerHTML = `<td>${hasExpand ? '<span class="expand-arrow">▶</span> ' : ""}${agent.id}</td><td>${address}</td><td>${displayStatus}</td><td>${botCount}</td><td>${version}</td><td${heartbeatAttr}>${heartbeat}</td><td>${agent.approval_status}</td><td>${ah}</td>`;
    body.appendChild(tr);

    if (hasExpand) {
      const detailTr = document.createElement("tr");
      detailTr.className = "bot-detail-row";
      detailTr.style.display = "none";
      const colSpan = 8;
      let botTable = `<table class="sub-table"><thead><tr><th>${t("th_name")}</th><th>${t("th_market")}</th><th>${t("th_status")}</th><th>${t("th_trades")}</th><th>${t("th_quote_balance")}</th><th>${t("th_base_balance")}</th></tr></thead><tbody>`;
      for (const bot of agent.bots) {
        botTable += `<tr><td>${bot.name}</td><td>${bot.market}</td><td>${bot.status}</td><td>${bot.trade_count}</td><td>${formatNumber(bot.quote_balance, 2)}</td><td>${formatNumber(bot.base_balance, 6)}</td></tr>`;
      }
      botTable += `</tbody></table>`;
      detailTr.innerHTML = `<td colspan="${colSpan}">${botTable}</td>`;
      body.appendChild(detailTr);

      tr.onclick = (e) => {
        if (e.target.closest("button")) return;
        const arrow = tr.querySelector(".expand-arrow");
        const open = detailTr.style.display !== "none";
        detailTr.style.display = open ? "none" : "table-row";
        arrow.textContent = open ? "▶" : "▼";
      };
    }
  }

  // Wire up all action buttons
  body.querySelectorAll("button[data-approve]").forEach((b) => {
    b.onclick = async () => { await api(`/api/v1/agents/${b.dataset.approve}/approve`, { method: "POST" }); await loadAgents(); await loadEvents(); };
  });
  body.querySelectorAll("button[data-reject]").forEach((b) => {
    b.onclick = async () => { await api(`/api/v1/agents/${b.dataset.reject}/reject`, { method: "POST" }); await loadAgents(); await loadEvents(); };
  });
  body.querySelectorAll("button[data-agent-start]").forEach((b) => {
    b.onclick = async () => { await api(`/api/v1/agents/${b.dataset.agentStart}/approve`, { method: "POST" }); await loadAgents(); await loadEvents(); };
  });
  body.querySelectorAll("button[data-agent-stop]").forEach((b) => {
    b.onclick = async () => { await api(`/api/v1/agents/${b.dataset.agentStop}/stop`, { method: "POST" }); await loadAgents(); await loadEvents(); };
  });
  body.querySelectorAll("button[data-agent-remove]").forEach((b) => {
    b.onclick = async () => {
      if (!await showConfirm(t("confirm_remove_agent"))) return;
      await api(`/api/v1/agents/${b.dataset.agentRemove}`, { method: "DELETE" });
      if (selectedAgentId === b.dataset.agentRemove) { selectedAgentId = null; closeLogsModal(); }
      await loadAgents(); await loadEvents();
    };
  });
  body.querySelectorAll("button[data-logs]").forEach((b) => {
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
 */
function openLogsModal(agentId) {
  logsModalOpen = true;
  document.getElementById("modal_selected_agent_id").value = agentId;
  document.getElementById("agent_logs_modal").showModal();
}

/** Close the agent-logs modal and stop polling. */
function closeLogsModal() {
  logsModalOpen = false;
  document.getElementById("agent_logs_modal").close();
}

/**
 * Fetch logs for the selected agent (with category and limit filters)
 * and render them into the logs modal. Newest entries appear at the bottom.
 */
async function loadAgentLogs() {
  const list = document.getElementById("modal_agent_logs_list");
  if (!selectedAgentId) { list.innerHTML = `<div class="log-item">${t("logs_no_agent")}</div>`; return; }

  const limit = Number(document.getElementById("modal_agent_logs_limit").value || "200");
  const category = document.getElementById("modal_log_category").value;
  const qs = new URLSearchParams({ limit: String(Math.max(1, Math.min(limit, 1000))) });
  if (category) qs.set("category", category);

  try {
    const payload = await api(`/api/v1/agents/${selectedAgentId}/logs?${qs.toString()}`);
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

/**
 * Fetch trade events from the API and render them into the
 * trade notifications panel. New trades trigger a toast.
 */
async function loadTradeEvents() {
  const events = await api("/api/v1/trade-events");
  const list = document.getElementById("trade_events_list");
  if (!list) return;
  list.innerHTML = "";

  for (const ev of events) {
    // Toast for new trades
    if (!seenTradeIds.has(ev.id)) {
      seenTradeIds.add(ev.id);
      const pnlStr = ev.trade_pnl >= 0 ? `+${ev.trade_pnl.toFixed(4)}` : ev.trade_pnl.toFixed(4);
      showToast(
        `${t("toast_trade")} #${ev.trade_number}`,
        `${ev.bot_name} @ ${ev.price.toFixed(2)} | PnL: ${pnlStr}`,
        ev.trade_pnl >= 0 ? "info" : "warn",
        4000,
      );
    }

    const item = document.createElement("div");
    const sideClass = ev.side === "buy" ? "trade-buy" : ev.side === "sell" ? "trade-sell" : "";
    const pnlClass = ev.trade_pnl >= 0 ? "trade-pnl-pos" : "trade-pnl-neg";
    const pnlStr = ev.trade_pnl >= 0 ? `+${ev.trade_pnl.toFixed(4)}` : ev.trade_pnl.toFixed(4);
    const timeLabel = new Date(ev.timestamp).toLocaleString();
    item.className = `event-item ${sideClass}`;
    item.innerHTML = `<div><strong>#${ev.trade_number}</strong> ${ev.bot_name} @ ${ev.price.toFixed(2)} &mdash; <span class="${pnlClass}">${pnlStr}</span> | ${t("lbl_equity")}: ${ev.total_equity.toFixed(2)}</div><div class="event-time">${timeLabel}</div>`;
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
 */
function drawEquityChart(data) {
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
  const minV = Math.min(...values);
  const maxV = Math.max(...values);
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
    ctx.fillText(v.toFixed(2), pad.left - 6, y + 4);
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
  if (!botId) {
    drawEquityChart([]);
    return;
  }
  try {
    const data = await api(`/api/v1/bots/${botId}/equity-history`);
    drawEquityChart(data);
  } catch {
    drawEquityChart([]);
  }
}

// Redraw chart when the bot selector changes
document.getElementById("equity_chart_bot")?.addEventListener("change", loadEquityChart);

// ──────────────────────────────────────────────────────────────
// Event handlers
// ──────────────────────────────────────────────────────────────

/** Create-bot button: validate grid, confirm if unprofitable, then POST. */
document.getElementById("create").onclick = async () => {
  await checkGridProfitability();
  if (lastGridPreview && !lastGridPreview.is_profitable) {
    if (!await showConfirm(t("grid_confirm_unprofitable"))) return;
  }
  await api("/api/v1/bots", { method: "POST", body: JSON.stringify({ name: document.getElementById("name").value, config: currentConfig() }) });
  await loadBots();
  document.getElementById("create_bot_modal").close();
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
  loadMarkets().then(() => { startMarketRealtime(); loadBalances(); toggleStartPrice(); });
};

document.getElementById("cancel_create_bot").onclick = () => { document.getElementById("create_bot_modal").close(); };
document.getElementById("check_grid_profit").onclick = async () => { await checkGridProfitability(); };

/** Run a quick backtest using the current form parameters. */
document.getElementById("backtest").onclick = async () => {
  const result = await api("/api/v1/backtest", { method: "POST", body: JSON.stringify({ config: currentConfig() }) });
  document.getElementById("backtest_result").textContent = JSON.stringify(result, null, 2);
};

/** When the market dropdown changes, reconnect WebSocket + refresh balances. */
document.getElementById("market").addEventListener("change", () => { startMarketRealtime(); loadBalances(); });

/**
 * Show/hide the "Skim ratio" field based on profit mode.
 * Only "skim" mode uses a ratio.
 */
function toggleSkimRatio() {
  document.getElementById("skim_ratio_label").style.display = document.getElementById("profit_mode").value === "skim" ? "block" : "none";
}
document.getElementById("profit_mode").addEventListener("change", toggleSkimRatio);
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
    updateLangFlags();
    applyTranslations();
    try { await api("/api/v1/auth/locale", { method: "POST", body: JSON.stringify({ locale: lang }) }); } catch { /* best-effort locale sync */ }
  };
});

// ──────────────────────────────────────────────────────────────
// Logout
// ──────────────────────────────────────────────────────────────

document.getElementById("btn_logout").onclick = () => {
  localStorage.removeItem("cryptobot_token");
  globalThis.location.href = "/login";
};

// ──────────────────────────────────────────────────────────────
// Initialisation
// ──────────────────────────────────────────────────────────────

(async () => {
  // Redirect to login if there is no stored token
  if (!authToken) { globalThis.location.href = "/login"; return; }

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
  applyRBAC();

  // Initial data load
  await loadMarkets();
  await loadBots();
  await loadAgents();
  await loadEvents();
  await loadTradeEvents();
  await loadEquityChart();
})();

// ──────────────────────────────────────────────────────────────
// Polling intervals
// ──────────────────────────────────────────────────────────────

/** Refresh bots, agents, events, and trades every 5 seconds. */
setInterval(async () => { await loadBots(); await loadAgents(); await loadEvents(); await loadTradeEvents(); await loadEquityChart(); if (logsModalOpen) await loadAgentLogs(); }, 5000);

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

  document.addEventListener("mouseover", (e) => {
    const trigger = e.target.closest(".info-tip");
    if (!trigger) return;
    const text = trigger.dataset.tip;
    if (!text) return;

    // Move tooltip into the open dialog (top-layer) so it renders above it
    const openDialog = trigger.closest("dialog[open]");
    const tipParent = openDialog || document.body;
    if (tipEl.parentNode !== tipParent) tipParent.appendChild(tipEl);

    tipEl.textContent = text;
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
    if (e.target.closest(".info-tip")) tipEl.style.display = "none";
  });
})();
