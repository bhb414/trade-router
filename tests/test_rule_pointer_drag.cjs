/* Run with node --test tests/test_rule_pointer_drag.cjs. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(
  path.join(__dirname, "../apps/studio/src/trade_router_studio/static/app.js"), "utf8",
);
function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1);
  const end = source.indexOf("\nfunction ", start + 1);
  return source.slice(start, end < 0 ? undefined : end);
}
function eventTarget() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(callback);
    },
    removeEventListener(type, callback) { listeners.get(type)?.delete(callback); },
    dispatch(type, event) {
      event.type = type;
      for (const callback of [...(listeners.get(type) || [])]) callback(event);
    },
  };
}
function element() {
  const node = eventTarget();
  const classes = new Set();
  Object.assign(node, {
    dataset: {}, style: {}, attributes: {}, capture: null, children: [], parentNode: null,
    offsetWidth: 400, offsetHeight: 100,
    bounds: { left: 20, top: 100, width: 400, height: 100 },
    classList: {
      add: (...values) => values.forEach(value => classes.add(value)),
      remove: (...values) => values.forEach(value => classes.delete(value)),
      contains: value => classes.has(value),
    },
    closest(selector) {
      if (selector === "[data-rule-drop-target]" && this.dataset.ruleDropTarget) return this;
      if (selector.startsWith(".") && this.classList.contains(selector.slice(1))) return this;
      return this.parentNode?.closest(selector) || null;
    },
    getBoundingClientRect() { return this.bounds; },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) {
      delete this.attributes[name];
      if (name.startsWith("data-")) {
        delete this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())];
      }
    },
    append(child) { this.children.push(child); child.parentNode = this; },
    remove() {
      if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
      this.parentNode = null;
    },
    querySelectorAll(selector) {
      return selector === "*" ? this.children.flatMap(child => [child, ...child.querySelectorAll("*")]) : [];
    },
    cloneNode(deep) {
      const copy = element();
      copy.className = this.className;
      copy.dataset = { ...this.dataset };
      copy.attributes = { ...this.attributes };
      copy.style = { ...this.style };
      if (deep) this.children.forEach(child => copy.append(child.cloneNode(true)));
      return copy;
    },
    setPointerCapture(id) { this.capture = id; },
    hasPointerCapture(id) { return this.capture === id; },
    releasePointerCapture() {
      this.capture = null;
      this.dispatch("lostpointercapture", {});
    },
  });
  Object.defineProperty(node, "className", {
    get() { return [...classes].join(" "); },
    set(value) { classes.clear(); value.split(/\s+/).filter(Boolean).forEach(item => classes.add(item)); },
  });
  return node;
}
function fixture({ disabled = false } = {}) {
  const sourceNode = element();
  const destination = element();
  const canvas = element();
  const body = element();
  const window = eventTarget();
  const fixture = { running: false, hit: destination, moves: [], notices: [], plan: { tree: {}, error: "" } };
  const nodes = [sourceNode, destination];
  canvas.querySelectorAll = selector => nodes.filter(node => selector === ".composer-rule-insertion[data-rule-drop-target]"
    ? node.classList.contains("composer-rule-insertion") && node.dataset.ruleDropTarget
    : selector.split(",").some(name => node.classList.contains(name.trim().slice(1))));
  canvas.contains = node => nodes.includes(node);
  const state = { selected: "strategy-a", blockRuleDragClickUntil: 0 };
  const context = vm.createContext({
    state, elements: { blockCanvas: canvas }, window,
    document: { elementFromPoint: () => fixture.hit, createElement: () => element(), body },
    selectedState: () => ({ running: fixture.running }),
    composerRuleDropPlan: () => fixture.plan,
    moveBlockRuleNode: (...args) => fixture.moves.push(args),
    notify: message => fixture.notices.push(message),
    Date, Math,
  });
  [
    "clearComposerRuleDropMarks", "createComposerRuleDragGhost", "updateComposerRuleDragGhost",
    "finishComposerRuleDrag", "bindComposerRuleDrag",
    "composerRulePointerDropAt", "updateComposerRulePointerDrop", "bindComposerRuleDropTarget",
  ].forEach(name => vm.runInContext(functionSource(name), context));
  context.bindComposerRuleDropTarget(destination, { kind: "buy", flowId: "flow-a", nodeId: "B" }, "relative", false);
  context.bindComposerRuleDrag(sourceNode, { kind: "buy", flowId: "flow-a", nodeId: "A" }, disabled);
  const pointer = (x, y, extra = {}) => ({
    pointerId: 7, button: 0, clientX: x, clientY: y, target: sourceNode,
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
    ...extra,
  });
  const seam = ({ top = 210, height = 12, left = 20, width = 400, nodeId = "B", mode = "before", disabled = false } = {}) => {
    const slot = element();
    slot.classList.add("composer-rule-insertion");
    slot.bounds = { left, top, width, height };
    context.bindComposerRuleDropTarget(slot, { kind: "buy", flowId: "flow-a", nodeId }, mode, disabled);
    nodes.push(slot);
    return slot;
  };
  return { ...fixture, backing: fixture, sourceNode, destination, canvas, body, state, window, context, pointer, seam, nodes };
}
function listenerCount(window) {
  return [...window.listeners.values()].reduce((total, callbacks) => total + callbacks.size, 0);
}

test("a click or tiny motion remains a click and releases pointer listeners", () => {
  const f = fixture();
  f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
  f.window.dispatch("pointermove", f.pointer(12, 12));
  assert.equal(f.state.blockRuleDrag, undefined);
  assert.equal(f.body.children.length, 0);
  f.window.dispatch("pointerup", f.pointer(12, 12));
  assert.equal(f.moves.length, 0);
  assert.equal(f.state.blockRuleDragClickUntil, 0);
  assert.equal(f.sourceNode.capture, null);
  assert.equal(listenerCount(f.window), 0);
});

test("pointer movement crosses threshold, highlights insertion, and reorders without HTML5 drag events", () => {
  const f = fixture();
  f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
  f.window.dispatch("pointermove", f.pointer(20, 120));
  assert.equal(f.canvas.classList.contains("rule-dragging"), true);
  assert.equal(f.sourceNode.classList.contains("rule-drag-source"), true);
  assert.equal(f.destination.classList.contains("rule-drop-before"), true);
  f.window.dispatch("pointerup", f.pointer(20, 180));
  assert.deepEqual(f.moves, [["buy", "flow-a", "A", "B", "after", false]]);
  assert.equal(f.canvas.classList.contains("rule-dragging"), false);
  assert.equal(f.destination.classList.contains("rule-drop-before"), false);
  assert.equal(f.state.blockRuleDrag, null);
  assert.equal(f.body.children.length, 0);
  assert.ok(f.state.blockRuleDragClickUntil > Date.now());
  assert.equal(listenerCount(f.window), 0);
});

test("a fixed translucent-ghost hook follows the original pointer grab offset at canvas zoom", () => {
  const f = fixture();
  f.sourceNode.bounds = { left: 20, top: 100, width: 200, height: 50 };
  f.sourceNode.dataset.ruleDropTarget = "A";
  f.sourceNode.dataset.blockNode = "buy-rule-A";
  f.sourceNode.setAttribute("id", "source-card");
  f.sourceNode.setAttribute("tabindex", "0");
  f.sourceNode.classList.add("selected");
  const child = element();
  child.setAttribute("id", "child-control");
  f.sourceNode.append(child);
  f.sourceNode.dispatch("pointerdown", f.pointer(70, 110));
  f.window.dispatch("pointermove", f.pointer(100, 130));
  assert.equal(f.body.children.length, 1);
  const ghost = f.body.children[0];
  assert.equal(ghost.classList.contains("composer-rule-drag-ghost"), true);
  assert.equal(ghost.attributes["aria-hidden"], "true");
  assert.equal(ghost.inert, true);
  assert.equal(ghost.style.position, "fixed");
  assert.equal(ghost.style.pointerEvents, "none");
  assert.equal(ghost.style.width, "400px");
  assert.equal(ghost.style.height, "100px");
  assert.equal(ghost.style.transform, "scale(0.5, 0.5)");
  assert.equal(ghost.style.left, "50px");
  assert.equal(ghost.style.top, "120px");
  assert.equal(ghost.children[0].classList.contains("rule-drag-source"), false);
  assert.equal(ghost.children[0].classList.contains("selected"), false);
  assert.equal(ghost.children[0].dataset.ruleDropTarget, undefined);
  assert.equal(ghost.children[0].attributes.id, undefined);
  assert.equal(ghost.children[0].children[0].attributes.id, undefined);
  assert.deepEqual(f.sourceNode.bounds, { left: 20, top: 100, width: 200, height: 50 });
  f.window.dispatch("pointermove", f.pointer(140, 170));
  assert.equal(f.body.children.length, 1);
  assert.equal(ghost.style.left, "90px");
  assert.equal(ghost.style.top, "160px");
  f.window.dispatch("pointerup", f.pointer(140, 170));
  assert.equal(f.body.children.length, 0);
  assert.equal(ghost.parentNode, null);
});

test("dropping on an unchanged original position is silent and has no invalid or insertion marker", () => {
  const f = fixture();
  f.backing.plan = { tree: null, error: "", unchanged: true };
  f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
  f.window.dispatch("pointermove", f.pointer(20, 120));
  assert.equal(f.destination.classList.contains("rule-drop-invalid"), false);
  assert.equal(f.destination.classList.contains("rule-drop-before"), false);
  assert.equal(f.destination.classList.contains("rule-drop-after"), false);
  assert.equal(f.body.children.length, 1);
  f.window.dispatch("pointerup", f.pointer(20, 120));
  assert.equal(f.moves.length, 0);
  assert.deepEqual(f.notices, []);
  assert.equal(f.body.children.length, 0);
  assert.equal(listenerCount(f.window), 0);
});

test("pointer move keeps an already-open inspector available", () => {
  const f = fixture();
  f.state.blockSelectedNode = "buy-rule-A";
  f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
  f.window.dispatch("pointermove", f.pointer(20, 120));
  f.window.dispatch("pointerup", f.pointer(20, 180));
  assert.deepEqual(f.moves, [["buy", "flow-a", "A", "B", "after", true]]);
});

test("end-of-level target chooses inside and rejected plans never move", () => {
  const f = fixture();
  f.context.bindComposerRuleDropTarget(f.destination, { kind: "buy", flowId: "flow-a", nodeId: "group" }, "inside", false);
  f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
  f.window.dispatch("pointermove", f.pointer(20, 180));
  assert.equal(f.destination.classList.contains("rule-drop-inside"), true);
  f.backing.plan = { tree: null, error: "기존 주문을 보존할 수 없습니다." };
  f.window.dispatch("pointermove", f.pointer(22, 180));
  assert.equal(f.destination.classList.contains("rule-drop-invalid"), true);
  f.window.dispatch("pointerup", f.pointer(22, 180));
  assert.equal(f.moves.length, 0);
  assert.deepEqual(f.notices, ["기존 주문을 보존할 수 없습니다."]);
  assert.equal(listenerCount(f.window), 0);
});

test("Escape, pointer cancellation, blur and explicit rerender cleanup all cancel without committing", () => {
  for (const cancel of ["escape", "pointercancel", "blur", "rerender", "lostcapture"]) {
    const f = fixture();
    f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
    f.window.dispatch("pointermove", f.pointer(20, 120));
    if (cancel === "escape") f.window.dispatch("keydown", f.pointer(0, 0, { key: "Escape" }));
    else if (cancel === "rerender") f.context.finishComposerRuleDrag();
    else if (cancel === "lostcapture") f.sourceNode.dispatch("lostpointercapture", {});
    else f.window.dispatch(cancel, f.pointer(20, 120));
    assert.equal(f.moves.length, 0, cancel);
    assert.equal(f.state.blockRulePointerDrag, null, cancel);
    assert.equal(f.canvas.classList.contains("rule-dragging"), false, cancel);
    assert.equal(f.body.children.length, 0, cancel);
    assert.equal(listenerCount(f.window), 0, cancel);
  }
});

test("disabled and running strategies cannot start or finish a move", () => {
  const disabled = fixture({ disabled: true });
  disabled.sourceNode.dispatch("pointerdown", disabled.pointer(10, 10));
  assert.equal(listenerCount(disabled.window), 0);
  const running = fixture();
  running.backing.running = true;
  running.sourceNode.dispatch("pointerdown", running.pointer(10, 10));
  assert.equal(listenerCount(running.window), 0);
  running.backing.running = false;
  running.sourceNode.dispatch("pointerdown", running.pointer(10, 10));
  running.window.dispatch("pointermove", running.pointer(20, 120));
  running.backing.running = true;
  running.window.dispatch("pointerup", running.pointer(20, 180));
  assert.equal(running.moves.length, 0);
  assert.equal(listenerCount(running.window), 0);
});

test("drag gaps and zero-height edge seams show the marker used for the exact committed target at every zoom", () => {
  for (const zoom of [.5, 1, 1.75]) {
    for (const [mode, nodeId, height] of [["before", "first", 0], ["before", "middle", 12], ["after", "last", 0], ["inside", "empty", 24]]) {
      const f = fixture();
      const top = 300 * zoom;
      const seam = f.seam({ top, height: height * zoom, left: 40 * zoom, width: 600 * zoom, mode, nodeId });
      const x = 180 * zoom;
      const y = top + height * zoom / 2;
      f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
      f.window.dispatch("pointermove", f.pointer(x, y));
      assert.equal(seam.classList.contains(`rule-drop-${mode}`), true, `${zoom}: ${mode}`);
      assert.equal(f.destination.classList.contains("rule-drop-before"), false);
      f.window.dispatch("pointerup", f.pointer(x, y));
      assert.deepEqual(f.moves, [["buy", "flow-a", "A", nodeId, mode, false]]);
      assert.equal(seam.classList.contains(`rule-drop-${mode}`), false);
    }
  }
});

test("card upper and lower halves resolve to their surrounding seams instead of a competing card marker", () => {
  for (const [y, mode] of [[120, "before"], [180, "after"]]) {
    const f = fixture();
    f.destination.classList.add("composer-rule-draggable");
    const branch = element();
    branch.classList.add("composer-rule-branch");
    branch.append(f.destination);
    const before = f.seam({ top: 90, height: 0, nodeId: "B", mode: "before" });
    const after = f.seam({ top: 230, height: 0, nodeId: "B", mode: "after" });
    branch.previousElementSibling = before;
    branch.nextElementSibling = after;
    f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
    f.window.dispatch("pointermove", f.pointer(100, y));
    assert.equal((mode === "before" ? before : after).classList.contains(`rule-drop-${mode}`), true);
    assert.equal(f.destination.classList.contains(`rule-drop-${mode}`), false);
    f.window.dispatch("pointerup", f.pointer(100, y));
    assert.deepEqual(f.moves, [["buy", "flow-a", "A", "B", mode, false]]);
  }
});

test("nested seams choose the deepest target under the pointer and the outer gutter keeps the parent target", () => {
  for (const [x, expected] of [[180, "nested-first"], [45, "outer-first"]]) {
    const f = fixture();
    const outer = f.seam({ top: 250, left: 40, width: 500, height: 0, nodeId: "outer-first" });
    const inner = f.seam({ top: 250, left: 80, width: 460, height: 0, nodeId: "nested-first" });
    f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
    f.window.dispatch("pointermove", f.pointer(x, 250));
    assert.equal((expected === "nested-first" ? inner : outer).classList.contains("rule-drop-before"), true);
    assert.equal((expected === "nested-first" ? outer : inner).classList.contains("rule-drop-before"), false);
    f.window.dispatch("pointerup", f.pointer(x, 250));
    assert.deepEqual(f.moves, [["buy", "flow-a", "A", expected, "before", false]]);
  }
});

test("outside the canvas or over the inspector never drops onto a seam behind it", () => {
  for (const blocked of ["outside", "inspector"]) {
    const f = fixture();
    const seam = f.seam({ top: 250, height: 0 });
    const overlay = element();
    if (blocked === "inspector") {
      overlay.classList.add("composer-inspector");
      f.nodes.push(overlay);
    }
    f.backing.hit = overlay;
    f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
    f.window.dispatch("pointermove", f.pointer(100, 250));
    assert.equal(seam.classList.contains("rule-drop-before"), false);
    f.window.dispatch("pointerup", f.pointer(100, 250));
    assert.equal(f.moves.length, 0);
    assert.deepEqual(f.notices, []);
  }
});

test("a same-position seam is a silent no-op, and a different flow shows rejection without committing", () => {
  for (const unchanged of [true, false]) {
    const f = fixture();
    const seam = f.seam({ top: 250, height: 0 });
    f.backing.plan = unchanged ? { tree: null, error: "", unchanged: true }
      : { tree: null, error: "같은 시간대 흐름 안에서만 조건을 이동할 수 있습니다." };
    f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
    f.window.dispatch("pointermove", f.pointer(100, 250));
    assert.equal(seam.classList.contains("rule-drop-before"), false);
    assert.equal(seam.classList.contains("rule-drop-invalid"), !unchanged);
    f.window.dispatch("pointerup", f.pointer(100, 250));
    assert.equal(f.moves.length, 0);
    assert.equal(f.notices.length, unchanged ? 0 : 1);
  }
});
