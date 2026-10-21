"""Offline checks for a silent websocket beside completed-candle sell rules."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trade_router import RouterError, RouterErrorCode, Venue
from trade_router_studio.block_packages import block_package_from_source, render_block_strategy
from test_block_sell_flows import (
    SellHarness, close_condition, condition_group, profit_condition, sell_flow, settings_for,
)


def watchdog_harness(*, groups=None):
    settings = settings_for([
        sell_flow(
            sell_start_time="09:00", sell_end_time="15:20",
            sell_condition_groups=groups or [
                condition_group("mixed", profit_condition(), close_condition("150")),
            ],
        ),
    ])
    harness = SellHarness(settings, now="10:00:01")
    harness.rest_calls = []
    harness.rest_price = "104"
    harness.rest_changes = {}

    async def fresh_rest(code, *, venue):
        harness.rest_calls.append((code, venue))
        quote = SimpleNamespace(
            instrument=SimpleNamespace(code=code), price=Decimal(harness.rest_price),
            delayed=False, stale=False, received_at=harness.now,
            market_time=harness.now - timedelta(seconds=60),
            venue=venue, source_alias="primary",
        )
        for name, value in harness.rest_changes.items():
            setattr(quote, name, value)
        return quote

    harness.ctx.market.quote = fresh_rest
    return harness, settings


def tick(harness):
    return harness.strategy.on_timer(harness.ctx, SimpleNamespace(
        schedule_id="live-profit-watchdog", scheduled_at=harness.now,
    ))


def test_watchdog_is_independent_of_completed_candle_schedule_and_settings():
    harness, settings = watchdog_harness()
    asyncio.run(harness.strategy.on_start(harness.ctx))
    calls = {kwargs["schedule_id"]: (method, args, kwargs)
             for method, args, kwargs in harness.scheduler.calls}
    assert calls["live-profit-watchdog"][2]["seconds"] == 5
    assert calls["condition-sell-early-bars-1"][0] == "on_bar_close"
    assert calls["condition-sell-early-bars-1"][1] == ("3m",)
    restored = block_package_from_source("watchdog", render_block_strategy(settings))
    assert restored.settings.model_dump() == settings.model_dump()


def test_silent_stream_falls_back_before_next_candle_without_duplicate_order():
    harness, _ = watchdog_harness()

    async def exercise():
        await harness.prepare_live_position()
        await harness.strategy.on_quote(harness.ctx, harness.quote("102"))
        last_tick = harness.strategy._last_live_quote_at["005930"]
        harness.now += timedelta(seconds=9)
        await tick(harness)
        assert harness.rest_calls == []
        harness.now += timedelta(seconds=1)
        await tick(harness)
        assert len(harness.orders) == 1
        assert harness.orders[0]["quantity"] == Decimal("5")
        assert harness.strategy._last_live_quote_at["005930"] == last_tick
        assert harness.bar_calls == []

        async def pending_order():
            return (SimpleNamespace(
                instrument=SimpleNamespace(code="005930"),
                remaining_quantity=Decimal("5"), side=SimpleNamespace(value="SELL"),
            ),)

        harness.ctx.account.open_orders = pending_order
        harness.now += timedelta(seconds=5)
        await tick(harness)
        assert len(harness.orders) == 1

    asyncio.run(exercise())


def test_fallback_does_not_evaluate_candle_only_branch_when_profit_is_false():
    harness, _ = watchdog_harness()
    harness.rest_price = "102"

    async def exercise():
        await harness.prepare_live_position()
        await tick(harness)

    asyncio.run(exercise())
    assert len(harness.rest_calls) == 1
    assert harness.bar_calls == []
    assert harness.orders == []


def test_fallback_keeps_and_conditions_instead_of_selling_on_profit_alone():
    harness, _ = watchdog_harness(groups=[
        condition_group("profit", profit_condition()),
        condition_group("candle", close_condition("999")),
    ])

    async def exercise():
        await harness.prepare_live_position()
        await tick(harness)

    asyncio.run(exercise())
    assert len(harness.rest_calls) == 1
    assert harness.bar_calls
    assert harness.orders == []


@pytest.mark.parametrize("changes", [
    {"stale": True}, {"delayed": True}, {"price": Decimal("0")},
    {"price": Decimal("NaN")}, {"source_alias": "other-api"}, {"venue": Venue.NXT},
    {"age": 11}, {"age": -2}, {"naive": True},
])
def test_unusable_websocket_tick_neither_triggers_sell_nor_suppresses_rest(changes):
    harness, _ = watchdog_harness()

    async def exercise():
        await harness.prepare_live_position()
        quote = harness.quote()
        for name, value in changes.items():
            if name == "age":
                quote.received_at = harness.now - timedelta(seconds=value)
            elif name == "naive":
                quote.received_at = harness.now.replace(tzinfo=None)
            else:
                setattr(quote, name, value)
        await harness.strategy.on_quote(harness.ctx, quote)
        assert harness.orders == []
        assert harness.strategy._last_live_quote_at == {}
        await tick(harness)

    asyncio.run(exercise())
    assert len(harness.rest_calls) == 1
    assert len(harness.orders) == 1


@pytest.mark.parametrize("kind", ["stale", "old", "foreign-symbol", "foreign-source"])
def test_fallback_rejects_unusable_rest_result(kind):
    harness, _ = watchdog_harness()
    harness.rest_changes = {
        "stale": {"stale": True},
        "old": {"received_at": harness.now - timedelta(seconds=11)},
        "foreign-symbol": {"instrument": SimpleNamespace(code="000660")},
        "foreign-source": {"source_alias": "other-api"},
    }[kind]

    async def exercise():
        await harness.prepare_live_position()
        await tick(harness)

    asyncio.run(exercise())
    assert len(harness.rest_calls) == 1
    assert harness.orders == []


def test_watchdog_respects_window_and_does_not_adopt_account_only_holdings():
    harness, _ = watchdog_harness()

    async def exercise():
        await harness.prepare_live_position()
        harness.now = harness.now.replace(hour=16)
        await tick(harness)
        assert harness.rest_calls == []
        harness.now = harness.now.replace(hour=10)
        harness.position.strategy_quantity = Decimal("0")
        await tick(harness)

    asyncio.run(exercise())
    assert harness.rest_calls == []
    assert harness.orders == []


def test_quote_expiring_during_account_lookup_is_not_used_for_order():
    harness, _ = watchdog_harness()

    async def exercise():
        await harness.prepare_live_position()
        quote = harness.quote()
        harness.advance_on_balance_to = harness.now + timedelta(seconds=11)
        await harness.strategy.on_quote(harness.ctx, quote)
        assert harness.orders == []
        await tick(harness)

    asyncio.run(exercise())
    assert len(harness.rest_calls) == 1
    assert len(harness.orders) == 1


def test_watchdog_recovers_central_ownership_after_startup_balance_failure():
    harness, _ = watchdog_harness()
    harness.ctx.state.set("strategy_positions", {})
    balance_calls = []

    async def balance():
        balance_calls.append(harness.now)
        if len(balance_calls) == 1:
            raise RouterError(RouterErrorCode.RATE_LIMITED, "temporary startup failure")
        return await harness.balance()

    harness.ctx.account.balance = balance

    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        assert harness.strategy._live_positions == {}
        assert harness.ctx.state.get("strategy_positions") == {}
        await tick(harness)

    asyncio.run(exercise())
    assert "005930" in harness.strategy._live_positions
    assert len(harness.orders) == 1
    assert harness.orders[0]["quantity"] == Decimal("5")


@pytest.mark.parametrize("balance_fails", [False, True])
def test_empty_position_recovery_is_throttled_even_if_balance_fails(balance_fails):
    harness, _ = watchdog_harness()
    harness.ctx.state.set("strategy_positions", {})
    harness.position.strategy_quantity = Decimal("0")
    balance_calls = []

    async def balance():
        balance_calls.append(harness.now)
        if balance_fails:
            raise RouterError(RouterErrorCode.RATE_LIMITED, "temporary failure")
        return await harness.balance()

    harness.ctx.account.balance = balance

    async def exercise():
        await tick(harness)
        for _ in range(5):
            harness.now += timedelta(seconds=5)
            await tick(harness)
        assert len(balance_calls) == 1
        harness.now += timedelta(seconds=5)
        await tick(harness)

    asyncio.run(exercise())
    assert len(balance_calls) == 2
    assert harness.rest_calls == []
    assert harness.orders == []


@pytest.mark.parametrize("trade_age", [11, 3600, -2, "naive"])
def test_old_trade_replayed_by_websocket_is_not_healthy_but_rest_last_trade_may_be_old(trade_age):
    harness, _ = watchdog_harness()

    async def exercise():
        await harness.prepare_live_position()
        quote = harness.quote()
        quote.market_time = (
            harness.now.replace(tzinfo=None) if trade_age == "naive"
            else harness.now - timedelta(seconds=trade_age)
        )
        await harness.strategy.on_quote(harness.ctx, quote)
        assert harness.orders == []
        assert harness.strategy._last_live_quote_at == {}
        # This REST result deliberately has market_time 60 seconds old, but
        # was freshly obtained from the broker by the watchdog.
        await tick(harness)

    asyncio.run(exercise())
    assert len(harness.rest_calls) == 1
    assert len(harness.orders) == 1


def test_watchdog_health_uses_earlier_trade_time_for_recent_but_delayed_packet():
    harness, _ = watchdog_harness()

    async def exercise():
        await harness.prepare_live_position()
        quote = harness.quote("102")
        quote.market_time = harness.now - timedelta(seconds=8)
        await harness.strategy.on_quote(harness.ctx, quote)
        await tick(harness)
        assert harness.rest_calls == []
        harness.now += timedelta(seconds=3)
        await tick(harness)

    asyncio.run(exercise())
    assert len(harness.rest_calls) == 1
    assert len(harness.orders) == 1


def test_candle_only_strategy_does_not_register_profit_watchdog():
    harness, _ = watchdog_harness(groups=[condition_group("candle", close_condition("150"))])
    asyncio.run(harness.strategy.on_start(harness.ctx))
    assert all(kwargs["schedule_id"] != "live-profit-watchdog"
               for _, _, kwargs in harness.scheduler.calls)
