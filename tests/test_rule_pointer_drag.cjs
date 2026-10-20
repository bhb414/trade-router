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
    dataset: {}, style: {}, capture: null,
    classList: {
      add: (...values) => values.forEach(value => classes.add(value)),
      remove: (...values) => values.forEach(value => classes.delete(value)),
      contains: value => classes.has(value),
    },
    closest(selector) {
      return selector === "[data-rule-drop-target]" && this.dataset.ruleDropTarget ? this : null;
    },
    getBoundingClientRect: () => ({ top: 100, height: 100 }),
    setPointerCapture(id) { this.capture = id; },
    hasPointerCapture(id) { return this.capture === id; },
    releasePointerCapture() {
      this.capture = null;
      this.dispatch("lostpointercapture", {});
    },
  });
  return node;
}
function fixture({ disabled = false } = {}) {
  const sourceNode = element();
  const destination = element();
  const canvas = element();
  const window = eventTarget();
  const fixture = { running: false, hit: destination, moves: [], notices: [], plan: { tree: {}, error: "" } };
  const nodes = [sourceNode, destination];
  canvas.querySelectorAll = selector => nodes.filter(node => (
    selector.split(",").some(name => node.classList.contains(name.trim().slice(1)))
  ));
  canvas.contains = node => nodes.includes(node);
  const state = { selected: "strategy-a", blockRuleDragClickUntil: 0 };
  const context = vm.createContext({
    state, elements: { blockCanvas: canvas }, window,
    document: { elementFromPoint: () => fixture.hit },
    selectedState: () => ({ running: fixture.running }),
    composerRuleDropPlan: () => fixture.plan,
    moveBlockRuleNode: (...args) => fixture.moves.push(args),
    notify: message => fixture.notices.push(message),
    Date, Math,
  });
  [
    "clearComposerRuleDropMarks", "finishComposerRuleDrag", "bindComposerRuleDrag",
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
  return { ...fixture, backing: fixture, sourceNode, destination, canvas, state, window, context, pointer };
}
function listenerCount(window) {
  return [...window.listeners.values()].reduce((total, callbacks) => total + callbacks.size, 0);
}

test("a click or tiny motion remains a click and releases pointer listeners", () => {
  const f = fixture();
  f.sourceNode.dispatch("pointerdown", f.pointer(10, 10));
  f.window.dispatch("pointermove", f.pointer(12, 12));
  assert.equal(f.state.blockRuleDrag, undefined);
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
  assert.ok(f.state.blockRuleDragClickUntil > Date.now());
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
