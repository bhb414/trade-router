const state = {
  selected: null,
  dirty: false,
  strategies: [],
  providers: [],
  connections: [],
  accounts: [],
  logCursor: 0,
  logTimer: null,
};

const elements = {
  list: document.querySelector("#strategy-list"),
  count: document.querySelector("#strategy-count"),
  editor: document.querySelector("#source-editor"),
  lines: document.querySelector("#line-numbers"),
  fileName: document.querySelector("#file-name"),
  dirty: document.querySelector("#dirty-mark"),
  save: document.querySelector("#save-button"),
  run: document.querySelector("#run-button"),
  stop: document.querySelector("#stop-button"),
  remove: document.querySelector("#delete-button"),
  strategyState: document.querySelector("#strategy-state"),
  log: document.querySelector("#log-output"),
  toast: document.querySelector("#toast"),
  serverDot: document.querySelector("#server-dot"),
  runtime: document.querySelector("#runtime-status"),
  connections: document.querySelector("#connection-status"),
  workspace: document.querySelector("#workspace-path"),
  settingsModal: document.querySelector("#settings-modal"),
  connectionProvider: document.querySelector("#connection-provider"),
  accountConnection: document.querySelector("#account-connection"),
  productCodeField: document.querySelector("#product-code-field"),
};

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
      message = payload.detail || message;
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

function selectedState() {
  return state.strategies.find(item => item.name === state.selected);
}

function updateControls() {
  const selected = Boolean(state.selected);
  const running = Boolean(selectedState()?.running);
  elements.editor.disabled = !selected || running;
  elements.save.disabled = !selected || running;
  elements.remove.disabled = !selected || running;
  elements.run.disabled = !selected || running;
  elements.stop.disabled = !selected || !running;
  elements.strategyState.textContent = !selected
    ? "선택된 전략 없음"
    : running ? "실행 중 · 편집하려면 먼저 중지하세요" : "중지됨 · 편집 가능";
}

function renderStrategies() {
  elements.count.textContent = String(state.strategies.length);
  elements.list.replaceChildren();
  for (const strategy of state.strategies) {
    const button = document.createElement("button");
    button.className = `strategy-item${strategy.name === state.selected ? " active" : ""}${strategy.running ? " running" : ""}`;
    button.type = "button";
    const indicator = document.createElement("span");
    indicator.className = "indicator";
    const text = document.createElement("span");
    const name = document.createElement("span");
    name.className = "strategy-name";
    name.textContent = strategy.name;
    const meta = document.createElement("span");
    meta.className = "strategy-meta";
    meta.textContent = strategy.running
      ? "실행 중"
      : strategy.stop_requested
        ? "사용자 중지"
        : strategy.exit_code == null ? "중지됨" : `종료 코드 ${strategy.exit_code}`;
    text.append(name, meta);
    button.append(indicator, text);
    button.addEventListener("click", () => openStrategy(strategy.name));
    elements.list.append(button);
  }
  updateControls();
}

async function refreshStrategies() {
  state.strategies = await api("/api/strategies");
  renderStrategies();
}

async function openStrategy(name) {
  if (name === state.selected) return;
  if (state.dirty && !window.confirm("저장하지 않은 변경 사항이 있습니다. 저장하지 않고 다른 전략을 열까요?")) return;
  const payload = await api(`/api/strategies/${encodeURIComponent(name)}`);
  state.selected = name;
  elements.fileName.textContent = name;
  elements.editor.value = payload.source;
  setDirty(false);
  updateLineNumbers();
  state.logCursor = 0;
  elements.log.textContent = "전략을 실행하면 여기에 출력됩니다.";
  await refreshStrategies();
  await refreshLogs();
}

async function saveStrategy() {
  if (!state.selected) return;
  await api(`/api/strategies/${encodeURIComponent(state.selected)}`, {
    method: "PUT",
    body: JSON.stringify({ source: elements.editor.value }),
  });
  setDirty(false);
  notify("전략을 저장했습니다.");
  await refreshStrategies();
}

async function runStrategy() {
  if (!state.selected) return;
  if (state.dirty) await saveStrategy();
  await api(`/api/strategies/${encodeURIComponent(state.selected)}/start`, { method: "POST" });
  state.logCursor = 0;
  elements.log.textContent = "";
  notify("전략을 시작했습니다.");
  await Promise.all([refreshStrategies(), refreshStatus()]);
  await refreshLogs();
}

async function stopStrategy() {
  if (!state.selected) return;
  await api(`/api/strategies/${encodeURIComponent(state.selected)}/stop`, { method: "POST" });
  notify("전략을 중지했습니다.");
  await Promise.all([refreshStrategies(), refreshStatus()]);
  await refreshLogs();
}

async function deleteStrategy() {
  if (!state.selected || !window.confirm(`${state.selected} 파일을 삭제할까요?`)) return;
  await api(`/api/strategies/${encodeURIComponent(state.selected)}`, { method: "DELETE" });
  state.selected = null;
  elements.fileName.textContent = "전략을 선택하세요";
  elements.editor.value = "";
  elements.log.textContent = "전략을 실행하면 여기에 출력됩니다.";
  setDirty(false);
  updateLineNumbers();
  await refreshStrategies();
}

async function refreshLogs() {
  if (!state.selected) return;
  const payload = await api(`/api/strategies/${encodeURIComponent(state.selected)}/logs?after=${state.logCursor}`);
  if (payload.lines.length) {
    if (state.logCursor === 0) elements.log.textContent = "";
    elements.log.textContent += `${payload.lines.join("\n")}\n`;
    elements.log.scrollTop = elements.log.scrollHeight;
  }
  state.logCursor = payload.next;
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    elements.serverDot.classList.remove("offline");
    elements.serverDot.classList.add("online");
    elements.runtime.textContent = `실행 ${status.running} · v${status.version}`;
    elements.connections.textContent = `API ${status.registered_apis} · 계좌 ${status.registered_accounts}`;
    elements.workspace.textContent = status.workspace;
  } catch (_) {
    elements.serverDot.classList.remove("online");
    elements.serverDot.classList.add("offline");
    elements.runtime.textContent = "서버 연결 끊김";
    elements.connections.textContent = "서버 확인 불가";
  }
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

function refreshProviderFields() {
  const provider = selectedProvider(elements.connectionProvider.value);
  document.querySelector("#connection-key-label").textContent = provider?.key_label || "App Key";
  document.querySelector("#connection-secret-label").textContent = provider?.secret_label || "App Secret";
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
    const title = document.createElement("strong");
    title.textContent = connection.label;
    const description = document.createElement("span");
    description.textContent = `${provider?.name || connection.provider} · 인증정보 저장됨`;
    information.append(title, description);
    const remove = document.createElement("button");
    remove.className = "registered-remove";
    remove.type = "button";
    remove.textContent = "삭제";
    remove.addEventListener("click", () => removeConnection(connection));
    item.append(information, remove);
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
    information.append(title, description);
    const remove = document.createElement("button");
    remove.className = "registered-remove";
    remove.type = "button";
    remove.textContent = "삭제";
    remove.addEventListener("click", () => removeAccount(account));
    item.append(information, remove);
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
  [state.providers, state.connections, state.accounts] = await Promise.all([
    api("/api/settings/providers"),
    api("/api/settings/connections"),
    api("/api/settings/accounts"),
  ]);
  renderSettings();
}

async function openSettings(tabName = "apis") {
  await loadSettings();
  activateSettingsTab(tabName);
  elements.settingsModal.hidden = false;
  const focusTarget = tabName === "apis" ? elements.connectionProvider : elements.accountConnection;
  window.setTimeout(() => focusTarget.focus(), 0);
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

async function removeAccount(account) {
  if (!window.confirm(`${account.alias} 계좌 등록을 삭제할까요?`)) return;
  try {
    await api(`/api/settings/accounts/${encodeURIComponent(account.id)}`, { method: "DELETE" });
    await Promise.all([loadSettings(), refreshStatus()]);
    notify("계좌 등록을 삭제했습니다.");
  } catch (error) { notify(error.message, true); }
}

function updateLineNumbers() {
  const count = elements.editor.value.split("\n").length;
  elements.lines.textContent = Array.from({ length: count }, (_, index) => index + 1).join("\n");
}

elements.editor.addEventListener("input", () => {
  setDirty(true);
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

document.querySelector("#new-strategy-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.querySelector("#new-strategy-name");
  let name = input.value.trim();
  if (!name) return;
  if (!name.toLowerCase().endsWith(".py")) name += ".py";
  try {
    await api(`/api/strategies/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ source: '"""새 자동매매 전략"""\n\n\ndef main():\n    print("strategy ready", flush=True)\n\n\nif __name__ == "__main__":\n    main()\n' }),
    });
    input.value = "";
    await refreshStrategies();
    await openStrategy(name);
  } catch (error) { notify(error.message, true); }
});

elements.save.addEventListener("click", () => saveStrategy().catch(error => notify(error.message, true)));
elements.run.addEventListener("click", () => runStrategy().catch(error => notify(error.message, true)));
elements.stop.addEventListener("click", () => stopStrategy().catch(error => notify(error.message, true)));
elements.remove.addEventListener("click", () => deleteStrategy().catch(error => notify(error.message, true)));
document.querySelector("#clear-log-button").addEventListener("click", () => { elements.log.textContent = ""; });
document.querySelector("#update-button").addEventListener("click", async () => {
  const button = document.querySelector("#update-button");
  button.disabled = true;
  button.textContent = "확인 중";
  try {
    const result = await api("/api/updates/check");
    notify(result.message);
  } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; button.textContent = "업데이트 확인"; }
});
document.querySelector("#settings-button").addEventListener("click", () => {
  openSettings().catch(error => notify(error.message, true));
});
document.querySelector("#settings-close").addEventListener("click", closeSettings);
elements.settingsModal.addEventListener("click", event => {
  if (event.target === elements.settingsModal) closeSettings();
});
document.querySelectorAll("[data-settings-tab]").forEach(button => {
  button.addEventListener("click", () => activateSettingsTab(button.dataset.settingsTab));
});
elements.connectionProvider.addEventListener("change", refreshProviderFields);
elements.accountConnection.addEventListener("change", refreshAccountFields);

document.querySelector("#connection-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector(".settings-submit");
  submit.disabled = true;
  try {
    await api("/api/settings/connections", {
      method: "POST",
      body: JSON.stringify({
        provider: elements.connectionProvider.value,
        label: document.querySelector("#connection-label").value,
        app_key: document.querySelector("#connection-key").value,
        app_secret: document.querySelector("#connection-secret").value,
      }),
    });
    document.querySelector("#connection-label").value = "";
    document.querySelector("#connection-key").value = "";
    document.querySelector("#connection-secret").value = "";
    await Promise.all([loadSettings(), refreshStatus()]);
    notify("API 인증정보를 안전하게 저장했습니다.");
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
      }),
    });
    document.querySelector("#account-alias").value = "";
    document.querySelector("#account-number").value = "";
    document.querySelector("#account-product-code").value = "";
    await Promise.all([loadSettings(), refreshStatus()]);
    notify("계좌를 등록했습니다.");
  } catch (error) { notify(error.message, true); }
  finally { submit.disabled = !state.connections.length; }
});

document.addEventListener("keydown", event => {
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
  try {
    await Promise.all([refreshStatus(), refreshStrategies()]);
    if (state.strategies.length) await openStrategy(state.strategies[0].name);
  } catch (error) { notify(error.message, true); }
  window.setInterval(() => refreshStatus(), 2000);
  window.setInterval(async () => {
    try {
      await refreshStrategies();
      await refreshLogs();
    } catch (_) {}
  }, 1000);
}

initialize();
