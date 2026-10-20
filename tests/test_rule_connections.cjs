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
  "blockRuleNodePath", "rewriteBlockRuleMove", "moveBlockRuleNode", "composerRuleDropPlan",
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

test("moving sibling conditions uses the target position after removal and recognizes no-ops", () => {
  const original = logic("ANY", ["A", "B", "C"].map(id => condition(id)));
  const before = structuredClone(original);
  const toEnd = context.rewriteBlockRuleMove(original, "A", "C", "after");
  assert.equal(toEnd.error, "");
  assert.deepEqual(structure(plain(toEnd.tree)), ["ANY", "B", "C", "A"]);
  const toStart = context.rewriteBlockRuleMove(original, "C", "A", "before");
  assert.deepEqual(structure(plain(toStart.tree)), ["ANY", "C", "A", "B"]);
  assert.equal(context.rewriteBlockRuleMove(original, "A", "B", "before").unchanged, true);
  assert.equal(context.rewriteBlockRuleMove(original, "C", "root", "inside").unchanged, true);
  assert.deepEqual(original, before);
});

test("moving between levels preserves IDs, condition content, actions and explicit surviving groups", () => {
  const original = logic("ANY", [
    logic("ALL", [condition("A"), condition("B")], null, "G"),
    condition("C", action("order-c")),
  ], action("root-order"));
  const movedOut = context.rewriteBlockRuleMove(original, "B", "C", "after");
  assert.deepEqual(structure(plain(movedOut.tree)), ["ANY", ["ALL", "A"], "C", "B"]);
  assert.equal(movedOut.tree.children[0].id, "G");
  assert.equal(movedOut.tree.children[1].action.id, "order-c");
  assert.equal(movedOut.tree.action.id, "root-order");
  const movedIn = context.rewriteBlockRuleMove(original, "C", "G", "inside");
  assert.deepEqual(structure(plain(movedIn.tree)), ["ANY", ["ALL", "A", "B", "C"]]);
  assert.equal(movedIn.tree.children[0].children[2].action.id, "order-c");
});

test("moving the last child prunes empty actionless ancestors but rejects orphaning an order", () => {
  const original = logic("ANY", [
    logic("ALL", [logic("ALL", [condition("A")], null, "inner")], null, "outer"),
    condition("B"),
  ]);
  const moved = context.rewriteBlockRuleMove(original, "A", "B", "after");
  assert.deepEqual(structure(plain(moved.tree)), ["ANY", "B", "A"]);
  original.children[0].action = action("group-order");
  const before = structuredClone(original);
  const rejected = context.rewriteBlockRuleMove(original, "A", "B", "after");
  assert.equal(rejected.tree, null);
  assert.match(rejected.error, /주문만 남습니다/);
  assert.deepEqual(original, before);
});

test("moving rejects self, descendants, the root, and conflicting AND branch actions", () => {
  const original = logic("ANY", [
    condition("A", action("order-a")),
    logic("ALL", [condition("B", action("order-b")), condition("C")], null, "G"),
  ]);
  const before = structuredClone(original);
  assert.match(context.rewriteBlockRuleMove(original, "A", "A", "after").error, /자기 자신/);
  assert.match(context.rewriteBlockRuleMove(original, "G", "B", "before").error, /하위 단계/);
  assert.equal(context.rewriteBlockRuleMove(original, "root", "G", "inside").tree, null);
  assert.match(context.rewriteBlockRuleMove(original, "A", "G", "inside").error, /각각 주문/);
  assert.deepEqual(original, before);
});

test("moves enforce final depth, child limit, and SELL condition requirement", () => {
  let deep = logic("ALL", [condition("deep-condition")], null, "deep-destination");
  for (let index = 0; index < 6; index += 1) deep = logic("ALL", [deep], null, `deep-${index}`);
  const root = logic("ANY", [
    logic("ALL", [condition("source-leaf")], null, "source-group"), deep,
  ]);
  assert.equal(context.blockRuleTreeEditError(root), "");
  assert.match(context.rewriteBlockRuleMove(root, "source-group", "deep-destination", "inside").error, /8단계/);
  const full = logic("ALL", Array.from({ length: 20 }, (_, index) => condition(`full-${index}`)), null, "full");
  assert.match(context.rewriteBlockRuleMove(logic("ANY", [condition("A"), full]), "A", "full", "inside").error, /20개/);
  const emptySell = logic("ANY", [logic("ALL", [], null, "empty-a"), logic("ALL", [], null, "empty-b")]);
  assert.match(context.rewriteBlockRuleMove(emptySell, "empty-a", "empty-b", "after", "SELL").error, /매도 흐름/);
});

test("a committed move preserves unfinished formula identities and is blocked while running", () => {
  const original = logic("ANY", [condition("A"), condition("B")], action("fallback"));
  const draft = new WeakMap([[original.children[0].condition, "ATR +"]]);
  let commits = 0;
  context.state = { blockPackage: { settings: { buy_rule_tree: original } } };
  context.selectedState = () => ({ running: false });
  context.blockRuleTree = () => context.state.blockPackage.settings.buy_rule_tree;
  context.ruleComposerNodeId = (kind, flowId, nodeId) => `${kind}-rule-${nodeId}`;
  context.commitBlockRuleMutation = () => { commits += 1; };
  context.notify = message => assert.fail(message);
  assert.equal(context.moveBlockRuleNode("buy", null, "A", "B", "after"), true);
  const changed = context.state.blockPackage.settings.buy_rule_tree;
  assert.equal(draft.get(changed.children[1].condition), "ATR +");
  assert.equal(changed.action.id, "fallback");
  assert.equal(context.state.blockSelectedNode, "buy-rule-A");
  assert.equal(commits, 1);
  assert.equal(context.moveBlockRuleNode("buy", null, "A", "B", "before", false), true);
  assert.equal(context.state.blockSelectedNode, false);
  assert.equal(commits, 2);
  const afterClosedMove = context.state.blockPackage.settings.buy_rule_tree;
  context.selectedState = () => ({ running: true });
  assert.equal(context.moveBlockRuleNode("buy", null, "A", "B", "after"), false);
  assert.equal(context.state.blockPackage.settings.buy_rule_tree, afterClosedMove);
  assert.equal(commits, 2);
});

test("drop proposals never cross strategies, time flows, or buy/sell sides", () => {
  context.state = {
    selected: "strategy-a",
    blockRuleDrag: { strategy: "strategy-a", kind: "buy", flowId: "flow-a", nodeId: "A" },
  };
  const reference = { kind: "buy", flowId: "flow-a", nodeId: "B" };
  context.blockRuleTree = () => logic("ANY", [condition("A"), condition("B")]);
  assert.equal(context.composerRuleDropPlan(reference, "after").error, "");
  assert.match(context.composerRuleDropPlan({ ...reference, kind: "sell" }, "after").error, /같은 시간대 흐름/);
  assert.match(context.composerRuleDropPlan({ ...reference, flowId: "flow-b" }, "after").error, /같은 시간대 흐름/);
  context.state.selected = "strategy-b";
  assert.match(context.composerRuleDropPlan(reference, "after").error, /같은 시간대 흐름/);
});
