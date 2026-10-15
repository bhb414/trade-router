const LOG_CLEAR_STORAGE_KEY = "trade-router.log-clear-cursors.v1";

const state = {
  view: "dashboard",
  selected: null,
  strategyType: "script",
  strategyTab: "overview",
  blockSource: null,
  blockPackage: null,
  blockConditions: [],
  blockConditionKey: null,
  blockConditionError: null,
  blockGraphRedraw: null,
  strategySearch: "",
  dirty: false,
  strategies: [],
  providers: [],
  connections: [],
  accounts: [],
  selectedConnectionId: null,
  selectedAccountId: null,
  connectionFormMode: null,
  accountFormMode: null,
  connectionStatuses: {},
  capabilities: [],
  orderPermissions: [],
  strategyPermission: null,
  activityTab: "logs",
  logCursor: 0,
  clearedLogCursors: {},
  logCursorInstanceId: null,
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
  blockExport: document.querySelector("#block-export-button"),
  blockZoomOut: document.querySelector("#block-zoom-out"),
  blockZoomLabel: document.querySelector("#block-zoom-label"),
  blockZoomIn: document.querySelector("#block-zoom-in"),
  blockFit: document.querySelector("#block-fit"),
  blockAutoLayout: document.querySelector("#block-auto-layout"),
  blockImportFile: document.querySelector("#block-import-file"),
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
  serviceDetailModal: document.querySelector("#service-detail-modal"),
  serviceDetailBody: document.querySelector("#service-detail-body"),
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

function restoreLogClearCursors(instanceId) {
  const normalizedInstanceId = String(instanceId || "");
  if (!normalizedInstanceId || state.logCursorInstanceId === normalizedInstanceId) return;
  const cursors = {};
  try {
    const saved = JSON.parse(window.localStorage.getItem(LOG_CLEAR_STORAGE_KEY) || "null");
    if (saved?.instanceId === normalizedInstanceId && saved.cursors && typeof saved.cursors === "object") {
      Object.entries(saved.cursors).forEach(([name, cursor]) => {
        const value = Number(cursor);
        if (Number.isSafeInteger(value) && value >= 0) cursors[name] = value;
      });
    }
  } catch (_) {}
  state.logCursorInstanceId = normalizedInstanceId;
  state.clearedLogCursors = cursors;
}

function persistLogClearCursors() {
  if (!state.logCursorInstanceId) return;
  try {
    window.localStorage.setItem(LOG_CLEAR_STORAGE_KEY, JSON.stringify({
      instanceId: state.logCursorInstanceId,
      cursors: state.clearedLogCursors,
    }));
  } catch (_) {}
}

function rememberLogClearCursor(name, cursor) {
  const value = Number(cursor);
  if (!name || !Number.isSafeInteger(value) || value < 0) return;
  state.clearedLogCursors[name] = value;
  persistLogClearCursors();
}

function forgetLogClearCursor(name) {
  if (!(name in state.clearedLogCursors)) return;
  delete state.clearedLogCursors[name];
  persistLogClearCursors();
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

function serviceDetailValue(value) {
  if (value == null || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function openServiceEventDetail(event, service) {
  document.querySelector("#service-detail-eyebrow").textContent = service.label;
  document.querySelector("#service-detail-title").textContent = serviceOperationLabel(event.operation);
  elements.serviceDetailBody.replaceChildren();
  const summary = document.createElement("div");
  summary.className = "service-detail-summary";
  const occurredAt = event.occurred_at ? formatTimestamp(event.occurred_at) : "—";
  for (const [label, value, className] of [
    ["처리 시각", occurredAt, ""],
    ["결과", event.outcome === "error" ? "오류" : "성공", event.outcome === "error" ? "negative" : "positive"],
    ["대상", serviceEventTarget(event), ""],
    ["소요 시간", event.duration_ms == null ? "—" : `${formatNumber(event.duration_ms)}ms`, ""],
    ["전략·호출 주체", event.source || "—", ""],
    ["기록 번호", event.id == null ? "—" : String(event.id), ""],
  ]) {
    const cell = document.createElement("div");
    const caption = document.createElement("span");
    caption.textContent = label;
    const content = document.createElement("strong");
    content.textContent = value;
    if (className) content.className = className;
    cell.append(caption, content);
    summary.append(cell);
  }
  const message = document.createElement("p");
  message.className = "service-detail-message";
  message.textContent = event.message || "상세 메시지가 없습니다.";
  const fields = document.createElement("dl");
  fields.className = "service-detail-fields";
  const details = Object.entries(event.details || {}).filter(([key]) => key !== "duration_ms");
  if (!details.length) {
    const empty = document.createElement("div");
    empty.className = "service-tab-empty";
    empty.textContent = "추가 진단 값이 없습니다.";
    elements.serviceDetailBody.append(summary, message, empty);
  } else {
    for (const [key, value] of details) {
      const row = document.createElement("div");
      row.className = "service-detail-field";
      const term = document.createElement("dt");
      term.textContent = key;
      const description = document.createElement("dd");
      description.textContent = serviceDetailValue(value);
      row.append(term, description);
      fields.append(row);
    }
    elements.serviceDetailBody.append(summary, message, fields);
  }
  elements.serviceDetailModal.hidden = false;
  document.querySelector("#service-detail-close").focus();
}

function closeServiceEventDetail() {
  elements.serviceDetailModal.hidden = true;
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
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-label", `${serviceOperationLabel(event.operation)} 상세 보기`);
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
      row.addEventListener("click", () => openServiceEventDetail(event, result));
      row.addEventListener("keydown", keyEvent => {
        if (keyEvent.key === "Enter" || keyEvent.key === " ") {
          keyEvent.preventDefault();
          openServiceEventDetail(event, result);
        }
      });
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
  CONDITION_SOURCE: ["connection", "종목·시세 데이터 연결"],
  MARKET_VENUE: ["connection", "시세·봉 시장"],
  TARGET_SOURCE: ["target", "종목 데이터 방식"],
  HTS_USER_ID: ["connection", "HTS 사용자 ID"],
  CONDITION_ID: ["target", "조건식 ID"],
  EXPECTED_CONDITION_NAME: ["target", "조건식 이름"],
  RANKING_KIND: ["target", "순위 종류"],
  RANKING_VENUE: ["target", "조회 시장"],
  RANKING_MARKET: ["target", "시장 구분"],
  RANKING_LIMIT: ["target", "상위 종목 수"],
  SYMBOLS: ["target", "지정 종목"],
  SYMBOL: ["target", "종목 코드"],
  CODE: ["target", "종목 코드"],
  ACTIVE_DATE: ["schedule", "실행 날짜"],
  BUY_BUDGET_PER_SYMBOL: ["risk", "종목당 매수 예산"],
  TOTAL_OPERATING_BUDGET: ["risk", "총 운용 예산"],
  MAX_NEW_BUYS_PER_SCAN: ["risk", "조회당 신규 매수 수"],
  BOLLINGER_PERIOD: ["risk", "볼린저 기간"],
  BOLLINGER_STDDEV: ["risk", "볼린저 표준편차"],
  BAR_INTERVAL: ["schedule", "봉 주기"],
  BAR_DATA_MODE: ["schedule", "봉 상태"],
  BAR_COUNT: ["risk", "조회 봉 수"],
  TAKE_PROFIT_MULTIPLIER: ["risk", "익절 기준(1.03 = +3%)"],
  STOP_LOSS_MULTIPLIER: ["risk", "손절 기준(0.97 = -3%)"],
  USE_BOLLINGER_EXIT: ["risk", "볼린저 청산"],
  USE_TAKE_PROFIT: ["risk", "익절"],
  USE_STOP_LOSS: ["risk", "손절"],
  BUY_SCAN_SECONDS: ["schedule", "조건검색 간격(초)"],
  BUY_START_TIME: ["schedule", "매수 시작 시각"],
  BUY_END_TIME: ["schedule", "매수 종료 시각"],
  SELL_START_TIME: ["schedule", "매도 판단 시작"],
  SELL_END_TIME: ["schedule", "매도 판단 종료"],
  CLOSE_TIME: ["schedule", "전량 청산 시각"],
  CLOSE_RETRY_START: ["schedule", "청산 재확인 시작"],
  CLOSE_RETRY_END: ["schedule", "청산 재확인 종료"],
  CLOSE_CONFIRM_TIME: ["schedule", "최종 확인 시각"],
  ORDER_ROUTE: ["connection", "주문 경로"],
  BUY_ORDER_TYPE: ["risk", "매수 주문 방식"],
  SELL_ORDER_TYPE: ["risk", "매도 주문 방식"],
  BUY_CONDITION_LOGIC: ["risk", "매수 조건 결합"],
  SELL_CONDITION_LOGIC: ["risk", "매도 조건 결합"],
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
  const templateMatch = source.match(/^BLOCK_TEMPLATE\s*=\s*(["'])(.*?)\1\s*$/m);
  const template = templateMatch?.[2] || "bar-observer-v1";

  for (const match of source.matchAll(/^([A-Z][A-Z0-9_]*)[ \t]*=[ \t]*(.+?)[ \t]*$/gm)) {
    const name = match[1];
    if (name === "BLOCK_TEMPLATE" || ignoredSecret.test(name)) continue;
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
      meta: ({ every: "반복 실행", on_bar_close: "완성 봉마다", daily_at: "지정 시각" }[method] || "중앙 일정"),
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
    [/ctx\.account\.buying_power\s*\(/g, "주문 가능 수량 계산"],
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
    // Block strategies are rendered from a fixed, validated template. Lifecycle
    // methods that only contain router-managed setup, logging, or shutdown work
    // are implementation details rather than user-authored blocks.
    if (!actions.length) return;
    handlers.push({
      id: `handler:${match[1]}`,
      name: match[1],
      title: BLOCK_EVENT_LABELS[match[1]] || match[1],
      actions,
      line: sourceLineNumber(source, match.index),
    });
  });

  if (template === "condition-bollinger-v1") {
    const schedulePresentation = {
      "condition-scan": ["종목 조회·매수", "설정한 초 간격·정각 맞춤"],
      "bollinger-sell": ["보유종목 매도", "완성 봉마다"],
      "close-all": ["장 마감 전량 청산", "지정 시각"],
      "close-retry": ["청산 재확인", "30초 간격"],
      "close-confirm": ["최종 잔여 확인", "지정 시각"],
    };
    schedules.forEach(schedule => {
      const presentation = schedulePresentation[schedule.title];
      if (!presentation) return;
      [schedule.title, schedule.meta] = presentation;
      schedule.fields = [];
    });
    handlers.splice(0, handlers.length,
      {
        id: "workflow:buy",
        name: "30초 주기",
        title: "종목 조회·매수",
        vertical: true,
        actions: [
          "종목 데이터 조회",
          "잔고·미체결 확인",
          "보유·주문 종목 제외",
          "주문 가능 수량 계산",
          "종목당·총 운용 예산 확인",
          "선택한 방식으로 매수",
        ],
      },
      {
        id: "workflow:sell",
        name: "완성 3분봉",
        title: "보유종목 매도",
        vertical: true,
        actions: [
          "전략 보유종목 확인",
          "완성 봉 조회",
          "가격·지표 값 계산",
          "AND·OR 조건 비교",
          "선택한 방식으로 매도",
        ],
      },
      {
        id: "workflow:close",
        name: "장 마감",
        title: "전량 청산",
        vertical: true,
        actions: [
          "잔고·미체결 확인",
          "전략 소유 수량 확인",
          "시장가 매도",
          "재확인",
        ],
      },
    );
  }

  return { fields, schedules, handlers, template };
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

const BLOCK_PACKAGE_FIELD_TYPES = {
  bar_count: "number",
  max_new_buys_per_scan: "number",
  ranking_limit: "number",
  bollinger_period: "number",
  buy_scan_seconds: "number",
  buy_budget_per_symbol: "decimal",
  total_operating_budget: "decimal",
  bollinger_stddev: "decimal",
  take_profit_multiplier: "decimal",
  stop_loss_multiplier: "decimal",
  use_bollinger_exit: "boolean",
  use_take_profit: "boolean",
  use_stop_loss: "boolean",
  symbols: "symbols",
};

const BLOCK_PACKAGE_SOURCE_NAMES = {
  route: "ORDER_ROUTE",
};

function packageFieldSourceName(key) {
  return BLOCK_PACKAGE_SOURCE_NAMES[key] || key.toUpperCase();
}

function blockPackageFields() {
  const settings = state.blockPackage?.settings;
  if (!settings) return [];
  const custom = new Set(["buy_conditions", "sell_conditions"]);
  const legacy = new Set([
    "use_bollinger_exit", "use_take_profit", "use_stop_loss",
    "take_profit_multiplier", "stop_loss_multiplier",
  ]);
  return Object.entries(settings)
    .filter(([key]) => key !== "expected_condition_name" && !custom.has(key) && !legacy.has(key))
    .map(([settingKey, value]) => {
      const sourceName = packageFieldSourceName(settingKey);
      const [category, label] = genericConstantMeta(sourceName);
      return {
        id: `setting:${settingKey}`,
        settingKey,
        sourceName,
        label,
        category,
        type: BLOCK_PACKAGE_FIELD_TYPES[settingKey] || "string",
        value,
      };
    });
}

function currentBlockModel() {
  const parsed = parseStrategyBlocks(elements.editor.value);
  if (!state.blockPackage) return parsed;
  return {
    ...parsed,
    template: state.blockPackage.template,
    fields: blockPackageFields(),
  };
}

function blockFieldOptions(field) {
  const settings = state.blockPackage?.settings || {};
  if (field.settingKey === "trading_account") {
    return state.accounts.map(account => ({
      value: account.alias,
      label: `${account.alias} · ${account.connection_label || account.provider || "계좌"}`,
    }));
  }
  if (field.settingKey === "condition_source" || field.settingKey === "market_source") {
    return state.connections
      .filter(connection => {
        if (field.settingKey === "market_source") return true;
        const capability = state.capabilities.find(item => item.connection_id === connection.id);
        if (settings.target_source === "HTS_CONDITION") return Boolean(capability?.conditions);
        if (settings.target_source === "RANKING") return Boolean(capability?.rankings);
        return Boolean(capability?.quotes && capability?.bars);
      })
      .map(connection => ({
        value: connection.label,
        label: `${connection.label} · ${selectedProvider(connection.provider)?.name || connection.provider}`,
      }));
  }
  if (field.settingKey === "hts_user_id") {
    const selected = state.connections.find(connection => (
      connection.id === settings.condition_source || connection.label === settings.condition_source
    ));
    const values = selected?.realtime_user_id
      ? [selected]
      : state.connections.filter(connection => connection.realtime_user_id);
    return values.map(connection => ({
      value: connection.realtime_user_id,
      label: `${connection.realtime_user_id} · ${connection.label}`,
    }));
  }
  if (field.settingKey === "condition_id") {
    return state.blockConditions.map(condition => ({
      value: condition.id,
      label: `${condition.id} · ${condition.name}`,
    }));
  }
  if (field.settingKey === "route") {
    const account = state.accounts.find(item => item.alias === settings.trading_account);
    const capability = state.capabilities.find(item => item.connection_id === account?.connection_id);
    const routes = capability?.order_routes?.length
      ? capability.order_routes
      : ["KRX", "NXT", "SOR", "AUTO"];
    return routes.map(route => ({ value: route, label: route }));
  }
  if (field.settingKey === "target_source") {
    return [
      { value: "HTS_CONDITION", label: "HTS 조건검색" },
      { value: "RANKING", label: "시장 순위검색" },
      { value: "SYMBOLS", label: "지정 종목" },
    ];
  }
  if (field.settingKey === "ranking_kind") {
    return [
      { value: "HTS_VIEW", label: "조회 순위" },
      { value: "VOLUME", label: "거래량 순위" },
      { value: "TRADING_VALUE", label: "거래대금 순위" },
      { value: "RISE", label: "상승률 순위" },
    ];
  }
  if (field.settingKey === "ranking_venue") {
    return ["KRX", "NXT", "INTEGRATED"].map(value => ({ value, label: value }));
  }
  if (field.settingKey === "market_venue") {
    return [
      { value: "KRX", label: "KRX" },
      { value: "NXT", label: "NXT" },
      { value: "INTEGRATED", label: "통합" },
    ];
  }
  if (field.settingKey === "ranking_market") {
    return [
      { value: "ALL", label: "전체" },
      { value: "KOSPI", label: "코스피" },
      { value: "KOSDAQ", label: "코스닥" },
    ];
  }
  if (["buy_order_type", "sell_order_type"].includes(field.settingKey)) {
    return [
      { value: "MARKET", label: "시장가" },
      { value: "LIMIT", label: "현재가 지정가" },
    ];
  }
  if (["buy_condition_logic", "sell_condition_logic"].includes(field.settingKey)) {
    return [
      { value: "ALL", label: "모두 충족 (AND)" },
      { value: "ANY", label: "하나 이상 (OR)" },
    ];
  }
  if (field.settingKey === "bar_interval") {
    return ["1m", "3m", "5m", "10m", "15m", "30m", "60m", "120m", "1d"]
      .map(value => ({ value, label: value === "1d" ? "일봉" : value }));
  }
  if (field.settingKey === "bar_data_mode") {
    return [
      { value: "COMPLETE_ONLY", label: "완성봉만" },
      { value: "INCLUDE_CURRENT", label: "진행 중 봉 포함" },
    ];
  }
  return null;
}

function normalizeBlockPackageValue(field, value) {
  if (field.type === "boolean") return value === true || value === "true";
  if (field.type === "number") {
    const normalized = String(value).trim();
    if (!/^-?\d+$/.test(normalized)) throw new Error(`${field.label}에 정수를 입력하세요.`);
    return Number(normalized);
  }
  if (field.type === "decimal") {
    const normalized = String(value).trim();
    if (!/^-?\d+(?:\.\d+)?$/.test(normalized)) throw new Error(`${field.label}에 숫자를 입력하세요.`);
    return normalized;
  }
  if (field.type === "symbols") {
    return [...new Set(String(value).split(/[\s,]+/).map(item => item.trim().toUpperCase()).filter(Boolean))];
  }
  return String(value);
}

function applyBlockFieldEdit(fieldId, value) {
  const parsed = currentBlockModel();
  const field = [
    ...parsed.fields,
    ...parsed.schedules.flatMap(schedule => schedule.fields),
  ].find(item => item.id === fieldId);
  if (!field) throw new Error("블록 구성이 변경되어 필드 위치를 찾지 못했습니다. 블록 화면을 다시 열어주세요.");
  if (field.settingKey && state.blockPackage) {
    const normalized = normalizeBlockPackageValue(field, value);
    state.blockPackage.settings[field.settingKey] = normalized;
    if (field.settingKey === "condition_source") {
      const connection = state.connections.find(item => (
        item.id === normalized || item.label === normalized
      ));
      if (connection?.realtime_user_id) {
        state.blockPackage.settings.hts_user_id = connection.realtime_user_id;
      }
      state.blockConditions = [];
      state.blockConditionKey = null;
      state.blockConditionError = null;
      refreshBlockConditions().catch(error => {
        state.blockConditionError = error.message;
        renderStrategyBlocks();
        notify(error.message, true);
      });
    } else if (field.settingKey === "hts_user_id") {
      state.blockConditions = [];
      state.blockConditionKey = null;
      state.blockConditionError = null;
      refreshBlockConditions().catch(error => {
        state.blockConditionError = error.message;
        renderStrategyBlocks();
        notify(error.message, true);
      });
    } else if (field.settingKey === "condition_id") {
      const selected = state.blockConditions.find(item => item.id === normalized);
      state.blockPackage.settings.expected_condition_name = selected?.name || "";
    } else if (field.settingKey === "target_source") {
      state.blockConditions = [];
      state.blockConditionKey = null;
      state.blockConditionError = null;
      refreshBlockConditions().catch(error => {
        state.blockConditionError = error.message;
        renderStrategyBlocks();
      });
    }
    setDirty(true);
    renderStrategyBlocks();
    return;
  }
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
  const choices = blockFieldOptions(field);
  if (field.type === "boolean" || choices) {
    control = document.createElement("select");
    const options = field.type === "boolean"
      ? [{ value: "true", label: "사용" }, { value: "false", label: "사용 안 함" }]
      : choices;
    if (!options.length) {
      const option = document.createElement("option");
      option.value = String(field.value ?? "");
      option.textContent = field.settingKey === "condition_id"
        ? (state.blockConditionError || "조회 가능한 조건식 없음")
        : "등록된 항목 없음";
      control.append(option);
    }
    if (field.value != null && !options.some(item => String(item.value) === String(field.value))) {
      options.unshift({
        value: String(field.value),
        label: `${field.value} · 현재 설정`,
      });
    }
    for (const item of options) {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label;
      control.append(option);
    }
    control.value = String(field.value);
  } else if (field.type === "symbols") {
    control = document.createElement("textarea");
    control.rows = 3;
    control.placeholder = "005930, 000660";
    control.value = Array.isArray(field.value) ? field.value.join(", ") : String(field.value || "");
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

function createStrategyBlockNode({ title, meta, tone, fields = [], actions = [], vertical = false }, disabled) {
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
    flow.className = `block-flow${vertical ? " vertical" : ""}`;
    actions.forEach((action, index) => {
      if (index) {
        const arrow = document.createElement("span");
        arrow.className = "block-flow-arrow";
        arrow.textContent = vertical ? "↓" : "→";
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

const BLOCK_CONDITION_METRICS = [
  ["open", "시가"], ["high", "고가"], ["low", "저가"], ["close", "종가"],
  ["volume", "거래량"], ["bollinger_upper", "볼린저 상단"],
  ["bollinger_middle", "볼린저 중심"], ["bollinger_lower", "볼린저 하단"],
  ["average_price", "평균 매수가"], ["profit_rate", "수익률(%)"],
];

const BLOCK_CONDITION_OPERATORS = [
  ["LT", "<"], ["LTE", "≤"], ["GT", ">"], ["GTE", "≥"], ["EQ", "="],
  ["CROSS_UP", "상향돌파"], ["CROSS_DOWN", "하향돌파"],
];

function conditionSelect(items, value, disabled, onChange) {
  const select = document.createElement("select");
  select.className = "block-condition-control";
  for (const [key, label] of items) {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = label;
    select.append(option);
  }
  select.value = value;
  select.disabled = disabled;
  select.addEventListener("change", event => onChange(event.currentTarget.value));
  return select;
}

function updateBlockCondition(kind, index, key, value) {
  const rows = state.blockPackage?.settings?.[`${kind}_conditions`];
  if (!Array.isArray(rows) || !rows[index]) return;
  rows[index][key] = ["left_offset", "right_offset"].includes(key)
    ? Math.max(0, Number.parseInt(value, 10) || 0)
    : key === "value" ? String(value || "0") : value;
  setDirty(true);
  renderStrategyBlocks();
}

function createConditionEditor(kind, disabled) {
  const wrap = document.createElement("div");
  wrap.className = "block-condition-editor";
  const rows = state.blockPackage?.settings?.[`${kind}_conditions`] || [];
  const metrics = kind === "buy"
    ? BLOCK_CONDITION_METRICS.filter(([key]) => !["average_price", "profit_rate"].includes(key))
    : BLOCK_CONDITION_METRICS;
  const rightMetrics = [...metrics, ["value", "직접 값"]];
  rows.forEach((condition, index) => {
    const row = document.createElement("div");
    row.className = "block-condition-row";
    const top = document.createElement("div");
    top.className = "block-condition-expression";
    top.append(
      conditionSelect(metrics, condition.left, disabled, value => updateBlockCondition(kind, index, "left", value)),
      conditionSelect(BLOCK_CONDITION_OPERATORS, condition.operator, disabled, value => updateBlockCondition(kind, index, "operator", value)),
      conditionSelect(rightMetrics, condition.right, disabled, value => updateBlockCondition(kind, index, "right", value)),
    );
    const detail = document.createElement("div");
    detail.className = "block-condition-detail";
    const leftOffset = document.createElement("input");
    leftOffset.type = "number";
    leftOffset.min = "0";
    leftOffset.max = "200";
    leftOffset.value = condition.left_offset ?? 0;
    leftOffset.title = "왼쪽 값의 N봉 전";
    leftOffset.disabled = disabled;
    leftOffset.addEventListener("change", event => updateBlockCondition(kind, index, "left_offset", event.currentTarget.value));
    const leftLabel = document.createElement("span");
    leftLabel.textContent = "봉 전";
    detail.append(leftOffset, leftLabel);
    if (condition.right === "value") {
      const value = document.createElement("input");
      value.type = "number";
      value.step = "any";
      value.value = condition.value ?? "0";
      value.title = "비교할 값";
      value.disabled = disabled;
      value.addEventListener("change", event => updateBlockCondition(kind, index, "value", event.currentTarget.value));
      const valueLabel = document.createElement("span");
      valueLabel.textContent = "비교값";
      detail.append(value, valueLabel);
    } else {
      const rightOffset = document.createElement("input");
      rightOffset.type = "number";
      rightOffset.min = "0";
      rightOffset.max = "200";
      rightOffset.value = condition.right_offset ?? 0;
      rightOffset.title = "오른쪽 값의 N봉 전";
      rightOffset.disabled = disabled;
      rightOffset.addEventListener("change", event => updateBlockCondition(kind, index, "right_offset", event.currentTarget.value));
      const rightLabel = document.createElement("span");
      rightLabel.textContent = "봉 전";
      detail.append(rightOffset, rightLabel);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "block-condition-remove";
    remove.textContent = "삭제";
    remove.disabled = disabled || (kind === "sell" && rows.length <= 1);
    remove.addEventListener("click", () => {
      rows.splice(index, 1);
      setDirty(true);
      renderStrategyBlocks();
    });
    detail.append(remove);
    row.append(top, detail);
    wrap.append(row);
    if (index < rows.length - 1) wrap.append(conditionJoin(
      state.blockPackage.settings[`${kind}_condition_logic`] === "ALL" ? "그리고 (AND)" : "또는 (OR)"
    ));
  });
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "block-condition-empty";
    empty.textContent = "추가 조건 없음 · 종목 데이터에 들어온 종목을 바로 주문 후보로 사용합니다.";
    wrap.append(empty);
  }
  const add = document.createElement("button");
  add.type = "button";
  add.className = "button quiet compact block-condition-add";
  add.textContent = "+ 조건 추가";
  add.disabled = disabled || rows.length >= 20;
  add.addEventListener("click", () => {
    rows.push({
      left: "close", operator: kind === "buy" ? "GT" : "LT",
      right: "bollinger_upper", value: "0", left_offset: 0, right_offset: 0,
    });
    setDirty(true);
    renderStrategyBlocks();
  });
  wrap.append(add);
  return wrap;
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

async function refreshBlockConditions() {
  if (state.blockPackage?.template !== "condition-bollinger-v1") return;
  const settings = state.blockPackage.settings;
  if ((settings.target_source || "HTS_CONDITION") !== "HTS_CONDITION") {
    state.blockConditions = [];
    state.blockConditionKey = null;
    state.blockConditionError = null;
    renderStrategyBlocks();
    return;
  }
  const connection = state.connections.find(item => (
    item.id === settings.condition_source || item.label === settings.condition_source
  ));
  const source = connection?.label || settings.condition_source;
  const userId = settings.hts_user_id;
  if (!connection || !userId || String(userId).startsWith("등록한 ")) {
    state.blockConditions = [];
    state.blockConditionKey = null;
    state.blockConditionError = null;
    renderStrategyBlocks();
    return;
  }
  const key = `${connection.id}\u0000${userId}`;
  if (state.blockConditionKey === key) return;
  state.blockConditionError = null;
  const payload = await api(
    `/api/block-options/conditions?source=${encodeURIComponent(source)}&user_id=${encodeURIComponent(userId)}`
  );
  state.blockConditions = payload.conditions || [];
  state.blockConditionKey = key;
  const selected = state.blockConditions.find(item => item.id === String(settings.condition_id));
  if (selected) state.blockPackage.settings.expected_condition_name = selected.name;
  renderStrategyBlocks();
}

function normalizeManagedBlockSettings(settings) {
  if (!settings) return settings;
  settings.target_source ||= "HTS_CONDITION";
  settings.ranking_kind ||= "RISE";
  settings.ranking_venue ||= "KRX";
  settings.ranking_market ||= "ALL";
  settings.ranking_limit ??= 20;
  settings.symbols = Array.isArray(settings.symbols) ? settings.symbols : [];
  settings.buy_condition_logic ||= "ALL";
  settings.buy_conditions = Array.isArray(settings.buy_conditions) ? settings.buy_conditions : [];
  settings.sell_condition_logic ||= "ANY";
  if (!Array.isArray(settings.sell_conditions) || !settings.sell_conditions.length) {
    settings.sell_conditions = [];
    if (settings.use_take_profit !== false) {
      settings.sell_conditions.push({
        left: "profit_rate", operator: "GTE", right: "value",
        value: String((Number(settings.take_profit_multiplier || 1.03) - 1) * 100),
        left_offset: 0, right_offset: 0,
      });
    }
    if (settings.use_stop_loss !== false) {
      settings.sell_conditions.push({
        left: "profit_rate", operator: "LTE", right: "value",
        value: String((Number(settings.stop_loss_multiplier || .97) - 1) * 100),
        left_offset: 0, right_offset: 0,
      });
    }
    if (settings.use_bollinger_exit !== false) {
      settings.sell_conditions.push({
        left: "close", operator: "LT", right: "bollinger_upper",
        value: "0", left_offset: 0, right_offset: 0,
      });
    }
  }
  settings.buy_order_type ||= "MARKET";
  settings.sell_order_type ||= "MARKET";
  return settings;
}

async function loadBlockEditorData(name) {
  const [blockPackage, providers, connections, accounts, capabilities] = await Promise.all([
    api(`/api/strategies/${encodeURIComponent(name)}/block-package`),
    api("/api/settings/providers"),
    api("/api/settings/connections"),
    api("/api/settings/accounts"),
    api("/api/system/capabilities"),
  ]);
  state.blockPackage = blockPackage;
  normalizeManagedBlockSettings(state.blockPackage.settings);
  state.providers = providers;
  state.connections = connections;
  state.accounts = accounts;
  state.capabilities = capabilities;
  state.blockConditions = [];
  state.blockConditionKey = null;
  state.blockConditionError = null;
  try {
    await refreshBlockConditions();
  } catch (error) {
    state.blockConditionError = error.message;
  }
}

function conditionJoin(text = "또는 (OR)") {
  const join = document.createElement("div");
  join.className = "block-condition-join";
  join.textContent = text;
  return join;
}

const CONDITION_GRAPH_DEFAULTS = {
  connection: { x: 70, y: 80 },
  target: { x: 470, y: 80 },
  buyConditions: { x: 850, y: 80 },
  buyRules: { x: 470, y: 470 },
  buyOrder: { x: 900, y: 500 },
  sellConditions: { x: 70, y: 760 },
  sellOrder: { x: 650, y: 800 },
  buySchedule: { x: 70, y: 1150 },
  sellSchedule: { x: 450, y: 1150 },
  closeSchedule: { x: 830, y: 1150 },
};

const CONDITION_GRAPH_EDGES = [
  ["connection", "target", "데이터 연결"],
  ["target", "buyConditions", "종목 목록"],
  ["buyConditions", "buyRules", "조건 통과"],
  ["buyRules", "buyOrder", "AND"],
  ["sellConditions", "sellOrder", "조건 통과"],
  ["buySchedule", "target", "주기"],
  ["sellSchedule", "sellConditions", "봉 마감"],
  ["closeSchedule", "sellOrder", "장 마감"],
];

const CONDITION_GRAPH_SIZE = { width: 1580, height: 1450 };

function ensureBlockLayout() {
  if (!state.blockPackage) return null;
  state.blockPackage.layout ||= {};
  state.blockPackage.layout.nodes ||= {};
  state.blockPackage.layout.viewport ||= { x: 24, y: 24, zoom: .75 };
  const viewport = state.blockPackage.layout.viewport;
  viewport.x = Number.isFinite(Number(viewport.x)) ? Number(viewport.x) : 24;
  viewport.y = Number.isFinite(Number(viewport.y)) ? Number(viewport.y) : 24;
  viewport.zoom = Math.min(2, Math.max(.4, Number(viewport.zoom) || .75));
  return state.blockPackage.layout;
}

function blockNodePosition(id) {
  const layout = ensureBlockLayout();
  const saved = layout?.nodes?.[id];
  const fallback = CONDITION_GRAPH_DEFAULTS[id] || { x: 40, y: 40 };
  return {
    x: Number.isFinite(Number(saved?.x)) ? Number(saved.x) : fallback.x,
    y: Number.isFinite(Number(saved?.y)) ? Number(saved.y) : fallback.y,
  };
}

function applyBlockViewport(world) {
  const viewport = ensureBlockLayout()?.viewport || { x: 24, y: 24, zoom: .75 };
  world.style.transform = `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})`;
  elements.blockCanvas.style.backgroundPosition = `${viewport.x}px ${viewport.y}px`;
  elements.blockCanvas.style.backgroundSize = `${18 * viewport.zoom}px ${18 * viewport.zoom}px`;
  if (elements.blockZoomLabel) elements.blockZoomLabel.textContent = `${Math.round(viewport.zoom * 100)}%`;
}

function blockEdgeGeometry(from, to) {
  const source = {
    left: from.offsetLeft,
    top: from.offsetTop,
    width: from.offsetWidth,
    height: from.offsetHeight,
  };
  const target = {
    left: to.offsetLeft,
    top: to.offsetTop,
    width: to.offsetWidth,
    height: to.offsetHeight,
  };
  source.right = source.left + source.width;
  source.bottom = source.top + source.height;
  source.cx = source.left + source.width / 2;
  source.cy = source.top + source.height / 2;
  target.right = target.left + target.width;
  target.bottom = target.top + target.height;
  target.cx = target.left + target.width / 2;
  target.cy = target.top + target.height / 2;

  const dx = target.cx - source.cx;
  const dy = target.cy - source.cy;
  const horizontalClearance = Math.abs(dx) - (source.width + target.width) / 2;
  const verticalClearance = Math.abs(dy) - (source.height + target.height) / 2;
  const horizontal = horizontalClearance >= verticalClearance;

  if (horizontal) {
    const direction = dx >= 0 ? 1 : -1;
    const x1 = direction > 0 ? source.right : source.left;
    const y1 = source.cy;
    const x2 = direction > 0 ? target.left : target.right;
    const y2 = target.cy;
    const bend = Math.max(48, Math.min(220, Math.abs(x2 - x1) * .45 + Math.abs(y2 - y1) * .08));
    return {
      x1, y1, x2, y2,
      c1x: x1 + direction * bend,
      c1y: y1,
      c2x: x2 - direction * bend,
      c2y: y2,
      labelX: (x1 + x2) / 2,
      labelY: (y1 + y2) / 2 - 9,
    };
  }

  const direction = dy >= 0 ? 1 : -1;
  const x1 = source.cx;
  const y1 = direction > 0 ? source.bottom : source.top;
  const x2 = target.cx;
  const y2 = direction > 0 ? target.top : target.bottom;
  const bend = Math.max(48, Math.min(220, Math.abs(y2 - y1) * .45 + Math.abs(x2 - x1) * .08));
  return {
    x1, y1, x2, y2,
    c1x: x1,
    c1y: y1 + direction * bend,
    c2x: x2,
    c2y: y2 - direction * bend,
    labelX: (x1 + x2) / 2 + 12,
    labelY: (y1 + y2) / 2 + 3,
  };
}

function drawBlockGraphEdges(world, edgeLayer) {
  [...edgeLayer.children].forEach(child => {
    if (child.tagName.toLowerCase() !== "defs") child.remove();
  });
  edgeLayer.setAttribute("viewBox", `0 0 ${CONDITION_GRAPH_SIZE.width} ${CONDITION_GRAPH_SIZE.height}`);
  for (const [fromId, toId, label] of CONDITION_GRAPH_EDGES) {
    const from = world.querySelector(`[data-block-node="${fromId}"]`);
    const to = world.querySelector(`[data-block-node="${toId}"]`);
    if (!from || !to) continue;
    const edge = blockEdgeGeometry(from, to);
    const port = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    port.setAttribute("cx", String(edge.x1));
    port.setAttribute("cy", String(edge.y1));
    port.setAttribute("r", "4");
    port.setAttribute("class", "block-edge-port");
    edgeLayer.append(port);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${edge.x1} ${edge.y1} C ${edge.c1x} ${edge.c1y}, ${edge.c2x} ${edge.c2y}, ${edge.x2} ${edge.y2}`);
    path.setAttribute("class", "block-edge-path");
    path.setAttribute("marker-end", "url(#block-edge-arrow)");
    edgeLayer.append(path);
    if (label) {
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", String(edge.labelX));
      text.setAttribute("y", String(edge.labelY));
      text.setAttribute("class", "block-edge-label");
      text.textContent = label;
      edgeLayer.append(text);
    }
  }
}

function createBlockEdgeLayer() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "block-edge-layer");
  svg.setAttribute("aria-hidden", "true");
  const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
  marker.setAttribute("id", "block-edge-arrow");
  marker.setAttribute("viewBox", "0 0 10 10");
  marker.setAttribute("refX", "8");
  marker.setAttribute("refY", "5");
  marker.setAttribute("markerWidth", "6");
  marker.setAttribute("markerHeight", "6");
  marker.setAttribute("orient", "auto-start-reverse");
  const arrow = document.createElementNS("http://www.w3.org/2000/svg", "path");
  arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
  marker.append(arrow);
  defs.append(marker);
  svg.append(defs);
  return svg;
}

function bindBlockNodeDrag(node, world, edgeLayer, running) {
  const handle = node.querySelector(".strategy-block-title");
  if (!handle || running) return;
  handle.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const viewport = ensureBlockLayout().viewport;
    const start = blockNodePosition(node.dataset.blockNode);
    const origin = { x: event.clientX, y: event.clientY };
    handle.setPointerCapture(event.pointerId);
    node.classList.add("dragging");
    const move = moveEvent => {
      const x = start.x + (moveEvent.clientX - origin.x) / viewport.zoom;
      const y = start.y + (moveEvent.clientY - origin.y) / viewport.zoom;
      state.blockPackage.layout.nodes[node.dataset.blockNode] = {
        x: Math.round(x * 10) / 10,
        y: Math.round(y * 10) / 10,
      };
      node.style.left = `${x}px`;
      node.style.top = `${y}px`;
      drawBlockGraphEdges(world, edgeLayer);
    };
    const end = () => {
      node.classList.remove("dragging");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", end);
      handle.removeEventListener("pointercancel", end);
      setDirty(true);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
  });
}

function bindBlockCanvasPan(world) {
  const canvas = elements.blockCanvas;
  if (canvas._blockPanHandler) canvas.removeEventListener("pointerdown", canvas._blockPanHandler);
  if (canvas._blockWheelHandler) canvas.removeEventListener("wheel", canvas._blockWheelHandler);
  const panHandler = event => {
    if (event.button !== 0 || event.target.closest(".strategy-block-node")) return;
    event.preventDefault();
    const viewport = ensureBlockLayout().viewport;
    const start = { x: viewport.x, y: viewport.y };
    const origin = { x: event.clientX, y: event.clientY };
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add("panning");
    const move = moveEvent => {
      viewport.x = start.x + moveEvent.clientX - origin.x;
      viewport.y = start.y + moveEvent.clientY - origin.y;
      applyBlockViewport(world);
    };
    const end = () => {
      canvas.classList.remove("panning");
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("pointerup", end);
      canvas.removeEventListener("pointercancel", end);
      setDirty(true);
    };
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
  };
  const wheelHandler = event => {
    event.preventDefault();
    const viewport = ensureBlockLayout().viewport;
    if (event.ctrlKey || event.metaKey) {
      const rect = canvas.getBoundingClientRect();
      const worldX = (event.clientX - rect.left - viewport.x) / viewport.zoom;
      const worldY = (event.clientY - rect.top - viewport.y) / viewport.zoom;
      const next = Math.min(2, Math.max(.4, viewport.zoom * Math.exp(-event.deltaY * .0015)));
      viewport.x = event.clientX - rect.left - worldX * next;
      viewport.y = event.clientY - rect.top - worldY * next;
      viewport.zoom = next;
    } else {
      viewport.x -= event.deltaX + (event.shiftKey ? event.deltaY : 0);
      viewport.y -= event.shiftKey ? 0 : event.deltaY;
    }
    applyBlockViewport(world);
    window.clearTimeout(canvas._blockViewDirtyTimer);
    canvas._blockViewDirtyTimer = window.setTimeout(() => setDirty(true), 180);
  };
  canvas._blockPanHandler = panHandler;
  canvas._blockWheelHandler = wheelHandler;
  canvas.addEventListener("pointerdown", panHandler);
  canvas.addEventListener("wheel", wheelHandler, { passive: false });
}

function setBlockZoom(nextZoom) {
  const world = elements.blockCanvas.querySelector(".block-canvas-world");
  if (!world || !state.blockPackage) return;
  const viewport = ensureBlockLayout().viewport;
  const rect = elements.blockCanvas.getBoundingClientRect();
  const centerX = rect.width / 2;
  const centerY = rect.height / 2;
  const worldX = (centerX - viewport.x) / viewport.zoom;
  const worldY = (centerY - viewport.y) / viewport.zoom;
  viewport.zoom = Math.min(2, Math.max(.4, nextZoom));
  viewport.x = centerX - worldX * viewport.zoom;
  viewport.y = centerY - worldY * viewport.zoom;
  applyBlockViewport(world);
  setDirty(true);
}

function fitBlockGraph() {
  const world = elements.blockCanvas.querySelector(".block-canvas-world");
  const nodes = [...(world?.querySelectorAll(".strategy-block-node") || [])];
  if (!world || !nodes.length || !state.blockPackage) return;
  const minX = Math.min(...nodes.map(node => node.offsetLeft));
  const minY = Math.min(...nodes.map(node => node.offsetTop));
  const maxX = Math.max(...nodes.map(node => node.offsetLeft + node.offsetWidth));
  const maxY = Math.max(...nodes.map(node => node.offsetTop + node.offsetHeight));
  const padding = 46;
  const zoom = Math.min(1.25, Math.max(.4, Math.min(
    (elements.blockCanvas.clientWidth - padding * 2) / (maxX - minX),
    (elements.blockCanvas.clientHeight - padding * 2) / (maxY - minY),
  )));
  const viewport = ensureBlockLayout().viewport;
  viewport.zoom = zoom;
  viewport.x = (elements.blockCanvas.clientWidth - (maxX - minX) * zoom) / 2 - minX * zoom;
  viewport.y = (elements.blockCanvas.clientHeight - (maxY - minY) * zoom) / 2 - minY * zoom;
  applyBlockViewport(world);
  setDirty(true);
}

function resetBlockGraphLayout() {
  if (!state.blockPackage) return;
  state.blockPackage.layout = {
    nodes: Object.fromEntries(Object.entries(CONDITION_GRAPH_DEFAULTS).map(([id, point]) => [id, { ...point }])),
    viewport: { x: 24, y: 24, zoom: 1 },
  };
  setDirty(true);
  renderStrategyBlocks();
  window.requestAnimationFrame(fitBlockGraph);
}

function createGraphSectionLabel(world, text, x, y) {
  const label = document.createElement("div");
  label.className = "block-graph-section-label";
  label.textContent = text;
  label.style.left = `${x}px`;
  label.style.top = `${y}px`;
  world.append(label);
}

function renderConditionBollingerBlocks(parsed, running) {
  const fields = new Map(parsed.fields.map(field => [field.settingKey, field]));
  const take = (...keys) => keys.map(key => fields.get(key)).filter(Boolean);
  const settings = state.blockPackage.settings;
  const targetFields = settings.target_source === "RANKING"
    ? take("target_source", "ranking_kind", "ranking_venue", "ranking_market", "ranking_limit")
    : settings.target_source === "SYMBOLS"
      ? take("target_source", "symbols")
      : take("target_source", "hts_user_id", "condition_id");
  const targetLabels = {
    HTS_CONDITION: "HTS 조건검색",
    RANKING: "시장 순위검색",
    SYMBOLS: "지정 종목",
  };
  const world = document.createElement("div");
  world.className = "block-canvas-world";
  world.style.width = `${CONDITION_GRAPH_SIZE.width}px`;
  world.style.height = `${CONDITION_GRAPH_SIZE.height}px`;
  const edgeLayer = createBlockEdgeLayer();
  world.append(edgeLayer);
  createGraphSectionLabel(world, "연결 · 종목 데이터", 70, 38);
  createGraphSectionLabel(world, "매수 판단 · 주문", 470, 428);
  createGraphSectionLabel(world, "보유종목 매도 판단", 70, 718);
  createGraphSectionLabel(world, "실행 일정", 70, 1108);

  const nodes = [
    { id: "connection", title: "전략 연결", meta: "계좌 · API", tone: "connection", width: 330,
      fields: take("strategy_id", "trading_account", "condition_source", "market_venue", "route") },
    { id: "target", title: targetLabels[settings.target_source] || "종목 데이터", meta: settings.target_source === "HTS_CONDITION"
      ? (state.blockConditionError ? "조회 실패" : `${state.blockConditions.length}개 조건식`) : "재사용 가능", tone: "target", width: 330,
      fields: targetFields, actions: ["조회 결과를 종목 목록으로 전달"] },
    { id: "buyConditions", title: "매수 추가 조건", meta: settings.buy_conditions.length
      ? `${settings.buy_conditions.length}개 · ${settings.buy_condition_logic}` : "조건 없이 통과", tone: "risk", width: 430,
      fields: take("buy_condition_logic", "bar_interval", "bar_data_mode", "bollinger_period", "bollinger_stddev"), conditionKind: "buy" },
    { id: "buyRules", title: "매수 조건", meta: "AND", tone: "risk", width: 340,
      fields: take("buy_budget_per_symbol", "total_operating_budget", "max_new_buys_per_scan"),
      actions: ["미보유", "미체결 없음", "가능 수량 ≥ 1", "운용 예산 이내"] },
    { id: "buyOrder", title: "매수 주문", meta: "중앙 주문 처리", tone: "event", width: 300,
      fields: take("buy_order_type"), actions: ["멱등성 예약", "위험 검사", "주문 제출", "상태 조정"], vertical: true },
    { id: "sellConditions", title: "매도 조건", meta: `${settings.sell_conditions.length}개 · ${settings.sell_condition_logic}`, tone: "risk", width: 500,
      fields: take("sell_condition_logic"), conditionKind: "sell" },
    { id: "sellOrder", title: "매도 주문", meta: "조건 충족", tone: "event", width: 300,
      fields: take("sell_order_type"), actions: ["전략 소유 수량", "주문 제출", "상태 조정"], vertical: true },
    { id: "buySchedule", title: "종목 조회 일정", meta: "정각 반복", tone: "schedule", width: 320,
      fields: take("buy_scan_seconds", "buy_start_time", "buy_end_time") },
    { id: "sellSchedule", title: "매도 판단 일정", meta: "봉 마감", tone: "schedule", width: 320,
      fields: take("sell_start_time", "sell_end_time") },
    { id: "closeSchedule", title: "장 마감 청산", meta: "정시 · 재확인", tone: "schedule", width: 360,
      fields: take("close_time", "close_retry_start", "close_retry_end", "close_confirm_time") },
  ];

  for (const definition of nodes) {
    const node = createStrategyBlockNode(definition, running);
    if (definition.conditionKind) node.append(createConditionEditor(definition.conditionKind, running));
    const point = blockNodePosition(definition.id);
    node.classList.add("graph-node");
    node.dataset.blockNode = definition.id;
    node.style.width = `${definition.width}px`;
    node.style.left = `${point.x}px`;
    node.style.top = `${point.y}px`;
    world.append(node);
    bindBlockNodeDrag(node, world, edgeLayer, running);
  }
  elements.blockCanvas.append(world);
  bindBlockCanvasPan(world);
  applyBlockViewport(world);
  state.blockGraphRedraw = () => drawBlockGraphEdges(world, edgeLayer);
  window.requestAnimationFrame(state.blockGraphRedraw);
}

function renderStrategyBlocks() {
  if (!elements.blockCanvas) return;
  const source = elements.editor.value;
  const parsed = currentBlockModel();
  const running = Boolean(selectedState()?.running);
  elements.blockCanvas.replaceChildren();
  elements.blockCanvas.classList.toggle(
    "graph-mode",
    parsed.template === "condition-bollinger-v1" && Boolean(state.blockPackage),
  );
  elements.blockSummary.textContent = `${parsed.fields.length}개 설정 · ${parsed.schedules.length}개 일정 · ${parsed.handlers.length}개 실행 흐름`;
  state.blockSource = source;

  if (!parsed.fields.length && !parsed.schedules.length && !parsed.handlers.length) {
    const empty = document.createElement("div");
    empty.className = "block-empty";
    empty.textContent = "표시할 수 있는 블록이 없습니다.\n새 블록 전략을 만들거나 제공되는 설정 블록을 추가해 주세요.";
    elements.blockCanvas.append(empty);
    return;
  }

  if (parsed.template === "condition-bollinger-v1" && state.blockPackage) {
    renderConditionBollingerBlocks(parsed, running);
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
      meta: schedule.meta,
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
        vertical: handler.vertical,
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
  elements.blockExport.disabled = !selected || state.strategyType !== "block";
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
  if (state.strategyType === "block") {
    await loadBlockEditorData(name);
  } else {
    state.blockPackage = null;
    state.blockConditions = [];
    state.blockConditionKey = null;
    state.blockConditionError = null;
  }
  state.strategySummary = null;
  showWorkspaceView("strategy");
  elements.fileName.textContent = displayStrategyName(name);
  configureStrategyTabs();
  elements.editor.value = payload.source;
  state.blockSource = null;
  setDirty(false);
  updateLineNumbers();
  setStrategyTab(state.strategyType === "block" ? "blocks" : "overview");
  state.logCursor = state.clearedLogCursors[name] || 0;
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
  state.blockPackage = null;
  state.blockConditions = [];
  state.blockConditionKey = null;
  state.blockConditionError = null;
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
  if (state.strategyType === "block") {
    if (!state.blockPackage) throw new Error("블록 정의를 불러오지 못했습니다. 전략을 다시 열어주세요.");
    await api(`/api/strategies/${encodeURIComponent(state.selected)}/block-package`, {
      method: "PUT",
      body: JSON.stringify(state.blockPackage),
    });
    const [strategy, blockPackage] = await Promise.all([
      api(`/api/strategies/${encodeURIComponent(state.selected)}`),
      api(`/api/strategies/${encodeURIComponent(state.selected)}/block-package`),
    ]);
    elements.editor.value = strategy.source;
    state.blockPackage = blockPackage;
    state.blockSource = strategy.source;
    updateLineNumbers();
    renderStrategyBlocks();
  } else {
    await api(`/api/strategies/${encodeURIComponent(state.selected)}`, {
      method: "PUT",
      body: JSON.stringify({
        source: elements.editor.value,
        strategy_type: state.strategyType,
      }),
    });
  }
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
  const payload = await api(
    `/api/strategies/${encodeURIComponent(state.selected)}/logs?after=${Number.MAX_SAFE_INTEGER}`
  );
  state.logCursor = payload.next;
  rememberLogClearCursor(state.selected, payload.next);
  resetActivity("표시할 활동 기록이 없습니다.");
  notify("이 시점 이전의 실행 기록을 화면에서 숨겼습니다.");
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
  const deletedName = state.selected;
  await api(`/api/strategies/${encodeURIComponent(state.selected)}`, { method: "DELETE" });
  forgetLogClearCursor(deletedName);
  state.selected = null;
  state.strategySummary = null;
  state.blockPackage = null;
  state.blockConditions = [];
  state.blockConditionKey = null;
  state.blockConditionError = null;
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
    const previousLogInstanceId = state.logCursorInstanceId;
    restoreLogClearCursors(status.instance_id);
    if (previousLogInstanceId && previousLogInstanceId !== state.logCursorInstanceId && state.selected) {
      state.logCursor = 0;
      resetActivity();
    }
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
  const format = provider?.account_number_format;
  const accountNumber = document.querySelector("#account-number");
  const hint = document.querySelector("#account-number-hint");
  elements.productCodeField.hidden = true;
  document.querySelector("#account-product-code").required = false;
  accountNumber.placeholder = state.selectedAccountId
    ? `${format?.placeholder || "계좌번호"} · 비워두면 기존 번호 유지`
    : format?.placeholder || "계좌번호";
  accountNumber.maxLength = Array.isArray(format?.groups)
    ? format.groups.reduce((sum, size) => sum + Number(size), 0) + format.groups.length - 1
    : 32;
  const providerHint = provider?.id === "kis"
    ? "8자리 계좌번호와 2자리 상품코드를 함께 입력하면 자동으로 8-2 형식이 적용됩니다."
    : provider?.id === "namuh"
      ? "NAMUH 계좌번호는 000-00-000000 형식으로 자동 정리됩니다."
      : "숫자를 입력하면 증권사 계좌번호 형식을 자동으로 적용합니다.";
  hint.textContent = state.selectedAccountId
    ? `${providerHint} 비워두면 저장된 계좌번호를 유지합니다.`
    : providerHint;
}

function formatGroupedDigits(value, groups) {
  if (!Array.isArray(groups) || !groups.length) return value;
  const total = groups.reduce((sum, size) => sum + Number(size), 0);
  let digits = String(value || "").replace(/\D/g, "").slice(0, total);
  const parts = [];
  for (const size of groups) {
    if (!digits) break;
    parts.push(digits.slice(0, size));
    digits = digits.slice(size);
  }
  return parts.join("-");
}

function formatAccountNumberInput() {
  const connection = state.connections.find(item => item.id === elements.accountConnection.value);
  const provider = selectedProvider(connection?.provider);
  const input = document.querySelector("#account-number");
  input.value = formatGroupedDigits(input.value, provider?.account_number_format?.groups);
}

function renderConnectionForm() {
  const detail = document.querySelector("#connection-detail");
  detail.hidden = state.connectionFormMode === null;
  if (detail.hidden) return;
  const connection = state.connections.find(item => item.id === state.selectedConnectionId);
  const editing = state.connectionFormMode === "edit" && Boolean(connection);
  const provider = connection?.provider || elements.connectionProvider.value || state.providers[0]?.id || "";
  elements.connectionProvider.value = provider;
  refreshProviderFields();
  if (connection) elements.connectionTarget.value = connection.target;
  refreshConnectionRateHint();

  elements.connectionProvider.disabled = false;
  elements.connectionTarget.disabled = false;
  document.querySelector("#connection-label").value = connection?.label || "";
  const keyField = document.querySelector("#connection-key-field");
  const secretField = document.querySelector("#connection-secret-field");
  const keyInput = document.querySelector("#connection-key");
  const secretInput = document.querySelector("#connection-secret");
  keyField.hidden = editing;
  secretField.hidden = editing;
  keyInput.required = !editing;
  secretInput.required = !editing;
  keyInput.value = "";
  secretInput.value = "";
  document.querySelector("#connection-credentials-locked").hidden = !editing;
  const realtime = document.querySelector("#connection-realtime-user-id");
  realtime.value = editing ? connection?.realtime_user_id || "" : "";
  realtime.placeholder = "KIS 실시간 주문·체결 통보용";
  document.querySelector("#connection-realtime-user-hint").textContent = editing
    ? "HTS ID를 포함한 연결 설정을 변경할 수 있습니다. App Key와 App Secret은 유지됩니다."
    : "비워두면 시세·호가 WebSocket만 사용하고 주문·체결은 REST로 확인합니다.";
  document.querySelector("#connection-form-title").textContent = editing ? connection.label : "새 API 등록";
  document.querySelector("#connection-form-description").textContent = editing
    ? "증권사·접속 대상·이름·HTS ID를 변경할 수 있습니다. 인증 키는 표시하지 않습니다."
    : "증권사 인증정보를 새로 저장합니다.";
  document.querySelector("#connection-submit-button").textContent = editing ? "변경 저장" : "API 등록";
}

function renderAccountForm() {
  const detail = document.querySelector("#account-detail");
  detail.hidden = state.accountFormMode === null;
  if (detail.hidden) return;
  const account = state.accounts.find(item => item.id === state.selectedAccountId);
  const editing = state.accountFormMode === "edit" && Boolean(account);
  if (account) elements.accountConnection.value = account.connection_id;
  elements.accountConnection.disabled = false;
  const alias = document.querySelector("#account-alias");
  alias.value = account?.alias || "";
  alias.disabled = false;
  document.querySelector("#account-alias-hint").textContent = editing
    ? "별칭을 변경해도 기존 전략의 이전 별칭은 호환 이름으로 유지됩니다."
    : "전략에서 사용할 계좌 이름입니다.";
  const accountNumber = document.querySelector("#account-number");
  accountNumber.value = "";
  accountNumber.required = !editing;
  document.querySelector("#account-product-code").value = "";

  const limits = account?.risk_limits || {};
  const costs = account?.cost_rates || {};
  document.querySelector("#account-max-order").value = limits.max_order_amount ?? "";
  document.querySelector("#account-max-daily-buy").value = limits.max_daily_buy_amount ?? "";
  document.querySelector("#account-max-exposure").value = limits.max_exposure ?? "";
  document.querySelector("#account-max-open-orders").value = limits.max_open_orders ?? "";
  document.querySelector("#account-buy-commission-rate").value = costs.buy_commission_rate ?? "";
  document.querySelector("#account-sell-commission-rate").value = costs.sell_commission_rate ?? "";
  document.querySelector("#account-sell-tax-rate").value = costs.sell_tax_rate ?? "";
  document.querySelector("#account-form-title").textContent = editing ? account.alias : "새 계좌 등록";
  document.querySelector("#account-form-description").textContent = editing
    ? "사용할 API·별칭·계좌번호·한도와 비용 설정을 편집합니다."
    : "API 연결과 거래 계좌를 연결합니다.";
  document.querySelector("#account-submit-button").textContent = editing ? "변경 저장" : "계좌 등록";
  refreshAccountFields();
}

function selectConnectionForEdit(connection) {
  state.selectedConnectionId = connection.id;
  state.connectionFormMode = "edit";
  renderConnectionForm();
  renderConnectionList();
}

function selectAccountForEdit(account) {
  state.selectedAccountId = account.id;
  state.accountFormMode = "edit";
  renderAccountForm();
  renderAccountList();
}

function startNewConnection() {
  state.selectedConnectionId = null;
  state.connectionFormMode = "new";
  renderConnectionForm();
  renderConnectionList();
  document.querySelector("#connection-label").focus();
}

function startNewAccount() {
  state.selectedAccountId = null;
  state.accountFormMode = "new";
  renderAccountForm();
  renderAccountList();
  document.querySelector("#account-alias").focus();
}

function closeConnectionForm() {
  state.connectionFormMode = null;
  state.selectedConnectionId = null;
  renderConnectionForm();
  renderConnectionList();
}

function closeAccountForm() {
  state.accountFormMode = null;
  state.selectedAccountId = null;
  renderAccountForm();
  renderAccountList();
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
    item.className = `registered-item${state.selectedConnectionId === connection.id ? " active" : ""}`;
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
    const edit = document.createElement("button");
    edit.className = "button quiet compact";
    edit.type = "button";
    edit.textContent = "편집";
    edit.addEventListener("click", () => selectConnectionForEdit(connection));
    const check = document.createElement("button");
    check.className = "button quiet compact";
    check.type = "button";
    check.textContent = "연결 확인";
    check.addEventListener("click", event => {
      event.stopPropagation();
      checkConnection(connection, check, status);
    });
    information.append(title, description, rateLimitText, capabilityText, status);
    const remove = document.createElement("button");
    remove.className = "registered-remove";
    remove.type = "button";
    remove.textContent = "삭제";
    remove.addEventListener("click", event => {
      event.stopPropagation();
      removeConnection(connection);
    });
    actions.append(edit, check, remove);
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
    item.className = `registered-item${state.selectedAccountId === account.id ? " active" : ""}`;
    const information = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = account.alias;
    const description = document.createElement("span");
    description.textContent = `${account.connection_label} · ${account.formatted_masked_account || account.masked_account}`;
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
    const edit = document.createElement("button");
    edit.className = "button quiet compact";
    edit.type = "button";
    edit.textContent = "편집";
    edit.addEventListener("click", () => selectAccountForEdit(account));
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
    permissionButton.addEventListener("click", event => {
      event.stopPropagation();
      toggleAccountPermission(account, Boolean(permission?.armed));
    });
    const remove = document.createElement("button");
    remove.className = "registered-remove";
    remove.type = "button";
    remove.textContent = "삭제";
    remove.addEventListener("click", event => {
      event.stopPropagation();
      removeAccount(account);
    });
    actions.append(permissionButton, edit, remove);
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
  if (state.selectedConnectionId && !state.connections.some(item => item.id === state.selectedConnectionId)) {
    state.selectedConnectionId = null;
    state.connectionFormMode = null;
  }
  if (state.selectedAccountId && !state.accounts.some(item => item.id === state.selectedAccountId)) {
    state.selectedAccountId = null;
    state.accountFormMode = null;
  }
  renderConnectionForm();
  renderAccountForm();
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
  state.connectionFormMode = null;
  state.accountFormMode = null;
  state.selectedConnectionId = null;
  state.selectedAccountId = null;
  await loadSettings();
  activateSettingsTab(tabName);
  elements.settingsModal.hidden = false;
  const focusTarget = tabName === "general"
    ? document.querySelector("#update-button")
    : tabName === "apis"
      ? document.querySelector("#new-connection-button")
      : document.querySelector("#new-account-button");
  window.setTimeout(() => focusTarget?.focus(), 0);
}

function closeSettings() {
  elements.settingsModal.hidden = true;
}

async function removeConnection(connection) {
  if (!window.confirm(`${connection.label} API 등록을 삭제할까요?`)) return;
  try {
    await api(`/api/settings/connections/${encodeURIComponent(connection.id)}`, { method: "DELETE" });
    if (state.selectedConnectionId === connection.id) closeConnectionForm();
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
    if (state.selectedAccountId === account.id) closeAccountForm();
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
  return `"""Trade Router 전략 템플릿.

아래 세 값을 자신의 설정에 맞게 바꾸세요. 비밀키와 전체 계좌번호는 이 파일에
입력하지 않습니다.
"""

from trade_router import Strategy, StrategyConfig


# 설정 > 계좌의 "계좌 별칭"입니다. 계좌번호가 아닙니다.
TRADING_ACCOUNT = "등록한 계좌 별칭"

# 설정 > API 키에 표시되는 등록된 API 이름입니다. App Key가 아닙니다.
MARKET_DATA_API = "등록한 API 이름"

# 조회할 국내주식 6자리 종목코드입니다.
SYMBOL = "005930"


class NewStrategy(Strategy):
    config = StrategyConfig(
        id="${strategyId}",
        trading_account=TRADING_ACCOUNT,
        # primary는 전략 안에서 사용할 시세 연결 이름입니다.
        market_sources={"primary": MARKET_DATA_API},
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
        bars = await ctx.market.source("primary").bars(SYMBOL, "3m", count=55)
        if bars:
            ctx.log.info(f"완성 3분봉 {len(bars)}개 · 최근 종가 {bars[-1].close}")

    async def on_stop(self, ctx):
        ctx.log.info("전략을 종료합니다.")
`;
}

function blockStrategyTemplate(strategyId) {
  return `"""Trade Router가 관리하는 조건검색·볼린저 블록 전략 설정."""

from decimal import Decimal

BLOCK_TEMPLATE = "condition-bollinger-v1"
STRATEGY_ID = "${strategyId}"
TRADING_ACCOUNT = "등록한 계좌 별칭"
CONDITION_SOURCE = "등록한 KIS 실전 API 이름"
MARKET_VENUE = "KRX"
HTS_USER_ID = "등록한 HTS ID"
CONDITION_ID = "1"
EXPECTED_CONDITION_NAME = ""
BUY_BUDGET_PER_SYMBOL = Decimal("50000")
TOTAL_OPERATING_BUDGET = Decimal("150000")
MAX_NEW_BUYS_PER_SCAN = 5
BOLLINGER_PERIOD = 55
BOLLINGER_STDDEV = Decimal("1.04")
TAKE_PROFIT_MULTIPLIER = Decimal("1.03")
STOP_LOSS_MULTIPLIER = Decimal("0.97")
USE_BOLLINGER_EXIT = True
USE_TAKE_PROFIT = True
USE_STOP_LOSS = True
BUY_SCAN_SECONDS = 30
BUY_START_TIME = "09:00"
BUY_END_TIME = "15:00"
BAR_INTERVAL = "3m"
SELL_START_TIME = "09:00"
SELL_END_TIME = "15:20"
CLOSE_TIME = "15:20"
CLOSE_RETRY_START = "15:20:30"
CLOSE_RETRY_END = "15:30"
CLOSE_CONFIRM_TIME = "15:30:05"
ORDER_ROUTE = "KRX"
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

async function exportBlockStrategy() {
  if (!state.selected || state.strategyType !== "block") return;
  if (state.dirty) await saveStrategy();
  const payload = await api(
    `/api/strategies/${encodeURIComponent(state.selected)}/block-package`,
  );
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], {
    type: "application/json;charset=utf-8",
  });
  const link = document.createElement("a");
  const safeName = displayStrategyName(state.selected).replace(/[\\/:*?"<>|]+/g, "-");
  link.href = URL.createObjectURL(blob);
  link.download = `${safeName}.trblock.json`;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
  notify("블록 전략 파일을 저장했습니다.");
}

async function importBlockStrategy(file) {
  if (!file) return;
  if (file.size > 512 * 1024) throw new Error("블록 전략 파일은 512 KiB 이하여야 합니다.");
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch (_) {
    throw new Error("블록 전략 파일의 JSON 형식을 확인하세요.");
  }
  const result = await api("/api/strategies/import-block", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  closeNewStrategyModal();
  await refreshStrategies();
  await openStrategy(result.name);
  notify("블록 전략을 가져왔습니다. 연결 별칭을 확인한 뒤 저장하세요.");
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
document.querySelector("#block-import-button").addEventListener("click", () => {
  elements.blockImportFile.value = "";
  elements.blockImportFile.click();
});
elements.blockImportFile.addEventListener("change", event => {
  importBlockStrategy(event.currentTarget.files?.[0]).catch(error => notify(error.message, true));
});
elements.blockExport.addEventListener("click", () => {
  exportBlockStrategy().catch(error => notify(error.message, true));
});
elements.blockZoomOut?.addEventListener("click", () => {
  const zoom = ensureBlockLayout()?.viewport?.zoom || 1;
  setBlockZoom(zoom / 1.15);
});
elements.blockZoomIn?.addEventListener("click", () => {
  const zoom = ensureBlockLayout()?.viewport?.zoom || 1;
  setBlockZoom(zoom * 1.15);
});
elements.blockZoomLabel?.addEventListener("click", () => setBlockZoom(1));
elements.blockFit?.addEventListener("click", fitBlockGraph);
elements.blockAutoLayout?.addEventListener("click", resetBlockGraphLayout);
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
document.querySelector("#service-detail-close").addEventListener("click", closeServiceEventDetail);
elements.serviceDetailModal.addEventListener("click", event => {
  if (event.target === elements.serviceDetailModal) closeServiceEventDetail();
});
document.querySelectorAll("[data-settings-tab]").forEach(button => {
  button.addEventListener("click", () => activateSettingsTab(button.dataset.settingsTab));
});
elements.connectionProvider.addEventListener("change", refreshProviderFields);
elements.connectionTarget.addEventListener("change", refreshConnectionRateHint);
elements.accountConnection.addEventListener("change", refreshAccountFields);
document.querySelector("#account-number").addEventListener("input", formatAccountNumberInput);
document.querySelector("#new-connection-button").addEventListener("click", startNewConnection);
document.querySelector("#new-account-button").addEventListener("click", startNewAccount);
document.querySelector("#connection-cancel-button").addEventListener("click", closeConnectionForm);
document.querySelector("#account-cancel-button").addEventListener("click", closeAccountForm);

document.querySelector("#connection-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector(".settings-submit");
  submit.disabled = true;
  try {
    const editing = state.connections.find(item => item.id === state.selectedConnectionId);
    let result;
    if (editing) {
      result = await api(`/api/settings/connections/${encodeURIComponent(editing.id)}`, {
        method: "PUT",
        body: JSON.stringify({
          provider: elements.connectionProvider.value,
          target: elements.connectionTarget.value,
          label: document.querySelector("#connection-label").value,
          realtime_user_id: document.querySelector("#connection-realtime-user-id").value,
        }),
      });
    } else {
      result = await api("/api/settings/connections?check=true", {
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
    }
    state.selectedConnectionId = null;
    state.connectionFormMode = null;
    await Promise.all([loadSettings(), refreshStatus(), refreshDashboard(true)]);
    if (editing) {
      notify("API 설정을 변경했습니다. 저장된 인증 키는 그대로 유지됩니다.");
    } else if (result.connection_status?.state === "connected") {
      notify("API를 저장하고 연결까지 확인했습니다.");
    } else {
      notify(result.connection_status?.message || "API는 저장했지만 연결 확인에 실패했습니다.", true);
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
    const editing = state.accounts.find(item => item.id === state.selectedAccountId);
    const payload = {
      connection_id: elements.accountConnection.value,
      alias: document.querySelector("#account-alias").value,
      account_number: document.querySelector("#account-number").value || null,
      product_code: document.querySelector("#account-product-code").value || null,
      max_order_amount: positiveNumberOrNull("#account-max-order"),
      max_daily_buy_amount: positiveNumberOrNull("#account-max-daily-buy"),
      max_exposure: positiveNumberOrNull("#account-max-exposure"),
      max_open_orders: positiveNumberOrNull("#account-max-open-orders"),
      buy_commission_rate: positiveNumberOrNull("#account-buy-commission-rate"),
      sell_commission_rate: positiveNumberOrNull("#account-sell-commission-rate"),
      sell_tax_rate: positiveNumberOrNull("#account-sell-tax-rate"),
    };
    let saved;
    if (editing) {
      saved = await api(`/api/settings/accounts/${encodeURIComponent(editing.id)}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
    } else {
      saved = await api("/api/settings/accounts", {
        method: "POST",
        body: JSON.stringify({
          ...payload,
        }),
      });
    }
    state.selectedAccountId = null;
    state.accountFormMode = null;
    await Promise.all([loadSettings(), refreshStatus(), refreshDashboard(true)]);
    notify(editing ? "계좌 설정을 변경했습니다." : "계좌를 등록했습니다.");
  } catch (error) { notify(error.message, true); }
  finally { submit.disabled = !state.connections.length; }
});

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !elements.serviceDetailModal.hidden) {
    closeServiceEventDetail();
    return;
  }
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
