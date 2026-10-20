/* Run with node --test tests/test_rule_connections.cjs. */
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
const context = vm.createContext({ structuredClone });
[
  "walkBlockRule", "blockRuleContainsAction", "rewriteBlockRuleConnection",
  "blockRuleTreeEditError", "retainBlockRuleConditionReferences",
  "setBlockRuleConnection", "ensureBlockRuleTree",
  "findBlockRuleNode", "removeBlockRuleNode", "blockRuleConditionNodes",
].forEach(name => vm.runInContext(functionSource(name), context));
const plain = value => JSON.parse(JSON.stringify(value));
const condition = (id, action = null) => ({ kind: "condition", id, condition: { label: id }, action });
const action = id => ({ kind: "order", id, side: "BUY", order_type: "MARKET" });
const logic = (operator, children, ownAction = null, id = "root") => ({
  kind: "logic", id, operator, children, action: ownAction,
});
const rewrite = (root, boundary, operator, nodeId = "root") => {
  let serial = 0;
  return plain(context.rewriteBlockRuleConnection(root, nodeId, boundary, operator, () => `new-${++serial}`));
};
const structure = node => node.kind === "condition" ? node.id : [
  node.operator, ...node.children.map(structure),
];
// Mirrors the documented interpreter behavior: ordered OR, AND gating, and
// a branch action taking precedence over the group's fallback.
const evaluate = (node, truth) => {
  if (node.kind === "condition") return [truth[node.id], truth[node.id] ? node.action : null];
  let selected = null;
  for (const child of node.children) {
    const [passed, childAction] = evaluate(child, truth);
    if (node.operator === "ANY" && passed) return [true, childAction || node.action];
    if (node.operator === "ALL" && !passed) return [false, null];
    selected ||= childAction;
  }
  return node.operator === "ANY" ? [false, null] : [true, selected || node.action];
};

test("changing one OR join introduces only the adjacent AND parentheses", () => {
  const original = logic("ANY", ["A", "B", "C", "D"].map(id => condition(id)), action("fallback"));
  const before = structuredClone(original);
  const changed = rewrite(original, 2, "ALL");
  assert.deepEqual(structure(changed), ["ANY", "A", ["ALL", "B", "C"], "D"]);
  assert.deepEqual(original, before);
  assert.deepEqual(changed.action, original.action);
  for (let mask = 0; mask < 16; mask += 1) {
    const truth = Object.fromEntries(["A", "B", "C", "D"].map((id, bit) => [id, Boolean(mask & (1 << bit))]));
    assert.equal(evaluate(changed, truth)[0], truth.A || (truth.B && truth.C) || truth.D);
  }
});

test("changing one AND join retains both unchanged AND segments", () => {
  const original = logic("ALL", ["A", "B", "C", "D"].map(id => condition(id)));
  const changed = rewrite(original, 2, "ANY");
  assert.deepEqual(structure(changed), ["ANY", ["ALL", "A", "B"], ["ALL", "C", "D"]]);
  assert.deepEqual(structure(original), ["ALL", "A", "B", "C", "D"]);
  for (let mask = 0; mask < 16; mask += 1) {
    const truth = Object.fromEntries(["A", "B", "C", "D"].map((id, bit) => [id, Boolean(mask & (1 << bit))]));
    assert.equal(evaluate(changed, truth)[0], (truth.A && truth.B) || (truth.C && truth.D));
  }
});

test("two operands change in place without another wrapper; nested actions retain scope and order", () => {
  const nested = logic("ANY", [condition("B"), condition("C")], action("nested"), "inner");
  const original = logic("ANY", [condition("A", action("first")), nested], action("fallback"));
  const changed = rewrite(original, 1, "ALL", "inner");
  assert.deepEqual(structure(changed), ["ANY", "A", ["ALL", "B", "C"]]);
  assert.equal(changed.children[1].id, "inner");
  assert.equal(evaluate(changed, { A: true, B: true, C: true })[1].id, "first");
  assert.equal(evaluate(changed, { A: false, B: true, C: true })[1].id, "nested");
  assert.deepEqual(evaluate(changed, { A: false, B: true, C: false }), [false, null]);
  assert.deepEqual(changed.children[1].action, nested.action);
});

test("invalid multi-action AND is rejected transactionally", () => {
  const original = logic("ANY", [condition("A", action("a")), condition("B", action("b")), condition("C")]);
  const before = structuredClone(original);
  let serial = 0;
  let commits = 0;
  const notices = [];
  context.state = { blockPackage: { settings: { buy_rule_tree: original } } };
  context.selectedState = () => ({ running: false });
  context.blockRuleTree = () => context.state.blockPackage.settings.buy_rule_tree;
  context.nextBlockRuleId = () => `generated-${++serial}`;
  context.commitBlockRuleMutation = () => { commits += 1; };
  context.notify = message => notices.push(message);
  context.setBlockRuleConnection("buy", null, "root", 1, "ALL");
  assert.equal(commits, 0);
  assert.equal(notices.length, 1);
  assert.match(notices[0], /각각 주문/);
  assert.equal(context.state.blockPackage.settings.buy_rule_tree, original);
  assert.deepEqual(original, before);
});

test("render-time lookup preserves user parentheses, IDs, and exact saved payload", () => {
  const original = logic("ALL", [
    logic("ALL", [condition("A")], null, "explicit-single"),
    logic("ALL", [condition("B"), condition("C")], null, "explicit-pair"),
  ]);
  const before = JSON.stringify(original);
  const owner = { buy_rule_tree: original };
  assert.equal(context.ensureBlockRuleTree("buy", owner), original);
  assert.equal(JSON.stringify(owner.buy_rule_tree), before);
});

test("successful structural edits retain condition identities for unfinished formula drafts", () => {
  const original = logic("ANY", [condition("A"), condition("B"), condition("C")]);
  const drafts = new WeakMap([[original.children[1].condition, "ATR +"]]);
  let serial = 0;
  context.state = { blockPackage: { settings: { buy_rule_tree: original } } };
  context.selectedState = () => ({ running: false });
  context.blockRuleTree = () => context.state.blockPackage.settings.buy_rule_tree;
  context.nextBlockRuleId = () => `identity-${++serial}`;
  context.commitBlockRuleMutation = () => {};
  context.notify = message => assert.fail(message);
  context.setBlockRuleConnection("buy", null, "root", 2, "ALL");
  const changed = context.state.blockPackage.settings.buy_rule_tree;
  assert.notEqual(changed, original);
  assert.equal(changed.children[1].children[0].condition, original.children[1].condition);
  assert.equal(drafts.get(changed.children[1].children[0].condition), "ATR +");
});

test("join edit respects nesting and node limits before applying the candidate", () => {
  let original = logic("ANY", [condition("A"), condition("B"), condition("C")], null, "deep");
  for (let index = 0; index < 7; index += 1) original = logic("ALL", [original], null, `depth-${index}`);
  const changed = rewrite(original, 1, "ALL", "deep");
  assert.match(context.blockRuleTreeEditError(changed), /8단계/);
  assert.equal(context.blockRuleTreeEditError(original), "");
  assert.equal(context.rewriteBlockRuleConnection(original, "missing", 1, "ALL", () => "unused"), null);
  const hundredNodes = logic("ANY", Array.from({ length: 20 }, (_, groupIndex) => logic(
    "ALL",
    Array.from({ length: groupIndex === 19 ? 3 : 4 }, (_, index) => condition(`condition-${groupIndex}-${index}`)),
    null, `group-${groupIndex}`,
  )));
  assert.equal(context.blockRuleTreeEditError(hundredNodes), "");
  assert.match(context.blockRuleTreeEditError(rewrite(hundredNodes, 1, "ALL")), /100개/);
  assert.match(
    context.blockRuleTreeEditError(logic("ALL", Array.from({ length: 21 }, (_, index) => condition(`c${index}`)))),
    /20개/,
  );
});

test("deleting a condition prunes all now-empty parentheses without changing surviving branches", () => {
  const surviving = condition("B", action("surviving-action"));
  const original = logic("ANY", [
    logic("ALL", [logic("ALL", [condition("A")], null, "inner")], null, "outer"),
    surviving,
  ]);
  context.state = { blockPackage: { settings: { sell_rule_tree: original } } };
  context.selectedState = () => ({ running: false });
  context.blockRuleTree = () => context.state.blockPackage.settings.sell_rule_tree;
  context.blockFlowNodePrefix = () => "sell";
  context.commitBlockRuleMutation = () => {};
  context.notify = message => assert.fail(message);
  context.removeBlockRuleNode("sell", null, "A");
  assert.deepEqual(structure(original), ["ANY", "B"]);
  assert.equal(original.children[0], surviving);
  assert.equal(original.children[0].action.id, "surviving-action");
});
