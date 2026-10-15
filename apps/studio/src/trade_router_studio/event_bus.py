from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any


class StrategyEventBus:
    """Bounded in-process fan-out for runtime-owned strategy events."""

    def __init__(self, *, queue_size: int = 1_000) -> None:
        self._queue_size = queue_size
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, strategy_name: str, event: dict[str, Any]) -> None:
        async with self._lock:
            queue = self._queues.setdefault(
                strategy_name, asyncio.Queue(maxsize=self._queue_size)
            )
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(dict(event))

    async def publish_many(
        self, strategy_names: Iterable[str], event: dict[str, Any]
    ) -> None:
        for strategy_name in tuple(dict.fromkeys(strategy_names)):
            await self.publish(strategy_name, event)

    async def next_event(
        self, strategy_name: str, *, timeout: float = 25.0
    ) -> dict[str, Any] | None:
        async with self._lock:
            queue = self._queues.setdefault(
                strategy_name, asyncio.Queue(maxsize=self._queue_size)
            )
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def unregister(self, strategy_name: str) -> None:
        async with self._lock:
            self._queues.pop(strategy_name, None)

    async def snapshot(self) -> dict[str, int]:
        async with self._lock:
            return {name: queue.qsize() for name, queue in self._queues.items()}
