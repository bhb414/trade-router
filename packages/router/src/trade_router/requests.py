from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from .rate_limits import RateLimitManager, RateLimitScope


class RequestPriority(IntEnum):
    RECONCILIATION = 0
    RISK = 10
    ORDER = 20
    ACCOUNT = 30
    STRATEGY_DATA = 40
    DISCOVERY = 50
    INTERACTIVE = 60
    HISTORY = 70


@dataclass(frozen=True, slots=True)
class BrokerRequestIntent:
    connection_id: str
    operation: str
    priority: RequestPriority
    rate_limit_scopes: tuple[RateLimitScope, ...] = ()
    dedupe_key: str | None = None
    cache_ttl: float = 0.0
    deadline: float | None = None

    def __post_init__(self) -> None:
        if not self.connection_id or not self.operation:
            raise ValueError("Connection and operation are required.")
        if self.cache_ttl < 0:
            raise ValueError("Cache TTL cannot be negative.")


@dataclass(order=True, slots=True)
class _QueuedRequest:
    priority: int
    sequence: int
    intent: BrokerRequestIntent = field(compare=False)
    operation: Callable[[], Awaitable[Any]] = field(compare=False)
    future: asyncio.Future[Any] = field(compare=False)


class RequestManager:
    """Priority queue, hierarchical throttling, de-duplication and short cache."""

    def __init__(
        self,
        rate_limits: RateLimitManager,
        *,
        worker_count: int = 4,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if worker_count < 1:
            raise ValueError("At least one request worker is required.")
        self._rate_limits = rate_limits
        self._worker_count = worker_count
        self._clock = clock or time.monotonic
        self._queue: asyncio.PriorityQueue[_QueuedRequest] = asyncio.PriorityQueue()
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()
        self._workers: list[asyncio.Task[None]] = []
        self._sequence = 0
        self._closed = False
        self._completed = 0
        self._deduplicated = 0
        self._cache_hits = 0
        self._next_cache_prune_at = 0.0

    async def execute(
        self,
        intent: BrokerRequestIntent,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        if self._closed:
            raise RuntimeError("Request manager is closed.")
        await self._ensure_started()

        key = intent.dedupe_key
        now = self._clock()
        if now >= self._next_cache_prune_at:
            self._cache = {
                cached_key: cached
                for cached_key, cached in self._cache.items()
                if cached[0] > now
            }
            self._next_cache_prune_at = now + 60.0
        if key:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                self._cache_hits += 1
                return cached[1]

        created = False
        async with self._lock:
            if key and key in self._inflight:
                future = self._inflight[key]
                self._deduplicated += 1
            else:
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                self._sequence += 1
                queued = _QueuedRequest(
                    priority=int(intent.priority),
                    sequence=self._sequence,
                    intent=intent,
                    operation=operation,
                    future=future,
                )
                if key:
                    self._inflight[key] = future
                await self._queue.put(queued)
                created = True

        try:
            if intent.deadline is None:
                return await asyncio.shield(future)
            timeout = max(0.0, intent.deadline - self._clock())
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        finally:
            if created and key and future.done():
                async with self._lock:
                    if self._inflight.get(key) is future:
                        self._inflight.pop(key, None)

    async def snapshot(self) -> dict[str, int]:
        return {
            "queued": self._queue.qsize(),
            "inflight": len(self._inflight),
            "completed": self._completed,
            "deduplicated": self._deduplicated,
            "cache_hits": self._cache_hits,
            "cached": len(self._cache),
        }

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._cache.clear()
        while not self._queue.empty():
            queued = self._queue.get_nowait()
            if not queued.future.done():
                queued.future.set_exception(RuntimeError("Request manager closed."))
            self._queue.task_done()

    async def _ensure_started(self) -> None:
        if self._workers:
            return
        async with self._lock:
            if not self._workers:
                self._workers = [
                    asyncio.create_task(self._worker(), name=f"router-request-{index}")
                    for index in range(self._worker_count)
                ]

    async def _worker(self) -> None:
        while True:
            queued = await self._queue.get()
            try:
                await self._rate_limits.acquire(queued.intent.rate_limit_scopes)
                result = await queued.operation()
                if queued.intent.dedupe_key and queued.intent.cache_ttl > 0:
                    self._cache[queued.intent.dedupe_key] = (
                        self._clock() + queued.intent.cache_ttl,
                        result,
                    )
                if not queued.future.done():
                    queued.future.set_result(result)
                self._completed += 1
            except asyncio.CancelledError:
                if not queued.future.done():
                    queued.future.set_exception(RuntimeError("Request worker stopped."))
                raise
            except Exception as exc:
                if not queued.future.done():
                    queued.future.set_exception(exc)
            finally:
                key = queued.intent.dedupe_key
                if key:
                    async with self._lock:
                        if self._inflight.get(key) is queued.future:
                            self._inflight.pop(key, None)
                self._queue.task_done()
