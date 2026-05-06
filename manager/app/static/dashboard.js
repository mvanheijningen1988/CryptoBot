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
  return (I18N[lang] && I18N[lang][key]) || (I18N.en && I18N.en[key]) || key;
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
    el.setAttribute("data-tip", t(el.dataset.tipKey));
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
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (authToken) headers["Authorization"] = "Bearer " + authToken;

  const res = await fetch(url, { ...options, headers });

  // Session expired or invalid → redirect to login
  if (res.status === 401) {
    localStorage.removeItem("cryptobot_token");
    window.location.href = "/login";
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
    start_price: Number(document.getElementById("start_price").value),
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
  let diffAbs = Number(summary.diff_24h_abs ?? NaN);
  let diffPct = Number(summary.diff_24h_pct ?? NaN);
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
    // Keep existing options on failure
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
    try { marketSocket.onopen = null; marketSocket.onmessage = null; marketSocket.onclose = null; marketSocket.onerror = null; marketSocket.close(); } catch (e) {}
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
  try { msg = JSON.parse(raw); } catch (e) { return; }
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
  const isViewer = currentUser && currentUser.role === "viewer";

  for (const bot of bots) {
    const m = bot.latest_metrics || {};
    const pnl = Number(m.unrealized_pnl_quote || 0);
    const tr = document.createElement("tr");

    // Viewers see no action buttons
    const acts = isViewer
      ? "<td>-</td>"
      : `<td><div class="bot-actions"><button data-start="${bot.id}">${t("btn_start")}</button><button class="secondary" data-stop="${bot.id}">${t("btn_stop")}</button></div></td>`;

    tr.innerHTML = `<td>${bot.name}</td><td>${bot.status}</td><td>${Number(m.total_equity_quote || 0).toFixed(2)}</td><td class="${pnl >= 0 ? "pnl-positive" : "pnl-negative"}">${pnl.toFixed(2)}</td>${acts}`;
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
// Agent popup (discovery notification)
// ──────────────────────────────────────────────────────────────

/**
 * Display a popup notification with the given message text.
 *
 * @param {string} message - The message to show.
 */
function showPopup(message) {
  document.getElementById("popup_text").textContent = message;
  document.getElementById("popup_backdrop").style.display = "flex";
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
  const isViewer = currentUser && currentUser.role === "viewer";

  for (const agent of agents) {
    const tr = document.createElement("tr");
    let ah = "<span>-</span>";

    if (!isViewer) {
      // Full action set for admins / moderators
      if (agent.approval_status === "pending")
        ah = `<div class="bot-actions"><button data-approve="${agent.id}">${t("btn_approve")}</button><button class="secondary" data-reject="${agent.id}">${t("btn_reject")}</button></div>`;
      else if (agent.approval_status === "approved")
        ah = `<div class="bot-actions"><button data-logs="${agent.id}">${t("btn_open_logs")}</button><button class="secondary" data-unapprove="${agent.id}">${t("btn_unapprove")}</button></div>`;
      else if (agent.approval_status === "rejected")
        ah = `<button data-approve="${agent.id}">${t("btn_approve")}</button>`;
    } else {
      // Viewers can only open logs for approved agents
      if (agent.approval_status === "approved")
        ah = `<button data-logs="${agent.id}">${t("btn_open_logs")}</button>`;
    }

    const displayStatus = agent.status === "offline" ? "dead" : agent.status;
    const botCount = agent.bot_count ?? 0;
    const version = agent.version || "-";
    tr.innerHTML = `<td>${agent.name}</td><td>${displayStatus}</td><td>${botCount}</td><td>${version}</td><td>${agent.approval_status}</td><td>${ah}</td>`;
    body.appendChild(tr);
  }

  // Wire up all action buttons
  body.querySelectorAll("button[data-approve]").forEach((b) => {
    b.onclick = async () => { await api(`/api/v1/agents/${b.dataset.approve}/approve`, { method: "POST" }); await loadAgents(); await loadEvents(); };
  });
  body.querySelectorAll("button[data-reject]").forEach((b) => {
    b.onclick = async () => { await api(`/api/v1/agents/${b.dataset.reject}/reject`, { method: "POST" }); await loadAgents(); await loadEvents(); };
  });
  body.querySelectorAll("button[data-unapprove]").forEach((b) => {
    b.onclick = async () => {
      await api(`/api/v1/agents/${b.dataset.unapprove}/unapprove`, { method: "POST" });
      // If the unapproved agent was selected in the logs modal, close it
      if (selectedAgentId === b.dataset.unapprove) { selectedAgentId = null; closeLogsModal(); }
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
  document.getElementById("agent_logs_modal").style.display = "flex";
}

/** Close the agent-logs modal and stop polling. */
function closeLogsModal() {
  logsModalOpen = false;
  document.getElementById("agent_logs_modal").style.display = "none";
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

    list.innerHTML = "";
    // Reverse so newest is at the bottom (natural scroll direction)
    for (const log of [...logs].reverse()) {
      const item = document.createElement("div");
      item.className = "log-item";
      const timeLabel = new Date(log.timestamp).toLocaleString();
      const botLabel = log.bot_id ? ` | bot ${log.bot_id}` : "";
      item.innerHTML = `<div><strong>[${log.event_type}]</strong> <span class="log-category">(${log.category || "system"})</span>${botLabel}</div><div>${log.message}</div><div class="event-time">${timeLabel}</div>`;
      list.appendChild(item);
    }
    list.scrollTop = list.scrollHeight;
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
    // Show a popup for each newly discovered agent (only once)
    if (event.event_type === "discovered" && !seenEventIds.has(event.id)) {
      seenEventIds.add(event.id);
      showPopup(event.message);
    }
    const item = document.createElement("div");
    item.className = "event-item";
    const timeLabel = new Date(event.timestamp).toLocaleString();
    item.innerHTML = `<div><strong>[${event.event_type}]</strong> <strong>[${event.agent_name || "unknown"}]</strong> ${event.message}</div><div class="event-time">${timeLabel}</div>`;
    list.appendChild(item);
  }
}

// ──────────────────────────────────────────────────────────────
// Event handlers
// ──────────────────────────────────────────────────────────────

/** Create-bot button: validate grid, confirm if unprofitable, then POST. */
document.getElementById("create").onclick = async () => {
  await checkGridProfitability();
  if (lastGridPreview && !lastGridPreview.is_profitable) {
    if (!window.confirm(t("grid_confirm_unprofitable"))) return;
  }
  await api("/api/v1/bots", { method: "POST", body: JSON.stringify({ name: document.getElementById("name").value, config: currentConfig() }) });
  await loadBots();
  document.getElementById("create_bot_modal").style.display = "none";
};

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
  document.getElementById("create_bot_modal").style.display = "flex";
  loadMarkets().then(() => { startMarketRealtime(); loadBalances(); toggleStartPrice(); });
};

document.getElementById("cancel_create_bot").onclick = () => { document.getElementById("create_bot_modal").style.display = "none"; };
document.getElementById("check_grid_profit").onclick = async () => { await checkGridProfitability(); };

/** Run a quick backtest using the current form parameters. */
document.getElementById("backtest").onclick = async () => {
  const result = await api("/api/v1/backtest", { method: "POST", body: JSON.stringify({ config: currentConfig() }) });
  document.getElementById("backtest_result").textContent = JSON.stringify(result, null, 2);
};

document.getElementById("popup_close").onclick = () => { document.getElementById("popup_backdrop").style.display = "none"; };

/** When the market dropdown changes, reconnect WebSocket + refresh balances. */
document.getElementById("market").addEventListener("change", () => { startMarketRealtime(); loadBalances(); });

/**
 * Show/hide the "Start price" field based on mode.
 * Only simulation mode uses a custom start price.
 */
function toggleStartPrice() {
  document.getElementById("start_price_label").style.display = document.getElementById("mode").value === "simulation" ? "block" : "none";
}
document.getElementById("mode").addEventListener("change", toggleStartPrice);
toggleStartPrice();

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
    try { await api("/api/v1/auth/locale", { method: "POST", body: JSON.stringify({ locale: lang }) }); } catch (e) {}
  };
});

// ──────────────────────────────────────────────────────────────
// Logout
// ──────────────────────────────────────────────────────────────

document.getElementById("btn_logout").onclick = () => {
  localStorage.removeItem("cryptobot_token");
  window.location.href = "/login";
};

// ──────────────────────────────────────────────────────────────
// Initialisation
// ──────────────────────────────────────────────────────────────

(async () => {
  // Redirect to login if there is no stored token
  if (!authToken) { window.location.href = "/login"; return; }

  // Validate the token and fetch the current user profile
  try {
    currentUser = await api("/api/v1/auth/me");
    // If the user still needs to change their password, send them back
    if (currentUser.must_change_password) { window.location.href = "/login"; return; }
  } catch (e) {
    window.location.href = "/login";
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
})();

// ──────────────────────────────────────────────────────────────
// Polling intervals
// ──────────────────────────────────────────────────────────────

/** Refresh bots, agents, and events every 5 seconds. */
setInterval(async () => { await loadBots(); await loadAgents(); await loadEvents(); if (logsModalOpen) await loadAgentLogs(); }, 5000);

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
    const text = trigger.getAttribute("data-tip");
    if (!text) return;

    tipEl.textContent = text;
    tipEl.style.display = "block";

    // Position above the trigger icon, horizontally centred
    const rect = trigger.getBoundingClientRect();
    const tipRect = tipEl.getBoundingClientRect();
    let left = rect.left + rect.width / 2 - tipRect.width / 2;
    let top = rect.top - tipRect.height - 6;

    // Clamp horizontally to viewport
    if (left < 4) left = 4;
    if (left + tipRect.width > window.innerWidth - 4) left = window.innerWidth - tipRect.width - 4;

    // If it overflows the top, show below instead
    if (top < 4) top = rect.bottom + 6;

    tipEl.style.left = left + "px";
    tipEl.style.top = top + "px";
  });

  document.addEventListener("mouseout", (e) => {
    if (e.target.closest(".info-tip")) tipEl.style.display = "none";
  });
})();
