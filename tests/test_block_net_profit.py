"""Fee-aware exit decisions on generated strategies; no broker connection."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trade_router import ScheduleKind

from test_block_sell_flows import (
    SellHarness, close_condition, condition_group, profit_condition, sell_flow, settings_for,
)


def harness_for(*, condition=None, groups=None):
    harness = SellHarness(settings_for([sell_flow(
        sell_start_time="09:00", sell_end_time="15:20",
        sell_condition_groups=groups or [condition_group("profit", condition or profit_condition())],
    )]), now="10:00:00")
    harness.rates = {"buy_commission_rate": Decimal("0.001"),
                     "sell_commission_rate": Decimal("0.001"), "sell_tax_rate": Decimal("0.002")}
    async def balance():
        return SimpleNamespace(positions=(harness.position,), **harness.rates)
    harness.balance = balance
    harness.ctx.account.balance = balance
    return harness


def net_value(harness, price="103", average="100"):
    return harness.strategy._indicator_value(
        "profit_rate", (), 0, Decimal(average), {}, current_price=Decimal(price),
    )


def test_net_return_includes_both_commissions_and_sale_tax_in_cost_basis():
    harness = harness_for()
    harness.rates.update(buy_commission_rate=Decimal("0.01"),
                         sell_commission_rate=Decimal("0.02"), sell_tax_rate=Decimal("0.03"))
    asyncio.run(harness.prepare_live_position())
    # Purchase 100 + 1 fee; sale 110 - 2.2 fee - 3.3 tax = 104.5.
    assert net_value(harness, "110") == (Decimal("104.5") / Decimal("101") - 1) * 100


def test_gross_take_profit_is_insufficient_but_net_take_profit_submits_once():
    harness = harness_for()
    async def run():
        await harness.prepare_live_position()
        await harness.strategy.on_quote(harness.ctx, harness.quote("103"))
        assert harness.orders == []
        await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
        await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
    asyncio.run(run())
    assert len(harness.orders) == 1


def test_stop_loss_uses_net_loss_before_gross_price_reaches_threshold():
    harness = harness_for(condition=profit_condition("-2.1", operator="LTE"))
    async def run():
        await harness.prepare_live_position()
        # Gross -1.9%, net approximately -2.292%.
        await harness.strategy.on_quote(harness.ctx, harness.quote("98.1"))
    asyncio.run(run())
    assert len(harness.orders) == 1


def test_explicit_zero_rates_keep_exact_price_return_and_boundary():
    harness = harness_for()
    harness.rates = dict.fromkeys(harness.rates, Decimal("0"))
    asyncio.run(harness.prepare_live_position())
    assert net_value(harness) == Decimal("3")
    asyncio.run(harness.strategy.on_quote(harness.ctx, harness.quote("103")))
    assert len(harness.orders) == 1


@pytest.mark.parametrize("invalid", [None, "", "bad", "NaN", "Infinity", "-0.01", "1"])
@pytest.mark.parametrize("key", ["buy_commission_rate", "sell_commission_rate", "sell_tax_rate"])
def test_missing_or_invalid_rates_clear_previous_value_without_gross_fallback(key, invalid):
    harness = harness_for()
    asyncio.run(harness.prepare_live_position())
    assert net_value(harness) is not None
    harness.rates[key] = invalid
    asyncio.run(harness.prepare_live_position())
    asyncio.run(harness.prepare_live_position())
    assert net_value(harness) is None
    asyncio.run(harness.strategy.on_quote(harness.ctx, harness.quote("110")))
    assert harness.orders == []
    assert sum("순수익률 조건 보류" in line for line in harness.logs) == 1


def test_invalid_combined_sale_cost_and_old_balance_without_rates_fail_closed():
    harness = harness_for()
    harness.rates.update(sell_commission_rate=Decimal("0.6"), sell_tax_rate=Decimal("0.5"))
    asyncio.run(harness.prepare_live_position())
    assert net_value(harness) is None
    harness.rates = {}
    asyncio.run(harness.prepare_live_position())
    assert net_value(harness) is None


def test_missing_cost_does_not_block_other_or_candle_condition():
    harness = harness_for(groups=[condition_group("either", profit_condition(), close_condition("150"))])
    harness.rates = {}
    async def run():
        await harness.strategy.on_start(harness.ctx)
        schedule_id = next(kwargs["schedule_id"] for _, _, kwargs in harness.scheduler.calls
                           if kwargs["schedule_id"].startswith("condition-sell-"))
        await harness.strategy.on_timer(harness.ctx, SimpleNamespace(
            schedule_id=schedule_id, scheduled_at=harness.now, kind=ScheduleKind.BAR_CLOSE,
        ))
    asyncio.run(run())
    assert len(harness.orders) == 1
    assert harness.bar_calls


def test_cost_change_is_rechecked_after_cached_live_candidate_before_order():
    harness = harness_for()
    async def run():
        harness.rates = dict.fromkeys(harness.rates, Decimal("0"))
        await harness.prepare_live_position()
        harness.rates["sell_tax_rate"] = Decimal("0.05")
        await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
    asyncio.run(run())
    assert harness.orders == []
    assert net_value(harness, "104") == Decimal("-1.2")


def test_healthy_websocket_still_recovers_new_cost_settings_without_restart():
    harness = harness_for()
    async def run():
        harness.rates = {}
        await harness.prepare_live_position()
        await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
        assert harness.orders == []
        harness.rates = dict.fromkeys(("buy_commission_rate", "sell_commission_rate", "sell_tax_rate"), Decimal("0"))
        harness.now += timedelta(seconds=31)
        harness.strategy._last_live_quote_at["005930"] = harness.now
        await harness.strategy._check_live_profit_fallback(harness.ctx)
        assert any("판단을 재개" in line for line in harness.logs)
        await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
    asyncio.run(run())
    assert len(harness.orders) == 1
