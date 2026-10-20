import json
import math
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from trade_router_studio.block_packages import (
    DEFAULT_CONDITION_BOLLINGER_SETTINGS,
    BlockTradeCondition,
    render_block_strategy,
)


CHART_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "studio"
    / "src"
    / "trade_router_studio"
    / "static"
    / "chart.js"
)


def test_chart_offers_atr_with_period_14_and_panel_placement() -> None:
    source = CHART_SCRIPT.read_text(encoding="utf-8")

    assert '["ATR", "ATR"]' in source
    assert 'if (["OBV", "ATR"].includes(indicator.type))' in source
    assert 'if (indicator.type === "ATR") indicator.period = 14;' in source
    assert 'if (indicator.type === "ATR") return `ATR (${indicator.period})`;' in source


def test_chart_atr_uses_raw_ohlc_and_wilder_smoothing() -> None:
    source = CHART_SCRIPT.read_text(encoding="utf-8")

    assert "const ATR_WARMUP_BARS = 100;" in source
    assert 'if (!String(interval).endsWith("m")) return 400;' in source
    assert "return Math.floor(1440 / minutes);" in source
    assert "if (length + 1 > historyLimit || rows.length < length + 1) return values;" in source
    assert "const warmup = Math.min(ATR_WARMUP_BARS, historyLimit - length - 1);" in source
    assert "const windowSize = length + 1 + warmup;" in source
    assert "for (let target = length; target < rows.length; target += 1)" in source
    assert "const start = Math.max(0, target - windowSize + 1);" in source
    assert "if (target - start < length) continue;" in source
    assert "wilderAtr(rawRows, indicator.period, maxHistory)" in source
    assert "row.high - row.low" in source
    assert "Math.abs(row.high - previousClose)" in source
    assert "Math.abs(row.low - previousClose)" in source
    assert "current = (current * (length - 1) + trueRanges[index]) / length" in source
    assert "indicatorSeries(indicator, rows, state.bars)" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required to execute chart.js")
def test_chart_wilder_atr_matches_python_strategy_runtime_on_fixed_ohlc() -> None:
    """Execute chart.js itself and compare every ATR point with the worker runtime."""

    rows = []
    previous_close = 100
    for index in range(130):
        open_value = previous_close + (2 if index % 5 == 0 else -1 if index % 3 == 0 else 0)
        close = 100 + ((index * 7) % 23) - 11
        high = max(open_value, close) + 1 + index % 4
        low = min(open_value, close) - 1 - index % 3
        rows.append({
            "open": open_value,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1_000 + index * 17,
        })
        previous_close = close

    # Instrument the existing IIFE only in-memory. The early return prevents
    # DOM initialization while the function body executed is the product's
    # actual wilderAtr implementation, rather than a copy in this test.
    node_program = r"""
const fs = require("fs");
const vm = require("vm");
let source = fs.readFileSync(process.env.CHART_SCRIPT_PATH, "utf8");
const marker = "  window.tradeRouterChart = { open, close };";
if (!source.includes(marker)) throw new Error("chart export marker not found");
source = source.replace(
  marker,
  "  globalThis.__wilderAtr = wilderAtr; globalThis.__sourceMaxHistory = sourceMaxHistory; return;\n" + marker,
);
const context = { crypto: { randomUUID: () => "test-id" } };
vm.runInNewContext(source, context, { filename: "chart.js" });
const rows = JSON.parse(fs.readFileSync(0, "utf8"));
const limit = context.__sourceMaxHistory("3m");
process.stdout.write(JSON.stringify({ limit, values: context.__wilderAtr(rows, 14, limit) }));
"""
    result = subprocess.run(
        [shutil.which("node"), "-e", node_program],
        input=json.dumps(rows),
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "CHART_SCRIPT_PATH": str(CHART_SCRIPT)},
    )
    chart = json.loads(result.stdout)
    assert chart["limit"] == 480

    namespace: dict[str, object] = {}
    exec(
        compile(
            render_block_strategy(DEFAULT_CONDITION_BOLLINGER_SETTINGS),
            "<managed-block>",
            "exec",
        ),
        namespace,
    )
    strategy = namespace["ManagedBlockStrategy"]()
    condition = BlockTradeCondition(
        left="atr",
        left_atr_period=14,
        left_bar_interval="3m",
        left_bar_data_mode="COMPLETE_ONLY",
        operator="LT",
        right="value",
        value=Decimal("999999"),
    ).model_dump(mode="json")
    bar_key = ("3m", True)
    python_rows = [
        SimpleNamespace(**{key: Decimal(str(value)) for key, value in row.items()})
        for row in rows
    ]
    expected = [
        strategy._condition_values(condition, {bar_key: python_rows[:index + 1]}, None)[0]
        for index in range(len(python_rows))
    ]

    assert len(chart["values"]) == len(expected)
    for index, (actual, wanted) in enumerate(zip(chart["values"], expected, strict=True)):
        if wanted is None:
            assert actual is None, f"ATR[{index}] should be unavailable"
        else:
            assert actual is not None, f"ATR[{index}] unexpectedly unavailable"
            assert math.isclose(actual, float(wanted), rel_tol=1e-12, abs_tol=1e-12), (
                index, actual, wanted,
            )
