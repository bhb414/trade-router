const state = {
  selected: null,
  dirty: false,
  strategies: [],
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
};

async function api(path, options = {}) {
  const response = await fetch(path, {
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
    elements.serverDot.classList.add("online");
    elements.runtime.textContent = `실행 ${status.running} · v${status.version}`;
    elements.connections.textContent = `증권사 연결 ${status.broker_connections}`;
    elements.workspace.textContent = status.workspace;
  } catch (_) {
    elements.serverDot.classList.remove("online");
    elements.runtime.textContent = "서버 연결 끊김";
  }
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

document.addEventListener("keydown", event => {
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
