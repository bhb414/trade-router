const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../apps/studio/src/trade_router_studio/static/app.js"), "utf8");
const functionSource = name => {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, name);
  const remaining = source.slice(start);
  const next = remaining.slice(1).search(/\n(?:async )?function /);
  return next < 0 ? remaining : remaining.slice(0, next + 1);
};
const plain = value => JSON.parse(JSON.stringify(value));
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {};
    this.listeners = {}; this.style = {}; this.className = ""; this.value = "";
    this.classList = {
      contains: value => this.className.split(/\s+/).includes(value),
      add: value => { if (!this.classList.contains(value)) this.className += ` ${value}`; },
      remove: value => { this.className = this.className.split(/\s+/).filter(item => item !== value).join(" "); },
      toggle: (value, enabled) => { if (enabled ?? !this.classList.contains(value)) this.classList.add(value); else this.classList.remove(value); },
    };
  }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  setCustomValidity(message) { this.validationMessage = message; }
  querySelectorAll() { return []; }
  dispatch(name, data = {}) {
    const event = { stopPropagation() {}, preventDefault() {}, ...data };
    for (const handler of this.listeners[name] || []) handler(event);
  }
  click() { if (!this.disabled) this.dispatch("click"); }
}
function harness() {
  let running = false, changes = 0, renders = 0, legacyPromotions = 0;
  const notices = [];
  const settings = {
    buy_flows: [], sell_flows: [],
    buy_rule_tree: { kind: "logic", id: "legacy-buy", children: [], action: { id: "old-buy" } },
    sell_rule_tree: { kind: "logic", id: "legacy-sell", children: [], action: { id: "old-sell" } },
    total_operating_budget: "200000",
  };
  const context = vm.createContext({
    structuredClone,
    document: Object.assign(new Element("document"), { createElement: tag => new Element(tag) }),
    elements: { blockCanvas: new Element("div") },
    window: { innerHeight: 1000, innerWidth: 1600 },
    state: { selected: "test-strategy", blockPackage: { settings }, conditionGroupSerial: 0, connections: [] },
    BLOCK_PACKAGE_FIELD_TYPES: { execution_scan_seconds: "number" },
    selectedState: () => ({ running }),
    setDirty: () => { changes += 1; },
    renderStrategyBlocks: () => { renders += 1; },
    flushBlockFieldEdits: () => true,
    settleBlockFieldEditsForRerender: () => {},
    promoteLegacyBlockRuleTrees: () => { legacyPromotions += 1; assert.fail("Execution editing must not migrate buy/sell rules"); },
    notify: message => notices.push(message),
    packageFieldSourceName: key => key.toUpperCase(),
    genericConstantMeta: name => ["schedule", name],
    blockConditionData: () => ({ conditions: [] }),
  });
  [
    "blockExecutionFlows", "blockExecutionFlowFields", "blockFlow", "blockFlowNodePrefix",
    "blockBuyFlows", "blockBuyFlow", "blockSellFlows", "blockSellFlow",
    "addBlockExecutionFlow", "removeBlockExecutionFlow", "createComposerTreeAddRow", "buildComposerTreeModel",
    "executionMetricOptions", "executionValueOptions", "executionOperatorOptions", "newExecutionCondition",
    "executionConditionSummary", "executionActionOptions", "executionActionLabel",
    "createExecutionConditionEditor", "updateExecutionCondition", "createExecutionActionEditor", "updateExecutionAction", "createBlockRuleNodeEditor",
    "formulaControl", "conditionSelect", "bindBlockConditionControl", "commitBlockRuleMutation",
    "nextBlockRuleId", "newBlockOrderAction", "newBlockRuleCondition", "newBlockRuleLogic", "newBlockRuleRoot",
    "ensureBlockRuleTree", "blockRuleTree", "walkBlockRule", "findBlockRuleNode", "blockRuleNodeCount", "blockRuleConditionNodes",
    "addBlockRuleChild", "addBlockRuleAction", "removeBlockRuleAction", "removeBlockRuleNode",
    "blockRuleActionConflict", "blockRuleNodePath", "blockRuleContainsAction",
    "rewriteBlockRuleConnection", "blockRuleTreeEditError", "setBlockRuleConnection", "retainBlockRuleConditionReferences",
    "rewriteBlockRuleMove", "moveBlockRuleNode", "composerRuleDropPlan", "bindComposerRuleDropTarget",
    "buildComposerRuleBranch", "ruleComposerNodeId", "ruleComposerActionId", "createComposerRuleAddMenu",
    "collectComposerRuleDefinitions", "applyBlockFieldEdit", "normalizeBlockPackageValue",
  ].forEach(name => vm.runInContext(functionSource(name), context));
  context.currentBlockModel = () => ({
    fields: context.blockExecutionFlows().flatMap(context.blockExecutionFlowFields), schedules: [],
  });
  const add = () => {
    const section = context.buildComposerTreeModel(settings).children.find(item => item.id === "execution-section");
    context.createComposerTreeAddRow(section.children.find(item => item.kind === "add"), running).click();
    return settings.execution_flows?.at(-1);
  };
  const reference = (flow, node) => ({ kind: "execution", flowId: flow.id, nodeId: node.id });
  const menuFor = (flow, nodeId) => {
    const model = context.buildComposerRuleBranch("execution", flow.id, flow.execution_rule_tree, true);
    const entries = [];
    const visit = entry => { entries.push(entry); (entry.children || []).forEach(visit); };
    visit(model);
    const entry = entries.find(item => item.kind === "rule-add" && item.ownerId === nodeId);
    return context.createComposerRuleAddMenu(entry, running).children[0].children[1];
  };
  return { context, settings, add, reference, menuFor, notices,
    running: value => { running = value; }, changes: () => changes, renders: () => renders, legacyPromotions: () => legacyPromotions };
}

test("opening an existing strategy adds no execution policy; adding/removing a flow is explicit and isolated", () => {
  const { context, settings, add, legacyPromotions } = harness();
  const before = JSON.stringify(settings);
  const section = context.buildComposerTreeModel(settings).children.find(item => item.id === "execution-section");
  assert.equal(section.title, "체결 구성");
  assert.equal(section.children.length, 1);
  assert.equal(section.children[0].action, "add-execution-flow");
  assert.equal(JSON.stringify(settings), before);
  const flow = add();
  assert.equal(flow.execution_scan_seconds, 5);
  assert.equal(flow.execution_start_time, "08:00");
  assert.equal(flow.execution_end_time, "20:00");
  assert.deepEqual(plain(flow.execution_rule_tree.children[0].condition), { metric: "elapsed_seconds", operator: ">=", value: 30 });
  assert.equal(context.blockRuleContainsAction(flow.execution_rule_tree), false);
  assert.equal(context.state.blockSelectedNode, `execution-flow-${flow.id}-schedule`);
  const executionEditor = context.createBlockRuleNodeEditor({
    kind: "execution", flowId: flow.id, nodeId: flow.execution_rule_tree.id, isRoot: true,
  }, false);
  const executionHelp = executionEditor.children[0].children.find(child => child.tagName === "P").textContent;
  assert.match(executionHelp, /아직 요청하지 않은 행동/);
  assert.match(executionHelp, /다음 확인 때 다른 조건의 행동도 실행/);
  for (const kind of ["buy", "sell"]) {
    const editor = context.createBlockRuleNodeEditor({
      kind, flowId: null, nodeId: settings[`${kind}_rule_tree`].id, isRoot: true,
    }, false);
    assert.equal(editor.children[0].children.find(child => child.tagName === "P").textContent,
      "연결선의 AND·OR를 눌러 그 연결만 바꿉니다. 하위 계층을 먼저 판단하며, OR에서는 먼저 참인 경로의 행동을 실행합니다.");
  }
  context.removeBlockExecutionFlow(flow.id);
  assert.deepEqual(plain(settings.execution_flows), []);
  const { execution_flows, ...unchanged } = settings;
  assert.equal(JSON.stringify(unchanged), before);
  assert.equal(legacyPromotions(), 0);
});

test("order-data controls change metric, comparison and value without a price expression or losing numeric validation", () => {
  const { context, add, reference, notices, settings } = harness();
  const beforeBuy = JSON.stringify(settings.buy_rule_tree);
  const flow = add(), node = flow.execution_rule_tree.children[0];
  const ref = reference(flow, node);
  let editor = context.createExecutionConditionEditor(ref, node, false);
  const input = editor.children[2].children[1];
  input.value = "45"; input.dispatch("input");
  assert.equal(node.condition.value, 45);
  input.value = "-1"; input.dispatch("change");
  assert.equal(node.condition.value, 45);
  assert.match(input.validationMessage, /0 이상/);
  assert.equal(notices.length, 1);
  const metric = editor.children[0].children[1];
  metric.value = "order_side"; metric.dispatch("change");
  assert.deepEqual(plain(node.condition), { metric: "order_side", operator: "==", value: "BUY" });
  editor = context.createExecutionConditionEditor(ref, node, false);
  assert.equal(editor.children[1].children[1].children.length, 2);
  const side = editor.children[2].children[1];
  side.value = "SELL"; side.dispatch("change");
  assert.equal(context.executionConditionSummary(node.condition), "매수 / 매도 = 매도");
  context.updateExecutionCondition(ref, "operator", ">=");
  assert.equal(node.condition.operator, "==");
  context.updateExecutionCondition(ref, "metric", "order_type");
  context.updateExecutionCondition(ref, "value", "BEST_LIMIT");
  assert.equal(context.executionConditionSummary(node.condition), "주문 방식 = 최유리지정가");
  for (const metric of ["remaining_quantity", "filled_quantity"]) {
    context.updateExecutionCondition(ref, "metric", metric);
    context.updateExecutionCondition(ref, "value", 2);
    assert.match(context.executionConditionSummary(node.condition), /2주/);
  }
  assert.equal(JSON.stringify(settings.buy_rule_tree), beforeBuy);
});

test("hover menu attaches cancel or amendment only to its selected condition and action editor preserves the ID", () => {
  const { context, add, reference, menuFor, settings, legacyPromotions } = harness();
  const beforeBuy = JSON.stringify(settings.buy_rule_tree), beforeSell = JSON.stringify(settings.sell_rule_tree);
  const flow = add(), node = flow.execution_rule_tree.children[0];
  const menu = menuFor(flow, node.id);
  assert.deepEqual(menu.children.map(item => item.textContent), ["조건식", "하위 계층 추가", "잔량 취소 · 앞 조건 통과 시", "최유리지정가로 정정 · 앞 조건 통과 시"]);
  menu.children[3].click();
  assert.equal(node.action.kind, "execution");
  assert.equal(node.action.action_type, "AMEND_BEST_LIMIT");
  assert.equal(flow.execution_rule_tree.action, null);
  const actionId = node.action.id;
  const editor = context.createExecutionActionEditor(reference(flow, node), false);
  const select = editor.children[1].children[1];
  assert.equal(select.value, "AMEND_BEST_LIMIT");
  assert.match(editor.children[2].textContent, /취소 후 새 주문을 내지/);
  select.value = "CANCEL_REMAINDER"; select.dispatch("change");
  assert.equal(node.action.id, actionId);
  assert.equal(node.action.action_type, "CANCEL_REMAINDER");
  const reopened = context.createExecutionActionEditor(reference(flow, node), false);
  assert.match(reopened.children[2].textContent, /새 매수 조건.*다시 매수/);
  const definitions = [];
  context.collectComposerRuleDefinitions(definitions, "execution", flow.id, flow.execution_rule_tree);
  assert.equal(definitions.find(item => item.ruleActionOwner).title, "잔량 취소");
  assert.equal(definitions.find(item => item.ruleNode && !item.ruleNode.isRoot).summary[0], "미체결 경과 시간 ≥ 30초");
  reopened.children[3].click();
  assert.equal(node.action, null);
  assert.equal(JSON.stringify(settings.buy_rule_tree), beforeBuy);
  assert.equal(JSON.stringify(settings.sell_rule_tree), beforeSell);
  assert.equal(legacyPromotions(), 0);
});

test("execution AND/OR joins and drag reordering retain condition data and actions in the same flow", () => {
  const { context, add, menuFor, settings } = harness();
  const flow = add(), root = flow.execution_rule_tree;
  menuFor(flow, root.id).children[0].click();
  menuFor(flow, root.id).children[0].click();
  const [a, b, c] = root.children;
  context.addBlockRuleAction("execution", flow.id, a.id, "CANCEL_REMAINDER");
  context.setBlockRuleConnection("execution", flow.id, root.id, 1, "ANY");
  const current = flow.execution_rule_tree;
  assert.equal(current.operator, "ANY");
  assert.equal(current.children[0].action.action_type, "CANCEL_REMAINDER");
  const actionId = current.children[0].action.id;
  assert.equal(context.moveBlockRuleNode("execution", flow.id, a.id, c.id, "after"), true);
  let moved;
  context.walkBlockRule(flow.execution_rule_tree, node => { if (node.id === a.id) moved = node; });
  assert.equal(moved.action.id, actionId);
  assert.equal(moved.condition, a.condition);
  context.state.blockRuleDrag = { strategy: "test-strategy", kind: "execution", flowId: flow.id, nodeId: b.id };
  assert.match(context.composerRuleDropPlan({ kind: "buy", flowId: flow.id, nodeId: c.id }, "after").error, /같은 시간대/);
  const saved = JSON.stringify(settings.execution_flows);
  settings.execution_flows = JSON.parse(saved);
  assert.equal(JSON.stringify(settings.execution_flows), saved);
});

test("schedule interval requires five seconds, flows are capped at eight, and running strategies reject mutations", () => {
  const { context, add, running, settings, reference, menuFor, changes } = harness();
  const flow = add(), node = flow.execution_rule_tree.children[0];
  const field = context.blockExecutionFlowFields(flow).find(item => item.settingKey === "execution_scan_seconds");
  assert.throws(() => context.applyBlockFieldEdit(field.id, "4"), /5초 이상/);
  assert.equal(flow.execution_scan_seconds, 5);
  context.applyBlockFieldEdit(field.id, "10");
  assert.equal(flow.execution_scan_seconds, 10);
  for (let index = 0; index < 9; index += 1) add();
  assert.equal(settings.execution_flows.length, 8);
  context.addBlockRuleAction("execution", flow.id, node.id, "CANCEL_REMAINDER");
  running(true);
  const before = JSON.stringify(settings), beforeChanges = changes();
  add();
  context.removeBlockExecutionFlow(flow.id);
  context.updateExecutionCondition(reference(flow, node), "value", 60);
  context.updateExecutionAction(reference(flow, node), "AMEND_BEST_LIMIT");
  context.applyBlockFieldEdit(field.id, "15");
  context.removeBlockRuleAction("execution", flow.id, node.id);
  context.addBlockRuleChild("execution", flow.id, flow.execution_rule_tree.id, "condition");
  assert.ok(menuFor(flow, node.id).children.every(button => button.disabled));
  assert.equal(JSON.stringify(settings), before);
  assert.equal(changes(), beforeChanges);
});
