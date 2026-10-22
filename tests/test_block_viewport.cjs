/* Run with node --test tests/test_block_viewport.cjs. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../apps/studio/src/trade_router_studio/static/app.js"), "utf8");
function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, name);
  const end = source.indexOf("\nfunction ", start + 1);
  return source.slice(start, end < 0 ? undefined : end);
}
const plain = value => JSON.parse(JSON.stringify(value));
function fixture({ viewport = { x: 1047.44, y: -195.592, zoom: .2 }, width = 1016, height = 620,
  rows = [{ x: 44, y: 38, width: 762, height: 70 }, { x: 80, y: 1400, width: 680, height: 80 }],
  resizeObserver = true } = {}) {
  const state = {
    view: "strategy", strategyTab: "blocks", strategyType: "block", selected: "example.py", strategyOpenToken: 4,
    dirty: false, strategyPermission: { available: true, armed: false }, blockSource: "unchanged source",
    blockPackage: { layout: { nodes: {}, viewport: { ...viewport } }, settings: { buy_enabled: true, sell_flows: [{ id: "sell-a" }] } },
  };
  const classes = new Set();
  const frames = new Map();
  const observers = [];
  const listeners = new Map();
  const persisted = [];
  let nextFrame = 0;
  const world = { style: {}, contains: node => node === tree, querySelector: () => tree };
  const canvas = {
    clientWidth: width, clientHeight: height, hiddenParent: false, world,
    style: {}, classList: { contains: name => classes.has(name) },
    contains: node => node === canvas.world || node === tree,
    closest: () => canvas.hiddenParent ? {} : null,
    querySelector: selector => selector === ".block-canvas-world" ? canvas.world : null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 150, top: 100, width: canvas.clientWidth, height: canvas.clientHeight,
      right: 150 + canvas.clientWidth, bottom: 100 + canvas.clientHeight }),
  };
  const rowElements = rows.map(row => ({ getBoundingClientRect: () => {
    const view = state.blockPackage.layout.viewport;
    const left = 150 + view.x + row.x * view.zoom;
    const top = 100 + view.y + row.y * view.zoom;
    return { left, top, width: row.width * view.zoom, height: row.height * view.zoom,
      right: left + row.width * view.zoom, bottom: top + row.height * view.zoom };
  } }));
  const tree = { offsetLeft: 0, offsetTop: 0, offsetWidth: 850, scrollWidth: 850, offsetHeight: 1600, scrollHeight: 1600,
    querySelectorAll: () => rowElements };
  const elements = {
    blockCanvas: canvas, blockZoomLabel: {}, strategyOverview: {}, strategyBlocks: {}, strategyCode: {},
    editor: { value: "unchanged source" }, dashboardView: {}, strategyView: {}, dashboardButton: { classList: { toggle() {} } },
  };
  const window = {
    requestAnimationFrame: callback => { frames.set(++nextFrame, callback); return nextFrame; },
    cancelAnimationFrame: id => frames.delete(id),
    addEventListener: (type, callback) => listeners.set(type, callback),
    removeEventListener: (type, callback) => { if (listeners.get(type) === callback) listeners.delete(type); },
  };
  if (resizeObserver) window.ResizeObserver = class {
    constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
    observe(node) { this.target = node; }
    disconnect() { this.disconnected = true; }
  };
  const context = vm.createContext({
    state, elements, window, document: { querySelectorAll: () => [] },
    BLOCK_ZOOM_MIN: .2, BLOCK_ZOOM_MAX: 2, BLOCK_VIEWPORT_COORD_LIMIT: 20000,
    persistBlockLayout: () => persisted.push(plain(state.blockPackage.layout)),
    syncBlockEditorFocus() {}, refreshStrategySummary: () => Promise.resolve(),
    renderStrategyBlocks: () => { throw new Error("Recovery must not regenerate conditions or dirty source"); },
  });
  ["clampBlockNumber", "ensureBlockLayout", "constrainBlockViewport", "applyBlockViewport", "fitBlockGraph",
    "visibleBlockCanvasBounds", "recoverEmptyBlockViewport", "currentBlockViewportRecovery",
    "scheduleBlockViewportRecovery", "bindBlockViewportRecovery", "setStrategyTab", "showWorkspaceView"]
    .forEach(name => vm.runInContext(functionSource(name), context));
  const flush = () => { const current = [...frames]; frames.clear(); current.forEach(([, callback]) => callback()); };
  const visibleCount = () => {
    const b = canvas.getBoundingClientRect();
    return rowElements.filter(row => { const r = row.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && r.right > b.left && r.left < b.right && r.bottom > b.top && r.top < b.bottom;
    }).length;
  };
  return { state, context, elements, world, tree, canvas, classes, frames, observers, listeners, persisted, flush, visibleCount, window };
}

test("opening the reported 20% off-screen view restores visible readable blocks and only local view state", () => {
  const h = fixture();
  const settings = h.state.blockPackage.settings;
  const before = plain({ settings, dirty: h.state.dirty, permission: h.state.strategyPermission, source: h.state.blockSource });
  assert.equal(h.visibleCount(), 0);
  h.context.bindBlockViewportRecovery(h.world, h.tree);
  h.flush();
  assert.ok(h.visibleCount() > 0);
  assert.equal(h.state.blockPackage.layout.viewport.zoom, 1);
  assert.equal(h.persisted.length, 1);
  assert.equal(h.state.blockPackage.settings, settings);
  assert.deepEqual(plain({ settings, dirty: h.state.dirty, permission: h.state.strategyPermission, source: h.state.blockSource }), before);
});

test("already-visible views retain their exact pan and zoom, including deep scrolled conditions", () => {
  for (const viewport of [{ x: 24, y: 24, zoom: .2 }, { x: 12, y: -1360, zoom: 1 }, { x: -330, y: 20, zoom: 1.4 }]) {
    const h = fixture({ viewport });
    assert.ok(h.visibleCount() > 0);
    h.context.bindBlockViewportRecovery(h.world, h.tree);
    h.flush();
    assert.deepEqual(h.state.blockPackage.layout.viewport, viewport);
    assert.equal(h.persisted.length, 0);
  }
});

test("narrow screens recover at a width-fitting zoom without shrinking a long strategy to fit its entire height", () => {
  for (const width of [390, 640, 850]) {
    const h = fixture({ width, height: 220 });
    h.tree.scrollHeight = 18000;
    h.context.bindBlockViewportRecovery(h.world, h.tree);
    h.flush();
    assert.ok(h.visibleCount() > 0);
    assert.ok(h.state.blockPackage.layout.viewport.zoom > .2);
    assert.ok(h.state.blockPackage.layout.viewport.zoom <= 1);
    const row = h.tree.querySelectorAll()[0].getBoundingClientRect();
    const bounds = h.canvas.getBoundingClientRect();
    assert.ok(row.left >= bounds.left && row.right <= bounds.right);
    assert.ok(row.top >= bounds.top && row.bottom <= bounds.bottom);
  }
});

test("hidden and zero-size canvases never measure-and-save a bad view or fit", () => {
  for (const hidden of ["tab", "workspace", "ancestor", "width", "height"]) {
    const h = fixture();
    if (hidden === "tab") h.state.strategyTab = "overview";
    if (hidden === "workspace") h.state.view = "dashboard";
    if (hidden === "ancestor") h.canvas.hiddenParent = true;
    if (hidden === "width") h.canvas.clientWidth = 0;
    if (hidden === "height") h.canvas.clientHeight = 0;
    const before = plain(h.state.blockPackage.layout.viewport);
    h.context.bindBlockViewportRecovery(h.world, h.tree);
    h.flush();
    h.context.fitBlockGraph();
    assert.deepEqual(h.state.blockPackage.layout.viewport, before, hidden);
    assert.equal(h.persisted.length, 0, hidden);
  }
});

test("returning to the block tab recovers a hidden-rendered tree even when source did not change", () => {
  const h = fixture();
  h.state.strategyTab = "overview";
  h.context.bindBlockViewportRecovery(h.world, h.tree);
  h.flush();
  assert.equal(h.persisted.length, 0);
  h.context.setStrategyTab("blocks");
  h.flush();
  assert.ok(h.visibleCount() > 0);
  assert.equal(h.persisted.length, 1);
});

test("returning from the dashboard recovers the existing block view", () => {
  const h = fixture();
  h.state.view = "dashboard";
  h.context.bindBlockViewportRecovery(h.world, h.tree);
  h.flush();
  h.context.showWorkspaceView("strategy");
  h.flush();
  assert.ok(h.visibleCount() > 0);
  assert.equal(h.persisted.length, 1);
});

test("a canvas resize recovers a view that was visible before the viewport became narrower", () => {
  const h = fixture({ viewport: { x: 800, y: 24, zoom: .5 } });
  h.context.bindBlockViewportRecovery(h.world, h.tree);
  h.flush();
  assert.equal(h.persisted.length, 0);
  h.canvas.clientWidth = 640;
  assert.equal(h.visibleCount(), 0);
  h.observers[0].callback();
  h.flush();
  assert.ok(h.visibleCount() > 0);
  assert.equal(h.persisted.length, 1);
});

test("pointer panning, dragging and active wheel panning are never interrupted by recovery", () => {
  for (const active of ["panning", "pointerdrag", "drag", "wheel"]) {
    const h = fixture();
    if (active === "panning") h.classes.add("panning");
    if (active === "pointerdrag") h.state.blockRulePointerDrag = {};
    if (active === "drag") h.state.blockRuleDrag = {};
    if (active === "wheel") {
      h.canvas._blockViewPersistTimer = 7;
      h.canvas._blockViewPersistLayout = h.state.blockPackage.layout;
    }
    const before = plain(h.state.blockPackage.layout.viewport);
    h.context.bindBlockViewportRecovery(h.world, h.tree);
    h.flush();
    h.observers[0].callback();
    h.flush();
    assert.deepEqual(h.state.blockPackage.layout.viewport, before, active);
    assert.equal(h.persisted.length, 0, active);
  }
});

test("a previous strategy's wheel-persist timer does not suppress recovery of the newly opened strategy", () => {
  const h = fixture();
  h.canvas._blockViewPersistTimer = 7;
  h.canvas._blockViewPersistLayout = { viewport: { x: -2000, y: -900, zoom: .2 } };
  h.context.bindBlockViewportRecovery(h.world, h.tree);
  h.flush();
  assert.ok(h.visibleCount() > 0);
  assert.equal(h.persisted.length, 1);
  assert.equal(h.canvas._blockViewPersistTimer, 7, "The previous strategy's persistence remains owned by that timer");
});

test("a manual view change after scheduling supersedes the queued recovery", () => {
  const h = fixture();
  h.context.bindBlockViewportRecovery(h.world, h.tree);
  h.state.blockPackage.layout.viewport.y -= 100;
  const manual = plain(h.state.blockPackage.layout.viewport);
  h.flush();
  assert.deepEqual(h.state.blockPackage.layout.viewport, manual);
  assert.equal(h.persisted.length, 0);
});

test("stale callbacks cannot recover a newly selected strategy, package or replaced world", () => {
  for (const stale of ["strategy", "package", "open-token", "world"]) {
    const h = fixture();
    h.context.bindBlockViewportRecovery(h.world, h.tree);
    if (stale === "strategy") h.state.selected = "other.py";
    if (stale === "package") h.state.blockPackage = structuredClone(h.state.blockPackage);
    if (stale === "open-token") h.state.strategyOpenToken += 1;
    if (stale === "world") h.canvas.world = {};
    const before = plain(h.state.blockPackage);
    h.flush();
    h.observers[0].callback();
    h.flush();
    assert.deepEqual(h.state.blockPackage, before, stale);
    assert.equal(h.persisted.length, 0, stale);
  }
});

test("rebinding disconnects the old observer and an already-delivered old frame is still harmless", () => {
  const h = fixture();
  h.context.bindBlockViewportRecovery(h.world, h.tree);
  const oldFrame = [...h.frames.values()][0];
  const oldObserver = h.observers[0];
  h.context.bindBlockViewportRecovery(h.world, h.tree);
  assert.equal(oldObserver.disconnected, true);
  oldFrame();
  oldObserver.callback();
  assert.equal(h.persisted.length, 0);
  h.flush();
  assert.equal(h.persisted.length, 1);
});

test("repeated resize notifications are idempotent after recovery", () => {
  const h = fixture();
  h.context.bindBlockViewportRecovery(h.world, h.tree);
  h.observers[0].callback();
  h.observers[0].callback();
  assert.equal(h.frames.size, 1);
  h.flush();
  const recovered = plain(h.state.blockPackage.layout.viewport);
  h.observers[0].callback();
  h.flush();
  assert.deepEqual(h.state.blockPackage.layout.viewport, recovered);
  assert.equal(h.persisted.length, 1);
});

test("browsers without ResizeObserver recover on window resize and remove the fallback listener", () => {
  const h = fixture({ resizeObserver: false });
  h.context.bindBlockViewportRecovery(h.world, h.tree);
  h.flush();
  assert.ok(h.visibleCount() > 0);
  h.state.blockPackage.layout.viewport.x = 3000;
  h.listeners.get("resize")();
  h.flush();
  assert.equal(h.persisted.length, 2);
  h.canvas._blockViewportRecovery.cleanup();
  assert.equal(h.listeners.has("resize"), false);
});

test("no rendered rows or detached content is left untouched instead of hiding a rendering failure", () => {
  for (const absent of ["rows", "tree", "world"]) {
    const h = fixture(absent === "rows" ? { rows: [] } : {});
    if (absent === "tree") h.world.contains = () => false;
    if (absent === "world") h.canvas.contains = () => false;
    h.context.bindBlockViewportRecovery(h.world, h.tree);
    h.flush();
    assert.equal(h.persisted.length, 0, absent);
  }
});

test("explicit fit still fits a visible canvas and tiny unusable canvases do not persist", () => {
  const h = fixture();
  h.context.fitBlockGraph();
  assert.ok(h.visibleCount() > 0);
  assert.equal(h.persisted.length, 1);
  for (const [width, height] of [[90, 600], [800, 92]]) {
    const tiny = fixture({ width, height });
    tiny.context.fitBlockGraph();
    assert.equal(tiny.persisted.length, 0);
  }
});
