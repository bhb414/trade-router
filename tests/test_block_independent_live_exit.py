"""Integration of generated live exits with independent worker event lanes."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trade_router import ScheduleKind
from trade_router_studio.strategy_worker import StrategyEventDispatcher
from test_block_realtime_exit_paths import (
    action, group, leaf, make_harness, stop, warm_candle_cache,
)
from test_block_sell_flows import SellHarness, close_condition, sell_flow, settings_for


def bar_event(harness, name="early"):
    return SimpleNamespace(schedule_id=f"condition-sell-{name}-bars-1",
                           kind=ScheduleKind.BAR_CLOSE, scheduled_at=harness.now, bar_interval="3m")


async def start(harness):
    await harness.strategy.on_start(harness.ctx)
    await harness.prepare_live_position()


def event_quote(harness, price="94"):
    now = harness.now.isoformat()
    return {"event_type": "quote", "source_alias": "primary", "quote": {
        "instrument": {"code": "005930"}, "price": price, "venue": "KRX", "provider": "kis",
        "received_at": now, "market_time": now, "correlation_id": "offline-live-test",
    }}


def test_actual_generated_strategy_classifies_raw_runtime_events():
    async def check():
        harness = make_harness(group("root", "ANY", leaf("candle", close_condition("150")), stop(), action=action("sell")))
        await start(harness)
        strategy = harness.strategy
        assert strategy.runtime_lane(event_quote(harness)) == "live"
        assert strategy.runtime_lane({"event_type": "order"}) == "execution"
        assert strategy.runtime_lane({"event_type": "fill"}) == "execution"
        assert strategy.runtime_lane({"event_type": "schedule", "kind": "INTERVAL", "schedule_id": "live-profit-watchdog"}) == "control"
        assert strategy.runtime_lane({"event_type": "schedule", "kind": "BAR_CLOSE", "schedule_id": "condition-sell-early-bars-1"}) == "sell"
        for schedule_id in harness.namespace["BUY_FLOW_SCHEDULES"]:
            assert strategy.runtime_lane({"event_type": "schedule", "kind": "INTERVAL", "schedule_id": schedule_id}) == "buy"

    asyncio.run(check())


@pytest.mark.parametrize("independent_flow", [False, True])
def test_blocked_candle_lookup_does_not_delay_stop_in_same_or_or_other_flow(independent_flow):
    async def check():
        if independent_flow:
            harness = SellHarness(settings_for([
                sell_flow("candles", sell_start_time="09:00", sell_end_time="15:20", sell_condition_groups=[],
                          sell_rule_tree=group("candle-root", "ALL", leaf("candle", close_condition("999")), action=action("candle-sell"))),
                sell_flow("live", sell_start_time="09:00", sell_end_time="15:20", sell_condition_groups=[],
                          sell_rule_tree=group("live-root", "ALL", stop(), action=action("stop-sell"))),
            ]), now="10:00:00")
            flow_name = "candles"
        else:
            harness = make_harness(group("root", "ANY", leaf("candle", close_condition("999")), stop(), action=action("sell")))
            flow_name = "early"
        started, release, submitted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_bars, original_sell = harness.bars, harness.sell

        async def slow_bars(*args, **kwargs):
            started.set()
            await release.wait()
            return await original_bars(*args, **kwargs)

        async def record_sell(**kwargs):
            result = await original_sell(**kwargs)
            submitted.set()
            return result

        harness.ctx.market.bars = slow_bars
        harness.ctx.orders.market_sell = record_sell
        await start(harness)
        dispatcher = StrategyEventDispatcher(harness.strategy, harness.ctx)
        event = bar_event(harness, flow_name)
        try:
            await dispatcher.submit({"event_type": "schedule", "kind": event.kind.value,
                                     "schedule_id": event.schedule_id, "scheduled_at": event.scheduled_at.isoformat(),
                                     "bar_interval": "3m"})
            await asyncio.wait_for(started.wait(), 1)
            await dispatcher.submit(event_quote(harness))
            await asyncio.wait_for(submitted.wait(), 1)
            assert len(harness.orders) == 1
            assert not release.is_set()
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert len(harness.orders) == 1
            assert not dispatcher.failure.done()
        finally:
            await dispatcher.close()

    asyncio.run(check())


@pytest.mark.parametrize("operator, warm, expected", [
    ("ALL", False, 0), ("ALL", True, 1), ("ANY", False, 1),
])
def test_live_mixed_rule_uses_fresh_cache_without_fetching(operator, warm, expected):
    async def check():
        harness = make_harness(group("root", operator, leaf("candle", close_condition("150")), stop(), action=action("sell")))
        await harness.prepare_live_position()
        if warm:
            await warm_candle_cache(harness)

        async def never_fetch(*args, **kwargs):
            pytest.fail("Live callback must not perform candle I/O")

        harness.ctx.market.bars = never_fetch
        await asyncio.wait_for(harness.strategy.on_quote(harness.ctx, harness.quote("94")), 1)
        assert len(harness.orders) == expected

    asyncio.run(check())


def test_previous_candle_period_cache_does_not_bypass_and_guard():
    async def check():
        harness = make_harness(group("root", "ALL", leaf("candle", close_condition("150")), stop(), action=action("sell")))
        await harness.prepare_live_position()
        await warm_candle_cache(harness)
        harness.now += timedelta(minutes=3)

        async def never_fetch(*args, **kwargs):
            pytest.fail("Expired cache must defer AND, not fetch inside live callback")

        harness.ctx.market.bars = never_fetch
        await harness.strategy.on_quote(harness.ctx, harness.quote("94"))
        assert harness.orders == []

    asyncio.run(check())


def test_simultaneous_watchdog_bar_and_quote_submit_one_sell():
    async def check():
        harness = make_harness(group("root", "ANY", leaf("candle", close_condition("150")), stop(), action=action("sell")))
        await start(harness)
        await warm_candle_cache(harness)
        submission_started, release = asyncio.Event(), asyncio.Event()
        attempted = []

        async def slow_sell(**kwargs):
            attempted.append(kwargs)
            submission_started.set()
            await release.wait()
            return await harness.sell(**kwargs)

        async def rest_quote(*args, **kwargs):
            return harness.quote("94")

        harness.ctx.orders.market_sell = slow_sell
        harness.ctx.market.quote = rest_quote
        candle = asyncio.create_task(harness.strategy.on_bar(harness.ctx, bar_event(harness)))
        await asyncio.wait_for(submission_started.wait(), 1)
        live = asyncio.create_task(harness.strategy.on_quote(harness.ctx, harness.quote("94")))
        watchdog = asyncio.create_task(harness.strategy.on_timer(harness.ctx, SimpleNamespace(
            schedule_id="live-profit-watchdog", scheduled_at=harness.now,
        )))
        try:
            await asyncio.wait_for(asyncio.gather(live, watchdog), 1)
            assert len(attempted) == 1
            release.set()
            await asyncio.wait_for(candle, 1)
            assert len(harness.orders) == 1
        finally:
            for task in (candle, live, watchdog):
                task.cancel()
            await asyncio.gather(candle, live, watchdog, return_exceptions=True)

    asyncio.run(check())
