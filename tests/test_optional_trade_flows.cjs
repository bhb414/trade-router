/* Run with node --test tests/test_optional_trade_flows.cjs. */
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
  const tail = source.slice(start + 1);
  const end = tail.search(/\n(?:async )?function /);
  return source.slice(start, end < 0 ? undefined : start + 1 + end);
};

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  click() { if (!this.disabled) this.listeners.click?.({ stopPropagation() {} }); }
}

function harness(settings = {}) {
  let running = false;
  let renders = 0;
  const context = vm.createContext({
    structuredClone,
    document: { createElement: tag => new Element(tag) },
    elements: { blockCanvas: { querySelectorAll: () => [] } },
    state: { blockPackage: { settings }, conditionGroupSerial: 0, blockSelectedNode: false },
    selectedState: () => ({ running }),
    flushBlockFieldEdits: () => true,
    newBlockTradeCondition: () => ({ left: "close", right: "value", value: "0" }),
    normalizeConditionAtrPeriods: () => {},
    ensureBlockRuleTree: () => {},
    newBlockRuleRoot: (kind, children) => ({ kind: "logic", id: `${kind}-root`, children }),
    newBlockRuleCondition: kind => ({ kind: "condition", id: `${kind}-condition` }),
    newConditionGroup: kind => ({ id: `${kind}-group`, conditions: [] }),
    nextBlockRuleId: prefix => `${prefix}-${++context.state.conditionGroupSerial}`,
    setDirty: dirty => { context.state.dirty = dirty; },
    renderStrategyBlocks: () => { renders += 1; },
    setScheduleSectionExpanded: () => {},
    refreshBlockConditions: async () => {},
    notify: () => {},
    genericConstantMeta: name => ["general", name],
    packageFieldSourceName: name => name.toUpperCase(),
    BLOCK_PACKAGE_FIELD_TYPES: {},
    blockRuleTree: () => ({ kind: "logic", id: "root", children: [] }),
    blockRuleNodeCount: () => 1,
    buildComposerRuleBranch: () => ({ kind: "rule-node", children: [] }),
  });
  for (const name of ["BLOCK_BUY_FLOW_FIELDS", "BLOCK_SELL_FLOW_FIELDS"]) {
    const match = source.match(new RegExp(`const ${name} = \\[[\\s\\S]*?\\];`));
    assert.ok(match, `Missing ${name}`);
    vm.runInContext(match[0], context);
  }
  [
    "blockBuyFlows", "blockSellFlows", "blockExecutionFlows", "blockFlowNodePrefix", "blockPackageFields",
    "addBlockBuyFlow", "removeBlockBuyFlow", "addBlockSellFlow", "removeBlockSellFlow",
    "addBlockExecutionFlow", "removeBlockExecutionFlow",
    "settleBlockFieldEditsForRerender",
    "normalizeManagedBlockSettings", "blockSellTargetMode", "blockSellTargetSummary", "buildComposerTreeModel", "createComposerInspector",
    "createComposerTreeAddRow",
  ].forEach(name => vm.runInContext(functionSource(name), context));
  return { context, settings, setRunning: value => { running = value; }, renders: () => renders };
}

test("legacy packages retain buy and sell flows when optional flags are absent", () => {
  for (const settings of [{}, { buy_flows: [], sell_flows: [] }]) {
    const h = harness(settings);
    h.context.normalizeManagedBlockSettings(settings);
    assert.equal(settings.buy_enabled, true);
    assert.equal(settings.sell_enabled, true);
    assert.equal(settings.buy_flows.length, 1);
    assert.equal(settings.sell_flows.length, 1);
  }
});

test("explicitly disabled sides stay empty across save and repeated load normalization", () => {
  for (const disabled of ["buy", "sell", "both"]) {
    const settings = {
      buy_enabled: disabled === "sell", sell_enabled: disabled === "buy",
      buy_flows: [{ id: "stale-buy" }], sell_flows: [{ id: "stale-sell" }],
      execution_flows: [{ id: "pending-orders", execution_rule_tree: { id: "exec" } }],
    };
    const h = harness(settings);
    h.context.normalizeManagedBlockSettings(settings);
    const reloaded = JSON.parse(JSON.stringify(settings));
    h.context.normalizeManagedBlockSettings(reloaded);
    assert.equal(reloaded.buy_flows.length, disabled === "sell" ? 1 : 0);
    assert.equal(reloaded.sell_flows.length, disabled === "buy" ? 1 : 0);
    assert.equal(reloaded.execution_flows[0].id, "pending-orders");
  }
});

for (const kind of ["buy", "sell"]) {
  const title = kind === "buy" ? "Buy" : "Sell";
  test(`${kind}: deleting the last flow disables only that side and clears its inspector`, () => {
    const settings = {};
    const h = harness(settings);
    h.context.normalizeManagedBlockSettings(settings);
    const flow = settings[`${kind}_flows`][0];
    const other = kind === "buy" ? "sell" : "buy";
    const otherBefore = JSON.stringify(settings[`${other}_flows`]);
    h.context.state.blockSelectedNode = `${h.context.blockFlowNodePrefix(kind, flow.id)}-schedule`;
    h.context[`removeBlock${title}Flow`](flow.id);
    assert.equal(settings[`${kind}_flows`].length, 0);
    assert.equal(settings[`${kind}_enabled`], false);
    assert.equal(settings[`${other}_enabled`], true);
    assert.equal(JSON.stringify(settings[`${other}_flows`]), otherBefore);
    assert.equal(h.context.state.blockSelectedNode, false);
    h.context.normalizeManagedBlockSettings(settings);
    assert.equal(settings[`${kind}_flows`].length, 0);
    assert.equal(h.context.state.dirty, true);
    assert.equal(h.renders(), 1);
  });

  test(`${kind}: deleting one of several flows keeps that side enabled`, () => {
    const h = harness({});
    h.context.normalizeManagedBlockSettings(h.settings);
    h.context[`addBlock${title}Flow`]();
    const flows = h.settings[`${kind}_flows`];
    h.context[`removeBlock${title}Flow`](flows[0].id);
    assert.equal(flows.length, 1);
    assert.equal(h.settings[`${kind}_enabled`], true);
  });

  test(`${kind}: empty-section add restores only the requested side`, () => {
    const h = harness({ buy_enabled: false, sell_enabled: false });
    h.context.normalizeManagedBlockSettings(h.settings);
    const model = h.context.buildComposerTreeModel(h.settings);
    const section = model.children.find(item => item.id === `${kind}-section`);
    assert.match(section.description, /흐름 없음/);
    assert.equal(section.children.length, 1);
    const button = h.context.createComposerTreeAddRow(section.children[0], false);
    button.click();
    const other = kind === "buy" ? "sell" : "buy";
    assert.equal(h.settings[`${kind}_flows`].length, 1);
    assert.equal(h.settings[`${kind}_enabled`], true);
    assert.equal(h.settings[`${other}_flows`].length, 0);
    assert.equal(h.settings[`${other}_enabled`], false);
    assert.equal(h.settings[`${kind}_flows`][0][`${kind}_rule_tree`].kind, "logic");
  });

  test(`${kind}: last-flow delete button is accessible in the inspector header and enabled only when stopped`, () => {
    const h = harness({});
    h.context.normalizeManagedBlockSettings(h.settings);
    const flow = h.settings[`${kind}_flows`][0];
    const definition = { id: "schedule", title: "흐름", flowKind: kind, flowId: flow.id };
    const inspector = h.context.createComposerInspector(definition, false, { buy: 1, sell: 1 });
    const remove = inspector.children[0].children.find(child => child.className?.includes("composer-inspector-remove-flow"));
    assert.ok(remove, "Flow removal must not require scrolling the inspector body");
    assert.match(remove.attributes["aria-label"], /흐름 삭제/);
    assert.equal(remove.disabled, false);
    const runningInspector = h.context.createComposerInspector(definition, true, { buy: 1, sell: 1 });
    assert.equal(runningInspector.children[0].children.find(child => child.className?.includes("composer-inspector-remove-flow")).disabled, true);
    h.setRunning(true);
    h.context[`removeBlock${title}Flow`](flow.id);
    h.context[`addBlock${title}Flow`]();
    assert.equal(h.settings[`${kind}_flows`].length, 1);
    assert.equal(h.renders(), 0);
    h.setRunning(false);
    remove.click();
    assert.equal(h.settings[`${kind}_flows`].length, 0);
  });

  test(`${kind}: deleting the selected flow closes its inspector even when another flow survives`, () => {
    const h = harness({});
    h.context.normalizeManagedBlockSettings(h.settings);
    h.context[`addBlock${title}Flow`]();
    const selected = h.settings[`${kind}_flows`][1];
    h.context.state.blockSelectedNode = `${h.context.blockFlowNodePrefix(kind, selected.id)}-rule-child`;
    h.context[`removeBlock${title}Flow`](selected.id);
    assert.equal(h.settings[`${kind}_flows`].length, 1);
    assert.equal(h.context.state.blockSelectedNode, false);
  });

  test(`${kind}: deleting another flow keeps the selected surviving panel`, () => {
    const h = harness({});
    h.context.normalizeManagedBlockSettings(h.settings);
    h.context[`addBlock${title}Flow`]();
    const flows = h.settings[`${kind}_flows`];
    const selected = `${h.context.blockFlowNodePrefix(kind, flows[1].id)}-schedule`;
    h.context.state.blockSelectedNode = selected;
    h.context[`removeBlock${title}Flow`](flows[0].id);
    assert.equal(h.context.state.blockSelectedNode, selected);
  });
}

test("execution: header removes the last flow, closes its inspector and survives reload as empty", () => {
  const h = harness({ buy_enabled: false, sell_enabled: false });
  h.context.normalizeManagedBlockSettings(h.settings);
  h.context.addBlockExecutionFlow();
  const flow = h.settings.execution_flows[0];
  const definition = { id: "schedule", title: "체결 흐름", flowKind: "execution", flowId: flow.id };
  const inspector = h.context.createComposerInspector(definition, false);
  const remove = inspector.children[0].children.find(child => child.className?.includes("composer-inspector-remove-flow"));
  assert.equal(remove.attributes["aria-label"], "이 체결 흐름 삭제");
  h.setRunning(true);
  remove.click();
  assert.equal(h.settings.execution_flows.length, 1);
  h.setRunning(false);
  remove.click();
  assert.equal(h.settings.execution_flows.length, 0);
  assert.equal(h.context.state.blockSelectedNode, false);
  const reloaded = JSON.parse(JSON.stringify(h.settings));
  h.context.normalizeManagedBlockSettings(reloaded);
  assert.equal(reloaded.execution_flows.length, 0);
  assert.equal(reloaded.buy_flows.length, 0);
  assert.equal(reloaded.sell_flows.length, 0);
});

test("an unfinished field cannot leave the deleted flow's inspector stuck open", () => {
  for (const kind of ["buy", "sell", "execution"]) {
    const h = harness({});
    h.context.normalizeManagedBlockSettings(h.settings);
    if (kind === "execution") h.context.addBlockExecutionFlow();
    const flows = h.settings[`${kind}_flows`];
    const flow = flows[0];
    h.context.state.blockSelectedNode = `${h.context.blockFlowNodePrefix(kind, flow.id)}-schedule`;
    let settlementAttempts = 0;
    const control = {
      value: "-", _blockCommittedValue: "30", _blockPackageOwner: h.context.state.blockPackage,
      _commitBlockField() {
        assert.equal(flows.includes(flow), true, "Field edits must settle before their owner is deleted");
        settlementAttempts += 1;
        throw new Error("정수를 입력하세요");
      },
      setCustomValidity(value) { this.validationMessage = value; },
    };
    h.context.elements.blockCanvas.querySelectorAll = () => [control];
    h.context[`removeBlock${kind[0].toUpperCase() + kind.slice(1)}Flow`](flow.id);
    assert.equal(settlementAttempts, 1);
    assert.equal(control.value, "30");
    assert.equal(control.validationMessage, "");
    assert.equal(flows.length, 0);
    assert.equal(h.context.state.blockSelectedNode, false);
  }
});

test("enable flags do not surface as generic text-input settings", () => {
  const h = harness({ buy_enabled: false, sell_enabled: false, strategy_id: "sample" });
  const fields = h.context.blockPackageFields();
  assert.equal(fields.length, 1);
  assert.equal(fields[0].settingKey, "strategy_id");
});
