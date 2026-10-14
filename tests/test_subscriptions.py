from __future__ import annotations

import asyncio

from trade_router.adapters.base import AdapterContext
from trade_router.adapters.simulated import SimulatedAdapter
from trade_router.core.subscriptions import SubscriptionHub
from trade_router.core.time import now_kst
from trade_router.models import (
    ProfilePublic,
    ProviderEnvironment,
    StreamKind,
    StreamSubscription,
)


class CountingSimulated(SimulatedAdapter):
    starts = 0

    async def stream(self, subscriptions):
        type(self).starts += 1
        async for event in super().stream(subscriptions):
            yield event


async def test_identical_local_subscriptions_share_one_provider_stream():
    CountingSimulated.starts = 0
    now = now_kst()
    profile = ProfilePublic(
        id="simulated",
        provider="simulated",
        environment=ProviderEnvironment.PAPER,
        created_at=now,
        updated_at=now,
    )
    adapter = CountingSimulated(AdapterContext(profile=profile, secrets={}))
    hub = SubscriptionHub()
    subscription = StreamSubscription(kind=StreamKind.QUOTE, code="005930")
    first = hub.subscribe("simulated", adapter, [subscription])
    await asyncio.wait_for(first.queue.get(), timeout=2)
    second = hub.subscribe("simulated", adapter, [subscription])
    await asyncio.wait_for(second.queue.get(), timeout=2)
    assert CountingSimulated.starts == 1
    await hub.close()
