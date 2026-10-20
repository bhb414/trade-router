const test = require("node:test");
const assert = require("node:assert/strict");
const Formula = require("../apps/studio/src/trade_router_studio/static/condition-formula.js");

const metric = (name, extra = {}) => ({
  kind: "metric", metric: name, bar_interval: "3m", bar_data_mode: "COMPLETE_ONLY", offset: 0, ...extra,
});
const units = (...values) => values.flatMap(value => typeof value === "string" ? Array.from(value) : [value]);
const constant = value => ({ kind: "constant", value: String(value) });

test("numbers, multiplication precedence and a single comparison become the existing AST", () => {
  const result = Formula.parse(units("2 + 3 * 4 >= 10"));
  assert.equal(result.operator, "GTE");
  assert.deepEqual(result.right_expression, constant(10));
  assert.equal(result.left_expression.operator, "ADD");
  assert.equal(result.left_expression.right.operator, "MULTIPLY");
});

test("typed parentheses alter precedence and division/subtraction remain left associative", () => {
  const result = Formula.parse(units("(12 - 2) / 5 / 2 = 1"));
  assert.equal(result.left_expression.operator, "DIVIDE");
  assert.equal(result.left_expression.left.operator, "DIVIDE");
  assert.equal(result.left_expression.left.left.operator, "SUBTRACT");
});

test("ATR volatility formula retains individual data parameters", () => {
  const atr = metric("atr", { atr_period: 14 });
  const close = metric("close", { bar_interval: "1m", offset: 3 });
  const result = Formula.parse(units("(", atr, " / ", close, ") * 100 > 3"));
  assert.deepEqual(result.left_expression.left.left, atr);
  assert.deepEqual(result.left_expression.left.right, close);
  assert.equal(result.left_expression.operator, "MULTIPLY");
});

test("negative and fractional constants can be typed directly", () => {
  const result = Formula.parse(units(metric("profit_rate"), " <= -2.3"));
  assert.deepEqual(result.right_expression, constant("-2.3"));
  assert.deepEqual(Formula.parse(units(".5 > -.25")).left_expression, constant(".5"));
});

test("unary minus before data is represented safely as zero minus data", () => {
  assert.equal(Formula.parse(units("-", metric("atr"), " < 0")).left_expression.operator, "SUBTRACT");
});

test("an unfinished draft does not manufacture zero or a comparator", () => {
  for (const text of ["", "2 +", "(2 + 3", "2 < ", "2 3 < 4", "2 + 3"]) {
    assert.throws(() => Formula.parse(units(text)));
  }
});

test("a second comparison and arbitrary executable text are rejected", () => {
  for (const text of ["1 < 2 < 3", "1 != 2", "alert(1) > 0", "Math.random() > 0", "1;2 > 0"]) {
    assert.throws(() => Formula.parse(units(text)));
  }
});

test("OBV direct comparison needs same series and different offsets", () => {
  const first = metric("obv");
  const before = metric("obv", { offset: 10 });
  assert.equal(Formula.parse(units(first, " > ", before)).operator, "GT");
  assert.throws(() => Formula.parse(units(first, " > ", first)), /봉 위치/);
  assert.throws(() => Formula.parse(units(first, " > ", metric("obv", { offset: 10, bar_interval: "1m" }))), /봉 주기/);
  assert.throws(() => Formula.parse(units(first, " + 1 > ", before)), /직접 비교/);
  assert.throws(() => Formula.parse(units(first, " > 0")), /직접 비교/);
});

test("cross comparisons keep backend same-series and live-profit constraints", () => {
  assert.equal(Formula.parse(units(metric("close"), "상향돌파", metric("bollinger_middle"))).operator, "CROSS_UP");
  assert.throws(() => Formula.parse(units(metric("close"), "하향돌파", metric("close", { bar_interval: "1m" }))), /같은 봉/);
  assert.throws(() => Formula.parse(units(metric("profit_rate"), "상향돌파3")), /수익률/);
  assert.throws(() => Formula.parse(units("2상향돌파3")), /같은 봉/);
});

test("formula serialization roundtrips AST without losing metric settings", () => {
  const original = Formula.parse(units("(", metric("atr", { atr_period: 20 }), " / ", metric("close"), ") * 100 < 5"));
  assert.deepEqual(Formula.parse(Formula.fromCondition(original)), original);
});

test("ATR serialization uses no redundant parentheses around a left-associative chain", () => {
  const atr = metric("atr", { atr_period: 14 });
  const close = metric("close");
  const original = Formula.parse(units("(", atr, " / ", close, ") * 100 < 3"));
  assert.deepEqual(Formula.fromCondition(original), units(atr, " ÷ ", close, " × 100 < 3"));
  assert.deepEqual(Formula.parse(Formula.fromCondition(original)), original);
});

test("serializer preserves necessary precedence and exact arithmetic subtree ordering", () => {
  const cases = [
    ["(1 + 2) * 3 > 0", "(1 + 2) × 3 > 0"],
    ["1 + 2 * 3 > 0", "1 + 2 × 3 > 0"],
    ["1 - (2 - 3) > 0", "1 − (2 − 3) > 0"],
    ["1 / (2 / 3) > 0", "1 ÷ (2 ÷ 3) > 0"],
    ["1 + (2 + 3) > 0", "1 + (2 + 3) > 0"],
    ["1 * (2 * 3) > 0", "1 × (2 × 3) > 0"],
    ["1 * (2 / 3) > 0", "1 × (2 ÷ 3) > 0"],
    ["(1 - 2) + 3 > 0", "1 − 2 + 3 > 0"],
    ["(1 / 2) * 3 > 0", "1 ÷ 2 × 3 > 0"],
    ["1 / (2 + 3) > 0", "1 ÷ (2 + 3) > 0"],
  ];
  for (const [input, displayed] of cases) {
    const original = Formula.parse(units(input));
    const serialized = Formula.fromCondition(original);
    assert.equal(serialized.join(""), displayed);
    assert.deepEqual(Formula.parse(serialized), original);
  }
});

test("insert at a mouse caret position adds only the requested text", () => {
  const original = units(metric("atr"), " > 3");
  const edit = Formula.replaceRange(original, 1, 1, " / 100");
  assert.deepEqual(edit.units, units(metric("atr"), " / 100 > 3"));
  assert.equal(edit.caret, 7);
  assert.deepEqual(original, units(metric("atr"), " > 3"));
});

test("data chips replace selected text atomically, preserving the suffix", () => {
  const original = units("12 > 0");
  const edit = Formula.replaceRange(original, 0, 2, [metric("close")]);
  assert.deepEqual(edit.units, units(metric("close"), " > 0"));
  assert.equal(edit.caret, 1);
});

test("backward selection replacement and chip deletion use the same caret model", () => {
  const original = units("(", metric("atr"), ") > 1");
  assert.deepEqual(Formula.replaceRange(original, 2, 1, "").units, units("() > 1"));
  assert.deepEqual(Formula.replaceRange(original, 3, 0, "25").units, units("25 > 1"));
});

test("incomplete draft units are recoverable and successful edits leave source AST untouched", () => {
  const original = Formula.parse(units(metric("close"), " > 10"));
  const draft = Formula.fromCondition(original);
  const edit = Formula.replaceRange(draft, draft.length, draft.length, "+");
  assert.throws(() => Formula.parse(edit.units));
  const fixed = Formula.replaceRange(edit.units, edit.caret, edit.caret, "2");
  assert.equal(Formula.parse(fixed.units).right_expression.operator, "ADD");
  assert.deepEqual(original.right_expression, constant("10"));
});

test("backend expression depth and finite constant limits are enforced", () => {
  assert.throws(() => Formula.parse(units("1 + 2 + 3 + 4 + 5 + 6 > 0")), /4단계/);
  assert.throws(() => Formula.parse(units("1e999 > 0")), /유한/);
  assert.throws(() => Formula.parse(units("1e19 > 0")), /절댓값/);
  assert.equal(Formula.parse(units("-1e18 < 1e18")).operator, "LT");
});
