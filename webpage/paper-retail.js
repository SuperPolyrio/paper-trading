const app = document.querySelector("#retailApp");
const state = {
  apiBase: (new URLSearchParams(location.search).get("api_base") || app.dataset.apiBase || "/paper-api/v1/paper").replace(/\/$/, ""),
  csrf: "",
  session: null,
  markets: [],
  watchlist: new Set(),
  selectedMarket: null,
  selectedOutcome: null,
  orderSide: "BUY",
  orderTab: "orders",
  toolsTab: "maker",
  leaderboardMetric: "return",
  marketCollection: "all",
  data: {
    wallets: [],
    performance: null,
    positions: [],
    orders: [],
    fills: [],
    ledger: [],
    predictions: [],
    predictionReport: null,
    notifications: [],
    leaderboard: [],
    following: new Set(),
    competitions: [],
    competitionStandings: null,
    riskProfile: null,
    pnlAttribution: null,
    accountTruth: null,
    eventRisk: null,
    eventRelations: null,
    lifecycle: null,
    makerWorkbench: null,
    conditionalOrders: [],
    positionOperations: [],
    positionOperationCandidates: null,
    scenarios: [],
    replays: [],
    dataRequests: [],
    officialHistory: [],
    preferences: { locale: "zh-CN", timezone_name: "Asia/Shanghai", reduce_motion: false, high_contrast: false },
  },
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const number = (value, fallback = null) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};
const valueText = (value, fallback = "不可用") => value === null || value === undefined || value === "" ? fallback : String(value);
const escapeHtml = (value) => valueText(value, "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
const money = (value, digits = 2) => {
  const parsed = number(value);
  return parsed === null ? "不可用" : `${parsed < 0 ? "-" : ""}$${Math.abs(parsed).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
};
const quantity = (value, digits = 4) => {
  const parsed = number(value);
  return parsed === null ? "不可用" : parsed.toLocaleString("en-US", { maximumFractionDigits: digits });
};
const percent = (value, digits = 2) => {
  const parsed = number(value);
  return parsed === null ? "不可用" : `${(parsed * 100).toFixed(digits)}%`;
};
const probability = (value) => {
  const parsed = number(value);
  return parsed === null ? "--" : `${(parsed * 100).toFixed(parsed < 0.1 || parsed > 0.9 ? 1 : 0)}¢`;
};
const time = (value) => {
  if (!value) return "不可用";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  const preferences = object(state.data.preferences);
  try {
    return parsed.toLocaleString(preferences.locale || "zh-CN", { hour12: false, timeZone: preferences.timezone_name || "Asia/Shanghai" });
  } catch (_) {
    return parsed.toLocaleString("zh-CN", { hour12: false });
  }
};
const shortId = (value, length = 8) => {
  const selected = valueText(value, "");
  return selected.length <= length * 2 + 3 ? selected : `${selected.slice(0, length)}...${selected.slice(-length)}`;
};
const pnlClass = (value) => number(value, 0) > 0 ? "positive" : number(value, 0) < 0 ? "negative" : "";
const idempotencyKey = () => globalThis.crypto?.randomUUID?.() || `retail-${Date.now()}-${Math.random().toString(16).slice(2)}`;

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 4500);
}

async function api(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const mutation = !["GET", "HEAD", "OPTIONS"].includes(method);
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body !== undefined && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (mutation && state.csrf) headers["X-Paper-CSRF"] = state.csrf;
  if (mutation && !headers["Idempotency-Key"] && !path.startsWith("/retail/session/")) headers["Idempotency-Key"] = idempotencyKey();
  const response = await fetch(`${state.apiBase}${path}`, {
    cache: "no-store",
    credentials: "same-origin",
    ...options,
    method,
    headers,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = object(payload.error);
    const error = new Error(valueText(detail.message || payload.error, `HTTP ${response.status}`));
    error.status = response.status;
    error.code = valueText(detail.code, "PAPER_REQUEST_FAILED");
    error.details = detail.details;
    throw error;
  }
  if (payload.data !== undefined) return payload.data;
  return payload;
}

function showBanner(message, kind = "warn") {
  const banner = $("#globalBanner");
  banner.textContent = message;
  banner.dataset.kind = kind;
  banner.hidden = !message;
}

function wallet() { return object(object(state.session).wallet); }
function accountId() { return String(wallet().account_id || ""); }
function strategyId() { return String(wallet().strategy_id || ""); }

async function refreshSession() {
  const session = await api("/retail/session");
  state.session = session;
  state.csrf = String(session.csrf_token || state.csrf || "");
  renderWallet();
  return session;
}

async function guestLogin() {
  const button = $("#guestLoginButton");
  button.disabled = true;
  $("#loginStatus").textContent = "正在创建链下模拟钱包...";
  try {
    const result = await api("/retail/session/guest", { method: "POST", body: JSON.stringify({ display_name: "Paper trader" }) });
    state.csrf = String(result.csrf_token || "");
    await refreshSession();
    $("#loginDialog").close();
    await loadWorkspace();
  } catch (error) {
    $("#loginStatus").textContent = `${error.code}: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function walletLogin() {
  const button = $("#walletLoginButton");
  button.disabled = true;
  try {
    if (!globalThis.ethereum) throw new Error("没有检测到 Rabby 或其他 EVM 钱包扩展");
    $("#loginStatus").textContent = "等待钱包授权地址...";
    const accounts = await globalThis.ethereum.request({ method: "eth_requestAccounts" });
    const address = String(accounts?.[0] || "");
    if (!address) throw new Error("钱包没有返回地址");
    const challenge = await api("/retail/session/challenge", { method: "POST", body: JSON.stringify({ wallet_address: address }) });
    $("#loginStatus").textContent = "请签名登录消息。它不包含订单或资产授权。";
    const signature = await globalThis.ethereum.request({ method: "personal_sign", params: [challenge.message, address] });
    const result = await api("/retail/session/wallet", {
      method: "POST",
      body: JSON.stringify({ challenge_id: challenge.challenge_id, wallet_address: address, signature }),
    });
    state.csrf = String(result.csrf_token || "");
    await refreshSession();
    $("#loginDialog").close();
    await loadWorkspace();
  } catch (error) {
    $("#loginStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function logout() {
  try { await api("/retail/session/logout", { method: "POST", body: "{}" }); } catch (_) { /* Clear local state regardless. */ }
  state.session = null;
  state.csrf = "";
  renderWallet();
  $("#walletDialog").close();
  $("#loginDialog").showModal();
}

function renderWallet() {
  const selected = wallet();
  const user = object(object(state.session).user);
  const loggedIn = Boolean(selected.virtual_wallet_id);
  $("#walletName").textContent = loggedIn ? valueText(selected.display_name, user.display_name) : "未登录";
  $("#walletId").textContent = loggedIn ? shortId(selected.virtual_wallet_id, 7) : "--";
  $("#walletAvatar").textContent = loggedIn ? valueText(selected.display_name, "PW").slice(0, 2).toUpperCase() : "PW";
  const perf = object(state.data.performance);
  const summary = object(perf.summary);
  $("#headerNav").textContent = loggedIn ? money(summary.equity ?? selected.cash_balance ?? selected.initial_balance) : "--";
  $("#headerCash").textContent = loggedIn ? money(summary.available_cash ?? number(selected.cash_balance, 0) - number(selected.cash_reserved, 0)) : "--";
  $("#walletDetails").innerHTML = loggedIn ? [
    ["模拟钱包", selected.virtual_wallet_id],
    ["账户", selected.account_id],
    ["基础货币", selected.base_currency || "pUSD"],
    ["初始资金", money(selected.initial_balance)],
    ["可用现金", money(summary.available_cash ?? number(selected.cash_balance, 0) - number(selected.cash_reserved, 0))],
    ["连接身份", valueText(user.display_name || object(state.session).provider, "Paper user")],
  ].map(([term, definition]) => `<div><dt>${escapeHtml(term)}</dt><dd title="${escapeHtml(definition)}">${escapeHtml(shortId(definition, 13))}</dd></div>`).join("") : "";
  renderWalletList();
}

async function loadWallets() {
  const result = await api("/retail/wallets");
  state.data.wallets = Array.isArray(result.items) ? result.items : [];
  renderWalletList();
}

function renderWalletList() {
  const node = $("#walletList");
  if (!node) return;
  node.innerHTML = state.data.wallets.length ? state.data.wallets.map((row) => `<div class="wallet-row"><div><strong>${escapeHtml(valueText(row.display_name))}${row.is_default ? " · 当前" : ""}</strong><small>${escapeHtml(shortId(row.virtual_wallet_id, 10))} · ${escapeHtml(money(number(row.cash_balance, 0) - number(row.cash_reserved, 0)))}</small></div>${row.is_default ? "" : `<button type="button" data-select-wallet="${escapeHtml(row.virtual_wallet_id)}">切换</button>`}</div>`).join("") : `<div class="empty-row">没有模拟钱包</div>`;
  $$('[data-select-wallet]', node).forEach((button) => button.addEventListener("click", () => void selectWallet(button.dataset.selectWallet)));
}

async function selectWallet(virtualWalletId) {
  try {
    await api(`/retail/wallets/${encodeURIComponent(virtualWalletId)}/default`, { method: "PUT", body: "{}" });
    await refreshSession();
    await loadWorkspace();
    toast("已切换模拟钱包");
  } catch (error) { toast(error.message, true); }
}

async function forkWallet(event) {
  event.preventDefault();
  try {
    const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
    await api("/retail/wallets/fork", { method: "POST", body: JSON.stringify(payload) });
    await loadWallets();
    toast("已从当前不可变账本分叉新的模拟钱包");
  } catch (error) { toast(error.message, true); }
}

async function resetWallet() {
  const current = wallet();
  if (!current.virtual_wallet_id) return;
  const accepted = globalThis.confirm("重置会冻结当前钱包，并创建保留完整审计历史的新一代 10,000 pUSD 钱包。继续吗？");
  if (!accepted) return;
  try {
    await api("/retail/wallets/reset", { method: "POST", body: JSON.stringify({ confirm_virtual_wallet_id: current.virtual_wallet_id }) });
    await refreshSession();
    await loadWorkspace();
    $("#walletDialog").close();
    toast("已创建全新一代钱包；旧钱包及其账本保持只读");
  } catch (error) { toast(error.message, true); }
}

async function loadMarkets() {
  const query = $("#marketSearch").value.trim();
  const category = $("#categoryTabs button.active")?.dataset.category || "";
  const params = new URLSearchParams({ q: query, state: "LIVE", limit: "100" });
  if (category) params.set("category", category);
  params.set("collection", state.marketCollection);
  const result = await api(`/retail/markets?${params}`);
  state.markets = Array.isArray(result.items) ? result.items : [];
  renderMarkets();
  renderPredictionMarkets();
  renderAdvancedMarketOptions();
}

async function loadWatchlist() {
  const result = await api("/retail/watchlist");
  state.watchlist = new Set(Array.isArray(result.market_slugs) ? result.market_slugs.map(String) : []);
}

function renderMarkets() {
  $("#marketCount").textContent = String(state.markets.length);
  const list = $("#marketList");
  list.innerHTML = state.markets.length ? state.markets.map((market) => {
    const outcomes = Array.isArray(market.outcomes) ? market.outcomes : [];
    const yes = outcomes.find((row) => String(row.name).toUpperCase() === "YES") || outcomes[0] || {};
    const no = outcomes.find((row) => String(row.name).toUpperCase() === "NO") || outcomes[1] || {};
    const active = market.market_slug === state.selectedMarket?.market_slug;
    const activity = state.marketCollection === "hot" ? ` · 本地成交 ${valueText(market.observed_trade_count_24h, 0)}` : "";
    const ending = state.marketCollection === "ending" && market.end_date ? ` · ${time(market.end_date)}` : "";
    return `<button class="market-row${active ? " active" : ""}" type="button" data-market-slug="${escapeHtml(market.market_slug)}">
      <strong>${escapeHtml(market.title)}</strong>
      <span class="market-row-meta"><span class="category-tag">${escapeHtml(categoryLabel(market.category))}</span><span>${escapeHtml(valueText(market.state))}${escapeHtml(activity)}${escapeHtml(ending)}</span><span class="market-row-prices">YES ${escapeHtml(probability(yes.best_ask))} · NO ${escapeHtml(probability(no.best_ask))}</span></span>
    </button>`;
  }).join("") : `<div class="empty-row">没有符合条件的 LIVE 市场</div>`;
  $$('[data-market-slug]', list).forEach((button) => button.addEventListener("click", () => void selectMarket(button.dataset.marketSlug)));
}

function categoryLabel(value) {
  return ({ politics: "政治", sports: "体育", weather: "天气", crypto: "加密货币", other: "其他" })[String(value || "").toLowerCase()] || valueText(value, "其他");
}

async function selectMarket(slug) {
  try {
    const detail = await api(`/retail/markets/${encodeURIComponent(slug)}`);
    state.selectedMarket = detail;
    state.selectedOutcome = (detail.outcomes || []).find((row) => String(row.name).toUpperCase() === "YES") || detail.outcomes?.[0] || null;
    renderMarkets();
    renderMarketDetail();
    $(".market-workspace").classList.add("detail-open");
    api(`/retail/event-relations?market_slug=${encodeURIComponent(slug)}`).then((graph) => {
      state.data.eventRelations = graph;
      renderEventRisk();
    }).catch(() => { /* Relationship evidence is supplemental to trading. */ });
  } catch (error) {
    toast(`${error.code}: ${error.message}`, true);
  }
}

function normalizeBookLevels(value) {
  const levels = Array.isArray(value) ? value : [];
  return levels.map((row) => {
    if (Array.isArray(row)) return { price: number(row[0]), size: number(row[1]) };
    const item = object(row);
    return { price: number(item.price ?? item.p), size: number(item.size ?? item.quantity ?? item.q) };
  }).filter((row) => row.price !== null && row.size !== null);
}

function renderBook(outcome) {
  const asks = normalizeBookLevels(outcome.asks).sort((a, b) => b.price - a.price).slice(-8);
  const bids = normalizeBookLevels(outcome.bids).sort((a, b) => b.price - a.price).slice(0, 8);
  const cumulative = (rows) => {
    let total = 0;
    return rows.map((row) => ({ ...row, total: (total += row.size) }));
  };
  const askRows = cumulative(asks);
  const bidRows = cumulative(bids);
  const body = (rows, type) => rows.map((row) => `<tr class="${type}"><td>${row.price.toFixed(4)}</td><td>${quantity(row.size)}</td><td>${quantity(row.total)}</td></tr>`).join("");
  const spread = number(outcome.best_ask) !== null && number(outcome.best_bid) !== null ? number(outcome.best_ask) - number(outcome.best_bid) : null;
  return `<table class="book-table"><thead><tr><th>价格</th><th>数量</th><th>累计</th></tr></thead><tbody>${body(askRows, "ask")}</tbody></table>
    <div class="spread-row">Bid ${escapeHtml(probability(outcome.best_bid))} · Spread ${spread === null ? "不可用" : `${(spread * 100).toFixed(1)}¢`} · Ask ${escapeHtml(probability(outcome.best_ask))}</div>
    <table class="book-table"><tbody>${body(bidRows, "bid")}</tbody></table>`;
}

function renderMarketDetail() {
  const market = state.selectedMarket;
  const detail = $("#marketDetail");
  if (!market) return;
  const outcomes = Array.isArray(market.outcomes) ? market.outcomes : [];
  const selected = state.selectedOutcome || outcomes[0] || {};
  const watched = state.watchlist.has(String(market.market_slug));
  const price = state.orderSide === "BUY" ? selected.best_ask : selected.best_bid;
  const terms = object(selected.terms);
  const lifecycle = object(market.lifecycle);
  const recentTrades = Array.isArray(market.recent_trades) ? market.recent_trades.filter((row) => String(row.asset_id) === String(selected.asset_id)).slice(0, 50) : [];
  const visibleTrades = recentTrades.slice(0, 8);
  detail.innerHTML = `<div class="market-detail-head">
      <div><button class="mobile-back" type="button">返回市场</button><span class="category-tag">${escapeHtml(categoryLabel(market.category))}</span><h2>${escapeHtml(market.title)}</h2><p>${escapeHtml(valueText(market.state))} · ${escapeHtml(valueText(market.event_title, "独立市场"))}</p><div class="market-description">${escapeHtml(valueText(market.description, "暂无规则摘要"))}</div></div>
      <button class="watch-button${watched ? " active" : ""}" type="button" title="${watched ? "移出自选" : "加入自选"}" aria-label="${watched ? "移出自选" : "加入自选"}">${watched ? "★" : "☆"}</button>
    </div>
    <dl class="market-rules"><div><dt>结束时间</dt><dd>${escapeHtml(time(market.end_date))}</dd></div><div><dt>Oracle</dt><dd>${escapeHtml(valueText(market.oracle))}</dd></div><div><dt>手续费</dt><dd>${terms.fee_rate_bps === null || terms.fee_rate_bps === undefined ? "未加载" : `${escapeHtml(quantity(terms.fee_rate_bps, 0))} bps`}</dd></div><div><dt>生命周期</dt><dd>${escapeHtml(valueText(lifecycle.phase, market.state))}</dd></div></dl>
    <div class="outcome-tabs">${outcomes.map((outcome) => `<button class="${String(outcome.asset_id) === String(selected.asset_id) ? "active" : ""}" type="button" data-outcome-id="${escapeHtml(outcome.asset_id)}" data-outcome-name="${escapeHtml(String(outcome.name).toUpperCase())}">${escapeHtml(valueText(outcome.name))} ${escapeHtml(probability(outcome.best_ask))}</button>`).join("")}</div>
    <div class="trade-grid">
      <section class="book-panel"><header><strong>${escapeHtml(valueText(selected.name))} L2 盘口</strong><span>${escapeHtml(valueText(selected.book_quality))} · ${selected.book_age_ms === null || selected.book_age_ms === undefined ? "age 不可用" : `${quantity(selected.book_age_ms, 0)} ms`}</span></header>${renderBook(selected)}<div class="market-price-chart"><canvas id="marketPriceChart" aria-label="近期成交价格走势"></canvas><span id="marketPriceChartEmpty">近期成交不足，暂不绘制价格走势</span></div>${visibleTrades.length ? `<div class="table-wrap">${table(["最近成交", "方向", "价格", "数量"], visibleTrades.map((row) => `<tr><td>${escapeHtml(time(row.event_ts))}</td><td>${escapeHtml(valueText(row.aggressor_side))}</td><td class="numeric">${escapeHtml(quantity(row.price))}</td><td class="numeric">${escapeHtml(quantity(row.size))}</td></tr>`))}</div>` : ""}</section>
      <section class="order-ticket"><header><strong>模拟订单</strong><span>绝不提交实盘</span></header>
        <div class="side-toggle"><button class="${state.orderSide === "BUY" ? "active" : ""}" data-side="BUY" type="button">买入</button><button class="${state.orderSide === "SELL" ? "active" : ""}" data-side="SELL" type="button">卖出</button></div>
        <form id="orderForm">
          <div class="field-grid"><label><span>限价 (0–1)</span><input name="limit_price" type="number" min="0.0001" max="0.9999" step="${escapeHtml(valueText(selected.tick_size, "0.001"))}" value="${escapeHtml(valueText(price, "0.5"))}" required></label><label><span>数量</span><input name="size" type="number" min="${escapeHtml(valueText(selected.min_order_size, "0.01"))}" step="0.0001" value="${escapeHtml(valueText(selected.min_order_size, "1"))}" required></label></div>
          <div class="field-grid"><label><span>数量单位</span><select name="amount_unit"><option value="SHARES">Shares</option><option value="QUOTE">pUSD 金额</option></select></label><label><span>有效期</span><select name="time_in_force"><option value="FOK">FOK 全成或撤</option><option value="FAK">FAK 可部分成</option><option value="GTC">GTC 持续挂单</option><option value="GTD">GTD 定时失效</option></select></label></div>
          <label class="checkbox-line"><input name="post_only" type="checkbox"><span>Post-only Maker</span></label>
          <label class="gtd-field" hidden><span>失效时间</span><input name="expires_at" type="datetime-local"></label>
          <div class="allocation-buttons"><button type="button" data-allocation="0.25">25%</button><button type="button" data-allocation="0.50">50%</button><button type="button" data-allocation="0.75">75%</button><button type="button" data-allocation="1">100%</button></div>
          <div id="orderEstimate" class="order-estimate"></div>
          <button class="primary${state.orderSide === "SELL" ? " sell-action" : ""}" type="submit">${state.orderSide === "BUY" ? "提交模拟买单" : "提交模拟卖单"}</button>
        </form>
      </section>
    </div>`;
  $(".mobile-back", detail).addEventListener("click", () => $(".market-workspace").classList.remove("detail-open"));
  $(".watch-button", detail).addEventListener("click", () => void toggleWatchlist());
  $$('[data-outcome-id]', detail).forEach((button) => button.addEventListener("click", () => {
    state.selectedOutcome = outcomes.find((row) => String(row.asset_id) === button.dataset.outcomeId) || selected;
    renderMarketDetail();
  }));
  $$('[data-side]', detail).forEach((button) => button.addEventListener("click", () => {
    state.orderSide = button.dataset.side;
    renderMarketDetail();
  }));
  const form = $("#orderForm", detail);
  form.addEventListener("input", updateOrderEstimate);
  form.addEventListener("change", updateOrderRules);
  form.addEventListener("submit", submitOrder);
  $$('[data-allocation]', form).forEach((button) => button.addEventListener("click", () => applyAllocation(Number(button.dataset.allocation))));
  requestAnimationFrame(() => drawMarketPriceChart($("#marketPriceChart", detail), recentTrades));
  updateOrderRules();
  updateOrderEstimate();
}

function drawMarketPriceChart(canvas, trades) {
  const empty = $("#marketPriceChartEmpty", canvas.parentElement);
  const points = trades.map((row) => ({ ts: new Date(row.event_ts).getTime(), price: number(row.price) })).filter((row) => Number.isFinite(row.ts) && row.price !== null).sort((a, b) => a.ts - b.ts);
  if (points.length < 2) { canvas.hidden = true; empty.hidden = false; return; }
  canvas.hidden = false;
  empty.hidden = true;
  const bounds = canvas.getBoundingClientRect();
  const ratio = Math.max(1, globalThis.devicePixelRatio || 1);
  canvas.width = Math.max(1, Math.floor(bounds.width * ratio));
  canvas.height = Math.max(1, Math.floor(bounds.height * ratio));
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  const inset = 14;
  const prices = points.map((row) => row.price);
  let min = Math.min(...prices);
  let max = Math.max(...prices);
  if (min === max) { min = Math.max(0, min - 0.01); max = Math.min(1, max + 0.01); }
  context.clearRect(0, 0, bounds.width, bounds.height);
  context.strokeStyle = "#147d64";
  context.lineWidth = 2;
  context.beginPath();
  points.forEach((point, index) => {
    const x = inset + ((bounds.width - inset * 2) * index / Math.max(1, points.length - 1));
    const y = inset + ((max - point.price) / Math.max(0.000001, max - min)) * (bounds.height - inset * 2);
    if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
  });
  context.stroke();
}

function updateOrderRules() {
  const form = $("#orderForm");
  if (!form) return;
  const tif = form.elements.time_in_force.value;
  const amountUnit = form.elements.amount_unit;
  const postOnly = form.elements.post_only;
  $(".gtd-field", form).hidden = tif !== "GTD";
  if (state.orderSide === "SELL" || ["GTC", "GTD"].includes(tif)) amountUnit.value = "SHARES";
  amountUnit.querySelector('option[value="QUOTE"]').disabled = state.orderSide === "SELL" || ["GTC", "GTD"].includes(tif);
  postOnly.disabled = !["GTC", "GTD"].includes(tif);
  if (postOnly.disabled) postOnly.checked = false;
  updateOrderEstimate();
}

function updateOrderEstimate() {
  const form = $("#orderForm");
  if (!form) return;
  const price = number(form.elements.limit_price.value, 0);
  const size = number(form.elements.size.value, 0);
  const unit = form.elements.amount_unit.value;
  const desiredShares = unit === "QUOTE" && price > 0 ? size / price : size;
  const postOnly = form.elements.post_only.checked;
  const outcome = object(state.selectedOutcome);
  const rawLevels = state.orderSide === "BUY" ? outcome.asks : outcome.bids;
  const levels = normalizeBookLevels(rawLevels).filter((row) => state.orderSide === "BUY" ? row.price <= price : row.price >= price).sort((a, b) => state.orderSide === "BUY" ? a.price - b.price : b.price - a.price);
  let remaining = desiredShares;
  let filled = 0;
  let gross = 0;
  if (!postOnly) levels.forEach((level) => {
    if (remaining <= 0) return;
    const take = Math.min(remaining, level.size);
    filled += take;
    gross += take * level.price;
    remaining -= take;
  });
  const vwap = filled > 0 ? gross / filled : null;
  const feeRate = number(object(outcome.terms).fee_rate, number(object(outcome.terms).fee_rate_bps, 0) / 10000);
  const fee = vwap === null ? 0 : Math.round((filled * feeRate * vwap * (1 - vwap)) * 1e5) / 1e5;
  const availablePosition = state.data.positions.find((row) => String(row.asset_id) === String(outcome.asset_id));
  const capacityLabel = postOnly ? "挂单等待成交" : remaining <= 1e-9 ? "当前可见深度可满足" : `可见深度缺少 ${quantity(remaining)}`;
  $("#orderEstimate").innerHTML = `<div><span>预计成交 shares</span><strong>${escapeHtml(quantity(postOnly ? 0 : filled))} / ${escapeHtml(quantity(desiredShares))}</strong></div><div><span>预计 VWAP</span><strong>${vwap === null ? "不立即成交" : escapeHtml(vwap.toFixed(5))}</strong></div><div><span>预计现金变化</span><strong>${escapeHtml(money(state.orderSide === "BUY" ? -(gross + fee) : gross - fee, 5))}</strong></div><div><span>预计手续费</span><strong>${escapeHtml(money(fee, 5))}</strong></div><div><span>容量</span><strong>${escapeHtml(capacityLabel)}</strong></div><div><span>可卖持仓</span><strong>${escapeHtml(quantity(number(availablePosition?.quantity, 0)))}</strong></div><div><span>数据证据</span><strong>${escapeHtml(valueText(outcome.book_quality, "不可用"))} · ${outcome.book_age_ms === null || outcome.book_age_ms === undefined ? "age?" : `${quantity(outcome.book_age_ms, 0)}ms`}</strong></div><div><span>模型</span><strong>${postOnly ? "Maker strict / research" : "Taker L2 depth"}</strong></div>`;
}

function applyAllocation(fraction) {
  const form = $("#orderForm");
  if (!form || !state.selectedOutcome) return;
  if (state.orderSide === "SELL") {
    const position = state.data.positions.find((row) => String(row.asset_id) === String(state.selectedOutcome.asset_id));
    form.elements.amount_unit.value = "SHARES";
    form.elements.size.value = Math.max(0, number(position?.quantity, 0) * fraction).toFixed(4);
  } else {
    const available = number(object(state.data.performance).summary?.available_cash, 0);
    form.elements.amount_unit.value = "QUOTE";
    form.elements.size.value = Math.max(0, available * fraction * 0.995).toFixed(4);
  }
  updateOrderRules();
}

async function submitOrder(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const outcome = state.selectedOutcome;
  if (!outcome || !accountId() || !strategyId()) return;
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    const tif = form.elements.time_in_force.value;
    let expiresAt = null;
    if (tif === "GTD") {
      const selected = new Date(form.elements.expires_at.value);
      if (Number.isNaN(selected.getTime())) throw new Error("GTD 必须填写有效失效时间");
      expiresAt = selected.toISOString();
    }
    const payload = {
      account_id: accountId(),
      strategy_id: strategyId(),
      asset_id: String(outcome.asset_id),
      market_id: String(state.selectedMarket.market_id || state.selectedMarket.market_slug),
      side: state.orderSide,
      time_in_force: tif,
      limit_price: form.elements.limit_price.value,
      size: form.elements.size.value,
      amount_unit: form.elements.amount_unit.value,
      post_only: form.elements.post_only.checked,
      ...(expiresAt ? { expires_at: expiresAt } : {}),
    };
    const result = await api("/orders", { method: "POST", body: JSON.stringify(payload) });
    toast(`模拟订单 #${result.intent_id} 已进入 ${valueText(result.status, "队列")}`);
    await loadAccountData();
    navigate("orders");
  } catch (error) {
    toast(`${error.code || "PAPER_ORDER_ERROR"}: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function toggleWatchlist() {
  const slug = String(state.selectedMarket.market_slug);
  const enabled = !state.watchlist.has(slug);
  try {
    await api(`/retail/watchlist/${encodeURIComponent(slug)}`, { method: "PUT", body: JSON.stringify({ enabled }) });
    enabled ? state.watchlist.add(slug) : state.watchlist.delete(slug);
    renderMarketDetail();
    toast(enabled ? "已加入自选" : "已移出自选");
  } catch (error) { toast(error.message, true); }
}

async function loadAccountData() {
  if (!accountId()) return;
  const routes = {
    performance: `/accounts/${accountId()}/performance?limit=200`,
    positions: `/accounts/${accountId()}/positions?limit=200`,
    orders: `/orders?account_id=${encodeURIComponent(accountId())}&limit=200`,
    fills: `/accounts/${accountId()}/fills?limit=200`,
    ledger: `/accounts/${accountId()}/ledger?limit=200`,
    pnlAttribution: "/retail/pnl-attribution",
    accountTruth: "/retail/account-truth",
    eventRisk: "/retail/event-risk",
    eventRelations: `/retail/event-relations${state.selectedMarket?.market_slug ? `?market_slug=${encodeURIComponent(state.selectedMarket.market_slug)}` : ""}`,
    lifecycle: "/retail/lifecycle",
  };
  const entries = Object.entries(routes);
  const results = await Promise.allSettled(entries.map(([, path]) => api(path)));
  const failures = [];
  results.forEach((result, index) => {
    const key = entries[index][0];
    if (result.status === "fulfilled") state.data[key] = ["performance", "pnlAttribution", "accountTruth", "eventRisk", "eventRelations", "lifecycle"].includes(key) ? result.value : Array.isArray(result.value.items) ? result.value.items : [];
    else failures.push(`${key}: ${result.reason.message}`);
  });
  renderWallet();
  renderPortfolio();
  renderOrderResources();
  renderEventRisk();
  if (failures.length) showBanner(`部分账户数据暂不可用：${failures.join("；")}`);
}

function metric(label, main, detail = "", className = "") {
  return `<div class="metric-cell"><span>${escapeHtml(label)}</span><strong class="${className}">${escapeHtml(main)}</strong><small>${escapeHtml(detail)}</small></div>`;
}

function renderPortfolio() {
  const performance = object(state.data.performance);
  const summary = object(performance.summary);
  const professional = object(performance.professional_pnl);
  const research = object(object(professional.curves).research_mid);
  $("#portfolioMetrics").innerHTML = [
    metric("总资产", money(summary.equity), `状态 ${valueText(summary.effective_status)}`),
    metric("可用现金", money(summary.available_cash), `冻结 ${money(summary.reserved_cash)}`),
    metric("已实现 PnL", money(summary.realized_pnl), "卖出/结算确认", pnlClass(summary.realized_pnl)),
    metric("未实现 PnL", money(summary.unrealized_pnl), "当前持仓估值", pnlClass(summary.unrealized_pnl)),
    metric("累计收益", percent(research.return), `TWR ${percent(research.time_weighted_return)}`, pnlClass(research.return)),
    metric("最大回撤", percent(research.max_drawdown_pct), money(research.max_drawdown), "negative"),
  ].join("");
  renderPnlChart(professional);
  renderPositions();
  renderAttribution();
  renderRealizedPnl();
  renderAccountTruth();
  renderLifecycle();
}

const CURVES = {
  official_mark: { label: "官方标记", color: "#2b69a8" },
  research_mid: { label: "研究中间价", color: "#147d64" },
  liquidation: { label: "可平仓价值", color: "#c7473b" },
  confirmed_return: { label: "已确认收益", color: "#6c5c96" },
};

function renderPnlChart(report) {
  const points = Array.isArray(report.points) ? report.points : [];
  const available = Object.entries(CURVES).filter(([key]) => object(report.curves)[key]?.status !== "UNAVAILABLE");
  $("#curveLegend").innerHTML = Object.entries(CURVES).map(([key, config]) => `<span><i style="background:${config.color}"></i>${escapeHtml(config.label)}${object(report.curves)[key]?.status === "UNAVAILABLE" ? "（不可用）" : ""}</span>`).join("");
  const quality = object(report.quality);
  $("#pnlQuality").innerHTML = `<span>状态 ${escapeHtml(valueText(report.status, "NO_DATA"))}</span><span>点数 ${escapeHtml(valueText(quality.point_count, "0"))}</span><span>不完整 ${escapeHtml(valueText(quality.incomplete_point_count, "0"))}</span><span>未定价数量 ${escapeHtml(quantity(quality.unpriced_quantity))}</span>`;
  const canvas = $("#pnlChart");
  const empty = $("#pnlChartEmpty");
  if (!points.length || !available.length) { canvas.hidden = true; empty.hidden = false; return; }
  canvas.hidden = false;
  empty.hidden = true;
  requestAnimationFrame(() => drawPnlCanvas(canvas, points, available));
}

function drawPnlCanvas(canvas, points, available) {
  const bounds = canvas.getBoundingClientRect();
  const ratio = Math.max(1, globalThis.devicePixelRatio || 1);
  canvas.width = Math.max(1, Math.floor(bounds.width * ratio));
  canvas.height = Math.max(1, Math.floor(bounds.height * ratio));
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  const width = bounds.width;
  const height = bounds.height;
  const inset = { left: 70, right: 18, top: 15, bottom: 28 };
  const values = points.flatMap((row) => available.map(([key]) => number(object(row.values)[key])).filter(Number.isFinite));
  if (!values.length) return;
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  context.clearRect(0, 0, width, height);
  context.font = "10px Inter, system-ui, sans-serif";
  context.textBaseline = "middle";
  for (let index = 0; index <= 4; index += 1) {
    const y = inset.top + ((height - inset.top - inset.bottom) * index / 4);
    const level = max - ((max - min) * index / 4);
    context.strokeStyle = "#e4e8ed";
    context.beginPath(); context.moveTo(inset.left, y); context.lineTo(width - inset.right, y); context.stroke();
    context.fillStyle = "#66727e"; context.textAlign = "right"; context.fillText(money(level, 0), inset.left - 7, y);
  }
  available.forEach(([key, config]) => {
    context.strokeStyle = config.color;
    context.lineWidth = 2;
    context.beginPath();
    let started = false;
    points.forEach((row, index) => {
      const selected = number(object(row.values)[key]);
      if (selected === null) { started = false; return; }
      const x = inset.left + ((width - inset.left - inset.right) * index / Math.max(1, points.length - 1));
      const y = inset.top + ((max - selected) / (max - min)) * (height - inset.top - inset.bottom);
      if (!started) { context.moveTo(x, y); started = true; } else context.lineTo(x, y);
    });
    context.stroke();
  });
  context.fillStyle = "#66727e";
  context.textAlign = "left"; context.fillText(time(points[0].observed_at), inset.left, height - 10);
  context.textAlign = "right"; context.fillText(time(points.at(-1).observed_at), width - inset.right, height - 10);
}

function table(headers, rows, emptyMessage = "暂无数据") {
  if (!rows.length) return `<div class="empty-row">${escapeHtml(emptyMessage)}</div>`;
  return `<table class="data-table"><thead><tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

function renderPositions() {
  const rows = state.data.positions.filter((row) => Math.abs(number(row.quantity, 0)) > 1e-12).map((row) => {
    const mark = number(row.research_mark ?? row.conservative_mark);
    const qty = number(row.quantity, 0);
    const currentValue = mark === null ? null : mark * qty;
    const cost = number(row.cost_basis, 0);
    const unrealized = currentValue === null ? null : currentValue - cost;
    const economics = object(row.economics);
    return `<tr><td title="${escapeHtml(row.asset_id)}">${escapeHtml(valueText(row.market_title, shortId(row.asset_id, 7)))}<br><small>${escapeHtml(valueText(row.outcome_name, shortId(row.asset_id, 5)))}</small></td><td class="numeric">${quantity(qty)}</td><td class="numeric">${money(economics.fee_exclusive_basis ?? cost)}<br><small>费 ${money(economics.entry_fees_usdc, 5)}</small></td><td class="numeric">${money(economics.gross_initial_value ?? cost)}<br><small>@ ${quantity(economics.avg_gross_price, 5)}</small></td><td class="numeric">${mark === null ? "不可用" : mark.toFixed(4)}</td><td class="numeric">${money(row.liquidation_mark === null ? null : number(row.liquidation_mark) * qty)}</td><td class="numeric ${pnlClass(unrealized)}">${money(unrealized)}</td><td>${escapeHtml(valueText(economics.status, row.mark_quality))}<br><small>${escapeHtml(valueText(row.mark_quality))}</small></td></tr>`;
  });
  $("#positionTable").innerHTML = table(["市场 / Outcome", "数量", "净入场成本 / 费", "总成本 / 均价", "研究价", "可平仓价值", "未实现 PnL", "证据"], rows, "当前没有持仓");
}

function renderAttribution() {
  const perf = object(state.data.performance);
  const summary = object(perf.summary);
  const research = object(object(object(perf.professional_pnl).curves).research_mid);
  const attribution = object(state.data.pnlAttribution);
  const components = object(attribution.components);
  const pnlMetrics = object(attribution.metrics);
  const reconciliation = object(attribution.reconciliation);
  const rows = [
    ["交易已实现", money(summary.realized_pnl)],
    ["持仓未实现", money(summary.unrealized_pnl)],
    ["累计费用（已计入成本/现金）", money(components.fees ?? object(perf.current).fee_cost, 5)],
    ["结算已实现", money(components.settlement_realized_pnl)],
    ["已到账 Rebate/Reward", money(components.received_rebates_rewards, 5)],
    ["实施短缺", money(object(perf.current).implementation_shortfall, 4)],
    ["研究总收益", money(research.pnl)],
    ["时间加权收益", percent(research.time_weighted_return)],
    ["Modified Dietz", percent(research.modified_dietz_return)],
    ["外部资金流", money(object(perf.professional_pnl).cumulative_external_capital_flow)],
    ["近 1 日 / 7 日 / 30 日", `${money(object(research.periods).day?.pnl)} / ${money(object(research.periods)["7d"]?.pnl)} / ${money(object(research.periods)["30d"]?.pnl)}`],
    ["Capital-days", quantity(object(state.data.predictionReport).capital_days)],
    ["交易名义金额 / 周转率", `${money(pnlMetrics.traded_notional)} / ${percent(pnlMetrics.turnover)}`],
    ["费用占成交额", percent(pnlMetrics.fee_to_notional, 4)],
    ["已平仓胜率", `${percent(pnlMetrics.win_rate)} · ${valueText(pnlMetrics.profitable_close_event_count, 0)} / ${valueText(pnlMetrics.close_event_count, 0)}`],
    ["Gross profit / loss", `${money(pnlMetrics.gross_profit)} / ${money(pnlMetrics.gross_loss)}`],
    ["Profit factor", quantity(pnlMetrics.profit_factor)],
    ["已实现 + 已到账奖励", money(pnlMetrics.realized_plus_received_rewards)],
    ["年化 Sharpe", quantity(research.annualized_sharpe)],
    ["年化 Sortino", quantity(research.annualized_sortino)],
    ["归因完整度", `${valueText(attribution.status, "NO_DATA")} · 未定价 ${quantity(attribution.unpriced_quantity)}`],
    ["账本对账", `${valueText(reconciliation.status, "NO_DATA")} · 差额 ${money(reconciliation.delta, 8)}`],
  ];
  $("#attributionList").innerHTML = rows.map(([term, definition]) => `<div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(definition)}</dd></div>`).join("");
  const dimension = $("#attributionDimension").value;
  const grouped = Array.isArray(object(attribution.dimensions)[dimension]) ? object(attribution.dimensions)[dimension] : [];
  $("#attributionTable").innerHTML = table(["分组", "资产", "持仓", "总成本", "当前价值", "已实现", "未实现", "经济 PnL", "费用*"], grouped.map((row) => `<tr><td title="${escapeHtml(row.key)}">${escapeHtml(valueText(row.label, row.key))}</td><td class="numeric">${escapeHtml(valueText(row.asset_count, 0))}</td><td class="numeric">${escapeHtml(quantity(row.quantity))}</td><td class="numeric">${escapeHtml(money(row.gross_basis))}</td><td class="numeric">${escapeHtml(money(row.marked_value))}</td><td class="numeric ${pnlClass(row.realized_pnl)}">${escapeHtml(money(row.realized_pnl))}</td><td class="numeric ${pnlClass(row.unrealized_pnl)}">${escapeHtml(money(row.unrealized_pnl))}</td><td class="numeric ${pnlClass(row.economic_pnl)}">${escapeHtml(money(row.economic_pnl))}</td><td class="numeric">${escapeHtml(money(row.fees, 5))}</td></tr>`), "当前没有可归因的交易或持仓");
}

function renderRealizedPnl() {
  const attributionRows = Array.isArray(object(state.data.pnlAttribution).asset_rows) ? object(state.data.pnlAttribution).asset_rows : [];
  const labels = new Map(attributionRows.map((row) => [String(row.asset_id), `${valueText(row.market_title, row.market_slug)} · ${valueText(row.outcome_name)}`]));
  let cumulative = 0;
  const rows = [...state.data.ledger]
    .sort((left, right) => new Date(left.event_ts).getTime() - new Date(right.event_ts).getTime())
    .map((row) => {
      cumulative += number(row.realized_pnl_delta, 0);
      return { ...row, cumulative_realized_pnl: cumulative };
    })
    .filter((row) => ["SELL", "SETTLEMENT", "REDEEM", "SETTLEMENT_REDEEM"].includes(String(row.event_type).toUpperCase()) || number(row.realized_pnl_delta, 0) !== 0)
    .reverse();
  $("#realizedPnlTable").innerHTML = table(
    ["时间", "市场 / Outcome", "动作", "数量变化", "现金变化", "费用*", "本次已实现", "累计已实现"],
    rows.map((row) => `<tr><td>${escapeHtml(time(row.event_ts))}</td><td title="${escapeHtml(row.asset_id)}">${escapeHtml(valueText(labels.get(String(row.asset_id)), shortId(row.asset_id, 7)))}</td><td>${escapeHtml(valueText(row.event_type))}</td><td class="numeric">${escapeHtml(quantity(row.shares_delta ?? row.position_delta ?? row.quantity_delta))}</td><td class="numeric ${pnlClass(row.cash_delta)}">${escapeHtml(money(row.cash_delta))}</td><td class="numeric">${escapeHtml(money(row.fee, 5))}</td><td class="numeric ${pnlClass(row.realized_pnl_delta)}">${escapeHtml(money(row.realized_pnl_delta))}</td><td class="numeric ${pnlClass(row.cumulative_realized_pnl)}">${escapeHtml(money(row.cumulative_realized_pnl))}</td></tr>`),
    "尚无卖出、结算或 Redeem 产生的已实现 PnL；费用已包含在现金变化和已实现 PnL 中，不会二次扣减",
  );
}

function renderAccountTruth() {
  const truth = object(state.data.accountTruth);
  if (!Object.keys(truth).length) {
    $("#accountTruthPanel").innerHTML = `<div class="empty-row">账户真值尚未加载</div>`;
    return;
  }
  const snapshot = object(truth.official_snapshot);
  const mismatches = Array.isArray(truth.mismatches) ? truth.mismatches : [];
  const summary = `<div class="definition-list"><div><dt>状态</dt><dd>${escapeHtml(valueText(truth.status))}</dd></div><div><dt>绑定钱包</dt><dd>${escapeHtml(shortId(truth.account_address, 8))}</dd></div><div><dt>官方快照</dt><dd>${escapeHtml(time(snapshot.source_as_of))}</dd></div><div><dt>比较项</dt><dd>${escapeHtml(valueText(truth.comparison_item_count, 0))}</dd></div><div><dt>内部账本被覆盖</dt><dd>${truth.ledger_overwritten ? "是" : "否"}</dd></div></div>`;
  const mismatchTable = table(["类型", "字段", "官方", "Paper", "差异", "原因"], mismatches.map((row) => `<tr><td>${escapeHtml(valueText(row.mismatch_type))}</td><td>${escapeHtml(valueText(row.field_name))}</td><td class="numeric">${escapeHtml(valueText(row.official_value))}</td><td class="numeric">${escapeHtml(valueText(row.paper_value))}</td><td class="numeric">${escapeHtml(valueText(row.delta))}</td><td>${escapeHtml(valueText(row.reason))}</td></tr>`), truth.status === "NOT_LINKED" ? "连接 EVM 钱包后才有官方账户真值；Paper PnL 不受影响" : "没有已记录差异");
  $("#accountTruthPanel").innerHTML = `${summary}${mismatchTable}`;
}

function renderLifecycle() {
  const lifecycle = object(state.data.lifecycle);
  const positions = Array.isArray(lifecycle.positions) ? lifecycle.positions : [];
  $("#lifecycleTable").innerHTML = table(["市场", "Outcome", "数量", "阶段", "预计 Payout", "预计结算 PnL", "Redeem"], positions.map((row) => `<tr><td>${escapeHtml(valueText(row.market_title, row.market_slug))}</td><td>${escapeHtml(valueText(row.outcome_name))}</td><td class="numeric">${escapeHtml(quantity(row.quantity))}</td><td>${escapeHtml(valueText(row.phase, row.market_state))}</td><td class="numeric">${escapeHtml(money(row.expected_payout))}</td><td class="numeric ${pnlClass(row.expected_realized_pnl)}">${escapeHtml(money(row.expected_realized_pnl))}</td><td>${escapeHtml(valueText(row.receivable_state, row.redeemed_at ? "REDEEMED" : "PENDING"))}</td></tr>`), "当前没有待结算或已结算持仓");
}

function renderEventRisk() {
  const risk = object(state.data.eventRisk);
  const relationships = Array.isArray(risk.relationships) ? risk.relationships : [];
  const worst = object(risk.event_worst_case);
  const best = object(risk.event_best_case);
  $("#eventRiskTable").innerHTML = table(["事件", "关系", "Conditions", "最坏 PnL", "最好 PnL", "模型"], relationships.map((row) => `<tr><td title="${escapeHtml(row.event_id)}">${escapeHtml(shortId(row.event_id, 10))}</td><td>${escapeHtml(valueText(row.relationship))}</td><td class="numeric">${escapeHtml(valueText(row.condition_ids?.length, 0))}</td><td class="numeric negative">${escapeHtml(money(worst[row.event_id]))}</td><td class="numeric positive">${escapeHtml(money(best[row.event_id]))}</td><td>${escapeHtml(valueText(risk.model_version))}</td></tr>`), risk.status === "NO_POSITIONS" ? "当前没有仓位" : `事件风险不可用：${valueText(risk.status)}`);
  const graph = object(state.data.eventRelations);
  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  $("#eventRelationTable").innerHTML = edges.length ? `<div class="table-caption">关系证据 · ${escapeHtml(valueText(graph.model_version))} · 未证明 ${escapeHtml((graph.unsupported_relationships || []).join(", ") || "无")}</div>${table(["关系", "来源", "节点 A", "节点 B", "置信度", "证据 Hash"], edges.map((row) => `<tr><td>${escapeHtml(valueText(row.relationship_type))}</td><td>${escapeHtml(valueText(row.source_kind))}</td><td title="${escapeHtml(row.source_node_id)}">${escapeHtml(shortId(row.source_node_id, 10))}</td><td title="${escapeHtml(row.target_node_id)}">${escapeHtml(shortId(row.target_node_id, 10))}</td><td class="numeric">${escapeHtml(percent(row.confidence))}</td><td title="${escapeHtml(row.source_hash)}">${escapeHtml(shortId(row.source_hash, 7))}</td></tr>`))}` : "";
}

const ORDER_TERMINAL = new Set(["CANCELED", "COMPLETED", "FILLED", "REJECTED", "EXPIRED"]);
function renderOrderResources() {
  let html = "";
  if (state.orderTab === "orders") {
    html = table(["订单", "Asset", "方向", "TIF", "价格", "数量", "状态", "时间", "操作"], state.data.orders.map((row) => `<tr><td>#${escapeHtml(row.intent_id)}</td><td title="${escapeHtml(row.asset_id)}">${escapeHtml(shortId(row.asset_id, 7))}</td><td>${escapeHtml(valueText(row.side))}</td><td>${escapeHtml(valueText(row.time_in_force))}${row.post_only ? " · Maker" : ""}</td><td class="numeric">${escapeHtml(quantity(row.limit_price))}</td><td class="numeric">${escapeHtml(quantity(row.size))}</td><td>${escapeHtml(valueText(row.status))}</td><td>${escapeHtml(time(row.created_at || row.decision_ts))}</td><td><button data-order-audit="${escapeHtml(row.intent_id)}" type="button">详情</button>${ORDER_TERMINAL.has(String(row.status).toUpperCase()) ? "" : ` <button data-cancel-order="${escapeHtml(row.intent_id)}" type="button">撤单</button>`}</td></tr>`), "还没有订单");
    $("#orderSectionKicker").textContent = "订单生命周期";
    $("#orderSectionTitle").textContent = "当前与历史订单";
    $("#cancelAllButton").hidden = false;
  } else if (state.orderTab === "fills") {
    html = table(["时间", "Asset", "方向", "价格", "数量", "费用", "Finality", "证据"], state.data.fills.map((row) => `<tr><td>${escapeHtml(time(row.created_at))}</td><td title="${escapeHtml(row.asset_id)}">${escapeHtml(shortId(row.asset_id, 7))}</td><td>${escapeHtml(valueText(row.side))}</td><td class="numeric">${escapeHtml(quantity(row.price))}</td><td class="numeric">${escapeHtml(quantity(row.size ?? row.quantity))}</td><td class="numeric">${escapeHtml(money(row.fee, 5))}</td><td>${escapeHtml(valueText(row.finality_status ?? row.status))}</td><td title="${escapeHtml(row.audit_key)}">${escapeHtml(shortId(row.audit_key, 7))}</td></tr>`), "还没有成交");
    $("#orderSectionKicker").textContent = "逐笔成交";
    $("#orderSectionTitle").textContent = "价格、数量、费用与终态";
    $("#cancelAllButton").hidden = true;
  } else {
    html = table(["时间", "事件", "Asset", "现金变化", "数量变化", "费用", "已实现 PnL", "余额"], state.data.ledger.map((row) => `<tr><td>${escapeHtml(time(row.event_ts))}</td><td>${escapeHtml(valueText(row.event_type))}</td><td title="${escapeHtml(row.asset_id)}">${escapeHtml(shortId(row.asset_id, 7))}</td><td class="numeric ${pnlClass(row.cash_delta)}">${escapeHtml(money(row.cash_delta))}</td><td class="numeric">${escapeHtml(quantity(row.position_delta ?? row.quantity_delta))}</td><td class="numeric">${escapeHtml(money(row.fee, 5))}</td><td class="numeric ${pnlClass(row.realized_pnl_delta)}">${escapeHtml(money(row.realized_pnl_delta))}</td><td class="numeric">${escapeHtml(money(row.cash_after))}</td></tr>`), "还没有资金流水");
    $("#orderSectionKicker").textContent = "不可变账本";
    $("#orderSectionTitle").textContent = "每次现金、持仓与 PnL 变化";
    $("#cancelAllButton").hidden = true;
  }
  $("#orderResourceTable").innerHTML = html;
  $$('[data-cancel-order]').forEach((button) => button.addEventListener("click", () => void cancelOrder(button.dataset.cancelOrder)));
  $$('[data-order-audit]').forEach((button) => button.addEventListener("click", () => void showOrderAudit(button.dataset.orderAudit)));
}

async function showOrderAudit(orderId) {
  try {
    const audit = await api(`/orders/${encodeURIComponent(orderId)}/audit`);
    const order = object(audit.order);
    const quality = object(audit.quality);
    const timeline = Array.isArray(audit.timeline) ? audit.timeline : [];
    const mutable = ["GTC", "GTD"].includes(String(order.time_in_force).toUpperCase()) && !ORDER_TERMINAL.has(String(order.status).toUpperCase());
    const summary = [["订单状态", order.status], ["执行可信度", quality.execution_fidelity], ["模型", quality.model_version], ["校准域", quality.calibration_domain], ["盘口年龄", quality.book_age_ms === null || quality.book_age_ms === undefined ? "不可用" : `${quality.book_age_ms} ms`], ["数据质量", quality.data_quality], ["容量", quality.capacity_status]];
    const timelineHtml = table(["时间", "类型", "状态", "原因", "Checkpoint"], timeline.map((row) => `<tr><td>${escapeHtml(time(row.event_ts))}</td><td>${escapeHtml(valueText(row.kind))}</td><td>${escapeHtml(valueText(row.state))}</td><td>${escapeHtml(valueText(row.reason, "--"))}</td><td title="${escapeHtml(row.checkpoint_id)}">${escapeHtml(shortId(row.checkpoint_id, 7))}</td></tr>`), "暂无生命周期事件");
    const replace = mutable ? `<form id="replaceOrderForm" class="inline-form" data-order-id="${escapeHtml(orderId)}"><label><span>新限价</span><input name="limit_price" type="number" min="0.0001" max="0.9999" step="0.0001" value="${escapeHtml(valueText(order.limit_price))}" required></label><label><span>新总数量</span><input name="size" type="number" min="0.0001" step="0.0001" value="${escapeHtml(valueText(order.size))}" required></label><button type="submit">撤旧并重挂</button></form>` : "";
    $("#orderAuditContent").innerHTML = `<div class="definition-list">${summary.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(valueText(value))}</dd></div>`).join("")}</div>${replace}<section class="audit-section"><h3>订单与账本时间线</h3>${timelineHtml}</section><section class="audit-section"><h3>风险与成交证据</h3><pre class="audit-json">${escapeHtml(JSON.stringify({ risk: audit.risk, execution_audit: audit.execution_audit, tca: audit.tca, maker_queue: audit.maker_queue, fills: audit.fills, ledger: audit.ledger }, null, 2))}</pre></section>`;
    $("#replaceOrderForm")?.addEventListener("submit", replaceOrder);
    $("#orderAuditDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function replaceOrder(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api(`/orders/${encodeURIComponent(form.dataset.orderId)}/replace`, { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form).entries())) });
    $("#orderAuditDialog").close();
    await loadAccountData();
    toast("旧订单已撤销，新订单已重新进入队列；原排队优先级不会保留");
  } catch (error) { toast(error.message, true); }
}

async function cancelOrder(orderId) {
  try {
    await api(`/orders/${encodeURIComponent(orderId)}`, { method: "DELETE", body: "{}" });
    toast(`订单 #${orderId} 已请求撤销`);
    await loadAccountData();
  } catch (error) { toast(error.message, true); }
}

async function cancelAllOrders() {
  if (!accountId()) return;
  try {
    const result = await api("/orders/cancel-all", { method: "POST", body: JSON.stringify({ account_id: accountId() }) });
    toast(`撤单请求完成：${valueText(result.canceled_count ?? result.count, "已处理")}`);
    await loadAccountData();
  } catch (error) { toast(error.message, true); }
}

async function loadPredictions() {
  const [predictions, report] = await Promise.all([api("/retail/predictions?limit=200"), api("/retail/predictions/report")]);
  state.data.predictions = Array.isArray(predictions.items) ? predictions.items : [];
  state.data.predictionReport = report;
  renderPredictions();
}

function renderPredictionMarkets() {
  const select = $("#predictionMarket");
  const current = select.value;
  select.innerHTML = `<option value="">选择市场</option>${state.markets.map((market) => `<option value="${escapeHtml(market.market_slug)}">${escapeHtml(market.title)}</option>`).join("")}`;
  if (state.markets.some((market) => market.market_slug === current)) select.value = current;
  updatePredictionOutcomes();
}

function updatePredictionOutcomes() {
  const market = state.markets.find((row) => row.market_slug === $("#predictionMarket").value);
  $("#predictionOutcome").innerHTML = market ? market.outcomes.map((outcome) => `<option value="${escapeHtml(outcome.asset_id)}">${escapeHtml(valueText(outcome.name))}</option>`).join("") : `<option value="">先选择市场</option>`;
}

function renderAdvancedMarketOptions() {
  for (const name of ["conditional", "scenario"]) {
    const marketSelect = $(`#${name}Market`);
    const current = marketSelect.value;
    marketSelect.innerHTML = state.markets.map((market) => `<option value="${escapeHtml(market.market_slug)}">${escapeHtml(market.title)}</option>`).join("");
    if (state.markets.some((market) => market.market_slug === current)) marketSelect.value = current;
    updateAdvancedOutcomes(name);
  }
  renderAssetOperationOptions();
}

function renderAssetOperationOptions() {
  const select = $("#assetOperationMarket");
  if (!select) return;
  const current = select.value;
  const mergeCandidates = Array.isArray(object(state.data.positionOperationCandidates).merge) ? object(state.data.positionOperationCandidates).merge : [];
  const bySlug = new Map(state.markets.map((market) => [String(market.market_slug), market.title]));
  mergeCandidates.forEach((candidate) => {
    if (candidate.market_slug) bySlug.set(String(candidate.market_slug), valueText(candidate.market_title, candidate.market_slug));
  });
  select.innerHTML = [...bySlug.entries()].map(([slug, title]) => `<option value="${escapeHtml(slug)}">${escapeHtml(title)}</option>`).join("");
  if (bySlug.has(current)) select.value = current;
  const matrix = $("#assetOperationMatrix");
  const conversions = Array.isArray(object(state.data.positionOperationCandidates).neg_risk_convert) ? object(state.data.positionOperationCandidates).neg_risk_convert : [];
  matrix.innerHTML = conversions.map((row) => `<option value="${escapeHtml(row.matrix_id)}" data-max="${escapeHtml(valueText(row.max_amount, ""))}">${escapeHtml(valueText(row.source_label, row.source_outcome_id))} · 最多 ${escapeHtml(quantity(row.max_amount))}</option>`).join("");
  updateAssetOperationFields();
}

function updateAdvancedOutcomes(name) {
  const market = state.markets.find((row) => row.market_slug === $(`#${name}Market`).value);
  const outcomeSelect = $(`#${name}Outcome`);
  outcomeSelect.innerHTML = market ? market.outcomes.map((outcome) => `<option value="${escapeHtml(outcome.asset_id)}">${escapeHtml(valueText(outcome.name))}</option>`).join("") : `<option value="">没有可用 Outcome</option>`;
}

function renderPredictions() {
  const report = object(state.data.predictionReport);
  $("#predictionMetrics").innerHTML = [
    metric("已结算预测", valueText(report.observation_count, "0"), `待结算 ${valueText(report.pending_resolution_count, "0")}`),
    metric("独立事件", valueText(report.independent_event_count, "0"), "防止同事件重复放大"),
    metric("Brier", quantity(report.brier_score), "越低越好"),
    metric("Log Loss", quantity(report.log_loss), "概率惩罚"),
    metric("平均 CLV", quantity(report.mean_clv), "相对最终盘前价格"),
    metric("Capital-days", quantity(report.capital_days), "资金占用"),
  ].join("");
  const bins = Array.isArray(report.calibration_bins) ? report.calibration_bins : [];
  $("#calibrationChart").innerHTML = bins.length ? bins.map((bin) => `<div class="calibration-bin" title="预测 ${percent(bin.mean_probability)}，结果 ${percent(bin.outcome_rate)}，n=${valueText(bin.count, 0)}"><i style="height:${Math.max(2, number(bin.outcome_rate, 0) * 100)}%"></i><span>${Math.round(number(bin.mean_probability, 0) * 100)}</span></div>`).join("") : `<div class="empty-row">结算样本不足，可靠性曲线不可用</div>`;
  const categories = Array.isArray(report.by_category) ? report.by_category : Object.entries(object(report.by_category)).map(([category, metrics]) => ({ category, ...object(metrics) }));
  $("#categoryQuality").innerHTML = table(["类别", "样本", "Brier", "Log Loss", "CLV"], categories.map((row) => `<tr><td>${escapeHtml(categoryLabel(row.category))}</td><td class="numeric">${escapeHtml(valueText(row.observation_count ?? row.count, 0))}</td><td class="numeric">${escapeHtml(quantity(row.brier_score))}</td><td class="numeric">${escapeHtml(quantity(row.log_loss))}</td><td class="numeric">${escapeHtml(quantity(row.mean_clv))}</td></tr>`), "暂无已结算类别样本");
  $("#predictionTable").innerHTML = table(["时间", "市场", "Outcome", "主观概率", "市场价", "信心", "状态", "理由"], state.data.predictions.map((row) => `<tr><td>${escapeHtml(time(row.decision_ts))}</td><td>${escapeHtml(valueText(row.market_slug))}</td><td>${escapeHtml(valueText(row.outcome_name))}</td><td class="numeric">${escapeHtml(percent(row.subjective_probability))}</td><td class="numeric">${escapeHtml(percent(row.decision_market_price))}</td><td class="numeric">${escapeHtml(percent(row.confidence))}</td><td>${row.resolved_outcome === null || row.resolved_outcome === undefined ? "待结算" : `结果 ${percent(row.resolved_outcome)}`}</td><td title="${escapeHtml(row.thesis)}">${escapeHtml(valueText(row.thesis, "--").slice(0, 80))}</td></tr>`), "还没有记录预测");
}

async function submitPrediction(event) {
  event.preventDefault();
  const market = state.markets.find((row) => row.market_slug === $("#predictionMarket").value);
  const outcome = market?.outcomes?.find((row) => String(row.asset_id) === $("#predictionOutcome").value);
  if (!market || !outcome) return toast("请选择市场和 Outcome", true);
  const marketPrice = number(outcome.best_ask ?? outcome.best_bid);
  if (marketPrice === null) return toast("该 Outcome 当前没有可记录的市场价格", true);
  const payload = {
    event_id: market.event_id || market.market_id,
    market_id: market.market_id,
    market_slug: market.market_slug,
    category: market.category,
    outcome_name: outcome.name,
    subjective_probability: $("#predictionProbability").value,
    confidence: $("#predictionConfidence").value,
    thesis: $("#predictionThesis").value,
    invalidation_condition: $("#predictionInvalidation").value,
    exit_plan: $("#predictionExit").value,
    decision_market_price: String(marketPrice),
    capital_at_risk: $("#predictionCapital").value,
    visibility: $("#predictionVisibility").value,
    evidence_sources: [],
  };
  try {
    await api("/retail/predictions", { method: "POST", body: JSON.stringify(payload) });
    event.currentTarget.reset();
    $("#predictionProbability").value = "0.50";
    $("#predictionConfidence").value = "0.50";
    await loadPredictions();
    toast("预测已保存；结算前不会计算 Brier 或 Log Loss");
  } catch (error) { toast(error.message, true); }
}

async function loadRiskProfile() {
  try {
    const profile = await api("/retail/risk-profile");
    state.data.riskProfile = profile;
    const form = $("#riskForm");
    Object.entries(profile || {}).forEach(([key, value]) => {
      if (form.elements[key] && value !== null && value !== undefined) form.elements[key].value = value;
    });
  } catch (error) {
    if (error.status !== 404) throw error;
  }
}

async function saveRiskProfile(event) {
  event.preventDefault();
  const payload = Object.fromEntries([...new FormData(event.currentTarget).entries()].map(([key, value]) => [key, value === "" ? null : value]));
  try {
    state.data.riskProfile = await api("/retail/risk-profile", { method: "PUT", body: JSON.stringify(payload) });
    toast("风险设置已保存到服务端");
  } catch (error) { toast(error.message, true); }
}

async function loadNotifications() {
  const result = await api("/retail/notifications?limit=100");
  state.data.notifications = Array.isArray(result.items) ? result.items : [];
  renderNotifications();
}

function renderNotifications() {
  const unread = state.data.notifications.filter((row) => !row.read_at).length;
  const badge = $("#notificationBadge");
  badge.textContent = String(unread);
  badge.hidden = unread === 0;
  $("#notificationList").innerHTML = state.data.notifications.length ? state.data.notifications.map((row) => `<article class="notification-item${row.read_at ? " read" : ""}"><i></i><div><strong>${escapeHtml(valueText(row.title, row.notification_type))}</strong><span>${escapeHtml(valueText(row.body, row.message))}</span></div>${row.read_at ? `<time>${escapeHtml(time(row.created_at))}</time>` : `<button type="button" data-read-notification="${escapeHtml(row.notification_id)}">已读</button>`}</article>`).join("") : `<div class="empty-row">没有通知</div>`;
  $$('[data-read-notification]').forEach((button) => button.addEventListener("click", () => void markNotificationRead(button.dataset.readNotification)));
}

async function markNotificationRead(notificationId) {
  try {
    await api(`/retail/notifications/${encodeURIComponent(notificationId)}/read`, { method: "PUT", body: "{}" });
    await loadNotifications();
  } catch (error) { toast(error.message, true); }
}

async function loadTools() {
  const entries = [
    ["makerWorkbench", "/retail/maker-workbench?limit=200"],
    ["conditionalOrders", `/conditional-orders?account_id=${encodeURIComponent(accountId())}&limit=100`],
    ["positionOperations", "/retail/position-operations?limit=100"],
    ["positionOperationCandidates", "/retail/position-operations/candidates?limit=100"],
    ["scenarios", `/scenarios?account_id=${encodeURIComponent(accountId())}&limit=100`],
    ["replays", "/replays?limit=100"],
    ["dataRequests", "/retail/data-requests"],
    ["officialHistory", "/retail/official-history"],
  ];
  const results = await Promise.allSettled(entries.map(([, path]) => api(path)));
  results.forEach((result, index) => {
    if (result.status !== "fulfilled") return;
    const key = entries[index][0];
    state.data[key] = ["makerWorkbench", "positionOperationCandidates"].includes(key) ? result.value : Array.isArray(result.value.items) ? result.value.items : [];
  });
  renderTools();
}

function renderTools() {
  renderMakerWorkbench();
  renderConditionalOrders();
  renderPositionOperations();
  renderScenarios();
  renderReplays();
  renderDataRequests();
  renderOfficialHistory();
}

function updateAssetOperationFields() {
  const form = $("#assetOperationForm");
  if (!form) return;
  const type = String(form.elements.operation_type.value);
  const converting = type === "NEG_RISK_CONVERT";
  $("#assetOperationMarketField").hidden = converting;
  $("#assetOperationMatrixField").hidden = !converting;
  form.elements.market_slug.required = !converting;
  form.elements.matrix_id.required = converting;
  const conversions = Array.isArray(object(state.data.positionOperationCandidates).neg_risk_convert) ? object(state.data.positionOperationCandidates).neg_risk_convert : [];
  const button = form.querySelector('button[type="submit"]');
  button.disabled = converting && conversions.length === 0;
  if (type === "MERGE") {
    const candidate = (object(state.data.positionOperationCandidates).merge || []).find((row) => String(row.market_slug) === String(form.elements.market_slug.value));
    if (candidate?.max_amount !== undefined) form.elements.amount.max = String(candidate.max_amount);
    else form.elements.amount.removeAttribute("max");
  } else if (converting) {
    const option = form.elements.matrix_id.selectedOptions?.[0];
    if (option?.dataset.max) form.elements.amount.max = option.dataset.max;
    else form.elements.amount.removeAttribute("max");
  } else {
    form.elements.amount.removeAttribute("max");
  }
}

function renderPositionOperations() {
  renderAssetOperationOptions();
  const candidates = object(state.data.positionOperationCandidates);
  const mergeCount = Array.isArray(candidates.merge) ? candidates.merge.length : 0;
  const convertCount = Array.isArray(candidates.neg_risk_convert) ? candidates.neg_risk_convert.length : 0;
  const redeemCount = Array.isArray(candidates.redeem) ? candidates.redeem.length : 0;
  $("#assetOperationAvailability").textContent = `可合并 ${mergeCount} · 可转换 ${convertCount} · 待 finality/redeem ${redeemCount}；Redeem 由确认后的结算状态机自动入账`;
  $("#assetOperationTable").innerHTML = table(
    ["时间", "操作", "市场", "数量", "现金变化", "已实现 PnL", "状态", "Receipt"],
    state.data.positionOperations.map((row) => `<tr><td>${escapeHtml(time(row.decision_ts || row.created_at))}</td><td>${escapeHtml(valueText(row.operation_type))}</td><td>${escapeHtml(valueText(row.market_title, row.market_slug || shortId(row.condition_id, 8)))}</td><td class="numeric">${escapeHtml(quantity(row.amount))}</td><td class="numeric ${pnlClass(row.collateral_delta)}">${escapeHtml(money(row.collateral_delta))}</td><td class="numeric ${pnlClass(row.realized_pnl_delta)}">${escapeHtml(money(row.realized_pnl_delta))}</td><td>${escapeHtml(valueText(row.state))}</td><td title="${escapeHtml(row.paper_receipt_ref)}">${escapeHtml(shortId(row.paper_receipt_ref, 9))}<br><small>非链上交易</small></td></tr>`),
    "尚未执行 Paper 资产操作",
  );
}

async function createPositionOperation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  const payload = {
    operation_type: values.operation_type,
    amount: values.amount,
    ...(values.operation_type === "NEG_RISK_CONVERT" ? { matrix_id: values.matrix_id } : { market_slug: values.market_slug }),
  };
  try {
    const operation = await api("/retail/position-operations", { method: "POST", body: JSON.stringify(payload) });
    await Promise.all([loadTools(), loadAccountData()]);
    toast(`${operation.operation_type} 已完成并写入不可变 Paper 账本`);
  } catch (error) { toast(`${error.code || "PAPER_OPERATION_ERROR"}: ${error.message}`, true); }
}

function renderMakerWorkbench() {
  const workbench = object(state.data.makerWorkbench);
  const orders = Array.isArray(workbench.orders) ? workbench.orders : [];
  const rebates = Array.isArray(workbench.rebates) ? workbench.rebates : [];
  const partial = orders.filter((row) => number(row.remaining_size, 0) > 0 && number(row.remaining_size, 0) < number(row.size, 0)).length;
  const full = orders.filter((row) => number(row.remaining_size, 0) === 0 && number(row.size, 0) > 0).length;
  $("#makerAuthority").textContent = valueText(workbench.authority_model, "STRICT");
  $("#makerSummary").innerHTML = [metric("Maker 订单", valueText(orders.length, 0), "当前钱包"), metric("Partial", valueText(partial, 0), "严格证据"), metric("Full", valueText(full, 0), "严格证据"), metric("Rebate 记录", valueText(rebates.reduce((sum, row) => sum + number(row.count, 0), 0), 0), "仅 RECEIVED 进入现金")].join("");
  $("#makerTable").innerHTML = table(["订单", "方向/价格", "剩余", "外部队列估计", "成交概率", "预期成交", "置信度", "Markout", "状态"], orders.map((row) => `<tr><td>#${escapeHtml(row.intent_id)}</td><td>${escapeHtml(valueText(row.side))} @ ${escapeHtml(quantity(row.limit_price))}</td><td class="numeric">${escapeHtml(quantity(row.remaining_size))}</td><td class="numeric">${escapeHtml(quantity(row.estimated_external_queue_ahead))}</td><td class="numeric">${escapeHtml(percent(row.fill_probability))}</td><td class="numeric">${escapeHtml(quantity(row.expected_fill_size))}</td><td class="numeric">${escapeHtml(percent(row.queue_confidence))}</td><td>${escapeHtml(valueText(object(row.markouts_json)["60s"], "--"))}</td><td>${escapeHtml(valueText(row.status))}<br><small>${escapeHtml(valueText(row.queue_model_version))}</small></td></tr>`), "当前钱包没有 post-only Maker 订单");
}

function renderConditionalOrders() {
  const terminal = new Set(["TRIGGERED", "CANCELLED", "EXPIRED", "FAILED"]);
  $("#conditionalOrderTable").innerHTML = table(
    ["创建时间", "类型", "Outcome", "触发", "子订单", "状态", "操作"],
    state.data.conditionalOrders.map((row) => {
      const child = object(row.child_order);
      const trigger = row.trigger_kind === "PRICE" ? `${valueText(row.reference_price)} ${valueText(row.trigger_operator)} ${quantity(row.trigger_value)}` : valueText(row.trigger_kind);
      const group = row.group_policy && row.group_policy !== "NONE" ? `${row.group_policy} · ${shortId(row.group_id, 5)}` : row.order_type;
      const stateLabel = row.trigger_source === "WAITING_PARENT" ? "WAITING_PARENT" : row.status;
      return `<tr><td>${escapeHtml(time(row.created_at))}</td><td>${escapeHtml(valueText(group))}</td><td title="${escapeHtml(row.asset_id)}">${escapeHtml(shortId(row.asset_id, 8))}</td><td>${escapeHtml(trigger)}</td><td>${escapeHtml(valueText(child.side))} ${escapeHtml(quantity(child.size))} @ ${escapeHtml(quantity(child.limit_price))}<br><small>${escapeHtml(valueText(child.time_in_force))}${child.post_only ? " · Post-only" : ""}</small></td><td>${escapeHtml(valueText(stateLabel))}</td><td>${terminal.has(String(row.status).toUpperCase()) ? "--" : `<button type="button" data-cancel-conditional="${escapeHtml(row.conditional_order_id)}">撤销</button>`}</td></tr>`;
    }),
    "尚未创建条件单",
  );
  $$('[data-cancel-conditional]').forEach((button) => button.addEventListener("click", () => void cancelConditionalOrder(button.dataset.cancelConditional)));
}

async function createConditionalOrder(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  const postOnly = form.elements.post_only.checked;
  const orderType = String(values.order_type);
  const structure = String(values.structure || "SINGLE");
  const childOrder = (limitPrice) => ({
    asset_id: values.asset_id,
    side: values.side,
    time_in_force: values.time_in_force,
    limit_price: limitPrice,
    size: values.size,
    amount_unit: "SHARES",
    post_only: postOnly,
  });
  const priceLeg = (type, operator, triggerValue, limitPrice) => ({
    order_type: type,
    trigger: {
      kind: "PRICE",
      operator,
      value: triggerValue,
      reference_price: "MID",
      ...(type === "TRAILING_STOP" ? { trailing_percent: "0.05" } : {}),
    },
    child_order: childOrder(limitPrice),
  });
  const payload = {
    account_id: accountId(),
    strategy_id: strategyId(),
    order_type: orderType,
    ...priceLeg(orderType, values.trigger_operator, values.trigger_value, values.limit_price),
  };
  if (postOnly && !["GTC", "GTD"].includes(String(values.time_in_force))) return toast("Post-only 子订单只允许 GTC 或 GTD", true);
  try {
    if (structure === "SINGLE") {
      await api("/conditional-orders", { method: "POST", body: JSON.stringify(payload) });
    } else {
      const parentIntentId = values.parent_intent_id ? Number(values.parent_intent_id) : null;
      if (["OTO", "BRACKET"].includes(structure) && !Number.isInteger(parentIntentId)) throw new Error("OTO/Bracket 需要有效的父 Paper 订单编号");
      const orders = structure === "OTO"
        ? [{ order_type: "OTO", trigger: { kind: "PARENT_TERMINAL" }, child_order: childOrder(values.limit_price), parent_intent_id: parentIntentId }]
        : [
          { ...priceLeg(structure, values.trigger_operator, values.trigger_value, values.limit_price), ...(structure === "BRACKET" ? { parent_intent_id: parentIntentId } : {}) },
          { ...priceLeg(structure, values.second_trigger_operator, values.second_trigger_value, values.second_limit_price), ...(structure === "BRACKET" ? { parent_intent_id: parentIntentId } : {}) },
        ];
      await api("/conditional-order-groups", {
        method: "POST",
        body: JSON.stringify({ account_id: accountId(), strategy_id: strategyId(), group_policy: structure, orders }),
      });
    }
    await loadTools();
    toast(`${structure} 高级订单已创建；只有可信、无 gap 的因果事件才能触发`);
  } catch (error) { toast(error.message, true); }
}

function updateConditionalStructure() {
  const structure = String($("#conditionalStructure").value || "SINGLE");
  const hasParent = ["OTO", "BRACKET"].includes(structure);
  const hasSecondLeg = ["OCO", "BRACKET"].includes(structure);
  const parentOnly = structure === "OTO";
  $("#conditionalParentField").hidden = !hasParent;
  $("#conditionalTypeField").hidden = structure !== "SINGLE";
  $("#conditionalPrimaryOperatorField").hidden = parentOnly;
  $("#conditionalPrimaryTriggerField").hidden = parentOnly;
  $("#conditionalSecondaryOperatorField").hidden = !hasSecondLeg;
  $("#conditionalSecondaryTriggerField").hidden = !hasSecondLeg;
  $("#conditionalSecondaryLimitField").hidden = !hasSecondLeg;
  const form = $("#conditionalOrderForm");
  form.elements.parent_intent_id.required = hasParent;
  form.elements.trigger_value.required = !parentOnly;
  form.elements.second_trigger_value.required = hasSecondLeg;
  form.elements.second_limit_price.required = hasSecondLeg;
}

async function cancelConditionalOrder(orderId) {
  try {
    await api(`/conditional-orders/${encodeURIComponent(orderId)}`, { method: "DELETE", body: JSON.stringify({ reason: "retail_user_cancel" }) });
    await loadTools();
    toast("条件单已撤销");
  } catch (error) { toast(error.message, true); }
}

function renderScenarios() {
  $("#scenarioTable").innerHTML = table(
    ["创建时间", "名称", "基准可平仓 NAV", "情景 NAV", "PnL 变化", "完整性", "证据 Hash"],
    state.data.scenarios.map((row) => {
      const result = object(row.result);
      return `<tr><td>${escapeHtml(time(row.created_at))}</td><td>${escapeHtml(valueText(row.name))}</td><td class="numeric">${escapeHtml(money(result.baseline_liquidation_nav))}</td><td class="numeric">${escapeHtml(money(result.scenario_nav))}</td><td class="numeric ${pnlClass(result.scenario_pnl_delta)}">${escapeHtml(money(result.scenario_pnl_delta))}</td><td>${result.scenario_complete ? "完整" : `未定价 ${escapeHtml(valueText(result.unmarked_assets?.length, 0))}`}</td><td title="${escapeHtml(row.result_hash)}">${escapeHtml(shortId(row.result_hash, 8))}</td></tr>`;
    }),
    "尚未运行情景分析",
  );
}

async function createScenario(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  const assetId = String(values.asset_id);
  const inputs = {
    payout_by_asset: {},
    price_shock_by_asset: { [assetId]: values.price_shock || "0" },
    fee_multiplier: values.fee_multiplier || "1",
    latency_shock_ms: values.latency_shock_ms || "0",
    book_depth_haircut: values.book_depth_haircut || "0",
    market_closure_assets: [],
    feed_outage_assets: form.elements.feed_outage.checked ? [assetId] : [],
    dispute_duration_hours: "0",
  };
  try {
    await api("/scenarios", { method: "POST", body: JSON.stringify({ account_id: accountId(), strategy_id: strategyId(), name: values.name, inputs }) });
    await loadTools();
    toast("情景已计算；结果属于研究视图，不会改变现金、持仓或正式 PnL");
  } catch (error) { toast(error.message, true); }
}

function renderReplays() {
  $("#replayTable").innerHTML = table(["名称", "时间窗", "模型", "状态", "进度", "Hash", "操作"], state.data.replays.map((row) => `<tr><td>${escapeHtml(valueText(row.name))}</td><td>${escapeHtml(time(row.start_ts))}<br>${escapeHtml(time(row.end_ts))}</td><td>${escapeHtml(valueText(row.execution_model))}</td><td>${escapeHtml(valueText(row.status))}</td><td class="numeric">${escapeHtml(valueText(row.processed_event_count, 0))} / ${escapeHtml(valueText(object(row.data_snapshot).event_count, "?"))}</td><td title="${escapeHtml(row.data_hash)}">${escapeHtml(shortId(row.data_hash, 7))}</td><td><button type="button" data-resume-replay="${escapeHtml(row.replay_session_id)}">运行</button> <button type="button" data-replay-report="${escapeHtml(row.replay_session_id)}">报告</button></td></tr>`), "还没有 Replay session");
  $$('[data-resume-replay]').forEach((button) => button.addEventListener("click", () => void resumeReplay(button.dataset.resumeReplay)));
  $$('[data-replay-report]').forEach((button) => button.addEventListener("click", () => void showReplayReport(button.dataset.replayReport)));
}

async function createReplay(event) {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  payload.account_id = accountId();
  payload.strategy_id = strategyId();
  payload.strategy_version = "retail_manual_v1";
  payload.start_ts = new Date(payload.start_ts).toISOString();
  payload.end_ts = new Date(payload.end_ts).toISOString();
  payload.speed = "1";
  payload.seed = 0;
  payload.benchmark = "CONSERVATIVE_NAV";
  try {
    await api("/replays", { method: "POST", body: JSON.stringify(payload) });
    await loadTools();
    toast("Replay 已创建，数据窗口与模型版本已冻结");
  } catch (error) { toast(error.message, true); }
}

async function resumeReplay(replayId) {
  try {
    await api(`/replays/${encodeURIComponent(replayId)}/resume`, { method: "POST", body: JSON.stringify({ max_events: 5000 }) });
    await loadTools();
    toast("Replay 已推进");
  } catch (error) { toast(error.message, true); }
}

async function showReplayReport(replayId) {
  try {
    const report = await api(`/replays/${encodeURIComponent(replayId)}/report`);
    $("#orderAuditContent").innerHTML = `<section class="audit-section"><h3>Replay 报告</h3><pre class="audit-json">${escapeHtml(JSON.stringify(report, null, 2))}</pre></section>`;
    $("#orderAuditDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

function renderDataRequests() {
  $("#dataRequestTable").innerHTML = table(["申请", "类型", "状态", "时间", "SHA256", "操作"], state.data.dataRequests.map((row) => `<tr><td title="${escapeHtml(row.request_id)}">${escapeHtml(shortId(row.request_id, 8))}</td><td>${escapeHtml(valueText(row.request_type))}</td><td>${escapeHtml(valueText(row.status))}</td><td>${escapeHtml(time(row.requested_at))}</td><td title="${escapeHtml(row.artifact_sha256)}">${escapeHtml(shortId(row.artifact_sha256, 7))}</td><td>${row.request_type === "EXPORT" && row.status === "COMPLETED" ? `<button type="button" data-download-request="${escapeHtml(row.request_id)}">下载并校验</button>` : "--"}</td></tr>`), "还没有隐私数据申请");
  $$('[data-download-request]').forEach((button) => button.addEventListener("click", () => void downloadDataRequest(button.dataset.downloadRequest)));
}

async function downloadDataRequest(requestId) {
  try {
    const response = await fetch(`${state.apiBase}/retail/data-requests/${encodeURIComponent(requestId)}/download`, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error(`下载失败 HTTP ${response.status}`);
    const blob = await response.blob();
    const href = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = href;
    link.download = `paper-account-${requestId}.zip`;
    link.click();
    URL.revokeObjectURL(href);
    toast(`完整数据包已下载，SHA256 ${shortId(response.headers.get("X-Content-SHA256"), 8)}`);
  } catch (error) { toast(error.message, true); }
}

function renderOfficialHistory() {
  $("#officialHistoryTable").innerHTML = table(["官方钱包历史", "窗口", "状态", "影子策略", "结果 Hash"], state.data.officialHistory.map((row) => `<tr><td>${escapeHtml(shortId(row.wallet_address, 8))}</td><td>${escapeHtml(time(row.start_ts))}<br>${escapeHtml(time(row.end_ts))}</td><td>${escapeHtml(valueText(row.status))}</td><td title="${escapeHtml(row.shadow_strategy_id)}">${escapeHtml(shortId(row.shadow_strategy_id, 8))}</td><td title="${escapeHtml(row.result_sha256)}">${escapeHtml(shortId(row.result_sha256, 7))}</td></tr>`), "尚未发起官方钱包历史只读同步");
}

async function requestOfficialHistory() {
  const provider = String(object(object(state.session).user).provider || object(state.session).provider || "");
  if (provider !== "EVM") {
    toast("请先使用 Rabby / EVM 钱包签名登录；访客身份没有可验证的官方钱包", true);
    return;
  }
  try {
    await api("/retail/official-history", {
      method: "POST",
      body: JSON.stringify({ window_start: "2020-01-01T00:00:00Z", window_end: new Date().toISOString(), compare_to_paper: true }),
    });
    await loadTools();
    toast("官方历史只读同步已排队；结果写入隔离 shadow，不覆盖 Paper 账本");
  } catch (error) { toast(error.message, true); }
}

async function exportAccountResource(resource) {
  try {
    const response = await fetch(`${state.apiBase}/accounts/${encodeURIComponent(accountId())}/export?resource=${encodeURIComponent(resource)}&format=csv`, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error(`导出失败 HTTP ${response.status}`);
    const blob = await response.blob();
    const href = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = href;
    link.download = `paper-${resource}.csv`;
    link.click();
    URL.revokeObjectURL(href);
    toast(`${resource} 已导出，SHA256 ${shortId(response.headers.get("X-Content-SHA256"), 8)}`);
  } catch (error) { toast(error.message, true); }
}

async function createDataRequest(requestType) {
  const reason = requestType === "DELETE" ? "User requested account data deletion review" : "User requested complete account export";
  try {
    await api("/retail/data-requests", { method: "POST", body: JSON.stringify({ request_type: requestType, reason }) });
    await loadTools();
    toast(requestType === "DELETE" ? "删除申请已登记，数据不会在未审核时直接销毁" : "完整导出申请已登记");
  } catch (error) { toast(error.message, true); }
}

async function loadLeaderboard() {
  const [result, following, competitions] = await Promise.all([api(`/retail/leaderboard?metric=${encodeURIComponent(state.leaderboardMetric)}&limit=100`), api("/retail/following"), api("/retail/competitions")]);
  state.data.leaderboard = Array.isArray(result.items) ? result.items : [];
  state.data.following = new Set(Array.isArray(following.virtual_wallet_ids) ? following.virtual_wallet_ids.map(String) : []);
  state.data.competitions = Array.isArray(competitions.items) ? competitions.items : [];
  renderLeaderboard();
  renderCompetitions();
}

function renderLeaderboard() {
  const metricField = { return: "last_public_return", drawdown: "last_public_drawdown_pct", brier: "last_brier", execution: "last_execution_score" }[state.leaderboardMetric];
  const metricFormat = state.leaderboardMetric === "return" || state.leaderboardMetric === "drawdown" ? percent : quantity;
  $("#leaderboardTable").innerHTML = table(["排名", "模拟组合", "总资产", "本榜指标", "关注者", "数据时间", "操作"], state.data.leaderboard.map((row, index) => `<tr><td>${index + 1}</td><td>${escapeHtml(valueText(row.display_name))}<br><small>${escapeHtml(shortId(row.virtual_wallet_id, 7))}</small></td><td class="numeric">${escapeHtml(money(row.last_public_nav))}</td><td class="numeric">${escapeHtml(metricFormat(row[metricField]))}</td><td class="numeric">${escapeHtml(valueText(row.follower_count, 0))}</td><td>${escapeHtml(time(row.metrics_as_of))}</td><td><button type="button" data-follow-wallet="${escapeHtml(row.virtual_wallet_id)}">${state.data.following.has(String(row.virtual_wallet_id)) ? "取消关注" : "关注"}</button> <button type="button" data-public-portfolio="${escapeHtml(row.virtual_wallet_id)}">组合</button> <button type="button" data-public-journal="${escapeHtml(row.virtual_wallet_id)}">日记</button></td></tr>`), "当前没有选择公开且有可验证指标的组合");
  $$('[data-follow-wallet]').forEach((button) => button.addEventListener("click", () => void toggleFollow(button.dataset.followWallet)));
  $$('[data-public-portfolio]').forEach((button) => button.addEventListener("click", () => void showPublicPortfolio(button.dataset.publicPortfolio)));
  $$('[data-public-journal]').forEach((button) => button.addEventListener("click", () => void showPublicJournal(button.dataset.publicJournal)));
}

async function showPublicPortfolio(virtualWalletId) {
  try {
    const result = await api(`/retail/portfolios/${encodeURIComponent(virtualWalletId)}?limit=100`);
    const nav = object(result.nav);
    const positions = Array.isArray(result.positions) ? result.positions : [];
    const activity = Array.isArray(result.activity) ? result.activity : [];
    const summary = `<div class="definition-list"><div><dt>组合</dt><dd>${escapeHtml(valueText(result.display_name))}</dd></div><div><dt>披露截止</dt><dd>${escapeHtml(time(result.as_of))}</dd></div><div><dt>披露模式</dt><dd>${escapeHtml(valueText(result.visibility))} · ${escapeHtml(valueText(result.disclosure_delay_minutes, 0))} 分钟</dd></div><div><dt>总资产</dt><dd>${escapeHtml(money(nav.equity))}</dd></div><div><dt>累计 PnL</dt><dd class="${pnlClass(nav.total_pnl)}">${escapeHtml(money(nav.total_pnl))}</dd></div><div><dt>回撤</dt><dd>${escapeHtml(percent(nav.drawdown_pct))}</dd></div></div>`;
    const positionTable = table(["市场 / Outcome", "数量", "成本", "数据时间"], positions.map((row) => `<tr><td>${escapeHtml(valueText(row.market_title, row.market_slug))}<br><small>${escapeHtml(valueText(row.outcome_name))}</small></td><td class="numeric">${escapeHtml(quantity(row.quantity))}</td><td class="numeric">${escapeHtml(money(row.cost_basis))}</td><td>${escapeHtml(time(row.event_ts))}</td></tr>`), "披露截止时间前没有持仓");
    const activityTable = table(["时间", "市场 / Outcome", "动作", "数量", "现金", "已实现 PnL"], activity.map((row) => `<tr><td>${escapeHtml(time(row.event_ts))}</td><td>${escapeHtml(valueText(row.market_title, row.market_slug))}<br><small>${escapeHtml(valueText(row.outcome_name))}</small></td><td>${escapeHtml(valueText(row.event_type))}</td><td class="numeric">${escapeHtml(quantity(row.shares_delta))}</td><td class="numeric ${pnlClass(row.cash_delta)}">${escapeHtml(money(row.cash_delta))}</td><td class="numeric ${pnlClass(row.realized_pnl_delta)}">${escapeHtml(money(row.realized_pnl_delta))}</td></tr>`), "披露截止时间前没有交易动态");
    $("#orderAuditContent").innerHTML = `${summary}<section class="audit-section"><h3>披露持仓</h3>${positionTable}</section><section class="audit-section"><h3>交易动态</h3>${activityTable}</section>`;
    $("#orderAuditDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function showPublicJournal(virtualWalletId) {
  try {
    const result = await api(`/retail/portfolios/${encodeURIComponent(virtualWalletId)}/journal?limit=100`);
    const rows = Array.isArray(result.items) ? result.items : [];
    $("#orderAuditContent").innerHTML = `<section class="audit-section"><h3>${escapeHtml(valueText(result.display_name))} · 预测日记</h3>${table(["时间", "市场", "Outcome", "概率", "状态", "理由"], rows.map((row) => `<tr><td>${escapeHtml(time(row.decision_ts))}</td><td>${escapeHtml(valueText(row.market_slug))}</td><td>${escapeHtml(valueText(row.outcome_name))}</td><td class="numeric">${escapeHtml(percent(row.subjective_probability))}</td><td>${row.resolved_outcome === null || row.resolved_outcome === undefined ? "待结算" : `结果 ${percent(row.resolved_outcome)}`}</td><td>${escapeHtml(valueText(row.thesis, "--"))}</td></tr>`), "没有达到公开条件的日记")}</section>`;
    $("#orderAuditDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

async function toggleFollow(virtualWalletId) {
  const enabled = !state.data.following.has(String(virtualWalletId));
  try {
    await api(`/retail/following/${encodeURIComponent(virtualWalletId)}`, { method: "PUT", body: JSON.stringify({ enabled }) });
    await loadLeaderboard();
  } catch (error) { toast(error.message, true); }
}

function renderCompetitions() {
  $("#competitionTable").innerHTML = table(["比赛", "时间", "初始资金", "状态", "我的收益", "规则", "操作"], state.data.competitions.map((row) => `<tr><td>${escapeHtml(valueText(row.name))}</td><td>${escapeHtml(time(row.starts_at))}<br>${escapeHtml(time(row.ends_at))}</td><td class="numeric">${escapeHtml(money(row.initial_balance))}</td><td>${escapeHtml(valueText(row.effective_status, row.status))}</td><td class="numeric ${pnlClass(row.server_return)}">${escapeHtml(percent(row.server_return))}</td><td title="${escapeHtml(row.rules_hash)}">${escapeHtml(shortId(row.rules_hash, 7))}</td><td>${row.virtual_wallet_id ? `<button type="button" data-select-wallet="${escapeHtml(row.virtual_wallet_id)}">进入账户</button>` : `<button type="button" data-join-competition="${escapeHtml(row.competition_id)}">加入</button>`} <button type="button" data-competition-standings="${escapeHtml(row.competition_id)}">排名</button></td></tr>`), "当前没有开放的模拟比赛");
  $$('[data-select-wallet]', $("#competitionTable")).forEach((button) => button.addEventListener("click", () => void selectWallet(button.dataset.selectWallet)));
  $$('[data-join-competition]').forEach((button) => button.addEventListener("click", () => void joinCompetition(button.dataset.joinCompetition)));
  $$('[data-competition-standings]').forEach((button) => button.addEventListener("click", () => void loadCompetitionStandings(button.dataset.competitionStandings)));
  renderCompetitionStandings();
}

async function loadCompetitionStandings(competitionId) {
  try {
    state.data.competitionStandings = await api(`/retail/competitions/${encodeURIComponent(competitionId)}/standings?metric=${encodeURIComponent(state.leaderboardMetric)}`);
    renderCompetitionStandings();
  } catch (error) { toast(error.message, true); }
}

function renderCompetitionStandings() {
  const result = object(state.data.competitionStandings);
  const rows = Array.isArray(result.items) ? result.items : [];
  const node = $("#competitionStandingsTable");
  if (!rows.length) { node.innerHTML = ""; return; }
  node.innerHTML = `<div class="table-caption">${escapeHtml(valueText(result.name))} · ${escapeHtml(valueText(result.metric))} · 截止 ${escapeHtml(time(result.score_cutoff))}</div>${table(["排名", "参与者", "收益", "回撤", "Brier", "执行质量", "证据时间"], rows.map((row) => { const score = object(row.score); return `<tr><td>${escapeHtml(row.rank)}</td><td>${row.is_current_user ? "我的账户 · " : ""}${escapeHtml(valueText(row.display_name, row.participant_id))}</td><td class="numeric ${pnlClass(score.return)}">${escapeHtml(percent(score.return))}</td><td class="numeric">${escapeHtml(percent(score.drawdown_pct))}</td><td class="numeric">${escapeHtml(quantity(score.brier))}</td><td class="numeric">${escapeHtml(quantity(score.execution))}</td><td>${escapeHtml(time(score.nav_as_of || result.score_cutoff))}</td></tr>`; }), "暂无参赛成绩")}`;
}

async function createCompetition(event) {
  event.preventDefault();
  const formData = new FormData(event.currentTarget);
  const payload = Object.fromEntries(formData.entries());
  payload.starts_at = new Date(payload.starts_at).toISOString();
  payload.ends_at = new Date(payload.ends_at).toISOString();
  const categories = formData.getAll("competition_category").map(String);
  delete payload.competition_category;
  payload.market_scope = categories.length ? { categories } : {};
  try {
    await api("/retail/competitions", { method: "POST", body: JSON.stringify(payload) });
    $("#competitionDialog").close();
    await loadLeaderboard();
    toast("比赛已创建；参赛账户将使用独立初始资金和服务端时间");
  } catch (error) { toast(error.message, true); }
}

async function joinCompetition(competitionId) {
  try {
    await api(`/retail/competitions/${encodeURIComponent(competitionId)}/join`, { method: "POST", body: "{}" });
    await loadWallets();
    await loadLeaderboard();
    toast("已创建独立比赛钱包，历史订单不会回填到比赛成绩");
  } catch (error) { toast(error.message, true); }
}

async function saveVisibility(event) {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  try {
    await api("/retail/portfolio", { method: "PUT", body: JSON.stringify(payload) });
    await api("/retail/portfolio/refresh-metrics", { method: "POST", body: "{}" });
    $("#visibilityDialog").close();
    await loadLeaderboard();
    toast("组合公开设置已保存");
  } catch (error) { toast(error.message, true); }
}

async function loadWorkspace() {
  showBanner("");
  await loadPreferences().catch((error) => showBanner(`偏好设置未加载：${error.message}`));
  const tasks = [
    loadWallets(),
    loadWatchlist(),
    loadMarkets(),
    loadAccountData(),
    loadPredictions(),
    loadRiskProfile(),
    loadNotifications(),
    loadLeaderboard(),
    loadTools(),
  ];
  const results = await Promise.allSettled(tasks);
  const failures = results.filter((result) => result.status === "rejected").map((result) => result.reason.message);
  if (failures.length) showBanner(`部分模块未就绪：${failures.join("；")}`);
}

async function loadPreferences() {
  state.data.preferences = await api("/retail/preferences");
  applyPreferences();
}

function applyPreferences() {
  const preferences = object(state.data.preferences);
  document.documentElement.lang = preferences.locale || "zh-CN";
  document.documentElement.classList.toggle("reduce-motion", Boolean(preferences.reduce_motion));
  document.documentElement.classList.toggle("high-contrast", Boolean(preferences.high_contrast));
  const form = $("#preferencesForm");
  if (!form) return;
  form.elements.locale.value = preferences.locale || "zh-CN";
  form.elements.timezone_name.value = preferences.timezone_name || "Asia/Shanghai";
  form.elements.reduce_motion.checked = Boolean(preferences.reduce_motion);
  form.elements.high_contrast.checked = Boolean(preferences.high_contrast);
}

async function savePreferences(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    state.data.preferences = await api("/retail/preferences", {
      method: "PUT",
      body: JSON.stringify({
        locale: form.elements.locale.value,
        timezone_name: form.elements.timezone_name.value,
        reduce_motion: form.elements.reduce_motion.checked,
        high_contrast: form.elements.high_contrast.checked,
      }),
    });
    applyPreferences();
    $("#preferencesDialog").close();
    await loadWorkspace();
    toast("显示、时区和无障碍偏好已保存");
  } catch (error) { toast(error.message, true); }
}

const VIEW_LABELS = {
  markets: ["Market discovery", "市场"],
  portfolio: ["Account economics", "资产与 PnL"],
  orders: ["Order lifecycle", "订单与成交"],
  predictions: ["Forecast calibration", "预测复盘"],
  risk: ["Risk admission", "风险设置"],
  tools: ["Research and evidence", "研究工具"],
  leaderboard: ["Public portfolios", "排行榜"],
  notifications: ["Account events", "通知"],
};

function navigate(view) {
  if (!VIEW_LABELS[view]) return;
  if (view === "markets") $(".market-workspace")?.classList.remove("detail-open");
  $$('[data-view]').forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $$('[data-view-panel]').forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== view; });
  $("#pageKicker").textContent = VIEW_LABELS[view][0];
  $("#pageTitle").textContent = VIEW_LABELS[view][1];
  history.replaceState(null, "", `${location.pathname}${location.search}#${view}`);
  if (view === "portfolio") requestAnimationFrame(renderPortfolio);
  if (view === "tools") void loadTools().catch((error) => toast(error.message, true));
}

function bindEvents() {
  $$('[data-view]').forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
  $("#guestLoginButton").addEventListener("click", guestLogin);
  $("#walletLoginButton").addEventListener("click", walletLogin);
  $("#logoutButton").addEventListener("click", logout);
  $("#forkWalletForm").addEventListener("submit", forkWallet);
  $("#resetWalletButton").addEventListener("click", resetWallet);
  $("#walletMenuButton").addEventListener("click", () => state.session ? $("#walletDialog").showModal() : $("#loginDialog").showModal());
  $("#preferencesButton").addEventListener("click", () => { $("#walletDialog").close(); $("#preferencesDialog").showModal(); });
  $("#fidelityButton").addEventListener("click", () => $("#fidelityDialog").showModal());
  $("#portfolioVisibilityButton").addEventListener("click", () => $("#visibilityDialog").showModal());
  $$('[data-close-modal]').forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  let searchTimer;
  $("#marketSearch").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => void loadMarkets().catch((error) => toast(error.message, true)), 250); });
  $("#refreshMarketsButton").addEventListener("click", () => void loadMarkets().catch((error) => toast(error.message, true)));
  $$('[data-category]').forEach((button) => button.addEventListener("click", () => {
    $$('[data-category]').forEach((item) => item.classList.toggle("active", item === button));
    void loadMarkets().catch((error) => toast(error.message, true));
  }));
  $$('[data-collection]').forEach((button) => button.addEventListener("click", () => {
    state.marketCollection = button.dataset.collection;
    $$('[data-collection]').forEach((item) => item.classList.toggle("active", item === button));
    void loadMarkets().catch((error) => toast(error.message, true));
  }));
  $$('[data-order-tab]').forEach((button) => button.addEventListener("click", () => {
    state.orderTab = button.dataset.orderTab;
    $$('[data-order-tab]').forEach((item) => item.classList.toggle("active", item === button));
    renderOrderResources();
  }));
  $("#cancelAllButton").addEventListener("click", cancelAllOrders);
  $("#predictionMarket").addEventListener("change", updatePredictionOutcomes);
  $("#predictionForm").addEventListener("submit", submitPrediction);
  $("#conditionalMarket").addEventListener("change", () => updateAdvancedOutcomes("conditional"));
  $("#scenarioMarket").addEventListener("change", () => updateAdvancedOutcomes("scenario"));
  $("#conditionalOrderForm").addEventListener("submit", createConditionalOrder);
  $("#conditionalStructure").addEventListener("change", updateConditionalStructure);
  $("#assetOperationForm").addEventListener("submit", createPositionOperation);
  $("#assetOperationType").addEventListener("change", updateAssetOperationFields);
  $("#assetOperationMarket").addEventListener("change", updateAssetOperationFields);
  $("#assetOperationMatrix").addEventListener("change", updateAssetOperationFields);
  $("#scenarioForm").addEventListener("submit", createScenario);
  $("#riskForm").addEventListener("submit", saveRiskProfile);
  $("#attributionDimension").addEventListener("change", renderAttribution);
  $("#refreshAccountTruthButton").addEventListener("click", () => void loadAccountData().catch((error) => toast(error.message, true)));
  $("#refreshNotificationsButton").addEventListener("click", () => void loadNotifications().catch((error) => toast(error.message, true)));
  $$('[data-leaderboard-metric]').forEach((button) => button.addEventListener("click", () => {
    state.leaderboardMetric = button.dataset.leaderboardMetric;
    $$('[data-leaderboard-metric]').forEach((item) => item.classList.toggle("active", item === button));
    void loadLeaderboard().catch((error) => toast(error.message, true));
  }));
  $("#visibilityForm").addEventListener("submit", saveVisibility);
  $("#preferencesForm").addEventListener("submit", savePreferences);
  $$('[data-tools-tab]').forEach((button) => button.addEventListener("click", () => {
    state.toolsTab = button.dataset.toolsTab;
    $$('[data-tools-tab]').forEach((item) => item.classList.toggle("active", item === button));
    $$('[data-tools-panel]').forEach((panel) => { panel.hidden = panel.dataset.toolsPanel !== state.toolsTab; });
  }));
  $("#replayForm").addEventListener("submit", createReplay);
  $("#refreshReplaysButton").addEventListener("click", () => void loadTools().catch((error) => toast(error.message, true)));
  $$('[data-export-resource]').forEach((button) => button.addEventListener("click", () => void exportAccountResource(button.dataset.exportResource)));
  $("#requestDataExportButton").addEventListener("click", () => void createDataRequest("EXPORT"));
  $("#requestDataDeleteButton").addEventListener("click", () => void createDataRequest("DELETE"));
  $("#requestOfficialHistoryButton").addEventListener("click", () => void requestOfficialHistory());
  $("#createCompetitionButton").addEventListener("click", () => $("#competitionDialog").showModal());
  $("#competitionForm").addEventListener("submit", createCompetition);
  globalThis.addEventListener("resize", () => {
    if (!$("#portfolioView").hidden) renderPortfolio();
  });
}

async function boot() {
  bindEvents();
  updateConditionalStructure();
  const replayEnd = new Date();
  const replayStart = new Date(replayEnd.getTime() - 60 * 60 * 1000);
  $("#replayForm").elements.start_ts.value = replayStart.toISOString().slice(0, 16);
  $("#replayForm").elements.end_ts.value = replayEnd.toISOString().slice(0, 16);
  const competitionStart = new Date(Date.now() + 15 * 60 * 1000);
  const competitionEnd = new Date(competitionStart.getTime() + 7 * 24 * 60 * 60 * 1000);
  $("#competitionForm").elements.starts_at.value = competitionStart.toISOString().slice(0, 16);
  $("#competitionForm").elements.ends_at.value = competitionEnd.toISOString().slice(0, 16);
  navigate(location.hash.slice(1) in VIEW_LABELS ? location.hash.slice(1) : "markets");
  try {
    await refreshSession();
    if (wallet().virtual_wallet_id) await loadWorkspace();
    else $("#loginDialog").showModal();
  } catch (error) {
    if (error.status === 401) $("#loginDialog").showModal();
    else {
      showBanner(`模拟盘 API 暂不可用：${error.message}`);
      $("#loginDialog").showModal();
    }
  }
}

void boot();
