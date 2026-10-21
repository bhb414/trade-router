/* Run with node --test tests/test_rule_block_menu.cjs. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(
  path.join(__dirname, "../apps/studio/src/trade_router_studio/static/app.js"), "utf8",
);
const functionSource = name => {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `Missing ${name}`);
  const end = source.indexOf("\nfunction ", start + 1);
  return source.slice(start, end < 0 ? undefined : end);
};
const condition = (id, action = null) => ({
  kind: "condition", id, condition: { label: id }, action,
});
const action = id => ({ kind: "order", id, side: "SELL", order_type: "LIMIT", limit_price: 12300 });
const group = (id, children, operator = "ANY", ownAction = null) => ({
  kind: "logic", id, children, operator, action: ownAction,
});

class Element {
  constructor(tag, ownerDocument = null) {
    this.tagName = tag;
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this.attributes = {};
    this.bounds = { left: 300, right: 510, width: 210, top: 200, bottom: 230, height: 30 };
    this.offsetWidth = 210;
    this.offsetHeight = 30;
    this.className = "";
    this.classList = {
      contains: value => this.className.split(/\s+/).includes(value),
      add: value => { if (!this.classList.contains(value)) this.className += ` ${value}`; },
      remove: value => { this.className = this.className.split(/\s+/).filter(item => item !== value).join(" "); },
      toggle: (value, enabled) => {
        if (enabled ?? !this.classList.contains(value)) this.classList.add(value);
        else this.classList.remove(value);
      },
    };
  }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  removeEventListener(name, handler) { this.listeners[name] = (this.listeners[name] || []).filter(item => item !== handler); }
  getBoundingClientRect() { return this.bounds; }
  focus() { this.focused = true; if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  blur() { this.focused = false; if (this.ownerDocument?.activeElement === this) this.ownerDocument.activeElement = null; }
  contains(target) { return this === target || this.children.some(child => child.contains?.(target)); }
  querySelector(selector) { return selector === ".composer-inspector" ? this.inspector || null : null; }
  querySelectorAll() { return this.openMenus || []; }
  setPointerCapture(id) { this.capturedPointer = id; }
  hasPointerCapture(id) { return this.capturedPointer === id; }
  releasePointerCapture(id) { if (this.capturedPointer === id) this.capturedPointer = null; }
  dispatch(name, data = {}) {
    const event = { type: name, detail: 1, stopPropagation() {}, preventDefault() {}, ...data };
    for (const handler of [...(this.listeners[name] || [])]) handler(event);
  }
  click() { if (!this.disabled) this.dispatch("click"); }
}

function harness(root, kind = "sell") {
  const notices = [];
  let commits = 0;
  const document = new Element("document");
  document.createElement = tag => new Element(tag, document);
  const canvas = new Element("canvas", document);
  canvas.bounds = { left: 240, right: 1260, width: 1020, top: 100, bottom: 690, height: 590 };
  const context = vm.createContext({
    structuredClone,
    document,
    window: { innerHeight: 720, innerWidth: 1280 },
    elements: { blockCanvas: canvas },
    state: {
      conditionGroupSerial: 0,
      blockPackage: { settings: { [`${kind}_rule_tree`]: root } },
    },
    selectedState: () => ({ running: false }),
    blockRuleTree: () => root,
    blockFlowNodePrefix: (side, flowId) => `${side}-${flowId || "default"}`,
    newBlockTradeCondition: () => ({ left: "close", operator: "gt", right: "constant" }),
    notify: message => notices.push(message),
    commitBlockRuleMutation: () => { commits += 1; },
  });
  [
    "walkBlockRule", "blockRuleNodeCount", "blockRuleConditionNodes", "findBlockRuleNode",
    "blockRuleContainsAction", "blockRuleNodePath", "blockRuleActionConflict",
    "nextBlockRuleId", "newBlockOrderAction", "newBlockRuleCondition", "newBlockRuleLogic",
    "addBlockRuleChild", "addBlockRuleAction", "ruleComposerNodeId", "ruleComposerActionId",
    "buildComposerRuleBranch", "createComposerRuleAddMenu", "positionComposerRuleConnectors",
    "createComposerRuleInsertion", "createComposerLogicConnector", "bindComposerRuleDropTarget",
  ].forEach(name => vm.runInContext(functionSource(name), context));
  const model = () => context.buildComposerRuleBranch(kind, null, root, true);
  const entries = [];
  const visit = entry => { entries.push(entry); (entry.children || []).forEach(visit); };
  const entryFor = id => {
    entries.length = 0;
    visit(model());
    return entries.find(entry => entry.kind === "rule-add" && entry.ownerId === id);
  };
  const menuFor = (id, running = false) => {
    const toolbar = context.createComposerRuleAddMenu(entryFor(id), running);
    const menu = toolbar.children[0];
    const [summary, options] = menu.children;
    return { toolbar, menu, summary, options, buttons: options.children };
  };
  return { context, notices, document, canvas, model, entryFor, menuFor, commits: () => commits };
}

test("rendering exposes distinct condition and group scopes without modifying saved rules", () => {
  const root = group("root", [condition("A", action("leaf")), condition("B")], "ANY", action("fallback"));
  const saved = JSON.stringify(root);
  const h = harness(root);
  const model = h.model();
  const a = model.children.find(entry => entry.ruleNodeId === "A");
  const leafAdd = h.entryFor("A");
  assert.equal(leafAdd.parentId, "root");
  assert.equal(leafAdd.afterId, "A");
  assert.equal(leafAdd.ownerKind, "condition");
  assert.equal(h.entryFor("root").ownerKind, "logic");
  assert.equal(a.children.find(entry => entry.kind === "then").ownerId, "A");
  assert.equal(model.children.find(entry => entry.kind === "then").ownerId, "root");
  assert.equal(JSON.stringify(root), saved);
});

test("a condition-local addition inserts its sibling immediately after that condition", () => {
  const a = condition("A", action("a"));
  const b = condition("B");
  const root = group("root", [a, b], "ANY", action("fallback"));
  const h = harness(root);
  h.menuFor("A").buttons[0].click();
  assert.equal(root.children.length, 3);
  assert.equal(root.children[0], a);
  assert.equal(root.children[2], b);
  assert.equal(root.children[0].action.id, "a");
  assert.equal(root.action.id, "fallback");
  assert.equal(root.children[1].kind, "condition");
  assert.equal(h.commits(), 1);
});

test("every level exposes one insertion before, between and after its existing blocks", () => {
  const inner = group("inner", [condition("X"), condition("Y")]);
  const root = group("root", [condition("A"), inner, condition("B")]);
  const h = harness(root);
  const saved = JSON.stringify(root);
  const model = h.model();
  const slots = model.children.filter(entry => entry.kind === "rule-add");
  assert.deepEqual(Array.from(slots, entry => [entry.afterId, entry.beforeId]), [
    [null, "A"], ["A", "inner"], ["inner", "B"], ["B", null],
  ]);
  const nested = model.children.find(entry => entry.ruleNodeId === "inner");
  assert.deepEqual(Array.from(nested.children.filter(entry => entry.kind === "rule-add"), entry => [entry.afterId, entry.beforeId]), [
    [null, "X"], ["X", "Y"], ["Y", null],
  ]);
  assert.equal(model.children.find(entry => entry.ruleNodeId === "A").children.length, 0, "No hidden per-condition footer shifts the connector");
  assert.equal(JSON.stringify(root), saved);
});

test("each rendered insertion is the exact drag target, including nested and empty levels", () => {
  for (const root of [group("root", [condition("A"), group("inner", [condition("X")]), condition("B")]), group("root", [])]) {
    const h = harness(root);
    const visit = entry => {
      if (entry.kind === "rule-add") {
        const slot = h.context.createComposerRuleInsertion(entry, false);
        const { reference, mode, disabled } = slot._composerRuleDropTarget;
        assert.equal(reference.nodeId, entry.beforeId || entry.afterId || entry.parentId);
        assert.equal(mode, entry.beforeId ? "before" : entry.afterId ? "after" : "inside");
        assert.equal(slot.dataset.parentRuleId, entry.parentId);
        assert.equal(disabled, false);
        assert.equal(h.context.createComposerRuleInsertion(entry, true)._composerRuleDropTarget.disabled, true);
      }
      (entry.children || []).forEach(visit);
    };
    visit(h.model());
  }
});

test("first, middle and end menus insert at their exact boundary without changing sibling rules", () => {
  for (const boundary of [0, 1, 2]) {
    const a = condition("A", action("a"));
    const b = condition("B");
    const root = group("root", [a, b], "ANY", action("fallback"));
    const h = harness(root);
    const slot = h.model().children.filter(entry => entry.kind === "rule-add")[boundary];
    const toolbar = h.context.createComposerRuleAddMenu(slot, false);
    toolbar.children[0].children[1].children[0].click();
    assert.equal(root.children[boundary].kind, "condition");
    assert.notEqual(root.children[boundary], a);
    assert.notEqual(root.children[boundary], b);
    assert.deepEqual(root.children.filter(item => item === a || item === b), [a, b]);
    assert.equal(root.operator, "ANY");
    assert.equal(a.action.id, "a");
    assert.equal(root.action.id, "fallback");
    assert.equal(h.commits(), 1);
  }
});

test("the boundary after a nested level inserts beside it and attaches an action to that level", () => {
  const inner = group("inner", [condition("A")]);
  const b = condition("B");
  const root = group("root", [inner, b]);
  const h = harness(root);
  const slot = h.model().children.find(entry => entry.kind === "rule-add" && entry.afterId === "inner");
  const toolbar = h.context.createComposerRuleAddMenu(slot, false);
  const buttons = toolbar.children[0].children[1].children;
  assert.match(buttons[2].textContent, /앞 계층 통과 시/);
  buttons[2].click();
  assert.equal(inner.action.side, "SELL");
  assert.equal(root.action, null);
  buttons[1].click();
  assert.equal(root.children[0], inner);
  assert.equal(root.children[1].kind, "logic");
  assert.equal(root.children[2], b);
  assert.equal(inner.children.length, 1);
});

test("stale first or nonadjacent middle anchors cannot insert in the wrong location", () => {
  const root = group("root", [condition("A"), condition("B"), condition("C")]);
  const h = harness(root);
  const saved = JSON.stringify(root);
  h.context.addBlockRuleChild("sell", null, "root", "condition", "ALL", null, "missing");
  h.context.addBlockRuleChild("sell", null, "root", "condition", "ALL", "A", "C");
  assert.equal(JSON.stringify(root), saved);
  assert.equal(h.commits(), 0);
  assert.equal(h.notices.length, 2);
});

test("connector text stays halfway between actual cards including attached actions and zoom", () => {
  const h = harness(group("root", []));
  for (const scale of [.5, 1, 2]) {
    const card = (top, bottom) => ({ getBoundingClientRect: () => ({ top: top * scale, bottom: bottom * scale }) });
    const button = { style: {} };
    const slot = {
      offsetHeight: 36,
      getBoundingClientRect: () => ({ top: 180 * scale, height: 36 * scale }),
      previousElementSibling: { querySelector: () => card(20, 100), querySelectorAll: () => [] },
      nextElementSibling: { querySelector: () => card(216, 290), querySelectorAll: () => [] },
      querySelector: () => button,
    };
    h.context.positionComposerRuleConnectors({ querySelectorAll: selector => selector === ".composer-rule-insertion-between" ? [slot] : [] });
    assert.equal(button.style.top, "-22px", "An attached action occupies the gap, but must not push AND/OR below its midpoint");
    slot.previousElementSibling = { querySelector: () => null, querySelectorAll: () => [card(10, 40), card(60, 120)] };
    h.context.positionComposerRuleConnectors({ querySelectorAll: selector => selector === ".composer-rule-insertion-between" ? [slot] : [] });
    assert.equal(button.style.top, "-12px", "Nested levels use their nearest visible condition card");
  }
});

test("a first-position addition stays inside its selected level and nested additions retain existing identities", () => {
  const a = condition("A");
  const inner = group("inner", [a]);
  const b = condition("B");
  const root = group("root", [inner, b]);
  const h = harness(root);
  h.menuFor("inner").buttons[0].click();
  assert.equal(inner.children.length, 2);
  assert.equal(inner.children[1], a);
  h.menuFor("A").buttons[1].click();
  assert.equal(inner.children[2].kind, "logic");
  assert.equal(inner.children[2].children[0].kind, "condition");
  assert.equal(root.children[0], inner);
  assert.equal(root.children[1], b);
  assert.equal(h.commits(), 2);
});

test("stale insertion targets and child/depth limits reject additions without changing the tree", () => {
  const root = group("root", [condition("A")]);
  const h = harness(root);
  const before = JSON.stringify(root);
  h.context.addBlockRuleChild("sell", null, "root", "condition", "ALL", "missing");
  assert.equal(JSON.stringify(root), before);
  assert.equal(h.commits(), 0);
  assert.match(h.notices[0], /위치/);
  const full = group("full", Array.from({ length: 20 }, (_, i) => condition(`C${i}`)));
  const fullHarness = harness(full);
  fullHarness.context.addBlockRuleChild("sell", null, "full", "condition");
  assert.equal(full.children.length, 20);
  assert.equal(fullHarness.commits(), 0);
  let deep = group("deep", [condition("last")]);
  for (let i = 0; i < 7; i += 1) deep = group(`outer-${i}`, [deep]);
  const deepHarness = harness(deep);
  const deepBefore = JSON.stringify(deep);
  deepHarness.context.addBlockRuleChild("sell", null, "deep", "logic");
  assert.equal(JSON.stringify(deep), deepBefore);
  assert.equal(deepHarness.commits(), 0);
  assert.match(deepHarness.notices[0], /8단계/);
});

test("order menu attaches to its condition, while group order remains a separate fallback", () => {
  const a = condition("A");
  const root = group("root", [a, condition("B")]);
  const h = harness(root);
  h.menuFor("A").buttons[2].click();
  assert.equal(a.action.side, "SELL");
  assert.equal(root.action, null);
  const leafOrder = a.action;
  h.menuFor("root").buttons[2].click();
  assert.equal(root.action.side, "SELL");
  assert.notEqual(root.action.id, leafOrder.id);
  assert.equal(a.action, leafOrder);
  assert.equal(h.commits(), 2);
});

test("AND sibling action conflicts, empty sell groups and existing orders are unavailable and guarded", () => {
  const existingOrder = action("keep");
  const a = condition("A", existingOrder);
  const b = condition("B");
  const root = group("root", [a, b], "ALL");
  const h = harness(root);
  const existing = h.menuFor("A").buttons[2];
  assert.equal(existing.disabled, true);
  assert.match(existing.textContent, /이미 추가됨/);
  assert.equal(h.menuFor("B").buttons[2].disabled, true);
  h.context.addBlockRuleAction("sell", null, "B");
  h.context.addBlockRuleAction("sell", null, "A");
  assert.equal(b.action, null);
  assert.equal(a.action, existingOrder);
  assert.equal(h.commits(), 0);
  assert.match(h.notices[0], /AND/);
  assert.equal(h.menuFor("root").buttons[2].disabled, false);
  const empty = group("empty", []);
  const emptyHarness = harness(empty);
  assert.equal(emptyHarness.menuFor("empty").buttons[2].disabled, true);
  emptyHarness.context.addBlockRuleAction("sell", null, "empty");
  assert.equal(empty.action, null);
  assert.equal(emptyHarness.commits(), 0);
});

test("buy and sell menus match their flow and running guards prevent all mutations", () => {
  const root = group("root", [condition("A")]);
  const h = harness(root, "buy");
  const leaf = h.menuFor("A");
  assert.equal(leaf.buttons[2].textContent, "매수 주문 · 앞 조건 통과 시");
  assert.equal(leaf.toolbar.children.length, 2, "Last insertion keeps settings without expanding a separate drop area");
  assert.equal(h.menuFor("root").toolbar.children.length, 1);
  assert.ok(h.menuFor("A", true).buttons.every(button => button.disabled));
  const before = JSON.stringify(root);
  h.context.selectedState = () => ({ running: true });
  h.context.addBlockRuleAction("buy", null, "A");
  h.context.addBlockRuleChild("buy", null, "root", "condition");
  assert.equal(JSON.stringify(root), before);
  assert.equal(h.commits(), 0);
});

test("menu dismisses with Escape or outside document pointer and installs one persistent listener", () => {
  const h = harness(group("root", [condition("A"), condition("B")]));
  const first = h.menuFor("A");
  const second = h.menuFor("B");
  assert.equal(h.document.listeners.pointerdown.length, 1);
  assert.equal(h.canvas.listeners.pointerdown, undefined);
  first.menu.open = true;
  first.menu.dispatch("keydown", { key: "Escape" });
  assert.equal(first.menu.open, false);
  assert.equal(first.summary.focused, true);
  first.menu.open = true;
  second.menu.open = true;
  h.canvas.openMenus = [first.menu, second.menu];
  h.document.dispatch("pointerdown", { target: first.summary });
  assert.equal(first.menu.open, true);
  assert.equal(second.menu.open, false);
  second.summary.focus();
  h.document.dispatch("pointerdown", { target: new Element("outside-canvas") });
  assert.equal(first.menu.open, false);
  assert.equal(second.summary.focused, false, "Outside pointers release even a closed menu's focused summary");
  h.menuFor("A");
  assert.equal(h.document.listeners.pointerdown.length, 1, "Rendering replacement menus never adds another document listener");
});

test("pointer-closing a trigger releases focus while Escape and keyboard toggles retain it", () => {
  const h = harness(group("root", [condition("A")]));
  const { menu, summary } = h.menuFor("A");
  menu.open = true;
  summary.focus();
  summary.dispatch("click", { detail: 1 });
  assert.equal(menu.open, false);
  assert.equal(summary.focused, false);
  menu.open = true;
  summary.focus();
  let prevented = false;
  summary.dispatch("click", { detail: 0, preventDefault: () => { prevented = true; } });
  assert.equal(prevented, false, "Keyboard summary activation keeps native details behavior");
  assert.equal(summary.focused, true);
  menu.dispatch("keydown", { key: "Escape" });
  assert.equal(menu.open, false);
  assert.equal(summary.focused, true, "Escape returns focus to the menu trigger");
});

test("document dismissal uses the current canvas after a canvas replacement", () => {
  const h = harness(group("root", [condition("A")]));
  const old = h.menuFor("A");
  const replacement = new Element("canvas", h.document);
  h.context.elements.blockCanvas = replacement;
  const fresh = h.menuFor("A");
  fresh.menu.open = true;
  fresh.summary.focus();
  replacement.openMenus = [fresh.menu];
  h.document.dispatch("pointerdown", { target: new Element("outside") });
  assert.equal(fresh.menu.open, false);
  assert.equal(fresh.summary.focused, false);
  assert.equal(h.document.listeners.pointerdown.length, 1);
  assert.equal(old.menu.open, undefined);
});

test("opening a menu from the keyboard closes the previously open menu", () => {
  const h = harness(group("root", [condition("A"), condition("B")]));
  const first = h.menuFor("A");
  const second = h.menuFor("B");
  h.canvas.openMenus = [first.menu, second.menu];
  first.menu.open = true;
  second.menu.open = true;
  second.menu.dispatch("toggle");
  assert.equal(first.menu.open, false);
  assert.equal(second.menu.open, true);
  assert.equal(h.commits(), 0);
});

test("changing the canvas viewport dismisses measured popups without modifying conditions", () => {
  const root = group("root", [condition("A")]);
  const h = harness(root);
  const { menu } = h.menuFor("A");
  menu.open = true;
  h.canvas.openMenus = [menu];
  const viewport = { x: 25, y: -100, zoom: 1 };
  h.context.ensureBlockLayout = () => ({ viewport });
  h.context.constrainBlockViewport = value => value;
  vm.runInContext(functionSource("applyBlockViewport"), h.context);
  const original = JSON.stringify(root);
  const world = { style: {} };
  h.context.applyBlockViewport(world);
  assert.equal(menu.open, false);
  assert.equal(world.style.transform, "translate(25px, -100px) scale(1)");
  assert.deepEqual(viewport, { x: 25, y: -100, zoom: 1 });
  assert.equal(JSON.stringify(root), original);
  assert.equal(h.commits(), 0);
});

test("lower menu opens upward within scaled canvas space without moving its toolbar", () => {
  const h = harness(group("root", [condition("A")]));
  const { menu, summary, options, toolbar } = h.menuFor("A");
  summary.bounds = { top: 650, bottom: 680, height: 30 };
  options.bounds = { left: 300, right: 468, width: 168, top: 685, bottom: 805, height: 120 };
  options.offsetHeight = 150;
  const toolbarBefore = structuredClone(toolbar.bounds);
  menu.open = true;
  menu.dispatch("toggle");
  assert.equal(menu.classList.contains("opens-up"), true);
  assert.ok(parseFloat(options.style.maxHeight) * 0.8 <= 542);
  assert.deepEqual(toolbar.bounds, toolbarBefore);
  summary.bounds = { top: 150, bottom: 180, height: 30 };
  menu.dispatch("toggle");
  assert.equal(menu.classList.contains("opens-up"), false);
});

function popupLayout(options, { left, scale, width = 240, height = 120 }) {
  Object.defineProperty(options, "offsetWidth", {
    get: () => Math.min(width, Number.parseFloat(options.style.maxWidth) || width),
  });
  Object.defineProperty(options, "offsetHeight", {
    get: () => Math.min(height, Number.parseFloat(options.style.maxHeight) || height),
  });
  options.getBoundingClientRect = () => {
    const actualWidth = Math.min(width, Number.parseFloat(options.style.maxWidth) || width) * scale;
    const actualLeft = left + (Number.parseFloat(options.style.left) || 0) * scale;
    const actualHeight = options.offsetHeight * scale;
    return {
      left: actualLeft, right: actualLeft + actualWidth, width: actualWidth,
      top: 250, bottom: 250 + actualHeight, height: actualHeight,
    };
  };
}

test("menus stay inside both canvas edges at different zoom levels", () => {
  for (const scale of [0.5, 1, 2]) {
    for (const left of [210, 1200]) {
      const h = harness(group("root", [condition("A")]));
      const { menu, options } = h.menuFor("A");
      popupLayout(options, { left, scale });
      menu.open = true;
      menu.dispatch("toggle");
      const bounds = options.getBoundingClientRect();
      assert.ok(bounds.left >= h.canvas.bounds.left + 8, `left edge at ${scale}x`);
      assert.ok(bounds.right <= h.canvas.bounds.right - 8, `right edge at ${scale}x`);
    }
  }
});

test("a zoomed menu can shrink to a narrow visible canvas without its minimum width clipping", () => {
  const h = harness(group("root", [condition("A")]));
  h.canvas.bounds = { ...h.canvas.bounds, left: 900, right: 1260, width: 360 };
  const { menu, options } = h.menuFor("A");
  popupLayout(options, { left: 1200, scale: 2 });
  menu.open = true;
  menu.dispatch("toggle");
  const bounds = options.getBoundingClientRect();
  assert.equal(bounds.width, 344);
  assert.equal(bounds.left, 908);
  assert.equal(bounds.right, 1252);
  assert.equal(options.style.minWidth, "172px");
});

test("a 2x popup includes its scaled gap when fitting above or below a fractional-height trigger", () => {
  for (const opensUp of [true, false]) {
    const h = harness(group("root", [condition("A")]));
    h.canvas.bounds.top = 236.17;
    h.canvas.bounds.bottom = opensUp ? 690 : 436.17;
    const { menu, summary, options } = h.menuFor("A");
    summary.bounds = { ...summary.bounds, top: opensUp ? 437.15 : 250, bottom: opensUp ? 491.15 : 304 };
    const naturalHeight = 108 + 1 / 3;
    options.offsetWidth = 210;
    options.offsetHeight = 108;
    options.getBoundingClientRect = () => {
      const height = Math.min(naturalHeight, Number.parseFloat(options.style.maxHeight) || naturalHeight) * 2;
      const top = menu.classList.contains("opens-up")
        ? summary.bounds.top - 10 - height : summary.bounds.bottom + 10;
      return { left: 300, right: 720, width: 420, top, bottom: top + height, height };
    };
    menu.open = true;
    menu.dispatch("toggle");
    const bounds = options.getBoundingClientRect();
    assert.equal(menu.classList.contains("opens-up"), opensUp);
    assert.ok(bounds.top >= h.canvas.bounds.top + 8 - 1e-6);
    assert.ok(bounds.bottom <= h.canvas.bounds.bottom - 8 + 1e-6);
  }
});

test("scrolling or dragging an open menu does not pan, zoom, capture pointers or persist the canvas", () => {
  const h = harness(group("root", [condition("A")]));
  const viewport = { x: 25, y: -150, zoom: 1.5 };
  h.context.ensureBlockLayout = () => ({ viewport });
  h.context.applyBlockViewport = () => { throw new Error("Menu scrolling must not move the canvas"); };
  h.context.persistBlockLayout = () => { throw new Error("Menu scrolling must not persist a new viewport"); };
  h.canvas.setPointerCapture = () => { throw new Error("Menu scrollbar dragging must not capture a canvas pointer"); };
  vm.runInContext(functionSource("bindBlockCanvasPan"), h.context);
  h.context.bindBlockCanvasPan({});
  const original = structuredClone(viewport);
  let prevented = false;
  const target = {
    closest: selector => selector.split(",").some(item => item.trim() === ".composer-tree-rule-add-options") ? {} : null,
  };
  h.canvas.dispatch("pointerdown", {
    target, button: 0, pointerId: 1, clientX: 300, clientY: 400,
    preventDefault: () => { prevented = true; },
  });
  assert.equal(prevented, false, "Native popup scrollbar dragging remains available");
  assert.deepEqual(viewport, original);
  h.canvas.dispatch("wheel", {
    target, deltaY: 60, deltaX: 0, ctrlKey: false, metaKey: false,
    preventDefault: () => { prevented = true; },
  });
  assert.equal(prevented, false, "Native popup scrolling remains available");
  assert.deepEqual(viewport, original);
  h.canvas.dispatch("wheel", {
    target, deltaY: 60, deltaX: 0, ctrlKey: true, metaKey: false,
    preventDefault: () => { prevented = true; },
  });
  assert.equal(prevented, true, "Ctrl-wheel on a menu must not zoom the browser page");
  assert.deepEqual(viewport, original);
});

function panHarness() {
  const h = harness(group("root", [condition("A")]));
  const viewport = { x: 25, y: -150, zoom: 1 };
  const layout = { viewport };
  const calls = [];
  h.context.state.selected = "test-strategy";
  h.context.state.blockSelectedNode = "condition-A";
  h.context.state.blockPackage.layout = layout;
  h.canvas.inspector = new Element("aside");
  h.context.ensureBlockLayout = () => layout;
  h.context.BLOCK_VIEWPORT_COORD_LIMIT = 100000;
  h.context.clampBlockNumber = (value, min, max) => Math.min(max, Math.max(min, value));
  h.context.applyBlockViewport = () => calls.push("pan");
  h.context.persistBlockLayout = () => calls.push("persist");
  h.context.settleBlockFieldEditsForRerender = () => calls.push("settle");
  h.context.renderStrategyBlocks = () => { calls.push("render"); h.canvas.inspector = null; };
  h.context.focusComposerNode = id => { if (id) calls.push("node-focus"); };
  ["closeComposerInspector", "bindBlockCanvasPan"].forEach(name => vm.runInContext(functionSource(name), h.context));
  h.context.bindBlockCanvasPan({});
  const background = { closest: () => null };
  const pointer = { target: background, button: 0, pointerId: 7, clientX: 300, clientY: 400 };
  return { ...h, viewport, calls, pointer };
}

test("a blank canvas click closes the inspector through its normal edit-settling path", () => {
  const h = panHarness();
  h.canvas.dispatch("pointerdown", h.pointer);
  h.canvas.dispatch("pointermove", { ...h.pointer, clientX: 301, clientY: 401 });
  h.canvas.dispatch("pointerup", h.pointer);
  assert.deepEqual(h.calls, ["settle", "render"]);
  assert.equal(h.context.state.blockSelectedNode, false);
  assert.equal(h.canvas.inspector, null);
  assert.equal(h.canvas.focused, true);
  assert.deepEqual(h.viewport, { x: 25, y: -150, zoom: 1 }, "Small click jitter does not change the canvas viewport");
  assert.equal(h.canvas.listeners.pointerup.length, 0);
  assert.equal(h.canvas.listeners.pointermove.length, 0);
});

test("background close restores an unfinished numeric edit just like the close button", () => {
  const h = panHarness();
  const control = {
    value: "-", _blockCommittedValue: "30", _blockPackageOwner: h.context.state.blockPackage,
    _commitBlockField: () => { throw new Error("Incomplete number"); },
    setCustomValidity: value => { control.validity = value; },
  };
  h.canvas.querySelectorAll = selector => selector.includes("data-block-field-id") ? [control] : [];
  vm.runInContext(functionSource("settleBlockFieldEditsForRerender"), h.context);
  h.canvas.dispatch("pointerdown", h.pointer);
  h.canvas.dispatch("pointerup", h.pointer);
  assert.equal(control.value, "30");
  assert.equal(control.validity, "");
  assert.equal(h.canvas.inspector, null);
  assert.equal(h.context.state.blockSelectedNode, false);
});

test("a pan, cancellation, another pointer, or a changed selection does not dismiss the inspector", () => {
  for (const gesture of ["pan", "cancel", "other-pointer", "changed-selection"]) {
    const h = panHarness();
    h.canvas.dispatch("pointerdown", h.pointer);
    if (gesture === "pan") h.canvas.dispatch("pointermove", { ...h.pointer, clientX: 320 });
    if (gesture === "changed-selection") h.context.state.blockSelectedNode = "condition-B";
    h.canvas.dispatch(gesture === "cancel" ? "pointercancel" : "pointerup", {
      ...h.pointer, pointerId: gesture === "other-pointer" ? 99 : h.pointer.pointerId,
    });
    assert.ok(h.canvas.inspector, gesture);
    assert.equal(h.calls.includes("render"), false, gesture);
    assert.equal(h.calls.includes("persist"), gesture === "pan", gesture);
  }
});

test("cards, flow headings, controls, popups and inspector clicks do not start a background close", () => {
  for (const selector of [".composer-tree-row", ".composer-tree-flow", ".composer-inspector", ".composer-tree-rule-add-options", "button", "summary", "[contenteditable]"]) {
    const h = panHarness();
    const target = { closest: selectors => selectors.split(",").map(value => value.trim()).includes(selector) ? {} : null };
    h.canvas.dispatch("pointerdown", { ...h.pointer, target });
    h.canvas.dispatch("pointerup", { ...h.pointer, target });
    assert.ok(h.canvas.inspector, selector);
    assert.deepEqual(h.calls, [], selector);
    assert.equal(h.canvas.capturedPointer, undefined, selector);
  }
});

test("rebinding the canvas cleans up in-progress pointer listeners without closing the inspector", () => {
  const h = panHarness();
  h.canvas.dispatch("pointerdown", h.pointer);
  h.context.bindBlockCanvasPan({});
  assert.equal(h.canvas.listeners.pointerdown.length, 1);
  assert.equal(h.canvas.listeners.pointermove.length, 0);
  assert.equal(h.canvas.listeners.pointerup.length, 0);
  assert.equal(h.canvas.listeners.pointercancel.length, 0);
  assert.equal(h.canvas.capturedPointer, null);
  assert.deepEqual(h.calls, []);
  assert.ok(h.canvas.inspector);
});
