from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import BrokerCapabilities, InstrumentId, Quote, Venue


@runtime_checkable
class ProviderAdapterV1(Protocol):
    """Small first contract shared by every broker adapter."""

    name: str

    async def capabilities(self) -> BrokerCapabilities: ...

    async def quote(self, instrument: InstrumentId, venue: Venue) -> Quote: ...

    async def close(self) -> None: ...
