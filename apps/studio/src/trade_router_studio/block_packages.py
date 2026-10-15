from __future__ import annotations

import ast
import json
from datetime import time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


BLOCK_PACKAGE_KIND = "trade-router-block-strategy"
BAR_OBSERVER_TEMPLATE = "bar-observer-v1"
CONDITION_BOLLINGER_TEMPLATE = "condition-bollinger-v1"
BLOCK_TEMPLATE = CONDITION_BOLLINGER_TEMPLATE


def _validate_clock(value: str) -> str:
    candidate = value.strip()
    if len(candidate) not in {5, 8}:
        raise ValueError("Time must use HH:MM or HH:MM:SS.")
    try:
        time.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("Time must use HH:MM or HH:MM:SS.") from exc
    return candidate


class BlockStrategySettings(BaseModel):
    """Settings for the legacy read-only bar observer block."""

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

    _validate_times = field_validator("start_time", "end_time")(_validate_clock)


class ConditionBollingerBlockSettings(BaseModel):
    """Editable values for the router-owned condition/Bollinger workflow."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = Field(min_length=1, max_length=128)
    trading_account: str = Field(min_length=1, max_length=128)
    condition_source: str = Field(min_length=1, max_length=128)
    hts_user_id: str = Field(min_length=1, max_length=128)
    condition_id: str = Field(min_length=1, max_length=64)
    expected_condition_name: str = Field(default="", max_length=200)
    buy_budget_per_symbol: Decimal = Field(gt=0)
    total_operating_budget: Decimal = Field(gt=0)
    max_new_buys_per_scan: int = Field(ge=1, le=100)
    bollinger_period: int = Field(ge=2, le=2_000)
    bollinger_stddev: Decimal = Field(gt=0, le=Decimal("20"))
    take_profit_multiplier: Decimal = Field(gt=1)
    stop_loss_multiplier: Decimal = Field(gt=0, lt=1)
    buy_scan_seconds: int = Field(ge=1, le=86_400)
    buy_start_time: str
    buy_end_time: str
    bar_interval: str = Field(pattern=r"^[1-9]\d*[smhdw]$", max_length=16)
    sell_start_time: str
    sell_end_time: str
    close_time: str
    close_retry_start: str
    close_retry_end: str
    close_confirm_time: str
    route: Literal["KRX", "NXT", "SOR", "AUTO"] = "KRX"

    _validate_times = field_validator(
        "buy_start_time", "buy_end_time", "sell_start_time", "sell_end_time",
        "close_time", "close_retry_start", "close_retry_end", "close_confirm_time",
    )(_validate_clock)

    @model_validator(mode="after")
    def validate_limits(self) -> "ConditionBollingerBlockSettings":
        if self.buy_budget_per_symbol > self.total_operating_budget:
            raise ValueError("Per-symbol budget cannot exceed total operating budget.")
        if time.fromisoformat(self.buy_start_time) >= time.fromisoformat(self.buy_end_time):
            raise ValueError("Buy start time must be earlier than buy end time.")
        if time.fromisoformat(self.sell_start_time) >= time.fromisoformat(self.sell_end_time):
            raise ValueError("Sell start time must be earlier than sell end time.")
        close_times = [
            time.fromisoformat(self.close_time),
            time.fromisoformat(self.close_retry_start),
            time.fromisoformat(self.close_retry_end),
            time.fromisoformat(self.close_confirm_time),
        ]
        if close_times != sorted(close_times):
            raise ValueError("Close, retry, and confirm times must be chronological.")
        return self


class BlockStrategyPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal[1] = 1
    kind: Literal[BLOCK_PACKAGE_KIND] = BLOCK_PACKAGE_KIND
    template: Literal[BAR_OBSERVER_TEMPLATE, CONDITION_BOLLINGER_TEMPLATE]
    name: str = Field(min_length=1, max_length=120)
    settings: BlockStrategySettings | ConditionBollingerBlockSettings

    @model_validator(mode="after")
    def validate_template_settings(self) -> "BlockStrategyPackage":
        expected = ConditionBollingerBlockSettings if self.template == CONDITION_BOLLINGER_TEMPLATE else BlockStrategySettings
        if not isinstance(self.settings, expected):
            raise ValueError("Block template and settings do not match.")
        return self


DEFAULT_CONDITION_BOLLINGER_SETTINGS = ConditionBollingerBlockSettings(
    strategy_id="block-condition-bollinger",
    trading_account="등록한 계좌 별칭",
    condition_source="등록한 KIS 실전 API 이름",
    hts_user_id="등록한 HTS ID",
    condition_id="1",
    expected_condition_name="",
    buy_budget_per_symbol=Decimal("50000"),
    total_operating_budget=Decimal("150000"),
    max_new_buys_per_scan=5,
    bollinger_period=55,
    bollinger_stddev=Decimal("1.04"),
    take_profit_multiplier=Decimal("1.03"),
    stop_loss_multiplier=Decimal("0.97"),
    buy_scan_seconds=30,
    buy_start_time="09:00",
    buy_end_time="15:00",
    bar_interval="3m",
    sell_start_time="09:00",
    sell_end_time="15:20",
    close_time="15:20",
    close_retry_start="15:20:30",
    close_retry_end="15:30",
    close_confirm_time="15:30:05",
    route="KRX",
)


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_bar_observer(settings: BlockStrategySettings) -> str:
    return f'''"""Trade Router가 관리하는 블록 전략 실행 파일."""

from trade_router import Strategy, StrategyConfig

BLOCK_TEMPLATE = "{BAR_OBSERVER_TEMPLATE}"
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
            ctx.log.info(f"{{event.schedule_id}}: 완성 봉 {{len(bars)}}개 · 최근 종가 {{bars[-1].close}}")

    async def on_stop(self, ctx):
        ctx.log.info("블록 전략을 종료합니다.")
'''


_CONDITION_BOLLINGER_SOURCE = '''"""Trade Router가 관리하는 조건검색·볼린저 블록 전략."""

from datetime import datetime, time
from decimal import Decimal

from trade_router import OrderStatus, RiskLimits, RouterError, Strategy, StrategyConfig, Venue

BLOCK_TEMPLATE = __BLOCK_TEMPLATE__
STRATEGY_ID = __STRATEGY_ID__
TRADING_ACCOUNT = __TRADING_ACCOUNT__
CONDITION_SOURCE = __CONDITION_SOURCE__
HTS_USER_ID = __HTS_USER_ID__
CONDITION_ID = __CONDITION_ID__
EXPECTED_CONDITION_NAME = __EXPECTED_CONDITION_NAME__
BUY_BUDGET_PER_SYMBOL = Decimal(__BUY_BUDGET_PER_SYMBOL__)
TOTAL_OPERATING_BUDGET = Decimal(__TOTAL_OPERATING_BUDGET__)
MAX_NEW_BUYS_PER_SCAN = __MAX_NEW_BUYS_PER_SCAN__
BOLLINGER_PERIOD = __BOLLINGER_PERIOD__
BOLLINGER_STDDEV = Decimal(__BOLLINGER_STDDEV__)
TAKE_PROFIT_MULTIPLIER = Decimal(__TAKE_PROFIT_MULTIPLIER__)
STOP_LOSS_MULTIPLIER = Decimal(__STOP_LOSS_MULTIPLIER__)
BUY_SCAN_SECONDS = __BUY_SCAN_SECONDS__
BUY_START_TIME = __BUY_START_TIME__
BUY_END_TIME = __BUY_END_TIME__
BAR_INTERVAL = __BAR_INTERVAL__
SELL_START_TIME = __SELL_START_TIME__
SELL_END_TIME = __SELL_END_TIME__
CLOSE_TIME = __CLOSE_TIME__
CLOSE_RETRY_START = __CLOSE_RETRY_START__
CLOSE_RETRY_END = __CLOSE_RETRY_END__
CLOSE_CONFIRM_TIME = __CLOSE_CONFIRM_TIME__
ORDER_ROUTE = __ORDER_ROUTE__


class ManagedBlockStrategy(Strategy):
    config = StrategyConfig(
        id=STRATEGY_ID,
        trading_account=TRADING_ACCOUNT,
        market_sources={"primary": CONDITION_SOURCE},
        required_capabilities={"primary": ("conditions", "quotes", "bars")},
        limits=RiskLimits(max_exposure=TOTAL_OPERATING_BUDGET),
    )

    async def on_start(self, ctx):
        ctx.scheduler.every(
            seconds=BUY_SCAN_SECONDS,
            schedule_id="condition-scan",
            between=(BUY_START_TIME, BUY_END_TIME),
            aligned=True,
        )
        ctx.scheduler.on_bar_close(
            BAR_INTERVAL,
            schedule_id="bollinger-sell",
            between=(SELL_START_TIME, SELL_END_TIME),
        )
        ctx.scheduler.daily_at(CLOSE_TIME, schedule_id="close-all")
        ctx.scheduler.every(
            seconds=30,
            schedule_id="close-retry",
            between=(CLOSE_RETRY_START, CLOSE_RETRY_END),
            aligned=True,
        )
        ctx.scheduler.daily_at(CLOSE_CONFIRM_TIME, schedule_id="close-confirm")

        now = datetime.now().astimezone()
        local_time = now.time().replace(tzinfo=None)
        if time.fromisoformat(CLOSE_TIME) <= local_time < time.fromisoformat(CLOSE_RETRY_END):
            await self._liquidate_owned_positions(ctx, now, reason="장 마감 청산 시각 이후 시작 보완")
        elif time.fromisoformat(CLOSE_RETRY_END) <= local_time < time.fromisoformat(CLOSE_CONFIRM_TIME):
            await self._liquidate_owned_positions(ctx, now, reason="장 종료 후 청산 결과 확인", submit_orders=False)

        conditions = await ctx.discovery.conditions(HTS_USER_ID)
        selected = next((item for item in conditions if item.id == CONDITION_ID), None)
        if selected is None:
            available = ", ".join(f"{item.id}:{item.name}" for item in conditions)
            raise RuntimeError(f"조건식 {CONDITION_ID}을 찾지 못했습니다. 사용 가능: {available or '없음'}")
        if EXPECTED_CONDITION_NAME and " ".join(selected.name.split()) != " ".join(EXPECTED_CONDITION_NAME.split()):
            raise RuntimeError(f"조건식 {CONDITION_ID} 이름이 예상과 다릅니다: {selected.name}")
        ctx.log.info(f"조건식 연결: {selected.id} · {selected.name}")

    def _sync_tracked_positions(self, ctx, balance):
        positions = {item.instrument.code: item for item in balance.positions}
        tracked = dict(ctx.state.get("strategy_positions", {}))
        if not ctx.state.get("ownership_initialized", False):
            for position in balance.positions:
                if position.strategy_quantity is not None and position.strategy_quantity > 0:
                    tracked[position.instrument.code] = str(min(position.strategy_quantity, position.quantity))
            ctx.state.set("ownership_initialized", True)
        for code in list(tracked):
            position = positions.get(code)
            if position is None or position.quantity <= 0:
                tracked.pop(code, None)
                continue
            quantity = min(Decimal(str(tracked[code])), position.quantity)
            if position.strategy_quantity is not None:
                quantity = min(quantity, position.strategy_quantity)
            if quantity <= 0:
                tracked.pop(code, None)
            else:
                tracked[code] = str(quantity)
        ctx.state.set("strategy_positions", tracked)
        return tracked

    async def _liquidate_owned_positions(self, ctx, scheduled_at, *, reason, submit_orders=True):
        try:
            balance = await ctx.account.balance()
            open_orders = await ctx.account.open_orders()
        except RouterError as exc:
            ctx.log.warning(f"청산 확인 보류 [{exc.code.value}]: {exc.message} · 다음 확인에서 재시도합니다.")
            return
        positions = {item.instrument.code: item for item in balance.positions}
        pending_codes = {item.instrument.code for item in open_orders if item.remaining_quantity > 0}
        tracked = self._sync_tracked_positions(ctx, balance)
        for code in list(tracked):
            position = positions.get(code)
            if position is None or position.quantity <= 0:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)
                continue
            if position.strategy_quantity is not None and position.strategy_quantity <= 0:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)
                continue
            if code in pending_codes:
                ctx.log.info(f"{code} 청산 대기: 계좌에 미체결 주문이 있습니다.")
                continue
            if not submit_orders:
                ctx.log.warning(f"{code} 장 종료 후 잔여 확인: 전략 소유 {tracked[code]}주 · 실제 보유 {position.quantity}주")
                continue
            tracked_quantity = position.strategy_quantity if position.strategy_quantity is not None else Decimal(str(tracked[code]))
            quantity = min(tracked_quantity, position.available_quantity)
            if quantity <= 0:
                continue
            try:
                order = await ctx.orders.market_sell(
                    code=code,
                    quantity=quantity,
                    route=ORDER_ROUTE,
                    client_order_id=f"close-{code}-{scheduled_at:%Y%m%d%H%M%S}-sell",
                )
            except RouterError as exc:
                ctx.log.error(f"{code} 청산 실패 [{exc.code.value}]: {exc.message}")
                continue
            ctx.log.info(f"{code} {reason} · 전략 소유 {quantity}주 시장가 매도 요청: {order.status.value}")
            if order.status is OrderStatus.FILLED:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)

    async def on_timer(self, ctx, event):
        if event.schedule_id in {"close-all", "close-retry", "close-confirm"}:
            labels = {
                "close-all": "장 마감 정시 전량 청산",
                "close-retry": "장 마감 전량 청산 재확인",
                "close-confirm": "장 종료 후 청산 결과 확인",
            }
            await self._liquidate_owned_positions(
                ctx,
                event.scheduled_at,
                reason=labels[event.schedule_id],
                submit_orders=event.schedule_id != "close-confirm",
            )
            return
        if event.schedule_id != "condition-scan":
            return
        matches = await ctx.discovery.matches(HTS_USER_ID, CONDITION_ID)
        ctx.log.info(f"조건검색 {len(matches)}종목 · 신규 매수 후보를 확인합니다.")
        if not matches:
            return
        try:
            balance = await ctx.account.balance()
            open_orders = await ctx.account.open_orders()
        except RouterError as exc:
            ctx.log.warning(f"매수 판단 보류 [{exc.code.value}]: {exc.message} · 다음 조회에서 재시도합니다.")
            return
        held_codes = {item.instrument.code for item in balance.positions if item.quantity > 0}
        pending_codes = {item.instrument.code for item in open_orders if item.remaining_quantity > 0}
        tracked = self._sync_tracked_positions(ctx, balance)
        submitted = 0
        for match in matches:
            code = match.instrument.code
            if code in held_codes or code in pending_codes or code in tracked:
                continue
            try:
                buying_power = await ctx.account.buying_power(code, order_type="MARKET")
            except RouterError as exc:
                ctx.log.warning(f"{code} 주문 가능 수량 확인 실패 [{exc.code.value}]: {exc.message}")
                continue
            budget_quantity = int(BUY_BUDGET_PER_SYMBOL // buying_power.calculation_price)
            quantity = min(int(buying_power.maximum_quantity), budget_quantity)
            if quantity < 1:
                ctx.log.info(f"{code} 건너뜀: 시장가 계산단가 {buying_power.calculation_price}원 · 종목당 예산 {BUY_BUDGET_PER_SYMBOL}원")
                continue
            try:
                order = await ctx.orders.market_buy(
                    code=code,
                    quantity=quantity,
                    route=ORDER_ROUTE,
                    client_order_id=f"condition-{code}-{event.scheduled_at:%Y%m%d%H%M%S}-buy",
                )
            except RouterError as exc:
                ctx.log.error(f"{code} 매수 요청 실패 [{exc.code.value}]: {exc.message}")
                continue
            if order.status is not OrderStatus.REJECTED:
                tracked[code] = str(quantity)
                ctx.state.set("strategy_positions", tracked)
                submitted += 1
                ctx.log.info(f"{code} 시장가 {quantity}주 매수 요청: {order.status.value}")
            if submitted >= MAX_NEW_BUYS_PER_SCAN:
                break

    async def on_bar(self, ctx, event):
        try:
            balance = await ctx.account.balance()
            open_orders = await ctx.account.open_orders()
        except RouterError as exc:
            ctx.log.warning(f"매도 판단 보류 [{exc.code.value}]: {exc.message} · 다음 완성 봉에서 재시도합니다.")
            return
        positions = {item.instrument.code: item for item in balance.positions}
        pending_codes = {item.instrument.code for item in open_orders if item.remaining_quantity > 0}
        tracked = self._sync_tracked_positions(ctx, balance)
        for code in list(tracked):
            position = positions.get(code)
            if position is None or position.quantity <= 0:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)
                continue
            if position.strategy_quantity is not None and position.strategy_quantity <= 0:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)
                continue
            if code in pending_codes:
                continue
            bars = await ctx.market.bars(
                code,
                BAR_INTERVAL,
                count=BOLLINGER_PERIOD + 1,
                venue=Venue.KRX,
                complete_only=True,
            )
            if len(bars) < BOLLINGER_PERIOD:
                ctx.log.warning(f"{code} 매도 판단 보류: 완성 봉 {len(bars)}/{BOLLINGER_PERIOD}개")
                continue
            closes = [bar.close for bar in bars[-BOLLINGER_PERIOD:]]
            mean = sum(closes, Decimal("0")) / Decimal(BOLLINGER_PERIOD)
            variance = sum(((value - mean) ** 2 for value in closes), Decimal("0")) / Decimal(BOLLINGER_PERIOD)
            upper = mean + BOLLINGER_STDDEV * variance.sqrt()
            latest = bars[-1]
            average_price = position.average_price
            take_profit_price = average_price * TAKE_PROFIT_MULTIPLIER
            stop_loss_price = average_price * STOP_LOSS_MULTIPLIER
            sell_reason = None
            if average_price > 0 and latest.close >= take_profit_price:
                sell_reason = f"익절: 종가 {latest.close}원 >= {take_profit_price:.2f}원"
            elif average_price > 0 and latest.close <= stop_loss_price:
                sell_reason = f"손절: 종가 {latest.close}원 <= {stop_loss_price:.2f}원"
            elif latest.close < upper:
                sell_reason = f"볼린저 청산: 종가 {latest.close}원 < 상단 {upper:.2f}원"
            if sell_reason is None:
                continue
            tracked_quantity = position.strategy_quantity if position.strategy_quantity is not None else Decimal(str(tracked[code]))
            quantity = min(tracked_quantity, position.available_quantity)
            if quantity <= 0:
                continue
            try:
                order = await ctx.orders.market_sell(
                    code=code,
                    quantity=quantity,
                    route=ORDER_ROUTE,
                    client_order_id=f"exit-{code}-{event.scheduled_at:%Y%m%d%H%M%S}-sell",
                )
            except RouterError as exc:
                ctx.log.error(f"{code} 매도 요청 실패 [{exc.code.value}]: {exc.message}")
                continue
            ctx.log.info(f"{code} {sell_reason} · 시장가 {quantity}주 매도 요청: {order.status.value}")
            if order.status is OrderStatus.FILLED:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)

    async def on_stop(self, ctx):
        ctx.log.info("조건검색·볼린저 블록 전략을 종료합니다.")
'''


def _render_condition_bollinger(settings: ConditionBollingerBlockSettings) -> str:
    replacements = {
        "__BLOCK_TEMPLATE__": _literal(CONDITION_BOLLINGER_TEMPLATE),
        "__STRATEGY_ID__": _literal(settings.strategy_id),
        "__TRADING_ACCOUNT__": _literal(settings.trading_account),
        "__CONDITION_SOURCE__": _literal(settings.condition_source),
        "__HTS_USER_ID__": _literal(settings.hts_user_id),
        "__CONDITION_ID__": _literal(settings.condition_id),
        "__EXPECTED_CONDITION_NAME__": _literal(settings.expected_condition_name),
        "__BUY_BUDGET_PER_SYMBOL__": _literal(str(settings.buy_budget_per_symbol)),
        "__TOTAL_OPERATING_BUDGET__": _literal(str(settings.total_operating_budget)),
        "__MAX_NEW_BUYS_PER_SCAN__": str(settings.max_new_buys_per_scan),
        "__BOLLINGER_PERIOD__": str(settings.bollinger_period),
        "__BOLLINGER_STDDEV__": _literal(str(settings.bollinger_stddev)),
        "__TAKE_PROFIT_MULTIPLIER__": _literal(str(settings.take_profit_multiplier)),
        "__STOP_LOSS_MULTIPLIER__": _literal(str(settings.stop_loss_multiplier)),
        "__BUY_SCAN_SECONDS__": str(settings.buy_scan_seconds),
        "__BUY_START_TIME__": _literal(settings.buy_start_time),
        "__BUY_END_TIME__": _literal(settings.buy_end_time),
        "__BAR_INTERVAL__": _literal(settings.bar_interval),
        "__SELL_START_TIME__": _literal(settings.sell_start_time),
        "__SELL_END_TIME__": _literal(settings.sell_end_time),
        "__CLOSE_TIME__": _literal(settings.close_time),
        "__CLOSE_RETRY_START__": _literal(settings.close_retry_start),
        "__CLOSE_RETRY_END__": _literal(settings.close_retry_end),
        "__CLOSE_CONFIRM_TIME__": _literal(settings.close_confirm_time),
        "__ORDER_ROUTE__": _literal(settings.route),
    }
    source = _CONDITION_BOLLINGER_SOURCE
    for token, value in replacements.items():
        source = source.replace(token, value)
    return source


def render_block_strategy(
    settings: BlockStrategySettings | ConditionBollingerBlockSettings,
    *,
    template: str | None = None,
) -> str:
    if isinstance(settings, ConditionBollingerBlockSettings):
        if template not in {None, CONDITION_BOLLINGER_TEMPLATE}:
            raise ValueError("Block template and settings do not match.")
        return _render_condition_bollinger(settings)
    if template not in {None, BAR_OBSERVER_TEMPLATE}:
        raise ValueError("Block template and settings do not match.")
    return _render_bar_observer(settings)


def _literal_value(node: ast.AST | None, constants: dict[str, object]) -> object | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_literal_value(item, constants) for item in node.elts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Decimal" and len(node.args) == 1:
        value = _literal_value(node.args[0], constants)
        return Decimal(str(value)) if value is not None else None
    return None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _constants_from_source(source: str) -> tuple[ast.Module, dict[str, object]]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("Block strategy source is not valid Python.") from exc
    constants: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) and node.targets else node.target
        if not isinstance(target, ast.Name):
            continue
        value = _literal_value(node.value, constants)
        if value is not None:
            constants[target.id] = value
    return tree, constants


def _condition_package(name: str, constants: dict[str, object]) -> BlockStrategyPackage:
    mapping = {
        "strategy_id": "STRATEGY_ID", "trading_account": "TRADING_ACCOUNT",
        "condition_source": "CONDITION_SOURCE", "hts_user_id": "HTS_USER_ID",
        "condition_id": "CONDITION_ID", "expected_condition_name": "EXPECTED_CONDITION_NAME",
        "buy_budget_per_symbol": "BUY_BUDGET_PER_SYMBOL", "total_operating_budget": "TOTAL_OPERATING_BUDGET",
        "max_new_buys_per_scan": "MAX_NEW_BUYS_PER_SCAN", "bollinger_period": "BOLLINGER_PERIOD",
        "bollinger_stddev": "BOLLINGER_STDDEV", "take_profit_multiplier": "TAKE_PROFIT_MULTIPLIER",
        "stop_loss_multiplier": "STOP_LOSS_MULTIPLIER", "buy_scan_seconds": "BUY_SCAN_SECONDS",
        "buy_start_time": "BUY_START_TIME", "buy_end_time": "BUY_END_TIME",
        "bar_interval": "BAR_INTERVAL", "sell_start_time": "SELL_START_TIME",
        "sell_end_time": "SELL_END_TIME", "close_time": "CLOSE_TIME",
        "close_retry_start": "CLOSE_RETRY_START", "close_retry_end": "CLOSE_RETRY_END",
        "close_confirm_time": "CLOSE_CONFIRM_TIME", "route": "ORDER_ROUTE",
    }
    required = {field: constants.get(source_name) for field, source_name in mapping.items()}
    if any(value is None for value in required.values()):
        raise ValueError("This condition/Bollinger block has unsupported fields.")
    settings = ConditionBollingerBlockSettings.model_validate(required)
    return BlockStrategyPackage(name=name, template=CONDITION_BOLLINGER_TEMPLATE, settings=settings)


def _bar_observer_package(name: str, tree: ast.Module, constants: dict[str, object]) -> BlockStrategyPackage:
    schedule_id = start_time = end_time = None
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
        "strategy_id": constants.get("STRATEGY_ID"), "trading_account": constants.get("TRADING_ACCOUNT"),
        "market_source": constants.get("MARKET_SOURCE"), "symbol": constants.get("SYMBOL"),
        "bar_interval": constants.get("BAR_INTERVAL"), "bar_count": constants.get("BAR_COUNT"),
        "schedule_id": schedule_id, "start_time": start_time, "end_time": end_time,
    }
    if any(value is None for value in required.values()):
        raise ValueError("This block strategy uses unsupported blocks.")
    settings = BlockStrategySettings.model_validate(required)
    return BlockStrategyPackage(name=name, template=BAR_OBSERVER_TEMPLATE, settings=settings)


def block_package_from_source(name: str, source: str) -> BlockStrategyPackage:
    tree, constants = _constants_from_source(source)
    template = constants.get("BLOCK_TEMPLATE")
    if template == CONDITION_BOLLINGER_TEMPLATE:
        return _condition_package(name, constants)
    if template not in {None, BAR_OBSERVER_TEMPLATE}:
        raise ValueError(f"Unsupported block template: {template}")
    return _bar_observer_package(name, tree, constants)
