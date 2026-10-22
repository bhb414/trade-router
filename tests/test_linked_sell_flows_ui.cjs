const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../apps/studio/src/trade_router_studio/static/app.js"), "utf8");
function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, name);
  const end = source.slice(start + 1).search(/\n(?:(?:async )?function |const |let )/);
  return source.slice(start, end < 0 ? undefined : start + 1 + end);
}
class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.listeners = {}; this.textContent = ""; this.style = { setProperty() {} }; }
  append(...children) { this.children.push(...children); }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  setCustomValidity(message) { this.message = message; }
}
const plain = value => JSON.parse(JSON.stringify(value));
const text = element => element.textContent + element.children.map(text).join(" ");
function harness(settings = {}) {
  let running = false, renders = 0;
  const notices = [];
  const context = vm.createContext({
    structuredClone,
    document: { createElement: tag => new Element(tag) },
    state: { blockPackage: { settings }, conditionGroupSerial: 0, connections: [] },
    elements: { blockCanvas: { querySelectorAll: () => [] } },
    selectedState: () => ({ running }),
    flushBlockFieldEdits: () => true, settleBlockFieldEditsForRerender: () => {},
    setDirty: value => { context.state.dirty = value; },
    renderStrategyBlocks: () => { renders += 1; },
    setScheduleSectionExpanded: () => {},
    notify: message => notices.push(message),
    newConditionGroup: () => ({ conditions: [] }),
    newBlockRuleCondition: () => ({ kind: "condition", id: "test" }),
    newBlockRuleRoot: (kind, children) => ({ kind: "logic", children }),
    newBlockTradeCondition: () => ({}), normalizeConditionAtrPeriods: () => {}, ensureBlockRuleTree: () => {},
    packageFieldSourceName: key => key.toUpperCase(), genericConstantMeta: key => ["general", key],
    BLOCK_PACKAGE_FIELD_TYPES: { sell_scan_seconds: "number" },
    blockRuleTree: () => ({ children: [] }), blockRuleNodeCount: () => 0,
    buildComposerRuleBranch: () => ({ kind: "rule-node" }),
    blockRuleConditionSummary: condition => condition.label || "종가 > 100",
  });
  for (const name of ["BLOCK_BUY_FLOW_FIELDS", "BLOCK_SELL_FLOW_FIELDS"]) {
    vm.runInContext(source.match(new RegExp(`const ${name} = \\[[\\s\\S]*?\\];`))[0], context);
  }
  [
    "blockBuyFlows", "blockBuyFlow", "blockSellFlows", "blockSellFlow", "blockFlow", "blockFlowNodePrefix", "blockExecutionFlows",
    "blockBuyLinkNodes", "blockBuyLinkNodeLabel", "applyBlockSellLinks", "blockSellFlowFields", "blockSellTargetMode", "blockSellTargetSummary", "blockSellFlowVisibleFields", "blockSellFlowDescription",
    "addBlockSellFlow", "removeBlockBuyFlow", "normalizeManagedBlockSettings", "normalizeBlockPackageValue", "blockFieldOptions",
    "applyBlockFieldEdit", "createBlockFieldControl", "createBlockBuyFlowLinkControl", "createScheduleFields", "buildComposerTreeModel",
  ].forEach(name => vm.runInContext(functionSource(name), context));
  context.currentBlockModel = () => ({ fields: context.blockSellFlows().flatMap(context.blockSellFlowFields), schedules: [] });
  return { context, settings, notices, setRunning: value => { running = value; }, renders: () => renders };
}
const buy = (id, name = id) => ({ id, name, target_source: "SYMBOLS", symbols: ["005930"], buy_condition_groups: [] });
const sell = (id, ids = [], mode = "BUY_FLOWS") => ({ id, name: id, target_mode: mode, buy_flow_ids: ids, buy_node_refs: [], sell_scan_seconds: 30, operating_start_time: "08:00", operating_end_time: "20:00", sell_condition_groups: [] });
function field(h, id, key) { return h.context.blockSellFlowFields(h.settings.sell_flows.find(flow => flow.id === id)).find(item => item.settingKey === key); }

test("new sell flow has no guessed linkage and explicit operating hours; legacy saved flows retain their window", () => {
  const h = harness({ buy_flows: [buy("morning")], sell_flows: [] });
  h.context.addBlockSellFlow();
  const flow = h.settings.sell_flows[0];
  assert.equal(flow.target_mode, "BUY_FLOWS");
  assert.deepEqual(plain(flow.buy_flow_ids), []);
  assert.match(h.context.blockSellTargetSummary(flow), /미연결.*대기/);
  assert.equal(flow.operating_start_time, "08:00");
  assert.equal(flow.operating_end_time, "20:00");
  assert.equal(h.context.blockSellFlowVisibleFields(flow).some(item => item.settingKey === "sell_start_time"), false);
  const old = sell("old"); delete old.target_mode; delete old.buy_flow_ids;
  delete old.operating_start_time; delete old.operating_end_time;
  old.sell_start_time = "09:00"; old.sell_end_time = "15:20";
  const legacy = harness({ buy_flows: [buy("morning")], sell_flows: [old] });
  legacy.context.normalizeManagedBlockSettings(legacy.settings);
  assert.equal(old.target_mode, "POSITION_SCOPE");
  assert.equal(old.operating_start_time, "09:00");
  assert.equal(old.operating_end_time, "15:20");
  assert.equal("sell_start_time" in old, false);
  assert.equal("sell_end_time" in old, false);
  assert.deepEqual(plain(old.buy_flow_ids), []);
  assert.equal(legacy.context.blockSellTargetSummary(old), "이 전략 보유종목");
});

test("checkbox links real buy flow IDs, survives reload and renaming, and never selects every flow", () => {
  const h = harness({ buy_flows: [buy("morning", "장초반"), buy("intraday", "장중")], sell_flows: [sell("exit")] });
  const control = h.context.createBlockFieldControl(field(h, "exit", "buy_flow_ids"), false);
  const checkboxes = control.children[1].children.map(group => group.children[0].children[0]);
  assert.deepEqual(checkboxes.map(checkbox => checkbox.checked), [false, false]);
  checkboxes[0].checked = true; checkboxes[0].listeners.change();
  assert.deepEqual(plain(h.settings.sell_flows[0].buy_flow_ids), ["morning"]);
  assert.equal(h.context.state.dirty, true);
  const restored = plain(h.settings);
  restored.buy_flows[0].name = "변경한 이름";
  h.context.normalizeManagedBlockSettings(restored);
  assert.match(h.context.blockSellTargetSummary(restored.sell_flows[0], restored), /변경한 이름/);
  assert.deepEqual(restored.sell_flows[0].buy_flow_ids, ["morning"]);
});

test("multiple sell flows may select the same source; pending quantity protection is explained", () => {
  const h = harness({ buy_flows: [buy("morning"), buy("intraday")], sell_flows: [sell("stop", ["morning"]), sell("exit")] });
  const control = h.context.createBlockFieldControl(field(h, "exit", "buy_flow_ids"), false);
  const checkbox = control.children[1].children[0].children[0].children[0];
  assert.equal(checkbox.disabled, false);
  checkbox.checked = true; checkbox.listeners.change();
  assert.deepEqual(plain(h.settings.sell_flows[1].buy_flow_ids), ["morning"]);
  const selected = h.context.createBlockFieldControl(field(h, "exit", "buy_flow_ids"), false);
  assert.match(text(selected), /겹쳐도.*미체결.*중복 주문하지/);
});

test("running state rejects a stale checkbox event without changing targets", () => {
  const h = harness({ buy_flows: [buy("morning")], sell_flows: [sell("exit")] });
  const control = h.context.createBlockFieldControl(field(h, "exit", "buy_flow_ids"), false);
  h.setRunning(true);
  const checkbox = control.children[1].children[0].children[0].children[0];
  checkbox.checked = true; checkbox.listeners.change();
  h.context.applyBlockFieldEdit(field(h, "exit", "target_mode").id, "POSITION_SCOPE");
  assert.deepEqual(h.settings.sell_flows[0].buy_flow_ids, []);
  assert.equal(h.settings.sell_flows[0].target_mode, "BUY_FLOWS");
  assert.equal(h.renders(), 0);
});

test("deleting a buy flow leaves an explicit broken reference that can be unlinked instead of retargeting holdings", () => {
  const h = harness({ buy_flows: [buy("morning"), buy("intraday")], sell_flows: [sell("exit", ["morning"])] });
  h.context.removeBlockBuyFlow("morning");
  assert.deepEqual(h.settings.sell_flows[0].buy_flow_ids, ["morning"]);
  assert.match(h.context.blockSellTargetSummary(h.settings.sell_flows[0]), /삭제된 매수 흐름/);
  const control = h.context.createBlockFieldControl(field(h, "exit", "buy_flow_ids"), false);
  const missing = control.children[1].children.flatMap(group => group.children).find(option => /삭제된/.test(text(option)));
  assert.ok(missing);
  missing.children[0].checked = false; missing.children[0].listeners.change();
  assert.deepEqual(plain(h.settings.sell_flows[0].buy_flow_ids), []);
});

test("position target mode supports sell-only strategies with separate operating hours and no hidden buy linkage", () => {
  const h = harness({ buy_enabled: false, buy_flows: [], sell_flows: [sell("exit")], sell_position_scope: "ACCOUNT" });
  h.context.applyBlockFieldEdit(field(h, "exit", "target_mode").id, "POSITION_SCOPE");
  const flow = h.settings.sell_flows[0];
  const fields = h.context.blockSellFlowVisibleFields(flow);
  assert.equal(fields.some(item => ["sell_start_time", "sell_end_time", "buy_flow_ids"].includes(item.settingKey)), false);
  assert.deepEqual(plain(fields.filter(item => item.settingKey.startsWith("operating_")).map(item => item.settingKey)), ["operating_start_time", "operating_end_time"]);
  assert.equal(h.context.blockSellTargetSummary(flow), "계좌 전체 보유종목");
  const section = h.context.buildComposerTreeModel(h.settings).children.find(item => item.id === "sell-section");
  assert.equal(section.children[0].nodeId, "sellTargets");
  assert.equal(section.children[1].badge, "08:00–20:00");
});

test("linked-only canvas shows source names and does not imply account-wide target or time-based selling", () => {
  const h = harness({ buy_flows: [buy("morning", "장초반")], sell_flows: [sell("exit", ["morning"])], sell_position_scope: "ACCOUNT" });
  const section = h.context.buildComposerTreeModel(h.settings).children.find(item => item.id === "sell-section");
  assert.equal(section.children.some(item => item.nodeId === "sellTargets"), false);
  assert.equal(section.children[0].badge, "08:00–20:00");
  assert.doesNotMatch(section.description, /계좌 전체 보유종목/);
  assert.doesNotMatch(section.children.at(-1).label, /시간/);
  assert.match(h.context.blockSellFlowDescription(h.settings.sell_flows[0]), /실제로 체결된 수량만/);
});

const treeBuy = () => ({ ...buy("morning", "장초반"), buy_rule_tree: {
  id: "root", kind: "logic", children: [
    { id: "a", kind: "condition", condition: { label: "종가 > 100" } },
    { id: "group", kind: "logic", operator: "ANY", children: [
      { id: "b", kind: "condition", condition: { label: "ATR < 5" } },
      { id: "subgroup", kind: "logic", operator: "ALL", children: [
        { id: "c", kind: "condition", condition: { label: "OBV > 전봉" } },
      ] },
    ] },
  ],
} });

test("flow, individual IF and nested groups can be selected together and serialize in one render", () => {
  const h = harness({ buy_flows: [treeBuy(), buy("intraday", "장중")], sell_flows: [sell("exit", ["intraday"])] });
  const view = () => h.context.createBlockFieldControl(field(h, "exit", "buy_flow_ids"), false);
  let control = view();
  assert.match(text(control), /조건 묶음 · OR.*IF ATR < 5.*조건 묶음 · AND.*IF OBV > 전봉/);
  const option = (panel, match) => panel.children[1].children.flatMap(group => group.children).find(item => match.test(text(item).trim()));
  const select = match => {
    const checkbox = option(view(), match).children[0];
    checkbox.checked = true; checkbox.listeners.change();
  };
  select(/^IF 종가/);
  select(/^조건 묶음 · OR/);
  select(/^IF OBV/);
  const flow = h.settings.sell_flows[0];
  assert.deepEqual(plain(flow.buy_flow_ids), ["intraday"]);
  assert.deepEqual(plain(flow.buy_node_refs), [
    { flow_id: "morning", node_id: "a" },
    { flow_id: "morning", node_id: "group" },
    { flow_id: "morning", node_id: "c" },
  ]);
  assert.equal(h.renders(), 3);
  assert.match(text(option(view(), /^IF OBV/)), /상위 선택에 포함/);
  const restored = plain(h.settings);
  h.context.normalizeManagedBlockSettings(restored);
  assert.deepEqual(restored.sell_flows[0].buy_node_refs, plain(flow.buy_node_refs));
  assert.match(h.context.blockSellTargetSummary(flow), /장중 전체.*IF 종가.*조건 묶음 · OR.*IF OBV/);
});

test("selecting the entire flow does not discard existing child choices and explains overlap", () => {
  const flow = sell("exit"); flow.buy_node_refs = [{ flow_id: "morning", node_id: "a" }];
  const h = harness({ buy_flows: [treeBuy()], sell_flows: [flow] });
  const control = h.context.createBlockFieldControl(field(h, "exit", "buy_flow_ids"), false);
  const checkbox = control.children[1].children[0].children[0].children[0];
  checkbox.checked = true; checkbox.listeners.change();
  assert.deepEqual(plain(flow.buy_flow_ids), ["morning"]);
  assert.deepEqual(plain(flow.buy_node_refs), [{ flow_id: "morning", node_id: "a" }]);
  const selected = h.context.createBlockFieldControl(field(h, "exit", "buy_flow_ids"), false);
  assert.match(text(selected), /개별 조건 1개도 선택됨/);
  assert.match(text(selected), /겹쳐 선택한 체결분은 한 번만 계산/);
  assert.equal(h.context.blockSellTargetSummary(flow), "장초반 전체");
});

test("deleted node references remain visible and removable with the other chosen sources intact", () => {
  const flow = sell("exit", ["intraday"]);
  flow.buy_node_refs = [{ flow_id: "morning", node_id: "deleted" }];
  const h = harness({ buy_flows: [treeBuy(), buy("intraday")], sell_flows: [flow] });
  const control = h.context.createBlockFieldControl(field(h, "exit", "buy_flow_ids"), false);
  assert.match(h.context.blockSellTargetSummary(flow), /삭제된 조건/);
  const missing = control.children[1].children[0].children.find(option => /삭제된 조건/.test(text(option)));
  missing.children[0].checked = false; missing.children[0].listeners.change();
  assert.deepEqual(plain(flow.buy_node_refs), []);
  assert.deepEqual(plain(flow.buy_flow_ids), ["intraday"]);
});

test("the atomic selection editor rejects malformed references without partially changing the flow", () => {
  const h = harness({ buy_flows: [treeBuy()], sell_flows: [sell("exit")] });
  assert.throws(() => h.context.applyBlockSellLinks("exit", ["morning"], [{ flow_id: "morning" }]), /매수 조건/);
  assert.deepEqual(h.settings.sell_flows[0].buy_flow_ids, []);
  assert.deepEqual(h.settings.sell_flows[0].buy_node_refs, []);
  assert.equal(h.renders(), 0);
});

test("a detached checkbox cannot change the next strategy when its flow ID is reused", () => {
  const h = harness({ buy_flows: [treeBuy()], sell_flows: [sell("exit")] });
  const control = h.context.createBlockFieldControl(field(h, "exit", "buy_flow_ids"), false);
  const replacement = { buy_flows: [treeBuy()], sell_flows: [sell("exit")] };
  h.context.state.blockPackage = { settings: replacement };
  const checkbox = control.children[1].children[0].children[0].children[0];
  checkbox.checked = true; checkbox.listeners.change();
  assert.deepEqual(replacement.sell_flows[0].buy_flow_ids, []);
  assert.equal(h.renders(), 0);
});

test("legacy regular and afterhours exits preserve independent second-resolution windows, trees and orders", () => {
  const regular = { ...sell("regular"), target_mode: "LEGACY_TIME", sell_start_time: "09:00", sell_end_time: "15:20:00" };
  const after = { ...sell("after"), sell_start_time: "16:00:00", sell_end_time: "19:59:00" };
  delete after.target_mode;
  for (const flow of [regular, after]) {
    delete flow.operating_start_time; delete flow.operating_end_time;
    flow.sell_rule_tree = { kind: "logic", id: `${flow.id}-root`, operator: "ANY", children: [{ kind: "condition", id: `${flow.id}-stop`, condition: { left: "profit_rate", operator: "LTE", value: "-3" } }], action: { order_type: flow.id === "regular" ? "MARKET" : "BEST_LIMIT", quantity_percent: "100" } };
  }
  const trees = [regular, after].map(flow => plain(flow.sell_rule_tree));
  const h = harness({ buy_enabled: false, sell_position_scope: "ACCOUNT", sell_flows: [regular, after] });
  h.context.normalizeManagedBlockSettings(h.settings);
  assert.deepEqual([regular, after].map(flow => [flow.target_mode, flow.operating_start_time, flow.operating_end_time]), [
    ["POSITION_SCOPE", "09:00", "15:20:00"], ["POSITION_SCOPE", "16:00:00", "19:59:00"],
  ]);
  assert.equal(h.settings.sell_position_scope, "ACCOUNT");
  assert.deepEqual([regular, after].map(flow => plain(flow.sell_rule_tree)), trees);
  const reloaded = plain(h.settings);
  h.context.normalizeManagedBlockSettings(reloaded);
  assert.deepEqual(plain(reloaded), plain(h.settings));
});

test("pre-flow imports preserve their top-level window without creating linked ownership", () => {
  const h = harness({ buy_enabled: false, sell_start_time: "10:12:13", sell_end_time: "18:19:20", sell_condition_groups: [] });
  h.context.normalizeManagedBlockSettings(h.settings);
  const flow = h.settings.sell_flows[0];
  assert.equal(flow.target_mode, "POSITION_SCOPE");
  assert.equal(flow.operating_start_time, "10:12:13");
  assert.equal(flow.operating_end_time, "18:19:20");
  assert.deepEqual(plain(flow.buy_flow_ids), []);
  assert.deepEqual(plain(flow.buy_node_refs), []);
});

test("an explicit legacy flow without its own times uses the old flow defaults, not unrelated parent hours", () => {
  for (const target_mode of [undefined, "LEGACY_TIME"]) {
    const flow = { id: "legacy", target_mode, sell_condition_groups: [] };
    const h = harness({ buy_enabled: false, sell_start_time: "16:00", sell_end_time: "20:00", sell_flows: [flow] });
    h.context.normalizeManagedBlockSettings(h.settings);
    assert.equal(flow.target_mode, "POSITION_SCOPE");
    assert.equal(flow.operating_start_time, "09:00");
    assert.equal(flow.operating_end_time, "15:20");
  }
});

test("v0.1.71 nonlegacy exits ignore stale old windows during migration and preserve links", () => {
  for (const mode of ["BUY_FLOWS", "POSITION_SCOPE"]) {
    const flow = sell("exit", ["morning"], mode);
    delete flow.operating_start_time; delete flow.operating_end_time;
    flow.sell_start_time = "09:00"; flow.sell_end_time = "15:20";
    flow.buy_node_refs = [{ flow_id: "morning", node_id: "deleted-but-retained" }];
    const h = harness({ buy_flows: [buy("morning")], sell_flows: [flow] });
    h.context.normalizeManagedBlockSettings(h.settings);
    assert.equal(flow.target_mode, mode);
    assert.equal(flow.operating_start_time, "08:00");
    assert.equal(flow.operating_end_time, "20:00");
    assert.equal("sell_start_time" in flow, false);
    assert.deepEqual(flow.buy_flow_ids, ["morning"]);
    assert.deepEqual(flow.buy_node_refs, [{ flow_id: "morning", node_id: "deleted-but-retained" }]);
  }
});

test("operating hour input edits survive target changes, save and reload in both modes", () => {
  for (const mode of ["BUY_FLOWS", "POSITION_SCOPE"]) {
    const h = harness({ buy_flows: [buy("morning")], sell_flows: [sell("exit", ["morning"], mode)] });
    const start = h.context.createBlockFieldControl(field(h, "exit", "operating_start_time"), false).children[1];
    assert.equal(start.type, "time");
    assert.equal(start.step, "1");
    start.value = "10:05:30"; start.listeners.input();
    h.context.applyBlockFieldEdit(field(h, "exit", "operating_end_time").id, "18:00:01");
    h.context.applyBlockFieldEdit(field(h, "exit", "target_mode").id, mode === "BUY_FLOWS" ? "POSITION_SCOPE" : "BUY_FLOWS");
    const reloaded = plain(h.settings);
    h.context.normalizeManagedBlockSettings(reloaded);
    assert.equal(reloaded.sell_flows[0].operating_start_time, "10:05:30");
    assert.equal(reloaded.sell_flows[0].operating_end_time, "18:00:01");
    assert.deepEqual(reloaded.sell_flows[0].buy_flow_ids, ["morning"]);
    assert.equal("sell_start_time" in reloaded.sell_flows[0], false);
    const panel = h.context.createScheduleFields(h.context.blockSellFlowVisibleFields(h.settings.sell_flows[0]), false);
    const operating = panel.children.find(child => child.className === "block-sell-operating-hours");
    assert.ok(operating);
    assert.match(text(operating), /작동 시간.*한국 시각.*시작 포함, 종료 제외.*실시간 시세와 봉 조건 판단.*작동 시작.*작동 종료/);
    assert.equal(operating.children.filter(child => child.className === "strategy-block-field").length, 2);
  }
});

test("invalid time or removed target mode cannot change configuration, and running strategy locks time controls", () => {
  const h = harness({ sell_flows: [sell("exit")] });
  for (const value of ["", "25:00", "09:60", "9:00", "09:00:60"]) {
    assert.throws(() => h.context.applyBlockFieldEdit(field(h, "exit", "operating_start_time").id, value), /시:분/);
    assert.equal(h.settings.sell_flows[0].operating_start_time, "08:00");
  }
  assert.throws(() => h.context.applyBlockFieldEdit(field(h, "exit", "target_mode").id, "LEGACY_TIME"), /대상 방식/);
  assert.deepEqual(plain(h.context.blockFieldOptions(field(h, "exit", "target_mode"))).map(option => option.value), ["BUY_FLOWS", "POSITION_SCOPE"]);
  const control = h.context.createBlockFieldControl(field(h, "exit", "operating_start_time"), true).children[1];
  assert.equal(control.disabled, true);
  h.setRunning(true);
  h.context.applyBlockFieldEdit(field(h, "exit", "operating_start_time").id, "10:00");
  assert.equal(h.settings.sell_flows[0].operating_start_time, "08:00");
});
