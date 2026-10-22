/* Run with node --test tests/test_sell_position_scope.cjs. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(
  path.join(__dirname, "../apps/studio/src/trade_router_studio/static/app.js"), "utf8",
);
function functionSource(name) {
  let start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `Missing ${name}`);
  if (source.slice(start - 6, start) === "async ") start -= 6;
  const end = source.slice(start + 1).search(/\n(?:(?:async )?function |const |let )/);
  return source.slice(start, end < 0 ? undefined : start + 1 + end);
}

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.listeners = {};
  }
  append(...children) { this.children.push(...children); }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  setCustomValidity(value) { this.validityMessage = value; }
}

function harness(settings = {}) {
  let running = false;
  let renders = 0;
  const context = vm.createContext({
    structuredClone,
    document: { createElement: tag => new Element(tag) },
    state: { blockPackage: { template: "condition-bollinger-v1", settings }, connections: [], conditionGroupSerial: 0 },
    elements: { editor: { value: "" } },
    selectedState: () => ({ running }),
    parseStrategyBlocks: () => ({ fields: [], schedules: [], template: "condition-bollinger-v1" }),
    blockBuyFlowFields: () => [], blockSellFlowFields: () => [], blockExecutionFlowFields: () => [],
    newBlockTradeCondition: () => ({ left: "close", right: "value", value: "0" }),
    normalizeConditionAtrPeriods: () => {}, ensureBlockRuleTree: () => {},
    setDirty: dirty => { context.state.dirty = dirty; },
    renderStrategyBlocks: () => { renders += 1; }, notify: () => {},
    genericConstantMeta: name => ["general", name],
    packageFieldSourceName: name => name.toUpperCase(), BLOCK_PACKAGE_FIELD_TYPES: {},
    blockRuleTree: () => ({ kind: "logic", id: "root", children: [] }),
    blockRuleNodeCount: () => 1, buildComposerRuleBranch: () => ({ kind: "rule-node", children: [] }),
  });
  for (const name of ["BLOCK_BUY_FLOW_FIELDS", "BLOCK_SELL_FLOW_FIELDS"]) {
    const match = source.match(new RegExp(`const ${name} = \\[[\\s\\S]*?\\];`));
    assert.ok(match);
    vm.runInContext(match[0], context);
  }
  [
    "blockBuyFlows", "blockSellFlows", "blockExecutionFlows", "blockFlowNodePrefix", "blockPackageFields",
    "blockSellPositionScopeField", "blockSellPositionScopeDefinition", "blockFieldOptions",
    "normalizeBlockPackageValue", "applyBlockFieldEdit", "createBlockFieldControl", "currentBlockModel",
    "normalizeManagedBlockSettings", "blockSellTargetMode", "blockSellTargetSummary", "buildComposerTreeModel", "loadBlockEditorData",
  ].forEach(name => vm.runInContext(functionSource(name), context));
  return { context, settings, setRunning: value => { running = value; }, renders: () => renders };
}

test("legacy strategies default to strategy holdings; account selection survives normalization and reload", () => {
  for (const initial of [undefined, "STRATEGY", "ACCOUNT"]) {
    const settings = { buy_enabled: false, sell_enabled: false };
    if (initial) settings.sell_position_scope = initial;
    const h = harness(settings);
    h.context.normalizeManagedBlockSettings(settings);
    assert.equal(settings.sell_position_scope, initial || "STRATEGY");
    const restored = JSON.parse(JSON.stringify(settings));
    h.context.normalizeManagedBlockSettings(restored);
    assert.equal(restored.sell_position_scope, initial || "STRATEGY");
    assert.equal(restored.sell_flows.length, 0);
    assert.equal(restored.buy_flows.length, 0);
  }
});

test("scope is a single dedicated field instead of a duplicate generic field", () => {
  const h = harness({ sell_position_scope: "ACCOUNT", strategy_id: "test" });
  assert.equal(h.context.blockPackageFields().some(field => field.settingKey === "sell_position_scope"), false);
  const fields = h.context.currentBlockModel().fields.filter(field => field.settingKey === "sell_position_scope");
  assert.equal(fields.length, 1);
  assert.equal(fields[0].value, "ACCOUNT");
  assert.equal(fields[0].label, "매도 대상");
});

test("loading an unrelated observer template does not introduce trading settings or a sell selector", async () => {
  const h = harness({});
  const settings = { strategy_id: "observer", symbols: ["005930"], interval: "3m" };
  const before = structuredClone(settings);
  h.context.api = async url => url.endsWith("/block-package")
    ? { template: "bar-observer-v1", settings } : [];
  h.context.resetConditionFormulaDrafts = () => {};
  h.context.prepareBlockVisualLayout = () => {};
  await h.context.loadBlockEditorData("observer.py");
  assert.deepEqual(settings, before);
  assert.equal(h.context.currentBlockModel().fields.some(field => field.settingKey === "sell_position_scope"), false);
});

test("sell target appears once before all sell flows and never under buy or execution", () => {
  const h = harness({
    sell_position_scope: "ACCOUNT",
    buy_flows: [{ id: "buy" }], sell_flows: [{ id: "a" }, { id: "b" }],
    execution_flows: [{ id: "exec" }],
  });
  const model = h.context.buildComposerTreeModel(h.settings);
  const sell = model.children.find(section => section.id === "sell-section");
  assert.equal(sell.children[0].nodeId, "sellTargets");
  assert.equal(sell.children.filter(item => item.nodeId === "sellTargets").length, 1);
  assert.match(sell.description, /계좌 전체 보유종목/);
  const buy = model.children.find(section => section.id === "buy-section");
  const execution = model.children.find(section => section.id === "execution-section");
  assert.equal(buy.children.some(item => item.nodeId === "sellTargets"), false);
  assert.equal(execution.children.some(item => item.nodeId === "sellTargets"), false);
  assert.match(execution.description, /이 전략의 미체결 주문/);
});

test("an absent sell flow keeps its add-only empty state even with account scope saved", () => {
  const h = harness({ sell_position_scope: "ACCOUNT", sell_enabled: false, sell_flows: [] });
  const sell = h.context.buildComposerTreeModel(h.settings).children.find(section => section.id === "sell-section");
  assert.equal(sell.children.length, 1);
  assert.equal(sell.children[0].action, "add-sell-flow");
  assert.match(sell.description, /자동 매도하지 않음/);
});

test("account target explains manual and other strategy holdings, average cost, and sellable quantity", () => {
  const h = harness({ sell_position_scope: "ACCOUNT" });
  const definition = h.context.blockSellPositionScopeDefinition(h.settings);
  assert.equal(definition.summary[0], "계좌 전체 보유종목");
  assert.match(definition.description, /수동 매수와 다른 전략/);
  assert.match(definition.description, /계좌 평균 매입가/);
  assert.match(definition.description, /매도 가능한 수량/);
  h.settings.sell_position_scope = "STRATEGY";
  const owned = h.context.blockSellPositionScopeDefinition(h.settings);
  assert.equal(owned.summary[0], "이 전략 보유종목");
  assert.match(owned.description, /계좌 평균 매입가/);
  assert.match(owned.description, /매도 수량은 이 전략 보유분으로 제한/);
  assert.equal(owned.actions.includes("계좌 평균 매입가"), true);
  assert.equal(owned.actions.includes("이 전략 평균 매입가"), false);
});

test("dropdown changes only shared sell scope, preserving all flows and marking the package dirty", () => {
  const h = harness({
    sell_position_scope: "STRATEGY", buy_flows: [{ id: "buy" }],
    sell_flows: [{ id: "sell" }], execution_flows: [{ id: "exec" }],
    total_operating_budget: "150000",
  });
  const before = structuredClone(h.settings);
  const control = h.context.createBlockFieldControl(h.context.blockSellPositionScopeField(), false).children[1];
  assert.equal(control.tagName, "SELECT");
  assert.deepEqual(control.children.map(option => option.textContent), ["이 전략 보유종목", "계좌 전체 보유종목"]);
  control.value = "ACCOUNT";
  control.listeners.change();
  assert.equal(h.settings.sell_position_scope, "ACCOUNT");
  assert.deepEqual(h.settings, { ...before, sell_position_scope: "ACCOUNT" });
  assert.equal(h.context.state.dirty, true);
  assert.equal(h.renders(), 1);
});

test("running strategy scope remains immutable even if a stale change event is delivered", () => {
  const h = harness({ sell_position_scope: "STRATEGY" });
  h.setRunning(true);
  const control = h.context.createBlockFieldControl(h.context.blockSellPositionScopeField(), true).children[1];
  assert.equal(control.disabled, true);
  control.value = "ACCOUNT";
  control.listeners.change();
  assert.equal(h.settings.sell_position_scope, "STRATEGY");
  assert.equal(h.renders(), 0);
});

test("unknown scope is rejected rather than broadening the sell target", () => {
  const h = harness({ sell_position_scope: "STRATEGY" });
  assert.throws(() => h.context.applyBlockFieldEdit("setting:sell_position_scope", "ALL"), /매도 대상/);
  assert.equal(h.settings.sell_position_scope, "STRATEGY");
});
