from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .scheduler import SchedulerPlan


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_amount: Decimal | None = None
    max_daily_buy_amount: Decimal | None = None
    max_exposure: Decimal | None = None
    max_open_orders: int | None = None
    max_daily_buy_count: int | None = None

    def __post_init__(self) -> None:
        numeric = (
            self.max_order_amount,
            self.max_daily_buy_amount,
            self.max_exposure,
        )
        if any(value is not None and value <= 0 for value in numeric):
            raise ValueError("Risk-limit amounts must be positive.")
        counts = (self.max_daily_buy_count, self.max_open_orders)
        if any(value is not None and value < 1 for value in counts):
            raise ValueError("Risk-limit counts must be positive.")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    id: str
    trading_account: str
    market_sources: dict[str, str] = field(default_factory=dict)
    required_capabilities: dict[str, tuple[str, ...]] = field(default_factory=dict)
    limits: RiskLimits = field(default_factory=RiskLimits)

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.trading_account.strip():
            raise ValueError("Strategy id and trading account are required.")


class StrategyContext:
    """Runtime-owned services injected into a Strategy instance."""

    def __init__(
        self,
        *,
        config: StrategyConfig,
        market: Any,
        account: Any,
        orders: Any,
        discovery: Any,
        state: Any,
        logger: Any,
        scheduler: SchedulerPlan | None = None,
        extensions: Any = None,
    ) -> None:
        self.config = config
        self.market = market
        self.account = account
        self.orders = orders
        self.discovery = discovery
        self.state = state
        self.log = logger
        self.scheduler = scheduler or SchedulerPlan()
        self.extensions = extensions


class Strategy:
    config: StrategyConfig

    async def on_start(self, ctx: StrategyContext) -> None:
        return None

    async def on_quote(self, ctx: StrategyContext, quote: Any) -> None:
        return None

    async def on_orderbook(self, ctx: StrategyContext, orderbook: Any) -> None:
        return None

    async def on_bar(self, ctx: StrategyContext, event: Any) -> None:
        return None

    async def on_timer(self, ctx: StrategyContext, event: Any) -> None:
        return None

    async def on_order(self, ctx: StrategyContext, order: Any) -> None:
        return None

    async def on_fill(self, ctx: StrategyContext, fill: Any) -> None:
        return None

    async def on_stop(self, ctx: StrategyContext) -> None:
        return None
