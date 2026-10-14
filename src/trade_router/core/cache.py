from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class _CacheValue(Generic[T]):
    value: T
    expires_at: float


class AsyncSingleFlightCache(Generic[T]):
    """Deduplicates in-flight reads and optionally keeps a short successful-result cache."""

    def __init__(self) -> None:
        self._values: dict[str, _CacheValue[T]] = {}
        self._tasks: dict[str, asyncio.Task[T]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self, key: str, factory: Callable[[], Awaitable[T]], *, ttl: float = 0.0
    ) -> T:
        async with self._lock:
            cached = self._values.get(key)
            now = time.monotonic()
            if cached and cached.expires_at > now:
                return cached.value
            task = self._tasks.get(key)
            if task is None:
                task = asyncio.create_task(factory())
                self._tasks[key] = task
        try:
            value = await asyncio.shield(task)
        finally:
            async with self._lock:
                if self._tasks.get(key) is task and task.done():
                    self._tasks.pop(key, None)
        if ttl > 0:
            async with self._lock:
                self._values[key] = _CacheValue(value, time.monotonic() + ttl)
        return value

    async def invalidate(self, prefix: str | None = None) -> None:
        async with self._lock:
            if prefix is None:
                self._values.clear()
            else:
                for key in [key for key in self._values if key.startswith(prefix)]:
                    self._values.pop(key, None)
