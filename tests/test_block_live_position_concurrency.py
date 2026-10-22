"""Overlapping account refreshes cannot remove a newer holding's live feed."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from test_block_realtime_exit_paths import action, group, make_harness, stop


def harness():
    return make_harness(group("root", "ALL", stop(), action=action("sell")))


def balance_for(h, code, *, seconds=0):
    positions = () if code is None else (SimpleNamespace(
        instrument=SimpleNamespace(code=code), quantity=Decimal("5"),
        available_quantity=Decimal("5"), average_price=Decimal("100"), strategy_quantity=Decimal("5"),
    ),)
    return SimpleNamespace(positions=positions, as_of=h.now + timedelta(seconds=seconds),
                           buy_commission_rate=Decimal("0"), sell_commission_rate=Decimal("0"), sell_tax_rate=Decimal("0"))


def test_old_subscribe_completion_does_not_remove_new_position_subscription():
    async def check():
        h = harness()
        old_started, release = asyncio.Event(), asyncio.Event()
        unsubscribed = []

        async def subscribe(code, **kwargs):
            if code == "005930":
                old_started.set()
                await release.wait()
            return ({"id": f"subscription-{code}"},)

        async def unsubscribe(identifier):
            unsubscribed.append(identifier)

        h.ctx.market.subscribe_quotes = subscribe
        h.ctx.market.unsubscribe = unsubscribe
        old = asyncio.create_task(h.strategy._refresh_live_positions(h.ctx, balance_for(h, "005930"), ()))
        await old_started.wait()
        await h.strategy._refresh_live_positions(h.ctx, balance_for(h, "000660", seconds=1), ())
        release.set()
        tracked, positions, _ = await old
        assert set(tracked) == set(positions) == {"000660"}
        assert set(h.strategy._live_positions) == {"000660"}
        assert set(h.strategy._live_quote_subscriptions) == {"000660"}
        assert unsubscribed == ["subscription-005930"]

    asyncio.run(check())


def test_late_empty_balance_cannot_erase_newer_holdings_or_cost_rates():
    async def check():
        h = harness()
        newer = balance_for(h, "005930", seconds=2)
        await h.strategy._refresh_live_positions(h.ctx, newer, ())
        subscriptions = dict(h.strategy._live_quote_subscriptions)
        stale = balance_for(h, None, seconds=1)
        stale.sell_tax_rate = Decimal("NaN")
        tracked, positions, pending = await h.strategy._refresh_live_positions(h.ctx, stale, ())
        assert tracked == {"005930": "5"}
        assert set(positions) == {"005930"}
        assert pending == set()
        assert h.strategy._live_quote_subscriptions == subscriptions
        assert h.strategy._profit_cost_rates == (Decimal("0"),) * 3

    asyncio.run(check())


def test_overlapping_same_symbol_refresh_adopts_one_pending_subscription():
    async def check():
        h = harness()
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def subscribe(code, **kwargs):
            calls.append(code)
            started.set()
            await release.wait()
            return ({"id": "single-live-feed"},)

        h.ctx.market.subscribe_quotes = subscribe
        old = asyncio.create_task(h.strategy._refresh_live_positions(h.ctx, balance_for(h, "005930"), ()))
        await started.wait()
        await asyncio.wait_for(h.strategy._refresh_live_positions(h.ctx, balance_for(h, "005930", seconds=1), ()), 1)
        release.set()
        await old
        assert calls == ["005930"]
        assert h.strategy._live_quote_subscriptions["005930"] == {"KRX": ("single-live-feed",)}
        assert h.strategy._live_quote_subscribing == set()

    asyncio.run(check())


def test_delayed_unsubscribe_does_not_remove_a_new_subscription_for_same_symbol():
    async def check():
        h = harness()
        started, release = asyncio.Event(), asyncio.Event()
        counter = 0
        removed = []

        async def subscribe(code, **kwargs):
            nonlocal counter
            counter += 1
            return ({"id": f"feed-{counter}"},)

        async def unsubscribe(identifier):
            removed.append(identifier)
            started.set()
            await release.wait()

        h.ctx.market.subscribe_quotes = subscribe
        h.ctx.market.unsubscribe = unsubscribe
        await h.strategy._refresh_live_positions(h.ctx, balance_for(h, "005930"), ())
        old = asyncio.create_task(h.strategy._refresh_live_positions(h.ctx, balance_for(h, None, seconds=1), ()))
        await started.wait()
        await h.strategy._refresh_live_positions(h.ctx, balance_for(h, "005930", seconds=2), ())
        release.set()
        tracked, positions, _ = await old
        assert set(tracked) == set(positions) == {"005930"}
        assert h.strategy._live_quote_subscriptions["005930"] == {"KRX": ("feed-2",)}
        assert removed == ["feed-1"]

    asyncio.run(check())
