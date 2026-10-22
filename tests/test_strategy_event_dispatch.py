from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trade_router import Strategy
from trade_router_studio.event_bus import RuntimeEventBuffer, StrategyEventBus
from trade_router_studio.strategy_worker import StrategyEventDispatcher, _run_events


def quote(price: str = "94", *, code: str = "005930", venue: str = "KRX", sequence: int = 0) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {"event_type": "quote", "sequence": sequence, "source_alias": "primary", "quote": {
        "instrument": {"code": code}, "price": price, "venue": venue, "provider": "kis",
        "market_time": now, "received_at": now, "correlation_id": str(sequence),
    }}


def schedule(name: str, sequence: int = 0, *, bar: bool = False) -> dict:
    return {"event_type": "schedule", "schedule_id": name, "sequence": sequence,
            "kind": "BAR_CLOSE" if bar else "INTERVAL", "scheduled_at": datetime.now(timezone.utc).isoformat(),
            "bar_interval": "3m" if bar else None}


def context() -> SimpleNamespace:
    return SimpleNamespace(log=SimpleNamespace(warning=lambda message: None))


class FakeRuntime:
    def __init__(self) -> None:
        self.events: asyncio.Queue[dict] = asyncio.Queue()

    async def next_event(self) -> dict:
        return await self.events.get()


def test_buy_search_and_sell_bar_fetch_do_not_block_live_quote() -> None:
    async def check() -> None:
        class Managed(Strategy):
            def __init__(self) -> None:
                self.buy_started, self.sell_started, self.live_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
                self.release = asyncio.Event()
                self.active = 0
                self.cancelled = 0

            def runtime_lane(self, event):
                return "live" if event["event_type"] == "quote" else "sell" if event["kind"] == "BAR_CLOSE" else "buy"

            async def on_timer(self, ctx, event):
                self.active += 1
                self.buy_started.set()
                try:
                    await self.release.wait()
                finally:
                    self.active -= 1
                    self.cancelled += 1

            async def on_bar(self, ctx, event):
                self.active += 1
                self.sell_started.set()
                try:
                    await self.release.wait()
                finally:
                    self.active -= 1
                    self.cancelled += 1

            async def on_quote(self, ctx, value):
                assert self.active == 2
                assert value.price == Decimal("94")
                self.live_seen.set()

        strategy, runtime = Managed(), FakeRuntime()
        stop = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(_run_events(strategy, context(), runtime, stop))
        runtime.events.put_nowait(schedule("buy"))
        await asyncio.wait_for(strategy.buy_started.wait(), 1)
        runtime.events.put_nowait(schedule("sell", bar=True))
        await asyncio.wait_for(strategy.sell_started.wait(), 1)
        runtime.events.put_nowait(quote())
        await asyncio.wait_for(strategy.live_seen.wait(), 1)
        stop.set_result(None)
        await asyncio.wait_for(task, 1)
        assert strategy.active == 0
        assert strategy.cancelled == 2
        assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("strategy-")]

    asyncio.run(check())


def test_user_strategy_remains_serial() -> None:
    async def check() -> None:
        class Custom(Strategy):
            def __init__(self):
                self.started, self.release, self.quoted = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def on_timer(self, ctx, event):
                self.started.set()
                await self.release.wait()

            async def on_quote(self, ctx, event):
                self.quoted.set()

        strategy, runtime = Custom(), FakeRuntime()
        stop = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(_run_events(strategy, context(), runtime, stop))
        runtime.events.put_nowait(schedule("buy"))
        await strategy.started.wait()
        runtime.events.put_nowait(quote())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not strategy.quoted.is_set()
        strategy.release.set()
        await asyncio.wait_for(strategy.quoted.wait(), 1)
        stop.set_result(None)
        await task

    asyncio.run(check())


def test_live_lane_is_serial_and_does_not_duplicate_order_decisions() -> None:
    async def check() -> None:
        class Managed(Strategy):
            def __init__(self):
                self.active = self.max_active = self.requests = self.count = 0
                self.pending = False
                self.finished = asyncio.Event()

            def runtime_lane(self, event):
                return "live"

            async def on_quote(self, ctx, value):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if value.price <= 97 and not self.pending:
                    await asyncio.sleep(0)
                    self.pending = True
                    self.requests += 1
                self.active -= 1
                self.count += 1
                if self.count == 20:
                    self.finished.set()

        strategy = Managed()
        dispatcher = StrategyEventDispatcher(strategy, context())
        try:
            for i in range(20):
                await dispatcher.submit(quote(sequence=i))
            await asyncio.wait_for(strategy.finished.wait(), 1)
            assert strategy.max_active == 1
            assert strategy.requests == 1
            assert len(dispatcher.tasks) == 5
        finally:
            await dispatcher.close()

    asyncio.run(check())


def test_blocked_buy_schedule_queue_is_bounded_without_blocking_live_lane() -> None:
    async def check() -> None:
        class Managed(Strategy):
            def __init__(self):
                self.started, self.release, self.quoted = asyncio.Event(), asyncio.Event(), asyncio.Event()

            def runtime_lane(self, event):
                return "live" if event["event_type"] == "quote" else "buy"

            async def on_timer(self, ctx, value):
                self.started.set()
                await self.release.wait()

            async def on_quote(self, ctx, value):
                self.quoted.set()

        strategy = Managed()
        dispatcher = StrategyEventDispatcher(strategy, context(), capacity=2)
        try:
            await dispatcher.submit(schedule("search"))
            await strategy.started.wait()
            for i in range(1000):
                await dispatcher.submit(schedule("search", i))
            assert dispatcher.queues["buy"].qsize() == 1
            assert dispatcher.queues["buy"].coalesced == 999
            await dispatcher.submit(quote())
            await asyncio.wait_for(strategy.quoted.wait(), 1)
        finally:
            await dispatcher.close()

    asyncio.run(check())


def test_callback_failure_cancels_and_awaits_other_lanes() -> None:
    async def check() -> None:
        class Broken(Strategy):
            def __init__(self):
                self.started, self.cancelled = asyncio.Event(), asyncio.Event()

            def runtime_lane(self, event):
                return "live" if event["event_type"] == "quote" else "buy"

            async def on_timer(self, ctx, value):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()

            async def on_quote(self, ctx, value):
                raise ValueError("test failure")

        strategy, runtime = Broken(), FakeRuntime()
        stop = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(_run_events(strategy, context(), runtime, stop))
        runtime.events.put_nowait(schedule("search"))
        await strategy.started.wait()
        runtime.events.put_nowait(quote())
        with pytest.raises(ValueError, match="test failure"):
            await asyncio.wait_for(task, 1)
        assert strategy.cancelled.is_set()
        stop.cancel()

    asyncio.run(check())


def test_event_bus_prioritizes_quote_ahead_of_buy_schedule_backlog() -> None:
    async def check() -> None:
        bus = StrategyEventBus()
        for i in range(30):
            await bus.publish("test", schedule(f"search-{i}"))
        await bus.publish("test", quote())
        assert (await bus.next_event("test"))["event_type"] == "quote"
        assert (await bus.next_event("test"))["schedule_id"] == "search-0"

    asyncio.run(check())


def test_event_bus_priority_has_fairness_for_scheduled_events() -> None:
    async def check() -> None:
        bus = StrategyEventBus()
        await bus.publish("test", schedule("watchdog"))
        for i in range(30):
            await bus.publish("test", quote(sequence=i))
        events = [await bus.next_event("test") for _ in range(17)]
        assert events[-1]["schedule_id"] == "watchdog"

    asyncio.run(check())


def test_overload_keeps_stop_touch_profit_touch_and_latest_quote() -> None:
    async def check() -> None:
        buffer = RuntimeEventBuffer(1)
        for i, price in enumerate(("100", "94", "96", "108", "99")):
            await buffer.put(quote(price, sequence=i))
        assert buffer.qsize() == 3
        assert buffer.high_water == 1
        values = [await buffer.get() for _ in range(3)]
        assert [row["quote"]["price"] for row in values] == ["94", "108", "99"]
        assert [row["sequence"] for row in values] == [1, 3, 4]
        assert buffer.coalesced == 2

    asyncio.run(check())


def test_quote_extrema_are_separate_for_different_venues() -> None:
    async def check() -> None:
        buffer = RuntimeEventBuffer(2)
        await buffer.put(quote("94", venue="KRX"))
        await buffer.put(quote("103", venue="NXT"))
        await buffer.put(quote("99", venue="KRX"))
        values = [await buffer.get() for _ in range(buffer.qsize())]
        assert {(row["quote"]["venue"], row["quote"]["price"]) for row in values} == {
            ("KRX", "94"), ("KRX", "99"), ("NXT", "103"),
        }

    asyncio.run(check())


def test_order_fill_backpressure_never_drops_notifications() -> None:
    async def check() -> None:
        bus = StrategyEventBus(queue_size=1)
        await bus.publish("test", {"event_type": "order", "sequence": 1})
        pending = asyncio.create_task(bus.publish("test", {"event_type": "fill", "sequence": 2}))
        await asyncio.sleep(0)
        assert not pending.done()
        # Other strategies are not held behind the full queue's bus lock.
        await asyncio.wait_for(bus.publish("other", {"event_type": "order", "sequence": 3}), 1)
        assert (await bus.next_event("test"))["sequence"] == 1
        await asyncio.wait_for(pending, 1)
        assert (await bus.next_event("test"))["sequence"] == 2

    asyncio.run(check())


def test_unregister_releases_blocked_publisher_and_waiter() -> None:
    async def check() -> None:
        bus = StrategyEventBus(queue_size=1)
        await bus.publish("full", {"event_type": "order", "sequence": 1})
        pending = asyncio.create_task(bus.publish("full", {"event_type": "fill", "sequence": 2}))
        waiting = asyncio.create_task(bus.next_event("empty", timeout=20))
        await asyncio.sleep(0)
        await bus.unregister("full")
        await bus.unregister("empty")
        await asyncio.wait_for(pending, 1)
        assert await asyncio.wait_for(waiting, 1) is None

    asyncio.run(check())
