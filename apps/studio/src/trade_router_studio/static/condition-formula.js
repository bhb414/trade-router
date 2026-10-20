(function (scope) {
  "use strict";

  const arithmetic = { "+": "ADD", "-": "SUBTRACT", "−": "SUBTRACT", "*": "MULTIPLY", "×": "MULTIPLY", "/": "DIVIDE", "÷": "DIVIDE" };
  const comparisons = { "<": "LT", "<=": "LTE", "≤": "LTE", ">": "GT", ">=": "GTE", "≥": "GTE", "=": "EQ", "==": "EQ", "상향돌파": "CROSS_UP", "하향돌파": "CROSS_DOWN" };
  const arithmeticLabels = { ADD: "+", SUBTRACT: "−", MULTIPLY: "×", DIVIDE: "÷" };
  const comparisonLabels = { LT: "<", LTE: "≤", GT: ">", GTE: "≥", EQ: "=", CROSS_UP: "상향돌파", CROSS_DOWN: "하향돌파" };
  const clone = value => JSON.parse(JSON.stringify(value));

  function expressionUnits(expression, parentPrecedence = 0, isRightChild = false) {
    if (expression.kind === "metric") return [clone(expression)];
    if (expression.kind === "constant") return Array.from(String(expression.value));
    const precedence = ["MULTIPLY", "DIVIDE"].includes(expression.operator) ? 2 : 1;
    const units = [...expressionUnits(expression.left, precedence), " ", arithmeticLabels[expression.operator], " ",
      ...expressionUnits(expression.right, precedence, true)];
    // The parser is left associative. A same-precedence right subtree needs
    // parentheses even for +/* so serialization preserves evaluation order.
    return precedence < parentPrecedence || (isRightChild && precedence === parentPrecedence)
      ? ["(", ...units, ")"] : units;
  }

  function fromCondition(condition) {
    return [...expressionUnits(condition.left_expression), " ", ...comparisonLabels[condition.operator], " ",
      ...expressionUnits(condition.right_expression)];
  }

  // One metric is one caret unit. All other units are ordinary editable text.
  function replaceRange(units, start, end, replacement) {
    const left = Math.max(0, Math.min(start, end, units.length));
    const right = Math.max(left, Math.min(Math.max(start, end), units.length));
    const inserted = typeof replacement === "string" ? Array.from(replacement) : replacement.map(clone);
    return { units: [...units.slice(0, left), ...inserted, ...units.slice(right)], caret: left + inserted.length };
  }

  function tokenize(units) {
    const tokens = [];
    let index = 0;
    while (index < units.length) {
      const unit = units[index];
      if (unit && typeof unit === "object") {
        if (unit.kind !== "metric") throw new Error("데이터 항목을 다시 선택해 주세요.");
        tokens.push({ kind: "metric", value: clone(unit) });
        index += 1;
        continue;
      }
      if (/\s/.test(unit)) { index += 1; continue; }
      const remaining = [];
      for (let cursor = index; cursor < units.length && typeof units[cursor] === "string"; cursor += 1) remaining.push(units[cursor]);
      const text = remaining.join("");
      const word = /^(상향돌파|하향돌파)/.exec(text);
      if (word) { tokens.push({ kind: "compare", value: comparisons[word[0]] }); index += word[0].length; continue; }
      const pair = text.slice(0, 2);
      if (comparisons[pair]) { tokens.push({ kind: "compare", value: comparisons[pair] }); index += 2; continue; }
      if (comparisons[unit]) { tokens.push({ kind: "compare", value: comparisons[unit] }); index += 1; continue; }
      if (arithmetic[unit]) { tokens.push({ kind: "operator", value: arithmetic[unit] }); index += 1; continue; }
      if (unit === "(" || unit === ")") { tokens.push({ kind: unit }); index += 1; continue; }
      const number = /^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?/.exec(text);
      if (number) {
        if (!Number.isFinite(Number(number[0]))) throw new Error("숫자는 유한한 값으로 입력해 주세요.");
        if (Math.abs(Number(number[0])) > 1e18) throw new Error("숫자의 절댓값은 1,000,000,000,000,000,000 이하여야 합니다.");
        tokens.push({ kind: "constant", value: number[0] });
        index += number[0].length;
        continue;
      }
      throw new Error(`'${unit}'은 사용할 수 없습니다. 지표는 데이터 추가에서 선택해 주세요.`);
    }
    return tokens;
  }

  function parse(units) {
    const tokens = tokenize(units);
    let cursor = 0;
    const peek = () => tokens[cursor];
    function value() {
      const token = tokens[cursor++];
      if (!token) throw new Error("식이 미완성입니다. 숫자나 데이터를 입력해 주세요.");
      if (token.kind === "constant") return { kind: "constant", value: token.value };
      if (token.kind === "metric") return token.value;
      if (token.kind === "(") {
        const result = sum();
        if (peek()?.kind !== ")") throw new Error("여는 괄호에 맞는 닫는 괄호를 입력해 주세요.");
        cursor += 1;
        return result;
      }
      if (token.kind === "operator" && ["ADD", "SUBTRACT"].includes(token.value)) {
        const operand = value();
        if (token.value === "ADD") return operand;
        if (operand.kind === "constant") {
          operand.value = operand.value.startsWith("-") ? operand.value.slice(1) : `-${operand.value}`;
          return operand;
        }
        return { kind: "binary", operator: "SUBTRACT", left: { kind: "constant", value: "0" }, right: operand };
      }
      throw new Error("이 위치에는 숫자, 데이터 또는 여는 괄호가 필요합니다.");
    }
    function product() {
      let left = value();
      while (peek()?.kind === "operator" && ["MULTIPLY", "DIVIDE"].includes(peek().value)) {
        const operator = tokens[cursor++].value;
        left = { kind: "binary", operator, left, right: value() };
      }
      return left;
    }
    function sum() {
      let left = product();
      while (peek()?.kind === "operator" && ["ADD", "SUBTRACT"].includes(peek().value)) {
        const operator = tokens[cursor++].value;
        left = { kind: "binary", operator, left, right: product() };
      }
      return left;
    }
    const left_expression = sum();
    if (peek()?.kind !== "compare") throw new Error("두 값을 비교할 연산자(<, ≤, >, ≥, =) 하나를 입력해 주세요.");
    const operator = tokens[cursor++].value;
    const right_expression = sum();
    if (cursor !== tokens.length) {
      if (peek()?.kind === "compare") throw new Error("한 조건식에는 비교 연산자를 하나만 사용해 주세요.");
      throw new Error("값 사이에 연산자를 입력하고 괄호가 맞는지 확인해 주세요.");
    }
    const result = { left_expression, operator, right_expression };
    validate(result);
    return result;
  }

  function validate(condition) {
    const metrics = [];
    function inspect(node, depth = 0) {
      if (depth > 4) throw new Error("계산식은 최대 4단계까지 중첩할 수 있습니다.");
      if (node.kind === "metric") metrics.push(node);
      return node.kind === "binary" ? 1 + inspect(node.left, depth + 1) + inspect(node.right, depth + 1) : 1;
    }
    for (const side of ["left_expression", "right_expression"]) {
      if (inspect(condition[side]) > 31) throw new Error("계산식은 최대 31개 항목까지 사용할 수 있습니다.");
    }
    if (metrics.some(metric => metric.metric === "obv")) {
      const left = condition.left_expression;
      const right = condition.right_expression;
      if (left.kind !== "metric" || right.kind !== "metric" || left.metric !== "obv" || right.metric !== "obv") {
        throw new Error("OBV는 다른 시점의 OBV와 직접 비교해 주세요.");
      }
      if ((left.bar_interval || "3m") !== (right.bar_interval || "3m")
        || (left.bar_data_mode || "COMPLETE_ONLY") !== (right.bar_data_mode || "COMPLETE_ONLY")) {
        throw new Error("OBV 양쪽의 봉 주기와 봉 상태를 같게 설정해 주세요.");
      }
      if ((left.offset || 0) === (right.offset || 0)) throw new Error("OBV 양쪽의 봉 위치를 서로 다르게 설정해 주세요.");
    }
    if (["CROSS_UP", "CROSS_DOWN"].includes(condition.operator)) {
      if (metrics.some(metric => metric.metric === "profit_rate")) throw new Error("실시간 수익률에는 돌파 비교를 사용할 수 없습니다.");
      const bars = metrics.filter(metric => !["average_price", "profit_rate"].includes(metric.metric));
      if (!bars.length || new Set(bars.map(metric => `${metric.bar_interval || "3m"}:${metric.bar_data_mode || "COMPLETE_ONLY"}`)).size !== 1) {
        throw new Error("돌파 비교에는 같은 봉 주기와 봉 상태의 지표를 사용해 주세요.");
      }
    }
  }

  const api = { fromCondition, expressionUnits, replaceRange, tokenize, parse, validate };
  scope.ConditionFormula = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
