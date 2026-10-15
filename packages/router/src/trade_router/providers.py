from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    BrokerCapabilities,
    BuyingPower,
    InstrumentId,
    OrderRequest,
    ProviderOrderAck,
    Quote,
    Venue,
)


@runtime_checkable
class ProviderAdapterV1(Protocol):
    """Small first contract shared by every broker adapter."""

    name: str

    async def capabilities(self) -> BrokerCapabilities: ...

    async def quote(self, instrument: InstrumentId, venue: Venue) -> Quote: ...

    async def close(self) -> None: ...


@runtime_checkable
class OrderExecutionAdapterV1(Protocol):
    """Account-bound order operations implemented by broker adapters.

    The central order service owns idempotency, permissions and risk checks.
    Adapters only translate a validated request to one broker operation.
    """

    def capabilities(self, account_reference: str) -> BrokerCapabilities: ...

    async def buying_power(
        self,
        account_reference: str,
        code: str,
        *,
        order_type: object,
        price: object | None = None,
    ) -> BuyingPower: ...

    async def submit_order(
        self,
        account_reference: str,
        request: OrderRequest,
    ) -> ProviderOrderAck: ...

    async def amend_order(
        self,
        account_reference: str,
        provider_order_id: str,
        *,
        quantity: object,
        order_type: object,
        price: object | None,
        route: object,
    ) -> ProviderOrderAck: ...

    async def cancel_order(
        self,
        account_reference: str,
        provider_order_id: str,
        *,
        quantity: object | None,
        route: object,
    ) -> ProviderOrderAck: ...
