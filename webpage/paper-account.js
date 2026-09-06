const SESSION_KEY = "pmq-paper-api-session-v1";
const TERMINAL_ORDER_STATES = new Set(["CANCELED", "COMPLETED", "FILLED", "REJECTED", "EXPIRED"]);
const VIEW_TITLES = Object.freeze({
  overview: "Portfolio overview",
  orders: "Orders",
  positions: "Positions",
  fills: "Fills",
  ledger: "Ledger",
  journal: "Trading journal",
  performance: "NAV and transaction costs",
  replays: "Historical replay",
  research: "Scenario and synthetic orders",
  admin: "Operations control plane",
});

const app = document.querySelector("#paperApp");
const initialBase = new URLSearchParams(location.search).get("api_base") || app.dataset.apiBase || "/v1/paper";
const state = {
  apiBase: initialBase.replace(/\/$/, ""),
  apiKey: sessionStorage.getItem(SESSION_KEY) || "",
  accounts: [],
  accountId: new URLSearchParams(location.search).get("account_id") || "",
  view: location.hash.slice(1) in VIEW_TITLES ? location.hash.slice(1) : "overview",
  data: { performance: null, orders: [], positions: [], fills: [], ledger: [], journal: [], tca: [], replays: [], scenarios: [], conditionalOrders: [] },
  auditOrderId: null,
  replayId: "",
  replaySession: null,
  replayEvents: [],
  replayReport: null,
  adminLoaded: false,
  admin: { dashboard: null, jobs: [], dlq: [], notices: [], incidents: [], retention: [], bundles: [] },
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const asObject = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const asNumber = (value, fallback = null) => {
  const selected = Number(value);
  return Number.isFinite(selected) ? selected : fallback;
};
const text = (value, fallback = "Unavailable") => value === null || value === undefined || value === "" ? fallback : String(value);
const escapeHtml = (value) => text(value, "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
const formatMoney = (value, digits = 2) => {
  const selected = asNumber(value);
  return selected === null ? "Unavailable" : new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(selected);
};
const formatNumber = (value, digits = 4) => {
  const selected = asNumber(value);
  return selected === null ? "Unavailable" : selected.toLocaleString("en-US", { maximumFractionDigits: digits });
};
const formatPct = (value, digits = 2) => {
  const selected = asNumber(value);
  return selected === null ? "Unavailable" : `${(selected * 100).toFixed(digits)}%`;
};
const formatTime = (value) => {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
};
const shortId = (value, size = 10) => {
  const selected = text(value, "");
  return selected.length <= size * 2 + 3 ? selected : `${selected.slice(0, size)}...${selected.slice(-size)}`;
};
const pnlClass = (value) => asNumber(value, 0) > 0 ? "positive" : asNumber(value, 0) < 0 ? "negative" : "";

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 4200);
}

async function apiRequest(path, options = {}) {
  if (!state.apiKey) throw new Error("Connect a Paper API key first");
  const response = await fetch(`${state.apiBase}${path}`, {
    cache: "no-store",
    ...options,
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${state.apiKey}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = asObject(payload.error);
    const exception = new Error(text(error.message, `Paper API returned HTTP ${response.status}`));
    exception.code = text(error.code, "PAPER_HTTP_ERROR");
    exception.requestId = error.request_id;
    throw exception;
  }
  if (!("data" in payload)) throw new Error("Paper API returned an invalid response envelope");
  return payload.data;
}

function setConnection(connected, detail) {
  const button = $("#connectionButton");
  button.classList.toggle("connected", connected);
  $("#connectionLabel").textContent = detail;
}

async function connect() {
  setConnection(false, "Connecting");
  try {
    await loadAccounts();
    setConnection(true, "Connected");
    $("#connectionDialog").close();
  } catch (error) {
    setConnection(false, "Connection failed");
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
    $("#connectionDialog").showModal();
  }
}

async function loadAccounts() {
  const page = await apiRequest("/accounts?limit=200");
  state.accounts = Array.isArray(page.items) ? page.items : [];
  if (!state.accounts.some((row) => String(row.account_id) === state.accountId)) {
    state.accountId = state.accounts[0] ? String(state.accounts[0].account_id) : "";
  }
  renderAccountPicker();
  $("#emptyState").hidden = state.accounts.length > 0;
  $("#workspaceContent").hidden = state.accounts.length === 0;
  if (state.accountId) await loadAccountWorkspace();
  else if (state.view === "admin") {
    $("#emptyState").hidden = true;
    $("#workspaceContent").hidden = false;
    await loadAdmin();
  }
}

function renderAccountPicker() {
  const select = $("#accountSelect");
  select.innerHTML = state.accounts.length
    ? state.accounts.map((account) => `<option value="${escapeHtml(account.account_id)}">${escapeHtml(account.name)}</option>`).join("")
    : `<option value="">No accounts</option>`;
  select.value = state.accountId;
  select.disabled = state.accounts.length === 0;
  $("#forkButton").disabled = !state.accountId;
  $("#exportButton").disabled = !state.accountId;
}

async function loadAccountWorkspace() {
  const account = state.accounts.find((row) => String(row.account_id) === state.accountId);
  if (!account) return;
  $("#accountName").textContent = account.name;
  $("#accountMeta").textContent = `Generation ${text(account.current_generation, "0")} | ${text(account.base_currency, "USDC")} | ${shortId(account.account_id, 8)}`;
  $("#accountStatus").textContent = "Loading";
  $("#accountStatus").className = "status-pill neutral";
  const routes = {
    performance: `/accounts/${state.accountId}/performance?limit=200`,
    orders: `/orders?account_id=${state.accountId}&limit=200`,
    positions: `/accounts/${state.accountId}/positions?limit=200`,
    fills: `/accounts/${state.accountId}/fills?limit=200`,
    ledger: `/accounts/${state.accountId}/ledger?limit=200`,
    journal: `/accounts/${state.accountId}/journal?limit=200`,
    tca: `/accounts/${state.accountId}/tca?limit=200`,
    replays: "/replays?limit=200",
    scenarios: `/scenarios?account_id=${state.accountId}&limit=200`,
    conditionalOrders: `/conditional-orders?account_id=${state.accountId}&limit=200`,
  };
  const entries = Object.entries(routes);
  const results = await Promise.allSettled(entries.map(([, path]) => apiRequest(path)));
  const failures = [];
  results.forEach((result, index) => {
    const key = entries[index][0];
    if (result.status === "fulfilled") {
      state.data[key] = key === "performance" ? result.value : Array.isArray(result.value.items) ? result.value.items : [];
    } else {
      failures.push(`${key}: ${result.reason.message}`);
      state.data[key] = key === "performance" ? null : [];
    }
  });
  renderAll();
  if (failures.length) showToast(`Partial account data: ${failures.join("; ")}`, true);
}

function statusClass(value) {
  const status = text(value, "").toUpperCase();
  if (["ACTIVE", "FILLED", "COMPLETED", "PASS", "CONFIRMED", "REDEEMED"].includes(status)) return "good";
  if (["RISK_LOCKED", "INSOLVENT", "REJECTED", "FAILED", "VOIDED"].includes(status)) return "bad";
  if (["DATA_DEGRADED", "FROZEN", "PARTIAL", "WORKING", "QUEUED", "PENDING"].includes(status)) return "warn";
  return "neutral";
}

function metricCell(label, value, detail = "", className = "") {
  return `<div class="metric-cell"><span>${escapeHtml(label)}</span><strong class="${className}">${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></div>`;
}

function renderMetrics() {
  const performance = asObject(state.data.performance);
  const summary = asObject(performance.summary);
  $("#metricBand").innerHTML = [
    metricCell("Balance", formatMoney(summary.balance), "Ledger cash"),
    metricCell("Equity", formatMoney(summary.equity), "Current mark", pnlClass(asNumber(summary.equity, 0) - asNumber(summary.balance, 0))),
    metricCell("Available", formatMoney(summary.available_cash), "After reservations"),
    metricCell("Reserved", formatMoney(summary.reserved_cash), `${text(summary.open_orders, "0")} open orders`),
    metricCell("Realized PnL", formatMoney(summary.realized_pnl), "Closed exposure", pnlClass(summary.realized_pnl)),
    metricCell("Unrealized PnL", formatMoney(summary.unrealized_pnl), "Open positions", pnlClass(summary.unrealized_pnl)),
    metricCell("Confirmed NAV", formatMoney(summary.confirmed_nav), "Finality view"),
    metricCell("Liquidation NAV", formatMoney(summary.liquidation_nav), "Walk-book view"),
  ].join("");
  const status = text(summary.effective_status, "Unavailable");
  $("#accountStatus").textContent = status;
  $("#accountStatus").className = `status-pill ${statusClass(status)}`;
}

function renderQualitySummary() {
  const quality = asObject(asObject(state.data.performance).data_quality);
  const current = asObject(asObject(state.data.performance).current);
  const rows = [
    ["NAV completeness", quality.nav_complete === true ? "Complete" : "Incomplete", `${text(quality.unmarkable_positions, "0")} unmarkable positions`],
    ["Latest valuation", formatTime(quality.observed_at), text(quality.valuation_model_version, "Model unavailable")],
    ["Open inventory", text(current.open_positions, "0"), `${text(current.gross_exposure, "Unavailable")} gross exposure`],
    ["Drawdown", formatPct(current.drawdown_pct), `${formatMoney(current.drawdown)} from high watermark`],
  ];
  $("#qualitySummary").innerHTML = rows.map(([label, value, detail]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd><small>${escapeHtml(detail)}</small></div>`).join("");
  const label = quality.nav_complete === true ? "Complete" : "Degraded";
  $("#navQualityLabel").textContent = label;
  $("#navQualityLabel").className = `quality-label ${quality.nav_complete === true ? "good" : "warn"}`;
  $("#performanceQualityLabel").textContent = label;
  $("#performanceQualityLabel").className = `quality-label ${quality.nav_complete === true ? "good" : "warn"}`;
}

function qualityValue(row) {
  const result = asObject(row.result);
  const fidelity = asObject(result.fidelity);
  return text(fidelity.fidelity_level || result.execution_fidelity || row.fidelity_level, "Unassessed");
}

function dataQualityValue(row) {
  const result = asObject(row.result);
  const fidelity = asObject(result.fidelity);
  return text(fidelity.data_quality || result.coverage_grade || row.mark_quality || row.coverage_grade, "Unassessed");
}

const TABLES = Object.freeze({
  orders: {
    columns: [
      ["intent_id", "Order", "84px", (row) => `<div class="cell-main"><strong>#${escapeHtml(row.intent_id)}</strong><small>${escapeHtml(formatTime(row.created_at))}</small></div>`],
      ["asset_id", "Market / asset", "220px", (row) => `<div class="cell-main"><strong>${escapeHtml(text(row.market_id, shortId(row.asset_id, 9)))}</strong><small title="${escapeHtml(row.asset_id)}">${escapeHtml(shortId(row.asset_id, 10))}</small></div>`],
      ["side", "Side / TIF", "90px", (row) => `<div class="cell-main"><strong class="side-${String(row.side).toLowerCase()}">${escapeHtml(row.side)}</strong><small>${escapeHtml(row.time_in_force)}</small></div>`],
      ["limit_price", "Limit", "86px", (row) => formatNumber(row.limit_price, 4), "numeric"],
      ["size", "Size", "86px", (row) => formatNumber(row.size, 4), "numeric"],
      ["status", "Status", "112px", (row) => `<span class="quality-chip ${statusClass(row.status)}">${escapeHtml(row.status)}</span>`],
      ["fidelity", "Fidelity", "180px", (row) => escapeHtml(qualityValue(row))],
      ["quality", "Data quality", "130px", (row) => escapeHtml(dataQualityValue(row))],
    ],
    orderRows: true,
  },
  positions: {
    columns: [
      ["asset_id", "Asset", "220px", (row) => `<div class="cell-main"><strong>${escapeHtml(text(row.market_id, shortId(row.asset_id, 9)))}</strong><small title="${escapeHtml(row.asset_id)}">${escapeHtml(shortId(row.asset_id, 10))}</small></div>`],
      ["quantity", "Quantity", "100px", (row) => formatNumber(row.quantity), "numeric"],
      ["reserved_quantity", "Reserved", "92px", (row) => formatNumber(row.reserved_quantity), "numeric"],
      ["cost_basis", "Cost basis", "110px", (row) => formatMoney(row.cost_basis, 4), "numeric"],
      ["research_mark", "Mark", "86px", (row) => formatNumber(row.research_mark, 4), "numeric"],
      ["liquidation_mark", "Exit mark", "92px", (row) => formatNumber(row.liquidation_mark, 4), "numeric"],
      ["realized_pnl", "Realized PnL", "112px", (row) => `<span class="${pnlClass(row.realized_pnl)}">${escapeHtml(formatMoney(row.realized_pnl))}</span>`, "numeric"],
      ["mark_quality", "Mark quality", "130px", (row) => `<span class="quality-chip ${String(row.mark_quality).includes("FRESH") ? "good" : "warn"}">${escapeHtml(text(row.mark_quality))}</span>`],
    ],
  },
  fills: {
    columns: [
      ["created_at", "Time", "170px", (row) => formatTime(row.created_at)],
      ["asset_id", "Asset", "200px", (row) => `<div class="cell-main"><strong>${escapeHtml(text(row.market_id, shortId(row.asset_id, 9)))}</strong><small>${escapeHtml(shortId(row.asset_id, 10))}</small></div>`],
      ["side", "Side", "72px", (row) => `<strong class="side-${String(row.side).toLowerCase()}">${escapeHtml(row.side)}</strong>`],
      ["price", "Price", "84px", (row) => formatNumber(row.price, 4), "numeric"],
      ["size", "Size", "90px", (row) => formatNumber(row.size, 4), "numeric"],
      ["notional", "Notional", "100px", (row) => formatMoney(row.notional, 4), "numeric"],
      ["fee", "Fee", "86px", (row) => formatMoney(row.fee, 4), "numeric"],
      ["audit_key", "Audit key", "190px", (row) => shortId(row.audit_key, 10)],
    ],
  },
  ledger: {
    columns: [
      ["event_ts", "Time", "170px", (row) => formatTime(row.event_ts)],
      ["event_type", "Event", "150px", (row) => escapeHtml(row.event_type)],
      ["asset_id", "Asset", "180px", (row) => escapeHtml(shortId(row.asset_id, 10))],
      ["shares_delta", "Shares", "96px", (row) => formatNumber(row.shares_delta), "numeric"],
      ["cash_delta", "Cash", "100px", (row) => `<span class="${pnlClass(row.cash_delta)}">${escapeHtml(formatMoney(row.cash_delta, 4))}</span>`, "numeric"],
      ["fee", "Fee", "86px", (row) => formatMoney(row.fee, 4), "numeric"],
      ["realized_pnl_delta", "Realized", "104px", (row) => formatMoney(row.realized_pnl_delta, 4), "numeric"],
      ["cash_after", "Cash after", "110px", (row) => formatMoney(row.cash_after, 4), "numeric"],
    ],
  },
  journal: {
    columns: [
      ["event_ts", "Time", "170px", (row) => formatTime(row.event_ts)],
      ["journal_id", "Journal", "190px", (row) => escapeHtml(shortId(row.journal_id, 10))],
      ["event_type", "Event", "150px", (row) => escapeHtml(row.event_type)],
      ["account_code", "Account", "160px", (row) => escapeHtml(row.account_code)],
      ["debit", "Debit", "110px", (row) => formatMoney(row.debit, 4), "numeric"],
      ["credit", "Credit", "110px", (row) => formatMoney(row.credit, 4), "numeric"],
      ["line_index", "Line", "70px", (row) => escapeHtml(row.line_index), "numeric"],
    ],
  },
  nav: {
    columns: [
      ["observed_at", "Observed", "170px", (row) => formatTime(row.observed_at)],
      ["equity", "Equity", "110px", (row) => formatMoney(row.equity), "numeric"],
      ["conservative_equity", "Conservative", "120px", (row) => formatMoney(row.conservative_equity), "numeric"],
      ["realized_pnl", "Realized", "105px", (row) => formatMoney(row.realized_pnl), "numeric"],
      ["unrealized_pnl", "Unrealized", "110px", (row) => formatMoney(row.unrealized_pnl), "numeric"],
      ["gross_exposure", "Exposure", "105px", (row) => formatMoney(row.gross_exposure), "numeric"],
      ["drawdown_pct", "Drawdown", "96px", (row) => formatPct(row.drawdown_pct), "numeric"],
      ["nav_complete", "Quality", "96px", (row) => `<span class="quality-chip ${row.nav_complete ? "good" : "warn"}">${row.nav_complete ? "Complete" : "Degraded"}</span>`],
    ],
  },
  tca: {
    columns: [
      ["updated_at", "Time", "170px", (row) => formatTime(row.updated_at)],
      ["order_id", "Order", "180px", (row) => escapeHtml(shortId(row.order_id, 10))],
      ["side", "Side", "70px", (row) => escapeHtml(row.side)],
      ["fill_vwap", "VWAP", "84px", (row) => formatNumber(row.fill_vwap, 4), "numeric"],
      ["implementation_shortfall", "Shortfall", "110px", (row) => formatMoney(row.implementation_shortfall, 4), "numeric"],
      ["fee_cost", "Fees", "88px", (row) => formatMoney(row.fee_cost, 4), "numeric"],
      ["capacity_status", "Capacity", "130px", (row) => escapeHtml(row.capacity_status)],
      ["fidelity_level", "Fidelity", "190px", (row) => escapeHtml(row.fidelity_level)],
      ["status", "Status", "110px", (row) => `<span class="quality-chip ${statusClass(row.status)}">${escapeHtml(row.status)}</span>`],
    ],
  },
  replays: {
    columns: [
      ["name", "Session", "210px", (row) => `<div class="cell-main"><strong>${escapeHtml(row.name)}</strong><small>${escapeHtml(shortId(row.replay_session_id, 8))}</small></div>`],
      ["status", "Status", "112px", (row) => `<span class="quality-chip ${statusClass(row.status)}">${escapeHtml(row.status)}</span>`],
      ["progress", "Progress", "110px", (row) => `${escapeHtml(text(row.cursor_event_index, "0"))} / ${escapeHtml(text(row.event_count, "0"))}`, "numeric"],
      ["range", "Range", "230px", (row) => `<div class="cell-main"><strong>${escapeHtml(formatTime(row.start_ts))}</strong><small>${escapeHtml(formatTime(row.end_ts))}</small></div>`],
      ["execution_model", "Execution model", "220px", (row) => escapeHtml(row.execution_model)],
      ["benchmark", "Benchmark", "150px", (row) => escapeHtml(row.benchmark)],
      ["data_hash", "Data hash", "170px", (row) => `<span title="${escapeHtml(row.data_hash)}">${escapeHtml(shortId(row.data_hash, 8))}</span>`],
    ],
    replayRows: true,
  },
  replayEvents: {
    columns: [
      ["event_index", "Index", "72px", (row) => escapeHtml(row.event_index), "numeric"],
      ["event_ts_ns", "Timestamp (ns)", "180px", (row) => escapeHtml(row.event_ts_ns), "numeric"],
      ["event_type", "Event type", "180px", (row) => escapeHtml(row.event_type)],
      ["event_id", "Event ID", "230px", (row) => `<span title="${escapeHtml(row.event_id)}">${escapeHtml(shortId(row.event_id, 11))}</span>`],
    ],
  },
  scenarios: {
    columns: [
      ["name", "Scenario", "190px", (row) => `<div class="cell-main"><strong>${escapeHtml(row.name)}</strong><small>${escapeHtml(shortId(row.scenario_run_id, 8))}</small></div>`],
      ["status", "Status", "100px", (row) => `<span class="quality-chip ${statusClass(row.status)}">${escapeHtml(row.status)}</span>`],
      ["scenario_nav", "Scenario NAV", "118px", (row) => formatMoney(asObject(row.result).scenario_nav), "numeric"],
      ["scenario_pnl_delta", "PnL delta", "110px", (row) => formatMoney(asObject(row.result).scenario_pnl_delta), "numeric"],
      ["complete", "Coverage", "105px", (row) => `<span class="quality-chip ${asObject(row.result).scenario_complete ? "good" : "warn"}">${asObject(row.result).scenario_complete ? "Complete" : "Unmarked"}</span>`],
      ["created_at", "Created", "170px", (row) => formatTime(row.created_at)],
    ],
  },
  conditionalOrders: {
    columns: [
      ["order_type", "Type", "130px", (row) => escapeHtml(row.order_type)],
      ["asset_id", "Asset", "220px", (row) => escapeHtml(shortId(row.asset_id, 11))],
      ["status", "Status", "112px", (row) => `<span class="quality-chip ${statusClass(row.status)}">${escapeHtml(row.status)}</span>`],
      ["trigger", "Trigger", "145px", (row) => `${escapeHtml(text(row.trigger_operator, row.trigger_kind))} ${escapeHtml(text(row.trigger_value, ""))}`],
      ["child", "Child", "145px", (row) => `${escapeHtml(text(asObject(row.child_order).side))} ${escapeHtml(text(asObject(row.child_order).time_in_force))}`],
      ["trigger_ts", "Triggered", "170px", (row) => formatTime(row.trigger_ts)],
      ["actions", "Actions", "90px", (row) => ["ARMED", "PENDING_DATA", "TRIGGERING"].includes(String(row.status).toUpperCase()) ? `<button type="button" data-conditional-cancel="${escapeHtml(row.conditional_order_id)}">Cancel</button>` : ""],
    ],
  },
});

function renderTable(target, rows, config, limit = null) {
  const selected = limit ? rows.slice(0, limit) : rows;
  const columns = config.columns;
  target.innerHTML = `<table class="paper-table"><colgroup>${columns.map(([, , width]) => `<col style="width:${width}">`).join("")}</colgroup><thead><tr>${columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr></thead><tbody>${selected.length ? selected.map((row) => `<tr${config.orderRows ? ` data-order-id="${escapeHtml(row.intent_id)}"` : config.replayRows ? ` data-replay-id="${escapeHtml(row.replay_session_id)}"` : ""}>${columns.map(([, , , render, className = ""]) => `<td class="${className}">${render(row)}</td>`).join("")}</tr>`).join("") : `<tr><td class="empty-row" colspan="${columns.length}">No persisted rows for this account</td></tr>`}</tbody></table>`;
}

function filteredRows(resource) {
  const needle = $("#resourceFilter").value.trim().toLowerCase();
  const rows = state.data[resource] || [];
  if (!needle) return rows;
  return rows.filter((row) => JSON.stringify(row).toLowerCase().includes(needle));
}

function renderResource() {
  if (!["orders", "positions", "fills", "ledger", "journal"].includes(state.view)) return;
  const rows = filteredRows(state.view);
  $("#resourceTitle").textContent = VIEW_TITLES[state.view];
  $("#resourceCount").textContent = `${rows.length} rows loaded`;
  renderTable($("#resourceTable"), rows, TABLES[state.view]);
}

function renderPerformance() {
  const history = Array.isArray(asObject(state.data.performance).history) ? state.data.performance.history : [];
  renderTable($("#navHistoryTable"), history, TABLES.nav);
  renderTable($("#tcaTable"), state.data.tca, TABLES.tca);
  drawNavChart($("#performanceChart"), $("#performanceChartEmpty"), history);
}

function replayRows() {
  return state.data.replays.filter((row) => String(row.account_id) === state.accountId);
}

function reportList(target, rows) {
  target.innerHTML = rows.map(([label, value, detail = ""]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(text(value))}</dd><small>${escapeHtml(detail)}</small></div>`).join("");
}

function replayAttributionRows(attribution) {
  const valueKeys = { market: "market_id", event: "event_id", category: "category" };
  return ["market", "event", "category"].flatMap((dimension) => {
    const rows = Array.isArray(attribution[dimension]) ? attribution[dimension] : [];
    return rows.map((row) => ({
      dimension,
      value: text(row[valueKeys[dimension]], "unknown"),
      realized_pnl: row.realized_pnl,
      fees: row.fees,
      event_count: row.event_count,
    }));
  });
}

function renderReplayDetail() {
  const session = asObject(state.replaySession);
  const detail = $("#replayDetail");
  detail.hidden = !session.replay_session_id;
  if (detail.hidden) return;
  $("#replayDetailTitle").textContent = text(session.name, "Replay");
  $("#replayDetailMeta").textContent = `${text(session.source_mode)} | ${shortId(session.replay_session_id, 8)}`;
  const progress = asNumber(session.event_count, 0) > 0
    ? asNumber(session.cursor_event_index, 0) / asNumber(session.event_count, 1)
    : 0;
  $("#replayMetricBand").innerHTML = [
    metricCell("Status", text(session.status), `${formatPct(progress, 0)} complete`),
    metricCell("Events", `${text(session.cursor_event_index, "0")} / ${text(session.event_count, "0")}`, "Frozen scheduler cursor"),
    metricCell("Speed", `${text(session.speed, "1")}x`, `Seed ${text(session.seed, "0")}`),
    metricCell("Data hash", shortId(session.data_hash, 8), `${text(session.benchmark)} benchmark`),
    metricCell("Artifact", shortId(session.artifact_hash, 8), text(session.strategy_version)),
  ].join("");
  const reportEnvelope = asObject(state.replayReport);
  const report = asObject(reportEnvelope.report);
  const performance = asObject(report.performance);
  const risk = asObject(report.risk);
  const benchmark = asObject(report.benchmark_comparison);
  const dataQuality = asObject(report.data_quality);
  const attribution = asObject(report.attribution);
  reportList($("#replayPerformance"), [
    ["Start equity", formatMoney(performance.start_equity)],
    ["End equity", formatMoney(performance.end_equity)],
    ["Net PnL", formatMoney(performance.net_pnl)],
    ["Confirmed PnL", formatMoney(performance.confirmed_pnl), "Finality-aware valuation"],
    ["Return", formatPct(performance.return_pct)],
    ["Sharpe / Sortino", `${text(performance.sharpe)} / ${text(performance.sortino)}`],
    ["Turnover", formatPct(performance.turnover)],
    ["Fill ratio", formatPct(performance.fill_ratio)],
    ["Filled notional", formatMoney(performance.filled_notional)],
    ["Fees", formatMoney(performance.fee_cost, 4)],
    ["Fee drag", formatPct(performance.fee_drag)],
    ["Latency slippage", formatMoney(performance.latency_slippage, 4)],
    ["Shortfall", formatMoney(performance.implementation_shortfall, 4)],
  ]);
  reportList($("#replayRisk"), [
    ["Max drawdown", formatPct(risk.max_drawdown_pct)],
    ["Gross exposure", formatMoney(risk.max_gross_exposure)],
    ["NAV snapshots", risk.nav_snapshot_count],
    ["Incomplete NAV", risk.incomplete_nav_snapshots],
    ["Completed events", risk.completed_order_events],
    ["Rejected events", risk.rejected_order_events],
    ["Capacity rejected", formatPct(performance.capacity_rejected_ratio)],
    ["Confidence coverage", formatPct(risk.confidence_coverage)],
  ]);
  reportList($("#replayBenchmark"), [
    ["Benchmark", benchmark.benchmark],
    ["Strategy return", formatPct(benchmark.strategy_return_pct)],
    ["Benchmark return", formatPct(benchmark.benchmark_return_pct)],
    ["Excess return", formatPct(benchmark.excess_return_pct)],
    ["Outcome", benchmark.status],
    ["Report hash", shortId(report.report_hash, 8)],
  ]);
  reportList($("#replayDataQuality"), [
    ["NAV complete", dataQuality.nav_complete ? "PASS" : "INCOMPLETE"],
    ["NAV history", dataQuality.has_nav_history ? "AVAILABLE" : "MISSING"],
    ["TCA", dataQuality.has_tca ? "AVAILABLE" : "MISSING"],
    ["Fills", dataQuality.has_fills ? "AVAILABLE" : "MISSING"],
    ["Ledger attribution", dataQuality.has_ledger_attribution ? "AVAILABLE" : "MISSING"],
    ["Confidence coverage", formatPct(dataQuality.confidence_coverage)],
  ]);
  $("#replayAttributionBasis").textContent = text(attribution.basis, "Ledger events").replaceAll("_", " ");
  renderTable($("#replayAttributionTable"), replayAttributionRows(attribution), {
    columns: [
      ["dimension", "Dimension", "100px", (row) => escapeHtml(row.dimension)],
      ["value", "Value", "240px", (row) => escapeHtml(row.value)],
      ["realized_pnl", "Realized PnL", "120px", (row) => formatMoney(row.realized_pnl, 4), "numeric"],
      ["fees", "Fees", "100px", (row) => formatMoney(row.fees, 4), "numeric"],
      ["event_count", "Events", "80px", (row) => escapeHtml(row.event_count), "numeric"],
    ],
  });
  const limitations = Array.isArray(reportEnvelope.limitations) ? reportEnvelope.limitations : [];
  $("#replayLimitations").textContent = limitations.includes("RECORDED_PAPER_LIFECYCLE_NOT_L2_REMATCH")
    ? "Lifecycle evidence"
    : "Report pending";
  renderTable($("#replayEventsTable"), state.replayEvents, TABLES.replayEvents);
  const status = String(session.status || "").toUpperCase();
  $("#pauseReplayButton").disabled = !["CREATED", "RUNNING"].includes(status);
  $("#resumeReplayButton").disabled = !["CREATED", "PAUSED", "RUNNING"].includes(status);
  $("#forkReplayButton").disabled = !session.replay_session_id;
}

function renderReplays() {
  const rows = replayRows();
  $("#replayCount").textContent = `${rows.length} sessions`;
  renderTable($("#replayTable"), rows, TABLES.replays);
  if (state.replayId && !rows.some((row) => String(row.replay_session_id) === state.replayId)) {
    state.replayId = "";
    state.replaySession = null;
    state.replayEvents = [];
    state.replayReport = null;
  }
  renderReplayDetail();
}

function renderResearch() {
  const scenarios = state.data.scenarios || [];
  const conditionalOrders = state.data.conditionalOrders || [];
  $("#researchCount").textContent = `${scenarios.length + conditionalOrders.length} persisted records`;
  renderTable($("#scenarioTable"), scenarios, TABLES.scenarios);
  renderTable($("#conditionalOrderTable"), conditionalOrders, TABLES.conditionalOrders);
}

function adminTable(headers, rows, emptyLabel) {
  return `<table class="paper-table"><thead><tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr></thead><tbody>${rows.length ? rows.join("") : `<tr><td class="empty-row" colspan="${headers.length}">${escapeHtml(emptyLabel)}</td></tr>`}</tbody></table>`;
}

function renderAdmin() {
  const dashboard = asObject(state.admin.dashboard);
  const tenant = asObject(dashboard.tenant);
  const account = state.accounts.find((row) => String(row.account_id) === state.accountId);
  $("#adminTenantStatus").textContent = text(tenant.status);
  $("#adminTenantName").textContent = text(tenant.name, shortId(tenant.tenant_id, 8));
  $("#adminAccountStatus").textContent = text(account?.status);
  $("#adminAccountName").textContent = text(account?.name, "No account selected");
  $("#adminUpdatedAt").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  $("#freezeTenantButton").disabled = tenant.status === "FROZEN";
  $("#unfreezeTenantButton").disabled = tenant.status !== "FROZEN";
  ["freezeAccountButton", "unfreezeAccountButton", "reconcileAccountButton", "killAccountButton", "createBundleButton"].forEach((id) => { $("#" + id).disabled = !account; });
  if (account) {
    $("#freezeAccountButton").disabled = account.status === "FROZEN";
    $("#unfreezeAccountButton").disabled = account.status !== "FROZEN";
  }

  $("#adminDlqTable").innerHTML = adminTable(
    ["Event", "Source", "Status", "Attempts", "Actions"],
    state.admin.dlq.map((row) => `<tr><td><div class="cell-main"><strong>${escapeHtml(text(row.event_type))}</strong><small>${escapeHtml(shortId(row.dlq_event_id, 7))}</small></div></td><td>${escapeHtml(text(row.source))}</td><td><span class="quality-chip ${statusClass(row.status)}">${escapeHtml(text(row.status))}</span></td><td class="numeric">${escapeHtml(text(row.attempt_count, "0"))}</td><td><div class="admin-table-actions"><button data-dlq-action="replay" data-id="${escapeHtml(row.dlq_event_id)}" type="button">Replay</button><button data-dlq-action="ignore" data-id="${escapeHtml(row.dlq_event_id)}" type="button">Ignore</button></div></td></tr>`),
    "No failed events",
  );
  $("#adminJobsTable").innerHTML = adminTable(
    ["Job", "Target", "Mode", "Status", "Completed"],
    state.admin.jobs.map((row) => `<tr><td><div class="cell-main"><strong>${escapeHtml(text(row.job_type))}</strong><small>${escapeHtml(shortId(row.job_id, 7))}</small></div></td><td>${escapeHtml(text(row.target_type))} ${escapeHtml(shortId(row.target_id, 6))}</td><td>${escapeHtml(text(row.mode))}</td><td><span class="quality-chip ${statusClass(row.status)}">${escapeHtml(text(row.status))}</span></td><td>${escapeHtml(formatTime(row.completed_at))}</td></tr>`),
    "No admin jobs",
  );
  $("#adminIncidentsTable").innerHTML = adminTable(
    ["Incident", "Severity", "Status", "Started", "Actions"],
    state.admin.incidents.map((row) => `<tr><td><div class="cell-main"><strong>${escapeHtml(text(row.title))}</strong><small>${escapeHtml(text(row.summary))}</small></div></td><td>${escapeHtml(text(row.severity))}</td><td><span class="quality-chip ${statusClass(row.status)}">${escapeHtml(text(row.status))}</span></td><td>${escapeHtml(formatTime(row.started_at))}</td><td><div class="admin-table-actions"><button data-incident-action="note" data-id="${escapeHtml(row.incident_id)}" type="button">Note</button><button data-incident-action="resolve" data-id="${escapeHtml(row.incident_id)}" type="button">Resolve</button></div></td></tr>`),
    "No incidents",
  );
  $("#adminNoticesTable").innerHTML = adminTable(
    ["Notice", "Status", "Starts", "Ends", "Action"],
    state.admin.notices.map((row) => `<tr><td><div class="cell-main"><strong>${escapeHtml(text(row.title))}</strong><small>${escapeHtml(text(row.message))}</small></div></td><td><span class="quality-chip ${statusClass(row.status)}">${escapeHtml(text(row.status))}</span></td><td>${escapeHtml(formatTime(row.starts_at))}</td><td>${escapeHtml(formatTime(row.ends_at))}</td><td><div class="admin-table-actions"><button data-notice-complete data-id="${escapeHtml(row.notice_id)}" type="button">Complete</button></div></td></tr>`),
    "No maintenance notices",
  );
  $("#adminRetentionTable").innerHTML = adminTable(
    ["Resource", "Days", "Legal hold", "Version", "Actions"],
    state.admin.retention.map((row) => `<tr data-retention-resource="${escapeHtml(row.resource_type)}"><td><strong>${escapeHtml(text(row.resource_type))}</strong></td><td><input data-retention-days type="number" min="1" step="1" value="${escapeHtml(text(row.retention_days, "30"))}" aria-label="Retention days"></td><td><input data-retention-hold type="checkbox" ${row.legal_hold ? "checked" : ""} aria-label="Legal hold"></td><td>${escapeHtml(text(row.policy_version))}</td><td><div class="admin-table-actions"><button data-retention-action="save" type="button">Save</button><button data-retention-action="dry-run" type="button">Dry run</button><button data-retention-action="apply" class="danger-action" type="button">Apply</button></div></td></tr>`),
    "No retention policies",
  );
  $("#adminBundlesTable").innerHTML = adminTable(
    ["Bundle", "Scope", "Size", "Expires", "Action"],
    state.admin.bundles.map((row) => `<tr><td><div class="cell-main"><strong>${escapeHtml(shortId(row.bundle_id, 8))}</strong><small>${escapeHtml(shortId(row.content_sha256, 8))}</small></div></td><td>${row.account_id ? `Account ${escapeHtml(shortId(row.account_id, 6))}` : "Tenant"}</td><td>${escapeHtml(formatNumber(row.byte_count, 0))} bytes</td><td>${escapeHtml(formatTime(row.expires_at))}</td><td><div class="admin-table-actions"><button data-bundle-download data-id="${escapeHtml(row.bundle_id)}" type="button">Download</button></div></td></tr>`),
    "No evidence bundles",
  );
}

async function loadAdmin() {
  if (!state.apiKey) return;
  const routes = {
    dashboard: "/admin/dashboard",
    jobs: "/admin/jobs?limit=200",
    dlq: "/admin/dlq?limit=200",
    notices: "/admin/notices",
    incidents: "/admin/incidents",
    retention: "/admin/retention",
    bundles: "/admin/evidence-bundles?limit=200",
  };
  try {
    const values = await Promise.all(Object.values(routes).map((path) => apiRequest(path)));
    Object.keys(routes).forEach((key, index) => {
      const value = values[index];
      state.admin[key] = key === "dashboard" ? value : Array.isArray(value.items) ? value.items : [];
    });
    state.adminLoaded = true;
    renderAdmin();
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
  }
}

async function openReplay(replayId) {
  state.replayId = String(replayId);
  try {
    const session = await apiRequest(`/replays/${state.replayId}`);
    const eventsPage = await apiRequest(`/replays/${state.replayId}/events?limit=200`);
    state.replaySession = session;
    state.replayEvents = Array.isArray(eventsPage.items) ? eventsPage.items : [];
    state.replayReport = String(session.status).toUpperCase() === "COMPLETED"
      ? await apiRequest(`/replays/${state.replayId}/report`)
      : null;
    renderReplayDetail();
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
  }
}

function drawNavChart(canvas, empty, history) {
  const rows = [...history].reverse().filter((row) => asNumber(row.equity) !== null || asNumber(row.conservative_equity) !== null);
  empty.hidden = rows.length > 1;
  canvas.hidden = rows.length <= 1;
  if (rows.length <= 1) return;
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.max(1, Math.round(rect.height * ratio));
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  const width = rect.width;
  const height = rect.height;
  const inset = { left: 56, right: 18, top: 14, bottom: 28 };
  const values = rows.flatMap((row) => [asNumber(row.equity), asNumber(row.conservative_equity)]).filter((value) => value !== null);
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  const pad = Math.max((max - min) * 0.1, 0.01);
  min -= pad;
  max += pad;
  context.clearRect(0, 0, width, height);
  context.font = "10px Inter, system-ui, sans-serif";
  context.textBaseline = "middle";
  for (let index = 0; index <= 4; index += 1) {
    const y = inset.top + ((height - inset.top - inset.bottom) * index / 4);
    const value = max - ((max - min) * index / 4);
    context.strokeStyle = "#e4e8ed";
    context.beginPath(); context.moveTo(inset.left, y); context.lineTo(width - inset.right, y); context.stroke();
    context.fillStyle = "#7b8490";
    context.textAlign = "right";
    context.fillText(formatMoney(value, 0), inset.left - 7, y);
  }
  const draw = (field, color) => {
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.beginPath();
    let started = false;
    rows.forEach((row, index) => {
      const value = asNumber(row[field]);
      if (value === null) return;
      const x = inset.left + ((width - inset.left - inset.right) * index / Math.max(1, rows.length - 1));
      const y = inset.top + ((max - value) / (max - min)) * (height - inset.top - inset.bottom);
      if (!started) { context.moveTo(x, y); started = true; } else context.lineTo(x, y);
    });
    context.stroke();
  };
  draw("equity", "#1769e0");
  draw("conservative_equity", "#16794b");
  context.fillStyle = "#7b8490";
  context.textAlign = "left";
  context.fillText(formatTime(rows[0].observed_at), inset.left, height - 10);
  context.textAlign = "right";
  context.fillText(formatTime(rows.at(-1).observed_at), width - inset.right, height - 10);
}

function renderOverview() {
  renderMetrics();
  renderQualitySummary();
  renderTable($("#overviewPositions"), state.data.positions.filter((row) => asNumber(row.quantity, 0) !== 0), TABLES.positions, 6);
  renderTable($("#overviewOrders"), state.data.orders, TABLES.orders, 6);
  const history = Array.isArray(asObject(state.data.performance).history) ? state.data.performance.history : [];
  drawNavChart($("#navChart"), $("#navChartEmpty"), history);
  $("#navLegend").innerHTML = `<span><i></i>Equity</span><span><i></i>Conservative equity</span>`;
}

function renderAll() {
  renderOverview();
  renderResource();
  renderPerformance();
  renderReplays();
  renderResearch();
  if (state.adminLoaded) renderAdmin();
  navigate(state.view, false);
}

function navigate(view, updateLocation = true) {
  if (!(view in VIEW_TITLES)) return;
  state.view = view;
  $("#pageTitle").textContent = VIEW_TITLES[view];
  $$(".paper-nav [data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $("#overviewView").hidden = view !== "overview";
  $("#resourceView").hidden = !["orders", "positions", "fills", "ledger", "journal"].includes(view);
  $("#performanceView").hidden = view !== "performance";
  $("#replayView").hidden = view !== "replays";
  $("#researchView").hidden = view !== "research";
  $("#adminView").hidden = view !== "admin";
  $("#workspaceContent").hidden = view !== "admin" && state.accounts.length === 0;
  $("#emptyState").hidden = view === "admin" || state.accounts.length > 0;
  if (updateLocation) history.replaceState(null, "", `${location.pathname}${location.search}#${view}`);
  renderResource();
  if (view === "performance") requestAnimationFrame(renderPerformance);
  if (view === "replays" && !state.replayId && replayRows()[0]) void openReplay(replayRows()[0].replay_session_id);
  if (view === "admin" && !state.adminLoaded) void loadAdmin();
}

async function openAudit(orderId) {
  try {
    const audit = await apiRequest(`/audit/${orderId}`);
    state.auditOrderId = Number(orderId);
    const order = asObject(audit.order);
    const quality = asObject(audit.quality);
    $("#auditTitle").textContent = `Order #${orderId}`;
    $("#auditSummary").innerHTML = [
      ["Market", text(order.market_id, shortId(order.asset_id, 10))],
      ["Side / TIF", `${text(order.side)} / ${text(order.time_in_force)}`],
      ["Limit / size", `${formatNumber(order.limit_price, 4)} / ${formatNumber(order.size, 4)}`],
      ["Status", text(order.status)],
    ].map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong title="${escapeHtml(value)}">${escapeHtml(value)}</strong></div>`).join("");
    const qualityRows = [
      ["Fidelity", quality.execution_fidelity, "info"],
      ["Confidence", quality.model_confidence, quality.model_confidence === "UNASSESSED" ? "warn" : "info"],
      ["Calibration", quality.calibration_domain, quality.calibration_domain === "UNASSESSED" ? "warn" : "info"],
      ["Data", quality.data_quality, quality.data_quality === "DATA_DEGRADED" ? "warn" : "good"],
      ["Capacity", quality.capacity_status, quality.capacity_status === "CAPACITY_EXCEEDED" ? "bad" : "info"],
      ["Model", quality.model_version, "info"],
    ];
    $("#auditQuality").innerHTML = qualityRows.map(([label, value, className]) => `<span class="quality-chip ${className}">${escapeHtml(label)}: ${escapeHtml(text(value))}</span>`).join("");
    const timeline = Array.isArray(audit.timeline) ? audit.timeline : [];
    $("#auditTimeline").innerHTML = timeline.length ? timeline.map((row) => `<li><time>${escapeHtml(formatTime(row.event_ts))}</time><div class="timeline-event"><strong>${escapeHtml(text(row.state, row.event_type))}</strong><small>${escapeHtml(text(row.reason, row.kind))}${row.checkpoint_id ? ` | ${escapeHtml(shortId(row.checkpoint_id, 9))}` : ""}</small></div></li>`).join("") : `<li><div class="timeline-event"><strong>No lifecycle events</strong><small>No persisted timeline rows were returned.</small></div></li>`;
    const execution = asObject(audit.execution_audit);
    const tca = asObject(audit.tca);
    const evidenceRows = [
      ["Decision checkpoint", execution.decision_checkpoint_id || tca.decision_checkpoint_id],
      ["Arrival checkpoint", execution.arrival_checkpoint_id || tca.arrival_checkpoint_id],
      ["Coverage grade", quality.coverage_grade],
      ["Book age", quality.book_age_ms === null || quality.book_age_ms === undefined ? null : `${quality.book_age_ms} ms`],
      ["Fill VWAP", tca.fill_vwap],
      ["Implementation shortfall", tca.implementation_shortfall],
      ["Fees", tca.fee_cost],
      ["Filled size", execution.filled_size || tca.filled_size],
      ["Audit key", execution.audit_key || order.result_audit_key],
    ];
    $("#auditEvidence").innerHTML = evidenceRows.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd title="${escapeHtml(text(value))}">${escapeHtml(text(value))}</dd></div>`).join("");
    const cancel = $("#cancelPaperOrder");
    cancel.hidden = TERMINAL_ORDER_STATES.has(String(order.status).toUpperCase());
    $("#orderAuditDialog").showModal();
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
  }
}

async function cancelCurrentOrder() {
  if (!state.auditOrderId || !confirm(`Cancel paper order #${state.auditOrderId}?`)) return;
  try {
    await apiRequest(`/orders/${state.auditOrderId}`, {
      method: "DELETE",
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    $("#orderAuditDialog").close();
    await loadAccountWorkspace();
    showToast(`Paper order #${state.auditOrderId} cancel requested`);
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
  }
}

function openAccountDialog(mode) {
  const isFork = mode === "fork";
  $("#accountMode").value = mode;
  $("#accountDialogKicker").textContent = isFork ? "Immutable generation" : "Paper account";
  $("#accountDialogTitle").textContent = isFork ? "Fork account" : "New account";
  $("#accountNameInput").value = isFork ? `${text($("#accountName").textContent, "Paper account")} fork` : "";
  $("#initialCashField").hidden = isFork;
  $("#accountDialog").showModal();
  $("#accountNameInput").focus();
}

async function submitAccount(event) {
  event.preventDefault();
  const mode = $("#accountMode").value;
  const body = mode === "fork"
    ? { name: $("#accountNameInput").value.trim() }
    : { name: $("#accountNameInput").value.trim(), initial_cash: $("#initialCashInput").value };
  const path = mode === "fork" ? `/accounts/${state.accountId}/fork` : "/accounts";
  try {
    const account = await apiRequest(path, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    $("#accountDialog").close();
    state.accountId = String(account.account_id);
    await loadAccounts();
    showToast(mode === "fork" ? "Paper account forked" : "Paper account created");
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
  }
}

function replayLocalTime(date) {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 19);
}

function openReplayDialog(mode) {
  const isFork = mode === "fork";
  $("#replayMode").value = mode;
  $("#replayDialogTitle").textContent = isFork ? "Fork replay" : "New replay";
  $("#replaySourceFields").hidden = isFork;
  $("#replayNameInput").value = isFork
    ? `${text(asObject(state.replaySession).name, "Replay")} fork`
    : `Replay ${new Date().toLocaleDateString()}`;
  if (!isFork) {
    const end = new Date();
    const start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
    $("#replayStartInput").value = replayLocalTime(start);
    $("#replayEndInput").value = replayLocalTime(end);
  }
  $("#replayBenchmarkInput").value = text(asObject(state.replaySession).benchmark, "CASH");
  $("#replayDialog").showModal();
  $("#replayNameInput").focus();
}

async function refreshReplays(selectedId = state.replayId) {
  const page = await apiRequest("/replays?limit=200");
  state.data.replays = Array.isArray(page.items) ? page.items : [];
  renderReplays();
  if (selectedId) await openReplay(selectedId);
}

async function submitReplay(event) {
  event.preventDefault();
  const mode = $("#replayMode").value;
  const isFork = mode === "fork";
  const account = state.accounts.find((row) => String(row.account_id) === state.accountId);
  if (!account || (isFork && !state.replayId)) return;
  const body = isFork
    ? {
        name: $("#replayNameInput").value.trim(),
        benchmark: $("#replayBenchmarkInput").value,
      }
    : {
        account_id: state.accountId,
        strategy_id: account.default_strategy_id,
        name: $("#replayNameInput").value.trim(),
        start_ts: new Date($("#replayStartInput").value).toISOString(),
        end_ts: new Date($("#replayEndInput").value).toISOString(),
        speed: $("#replaySpeedInput").value,
        seed: Number($("#replaySeedInput").value),
        strategy_version: $("#replayStrategyVersionInput").value.trim(),
        execution_model: $("#replayExecutionModelInput").value.trim(),
        benchmark: $("#replayBenchmarkInput").value,
      };
  const path = isFork ? `/replays/${state.replayId}/fork` : "/replays";
  try {
    const session = await apiRequest(path, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    $("#replayDialog").close();
    await refreshReplays(String(session.replay_session_id));
    showToast(isFork ? "Replay fork created" : "Replay session created");
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
  }
}

async function mutateReplay(action) {
  if (!state.replayId) return;
  try {
    const session = await apiRequest(`/replays/${state.replayId}/${action}`, {
      method: "POST",
      body: action === "resume" ? JSON.stringify({ max_events: 5000 }) : undefined,
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    await refreshReplays(String(session.replay_session_id));
    showToast(action === "pause" ? "Replay paused" : `Replay ${text(session.status).toLowerCase()}`);
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
  }
}

function parseJsonObject(value, label) {
  let parsed;
  try {
    parsed = JSON.parse(value || "{}");
  } catch {
    throw new Error(`${label} must be valid JSON`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return parsed;
}

function openScenarioDialog() {
  $("#scenarioNameInput").value = "";
  $("#scenarioPayoutInput").value = "{}";
  $("#scenarioShockInput").value = "{}";
  $("#scenarioHaircutInput").value = "0";
  $("#scenarioFeeInput").value = "1";
  $("#scenarioLatencyInput").value = "0";
  $("#scenarioDisputeInput").value = "0";
  $("#scenarioDialog").showModal();
  $("#scenarioNameInput").focus();
}

async function submitScenario(event) {
  event.preventDefault();
  try {
    const body = {
      account_id: state.accountId,
      name: $("#scenarioNameInput").value.trim(),
      inputs: {
        payout_by_asset: parseJsonObject($("#scenarioPayoutInput").value, "Resolution payouts"),
        price_shock_by_asset: parseJsonObject($("#scenarioShockInput").value, "Price shocks"),
        book_depth_haircut: $("#scenarioHaircutInput").value,
        fee_multiplier: $("#scenarioFeeInput").value,
        latency_shock_ms: $("#scenarioLatencyInput").value,
        dispute_duration_hours: $("#scenarioDisputeInput").value,
      },
    };
    await apiRequest("/scenarios", {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    $("#scenarioDialog").close();
    await loadAccountWorkspace();
    showToast("Scenario run persisted outside paper PnL");
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
  }
}

function openConditionalDialog() {
  $("#conditionalAssetInput").value = "";
  $("#conditionalDialog").showModal();
  $("#conditionalAssetInput").focus();
}

async function submitConditional(event) {
  event.preventDefault();
  const account = state.accounts.find((row) => String(row.account_id) === state.accountId);
  if (!account?.default_strategy_id) return showToast("Account strategy is unavailable", true);
  const orderType = $("#conditionalTypeInput").value;
  const trigger = {
    kind: "PRICE",
    operator: $("#conditionalOperatorInput").value,
    value: $("#conditionalTriggerInput").value,
    reference_price: "MID",
    ...(orderType === "TRAILING_STOP" ? { trailing_percent: $("#conditionalTrailingInput").value } : {}),
  };
  const body = {
    account_id: state.accountId,
    strategy_id: account.default_strategy_id,
    order_type: orderType,
    trigger,
    child_order: {
      asset_id: $("#conditionalAssetInput").value.trim(),
      side: $("#conditionalSideInput").value,
      time_in_force: $("#conditionalTifInput").value,
      limit_price: $("#conditionalLimitInput").value,
      size: $("#conditionalSizeInput").value,
      amount_unit: "SHARES",
    },
  };
  try {
    await apiRequest("/conditional-orders", {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    $("#conditionalDialog").close();
    await loadAccountWorkspace();
    showToast("Conditional order armed");
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
  }
}

async function cancelConditional(orderId) {
  if (!confirm("Cancel this synthetic conditional order?")) return;
  try {
    await apiRequest(`/conditional-orders/${orderId}`, {
      method: "DELETE",
      body: JSON.stringify({ reason: "user_cancel" }),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    await loadAccountWorkspace();
    showToast("Conditional order cancelled");
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
  }
}

async function adminPost(path, body, successMessage) {
  try {
    const result = await apiRequest(path, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    await loadAccounts();
    await loadAdmin();
    showToast(successMessage);
    return result;
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
    return null;
  }
}

function requestedReason(action) {
  const reason = prompt(`Reason for ${action}:`);
  return reason === null ? null : reason.trim();
}

async function setTenantFrozen(frozen) {
  if (frozen && !confirm("Freeze this paper tenant and block new trading mutations?")) return;
  const reason = requestedReason(frozen ? "tenant freeze" : "tenant unfreeze");
  if (!reason) return showToast("A reason is required", true);
  await adminPost(`/admin/tenant/${frozen ? "freeze" : "unfreeze"}`, { reason }, frozen ? "Tenant frozen" : "Tenant unfrozen");
}

async function administerSelectedAccount(action) {
  if (!state.accountId) return;
  if (["freeze", "kill"].includes(action) && !confirm(`${action === "kill" ? "Kill" : "Freeze"} the selected paper account?`)) return;
  if (action === "reconcile") {
    await adminPost(`/admin/accounts/${state.accountId}/reconcile`, { mode: "DRY_RUN" }, "Account reconciliation completed");
    return;
  }
  const reason = requestedReason(`account ${action}`);
  if (!reason) return showToast("A reason is required", true);
  await adminPost(`/admin/accounts/${state.accountId}/${action}`, { reason }, `Account ${action} completed`);
}

function openSupportDialog(mode) {
  const incident = mode === "incident";
  $("#supportMode").value = mode;
  $("#supportDialogTitle").textContent = incident ? "New incident" : "New maintenance notice";
  $("#supportBodyLabel").textContent = incident ? "Summary" : "Message";
  $("#supportSeverityField").hidden = !incident;
  $("#supportEndsField").hidden = incident;
  $("#supportTitleInput").value = "";
  $("#supportBodyInput").value = "";
  const start = new Date();
  $("#supportStartsInput").value = replayLocalTime(start);
  $("#supportEndsInput").value = replayLocalTime(new Date(start.getTime() + 60 * 60 * 1000));
  $("#supportDialog").showModal();
  $("#supportTitleInput").focus();
}

async function submitSupportRecord(event) {
  event.preventDefault();
  const mode = $("#supportMode").value;
  const incident = mode === "incident";
  const body = incident
    ? {
        title: $("#supportTitleInput").value.trim(),
        summary: $("#supportBodyInput").value.trim(),
        severity: $("#supportSeverityInput").value,
        started_at: new Date($("#supportStartsInput").value).toISOString(),
      }
    : {
        title: $("#supportTitleInput").value.trim(),
        message: $("#supportBodyInput").value.trim(),
        starts_at: new Date($("#supportStartsInput").value).toISOString(),
        ends_at: $("#supportEndsInput").value ? new Date($("#supportEndsInput").value).toISOString() : null,
      };
  const result = await adminPost(`/admin/${incident ? "incidents" : "notices"}`, body, incident ? "Incident created" : "Maintenance notice created");
  if (result) $("#supportDialog").close();
}

async function handleAdminTableAction(target) {
  const dlqAction = target.dataset.dlqAction;
  if (dlqAction) {
    const body = dlqAction === "ignore" ? { reason: requestedReason("DLQ ignore") } : {};
    if (dlqAction === "ignore" && !body.reason) return;
    await adminPost(`/admin/dlq/${target.dataset.id}/${dlqAction}`, body, `DLQ ${dlqAction} completed`);
    return;
  }
  const incidentAction = target.dataset.incidentAction;
  if (incidentAction) {
    if (incidentAction === "note") {
      const body = prompt("Incident note:");
      if (!body?.trim()) return;
      await adminPost(`/admin/incidents/${target.dataset.id}/notes`, { body: body.trim() }, "Incident note added");
    } else {
      await adminPost(`/admin/incidents/${target.dataset.id}/status`, { status: "RESOLVED" }, "Incident resolved");
    }
    return;
  }
  if (target.hasAttribute("data-notice-complete")) {
    await adminPost(`/admin/notices/${target.dataset.id}/status`, { status: "COMPLETED" }, "Notice completed");
    return;
  }
  const retentionAction = target.dataset.retentionAction;
  if (retentionAction) {
    const row = target.closest("tr[data-retention-resource]");
    const resource = row.dataset.retentionResource;
    if (retentionAction === "apply" && !confirm(`Apply retention deletion for ${resource}?`)) return;
    const body = retentionAction === "save"
      ? { retention_days: Number(row.querySelector("[data-retention-days]").value), legal_hold: row.querySelector("[data-retention-hold]").checked }
      : { mode: retentionAction === "apply" ? "APPLY" : "DRY_RUN" };
    await adminPost(`/admin/retention/${resource}${retentionAction === "save" ? "" : "/run"}`, body, `Retention ${retentionAction} completed`);
    return;
  }
  if (target.hasAttribute("data-bundle-download")) await downloadEvidenceBundle(target.dataset.id);
}

async function createEvidenceBundle() {
  const body = state.accountId ? { account_id: state.accountId } : {};
  await adminPost("/admin/evidence-bundles", body, "Evidence bundle created");
}

async function downloadEvidenceBundle(bundleId) {
  try {
    const response = await fetch(`${state.apiBase}/admin/evidence-bundles/${bundleId}/download`, {
      headers: { Authorization: `Bearer ${state.apiKey}`, Accept: "application/zip" }, cache: "no-store",
    });
    if (!response.ok) throw new Error(`Evidence download failed with HTTP ${response.status}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `paper-evidence-${bundleId}.zip`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    showToast("Evidence bundle downloaded");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function exportAccount() {
  if (!state.accountId) return;
  const resource = $("#exportResource").value;
  const format = $("#exportFormat").value;
  try {
    const response = await fetch(`${state.apiBase}/accounts/${state.accountId}/export?resource=${encodeURIComponent(resource)}&format=${encodeURIComponent(format)}`, {
      headers: { Authorization: `Bearer ${state.apiKey}` }, cache: "no-store",
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const error = asObject(payload.error);
      throw Object.assign(new Error(text(error.message, `Export failed with HTTP ${response.status}`)), { code: error.code });
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const name = disposition.match(/filename="([^"]+)"/)?.[1] || `paper-${state.accountId}-${resource}.${format}`;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = name; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    showToast(`Exported ${response.headers.get("X-Export-Row-Count") || "0"} ${resource} rows`);
  } catch (error) {
    showToast(`${error.code ? `${error.code}: ` : ""}${error.message}`, true);
  }
}

function bindEvents() {
  $$(".paper-nav [data-view]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
  $$('[data-open-view]').forEach((button) => button.addEventListener("click", () => navigate(button.dataset.openView)));
  $$('[data-create-account]').forEach((button) => button.addEventListener("click", () => openAccountDialog("create")));
  $$('[data-close-dialog]').forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  $("#connectionButton").addEventListener("click", () => {
    $("#apiBaseInput").value = state.apiBase;
    $("#apiKeyInput").value = "";
    $("#connectionDialog").showModal();
  });
  $("#modelDisclosureButton").addEventListener("click", () => $("#modelDisclosureDialog").showModal());
  $("#connectionForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    state.apiBase = $("#apiBaseInput").value.trim().replace(/\/$/, "");
    state.apiKey = $("#apiKeyInput").value.trim();
    sessionStorage.setItem(SESSION_KEY, state.apiKey);
    await connect();
  });
  $("#disconnectButton").addEventListener("click", () => {
    sessionStorage.removeItem(SESSION_KEY);
    state.apiKey = "";
    state.accounts = [];
    renderAccountPicker();
    setConnection(false, "Not connected");
    $("#connectionDialog").close();
    $("#workspaceContent").hidden = true;
    $("#emptyState").hidden = false;
  });
  $("#accountSelect").addEventListener("change", async (event) => {
    state.accountId = event.target.value;
    const url = new URL(location.href);
    url.searchParams.set("account_id", state.accountId);
    history.replaceState(null, "", url);
    await loadAccountWorkspace();
  });
  $("#refreshButton").addEventListener("click", loadAccountWorkspace);
  $("#createAccountButton").addEventListener("click", () => openAccountDialog("create"));
  $("#forkButton").addEventListener("click", () => openAccountDialog("fork"));
  $("#accountForm").addEventListener("submit", submitAccount);
  $("#createReplayButton").addEventListener("click", () => openReplayDialog("create"));
  $("#replayForm").addEventListener("submit", submitReplay);
  $("#pauseReplayButton").addEventListener("click", () => mutateReplay("pause"));
  $("#resumeReplayButton").addEventListener("click", () => mutateReplay("resume"));
  $("#forkReplayButton").addEventListener("click", () => openReplayDialog("fork"));
  $("#createScenarioButton").addEventListener("click", openScenarioDialog);
  $("#scenarioForm").addEventListener("submit", submitScenario);
  $("#createConditionalButton").addEventListener("click", openConditionalDialog);
  $("#conditionalForm").addEventListener("submit", submitConditional);
  $("#refreshAdminButton").addEventListener("click", loadAdmin);
  $("#freezeTenantButton").addEventListener("click", () => setTenantFrozen(true));
  $("#unfreezeTenantButton").addEventListener("click", () => setTenantFrozen(false));
  $("#freezeAccountButton").addEventListener("click", () => administerSelectedAccount("freeze"));
  $("#unfreezeAccountButton").addEventListener("click", () => administerSelectedAccount("unfreeze"));
  $("#reconcileAccountButton").addEventListener("click", () => administerSelectedAccount("reconcile"));
  $("#killAccountButton").addEventListener("click", () => administerSelectedAccount("kill"));
  $("#createIncidentButton").addEventListener("click", () => openSupportDialog("incident"));
  $("#createNoticeButton").addEventListener("click", () => openSupportDialog("notice"));
  $("#supportForm").addEventListener("submit", submitSupportRecord);
  $("#createBundleButton").addEventListener("click", createEvidenceBundle);
  $("#resourceFilter").addEventListener("input", renderResource);
  $("#exportButton").addEventListener("click", exportAccount);
  $("#cancelPaperOrder").addEventListener("click", cancelCurrentOrder);
  document.body.addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-order-id]");
    if (row) openAudit(row.dataset.orderId);
    const replayRow = event.target.closest("tr[data-replay-id]");
    if (replayRow) openReplay(replayRow.dataset.replayId);
    const adminAction = event.target.closest("[data-dlq-action], [data-incident-action], [data-notice-complete], [data-retention-action], [data-bundle-download]");
    if (adminAction) void handleAdminTableAction(adminAction);
    const conditionalCancel = event.target.closest("[data-conditional-cancel]");
    if (conditionalCancel) void cancelConditional(conditionalCancel.dataset.conditionalCancel);
  });
  window.addEventListener("resize", () => {
    clearTimeout(bindEvents.resizeTimer);
    bindEvents.resizeTimer = setTimeout(() => {
      const historyRows = Array.isArray(asObject(state.data.performance).history) ? state.data.performance.history : [];
      if (state.view === "overview") drawNavChart($("#navChart"), $("#navChartEmpty"), historyRows);
      if (state.view === "performance") drawNavChart($("#performanceChart"), $("#performanceChartEmpty"), historyRows);
    }, 120);
  });
}

async function initialize() {
  bindEvents();
  $("#apiBaseInput").value = state.apiBase;
  navigate(state.view, false);
  if (!state.apiKey) {
    setConnection(false, "Not connected");
    $("#connectionDialog").showModal();
    return;
  }
  await connect();
}

initialize();
