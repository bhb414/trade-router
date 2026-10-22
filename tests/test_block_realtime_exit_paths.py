"""A live stop must survive OR ordering without bypassing AND/action semantics."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trade_router import RouterError, RouterErrorCode
from test_block_sell_flows import (
    SellHarness, close_condition, profit_condition, sell_flow, settings_for,
)


def leaf(identifier, condition, action=None):
    return {"id": identifier, "kind": "condition",
            "condition": condition.model_dump(mode="json"), "action": action}


def action(identifier, order_type="MARKET"):
    return {"id": identifier, "kind": "order", "side": "SELL", "order_type": order_type}


def group(identifier, operator, *children, action=None):
    return {"id": identifier, "kind": "logic", "operator": operator,
            "children": list(children), "action": action}


def make_harness(tree):
    return SellHarness(settings_for([sell_flow(
        sell_start_time="09:00", sell_end_time="15:20",
        sell_condition_groups=[], sell_rule_tree=tree,
    )]), now="10:00:00")


def stop(identifier="stop", **kwargs):
    return leaf(identifier, profit_condition("-3", operator="LTE"), **kwargs)


def run_quote(harness, price="94"):
    async def exercise():
        await harness.prepare_live_position()
        await harness.strategy.on_quote(harness.ctx, harness.quote(price))
        await harness.strategy.on_quote(harness.ctx, harness.quote(price))
    asyncio.run(exercise())


@pytest.mark.parametrize("stop_first", [False, True])
def test_true_candle_or_stop_requests_once_regardless_of_condition_order(stop_first):
    children = [leaf("candle", close_condition("150")), stop()]
    if stop_first:
        children.reverse()
    harness = make_harness(group("root", "ANY", *children, action=action("sell")))
    run_quote(harness)
    assert len(harness.orders) == 1
    assert harness.orders[0]["quantity"] == Decimal("5")


def test_live_proof_does_not_change_first_true_branch_order_style():
    harness = make_harness(group(
        "root", "ANY",
        leaf("candle", close_condition("150"), action("first", "LIMIT")),
        stop(action=action("second", "MARKET")),
    ))
    run_quote(harness)
    assert len(harness.orders) == 1
    assert harness.orders[0]["price"] == Decimal("94")


def test_true_candle_branch_is_not_triggered_when_stop_path_has_failing_and_guard():
    harness = make_harness(group(
        "root", "ANY", leaf("candle", close_condition("150")),
        group("guarded-stop", "ALL", stop(), leaf("guard", close_condition("999"))),
        action=action("sell"),
    ))
    run_quote(harness)
    assert harness.orders == []


def test_nested_true_stop_path_still_requires_outer_and_guard():
    harness = make_harness(group(
        "root", "ALL", leaf("outer", close_condition("999")),
        group("choice", "ANY", leaf("candle", close_condition("150")), stop()),
        action=action("sell"),
    ))
    run_quote(harness)
    assert harness.orders == []


def test_nested_true_stop_path_survives_candle_first_under_all():
    harness = make_harness(group(
        "root", "ALL", leaf("outer", close_condition("150")),
        group("choice", "ANY", leaf("candle", close_condition("150")), stop()),
        action=action("sell"),
    ))
    run_quote(harness)
    assert len(harness.orders) == 1


def test_true_branch_without_action_does_not_borrow_later_stop_action():
    harness = make_harness(group(
        "root", "ANY", leaf("first-no-action", close_condition("150")),
        stop(action=action("later-action")),
    ))
    run_quote(harness)
    assert harness.orders == []


def test_candle_only_truth_is_not_evaluated_on_quote_when_stop_is_false():
    harness = make_harness(group(
        "root", "ANY", leaf("candle", close_condition("150")), stop(),
        action=action("sell"),
    ))
    run_quote(harness, "99")
    assert harness.orders == []
    assert harness.bar_calls == []


@pytest.mark.parametrize("price, expected", [("97.01", 0), ("97", 1), ("94", 1)])
def test_pure_live_stop_uses_inclusive_boundary_without_any_bar_dependency(price, expected):
    harness = make_harness(group("root", "ALL", stop(), action=action("sell")))

    async def unavailable_bars(*args, **kwargs):
        pytest.fail("A live profit comparison must not request candles")

    harness.ctx.market.bars = unavailable_bars
    run_quote(harness, price)
    assert len(harness.orders) == expected


def test_failed_candle_lookup_does_not_block_independent_live_stop():
    harness = make_harness(group(
        "root", "ANY", leaf("candle", close_condition("150")), stop(),
        action=action("sell"),
    ))

    async def unavailable_bars(*args, **kwargs):
        raise RouterError(RouterErrorCode.RATE_LIMITED, "offline candle failure")

    harness.ctx.market.bars = unavailable_bars
    run_quote(harness)
    assert len(harness.orders) == 1
    assert any("offline candle failure" in line for line in harness.logs)


def test_silent_stream_watchdog_uses_same_candle_first_stop_proof():
    harness = make_harness(group(
        "root", "ANY", leaf("candle", close_condition("150")), stop(),
        action=action("sell"),
    ))

    async def quote(code, **kwargs):
        return harness.quote("94")

    harness.ctx.market.quote = quote

    async def exercise():
        await harness.prepare_live_position()
        await harness.strategy.on_timer(harness.ctx, SimpleNamespace(
            schedule_id="live-profit-watchdog", scheduled_at=harness.now,
        ))

    asyncio.run(exercise())
    assert len(harness.orders) == 1
