from __future__ import annotations

import ast
import json
from datetime import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


BLOCK_PACKAGE_KIND = "trade-router-block-strategy"
BLOCK_TEMPLATE = "bar-observer-v1"


class BlockStrategySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1, max_length=128)
    trading_account: str = Field(min_length=1, max_length=128)
    market_source: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    bar_interval: str = Field(pattern=r"^[1-9]\d*[smhdw]$", max_length=16)
    bar_count: int = Field(ge=1, le=2_000)
    schedule_id: str = Field(min_length=1, max_length=128)
    start_time: str
    end_time: str

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_clock(cls, value: str) -> str:
        candidate = value.strip()
        if len(candidate) not in {5, 8}:
            raise ValueError("Time must use HH:MM or HH:MM:SS.")
        try:
            time.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError("Time must use HH:MM or HH:MM:SS.") from exc
        return candidate


class BlockStrategyPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal[1] = 1
    kind: Literal[BLOCK_PACKAGE_KIND] = BLOCK_PACKAGE_KIND
    template: Literal[BLOCK_TEMPLATE] = BLOCK_TEMPLATE
    name: str = Field(min_length=1, max_length=120)
    settings: BlockStrategySettings


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_block_strategy(settings: BlockStrategySettings) -> str:
    return f'''"""Trade Router가 관리하는 블록 전략 실행 파일."""

from trade_router import Strategy, StrategyConfig


STRATEGY_ID = {_literal(settings.strategy_id)}
TRADING_ACCOUNT = {_literal(settings.trading_account)}
MARKET_SOURCE = {_literal(settings.market_source)}
SYMBOL = {_literal(settings.symbol)}
BAR_INTERVAL = {_literal(settings.bar_interval)}
BAR_COUNT = {settings.bar_count}


class ManagedBlockStrategy(Strategy):
    config = StrategyConfig(
        id=STRATEGY_ID,
        trading_account=TRADING_ACCOUNT,
        market_sources={{"primary": MARKET_SOURCE}},
        required_capabilities={{"primary": ("quotes", "bars")}},
    )

    async def on_start(self, ctx):
        ctx.scheduler.on_bar_close(
            BAR_INTERVAL,
            schedule_id={_literal(settings.schedule_id)},
            between=({_literal(settings.start_time)}, {_literal(settings.end_time)}),
        )
        ctx.log.info("블록 전략을 시작했습니다.")

    async def on_bar(self, ctx, event):
        bars = await ctx.market.bars(SYMBOL, BAR_INTERVAL, count=BAR_COUNT)
        if bars:
            ctx.log.info(
                f"{{event.schedule_id}}: 완성 봉 {{len(bars)}}개 · "
                f"최근 종가 {{bars[-1].close}}"
            )

    async def on_stop(self, ctx):
        ctx.log.info("블록 전략을 종료합니다.")
'''


def _literal_value(node: ast.AST | None, constants: dict[str, object]) -> object | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_literal_value(item, constants) for item in node.elts)
    return None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def block_package_from_source(name: str, source: str) -> BlockStrategyPackage:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("Block strategy source is not valid Python.") from exc

    constants: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = (
            node.targets[0]
            if isinstance(node, ast.Assign) and node.targets
            else node.target
        )
        if not isinstance(target, ast.Name):
            continue
        value = _literal_value(node.value, constants)
        if value is not None:
            constants[target.id] = value

    schedule_id: object | None = None
    start_time: object | None = None
    end_time: object | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "on_bar_close":
            continue
        keywords = {item.arg: item.value for item in node.keywords if item.arg}
        schedule_id = _literal_value(keywords.get("schedule_id"), constants)
        between = _literal_value(keywords.get("between"), constants)
        if isinstance(between, tuple) and len(between) == 2:
            start_time, end_time = between
        break

    required = {
        "strategy_id": constants.get("STRATEGY_ID"),
        "trading_account": constants.get("TRADING_ACCOUNT"),
        "market_source": constants.get("MARKET_SOURCE"),
        "symbol": constants.get("SYMBOL"),
        "bar_interval": constants.get("BAR_INTERVAL"),
        "bar_count": constants.get("BAR_COUNT"),
        "schedule_id": schedule_id,
        "start_time": start_time,
        "end_time": end_time,
    }
    if any(value is None for value in required.values()):
        raise ValueError(
            "This block strategy uses blocks that cannot be exported by this version."
        )
    settings = BlockStrategySettings.model_validate(required)
    return BlockStrategyPackage(name=name, settings=settings)
