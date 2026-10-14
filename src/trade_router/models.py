from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RouterModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        use_enum_values=False,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


class ProviderEnvironment(str, Enum):
    LIVE = "live"
    PAPER = "paper"


class AssetType(str, Enum):
    EQUITY = "EQUITY"


class MarketDataVenue(str, Enum):
    KRX = "KRX"
    NXT = "NXT"
    INTEGRATED = "INTEGRATED"


class OrderRoute(str, Enum):
    KRX = "KRX"
    NXT = "NXT"
    SOR = "SOR"
    AUTO = "AUTO"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(str, Enum):
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"
    GTP = "GTP"


class OrderStatus(str, Enum):
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN_RECONCILING = "UNKNOWN_RECONCILING"


class StreamKind(str, Enum):
    QUOTE = "QUOTE"
    ORDERBOOK = "ORDERBOOK"
    TRADE = "TRADE"
    ORDER = "ORDER"
    FILL = "FILL"


class Capability(str, Enum):
    INSTRUMENT_SEARCH = "instruments.search"
    QUOTE = "market.quote"
    ORDERBOOK = "market.orderbook"
    BARS_MINUTE = "market.bars.minute"
    BARS_DAY = "market.bars.day"
    BARS_WEEK = "market.bars.week"
    STREAM_QUOTE = "stream.quote"
    STREAM_ORDERBOOK = "stream.orderbook"
    STREAM_TRADES = "stream.trades"
    STREAM_ORDERS = "stream.orders"
    ACCOUNT_BALANCE = "account.balance"
    ACCOUNT_POSITIONS = "account.positions"
    ACCOUNT_ORDERS = "account.orders"
    ACCOUNT_FILLS = "account.fills"
    BUYING_POWER = "account.buying_power"
    SELLABLE_QUANTITY = "account.sellable_quantity"
    ORDER_MARKET = "order.market"
    ORDER_LIMIT = "order.limit"
    ORDER_AMEND = "order.amend"
    ORDER_CANCEL = "order.cancel"
    DISCOVERY_RANKINGS = "discovery.rankings"
    DISCOVERY_CONDITIONS = "discovery.conditions"


class InstrumentId(RouterModel):
    country: Literal["KR"] = "KR"
    asset: Literal[AssetType.EQUITY] = AssetType.EQUITY
    code: str = Field(min_length=6, max_length=12)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        value = value.strip().upper()
        if not value.isalnum():
            raise ValueError("instrument code must be alphanumeric")
        return value


class Instrument(RouterModel):
    instrument: InstrumentId
    name: str | None = None
    market: str | None = None
    currency: Literal["KRW"] = "KRW"
    nxt_eligible: bool | None = None
    etf: bool | None = None


class SourceMetadata(RouterModel):
    provider: str
    profile: str
    source_timestamp: datetime
    received_at: datetime
    connection_generation: int = 0
    delayed: bool = False
    stale: bool = False
    correlation_id: str

    _timestamps_are_aware = field_validator("source_timestamp", "received_at")(_aware)


class Quote(RouterModel):
    instrument: InstrumentId
    venue: MarketDataVenue
    last: Decimal
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    previous_close: Decimal | None = None
    change: Decimal | None = None
    change_rate: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: int | None = None
    trading_status: str | None = None
    source: SourceMetadata


class OrderBookLevel(RouterModel):
    price: Decimal
    quantity: int
    order_count: int | None = None


class OrderBook(RouterModel):
    instrument: InstrumentId
    venue: MarketDataVenue
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    source: SourceMetadata


class Trade(RouterModel):
    instrument: InstrumentId
    venue: MarketDataVenue
    price: Decimal
    quantity: int
    side: OrderSide | None = None
    source: SourceMetadata


class Bar(RouterModel):
    instrument: InstrumentId
    venue: MarketDataVenue
    interval: str
    started_at: datetime
    ended_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    complete: bool = True
    source: SourceMetadata

    _timestamps_are_aware = field_validator("started_at", "ended_at")(_aware)


class Balance(RouterModel):
    account_alias: str
    currency: Literal["KRW"] = "KRW"
    cash: Decimal | None = None
    available_cash: Decimal | None = None
    buying_power: Decimal | None = None
    total_equity: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    source: SourceMetadata


class Position(RouterModel):
    account_alias: str
    instrument: InstrumentId
    name: str | None = None
    quantity: int
    available_quantity: int | None = None
    average_price: Decimal | None = None
    market_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    unrealized_pnl_rate: Decimal | None = None
    source: SourceMetadata


class BuyingPower(RouterModel):
    account_alias: str
    instrument: InstrumentId
    order_type: OrderType
    price: Decimal | None = None
    max_quantity: int
    available_amount: Decimal | None = None
    calculation_price: Decimal | None = None
    source: SourceMetadata


class OrderRequest(RouterModel):
    strategy_id: str
    account_alias: str
    instrument: InstrumentId
    side: OrderSide
    order_type: OrderType
    quantity: int = Field(gt=0)
    price: Decimal | None = Field(default=None, gt=0)
    route: OrderRoute = OrderRoute.AUTO
    time_in_force: TimeInForce = TimeInForce.DAY
    client_order_id: str = Field(min_length=1, max_length=128)
    provider_options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_price(self) -> "OrderRequest":
        if self.order_type == OrderType.LIMIT and self.price is None:
            raise ValueError("limit orders require price")
        if self.order_type == OrderType.MARKET and self.price is not None:
            raise ValueError("market orders must not include price")
        return self


class OrderPreview(RouterModel):
    request: OrderRequest
    allowed: bool
    estimated_price: Decimal | None = None
    estimated_notional: Decimal | None = None
    max_notional: Decimal | None = None
    available_quantity: int | None = None
    available_amount: Decimal | None = None
    block_code: str | None = None
    block_reason: str | None = None


class Order(RouterModel):
    router_order_id: str
    client_order_id: str
    strategy_id: str
    account_alias: str
    provider: str
    provider_order_id: str | None = None
    instrument: InstrumentId
    side: OrderSide
    order_type: OrderType
    route: OrderRoute
    time_in_force: TimeInForce
    quantity: int
    price: Decimal | None = None
    filled_quantity: int = 0
    average_fill_price: Decimal | None = None
    status: OrderStatus
    submitted_at: datetime
    updated_at: datetime
    message: str | None = None
    correlation_id: str

    _timestamps_are_aware = field_validator("submitted_at", "updated_at")(_aware)


class Fill(RouterModel):
    fill_id: str
    router_order_id: str
    provider_fill_id: str | None = None
    instrument: InstrumentId
    side: OrderSide
    quantity: int
    price: Decimal
    fee: Decimal | None = None
    tax: Decimal | None = None
    filled_at: datetime

    _timestamp_is_aware = field_validator("filled_at")(_aware)


class OrderSubmission(RouterModel):
    accepted: bool
    order: Order | None = None
    replayed: bool = False
    block_code: str | None = None
    block_reason: str | None = None


class RiskLimits(RouterModel):
    max_order_krw: Decimal | None = Field(default=None, gt=0)
    max_daily_buy_krw: Decimal | None = Field(default=None, gt=0)
    max_exposure_krw: Decimal | None = Field(default=None, gt=0)
    max_open_orders: int | None = Field(default=None, gt=0)
    allowed_order_types: set[OrderType] = Field(
        default_factory=lambda: {OrderType.MARKET, OrderType.LIMIT}
    )
    allowed_routes: set[OrderRoute] = Field(
        default_factory=lambda: {
            OrderRoute.KRX,
            OrderRoute.NXT,
            OrderRoute.SOR,
            OrderRoute.AUTO,
        }
    )


class StrategyConfig(RouterModel):
    id: str = Field(min_length=1, max_length=128)
    trading_account: str
    market_data: str
    risk: RiskLimits = Field(default_factory=RiskLimits)


class BrokerCapabilities(RouterModel):
    provider: str
    adapter_version: str
    protocol_version: Literal["1"] = "1"
    environments: set[ProviderEnvironment]
    features: set[Capability]
    market_data_venues: set[MarketDataVenue]
    order_routes: set[OrderRoute]
    bar_intervals: set[str]
    provider_option_schema: dict[str, Any] = Field(default_factory=dict)

    def supports(self, feature: Capability | str) -> bool:
        try:
            normalized = (
                feature if isinstance(feature, Capability) else Capability(feature)
            )
        except ValueError:
            return False
        return normalized in self.features


class ProfilePublic(RouterModel):
    id: str
    provider: str
    environment: ProviderEnvironment
    display_name: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    secret_configured: bool = False
    created_at: datetime
    updated_at: datetime

    _timestamps_are_aware = field_validator("created_at", "updated_at")(_aware)


class AccountPublic(RouterModel):
    alias: str
    profile_id: str
    masked_account: str
    armed: bool
    risk: RiskLimits
    created_at: datetime
    updated_at: datetime

    _timestamps_are_aware = field_validator("created_at", "updated_at")(_aware)


class StrategyPublic(RouterModel):
    id: str
    trading_account: str
    market_data: str
    armed: bool
    risk: RiskLimits
    created_at: datetime
    updated_at: datetime

    _timestamps_are_aware = field_validator("created_at", "updated_at")(_aware)


class StreamSubscription(RouterModel):
    kind: StreamKind
    code: str | None = None
    venue: MarketDataVenue = MarketDataVenue.INTEGRATED
    account_alias: str | None = None


class StreamEvent(RouterModel):
    kind: StreamKind
    subscription_key: str
    sequence: int
    event_id: str
    occurred_at: datetime
    payload: dict[str, Any]

    _timestamp_is_aware = field_validator("occurred_at")(_aware)


class RankingItem(RouterModel):
    rank: int
    instrument: Instrument
    value: Decimal | int | None = None
    change_rate: Decimal | None = None
    provider_fields: dict[str, Any] = Field(default_factory=dict)


class ConditionDefinition(RouterModel):
    id: str
    name: str


class HealthStatus(RouterModel):
    status: Literal["starting", "ready", "degraded", "stopping"]
    version: str
    database_ready: bool
    unresolved_orders: int = 0
    accepting_orders: bool = False
    started_at: datetime

    _timestamp_is_aware = field_validator("started_at")(_aware)
