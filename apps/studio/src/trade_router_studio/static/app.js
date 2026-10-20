const LOG_CLEAR_STORAGE_KEY = "trade-router.log-clear-cursors.v1";
const BLOCK_LAYOUT_STORAGE_KEY = "trade-router.block-layouts.v5";
const UPDATE_NOTICE_STORAGE_KEY = "trade-router.update-notice-version.v1";
const BLOCK_VIEWPORT_COORD_LIMIT = 20000;
const BLOCK_ZOOM_MIN = .2;
const BLOCK_ZOOM_MAX = 2;

const state = {
  view: "dashboard",
  selected: null,
  strategyType: "script",
  strategyTab: "overview",
  blockSource: null,
  blockPackage: null,
  blockConditions: [],
  blockConditionCache: new Map(),
  blockConditionKey: null,
  blockConditionError: null,
  blockGraphRedraw: null,
  blockGraphPlan: null,
  blockSelectedNode: null,
  blockExpressionSelections: new WeakMap(),
  blockRenderedRunning: null,
  conditionGroupSerial: 0,
  blockScheduleExpanded: new Map(),
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
  databaseRecoveryKey: null,
  systemStatus: null,
  dashboard: null,
  dashboardLoading: false,
  strategySummary: null,
  strategyListSignature: null,
  strategyOpenToken: 0,
  openingStrategy: null,
  dashboardServiceTab: "market",
  strategyServiceTab: "market",
  strategyBalanceTab: "balance",
  accountBalanceTab: "balance",
  activeAccount: null,
  updateResult: null,
  updateCheckPromise: null,
};

const elements = {
  list: document.querySelector("#strategy-list"),
  count: document.querySelector("#strategy-count"),
  dashboardButton: document.querySelector("#dashboard-button"),
  chartButton: document.querySelector("#chart-button"),
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
  blockAddFlow: document.querySelector("#block-add-flow"),
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
  updateAvailable: document.querySelector("#update-available-button"),
  updateAvailableVersion: document.querySelector("#update-available-version"),
  settingsModal: document.querySelector("#settings-modal"),
  serviceDetailModal: document.querySelector("#service-detail-modal"),
  serviceDetailBody: document.querySelector("#service-detail-body"),
  accountBalanceModal: document.querySelector("#account-balance-modal"),
  accountBalanceBody: document.querySelector("#account-balance-body"),
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

function renderUpdateResult(result) {
  state.updateResult = result;
  const button = document.querySelector("#update-button");
  const status = document.querySelector("#update-status");
  status.textContent = result.message;
  if (result.available && result.can_apply) {
    button.dataset.ready = "true";
    button.textContent = "업데이트 시작";
  } else {
    delete button.dataset.ready;
    button.textContent = "업데이트 확인";
  }
  const available = Boolean(result.available);
  elements.updateAvailable.hidden = !available;
  elements.updateAvailable.classList.toggle("pending-assets", available && !result.can_apply);
  elements.updateAvailableVersion.textContent = result.latest ? `v${result.latest}` : "새 버전";
  elements.updateAvailable.title = available ? result.message : "";
  elements.updateAvailable.setAttribute("aria-label", available ? result.message : "업데이트 확인");
}

function fetchUpdateResult() {
  if (!state.updateCheckPromise) {
    state.updateCheckPromise = api("/api/updates/check")
      .finally(() => { state.updateCheckPromise = null; });
  }
  return state.updateCheckPromise;
}

function updateNoticeWasShown(version) {
  if (!version) return false;
  try {
    return window.sessionStorage.getItem(UPDATE_NOTICE_STORAGE_KEY) === version;
  } catch (_) {
    return false;
  }
}

function rememberUpdateNotice(version) {
  if (!version) return;
  try { window.sessionStorage.setItem(UPDATE_NOTICE_STORAGE_KEY, version); }
  catch (_) {}
}

async function checkForStartupUpdate() {
  try {
    const result = await fetchUpdateResult();
    renderUpdateResult(result);
    const version = String(result.latest || "");
    if (result.available && !updateNoticeWasShown(version)) {
      rememberUpdateNotice(version);
      notify(`${result.message} 상단의 업데이트 표시에서 확인할 수 있습니다.`);
    }
  } catch (_) {
    // Trade Router remains fully usable while offline. Manual checks still report errors.
  }
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

function clampBlockNumber(value, minimum, maximum, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(maximum, Math.max(minimum, number)) : fallback;
}

function cloneBlockLayout(layout) {
  const viewport = layout?.viewport || {};
  return {
    // Node coordinates belonged to the former free-form graph. Keep the
    // package field empty so legacy files still validate without allowing an
    // imported arrangement to change the fixed Composer hierarchy.
    nodes: {},
    viewport: {
      x: clampBlockNumber(viewport.x, -BLOCK_VIEWPORT_COORD_LIMIT, BLOCK_VIEWPORT_COORD_LIMIT, 24),
      y: clampBlockNumber(viewport.y, -BLOCK_VIEWPORT_COORD_LIMIT, BLOCK_VIEWPORT_COORD_LIMIT, 24),
      zoom: clampBlockNumber(viewport.zoom, BLOCK_ZOOM_MIN, BLOCK_ZOOM_MAX, 1),
    },
  };
}

function blockLayoutIdentity(name) {
  const workspace = String(state.systemStatus?.workspace || "default-workspace");
  return `${workspace}\u0000${name}`;
}

function storedBlockLayout(name) {
  if (!name) return null;
  try {
    const saved = JSON.parse(window.localStorage.getItem(BLOCK_LAYOUT_STORAGE_KEY) || "null");
    const layout = saved?.[blockLayoutIdentity(name)];
    return layout && typeof layout === "object" ? cloneBlockLayout(layout) : null;
  } catch (_) {
    return null;
  }
}

function persistBlockLayout(name = state.selected, layout = state.blockPackage?.layout) {
  if (!name || !layout) return;
  try {
    const saved = JSON.parse(window.localStorage.getItem(BLOCK_LAYOUT_STORAGE_KEY) || "{}") || {};
    saved[blockLayoutIdentity(name)] = cloneBlockLayout(layout);
    window.localStorage.setItem(BLOCK_LAYOUT_STORAGE_KEY, JSON.stringify(saved));
  } catch (_) {}
}

function forgetBlockLayout(name) {
  if (!name) return;
  try {
    const saved = JSON.parse(window.localStorage.getItem(BLOCK_LAYOUT_STORAGE_KEY) || "{}") || {};
    delete saved[blockLayoutIdentity(name)];
    window.localStorage.setItem(BLOCK_LAYOUT_STORAGE_KEY, JSON.stringify(saved));
  } catch (_) {}
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
  const suffix = kind === "money" ? "원" : kind === "times" ? "회" : "건";
  if (usage?.limit == null) {
    value.textContent = "미설정";
  } else if (usage?.used == null) {
    value.textContent = `한도 ${formatNumber(usage.limit)}${suffix}`;
  } else {
    value.textContent = `${formatNumber(usage.used)} / ${formatNumber(usage.limit)}${suffix}`;
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
  document.querySelector("#overview-cost-basis").textContent = performance.costs_complete
    ? "체결 금액에 계좌별 수수료율과 매도 세율을 반영한 순손익입니다."
    : "수수료율 또는 매도 세율이 비어 있어 현재 손익의 비용은 미확정입니다.";
  renderStrategyBalanceContent();

  renderServiceResultTabs(
    document.querySelector("#overview-service-results"),
    summary?.service_results || [],
    "strategy"
  );

}

function metricCard(label, raw, { unit = "원", signed = false, percent = false } = {}) {
  const article = document.createElement("article");
  const caption = document.createElement("span");
  caption.textContent = label;
  const value = document.createElement("strong");
  value.textContent = percent
    ? raw == null ? "—" : Number(raw).toLocaleString("ko-KR", { maximumFractionDigits: 2 })
    : formatNumber(raw, { signed });
  if (signed || percent) value.className = profitClass(raw);
  const suffix = document.createElement("small");
  suffix.textContent = unit;
  article.append(caption, value, suffix);
  return article;
}

function dataTable(columns, rows, emptyMessage, { onRowClick = null, rowClass = "" } = {}) {
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "balance-empty";
    empty.textContent = emptyMessage;
    return empty;
  }
  const wrap = document.createElement("div");
  wrap.className = "balance-table-wrap";
  const table = document.createElement("table");
  table.className = "balance-table";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  columns.forEach(column => {
    const cell = document.createElement("th");
    cell.textContent = column.label;
    headRow.append(cell);
  });
  head.append(headRow);
  const body = document.createElement("tbody");
  rows.forEach(row => {
    const line = document.createElement("tr");
    if (rowClass) line.className = rowClass;
    if (onRowClick) {
      line.tabIndex = 0;
      line.title = "차트 열기";
      line.addEventListener("click", () => onRowClick(row));
      line.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onRowClick(row);
        }
      });
    }
    columns.forEach(column => {
      const cell = document.createElement("td");
      const output = column.render(row);
      cell.textContent = output?.text ?? String(output ?? "—");
      if (output?.className) cell.className = output.className;
      line.append(cell);
    });
    body.append(line);
  });
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

function positionTable(positions, emptyMessage = "이 전략이 보유한 종목이 없습니다.", chartOptions = {}) {
  return dataTable([
    { label: "종목", render: row => instrumentLabel(row) },
    { label: "보유 / 주문가능", render: row => `${formatNumber(row.quantity)} / ${formatNumber(row.available_quantity)}주` },
    { label: "평균 매입가", render: row => `${formatNumber(row.average_price)}원` },
    { label: "현재가", render: row => row.market_price == null ? "—" : `${formatNumber(row.market_price)}원` },
    { label: "평가금액", render: row => row.market_value == null ? "—" : `${formatNumber(row.market_value)}원` },
    { label: "평가손익", render: row => ({ text: row.unrealized_profit == null ? "—" : `${formatNumber(row.unrealized_profit, { signed: true })}원`, className: profitClass(row.unrealized_profit) }) },
    { label: "수익률", render: row => ({ text: row.return_percent == null ? "—" : `${Number(row.return_percent).toLocaleString("ko-KR", { maximumFractionDigits: 2 })}%`, className: profitClass(row.return_percent) }) },
  ], positions || [], emptyMessage, {
    rowClass: "chart-open-row",
    onRowClick: row => window.tradeRouterChart?.open({
      code: row.code,
      name: row.name,
      source: chartOptions.source || "AUTO",
    }),
  });
}

function instrumentLabel(row) {
  const code = String(row?.code || "").trim();
  const name = String(row?.name || "").trim();
  if (name && name !== code) return code ? `${name} · ${code}` : name;
  return code || name || "—";
}

function tradeHistoryTable(rows, emptyMessage) {
  return dataTable([
    { label: "종목", render: row => instrumentLabel(row) },
    { label: "구분", render: row => row.side === "SELL" ? "매도" : "매수" },
    { label: "시각", render: row => formatTimestamp(row.updated_at || row.created_at) },
    { label: "주문", render: row => `${row.order_type === "LIMIT" ? "지정가" : "시장가"} ${formatNumber(row.quantity)}주` },
    { label: "체결", render: row => row.filled_quantity && Number(row.filled_quantity) > 0 ? `${formatNumber(row.filled_quantity)}주 · ${formatNumber(row.average_fill_price)}원` : "—" },
    {
      label: "매매손익",
      render: row => {
        if (row.realized_profit == null) return "—";
        const estimate = row.profit_costs_complete ? "" : " (추정)";
        return {
          text: `${formatNumber(row.realized_profit, { signed: true })}원${estimate}`,
          className: profitClass(row.realized_profit),
        };
      },
    },
    {
      label: "수익률",
      render: row => row.return_percent == null
        ? "—"
        : {
            text: `${Number(row.return_percent).toLocaleString("ko-KR", { maximumFractionDigits: 2 })}%`,
            className: profitClass(row.return_percent),
          },
    },
    { label: "상태", render: row => ({ text: orderStatusPresentation(row.status)[0], className: orderStatusPresentation(row.status)[1] }) },
  ], rows || [], emptyMessage, {
    rowClass: "chart-open-row",
    onRowClick: row => window.tradeRouterChart?.open({
      code: row.code,
      name: row.name,
      source: "AUTO",
    }),
  });
}

function renderStrategyBalanceContent() {
  const container = document.querySelector("#overview-balance-content");
  if (!container) return;
  const previousTable = container.querySelector(".balance-table-wrap");
  const previousScroll = previousTable
    ? { top: previousTable.scrollTop, left: previousTable.scrollLeft }
    : null;
  container.replaceChildren();
  document.querySelectorAll("[data-balance-tab]").forEach(button => {
    const active = button.dataset.balanceTab === state.strategyBalanceTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  const summary = state.strategySummary || {};
  const performance = summary.performance || {};
  if (state.strategyBalanceTab === "trades") {
    const metrics = document.createElement("div");
    metrics.className = "strategy-performance-grid compact trade-performance-grid";
    metrics.append(
      metricCard("실현 손익", performance.realized_profit, { signed: true }),
      metricCard("누적 매수", performance.filled_buy_amount),
      metricCard("누적 매도", performance.filled_sell_amount),
      metricCard("매도 승률", performance.win_rate, { unit: "%", percent: true }),
      metricCard("수수료·세금", Number(performance.commission || 0) + Number(performance.tax || 0)),
    );
    container.append(
      metrics,
      tradeHistoryTable(summary.trade_history || [], "이 전략의 매매기록이 없습니다."),
    );
    const nextTable = container.querySelector(".balance-table-wrap");
    if (nextTable && previousScroll) {
      nextTable.scrollTop = previousScroll.top;
      nextTable.scrollLeft = previousScroll.left;
    }
    return;
  }
  const metrics = document.createElement("div");
  metrics.className = "strategy-performance-grid compact";
  metrics.append(
    metricCard("추정 손익", performance.estimated_profit, { signed: true }),
    metricCard("추정 수익률", performance.estimated_return_percent, { unit: "%", percent: true }),
    metricCard("실현 손익", performance.realized_profit, { signed: true }),
    metricCard("평가 손익", performance.unrealized_profit, { signed: true }),
    metricCard("수수료·세금", Number(performance.commission || 0) + Number(performance.tax || 0)),
  );
  const heading = document.createElement("div");
  heading.className = "balance-subheading";
  heading.innerHTML = `<strong>보유 종목</strong><span>${performance.positions?.length || 0}종목</span>`;
  container.append(metrics, heading, positionTable(performance.positions || []));
  const nextTable = container.querySelector(".balance-table-wrap");
  if (nextTable && previousScroll) {
    nextTable.scrollTop = previousScroll.top;
    nextTable.scrollLeft = previousScroll.left;
  }
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
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.setAttribute("aria-label", `${account.alias} 계좌 상세 보기`);
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
  const statusWrap = document.createElement("div");
  statusWrap.className = "dashboard-account-status";
  const status = document.createElement("span");
  status.className = `status-pill ${account.state || "unchecked"}`;
  status.textContent = account.state === "ok" ? "정상" : account.state === "unsupported" ? "미지원" : "조회 오류";
  status.title = account.message || "";
  const detailHint = document.createElement("span");
  detailHint.className = "dashboard-detail-hint";
  detailHint.textContent = "상세 ›";
  statusWrap.append(status, detailHint);
  heading.append(copy, statusWrap);

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
  body.append(values);
  row.append(heading, body);
  row.addEventListener("click", () => openAccountBalance(account));
  row.addEventListener("keydown", event => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openAccountBalance(account);
    }
  });
  return row;
}

function openAccountBalance(account) {
  state.activeAccount = account;
  state.accountBalanceTab = "balance";
  document.querySelector("#account-balance-title").textContent = account.alias;
  renderAccountBalanceModal();
  elements.accountBalanceModal.hidden = false;
}

function renderAccountBalanceModal() {
  const account = state.activeAccount;
  if (!account) return;
  const body = elements.accountBalanceBody;
  body.replaceChildren();
  const target = account.target === "LIVE" ? "실전" : account.target === "PAPER" ? "모의" : account.target;
  const meta = document.createElement("div");
  meta.className = "account-modal-meta";
  meta.textContent = `${account.connection_label || account.provider || "API"} · ${target || ""} · ${account.masked_account || ""} · ${account.message || ""}`;
  const tabs = document.createElement("div");
  tabs.className = "balance-tabs account-balance-tabs";
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "계좌 상세 내용");
  for (const [id, label] of [["balance", "잔고"], ["trades", "매매기록"]]) {
    const button = document.createElement("button");
    const active = state.accountBalanceTab === id;
    button.className = `balance-tab${active ? " active" : ""}`;
    button.type = "button";
    button.textContent = label;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(active));
    button.addEventListener("click", () => {
      state.accountBalanceTab = id;
      renderAccountBalanceModal();
    });
    tabs.append(button);
  }

  body.append(meta, tabs);
  if (state.accountBalanceTab === "trades") {
    const performance = account.trade_performance || {};
    const metrics = document.createElement("div");
    metrics.className = "strategy-performance-grid account-modal-metrics account-trade-metrics";
    metrics.append(
      metricCard("실현 손익", performance.realized_profit, { signed: true }),
      metricCard("평가 손익", performance.unrealized_profit, { signed: true }),
      metricCard("누적 매수", performance.filled_buy_amount),
      metricCard("누적 매도", performance.filled_sell_amount),
      metricCard("수수료·세금", Number(performance.commission || 0) + Number(performance.tax || 0)),
    );
    const note = document.createElement("p");
    note.className = "account-trade-note";
    note.textContent = "이 계좌에서 Trade Router가 제출한 체결을 기준으로 계산합니다.";
    body.append(
      metrics,
      note,
      tradeHistoryTable(account.trade_history || [], "이 계좌의 매매기록이 없습니다."),
    );
    return;
  }

  const metrics = document.createElement("div");
  metrics.className = "strategy-performance-grid account-modal-metrics";
  metrics.append(
    metricCard("총 자산", account.total_value),
    metricCard("예수금", account.cash),
    metricCard("보유 평가액", account.market_value),
    metricCard("평가손익", account.unrealized_profit, { signed: true }),
  );
  const positionsHeading = document.createElement("div");
  positionsHeading.className = "balance-subheading";
  positionsHeading.innerHTML = `<strong>보유 종목</strong><span>${account.position_count || 0}종목</span>`;
  const limitHeading = document.createElement("div");
  limitHeading.className = "balance-subheading";
  limitHeading.innerHTML = "<strong>계좌 주문 한도</strong><span>계좌 설정 기준</span>";
  const limits = document.createElement("div");
  limits.className = "limit-list account-modal-limits";
  appendLimitRow(limits, "1회 주문금액", account.limits?.max_order_amount);
  appendLimitRow(limits, "일일 누적 매수금액", account.limits?.max_daily_buy_amount);
  appendLimitRow(limits, "일일 누적 매수횟수", account.limits?.max_daily_buy_count, "times");
  appendLimitRow(limits, "최대 보유 노출액", account.limits?.max_exposure);
  appendLimitRow(limits, "동시 미체결 주문", account.limits?.max_open_orders, "count");
  body.append(
    metrics,
    positionsHeading,
    positionTable(
      account.positions || [],
      "이 계좌에 보유 종목이 없습니다.",
      { source: account.connection_id || "AUTO" },
    ),
    limitHeading,
    limits,
  );
}

function closeAccountBalance() {
  elements.accountBalanceModal.hidden = true;
  state.activeAccount = null;
  state.accountBalanceTab = "balance";
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
  SELL_SCAN_SECONDS: ["schedule", "보완 판단 간격(초)"],
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
  sell_scan_seconds: "number",
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
  const custom = new Set([
    "buy_conditions", "sell_conditions", "buy_condition_groups", "sell_condition_groups",
    "buy_rule_tree", "sell_rule_tree",
    "buy_condition_logic", "sell_condition_logic", "bar_interval", "bar_data_mode",
    "bollinger_period", "bollinger_stddev", "buy_flows", "sell_flows",
  ]);
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
    fields: [
      ...blockPackageFields(),
      ...blockBuyFlows().flatMap(blockBuyFlowFields),
      ...blockSellFlows().flatMap(blockSellFlowFields),
    ],
  };
}

const BLOCK_BUY_FLOW_FIELDS = [
  "target_source", "hts_user_id", "condition_id", "expected_condition_name",
  "ranking_kind", "ranking_venue", "ranking_market", "ranking_limit", "symbols",
  "buy_scan_seconds", "buy_start_time", "buy_end_time", "buy_condition_groups", "buy_rule_tree",
];

const BLOCK_SELL_FLOW_FIELDS = [
  "sell_scan_seconds", "sell_start_time", "sell_end_time", "sell_condition_groups", "sell_rule_tree",
];

function blockBuyFlows() {
  const flows = state.blockPackage?.settings?.buy_flows;
  return Array.isArray(flows) ? flows : [];
}

function blockBuyFlow(flowId) {
  return blockBuyFlows().find(flow => flow.id === flowId);
}

function blockBuyFlowFields(flow) {
  return ["name", ...BLOCK_BUY_FLOW_FIELDS]
    .filter(key => !["expected_condition_name", "buy_condition_groups", "buy_rule_tree"].includes(key))
    .map(settingKey => {
      const sourceName = packageFieldSourceName(settingKey);
      const [category, label] = genericConstantMeta(sourceName);
      return {
        id: `flow:${flow.id}:${settingKey}`, flowKind: "buy", flowId: flow.id, settingKey, sourceName,
        label: settingKey === "name" ? "흐름 이름" : label, category,
        type: BLOCK_PACKAGE_FIELD_TYPES[settingKey] || "string", value: flow[settingKey],
      };
    });
}

function blockSellFlows() {
  const flows = state.blockPackage?.settings?.sell_flows;
  return Array.isArray(flows) ? flows : [];
}

function blockSellFlow(flowId) {
  return blockSellFlows().find(flow => flow.id === flowId);
}

function blockSellFlowFields(flow) {
  return ["name", ...BLOCK_SELL_FLOW_FIELDS]
    .filter(key => !["sell_condition_groups", "sell_rule_tree"].includes(key))
    .map(settingKey => {
      const sourceName = packageFieldSourceName(settingKey);
      const [category, label] = genericConstantMeta(sourceName);
      return {
        id: `sell-flow:${flow.id}:${settingKey}`, flowKind: "sell", flowId: flow.id,
        settingKey, sourceName, label: settingKey === "name" ? "흐름 이름" : label,
        category, type: BLOCK_PACKAGE_FIELD_TYPES[settingKey] || "string", value: flow[settingKey],
      };
    });
}

function blockFlow(kind, flowId) {
  return kind === "sell" ? blockSellFlow(flowId) : blockBuyFlow(flowId);
}

function blockFlowNodePrefix(kind, flowId) {
  return kind === "sell" ? `sell-flow-${flowId}` : `flow-${flowId}`;
}

function blockConditionScope(flow) {
  const settings = state.blockPackage?.settings;
  const connection = state.connections.find(item => (
    item.id === settings?.condition_source || item.label === settings?.condition_source
  ));
  const userId = flow?.hts_user_id;
  if (!connection || !userId || String(userId).startsWith("등록한 ")) return null;
  return { key: `${connection.id}\u0000${userId}`, source: connection.label, userId };
}

function blockConditionData(flow) {
  const scope = blockConditionScope(flow);
  return scope ? state.blockConditionCache.get(scope.key) || { conditions: [] } : { conditions: [] };
}

function addBlockBuyFlow() {
  if (!flushBlockFieldEdits(false)) return;
  const settings = state.blockPackage?.settings;
  const flows = blockBuyFlows();
  if (!settings || flows.length >= 8 || selectedState()?.running) return;
  const previous = flows[flows.length - 1];
  const end = previous?.buy_end_time || "09:00";
  const parts = String(end).split(":").map(Number);
  const minutes = Math.min(23 * 60, (parts[0] || 0) * 60 + (parts[1] || 0));
  const clock = value => `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
  const flow = {
    id: `flow-${Date.now().toString(36)}-${++state.conditionGroupSerial}`,
    name: `매수 흐름 ${flows.length + 1}`,
    target_source: previous?.target_source || "HTS_CONDITION",
    hts_user_id: previous?.hts_user_id || settings.hts_user_id || "",
    condition_id: "", expected_condition_name: "",
    ranking_kind: previous?.ranking_kind || "RISE",
    ranking_venue: previous?.ranking_venue || "KRX",
    ranking_market: previous?.ranking_market || "ALL", ranking_limit: previous?.ranking_limit ?? 20,
    symbols: [], buy_scan_seconds: previous?.buy_scan_seconds || 30,
    buy_start_time: clock(minutes), buy_end_time: clock(Math.min(23 * 60 + 59, minutes + 60)),
    buy_condition_groups: [],
    buy_rule_tree: newBlockRuleRoot("buy", [], true),
  };
  flows.push(flow);
  setScheduleSectionExpanded(`flow:${flow.id}`, true);
  state.blockSelectedNode = `flow-${flow.id}-schedule`;
  setDirty(true);
  renderStrategyBlocks();
  refreshBlockConditions().catch(error => notify(error.message, true));
}

function removeBlockBuyFlow(flowId) {
  const flows = blockBuyFlows();
  const index = flows.findIndex(flow => flow.id === flowId);
  if (index < 0 || flows.length <= 1 || selectedState()?.running) return;
  flows.splice(index, 1);
  if (String(state.blockSelectedNode || "").startsWith(`flow-${flowId}-`)) state.blockSelectedNode = "connection";
  setDirty(true);
  renderStrategyBlocks();
}

function addBlockSellFlow() {
  if (!flushBlockFieldEdits(false)) return;
  const settings = state.blockPackage?.settings;
  const flows = blockSellFlows();
  if (!settings || flows.length >= 8 || selectedState()?.running) return;
  const previous = flows[flows.length - 1];
  const startValue = previous?.sell_end_time || settings.sell_start_time || "09:00";
  const parts = String(startValue).split(":").map(Number);
  const minutes = Math.min(23 * 60 + 58, (parts[0] || 0) * 60 + (parts[1] || 0));
  const clock = value => `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
  const flow = {
    id: `sell-flow-${Date.now().toString(36)}-${++state.conditionGroupSerial}`,
    name: `매도 흐름 ${flows.length + 1}`,
    sell_scan_seconds: previous?.sell_scan_seconds || settings.buy_scan_seconds || 30,
    sell_start_time: clock(minutes),
    sell_end_time: clock(Math.min(23 * 60 + 59, minutes + 60)),
    sell_condition_groups: [newConditionGroup("sell")],
    sell_rule_tree: newBlockRuleRoot("sell", [newBlockRuleCondition("sell")], true),
  };
  flows.push(flow);
  setScheduleSectionExpanded(`sell-flow:${flow.id}`, true);
  state.blockSelectedNode = `${blockFlowNodePrefix("sell", flow.id)}-schedule`;
  setDirty(true);
  renderStrategyBlocks();
}

function removeBlockSellFlow(flowId) {
  const flows = blockSellFlows();
  const index = flows.findIndex(flow => flow.id === flowId);
  if (index < 0 || flows.length <= 1 || selectedState()?.running) return;
  flows.splice(index, 1);
  if (String(state.blockSelectedNode || "").startsWith(`${blockFlowNodePrefix("sell", flowId)}-`)) {
    state.blockSelectedNode = `${blockFlowNodePrefix("sell", flows[Math.max(0, index - 1)].id)}-schedule`;
  }
  setDirty(true);
  renderStrategyBlocks();
}

function blockFieldOptions(field) {
  const settings = state.blockPackage?.settings || {};
  const flow = field.flowId ? blockFlow(field.flowKind || "buy", field.flowId) : null;
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
        const sources = new Set(blockBuyFlows().map(item => item.target_source));
        if (!sources.size) sources.add(settings.target_source || "HTS_CONDITION");
        return (!sources.has("HTS_CONDITION") || Boolean(capability?.conditions))
          && (!sources.has("RANKING") || Boolean(capability?.rankings))
          && (!sources.has("SYMBOLS") || Boolean(capability?.quotes && capability?.bars));
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
    return blockConditionData(flow || settings).conditions.map(condition => ({
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

function applyBlockFieldEdit(fieldId, value, { render = true } = {}) {
  const parsed = currentBlockModel();
  const field = [
    ...parsed.fields,
    ...parsed.schedules.flatMap(schedule => schedule.fields),
  ].find(item => item.id === fieldId);
  if (!field) throw new Error("블록 구성이 변경되어 필드 위치를 찾지 못했습니다. 블록 화면을 다시 열어주세요.");
  if (field.settingKey && state.blockPackage) {
    const normalized = normalizeBlockPackageValue(field, value);
    const settings = state.blockPackage.settings;
    const flowKind = field.flowKind || "buy";
    const target = field.flowId ? blockFlow(flowKind, field.flowId) : settings;
    if (!target) return;
    const changed = JSON.stringify(target[field.settingKey]) !== JSON.stringify(normalized);
    if (changed) {
      target[field.settingKey] = normalized;
      if (field.settingKey === "condition_source") {
        const connection = state.connections.find(item => item.id === normalized || item.label === normalized);
        for (const flow of blockBuyFlows()) {
          if (connection?.realtime_user_id) flow.hts_user_id = connection.realtime_user_id;
          flow.expected_condition_name = "";
        }
      } else if (field.settingKey === "condition_id") {
        const selected = blockConditionData(target).conditions.find(item => String(item.id) === normalized);
        target.expected_condition_name = selected?.name || "";
      } else if (field.settingKey === "hts_user_id") {
        target.expected_condition_name = "";
      }
      setDirty(true);
      if (!render && field.flowId) {
        const scheduleKey = flowKind === "sell" ? `sell-flow:${field.flowId}` : `flow:${field.flowId}`;
        const panel = [...elements.blockCanvas.querySelectorAll("[data-schedule-key]")]
          .find(node => node.dataset.scheduleKey === scheduleKey);
        const heading = panel?.querySelector(".schedule-section-copy strong");
        const start = flowKind === "sell" ? target.sell_start_time : target.buy_start_time;
        const end = flowKind === "sell" ? target.sell_end_time : target.buy_end_time;
        if (heading) heading.textContent = `${start}–${end}  ${target.name}`;
        const badge = panel?.querySelector(".schedule-section-badge");
        const seconds = flowKind === "sell" ? target.sell_scan_seconds : target.buy_scan_seconds;
        if (badge) badge.textContent = `${seconds}초마다`;
      }
    }
    if (render) {
      renderStrategyBlocks();
      if (["condition_source", "hts_user_id", "target_source"].includes(field.settingKey)) {
        refreshBlockConditions().catch(error => notify(error.message, true));
      }
    }
    return;
  }
  const replacement = formatBlockFieldValue(field, value);
  elements.editor.value = `${elements.editor.value.slice(0, field.start)}${replacement}${elements.editor.value.slice(field.end)}`;
  elements.editor.dispatchEvent(new Event("input"));
  renderStrategyBlocks();
}

function flushBlockFieldEdits(strict = true) {
  if (!state.blockPackage || !elements.blockCanvas) return true;
  for (const control of elements.blockCanvas.querySelectorAll("[data-block-field-id], [data-block-condition-input]")) {
    if (control.disabled || control._blockPackageOwner !== state.blockPackage || control.value === control._blockCommittedValue) continue;
    try {
      control._commitBlockField(false);
    } catch (error) {
      control.setCustomValidity(error.message);
      if (strict) throw error;
      return false;
    }
  }
  return flushConditionFormulaDrafts(strict);
}

function settleBlockFieldEditsForRerender() {
  if (!state.blockPackage || !elements.blockCanvas) return;
  for (const control of elements.blockCanvas.querySelectorAll("[data-block-field-id], [data-block-condition-input]")) {
    if (control.disabled || control._blockPackageOwner !== state.blockPackage || control.value === control._blockCommittedValue) continue;
    try {
      control._commitBlockField(false);
    } catch (_) {
      // A half-entered numeric value must not trap the user in the inspector
      // or leave controls editable after the strategy starts elsewhere.
      control.value = control._blockCommittedValue;
      control.setCustomValidity("");
    }
  }
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
        ? (blockConditionData(field.flowId ? blockBuyFlow(field.flowId) : state.blockPackage?.settings).error || "조회 가능한 조건식 없음")
        : "등록된 항목 없음";
      control.append(option);
    }
    if (field.value != null && !options.some(item => String(item.value) === String(field.value))) {
      options.unshift({
        value: String(field.value),
        label: field.value === ""
          ? (field.settingKey === "condition_id" ? "조건식을 선택하세요" : "선택하세요")
          : `${field.value} · 현재 설정`,
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
    control.type = field.type === "date" ? "date"
      : field.flowId && ["buy_start_time", "buy_end_time", "sell_start_time", "sell_end_time"].includes(field.settingKey) ? "time"
        : ["number", "decimal"].includes(field.type) ? "number" : "text";
    if (control.type === "time") control.step = "1";
    if (["number", "decimal"].includes(field.type)) control.step = "any";
    control.value = field.value;
  }
  control.id = label.htmlFor;
  control.className = "block-field-control";
  control.disabled = disabled;
  control.dataset.blockFieldId = field.id;
  control._blockPackageOwner = state.blockPackage;
  control._blockCommittedValue = control.value;
  control._commitBlockField = (render = true) => {
    applyBlockFieldEdit(field.id, control.value, { render });
    control._blockCommittedValue = control.value;
    control.setCustomValidity("");
  };
  if (field.settingKey && state.blockPackage && control.tagName !== "SELECT") {
    control.addEventListener("input", () => {
      try {
        // Commit text as it is entered without replacing the focused DOM control.
        control._commitBlockField(false);
      } catch (error) {
        // Keep an incomplete numeric edit visible; Save validates it before sending.
        control.setCustomValidity(error.message);
      }
    });
  }
  control.addEventListener("change", () => {
    try {
      control._commitBlockField(true);
    } catch (error) {
      control.setCustomValidity(error.message);
      notify(error.message, true);
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

function createComposerTreeRow(definition, disabled) {
  const node = document.createElement("article");
  node.className = `composer-tree-card composer-tree-row ${definition.tone || "general"}`;
  node.classList.toggle("composer-tree-action", Boolean(definition.ruleActionOwner));
  node.dataset.blockNode = definition.id;
  const selected = state.blockSelectedNode === definition.id;
  node.classList.toggle("selected", selected);
  node.classList.toggle("has-summary", Boolean(definition.summary?.filter(Boolean).length));
  node.tabIndex = state.blockSelectedNode ? (selected ? 0 : -1) : (definition.id === "connection" ? 0 : -1);
  node.setAttribute("role", "treeitem");
  node.setAttribute("aria-selected", selected ? "true" : "false");
  const heading = document.createElement("div");
  heading.className = "composer-tree-row-main";
  const logic = document.createElement("span");
  logic.className = `composer-tree-badge composer-tree-logic ${definition.logicTone || definition.tone || "general"}`;
  logic.textContent = definition.logic || "블록";
  const copy = document.createElement("span");
  copy.className = "composer-node-copy";
  const name = document.createElement("strong");
  name.textContent = definition.title;
  const detail = document.createElement("small");
  detail.textContent = definition.meta || "";
  copy.append(name, detail);
  const edit = document.createElement("span");
  edit.className = "composer-tree-edit composer-node-edit";
  edit.setAttribute("aria-hidden", "true");
  const select = event => {
    event?.stopPropagation();
    state.blockSelectedNode = definition.id;
    renderStrategyBlocks();
    focusComposerInspector();
  };
  heading.append(logic, copy, edit);
  const summary = document.createElement("div");
  summary.className = "composer-tree-summary";
  (definition.summary || []).filter(Boolean).slice(0, 3).forEach(text => {
    const chip = document.createElement("span");
    chip.className = "composer-summary-chip";
    chip.textContent = text;
    summary.append(chip);
  });
  node.append(heading);
  if (summary.childElementCount) node.append(summary);
  node.addEventListener("click", select);
  node.addEventListener("keydown", event => {
    if (event.target !== node) return;
    if (["Enter", " "].includes(event.key)) {
      event.preventDefault();
      select(event);
      return;
    }
    if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    const rows = [...elements.blockCanvas.querySelectorAll(".composer-tree-row[data-block-node]")];
    const index = rows.indexOf(node);
    const nextIndex = event.key === "Home" ? 0
      : event.key === "End" ? rows.length - 1
        : Math.min(rows.length - 1, Math.max(0, index + (event.key === "ArrowDown" ? 1 : -1)));
    const next = rows[nextIndex];
    if (!next || next === node) return;
    event.preventDefault();
    rows.forEach(row => { row.tabIndex = row === next ? 0 : -1; });
    next.focus({ preventScroll: true });
  });
  node.setAttribute("aria-label", `${definition.title}. ${definition.meta || ""}. Enter로 편집`);
  node.classList.toggle("read-only", disabled);
  return node;
}

function focusComposerInspector() {
  window.requestAnimationFrame(() => {
    elements.blockCanvas.querySelector(".composer-inspector-close")?.focus({ preventScroll: true });
  });
}

function focusComposerNode(id) {
  if (!id) return;
  window.requestAnimationFrame(() => {
    const node = [...elements.blockCanvas.querySelectorAll("[data-block-node]")]
      .find(candidate => candidate.dataset.blockNode === id);
    node?.focus({ preventScroll: true });
  });
}

function closeComposerInspector(returnToNode = state.blockSelectedNode) {
  settleBlockFieldEditsForRerender();
  state.blockSelectedNode = false;
  renderStrategyBlocks();
  focusComposerNode(returnToNode);
}

function createComposerInspector(definition, running, flowCounts = {}) {
  if (!definition) return null;
  const inspector = document.createElement("aside");
  inspector.className = "composer-inspector";
  inspector.dataset.blockInspector = definition.id;
  inspector.setAttribute("role", "region");
  inspector.setAttribute("aria-label", `${definition.title} 상세 설정`);
  const heading = document.createElement("header");
  heading.className = "composer-inspector-header";
  const copy = document.createElement("div");
  copy.className = "composer-inspector-heading";
  const eyebrow = document.createElement("span");
  eyebrow.className = `composer-logic-pill ${definition.logicTone || definition.tone || "general"}`;
  eyebrow.textContent = definition.logic || "블록";
  const title = document.createElement("strong");
  title.textContent = definition.title;
  const meta = document.createElement("small");
  meta.textContent = definition.meta || "선택한 블록의 설정";
  copy.append(eyebrow, title, meta);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "composer-inspector-close";
  close.textContent = "×";
  close.title = "상세 편집 닫기";
  close.setAttribute("aria-label", "상세 편집 닫기");
  close.addEventListener("click", () => closeComposerInspector(definition.id));
  heading.append(copy, close);
  inspector.append(heading);

  const body = document.createElement("div");
  body.className = "composer-inspector-body";
  if (definition.description) {
    const description = document.createElement("p");
    description.className = "composer-inspector-description";
    description.textContent = definition.description;
    body.append(description);
  }
  if (definition.fields?.length) body.append(createScheduleFields(definition.fields, running));
  if (definition.conditionGroup) {
    const group = definition.conditionGroup;
    body.append(createConditionEditor(group.kind, group.index, running, group.flowId));
  }
  if (definition.ruleNode) body.append(createBlockRuleNodeEditor(definition.ruleNode, running));
  if (definition.ruleActionOwner) body.append(createBlockRuleActionEditor(definition.ruleActionOwner, running));
  if (definition.emptyBuyFlowId) body.append(createEmptyBuyConditionEditor(running, definition.emptyBuyFlowId));
  if (definition.actions?.length) {
    const actions = document.createElement("div");
    actions.className = "composer-inspector-actions";
    definition.actions.forEach(action => {
      const item = document.createElement("span");
      item.textContent = action;
      actions.append(item);
    });
    body.append(actions);
  }
  if (definition.flowId) {
    const flowKind = definition.flowKind || "buy";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button quiet compact danger-text composer-remove-flow";
    remove.textContent = `이 ${flowKind === "sell" ? "매도" : "매수"} 흐름 삭제`;
    remove.disabled = running || (flowKind === "sell" ? flowCounts.sell : flowCounts.buy) <= 1;
    remove.addEventListener("click", () => (
      flowKind === "sell" ? removeBlockSellFlow(definition.flowId) : removeBlockBuyFlow(definition.flowId)
    ));
    body.append(remove);
  }
  inspector.append(body);
  return inspector;
}

function createAttachedRuleActionEditor(kind, flowId, node, disabled) {
  const section = document.createElement("section");
  section.className = "rule-attached-action";
  const heading = document.createElement("div");
  heading.className = "rule-editor-section-heading";
  const title = document.createElement("strong");
  title.textContent = "통과하면";
  const hint = document.createElement("span");
  hint.textContent = node.action ? "여기서 주문 실행" : "주문 없이 다음 판단으로";
  heading.append(title, hint);
  section.append(heading);
  if (node.action) {
    const summary = document.createElement("div");
    summary.className = `rule-action-summary ${kind}`;
    const side = document.createElement("strong");
    side.textContent = kind === "sell" ? "매도 주문" : "매수 주문";
    const order = document.createElement("span");
    order.textContent = node.action.order_type === "LIMIT" ? "현재가 지정가" : "시장가";
    summary.append(side, order);
    section.append(summary);
  } else {
    const add = document.createElement("button");
    add.type = "button";
    add.className = "button quiet compact rule-add-action";
    add.textContent = `+ ${kind === "sell" ? "매도" : "매수"} 행동 추가`;
    add.disabled = disabled;
    add.addEventListener("click", () => addBlockRuleAction(kind, flowId, node.id));
    section.append(add);
  }
  return section;
}

function createBlockRuleNodeEditor(reference, disabled) {
  const found = findBlockRuleNode(reference.kind, reference.flowId, reference.nodeId);
  const node = found?.node;
  const wrap = document.createElement("div");
  wrap.className = "rule-node-editor";
  if (!node) return wrap;
  if (node.kind === "logic") {
    const section = document.createElement("section");
    section.className = "rule-editor-section";
    const title = document.createElement("strong");
    title.className = "rule-editor-section-title";
    title.textContent = "조건 연결";
    section.append(title);
    (node.children || []).slice(1).forEach((child, offset) => {
      const boundaryIndex = offset + 1;
      const logic = conditionSelect(
        [["ALL", "AND · 모두 충족"], ["ANY", "OR · 하나 이상 충족"]],
        node.operator || "ALL", disabled,
        value => setBlockRuleConnection(reference.kind, reference.flowId, node.id, boundaryIndex, value),
      );
      section.append(formulaControl(`${boundaryIndex}번과 ${boundaryIndex + 1}번 사이`, logic, true));
    });
    const help = document.createElement("p");
    help.className = "composer-inspector-hint";
    help.textContent = "조건 사이의 AND·OR를 눌러 그 연결만 바꿉니다. 괄호 안을 먼저 판단하며, OR에서는 먼저 참인 경로의 주문을 실행합니다.";
    section.append(help);
    const adds = document.createElement("div");
    adds.className = "rule-node-add-actions";
    const addControl = (label, childKind, operator = "ALL") => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button quiet compact";
      button.textContent = label;
      const addedNodes = childKind === "logic" ? 2 : 1;
      const addedDepth = childKind === "logic" ? 2 : 1;
      button.disabled = disabled
        || (node.children || []).length >= 20
        || blockRuleNodeCount(blockRuleTree(reference.kind, reference.flowId)) + addedNodes > 100
        || Number(found.depth || 0) + addedDepth > 8;
      button.addEventListener("click", () => addBlockRuleChild(
        reference.kind, reference.flowId, node.id, childKind, operator,
      ));
      adds.append(button);
    };
    addControl("+ 조건식", "condition");
    addControl("+ 괄호 묶음", "logic", "ALL");
    section.append(adds);
    wrap.append(section);
  } else {
    wrap.append(createConditionFormulaEditor(reference.kind, node, disabled));
  }
  wrap.append(createAttachedRuleActionEditor(reference.kind, reference.flowId, node, disabled));
  if (!reference.isRoot) {
    const group = document.createElement("button");
    group.type = "button";
    group.className = "button quiet compact";
    group.textContent = "( ) 이 조건을 괄호로 묶기";
    group.disabled = disabled;
    group.addEventListener("click", () => wrapBlockRuleNode(reference.kind, reference.flowId, node.id));
    wrap.append(group);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button quiet compact danger-text rule-remove-node";
    remove.textContent = node.kind === "logic" ? "이 조건 묶음 삭제" : "이 조건식 삭제";
    remove.disabled = disabled;
    remove.addEventListener("click", () => removeBlockRuleNode(reference.kind, reference.flowId, node.id));
    wrap.append(remove);
  }
  return wrap;
}

function createBlockRuleActionEditor(reference, disabled) {
  const node = findBlockRuleNode(reference.kind, reference.flowId, reference.nodeId)?.node;
  const wrap = document.createElement("div");
  wrap.className = "rule-action-editor";
  if (!node?.action) return wrap;
  const hint = document.createElement("p");
  hint.className = "composer-inspector-hint";
  hint.textContent = reference.kind === "sell"
    ? "이 조건 경로가 통과하면 전략 소유 수량을 선택한 방식으로 매도합니다."
    : "이 조건 경로가 통과하면 공통 예산과 주문 위험 검사를 거쳐 매수합니다.";
  const order = conditionSelect(
    [["MARKET", "시장가"], ["LIMIT", "현재가 지정가"]],
    node.action.order_type || "MARKET", disabled,
    (value, render) => updateBlockRuleAction(reference.kind, reference.flowId, node.id, value, render),
  );
  const field = formulaControl("주문 방식", order, true);
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "button quiet compact danger-text";
  remove.textContent = "이 행동 삭제";
  remove.disabled = disabled;
  remove.addEventListener("click", () => removeBlockRuleAction(reference.kind, reference.flowId, node.id));
  wrap.append(hint, field, remove);
  return wrap;
}

const BLOCK_CONDITION_METRICS = [
  ["open", "시가"], ["high", "고가"], ["low", "저가"], ["close", "종가"],
  ["heikin_open", "하이킨아시 시가"], ["heikin_high", "하이킨아시 고가"],
  ["heikin_low", "하이킨아시 저가"], ["heikin_close", "하이킨아시 종가"],
  ["volume", "거래량"], ["obv", "OBV"], ["atr", "ATR"], ["bollinger_upper", "볼린저 상단"],
  ["bollinger_middle", "볼린저 중심"], ["bollinger_lower", "볼린저 하단"],
  ["average_price", "평균 매수가"], ["profit_rate", "실시간 수익률(평균가 대비 %)"],
];

const BLOCK_CONDITION_OPERATORS = [
  ["LT", "<"], ["LTE", "≤"], ["GT", ">"], ["GTE", "≥"], ["EQ", "="],
  ["CROSS_UP", "상향돌파"], ["CROSS_DOWN", "하향돌파"],
];

const BLOCK_EXPRESSION_OPERATORS = [
  ["ADD", "+"], ["SUBTRACT", "−"], ["MULTIPLY", "×"], ["DIVIDE", "÷"],
];

const BLOCK_EXPRESSION_METRICS = [
  ["constant", "숫자"],
  ...BLOCK_CONDITION_METRICS.filter(([key]) => key !== "obv"),
];

function blockSourceMaxHistory(interval = "3m") {
  if (!String(interval).endsWith("m")) return 400;
  const minutes = Math.max(1, Number.parseInt(String(interval), 10) || 1);
  return Math.floor(1440 / minutes);
}

function blockAtrMaxPeriod(interval = "3m") {
  return Math.max(1, Math.min(400, blockSourceMaxHistory(interval) - 1));
}

function clampBlockAtrPeriod(value, interval = "3m") {
  const maximum = blockAtrMaxPeriod(interval);
  return Math.min(maximum, Math.max(1, Number.parseInt(value, 10) || Math.min(14, maximum)));
}

function conditionSideExpression(condition, side) {
  const expression = condition?.[`${side}_expression`];
  return expression && typeof expression === "object" ? expression : null;
}

function walkConditionExpression(expression, visitor) {
  if (!expression || typeof expression !== "object") return;
  visitor(expression);
  if (expression.kind === "binary") {
    walkConditionExpression(expression.left, visitor);
    walkConditionExpression(expression.right, visitor);
  }
}

function expressionContainsMetric(expression, metric) {
  let found = false;
  walkConditionExpression(expression, node => {
    if (node.kind === "metric" && node.metric === metric) found = true;
  });
  return found;
}

function expressionDepth(expression) {
  if (!expression || expression.kind !== "binary") return 0;
  return 1 + Math.max(expressionDepth(expression.left), expressionDepth(expression.right));
}

function expressionNodeCount(expression) {
  if (!expression || expression.kind !== "binary") return expression ? 1 : 0;
  return 1 + expressionNodeCount(expression.left) + expressionNodeCount(expression.right);
}

function expressionNodeAt(expression, path) {
  return path.reduce((node, key) => node?.kind === "binary" ? node[key] : null, expression);
}

function replaceExpressionNode(expression, path, replacement) {
  if (!path.length) return replacement;
  const parent = expressionNodeAt(expression, path.slice(0, -1));
  if (parent?.kind === "binary") parent[path.at(-1)] = replacement;
  return expression;
}

function selectedExpressionPath(condition, side, expression) {
  let selections = state.blockExpressionSelections.get(condition);
  if (!selections) {
    selections = { left: [], right: [] };
    state.blockExpressionSelections.set(condition, selections);
  }
  const path = Array.isArray(selections[side]) ? selections[side] : [];
  if (!expressionNodeAt(expression, path)) selections[side] = [];
  return selections[side];
}

function setSelectedExpressionPath(condition, side, path) {
  let selections = state.blockExpressionSelections.get(condition);
  if (!selections) {
    selections = { left: [], right: [] };
    state.blockExpressionSelections.set(condition, selections);
  }
  selections[side] = [...path];
}

function expressionControlScope(kind, groupIndex, conditionIndex, side, flowId = null) {
  return `${kind}:${flowId || "shared"}:${groupIndex}:${conditionIndex}:${side}`;
}

function expressionControlKey(scope, path, field) {
  const node = Array.isArray(path) && path.length ? path.join(".") : "root";
  return `${scope}:${node}:${field}`;
}

function markExpressionControl(control, key) {
  control.dataset.expressionControl = key;
  return control;
}

function expressionMetricLeaf(metric = "close", source = {}) {
  const barInterval = source.bar_interval || "3m";
  return {
    kind: "metric",
    metric,
    offset: Math.max(0, Number.parseInt(source.offset, 10) || 0),
    bar_interval: barInterval,
    bar_data_mode: source.bar_data_mode || "COMPLETE_ONLY",
    bollinger_period: Math.max(2, Number.parseInt(source.bollinger_period, 10) || 55),
    bollinger_stddev: String(source.bollinger_stddev ?? "1.04"),
    atr_period: clampBlockAtrPeriod(source.atr_period, barInterval),
  };
}

function legacyOperandExpression(condition, side) {
  const metric = condition?.[side] || "close";
  if (metric === "value") return { kind: "constant", value: String(condition?.value ?? "0") };
  return expressionMetricLeaf(metric, {
    offset: condition?.[`${side}_offset`],
    bar_interval: condition?.[`${side}_bar_interval`],
    bar_data_mode: condition?.[`${side}_bar_data_mode`],
    bollinger_period: condition?.[`${side}_bollinger_period`],
    bollinger_stddev: condition?.[`${side}_bollinger_stddev`],
    atr_period: condition?.[`${side}_atr_period`],
  });
}

function displayedExpressionOffset(expression) {
  const offset = Math.max(0, Number.parseInt(expression?.offset, 10) || 0);
  return (expression?.bar_data_mode || "COMPLETE_ONLY") === "COMPLETE_ONLY" ? offset + 1 : offset;
}

function expressionBarPosition(expression) {
  const displayed = displayedExpressionOffset(expression);
  if ((expression?.bar_data_mode || "COMPLETE_ONLY") === "COMPLETE_ONLY") return `${displayed}봉 전 완성봉`;
  return displayed === 0 ? "현재 진행봉" : `${displayed}봉 전 (진행봉 포함)`;
}

function expressionIntervalLabel(interval = "3m") {
  return interval === "1d" ? "일봉" : `${String(interval).replace("m", "")}분봉`;
}

function conditionExpressionSummary(expression) {
  if (!expression || typeof expression !== "object") return "값 미설정";
  if (expression.kind === "constant") return String(expression.value ?? "0");
  if (expression.kind === "binary") {
    const operator = new Map(BLOCK_EXPRESSION_OPERATORS).get(expression.operator) || expression.operator || "+";
    return `(${conditionExpressionSummary(expression.left)} ${operator} ${conditionExpressionSummary(expression.right)})`;
  }
  const label = new Map(BLOCK_CONDITION_METRICS).get(expression.metric) || expression.metric || "지표";
  if (expression.metric === "average_price" || expression.metric === "profit_rate") return label;
  const interval = expressionIntervalLabel(expression.bar_interval);
  const position = expressionBarPosition(expression);
  if (expression.metric === "atr") return `ATR(${expression.atr_period || 14}, ${interval}, ${position})`;
  if (String(expression.metric).startsWith("bollinger_")) {
    return `${label}(${expression.bollinger_period || 55}, ${expression.bollinger_stddev || "1.04"}, ${interval}, ${position})`;
  }
  return `${label}(${interval}, ${position})`;
}

function atrVolatilityRateSource(expression) {
  if (expression?.kind !== "binary" || expression.operator !== "MULTIPLY") return null;
  const candidates = [[expression.left, expression.right], [expression.right, expression.left]];
  for (const [ratio, scale] of candidates) {
    if (scale?.kind !== "constant" || Number(scale.value) !== 100) continue;
    if (ratio?.kind !== "binary" || ratio.operator !== "DIVIDE") continue;
    if (ratio.left?.kind !== "metric" || ratio.left.metric !== "atr") continue;
    if (ratio.right?.kind !== "metric" || ratio.right.metric !== "close") continue;
    const sourceKey = node => [
      node.bar_interval || "3m",
      node.bar_data_mode || "COMPLETE_ONLY",
      Math.max(0, Number.parseInt(node.offset, 10) || 0),
    ].join(":");
    if (sourceKey(ratio.left) !== sourceKey(ratio.right)) continue;
    return ratio.left;
  }
  return null;
}

function conditionExpressionCompactSummary(expression, nested = false) {
  const atrSource = atrVolatilityRateSource(expression);
  if (atrSource) {
    const interval = atrSource.bar_interval === "1d"
      ? "일봉" : `${String(atrSource.bar_interval || "3m").replace("m", "")}분`;
    return `ATR 변동성률(${atrSource.atr_period || 14}) · ${interval} · ${expressionBarPosition(atrSource)}`;
  }
  if (!expression || typeof expression !== "object") return "값 미설정";
  if (expression.kind !== "binary") return conditionExpressionTokenLabel(expression);
  const operator = conditionExpressionTokenLabel(expression);
  const summary = `${conditionExpressionCompactSummary(expression.left, true)} ${operator} ${conditionExpressionCompactSummary(expression.right, true)}`;
  return nested ? `(${summary})` : summary;
}

function conditionExpressionTokenLabel(expression) {
  if (!expression || typeof expression !== "object") return "값 미설정";
  if (expression.kind === "constant") return String(expression.value ?? "0");
  if (expression.kind === "binary") {
    return new Map(BLOCK_EXPRESSION_OPERATORS).get(expression.operator) || expression.operator || "+";
  }
  const label = new Map(BLOCK_CONDITION_METRICS).get(expression.metric) || expression.metric || "지표";
  if (expression.metric === "atr") return `ATR(${expression.atr_period || 14})`;
  if (String(expression.metric).startsWith("bollinger_")) {
    return `${label}(${expression.bollinger_period || 55}, ${expression.bollinger_stddev || "1.04"})`;
  }
  return label;
}

function conditionExpressionMetricLeaves(expression) {
  const leaves = [];
  walkConditionExpression(expression, node => {
    if (node.kind === "metric" && !["average_price", "profit_rate"].includes(node.metric)) leaves.push(node);
  });
  return leaves;
}

function conditionExpressionCommonBarContext(expression) {
  const leaves = conditionExpressionMetricLeaves(expression);
  if (leaves.length < 2) return null;
  const first = leaves[0];
  const signature = node => [
    node.bar_interval || "3m",
    node.bar_data_mode || "COMPLETE_ONLY",
    Math.max(0, Number.parseInt(node.offset, 10) || 0),
  ].join(":");
  if (!leaves.every(node => signature(node) === signature(first))) return null;
  return {
    interval: first.bar_interval || "3m",
    mode: first.bar_data_mode || "COMPLETE_ONLY",
    offset: Math.max(0, Number.parseInt(first.offset, 10) || 0),
    leaves,
  };
}

function isLiveProfitCondition(condition) {
  const metrics = [];
  let hasOtherValue = false;
  for (const side of ["left", "right"]) {
    const expression = conditionSideExpression(condition, side);
    if (!expression) {
      const metric = condition?.[side];
      if (metric && metric !== "value") metrics.push(metric);
      continue;
    }
    walkConditionExpression(expression, node => {
      if (node.kind === "metric") metrics.push(node.metric);
      else if (node.kind !== "constant" && node.kind !== "binary") hasOtherValue = true;
    });
  }
  return !hasOtherValue && metrics.includes("profit_rate") && metrics.every(metric => metric === "profit_rate");
}

function hasBarMetricCondition(condition) {
  for (const side of ["left", "right"]) {
    const expression = conditionSideExpression(condition, side);
    if (expression) {
      let found = false;
      walkConditionExpression(expression, node => {
        if (node.kind === "metric" && !["average_price", "profit_rate"].includes(node.metric)) found = true;
      });
      if (found) return true;
      continue;
    }
    const metric = condition?.[side];
    if (metric && !["value", "average_price", "profit_rate"].includes(metric)) return true;
  }
  return false;
}

function conditionMetricSources(condition) {
  const sources = [];
  for (const side of ["left", "right"]) {
    const expression = conditionSideExpression(condition, side);
    if (expression) {
      walkConditionExpression(expression, node => {
        if (node.kind === "metric") sources.push({
          metric: node.metric,
          interval: node.bar_interval || "3m",
          mode: node.bar_data_mode || "COMPLETE_ONLY",
        });
      });
      continue;
    }
    const metric = condition?.[side];
    if (!metric || metric === "value") continue;
    sources.push({
      metric,
      interval: condition?.[`${side}_bar_interval`] || "3m",
      mode: condition?.[`${side}_bar_data_mode`] || "COMPLETE_ONLY",
    });
  }
  return sources;
}

function conditionCanUseCross(condition) {
  const sources = conditionMetricSources(condition);
  if (sources.some(source => source.metric === "profit_rate")) return false;
  const barSources = sources.filter(source => !["average_price", "profit_rate"].includes(source.metric));
  if (!barSources.length) return false;
  return new Set(barSources.map(source => `${source.interval}:${source.mode}`)).size === 1;
}

function conditionOperatorOptions(condition) {
  if (conditionCanUseCross(condition)) return BLOCK_CONDITION_OPERATORS;
  const ordinary = BLOCK_CONDITION_OPERATORS.filter(([key]) => !["CROSS_UP", "CROSS_DOWN"].includes(key));
  const current = BLOCK_CONDITION_OPERATORS.find(([key]) => key === condition?.operator);
  return current && ["CROSS_UP", "CROSS_DOWN"].includes(current[0])
    ? [...ordinary, [current[0], `${current[1]} (현재 설정 · 변경 필요)`, true]]
    : ordinary;
}

function normalizeConditionAtrPeriods(condition) {
  for (const side of ["left", "right"]) {
    const interval = condition?.[`${side}_bar_interval`] || "3m";
    condition[`${side}_atr_period`] = clampBlockAtrPeriod(condition[`${side}_atr_period`], interval);
    const expression = conditionSideExpression(condition, side);
    walkConditionExpression(expression, node => {
      if (node.kind !== "metric" || node.metric !== "atr") return;
      node.bar_interval ||= "3m";
      node.atr_period = clampBlockAtrPeriod(node.atr_period, node.bar_interval);
    });
  }
  return condition;
}

function displayedConditionOffset(condition, side) {
  const offset = Math.max(0, Number.parseInt(condition?.[`${side}_offset`], 10) || 0);
  return (condition?.[`${side}_bar_data_mode`] || "COMPLETE_ONLY") === "COMPLETE_ONLY"
    ? offset + 1
    : offset;
}

function storedConditionOffset(value, mode) {
  const displayed = Math.max(0, Number.parseInt(value, 10) || 0);
  return mode === "COMPLETE_ONLY" ? Math.max(0, displayed - 1) : displayed;
}

function blockConditionBarPosition(condition, side) {
  const mode = condition?.[`${side}_bar_data_mode`] || "COMPLETE_ONLY";
  const displayed = displayedConditionOffset(condition, side);
  if (mode === "COMPLETE_ONLY") return `${displayed}봉 전 완성봉`;
  return displayed === 0 ? "현재 진행봉" : `${displayed}봉 전 (진행봉 포함)`;
}

function blockConditionTiming(condition) {
  if (isLiveProfitCondition(condition)) return "실시간 현재가";
  const labels = [];
  for (const side of ["left", "right"]) {
    const expression = conditionSideExpression(condition, side);
    if (expression) {
      walkConditionExpression(expression, node => {
        if (node.kind !== "metric") return;
        if (node.metric === "profit_rate") labels.push("실시간 현재가");
        else if (node.metric !== "average_price") {
          labels.push(`${expressionIntervalLabel(node.bar_interval)} ${expressionBarPosition(node)}`);
        }
      });
      continue;
    }
    const metric = condition?.[side];
    if (!metric || metric === "value" || metric === "average_price" || metric === "profit_rate") continue;
    const interval = condition[`${side}_bar_interval`] || "3m";
    const intervalLabel = expressionIntervalLabel(interval);
    labels.push(`${intervalLabel} ${blockConditionBarPosition(condition, side)}`);
  }
  return [...new Set(labels)].join(" · ") || "계좌 값";
}

function bindBlockConditionControl(control, onChange) {
  control.dataset.blockConditionInput = "true";
  control._blockPackageOwner = state.blockPackage;
  control._blockCommittedValue = control.value;
  control._commitBlockField = (render = true) => {
    if (control.type === "number" && !String(control.value).trim()) throw new Error("조건 값에 숫자를 입력하세요.");
    onChange(control.value, render);
    control._blockCommittedValue = control.value;
    control.setCustomValidity("");
  };
  if (control.tagName !== "SELECT") control.addEventListener("input", () => {
    try { control._commitBlockField(false); }
    catch (error) { control.setCustomValidity(error.message); }
  });
  control.addEventListener("change", () => {
    try { control._commitBlockField(true); }
    catch (error) { control.setCustomValidity(error.message); notify(error.message, true); }
  });
}

function conditionSelect(items, value, disabled, onChange) {
  const select = document.createElement("select");
  select.className = "block-condition-control";
  for (const [key, label, optionDisabled = false] of items) {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = label;
    option.disabled = optionDisabled;
    select.append(option);
  }
  select.value = value;
  select.disabled = disabled;
  bindBlockConditionControl(select, onChange);
  return select;
}

function blockConditionGroups(kind, flowId = null) {
  const settings = flowId ? blockFlow(kind, flowId) : state.blockPackage?.settings;
  const groups = settings?.[`${kind}_condition_groups`];
  return Array.isArray(groups) ? groups : [];
}

function nextBlockRuleId(prefix) {
  state.conditionGroupSerial += 1;
  return `${prefix}-${Date.now().toString(36)}-${state.conditionGroupSerial}`;
}

function newBlockOrderAction(kind, orderType = null) {
  const side = kind === "sell" ? "SELL" : "BUY";
  const fallback = state.blockPackage?.settings?.[`${kind}_order_type`] || "MARKET";
  return { kind: "order", id: nextBlockRuleId(`${kind}-order`), side, order_type: orderType || fallback };
}

function newBlockRuleCondition(kind, condition = null) {
  return {
    kind: "condition",
    id: nextBlockRuleId(`${kind}-condition`),
    condition: structuredClone(condition || newBlockTradeCondition(kind)),
    action: null,
  };
}

function newBlockRuleLogic(kind, operator = "ALL", children = [], withAction = false) {
  return {
    kind: "logic",
    id: nextBlockRuleId(`${kind}-logic`),
    operator: operator === "ANY" ? "ANY" : "ALL",
    children,
    action: withAction ? newBlockOrderAction(kind) : null,
  };
}

function newBlockRuleRoot(kind, children = [], withAction = true) {
  const root = newBlockRuleLogic(kind, "ALL", children, withAction);
  root.id = `${kind}-rule-root`;
  return root;
}

function legacyGroupsRuleTree(kind, groups, withAction = true) {
  const children = (groups || []).map(group => newBlockRuleLogic(
    kind,
    "ANY",
    (group.conditions || []).map(condition => newBlockRuleCondition(kind, condition)),
    false,
  ));
  return newBlockRuleRoot(kind, children, withAction);
}

function ensureBlockRuleTree(kind, owner) {
  if (!owner || typeof owner !== "object") return null;
  const key = `${kind}_rule_tree`;
  const current = owner[key];
  if (current && current.kind === "logic" && Array.isArray(current.children)) {
    // Reading a strategy must preserve explicit parentheses, action scopes,
    // node IDs and the user's edit intent.
    return current;
  }
  owner[key] = legacyGroupsRuleTree(kind, owner[`${kind}_condition_groups`] || [], true);
  return owner[key];
}

function blockRuleTree(kind, flowId = null) {
  const owner = flowId ? blockFlow(kind, flowId) : state.blockPackage?.settings;
  return ensureBlockRuleTree(kind, owner);
}

function walkBlockRule(node, visitor, parent = null, index = -1, depth = 0) {
  if (!node || typeof node !== "object") return;
  visitor(node, parent, index, depth);
  if (node.kind === "logic") {
    (node.children || []).forEach((child, childIndex) => walkBlockRule(child, visitor, node, childIndex, depth + 1));
  }
}

function findBlockRuleNode(kind, flowId, nodeId) {
  let found = null;
  walkBlockRule(blockRuleTree(kind, flowId), (node, parent, index, depth) => {
    if (!found && node.id === nodeId) found = { node, parent, index, depth };
  });
  return found;
}

function blockRuleNodeCount(root) {
  let count = 0;
  walkBlockRule(root, () => { count += 1; });
  return count;
}

function promoteLegacyBlockRuleTree(kind, owner, flowId = null) {
  const key = `${kind}_rule_tree`;
  const root = owner?.[key];
  if (!root || !String(root.id || "").startsWith("legacy-")) return;
  walkBlockRule(root, node => {
    const oldNodeId = node.id;
    node.id = nextBlockRuleId(`${kind}-${node.kind === "logic" ? "logic" : "condition"}`);
    const prefix = blockFlowNodePrefix(kind, flowId);
    if (state.blockSelectedNode === `${prefix}-rule-${oldNodeId}`) {
      state.blockSelectedNode = `${prefix}-rule-${node.id}`;
    }
    if (node.action) {
      const oldActionId = node.action.id;
      node.action.id = nextBlockRuleId(`${kind}-order`);
      if (state.blockSelectedNode === `${prefix}-action-${oldActionId}`) {
        state.blockSelectedNode = `${prefix}-action-${node.action.id}`;
      }
    }
  });
  owner[`${kind}_condition_groups`] = [];
}

function promoteLegacyBlockRuleTrees() {
  const settings = state.blockPackage?.settings;
  if (!settings) return;
  promoteLegacyBlockRuleTree("buy", settings);
  promoteLegacyBlockRuleTree("sell", settings);
  (settings.buy_flows || []).forEach(flow => promoteLegacyBlockRuleTree("buy", flow, flow.id));
  (settings.sell_flows || []).forEach(flow => promoteLegacyBlockRuleTree("sell", flow, flow.id));
}

function commitBlockRuleMutation(render = true) {
  // The backend exposes materialized legacy trees for old packages. Once the
  // user edits that tree it becomes authoritative, so give every node a fresh
  // id and drop the stale flat groups that could otherwise rebuild over it.
  promoteLegacyBlockRuleTrees();
  setDirty(true);
  if (render) renderStrategyBlocks();
}

function setBlockRuleOperator(kind, flowId, nodeId, operator) {
  const node = findBlockRuleNode(kind, flowId, nodeId)?.node;
  if (node?.kind !== "logic") return;
  const normalized = operator === "ANY" ? "ANY" : "ALL";
  if (normalized === "ALL" && (node.children || []).filter(blockRuleContainsAction).length > 1) {
    notify("AND 묶음은 행동이 들어 있는 자식 경로를 하나만 가질 수 있습니다. 행동을 정리한 뒤 AND로 바꿔 주세요.", true);
    return;
  }
  node.operator = normalized;
  commitBlockRuleMutation();
}

function rewriteBlockRuleConnection(root, nodeId, boundaryIndex, operator, createId) {
  const changed = structuredClone(root);
  let target = null;
  walkBlockRule(changed, node => { if (node.id === nodeId) target = node; });
  const normalized = operator === "ANY" ? "ANY" : "ALL";
  if (
    target?.kind !== "logic"
    || !Number.isInteger(boundaryIndex)
    || boundaryIndex < 1
    || boundaryIndex >= target.children.length
    || normalized === target.operator
  ) return null;
  const group = children => children.length === 1 ? children[0] : {
    kind: "logic",
    id: createId(),
    operator: "ALL",
    children,
    action: null,
  };
  if (target.children.length === 2) {
    target.operator = normalized;
  } else if (normalized === "ALL") {
    // A OR B OR C -> A OR (B AND C) when only the second join changes.
    // Existing child groups and their actions remain indivisible operands.
    target.children.splice(
      boundaryIndex - 1, 2,
      group(target.children.slice(boundaryIndex - 1, boundaryIndex + 1)),
    );
  } else {
    // A AND B AND C -> (A AND B) OR C. The explicit parentheses make
    // the unchanged connections visible instead of changing every sibling.
    target.children = [
      group(target.children.slice(0, boundaryIndex)),
      group(target.children.slice(boundaryIndex)),
    ];
    target.operator = "ANY";
  }
  return changed;
}

function blockRuleTreeEditError(root) {
  let count = 0;
  let deepest = 0;
  let error = "";
  walkBlockRule(root, (node, parent, index, depth) => {
    count += 1;
    deepest = Math.max(deepest, depth);
    if (
      node.kind === "logic"
      && node.operator === "ALL"
      && (node.children || []).filter(blockRuleContainsAction).length > 1
    ) error = "AND로 연결할 두 경로에 각각 주문이 있습니다. 주문을 공통 행동으로 옮긴 뒤 연결하세요.";
    if (node.kind === "logic" && (node.children || []).length > 20) {
      error = "한 조건 묶음에는 자식을 20개까지 넣을 수 있습니다.";
    }
  });
  if (count > 100) return "한 흐름의 조건 계층은 노드 100개까지 만들 수 있습니다.";
  if (deepest > 8) return "조건 괄호는 8단계까지 중첩할 수 있습니다.";
  return error;
}

function retainBlockRuleConditionReferences(original, changed) {
  // Formula input can contain an unfinished local draft keyed to the condition
  // object. Structural edits preserve those objects after validation succeeds.
  const conditions = new Map();
  walkBlockRule(original, node => {
    if (node.kind === "condition") conditions.set(node.id, node.condition);
  });
  walkBlockRule(changed, node => {
    if (node.kind === "condition" && conditions.has(node.id)) {
      node.condition = conditions.get(node.id);
    }
  });
}

function setBlockRuleConnection(kind, flowId, nodeId, boundaryIndex, operator) {
  if (selectedState()?.running) return;
  const owner = flowId ? blockFlow(kind, flowId) : state.blockPackage?.settings;
  const original = blockRuleTree(kind, flowId);
  const changed = rewriteBlockRuleConnection(
    original, nodeId, boundaryIndex, operator,
    () => nextBlockRuleId(`${kind}-logic`),
  );
  if (!changed || !owner) return;
  const error = blockRuleTreeEditError(changed);
  if (error) {
    notify(error, true);
    return;
  }
  retainBlockRuleConditionReferences(original, changed);
  owner[`${kind}_rule_tree`] = changed;
  commitBlockRuleMutation();
}

function wrapBlockRuleNode(kind, flowId, nodeId) {
  if (selectedState()?.running) return;
  const owner = flowId ? blockFlow(kind, flowId) : state.blockPackage?.settings;
  const original = blockRuleTree(kind, flowId);
  const root = structuredClone(original);
  let wrapper = null;
  walkBlockRule(root, (node, parent, index) => {
    if (!wrapper && node.id === nodeId && parent) {
      wrapper = newBlockRuleLogic(kind, "ALL", [node], false);
      parent.children[index] = wrapper;
    }
  });
  if (!wrapper || !owner) return;
  const error = blockRuleTreeEditError(root);
  if (error) {
    notify(error, true);
    return;
  }
  retainBlockRuleConditionReferences(original, root);
  owner[`${kind}_rule_tree`] = root;
  state.blockSelectedNode = ruleComposerNodeId(kind, flowId, wrapper.id);
  commitBlockRuleMutation();
}

function addBlockRuleChild(kind, flowId, parentId, childKind, operator = "ALL") {
  const parentFound = findBlockRuleNode(kind, flowId, parentId);
  const parent = parentFound?.node;
  const root = blockRuleTree(kind, flowId);
  if (parent?.kind !== "logic") return;
  if ((parent.children || []).length >= 20) {
    notify("한 조건 묶음에는 자식을 20개까지 넣을 수 있습니다.", true);
    return;
  }
  const addedNodes = childKind === "logic" ? 2 : 1;
  const addedDepth = childKind === "logic" ? 2 : 1;
  if (blockRuleNodeCount(root) + addedNodes > 100) {
    notify("한 흐름의 조건 계층은 노드 100개까지 만들 수 있습니다.", true);
    return;
  }
  if ((parentFound?.depth || 0) + addedDepth > 8) {
    notify("조건 괄호는 8단계까지 중첩할 수 있습니다.", true);
    return;
  }
  const child = childKind === "logic"
    ? newBlockRuleLogic(kind, operator, [newBlockRuleCondition(kind)], false)
    : newBlockRuleCondition(kind);
  parent.children.push(child);
  state.blockSelectedNode = `${blockFlowNodePrefix(kind, flowId)}-rule-${child.id}`;
  commitBlockRuleMutation();
}

function removeBlockRuleNode(kind, flowId, nodeId) {
  const found = findBlockRuleNode(kind, flowId, nodeId);
  if (!found?.parent || selectedState()?.running) return;
  if (kind === "sell") {
    const rootConditionCount = blockRuleConditionNodes(blockRuleTree(kind, flowId)).length;
    const removedConditionCount = found.node.kind === "condition"
      ? 1 : blockRuleConditionNodes(found.node).length;
    if (rootConditionCount - removedConditionCount < 1) {
      notify("매도 흐름에는 조건식이 하나 이상 필요합니다. 새 조건을 만든 뒤 이 조건을 삭제하세요.", true);
      return;
    }
  }
  let selectedParent = found.parent;
  if (found.parent.children.length === 1) {
    const parentFound = findBlockRuleNode(kind, flowId, found.parent.id);
    if (parentFound?.parent) {
      // A nested logic group may not remain empty: deleting its final child
      // removes that now-meaningless pair of parentheses as one unit.
      parentFound.parent.children.splice(parentFound.index, 1);
      selectedParent = parentFound.parent;
    } else {
      found.parent.children.splice(found.index, 1);
    }
  } else {
    found.parent.children.splice(found.index, 1);
  }
  // Explicit parentheses can nest several one-child groups. Prune all empty
  // ancestors after deletion so a saved tree never contains an empty group.
  while (selectedParent.kind === "logic" && !selectedParent.children.length) {
    const emptyParent = findBlockRuleNode(kind, flowId, selectedParent.id);
    if (!emptyParent?.parent) break;
    emptyParent.parent.children.splice(emptyParent.index, 1);
    selectedParent = emptyParent.parent;
  }
  let clearedUnsafeSellAction = false;
  if (kind === "sell") {
    walkBlockRule(blockRuleTree(kind, flowId), node => {
      if (node.kind === "logic" && node.action && blockRuleConditionNodes(node).length === 0) {
        node.action = null;
        clearedUnsafeSellAction = true;
      }
    });
  }
  state.blockSelectedNode = `${blockFlowNodePrefix(kind, flowId)}-rule-${selectedParent.id}`;
  commitBlockRuleMutation();
  if (clearedUnsafeSellAction) notify("조건이 없는 매도 묶음의 주문 행동도 함께 제거했습니다.");
}

function blockRuleContainsAction(node) {
  let found = false;
  walkBlockRule(node, candidate => { if (candidate.action) found = true; });
  return found;
}

function blockRuleNodePath(root, nodeId, path = []) {
  if (!root) return null;
  const next = [...path, root];
  if (root.id === nodeId) return next;
  if (root.kind !== "logic") return null;
  for (const child of root.children || []) {
    const found = blockRuleNodePath(child, nodeId, next);
    if (found) return found;
  }
  return null;
}

function blockRuleActionConflict(root, nodeId) {
  const path = blockRuleNodePath(root, nodeId);
  if (!path) return false;
  for (let index = 0; index < path.length - 1; index += 1) {
    const ancestor = path[index];
    if (ancestor.kind !== "logic" || ancestor.operator !== "ALL") continue;
    const activeBranch = path[index + 1];
    if ((ancestor.children || []).some(child => child !== activeBranch && blockRuleContainsAction(child))) return true;
  }
  return false;
}

function addBlockRuleAction(kind, flowId, nodeId) {
  const node = findBlockRuleNode(kind, flowId, nodeId)?.node;
  if (!node || node.action || selectedState()?.running) return;
  if (kind === "sell" && node.kind === "logic" && blockRuleConditionNodes(node).length === 0) {
    notify("매도 행동을 추가하려면 이 묶음에 조건식을 먼저 추가하세요.", true);
    return;
  }
  if (blockRuleActionConflict(blockRuleTree(kind, flowId), nodeId)) {
    notify("AND 묶음에서는 한 자식 경로에만 행동을 둘 수 있습니다. 묶음 자체의 공통 행동을 사용해 주세요.", true);
    return;
  }
  node.action = newBlockOrderAction(kind);
  state.blockSelectedNode = `${blockFlowNodePrefix(kind, flowId)}-action-${node.action.id}`;
  commitBlockRuleMutation();
}

function removeBlockRuleAction(kind, flowId, nodeId) {
  const node = findBlockRuleNode(kind, flowId, nodeId)?.node;
  if (!node?.action || selectedState()?.running) return;
  node.action = null;
  state.blockSelectedNode = `${blockFlowNodePrefix(kind, flowId)}-rule-${node.id}`;
  commitBlockRuleMutation();
}

function updateBlockRuleAction(kind, flowId, nodeId, orderType, render = true) {
  const node = findBlockRuleNode(kind, flowId, nodeId)?.node;
  if (!node?.action) return;
  node.action.order_type = orderType === "LIMIT" ? "LIMIT" : "MARKET";
  commitBlockRuleMutation(render);
}

function updateBlockCondition(kind, groupIndex, conditionIndex, key, value, flowId = null, render = true) {
  const rows = blockConditionGroups(kind, flowId)[groupIndex]?.conditions;
  if (!Array.isArray(rows) || !rows[conditionIndex]) return;
  const condition = rows[conditionIndex];
  const atrSide = key.match(/^(left|right)_atr_period$/)?.[1];
  const normalized = atrSide
    ? clampBlockAtrPeriod(value, condition[`${atrSide}_bar_interval`] || "3m")
    : [
      "left_offset", "right_offset", "left_bollinger_period", "right_bollinger_period",
    ].includes(key)
      ? Math.max(0, Number.parseInt(value, 10) || 0)
    : ["value", "left_bollinger_stddev", "right_bollinger_stddev"].includes(key)
      ? String(value || "0") : value;
  condition[key] = normalized;
  const intervalSide = key.match(/^(left|right)_bar_interval$/)?.[1];
  if (intervalSide) {
    condition[`${intervalSide}_atr_period`] = clampBlockAtrPeriod(
      condition[`${intervalSide}_atr_period`], normalized,
    );
  }
  if (["left", "right"].includes(key)) {
    const other = key === "left" ? "right" : "left";
    if (normalized === "obv") {
      condition[other] = "obv";
      condition[`${other}_bar_interval`] = condition[`${key}_bar_interval`] || "3m";
      condition[`${other}_bar_data_mode`] = condition[`${key}_bar_data_mode`] || "COMPLETE_ONLY";
      if (Number(condition.left_offset || 0) === Number(condition.right_offset || 0)) {
        condition.left_offset = 0;
        condition.right_offset = 10;
      }
    } else if (condition[other] === "obv") {
      condition[other] = key === "left" ? "value" : "close";
    }
    if (normalized === "atr") {
      condition[`${key}_atr_period`] = clampBlockAtrPeriod(
        condition[`${key}_atr_period`], condition[`${key}_bar_interval`] || "3m",
      );
    }
  }
  if (condition.left === "obv" && condition.right === "obv") {
    for (const suffix of ["bar_interval", "bar_data_mode"]) {
      if (key === `left_${suffix}`) condition[`right_${suffix}`] = normalized;
      if (key === `right_${suffix}`) condition[`left_${suffix}`] = normalized;
    }
  }
  setDirty(true);
  if (render) renderStrategyBlocks();
}

function mutateBlockExpression(kind, groupIndex, conditionIndex, side, path, mutator, flowId = null, render = true) {
  const rows = blockConditionGroups(kind, flowId)[groupIndex]?.conditions;
  const condition = rows?.[conditionIndex];
  if (!condition) return;
  const key = `${side}_expression`;
  const root = conditionSideExpression(condition, side);
  if (!root) return;
  const node = expressionNodeAt(root, path);
  if (!node) return;
  const replacement = mutator(node, root);
  if (replacement && replacement !== node) condition[key] = replaceExpressionNode(root, path, replacement);
  setDirty(true);
  if (render) renderStrategyBlocks();
}

function enableBlockExpression(kind, groupIndex, conditionIndex, side, flowId = null) {
  const rows = blockConditionGroups(kind, flowId)[groupIndex]?.conditions;
  const condition = rows?.[conditionIndex];
  if (!condition) return;
  condition[`${side}_expression`] = legacyOperandExpression(condition, side);
  setDirty(true);
  renderStrategyBlocks();
}

function disableBlockExpression(kind, groupIndex, conditionIndex, side, flowId = null) {
  const rows = blockConditionGroups(kind, flowId)[groupIndex]?.conditions;
  const condition = rows?.[conditionIndex];
  if (!condition) return;
  delete condition[`${side}_expression`];
  setDirty(true);
  renderStrategyBlocks();
}

function newBlockTradeCondition(kind) {
  return {
    left: "close", operator: kind === "buy" ? "GT" : "LT",
    right: "bollinger_upper", value: "0", left_offset: 0, right_offset: 0,
    left_bar_interval: "3m", left_bar_data_mode: "COMPLETE_ONLY",
    left_bollinger_period: 55, left_bollinger_stddev: "1.04",
    left_atr_period: 14,
    right_bar_interval: "3m", right_bar_data_mode: "COMPLETE_ONLY",
    right_bollinger_period: 55, right_bollinger_stddev: "1.04",
    right_atr_period: 14,
  };
}

function newConditionGroup(kind) {
  state.conditionGroupSerial += 1;
  const serial = `${Date.now().toString(36)}-${state.conditionGroupSerial}`;
  return {
    id: `${kind}-${serial}`,
    conditions: [newBlockTradeCondition(kind)],
  };
}

function addConditionGroup(kind, flowId = null) {
  const groups = blockConditionGroups(kind, flowId);
  if (groups.length >= 8) return;
  const group = newConditionGroup(kind);
  groups.push(group);
  setScheduleSectionExpanded(`condition:${flowId || kind}:${group.id}`, true);
  state.blockSelectedNode = flowId
    ? `${blockFlowNodePrefix(kind, flowId)}-group-${group.id}`
    : `${kind}Group-${group.id}`;
  setDirty(true);
  renderStrategyBlocks();
}

function removeConditionGroup(kind, groupIndex, flowId = null) {
  const groups = blockConditionGroups(kind, flowId);
  if (!groups[groupIndex] || (kind === "sell" && groups.length <= 1)) return;
  const [removed] = groups.splice(groupIndex, 1);
  const removedNode = removed?.id && (
    flowId ? `${blockFlowNodePrefix(kind, flowId)}-group-${removed.id}` : `${kind}Group-${removed.id}`
  );
  if (removedNode && state.blockSelectedNode === removedNode) {
    state.blockSelectedNode = flowId
      ? `${blockFlowNodePrefix(kind, flowId)}-${kind === "buy" ? "target" : "schedule"}`
      : "sellSchedule";
  }
  setDirty(true);
  renderStrategyBlocks();
}

const BLOCK_BAR_INTERVALS = ["1m", "3m", "5m", "10m", "15m", "30m", "60m", "120m", "1d"];

function expressionField(labelText, control) {
  const label = document.createElement("label");
  label.textContent = labelText;
  label.append(control);
  return label;
}

function expressionInput({ type = "number", value, min = null, max = null, step = null, disabled, onChange }) {
  const input = document.createElement("input");
  input.type = type;
  input.value = value;
  if (min != null) input.min = String(min);
  if (max != null) input.max = String(max);
  if (step != null) input.step = String(step);
  input.disabled = disabled;
  bindBlockConditionControl(input, onChange);
  return input;
}

function expressionMetricOptions(kind, currentMetric = null) {
  const options = kind === "buy"
    ? BLOCK_EXPRESSION_METRICS.filter(([key]) => !["average_price", "profit_rate"].includes(key))
    : BLOCK_EXPRESSION_METRICS;
  return currentMetric === "obv" ? [...options, ["obv", "OBV"]] : options;
}

function expressionPathsEqual(left, right) {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

function appendConditionExpressionTokens(container, expression, path, selectedPath, onSelect, controlScope) {
  if (expression.kind === "binary") {
    const nested = path.length > 0;
    if (nested) {
      const open = document.createElement("span");
      open.className = "block-expression-parenthesis";
      open.textContent = "(";
      container.append(open);
    }
    appendConditionExpressionTokens(container, expression.left, [...path, "left"], selectedPath, onSelect, controlScope);
    const operator = document.createElement("button");
    operator.type = "button";
    operator.className = "block-expression-token operator";
    operator.textContent = conditionExpressionTokenLabel(expression);
    operator.title = `계산 선택 · ${conditionExpressionSummary(expression)}`;
    operator.classList.toggle("selected", expressionPathsEqual(path, selectedPath));
    operator.setAttribute("aria-pressed", expressionPathsEqual(path, selectedPath) ? "true" : "false");
    markExpressionControl(operator, expressionControlKey(controlScope, path, "token"));
    operator.addEventListener("click", () => onSelect(path));
    container.append(operator);
    appendConditionExpressionTokens(container, expression.right, [...path, "right"], selectedPath, onSelect, controlScope);
    if (nested) {
      const close = document.createElement("span");
      close.className = "block-expression-parenthesis";
      close.textContent = ")";
      container.append(close);
    }
    return;
  }
  const token = document.createElement("button");
  token.type = "button";
  token.className = `block-expression-token ${expression.kind === "constant" ? "constant" : "metric"}`;
  token.textContent = conditionExpressionTokenLabel(expression);
  token.title = `값 선택 · ${conditionExpressionSummary(expression)}`;
  token.classList.toggle("selected", expressionPathsEqual(path, selectedPath));
  token.setAttribute("aria-pressed", expressionPathsEqual(path, selectedPath) ? "true" : "false");
  markExpressionControl(token, expressionControlKey(controlScope, path, "token"));
  token.addEventListener("click", () => onSelect(path));
  container.append(token);
}

function createExpressionCommonBarContext(
  kind, groupIndex, conditionIndex, side, rootExpression, disabled, flowId,
) {
  const context = conditionExpressionCommonBarContext(rootExpression);
  if (!context) return null;
  const controlScope = expressionControlScope(kind, groupIndex, conditionIndex, side, flowId);
  const section = document.createElement("section");
  section.className = "block-expression-common-context";
  const heading = document.createElement("div");
  heading.className = "block-expression-common-heading";
  const title = document.createElement("strong");
  title.textContent = "공통 봉 기준";
  const meta = document.createElement("span");
  meta.textContent = `${context.leaves.length}개 지표에 함께 적용`;
  heading.append(title, meta);
  const fields = document.createElement("div");
  fields.className = "block-expression-common-fields";
  const mutateAll = (mutator, render = true) => mutateBlockExpression(
    kind, groupIndex, conditionIndex, side, [], root => {
      walkConditionExpression(root, node => {
        if (node.kind === "metric" && !["average_price", "profit_rate"].includes(node.metric)) mutator(node);
      });
      return root;
    }, flowId, render,
  );
  const interval = document.createElement("select");
  BLOCK_BAR_INTERVALS.forEach(value => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value === "1d" ? "일봉" : value;
    interval.append(option);
  });
  interval.value = context.interval;
  interval.disabled = disabled;
  markExpressionControl(interval, expressionControlKey(controlScope, ["common"], "interval"));
  bindBlockConditionControl(interval, (value, render) => mutateAll(node => {
    node.bar_interval = value;
    if (node.metric === "atr") node.atr_period = clampBlockAtrPeriod(node.atr_period, value);
  }, render));
  const mode = document.createElement("select");
  [["COMPLETE_ONLY", "완성봉만"], ["INCLUDE_CURRENT", "진행 중 봉 포함"]].forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    mode.append(option);
  });
  mode.value = context.mode;
  mode.disabled = disabled;
  markExpressionControl(mode, expressionControlKey(controlScope, ["common"], "mode"));
  bindBlockConditionControl(mode, (value, render) => mutateAll(node => { node.bar_data_mode = value; }, render));
  const position = expressionInput({
    value: displayedExpressionOffset({ offset: context.offset, bar_data_mode: context.mode }),
    min: context.mode === "COMPLETE_ONLY" ? 1 : 0,
    max: context.mode === "COMPLETE_ONLY" ? 201 : 200,
    disabled,
    onChange: (value, render) => mutateAll(node => {
      node.offset = storedConditionOffset(value, node.bar_data_mode || "COMPLETE_ONLY");
    }, render),
  });
  markExpressionControl(position, expressionControlKey(controlScope, ["common"], "offset"));
  fields.append(expressionField("봉 주기", interval), expressionField("봉 상태", mode), expressionField("봉 위치", position));
  section.append(heading, fields);
  return section;
}

function createExpressionNodeEditor(
  kind, groupIndex, conditionIndex, side, expression, path, disabled, flowId, rootExpression, sharedBarContext,
) {
  const box = document.createElement("div");
  box.className = "block-expression-selected-editor";
  const controlScope = expressionControlScope(kind, groupIndex, conditionIndex, side, flowId);
  const controlKey = field => expressionControlKey(controlScope, path, field);
  const title = document.createElement("strong");
  title.className = "block-expression-selected-title";
  title.textContent = expression.kind === "binary" ? "선택한 연산" : "선택한 값";
  box.append(title);

  const mutate = (mutator, render = true) => mutateBlockExpression(
    kind, groupIndex, conditionIndex, side, path, mutator, flowId, render,
  );

  if (expression.kind === "binary") {
    const operator = conditionSelect(BLOCK_EXPRESSION_OPERATORS, expression.operator || "ADD", disabled, (value, render) => {
      mutate(node => { node.operator = value; }, render);
    });
    markExpressionControl(operator, controlKey("operator"));
    const operatorField = expressionField("연산", operator);
    operatorField.classList.add("block-expression-primary-field");
    box.append(operatorField);
    return box;
  }

  const selectedKind = expression.kind === "constant" ? "constant" : expression.metric;
  const metric = conditionSelect(
    expressionMetricOptions(kind, expression.metric), selectedKind, disabled || expression.metric === "obv", (value, render) => {
    mutate(node => value === "constant"
      ? { kind: "constant", value: "0" }
      : expressionMetricLeaf(value, node.kind === "metric" ? node : {}), render);
    },
  );
  markExpressionControl(metric, controlKey("metric"));
  const metricField = expressionField("값 종류", metric);
  metricField.classList.add("block-expression-primary-field");
  box.append(metricField);

  if (expression.kind === "constant") {
    const value = expressionInput({ value: expression.value ?? "0", step: "any", disabled,
      onChange: (next, render) => mutate(node => { node.value = String(next || "0"); }, render) });
    markExpressionControl(value, controlKey("value"));
    const valueField = expressionField("숫자", value);
    valueField.classList.add("block-expression-primary-field");
    box.append(valueField);
  } else if (["average_price", "profit_rate"].includes(expression.metric)) {
    const hint = document.createElement("span");
    hint.className = "block-condition-operand-hint";
    hint.textContent = expression.metric === "average_price"
      ? "계좌의 평균 매수가를 사용합니다."
      : "실시간 현재가와 평균 매수가로 계산하며 수수료·세금은 포함하지 않습니다.";
    box.append(hint);
  } else {
    const interval = document.createElement("select");
    BLOCK_BAR_INTERVALS.forEach(value => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value === "1d" ? "일봉" : value;
      interval.append(option);
    });
    interval.value = expression.bar_interval || "3m";
    interval.disabled = disabled;
    markExpressionControl(interval, controlKey("interval"));
    bindBlockConditionControl(interval, (value, render) => mutate(node => {
      node.bar_interval = value;
      if (node.metric === "atr") node.atr_period = clampBlockAtrPeriod(node.atr_period, value);
    }, render));

    const mode = document.createElement("select");
    [["COMPLETE_ONLY", "완성봉만"], ["INCLUDE_CURRENT", "진행 중 봉 포함"]].forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      mode.append(option);
    });
    mode.value = expression.bar_data_mode || "COMPLETE_ONLY";
    mode.disabled = disabled;
    markExpressionControl(mode, controlKey("mode"));
    bindBlockConditionControl(mode, (value, render) => mutate(node => { node.bar_data_mode = value; }, render));

    const barDataMode = expression.bar_data_mode || "COMPLETE_ONLY";
    const offset = expressionInput({ value: displayedExpressionOffset(expression),
      min: barDataMode === "COMPLETE_ONLY" ? 1 : 0,
      max: barDataMode === "COMPLETE_ONLY" ? 201 : 200,
      disabled,
      onChange: (value, render) => mutate(node => {
        node.offset = storedConditionOffset(value, node.bar_data_mode || "COMPLETE_ONLY");
      }, render) });
    markExpressionControl(offset, controlKey("offset"));
    const barFields = document.createElement("div");
    barFields.className = "block-expression-bar-fields";
    barFields.append(expressionField("봉 주기", interval), expressionField("봉 상태", mode), expressionField("봉 위치", offset));
    if (sharedBarContext) {
      const individual = document.createElement("details");
      individual.className = "block-expression-individual-context";
      const summary = document.createElement("summary");
      summary.textContent = "이 값만 봉 기준 다르게 설정";
      individual.append(summary, barFields);
      box.append(individual);
    } else {
      box.append(barFields);
    }

    if (expression.metric === "atr") {
      const maximum = blockAtrMaxPeriod(expression.bar_interval);
      const period = expressionInput({ value: clampBlockAtrPeriod(expression.atr_period, expression.bar_interval), min: 1, max: maximum, disabled,
        onChange: (value, render) => mutate(node => {
          node.atr_period = clampBlockAtrPeriod(value, node.bar_interval);
          period.value = String(node.atr_period);
        }, render) });
      markExpressionControl(period, controlKey("atr-period"));
      box.append(expressionField("ATR 기간", period));
      const hint = document.createElement("span");
      hint.className = "block-condition-operand-hint";
      hint.textContent = "ATR은 실제 고가·저가·종가의 True Range를 Wilder 방식으로 평활합니다.";
      box.append(hint);
    }
    if (String(expression.metric).startsWith("bollinger_")) {
      const period = expressionInput({ value: expression.bollinger_period || 55, min: 2, max: 2000, disabled,
        onChange: (value, render) => mutate(node => { node.bollinger_period = Math.max(2, Number.parseInt(value, 10) || 55); }, render) });
      const deviation = expressionInput({ value: expression.bollinger_stddev ?? "1.04", min: .01, step: "any", disabled,
        onChange: (value, render) => mutate(node => { node.bollinger_stddev = String(value || "1.04"); }, render) });
      markExpressionControl(period, controlKey("bollinger-period"));
      markExpressionControl(deviation, controlKey("bollinger-stddev"));
      box.append(expressionField("볼린저 기간", period), expressionField("표준편차", deviation));
    }
    const offsetHint = document.createElement("span");
    offsetHint.className = "block-condition-operand-hint";
    offsetHint.textContent = barDataMode === "COMPLETE_ONLY"
      ? "1봉 전이 가장 최근 완성봉입니다."
      : "0봉 전이 현재 진행봉입니다.";
    box.append(offsetHint);
  }

  return box;
}

function createExpressionWorkbench(kind, groupIndex, conditionIndex, side, condition, disabled, flowId, expression) {
  const workbench = document.createElement("div");
  workbench.className = "block-expression-workbench";
  const selectedPath = selectedExpressionPath(condition, side, expression);
  const selectedNode = expressionNodeAt(expression, selectedPath) || expression;
  const controlScope = expressionControlScope(kind, groupIndex, conditionIndex, side, flowId);
  const formula = document.createElement("div");
  formula.className = "block-expression-formula";
  formula.setAttribute("aria-label", "수식 항목을 선택하여 편집");
  appendConditionExpressionTokens(formula, expression, [], selectedPath, path => {
    setSelectedExpressionPath(condition, side, path);
    renderStrategyBlocks();
  }, controlScope);
  workbench.append(formula);

  const commonContext = createExpressionCommonBarContext(
    kind, groupIndex, conditionIndex, side, expression, disabled, flowId,
  );
  if (commonContext) workbench.append(commonContext);
  workbench.append(createExpressionNodeEditor(
    kind, groupIndex, conditionIndex, side, selectedNode, selectedPath, disabled, flowId, expression,
    Boolean(commonContext),
  ));

  const actions = document.createElement("div");
  actions.className = "block-expression-actions";
  const extendDisabled = disabled || expressionContainsMetric(expression, "obv")
    || expressionDepth(expression) >= 4 || expressionNodeCount(expression) >= 31;
  const addCalculation = (label, path, selectRoot = false) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button quiet compact";
    button.textContent = label;
    button.disabled = extendDisabled;
    markExpressionControl(button, expressionControlKey(
      controlScope, path, selectRoot ? "add-root" : "add-selected",
    ));
    button.addEventListener("click", () => {
      if (selectRoot) setSelectedExpressionPath(condition, side, []);
      mutateBlockExpression(kind, groupIndex, conditionIndex, side, path, node => ({
        kind: "binary", operator: "ADD", left: structuredClone(node), right: { kind: "constant", value: "0" },
      }), flowId);
    });
    actions.append(button);
  };
  addCalculation("+ 전체 식에 연산 추가", [], true);
  if (selectedPath.length) addCalculation(
    selectedNode.kind === "binary" ? "+ 선택 계산에 연산 추가" : "+ 선택 값에 연산 추가",
    selectedPath,
  );
  if (selectedNode.kind === "binary") {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button quiet compact danger-text";
    remove.textContent = selectedPath.length ? "선택 계산 제거 · 왼쪽 유지" : "전체 계산 제거 · 왼쪽 유지";
    remove.disabled = disabled;
    markExpressionControl(remove, expressionControlKey(controlScope, selectedPath, "remove"));
    remove.addEventListener("click", () => mutateBlockExpression(
      kind, groupIndex, conditionIndex, side, selectedPath,
      node => structuredClone(node.left), flowId,
    ));
    actions.append(remove);
  }
  workbench.append(actions);
  return workbench;
}

function formulaMetricTokenLabel(expression) {
  if (!expression || expression.kind !== "metric") return conditionExpressionTokenLabel(expression);
  const label = new Map(BLOCK_CONDITION_METRICS).get(expression.metric) || expression.metric || "지표";
  if (["average_price", "profit_rate"].includes(expression.metric)) return label;
  const context = `${expressionIntervalLabel(expression.bar_interval)}, ${expressionBarPosition(expression)}`;
  if (expression.metric === "atr") return `ATR[${expression.atr_period || 14}, ${context}]`;
  if (String(expression.metric).startsWith("bollinger_")) {
    return `${label}[${expression.bollinger_period || 55}, ${expression.bollinger_stddev || "1.04"}, ${context}]`;
  }
  return `${label}[${context}]`;
}

function formulaExpressionSummary(expression, nested = false) {
  if (!expression || typeof expression !== "object") return "값 미설정";
  if (expression.kind !== "binary") return expression.kind === "metric"
    ? formulaMetricTokenLabel(expression) : String(expression.value ?? "0");
  const operator = new Map(BLOCK_EXPRESSION_OPERATORS).get(expression.operator) || expression.operator || "+";
  const text = `${formulaExpressionSummary(expression.left, true)} ${operator} ${formulaExpressionSummary(expression.right, true)}`;
  return nested ? `(${text})` : text;
}

function effectiveFormulaExpression(condition, side) {
  return conditionSideExpression(condition, side) || legacyOperandExpression(condition, side);
}

function formulaControl(labelText, control, wide = false) {
  const label = document.createElement("label");
  if (wide) label.className = "wide";
  const text = document.createElement("span");
  text.textContent = labelText;
  label.append(text, control);
  return label;
}

function createCompactFormulaTokenEditor(kind, expression, disabled, updateToken) {
  if (!expression) return null;
  const panel = document.createElement("div");
  panel.className = "condition-token-editor";
  const heading = document.createElement("div");
  heading.className = "condition-token-editor-heading";
  const title = document.createElement("strong");
  title.textContent = expression.kind === "binary" ? "선택한 계산" : "선택한 데이터";
  const summary = document.createElement("span");
  summary.textContent = expression.kind === "metric" ? formulaMetricTokenLabel(expression) : conditionExpressionTokenLabel(expression);
  heading.append(title, summary);
  panel.append(heading);
  const grid = document.createElement("div");
  grid.className = "condition-token-editor-grid";
  const mutate = (mutator, render = true) => updateToken(mutator(structuredClone(expression)), render);
  if (expression.kind === "binary") {
    const operator = conditionSelect(BLOCK_EXPRESSION_OPERATORS, expression.operator || "ADD", disabled, (value, render) => {
      mutate(node => { node.operator = value; return node; }, render);
    });
    grid.append(formulaControl("연산", operator, true));
  } else {
    const metricOptions = BLOCK_CONDITION_METRICS.filter(([key]) => kind !== "buy" || !["average_price", "profit_rate"].includes(key));
    const current = expression.kind === "constant" ? "constant" : expression.metric;
    const metric = conditionSelect(metricOptions, current, disabled, (value, render) => {
      mutate(node => value === "constant"
        ? { kind: "constant", value: "0" }
        : expressionMetricLeaf(value, node.kind === "metric" ? node : {}), render);
    });
    grid.append(formulaControl("데이터", metric, true));
    if (expression.kind === "constant") {
      const value = expressionInput({ value: expression.value ?? "0", step: "any", disabled,
        onChange: (next, render) => mutate(node => { node.value = String(next || "0"); return node; }, render) });
      grid.append(formulaControl("숫자", value, true));
    } else if (["average_price", "profit_rate"].includes(expression.metric)) {
      const hint = document.createElement("p");
      hint.className = "condition-token-editor-hint";
      hint.textContent = expression.metric === "average_price"
        ? "전략이 추적하는 평균 매수가를 사용합니다."
        : "실시간 현재가와 평균 매수가로 계산합니다.";
      grid.append(hint);
    } else {
      const interval = document.createElement("select");
      BLOCK_BAR_INTERVALS.forEach(value => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value === "1d" ? "일봉" : value;
        interval.append(option);
      });
      interval.value = expression.bar_interval || "3m";
      interval.disabled = disabled;
      bindBlockConditionControl(interval, (value, render) => mutate(node => {
        node.bar_interval = value;
        if (node.metric === "atr") node.atr_period = clampBlockAtrPeriod(node.atr_period, value);
        return node;
      }, render));
      const mode = conditionSelect(
        [["COMPLETE_ONLY", "완성봉만"], ["INCLUDE_CURRENT", "진행 중 봉 포함"]],
        expression.bar_data_mode || "COMPLETE_ONLY", disabled,
        (value, render) => mutate(node => { node.bar_data_mode = value; return node; }, render),
      );
      const barMode = expression.bar_data_mode || "COMPLETE_ONLY";
      const offset = expressionInput({
        value: displayedExpressionOffset(expression), min: barMode === "COMPLETE_ONLY" ? 1 : 0,
        max: barMode === "COMPLETE_ONLY" ? 201 : 200, disabled,
        onChange: (value, render) => mutate(node => {
          node.offset = storedConditionOffset(value, node.bar_data_mode || "COMPLETE_ONLY");
          return node;
        }, render),
      });
      grid.append(
        formulaControl("봉 주기", interval),
        formulaControl("봉 상태", mode),
        formulaControl("봉 위치", offset),
      );
      if (expression.metric === "atr") {
        const period = expressionInput({
          value: clampBlockAtrPeriod(expression.atr_period, expression.bar_interval), min: 1,
          max: blockAtrMaxPeriod(expression.bar_interval), disabled,
          onChange: (value, render) => mutate(node => {
            node.atr_period = clampBlockAtrPeriod(value, node.bar_interval);
            return node;
          }, render),
        });
        grid.append(formulaControl("ATR 기간", period));
      }
      if (String(expression.metric).startsWith("bollinger_")) {
        const period = expressionInput({ value: expression.bollinger_period || 55, min: 2, max: 2000, disabled,
          onChange: (value, render) => mutate(node => { node.bollinger_period = Math.max(2, Number.parseInt(value, 10) || 55); return node; }, render) });
        const deviation = expressionInput({ value: expression.bollinger_stddev || "1.04", min: .01, step: "any", disabled,
          onChange: (value, render) => mutate(node => { node.bollinger_stddev = String(value || "1.04"); return node; }, render) });
        grid.append(formulaControl("볼린저 기간", period), formulaControl("표준편차", deviation));
      }
    }
  }
  panel.append(grid);
  return panel;
}

let conditionFormulaDrafts = new WeakMap();

function resetConditionFormulaDrafts() {
  conditionFormulaDrafts = new WeakMap();
}

function getConditionFormulaDraft(condition) {
  let draft = conditionFormulaDrafts.get(condition);
  if (!draft) {
    const units = ConditionFormula.fromCondition({
      operator: condition.operator,
      left_expression: effectiveFormulaExpression(condition, "left"),
      right_expression: effectiveFormulaExpression(condition, "right"),
    });
    draft = { units, anchor: units.length, focus: units.length, metricIndex: null, error: "", undo: [], redo: [] };
    conditionFormulaDrafts.set(condition, draft);
  }
  return draft;
}

function flushConditionFormulaDrafts(strict = true) {
  if (!strict) return true; // An unfinished expression must remain editable across inspector renders.
  const visit = value => {
    if (!value || typeof value !== "object") return;
    const draft = conditionFormulaDrafts.get(value);
    if (draft) {
      try { ConditionFormula.parse(draft.units); }
      catch (error) { throw new Error(`미완성 조건식을 먼저 수정해 주세요. IF 조건식을 열어 확인하세요. ${error.message}`); }
    }
    Object.values(value).forEach(visit);
  };
  visit(state.blockPackage);
  return true;
}

function formulaDomLength(node) {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent.length;
  if (node.dataset?.formulaMetric != null) return 1;
  return [...node.childNodes].reduce((total, child) => total + formulaDomLength(child), 0);
}

function formulaDomOffset(strip, node, offset) {
  if (!node || !strip.contains(node)) return null;
  let position = node.nodeType === Node.TEXT_NODE ? offset
    : [...node.childNodes].slice(0, offset).reduce((total, child) => total + formulaDomLength(child), 0);
  while (node !== strip) {
    if (node.parentNode?.dataset?.formulaMetric != null) { position = 1; node = node.parentNode; }
    for (let sibling = node.previousSibling; sibling; sibling = sibling.previousSibling) position += formulaDomLength(sibling);
    node = node.parentNode;
  }
  return position;
}

function formulaDomPoint(strip, offset) {
  let remaining = Math.max(0, offset);
  for (let index = 0; index < strip.childNodes.length; index += 1) {
    const node = strip.childNodes[index];
    const length = formulaDomLength(node);
    if (node.nodeType === Node.TEXT_NODE && remaining <= length) return [node, remaining];
    if (remaining === 0) return [strip, index];
    remaining -= length;
  }
  return [strip, strip.childNodes.length];
}

function captureConditionFormulaFocus(inspector) {
  const editor = inspector?.querySelector(".condition-formula-strip");
  if (!editor || document.activeElement !== editor) return null;
  editor._captureFormulaSelection();
  return { condition: editor._formulaCondition, anchor: editor._formulaDraft.anchor, focus: editor._formulaDraft.focus };
}

function restoreConditionFormulaFocus(snapshot, inspector) {
  if (!snapshot) return;
  const editor = inspector?.querySelector(".condition-formula-strip");
  if (editor?._formulaCondition !== snapshot.condition) return;
  editor._formulaDraft.anchor = snapshot.anchor;
  editor._formulaDraft.focus = snapshot.focus;
  editor._restoreFormulaSelection();
}

function createConditionFormulaEditor(kind, ruleNode, disabled) {
  const condition = ruleNode.condition;
  const draft = getConditionFormulaDraft(condition);
  const panel = document.createElement("section");
  panel.className = "condition-formula-editor";
  const hint = document.createElement("p");
  hint.className = "condition-formula-hint";
  hint.textContent = "원하는 위치를 클릭하고 숫자·연산자를 입력하세요. 데이터는 클릭해서 설정합니다.";
  const strip = document.createElement("div");
  strip.className = "condition-formula-strip";
  strip.contentEditable = String(!disabled);
  strip.spellcheck = false;
  strip.setAttribute("role", "textbox");
  strip.setAttribute("aria-label", "조건식 입력");
  strip.setAttribute("aria-multiline", "false");
  strip.setAttribute("aria-readonly", String(disabled));
  strip.dataset.placeholder = "데이터를 추가하고 조건식을 입력하세요";
  strip._formulaCondition = condition;
  strip._formulaDraft = draft;
  const status = document.createElement("p");
  status.className = "condition-formula-status";
  status.setAttribute("aria-live", "polite");
  const timing = document.createElement("span");
  timing.className = "block-condition-timing";
  const keypad = document.createElement("div");
  keypad.className = "condition-formula-keypad";
  const palette = document.createElement("div");
  palette.className = "condition-data-palette";
  palette.hidden = true;
  const details = document.createElement("div");
  details.className = "condition-formula-data-settings";
  panel.append(hint, strip, status, timing, keypad, palette, details);

  const capture = () => {
    const selection = window.getSelection();
    const anchor = formulaDomOffset(strip, selection?.anchorNode, selection?.anchorOffset);
    const focus = formulaDomOffset(strip, selection?.focusNode, selection?.focusOffset);
    if (anchor != null && focus != null) {
      draft.anchor = Math.min(anchor, draft.units.length);
      draft.focus = Math.min(focus, draft.units.length);
    }
  };
  const restore = () => {
    if (disabled || !strip.isConnected) return;
    strip.focus({ preventScroll: true });
    const anchor = formulaDomPoint(strip, draft.anchor);
    const focus = formulaDomPoint(strip, draft.focus);
    window.getSelection()?.setBaseAndExtent(anchor[0], anchor[1], focus[0], focus[1]);
  };
  strip._captureFormulaSelection = capture;
  strip._restoreFormulaSelection = restore;
  const renderText = () => {
    strip.replaceChildren();
    let text = "";
    const flushText = () => { if (text) strip.append(document.createTextNode(text)); text = ""; };
    draft.units.forEach((unit, index) => {
      if (typeof unit === "string") { text += unit; return; }
      flushText();
      const chip = document.createElement("span");
      chip.className = `condition-formula-token metric${draft.metricIndex === index ? " selected" : ""}`;
      chip.contentEditable = "false";
      chip.dataset.formulaMetric = String(index);
      chip.setAttribute("role", "button");
      chip.setAttribute("aria-label", `${formulaMetricTokenLabel(unit)} 데이터 설정`);
      chip.textContent = formulaMetricTokenLabel(unit);
      chip.title = "클릭하여 지표와 봉 기준 변경";
      strip.append(chip);
    });
    flushText();
    if (!strip.lastChild || strip.lastChild.nodeType !== Node.TEXT_NODE) strip.append(document.createTextNode(""));
  };
  const validate = (commit = true) => {
    try {
      const parsed = ConditionFormula.parse(draft.units);
      if (commit) {
        Object.assign(condition, parsed);
        normalizeConditionAtrPeriods(condition);
        commitBlockRuleMutation(false);
      }
      draft.error = "";
    } catch (error) {
      draft.error = error.message;
      if (commit) setDirty(true);
    }
    strip.setAttribute("aria-invalid", String(Boolean(draft.error)));
    status.hidden = !draft.error;
    status.textContent = draft.error;
    timing.hidden = Boolean(draft.error);
    timing.textContent = `판단 기준 · ${blockConditionTiming(condition)}`;
    timing.classList.toggle("live", isLiveProfitCondition(condition));
  };
  const checkpoint = () => {
    draft.undo.push({ units: structuredClone(draft.units), anchor: draft.anchor, focus: draft.focus });
    if (draft.undo.length > 100) draft.undo.shift();
    draft.redo = [];
  };
  const showDetails = () => {
    details.replaceChildren();
    const unit = draft.units[draft.metricIndex];
    if (!unit || typeof unit === "string") return;
    const editor = createCompactFormulaTokenEditor(kind, unit, disabled, (next, rerender) => {
      checkpoint();
      draft.units[draft.metricIndex] = next;
      validate();
      renderText();
      if (rerender) showDetails();
      else {
        const summary = details.querySelector(".condition-token-editor-heading > span");
        if (summary) summary.textContent = formulaMetricTokenLabel(next);
      }
      return true;
    });
    if (editor) details.append(editor);
  };
  const insert = (replacement, { preserveSelection = false, selectMetric = false } = {}) => {
    if (disabled) return;
    capture();
    const start = Math.min(draft.anchor, draft.focus);
    const result = ConditionFormula.replaceRange(draft.units, draft.anchor, draft.focus, replacement);
    if (result.units.length > 512) { notify("조건식은 512자 이내로 입력해 주세요.", true); return; }
    checkpoint();
    draft.units = result.units;
    draft.anchor = preserveSelection ? start : result.caret;
    draft.focus = result.caret;
    draft.metricIndex = selectMetric ? start : null;
    palette.hidden = true;
    validate();
    renderText();
    showDetails();
    restore();
  };
  const erase = backwards => {
    capture();
    if (draft.anchor === draft.focus) {
      if (backwards) draft.anchor = Math.max(0, draft.anchor - 1);
      else draft.focus = Math.min(draft.units.length, draft.focus + 1);
    }
    // The adjusted range is model state; do not overwrite it from the old DOM selection.
    const result = ConditionFormula.replaceRange(draft.units, draft.anchor, draft.focus, []);
    checkpoint();
    draft.units = result.units;
    draft.anchor = draft.focus = result.caret;
    draft.metricIndex = null;
    validate();
    renderText();
    showDetails();
    restore();
  };
  const undo = redo => {
    const source = redo ? draft.redo : draft.undo;
    const target = redo ? draft.undo : draft.redo;
    if (!source.length) return;
    target.push({ units: structuredClone(draft.units), anchor: draft.anchor, focus: draft.focus });
    Object.assign(draft, source.pop());
    draft.metricIndex = null;
    validate();
    renderText();
    showDetails();
    restore();
  };
  strip.addEventListener("beforeinput", event => {
    if (disabled) { event.preventDefault(); return; }
    if (event.isComposing) return;
    if (["insertText", "insertReplacementText"].includes(event.inputType)) {
      event.preventDefault();
      insert(event.data || "");
    } else if (event.inputType.startsWith("delete")) {
      event.preventDefault();
      erase(event.inputType.endsWith("Backward"));
    } else if (event.inputType === "historyUndo" || event.inputType === "historyRedo") {
      event.preventDefault();
      undo(event.inputType === "historyRedo");
    } else if (["insertParagraph", "insertLineBreak"].includes(event.inputType)) event.preventDefault();
  });
  strip.addEventListener("input", event => {
    if (event.isComposing || disabled) return;
    // Browser/IME edits are converted back to text and atomic data only.
    const units = [];
    const read = node => {
      if (node.nodeType === Node.TEXT_NODE) units.push(...Array.from(node.textContent));
      else if (node.dataset?.formulaMetric != null) {
        const metric = draft.units[Number(node.dataset.formulaMetric)];
        if (metric && typeof metric === "object") units.push(structuredClone(metric));
      } else [...node.childNodes].forEach(read);
    };
    [...strip.childNodes].forEach(read);
    capture();
    checkpoint();
    draft.units = units;
    draft.metricIndex = null;
    validate();
    renderText();
    showDetails();
    restore();
  });
  strip.addEventListener("paste", event => {
    event.preventDefault();
    insert((event.clipboardData?.getData("text/plain") || "").replace(/[\r\n]+/g, " "));
  });
  strip.addEventListener("drop", event => event.preventDefault());
  strip.addEventListener("keydown", event => {
    if (disabled || event.isComposing) return;
    if ((event.ctrlKey || event.metaKey) && ["z", "y"].includes(event.key.toLowerCase())) {
      event.preventDefault();
      undo(event.key.toLowerCase() === "y" || event.shiftKey);
      return;
    }
    if (event.key === "Backspace" || event.key === "Delete") {
      event.preventDefault();
      erase(event.key === "Backspace");
    } else if (["ArrowLeft", "ArrowRight"].includes(event.key) && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      capture();
      const left = event.key === "ArrowLeft";
      const position = !event.shiftKey && draft.anchor !== draft.focus
        ? (left ? Math.min(draft.anchor, draft.focus) : Math.max(draft.anchor, draft.focus))
        : Math.max(0, Math.min(draft.units.length, draft.focus + (left ? -1 : 1)));
      draft.focus = position;
      if (!event.shiftKey) draft.anchor = position;
      draft.metricIndex = null;
      strip.querySelectorAll(".selected").forEach(chip => chip.classList.remove("selected"));
      showDetails();
      restore();
    } else if (event.key === "Enter") event.preventDefault();
  });
  strip.addEventListener("pointerdown", event => {
    const chip = event.target.closest("[data-formula-metric]");
    if (!chip || disabled) return;
    event.preventDefault();
    draft.metricIndex = Number(chip.dataset.formulaMetric);
    draft.anchor = draft.metricIndex;
    draft.focus = draft.metricIndex + 1;
    palette.hidden = true;
    strip.querySelectorAll(".selected").forEach(item => item.classList.remove("selected"));
    chip.classList.add("selected");
    showDetails();
    restore();
  });
  strip.addEventListener("click", event => {
    if (event.target.closest("[data-formula-metric]")) return;
    capture();
    draft.metricIndex = null;
    strip.querySelectorAll(".selected").forEach(chip => chip.classList.remove("selected"));
    showDetails();
  });
  strip.addEventListener("keyup", capture);
  strip.addEventListener("mouseup", capture);
  const key = (label, callback, className = "") => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `condition-formula-key ${className}`.trim();
    button.textContent = label;
    button.disabled = disabled;
    button.addEventListener("pointerdown", event => { capture(); event.preventDefault(); });
    button.addEventListener("click", callback);
    keypad.append(button);
  };
  key("+ 데이터 추가", () => { palette.hidden = !palette.hidden; }, "data");
  [...BLOCK_EXPRESSION_OPERATORS.map(([, label]) => label), "(", ")", ...BLOCK_CONDITION_OPERATORS.map(([, label]) => label)]
    .forEach(operator => key(operator, () => insert(operator), ["<", "≤", ">", "≥", "=", "상향돌파", "하향돌파"].includes(operator) ? "compare" : ""));
  BLOCK_CONDITION_METRICS.filter(([metric]) => kind !== "buy" || !["average_price", "profit_rate"].includes(metric))
    .forEach(([metric, label]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.disabled = disabled;
      button.addEventListener("pointerdown", event => { capture(); event.preventDefault(); });
      button.addEventListener("click", () => insert([expressionMetricLeaf(metric)], { selectMetric: true }));
      palette.append(button);
    });
  renderText();
  validate(false);
  showDetails();
  return panel;
}

function appendExpressionModeAction(panel, { enabled, disabled, onClick, controlKey = null }) {
  const action = document.createElement("button");
  action.type = "button";
  action.className = "button quiet compact";
  action.textContent = enabled ? "단순 값으로 되돌리기" : "+ 계산식 만들기";
  action.disabled = disabled;
  if (controlKey) markExpressionControl(action, controlKey);
  action.addEventListener("click", onClick);
  panel.append(action);
}

function appendAdvancedExpressionAction(panel, { disabled, onClick, controlKey = null }) {
  const action = document.createElement("button");
  action.type = "button";
  action.className = "button quiet compact block-expression-advanced-action";
  action.textContent = "+ 고급 계산식";
  action.disabled = disabled;
  if (controlKey) markExpressionControl(action, controlKey);
  action.addEventListener("click", onClick);
  panel.append(action);
}

function atrVolatilityRateExpression(condition, side) {
  const source = {
    offset: condition?.[`${side}_offset`],
    bar_interval: condition?.[`${side}_bar_interval`] || "3m",
    bar_data_mode: condition?.[`${side}_bar_data_mode`] || "COMPLETE_ONLY",
    bollinger_period: condition?.[`${side}_bollinger_period`],
    bollinger_stddev: condition?.[`${side}_bollinger_stddev`],
    atr_period: condition?.[`${side}_atr_period`] || 14,
  };
  return {
    kind: "binary",
    operator: "MULTIPLY",
    left: {
      kind: "binary",
      operator: "DIVIDE",
      left: expressionMetricLeaf("atr", source),
      right: expressionMetricLeaf("close", source),
    },
    right: { kind: "constant", value: "100" },
  };
}

function applyAtrVolatilityRatePreset(kind, groupIndex, conditionIndex, side, condition, flowId = null) {
  condition[`${side}_expression`] = atrVolatilityRateExpression(condition, side);
  setSelectedExpressionPath(condition, side, ["left", "left"]);
  setDirty(true);
  renderStrategyBlocks();
}

function createConditionOperandEditor(kind, groupIndex, conditionIndex, side, condition, disabled, flowId = null) {
  const panel = document.createElement("div");
  panel.className = "block-condition-operand";
  const expression = conditionSideExpression(condition, side);
  const metric = condition[side];
  const controlScope = expressionControlScope(kind, groupIndex, conditionIndex, side, flowId);
  const setupControlKey = field => expressionControlKey(controlScope, [], field);
  const title = document.createElement("strong");
  title.textContent = expression
    ? "계산 값"
    : metric === "profit_rate"
    ? "실시간 계산"
    : metric === "obv"
      ? "OBV 계산"
    : metric === "atr"
      ? "ATR 계산"
    : metric === "average_price"
      ? "계좌 값"
      : side === "right" && metric === "value"
        ? "비교 기준"
        : side === "left" ? "왼쪽 값 설정" : "오른쪽 값 설정";
  panel.append(title);
  const enableExpression = () => enableBlockExpression(kind, groupIndex, conditionIndex, side, flowId);
  if (expression || side === "left") {
    const setupActions = document.createElement("div");
    setupActions.className = "block-expression-setup-actions";
    appendExpressionModeAction(setupActions, {
      enabled: Boolean(expression), disabled: disabled || (!expression && metric === "obv"),
      controlKey: setupControlKey("expression-mode"),
      onClick: () => expression
        ? disableBlockExpression(kind, groupIndex, conditionIndex, side, flowId)
        : enableExpression(),
    });
    if (!expression && side === "left" && metric !== "obv") {
      const preset = document.createElement("button");
      preset.type = "button";
      preset.className = "button quiet compact block-expression-preset";
      preset.textContent = "+ ATR 변동성률 수식";
      preset.title = "((ATR(14) ÷ 종가) × 100) 수식을 만듭니다.";
      preset.disabled = disabled;
      markExpressionControl(preset, setupControlKey("atr-volatility-preset"));
      preset.addEventListener("click", () => applyAtrVolatilityRatePreset(
        kind, groupIndex, conditionIndex, side, condition, flowId,
      ));
      setupActions.append(preset);
    }
    panel.append(setupActions);
  }
  const appendRightAdvancedAction = () => {
    if (side !== "right" || metric === "obv") return;
    const actions = document.createElement("div");
    actions.className = "block-expression-setup-actions advanced";
    appendAdvancedExpressionAction(actions, {
      disabled,
      onClick: enableExpression,
      controlKey: setupControlKey("advanced-expression"),
    });
    panel.append(actions);
  };
  if (expression) {
    panel.append(createExpressionWorkbench(
      kind, groupIndex, conditionIndex, side, condition, disabled, flowId, expression,
    ));
    return panel;
  }
  if (side === "right" && metric === "value") {
    const row = document.createElement("label");
    row.textContent = "비교값";
    const input = document.createElement("input");
    input.type = "number";
    input.step = "any";
    input.value = condition.value ?? "0";
    input.disabled = disabled;
    markExpressionControl(input, setupControlKey("direct-value"));
    bindBlockConditionControl(input, (value, render) => updateBlockCondition(kind, groupIndex, conditionIndex, "value", value, flowId, render));
    row.append(input);
    panel.append(row);
    appendRightAdvancedAction();
    return panel;
  }
  if (metric === "average_price") {
    const hint = document.createElement("span");
    hint.className = "block-condition-operand-hint";
    hint.textContent = "계좌의 평균 매수가를 사용합니다.";
    panel.append(hint);
    appendRightAdvancedAction();
    return panel;
  }
  if (metric === "profit_rate") {
    const hint = document.createElement("span");
    hint.className = "block-condition-operand-hint";
    hint.textContent = "실시간 현재가와 계좌 평균 매수가로 자동 계산합니다. 수수료·세금은 포함하지 않습니다.";
    panel.append(hint);
    appendRightAdvancedAction();
    return panel;
  }
  const intervalRow = document.createElement("label");
  intervalRow.textContent = "봉 주기";
  const interval = document.createElement("select");
  BLOCK_BAR_INTERVALS.forEach(value => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value === "1d" ? "일봉" : value;
    interval.append(option);
  });
  interval.value = condition[`${side}_bar_interval`] || "3m";
  interval.disabled = disabled;
  bindBlockConditionControl(interval, (value, render) => updateBlockCondition(kind, groupIndex, conditionIndex, `${side}_bar_interval`, value, flowId, render));
  intervalRow.append(interval);
  const modeRow = document.createElement("label");
  modeRow.textContent = "봉 상태";
  const mode = document.createElement("select");
  [["COMPLETE_ONLY", "완성봉만"], ["INCLUDE_CURRENT", "진행 중 봉 포함"]].forEach(([value, text]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    mode.append(option);
  });
  mode.value = condition[`${side}_bar_data_mode`] || "COMPLETE_ONLY";
  mode.disabled = disabled;
  bindBlockConditionControl(mode, (value, render) => updateBlockCondition(kind, groupIndex, conditionIndex, `${side}_bar_data_mode`, value, flowId, render));
  modeRow.append(mode);
  const offsetRow = document.createElement("label");
  offsetRow.textContent = "봉 위치";
  const offset = document.createElement("input");
  offset.type = "number";
  const barDataMode = condition[`${side}_bar_data_mode`] || "COMPLETE_ONLY";
  offset.min = barDataMode === "COMPLETE_ONLY" ? "1" : "0";
  offset.max = barDataMode === "COMPLETE_ONLY" ? "201" : "200";
  offset.value = displayedConditionOffset(condition, side);
  offset.disabled = disabled;
  bindBlockConditionControl(offset, (value, render) => updateBlockCondition(
    kind, groupIndex, conditionIndex, `${side}_offset`, storedConditionOffset(value, barDataMode), flowId, render,
  ));
  offsetRow.append(offset);
  panel.append(intervalRow, modeRow, offsetRow);
  const offsetHint = document.createElement("span");
  offsetHint.className = "block-condition-operand-hint";
  offsetHint.textContent = barDataMode === "COMPLETE_ONLY"
    ? "1봉 전이 가장 최근 완성봉입니다. 예: 1봉 전과 11봉 전을 비교할 수 있습니다."
    : "0봉 전은 현재 진행봉이며, 1봉 전부터 이전 봉입니다.";
  panel.append(offsetHint);
  if (metric === "obv") {
    const obvHint = document.createElement("span");
    obvHint.className = "block-condition-operand-hint";
    obvHint.textContent = "OBV는 실제 종가 방향에 따라 거래량을 누적합니다. 양쪽 봉 주기와 봉 상태는 자동으로 같게 맞추며, 처음 선택하면 1봉 전과 11봉 전을 비교합니다.";
    panel.append(obvHint);
  }
  if (metric === "atr") {
    const periodRow = document.createElement("label");
    periodRow.textContent = "ATR 기간";
    const period = document.createElement("input");
    period.type = "number";
    period.min = "1";
    period.max = String(blockAtrMaxPeriod(condition[`${side}_bar_interval`] || "3m"));
    period.value = clampBlockAtrPeriod(
      condition[`${side}_atr_period`], condition[`${side}_bar_interval`] || "3m",
    );
    period.disabled = disabled;
    bindBlockConditionControl(period, (value, render) => {
      const normalized = clampBlockAtrPeriod(value, condition[`${side}_bar_interval`] || "3m");
      period.value = String(normalized);
      updateBlockCondition(kind, groupIndex, conditionIndex, `${side}_atr_period`, normalized, flowId, render);
    });
    periodRow.append(period);
    const atrHint = document.createElement("span");
    atrHint.className = "block-condition-operand-hint";
    atrHint.textContent = "ATR은 실제 고가·저가·종가의 True Range를 Wilder 방식으로 평활합니다.";
    panel.append(periodRow, atrHint);
  }
  if (metric.startsWith("bollinger_")) {
    const periodRow = document.createElement("label");
    periodRow.textContent = "볼린저 기간";
    const period = document.createElement("input");
    period.type = "number";
    period.min = "2";
    period.max = "2000";
    period.value = condition[`${side}_bollinger_period`] ?? 55;
    period.disabled = disabled;
    bindBlockConditionControl(period, (value, render) => updateBlockCondition(kind, groupIndex, conditionIndex, `${side}_bollinger_period`, value, flowId, render));
    periodRow.append(period);
    const deviationRow = document.createElement("label");
    deviationRow.textContent = "표준편차";
    const deviation = document.createElement("input");
    deviation.type = "number";
    deviation.min = "0.01";
    deviation.step = "any";
    deviation.value = condition[`${side}_bollinger_stddev`] ?? "1.04";
    deviation.disabled = disabled;
    bindBlockConditionControl(deviation, (value, render) => updateBlockCondition(kind, groupIndex, conditionIndex, `${side}_bollinger_stddev`, value, flowId, render));
    deviationRow.append(deviation);
    panel.append(periodRow, deviationRow);
  }
  appendRightAdvancedAction();
  return panel;
}

function createConditionEditor(kind, groupIndex, disabled, flowId = null) {
  const wrap = document.createElement("div");
  wrap.className = "block-condition-editor";
  const groups = blockConditionGroups(kind, flowId);
  const rows = groups[groupIndex]?.conditions || [];
  const metrics = kind === "buy"
    ? BLOCK_CONDITION_METRICS.filter(([key]) => !["average_price", "profit_rate"].includes(key))
    : BLOCK_CONDITION_METRICS;
  const rightMetrics = [...metrics, ["value", "직접 값"]];
  const evaluationHint = document.createElement("p");
  evaluationHint.className = "block-condition-evaluation-hint";
  evaluationHint.textContent = "토큰을 눌러 값을 편집하세요. 같은 블록 안은 OR, 다음 블록과는 AND입니다.";
  wrap.append(evaluationHint);
  rows.forEach((condition, index) => {
    const row = document.createElement("div");
    row.className = "block-condition-row";
    const top = document.createElement("div");
    top.className = "block-condition-expression";
    const operandControl = (side, options) => {
      const expression = conditionSideExpression(condition, side);
      if (!expression) return conditionSelect(options, condition[side], disabled, (value, render) => updateBlockCondition(
        kind, groupIndex, index, side, value, flowId, render,
      ));
      const summary = document.createElement("div");
      summary.className = "block-condition-control";
      summary.style.cssText = "height:auto;min-height:28px;display:flex;align-items:center;overflow-wrap:anywhere;line-height:1.35;background:#f4f7fb;color:#344054";
      summary.textContent = conditionExpressionCompactSummary(expression);
      summary.title = conditionExpressionSummary(expression);
      return summary;
    };
    top.append(
      operandControl("left", metrics),
      conditionSelect(conditionOperatorOptions(condition), condition.operator, disabled, (value, render) => updateBlockCondition(kind, groupIndex, index, "operator", value, flowId, render)),
      operandControl("right", rightMetrics),
    );
    const detail = document.createElement("div");
    detail.className = "block-condition-operands";
    if (conditionSideExpression(condition, "left") || conditionSideExpression(condition, "right")) {
      detail.style.gridTemplateColumns = "minmax(0, 1fr)";
    }
    detail.append(
      createConditionOperandEditor(kind, groupIndex, index, "left", condition, disabled, flowId),
      createConditionOperandEditor(kind, groupIndex, index, "right", condition, disabled, flowId),
    );
    const timing = document.createElement("span");
    timing.className = `block-condition-timing${isLiveProfitCondition(condition) ? " live" : ""}`;
    timing.textContent = `판단 기준 · ${blockConditionTiming(condition)}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "block-condition-remove";
    remove.textContent = "삭제";
    remove.disabled = disabled || rows.length <= 1;
    remove.addEventListener("click", () => {
      rows.splice(index, 1);
      setDirty(true);
      renderStrategyBlocks();
    });
    const footer = document.createElement("div");
    footer.className = "block-condition-row-footer";
    footer.append(remove);
    row.append(top, timing, detail, footer);
    wrap.append(row);
    if (index < rows.length - 1) wrap.append(conditionJoin("또는 (OR)"));
  });
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "block-condition-empty";
    empty.textContent = "이 조건 블록에는 비교식이 없습니다.";
    wrap.append(empty);
  }
  const add = document.createElement("button");
  add.type = "button";
  add.className = "button quiet compact block-condition-add";
  add.textContent = "+ OR 조건";
  add.disabled = disabled || rows.length >= 20;
  add.addEventListener("click", () => {
    rows.push(newBlockTradeCondition(kind));
    setDirty(true);
    renderStrategyBlocks();
  });
  const groupActions = document.createElement("div");
  groupActions.className = "block-condition-group-actions";
  const addGroup = document.createElement("button");
  addGroup.type = "button";
  addGroup.className = "button quiet compact";
  addGroup.textContent = "+ AND 조건 블록";
  addGroup.disabled = disabled || groups.length >= 8;
  addGroup.addEventListener("click", () => addConditionGroup(kind, flowId));
  const removeGroup = document.createElement("button");
  removeGroup.type = "button";
  removeGroup.className = "button quiet compact danger-text";
  removeGroup.textContent = "이 조건 블록 삭제";
  removeGroup.disabled = disabled || (kind === "sell" && groups.length <= 1);
  removeGroup.addEventListener("click", () => removeConditionGroup(kind, groupIndex, flowId));
  groupActions.append(add, addGroup, removeGroup);
  wrap.append(groupActions);
  return wrap;
}

function createEmptyBuyConditionEditor(disabled, flowId = null) {
  const wrap = document.createElement("div");
  wrap.className = "block-condition-editor";
  const empty = document.createElement("p");
  empty.className = "block-condition-empty";
  empty.textContent = "추가 매수 조건이 없어 종목 목록이 매수 주문 조건으로 바로 전달됩니다.";
  const add = document.createElement("button");
  add.type = "button";
  add.className = "button quiet compact block-condition-add";
  add.textContent = "+ 매수 조건 블록";
  add.disabled = disabled;
  add.addEventListener("click", () => addConditionGroup("buy", flowId));
  wrap.append(empty, add);
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

async function refreshBlockConditions(openToken = null) {
  if (state.blockPackage?.template !== "condition-bollinger-v1") return;
  const owner = state.blockPackage;
  const source = owner.settings.condition_source;
  const token = openToken ?? state.strategyOpenToken;
  const scopes = new Map();
  blockBuyFlows().filter(flow => flow.target_source === "HTS_CONDITION").forEach(flow => {
    const scope = blockConditionScope(flow);
    if (scope) scopes.set(scope.key, scope);
  });
  await Promise.all([...scopes.values()].map(async scope => {
    let entry = state.blockConditionCache.get(scope.key);
    if (!entry || entry.error) {
      entry = { conditions: [], loading: true };
      state.blockConditionCache.set(scope.key, entry);
      entry.pending = api(`/api/block-options/conditions?source=${encodeURIComponent(scope.source)}&user_id=${encodeURIComponent(scope.userId)}`)
        .then(payload => { entry.conditions = payload.conditions || []; entry.error = null; })
        .catch(error => { entry.error = error.message; })
        .finally(() => { entry.loading = false; entry.pending = null; });
    }
    if (entry.pending) await entry.pending;
    if (state.blockPackage !== owner || state.strategyOpenToken !== token || owner.settings.condition_source !== source) return;
    // Re-resolve each live flow: an earlier user/API request cannot overwrite a newer selection.
    blockBuyFlows().filter(flow => flow.target_source === "HTS_CONDITION" && blockConditionScope(flow)?.key === scope.key)
      .forEach(flow => {
        const selected = entry.conditions.find(item => String(item.id) === String(flow.condition_id));
        if (selected) flow.expected_condition_name = selected.name;
      });
  }));
  if (state.blockPackage === owner && state.strategyOpenToken === token && owner.settings.condition_source === source) renderStrategyBlocks();
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
  const migrateGroups = (kind, rows, logic) => {
    if (!rows.length) return [];
    const groupedRows = logic === "ANY" ? [rows] : rows.map(row => [row]);
    return groupedRows.map((conditions, index) => ({
      id: `${kind}-${index + 1}`,
      bar_interval: settings.bar_interval || "3m",
      bar_data_mode: settings.bar_data_mode || "COMPLETE_ONLY",
      bollinger_period: Number(settings.bollinger_period || 55),
      bollinger_stddev: String(settings.bollinger_stddev || "1.04"),
      conditions: conditions.map(condition => ({ ...condition })),
    }));
  };
  if (!Array.isArray(settings.buy_condition_groups)) {
    settings.buy_condition_groups = migrateGroups("buy", settings.buy_conditions, settings.buy_condition_logic);
  }
  if (!Array.isArray(settings.sell_condition_groups) || !settings.sell_condition_groups.length) {
    settings.sell_condition_groups = migrateGroups("sell", settings.sell_conditions, settings.sell_condition_logic);
  }
  [settings.buy_condition_groups, settings.sell_condition_groups].forEach(groups => {
    groups.forEach(group => {
      const common = {
        bar_interval: group.bar_interval || settings.bar_interval || "3m",
        bar_data_mode: group.bar_data_mode || settings.bar_data_mode || "COMPLETE_ONLY",
        bollinger_period: Number(group.bollinger_period || settings.bollinger_period || 55),
        bollinger_stddev: String(group.bollinger_stddev || settings.bollinger_stddev || "1.04"),
      };
      group.conditions = (group.conditions || []).map(condition => ({
        ...newBlockTradeCondition("sell"),
        ...condition,
        left_bar_interval: condition.left_bar_interval || common.bar_interval,
        left_bar_data_mode: condition.left_bar_data_mode || common.bar_data_mode,
        left_bollinger_period: Number(condition.left_bollinger_period || common.bollinger_period),
        left_bollinger_stddev: String(condition.left_bollinger_stddev || common.bollinger_stddev),
        left_atr_period: Number(condition.left_atr_period || 14),
        right_bar_interval: condition.right_bar_interval || common.bar_interval,
        right_bar_data_mode: condition.right_bar_data_mode || common.bar_data_mode,
        right_bollinger_period: Number(condition.right_bollinger_period || common.bollinger_period),
        right_bollinger_stddev: String(condition.right_bollinger_stddev || common.bollinger_stddev),
        right_atr_period: Number(condition.right_atr_period || 14),
      }));
      delete group.bar_interval;
      delete group.bar_data_mode;
      delete group.bollinger_period;
      delete group.bollinger_stddev;
    });
  });
  if (!Array.isArray(settings.buy_flows) || !settings.buy_flows.length) {
    const legacy = Object.fromEntries(BLOCK_BUY_FLOW_FIELDS.map(key => [key, settings[key]]));
    settings.buy_flows = [{ id: "buy-flow-1", name: "매수 흐름 1", ...structuredClone(legacy) }];
  }
  settings.buy_flows.forEach((flow, index) => {
    flow.id ||= `buy-flow-${index + 1}`;
    flow.name ||= `매수 흐름 ${index + 1}`;
    for (const key of BLOCK_BUY_FLOW_FIELDS) {
      if (flow[key] == null) flow[key] = structuredClone(settings[key] ?? (key === "buy_condition_groups" || key === "symbols" ? [] : ""));
    }
    flow.buy_condition_groups.forEach(group => {
      group.conditions = (group.conditions || []).map(condition => ({
        ...newBlockTradeCondition("buy"), ...condition,
        left_bar_interval: condition.left_bar_interval || group.bar_interval || settings.bar_interval || "3m",
        right_bar_interval: condition.right_bar_interval || group.bar_interval || settings.bar_interval || "3m",
        left_bar_data_mode: condition.left_bar_data_mode || group.bar_data_mode || settings.bar_data_mode || "COMPLETE_ONLY",
        right_bar_data_mode: condition.right_bar_data_mode || group.bar_data_mode || settings.bar_data_mode || "COMPLETE_ONLY",
        left_atr_period: Number(condition.left_atr_period || 14),
        right_atr_period: Number(condition.right_atr_period || 14),
      }));
    });
  });
  if (!Array.isArray(settings.sell_flows) || !settings.sell_flows.length) {
    settings.sell_flows = [{
      id: "sell-flow-1",
      name: "매도 흐름 1",
      sell_scan_seconds: settings.sell_scan_seconds || settings.buy_scan_seconds || 30,
      sell_start_time: settings.sell_start_time || "09:00",
      sell_end_time: settings.sell_end_time || "15:20",
      sell_condition_groups: structuredClone(settings.sell_condition_groups || []),
      sell_rule_tree: settings.sell_rule_tree ? structuredClone(settings.sell_rule_tree) : null,
    }];
  }
  settings.sell_flows.forEach((flow, index) => {
    flow.id ||= `sell-flow-${index + 1}`;
    flow.name ||= `매도 흐름 ${index + 1}`;
    const fallbacks = {
      sell_scan_seconds: settings.sell_scan_seconds || settings.buy_scan_seconds || 30,
      sell_start_time: settings.sell_start_time || "09:00",
      sell_end_time: settings.sell_end_time || "15:20",
      sell_condition_groups: settings.sell_condition_groups || [],
      sell_rule_tree: settings.sell_rule_tree || null,
    };
    for (const key of BLOCK_SELL_FLOW_FIELDS) {
      if (flow[key] == null) flow[key] = structuredClone(fallbacks[key]);
    }
    if (!Array.isArray(flow.sell_condition_groups) || !flow.sell_condition_groups.length) {
      flow.sell_condition_groups = structuredClone(settings.sell_condition_groups || []);
    }
    flow.sell_condition_groups.forEach(group => {
      const common = {
        bar_interval: group.bar_interval || settings.bar_interval || "3m",
        bar_data_mode: group.bar_data_mode || settings.bar_data_mode || "COMPLETE_ONLY",
        bollinger_period: Number(group.bollinger_period || settings.bollinger_period || 55),
        bollinger_stddev: String(group.bollinger_stddev || settings.bollinger_stddev || "1.04"),
      };
      group.conditions = (group.conditions || []).map(condition => ({
        ...newBlockTradeCondition("sell"), ...condition,
        left_bar_interval: condition.left_bar_interval || common.bar_interval,
        right_bar_interval: condition.right_bar_interval || common.bar_interval,
        left_bar_data_mode: condition.left_bar_data_mode || common.bar_data_mode,
        right_bar_data_mode: condition.right_bar_data_mode || common.bar_data_mode,
        left_bollinger_period: Number(condition.left_bollinger_period || common.bollinger_period),
        right_bollinger_period: Number(condition.right_bollinger_period || common.bollinger_period),
        left_bollinger_stddev: String(condition.left_bollinger_stddev || common.bollinger_stddev),
        right_bollinger_stddev: String(condition.right_bollinger_stddev || common.bollinger_stddev),
        left_atr_period: Number(condition.left_atr_period || 14),
        right_atr_period: Number(condition.right_atr_period || 14),
      }));
      delete group.bar_interval;
      delete group.bar_data_mode;
      delete group.bollinger_period;
      delete group.bollinger_stddev;
    });
  });
  [
    ...(settings.buy_condition_groups || []),
    ...(settings.sell_condition_groups || []),
    ...settings.buy_flows.flatMap(flow => flow.buy_condition_groups || []),
    ...settings.sell_flows.flatMap(flow => flow.sell_condition_groups || []),
  ].forEach(group => (group.conditions || []).forEach(normalizeConditionAtrPeriods));
  settings.buy_order_type ||= "MARKET";
  settings.sell_order_type ||= "MARKET";
  ensureBlockRuleTree("buy", settings);
  ensureBlockRuleTree("sell", settings);
  settings.buy_flows.forEach(flow => ensureBlockRuleTree("buy", flow));
  settings.sell_flows.forEach(flow => ensureBlockRuleTree("sell", flow));
  return settings;
}

async function loadBlockEditorData(name, openToken = null) {
  const [blockPackage, providers, connections, accounts, capabilities] = await Promise.all([
    api(`/api/strategies/${encodeURIComponent(name)}/block-package`),
    api("/api/settings/providers"),
    api("/api/settings/connections"),
    api("/api/settings/accounts"),
    api("/api/system/capabilities"),
  ]);
  if (openToken != null && openToken !== state.strategyOpenToken) return false;
  state.blockPackage = blockPackage;
  resetConditionFormulaDrafts();
  state.blockExpressionSelections = new WeakMap();
  state.blockConditionCache = new Map();
  normalizeManagedBlockSettings(state.blockPackage.settings);
  prepareBlockVisualLayout(name);
  state.providers = providers;
  state.connections = connections;
  state.accounts = accounts;
  state.capabilities = capabilities;
  state.blockConditions = [];
  state.blockConditionKey = null;
  state.blockConditionError = null;
  return openToken == null || openToken === state.strategyOpenToken;
}

function conditionJoin(text = "또는 (OR)") {
  const join = document.createElement("div");
  join.className = "block-condition-join";
  join.textContent = text;
  return join;
}

function ruleComposerNodeId(kind, flowId, nodeId) {
  return `${blockFlowNodePrefix(kind, flowId)}-rule-${nodeId}`;
}

function ruleComposerActionId(kind, flowId, actionId) {
  return `${blockFlowNodePrefix(kind, flowId)}-action-${actionId}`;
}

function buildComposerRuleBranch(kind, flowId, node, isRoot = false, depth = 0, totalCount = null) {
  const ruleCount = totalCount ?? blockRuleNodeCount(node);
  const children = [];
  if (node.kind === "logic") {
    (node.children || []).forEach((child, index) => {
      if (index) {
        children.push({
          kind: "logic-join",
          id: `${node.id}-join-${index}`,
          ruleKind: kind,
          flowId,
          parentId: node.id,
          boundaryIndex: index,
          operator: node.operator || "ALL",
        });
      }
      children.push(buildComposerRuleBranch(kind, flowId, child, false, depth + 1, ruleCount));
    });
    children.push({
      kind: "rule-add",
      id: `${node.id}-add-child`,
      ruleKind: kind,
      flowId,
      parentId: node.id,
      nodeId: ruleComposerNodeId(kind, flowId, node.id),
      isRoot,
      depth,
      totalCount: ruleCount,
      disabled: (node.children || []).length >= 20,
    });
  }
  if (node.action) {
    children.push({
      kind: "then",
      id: `${node.id}-then`,
      ruleKind: kind,
      flowId,
      ownerId: node.id,
      children: [{ kind: "node", nodeId: ruleComposerActionId(kind, flowId, node.action.id) }],
    });
  }
  return {
    kind: "rule-node",
    id: ruleComposerNodeId(kind, flowId, node.id),
    nodeId: ruleComposerNodeId(kind, flowId, node.id),
    tone: node.kind === "logic" ? (node.operator === "ANY" ? "any" : "all") : "condition",
    ruleKind: kind,
    flowId,
    ruleNodeId: node.id,
    isLogic: node.kind === "logic",
    isRoot,
    children,
  };
}

function buildComposerTreeModel(settings) {
  const flows = Array.isArray(settings?.buy_flows) ? settings.buy_flows : [];
  const sellFlows = Array.isArray(settings?.sell_flows) ? settings.sell_flows : [];
  const flowBranches = flows.map((flow, flowIndex) => {
    const prefix = `flow-${flow.id}`;
    const ruleTree = blockRuleTree("buy", flow.id);
    return {
      kind: "branch",
      id: prefix,
      title: flow.name || `매수 흐름 ${flowIndex + 1}`,
      badge: `${flow.buy_start_time}–${flow.buy_end_time}`,
      tone: "schedule",
      nodeId: `${prefix}-schedule`,
      children: [
        { kind: "node", nodeId: `${prefix}-target` },
        buildComposerRuleBranch("buy", flow.id, ruleTree, true, 0, blockRuleNodeCount(ruleTree)),
      ],
    };
  });
  const sellFlowBranches = sellFlows.map((flow, flowIndex) => {
    const prefix = blockFlowNodePrefix("sell", flow.id);
    const ruleTree = blockRuleTree("sell", flow.id);
    return {
      kind: "branch",
      id: prefix,
      title: flow.name || `매도 흐름 ${flowIndex + 1}`,
      badge: `${flow.sell_start_time}–${flow.sell_end_time}`,
      tone: "sell",
      nodeId: `${prefix}-schedule`,
      children: [
        buildComposerRuleBranch("sell", flow.id, ruleTree, true, 0, blockRuleNodeCount(ruleTree)),
      ],
    };
  });
  return {
    kind: "branch",
    id: "strategy-root",
    nodeId: "connection",
    children: [{
      kind: "section",
      id: "buy-section",
      title: "매수 구성",
      badge: "BUY",
      tone: "buy",
      description: "시간대별 종목 선정과 조건 · 예산과 보유종목은 전략 전체 공유",
      children: [
        ...flowBranches,
        {
          kind: "add",
          id: "add-buy-flow",
          label: "시간대별 매수 흐름 추가",
          action: "add-buy-flow",
          disabled: flows.length >= 8,
        },
        {
          kind: "node",
          id: "shared-buy-budget",
          badge: "SHARED",
          tone: "shared",
          nodeId: "buyRules",
        },
      ],
    }, {
      kind: "section",
      id: "sell-section",
      title: "매도 구성",
      badge: "SELL",
      tone: "sell",
      description: "시간대별 매도 조건 · 보유종목과 주문 처리는 전략 전체 공유",
      children: [
        ...sellFlowBranches,
        {
          kind: "add",
          id: "add-sell-flow",
          label: "시간대별 매도 흐름 추가",
          action: "add-sell-flow",
          disabled: sellFlows.length >= 8,
        },
      ],
    }],
  };
}

function createComposerTreeAddRow(entry, running) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "composer-tree-add-row";
  button.dataset.composerAdd = entry.action || "";
  button.disabled = running || Boolean(entry.disabled);
  const badge = document.createElement("span");
  badge.className = "composer-tree-badge add";
  badge.textContent = "+";
  const label = document.createElement("span");
  label.textContent = entry.label || "블록 추가";
  button.append(badge, label);
  button.addEventListener("click", event => {
    event.stopPropagation();
    if (entry.action === "add-buy-flow") addBlockBuyFlow();
    else if (entry.action === "add-sell-flow") addBlockSellFlow();
    else if (entry.action === "add-rule-action") {
      addBlockRuleAction(entry.ruleKind, entry.flowId, entry.ownerId);
    }
  });
  return button;
}

function createComposerLogicConnector(entry, running) {
  const row = document.createElement("div");
  row.className = "composer-logic-connector-row";
  const button = document.createElement("button");
  button.type = "button";
  button.className = `composer-logic-connector ${entry.operator === "ANY" ? "any" : "all"}`;
  button.textContent = entry.operator === "ANY" ? "OR" : "AND";
  button.title = `${button.textContent} · 이 연결만 ${entry.operator === "ANY" ? "AND" : "OR"}로 변경`;
  button.setAttribute("aria-label", `${entry.boundaryIndex}번과 ${entry.boundaryIndex + 1}번 조건 사이 ${button.textContent} 연결 변경`);
  button.disabled = running;
  button.addEventListener("click", event => {
    event.stopPropagation();
    setBlockRuleConnection(
      entry.ruleKind, entry.flowId, entry.parentId, entry.boundaryIndex,
      entry.operator === "ANY" ? "ALL" : "ANY",
    );
  });
  row.append(button);
  return row;
}

function createComposerRuleAddMenu(entry, running) {
  const toolbar = document.createElement("div");
  toolbar.className = "composer-rule-toolbar";
  const menu = document.createElement("details");
  menu.className = "composer-tree-rule-add";
  const summary = document.createElement("summary");
  summary.textContent = "+ 조건 추가";
  summary.setAttribute("aria-label", "조건식 또는 괄호 묶음 추가 메뉴");
  menu.append(summary);
  const options = document.createElement("div");
  options.className = "composer-tree-rule-add-options";
  const make = (label, childKind, operator = "ALL") => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    const addedNodes = childKind === "logic" ? 2 : 1;
    const addedDepth = childKind === "logic" ? 2 : 1;
    button.disabled = running
      || Boolean(entry.disabled)
      || Number(entry.totalCount || 0) + addedNodes > 100
      || Number(entry.depth || 0) + addedDepth > 8;
    button.addEventListener("click", event => {
      event.stopPropagation();
      addBlockRuleChild(entry.ruleKind, entry.flowId, entry.parentId, childKind, operator);
    });
    options.append(button);
  };
  make("+ 조건식", "condition");
  make("( ) 괄호 묶음", "logic", "ALL");
  menu.append(options);
  const settings = document.createElement("button");
  settings.type = "button";
  settings.className = "composer-rule-settings";
  settings.textContent = entry.isRoot ? "전체 조건 설정" : "괄호 설정";
  settings.dataset.blockNode = entry.nodeId;
  settings.setAttribute("aria-label", `${settings.textContent} · 연결과 주문 편집`);
  settings.classList.toggle("selected", state.blockSelectedNode === entry.nodeId);
  settings.addEventListener("click", event => {
    event.stopPropagation();
    state.blockSelectedNode = entry.nodeId;
    renderStrategyBlocks();
    focusComposerInspector();
  });
  toolbar.append(menu, settings);
  return toolbar;
}

function createComposerThenBranch(entry, definitions, running) {
  const branch = document.createElement("div");
  branch.className = "composer-then-branch";
  const label = document.createElement("span");
  label.className = "composer-then-label";
  label.textContent = "→ 실행";
  branch.append(label);
  const children = document.createElement("div");
  children.className = "composer-then-children";
  (entry.children || []).forEach(child => children.append(renderComposerTreeBranch(child, definitions, running)));
  branch.append(children);
  return branch;
}

function createComposerTreeBranchHeader(entry, running) {
  const header = document.createElement("div");
  header.className = "composer-tree-flow";
  const badge = document.createElement(entry.ruleNodeId ? "button" : "span");
  if (entry.ruleNodeId) badge.type = "button";
  badge.className = `composer-tree-badge ${entry.tone || "general"}`;
  badge.textContent = entry.badge || "GROUP";
  if (entry.ruleNodeId) {
    badge.disabled = running;
    badge.title = "묶음 연결 방식 변경";
    badge.addEventListener("click", event => {
      event.stopPropagation();
      const node = findBlockRuleNode(entry.ruleKind, entry.flowId, entry.ruleNodeId)?.node;
      if (node?.kind === "logic") {
        setBlockRuleOperator(entry.ruleKind, entry.flowId, node.id, node.operator === "ANY" ? "ALL" : "ANY");
      }
    });
  }
  const copy = document.createElement("span");
  copy.className = "composer-node-copy";
  const title = document.createElement("strong");
  title.textContent = entry.title || "전략 흐름";
  copy.append(title);
  if (entry.description) {
    const description = document.createElement("small");
    description.textContent = entry.description;
    copy.append(description);
  }
  header.append(badge, copy);
  return header;
}

function renderComposerTreeBranch(entry, definitions, running) {
  if (entry.kind === "add") return createComposerTreeAddRow(entry, running);
  if (entry.kind === "logic-join") return createComposerLogicConnector(entry, running);
  if (entry.kind === "rule-add") return createComposerRuleAddMenu(entry, running);
  if (entry.kind === "then") return createComposerThenBranch(entry, definitions, running);
  const branch = document.createElement(entry.kind === "section" ? "section" : "div");
  branch.className = `composer-tree-branch${entry.kind === "section" ? " composer-tree-section" : ""}${entry.kind === "rule-node" ? " composer-rule-branch" : ""}`;
  if (entry.kind === "rule-node") {
    branch.classList.add(entry.isRoot ? "composer-rule-root" : "composer-rule-nested");
    branch.classList.toggle("composer-rule-group", entry.isLogic);
    if (entry.isLogic) branch.setAttribute("aria-label", entry.isRoot ? "전체 조건" : "괄호로 묶은 조건");
  }
  if (entry.tone) {
    branch.classList.add(entry.tone);
    branch.dataset.tone = entry.tone;
  }
  branch.dataset.composerBranch = entry.id || entry.nodeId || "branch";
  if (entry.title) branch.append(createComposerTreeBranchHeader(entry, running));
  if (entry.nodeId && !entry.isLogic) {
    const definition = definitions.get(entry.nodeId);
    if (definition) branch.append(createComposerTreeRow(definition, running));
  }
  if (entry.children?.length) {
    const children = document.createElement("div");
    children.className = "composer-tree-children";
    children.setAttribute("role", "group");
    entry.children.forEach(child => children.append(renderComposerTreeBranch(child, definitions, running)));
    branch.append(children);
  }
  return branch;
}

function prepareBlockVisualLayout(name) {
  if (!state.blockPackage || state.blockPackage.template !== "condition-bollinger-v1") return;
  const localLayout = storedBlockLayout(name);
  const packageLayout = cloneBlockLayout(state.blockPackage.layout);
  const legacyFreeLayout = Object.keys(state.blockPackage.layout?.nodes || {}).length > 0;
  state.blockPackage.layout = {
    nodes: {},
    // The fixed tree has one canonical arrangement. Only the user's local
    // canvas view is preferred. A fixed-tree package may carry its viewport,
    // while a legacy free-graph package starts from the canonical 100% view.
    viewport: localLayout?.viewport || (!legacyFreeLayout ? packageLayout.viewport : null) || { x: 34, y: 34, zoom: 1 },
  };
  state.blockGraphPlan = null;
}

function ensureBlockLayout() {
  if (!state.blockPackage) return null;
  state.blockPackage.layout ||= {};
  // Older packages may contain freely dragged node coordinates. They remain
  // accepted by the backend for import compatibility, but the fixed tree UI
  // intentionally never reads or persists them.
  state.blockPackage.layout.nodes = {};
  state.blockPackage.layout.viewport ||= { x: 24, y: 24, zoom: 1 };
  const viewport = state.blockPackage.layout.viewport;
  viewport.x = clampBlockNumber(viewport.x, -BLOCK_VIEWPORT_COORD_LIMIT, BLOCK_VIEWPORT_COORD_LIMIT, 24);
  viewport.y = clampBlockNumber(viewport.y, -BLOCK_VIEWPORT_COORD_LIMIT, BLOCK_VIEWPORT_COORD_LIMIT, 24);
  viewport.zoom = clampBlockNumber(viewport.zoom, BLOCK_ZOOM_MIN, BLOCK_ZOOM_MAX, 1);
  return state.blockPackage.layout;
}

function constrainBlockViewport(viewport) {
  viewport.x = clampBlockNumber(viewport.x, -BLOCK_VIEWPORT_COORD_LIMIT, BLOCK_VIEWPORT_COORD_LIMIT, 24);
  viewport.y = clampBlockNumber(viewport.y, -BLOCK_VIEWPORT_COORD_LIMIT, BLOCK_VIEWPORT_COORD_LIMIT, 24);
  viewport.zoom = clampBlockNumber(viewport.zoom, BLOCK_ZOOM_MIN, BLOCK_ZOOM_MAX, 1);
  return viewport;
}

function applyBlockViewport(world) {
  const viewport = constrainBlockViewport(ensureBlockLayout()?.viewport || { x: 24, y: 24, zoom: 1 });
  world.style.transform = `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})`;
  elements.blockCanvas.style.backgroundPosition = `${viewport.x}px ${viewport.y}px`;
  elements.blockCanvas.style.backgroundSize = `${18 * viewport.zoom}px ${18 * viewport.zoom}px`;
  if (elements.blockZoomLabel) elements.blockZoomLabel.textContent = `${Math.round(viewport.zoom * 100)}%`;
}

function bindBlockCanvasPan(world) {
  const canvas = elements.blockCanvas;
  if (canvas._blockPanHandler) canvas.removeEventListener("pointerdown", canvas._blockPanHandler);
  if (canvas._blockWheelHandler) canvas.removeEventListener("wheel", canvas._blockWheelHandler);
  if (canvas._blockKeyHandler) canvas.removeEventListener("keydown", canvas._blockKeyHandler);
  const panHandler = event => {
    if (event.button !== 0 || event.target.closest(".composer-tree-row, .composer-tree-add-row, .composer-inspector, button, input, select, textarea, a, summary, [contenteditable]")) return;
    event.preventDefault();
    const viewport = ensureBlockLayout().viewport;
    const strategyName = state.selected;
    const layout = state.blockPackage?.layout;
    const start = { x: viewport.x, y: viewport.y };
    const origin = { x: event.clientX, y: event.clientY };
    let moved = false;
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add("panning");
    const move = moveEvent => {
      viewport.x = clampBlockNumber(
        start.x + moveEvent.clientX - origin.x,
        -BLOCK_VIEWPORT_COORD_LIMIT,
        BLOCK_VIEWPORT_COORD_LIMIT,
        start.x,
      );
      viewport.y = clampBlockNumber(
        start.y + moveEvent.clientY - origin.y,
        -BLOCK_VIEWPORT_COORD_LIMIT,
        BLOCK_VIEWPORT_COORD_LIMIT,
        start.y,
      );
      moved ||= Math.abs(viewport.x - start.x) >= .1 || Math.abs(viewport.y - start.y) >= .1;
      applyBlockViewport(world);
    };
    const end = endEvent => {
      if (canvas.hasPointerCapture?.(endEvent.pointerId)) {
        canvas.releasePointerCapture(endEvent.pointerId);
      }
      canvas.classList.remove("panning");
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("pointerup", end);
      canvas.removeEventListener("pointercancel", end);
      if (moved) persistBlockLayout(strategyName, layout);
    };
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
  };
  const wheelHandler = event => {
    if (event.target.closest(".composer-inspector")) return;
    event.preventDefault();
    const viewport = ensureBlockLayout().viewport;
    if (event.ctrlKey || event.metaKey) {
      const rect = canvas.getBoundingClientRect();
      const worldX = (event.clientX - rect.left - viewport.x) / viewport.zoom;
      const worldY = (event.clientY - rect.top - viewport.y) / viewport.zoom;
      const next = Math.min(2, Math.max(.2, viewport.zoom * Math.exp(-event.deltaY * .0015)));
      viewport.x = event.clientX - rect.left - worldX * next;
      viewport.y = event.clientY - rect.top - worldY * next;
      viewport.zoom = next;
    } else {
      viewport.x -= event.deltaX + (event.shiftKey ? event.deltaY : 0);
      viewport.y -= event.shiftKey ? 0 : event.deltaY;
    }
    constrainBlockViewport(viewport);
    applyBlockViewport(world);
    window.clearTimeout(canvas._blockViewPersistTimer);
    const strategyName = state.selected;
    const layout = state.blockPackage?.layout;
    canvas._blockViewPersistTimer = window.setTimeout(
      () => persistBlockLayout(strategyName, layout),
      180,
    );
  };
  const keyHandler = event => {
    if (event.target !== canvas || event.ctrlKey || event.metaKey || event.altKey) return;
    const viewport = ensureBlockLayout().viewport;
    const step = event.shiftKey ? 120 : 40;
    const movement = {
      ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step],
    }[event.key];
    if (movement) {
      event.preventDefault();
      viewport.x += movement[0];
      viewport.y += movement[1];
      constrainBlockViewport(viewport);
      applyBlockViewport(world);
      persistBlockLayout();
      return;
    }
    if (["+", "="].includes(event.key)) {
      event.preventDefault();
      setBlockZoom(viewport.zoom + .1);
    } else if (["-", "_"].includes(event.key)) {
      event.preventDefault();
      setBlockZoom(viewport.zoom - .1);
    } else if (event.key === "0") {
      event.preventDefault();
      setBlockZoom(1);
    } else if (event.key === "Home") {
      event.preventDefault();
      fitBlockGraph();
    }
  };
  canvas._blockPanHandler = panHandler;
  canvas._blockWheelHandler = wheelHandler;
  canvas._blockKeyHandler = keyHandler;
  canvas.addEventListener("pointerdown", panHandler);
  canvas.addEventListener("wheel", wheelHandler, { passive: false });
  canvas.addEventListener("keydown", keyHandler);
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
  viewport.zoom = Math.min(2, Math.max(.2, nextZoom));
  viewport.x = centerX - worldX * viewport.zoom;
  viewport.y = centerY - worldY * viewport.zoom;
  applyBlockViewport(world);
  persistBlockLayout();
}

function fitBlockGraph() {
  const world = elements.blockCanvas.querySelector(".block-canvas-world");
  const tree = world?.querySelector(".composer-fixed-tree");
  if (!world || !tree || !state.blockPackage) return;
  const minX = tree.offsetLeft;
  const minY = tree.offsetTop;
  const maxX = minX + Math.max(tree.offsetWidth, tree.scrollWidth);
  const maxY = minY + Math.max(tree.offsetHeight, tree.scrollHeight);
  if (maxX <= minX || maxY <= minY) return;
  const padding = 46;
  const zoom = Math.min(1.25, Math.max(.2, Math.min(
    (elements.blockCanvas.clientWidth - padding * 2) / (maxX - minX),
    (elements.blockCanvas.clientHeight - padding * 2) / (maxY - minY),
  )));
  const viewport = ensureBlockLayout().viewport;
  viewport.zoom = zoom;
  viewport.x = (elements.blockCanvas.clientWidth - (maxX - minX) * zoom) / 2 - minX * zoom;
  viewport.y = (elements.blockCanvas.clientHeight - (maxY - minY) * zoom) / 2 - minY * zoom;
  applyBlockViewport(world);
  persistBlockLayout();
}

function resetBlockGraphLayout() {
  if (!state.blockPackage) return;
  state.blockPackage.layout = {
    nodes: {},
    viewport: { x: 34, y: 34, zoom: 1 },
  };
  state.blockGraphPlan = null;
  persistBlockLayout();
  renderStrategyBlocks();
}

function scheduleSectionKey(key) {
  return `${state.selected || "draft"}:${key}`;
}

function setScheduleSectionExpanded(key, expanded) {
  state.blockScheduleExpanded.set(scheduleSectionKey(key), expanded);
}

function scheduleSectionExpanded(key, initiallyOpen = false) {
  const scoped = scheduleSectionKey(key);
  if (!state.blockScheduleExpanded.has(scoped)) state.blockScheduleExpanded.set(scoped, initiallyOpen);
  return state.blockScheduleExpanded.get(scoped);
}

function captureScheduleView() {
  const active = document.activeElement;
  const owner = elements.blockCanvas.contains(active) ? active.closest("[data-schedule-key]") : null;
  const controlIndex = owner ? [...owner.querySelectorAll("input, select, textarea, button")].indexOf(active) : -1;
  let selection = null;
  try { if (active?.selectionStart != null) selection = [active.selectionStart, active.selectionEnd]; } catch (_) { /* Non-text control. */ }
  return { top: elements.blockCanvas.scrollTop, left: elements.blockCanvas.scrollLeft,
    owner: owner?.dataset.scheduleKey, id: active?.id, controlIndex, selection };
}

function restoreScheduleView(snapshot) {
  if (!snapshot) return;
  const owner = snapshot.owner ? [...elements.blockCanvas.querySelectorAll("[data-schedule-key]")]
    .find(node => node.dataset.scheduleKey === snapshot.owner) : null;
  const control = snapshot.id ? document.getElementById(snapshot.id)
    : owner?.querySelectorAll("input, select, textarea, button")[snapshot.controlIndex];
  if (control && elements.blockCanvas.contains(control) && !control.disabled && !control.closest("[hidden]")) {
    control.focus({ preventScroll: true });
    if (snapshot.selection) {
      try { control.setSelectionRange(...snapshot.selection); } catch (_) { /* Non-text control. */ }
    }
  }
  elements.blockCanvas.scrollTop = snapshot.top;
  elements.blockCanvas.scrollLeft = snapshot.left;
}

function captureComposerInspectorView() {
  const inspector = elements.blockCanvas?.querySelector(".composer-inspector");
  const body = inspector?.querySelector(".composer-inspector-body");
  if (!inspector || !body) return null;
  const controls = [...inspector.querySelectorAll("input, select, textarea, button")];
  const active = inspector.contains(document.activeElement) ? document.activeElement : null;
  let selection = null;
  try { if (active?.selectionStart != null) selection = [active.selectionStart, active.selectionEnd]; } catch (_) { /* Non-text control. */ }
  return {
    node: inspector.dataset.blockInspector,
    top: body.scrollTop,
    left: body.scrollLeft,
    expressionControl: active?.dataset?.expressionControl || null,
    id: active?.id || null,
    controlIndex: active ? controls.indexOf(active) : -1,
    selection,
    formulaFocus: captureConditionFormulaFocus(inspector),
  };
}

function restoreComposerInspectorView(snapshot) {
  if (!snapshot) return;
  const inspector = elements.blockCanvas?.querySelector(".composer-inspector");
  const body = inspector?.querySelector(".composer-inspector-body");
  if (!inspector || !body || inspector.dataset.blockInspector !== snapshot.node) return;
  const controls = [...inspector.querySelectorAll("input, select, textarea, button")];
  const control = snapshot.expressionControl
    ? inspector.querySelector(`[data-expression-control="${CSS.escape(snapshot.expressionControl)}"]`)
    : snapshot.id ? inspector.querySelector(`#${CSS.escape(snapshot.id)}`) : controls[snapshot.controlIndex];
  if (control && !control.disabled) {
    control.focus({ preventScroll: true });
    if (snapshot.selection) {
      try { control.setSelectionRange(...snapshot.selection); } catch (_) { /* Non-text control. */ }
    }
  }
  restoreConditionFormulaFocus(snapshot.formulaFocus, inspector);
  body.scrollTop = snapshot.top;
  body.scrollLeft = snapshot.left;
}

function createScheduleSection({ key, title, summary = "", badge = "", tone = "", open = false, action = null }) {
  const section = document.createElement("section");
  section.className = `schedule-section ${tone}`.trim();
  section.dataset.scheduleKey = key;
  const expanded = scheduleSectionExpanded(key, open);
  const header = document.createElement("header");
  header.className = "schedule-section-heading";
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "schedule-section-toggle";
  toggle.setAttribute("aria-expanded", String(expanded));
  toggle.setAttribute("aria-label", `${title} ${expanded ? "접기" : "펼치기"}`);
  const chevron = document.createElement("span");
  chevron.className = "schedule-chevron";
  chevron.textContent = expanded ? "⌄" : "›";
  const copy = document.createElement("span");
  copy.className = "schedule-section-copy";
  const strong = document.createElement("strong");
  strong.textContent = title;
  const detail = document.createElement("small");
  detail.textContent = summary;
  copy.append(strong, detail);
  toggle.append(chevron, copy);
  if (badge) {
    const tag = document.createElement("span");
    tag.className = "schedule-section-badge";
    tag.textContent = badge;
    toggle.append(tag);
  }
  const verb = document.createElement("span");
  verb.className = "schedule-section-verb";
  verb.textContent = expanded ? "접기" : "편집 · 펼치기";
  toggle.append(verb);
  toggle.addEventListener("click", () => {
    setScheduleSectionExpanded(key, !expanded);
    renderStrategyBlocks();
  });
  header.append(toggle);
  if (action) header.append(action);
  const content = document.createElement("div");
  content.className = "schedule-section-content";
  content.hidden = !expanded;
  section.append(header, content);
  return { section, content };
}

function createScheduleFields(fields, disabled) {
  const grid = document.createElement("div");
  grid.className = "schedule-fields-grid";
  fields.forEach(field => grid.append(createBlockFieldControl(field, disabled)));
  return grid;
}

function scheduleConditionSummary(group) {
  const labels = new Map([...BLOCK_CONDITION_METRICS, ["value", "직접 값"]]);
  const operators = new Map(BLOCK_CONDITION_OPERATORS);
  const rows = group.conditions || [];
  const operand = (row, side) => {
    const expression = conditionSideExpression(row, side);
    if (expression) return conditionExpressionSummary(expression);
    const metric = row[side];
    if (side === "right" && metric === "value") return row.value;
    const label = labels.get(metric) || metric;
    if (!["obv", "atr"].includes(metric)) return label;
    const interval = row[`${side}_bar_interval`] || "3m";
    const intervalLabel = expressionIntervalLabel(interval);
    const parameters = metric === "atr" ? `${row[`${side}_atr_period`] || 14} · ` : "";
    return `${label}(${parameters}${intervalLabel} · ${blockConditionBarPosition(row, side)})`;
  };
  const snippets = rows.slice(0, 2).map(row => `${operand(row, "left")} ${operators.get(row.operator) || row.operator} ${operand(row, "right")}`);
  return `${snippets.join(" 또는 ")}${rows.length > 2 ? ` 외 ${rows.length - 2}개` : ""}`;
}

function appendScheduleConditions(container, kind, flowId, running) {
  const groups = blockConditionGroups(kind, flowId);
  if (!groups.length && kind === "buy") {
    const empty = document.createElement("div");
    empty.className = "schedule-empty-condition";
    empty.append(createEmptyBuyConditionEditor(running, flowId));
    container.append(empty);
    return;
  }
  groups.forEach((group, index) => {
    if (index) {
      const join = document.createElement("div");
      join.className = "schedule-and-join";
      join.textContent = "그리고 (AND)";
      container.append(join);
    }
    const panel = createScheduleSection({ key: `condition:${flowId || kind}:${group.id}`,
      title: `${kind === "buy" ? "매수" : "매도"} 조건 ${index + 1}`, summary: scheduleConditionSummary(group),
      badge: `${group.conditions.length}개 중 하나 (OR)`, tone: "condition" });
    panel.content.append(createConditionEditor(kind, index, running, flowId));
    container.append(panel.section);
  });
}

function renderConditionSchedule(parsed, running, snapshot) {
  const settings = state.blockPackage.settings;
  const fields = new Map(parsed.fields.filter(field => !field.flowId).map(field => [field.settingKey, field]));
  const take = (...keys) => keys.map(key => fields.get(key)).filter(Boolean);
  const root = document.createElement("div");
  root.className = "schedule-editor";
  const intro = document.createElement("div");
  intro.className = "schedule-editor-intro";
  const title = document.createElement("strong");
  title.textContent = "한 전략의 시간별 매수·매도 일정";
  const hint = document.createElement("p");
  hint.textContent = "각 시간대의 종목 조회·매수 조건과 매도 조건을 따로 설정합니다. 계좌·예산·보유종목은 전략 전체에서 공유합니다.";
  intro.append(title, hint);
  root.append(intro);
  const budget = Number(settings.total_operating_budget || 0).toLocaleString("ko-KR");
  const shared = createScheduleSection({ key: "shared", title: "공통 설정", tone: "shared",
    summary: `${settings.trading_account} · 총 운용 예산 ${budget}원 · 모든 매수 흐름이 공유`, badge: "계좌 · 예산 · 주문" });
  shared.content.append(createScheduleFields(take("strategy_id", "trading_account", "condition_source", "market_venue", "route",
    "buy_budget_per_symbol", "total_operating_budget", "max_new_buys_per_scan", "buy_order_type", "sell_order_type"), running));
  root.append(shared.section);
  const flowHeading = document.createElement("div");
  flowHeading.className = "schedule-list-heading";
  const flowTitle = document.createElement("strong");
  flowTitle.textContent = "매수 일정";
  const flowHint = document.createElement("span");
  flowHint.textContent = "한국 시각 · 종료 시각 제외 · 시간순 표시";
  flowHeading.append(flowTitle, flowHint);
  root.append(flowHeading);
  const flows = blockBuyFlows();
  const presentation = flows.map((flow, index) => ({ flow, index }))
    .sort((a, b) => String(a.flow.buy_start_time).localeCompare(String(b.flow.buy_start_time)) || a.index - b.index);
  const list = document.createElement("div");
  list.className = "schedule-flow-list";
  presentation.forEach(({ flow, index }) => {
    const flowFields = new Map(blockBuyFlowFields(flow).map(field => [field.settingKey, field]));
    const takeFlow = (...keys) => keys.map(key => flowFields.get(key)).filter(Boolean);
    const groups = blockConditionGroups("buy", flow.id);
    const sourceLabel = flow.target_source === "RANKING" ? "순위검색" : flow.target_source === "SYMBOLS" ? "지정 종목" : "HTS 조건검색";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button quiet compact schedule-flow-remove";
    remove.textContent = "삭제";
    remove.setAttribute("aria-label", `${flow.name} 매수 흐름 삭제`);
    remove.disabled = running || flows.length <= 1;
    remove.addEventListener("click", () => removeBlockBuyFlow(flow.id));
    const panel = createScheduleSection({ key: `flow:${flow.id}`, title: `${flow.buy_start_time}–${flow.buy_end_time}  ${flow.name}`,
      summary: `${sourceLabel} → ${groups.length ? `매수 조건 ${groups.length}개 블록 모두 통과` : "추가 조건 없이 통과"} → 공통 주문`,
      badge: `${flow.buy_scan_seconds}초마다`, tone: "buy-flow", open: index === 0, action: remove });
    panel.content.append(createScheduleFields(takeFlow("name", "buy_start_time", "buy_end_time", "buy_scan_seconds"), running));
    const branch = document.createElement("div");
    branch.className = "schedule-branch";
    const conditionData = blockConditionData(flow);
    const discovery = createScheduleSection({ key: `source:${flow.id}`, title: "종목 가져오기", tone: "discovery", open: true,
      summary: flow.target_source === "HTS_CONDITION"
        ? `${sourceLabel} · ${flow.expected_condition_name || flow.condition_id || "조건식을 선택하세요"}`
        : flow.target_source === "SYMBOLS" ? `${sourceLabel} · ${(flow.symbols || []).join(", ") || "종목을 입력하세요"}` : `${sourceLabel} · 상위 ${flow.ranking_limit}개`,
      badge: conditionData.loading ? "조건식 조회 중" : "조회" });
    const targetFields = flow.target_source === "RANKING"
      ? takeFlow("target_source", "ranking_kind", "ranking_venue", "ranking_market", "ranking_limit")
      : flow.target_source === "SYMBOLS" ? takeFlow("target_source", "symbols") : takeFlow("target_source", "hts_user_id", "condition_id");
    discovery.content.append(createScheduleFields(targetFields, running));
    branch.append(discovery.section);
    const conditionChain = document.createElement("div");
    conditionChain.className = "schedule-condition-chain";
    appendScheduleConditions(conditionChain, "buy", flow.id, running);
    branch.append(conditionChain);
    const order = document.createElement("div");
    order.className = "schedule-order-summary";
    const orderTitle = document.createElement("strong");
    orderTitle.textContent = `${settings.buy_order_type === "LIMIT" ? "현재가 지정가" : "시장가"} 매수 · 공통 주문 처리`;
    const orderHint = document.createElement("span");
    orderHint.textContent = "적용 시간 · 미보유 · 미체결 · 가능 수량 · 전략 전체 운용 예산을 확인한 뒤 주문합니다.";
    order.append(orderTitle, orderHint);
    branch.append(order);
    panel.content.append(branch);
    list.append(panel.section);
  });
  root.append(list);
  const add = document.createElement("button");
  add.type = "button";
  add.className = "button quiet schedule-add-flow";
  add.textContent = "+ 매수 흐름 추가";
  add.disabled = running || flows.length >= 8;
  add.addEventListener("click", addBlockBuyFlow);
  root.append(add);
  const parallel = document.createElement("div");
  parallel.className = "schedule-list-heading schedule-parallel-heading";
  const parallelTitle = document.createElement("strong");
  parallelTitle.textContent = "공통 보유종목 관리";
  const parallelHint = document.createElement("span");
  parallelHint.textContent = "위 매수 일정의 종료와 관계없이 별도로 실행";
  parallel.append(parallelTitle, parallelHint);
  root.append(parallel);
  const sellFlows = blockSellFlows();
  const sellList = document.createElement("div");
  sellList.className = "schedule-flow-list";
  sellFlows.map((flow, index) => ({ flow, index }))
    .sort((a, b) => String(a.flow.sell_start_time).localeCompare(String(b.flow.sell_start_time)) || a.index - b.index)
    .forEach(({ flow, index }) => {
      const flowFields = new Map(blockSellFlowFields(flow).map(field => [field.settingKey, field]));
      const takeFlow = (...keys) => keys.map(key => flowFields.get(key)).filter(Boolean);
      const groups = blockConditionGroups("sell", flow.id);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "button quiet compact schedule-flow-remove";
      remove.textContent = "삭제";
      remove.setAttribute("aria-label", `${flow.name} 매도 흐름 삭제`);
      remove.disabled = running || sellFlows.length <= 1;
      remove.addEventListener("click", () => removeBlockSellFlow(flow.id));
      const panel = createScheduleSection({
        key: `sell-flow:${flow.id}`,
        title: `${flow.sell_start_time}–${flow.sell_end_time}  ${flow.name}`,
        summary: `전략 보유종목 → 매도 조건 ${groups.length}개 블록 모두 통과 → 공통 주문`,
        badge: `${flow.sell_scan_seconds}초 보완 판단`, tone: "sell", open: index === 0, action: remove,
      });
      panel.content.append(createScheduleFields(
        takeFlow("name", "sell_start_time", "sell_end_time", "sell_scan_seconds"), running,
      ));
      const sellHint = document.createElement("p");
      sellHint.className = "schedule-explanation";
      sellHint.textContent = "수익률 조건은 실시간 현재가, 차트 조건은 각 조건에 설정한 봉 주기와 완성 여부로 판단합니다.";
      panel.content.append(sellHint);
      appendScheduleConditions(panel.content, "sell", flow.id, running);
      sellList.append(panel.section);
    });
  root.append(sellList);
  const addSell = document.createElement("button");
  addSell.type = "button";
  addSell.className = "button quiet schedule-add-flow";
  addSell.textContent = "+ 매도 흐름 추가";
  addSell.disabled = running || sellFlows.length >= 8;
  addSell.addEventListener("click", addBlockSellFlow);
  root.append(addSell);
  const generated = document.createElement("p");
  generated.className = "schedule-generated-note";
  generated.textContent = "실행용 Python 코드 자동 생성";
  root.append(generated);
  elements.blockCanvas.append(root);
  restoreScheduleView(snapshot);
}

function blockRuleConditionNodes(root) {
  const conditions = [];
  walkBlockRule(root, node => {
    if (node.kind === "condition" && node.condition) conditions.push(node.condition);
  });
  return conditions;
}

function blockRuleConditionSummary(condition) {
  const operator = new Map(BLOCK_CONDITION_OPERATORS).get(condition.operator) || condition.operator || "=";
  return `${formulaExpressionSummary(effectiveFormulaExpression(condition, "left"))} ${operator} ${formulaExpressionSummary(effectiveFormulaExpression(condition, "right"))}`;
}

function collectComposerRuleDefinitions(nodes, kind, flowId, root) {
  walkBlockRule(root, (node, parent, index, depth) => {
    const isRoot = node === root;
    const nodeId = ruleComposerNodeId(kind, flowId, node.id);
    if (node.kind === "logic") {
      const operator = node.operator === "ANY" ? "ANY" : "ALL";
      const conditionCount = blockRuleConditionNodes(node).length;
      nodes.push({
        id: nodeId,
        title: isRoot ? "전체 조건 설정" : "괄호 설정",
        meta: `${conditionCount}개 조건 · 연결과 주문`,
        tone: "condition",
        logic: "( )",
        logicTone: "condition",
        width: 560,
        summary: [],
        description: isRoot
          ? "이 흐름의 조건과 주문을 설정합니다."
          : "이 안의 조건은 한 묶음으로 판단합니다.",
        ruleNode: { kind, flowId, nodeId: node.id, isRoot },
      });
    } else {
      nodes.push({
        id: nodeId,
        title: "조건식",
        meta: blockConditionTiming(node.condition),
        tone: "condition",
        logic: "IF",
        logicTone: "condition",
        width: 620,
        summary: [blockRuleConditionSummary(node.condition)],
        description: "식의 데이터와 계산·비교를 한 줄에서 구성합니다. 데이터 토큰을 누르면 지표와 봉 기준을 편집할 수 있습니다.",
        ruleNode: { kind, flowId, nodeId: node.id, isRoot: false },
      });
    }
    if (node.action) {
      nodes.push({
        id: ruleComposerActionId(kind, flowId, node.action.id),
        title: kind === "sell" ? "매도 주문" : "매수 주문",
        meta: node.action.order_type === "LIMIT" ? "현재가 지정가" : "시장가",
        tone: "event",
        logic: node.action.side || (kind === "sell" ? "SELL" : "BUY"),
        logicTone: kind === "sell" ? "sell" : "buy",
        width: 420,
        summary: [node.action.order_type === "LIMIT" ? "현재가 지정가 주문" : "시장가 주문"],
        actions: kind === "sell"
          ? ["전략 소유 수량", "주문 제출", "상태 조정"]
          : ["공통 예산", "멱등성 예약", "위험 검사", "주문 제출"],
        ruleActionOwner: { kind, flowId, nodeId: node.id, order_type: node.action.order_type },
      });
    }
  });
}

function renderConditionBollingerBlocks(parsed, running, inspectorSnapshot = null) {
  const fields = new Map(parsed.fields.filter(field => !field.flowId).map(field => [field.settingKey, field]));
  const take = (...keys) => keys.map(key => fields.get(key)).filter(Boolean);
  const settings = state.blockPackage.settings;
  const flows = blockBuyFlows();
  const sellFlows = blockSellFlows();
  const targetLabels = { HTS_CONDITION: "HTS 조건검색", RANKING: "시장 순위검색", SYMBOLS: "지정 종목" };
  const world = document.createElement("div");
  world.className = "block-canvas-world composer-tree-world";
  state.blockGraphPlan = null;
  const money = value => `${Number(value || 0).toLocaleString("ko-KR")}원`;
  const valueOf = key => fields.get(key)?.value;
  const nodes = [
    { id: "connection", title: "전략 연결", meta: "모든 흐름 공통", tone: "connection", logic: "공통", logicTone: "root", width: 460,
      fields: take("strategy_id", "trading_account", "condition_source", "market_venue", "route"),
      summary: [valueOf("trading_account"), valueOf("condition_source"), `${valueOf("market_venue")} → ${valueOf("route")}`],
      description: "계좌와 API, 시세 시장, 주문 경로를 모든 매수 일정과 매도 판단에서 공유합니다.",
      actions: ["같은 계좌", "공통 예산", "공통 보유종목"] },
    { id: "buyRules", title: "공통 예산 · 위험", meta: "전략 전체 공유", tone: "risk", logic: "공통", logicTone: "all", width: 460,
      fields: take("buy_budget_per_symbol", "total_operating_budget", "max_new_buys_per_scan"),
      summary: [`종목당 ${money(valueOf("buy_budget_per_symbol"))}`, `총 ${money(valueOf("total_operating_budget"))}`, `조회당 ${valueOf("max_new_buys_per_scan")}종목`],
      actions: ["미보유", "미체결 없음", "가능 수량 ≥ 1", "운용 예산 이내"] },
  ];
  flows.forEach((flow, flowIndex) => {
    const prefix = `flow-${flow.id}`;
    const flowFields = new Map(blockBuyFlowFields(flow).map(field => [field.settingKey, field]));
    const takeFlow = (...keys) => keys.map(key => flowFields.get(key)).filter(Boolean);
    const conditions = blockConditionData(flow);
    const targetFields = flow.target_source === "RANKING"
      ? takeFlow("target_source", "ranking_kind", "ranking_venue", "ranking_market", "ranking_limit")
      : flow.target_source === "SYMBOLS" ? takeFlow("target_source", "symbols") : takeFlow("target_source", "hts_user_id", "condition_id");
    const selectedCondition = conditions.conditions?.find(item => String(item.id) === String(flow.condition_id));
    const targetSummary = flow.target_source === "HTS_CONDITION"
      ? (selectedCondition?.name || flow.expected_condition_name || `조건식 ${flow.condition_id || "미선택"}`)
      : flow.target_source === "RANKING" ? `${flow.ranking_kind} · ${flow.ranking_market} · ${flow.ranking_limit}개`
        : (flow.symbols || []).join(", ") || "종목 미지정";
    nodes.push({
      id: `${prefix}-schedule`, title: flow.name || `매수 흐름 ${flowIndex + 1}`, meta: "한국 시각 · 종료 시각 제외", tone: "schedule", logic: "시간", logicTone: "schedule", width: 460,
      fields: takeFlow("name", "buy_start_time", "buy_end_time", "buy_scan_seconds"), flowKind: "buy", flowId: flow.id,
      summary: [`${flow.buy_start_time}–${flow.buy_end_time}`, `${flow.buy_scan_seconds}초마다`],
      description: "이 시간대의 종목 조회와 매수 판단은 다른 매수 흐름과 독립적으로 실행됩니다.",
    }, {
      id: `${prefix}-target`, title: targetLabels[flow.target_source] || "종목 데이터", meta: flow.target_source === "HTS_CONDITION"
        ? (conditions.loading ? "조건식 조회 중" : conditions.error ? "조회 실패" : `${conditions.conditions.length}개 조건식`) : "이 흐름에서 조회",
      tone: "target", logic: "대상", logicTone: "universe", width: 460, fields: targetFields,
      summary: [targetSummary], actions: ["조회 결과를 이 흐름의 조건에 전달"], vertical: true,
    });
    collectComposerRuleDefinitions(nodes, "buy", flow.id, blockRuleTree("buy", flow.id));
  });
  sellFlows.forEach((flow, flowIndex) => {
    const prefix = blockFlowNodePrefix("sell", flow.id);
    const flowFields = new Map(blockSellFlowFields(flow).map(field => [field.settingKey, field]));
    const takeFlow = (...keys) => keys.map(key => flowFields.get(key)).filter(Boolean);
    const ruleConditions = blockRuleConditionNodes(blockRuleTree("sell", flow.id));
    const hasLiveConditions = ruleConditions.some(isLiveProfitCondition);
    const hasBarConditions = ruleConditions.some(hasBarMetricCondition);
    const scheduleMeta = hasLiveConditions && hasBarConditions
      ? "실시간 · 봉 시점"
      : hasLiveConditions ? "실시간" : "봉 시점";
    const scheduleActions = [
      ...(hasLiveConditions ? ["수익률 · 실시간 현재가"] : []),
      ...(hasBarConditions ? ["차트 값 · 각 조건의 봉 설정"] : []),
      `${flow.sell_scan_seconds}초 보완 판단`,
    ];
    nodes.push({
      id: `${prefix}-schedule`, title: flow.name || `매도 흐름 ${flowIndex + 1}`,
      meta: "한국 시각 · 종료 시각 제외", tone: "schedule", logic: "시간", logicTone: "schedule", width: 460,
      fields: takeFlow("name", "sell_start_time", "sell_end_time", "sell_scan_seconds"),
      flowKind: "sell", flowId: flow.id,
      summary: [`${flow.sell_start_time}–${flow.sell_end_time}`, scheduleMeta],
      description: "이 시간대에는 이 매도 계층만 독립적으로 판단합니다. 조건이나 묶음에 붙은 행동이 통과한 경로에서 실행됩니다.",
      actions: scheduleActions,
    });
    collectComposerRuleDefinitions(nodes, "sell", flow.id, blockRuleTree("sell", flow.id));
  });
  const nodeIds = new Set(nodes.map(node => node.id));
  if (state.blockSelectedNode == null || (state.blockSelectedNode && !nodeIds.has(state.blockSelectedNode))) {
    state.blockSelectedNode = false;
  }
  const definitions = new Map(nodes.map(node => [node.id, node]));
  const tree = document.createElement("div");
  tree.className = "composer-fixed-tree";
  tree.setAttribute("role", "tree");
  tree.setAttribute("aria-label", "전략 실행 계층");
  tree.append(renderComposerTreeBranch(buildComposerTreeModel(settings), definitions, running));
  world.append(tree);
  elements.blockCanvas.append(world);
  bindBlockCanvasPan(world);
  applyBlockViewport(world);
  state.blockGraphRedraw = null;
  window.requestAnimationFrame(() => {
    world.style.width = `${Math.max(1120, tree.scrollWidth + tree.offsetLeft + 180)}px`;
    world.style.height = `${Math.max(1100, tree.scrollHeight + tree.offsetTop + 180)}px`;
  });
  const selected = nodes.find(node => node.id === state.blockSelectedNode);
  const inspector = createComposerInspector(selected, running, { buy: flows.length, sell: sellFlows.length });
  if (inspector) {
    elements.blockCanvas.append(inspector);
    restoreComposerInspectorView(inspectorSnapshot);
  }
}

function renderStrategyBlocks() {
  if (!elements.blockCanvas || !flushBlockFieldEdits(false)) return;
  const source = elements.editor.value;
  const parsed = currentBlockModel();
  const running = Boolean(selectedState()?.running);
  const managed = parsed.template === "condition-bollinger-v1" && Boolean(state.blockPackage);
  state.blockRenderedRunning = managed ? running : null;
  const inspectorSnapshot = managed ? captureComposerInspectorView() : null;
  const zoomControls = elements.strategyBlocks.querySelector(".block-zoom-controls");
  if (zoomControls) zoomControls.hidden = !managed;
  const footer = elements.strategyBlocks.querySelector(".block-editor-status");
  if (footer) footer.hidden = !managed;
  elements.blockCanvas.classList.remove("schedule-mode", "graph-mode", "composer-mode", "panning");
  elements.strategyBlocks.classList.remove("managed-schedule", "managed-composer");
  elements.strategyBlocks.classList.toggle("managed-composer", managed);
  elements.blockCanvas.setAttribute("aria-label", managed ? "전체 구성을 이동하고 확대할 수 있는 전략 트리 캔버스" : "블록 전략 캔버스");
  if (managed) {
    elements.blockCanvas.classList.add("graph-mode", "composer-mode");
    elements.blockCanvas.scrollTop = 0;
    elements.blockCanvas.scrollLeft = 0;
  } else {
    elements.blockCanvas.style.backgroundPosition = "";
    elements.blockCanvas.style.backgroundSize = "";
    state.blockGraphRedraw = null;
  }
  if (elements.blockAddFlow) {
    elements.blockAddFlow.hidden = parsed.template !== "condition-bollinger-v1" || !state.blockPackage;
    elements.blockAddFlow.disabled = running || blockBuyFlows().length >= 8;
  }
  elements.blockCanvas.replaceChildren();

  if (parsed.template === "condition-bollinger-v1" && state.blockPackage) {
    const flows = blockBuyFlows();
    const sellFlows = blockSellFlows();
    const buyConditions = flows.reduce(
      (count, flow) => count + blockRuleConditionNodes(blockRuleTree("buy", flow.id)).length, 0,
    );
    const sellConditions = sellFlows.reduce(
      (count, flow) => count + blockRuleConditionNodes(blockRuleTree("sell", flow.id)).length, 0,
    );
    elements.blockSummary.textContent = `매수 흐름 ${flows.length}개 · 매수 조건 ${buyConditions}개 · 매도 흐름 ${sellFlows.length}개 · 매도 조건 ${sellConditions}개 · 예산·보유 공유`;
  } else {
    elements.blockSummary.textContent = `${parsed.fields.length}개 설정 · ${parsed.schedules.length}개 일정 · ${parsed.handlers.length}개 실행 흐름`;
  }
  state.blockSource = source;

  if (!parsed.fields.length && !parsed.schedules.length && !parsed.handlers.length) {
    const empty = document.createElement("div");
    empty.className = "block-empty";
    empty.textContent = "표시할 수 있는 블록이 없습니다.\n새 블록 전략을 만들거나 제공되는 설정 블록을 추가해 주세요.";
    elements.blockCanvas.append(empty);
    return;
  }

  if (parsed.template === "condition-bollinger-v1" && state.blockPackage) {
    renderConditionBollingerBlocks(parsed, running, inspectorSnapshot);
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

function syncBlockEditorFocus() {
  document.body.classList.toggle(
    "block-editor-focus",
    state.view === "strategy" && state.strategyType === "block" && state.strategyTab === "blocks",
  );
}

function setStrategyTab(tab) {
  const allowed = state.strategyType === "block"
    ? new Set(["overview", "blocks"])
    : new Set(["overview", "code"]);
  if (!allowed.has(tab)) tab = "overview";
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
  syncBlockEditorFocus();
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
  syncBlockEditorFocus();
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
  if (
    selected
    && state.strategyType === "block"
    && state.strategyTab === "blocks"
    && state.blockRenderedRunning !== running
  ) {
    settleBlockFieldEditsForRerender();
    renderStrategyBlocks();
  }
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
    button.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      // Open on pointer-down so a periodic list refresh cannot replace the
      // button between pointer-down and click and swallow the user's choice.
      openStrategy(strategy.name).catch(error => notify(error.message, true));
    });
    button.addEventListener("click", event => {
      if (event.detail !== 0) return;
      openStrategy(strategy.name).catch(error => notify(error.message, true));
    });
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

async function refreshStrategies({ force = false } = {}) {
  const strategies = await api("/api/strategies");
  const signature = JSON.stringify(strategies);
  state.strategies = strategies;
  if (force || signature !== state.strategyListSignature) {
    state.strategyListSignature = signature;
    renderStrategies();
  }
}

async function openStrategy(name) {
  if (name === state.selected && state.view === "strategy") {
    if (state.openingStrategy && state.openingStrategy !== name) {
      state.strategyOpenToken += 1;
      state.openingStrategy = null;
    }
    state.strategyBalanceTab = "balance";
    setStrategyTab("overview");
    return;
  }
  if (name === state.openingStrategy) return;
  if (state.dirty && !window.confirm("저장하지 않은 변경 사항이 있습니다. 저장하지 않고 다른 전략을 열까요?")) return;
  const openToken = ++state.strategyOpenToken;
  state.blockSelectedNode = null;
  state.openingStrategy = name;
  try {
    const payload = await api(`/api/strategies/${encodeURIComponent(name)}`);
    if (openToken !== state.strategyOpenToken) return;
    const strategyType = payload.strategy_type === "block" ? "block" : "script";
    if (strategyType === "block") {
      if (!await loadBlockEditorData(name, openToken)) return;
    } else {
      state.blockPackage = null;
      resetConditionFormulaDrafts();
      state.blockExpressionSelections = new WeakMap();
      state.blockConditions = [];
      state.blockConditionKey = null;
      state.blockConditionError = null;
      state.blockGraphPlan = null;
    }
    if (openToken !== state.strategyOpenToken) return;
    state.selected = name;
    state.strategyType = strategyType;
    state.strategySummary = null;
    state.strategyBalanceTab = "balance";
    showWorkspaceView("strategy");
    elements.fileName.textContent = displayStrategyName(name);
    configureStrategyTabs();
    elements.editor.value = payload.source;
    state.blockSource = null;
    setDirty(false);
    updateLineNumbers();
    setStrategyTab("overview");
    state.logCursor = state.clearedLogCursors[name] || 0;
    resetActivity();
    renderStrategies();
    if (strategyType === "block") {
      try {
        await refreshBlockConditions(openToken);
      } catch (error) {
        if (openToken === state.strategyOpenToken) {
          state.blockConditionError = error.message;
          if (state.strategyTab === "blocks") renderStrategyBlocks();
        }
      }
    }
    if (openToken !== state.strategyOpenToken) return;
    await refreshStrategyPermission();
    await Promise.all([refreshStrategySummary(), refreshLogs(), refreshOrders()]);
  } finally {
    if (openToken === state.strategyOpenToken) state.openingStrategy = null;
  }
}

async function openDashboard() {
  if (state.view === "dashboard") {
    await refreshDashboard(true);
    return;
  }
  if (state.dirty && !window.confirm("저장하지 않은 변경 사항이 있습니다. 저장하지 않고 대시보드로 이동할까요?")) return;
  state.strategyOpenToken += 1;
  state.openingStrategy = null;
  state.selected = null;
  state.strategyType = "script";
  state.strategyPermission = null;
  state.strategySummary = null;
  state.blockPackage = null;
  resetConditionFormulaDrafts();
  state.blockExpressionSelections = new WeakMap();
  state.blockConditions = [];
  state.blockConditionKey = null;
  state.blockConditionError = null;
  setDirty(false);
  showWorkspaceView("dashboard");
  resetActivity("전략을 선택하면 실행 기록이 표시됩니다.");
  renderStrategies();
  renderDashboard();
  await refreshDashboard();
}

async function refreshStrategyPermission() {
  const previousSignature = JSON.stringify(state.strategyPermission);
  if (!state.selected) {
    state.strategyPermission = null;
    if (previousSignature !== JSON.stringify(state.strategyPermission)) updateControls();
    return;
  }
  try {
    state.strategyPermission = await api(
      `/api/strategies/${encodeURIComponent(state.selected)}/order-permission`
    );
  } catch (_) {
    state.strategyPermission = null;
  }
  if (previousSignature !== JSON.stringify(state.strategyPermission)) updateControls();
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
  flushBlockFieldEdits();
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
    resetConditionFormulaDrafts();
    state.blockExpressionSelections = new WeakMap();
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
  flushBlockFieldEdits();
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
  forgetBlockLayout(deletedName);
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
    if (status.database_recovery) {
      const recoveryKey = `${status.instance_id}:${status.database_recovery.backup_path}`;
      elements.runtime.hidden = false;
      elements.runtime.textContent = "실행 기록 DB 자동 복구 · 주문 허용 확인 필요";
      elements.runtime.title = status.database_recovery.backup_path || "";
      if (state.databaseRecoveryKey !== recoveryKey) {
        state.databaseRecoveryKey = recoveryKey;
        notify(status.database_recovery.message, true);
      }
    } else if (status.unresolved_orders) {
      elements.runtime.hidden = false;
      elements.runtime.textContent = `확인 필요 주문 ${status.unresolved_orders}`;
      elements.runtime.title = "";
    } else {
      elements.runtime.hidden = true;
      elements.runtime.textContent = "";
      elements.runtime.title = "";
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

function positiveIntegerOrNull(selector) {
  const value = positiveNumberOrNull(selector);
  if (value == null) return null;
  if (!/^\d+$/.test(value) || Number(value) < 1) {
    throw new Error("횟수와 주문 건수 한도는 1 이상의 정수로 입력하세요.");
  }
  return Number(value);
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
  document.querySelector("#account-max-daily-buy-count").value = limits.max_daily_buy_count ?? "";
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
      limits.max_daily_buy_amount && `일일 누적매수액 ${Number(limits.max_daily_buy_amount).toLocaleString()}원`,
      limits.max_daily_buy_count && `일일 누적매수 ${limits.max_daily_buy_count}회`,
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
  flushBlockFieldEdits();
  if (state.dirty) await saveStrategy();
  const payload = state.blockPackage || await api(
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
  const importedName = displayStrategyName(result.name);
  notify(result.copied
    ? `같은 전략과 구분하기 위해 ${importedName}(으)로 복사했습니다. 연결 별칭을 확인하세요.`
    : `${importedName} 블록 전략을 가져왔습니다. 연결 별칭을 확인한 뒤 저장하세요.`);
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
elements.chartButton.addEventListener("click", () => {
  window.tradeRouterChart?.open();
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
elements.blockAddFlow?.addEventListener("click", addBlockBuyFlow);
elements.blockAutoLayout?.addEventListener("click", resetBlockGraphLayout);
elements.newStrategyModal.addEventListener("click", event => {
  if (event.target === elements.newStrategyModal) closeNewStrategyModal();
});
document.querySelectorAll("[data-strategy-tab]").forEach(button => {
  button.addEventListener("click", () => setStrategyTab(button.dataset.strategyTab));
});
document.querySelectorAll("[data-balance-tab]").forEach(button => {
  button.addEventListener("click", () => {
    state.strategyBalanceTab = button.dataset.balanceTab;
    renderStrategyBalanceContent();
  });
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
    const result = await fetchUpdateResult();
    renderUpdateResult(result);
    notify(result.message);
  } catch (error) {
    status.textContent = error.message;
    notify(error.message, true);
    delete button.dataset.ready;
    button.textContent = "업데이트 확인";
  }
  finally { button.disabled = false; }
});
elements.updateAvailable.addEventListener("click", () => {
  openSettings("general").catch(error => notify(error.message, true));
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
document.querySelector("#account-balance-close").addEventListener("click", closeAccountBalance);
elements.accountBalanceModal.addEventListener("click", event => {
  if (event.target === elements.accountBalanceModal) closeAccountBalance();
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
      max_daily_buy_count: positiveIntegerOrNull("#account-max-daily-buy-count"),
      max_exposure: positiveNumberOrNull("#account-max-exposure"),
      max_open_orders: positiveIntegerOrNull("#account-max-open-orders"),
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
  if (event.key === "Escape" && !elements.accountBalanceModal.hidden) {
    closeAccountBalance();
    return;
  }
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
  if (event.key === "Escape" && elements.blockCanvas.querySelector(".composer-inspector")) {
    event.preventDefault();
    closeComposerInspector();
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
  void checkForStartupUpdate();
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
