const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../apps/studio/src/trade_router_studio/static/app.js"), "utf8");
function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1);
  const end = source.slice(start + 1).search(/\n(?:(?:async )?function |const |let )/);
  return source.slice(start, end < 0 ? undefined : start + 1 + end);
}
class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.textContent = ""; }
  append(...children) { this.children.push(...children); }
}
function textOf(element) { return element.textContent + element.children.map(textOf).join(" "); }
function harness() {
  const context = vm.createContext({
    structuredClone,
    document: { createElement: tag => new Element(tag) },
    conditionSelect: (options, value, disabled) => Object.assign(new Element("select"), { options, value, disabled }),
    conditionSideExpression: (condition, side) => condition[`${side}_expression`] || null,
    expressionControlScope: () => "", expressionControlKey: () => "",
    appendAdvancedExpressionAction: () => {},
    isLiveProfitCondition: condition => condition.left === "profit_rate" && condition.right === "value",
    walkConditionExpression: (expression, visitor) => {
      visitor(expression);
      if (expression.left) context.walkConditionExpression(expression.left, visitor);
      if (expression.right) context.walkConditionExpression(expression.right, visitor);
    },
  });
  for (const name of ["BLOCK_CONDITION_METRICS", "BLOCK_EXPRESSION_OPERATORS"]) {
    const match = source.match(new RegExp(`const ${name} = \\[[\\s\\S]*?\\];`));
    assert.ok(match);
    vm.runInContext(match[0], context);
  }
  [
    "blockProfitRateHint", "formulaMetricTokenLabel", "conditionExpressionSummary", "formulaExpressionSummary",
    "formulaControl", "createCompactFormulaTokenEditor", "createConditionOperandEditor", "blockConditionTiming",
  ].forEach(name => vm.runInContext(functionSource(name), context));
  return context;
}

test("existing profit_rate expressions display net profit consistently without changing their saved metric", () => {
  const h = harness();
  const expression = { kind: "metric", metric: "profit_rate", bar_interval: "3m", offset: 4 };
  const before = structuredClone(expression);
  for (const label of [h.formulaMetricTokenLabel(expression), h.conditionExpressionSummary(expression), h.formulaExpressionSummary(expression)]) {
    assert.equal(label, "실시간 순수익률(비용 포함 %)");
    assert.doesNotMatch(label, /봉|평균가 대비/);
  }
  assert.deepEqual(expression, before);
});

test("the formula data editor explains account fees, tax, estimation and missing-cost behavior", () => {
  const h = harness();
  const panel = h.createCompactFormulaTokenEditor("sell", { kind: "metric", metric: "profit_rate" }, false, () => {});
  const visible = textOf(panel);
  for (const expected of [/추정 순수익률/, /계좌 평균 매수가/, /매수·매도 수수료율/, /매도세율/, /비워 둔 비용 항목/, /각각 0으로 계산/, /입력한 수수료·세율만 반영/]) {
    assert.match(visible, expected);
  }
  assert.doesNotMatch(visible, /수수료·세금은 포함하지|봉 주기|봉 상태/);
  assert.equal(panel.children[1].children[0].children[1].options.find(item => item[0] === "profit_rate")[1], "실시간 순수익률(비용 포함 %)");
});

test("legacy operand editor uses the same net-profit explanation and remains independent of candle parameters", () => {
  const h = harness();
  const panel = h.createConditionOperandEditor("sell", 0, 0, "right", { right: "profit_rate" }, false);
  assert.match(textOf(panel), /실시간 순수익률/);
  assert.match(textOf(panel), /추정 순수익률/);
  assert.match(textOf(panel), /각각 0으로 계산/);
  assert.doesNotMatch(textOf(panel), /판단을 보류/);
  assert.equal(panel.children.some(child => child.tagName === "INPUT" || child.tagName === "SELECT"), false);
});

test("timing summaries mark both direct and arithmetic profit conditions as live net profit", () => {
  const h = harness();
  assert.equal(h.blockConditionTiming({ left: "profit_rate", right: "value" }), "실시간 순수익률 · 비용 포함");
  assert.equal(h.blockConditionTiming({ left_expression: {
    kind: "binary", operator: "ADD", left: { kind: "metric", metric: "profit_rate" }, right: { kind: "constant", value: "1" },
  } }), "실시간 순수익률 · 비용 포함");
});

test("average-price data is account average price in the compact editor", () => {
  const h = harness();
  const panel = h.createCompactFormulaTokenEditor("sell", { kind: "metric", metric: "average_price" }, false, () => {});
  assert.match(textOf(panel), /계좌의 평균 매수가/);
  assert.doesNotMatch(textOf(panel), /전략이 추적하는/);
});
