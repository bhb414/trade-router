from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    ETN = "ETN"


class Venue(StrEnum):
    KRX = "KRX"
    NXT = "NXT"
    INTEGRATED = "INTEGRATED"


class OrderRoute(StrEnum):
    KRX = "KRX"
    NXT = "NXT"
    SOR = "SOR"
    AUTO = "AUTO"


class ConnectionTarget(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(StrEnum):
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN_RECONCILING = "UNKNOWN_RECONCILING"


class RankingKind(StrEnum):
    """Broker-neutral domestic-stock ranking families."""

    HTS_VIEW = "HTS_VIEW"
    VOLUME = "VOLUME"
    TRADING_VALUE = "TRADING_VALUE"
    RISE = "RISE"


@dataclass(frozen=True, slots=True)
class AccountOrder:
    """Broker order normalized for account reconciliation.

    Orders placed outside Trade Router do not have a ``client_order_id``. This
    model is separate from :class:`Order`, which represents a router-owned
    ledger order.
    """

    provider_order_id: str
    account_alias: str
    instrument: "InstrumentId"
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    ordered_at: datetime
    provider: str
    price: Decimal | None = None
    average_fill_price: Decimal | None = None
    route: OrderRoute = OrderRoute.AUTO

    def __post_init__(self) -> None:
        if not self.provider_order_id.strip():
            raise ValueError("Provider order id is required.")
        if self.ordered_at.tzinfo is None:
            raise ValueError("Order timestamp must include a timezone.")
        quantities = (
            self.requested_quantity,
            self.filled_quantity,
            self.remaining_quantity,
        )
        if min(quantities) < 0:
            raise ValueError("Order quantities cannot be negative.")
        if self.filled_quantity + self.remaining_quantity > self.requested_quantity:
            raise ValueError("Filled and remaining quantities exceed requested quantity.")
        if self.price is not None and self.price < 0:
            raise ValueError("Order price cannot be negative.")
        if self.average_fill_price is not None and self.average_fill_price < 0:
            raise ValueError("Average fill price cannot be negative.")


@dataclass(frozen=True, slots=True)
class BuyingPower:
    instrument: "InstrumentId"
    account_alias: str
    order_type: OrderType
    requested_price: Decimal
    calculation_price: Decimal
    cash_available: Decimal
    maximum_quantity: Decimal
    provider: str
    as_of: datetime
    route: OrderRoute = OrderRoute.AUTO

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("Buying-power timestamp must include a timezone.")
        values = (
            self.requested_price,
            self.calculation_price,
            self.cash_available,
            self.maximum_quantity,
        )
        if min(values) < 0:
            raise ValueError("Buying-power values cannot be negative.")


@dataclass(frozen=True, slots=True)
class InstrumentId:
    code: str
    country: str = "KR"
    asset: AssetClass = AssetClass.EQUITY

    def __post_init__(self) -> None:
        code = self.code.strip().upper()
        if not code or len(code) > 24:
            raise ValueError("Instrument code must contain 1 to 24 characters.")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "country", self.country.strip().upper())


@dataclass(frozen=True, slots=True)
class Quote:
    instrument: InstrumentId
    price: Decimal
    venue: Venue
    provider: str
    market_time: datetime
    received_at: datetime
    correlation_id: str
    delayed: bool = False
    source_alias: str | None = None
    connection_generation: int = 0
    stale: bool = False

    def __post_init__(self) -> None:
        if self.price < 0:
            raise ValueError("Quote price cannot be negative.")
        if self.market_time.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("Quote timestamps must include a timezone.")


@dataclass(frozen=True, slots=True)
class Bar:
    instrument: InstrumentId
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    venue: Venue
    provider: str
    correlation_id: str
    complete: bool = True
    source_alias: str | None = None
    received_at: datetime | None = None
    connection_generation: int = 0
    stale: bool = False

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is None or self.close_time.tzinfo is None:
            raise ValueError("Bar timestamps must include a timezone.")
        if self.received_at is not None and self.received_at.tzinfo is None:
            raise ValueError("Bar receive timestamp must include a timezone.")
        if self.close_time <= self.open_time:
            raise ValueError("Bar close time must be after open time.")
        if min(self.open, self.high, self.low, self.close, self.volume) < 0:
            raise ValueError("Bar prices and volume cannot be negative.")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("Bar high is below another price.")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("Bar low is above another price.")


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.price < 0 or self.quantity < 0:
            raise ValueError("Order-book price and quantity cannot be negative.")


@dataclass(frozen=True, slots=True)
class OrderBook:
    instrument: InstrumentId
    asks: tuple[OrderBookLevel, ...]
    bids: tuple[OrderBookLevel, ...]
    venue: Venue
    provider: str
    market_time: datetime
    received_at: datetime
    correlation_id: str
    source_alias: str | None = None
    connection_generation: int = 0
    stale: bool = False

    def __post_init__(self) -> None:
        if self.market_time.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("Order-book timestamps must include a timezone.")


@dataclass(frozen=True, slots=True)
class Position:
    instrument: InstrumentId
    quantity: Decimal
    available_quantity: Decimal
    average_price: Decimal
    market_price: Decimal | None = None
    strategy_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        if min(self.quantity, self.available_quantity, self.average_price) < 0:
            raise ValueError("Position values cannot be negative.")
        if self.available_quantity > self.quantity:
            raise ValueError("Available quantity cannot exceed total quantity.")
        if self.strategy_quantity is not None and self.strategy_quantity < 0:
            raise ValueError("Strategy quantity cannot be negative.")


@dataclass(frozen=True, slots=True)
class Balance:
    cash: Decimal
    withdrawable_cash: Decimal
    total_value: Decimal
    positions: tuple[Position, ...] = ()
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("Balance timestamp must include a timezone.")


@dataclass(frozen=True, slots=True)
class ConditionDefinition:
    """One broker-saved instrument screening condition."""

    id: str
    name: str
    provider: str
    source_alias: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.name.strip():
            raise ValueError("Condition id and name are required.")


@dataclass(frozen=True, slots=True)
class ConditionMatch:
    """A normalized instrument returned by a broker condition search."""

    condition_id: str
    instrument: InstrumentId
    provider: str
    name: str | None = None
    price: Decimal | None = None
    change_rate: Decimal | None = None
    source_alias: str | None = None

    def __post_init__(self) -> None:
        if not self.condition_id.strip():
            raise ValueError("Condition id is required.")
        if self.price is not None and self.price < 0:
            raise ValueError("Condition-match price cannot be negative.")


@dataclass(frozen=True, slots=True)
class RankingEntry:
    """One normalized instrument in a broker ranking response."""

    rank: int
    kind: RankingKind
    instrument: InstrumentId
    provider: str
    name: str | None = None
    price: Decimal | None = None
    change_rate: Decimal | None = None
    volume: Decimal | None = None
    trading_value: Decimal | None = None
    source_alias: str | None = None

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError("Ranking position must be positive.")
        numeric = (self.price, self.volume, self.trading_value)
        if any(value is not None and value < 0 for value in numeric):
            raise ValueError("Ranking price, volume and trading value cannot be negative.")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    instrument: InstrumentId
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    client_order_id: str
    route: OrderRoute = OrderRoute.AUTO
    price: Decimal | None = None
    provider_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("Order quantity must be positive.")
        if not self.client_order_id.strip():
            raise ValueError("client_order_id is required.")
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("Limit orders require a price.")
        if self.price is not None and self.price <= 0:
            raise ValueError("Order price must be positive.")


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    client_order_id: str
    account_alias: str
    request: OrderRequest
    status: OrderStatus
    submitted_at: datetime
    provider_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    commission: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    costs_complete: bool = False
    message: str | None = None

    def __post_init__(self) -> None:
        if self.submitted_at.tzinfo is None:
            raise ValueError("Order timestamp must include a timezone.")
        if self.filled_quantity < 0 or self.filled_quantity > self.request.quantity:
            raise ValueError("Filled quantity is outside the order quantity.")
        if self.commission < 0 or self.tax < 0:
            raise ValueError("Order costs cannot be negative.")


@dataclass(frozen=True, slots=True)
class ProviderOrderAck:
    """Broker acknowledgement before fills are reconciled into the ledger."""

    provider_order_id: str
    provider: str
    acknowledged_at: datetime
    correlation_id: str
    provider_options: dict[str, Any] = field(default_factory=dict)
    message: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_order_id.strip():
            raise ValueError("Provider order id is required.")
        if self.acknowledged_at.tzinfo is None:
            raise ValueError("Acknowledgement timestamp must include a timezone.")


@dataclass(frozen=True, slots=True)
class Fill:
    id: str
    order_id: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    commission: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    costs_complete: bool = False

    def __post_init__(self) -> None:
        if self.quantity <= 0 or self.price <= 0:
            raise ValueError("Fill quantity and price must be positive.")
        if self.commission < 0 or self.tax < 0:
            raise ValueError("Fill costs cannot be negative.")
        if self.filled_at.tzinfo is None:
            raise ValueError("Fill timestamp must include a timezone.")


@dataclass(frozen=True, slots=True)
class ExtensionCapability:
    id: str
    version: int = 1
    targets: tuple[ConnectionTarget, ...] = ()


@dataclass(frozen=True, slots=True)
class BrokerCapabilities:
    provider: str
    quotes: bool = False
    orderbook: bool = False
    bars: bool = False
    realtime: bool = False
    accounts: bool = False
    order_history: bool = False
    cash_orders: bool = False
    amend_orders: bool = False
    cancel_orders: bool = False
    conditions: bool = False
    rankings: bool = False
    venues: tuple[Venue, ...] = ()
    order_routes: tuple[OrderRoute, ...] = ()
    extensions: tuple[ExtensionCapability, ...] = ()

    def supports_extension(
        self,
        extension_id: str,
        *,
        target: ConnectionTarget | None = None,
    ) -> bool:
        for extension in self.extensions:
            if extension.id != extension_id:
                continue
            return target is None or not extension.targets or target in extension.targets
        return False
