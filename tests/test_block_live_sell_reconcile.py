"""Focused contracts for the managed block strategy's realtime sell guard."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from trade_router import OrderStatus
from trade_router_studio.block_packages import (
    DEFAULT_CONDITION_BOLLINGER_SETTINGS,
    render_block_strategy,
)


SEOUL = ZoneInfo("Asia/Seoul")


class MemoryState:
    def __init__(self) -> None:
        self.values = {
            "ownership_initialized": True,
            "strategy_positions": {"079650": "5"},
        }

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value) -> None:
        self.values[key] = value


class LiveSellHarness:
    def __init__(self) -> None:
        namespace: dict[str, object] = {}
        source = render_block_strategy(DEFAULT_CONDITION_BOLLINGER_SETTINGS)
        exec(compile(source, "<managed-block>", "exec"), namespace)
        harness = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return harness.now.astimezone(tz) if tz is not None else harness.now

        namespace["datetime"] = Clock
        self.strategy = namespace["ManagedBlockStrategy"]()
        self.now = datetime(2026, 10, 20, 10, 0, tzinfo=SEOUL)
        self.position = SimpleNamespace(
            instrument=SimpleNamespace(code="079650"),
            quantity=Decimal("5"),
            available_quantity=Decimal("5"),
            average_price=Decimal("100"),
            strategy_quantity=Decimal("5"),
        )
        self.positions = [self.position]
        self.open_order_rows: list[object] = []
        self.submitted: list[dict[str, object]] = []
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.ctx = SimpleNamespace(
            state=MemoryState(),
            account=SimpleNamespace(
                balance=self.balance,
                open_orders=self.open_orders,
            ),
            market=SimpleNamespace(
                subscribe_quotes=self.subscribe_quotes,
                unsubscribe=self.unsubscribe,
            ),
            orders=SimpleNamespace(market_sell=self.market_sell),
            log=SimpleNamespace(
                info=lambda message: None,
                warning=lambda message: None,
                error=lambda message: None,
            ),
        )

    async def balance(self):
        return SimpleNamespace(positions=tuple(self.positions))

    async def open_orders(self):
        return tuple(self.open_order_rows)

    async def subscribe_quotes(self, code, *, venue):
        self.subscribed.append(f"{venue.value}:{code}")
        return ({"id": f"quote-{venue.value}-{code}"},)

    async def unsubscribe(self, subscription_id):
        self.unsubscribed.append(subscription_id)

    async def market_sell(self, **kwargs):
        self.submitted.append(kwargs)
        return SimpleNamespace(status=OrderStatus.ACKNOWLEDGED)

    def quote(self):
        return SimpleNamespace(
            instrument=SimpleNamespace(code="079650"),
            price=Decimal("104"),
            delayed=False,
            stale=False,
            received_at=self.now,
        )

    def broker_sell(self):
        return SimpleNamespace(
            instrument=SimpleNamespace(code="079650"),
            remaining_quantity=Decimal("5"),
            side=SimpleNamespace(value="SELL"),
        )


def test_realtime_sell_guard_releases_after_external_cancel_without_duplicates() -> None:
    harness = LiveSellHarness()

    async def exercise() -> None:
        balance = await harness.balance()
        await harness.strategy._refresh_live_positions(harness.ctx, balance, ())

        # The first matching quote reserves the symbol before submitting.
        await harness.strategy.on_quote(harness.ctx, harness.quote())
        assert len(harness.submitted) == 1

        # The broker snapshot can lag a fresh acknowledgement.  Quotes inside
        # the local grace window must not create a duplicate sell.
        harness.now = datetime(2026, 10, 20, 10, 0, 0, 500000, tzinfo=SEOUL)
        await harness.strategy.on_quote(harness.ctx, harness.quote())
        assert len(harness.submitted) == 1

        # Once the broker reports it open, the reservation remains authoritative.
        harness.now = datetime(2026, 10, 20, 10, 0, 1, tzinfo=SEOUL)
        harness.open_order_rows = [harness.broker_sell()]
        await harness.strategy._refresh_live_positions(
            harness.ctx, await harness.balance(), await harness.open_orders()
        )
        harness.now = datetime(2026, 10, 20, 10, 0, 2, tzinfo=SEOUL)
        await harness.strategy.on_quote(harness.ctx, harness.quote())
        assert len(harness.submitted) == 1

        # A cancel from another device removes the broker row.  After the short
        # reconciliation interval, a still-matching quote may submit again.
        harness.open_order_rows = []
        harness.now = datetime(2026, 10, 20, 10, 0, 5, tzinfo=SEOUL)
        await harness.strategy.on_quote(harness.ctx, harness.quote())

    asyncio.run(exercise())

    assert len(harness.submitted) == 2
    assert harness.submitted[0]["code"] == "079650"
    assert harness.submitted[1]["code"] == "079650"


def test_realtime_sell_guard_clears_after_external_fill_without_reselling() -> None:
    harness = LiveSellHarness()

    async def exercise() -> None:
        await harness.strategy._refresh_live_positions(
            harness.ctx, await harness.balance(), ()
        )
        await harness.strategy.on_quote(harness.ctx, harness.quote())
        assert len(harness.submitted) == 1

        harness.now = datetime(2026, 10, 20, 10, 0, 1, tzinfo=SEOUL)
        harness.open_order_rows = [harness.broker_sell()]
        await harness.strategy._refresh_live_positions(
            harness.ctx, await harness.balance(), await harness.open_orders()
        )

        # Another device fills the sell: the position and open order both vanish.
        harness.positions = []
        harness.open_order_rows = []
        harness.now = datetime(2026, 10, 20, 10, 0, 5, tzinfo=SEOUL)
        await harness.strategy.on_quote(harness.ctx, harness.quote())

    asyncio.run(exercise())

    assert len(harness.submitted) == 1
    assert "079650" not in harness.strategy._live_sell_pending
    assert "079650" not in harness.strategy._live_positions
    assert harness.unsubscribed == ["quote-KRX-079650"]

