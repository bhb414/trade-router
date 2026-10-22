from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any


class RuntimeEventBuffer:
    """Bounded event slots with lossless order/fill backpressure.

    Under pressure a quote slot retains its lowest, highest and latest price,
    in arrival order. Keeping only the newest quote erases a stop threshold
    touched before a rebound. A slot contains at most three observations.
    """

    def __init__(self, capacity: int = 1_000, *, priority: bool = False,
                 coalesce_schedules: bool = False) -> None:
        self.capacity = max(1, capacity)
        self.priority = priority
        self.coalesce_schedules = coalesce_schedules
        self._slots: deque[list[dict[str, Any]]] = deque()
        self._condition = asyncio.Condition()
        self._closed = False
        self._priority_streak = 0
        self.coalesced = 0
        self.high_water = 0

    @staticmethod
    def _key(event: dict[str, Any]) -> tuple[Any, ...] | None:
        kind = event.get("event_type", "schedule")
        if kind == "schedule" and event.get("schedule_id"):
            return (kind, event["schedule_id"])
        if kind not in {"quote", "orderbook"}:
            return None
        payload = event.get(kind)
        if not isinstance(payload, dict):
            return None
        instrument = payload.get("instrument") or {}
        if not instrument.get("code"):
            return None
        return (kind, event.get("source_alias"), payload.get("provider"),
                payload.get("venue"), instrument.get("country", "KR"),
                instrument.get("asset", "EQUITY"), instrument["code"],
                payload.get("connection_generation", 0),
                bool(payload.get("stale")), bool(payload.get("delayed")))

    def _merge(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if events[-1].get("event_type") != "quote":
            self.coalesced += len(events) - 1
            return events[-1:]
        try:
            prices = [Decimal(str(event["quote"]["price"])) for event in events]
            if not all(price.is_finite() for price in prices):
                return events
        except (KeyError, ValueError, InvalidOperation):
            return events
        selected = {min(range(len(prices)), key=prices.__getitem__),
                    max(range(len(prices)), key=prices.__getitem__), len(events) - 1}
        result = [event for index, event in enumerate(events) if index in selected]
        self.coalesced += len(events) - len(result)
        return result

    async def put(self, event: dict[str, Any]) -> None:
        value = dict(event)
        key = self._key(value)
        async with self._condition:
            while not self._closed:
                merge = (len(self._slots) >= self.capacity
                         or (self.coalesce_schedules and key and key[0] == "schedule"))
                if merge and key is not None:
                    matching = [slot for slot in self._slots if self._key(slot[0]) == key]
                    if matching:
                        combined = [item for slot in matching for item in slot] + [value]
                        merged = self._merge(combined)
                        if len(merged) <= 3:
                            first = matching[0]
                            matching_ids = {id(slot) for slot in matching[1:]}
                            self._slots = deque(slot for slot in self._slots
                                                if id(slot) not in matching_ids)
                            first[:] = merged
                            self._condition.notify_all()
                            return
                if len(self._slots) < self.capacity:
                    self._slots.append([value])
                    self.high_water = max(self.high_water, len(self._slots))
                    self._condition.notify_all()
                    return
                # Malformed market messages contain no usable threshold data.
                if value.get("event_type") in {"quote", "orderbook"} and key is None:
                    index = next((i for i, slot in enumerate(self._slots)
                                  if slot[0].get("event_type") in {"quote", "orderbook"}
                                  and self._key(slot[0]) is None), None)
                    if index is not None:
                        del self._slots[index]
                        self.coalesced += 1
                        continue
                # Never discard an order/fill or a different symbol's price.
                # Consumers can free space without acquiring the bus lock.
                await self._condition.wait()
            raise RuntimeError("Runtime event buffer is closed.")

    async def get(self) -> dict[str, Any]:
        async with self._condition:
            while not self._slots:
                if self._closed:
                    raise RuntimeError("Runtime event buffer is closed.")
                await self._condition.wait()
            index = 0
            if self.priority and self._priority_streak < 16:
                def rank(slot: list[dict[str, Any]]) -> int:
                    kind = slot[0].get("event_type", "schedule")
                    return 0 if kind in {"order", "fill"} else 1 if kind == "quote" else 2
                index = min(range(len(self._slots)), key=lambda i: rank(self._slots[i]))
                self._priority_streak = self._priority_streak + 1 if rank(self._slots[index]) < 2 else 0
            else:
                self._priority_streak = 0
            slot = self._slots[index]
            value = slot.pop(0)
            if not slot:
                del self._slots[index]
            self._condition.notify_all()
            return value

    def qsize(self) -> int:
        return sum(len(slot) for slot in self._slots)

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._slots.clear()
            self._condition.notify_all()


class StrategyEventBus:
    """Prioritize live decisions while retaining order/fill notifications."""

    def __init__(self, *, queue_size: int = 1_000) -> None:
        self._queue_size = queue_size
        self._queues: dict[str, RuntimeEventBuffer] = {}
        self._lock = asyncio.Lock()

    async def _queue(self, strategy_name: str) -> RuntimeEventBuffer:
        async with self._lock:
            return self._queues.setdefault(strategy_name, RuntimeEventBuffer(self._queue_size, priority=True))

    async def publish(self, strategy_name: str, event: dict[str, Any]) -> None:
        queue = await self._queue(strategy_name)
        try:
            await queue.put(event)
        except RuntimeError:
            # Unregistration explicitly ends this consumer's lifetime.
            return

    async def publish_many(
        self, strategy_names: Iterable[str], event: dict[str, Any]
    ) -> None:
        for strategy_name in tuple(dict.fromkeys(strategy_names)):
            await self.publish(strategy_name, event)

    async def next_event(
        self, strategy_name: str, *, timeout: float = 25.0
    ) -> dict[str, Any] | None:
        queue = await self._queue(strategy_name)
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except (TimeoutError, RuntimeError):
            return None

    async def unregister(self, strategy_name: str) -> None:
        async with self._lock:
            queue = self._queues.pop(strategy_name, None)
        if queue is not None:
            await queue.close()

    async def snapshot(self) -> dict[str, int]:
        async with self._lock:
            return {name: queue.qsize() for name, queue in self._queues.items()}
