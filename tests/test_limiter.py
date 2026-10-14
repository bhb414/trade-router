from __future__ import annotations

import asyncio

from trade_router.core.limiter import PriorityRateLimiter


async def test_priority_limiter_prefers_risk_request_when_queued():
    limiter = PriorityRateLimiter(1, period=0.03)
    await limiter.acquire()
    order: list[str] = []

    async def wait(name: str, priority: int):
        await limiter.acquire(priority=priority)
        order.append(name)

    ordinary = asyncio.create_task(wait("market", 20))
    urgent = asyncio.create_task(wait("risk", 0))
    await asyncio.gather(ordinary, urgent)
    await limiter.close()
    assert order == ["risk", "market"]
