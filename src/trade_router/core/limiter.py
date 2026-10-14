from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass(order=True)
class _Waiter:
    priority: int
    sequence: int
    future: asyncio.Future[None] = field(compare=False)


class PriorityRateLimiter:
    """Sliding-window limiter that releases lower numeric priorities first."""

    def __init__(self, calls: int, period: float = 1.0) -> None:
        if calls <= 0 or period <= 0:
            raise ValueError("calls and period must be positive")
        self.calls = calls
        self.period = period
        self._timestamps: deque[float] = deque()
        self._waiters: list[_Waiter] = []
        self._counter = itertools.count()
        self._condition = asyncio.Condition()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    async def acquire(self, *, priority: int = 20) -> None:
        if self._closed:
            raise RuntimeError("rate limiter is closed")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        async with self._condition:
            heapq.heappush(
                self._waiters, _Waiter(priority, next(self._counter), future)
            )
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._run())
            self._condition.notify_all()
        try:
            await future
        except asyncio.CancelledError:
            future.cancel()
            async with self._condition:
                self._condition.notify_all()
            raise

    async def _run(self) -> None:
        while True:
            async with self._condition:
                while self._waiters and self._waiters[0].future.cancelled():
                    heapq.heappop(self._waiters)
                if not self._waiters or self._closed:
                    return
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self.period:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.calls:
                    waiter = heapq.heappop(self._waiters)
                    self._timestamps.append(now)
                    if not waiter.future.done():
                        waiter.future.set_result(None)
                    continue
                wait_for = max(0.001, self.period - (now - self._timestamps[0]))
            await asyncio.sleep(wait_for)

    async def close(self) -> None:
        self._closed = True
        async with self._condition:
            for waiter in self._waiters:
                if not waiter.future.done():
                    waiter.future.cancel()
            self._waiters.clear()
            self._condition.notify_all()
        if self._worker:
            self._worker.cancel()

    def snapshot(self) -> dict[str, int | float | bool]:
        now = time.monotonic()
        return {
            "calls": self.calls,
            "period_seconds": self.period,
            "recent_calls": sum(
                1 for value in self._timestamps if now - value < self.period
            ),
            "queued": sum(1 for item in self._waiters if not item.future.done()),
            "closed": self._closed,
        }


class RateLimiterPool:
    def __init__(self) -> None:
        self._limiters: dict[tuple[str, str], PriorityRateLimiter] = {}

    def get(
        self, profile: str, group: str, *, calls: int, period: float = 1.0
    ) -> PriorityRateLimiter:
        key = (profile, group)
        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = PriorityRateLimiter(calls, period)
            self._limiters[key] = limiter
        return limiter

    async def close(self) -> None:
        await asyncio.gather(
            *(limiter.close() for limiter in self._limiters.values()),
            return_exceptions=True,
        )
        self._limiters.clear()

    def snapshot(self) -> list[dict[str, str | int | float | bool]]:
        return [
            {"profile": profile, "group": group, **limiter.snapshot()}
            for (profile, group), limiter in sorted(self._limiters.items())
        ]
