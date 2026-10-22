"""Filled buy lineage selectors must remain precise through generated source."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trade_router import OrderStatus, ScheduleKind, RouterError, RouterErrorCode
from trade_router_studio.block_packages import block_package_from_source, render_block_strategy
from test_block_sell_flows import SellHarness, sell_flow, settings_for
from test_block_realtime_exit_paths import action, group, leaf
from test_block_sell_flows import close_condition, condition_group, profit_condition


def harness_for(**changes):
    flow = sell_flow(target_mode="BUY_FLOWS", buy_flow_ids=["morning"], **changes)
    harness = SellHarness(settings_for([flow]), now="11:00:00")
    harness.position.buy_origin_lots = (
        {"flow_id": "morning", "node_ids": ("root", "choice", "first"), "quantity": "2"},
        {"flow_id": "midday", "node_ids": ("root", "choice", "second"), "quantity": "1"},
    )
    return harness


def test_mixed_selection_round_trip_and_deleted_refs_preserved():
    settings = settings_for([sell_flow(
        target_mode="BUY_FLOWS", buy_flow_ids=["morning", "removed"],
        buy_node_refs=[{"flow_id": "midday", "node_id": "nested-if"}],
    )])
    restored = block_package_from_source("linked", render_block_strategy(settings)).settings
    assert restored.model_dump(mode="json") == settings.model_dump(mode="json")


def test_whole_flow_and_nested_refs_are_union_once_per_actual_lot():
    harness = harness_for(buy_node_refs=[
        {"flow_id": "morning", "node_id": "first"},
        {"flow_id": "midday", "node_id": "choice"},
        {"flow_id": "midday", "node_id": "second"},
    ])
    async def exercise():
        await harness.prepare_live_position()
        await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
    asyncio.run(exercise())
    assert len(harness.orders) == 1
    order = harness.orders[0]
    assert order["quantity"] == Decimal("3")
    assert order["sell_buy_flow_ids"] == ["morning"]
    assert len(order["sell_buy_node_refs"]) == 3
    assert "sell_position_scope" not in order


def test_linked_flow_ignores_old_clock_but_empty_or_untagged_holdings_do_not_sell():
    for empty_selection in (False, True):
        harness = harness_for()
        if empty_selection:
            harness.namespace["SELL_FLOWS"][0]["buy_flow_ids"] = []
        else:
            harness.position.buy_origin_lots = ()
        async def exercise():
            await harness.prepare_live_position()
            await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
        asyncio.run(exercise())
        assert harness.orders == []


def test_origin_uses_actual_or_path_not_identical_sibling_condition():
    harness = harness_for()
    condition = close_condition("150").model_dump(mode="json")
    first = {"id": "first", "kind": "condition", "condition": dict(condition)}
    second = {"id": "second", "kind": "condition", "condition": dict(condition)}
    tree = group("root", "ALL", group("choice", "ANY", first, second),
                 leaf("required", close_condition("100")))
    flow = {"id": "morning", "buy_rule_tree": tree}
    selected_action = {"id": "purchase"}
    options = harness.strategy._buy_origin_options(
        flow, [first["condition"], tree["children"][1]["condition"]], selected_action)
    assert options == {"buy_flow_id": "morning", "buy_rule_node_ids": [
        "root", "choice", "first", "required", "purchase"]}


def test_scoped_fill_keeps_other_origins_subscribed():
    harness = harness_for()
    async def exercise():
        await harness.prepare_live_position()
        harness.position.quantity = Decimal("3")
        harness.position.available_quantity = Decimal("3")
        harness.position.strategy_quantity = Decimal("3")
        await harness.strategy.on_order(harness.ctx, SimpleNamespace(
            request=SimpleNamespace(instrument=harness.position.instrument,
                                    side=SimpleNamespace(value="SELL")),
            status=OrderStatus.FILLED))
        assert "005930" in harness.strategy._live_positions
        assert harness.ctx.state.get("strategy_positions")["005930"] == "3"
        await harness.strategy.on_fill(harness.ctx, SimpleNamespace(order_id="fill-without-symbol"))
        assert "005930" in harness.strategy._live_positions
    asyncio.run(exercise())


def test_linked_schedule_uses_market_window_and_legacy_keeps_saved_window():
    harness = harness_for()
    asyncio.run(harness.strategy.on_start(harness.ctx))
    schedules = [kwargs for _, _, kwargs in harness.scheduler.calls
                 if kwargs["schedule_id"].startswith("condition-sell")]
    assert schedules[0]["between"] == ("08:00", "20:00")
    migrated = settings_for([sell_flow()]).sell_flows[0].model_dump(mode="json")
    assert harness.strategy._sell_schedule_window(migrated) == ("09:00", "10:00")


def test_buy_source_discovery_cannot_hold_startup_exit_registration():
    harness = harness_for()
    async def blocked(*args):
        pytest.fail("HTS discovery belongs to the buy lane, not strategy startup")
    harness.ctx.discovery.conditions = blocked
    asyncio.run(harness.strategy.on_start(harness.ctx))
    assert any(kwargs["schedule_id"] == "live-profit-watchdog"
               for _, _, kwargs in harness.scheduler.calls)


def test_confirmed_scoped_fill_releases_stop_for_remaining_origin_immediately():
    harness = harness_for()
    async def exercise():
        await harness.prepare_live_position()
        harness.strategy._mark_live_sell_pending("005930")
        await harness.strategy.on_order(harness.ctx, SimpleNamespace(
            request=SimpleNamespace(instrument=harness.position.instrument,
                                    side=SimpleNamespace(value="SELL")),
            status=OrderStatus.FILLED))
        assert "005930" not in harness.strategy._live_sell_pending
        await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
    asyncio.run(exercise())
    assert len(harness.orders) == 1


def test_immediate_fill_response_keeps_other_origin_stop_live_without_grace_delay():
    flows = [
        sell_flow("profit", target_mode="BUY_FLOWS", buy_flow_ids=["morning"]),
        sell_flow("stop", target_mode="BUY_FLOWS", buy_flow_ids=["midday"],
                  sell_condition_groups=[condition_group("stop-rule", profit_condition("-3", operator="LTE"))]),
    ]
    harness = SellHarness(settings_for(flows), now="11:00:00")
    harness.position.buy_origin_lots = (
        {"flow_id": "morning", "node_ids": ("first",), "quantity": "2"},
        {"flow_id": "midday", "node_ids": ("second",), "quantity": "1"},
    )
    async def sell(**kwargs):
        harness.orders.append(kwargs)
        if len(harness.orders) == 1:
            harness.position.quantity = Decimal("1")
            harness.position.available_quantity = Decimal("1")
            harness.position.strategy_quantity = Decimal("1")
            harness.position.buy_origin_lots = (harness.position.buy_origin_lots[1],)
            return SimpleNamespace(status=OrderStatus.FILLED, message="")
        return SimpleNamespace(status=OrderStatus.ACKNOWLEDGED, message="")
    harness.ctx.orders.market_sell = sell
    async def exercise():
        await harness.prepare_live_position()
        await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
        await harness.strategy.on_quote(harness.ctx, harness.quote("94"))
    asyncio.run(exercise())
    assert [(order["sell_buy_flow_ids"], order["quantity"]) for order in harness.orders] == [
        (["morning"], Decimal("2")), (["midday"], Decimal("1"))]


def test_terminal_fill_does_not_clear_a_different_broker_pending_sell():
    harness = harness_for()
    async def pending():
        return (SimpleNamespace(instrument=harness.position.instrument,
                                remaining_quantity=Decimal("1"), side=SimpleNamespace(value="SELL")),)
    harness.ctx.account.open_orders = pending
    async def exercise():
        await harness.prepare_live_position()
        await harness.strategy.on_order(harness.ctx, SimpleNamespace(
            request=SimpleNamespace(instrument=harness.position.instrument,
                                    side=SimpleNamespace(value="SELL")),
            status=OrderStatus.FILLED))
        await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
    asyncio.run(exercise())
    assert harness.orders == []
    assert "005930" in harness.strategy._live_sell_pending


@pytest.mark.parametrize("interval", ["3m", "1d"])
def test_sell_prefetch_warms_candle_guard_even_when_leading_stop_is_false(interval):
    tree = group("root", "ALL",
                 leaf("stop", profit_condition("-3", operator="LTE")),
                 leaf("guard", close_condition("150", interval=interval)),
                 action=action("sell"))
    harness = harness_for(sell_rule_tree=tree, sell_condition_groups=[])
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        # Normal market quote is 104 (+4%), so the first AND child is false.
        # A dedicated sell-lane prefetch must still obtain the completed guard.
        await harness.strategy.on_timer(harness.ctx, SimpleNamespace(
            schedule_id="sell-condition-prefetch", scheduled_at=harness.now,
            kind=ScheduleKind.INTERVAL))
        assert harness.bar_calls
        async def forbidden(*args, **kwargs):
            pytest.fail("a fresh stop-loss tick must not await candle I/O")
        harness.ctx.market.bars = forbidden
        await harness.strategy.on_quote(harness.ctx, harness.quote("94"))
    asyncio.run(exercise())
    assert len(harness.orders) == 1
    assert harness.orders[0]["quantity"] == Decimal("2")


def test_starting_after_daily_open_registers_prefetch_and_repeating_daily_exit():
    tree = group("root", "ALL", leaf("daily", close_condition("150", interval="1d")),
                 action=action("sell"))
    harness = harness_for(sell_rule_tree=tree, sell_condition_groups=[])
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        # Startup at 11:00 must not wait until tomorrow's 08:00 daily event.
        prefetch = next(kwargs for method, _, kwargs in harness.scheduler.calls
                        if method == "every" and kwargs["schedule_id"] == "sell-condition-prefetch")
        assert prefetch["seconds"] == 5
        checks = [kwargs for method, _, kwargs in harness.scheduler.calls
                  if method == "every" and kwargs["schedule_id"].startswith("condition-sell")]
        assert checks
        await harness.strategy.on_timer(harness.ctx, SimpleNamespace(
            schedule_id=prefetch["schedule_id"], scheduled_at=harness.now,
            kind=ScheduleKind.INTERVAL))
        await harness.strategy.on_timer(harness.ctx, SimpleNamespace(
            schedule_id=checks[0]["schedule_id"], scheduled_at=harness.now,
            kind=ScheduleKind.INTERVAL))
    asyncio.run(exercise())
    assert len(harness.orders) == 1


@pytest.mark.parametrize("path", ["quote", "bar"])
@pytest.mark.parametrize("error_code", [
    RouterErrorCode.ORDER_REJECTED, RouterErrorCode.ORDER_PERMISSION_REQUIRED,
    RouterErrorCode.RISK_LIMIT_EXCEEDED, RouterErrorCode.INVALID_REQUEST,
    RouterErrorCode.UNSUPPORTED_CAPABILITY, RouterErrorCode.IDEMPOTENCY_CONFLICT,
    RouterErrorCode.ORDER_STATUS_UNKNOWN,
])
def test_confirmed_preflight_rejection_releases_only_its_own_sell_reservation(path, error_code):
    harness = harness_for()
    attempts = []
    async def rejected(**kwargs):
        attempts.append(kwargs)
        raise RouterError(error_code, "test preflight failure")
    harness.ctx.orders.market_sell = rejected
    async def exercise():
        await harness.prepare_live_position()
        for _ in range(2):
            if path == "quote":
                await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
            else:
                await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
                    schedule_id="test-sell", scheduled_at=harness.now, kind=ScheduleKind.INTERVAL))
    asyncio.run(exercise())
    ambiguous = error_code is RouterErrorCode.ORDER_STATUS_UNKNOWN
    assert ("005930" in harness.strategy._live_sell_pending) is ambiguous
    assert len(attempts) == (1 if ambiguous else 2)


@pytest.mark.parametrize("path", ["quote", "bar"])
def test_failure_before_reservation_does_not_clear_another_callback_pending_sell(path):
    harness = harness_for()
    async def route_lookup(*args, **kwargs):
        harness.strategy._mark_live_sell_pending("005930")
        raise RouterError(RouterErrorCode.INVALID_REQUEST, "route validation failed")
    harness.strategy._effective_order_route = route_lookup
    async def exercise():
        await harness.prepare_live_position()
        if path == "quote":
            await harness.strategy.on_quote(harness.ctx, harness.quote("104"))
        else:
            await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
                schedule_id="test-sell", scheduled_at=harness.now, kind=ScheduleKind.INTERVAL))
    asyncio.run(exercise())
    assert "005930" in harness.strategy._live_sell_pending
    assert harness.orders == []
