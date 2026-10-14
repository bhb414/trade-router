from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from ..core.limiter import RateLimiterPool
from ..errors import UnsupportedCapabilityError
from ..models import (
    Balance,
    Bar,
    BrokerCapabilities,
    BuyingPower,
    Capability,
    ConditionDefinition,
    Fill,
    Instrument,
    MarketDataVenue,
    Order,
    OrderBook,
    OrderPreview,
    OrderRequest,
    Position,
    ProfilePublic,
    Quote,
    RankingItem,
    StreamEvent,
    StreamSubscription,
)


@dataclass(slots=True)
class AdapterContext:
    profile: ProfilePublic
    secrets: dict[str, Any]
    connection_generation: int = 1
    settings: dict[str, Any] = field(default_factory=dict)
    persist_secrets: Callable[[dict[str, Any]], None] | None = None
    limiters: RateLimiterPool | None = None

    def save_secrets(self) -> None:
        if self.persist_secrets is not None:
            self.persist_secrets(self.secrets)


class ProviderAdapterV1(ABC):
    provider_id: str
    adapter_version: str = "0.1.0"

    def __init__(self, context: AdapterContext) -> None:
        self.context = context

    @classmethod
    @abstractmethod
    def capabilities(cls) -> BrokerCapabilities: ...

    async def close(self) -> None:
        return None

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider_id}

    def require(self, capability: Capability) -> None:
        if not self.capabilities().supports(capability):
            raise UnsupportedCapabilityError(self.provider_id, capability.value)

    async def search_instruments(
        self, query: str, *, limit: int = 20
    ) -> list[Instrument]:
        raise UnsupportedCapabilityError(
            self.provider_id, Capability.INSTRUMENT_SEARCH.value
        )

    async def get_quote(self, code: str, venue: MarketDataVenue) -> Quote:
        raise UnsupportedCapabilityError(self.provider_id, Capability.QUOTE.value)

    async def get_orderbook(self, code: str, venue: MarketDataVenue) -> OrderBook:
        raise UnsupportedCapabilityError(self.provider_id, Capability.ORDERBOOK.value)

    async def get_bars(
        self,
        code: str,
        *,
        interval: str,
        count: int,
        venue: MarketDataVenue,
        until: date | None = None,
    ) -> list[Bar]:
        raise UnsupportedCapabilityError(self.provider_id, "market.bars")

    async def get_balance(self, account_alias: str, account_number: str) -> Balance:
        raise UnsupportedCapabilityError(
            self.provider_id, Capability.ACCOUNT_BALANCE.value
        )

    async def get_positions(
        self, account_alias: str, account_number: str
    ) -> list[Position]:
        raise UnsupportedCapabilityError(
            self.provider_id, Capability.ACCOUNT_POSITIONS.value
        )

    async def get_orders(
        self, account_alias: str, account_number: str, *, open_only: bool = False
    ) -> list[Order]:
        raise UnsupportedCapabilityError(
            self.provider_id, Capability.ACCOUNT_ORDERS.value
        )

    async def get_fills(self, account_alias: str, account_number: str) -> list[Fill]:
        raise UnsupportedCapabilityError(
            self.provider_id, Capability.ACCOUNT_FILLS.value
        )

    async def get_buying_power(
        self, account_alias: str, account_number: str, request: OrderRequest
    ) -> BuyingPower:
        raise UnsupportedCapabilityError(
            self.provider_id, Capability.BUYING_POWER.value
        )

    async def preview_order(
        self, account_number: str, request: OrderRequest
    ) -> OrderPreview:
        raise UnsupportedCapabilityError(self.provider_id, "order.preview")

    async def submit_order(
        self, account_number: str, request: OrderRequest, *, router_order_id: str
    ) -> Order:
        capability = (
            Capability.ORDER_MARKET
            if request.order_type.value == "MARKET"
            else Capability.ORDER_LIMIT
        )
        raise UnsupportedCapabilityError(self.provider_id, capability.value)

    async def amend_order(
        self,
        account_number: str,
        order: Order,
        *,
        quantity: int | None,
        price: str | None,
    ) -> Order:
        raise UnsupportedCapabilityError(self.provider_id, Capability.ORDER_AMEND.value)

    async def cancel_order(self, account_number: str, order: Order) -> Order:
        raise UnsupportedCapabilityError(
            self.provider_id, Capability.ORDER_CANCEL.value
        )

    async def stream(
        self, subscriptions: list[StreamSubscription]
    ) -> AsyncIterator[StreamEvent]:
        if False:
            yield StreamEvent  # pragma: no cover
        raise UnsupportedCapabilityError(self.provider_id, "stream")

    async def rankings(
        self, ranking: str, *, market: str = "ALL", limit: int = 20
    ) -> list[RankingItem]:
        raise UnsupportedCapabilityError(
            self.provider_id, Capability.DISCOVERY_RANKINGS.value
        )

    async def list_conditions(self) -> list[ConditionDefinition]:
        raise UnsupportedCapabilityError(
            self.provider_id, Capability.DISCOVERY_CONDITIONS.value
        )

    async def run_condition(self, condition_id: str) -> list[Instrument]:
        raise UnsupportedCapabilityError(
            self.provider_id, Capability.DISCOVERY_CONDITIONS.value
        )
