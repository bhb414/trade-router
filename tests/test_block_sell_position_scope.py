"""Offline sell-universe contracts; fake account only, no server or broker calls."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from trade_router import OrderStatus, ScheduleKind
from trade_router_studio.block_packages import (
    ConditionBollingerBlockSettings,
    block_package_from_source,
    render_block_strategy,
)
from test_block_optional_flows import Harness, settings_for


def scope_settings(scope="STRATEGY", *, candle=False, order_type="MARKET", **changes):
    condition = {
        "left": "close" if candle else "profit_rate", "operator": "GTE",
        "right": "value", "value": "150" if candle else "3",
    }
    if candle:
        condition.update(left_bar_interval="3m", left_bar_data_mode="COMPLETE_ONLY")
    values = {
        "buy_enabled": False, "sell_enabled": True,
        "sell_position_scope": scope, "sell_rule_tree": None,
        "sell_order_type": order_type,
        "sell_flows": [{
            "id": "exit", "name": "매도 흐름", "sell_scan_seconds": 20,
            "sell_start_time": "09:00", "sell_end_time": "15:20",
            "sell_condition_groups": [{"id": "condition", "conditions": [condition]}],
        }],
    }
    values.update(changes)
    return settings_for(**values)


def holding(code, *, quantity="10", available="7", owned="2"):
    return SimpleNamespace(
        instrument=SimpleNamespace(code=code), quantity=Decimal(quantity),
        available_quantity=Decimal(available), average_price=Decimal("100"),
        strategy_quantity=None if owned is None else Decimal(owned),
    )


class ScopeHarness(Harness):
    def __init__(self, settings):
        super().__init__(settings)
        self.positions = [holding("005930"), holding("000660", quantity="8", available="6", owned="0")]
        self.pending = []
        self.sent_orders = []
        self.rest_codes = []
        self.ctx.account.balance = AsyncMock(side_effect=self.balance)
        self.ctx.account.open_orders = AsyncMock(side_effect=lambda: tuple(self.pending))
        self.ctx.market.quote = AsyncMock(side_effect=self.rest_quote)
        self.ctx.market.bars = AsyncMock(side_effect=self.bars)
        self.ctx.state.set("strategy_positions", {"005930": "2"})
        for method in ("market_sell", "limit_sell", "best_limit_sell"):
            setattr(self.ctx.orders, method, AsyncMock(side_effect=self.sell))

    async def balance(self):
        return SimpleNamespace(positions=tuple(self.positions), buy_commission_rate=Decimal("0"),
                               sell_commission_rate=Decimal("0"), sell_tax_rate=Decimal("0"))

    def quote(self, code="005930", price="104"):
        return SimpleNamespace(
            instrument=SimpleNamespace(code=code), price=Decimal(price),
            delayed=False, stale=False, received_at=self.now, market_time=self.now,
        )

    async def rest_quote(self, code, **kwargs):
        self.rest_codes.append(code)
        return self.quote(code)

    async def bars(self, code, interval, **kwargs):
        return [SimpleNamespace(
            open=Decimal("190"), high=Decimal("210"), low=Decimal("180"),
            close=Decimal("200"), volume=Decimal("1000"),
        ) for _ in range(kwargs["count"])]

    async def sell(self, **kwargs):
        self.sent_orders.append(kwargs)
        return SimpleNamespace(status=OrderStatus.ACKNOWLEDGED, message="")

    async def refresh(self):
        return await self.strategy._refresh_live_positions(
            self.ctx, await self.balance(), tuple(self.pending),
        )

    async def scheduled(self):
        # Pick the production schedule created by on_start, so both the timer
        # and complete-candle dispatch paths are exercised, not a private helper.
        method, _, kwargs = next(call for call in self.scheduler.calls
                                  if call[2]["schedule_id"].startswith("condition-sell-"))
        event = SimpleNamespace(
            schedule_id=kwargs["schedule_id"], scheduled_at=self.now,
            kind=ScheduleKind.BAR_CLOSE if method == "on_bar_close" else ScheduleKind.INTERVAL,
        )
        await self.strategy.on_timer(self.ctx, event)

    async def watchdog(self):
        await self.strategy.on_timer(self.ctx, SimpleNamespace(
            schedule_id="live-profit-watchdog", scheduled_at=self.now,
        ))


class SellPositionScopeTests(unittest.TestCase):
    def test_legacy_settings_and_source_default_to_strategy_scope(self):
        values = scope_settings().model_dump(mode="json")
        values.pop("sell_position_scope")
        self.assertEqual(ConditionBollingerBlockSettings.model_validate(values).sell_position_scope, "STRATEGY")
        source = render_block_strategy(scope_settings())
        legacy = "\n".join(line for line in source.splitlines()
                           if not line.startswith("SELL_POSITION_SCOPE ="))
        self.assertEqual(block_package_from_source("legacy", legacy).settings.sell_position_scope, "STRATEGY")

    def test_scope_round_trips_including_disabled_sell(self):
        for scope in ("STRATEGY", "ACCOUNT"):
            for enabled in (True, False):
                with self.subTest(scope=scope, enabled=enabled):
                    settings = scope_settings(scope, sell_enabled=enabled)
                    restored = block_package_from_source("scope", render_block_strategy(settings)).settings
                    self.assertEqual(restored.model_dump(mode="json"), settings.model_dump(mode="json"))

    def test_invalid_settings_scope_is_rejected_instead_of_widened(self):
        for value in (None, "", "account", "ALL", True, 0, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                scope_settings(value)

    def test_invalid_explicit_source_scope_is_rejected(self):
        source = render_block_strategy(scope_settings())
        for value in ("None", "''", "'ALL'", "'account'", "True", "0", "[]", "{}", "str('ACCOUNT')"):
            malformed = "\n".join(
                f"SELL_POSITION_SCOPE = {value}" if line.startswith("SELL_POSITION_SCOPE =") else line
                for line in source.splitlines()
            )
            with self.subTest(value=value), self.assertRaises(ValueError):
                block_package_from_source("invalid", malformed)

    def test_live_positions_select_account_holdings_without_adopting_ownership(self):
        for scope, expected in (("STRATEGY", {"005930"}), ("ACCOUNT", {"005930", "000660"})):
            with self.subTest(scope=scope):
                harness = ScopeHarness(scope_settings(scope))
                asyncio.run(harness.refresh())
                self.assertEqual(set(harness.strategy._live_positions), expected)
                self.assertEqual(harness.ctx.state.get("strategy_positions"), {"005930": "2"})
                subscribed = {call.args[0] for call in harness.ctx.market.subscribe_quotes.await_args_list}
                self.assertEqual(subscribed, expected)

    def test_scheduled_and_candle_sells_use_selected_quantities_and_order_scope(self):
        for scope in ("STRATEGY", "ACCOUNT"):
            for candle in (False, True):
                for order_type in ("MARKET", "LIMIT", "BEST_LIMIT"):
                    with self.subTest(scope=scope, candle=candle, order_type=order_type):
                        harness = ScopeHarness(scope_settings(scope, candle=candle, order_type=order_type))
                        harness.start()
                        asyncio.run(harness.scheduled())
                        quantities = {order["code"]: order["quantity"] for order in harness.sent_orders}
                        expected = {"005930": Decimal("2")} if scope == "STRATEGY" else {
                            "005930": Decimal("7"), "000660": Decimal("6"),
                        }
                        self.assertEqual(quantities, expected)
                        method = {"MARKET": "market_sell", "LIMIT": "limit_sell", "BEST_LIMIT": "best_limit_sell"}[order_type]
                        self.assertEqual(getattr(harness.ctx.orders, method).await_count, len(expected))
                        for order in harness.sent_orders:
                            if scope == "ACCOUNT":
                                self.assertEqual(order.get("sell_position_scope"), "ACCOUNT")
                            else:
                                self.assertNotIn("sell_position_scope", order)
                        self.assertEqual(harness.ctx.state.get("strategy_positions"), {"005930": "2"})

    def test_live_profit_exits_use_selected_scope_for_all_order_types(self):
        for scope in ("STRATEGY", "ACCOUNT"):
            for order_type in ("MARKET", "LIMIT", "BEST_LIMIT"):
                with self.subTest(scope=scope, order_type=order_type):
                    harness = ScopeHarness(scope_settings(scope, order_type=order_type))

                    async def exercise():
                        await harness.refresh()
                        for code in ("005930", "000660"):
                            await harness.strategy.on_quote(harness.ctx, harness.quote(code))

                    asyncio.run(exercise())
                    expected = {"005930": Decimal("2")} if scope == "STRATEGY" else {
                        "005930": Decimal("7"), "000660": Decimal("6"),
                    }
                    self.assertEqual({order["code"]: order["quantity"] for order in harness.sent_orders}, expected)
                    method = {"MARKET": "market_sell", "LIMIT": "limit_sell", "BEST_LIMIT": "best_limit_sell"}[order_type]
                    self.assertEqual(getattr(harness.ctx.orders, method).await_count, len(expected))
                    for order in harness.sent_orders:
                        self.assertEqual(order.get("sell_position_scope"), "ACCOUNT" if scope == "ACCOUNT" else None)
                    self.assertEqual(harness.ctx.state.get("strategy_positions"), {"005930": "2"})

    def test_zero_available_or_quantity_never_sells_account_holding(self):
        harness = ScopeHarness(scope_settings("ACCOUNT"))
        harness.positions = [holding("005930", available="0"), holding("000660", quantity="0", owned="0")]
        harness.start()
        asyncio.run(harness.scheduled())
        asyncio.run(harness.strategy.on_quote(harness.ctx, harness.quote()))
        self.assertEqual(harness.sent_orders, [])

    def test_unknown_strategy_quantity_falls_back_only_to_existing_ownership(self):
        for scope, expected in (("STRATEGY", {"005930": Decimal("2")}),
                                ("ACCOUNT", {"005930": Decimal("7"), "000660": Decimal("6")})):
            with self.subTest(scope=scope):
                harness = ScopeHarness(scope_settings(scope))
                for position in harness.positions:
                    position.strategy_quantity = None
                harness.start()
                asyncio.run(harness.scheduled())
                self.assertEqual({order["code"]: order["quantity"] for order in harness.sent_orders}, expected)

    def test_open_orders_and_local_pending_block_duplicate_account_exits(self):
        harness = ScopeHarness(scope_settings("ACCOUNT"))
        harness.pending = [SimpleNamespace(
            instrument=SimpleNamespace(code="000660"), remaining_quantity=Decimal("1"),
            side=SimpleNamespace(value="SELL"),
        )]
        harness.start()

        async def exercise():
            await harness.scheduled()
            for code in ("005930", "000660"):
                await harness.strategy.on_quote(harness.ctx, harness.quote(code))
            await harness.scheduled()

        asyncio.run(exercise())
        self.assertEqual([order["code"] for order in harness.sent_orders], ["005930"])

    def test_account_watchdog_discovers_manual_holding_despite_healthy_old_ticks(self):
        harness = ScopeHarness(scope_settings("ACCOUNT"))
        harness.positions = [holding("005930")]
        harness.start()

        async def exercise():
            await harness.strategy.on_quote(harness.ctx, harness.quote(price="101"))
            await harness.watchdog()
            initial_balance_calls = harness.ctx.account.balance.await_count
            harness.positions.append(holding("000660", quantity="8", available="6", owned="0"))
            for _ in range(5):
                harness.now += timedelta(seconds=5)
                await harness.strategy.on_quote(harness.ctx, harness.quote(price="101"))
                await harness.watchdog()
            self.assertEqual(harness.ctx.account.balance.await_count, initial_balance_calls)
            self.assertNotIn("000660", harness.strategy._live_positions)
            harness.now += timedelta(seconds=5)
            await harness.strategy.on_quote(harness.ctx, harness.quote(price="101"))
            await harness.watchdog()

        asyncio.run(exercise())
        self.assertIn("000660", harness.strategy._live_positions)
        self.assertIn("000660", harness.rest_codes)
        self.assertEqual([(order["code"], order["quantity"]) for order in harness.sent_orders], [("000660", Decimal("6"))])
        self.assertEqual(harness.ctx.state.get("strategy_positions"), {"005930": "2"})

    def test_disabled_account_scope_cannot_start_sell_watchers_or_send_orders(self):
        harness = ScopeHarness(scope_settings("ACCOUNT", sell_enabled=False))
        harness.start()
        asyncio.run(harness.watchdog())
        asyncio.run(harness.strategy.on_quote(harness.ctx, harness.quote()))
        self.assertEqual(harness.scheduler.calls, [])
        harness.ctx.account.balance.assert_not_awaited()
        harness.ctx.market.subscribe_quotes.assert_not_awaited()
        self.assertEqual(harness.sent_orders, [])


if __name__ == "__main__":
    unittest.main()
