"use strict";

const state = {
  exchanges: [],
  orders: [],
  fills: [],
  currentPositions: [],
  orderCursor: null,
  fillCursor: null,
  activeTab: "orders",
  selectedOrderId: null,
  control: {
    kill_switch: false,
    close_only: false,
    reason: null,
  },
};

const elements = {
  modeBadge: document.querySelector("#mode-badge"),
  controlReason: document.querySelector("#control-reason"),
  controlToggle: document.querySelector("#close-only-toggle"),
  controlMessage: document.querySelector("#control-message"),
  orderForm: document.querySelector("#order-form"),
  orderExchange: document.querySelector("#order-exchange"),
  orderSymbol: document.querySelector("#order-symbol"),
  orderSide: document.querySelector("#order-side"),
  orderType: document.querySelector("#order-type"),
  orderQuantity: document.querySelector("#order-quantity"),
  orderLimitPrice: document.querySelector("#order-limit-price"),
  orderReduceOnly: document.querySelector("#order-reduce-only"),
  orderSubmit: document.querySelector("#order-submit"),
  orderMessage: document.querySelector("#order-message"),
  form: document.querySelector("#filter-form"),
  from: document.querySelector("#from-date"),
  to: document.querySelector("#to-date"),
  exchange: document.querySelector("#exchange-id"),
  symbol: document.querySelector("#symbol"),
  strategy: document.querySelector("#strategy-id"),
  side: document.querySelector("#side"),
  status: document.querySelector("#status"),
  submit: document.querySelector("#filter-form .primary-button"),
  quickRanges: [...document.querySelectorAll("[data-range]")],
  error: document.querySelector("#error-banner"),
  updated: document.querySelector("#last-updated"),
  currentRefresh: document.querySelector("#current-refresh"),
  currentCloseAll: document.querySelector("#close-all"),
  currentTotal: document.querySelector("#unrealized-total"),
  currentUpdated: document.querySelector("#current-updated"),
  currentError: document.querySelector("#current-error"),
  currentBody: document.querySelector("#current-positions-body"),
  currentEmpty: document.querySelector("#current-positions-empty"),
  ordersTab: document.querySelector("#orders-tab"),
  fillsTab: document.querySelector("#fills-tab"),
  ordersPanel: document.querySelector("#orders-panel"),
  fillsPanel: document.querySelector("#fills-panel"),
  ordersBody: document.querySelector("#orders-body"),
  fillsBody: document.querySelector("#fills-body"),
  ordersEmpty: document.querySelector("#orders-empty"),
  fillsEmpty: document.querySelector("#fills-empty"),
  ordersMore: document.querySelector("#orders-more"),
  fillsMore: document.querySelector("#fills-more"),
  dialog: document.querySelector("#order-dialog"),
  dialogClose: document.querySelector("#dialog-close"),
  orderCancel: document.querySelector("#order-cancel"),
  orderDetail: document.querySelector("#order-detail"),
  orderFills: document.querySelector("#order-fills"),
};

const statusLabels = {
  pending: "受付済み",
  processing: "処理中",
  open: "未約定",
  partially_filled: "一部約定",
  filled: "約定済み",
  canceled: "取消済み",
  rejected: "拒否",
  failed: "失敗",
};
const cancellableStatuses = new Set(["pending", "processing", "open", "partially_filled"]);

function requestId(prefix) {
  const identity = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${identity}`;
}

function utcDateString(offsetDays = 0) {
  const value = new Date();
  value.setUTCHours(0, 0, 0, 0);
  value.setUTCDate(value.getUTCDate() + offsetDays);
  return value.toISOString().slice(0, 10);
}

function setQuickRange(range) {
  elements.quickRanges.forEach((button) => button.classList.toggle("active", button.dataset.range === String(range)));
  if (range === "all") {
    elements.from.value = "";
    elements.to.value = "";
    return;
  }
  const days = Number(range);
  elements.to.value = utcDateString(0);
  elements.from.value = utcDateString(-(days - 1));
}

function commonParams() {
  const params = new URLSearchParams({ limit: "100" });
  if (elements.from.value) params.set("from", elements.from.value);
  if (elements.to.value) params.set("to", elements.to.value);
  if (elements.exchange.value) params.set("exchange_id", elements.exchange.value);
  if (elements.symbol.value.trim()) params.set("symbol", elements.symbol.value.trim());
  if (elements.side.value) params.set("side", elements.side.value);
  return params;
}

function errorDetail(payload, fallback) {
  if (typeof payload?.detail === "string") return payload.detail;
  if (Array.isArray(payload?.detail)) {
    return payload.detail.map((item) => item.msg || "入力内容が不正です").join(" / ");
  }
  return fallback;
}

async function fetchJson(url, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(url, {
    ...options,
    headers,
    cache: "no-store",
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (_error) {
    payload = null;
  }
  if (!response.ok) {
    throw new Error(errorDetail(payload, `処理に失敗しました (${response.status})`));
  }
  return payload;
}

function postJson(url, payload = undefined) {
  const options = { method: "POST" };
  if (payload !== undefined) options.body = JSON.stringify(payload);
  return fetchJson(url, options);
}

function showError(message) {
  elements.error.textContent = message;
  elements.error.hidden = false;
}

function clearError() {
  elements.error.hidden = true;
  elements.error.textContent = "";
}

function showOperation(element, message, kind = "success") {
  element.textContent = message;
  element.className = `operation-message ${kind}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[character]);
}

function formatNumber(value, maximumFractionDigits = 8) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return new Intl.NumberFormat("ja-JP", {
    maximumFractionDigits,
    minimumFractionDigits: 0,
  }).format(number);
}

function formatSigned(value) {
  const number = Number(value);
  const prefix = number > 0 ? "+" : "";
  return `${prefix}${formatNumber(value)}`;
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${new Intl.DateTimeFormat("ja-JP", {
    timeZone: "UTC",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date)} UTC`;
}

function sideLabel(side) {
  if (side === "buy") return "買い";
  if (side === "sell") return "売り";
  return "—";
}

function modeLabel() {
  if (state.control.kill_switch) return "KILL SWITCH";
  if (state.control.close_only) return "CLOSE-ONLY";
  return "NORMAL";
}

function renderControl() {
  const mode = state.control.kill_switch ? "halted" : state.control.close_only ? "close-only" : "normal";
  elements.modeBadge.className = `mode-badge ${mode}`;
  elements.modeBadge.innerHTML = `<span aria-hidden="true"></span>${modeLabel()}`;
  elements.controlToggle.textContent = state.control.close_only
    ? "Close-onlyを解除"
    : "Close-onlyを有効化";
  elements.controlToggle.disabled = state.control.kill_switch;
  elements.controlReason.value = state.control.reason || "";
  elements.orderSubmit.disabled = state.control.kill_switch;
  elements.orderReduceOnly.checked = state.control.close_only || elements.orderReduceOnly.checked;
  elements.orderReduceOnly.disabled = state.control.close_only || state.control.kill_switch;
  elements.currentCloseAll.disabled = state.control.kill_switch || state.currentPositions.length === 0;
  if (state.control.kill_switch) {
    showOperation(elements.controlMessage, `Kill Switchにより全注文が停止しています。${state.control.reason || ""}`, "error");
  } else if (state.control.close_only) {
    showOperation(elements.controlMessage, `Close-only中です。建玉を増やす注文は拒否されます。${state.control.reason || ""}`, "warning");
  } else {
    showOperation(elements.controlMessage, "通常モードです。", "success");
  }
}

async function loadControl() {
  state.control = await fetchJson("/ui-api/trading-control");
  renderControl();
}

async function setCloseOnly() {
  if (state.control.kill_switch) return;
  const enabled = !state.control.close_only;
  const action = enabled ? "有効化" : "解除";
  const warning = enabled
    ? "Close-onlyを有効化します。未完了の通常注文には取消要求が設定されます。"
    : "Close-onlyを解除し、新規建玉を許可します。";
  if (!window.confirm(`${warning}\nよろしいですか？`)) return;
  elements.controlToggle.disabled = true;
  try {
    state.control = await postJson("/ui-api/close-only", {
      enabled,
      reason: elements.controlReason.value.trim() || null,
    });
    renderControl();
    const count = Number(state.control.cancellation_requested_count || 0);
    showOperation(
      elements.controlMessage,
      `Close-onlyを${action}しました。${enabled ? `取消要求: ${count}件` : ""}`,
      enabled ? "warning" : "success",
    );
    await loadOrders(false);
  } catch (error) {
    showOperation(elements.controlMessage, error.message, "error");
  } finally {
    elements.controlToggle.disabled = state.control.kill_switch;
  }
}

function updateOrderSymbols() {
  const selected = state.exchanges.find((item) => item.exchange_id === elements.orderExchange.value);
  const symbols = selected?.symbols || [];
  elements.orderSymbol.innerHTML = symbols.length
    ? symbols.map((symbol) => `<option value="${escapeHtml(symbol)}">${escapeHtml(symbol)}</option>`).join("")
    : '<option value="">取引所を先に選択</option>';
  elements.orderSymbol.disabled = symbols.length === 0;
}

async function loadExchanges() {
  const payload = await fetchJson("/ui-api/exchanges");
  state.exchanges = Array.isArray(payload) ? payload : [];
  elements.exchange.innerHTML = [
    '<option value="">すべて</option>',
    ...state.exchanges.map((item) => (
      `<option value="${escapeHtml(item.exchange_id)}">${escapeHtml(item.exchange_id)}</option>`
    )),
  ].join("");
  elements.orderExchange.innerHTML = [
    '<option value="">選択してください</option>',
    ...state.exchanges.map((item) => (
      `<option value="${escapeHtml(item.exchange_id)}">${escapeHtml(item.exchange_id)}</option>`
    )),
  ].join("");
  updateOrderSymbols();
}

function validateRange() {
  if (elements.from.value && elements.to.value && elements.from.value > elements.to.value) {
    throw new Error("開始日は終了日以前にしてください。");
  }
}

function setLoading(loading) {
  elements.submit.disabled = loading;
  elements.submit.textContent = loading ? "読み込み中…" : "表示を更新";
}

async function loadAll() {
  clearError();
  try {
    validateRange();
    setLoading(true);
    state.orderCursor = null;
    state.fillCursor = null;
    await Promise.all([loadOrders(false), loadFills(false)]);
    elements.updated.textContent = `${new Intl.DateTimeFormat("ja-JP", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date())} 更新`;
  } catch (error) {
    showError(error instanceof Error ? error.message : "履歴を取得できませんでした。");
  } finally {
    setLoading(false);
  }
}

async function loadOrders(append) {
  const params = commonParams();
  if (elements.status?.value) params.set("status", elements.status.value);
  if (elements.strategy?.value.trim()) params.set("strategy_id", elements.strategy.value.trim());
  if (append && state.orderCursor) params.set("cursor", state.orderCursor);
  const payload = await fetchJson(`/ui-api/history/orders?${params}`);
  state.orders = append ? [...state.orders, ...payload.items] : payload.items;
  state.orderCursor = payload.next_cursor;
  renderOrders();
}

async function loadFills(append) {
  const params = commonParams();
  if (append && state.fillCursor) params.set("cursor", state.fillCursor);
  const payload = await fetchJson(`/ui-api/history/fills?${params}`);
  state.fills = append ? [...state.fills, ...payload.items] : payload.items;
  state.fillCursor = payload.next_cursor;
  renderFills();
}

function orderPayload() {
  const quantity = Number(elements.orderQuantity.value);
  if (!Number.isFinite(quantity) || quantity <= 0) {
    throw new Error("数量は0より大きい値を入力してください。");
  }
  const payload = {
    request_id: requestId("manual"),
    exchange_id: elements.orderExchange.value,
    symbol: elements.orderSymbol.value,
    side: elements.orderSide.value,
    order_type: elements.orderType.value,
    quantity: elements.orderQuantity.value,
    reduce_only: elements.orderReduceOnly.checked,
  };
  if (!payload.exchange_id || !payload.symbol) {
    throw new Error("取引所と銘柄を選択してください。");
  }
  if (payload.order_type === "limit") {
    const limitPrice = Number(elements.orderLimitPrice.value);
    if (!Number.isFinite(limitPrice) || limitPrice <= 0) {
      throw new Error("指値価格は0より大きい値を入力してください。");
    }
    payload.limit_price = elements.orderLimitPrice.value;
  }
  return payload;
}

async function submitOrder(event) {
  event.preventDefault();
  try {
    const payload = orderPayload();
    const detail = [
      `${payload.exchange_id} ${payload.symbol}`,
      `${sideLabel(payload.side)} ${payload.order_type === "market" ? "成行" : `指値 ${payload.limit_price}`}`,
      `数量 ${payload.quantity}`,
      payload.reduce_only ? "Reduce-only" : "新規建玉を許可",
    ].join("\n");
    if (!window.confirm(`${detail}\n\nこのPaper注文を送信しますか？`)) return;
    elements.orderSubmit.disabled = true;
    elements.orderSubmit.textContent = "送信中…";
    const order = await postJson("/ui-api/orders", payload);
    showOperation(
      elements.orderMessage,
      `注文を受け付けました: ${order.id}（${statusLabels[order.status] || order.status}）`,
      "success",
    );
    elements.orderQuantity.value = "";
    elements.orderLimitPrice.value = "";
    await loadOrders(false);
  } catch (error) {
    showOperation(elements.orderMessage, error.message, "error");
  } finally {
    elements.orderSubmit.disabled = state.control.kill_switch;
    elements.orderSubmit.textContent = "注文内容を確認";
  }
}

function resetCurrentPositions(message = "現在値はまだ取得されていません") {
  state.currentPositions = [];
  elements.currentBody.innerHTML = "";
  elements.currentEmpty.textContent = message;
  elements.currentEmpty.hidden = false;
  elements.currentTotal.textContent = "—";
  elements.currentTotal.classList.remove("positive", "negative");
  elements.currentUpdated.textContent = "「現在値を更新」を押すと市場価格を取得します";
  elements.currentError.hidden = true;
  elements.currentError.textContent = "";
  elements.currentCloseAll.disabled = true;
}

function renderCurrentPositions() {
  elements.currentBody.innerHTML = state.currentPositions.map((position) => {
    const side = position.position_side === "buy" ? "buy" : "sell";
    const quantity = Number(position.quantity);
    const pnl = Number(position.unrealized_pnl);
    const pnlClass = Number.isFinite(pnl) && pnl > 0 ? "positive" : Number.isFinite(pnl) && pnl < 0 ? "negative" : "";
    const currentPrice = position.valuation_status === "ok"
      ? formatNumber(position.current_price)
      : `<span class="valuation-unavailable" title="${escapeHtml(position.valuation_error || "価格を取得できませんでした")}">取得失敗</span>`;
    return `
      <tr>
        <td>
          <strong class="token-pair">${escapeHtml(position.exchange_id)} · ${escapeHtml(position.base_asset)} / ${escapeHtml(position.quote_asset || "—")}</strong>
          <span class="symbol-caption">${escapeHtml(position.symbol)}</span>
        </td>
        <td><span class="side-pill ${side}">${sideLabel(side)}</span></td>
        <td class="numeric">${formatNumber(Number.isFinite(quantity) ? Math.abs(quantity) : position.quantity)}</td>
        <td class="numeric">${formatNumber(position.average_entry_price)}</td>
        <td class="numeric">${currentPrice}</td>
        <td class="numeric pnl-value ${pnlClass}">${position.valuation_status === "ok" ? formatSigned(position.unrealized_pnl) : "—"}</td>
        <td>${escapeHtml(formatDateTime(position.price_observed_at))}</td>
        <td><button class="row-danger-button" type="button" data-close-exchange="${escapeHtml(position.exchange_id)}" data-close-symbol="${escapeHtml(position.symbol)}" ${state.control.kill_switch ? "disabled" : ""}>全量決済</button></td>
      </tr>`;
  }).join("");
  elements.currentEmpty.hidden = state.currentPositions.length > 0;
  elements.currentCloseAll.disabled = state.control.kill_switch || state.currentPositions.length === 0;
}

async function loadCurrentPositions() {
  elements.currentRefresh.disabled = true;
  elements.currentRefresh.textContent = "取得中…";
  elements.currentError.hidden = true;
  elements.currentError.textContent = "";
  try {
    if (!elements.exchange.value) {
      throw new Error("現在建玉を取得する取引所を選択してください。");
    }
    const params = new URLSearchParams({ exchange_id: elements.exchange.value });
    if (elements.symbol.value.trim()) params.set("symbol", elements.symbol.value.trim());
    const payload = await fetchJson(`/ui-api/current-positions?${params}`);
    state.currentPositions = payload.positions || [];
    renderCurrentPositions();
    const total = payload.total_unrealized_pnl;
    elements.currentTotal.textContent = total === null ? "—" : formatSigned(total);
    elements.currentTotal.classList.toggle("positive", Number(total) > 0);
    elements.currentTotal.classList.toggle("negative", Number(total) < 0);
    elements.currentUpdated.textContent = `${formatDateTime(payload.refreshed_at)} 取得`;
    if (!payload.valuation_complete) {
      elements.currentError.textContent = `${payload.unpriced_count}件の建玉で現在価格を取得できませんでした。全決済操作は利用できます。`;
      elements.currentError.hidden = false;
    }
    if (!state.currentPositions.length) {
      elements.currentEmpty.textContent = "現在保有している建玉はありません";
    }
  } catch (error) {
    resetCurrentPositions("現在値を取得できませんでした");
    elements.currentUpdated.textContent = "更新失敗";
    elements.currentError.textContent = error instanceof Error ? error.message : "現在値を取得できませんでした。";
    elements.currentError.hidden = false;
  } finally {
    elements.currentRefresh.disabled = false;
    elements.currentRefresh.textContent = "現在値を更新";
  }
}

async function closePositions(exchangeId, symbol = null) {
  if (state.control.kill_switch) {
    throw new Error("Kill Switch中は全決済できません。");
  }
  const label = symbol ? `${exchangeId} ${symbol}の全建玉` : `${exchangeId}の全建玉`;
  if (!window.confirm(`${label}をReduce-only成行で決済します。\nよろしいですか？`)) return;
  const payload = {
    request_id: requestId("close"),
    exchange_id: exchangeId,
  };
  if (symbol) payload.symbol = symbol;
  const result = await postJson("/ui-api/positions/close", payload);
  showOperation(
    elements.orderMessage,
    `全決済注文を${result.items.length}件受け付けました。`,
    "warning",
  );
  await loadOrders(false);
  window.setTimeout(() => loadCurrentPositions(), 1200);
}

function renderOrders() {
  elements.ordersBody.innerHTML = state.orders.map((order) => {
    const side = order.side === "buy" ? "buy" : "sell";
    const statusClass = Object.hasOwn(statusLabels, order.status) ? order.status : "pending";
    const orderType = order.order_type === "limit" ? "指値" : "成行";
    return `
      <tr tabindex="0" data-order-id="${escapeHtml(order.id)}" aria-label="${escapeHtml(order.symbol)} の注文詳細を開く">
        <td>${escapeHtml(formatDateTime(order.created_at))}</td>
        <td><strong>${escapeHtml(order.exchange_id)}</strong></td>
        <td><strong>${escapeHtml(order.symbol)}</strong></td>
        <td><span class="side-pill ${side}">${sideLabel(side)}</span></td>
        <td>${orderType}${order.reduce_only ? " / Reduce-only" : ""}</td>
        <td class="numeric">${formatNumber(order.quantity)}</td>
        <td class="numeric">${formatNumber(order.filled_quantity)}</td>
        <td class="numeric">${formatNumber(order.average_fill_price)}</td>
        <td><span class="status-pill ${statusClass}">${escapeHtml(statusLabels[order.status] || order.status)}</span></td>
      </tr>`;
  }).join("");
  elements.ordersEmpty.hidden = state.orders.length > 0;
  elements.ordersMore.hidden = !state.orderCursor;
}

function renderFills() {
  elements.fillsBody.innerHTML = state.fills.map((fill) => {
    const side = fill.side === "buy" ? "buy" : "sell";
    const notional = Number(fill.quantity) * Number(fill.price);
    return `
      <tr>
        <td>${escapeHtml(formatDateTime(fill.executed_at))}</td>
        <td><strong>${escapeHtml(fill.exchange_id)}</strong></td>
        <td><strong>${escapeHtml(fill.symbol)}</strong></td>
        <td><span class="side-pill ${side}">${sideLabel(side)}</span></td>
        <td class="numeric">${formatNumber(fill.quantity)}</td>
        <td class="numeric">${formatNumber(fill.price)}</td>
        <td class="numeric">${formatNumber(notional)}</td>
        <td class="numeric">${formatNumber(fill.fee)}</td>
        <td><span class="role-pill">${escapeHtml(fill.liquidity_role)}</span></td>
      </tr>`;
  }).join("");
  elements.fillsEmpty.hidden = state.fills.length > 0;
  elements.fillsMore.hidden = !state.fillCursor;
}

function setActiveTab(tab) {
  state.activeTab = tab;
  const ordersActive = tab === "orders";
  elements.ordersTab.setAttribute("aria-selected", String(ordersActive));
  elements.fillsTab.setAttribute("aria-selected", String(!ordersActive));
  elements.ordersPanel.hidden = !ordersActive;
  elements.fillsPanel.hidden = ordersActive;
}

function orderDetailMarkup(order) {
  const details = [
    ["注文ID", order.id, "full"],
    ["受付日時", formatDateTime(order.created_at)],
    ["更新日時", formatDateTime(order.updated_at)],
    ["取引所", `${order.exchange_id} / ${order.exchange_network}`],
    ["銘柄", order.symbol],
    ["戦略ID", order.strategy_id || "—"],
    ["売買", sideLabel(order.side)],
    ["注文種別", order.order_type === "limit" ? "指値" : "成行"],
    ["Reduce-only", order.reduce_only ? "はい" : "いいえ"],
    ["注文数量", formatNumber(order.quantity)],
    ["指値価格", formatNumber(order.limit_price)],
    ["約定数量", formatNumber(order.filled_quantity)],
    ["平均約定価格", formatNumber(order.average_fill_price)],
    ["手数料合計", formatNumber(order.total_fee)],
    ["状態", statusLabels[order.status] || order.status],
  ];
  if (order.rejection_reason) details.push(["理由", order.rejection_reason, "full"]);
  return `<div class="detail-grid">${details.map(([label, value, width]) => `
    <div class="detail-item ${width || ""}">
      <span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>
    </div>`).join("")}</div>`;
}

function updateCancelButton(order) {
  const cancellable = cancellableStatuses.has(order.status) && !order.cancellation_requested;
  elements.orderCancel.hidden = !cancellable;
  elements.orderCancel.disabled = !cancellable;
}

async function openOrderDetail(orderId) {
  const order = state.orders.find((item) => item.id === orderId);
  if (!order) return;
  state.selectedOrderId = order.id;
  elements.orderDetail.innerHTML = orderDetailMarkup(order);
  updateCancelButton(order);
  elements.orderFills.innerHTML = '<p class="related-empty">約定を読み込み中…</p>';
  elements.dialog.showModal();
  try {
    const fills = await fetchJson(`/ui-api/orders/${encodeURIComponent(order.id)}/fills`);
    elements.orderFills.innerHTML = fills.length ? fills.map((fill) => `
      <div class="related-fill">
        <span><strong>${escapeHtml(sideLabel(fill.side))}</strong> ${escapeHtml(formatDateTime(fill.executed_at))}</span>
        <span>${formatNumber(fill.quantity)} × ${formatNumber(fill.price)}</span>
        <span>手数料 ${formatNumber(fill.fee)}</span>
      </div>`).join("") : '<p class="related-empty">この注文には約定がありません。</p>';
  } catch (error) {
    elements.orderFills.innerHTML = `<p class="related-empty">${escapeHtml(error.message)}</p>`;
  }
}

async function cancelSelectedOrder() {
  const order = state.orders.find((item) => item.id === state.selectedOrderId);
  if (!order || !cancellableStatuses.has(order.status)) return;
  if (!window.confirm(`${order.exchange_id} ${order.symbol}の注文を取り消しますか？`)) return;
  elements.orderCancel.disabled = true;
  try {
    const updated = await postJson(`/ui-api/orders/${encodeURIComponent(order.id)}/cancel`);
    state.orders = state.orders.map((item) => item.id === updated.id ? updated : item);
    renderOrders();
    elements.orderDetail.innerHTML = orderDetailMarkup(updated);
    updateCancelButton(updated);
    showOperation(elements.orderMessage, `注文 ${updated.id} に取消要求を設定しました。`, "warning");
  } catch (error) {
    showOperation(elements.orderMessage, error.message, "error");
    elements.orderCancel.disabled = false;
  }
}

elements.orderForm.addEventListener("submit", submitOrder);
elements.orderExchange.addEventListener("change", updateOrderSymbols);
elements.orderType.addEventListener("change", () => {
  const limit = elements.orderType.value === "limit";
  elements.orderLimitPrice.disabled = !limit;
  elements.orderLimitPrice.required = limit;
  if (!limit) elements.orderLimitPrice.value = "";
});
elements.controlToggle.addEventListener("click", setCloseOnly);

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  elements.quickRanges.forEach((button) => button.classList.remove("active"));
  loadAll();
});

elements.currentRefresh.addEventListener("click", loadCurrentPositions);
elements.currentCloseAll.addEventListener("click", async () => {
  try {
    await closePositions(elements.exchange.value);
  } catch (error) {
    showOperation(elements.orderMessage, error.message, "error");
  }
});
elements.currentBody.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-close-symbol]");
  if (!button) return;
  button.disabled = true;
  try {
    await closePositions(button.dataset.closeExchange, button.dataset.closeSymbol);
  } catch (error) {
    showOperation(elements.orderMessage, error.message, "error");
  } finally {
    button.disabled = state.control.kill_switch;
  }
});
elements.symbol.addEventListener("input", () => resetCurrentPositions());
elements.exchange.addEventListener("change", () => resetCurrentPositions());

elements.quickRanges.forEach((button) => button.addEventListener("click", () => {
  setQuickRange(button.dataset.range);
  loadAll();
}));
elements.ordersTab.addEventListener("click", () => setActiveTab("orders"));
elements.fillsTab.addEventListener("click", () => setActiveTab("fills"));

elements.ordersMore.addEventListener("click", async () => {
  elements.ordersMore.disabled = true;
  try {
    await loadOrders(true);
  } catch (error) {
    showError(error.message);
  } finally {
    elements.ordersMore.disabled = false;
  }
});
elements.fillsMore.addEventListener("click", async () => {
  elements.fillsMore.disabled = true;
  try {
    await loadFills(true);
  } catch (error) {
    showError(error.message);
  } finally {
    elements.fillsMore.disabled = false;
  }
});
elements.ordersBody.addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-order-id]");
  if (row) openOrderDetail(row.dataset.orderId);
});
elements.ordersBody.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const row = event.target.closest("tr[data-order-id]");
  if (row) {
    event.preventDefault();
    openOrderDetail(row.dataset.orderId);
  }
});

elements.orderCancel.addEventListener("click", cancelSelectedOrder);
elements.dialogClose.addEventListener("click", () => elements.dialog.close());
elements.dialog.addEventListener("click", (event) => {
  if (event.target === elements.dialog) elements.dialog.close();
});

async function initialize() {
  setQuickRange(30);
  setActiveTab("orders");
  resetCurrentPositions();
  try {
    await Promise.all([loadExchanges(), loadControl()]);
  } catch (error) {
    showError(error instanceof Error ? error.message : "初期データを取得できませんでした。");
  }
  await loadAll();
}

initialize();
