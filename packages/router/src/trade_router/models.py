from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


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

    def __post_init__(self) -> None:
        if self.price < 0:
            raise ValueError("Quote price cannot be negative.")
        if self.market_time.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("Quote timestamps must include a timezone.")


@dataclass(frozen=True, slots=True)
class BrokerCapabilities:
    provider: str
    quotes: bool = False
    orderbook: bool = False
    bars: bool = False
    realtime: bool = False
    accounts: bool = False
    cash_orders: bool = False
    conditions: bool = False
    venues: tuple[Venue, ...] = ()
    order_routes: tuple[OrderRoute, ...] = ()
