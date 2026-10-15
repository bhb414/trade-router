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


class BlockTradeCondition(BaseModel):
    """One broker-neutral comparison used by a managed block strategy."""

    model_config = ConfigDict(extra="forbid")
    left: Literal[
        "open", "high", "low", "close", "volume",
        "bollinger_upper", "bollinger_middle", "bollinger_lower",
        "average_price", "profit_rate",
    ] = "close"
    operator: Literal["LT", "LTE", "GT", "GTE", "EQ", "CROSS_UP", "CROSS_DOWN"] = "LT"
    right: Literal[
        "open", "high", "low", "close", "volume",
        "bollinger_upper", "bollinger_middle", "bollinger_lower",
        "average_price", "profit_rate", "value",
    ] = "bollinger_upper"
    value: Decimal = Decimal("0")
    left_offset: int = Field(default=0, ge=0, le=200)
    right_offset: int = Field(default=0, ge=0, le=200)


DEFAULT_SELL_CONDITIONS = (
    BlockTradeCondition(left="profit_rate", operator="GTE", right="value", value=Decimal("3")),
    BlockTradeCondition(left="profit_rate", operator="LTE", right="value", value=Decimal("-3")),
    BlockTradeCondition(left="close", operator="LT", right="bollinger_upper"),
)


class ConditionBollingerBlockSettings(BaseModel):
    """Editable values for the router-owned condition/Bollinger workflow."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = Field(min_length=1, max_length=128)
    trading_account: str = Field(min_length=1, max_length=128)
    condition_source: str = Field(min_length=1, max_length=128)
    market_venue: Literal["KRX", "NXT", "INTEGRATED"] = "KRX"
    target_source: Literal["HTS_CONDITION", "RANKING", "SYMBOLS"] = "HTS_CONDITION"
    hts_user_id: str = Field(min_length=1, max_length=128)
    condition_id: str = Field(min_length=1, max_length=64)
    expected_condition_name: str = Field(default="", max_length=200)
    ranking_kind: Literal["HTS_VIEW", "VOLUME", "TRADING_VALUE", "RISE"] = "RISE"
    ranking_venue: Literal["KRX", "NXT", "INTEGRATED"] = "KRX"
    ranking_market: Literal["ALL", "KOSPI", "KOSDAQ"] = "ALL"
    ranking_limit: int = Field(default=20, ge=1, le=100)
    symbols: list[str] = Field(default_factory=list, max_length=200)
    buy_budget_per_symbol: Decimal = Field(gt=0)
    total_operating_budget: Decimal = Field(gt=0)
    max_new_buys_per_scan: int = Field(ge=1, le=100)
    bollinger_period: int = Field(ge=2, le=2_000)
    bollinger_stddev: Decimal = Field(gt=0, le=Decimal("20"))
    take_profit_multiplier: Decimal = Field(gt=1)
    stop_loss_multiplier: Decimal = Field(gt=0, lt=1)
    use_bollinger_exit: bool = True
    use_take_profit: bool = True
    use_stop_loss: bool = True
    buy_condition_logic: Literal["ALL", "ANY"] = "ALL"
    buy_conditions: list[BlockTradeCondition] = Field(default_factory=list, max_length=20)
    sell_condition_logic: Literal["ALL", "ANY"] = "ANY"
    sell_conditions: list[BlockTradeCondition] = Field(
        default_factory=lambda: [item.model_copy() for item in DEFAULT_SELL_CONDITIONS],
        min_length=1,
        max_length=20,
    )
    buy_order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    sell_order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    buy_scan_seconds: int = Field(ge=1, le=86_400)
    buy_start_time: str
    buy_end_time: str
    bar_interval: str = Field(max_length=16)
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

    @field_validator("bar_interval")
    @classmethod
    def validate_bar_interval(cls, value: str) -> str:
        candidate = value.strip().lower()
        if candidate in {"1d", "d", "day"}:
            return "1d"
        if candidate == "1h":
            candidate = "60m"
        if not candidate.endswith("m") or not candidate[:-1].isdigit():
            raise ValueError("관리형 블록 전략은 1분봉부터 120분봉과 일봉을 지원합니다.")
        minutes = int(candidate[:-1])
        if not 1 <= minutes <= 120:
            raise ValueError("관리형 블록 전략은 1분봉부터 120분봉과 일봉을 지원합니다.")
        return f"{minutes}m"

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
        self.symbols = list(dict.fromkeys(code.strip().upper() for code in self.symbols if code.strip()))
        if self.target_source == "SYMBOLS" and not self.symbols:
            raise ValueError("At least one symbol is required for a symbol-list source.")
        position_only_metrics = {"average_price", "profit_rate"}
        for condition in self.buy_conditions:
            if condition.left in position_only_metrics or condition.right in position_only_metrics:
                raise ValueError(
                    "매수 조건에서는 보유 후에만 생기는 평균 매수가·수익률을 사용할 수 없습니다."
                )
        if not self.sell_conditions:
            raise ValueError("At least one sell condition must be configured.")
        required_bars = 1
        for condition in (*self.buy_conditions, *self.sell_conditions):
            offset = max(condition.left_offset, condition.right_offset)
            if condition.operator in {"CROSS_UP", "CROSS_DOWN"}:
                offset += 1
            uses_bollinger = condition.left.startswith("bollinger_") or condition.right.startswith("bollinger_")
            required_bars = max(
                required_bars,
                offset + (self.bollinger_period if uses_bollinger else 1),
            )
        if self.bar_interval == "1d":
            if required_bars > 400:
                raise ValueError(
                    "일봉 조건에 필요한 이력은 한 번에 최대 400봉까지 지원합니다."
                )
            return self
        interval_minutes = int(self.bar_interval[:-1])
        # The KIS minute-history adapter deliberately performs at most twelve
        # 120-row calls per evaluation so one strategy cannot monopolize the
        # shared API queue. Reject block settings that can never fit that bound.
        if required_bars * interval_minutes > 1_440:
            raise ValueError(
                "선택한 봉 주기·지표 기간·N봉 전 값에 필요한 이력이 관리형 1회 조회 범위를 초과합니다."
            )
        return self


class BlockNodePosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=-10_000, le=10_000)
    y: float = Field(ge=-10_000, le=10_000)


class BlockViewport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(default=24, ge=-20_000, le=20_000)
    y: float = Field(default=24, ge=-20_000, le=20_000)
    zoom: float = Field(default=0.75, ge=0.4, le=2)


class BlockEditorLayout(BaseModel):
    """Visual-only node positions. The compiler intentionally ignores them."""

    model_config = ConfigDict(extra="forbid")
    nodes: dict[str, BlockNodePosition] = Field(default_factory=dict)
    viewport: BlockViewport = Field(default_factory=BlockViewport)


class BlockStrategyPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal[1] = 1
    kind: Literal[BLOCK_PACKAGE_KIND] = BLOCK_PACKAGE_KIND
    template: Literal[BAR_OBSERVER_TEMPLATE, CONDITION_BOLLINGER_TEMPLATE]
    name: str = Field(min_length=1, max_length=120)
    settings: BlockStrategySettings | ConditionBollingerBlockSettings
    layout: BlockEditorLayout = Field(default_factory=BlockEditorLayout)

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
    use_bollinger_exit=True,
    use_take_profit=True,
    use_stop_loss=True,
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
    market_venue="KRX",
    route="KRX",
)


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _layout_literal(layout: BlockEditorLayout | None) -> str:
    payload = (layout or BlockEditorLayout()).model_dump(mode="json")
    return _literal(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _json_literal(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif isinstance(value, (list, tuple)):
        value = [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value]
    return _literal(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _layout_from_constants(constants: dict[str, object]) -> BlockEditorLayout:
    raw = constants.get("BLOCK_LAYOUT_JSON")
    if not isinstance(raw, str) or not raw:
        return BlockEditorLayout()
    try:
        return BlockEditorLayout.model_validate(json.loads(raw))
    except (ValueError, TypeError, json.JSONDecodeError):
        return BlockEditorLayout()


def _render_bar_observer(
    settings: BlockStrategySettings,
    layout: BlockEditorLayout | None = None,
) -> str:
    return f'''"""Trade Router가 관리하는 블록 전략 실행 파일."""

from trade_router import Strategy, StrategyConfig

BLOCK_TEMPLATE = "{BAR_OBSERVER_TEMPLATE}"
BLOCK_LAYOUT_JSON = {_layout_literal(layout)}
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


_CONDITION_BOLLINGER_SOURCE = '''"""Trade Router가 관리하는 종목 데이터·조건 비교 블록 전략."""

import json
from datetime import datetime, time
from decimal import Decimal

from trade_router import OrderStatus, RankingKind, RiskLimits, RouterError, Strategy, StrategyConfig, Venue

BLOCK_TEMPLATE = __BLOCK_TEMPLATE__
BLOCK_LAYOUT_JSON = __BLOCK_LAYOUT_JSON__
STRATEGY_ID = __STRATEGY_ID__
TRADING_ACCOUNT = __TRADING_ACCOUNT__
CONDITION_SOURCE = __CONDITION_SOURCE__
MARKET_VENUE = __MARKET_VENUE__
TARGET_SOURCE = __TARGET_SOURCE__
HTS_USER_ID = __HTS_USER_ID__
CONDITION_ID = __CONDITION_ID__
EXPECTED_CONDITION_NAME = __EXPECTED_CONDITION_NAME__
RANKING_KIND = __RANKING_KIND__
RANKING_VENUE = __RANKING_VENUE__
RANKING_MARKET = __RANKING_MARKET__
RANKING_LIMIT = __RANKING_LIMIT__
SYMBOLS_JSON = __SYMBOLS_JSON__
BUY_BUDGET_PER_SYMBOL = Decimal(__BUY_BUDGET_PER_SYMBOL__)
TOTAL_OPERATING_BUDGET = Decimal(__TOTAL_OPERATING_BUDGET__)
MAX_NEW_BUYS_PER_SCAN = __MAX_NEW_BUYS_PER_SCAN__
BOLLINGER_PERIOD = __BOLLINGER_PERIOD__
BOLLINGER_STDDEV = Decimal(__BOLLINGER_STDDEV__)
TAKE_PROFIT_MULTIPLIER = Decimal(__TAKE_PROFIT_MULTIPLIER__)
STOP_LOSS_MULTIPLIER = Decimal(__STOP_LOSS_MULTIPLIER__)
USE_BOLLINGER_EXIT = __USE_BOLLINGER_EXIT__
USE_TAKE_PROFIT = __USE_TAKE_PROFIT__
USE_STOP_LOSS = __USE_STOP_LOSS__
BUY_CONDITION_LOGIC = __BUY_CONDITION_LOGIC__
BUY_CONDITIONS_JSON = __BUY_CONDITIONS_JSON__
SELL_CONDITION_LOGIC = __SELL_CONDITION_LOGIC__
SELL_CONDITIONS_JSON = __SELL_CONDITIONS_JSON__
BUY_ORDER_TYPE = __BUY_ORDER_TYPE__
SELL_ORDER_TYPE = __SELL_ORDER_TYPE__
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

SYMBOLS = tuple(json.loads(SYMBOLS_JSON))
BUY_CONDITIONS = tuple(json.loads(BUY_CONDITIONS_JSON))
SELL_CONDITIONS = tuple(json.loads(SELL_CONDITIONS_JSON))
DISCOVERY_CAPABILITY = {
    "HTS_CONDITION": "conditions",
    "RANKING": "rankings",
}.get(TARGET_SOURCE)
REQUIRED_CAPABILITIES = tuple(
    item for item in (DISCOVERY_CAPABILITY, "quotes", "bars") if item
)


class ManagedBlockStrategy(Strategy):
    config = StrategyConfig(
        id=STRATEGY_ID,
        trading_account=TRADING_ACCOUNT,
        market_sources={"primary": CONDITION_SOURCE},
        required_capabilities={"primary": REQUIRED_CAPABILITIES},
        limits=RiskLimits(max_exposure=TOTAL_OPERATING_BUDGET),
    )

    async def _candidate_codes(self, ctx):
        if TARGET_SOURCE == "HTS_CONDITION":
            rows = await ctx.discovery.matches(HTS_USER_ID, CONDITION_ID)
            codes = [item.instrument.code for item in rows]
            label = f"HTS 조건검색 {CONDITION_ID}"
        elif TARGET_SOURCE == "RANKING":
            rows = await ctx.discovery.ranking(
                RankingKind(RANKING_KIND),
                venue=Venue(RANKING_VENUE),
                market=RANKING_MARKET,
                limit=RANKING_LIMIT,
            )
            codes = [item.instrument.code for item in rows]
            label = f"{RANKING_KIND} 순위"
        else:
            codes = list(SYMBOLS)
            label = "지정 종목"
        unique = list(dict.fromkeys(code.strip().upper() for code in codes if code.strip()))
        ctx.log.info(f"{label} {len(unique)}종목 · 신규 매수 후보를 확인합니다.")
        return unique

    @staticmethod
    def _bollinger_values(bars, offset):
        end = len(bars) - offset
        start = end - BOLLINGER_PERIOD
        if start < 0 or end <= 0:
            return None
        closes = [bar.close for bar in bars[start:end]]
        mean = sum(closes, Decimal("0")) / Decimal(BOLLINGER_PERIOD)
        variance = sum(((value - mean) ** 2 for value in closes), Decimal("0")) / Decimal(BOLLINGER_PERIOD)
        deviation = BOLLINGER_STDDEV * variance.sqrt()
        return mean + deviation, mean, mean - deviation

    def _indicator_value(self, metric, bars, offset, average_price):
        index = len(bars) - 1 - offset
        if index < 0:
            return None
        bar = bars[index]
        if metric in {"open", "high", "low", "close", "volume"}:
            return getattr(bar, metric)
        if metric in {"bollinger_upper", "bollinger_middle", "bollinger_lower"}:
            values = self._bollinger_values(bars, offset)
            if values is None:
                return None
            return values[{"bollinger_upper": 0, "bollinger_middle": 1, "bollinger_lower": 2}[metric]]
        if metric == "average_price":
            return average_price if average_price and average_price > 0 else None
        if metric == "profit_rate":
            if not average_price or average_price <= 0:
                return None
            return (bar.close / average_price - Decimal("1")) * Decimal("100")
        return None

    def _condition_values(self, condition, bars, average_price, extra_offset=0):
        left = self._indicator_value(
            condition["left"], bars, int(condition.get("left_offset", 0)) + extra_offset, average_price
        )
        if condition["right"] == "value":
            right = Decimal(str(condition.get("value", "0")))
        else:
            right = self._indicator_value(
                condition["right"], bars, int(condition.get("right_offset", 0)) + extra_offset, average_price
            )
        return left, right

    def _condition_matches(self, condition, bars, average_price):
        left, right = self._condition_values(condition, bars, average_price)
        if left is None or right is None:
            return False
        operator = condition["operator"]
        if operator == "LT":
            return left < right
        if operator == "LTE":
            return left <= right
        if operator == "GT":
            return left > right
        if operator == "GTE":
            return left >= right
        if operator == "EQ":
            return left == right
        previous_left, previous_right = self._condition_values(condition, bars, average_price, 1)
        if previous_left is None or previous_right is None:
            return False
        if operator == "CROSS_UP":
            return previous_left <= previous_right and left > right
        if operator == "CROSS_DOWN":
            return previous_left >= previous_right and left < right
        return False

    def _conditions_match(self, conditions, logic, bars, average_price=None):
        if not conditions:
            return True
        results = [self._condition_matches(item, bars, average_price) for item in conditions]
        return all(results) if logic == "ALL" else any(results)

    @staticmethod
    def _required_bar_count(conditions):
        required = 1
        for item in conditions:
            offset = max(int(item.get("left_offset", 0)), int(item.get("right_offset", 0)))
            if item["operator"] in {"CROSS_UP", "CROSS_DOWN"}:
                offset += 1
            uses_bollinger = item["left"].startswith("bollinger_") or item["right"].startswith("bollinger_")
            required = max(required, offset + (BOLLINGER_PERIOD if uses_bollinger else 1))
        return required

    @staticmethod
    def _condition_summary(condition):
        operators = {
            "LT": "<", "LTE": "≤", "GT": ">", "GTE": "≥", "EQ": "=",
            "CROSS_UP": "상향돌파", "CROSS_DOWN": "하향돌파",
        }
        right = condition["value"] if condition["right"] == "value" else condition["right"]
        return f"{condition['left']} {operators.get(condition['operator'], condition['operator'])} {right}"

    @staticmethod
    async def _current_price(ctx, code):
        quote = await ctx.market.quote(code, venue=Venue(MARKET_VENUE))
        return quote.price

    async def on_start(self, ctx):
        ctx.scheduler.every(
            seconds=BUY_SCAN_SECONDS,
            schedule_id="condition-scan",
            between=(BUY_START_TIME, BUY_END_TIME),
            aligned=True,
        )
        if BAR_INTERVAL == "1d":
            ctx.scheduler.daily_at(SELL_START_TIME, schedule_id="bollinger-sell-daily")
        else:
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

        if TARGET_SOURCE == "HTS_CONDITION":
            conditions = await ctx.discovery.conditions(HTS_USER_ID)
            selected = next((item for item in conditions if item.id == CONDITION_ID), None)
            if selected is None:
                available = ", ".join(f"{item.id}:{item.name}" for item in conditions)
                raise RuntimeError(f"조건식 {CONDITION_ID}을 찾지 못했습니다. 사용 가능: {available or '없음'}")
            if EXPECTED_CONDITION_NAME and " ".join(selected.name.split()) != " ".join(EXPECTED_CONDITION_NAME.split()):
                raise RuntimeError(f"조건식 {CONDITION_ID} 이름이 예상과 다릅니다: {selected.name}")
            ctx.log.info(f"종목 데이터 연결: HTS 조건식 {selected.id} · {selected.name}")
        elif TARGET_SOURCE == "RANKING":
            ctx.log.info(f"종목 데이터 연결: {RANKING_KIND} · {RANKING_MARKET} · 상위 {RANKING_LIMIT}종목")
        else:
            ctx.log.info(f"종목 데이터 연결: 지정 종목 {len(SYMBOLS)}개")

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
        if event.schedule_id == "bollinger-sell-daily":
            await self.on_bar(ctx, event)
            return
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
        try:
            candidate_codes = await self._candidate_codes(ctx)
        except RouterError as exc:
            ctx.log.warning(f"종목 데이터 조회 보류 [{exc.code.value}]: {exc.message} · 다음 조회에서 재시도합니다.")
            return
        if not candidate_codes:
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
        for code in candidate_codes:
            if code in held_codes or code in pending_codes or code in tracked:
                continue
            if BUY_CONDITIONS:
                required = self._required_bar_count(BUY_CONDITIONS)
                try:
                    bars = await ctx.market.bars(
                        code, BAR_INTERVAL, count=required, venue=Venue(MARKET_VENUE), complete_only=True,
                    )
                except RouterError as exc:
                    ctx.log.warning(f"{code} 매수 조건 자료 조회 실패 [{exc.code.value}]: {exc.message}")
                    continue
                if len(bars) < required:
                    ctx.log.info(f"{code} 매수 판단 보류: 완성 봉 {len(bars)}/{required}개")
                    continue
                if not self._conditions_match(BUY_CONDITIONS, BUY_CONDITION_LOGIC, bars):
                    continue
            limit_price = None
            try:
                market_price = await self._current_price(ctx, code)
                if market_price <= 0:
                    raise RuntimeError(f"{MARKET_VENUE} 현재가가 0 이하입니다.")
                if BUY_ORDER_TYPE == "LIMIT":
                    limit_price = market_price
                buying_power = await ctx.account.buying_power(
                    code,
                    order_type=BUY_ORDER_TYPE,
                    price=limit_price,
                    route=ORDER_ROUTE,
                )
            except RouterError as exc:
                ctx.log.warning(f"{code} {MARKET_VENUE} 주문 전 조회 실패 [{exc.code.value}]: {exc.message}")
                continue
            except RuntimeError as exc:
                ctx.log.warning(f"{code} {MARKET_VENUE} 주문 전 조회 실패: {exc}")
                continue
            calculation_price = max(buying_power.calculation_price, market_price)
            if calculation_price <= 0:
                ctx.log.warning(f"{code} 주문 전 조회 실패: 보수적 계산단가를 확인할 수 없습니다.")
                continue
            budget_quantity = int(BUY_BUDGET_PER_SYMBOL // calculation_price)
            quantity = min(int(buying_power.maximum_quantity), budget_quantity)
            if quantity < 1:
                ctx.log.info(f"{code} 건너뜀: {MARKET_VENUE} 보수적 계산단가 {calculation_price}원 · 종목당 예산 {BUY_BUDGET_PER_SYMBOL}원")
                continue
            try:
                order_id = f"target-{code}-{event.scheduled_at:%Y%m%d%H%M%S}-buy"
                if BUY_ORDER_TYPE == "LIMIT":
                    order = await ctx.orders.limit_buy(
                        code=code, quantity=quantity, price=limit_price, route=ORDER_ROUTE,
                        client_order_id=order_id,
                    )
                else:
                    order = await ctx.orders.market_buy(
                        code=code, quantity=quantity, route=ORDER_ROUTE, client_order_id=order_id,
                    )
            except RouterError as exc:
                ctx.log.error(f"{code} 매수 요청 실패 [{exc.code.value}]: {exc.message}")
                continue
            if order.status is not OrderStatus.REJECTED:
                tracked[code] = str(quantity)
                ctx.state.set("strategy_positions", tracked)
                submitted += 1
                price_text = f" {limit_price}원" if limit_price is not None else ""
                ctx.log.info(f"{code} {BUY_ORDER_TYPE}{price_text} {quantity}주 매수 요청: {order.status.value}")
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
            required = self._required_bar_count(SELL_CONDITIONS)
            bars = await ctx.market.bars(
                code, BAR_INTERVAL, count=required, venue=Venue(MARKET_VENUE), complete_only=True,
            )
            if len(bars) < required:
                ctx.log.warning(f"{code} 매도 판단 보류: 완성 봉 {len(bars)}/{required}개")
                continue
            average_price = position.average_price
            matched = [
                item for item in SELL_CONDITIONS
                if self._condition_matches(item, bars, average_price)
            ]
            should_sell = (
                len(matched) == len(SELL_CONDITIONS)
                if SELL_CONDITION_LOGIC == "ALL"
                else bool(matched)
            )
            if not should_sell:
                continue
            sell_reason = (" 그리고 " if SELL_CONDITION_LOGIC == "ALL" else " 또는 ").join(
                self._condition_summary(item) for item in matched
            )
            tracked_quantity = position.strategy_quantity if position.strategy_quantity is not None else Decimal(str(tracked[code]))
            quantity = min(tracked_quantity, position.available_quantity)
            if quantity <= 0:
                continue
            limit_price = None
            try:
                order_id = f"exit-{code}-{event.scheduled_at:%Y%m%d%H%M%S}-sell"
                if SELL_ORDER_TYPE == "LIMIT":
                    limit_price = await self._current_price(ctx, code)
                    order = await ctx.orders.limit_sell(
                        code=code, quantity=quantity, price=limit_price, route=ORDER_ROUTE,
                        client_order_id=order_id,
                    )
                else:
                    order = await ctx.orders.market_sell(
                        code=code, quantity=quantity, route=ORDER_ROUTE, client_order_id=order_id,
                    )
            except RouterError as exc:
                ctx.log.error(f"{code} 매도 요청 실패 [{exc.code.value}]: {exc.message}")
                continue
            price_text = f" {limit_price}원" if limit_price is not None else ""
            ctx.log.info(f"{code} {sell_reason} · {SELL_ORDER_TYPE}{price_text} {quantity}주 매도 요청: {order.status.value}")
            if order.status is OrderStatus.FILLED:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)

    async def on_stop(self, ctx):
        ctx.log.info("조건검색·볼린저 블록 전략을 종료합니다.")
'''


def _render_condition_bollinger(
    settings: ConditionBollingerBlockSettings,
    layout: BlockEditorLayout | None = None,
) -> str:
    replacements = {
        "__BLOCK_TEMPLATE__": _literal(CONDITION_BOLLINGER_TEMPLATE),
        "__BLOCK_LAYOUT_JSON__": _layout_literal(layout),
        "__STRATEGY_ID__": _literal(settings.strategy_id),
        "__TRADING_ACCOUNT__": _literal(settings.trading_account),
        "__CONDITION_SOURCE__": _literal(settings.condition_source),
        "__MARKET_VENUE__": _literal(settings.market_venue),
        "__TARGET_SOURCE__": _literal(settings.target_source),
        "__HTS_USER_ID__": _literal(settings.hts_user_id),
        "__CONDITION_ID__": _literal(settings.condition_id),
        "__EXPECTED_CONDITION_NAME__": _literal(settings.expected_condition_name),
        "__RANKING_KIND__": _literal(settings.ranking_kind),
        "__RANKING_VENUE__": _literal(settings.ranking_venue),
        "__RANKING_MARKET__": _literal(settings.ranking_market),
        "__RANKING_LIMIT__": str(settings.ranking_limit),
        "__SYMBOLS_JSON__": _json_literal(settings.symbols),
        "__BUY_BUDGET_PER_SYMBOL__": _literal(str(settings.buy_budget_per_symbol)),
        "__TOTAL_OPERATING_BUDGET__": _literal(str(settings.total_operating_budget)),
        "__MAX_NEW_BUYS_PER_SCAN__": str(settings.max_new_buys_per_scan),
        "__BOLLINGER_PERIOD__": str(settings.bollinger_period),
        "__BOLLINGER_STDDEV__": _literal(str(settings.bollinger_stddev)),
        "__TAKE_PROFIT_MULTIPLIER__": _literal(str(settings.take_profit_multiplier)),
        "__STOP_LOSS_MULTIPLIER__": _literal(str(settings.stop_loss_multiplier)),
        "__USE_BOLLINGER_EXIT__": str(settings.use_bollinger_exit),
        "__USE_TAKE_PROFIT__": str(settings.use_take_profit),
        "__USE_STOP_LOSS__": str(settings.use_stop_loss),
        "__BUY_CONDITION_LOGIC__": _literal(settings.buy_condition_logic),
        "__BUY_CONDITIONS_JSON__": _json_literal(settings.buy_conditions),
        "__SELL_CONDITION_LOGIC__": _literal(settings.sell_condition_logic),
        "__SELL_CONDITIONS_JSON__": _json_literal(settings.sell_conditions),
        "__BUY_ORDER_TYPE__": _literal(settings.buy_order_type),
        "__SELL_ORDER_TYPE__": _literal(settings.sell_order_type),
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


def compile_block_strategy(
    settings: BlockStrategySettings | ConditionBollingerBlockSettings,
    *,
    template: str | None = None,
    layout: BlockEditorLayout | None = None,
) -> str:
    if isinstance(settings, ConditionBollingerBlockSettings):
        if template not in {None, CONDITION_BOLLINGER_TEMPLATE}:
            raise ValueError("Block template and settings do not match.")
        return _render_condition_bollinger(settings, layout)
    if template not in {None, BAR_OBSERVER_TEMPLATE}:
        raise ValueError("Block template and settings do not match.")
    return _render_bar_observer(settings, layout)


# Compatibility name for older extensions. New code should use the explicit
# compiler name because a block package is the source of truth.
render_block_strategy = compile_block_strategy


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
        "condition_source": "CONDITION_SOURCE", "market_venue": "MARKET_VENUE", "hts_user_id": "HTS_USER_ID",
        "condition_id": "CONDITION_ID", "expected_condition_name": "EXPECTED_CONDITION_NAME",
        "target_source": "TARGET_SOURCE", "ranking_kind": "RANKING_KIND",
        "ranking_venue": "RANKING_VENUE", "ranking_market": "RANKING_MARKET",
        "ranking_limit": "RANKING_LIMIT",
        "buy_budget_per_symbol": "BUY_BUDGET_PER_SYMBOL", "total_operating_budget": "TOTAL_OPERATING_BUDGET",
        "max_new_buys_per_scan": "MAX_NEW_BUYS_PER_SCAN", "bollinger_period": "BOLLINGER_PERIOD",
        "bollinger_stddev": "BOLLINGER_STDDEV", "take_profit_multiplier": "TAKE_PROFIT_MULTIPLIER",
        "stop_loss_multiplier": "STOP_LOSS_MULTIPLIER",
        "use_bollinger_exit": "USE_BOLLINGER_EXIT", "use_take_profit": "USE_TAKE_PROFIT",
        "use_stop_loss": "USE_STOP_LOSS", "buy_scan_seconds": "BUY_SCAN_SECONDS",
        "buy_condition_logic": "BUY_CONDITION_LOGIC", "sell_condition_logic": "SELL_CONDITION_LOGIC",
        "buy_order_type": "BUY_ORDER_TYPE", "sell_order_type": "SELL_ORDER_TYPE",
        "buy_start_time": "BUY_START_TIME", "buy_end_time": "BUY_END_TIME",
        "bar_interval": "BAR_INTERVAL", "sell_start_time": "SELL_START_TIME",
        "sell_end_time": "SELL_END_TIME", "close_time": "CLOSE_TIME",
        "close_retry_start": "CLOSE_RETRY_START", "close_retry_end": "CLOSE_RETRY_END",
        "close_confirm_time": "CLOSE_CONFIRM_TIME", "route": "ORDER_ROUTE",
    }
    required = {field: constants.get(source_name) for field, source_name in mapping.items()}
    # v0.1.13 packages always enabled the three built-in exit conditions.
    for field in ("use_bollinger_exit", "use_take_profit", "use_stop_loss"):
        if required[field] is None:
            required[field] = True
    defaults = {
        "target_source": "HTS_CONDITION", "market_venue": "KRX", "ranking_kind": "RISE", "ranking_venue": "KRX",
        "ranking_market": "ALL", "ranking_limit": 20,
        "buy_condition_logic": "ALL", "sell_condition_logic": "ANY",
        "buy_order_type": "MARKET", "sell_order_type": "MARKET",
    }
    for field, value in defaults.items():
        if required[field] is None:
            required[field] = value
    symbols_json = constants.get("SYMBOLS_JSON")
    try:
        required["symbols"] = json.loads(symbols_json) if isinstance(symbols_json, str) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        required["symbols"] = []
    buy_json = constants.get("BUY_CONDITIONS_JSON")
    sell_json = constants.get("SELL_CONDITIONS_JSON")
    try:
        required["buy_conditions"] = json.loads(buy_json) if isinstance(buy_json, str) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        required["buy_conditions"] = []
    try:
        required["sell_conditions"] = json.loads(sell_json) if isinstance(sell_json, str) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        required["sell_conditions"] = []
    if not required["sell_conditions"]:
        legacy_conditions: list[dict[str, object]] = []
        if required["use_take_profit"]:
            multiplier = Decimal(str(required["take_profit_multiplier"]))
            legacy_conditions.append({"left": "profit_rate", "operator": "GTE", "right": "value", "value": (multiplier - 1) * 100})
        if required["use_stop_loss"]:
            multiplier = Decimal(str(required["stop_loss_multiplier"]))
            legacy_conditions.append({"left": "profit_rate", "operator": "LTE", "right": "value", "value": (multiplier - 1) * 100})
        if required["use_bollinger_exit"]:
            legacy_conditions.append({"left": "close", "operator": "LT", "right": "bollinger_upper", "value": 0})
        required["sell_conditions"] = legacy_conditions or [item.model_dump() for item in DEFAULT_SELL_CONDITIONS]
    if any(value is None for value in required.values()):
        raise ValueError("This condition/Bollinger block has unsupported fields.")
    settings = ConditionBollingerBlockSettings.model_validate(required)
    return BlockStrategyPackage(
        name=name,
        template=CONDITION_BOLLINGER_TEMPLATE,
        settings=settings,
        layout=_layout_from_constants(constants),
    )


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
    return BlockStrategyPackage(
        name=name,
        template=BAR_OBSERVER_TEMPLATE,
        settings=settings,
        layout=_layout_from_constants(constants),
    )


def block_package_from_source(name: str, source: str) -> BlockStrategyPackage:
    tree, constants = _constants_from_source(source)
    template = constants.get("BLOCK_TEMPLATE")
    if template == CONDITION_BOLLINGER_TEMPLATE:
        return _condition_package(name, constants)
    if template not in {None, BAR_OBSERVER_TEMPLATE}:
        raise ValueError(f"Unsupported block template: {template}")
    return _bar_observer_package(name, tree, constants)
