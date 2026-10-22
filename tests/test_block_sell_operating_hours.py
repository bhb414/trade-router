"""One Korean-time window gates live, candle and maintenance sell paths."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trade_router import ScheduleKind
from trade_router_studio.block_packages import BlockSellFlow, block_package_from_source, render_block_strategy
from test_block_sell_flows import SellHarness, at, close_condition, condition_group, profit_condition, sell_flow, settings_for
from test_block_realtime_exit_paths import action, group, leaf


@pytest.mark.parametrize("legacy_mode", [None, "LEGACY_TIME"])
def test_old_modes_migrate_without_changing_rules_scope_ids_or_seconds(legacy_mode):
    raw = sell_flow(sell_start_time="16:00:03", sell_end_time="19:59:17")
    if legacy_mode is not None:
        raw["target_mode"] = legacy_mode
    original = deepcopy(raw)
    migrated = BlockSellFlow.model_validate(raw)
    assert raw == original
    assert migrated.target_mode == "POSITION_SCOPE"
    assert (migrated.operating_start_time, migrated.operating_end_time) == ("16:00:03", "19:59:17")
    result = migrated.model_dump(mode="json")
    assert "sell_start_time" not in result and "sell_end_time" not in result
    for key in ("id", "name", "sell_condition_groups", "sell_scan_seconds"):
        assert result[key] == original[key]
    assert BlockSellFlow.model_validate(result).model_dump(mode="json") == result


@pytest.mark.parametrize("target_mode", ["BUY_FLOWS", "POSITION_SCOPE"])
def test_v71_explicit_targets_keep_all_day_behavior_and_selectors(target_mode):
    raw = sell_flow(target_mode=target_mode, sell_start_time="09:00", sell_end_time="09:01",
                    buy_flow_ids=["morning"], buy_node_refs=[{"flow_id": "midday", "node_id": "nested"}])
    migrated = BlockSellFlow.model_validate(raw)
    assert (migrated.operating_start_time, migrated.operating_end_time) == ("08:00", "20:00")
    assert migrated.buy_flow_ids == ["morning"]
    assert migrated.buy_node_refs[0].node_id == "nested"
    del raw["sell_start_time"], raw["sell_end_time"]
    assert BlockSellFlow.model_validate(raw) == migrated


def test_missing_legacy_clocks_keep_old_defaults_but_explicit_new_clocks_win():
    raw = sell_flow()
    del raw["sell_start_time"], raw["sell_end_time"]
    model = BlockSellFlow.model_validate(raw)
    assert (model.operating_start_time, model.operating_end_time) == ("09:00", "15:20")
    raw.update(target_mode="LEGACY_TIME", operating_start_time="08:15", operating_end_time="19:30")
    model = BlockSellFlow.model_validate(raw)
    assert (model.operating_start_time, model.operating_end_time) == ("08:15", "19:30")


@pytest.mark.parametrize("target_mode", ["BUY_FLOWS", "POSITION_SCOPE"])
@pytest.mark.parametrize("start,end", [("10:00", "10:00"), ("20:00", "08:00"), ("25:00", "20:00")])
def test_new_operating_hours_fail_closed_for_all_targets(target_mode, start, end):
    with pytest.raises(ValidationError):
        settings_for([sell_flow(target_mode=target_mode, operating_start_time=start, operating_end_time=end)])


def test_explicit_flow_hours_ignore_unused_top_level_order_but_fallback_validates():
    settings_for([sell_flow()], sell_start_time="20:00", sell_end_time="08:00")
    with pytest.raises(ValidationError):
        settings_for([], sell_start_time="20:00", sell_end_time="08:00")


def test_canonical_source_round_trip_preserves_targets_rules_and_independent_hours():
    settings = settings_for([
        sell_flow(target_mode="BUY_FLOWS", buy_flow_ids=["morning"],
                  operating_start_time="09:00:07", operating_end_time="15:19:53"),
        sell_flow("after", target_mode="POSITION_SCOPE", operating_start_time="16:00", operating_end_time="19:59"),
    ], sell_position_scope="ACCOUNT")
    restored = block_package_from_source("hours", render_block_strategy(settings)).settings
    assert restored.model_dump(mode="json") == settings.model_dump(mode="json")


def make_harness(target_mode="POSITION_SCOPE", **changes):
    tree = group("root", "ALL", leaf("profit", profit_condition()),
                 leaf("bar", close_condition("150")), action=action("sell"))
    flow = sell_flow(target_mode=target_mode, buy_flow_ids=["morning"],
                     operating_start_time="10:00", operating_end_time="11:00",
                     sell_condition_groups=[], sell_rule_tree=tree)
    flow.update(changes)
    harness = SellHarness(settings_for([flow]), now="10:30:00")
    harness.position.buy_origin_lots = ({"flow_id": "morning", "node_ids": ["nested"], "quantity": "5"},)
    async def quote(*args, **kwargs):
        return harness.quote("104")
    harness.ctx.market.quote = quote
    return harness


async def trigger(harness, path, scheduled_at=None):
    scheduled_at = scheduled_at or harness.now
    if path == "quote":
        quote = harness.quote("104")
        quote.received_at = scheduled_at
        await harness.strategy.on_quote(harness.ctx, quote)
    elif path == "bar":
        await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
            schedule_id="condition-sell-early-bars-1", scheduled_at=scheduled_at, kind=ScheduleKind.BAR_CLOSE))
    else:
        await harness.strategy.on_timer(harness.ctx, SimpleNamespace(
            schedule_id="live-profit-watchdog" if path == "watchdog" else "sell-condition-prefetch",
            scheduled_at=scheduled_at, kind=ScheduleKind.INTERVAL))


@pytest.mark.parametrize("mode", ["BUY_FLOWS", "POSITION_SCOPE"])
@pytest.mark.parametrize("path", ["quote", "bar", "watchdog", "prefetch"])
@pytest.mark.parametrize("clock,expected", [("09:59:59", False), ("10:00:00", True), ("11:00:00", False)])
def test_every_exit_path_obeys_same_inclusive_start_exclusive_end(mode, path, clock, expected):
    harness = make_harness(mode)
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await harness.prepare_live_position()
        harness.now = at(clock)
        if path in {"quote", "watchdog"}:
            await harness.strategy._warm_exit_bars(harness.ctx, "005930", harness.namespace["SELL_FLOWS"][0]["sell_rule_tree"])
        harness.bar_calls.clear()
        await trigger(harness, path)
    asyncio.run(exercise())
    assert bool(harness.bar_calls if path == "prefetch" else harness.orders) is expected


@pytest.mark.parametrize("path", ["quote", "bar", "watchdog", "prefetch"])
@pytest.mark.parametrize("scheduled_clock,current_clock", [("09:59:59", "10:00:00"), ("10:59:59", "11:00:00")])
def test_queued_event_must_be_inside_window_at_event_and_processing_times(path, scheduled_clock, current_clock):
    harness = make_harness()
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await harness.prepare_live_position()
        harness.now = at(current_clock)
        await trigger(harness, path, at(scheduled_clock))
    asyncio.run(exercise())
    assert harness.orders == []
    assert harness.bar_calls == []


@pytest.mark.parametrize("path", ["quote", "bar", "watchdog", "prefetch"])
def test_weekend_is_closed_for_realtime_as_well_as_scheduler(path):
    harness = make_harness()
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await harness.prepare_live_position()
        harness.now += timedelta(days=4)  # Tuesday -> Saturday in Seoul.
        await trigger(harness, path)
    asyncio.run(exercise())
    assert harness.orders == []
    assert harness.bar_calls == []


def test_clock_guard_uses_seoul_date_and_rejects_naive_cross_day_or_future_events():
    harness = make_harness(operating_start_time="08:00", operating_end_time="20:00")
    flow = harness.namespace["SELL_FLOWS"][0]
    is_active = harness.strategy._sell_flow_event_is_active
    # 08:00 Seoul is 23:00 on the previous UTC calendar day.
    now = at("08:00:00")
    assert is_active(flow, now.astimezone(timezone.utc), now)
    assert not is_active(flow, now.replace(tzinfo=None), now)
    assert not is_active(flow, now - timedelta(days=1), now)
    assert not is_active(flow, now + timedelta(seconds=1), now)


@pytest.mark.parametrize("path", ["quote", "bar"])
@pytest.mark.parametrize("phase", ["balance", "route", "route-refresh"])
def test_awaited_work_crossing_end_never_submits_or_leaves_pending_reservation(path, phase):
    harness = make_harness(operating_start_time="09:00", operating_end_time="15:30:02")
    harness.now = at("15:29:59")
    calls = []
    async def route(*args, **kwargs):
        calls.append(1)
        harness.now = at("15:30:00") if phase == "route-refresh" and len(calls) == 1 else at("15:30:02")
        return "NXT"
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        await harness.prepare_live_position()
        await harness.strategy._warm_exit_bars(harness.ctx, "005930", harness.namespace["SELL_FLOWS"][0]["sell_rule_tree"])
        if phase == "balance":
            harness.advance_on_balance_to = at("15:30:02")
        else:
            harness.strategy._effective_order_route = route
        await trigger(harness, path)
    asyncio.run(exercise())
    assert harness.orders == []
    assert "005930" not in harness.strategy._live_sell_pending
    assert "005930" not in harness.strategy._sell_submitting
    if phase == "route-refresh":
        assert len(calls) == 2


def test_schedules_share_exact_window_and_completed_minute_bar_cadence():
    harness = make_harness(operating_start_time="10:00:03", operating_end_time="11:00:07")
    asyncio.run(harness.strategy.on_start(harness.ctx))
    calls = [(method, args, options) for method, args, options in harness.scheduler.calls
             if options["schedule_id"].startswith("condition-sell") or options["schedule_id"] in
             {"live-profit-watchdog", "sell-condition-prefetch"}]
    assert len(calls) == 3
    assert all(options["between"] == ("10:00:03", "11:00:07") for _, _, options in calls)
    assert any(method == "on_bar_close" and args == ("3m",) for method, args, _ in calls)


def test_migrated_daily_conditions_repeat_but_cache_and_pending_order_prevent_duplicates():
    harness = SellHarness(settings_for([sell_flow(sell_condition_groups=[
        condition_group("daily", close_condition("100", interval="1d"))])]))
    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        tree = harness.namespace["SELL_FLOWS"][0]["sell_rule_tree"]
        await harness.strategy._warm_exit_bars(harness.ctx, "005930", tree)
        await harness.strategy._warm_exit_bars(harness.ctx, "005930", tree)
        assert len(harness.bar_calls) == 1
        checks = [options for method, _, options in harness.scheduler.calls
                  if method == "every" and options["schedule_id"].startswith("condition-sell")]
        assert checks[0]["between"] == ("09:00", "10:00")
        for _ in range(2):
            await harness.strategy.on_timer(harness.ctx, SimpleNamespace(
                schedule_id=checks[0]["schedule_id"], scheduled_at=harness.now, kind=ScheduleKind.INTERVAL))
    asyncio.run(exercise())
    assert len(harness.orders) == 1
    assert len(harness.bar_calls) == 1


@pytest.mark.parametrize("venue,close", [("KRX", "15:30:00"), ("NXT", "20:00:00"), ("INTEGRATED", "20:00:00")])
def test_completed_daily_cache_refetches_on_venue_close_and_updates_condition(venue, close):
    flow = sell_flow(target_mode="POSITION_SCOPE", operating_start_time="08:00", operating_end_time="23:59",
                     sell_condition_groups=[condition_group("daily", close_condition("150", interval="1d"))])
    harness = SellHarness(settings_for([flow], market_venue=venue), now="11:00:00")
    close_at = at(close)
    calls = []
    async def daily_bars(code, interval, **kwargs):
        calls.append((venue, harness.now))
        price = 200 if harness.now >= close_at else 100
        return [SimpleNamespace(open=price, high=price, low=price, close=price, volume=1000)]
    harness.ctx.market.bars = daily_bars
    tree = harness.namespace["SELL_FLOWS"][0]["sell_rule_tree"]
    async def evaluate():
        return (await harness.strategy._rule_tree_result(harness.ctx, "005930", tree, side="test"))[0]
    async def exercise():
        await harness.strategy._warm_exit_bars(harness.ctx, "005930", tree)
        assert not await evaluate()
        harness.now = close_at - timedelta(seconds=1)
        await harness.strategy._warm_exit_bars(harness.ctx, "005930", tree)
        assert len(calls) == 1
        assert not await evaluate()
        harness.now = close_at
        # A live mixed rule must reject the obsolete morning guard cache.
        cached_result = await harness.strategy._rule_tree_result(
            harness.ctx, "005930", tree, side="test", cache_only=True)
        assert not cached_result[0]
        await harness.strategy._warm_exit_bars(harness.ctx, "005930", tree)
        assert len(calls) == 2
        assert await evaluate()
        await harness.strategy._warm_exit_bars(harness.ctx, "005930", tree)
        assert len(calls) == 2
    asyncio.run(exercise())
