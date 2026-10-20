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
  constructor(tag) {
    this.tagName = tag;
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
  getBoundingClientRect() { return this.bounds; }
  focus() { this.focused = true; }
  contains(target) { return this === target || this.children.some(child => child.contains?.(target)); }
  querySelectorAll() { return this.openMenus || []; }
  dispatch(name, data = {}) {
    const event = { stopPropagation() {}, preventDefault() {}, ...data };
    for (const handler of this.listeners[name] || []) handler(event);
  }
  click() { if (!this.disabled) this.dispatch("click"); }
}

function harness(root, kind = "sell") {
  const notices = [];
  let commits = 0;
  const canvas = new Element("canvas");
  canvas.bounds = { left: 240, right: 1260, width: 1020, top: 100, bottom: 690, height: 590 };
  const context = vm.createContext({
    structuredClone,
    document: { createElement: tag => new Element(tag) },
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
    bindComposerRuleDropTarget: () => {},
  });
  [
    "walkBlockRule", "blockRuleNodeCount", "blockRuleConditionNodes", "findBlockRuleNode",
    "blockRuleContainsAction", "blockRuleNodePath", "blockRuleActionConflict",
    "nextBlockRuleId", "newBlockOrderAction", "newBlockRuleCondition", "newBlockRuleLogic",
    "addBlockRuleChild", "addBlockRuleAction", "ruleComposerNodeId", "ruleComposerActionId",
    "buildComposerRuleBranch", "createComposerRuleAddMenu",
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
  return { context, notices, canvas, model, entryFor, menuFor, commits: () => commits };
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

test("group additions stay inside the selected level and nested additions retain existing identities", () => {
  const a = condition("A");
  const inner = group("inner", [a]);
  const b = condition("B");
  const root = group("root", [inner, b]);
  const h = harness(root);
  h.menuFor("inner").buttons[0].click();
  assert.equal(inner.children.length, 2);
  assert.equal(inner.children[0], a);
  h.menuFor("A").buttons[1].click();
  assert.equal(inner.children[1].kind, "logic");
  assert.equal(inner.children[1].children[0].kind, "condition");
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
  assert.equal(leaf.buttons[2].textContent, "매수 주문");
  assert.equal(leaf.toolbar.children.length, 1, "Leaf footer has no group settings or hierarchy drop target");
  assert.equal(h.menuFor("root").toolbar.children.length, 3);
  assert.ok(h.menuFor("A", true).buttons.every(button => button.disabled));
  const before = JSON.stringify(root);
  h.context.selectedState = () => ({ running: true });
  h.context.addBlockRuleAction("buy", null, "A");
  h.context.addBlockRuleChild("buy", null, "root", "condition");
  assert.equal(JSON.stringify(root), before);
  assert.equal(h.commits(), 0);
});

test("menu dismisses with Escape or outside pointer and installs one canvas listener", () => {
  const h = harness(group("root", [condition("A"), condition("B")]));
  const first = h.menuFor("A");
  const second = h.menuFor("B");
  assert.equal(h.canvas.listeners.pointerdown.length, 1);
  first.menu.open = true;
  first.menu.dispatch("keydown", { key: "Escape" });
  assert.equal(first.menu.open, false);
  assert.equal(first.summary.focused, true);
  first.menu.open = true;
  second.menu.open = true;
  h.canvas.openMenus = [first.menu, second.menu];
  h.canvas.dispatch("pointerdown", { target: first.summary });
  assert.equal(first.menu.open, true);
  assert.equal(second.menu.open, false);
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
