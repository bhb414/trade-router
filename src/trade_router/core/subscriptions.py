from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from uuid import uuid4

from ..adapters.base import ProviderAdapterV1
from ..models import StreamEvent, StreamSubscription


def subscription_key(subscription: StreamSubscription) -> str:
    return (
        f"{subscription.kind.value}:{subscription.venue.value}:"
        f"{subscription.code or subscription.account_alias or '*'}"
    )


@dataclass(slots=True)
class SubscriptionClient:
    id: str
    queue: asyncio.Queue[StreamEvent]


class _ProfileStream:
    def __init__(self, adapter: ProviderAdapterV1) -> None:
        self.adapter = adapter
        self.clients: dict[str, tuple[set[str], asyncio.Queue[StreamEvent]]] = {}
        self.subscriptions: dict[str, StreamSubscription] = {}
        self.changed = asyncio.Event()
        self.closed = False
        self._opened_once = False
        self.task = asyncio.create_task(self._run())

    def add(self, subscriptions: list[StreamSubscription]) -> SubscriptionClient:
        client_id = uuid4().hex
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=500)
        keys = {subscription_key(item) for item in subscriptions}
        before = set(self.subscriptions)
        self.clients[client_id] = (keys, queue)
        for item in subscriptions:
            self.subscriptions[subscription_key(item)] = item
        if set(self.subscriptions) != before:
            self.changed.set()
        return SubscriptionClient(client_id, queue)

    def remove(self, client_id: str) -> None:
        before = set(self.subscriptions)
        self.clients.pop(client_id, None)
        used = (
            set().union(*(keys for keys, _ in self.clients.values()))
            if self.clients
            else set()
        )
        self.subscriptions = {
            key: value for key, value in self.subscriptions.items() if key in used
        }
        if set(self.subscriptions) != before:
            self.changed.set()

    async def close(self) -> None:
        self.closed = True
        self.changed.set()
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task

    async def _run(self) -> None:
        backoff = 1.0
        while not self.closed:
            if not self.subscriptions:
                self.changed.clear()
                await self.changed.wait()
                continue
            configured = list(self.subscriptions.values())
            self.changed.clear()
            if self._opened_once:
                self.adapter.context.connection_generation += 1
            self._opened_once = True
            stream = self.adapter.stream(configured)
            try:
                while not self.closed:
                    event_task = asyncio.create_task(anext(stream))
                    changed_task = asyncio.create_task(self.changed.wait())
                    done, pending = await asyncio.wait(
                        {event_task, changed_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    if changed_task in done and changed_task.result():
                        event_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await event_task
                        await stream.aclose()
                        break
                    event = event_task.result()
                    self._broadcast(event)
                    backoff = 1.0
            except StopAsyncIteration:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await stream.aclose()
                raise
            except Exception:
                with suppress(Exception):
                    await stream.aclose()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _broadcast(self, event: StreamEvent) -> None:
        for keys, queue in self.clients.values():
            if event.subscription_key not in keys:
                continue
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(event)


class SubscriptionHub:
    """One reconnecting provider stream per profile, fanned out to local clients."""

    def __init__(self) -> None:
        self._profiles: dict[str, _ProfileStream] = {}

    def subscribe(
        self,
        profile_id: str,
        adapter: ProviderAdapterV1,
        subscriptions: list[StreamSubscription],
    ) -> SubscriptionClient:
        profile = self._profiles.get(profile_id)
        if profile is None:
            profile = _ProfileStream(adapter)
            self._profiles[profile_id] = profile
        return profile.add(subscriptions)

    def unsubscribe(self, profile_id: str, client_id: str) -> None:
        profile = self._profiles.get(profile_id)
        if profile:
            profile.remove(client_id)

    async def close(self) -> None:
        await asyncio.gather(
            *(profile.close() for profile in self._profiles.values()),
            return_exceptions=True,
        )
        self._profiles.clear()
