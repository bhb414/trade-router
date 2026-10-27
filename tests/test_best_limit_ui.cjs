/* Run with node --test tests/test_best_limit_ui.cjs. */
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
  const remaining = source.slice(start);
  const next = remaining.slice(1).search(/\n(?:async )?function /);
  return next < 0 ? remaining : remaining.slice(0, next + 1);
};
const plain = value => JSON.parse(JSON.stringify(value));
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.listeners = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
}
function harness(kind = "buy") {
  const root = {
    kind: "logic", id: "root", children: [], operator: "ALL",
    action: { kind: "order", id: "order", side: kind === "sell" ? "SELL" : "BUY", order_type: "MARKET" },
  };
  let commits = 0;
  const context = vm.createContext({
    structuredClone,
    document: { createElement: tag => new Element(tag) },
    elements: { orders: new Element("div") },
    state: { conditionGroupSerial: 0, blockPackage: { settings: {
      buy_order_type: "BEST_LIMIT", sell_order_type: "BEST_LIMIT",
    } } },
    findBlockRuleNode: () => ({ node: root }),
    commitBlockRuleMutation: () => { commits += 1; },
    formulaControl: (label, control) => ({ label, control }),
    conditionSelect: (options, value, disabled, onChange) => ({ options, value, disabled, onChange }),
    ruleComposerNodeId: () => "rule",
    ruleComposerActionId: () => "action",
    dataTable: (columns, rows) => columns.find(column => column.label === "주문").render(rows[0]),
    formatNumber: value => String(value),
  });
  [
    "orderTypeLabel", "blockOrderTypeOptions", "blockOrderTypeHint", "blockFieldOptions",
    "createBlockRuleActionEditor", "updateBlockRuleAction", "newBlockOrderAction", "nextBlockRuleId",
    "blockSellFlows", "blockSellFlow", "blockSellTargetMode", "collectComposerRuleDefinitions", "walkBlockRule", "blockRuleConditionNodes",
    "tradeHistoryTable", "renderOrders", "orderStatusPresentation",
  ].forEach(name => vm.runInContext(functionSource(name), context));
  return { context, root, commits: () => commits };
}

test("buy and sell actions retain the selected BEST_LIMIT value and its explanation", () => {
  for (const kind of ["buy", "sell"]) {
    const { context, root, commits } = harness(kind);
    const choices = context.blockFieldOptions({ settingKey: `${kind}_order_type` });
    assert.equal(choices.find(option => option.value === "BEST_LIMIT").label, "최유리지정가");
    const editor = context.createBlockRuleActionEditor({ kind, flowId: null, nodeId: "root" }, false);
    const selector = editor.children[1].control;
    const hint = editor.children[2];
    assert.equal(selector.options.find(option => option[0] === "BEST_LIMIT")[1], "최유리지정가");
    assert.equal(hint.hidden, true);
    selector.onChange("BEST_LIMIT", false);
    assert.equal(root.action.order_type, "BEST_LIMIT");
    assert.equal(commits(), 1);
    assert.equal(hint.hidden, false);
    assert.match(hint.textContent, /최저 매도호가.*최고 매수호가/);
    assert.match(hint.textContent, /일부만 체결.*미체결/);
    assert.match(hint.textContent, /15:30~15:40/);
    const reopened = context.createBlockRuleActionEditor({ kind, flowId: null, nodeId: "root" }, false);
    assert.equal(reopened.children[1].control.value, "BEST_LIMIT");
    context.updateBlockRuleAction(kind, null, "root", "UNSUPPORTED");
    assert.equal(root.action.order_type, "BEST_LIMIT");
    assert.equal(commits(), 1);
    selector.onChange("LIMIT", false);
    assert.equal(root.action.order_type, "LIMIT");
    assert.equal(hint.hidden, true);
  }
});

test("new actions inherit BEST_LIMIT and composer cards name it correctly after serialization", () => {
  for (const kind of ["buy", "sell"]) {
    const { context, root } = harness(kind);
    root.action = context.newBlockOrderAction(kind);
    assert.equal(root.action.order_type, "BEST_LIMIT");
    assert.equal(root.action.side, kind === "buy" ? "BUY" : "SELL");
    const definitions = [];
    context.collectComposerRuleDefinitions(definitions, kind, null, plain(root));
    const actionCard = definitions.find(node => node.ruleActionOwner);
    assert.equal(actionCard.meta, "최유리지정가");
    assert.deepEqual(plain(actionCard.summary), kind === "sell"
      ? ["최유리지정가 주문", "작동 시간 내 조건 없이 매도"]
      : ["최유리지정가 주문"]);
    assert.equal(actionCard.ruleActionOwner.order_type, "BEST_LIMIT");
  }
});

test("both order history presentations distinguish BEST_LIMIT from market and fixed-price orders", () => {
  const { context } = harness();
  assert.equal(context.tradeHistoryTable([{ order_type: "BEST_LIMIT", quantity: 7 }]), "최유리지정가 7주");
  context.renderOrders([
    { status: "ACKNOWLEDGED", account_alias: "계좌A", request: { order_type: "BEST_LIMIT", route: "NXT", quantity: 7 } },
    { status: "ACKNOWLEDGED", account_alias: "계좌A", request: { order_type: "LIMIT", route: "NXT", price: 12300, quantity: 7 } },
    { status: "ACKNOWLEDGED", account_alias: "계좌A", request: { order_type: "MARKET", route: "KRX", quantity: 7 } },
  ]);
  const labels = context.elements.orders.children.map(item => item.children[1].children[0].textContent);
  assert.deepEqual(labels, [
    "최유리지정가 · NXT · 계좌 계좌A",
    "지정가 12300원 · NXT · 계좌 계좌A",
    "시장가 · KRX · 계좌 계좌A",
  ]);
  assert.equal(context.orderTypeLabel("OTHER"), "OTHER");
  assert.equal(context.orderTypeLabel(null), "주문 방식 미확인");
});
