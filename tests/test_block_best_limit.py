"""Offline submission/session contracts for best opposing-quote orders."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trade_router import ScheduleKind
from trade_router_studio.block_packages import block_package_from_source, render_block_strategy
from test_block_buy_flows import Harness, at, flow, nested_action_tree, settings_for
from test_block_sell_flows import SellHarness, sell_flow, settings_for as sell_settings_for


def buy_harness(clock="08:10:00", route="NXT"):
    settings = settings_for([
        flow(buy_start_time="08:00", buy_end_time="20:00",
             buy_rule_tree=nested_action_tree("best", "BEST_LIMIT")),
    ], route=route)
    harness = Harness(settings, now=clock, pending=True)
    harness.power_calls = []

    async def submit(**kwargs):
        return await harness.buy(order_type="BEST_LIMIT", **kwargs)

    async def buying_power(code, **kwargs):
        harness.power_calls.append(kwargs)
        return await harness.buying_power(code, **kwargs)

    harness.ctx.orders.best_limit_buy = submit
    harness.ctx.account.buying_power = buying_power
    return harness, settings


def sell_harness(clock="08:10:00", route="NXT"):
    settings = sell_settings_for([
        sell_flow(sell_start_time="08:00", sell_end_time="20:00"),
    ], route=route, sell_order_type="BEST_LIMIT")
    harness = SellHarness(settings, now=clock)

    async def submit(**kwargs):
        return await harness.sell(order_type="BEST_LIMIT", **kwargs)

    harness.ctx.orders.best_limit_sell = submit
    return harness, settings


@pytest.mark.parametrize("route", ["NXT", "SOR", "AUTO"])
@pytest.mark.parametrize("clock", ["08:10:00", "09:30:00", "15:40:00", "19:59:00"])
def test_best_limit_buy_submits_correct_type_without_explicit_price_and_reserves_symbol(route, clock):
    harness, _ = buy_harness(clock, route)

    async def exercise():
        await harness.tick()
        harness.now = harness.now.replace(second=20)
        await harness.tick()

    asyncio.run(exercise())
    assert len(harness.order_calls) == 1
    order = harness.order_calls[0]
    assert order["order_type"] == "BEST_LIMIT"
    assert order["route"] == route
    assert "price" not in order
    assert harness.power_calls == [{"order_type": "BEST_LIMIT", "price": None, "route": route}]
    assert "005930" in harness.ctx.state.get("pending_buy_submissions")


@pytest.mark.parametrize("route", ["NXT", "SOR", "AUTO"])
@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("clock", ["08:10:00", "15:40:00", "19:59:00"])
def test_best_limit_sell_uses_best_limit_for_bar_and_realtime_paths(route, live, clock):
    harness, _ = sell_harness(clock, route)

    async def exercise():
        if live:
            await harness.prepare_live_position()
            await harness.strategy.on_quote(harness.ctx, harness.quote())
            await harness.strategy.on_quote(harness.ctx, harness.quote())
        else:
            await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
                schedule_id="condition-sell-live", scheduled_at=harness.now,
                kind=ScheduleKind.INTERVAL,
            ))

    asyncio.run(exercise())
    assert len(harness.orders) == 1
    assert harness.orders[0]["order_type"] == "BEST_LIMIT"
    assert harness.orders[0]["route"] == route
    assert harness.orders[0]["quantity"] == Decimal("5")
    assert "price" not in harness.orders[0]


@pytest.mark.parametrize("route", ["NXT", "SOR", "AUTO"])
@pytest.mark.parametrize("side", ["BUY", "SELL", "LIVE_SELL"])
def test_auction_wait_does_not_suppress_best_limit_for_rest_of_day(route, side):
    harness, _ = buy_harness("15:35:00", route) if side == "BUY" else sell_harness("15:35:00", route)

    async def tick():
        if side == "BUY":
            await harness.tick()
        elif side == "LIVE_SELL":
            await harness.strategy.on_quote(harness.ctx, harness.quote())
        else:
            await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
                schedule_id="condition-sell-live", scheduled_at=harness.now,
                kind=ScheduleKind.INTERVAL,
            ))

    async def exercise():
        if side == "LIVE_SELL":
            await harness.prepare_live_position()
        await tick()
        assert not (harness.order_calls if side == "BUY" else harness.orders)
        assert not harness.ctx.state.get("daily_buy_exclusions", {}).get("symbols")
        assert not harness.ctx.state.get("daily_order_suppressions", {}).get("orders")
        harness.now = at("15:40:00")
        await tick()

    asyncio.run(exercise())
    assert len(harness.order_calls if side == "BUY" else harness.orders) == 1


@pytest.mark.parametrize(("route", "clock", "expected"), [
    ("NXT", "07:59:59", False), ("NXT", "08:00:00", True),
    ("NXT", "08:49:59", True), ("NXT", "08:50:00", False),
    ("NXT", "09:00:00", False), ("NXT", "09:00:29", False),
    ("NXT", "09:00:30", True), ("NXT", "15:19:59", True),
    ("NXT", "15:20:00", False), ("NXT", "15:30:00", False),
    ("NXT", "15:39:59", False), ("NXT", "15:40:00", True),
    ("NXT", "19:59:59", True), ("NXT", "20:00:00", False),
    ("SOR", "09:00:00", True), ("SOR", "15:20:00", False),
    ("SOR", "15:35:00", False), ("SOR", "15:40:00", True),
    ("AUTO", "08:10:00", True), ("AUTO", "15:35:00", False),
    ("AUTO", "15:40:00", True),
    ("KRX", "08:10:00", False), ("KRX", "09:00:00", True),
    ("KRX", "15:20:00", False), ("KRX", "16:00:00", False),
])
def test_best_limit_session_boundaries(route, clock, expected):
    harness, _ = buy_harness(route=route)
    assert harness.strategy._order_type_session_is_open(at(clock), route, "BEST_LIMIT") is expected


def test_best_limit_rechecks_session_after_buying_power_request():
    harness, _ = buy_harness("15:19:59", "SOR")
    original = harness.ctx.account.buying_power

    async def delayed(code, **kwargs):
        result = await original(code, **kwargs)
        harness.now = at("15:20:00")
        return result

    harness.ctx.account.buying_power = delayed
    asyncio.run(harness.tick())
    assert harness.order_calls == []
    assert not harness.ctx.state.get("pending_buy_submissions")


def test_best_limit_settings_survive_python_and_block_round_trip():
    for _, settings in (buy_harness(), sell_harness()):
        restored = block_package_from_source("최유리지정가", render_block_strategy(settings))
        assert restored.settings.model_dump(mode="json") == settings.model_dump(mode="json")
