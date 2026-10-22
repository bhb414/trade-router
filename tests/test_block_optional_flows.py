"""Offline contracts for a strategy with zero buy and/or sell flows.

Run with unittest; no broker, web server, pytest plugin, or account is used.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from trade_router_studio.block_packages import (
    DEFAULT_CONDITION_BOLLINGER_SETTINGS,
    ConditionBollingerBlockSettings,
    block_package_from_source,
    render_block_strategy,
)


SEOUL = ZoneInfo("Asia/Seoul")


def settings_for(**changes):
    values = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    values.update(
        strategy_id="optional-flows-test", trading_account="test-account",
        condition_source="test-api", hts_user_id="test-user", condition_id="1",
    )
    values.update(changes)
    return ConditionBollingerBlockSettings.model_validate(values)


def buy_flow():
    values = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    result = {key: values[key] for key in (
        "target_source", "hts_user_id", "condition_id", "expected_condition_name",
        "ranking_kind", "ranking_venue", "ranking_market", "ranking_limit",
        "symbols", "buy_scan_seconds", "buy_start_time", "buy_end_time",
        "buy_condition_groups",
    )}
    result.update(id="morning", name="매수 흐름", hts_user_id="test-user")
    return result


def sell_flow():
    return {
        "id": "exit", "name": "매도 흐름", "sell_scan_seconds": 20,
        "sell_start_time": "09:00", "sell_end_time": "15:20",
        "sell_condition_groups": [{
            "id": "profit", "conditions": [{
                "left": "profit_rate", "operator": "GTE", "right": "value", "value": "3",
            }],
        }],
    }


def execution_flow():
    return {
        "id": "unfilled", "name": "미체결 관리",
        "execution_start_time": "09:00", "execution_end_time": "15:20",
        "execution_scan_seconds": 5,
        "execution_rule_tree": {
            "kind": "logic", "id": "pending-root", "operator": "ALL",
            "children": [{
                "kind": "condition", "id": "timeout", "condition": {
                    "metric": "elapsed_seconds", "operator": ">=", "value": 30,
                },
            }],
            "action": {"kind": "execution", "id": "cancel", "action_type": "CANCEL_REMAINDER"},
        },
    }


class MemoryState:
    def __init__(self):
        self.values = {"ownership_initialized": True, "strategy_positions": {"005930": "5"}}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class RecordingScheduler:
    def __init__(self):
        self.calls = []

    def every(self, *args, **kwargs):
        self.calls.append(("every", args, kwargs))

    def daily_at(self, *args, **kwargs):
        self.calls.append(("daily_at", args, kwargs))

    def on_bar_close(self, *args, **kwargs):
        self.calls.append(("on_bar_close", args, kwargs))


class Harness:
    def __init__(self, settings):
        self.now = datetime(2026, 10, 21, 10, 5, tzinfo=SEOUL)
        self.namespace = {}
        exec(compile(render_block_strategy(settings), "<optional-flows-test>", "exec"), self.namespace)
        harness = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return harness.now.astimezone(tz) if tz else harness.now

        self.namespace["datetime"] = Clock
        self.strategy = self.namespace["ManagedBlockStrategy"]()
        self.scheduler = RecordingScheduler()
        self.logs = []
        self.position = SimpleNamespace(
            instrument=SimpleNamespace(code="005930"), quantity=Decimal("5"),
            available_quantity=Decimal("5"), average_price=Decimal("100"),
            strategy_quantity=Decimal("5"),
        )
        self.ctx = SimpleNamespace(
            state=MemoryState(), scheduler=self.scheduler,
            discovery=SimpleNamespace(
                conditions=AsyncMock(return_value=[SimpleNamespace(id="1", name="테스트 조건식")]),
                condition_results=AsyncMock(return_value=[]), ranking=AsyncMock(return_value=[]),
            ),
            account=SimpleNamespace(
                balance=AsyncMock(return_value=SimpleNamespace(
                    positions=(self.position,), buy_commission_rate=Decimal("0"),
                    sell_commission_rate=Decimal("0"), sell_tax_rate=Decimal("0"),
                )),
                open_orders=AsyncMock(return_value=()),
            ),
            market=SimpleNamespace(
                quote=AsyncMock(), bars=AsyncMock(return_value=[]),
                subscribe_quotes=AsyncMock(return_value=({"id": "quote-005930"},)),
                unsubscribe=AsyncMock(),
            ),
            orders=SimpleNamespace(**{name: AsyncMock() for name in (
                "market_buy", "limit_buy", "best_limit_buy",
                "market_sell", "limit_sell", "best_limit_sell",
            )}),
            log=SimpleNamespace(info=self.logs.append, warning=self.logs.append, error=self.logs.append),
        )

    def start(self):
        asyncio.run(self.strategy.on_start(self.ctx))

    def schedule_ids(self):
        return [kwargs["schedule_id"] for _, _, kwargs in self.scheduler.calls]


class OptionalFlowTests(unittest.TestCase):
    def assert_no_orders(self, harness):
        for mock in vars(harness.ctx.orders).values():
            mock.assert_not_awaited()

    def test_existing_settings_missing_flags_keep_both_legacy_flows(self):
        values = settings_for().model_dump(mode="json")
        values.pop("buy_enabled", None)
        values.pop("sell_enabled", None)
        settings = ConditionBollingerBlockSettings.model_validate(values)
        self.assertTrue(settings.buy_enabled)
        self.assertTrue(settings.sell_enabled)
        harness = Harness(settings)
        harness.start()
        self.assertEqual(len(harness.namespace["BUY_FLOWS"]), 1)
        self.assertEqual(len(harness.namespace["SELL_FLOWS"]), 1)
        self.assertIn("condition-scan", harness.schedule_ids())
        self.assertTrue(any(key.startswith("condition-sell") for key in harness.schedule_ids()))

    def test_old_python_without_flags_round_trips_as_enabled(self):
        source = render_block_strategy(settings_for())
        source = "\n".join(line for line in source.splitlines()
                           if not line.startswith(("BUY_ENABLED =", "SELL_ENABLED =")))
        restored = block_package_from_source("구버전", source).settings
        self.assertTrue(restored.buy_enabled)
        self.assertTrue(restored.sell_enabled)
        harness = Harness(restored)
        self.assertEqual(len(harness.namespace["BUY_FLOWS"]), 1)
        self.assertEqual(len(harness.namespace["SELL_FLOWS"]), 1)

    def test_all_flag_combinations_survive_source_round_trip(self):
        for buy_enabled in (False, True):
            for sell_enabled in (False, True):
                with self.subTest(buy_enabled=buy_enabled, sell_enabled=sell_enabled):
                    original = settings_for(buy_enabled=buy_enabled, sell_enabled=sell_enabled)
                    restored = block_package_from_source("선택 흐름", render_block_strategy(original)).settings
                    self.assertEqual(restored.model_dump(mode="json"), original.model_dump(mode="json"))

    def test_malformed_explicit_source_flags_fail_closed_instead_of_enabling(self):
        source = render_block_strategy(settings_for(buy_enabled=False, sell_enabled=False))
        for constant in ("BUY_ENABLED", "SELL_ENABLED"):
            for literal in ("None", "0", "1", "'false'", "'True'", "[]", "{}", "bool(0)"):
                with self.subTest(constant=constant, literal=literal):
                    malformed = source.replace(f"{constant} = False", f"{constant} = {literal}")
                    with self.assertRaises(ValueError):
                        block_package_from_source("잘못된 흐름 설정", malformed)

    def test_non_boolean_settings_flags_are_rejected(self):
        for field in ("buy_enabled", "sell_enabled"):
            for value in (None, 0, 1, "false", "True", [], {}):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        settings_for(**{field: value})

    def test_disabled_empty_lists_do_not_restore_legacy_fallback(self):
        harness = Harness(settings_for(buy_enabled=False, sell_enabled=False, buy_flows=[], sell_flows=[]))
        harness.start()
        self.assertEqual(harness.namespace["BUY_FLOWS"], ())
        self.assertEqual(harness.namespace["SELL_FLOWS"], ())
        self.assertEqual(harness.scheduler.calls, [])
        harness.ctx.discovery.conditions.assert_not_awaited()
        harness.ctx.market.subscribe_quotes.assert_not_awaited()
        harness.ctx.account.balance.assert_not_awaited()

    def test_disabled_stored_flow_lists_never_register_or_discover(self):
        harness = Harness(settings_for(
            buy_enabled=False, sell_enabled=False, buy_flows=[buy_flow()], sell_flows=[sell_flow()],
        ))
        harness.start()
        self.assertEqual(harness.namespace["BUY_FLOWS"], ())
        self.assertEqual(harness.namespace["SELL_FLOWS"], ())
        self.assertEqual(harness.scheduler.calls, [])
        harness.ctx.discovery.conditions.assert_not_awaited()
        harness.ctx.market.subscribe_quotes.assert_not_awaited()

    def test_buy_only_preserves_scanning_without_sell_watchers(self):
        harness = Harness(settings_for(buy_enabled=True, sell_enabled=False))
        harness.start()
        self.assertEqual(harness.schedule_ids(), ["condition-scan"])
        # Source validation runs with the first buy scan; startup must not
        # block independent sell event registration on discovery I/O.
        harness.ctx.discovery.conditions.assert_not_awaited()
        harness.ctx.market.subscribe_quotes.assert_not_awaited()

    def test_sell_only_keeps_exit_schedules_without_hts_discovery(self):
        harness = Harness(settings_for(buy_enabled=False, sell_enabled=True))
        harness.start()
        self.assertNotIn("condition-scan", harness.schedule_ids())
        self.assertTrue(any(key.startswith("condition-sell") for key in harness.schedule_ids()))
        self.assertIn("live-profit-watchdog", harness.schedule_ids())
        harness.ctx.discovery.conditions.assert_not_awaited()
        harness.ctx.market.subscribe_quotes.assert_awaited()

    def test_no_sell_subscription_even_after_position_refresh(self):
        harness = Harness(settings_for(buy_enabled=True, sell_enabled=False))

        async def exercise():
            await harness.strategy._refresh_live_positions(
                harness.ctx, await harness.ctx.account.balance(), (),
            )
            await harness.strategy._subscribe_live_quote(harness.ctx, "005930")

        asyncio.run(exercise())
        harness.ctx.market.subscribe_quotes.assert_not_awaited()

    def test_queued_events_cannot_revive_disabled_flow_or_submit_order(self):
        harness = Harness(settings_for(buy_enabled=False, sell_enabled=False))
        harness.start()
        # A quote/bar already queued before an editor change is still safe.
        harness.strategy._ensure_live_state()
        harness.strategy._live_positions = {"005930": harness.position}

        async def exercise():
            await harness.strategy.on_quote(harness.ctx, SimpleNamespace(
                instrument=SimpleNamespace(code="005930"), price=Decimal("110"),
                delayed=False, stale=False, received_at=harness.now, market_time=harness.now,
            ))
            for schedule_id in ("condition-scan", "condition-sell-bars-1", "live-profit-watchdog"):
                event = SimpleNamespace(schedule_id=schedule_id, scheduled_at=harness.now)
                await harness.strategy.on_timer(harness.ctx, event)
            await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
                schedule_id="condition-sell-bars-1", scheduled_at=harness.now,
            ))

        asyncio.run(exercise())
        harness.ctx.account.balance.assert_not_awaited()
        harness.ctx.discovery.conditions.assert_not_awaited()
        harness.ctx.market.quote.assert_not_awaited()
        harness.ctx.market.bars.assert_not_awaited()
        self.assert_no_orders(harness)

    def test_execution_only_registers_and_dispatches_its_schedule(self):
        harness = Harness(settings_for(
            buy_enabled=False, sell_enabled=False, execution_flows=[execution_flow()],
        ))
        harness.start()
        self.assertEqual(harness.schedule_ids(), ["execution-manage-unfilled"])
        manager = harness.strategy._execution_rule_manager
        manager.tick = AsyncMock()
        asyncio.run(harness.strategy.on_timer(harness.ctx, SimpleNamespace(
            schedule_id="execution-manage-unfilled", scheduled_at=harness.now,
        )))
        manager.tick.assert_awaited_once_with(harness.ctx, "unfilled")
        harness.ctx.discovery.conditions.assert_not_awaited()
        harness.ctx.market.subscribe_quotes.assert_not_awaited()
        self.assert_no_orders(harness)

    def test_disabled_buy_does_not_require_symbol_source_candidates(self):
        settings = settings_for(buy_enabled=False, target_source="SYMBOLS", symbols=[])
        harness = Harness(settings)
        self.assertEqual(harness.namespace["BUY_FLOWS"], ())


if __name__ == "__main__":
    unittest.main()
