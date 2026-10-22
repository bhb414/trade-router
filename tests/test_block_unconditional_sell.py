"""Action-only exits use the ordinary guarded sell path, without dummy rules."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trade_router import ScheduleKind
from trade_router_studio.block_packages import block_package_from_source, render_block_strategy
from test_block_rule_trees import _action, _condition, _logic
from test_block_sell_flows import SellHarness, at, sell_flow, settings_for


def exit_tree(operator="ALL", order_type="MARKET"):
    return _logic("exit-root", operator, action=_action("exit-order", "SELL", order_type))


def make_harness(tree=None, *, now="10:30:00", scope="STRATEGY", route="KRX", **changes):
    flow = sell_flow(
        "exit", target_mode="POSITION_SCOPE", operating_start_time="10:00",
        operating_end_time="11:00", sell_condition_groups=[],
        sell_rule_tree=exit_tree() if tree is None else tree,
    )
    flow.update(changes)
    harness = SellHarness(settings_for([flow], sell_position_scope=scope, route=route), now=now)
    harness.ctx.orders.best_limit_sell = harness.sell
    return harness


async def scan(harness, *, scheduled_at=None):
    await harness.strategy.on_timer(harness.ctx, SimpleNamespace(
        schedule_id="condition-sell-exit-live", kind=ScheduleKind.INTERVAL,
        scheduled_at=scheduled_at or harness.now,
    ))


@pytest.mark.parametrize("operator", ["ALL", "ANY"])
def test_action_only_tree_round_trips_and_schedules_without_market_data(operator):
    tree = exit_tree(operator)
    settings = settings_for([sell_flow(sell_condition_groups=[], sell_rule_tree=tree)])
    restored = block_package_from_source("청산", render_block_strategy(settings)).settings
    assert restored.model_dump(mode="json") == settings.model_dump(mode="json")
    assert restored.sell_flows[0].sell_rule_tree.model_dump(mode="json") == tree

    harness = make_harness(tree)
    async def unexpected_market_data(*args, **kwargs):
        pytest.fail("A market liquidation must not wait for candle or indicator data")
    harness.ctx.market.quote = unexpected_market_data
    harness.ctx.market.bars = unexpected_market_data
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        schedule = next(call for call in harness.scheduler.calls
                        if call[2]["schedule_id"] == "condition-sell-exit-live")
        assert schedule == ("every", (), {
            "seconds": 45, "schedule_id": "condition-sell-exit-live",
            "between": ("10:00", "11:00"), "aligned": True,
        })
        await scan(harness)
    asyncio.run(exercise())
    assert len(harness.orders) == 1
    assert harness.orders[0]["quantity"] == Decimal("5")


@pytest.mark.parametrize("operator", ["ALL", "ANY"])
def test_empty_root_without_order_is_saved_but_never_places_order(operator):
    harness = make_harness(_logic("empty-root", operator))
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await scan(harness)
    asyncio.run(exercise())
    assert harness.orders == []


def test_last_condition_removal_retains_explicit_order_as_action_only_exit():
    original = exit_tree("ANY")
    original["children"] = [_condition("old-condition", value="999")]
    edited = deepcopy(original)
    edited["children"].clear()
    harness = make_harness(edited)
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await scan(harness)
    asyncio.run(exercise())
    assert len(harness.orders) == 1
    assert edited["action"] == original["action"]


@pytest.mark.parametrize("guard,expected", [("999", 0), ("100", 1)])
def test_nested_action_only_exit_still_obeys_parent_and_condition(guard, expected):
    tree = _logic("outer", "ALL", _condition("guard", value=guard), exit_tree("ANY"))
    harness = make_harness(tree)
    async def exercise():
        # A candle parent is scheduled on bar close rather than an interval.
        await harness.strategy.on_start(harness.ctx)
        await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
            schedule_id="condition-sell-exit-bars-1", kind=ScheduleKind.BAR_CLOSE,
            scheduled_at=harness.now,
        ))
    asyncio.run(exercise())
    assert len(harness.orders) == expected


@pytest.mark.parametrize("direct_first", [False, True])
def test_nested_or_selects_first_passing_order_with_action_only_branch(direct_first):
    direct = exit_tree("ANY", "MARKET")
    conditional = _condition("passing", value="100", action=_action("limit-exit", "SELL", "LIMIT"))
    children = [direct, conditional] if direct_first else [conditional, direct]
    harness = make_harness(_logic("outer", "ANY", *children))
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
            schedule_id="condition-sell-exit-bars-1", kind=ScheduleKind.BAR_CLOSE,
            scheduled_at=harness.now,
        ))
    asyncio.run(exercise())
    assert len(harness.orders) == 1
    assert ("price" in harness.orders[0]) is not direct_first


def test_nested_empty_group_without_order_still_rejected():
    with pytest.raises(ValidationError):
        make_harness(_logic("outer", "ALL", _logic("empty", "ALL")))


@pytest.mark.parametrize("clock,expected", [("09:59:59", 0), ("10:00:00", 1), ("11:00:00", 0)])
def test_action_only_exit_respects_operating_start_and_end(clock, expected):
    harness = make_harness(now=clock)
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await scan(harness)
    asyncio.run(exercise())
    assert len(harness.orders) == expected


def test_action_only_exit_rechecks_end_after_balance_await():
    harness = make_harness(now="10:59:59")
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        harness.advance_on_balance_to = at("11:00:00")
        await scan(harness)
    asyncio.run(exercise())
    assert harness.orders == []


@pytest.mark.parametrize("scope,expected", [("STRATEGY", "2"), ("ACCOUNT", "4")])
def test_action_only_exit_keeps_selected_holdings_and_available_quantity(scope, expected):
    harness = make_harness(scope=scope)
    harness.position.strategy_quantity = Decimal("2")
    harness.position.available_quantity = Decimal("4")
    harness.ctx.state.set("strategy_positions", {"005930": "2"})
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await scan(harness)
    asyncio.run(exercise())
    assert len(harness.orders) == 1
    assert harness.orders[0]["quantity"] == Decimal(expected)
    assert harness.orders[0].get("sell_position_scope") == ("ACCOUNT" if scope == "ACCOUNT" else None)


@pytest.mark.parametrize("selected,expected", [(True, "2"), (False, "0")])
def test_action_only_exit_limits_to_actual_selected_buy_origin_lots(selected, expected):
    harness = make_harness(target_mode="BUY_FLOWS", buy_flow_ids=["morning"] if selected else [])
    harness.position.buy_origin_lots = (
        {"flow_id": "morning", "node_ids": ["condition-1"], "quantity": "2"},
        {"flow_id": "other", "node_ids": ["condition-2"], "quantity": "3"},
    )
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await scan(harness)
    asyncio.run(exercise())
    assert sum((order["quantity"] for order in harness.orders), Decimal("0")) == Decimal(expected)
    if selected:
        assert harness.orders[0]["sell_buy_flow_ids"] == ["morning"]


@pytest.mark.parametrize("already_open", [False, True])
def test_action_only_exit_does_not_duplicate_pending_orders(already_open):
    harness = make_harness()
    if already_open:
        async def pending():
            return (SimpleNamespace(instrument=harness.position.instrument,
                                    remaining_quantity=Decimal("1"), side=SimpleNamespace(value="SELL")),)
        harness.ctx.account.open_orders = pending
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await scan(harness)
        await scan(harness)
    asyncio.run(exercise())
    assert len(harness.orders) == (0 if already_open else 1)


@pytest.mark.parametrize("order_type,expected", [("MARKET", 0), ("BEST_LIMIT", 1)])
def test_action_only_exit_keeps_nxt_aftermarket_order_type_guard(order_type, expected):
    harness = make_harness(exit_tree(order_type=order_type), now="16:30:00", route="NXT",
                           operating_start_time="16:00", operating_end_time="19:59")
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await scan(harness)
    asyncio.run(exercise())
    assert len(harness.orders) == expected
