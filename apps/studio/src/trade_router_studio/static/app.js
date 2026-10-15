const state = {
  view: "dashboard",
  selected: null,
  strategyType: "script",
  strategyTab: "overview",
  blockSource: null,
  strategySearch: "",
  dirty: false,
  strategies: [],
  providers: [],
  connections: [],
  accounts: [],
  connectionStatuses: {},
  capabilities: [],
  orderPermissions: [],
  strategyPermission: null,
  activityTab: "logs",
  logCursor: 0,
  logTimer: null,
  instanceId: null,
  systemStatus: null,
  dashboard: null,
  dashboardLoading: false,
  strategySummary: null,
  dashboardServiceTab: "market",
  strategyServiceTab: "market",
};

const elements = {
  list: document.querySelector("#strategy-list"),
  count: document.querySelector("#strategy-count"),
  dashboardButton: document.querySelector("#dashboard-button"),
  dashboardRefresh: document.querySelector("#dashboard-refresh"),
  dashboardView: document.querySelector("#dashboard-view"),
  strategyView: document.querySelector("#strategy-view"),
  strategyOverview: document.querySelector("#strategy-overview"),
  strategyBlocks: document.querySelector("#strategy-blocks"),
  blockCanvas: document.querySelector("#block-canvas"),
  blockSummary: document.querySelector("#block-editor-summary"),
  strategyCode: document.querySelector("#strategy-code"),
  strategySearch: document.querySelector("#strategy-search"),
  strategySearchInput: document.querySelector("#strategy-search-input"),
  newStrategyModal: document.querySelector("#new-strategy-modal"),
  editor: document.querySelector("#source-editor"),
  lines: document.querySelector("#line-numbers"),
  fileName: document.querySelector("#file-name"),
  strategyKindBadge: document.querySelector("#strategy-kind-badge"),
  blockTab: document.querySelector("#strategy-block-tab"),
  codeTab: document.querySelector("#strategy-code-tab"),
  dirty: document.querySelector("#dirty-mark"),
  save: document.querySelector("#save-button"),
  run: document.querySelector("#run-button"),
  stop: document.querySelector("#stop-button"),
  orderPermission: document.querySelector("#order-permission-button"),
  orderPermissionState: document.querySelector("#order-permission-state"),
  remove: document.querySelector("#delete-button"),
  strategyState: document.querySelector("#strategy-state"),
  log: document.querySelector("#log-output"),
  orders: document.querySelector("#order-output"),
  clearLog: document.querySelector("#clear-log-button"),
  toast: document.querySelector("#toast"),
  serverDot: document.querySelector("#server-dot"),
  runtime: document.querySelector("#runtime-status"),
  connections: document.querySelector("#connection-status"),
  workspace: document.querySelector("#workspace-path"),
  settingsModal: document.querySelector("#settings-modal"),
  connectionProvider: document.querySelector("#connection-provider"),
  connectionTarget: document.querySelector("#connection-target"),
  accountConnection: document.querySelector("#account-connection"),
  productCodeField: document.querySelector("#product-code-field"),
  currentClock: document.querySelector("#current-clock"),
  currentDate: document.querySelector("#current-date"),
  currentTime: document.querySelector("#current-time"),
};

const seoulDateFormatter = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul",
  month: "2-digit",
  day: "2-digit",
  weekday: "short",
});
const seoulTimeFormatter = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

function updateClock() {
  const now = new Date();
  elements.currentClock.dateTime = now.toISOString();
  elements.currentDate.textContent = seoulDateFormatter.format(now);
  elements.currentTime.textContent = seoulTimeFormatter.format(now);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") {
        message = payload.detail;
      } else if (payload.detail?.message) {
        message = payload.detail.message;
      }
    } catch (_) {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function notify(message, error = false) {
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", error);
  elements.toast.classList.add("visible");
  window.clearTimeout(notify.timer);
  notify.timer = window.setTimeout(() => elements.toast.classList.remove("visible"), 3200);
}

function setDirty(value) {
  state.dirty = value;
  elements.dirty.hidden = !value;
}

function resetActivity(message = "전략을 실행하면 활동 기록이 표시됩니다.") {
  elements.log.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "activity-empty";
  empty.textContent = message;
  elements.log.append(empty);
}

function appendActivity(entry) {
  elements.log.querySelector(".activity-empty")?.remove();
  const item = document.createElement("div");
  const level = ["system", "info", "success", "warning", "error", "output"].includes(entry.level)
    ? entry.level
    : "output";
  item.className = `activity-item ${level}`;
  const dot = document.createElement("span");
  dot.className = "activity-dot";
  const message = document.createElement("span");
  message.className = "activity-message";
  message.textContent = entry.message;
  const time = document.createElement("time");
  time.className = "activity-time";
  if (entry.timestamp) {
    const date = new Date(entry.timestamp);
    time.dateTime = entry.timestamp;
    time.textContent = Number.isNaN(date.valueOf())
      ? ""
      : date.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }
  item.append(dot, message, time);
  elements.log.append(item);
}

function selectedState() {
  return state.strategies.find(item => item.name === state.selected);
}

function displayStrategyName(name) {
  return String(name || "").replace(/\.py$/i, "");
}

function formatTimestamp(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? "—"
    : date.toLocaleString("ko-KR", { hour12: false });
}

function formatNumber(value, { signed = false } = {}) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const formatted = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 }).format(Math.abs(number));
  if (number < 0) return `-${formatted}`;
  return signed && number > 0 ? `+${formatted}` : formatted;
}

function profitClass(value) {
  const number = Number(value);
  return number > 0 ? "positive" : number < 0 ? "negative" : "";
}

function appendLimitRow(container, label, usage, kind = "money") {
  const row = document.createElement("div");
  row.className = "limit-row";
  const title = document.createElement("span");
  title.className = "limit-label";
  title.textContent = label;
  const track = document.createElement("div");
  track.className = "limit-track";
  const fill = document.createElement("div");
  fill.className = "limit-fill";
  const percent = Number(usage?.percent);
  const displayPercent = Number.isFinite(percent) ? Math.max(0, Math.min(percent, 100)) : 0;
  fill.style.width = `${displayPercent}%`;
  if (percent >= 100) fill.classList.add("danger");
  else if (percent >= 75) fill.classList.add("warning");
  track.append(fill);
  const value = document.createElement("span");
  value.className = "limit-value";
  if (usage?.limit == null) {
    value.textContent = "미설정";
  } else if (usage?.used == null) {
    value.textContent = `한도 ${formatNumber(usage.limit)}${kind === "money" ? "원" : "건"}`;
  } else {
    value.textContent = `${formatNumber(usage.used)} / ${formatNumber(usage.limit)}${kind === "money" ? "원" : "건"}`;
  }
  row.append(title, track, value);
  container.append(row);
}

function serviceCountItems(result) {
  if (result.id === "scheduler") {
    return [
      ["처리", result.total || 0],
      ["활성 일정", result.active_schedules || 0],
      ["대기 이벤트", result.queued_events || 0],
    ];
  }
  if (result.id === "account" && result.snapshot_success != null) {
    return [
      ["정상", result.snapshot_success || 0],
      ["오류", result.snapshot_error || 0],
      ["미지원", result.snapshot_unsupported || 0],
    ];
  }
  return [
    ["처리", result.total || 0],
    ["성공", result.success || 0],
    ["오류", result.error || 0],
  ];
}

function serviceOperationLabel(operation) {
  return {
    quote: "현재가",
    bars: "봉 조회",
    conditions: "조건식 목록",
    condition_matches: "조건검색 결과",
    ranking: "순위 조회",
    register: "일정 등록",
    deliver: "일정 전달",
    balance: "잔고",
    orders: "주문·체결 조회",
    buying_power: "주문 가능 수량",
    submit: "주문 제출",
  }[operation] || operation || "처리";
}

function serviceEventTarget(event) {
  const details = event?.details || {};
  const values = [
    details.code,
    details.account,
    details.schedule_id,
    details.condition_id ? `조건 ${details.condition_id}` : null,
    details.interval,
  ].filter(Boolean);
  return values.join(" · ") || event?.source || "—";
}

function renderServiceResultTabs(container, rows, scope) {
  container.replaceChildren();
  const services = (rows || []).filter(result => ["market", "scheduler", "account", "order"].includes(result.id));
  if (!services.length) return;
  const stateKey = scope === "dashboard" ? "dashboardServiceTab" : "strategyServiceTab";
  if (!services.some(result => result.id === state[stateKey])) state[stateKey] = services[0].id;

  const tabList = document.createElement("div");
  tabList.className = "service-tabs-list";
  tabList.setAttribute("role", "tablist");
  for (const result of services) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `service-tab ${result.id === state[stateKey] ? "active" : ""}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(result.id === state[stateKey]));
    const dot = document.createElement("span");
    dot.className = `service-state-dot ${result.state || "idle"}`;
    const label = document.createElement("span");
    label.textContent = result.label;
    const total = document.createElement("small");
    total.textContent = formatNumber(result.total || 0);
    button.append(dot, label, total);
    button.addEventListener("click", () => {
      state[stateKey] = result.id;
      renderServiceResultTabs(container, services, scope);
    });
    tabList.append(button);
  }

  const result = services.find(item => item.id === state[stateKey]);
  const detail = document.createElement("section");
  detail.className = "service-tab-detail";
  const summary = document.createElement("div");
  summary.className = "service-tab-summary";
  const copy = document.createElement("div");
  const heading = document.createElement("div");
  heading.className = "service-tab-heading";
  const title = document.createElement("strong");
  title.textContent = result.label;
  const stateLabel = document.createElement("span");
  stateLabel.className = `status-pill ${result.state || "idle"}`;
  stateLabel.textContent = {
    ok: "정상", error: "오류", busy: "처리 중", unsupported: "미지원", idle: "사용 전",
  }[result.state] || "확인 전";
  heading.append(title, stateLabel);
  const message = document.createElement("p");
  message.textContent = result.last_message || "오늘 처리한 요청이 없습니다.";
  copy.append(heading, message);
  const counts = document.createElement("div");
  counts.className = "service-tab-counts";
  for (const [label, value] of serviceCountItems(result)) {
    const item = document.createElement("span");
    item.innerHTML = `<small>${label}</small><strong>${formatNumber(value)}</strong>`;
    counts.append(item);
  }
  summary.append(copy, counts);
  detail.append(summary);

  const events = result.events || [];
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "service-tab-empty";
    empty.textContent = "표시할 처리 결과가 없습니다. 전략이 이 서비스를 사용하면 여기에 결과가 쌓입니다.";
    detail.append(empty);
  } else {
    const tableWrap = document.createElement("div");
    tableWrap.className = "service-event-table-wrap";
    const table = document.createElement("table");
    table.className = "service-event-table";
    table.innerHTML = "<thead><tr><th>시각</th><th>작업</th><th>대상</th><th>결과</th><th>내용</th><th>소요</th></tr></thead>";
    const body = document.createElement("tbody");
    for (const event of events) {
      const row = document.createElement("tr");
      const timestamp = event.occurred_at
        ? new Date(event.occurred_at).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })
        : "—";
      const values = [
        timestamp,
        serviceOperationLabel(event.operation),
        serviceEventTarget(event),
        event.outcome === "error" ? "오류" : "성공",
        event.message || "—",
        event.duration_ms == null ? "—" : `${formatNumber(event.duration_ms)}ms`,
      ];
      for (const [index, value] of values.entries()) {
        const cell = document.createElement(index === 0 ? "th" : "td");
        cell.textContent = value;
        if (index === 3) cell.className = event.outcome === "error" ? "negative" : "positive";
        row.append(cell);
      }
      body.append(row);
    }
    table.append(body);
    tableWrap.append(table);
    detail.append(tableWrap);
  }
  container.append(tabList, detail);
}

function renderStrategyOverview() {
  const strategy = selectedState();
  const armed = Boolean(state.strategyPermission?.armed);
  const summary = state.strategySummary;
  document.querySelector("#overview-runtime").textContent = strategy?.running ? "실행 중" : "중지됨";
  document.querySelector("#overview-permission").textContent = armed ? "허용" : "차단";
  document.querySelector("#overview-orders").textContent = `${summary?.orders?.today || 0}건`;
  document.querySelector("#overview-fills").textContent = `${summary?.orders?.filled || 0} · ${summary?.orders?.rejected || 0} · ${summary?.orders?.unresolved || 0}건`;
  const accountState = summary?.account?.state === "ok"
    ? "정상"
    : summary?.account?.state === "unsupported" ? "잔고 미지원" : summary?.account ? "조회 오류" : "미등록";
  const sources = Object.entries(summary?.market_sources || {}).map(([alias, source]) => `${alias}: ${source}`);
  const schedules = summary?.schedule?.schedules || [];
  const scheduleText = schedules.length
    ? schedules.map(item => `${item.id} · 다음 ${new Date(item.next_at).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}`).join(" / ")
    : strategy?.running ? "등록된 일정 없음" : "실행하면 등록 일정을 확인합니다.";
  document.querySelector("#overview-connection-line").textContent = [
    `ID ${summary?.strategy_id || "확인 필요"}`,
    `계좌 ${summary?.trading_account || "미등록"} · ${accountState}`,
    `시세 ${sources.length ? sources.join(" · ") : "미등록"}`,
    `일정 ${scheduleText}`,
  ].join("  |  ");
  document.querySelector("#overview-updated").textContent = summary?.generated_at
    ? `${new Date(summary.generated_at).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })} 기준`
    : "상태 확인 중";

  const performance = summary?.performance || {};
  const performanceFields = [
    ["#overview-estimated-profit", performance.estimated_profit],
    ["#overview-realized-profit", performance.realized_profit],
    ["#overview-unrealized-profit", performance.unrealized_profit],
    ["#overview-buy-amount", performance.filled_buy_amount],
    ["#overview-sell-amount", performance.filled_sell_amount],
    ["#overview-commission", performance.commission],
    ["#overview-tax", performance.tax],
  ];
  for (const [selector, value] of performanceFields) {
    const element = document.querySelector(selector);
    element.textContent = formatNumber(value, { signed: selector.includes("profit") });
    element.className = selector.includes("profit") ? profitClass(value) : "";
  }
  const returnElement = document.querySelector("#overview-estimated-return");
  returnElement.textContent = performance.estimated_return_percent == null
    ? "—"
    : Number(performance.estimated_return_percent).toLocaleString("ko-KR", { maximumFractionDigits: 2 });
  returnElement.className = profitClass(performance.estimated_return_percent);
  document.querySelector("#overview-cost-basis").textContent = performance.costs_complete
    ? "체결 금액에 계좌별 수수료율과 매도 세율을 반영한 순손익입니다."
    : "수수료율 또는 매도 세율이 비어 있어 현재 손익의 비용은 미확정입니다.";

  renderServiceResultTabs(
    document.querySelector("#overview-service-results"),
    summary?.service_results || [],
    "strategy"
  );

  const limits = document.querySelector("#overview-limit-list");
  limits.replaceChildren();
  appendLimitRow(limits, "1회 주문 (오늘 최고)", summary?.limits?.max_order_amount);
  appendLimitRow(limits, "오늘 매수 예산", summary?.limits?.max_daily_buy_amount);
  appendLimitRow(limits, "운용 예산", summary?.limits?.max_exposure);
  appendLimitRow(limits, "미체결 주문", summary?.limits?.max_open_orders, "count");
}

function renderDashboard() {
  const payload = state.dashboard;
  const summary = payload?.summary || {};
  document.querySelector("#dashboard-total-assets").textContent = formatNumber(summary.live_assets);
  const profit = document.querySelector("#dashboard-profit");
  profit.textContent = formatNumber(summary.live_unrealized_profit, { signed: true });
  profit.className = profitClass(summary.live_unrealized_profit);
  document.querySelector("#dashboard-valued-accounts").textContent = `${summary.valued_accounts || 0} / ${summary.account_count || 0}`;
  document.querySelector("#dashboard-unresolved").textContent = String(summary.unresolved_orders ?? state.systemStatus?.unresolved_orders ?? 0);
  document.querySelector("#dashboard-updated").textContent = payload?.generated_at
    ? `${formatTimestamp(payload.generated_at)} 기준`
    : state.dashboardLoading ? "계좌 상태 조회 중" : "계좌 상태 확인 전";
  document.querySelector("#dashboard-account-summary").textContent = `${summary.account_count || 0}개 계좌`;
  const queue = payload?.queue || {};
  document.querySelector("#dashboard-queue-summary").textContent = `대기 ${queue.queued || 0} · 처리 ${queue.inflight || 0}`;
  renderServiceResultTabs(
    document.querySelector("#dashboard-service-results"),
    payload?.service_results || [],
    "dashboard"
  );

  const accountList = document.querySelector("#dashboard-account-list");
  accountList.replaceChildren();
  if (!payload?.accounts?.length) {
    const empty = document.createElement("div");
    empty.className = "dashboard-empty";
    empty.textContent = state.dashboardLoading ? "계좌 잔고를 조회하고 있습니다." : "등록된 거래 계좌가 없습니다.";
    accountList.append(empty);
  } else {
    for (const account of payload.accounts) accountList.append(renderDashboardAccount(account));
  }

  const serviceList = document.querySelector("#dashboard-service-list");
  serviceList.replaceChildren();
  if (!payload?.services?.length) {
    const empty = document.createElement("div");
    empty.className = "dashboard-empty";
    empty.textContent = "등록된 API 연결이 없습니다.";
    serviceList.append(empty);
  } else {
    for (const service of payload.services) serviceList.append(renderDashboardService(service));
  }
}

function renderDashboardAccount(account) {
  const row = document.createElement("article");
  row.className = "dashboard-account-row";
  const heading = document.createElement("div");
  heading.className = "dashboard-row-heading";
  const copy = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = account.alias;
  const meta = document.createElement("p");
  const target = account.target === "LIVE" ? "실전" : account.target === "PAPER" ? "모의" : account.target;
  const permission = account.order_permission?.armed
    ? account.order_permission?.reconciled ? "주문 허용" : "주문 허용 · 조정 필요"
    : "주문 차단";
  meta.textContent = `${account.connection_label || account.provider || "API"} · ${target || ""} · ${account.masked_account || ""} · ${permission}`;
  copy.append(name, meta);
  const status = document.createElement("span");
  status.className = `status-pill ${account.state || "unchecked"}`;
  status.textContent = account.state === "ok" ? "정상" : account.state === "unsupported" ? "미지원" : "조회 오류";
  status.title = account.message || "";
  heading.append(copy, status);

  const body = document.createElement("div");
  body.className = "account-dashboard-body";
  const values = document.createElement("div");
  values.className = "account-values";
  for (const [label, raw, signed] of [
    ["총 자산", account.total_value, false],
    ["예수금", account.cash, false],
    ["평가손익", account.unrealized_profit, true],
  ]) {
    const cell = document.createElement("div");
    cell.className = "account-value";
    const caption = document.createElement("span");
    caption.textContent = label;
    const value = document.createElement("strong");
    value.textContent = raw == null ? "—" : `${formatNumber(raw, { signed })}원`;
    if (label === "평가손익") value.className = profitClass(raw);
    cell.append(caption, value);
    values.append(cell);
  }
  const limits = document.createElement("div");
  limits.className = "limit-list";
  appendLimitRow(limits, "일일 매수", account.limits?.max_daily_buy_amount);
  appendLimitRow(limits, "보유 노출", account.limits?.max_exposure);
  appendLimitRow(limits, "미체결", account.limits?.max_open_orders, "count");
  body.append(values, limits);
  row.append(heading, body);
  return row;
}

function renderDashboardService(service) {
  const row = document.createElement("article");
  row.className = "dashboard-service-row";
  const heading = document.createElement("div");
  heading.className = "dashboard-row-heading";
  const copy = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = service.label;
  const meta = document.createElement("p");
  const target = service.target === "LIVE" ? "실전" : service.target === "PAPER" ? "모의" : service.target;
  meta.textContent = `${String(service.provider || "").toUpperCase()} · ${target || ""} · ${service.message || ""}`;
  copy.append(name, meta);
  const status = document.createElement("span");
  status.className = `status-pill ${service.state || "unchecked"}`;
  status.textContent = service.state === "connected" ? "연결됨" : service.state === "error" ? "오류" : "확인 전";
  heading.append(copy, status);

  const body = document.createElement("div");
  body.className = "service-dashboard-body";
  const restCapacity = Number(service.rest?.capacity || 1);
  const restAvailable = Number(service.rest?.available ?? restCapacity);
  body.append(serviceMeter(
    "REST 요청 슬롯",
    Math.max(0, restCapacity - restAvailable),
    restCapacity,
    `${formatNumber(service.rest?.requests_per_second || 0)}회/초`
  ));
  body.append(serviceMeter(
    "WebSocket 구독",
    Number(service.websocket?.subscriptions || 0),
    Number(service.websocket?.max_subscriptions || 0),
    `${service.websocket?.sessions || 0}/${service.websocket?.max_sessions || 0} 세션`
  ));
  row.append(heading, body);
  return row;
}

function serviceMeter(label, used, limit, detail) {
  const meter = document.createElement("div");
  const head = document.createElement("div");
  head.className = "service-meter-head";
  const title = document.createElement("span");
  title.textContent = label;
  const value = document.createElement("span");
  value.textContent = `${used.toFixed(used % 1 ? 1 : 0)} / ${limit || "—"} · ${detail}`;
  head.append(title, value);
  const track = document.createElement("div");
  track.className = "limit-track";
  const fill = document.createElement("div");
  fill.className = "limit-fill";
  const percent = limit > 0 ? (used / limit) * 100 : 0;
  fill.style.width = `${Math.max(0, Math.min(percent, 100))}%`;
  if (percent >= 100) fill.classList.add("danger");
  else if (percent >= 75) fill.classList.add("warning");
  track.append(fill);
  meter.append(head, track);
  return meter;
}

async function refreshDashboard(force = false) {
  if (state.dashboardLoading) return;
  state.dashboardLoading = true;
  elements.dashboardRefresh.disabled = true;
  renderDashboard();
  try {
    state.dashboard = await api(`/api/dashboard${force ? "?refresh=true" : ""}`);
  } finally {
    state.dashboardLoading = false;
    elements.dashboardRefresh.disabled = false;
    renderDashboard();
  }
}

async function refreshStrategySummary() {
  if (!state.selected) {
    state.strategySummary = null;
    renderStrategyOverview();
    return;
  }
  state.strategySummary = await api(
    `/api/strategies/${encodeURIComponent(state.selected)}/summary`
  );
  renderStrategyOverview();
}

const BLOCK_CONSTANT_META = {
  STRATEGY_ID: ["general", "전략 ID"],
  TRADING_ACCOUNT: ["connection", "거래 계좌"],
  MARKET_SOURCE: ["connection", "시세 연결"],
  CONDITION_SOURCE: ["connection", "조건검색 연결"],
  HTS_USER_ID: ["connection", "HTS 사용자 ID"],
  CONDITION_ID: ["target", "조건식 ID"],
  EXPECTED_CONDITION_NAME: ["target", "조건식 이름"],
  SYMBOL: ["target", "종목 코드"],
  CODE: ["target", "종목 코드"],
  ACTIVE_DATE: ["schedule", "실행 날짜"],
  BUY_BUDGET_PER_SYMBOL: ["risk", "종목당 매수 예산"],
  TOTAL_OPERATING_BUDGET: ["risk", "총 운용 예산"],
  MAX_NEW_BUYS_PER_SCAN: ["risk", "조회당 신규 매수 수"],
  BOLLINGER_PERIOD: ["risk", "볼린저 기간"],
  BOLLINGER_STDDEV: ["risk", "볼린저 표준편차"],
  BAR_INTERVAL: ["schedule", "봉 주기"],
  BAR_COUNT: ["risk", "조회 봉 수"],
  TAKE_PROFIT_MULTIPLIER: ["risk", "익절 배수"],
  STOP_LOSS_MULTIPLIER: ["risk", "손절 배수"],
};

const BLOCK_CATEGORY_META = {
  connection: ["연결", "주문 계좌와 시세·검색 연결"],
  target: ["대상", "종목과 조건검색 입력"],
  risk: ["위험 및 판단", "예산·수량·지표·청산 기준"],
  schedule: ["실행 기준", "날짜와 시간 기준"],
  general: ["전략 설정", "코드 상단에서 바꿀 수 있는 설정"],
};

const BLOCK_EVENT_LABELS = {
  on_start: "시작",
  on_quote: "실시간 시세",
  on_orderbook: "실시간 호가",
  on_bar: "봉 마감",
  on_timer: "예약 실행",
  on_order: "주문 상태",
  on_fill: "체결",
  on_stop: "종료",
};

function sourceLineNumber(source, index) {
  return source.slice(0, index).split("\n").length;
}

function parsePythonString(raw) {
  return raw.slice(1, -1).replace(/\\([\\"'])/g, "$1");
}

function genericConstantMeta(name) {
  if (BLOCK_CONSTANT_META[name]) return BLOCK_CONSTANT_META[name];
  const label = name.toLocaleLowerCase("ko-KR").split("_").join(" ");
  if (/(ACCOUNT|SOURCE|PROFILE|USER_ID)/.test(name)) return ["connection", label];
  if (/(CONDITION|SYMBOL|CODE|UNIVERSE)/.test(name)) return ["target", label];
  if (/(BUDGET|LIMIT|MAX|MIN|PROFIT|LOSS|BOLLINGER|PERIOD|STDDEV|QUANTITY|EXPOSURE)/.test(name)) {
    return ["risk", label];
  }
  if (/(DATE|TIME|INTERVAL|SECONDS)/.test(name)) return ["schedule", label];
  return ["general", label];
}

function parseEditableLiteral(raw, start, id, label, category) {
  const trimmed = raw.trim();
  const leading = raw.indexOf(trimmed);
  const valueStart = start + Math.max(leading, 0);
  if (/^Decimal\((["']).*\1\)$/.test(trimmed)) {
    const match = trimmed.match(/^Decimal\((["'])(.*?)\1\)$/);
    return { id, label, category, type: "decimal", value: match[2], start: valueStart, end: valueStart + trimmed.length };
  }
  if (/^["'].*["']$/.test(trimmed)) {
    return { id, label, category, type: "string", value: parsePythonString(trimmed), start: valueStart, end: valueStart + trimmed.length };
  }
  if (/^-?\d+(?:\.\d+)?$/.test(trimmed)) {
    return { id, label, category, type: "number", value: trimmed, start: valueStart, end: valueStart + trimmed.length };
  }
  if (/^(True|False)$/.test(trimmed)) {
    return { id, label, category, type: "boolean", value: trimmed === "True", start: valueStart, end: valueStart + trimmed.length };
  }
  const dateMatch = trimmed.match(/^date\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)$/);
  if (dateMatch) {
    const value = `${dateMatch[1]}-${dateMatch[2].padStart(2, "0")}-${dateMatch[3].padStart(2, "0")}`;
    return { id, label, category, type: "date", value, start: valueStart, end: valueStart + trimmed.length };
  }
  return null;
}

function findBalancedCallEnd(source, openIndex) {
  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let index = openIndex; index < source.length; index += 1) {
    const char = source[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === quote) quote = null;
      continue;
    }
    if (char === "\"" || char === "'") {
      quote = char;
      continue;
    }
    if (char === "(") depth += 1;
    else if (char === ")") {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  return -1;
}

function scheduleStringField(content, base, name, label, id) {
  const pattern = new RegExp(`${name}\\s*=\\s*([\"'])(.*?)\\1`);
  const match = pattern.exec(content);
  if (!match) return null;
  const literal = `${match[1]}${match[2]}${match[1]}`;
  const localStart = match.index + match[0].lastIndexOf(literal);
  return { id, label, category: "schedule", type: "string", value: match[2], start: base + localStart, end: base + localStart + literal.length };
}

function scheduleNumberField(content, base, name, label, id) {
  const pattern = new RegExp(`${name}\\s*=\\s*(-?\\d+(?:\\.\\d+)?)`);
  const match = pattern.exec(content);
  if (!match) return null;
  const localStart = match.index + match[0].lastIndexOf(match[1]);
  return { id, label, category: "schedule", type: "number", value: match[1], start: base + localStart, end: base + localStart + match[1].length };
}

function scheduleBooleanField(content, base, name, label, id) {
  const pattern = new RegExp(`${name}\\s*=\\s*(True|False)`);
  const match = pattern.exec(content);
  if (!match) return null;
  const localStart = match.index + match[0].lastIndexOf(match[1]);
  return { id, label, category: "schedule", type: "boolean", value: match[1] === "True", start: base + localStart, end: base + localStart + match[1].length };
}

function parseStrategyBlocks(source) {
  const fields = [];
  const schedules = [];
  const handlers = [];
  const ignoredSecret = /(SECRET|TOKEN|PASSWORD|APP_?KEY)/;

  for (const match of source.matchAll(/^([A-Z][A-Z0-9_]*)[ \t]*=[ \t]*(.+?)[ \t]*$/gm)) {
    const name = match[1];
    if (ignoredSecret.test(name)) continue;
    const raw = match[2];
    const rawStart = match.index + match[0].indexOf(raw);
    const [category, label] = genericConstantMeta(name);
    const field = parseEditableLiteral(raw, rawStart, `constant:${name}`, label, category);
    if (field) fields.push({ ...field, sourceName: name, line: sourceLineNumber(source, match.index) });
  }

  const directFields = [
    ["config:id", "전략 ID", "general", /\bid\s*=\s*(["'][^"'\r\n]*["'])/m],
    ["config:account", "거래 계좌", "connection", /\btrading_account\s*=\s*(["'][^"'\r\n]*["'])/m],
    ["config:market", "기본 시세 연결", "connection", /\bmarket_sources\s*=\s*\{[^{}]*?["']primary["']\s*:\s*(["'][^"'\r\n]*["'])/m],
  ];
  for (const [id, label, category, pattern] of directFields) {
    const match = pattern.exec(source);
    if (!match) continue;
    const start = match.index + match[0].lastIndexOf(match[1]);
    if (fields.some(field => field.start === start)) continue;
    const field = parseEditableLiteral(match[1], start, id, label, category);
    if (field) fields.push({ ...field, line: sourceLineNumber(source, match.index) });
  }

  let cursor = 0;
  let scheduleIndex = 0;
  while (cursor < source.length) {
    const start = source.indexOf("ctx.scheduler.", cursor);
    if (start < 0) break;
    const methodMatch = source.slice(start).match(/^ctx\.scheduler\.([a-z_]+)\s*\(/);
    if (!methodMatch) {
      cursor = start + 14;
      continue;
    }
    const method = methodMatch[1];
    const openIndex = start + methodMatch[0].length - 1;
    const end = findBalancedCallEnd(source, openIndex);
    if (end < 0) break;
    const content = source.slice(openIndex + 1, end);
    const base = openIndex + 1;
    const idPrefix = `schedule:${scheduleIndex}`;
    const scheduleFields = [];
    const scheduleId = scheduleStringField(content, base, "schedule_id", "일정 ID", `${idPrefix}:id`);
    if (scheduleId) scheduleFields.push(scheduleId);
    const seconds = scheduleNumberField(content, base, "seconds", "실행 간격(초)", `${idPrefix}:seconds`);
    if (seconds) scheduleFields.push(seconds);
    const aligned = scheduleBooleanField(content, base, "aligned", "정각 맞춤", `${idPrefix}:aligned`);
    if (aligned) scheduleFields.push(aligned);
    const firstString = content.match(/^\s*(["'])(.*?)\1/);
    if (firstString && ["on_bar_close", "daily_at"].includes(method)) {
      const literal = `${firstString[1]}${firstString[2]}${firstString[1]}`;
      const localStart = firstString.index + firstString[0].lastIndexOf(literal);
      scheduleFields.push({
        id: `${idPrefix}:primary`,
        label: method === "on_bar_close" ? "봉 주기" : "실행 시각",
        category: "schedule",
        type: "string",
        value: firstString[2],
        start: base + localStart,
        end: base + localStart + literal.length,
      });
    }
    const between = /between\s*=\s*\(\s*(["'])(.*?)\1\s*,\s*(["'])(.*?)\3/.exec(content);
    if (between) {
      const firstLiteral = `${between[1]}${between[2]}${between[1]}`;
      const secondLiteral = `${between[3]}${between[4]}${between[3]}`;
      const firstStart = between.index + between[0].indexOf(firstLiteral);
      const secondStart = between.index + between[0].lastIndexOf(secondLiteral);
      scheduleFields.push({ id: `${idPrefix}:from`, label: "시작 시각", category: "schedule", type: "string", value: between[2], start: base + firstStart, end: base + firstStart + firstLiteral.length });
      scheduleFields.push({ id: `${idPrefix}:to`, label: "종료 시각", category: "schedule", type: "string", value: between[4], start: base + secondStart, end: base + secondStart + secondLiteral.length });
    }
    schedules.push({
      id: idPrefix,
      method,
      title: scheduleId?.value || ({ every: "주기 실행", on_bar_close: "봉 마감 실행", daily_at: "지정 시각 실행" }[method] || method),
      fields: scheduleFields,
      line: sourceLineNumber(source, start),
    });
    scheduleIndex += 1;
    cursor = end + 1;
  }

  const methodMatches = [...source.matchAll(/^[ \t]+(?:async\s+)?def ([a-z_][a-z0-9_]*)\s*\([^\n]*\)\s*:/gmi)];
  const handlerMatches = methodMatches.filter(match => match[1].startsWith("on_"));
  const actionPatterns = [
    [/ctx\.discovery\.conditions\s*\(/g, "조건식 목록 조회"],
    [/ctx\.discovery\.matches\s*\(/g, "조건검색 결과 조회"],
    [/ctx\.discovery\.ranking\s*\(/g, "순위 조회"],
    [/ctx\.market\.quote\s*\(/g, "현재가 조회"],
    [/ctx\.market\.bars\s*\(/g, "봉 조회"],
    [/ctx\.account\.balance\s*\(/g, "잔고 조회"],
    [/ctx\.account\.open_orders\s*\(/g, "미체결 조회"],
    [/ctx\.orders\.market_buy\s*\(/g, "시장가 매수"],
    [/ctx\.orders\.limit_buy\s*\(/g, "지정가 매수"],
    [/ctx\.orders\.market_sell\s*\(/g, "시장가 매도"],
    [/ctx\.orders\.limit_sell\s*\(/g, "지정가 매도"],
    [/_liquidate_owned_positions\s*\(/g, "전략 보유 수량 청산"],
  ];
  handlerMatches.forEach(match => {
    const nextMethod = methodMatches.find(candidate => candidate.index > match.index);
    const end = nextMethod?.index ?? source.length;
    const body = source.slice(match.index, end);
    const found = [];
    for (const [pattern, label] of actionPatterns) {
      pattern.lastIndex = 0;
      for (const action of body.matchAll(pattern)) found.push({ index: action.index, label });
    }
    const branchCount = (body.match(/^\s*if\s+/gm) || []).length;
    found.sort((left, right) => left.index - right.index);
    const actions = [];
    for (const item of found) {
      if (actions.at(-1) !== item.label) actions.push(item.label);
    }
    if (branchCount) actions.push(`조건 분기 ${branchCount}개`);
    if (!actions.length) actions.push("사용자 Python 로직");
    handlers.push({
      id: `handler:${match[1]}`,
      name: match[1],
      title: BLOCK_EVENT_LABELS[match[1]] || match[1],
      actions,
      line: sourceLineNumber(source, match.index),
    });
  });

  return { fields, schedules, handlers };
}

function formatBlockFieldValue(field, value) {
  if (field.type === "boolean") return value === true || value === "true" ? "True" : "False";
  if (field.type === "number") {
    const normalized = String(value).trim();
    if (!/^-?\d+(?:\.\d+)?$/.test(normalized)) throw new Error(`${field.label}에 숫자를 입력하세요.`);
    return normalized;
  }
  if (field.type === "decimal") {
    const normalized = String(value).trim();
    if (!/^-?\d+(?:\.\d+)?$/.test(normalized)) throw new Error(`${field.label}에 숫자를 입력하세요.`);
    return `Decimal(${JSON.stringify(normalized)})`;
  }
  if (field.type === "date") {
    const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!match) throw new Error(`${field.label}의 날짜를 확인하세요.`);
    return `date(${Number(match[1])}, ${Number(match[2])}, ${Number(match[3])})`;
  }
  return JSON.stringify(String(value));
}

function applyBlockFieldEdit(fieldId, value) {
  const parsed = parseStrategyBlocks(elements.editor.value);
  const field = [
    ...parsed.fields,
    ...parsed.schedules.flatMap(schedule => schedule.fields),
  ].find(item => item.id === fieldId);
  if (!field) throw new Error("블록 구성이 변경되어 필드 위치를 찾지 못했습니다. 블록 화면을 다시 열어주세요.");
  const replacement = formatBlockFieldValue(field, value);
  elements.editor.value = `${elements.editor.value.slice(0, field.start)}${replacement}${elements.editor.value.slice(field.end)}`;
  elements.editor.dispatchEvent(new Event("input"));
  renderStrategyBlocks();
}

function createBlockFieldControl(field, disabled) {
  const row = document.createElement("div");
  row.className = "strategy-block-field";
  const label = document.createElement("label");
  label.textContent = field.label;
  label.htmlFor = `block-field-${field.id.replace(/[^a-z0-9_-]/gi, "-")}`;
  let control;
  if (field.type === "boolean") {
    control = document.createElement("select");
    for (const [value, text] of [["true", "사용"], ["false", "사용 안 함"]]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      control.append(option);
    }
    control.value = String(field.value);
  } else {
    control = document.createElement("input");
    control.type = field.type === "date" ? "date" : ["number", "decimal"].includes(field.type) ? "number" : "text";
    if (["number", "decimal"].includes(field.type)) control.step = "any";
    control.value = field.value;
  }
  control.id = label.htmlFor;
  control.className = "block-field-control";
  control.disabled = disabled;
  control.addEventListener("change", event => {
    try {
      applyBlockFieldEdit(field.id, event.currentTarget.value);
    } catch (error) {
      notify(error.message, true);
      renderStrategyBlocks();
    }
  });
  row.append(label, control);
  return row;
}

function createStrategyBlockNode({ title, meta, tone, fields = [], actions = [] }, disabled) {
  const node = document.createElement("article");
  node.className = `strategy-block-node ${tone}`;
  const heading = document.createElement("div");
  heading.className = "strategy-block-title";
  const name = document.createElement("strong");
  name.textContent = title;
  const detail = document.createElement("span");
  detail.textContent = meta;
  heading.append(name, detail);
  node.append(heading);
  if (fields.length) {
    const list = document.createElement("div");
    list.className = "strategy-block-fields";
    fields.forEach(field => list.append(createBlockFieldControl(field, disabled)));
    node.append(list);
  }
  if (actions.length) {
    const flow = document.createElement("div");
    flow.className = "block-flow";
    actions.forEach((action, index) => {
      if (index) {
        const arrow = document.createElement("span");
        arrow.className = "block-flow-arrow";
        arrow.textContent = "→";
        flow.append(arrow);
      }
      const step = document.createElement("span");
      step.className = "block-flow-step";
      step.textContent = action;
      flow.append(step);
    });
    node.append(flow);
  }
  return node;
}

function createBlockLane(title, description, count, className = "") {
  const lane = document.createElement("section");
  lane.className = `block-lane ${className}`.trim();
  const heading = document.createElement("div");
  heading.className = "block-lane-heading";
  const copy = document.createElement("div");
  const name = document.createElement("h4");
  name.textContent = title;
  const summary = document.createElement("p");
  summary.textContent = description;
  const badge = document.createElement("span");
  badge.textContent = String(count);
  copy.append(name, summary);
  heading.append(copy, badge);
  lane.append(heading);
  return lane;
}

function renderStrategyBlocks() {
  if (!elements.blockCanvas) return;
  const source = elements.editor.value;
  const parsed = parseStrategyBlocks(source);
  const running = Boolean(selectedState()?.running);
  elements.blockCanvas.replaceChildren();
  elements.blockSummary.textContent = `${parsed.fields.length}개 설정 · ${parsed.schedules.length}개 일정 · ${parsed.handlers.length}개 이벤트`;
  state.blockSource = source;

  if (!parsed.fields.length && !parsed.schedules.length && !parsed.handlers.length) {
    const empty = document.createElement("div");
    empty.className = "block-empty";
    empty.textContent = "표시할 수 있는 블록이 없습니다.\n새 블록 전략을 만들거나 제공되는 설정 블록을 추가해 주세요.";
    elements.blockCanvas.append(empty);
    return;
  }

  const groups = new Map();
  parsed.fields.forEach(field => {
    if (!groups.has(field.category)) groups.set(field.category, []);
    groups.get(field.category).push(field);
  });
  const settingLane = createBlockLane("설정 블록", "전략 연결과 실행에 필요한 값을 편집합니다.", parsed.fields.length);
  for (const category of ["connection", "target", "risk", "schedule", "general"]) {
    const categoryFields = groups.get(category);
    if (!categoryFields?.length) continue;
    const [title, description] = BLOCK_CATEGORY_META[category];
    settingLane.append(createStrategyBlockNode({
      title,
      meta: `${categoryFields.length}개 값`,
      tone: category,
      fields: categoryFields,
      actions: category === "general" ? [description] : [],
    }, running));
  }
  elements.blockCanvas.append(settingLane);

  const scheduleLane = createBlockLane("일정 블록", "등록 순서대로 중앙 스케줄러가 실행합니다.", parsed.schedules.length);
  parsed.schedules.forEach(schedule => {
    scheduleLane.append(createStrategyBlockNode({
      title: schedule.title,
      meta: schedule.method,
      tone: "schedule",
      fields: schedule.fields,
    }, running));
  });
  if (!parsed.schedules.length) {
    scheduleLane.append(createStrategyBlockNode({
      title: "등록된 일정 없음",
      meta: "on_start",
      tone: "schedule",
      actions: ["일정 블록을 추가해 주세요"],
    }, running));
  }
  elements.blockCanvas.append(scheduleLane);

  if (parsed.handlers.length) {
    const eventLane = createBlockLane("이벤트 흐름", "제공된 블록이 실행되는 순서를 보여줍니다.", parsed.handlers.length, "events");
    const eventGrid = document.createElement("div");
    eventGrid.className = "block-event-grid";
    parsed.handlers.forEach(handler => {
      eventGrid.append(createStrategyBlockNode({
        title: handler.title,
        meta: handler.name,
        tone: "event",
        actions: handler.actions,
      }, running));
    });
    eventLane.append(eventGrid);
    elements.blockCanvas.append(eventLane);
  }
}

function setStrategyTab(tab) {
  const allowed = state.strategyType === "block"
    ? new Set(["overview", "blocks"])
    : new Set(["overview", "code"]);
  if (!allowed.has(tab)) tab = state.strategyType === "block" ? "blocks" : "code";
  state.strategyTab = tab;
  document.querySelectorAll("[data-strategy-tab]").forEach(button => {
    button.classList.toggle("active", button.dataset.strategyTab === tab);
  });
  elements.strategyOverview.hidden = tab !== "overview";
  elements.strategyBlocks.hidden = tab !== "blocks";
  elements.strategyCode.hidden = tab !== "code";
  if (tab === "overview" && state.selected) {
    refreshStrategySummary().catch(() => {});
  }
  if (tab === "blocks" && state.selected && state.blockSource !== elements.editor.value) {
    renderStrategyBlocks();
  }
}

function configureStrategyTabs() {
  const isBlock = state.strategyType === "block";
  elements.blockTab.hidden = !isBlock;
  elements.codeTab.hidden = isBlock;
  elements.strategyKindBadge.textContent = isBlock ? "BLK" : "PY";
  elements.strategyKindBadge.classList.toggle("block", isBlock);
  elements.editor.setAttribute(
    "aria-label",
    isBlock ? "블록 전략 자동 생성 코드" : "Python 전략 코드",
  );
}

function showWorkspaceView(view) {
  state.view = view;
  elements.dashboardView.hidden = view !== "dashboard";
  elements.strategyView.hidden = view !== "strategy";
  elements.dashboardButton.classList.toggle("active", view === "dashboard");
}

function updateControls() {
  const selected = state.view === "strategy" && Boolean(state.selected);
  const running = Boolean(selectedState()?.running);
  elements.editor.disabled = !selected || running || state.strategyType === "block";
  document.querySelectorAll(".block-field-control").forEach(control => {
    control.disabled = !selected || running;
  });
  elements.save.disabled = !selected || running;
  elements.remove.disabled = !selected || running;
  elements.run.disabled = !selected || running;
  elements.stop.disabled = !selected || !running;
  elements.orderPermission.disabled = !selected || !state.strategyPermission?.available;
  const orderArmed = Boolean(state.strategyPermission?.available && state.strategyPermission?.armed);
  elements.orderPermission.setAttribute("aria-checked", String(orderArmed));
  elements.orderPermission.classList.toggle("on", orderArmed);
  elements.orderPermissionState.textContent = !state.strategyPermission?.available
    ? "사용 불가"
    : orderArmed ? "켜짐" : "꺼짐";
  elements.orderPermission.title = !state.strategyPermission?.available
    ? "전략을 선택하면 주문 권한을 설정할 수 있습니다."
    : orderArmed ? "클릭하면 이 전략의 새 주문을 차단합니다." : "클릭하면 이 전략의 주문을 허용합니다.";
  elements.strategyState.textContent = !selected
    ? "선택된 전략 없음"
    : running ? "실행 중 · 편집하려면 먼저 중지하세요" : "중지됨 · 편집 가능";
  renderStrategyOverview();
}

function renderStrategies() {
  elements.count.textContent = String(state.strategies.length);
  elements.list.replaceChildren();
  const query = state.strategySearch.trim().toLocaleLowerCase("ko-KR");
  const visible = state.strategies.filter(strategy => (
    !query || displayStrategyName(strategy.name).toLocaleLowerCase("ko-KR").includes(query)
  ));
  for (const strategy of visible) {
    const button = document.createElement("button");
    button.className = `strategy-item${state.view === "strategy" && strategy.name === state.selected ? " active" : ""}${strategy.running ? " running" : ""}`;
    button.type = "button";
    const indicator = document.createElement("span");
    indicator.className = "indicator";
    const text = document.createElement("span");
    const name = document.createElement("span");
    name.className = "strategy-name";
    name.textContent = displayStrategyName(strategy.name);
    const meta = document.createElement("span");
    meta.className = "strategy-meta";
    const typeLabel = strategy.strategy_type === "block" ? "블록" : "스크립트";
    const runtimeLabel = strategy.running
      ? "실행 중"
      : strategy.stop_requested
        ? "사용자 중지"
        : strategy.exit_code == null ? "중지됨" : `종료 코드 ${strategy.exit_code}`;
    meta.textContent = `${typeLabel} · ${runtimeLabel}`;
    text.append(name, meta);
    button.append(indicator, text);
    button.addEventListener("click", () => openStrategy(strategy.name));
    elements.list.append(button);
  }
  if (!visible.length && state.strategies.length) {
    const empty = document.createElement("div");
    empty.className = "registered-empty";
    empty.textContent = "검색 결과가 없습니다.";
    elements.list.append(empty);
  }
  renderDashboard();
  updateControls();
}

async function refreshStrategies() {
  state.strategies = await api("/api/strategies");
  renderStrategies();
}

async function openStrategy(name) {
  if (name === state.selected && state.view === "strategy") return;
  if (state.dirty && !window.confirm("저장하지 않은 변경 사항이 있습니다. 저장하지 않고 다른 전략을 열까요?")) return;
  const payload = await api(`/api/strategies/${encodeURIComponent(name)}`);
  state.selected = name;
  state.strategyType = payload.strategy_type === "block" ? "block" : "script";
  state.strategySummary = null;
  showWorkspaceView("strategy");
  elements.fileName.textContent = displayStrategyName(name);
  configureStrategyTabs();
  elements.editor.value = payload.source;
  state.blockSource = null;
  setDirty(false);
  updateLineNumbers();
  setStrategyTab(state.strategyType === "block" ? "blocks" : "overview");
  state.logCursor = 0;
  resetActivity();
  await refreshStrategies();
  await refreshStrategyPermission();
  await Promise.all([refreshStrategySummary(), refreshLogs(), refreshOrders()]);
}

async function openDashboard() {
  if (state.view === "dashboard") {
    await refreshDashboard(true);
    return;
  }
  if (state.dirty && !window.confirm("저장하지 않은 변경 사항이 있습니다. 저장하지 않고 대시보드로 이동할까요?")) return;
  state.selected = null;
  state.strategyType = "script";
  state.strategyPermission = null;
  state.strategySummary = null;
  showWorkspaceView("dashboard");
  resetActivity("전략을 선택하면 실행 기록이 표시됩니다.");
  renderStrategies();
  renderDashboard();
  await refreshDashboard();
}

async function refreshStrategyPermission() {
  if (!state.selected) {
    state.strategyPermission = null;
    updateControls();
    return;
  }
  try {
    state.strategyPermission = await api(
      `/api/strategies/${encodeURIComponent(state.selected)}/order-permission`
    );
  } catch (_) {
    state.strategyPermission = null;
  }
  updateControls();
}

async function toggleStrategyPermission() {
  if (!state.selected || !state.strategyPermission?.available) return;
  const action = state.strategyPermission.armed ? "disarm-orders" : "arm-orders";
  await api(`/api/strategies/${encodeURIComponent(state.selected)}/${action}`, { method: "POST" });
  await refreshStrategyPermission();
  await refreshStrategySummary();
  notify(state.strategyPermission.armed ? "전략 주문을 허용했습니다." : "전략 주문을 차단했습니다.");
}

async function saveStrategy() {
  if (!state.selected) return;
  await api(`/api/strategies/${encodeURIComponent(state.selected)}`, {
    method: "PUT",
    body: JSON.stringify({
      source: elements.editor.value,
      strategy_type: state.strategyType,
    }),
  });
  setDirty(false);
  notify("전략을 저장했습니다.");
  await refreshStrategies();
  await refreshStrategySummary();
}

async function runStrategy() {
  if (!state.selected) return;
  if (state.dirty) await saveStrategy();
  await api(`/api/strategies/${encodeURIComponent(state.selected)}/start`, { method: "POST" });
  notify("전략을 시작했습니다.");
  await Promise.all([refreshStrategies(), refreshStatus()]);
  await refreshStrategyPermission();
  await refreshStrategySummary();
  await refreshLogs();
}

async function clearActivityLog() {
  if (!state.selected) {
    resetActivity("표시할 활동 기록이 없습니다.");
    return;
  }
  // Clearing is a per-browser view operation. Advance to the server's latest
  // monotonic cursor so a later strategy start only appends new activity.
  const payload = await api(
    `/api/strategies/${encodeURIComponent(state.selected)}/logs?after=${Number.MAX_SAFE_INTEGER}`
  );
  state.logCursor = payload.next;
  resetActivity("표시할 활동 기록이 없습니다.");
}

async function stopStrategy() {
  if (!state.selected) return;
  await api(`/api/strategies/${encodeURIComponent(state.selected)}/stop`, { method: "POST" });
  notify("전략을 중지했습니다.");
  await Promise.all([refreshStrategies(), refreshStatus()]);
  await refreshStrategyPermission();
  await refreshStrategySummary();
  await refreshLogs();
}

async function deleteStrategy() {
  if (!state.selected || !window.confirm(`${displayStrategyName(state.selected)} 전략을 삭제할까요?`)) return;
  await api(`/api/strategies/${encodeURIComponent(state.selected)}`, { method: "DELETE" });
  state.selected = null;
  state.strategySummary = null;
  elements.fileName.textContent = "전략을 선택하세요";
  elements.editor.value = "";
  resetActivity();
  setDirty(false);
  updateLineNumbers();
  showWorkspaceView("dashboard");
  await refreshStrategies();
}

async function refreshLogs() {
  if (!state.selected) return;
  const payload = await api(`/api/strategies/${encodeURIComponent(state.selected)}/logs?after=${state.logCursor}`);
  if (payload.lines.length) {
    const nearBottom = elements.log.scrollHeight - elements.log.scrollTop - elements.log.clientHeight < 48;
    const entries = payload.entries || payload.lines.map(message => ({ level: "output", message }));
    entries.forEach(appendActivity);
    if (nearBottom || state.logCursor === 0) elements.log.scrollTop = elements.log.scrollHeight;
  }
  state.logCursor = payload.next;
}

function orderStatusPresentation(status) {
  const values = {
    SUBMITTING: ["제출 중", "pending"],
    ACKNOWLEDGED: ["접수", "pending"],
    PARTIALLY_FILLED: ["부분 체결", "pending"],
    FILLED: ["체결", "positive"],
    CANCEL_PENDING: ["취소 중", "pending"],
    CANCELLED: ["취소", "negative"],
    REJECTED: ["거절", "negative"],
    UNKNOWN_RECONCILING: ["확인 필요", "negative"],
  };
  return values[status] || [status, ""];
}

function renderOrders(rows) {
  elements.orders.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "activity-empty";
    empty.textContent = "아직 저장된 주문이 없습니다.";
    elements.orders.append(empty);
    return;
  }
  for (const row of rows) {
    const request = row.request || {};
    const item = document.createElement("article");
    item.className = "order-item";

    const summary = document.createElement("div");
    summary.className = "order-summary";
    const title = document.createElement("strong");
    const side = request.side === "SELL" ? "매도" : "매수";
    title.textContent = `${request.code || "종목 미확인"} · ${side} ${request.quantity || "-"}주`;
    const [statusLabel, statusClass] = orderStatusPresentation(row.status);
    const badge = document.createElement("span");
    badge.className = `order-status ${statusClass}`.trim();
    badge.textContent = statusLabel;
    summary.append(title, badge);

    const meta = document.createElement("div");
    meta.className = "order-meta";
    const orderType = request.order_type === "LIMIT" ? `지정가 ${request.price || "-"}원` : "시장가";
    const created = row.created_at ? new Date(row.created_at).toLocaleTimeString("ko-KR", {
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }) : "";
    const route = request.route || "AUTO";
    const left = document.createElement("span");
    left.textContent = `${orderType} · ${route}`;
    const right = document.createElement("time");
    right.textContent = created;
    meta.append(left, right);
    item.append(summary, meta);

    if (row.message) {
      const message = document.createElement("div");
      message.className = "order-message";
      message.textContent = row.message;
      item.append(message);
    }
    elements.orders.append(item);
  }
}

async function refreshOrders() {
  const query = new URLSearchParams({ limit: "100" });
  if (state.strategyPermission?.strategy_id) {
    query.set("strategy_id", state.strategyPermission.strategy_id);
  }
  const rows = await api(`/api/runtime/orders?${query}`);
  renderOrders(rows);
}

function selectActivityTab(tab) {
  state.activityTab = tab;
  document.querySelectorAll("[data-activity-tab]").forEach(button => {
    const active = button.dataset.activityTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  elements.log.hidden = tab !== "logs";
  elements.orders.hidden = tab !== "orders";
  elements.clearLog.hidden = tab !== "logs";
  if (tab === "orders") refreshOrders().catch(error => notify(error.message, true));
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    state.systemStatus = status;
    state.instanceId = status.instance_id || state.instanceId;
    elements.serverDot.classList.remove("offline");
    elements.serverDot.classList.add("online");
    elements.serverDot.title = `서버 정상 · 실행 전략 ${status.running}개`;
    elements.serverDot.setAttribute("aria-label", elements.serverDot.title);
    if (status.unresolved_orders) {
      elements.runtime.hidden = false;
      elements.runtime.textContent = `확인 필요 주문 ${status.unresolved_orders}`;
    } else {
      elements.runtime.hidden = true;
      elements.runtime.textContent = "";
    }
    elements.connections.textContent = `API ${status.registered_apis} · 계좌 ${status.registered_accounts}`;
    elements.workspace.textContent = status.workspace;
    const generalVersion = document.querySelector("#general-version");
    const generalWorkspace = document.querySelector("#general-workspace");
    if (generalVersion) generalVersion.textContent = `v${status.version}`;
    if (generalWorkspace) generalWorkspace.textContent = status.workspace;
    renderDashboard();
    return status;
  } catch (_) {
    state.systemStatus = null;
    elements.serverDot.classList.remove("online");
    elements.serverDot.classList.add("offline");
    elements.serverDot.title = "서버 연결 끊김";
    elements.serverDot.setAttribute("aria-label", elements.serverDot.title);
    elements.runtime.hidden = false;
    elements.runtime.textContent = "서버 연결 끊김";
    elements.connections.textContent = "서버 확인 불가";
    renderDashboard();
    return null;
  }
}

function delay(milliseconds) {
  return new Promise(resolve => window.setTimeout(resolve, milliseconds));
}

async function waitForRestart(previousInstanceId, timeoutMilliseconds = 30000) {
  const deadline = Date.now() + timeoutMilliseconds;
  let sawDisconnect = false;
  while (Date.now() < deadline) {
    await delay(500);
    try {
      const status = await api("/api/status");
      const changed = Boolean(
        status.instance_id
        && (!previousInstanceId || status.instance_id !== previousInstanceId)
      );
      if (changed || (!status.instance_id && sawDisconnect)) {
        window.location.reload();
        return true;
      }
    } catch (_) {
      sawDisconnect = true;
      elements.runtime.textContent = "서버 재시작 중";
      elements.serverDot.classList.remove("online");
      elements.serverDot.classList.add("offline");
    }
  }
  return false;
}

function activateSettingsTab(tabName) {
  document.querySelectorAll("[data-settings-tab]").forEach(button => {
    button.classList.toggle("active", button.dataset.settingsTab === tabName);
  });
  document.querySelectorAll("[data-settings-panel]").forEach(panel => {
    panel.classList.toggle("active", panel.dataset.settingsPanel === tabName);
  });
}

function selectedProvider(providerId) {
  return state.providers.find(provider => provider.id === providerId);
}

function positiveNumberOrNull(selector) {
  const value = document.querySelector(selector).value.trim().replaceAll(",", "");
  return value ? value : null;
}

function formatRateLimits(rest, websocket) {
  const restText = rest
    ? `REST 초당 ${Number(rest.requests_per_second).toLocaleString()}회 · 최소 ${rest.minimum_interval_ms}ms`
    : "REST 한도 확인 전";
  const sessionText = websocket?.provider_max_sessions_per_key
    ? `키당 ${websocket.provider_max_sessions_per_key}세션`
    : `공용 ${websocket?.operating_max_sessions_per_key || 1}세션`;
  const websocketText = websocket
    ? `실시간 ${sessionText} · 세션당 ${websocket.max_subscriptions_per_session}구독 · 등록/해제 운영 초당 ${Number(websocket.control_messages_per_second).toLocaleString()}회`
    : "실시간 한도 확인 전";
  return `${restText} / ${websocketText}`;
}

function refreshConnectionRateHint() {
  const provider = selectedProvider(elements.connectionProvider.value);
  const target = provider?.targets?.find(item => item.id === elements.connectionTarget.value);
  const hint = document.querySelector("#connection-rate-limit-hint");
  hint.textContent = target
    ? `${formatRateLimits(target.rest_rate_limit, target.websocket_rate_limit)} · API 키별 자동 관리`
    : "고정 호출 한도를 불러오는 중입니다.";
}

function refreshProviderFields() {
  const provider = selectedProvider(elements.connectionProvider.value);
  document.querySelector("#connection-key-label").textContent = provider?.key_label || "App Key";
  document.querySelector("#connection-secret-label").textContent = provider?.secret_label || "App Secret";
  document.querySelector("#connection-realtime-user-field").hidden = provider?.id !== "kis";
  const previous = elements.connectionTarget.value;
  elements.connectionTarget.replaceChildren();
  for (const target of provider?.targets || []) {
    const option = document.createElement("option");
    option.value = target.id;
    option.textContent = target.name;
    elements.connectionTarget.append(option);
  }
  if ([...(provider?.targets || [])].some(target => target.id === previous)) {
    elements.connectionTarget.value = previous;
  }
  refreshConnectionRateHint();
}

function refreshAccountFields() {
  const connection = state.connections.find(item => item.id === elements.accountConnection.value);
  const provider = selectedProvider(connection?.provider);
  elements.productCodeField.hidden = !provider?.account_product_code;
  document.querySelector("#account-product-code").required = Boolean(provider?.account_product_code);
}

function renderConnectionList() {
  const list = document.querySelector("#connection-list");
  document.querySelector("#connection-count").textContent = `${state.connections.length}개`;
  list.replaceChildren();
  if (!state.connections.length) {
    const empty = document.createElement("div");
    empty.className = "registered-empty";
    empty.textContent = "등록된 API가 없습니다.";
    list.append(empty);
    return;
  }
  for (const connection of state.connections) {
    const provider = selectedProvider(connection.provider);
    const item = document.createElement("div");
    item.className = "registered-item";
    const information = document.createElement("div");
    information.className = "registered-copy";
    const title = document.createElement("strong");
    title.textContent = connection.label;
    const description = document.createElement("span");
    const targetName = provider?.targets?.find(target => target.id === connection.target)?.name || connection.target;
    const status = document.createElement("span");
    status.className = "connection-check-status";
    const savedStatus = state.connectionStatuses[connection.id];
    if (savedStatus?.state === "connected") {
      status.textContent = `연결됨 · 토큰 만료 ${new Date(savedStatus.expires_at).toLocaleString()}`;
      status.classList.add("connected");
    } else if (savedStatus?.state === "error") {
      status.textContent = savedStatus.message || "연결 확인 실패";
    } else {
      status.textContent = "연결 미확인";
    }
    const capability = state.capabilities.find(item => item.connection_id === connection.id);
    const enabled = [
      capability?.quotes && "현재가",
      capability?.bars && "봉",
      capability?.accounts && "잔고",
      capability?.order_history && "주문·체결 조회",
      capability?.realtime && "실시간",
      capability?.cash_orders && "주문",
    ].filter(Boolean);
    description.textContent = `${provider?.name || connection.provider} · ${targetName} · 인증정보 저장됨`;
    const capabilityText = document.createElement("span");
    capabilityText.textContent = enabled.length
      ? `현재 구현: ${enabled.join(" · ")}`
      : "이 증권사 어댑터는 아직 준비 중입니다.";
    const rateLimitText = document.createElement("span");
    rateLimitText.className = "rate-limit-summary";
    rateLimitText.textContent = `${formatRateLimits(connection.rest_rate_limit, connection.websocket_rate_limit)} · API 키별 자동 관리`;
    const actions = document.createElement("div");
    actions.className = "registered-actions";
    const check = document.createElement("button");
    check.className = "button quiet compact";
    check.type = "button";
    check.textContent = "연결 확인";
    check.addEventListener("click", () => checkConnection(connection, check, status));
    information.append(title, description, rateLimitText, capabilityText, status);
    const remove = document.createElement("button");
    remove.className = "registered-remove";
    remove.type = "button";
    remove.textContent = "삭제";
    remove.addEventListener("click", () => removeConnection(connection));
    actions.append(check, remove);
    item.append(information, actions);
    list.append(item);
  }
}

function renderAccountList() {
  const list = document.querySelector("#account-list");
  document.querySelector("#account-count").textContent = `${state.accounts.length}개`;
  list.replaceChildren();
  if (!state.accounts.length) {
    const empty = document.createElement("div");
    empty.className = "registered-empty";
    empty.textContent = "등록된 계좌가 없습니다.";
    list.append(empty);
    return;
  }
  for (const account of state.accounts) {
    const item = document.createElement("div");
    item.className = "registered-item";
    const information = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = account.alias;
    const description = document.createElement("span");
    const product = account.product_code ? `-${account.product_code}` : "";
    description.textContent = `${account.connection_label} · ${account.masked_account}${product}`;
    const limits = account.risk_limits || {};
    const limitSummary = document.createElement("span");
    const limitParts = [
      limits.max_order_amount && `1회 ${Number(limits.max_order_amount).toLocaleString()}원`,
      limits.max_daily_buy_amount && `일일 ${Number(limits.max_daily_buy_amount).toLocaleString()}원`,
      limits.max_exposure && `노출 ${Number(limits.max_exposure).toLocaleString()}원`,
      limits.max_open_orders && `미체결 ${limits.max_open_orders}건`,
    ].filter(Boolean);
    limitSummary.textContent = limitParts.length ? `계좌 상한 · ${limitParts.join(" · ")}` : "계좌 상한 미설정";
    const costs = account.cost_rates || {};
    const costSummary = document.createElement("span");
    costSummary.textContent = costs.buy_commission_rate != null && costs.sell_commission_rate != null && costs.sell_tax_rate != null
      ? `비용 반영 · 매수 ${costs.buy_commission_rate} / 매도 ${costs.sell_commission_rate} / 세금 ${costs.sell_tax_rate}`
      : "수수료·세율 미설정 · 전략 손익에 비용 미확정 표시";
    information.append(title, description, limitSummary, costSummary);
    const permission = state.orderPermissions.find(
      item => item.scope_type === "account" && item.scope_id === account.alias
    );
    const permissionText = document.createElement("span");
    permissionText.textContent = permission?.armed
      ? permission?.reconciled ? "주문 허용됨 · 계좌 조정 완료" : "주문 허용됨 · 첫 주문 전 계좌 조정"
      : "주문 차단됨";
    permissionText.className = permission?.armed && permission?.reconciled
      ? "connection-check-status connected"
      : "connection-check-status";
    information.append(permissionText);
    const actions = document.createElement("div");
    actions.className = "registered-actions";
    const permissionButton = document.createElement("button");
    permissionButton.className = `permission-toggle account-permission-toggle${permission?.armed ? " on" : ""}`;
    permissionButton.type = "button";
    permissionButton.setAttribute("role", "switch");
    permissionButton.setAttribute("aria-checked", String(Boolean(permission?.armed)));
    permissionButton.setAttribute("aria-label", `${account.alias} 계좌 주문 허용`);
    const permissionCopy = document.createElement("span");
    permissionCopy.className = "permission-toggle-copy";
    const permissionLabel = document.createElement("span");
    permissionLabel.textContent = "주문 허용";
    const permissionState = document.createElement("small");
    permissionState.textContent = permission?.armed ? "켜짐" : "꺼짐";
    permissionCopy.append(permissionLabel, permissionState);
    const permissionTrack = document.createElement("span");
    permissionTrack.className = "permission-toggle-track";
    permissionTrack.setAttribute("aria-hidden", "true");
    const permissionThumb = document.createElement("span");
    permissionThumb.className = "permission-toggle-thumb";
    permissionTrack.append(permissionThumb);
    permissionButton.append(permissionCopy, permissionTrack);
    permissionButton.addEventListener("click", () => toggleAccountPermission(account, Boolean(permission?.armed)));
    const remove = document.createElement("button");
    remove.className = "registered-remove";
    remove.type = "button";
    remove.textContent = "삭제";
    remove.addEventListener("click", () => removeAccount(account));
    actions.append(permissionButton, remove);
    item.append(information, actions);
    list.append(item);
  }
}

function renderSettings() {
  const currentProvider = elements.connectionProvider.value;
  elements.connectionProvider.replaceChildren();
  for (const provider of state.providers) {
    const option = document.createElement("option");
    option.value = provider.id;
    option.textContent = provider.name;
    elements.connectionProvider.append(option);
  }
  if (state.providers.some(provider => provider.id === currentProvider)) {
    elements.connectionProvider.value = currentProvider;
  }

  const currentConnection = elements.accountConnection.value;
  elements.accountConnection.replaceChildren();
  if (!state.connections.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "API를 먼저 등록하세요";
    elements.accountConnection.append(option);
  } else {
    for (const connection of state.connections) {
      const option = document.createElement("option");
      option.value = connection.id;
      option.textContent = `${connection.label} · ${selectedProvider(connection.provider)?.name || connection.provider}`;
      elements.accountConnection.append(option);
    }
    if (state.connections.some(connection => connection.id === currentConnection)) {
      elements.accountConnection.value = currentConnection;
    }
  }
  document.querySelector("#account-form .settings-submit").disabled = !state.connections.length;
  refreshProviderFields();
  refreshAccountFields();
  renderConnectionList();
  renderAccountList();
}

async function loadSettings() {
  [state.providers, state.connections, state.accounts, state.capabilities, state.orderPermissions] = await Promise.all([
    api("/api/settings/providers"),
    api("/api/settings/connections"),
    api("/api/settings/accounts"),
    api("/api/system/capabilities"),
    api("/api/runtime/order-permissions"),
  ]);
  const statusEntries = await Promise.all(state.connections.map(async connection => {
    try {
      const value = await api(`/api/settings/connections/${encodeURIComponent(connection.id)}/status`);
      return [connection.id, value];
    } catch (_) {
      return [connection.id, null];
    }
  }));
  state.connectionStatuses = Object.fromEntries(statusEntries);
  renderSettings();
}

async function openSettings(tabName = "general") {
  await loadSettings();
  activateSettingsTab(tabName);
  elements.settingsModal.hidden = false;
  const focusTarget = tabName === "general"
    ? document.querySelector("#update-button")
    : tabName === "apis" ? elements.connectionProvider : elements.accountConnection;
  window.setTimeout(() => focusTarget?.focus(), 0);
}

function closeSettings() {
  elements.settingsModal.hidden = true;
}

async function removeConnection(connection) {
  if (!window.confirm(`${connection.label} API 등록을 삭제할까요?`)) return;
  try {
    await api(`/api/settings/connections/${encodeURIComponent(connection.id)}`, { method: "DELETE" });
    await Promise.all([loadSettings(), refreshStatus()]);
    notify("API 등록을 삭제했습니다.");
  } catch (error) { notify(error.message, true); }
}

async function checkConnection(connection, button, status) {
  button.disabled = true;
  button.textContent = "확인 중";
  status.textContent = "증권사 인증 확인 중";
  try {
    const result = await api(`/api/settings/connections/${encodeURIComponent(connection.id)}/check`, { method: "POST" });
    state.connectionStatuses[connection.id] = result;
    status.textContent = `연결됨 · 토큰 만료 ${new Date(result.expires_at).toLocaleString()}`;
    status.classList.add("connected");
    notify(`${connection.label} 연결을 확인했습니다.`);
    await refreshStatus();
  } catch (error) {
    const detail = typeof error.message === "string" ? error.message : "연결 확인에 실패했습니다.";
    status.textContent = detail;
    status.classList.remove("connected");
    notify(detail, true);
  } finally {
    button.disabled = false;
    button.textContent = "연결 확인";
  }
}

async function removeAccount(account) {
  if (!window.confirm(`${account.alias} 계좌 등록을 삭제할까요?`)) return;
  try {
    await api(`/api/settings/accounts/${encodeURIComponent(account.id)}`, { method: "DELETE" });
    await Promise.all([loadSettings(), refreshStatus(), refreshDashboard(true)]);
    notify("계좌 등록을 삭제했습니다.");
  } catch (error) { notify(error.message, true); }
}

async function toggleAccountPermission(account, armed) {
  if (!armed && account.target === "LIVE") {
    const confirmed = window.confirm(
      `${account.alias}은 실전 계좌입니다. 잔고와 주문내역을 확인한 뒤 실제 주문을 허용할까요?`
    );
    if (!confirmed) return;
  }
  const action = armed ? "disarm" : "arm";
  try {
    await api(`/api/runtime/accounts/${encodeURIComponent(account.alias)}/${action}`, {
      method: "POST",
      body: JSON.stringify({ confirmation: account.target === "LIVE" ? "LIVE" : "" }),
    });
    await Promise.all([loadSettings(), refreshDashboard(true)]);
    notify(armed ? "계좌 주문을 차단했습니다." : "계좌 조정을 마치고 주문을 허용했습니다.");
  } catch (error) {
    notify(error.message, true);
  }
}

function updateLineNumbers() {
  const count = elements.editor.value.split("\n").length;
  elements.lines.textContent = Array.from({ length: count }, (_, index) => index + 1).join("\n");
}

elements.editor.addEventListener("input", () => {
  setDirty(true);
  state.blockSource = null;
  updateLineNumbers();
});
elements.editor.addEventListener("scroll", () => { elements.lines.scrollTop = elements.editor.scrollTop; });
elements.editor.addEventListener("keydown", event => {
  if (event.key === "Tab") {
    event.preventDefault();
    const start = elements.editor.selectionStart;
    const end = elements.editor.selectionEnd;
    elements.editor.setRangeText("    ", start, end, "end");
    elements.editor.dispatchEvent(new Event("input"));
  }
});

function scriptStrategyTemplate(strategyId) {
  return `"""Trade Router 자동매매 전략"""

from trade_router import Strategy, StrategyConfig


class NewStrategy(Strategy):
    config = StrategyConfig(
        id="${strategyId}",
        trading_account="등록한 계좌 별칭",
        market_sources={"primary": "등록한 API 연결 이름"},
        required_capabilities={"primary": ("quotes", "bars")},
    )

    async def on_start(self, ctx):
        ctx.scheduler.on_bar_close(
            "3m",
            schedule_id="morning-3m",
            between=("09:00", "10:00"),
        )
        ctx.log.info("전략을 시작했습니다.")

    async def on_bar(self, ctx, event):
        bars = await ctx.market.bars("005930", "3m", count=55)
        if bars:
            ctx.log.info(f"완성 3분봉 {len(bars)}개 · 최근 종가 {bars[-1].close}")

    async def on_stop(self, ctx):
        ctx.log.info("전략을 종료합니다.")
`;
}

function blockStrategyTemplate(strategyId) {
  return `"""Trade Router가 관리하는 블록 전략 실행 파일"""

from trade_router import Strategy, StrategyConfig


STRATEGY_ID = "${strategyId}"
TRADING_ACCOUNT = "등록한 계좌 별칭"
MARKET_SOURCE = "등록한 API 연결 이름"
SYMBOL = "005930"
BAR_INTERVAL = "3m"
BAR_COUNT = 55


class ManagedBlockStrategy(Strategy):
    config = StrategyConfig(
        id=STRATEGY_ID,
        trading_account=TRADING_ACCOUNT,
        market_sources={"primary": MARKET_SOURCE},
        required_capabilities={"primary": ("quotes", "bars")},
    )

    async def on_start(self, ctx):
        ctx.scheduler.on_bar_close(
            BAR_INTERVAL,
            schedule_id="regular-bars",
            between=("09:00", "15:20"),
        )
        ctx.log.info("블록 전략을 시작했습니다.")

    async def on_bar(self, ctx, event):
        bars = await ctx.market.bars(SYMBOL, BAR_INTERVAL, count=BAR_COUNT)
        if bars:
            ctx.log.info(f"완성 봉 {len(bars)}개 · 최근 종가 {bars[-1].close}")

    async def on_stop(self, ctx):
        ctx.log.info("블록 전략을 종료합니다.")
`;
}

function openNewStrategyModal() {
  elements.newStrategyModal.hidden = false;
  const input = document.querySelector("#new-strategy-name");
  input.value = "";
  const scriptType = document.querySelector('input[name="strategy-type"][value="script"]');
  if (scriptType) scriptType.checked = true;
  window.setTimeout(() => input.focus(), 0);
}

function closeNewStrategyModal() {
  elements.newStrategyModal.hidden = true;
}

document.querySelector("#new-strategy-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.querySelector("#new-strategy-name");
  const strategyType = document.querySelector('input[name="strategy-type"]:checked')?.value === "block"
    ? "block"
    : "script";
  const displayName = input.value.trim().replace(/\.py$/i, "");
  if (!displayName) return;
  const name = `${displayName}.py`;
  const strategyId = displayName
    .normalize("NFKC")
    .toLocaleLowerCase("ko-KR")
    .replace(/[^\p{L}\p{N}_-]+/gu, "-")
    .replace(/^-+|-+$/g, "") || `strategy-${Date.now()}`;
  try {
    await api(`/api/strategies/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({
        strategy_type: strategyType,
        source: strategyType === "block"
          ? blockStrategyTemplate(strategyId)
          : scriptStrategyTemplate(strategyId),
      }),
    });
    closeNewStrategyModal();
    await refreshStrategies();
    await openStrategy(name);
  } catch (error) { notify(error.message, true); }
});

elements.dashboardButton.addEventListener("click", () => {
  openDashboard().catch(error => notify(error.message, true));
});
elements.dashboardRefresh.addEventListener("click", () => {
  refreshDashboard(true).catch(error => notify(error.message, true));
});
document.querySelector("#strategy-search-button").addEventListener("click", () => {
  elements.strategySearch.hidden = false;
  elements.strategySearchInput.focus();
});
document.querySelector("#strategy-search-close").addEventListener("click", () => {
  state.strategySearch = "";
  elements.strategySearchInput.value = "";
  elements.strategySearch.hidden = true;
  renderStrategies();
});
elements.strategySearchInput.addEventListener("input", event => {
  state.strategySearch = event.currentTarget.value;
  renderStrategies();
});
document.querySelector("#new-strategy-button").addEventListener("click", openNewStrategyModal);
document.querySelector("#new-strategy-close").addEventListener("click", closeNewStrategyModal);
document.querySelector("#new-strategy-cancel").addEventListener("click", closeNewStrategyModal);
elements.newStrategyModal.addEventListener("click", event => {
  if (event.target === elements.newStrategyModal) closeNewStrategyModal();
});
document.querySelectorAll("[data-strategy-tab]").forEach(button => {
  button.addEventListener("click", () => setStrategyTab(button.dataset.strategyTab));
});

elements.save.addEventListener("click", () => saveStrategy().catch(error => notify(error.message, true)));
elements.run.addEventListener("click", () => runStrategy().catch(error => notify(error.message, true)));
elements.stop.addEventListener("click", () => stopStrategy().catch(error => notify(error.message, true)));
elements.orderPermission.addEventListener("click", () => toggleStrategyPermission().catch(error => notify(error.message, true)));
elements.remove.addEventListener("click", () => deleteStrategy().catch(error => notify(error.message, true)));
document.querySelector("#clear-log-button").addEventListener("click", () => {
  clearActivityLog().catch(error => notify(error.message, true));
});
document.querySelector("#update-button").addEventListener("click", async () => {
  const button = document.querySelector("#update-button");
  const status = document.querySelector("#update-status");
  if (button.dataset.ready === "true") {
    if (!window.confirm("업데이트 파일을 적용하기 위해 실행 중인 전략을 중지하고 다시 시작할까요?")) return;
    button.disabled = true;
    button.textContent = "다운로드 중";
    status.textContent = "업데이트 파일을 내려받아 SHA-256 무결성을 확인하고 있습니다.";
    const previousInstanceId = state.instanceId;
    try {
      const result = await api("/api/updates/apply", { method: "POST" });
      status.textContent = result.message;
      closeSettings();
      notify("업데이트를 적용하고 다시 시작합니다.");
      const restarted = await waitForRestart(previousInstanceId, 120000);
      if (!restarted) throw new Error("업데이트 후 다시 시작된 서버를 확인하지 못했습니다. CMD 상태를 확인하세요.");
    } catch (error) {
      button.disabled = false;
      button.textContent = "업데이트 시작";
      status.textContent = error.message;
      notify(error.message, true);
    }
    return;
  }
  button.disabled = true;
  button.textContent = "확인 중";
  try {
    const result = await api("/api/updates/check");
    status.textContent = result.message;
    if (result.available && result.can_apply) {
      button.dataset.ready = "true";
      button.textContent = "업데이트 시작";
    } else {
      delete button.dataset.ready;
      button.textContent = "업데이트 확인";
    }
    notify(result.message);
  } catch (error) {
    status.textContent = error.message;
    notify(error.message, true);
    delete button.dataset.ready;
    button.textContent = "업데이트 확인";
  }
  finally { button.disabled = false; }
});
document.querySelector("#restart-button").addEventListener("click", async () => {
  if (!window.confirm("실행 중인 전략을 중지하고 Trade Router를 재시작할까요?")) return;
  const button = document.querySelector("#restart-button");
  button.disabled = true;
  button.textContent = "재시작 중";
  const previousInstanceId = state.instanceId;
  try {
    await api("/api/system/restart", { method: "POST" });
    closeSettings();
    notify("Trade Router를 재시작하고 있습니다.");
    const restarted = await waitForRestart(previousInstanceId);
    if (!restarted) throw new Error("재시작된 서버를 확인하지 못했습니다. CMD 상태를 확인하세요.");
  } catch (error) {
    button.disabled = false;
    button.textContent = "재시작";
    notify(error.message, true);
  }
});
document.querySelector("#shutdown-button").addEventListener("click", async () => {
  if (!window.confirm("실행 중인 전략을 중지하고 Trade Router를 종료할까요?")) return;
  const button = document.querySelector("#shutdown-button");
  button.disabled = true;
  button.textContent = "종료 중";
  try {
    await api("/api/system/shutdown", { method: "POST" });
    closeSettings();
    notify("Trade Router를 종료하고 있습니다.");
  } catch (error) {
    button.disabled = false;
    button.textContent = "종료";
    notify(error.message, true);
  }
});
document.querySelector("#settings-button").addEventListener("click", () => {
  openSettings().catch(error => notify(error.message, true));
});
document.querySelectorAll("[data-activity-tab]").forEach(button => {
  button.addEventListener("click", () => selectActivityTab(button.dataset.activityTab));
});
document.querySelector("#settings-close").addEventListener("click", closeSettings);
elements.settingsModal.addEventListener("click", event => {
  if (event.target === elements.settingsModal) closeSettings();
});
document.querySelectorAll("[data-settings-tab]").forEach(button => {
  button.addEventListener("click", () => activateSettingsTab(button.dataset.settingsTab));
});
elements.connectionProvider.addEventListener("change", refreshProviderFields);
elements.connectionTarget.addEventListener("change", refreshConnectionRateHint);
elements.accountConnection.addEventListener("change", refreshAccountFields);

document.querySelector("#connection-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector(".settings-submit");
  submit.disabled = true;
  try {
    const created = await api("/api/settings/connections?check=true", {
      method: "POST",
      body: JSON.stringify({
        provider: elements.connectionProvider.value,
        target: elements.connectionTarget.value,
        label: document.querySelector("#connection-label").value,
        app_key: document.querySelector("#connection-key").value,
        app_secret: document.querySelector("#connection-secret").value,
        realtime_user_id: document.querySelector("#connection-realtime-user-id").value,
      }),
    });
    document.querySelector("#connection-label").value = "";
    document.querySelector("#connection-key").value = "";
    document.querySelector("#connection-secret").value = "";
    document.querySelector("#connection-realtime-user-id").value = "";
    await Promise.all([loadSettings(), refreshStatus(), refreshDashboard(true)]);
    if (created.connection_status?.state === "connected") {
      notify("API를 저장하고 연결까지 확인했습니다.");
    } else {
      notify(created.connection_status?.message || "API는 저장했지만 연결 확인에 실패했습니다.", true);
    }
  } catch (error) { notify(error.message, true); }
  finally { submit.disabled = false; }
});

document.querySelector("#account-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector(".settings-submit");
  submit.disabled = true;
  try {
    await api("/api/settings/accounts", {
      method: "POST",
      body: JSON.stringify({
        connection_id: elements.accountConnection.value,
        alias: document.querySelector("#account-alias").value,
        account_number: document.querySelector("#account-number").value,
        product_code: document.querySelector("#account-product-code").value,
        max_order_amount: positiveNumberOrNull("#account-max-order"),
        max_daily_buy_amount: positiveNumberOrNull("#account-max-daily-buy"),
        max_exposure: positiveNumberOrNull("#account-max-exposure"),
        max_open_orders: positiveNumberOrNull("#account-max-open-orders"),
        buy_commission_rate: positiveNumberOrNull("#account-buy-commission-rate"),
        sell_commission_rate: positiveNumberOrNull("#account-sell-commission-rate"),
        sell_tax_rate: positiveNumberOrNull("#account-sell-tax-rate"),
      }),
    });
    document.querySelector("#account-alias").value = "";
    document.querySelector("#account-number").value = "";
    document.querySelector("#account-product-code").value = "";
    document.querySelector("#account-max-order").value = "";
    document.querySelector("#account-max-daily-buy").value = "";
    document.querySelector("#account-max-exposure").value = "";
    document.querySelector("#account-max-open-orders").value = "";
    document.querySelector("#account-buy-commission-rate").value = "";
    document.querySelector("#account-sell-commission-rate").value = "";
    document.querySelector("#account-sell-tax-rate").value = "";
    await Promise.all([loadSettings(), refreshStatus(), refreshDashboard(true)]);
    notify("계좌를 등록했습니다.");
  } catch (error) { notify(error.message, true); }
  finally { submit.disabled = !state.connections.length; }
});

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !elements.newStrategyModal.hidden) {
    closeNewStrategyModal();
    return;
  }
  if (event.key === "Escape" && !elements.settingsModal.hidden) {
    closeSettings();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    if (!elements.save.disabled) saveStrategy().catch(error => notify(error.message, true));
  }
});
window.addEventListener("beforeunload", event => {
  if (!state.dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

async function initialize() {
  updateClock();
  window.setInterval(updateClock, 1000);
  try {
    await Promise.all([refreshStatus(), refreshStrategies(), refreshDashboard()]);
    showWorkspaceView("dashboard");
    renderDashboard();
    resetActivity("전략을 선택하면 실행 기록이 표시됩니다.");
  } catch (error) { notify(error.message, true); }
  window.setInterval(() => refreshStatus(), 2000);
  window.setInterval(() => {
    if (state.view === "dashboard") refreshDashboard().catch(() => {});
    else if (state.view === "strategy" && state.strategyTab === "overview") {
      refreshStrategySummary().catch(() => {});
    }
  }, 30000);
  window.setInterval(async () => {
    try {
      await refreshStrategies();
      await refreshStrategyPermission();
      await refreshLogs();
      if (state.activityTab === "orders") await refreshOrders();
    } catch (_) {}
  }, 1000);
}

initialize();
