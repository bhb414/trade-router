from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from ..core.time import KST, now_kst
from ..errors import OrderUncertainError, ProviderError
from ..models import (
    Balance,
    Bar,
    BrokerCapabilities,
    BuyingPower,
    Capability,
    ConditionDefinition,
    CredentialScope,
    Fill,
    Instrument,
    InstrumentId,
    MarketDataVenue,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderPreview,
    OrderRequest,
    OrderRoute,
    OrderSide,
    OrderStatus,
    Position,
    ProviderEnvironment,
    Quote,
    RankingItem,
    StreamEvent,
    StreamKind,
    StreamSubscription,
    Trade,
)
from .base import ProviderAdapterV1
from .utils import source_metadata


class SimulatedAdapter(ProviderAdapterV1):
    provider_id = "simulated"
    adapter_version = "0.1.0"

    def __init__(self, context) -> None:
        super().__init__(context)
        self._cash: dict[str, Decimal] = {}
        self._positions: dict[str, dict[str, tuple[int, Decimal]]] = {}
        self._orders: dict[str, Order] = {}
        self._fills: list[tuple[str, Fill]] = []
        self._sequence = 0

    @classmethod
    def capabilities(cls) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider=cls.provider_id,
            adapter_version=cls.adapter_version,
            environments={ProviderEnvironment.LIVE, ProviderEnvironment.PAPER},
            credential_scope=CredentialScope.NONE,
            features={
                Capability.INSTRUMENT_SEARCH,
                Capability.QUOTE,
                Capability.ORDERBOOK,
                Capability.BARS_MINUTE,
                Capability.BARS_DAY,
                Capability.BARS_WEEK,
                Capability.STREAM_QUOTE,
                Capability.STREAM_ORDERBOOK,
                Capability.STREAM_TRADES,
                Capability.ACCOUNT_BALANCE,
                Capability.ACCOUNT_POSITIONS,
                Capability.ACCOUNT_ORDERS,
                Capability.ACCOUNT_FILLS,
                Capability.BUYING_POWER,
                Capability.ORDER_MARKET,
                Capability.ORDER_LIMIT,
                Capability.ORDER_AMEND,
                Capability.ORDER_CANCEL,
                Capability.DISCOVERY_RANKINGS,
                Capability.DISCOVERY_CONDITIONS,
            },
            market_data_venues=set(MarketDataVenue),
            order_routes=set(OrderRoute),
            bar_intervals={"1m", "3m", "5m", "10m", "15m", "30m", "60m", "1d", "1w"},
            provider_option_schema={
                "behavior": {"enum": ["fill", "ack", "partial", "reject", "uncertain"]}
            },
        )

    def _price(self, code: str) -> Decimal:
        configured = self.context.settings.get("prices", {}).get(code)
        if configured is not None:
            return Decimal(str(configured))
        seed = int(hashlib.sha256(code.encode()).hexdigest()[:8], 16)
        return Decimal((seed % 9000 + 1000) * 10)

    def _source(self):
        return source_metadata(
            provider=self.provider_id,
            profile=self.context.profile.id,
            generation=self.context.connection_generation,
        )

    async def search_instruments(
        self, query: str, *, limit: int = 20
    ) -> list[Instrument]:
        catalog = {
            "005930": "삼성전자",
            "000660": "SK하이닉스",
            "035420": "NAVER",
            "035720": "카카오",
            "069500": "KODEX 200",
        }
        normalized = query.strip().upper()
        rows = [
            Instrument(
                instrument=InstrumentId(code=code),
                name=name,
                market="KOSPI",
                etf=code == "069500",
                nxt_eligible=code != "069500",
            )
            for code, name in catalog.items()
            if normalized in code or normalized in name.upper()
        ]
        return rows[:limit]

    async def get_quote(self, code: str, venue: MarketDataVenue) -> Quote:
        base = self._price(code)
        return Quote(
            instrument=InstrumentId(code=code),
            venue=venue,
            last=base,
            open=base - Decimal("100"),
            high=base + Decimal("300"),
            low=base - Decimal("250"),
            previous_close=base - Decimal("50"),
            change=Decimal("50"),
            change_rate=Decimal("0.1"),
            bid=base - Decimal("10"),
            ask=base + Decimal("10"),
            volume=123456,
            trading_status="OPEN",
            source=self._source(),
        )

    async def get_orderbook(self, code: str, venue: MarketDataVenue) -> OrderBook:
        price = self._price(code)
        return OrderBook(
            instrument=InstrumentId(code=code),
            venue=venue,
            bids=[
                OrderBookLevel(price=price - Decimal(i * 10), quantity=100 * i)
                for i in range(1, 11)
            ],
            asks=[
                OrderBookLevel(price=price + Decimal(i * 10), quantity=120 * i)
                for i in range(1, 11)
            ],
            source=self._source(),
        )

    async def get_bars(
        self,
        code: str,
        *,
        interval: str,
        count: int,
        venue: MarketDataVenue,
        until: date | None = None,
    ) -> list[Bar]:
        count = max(1, min(count, 1000))
        if interval.endswith("m"):
            minutes = int(interval[:-1])
            step = timedelta(minutes=minutes)
            end = now_kst().replace(second=0, microsecond=0)
        elif interval == "1w":
            step = timedelta(days=7)
            end = datetime.combine(
                until or date.today(), datetime.min.time(), tzinfo=KST
            )
        else:
            step = timedelta(days=1)
            end = datetime.combine(
                until or date.today(), datetime.min.time(), tzinfo=KST
            )
        base = self._price(code)
        bars: list[Bar] = []
        for offset in range(count, 0, -1):
            started = end - step * offset
            wave = Decimal(str(round(math.sin(offset / 5) * 100, 0)))
            open_price = base + wave
            close = open_price + Decimal((offset % 5 - 2) * 10)
            bars.append(
                Bar(
                    instrument=InstrumentId(code=code),
                    venue=venue,
                    interval=interval,
                    started_at=started,
                    ended_at=started + step,
                    open=open_price,
                    high=max(open_price, close) + Decimal("20"),
                    low=min(open_price, close) - Decimal("20"),
                    close=close,
                    volume=1000 + offset,
                    complete=True,
                    source=self._source(),
                )
            )
        return bars

    def _account_cash(self, account_number: str) -> Decimal:
        return self._cash.setdefault(
            account_number, Decimal(str(self.context.settings.get("cash", 10_000_000)))
        )

    async def get_balance(self, account_alias: str, account_number: str) -> Balance:
        cash = self._account_cash(account_number)
        positions = await self.get_positions(account_alias, account_number)
        market_value = sum(
            (p.market_value or Decimal(0) for p in positions), Decimal(0)
        )
        return Balance(
            account_alias=account_alias,
            cash=cash,
            available_cash=cash,
            buying_power=cash,
            total_equity=cash + market_value,
            market_value=market_value,
            unrealized_pnl=sum(
                (p.unrealized_pnl or Decimal(0) for p in positions), Decimal(0)
            ),
            source=self._source(),
        )

    async def get_positions(
        self, account_alias: str, account_number: str
    ) -> list[Position]:
        result: list[Position] = []
        for code, (quantity, average) in self._positions.setdefault(
            account_number, {}
        ).items():
            market = self._price(code)
            pnl = (market - average) * quantity
            result.append(
                Position(
                    account_alias=account_alias,
                    instrument=InstrumentId(code=code),
                    quantity=quantity,
                    available_quantity=quantity,
                    average_price=average,
                    market_price=market,
                    market_value=market * quantity,
                    unrealized_pnl=pnl,
                    unrealized_pnl_rate=(pnl / (average * quantity) * 100)
                    if quantity and average
                    else Decimal(0),
                    source=self._source(),
                )
            )
        return result

    async def get_orders(
        self, account_alias: str, account_number: str, *, open_only: bool = False
    ) -> list[Order]:
        rows = [
            order
            for order in self._orders.values()
            if order.account_alias == account_alias
        ]
        if open_only:
            rows = [
                order
                for order in rows
                if order.status
                in {
                    OrderStatus.ACKNOWLEDGED,
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.CANCEL_PENDING,
                }
            ]
        return rows

    async def get_fills(self, account_alias: str, account_number: str) -> list[Fill]:
        return [fill for alias, fill in self._fills if alias == account_alias]

    async def get_buying_power(
        self, account_alias: str, account_number: str, request: OrderRequest
    ) -> BuyingPower:
        quote = await self.get_quote(
            request.instrument.code, MarketDataVenue.INTEGRATED
        )
        calculation_price = request.price or (quote.last * Decimal("1.30")).quantize(
            Decimal("1")
        )
        cash = self._account_cash(account_number)
        return BuyingPower(
            account_alias=account_alias,
            instrument=request.instrument,
            order_type=request.order_type,
            price=request.price,
            max_quantity=int(cash // calculation_price),
            available_amount=cash,
            calculation_price=calculation_price,
            source=self._source(),
        )

    async def preview_order(
        self, account_number: str, request: OrderRequest
    ) -> OrderPreview:
        buying = await self.get_buying_power(
            request.account_alias, account_number, request
        )
        price = request.price or buying.calculation_price
        notional = price * request.quantity if price is not None else None
        if request.side == OrderSide.SELL:
            owned = self._positions.setdefault(account_number, {}).get(
                request.instrument.code, (0, Decimal(0))
            )[0]
            allowed = owned >= request.quantity
            return OrderPreview(
                request=request,
                allowed=allowed,
                estimated_price=price,
                estimated_notional=notional,
                max_notional=notional,
                available_quantity=owned,
                block_code=None if allowed else "INSUFFICIENT_POSITION",
                block_reason=None
                if allowed
                else "Sell quantity exceeds the simulated position.",
            )
        allowed = buying.max_quantity >= request.quantity
        return OrderPreview(
            request=request,
            allowed=allowed,
            estimated_price=price,
            estimated_notional=notional,
            max_notional=notional,
            available_quantity=buying.max_quantity,
            available_amount=buying.available_amount,
            block_code=None if allowed else "INSUFFICIENT_BUYING_POWER",
            block_reason=None if allowed else "Buy quantity exceeds available cash.",
        )

    async def submit_order(
        self, account_number: str, request: OrderRequest, *, router_order_id: str
    ) -> Order:
        behavior = str(request.provider_options.get("behavior", "fill"))
        now = now_kst()
        if behavior == "uncertain":
            raise OrderUncertainError(
                self.provider_id,
                "Simulation adapter deliberately lost the order acknowledgement.",
                correlation_id=uuid4().hex,
            )
        if behavior == "reject":
            status = OrderStatus.REJECTED
            message = "Simulation adapter rejected the order."
            filled = 0
        elif behavior == "ack":
            status = OrderStatus.ACKNOWLEDGED
            message = "Accepted by simulation adapter."
            filled = 0
        elif behavior == "partial":
            status = OrderStatus.PARTIALLY_FILLED
            message = "Partially filled by simulation adapter."
            filled = max(1, request.quantity // 2)
        else:
            status = OrderStatus.FILLED
            message = "Filled by simulation adapter."
            filled = request.quantity
        price = request.price or self._price(request.instrument.code)
        order = Order(
            router_order_id=router_order_id,
            client_order_id=request.client_order_id,
            strategy_id=request.strategy_id,
            account_alias=request.account_alias,
            provider=self.provider_id,
            provider_order_id=f"F{len(self._orders) + 1:010d}",
            instrument=request.instrument,
            side=request.side,
            order_type=request.order_type,
            route=request.route,
            time_in_force=request.time_in_force,
            quantity=request.quantity,
            price=request.price,
            filled_quantity=filled,
            average_fill_price=price if filled else None,
            status=status,
            submitted_at=now,
            updated_at=now,
            message=message,
            correlation_id=uuid4().hex,
        )
        if filled:
            positions = self._positions.setdefault(account_number, {})
            old_qty, old_average = positions.get(
                request.instrument.code, (0, Decimal(0))
            )
            if request.side == OrderSide.BUY:
                new_qty = old_qty + filled
                average = ((old_average * old_qty) + (price * filled)) / new_qty
                positions[request.instrument.code] = (new_qty, average)
                self._cash[account_number] = (
                    self._account_cash(account_number) - price * filled
                )
            else:
                new_qty = max(0, old_qty - filled)
                if new_qty:
                    positions[request.instrument.code] = (new_qty, old_average)
                else:
                    positions.pop(request.instrument.code, None)
                self._cash[account_number] = (
                    self._account_cash(account_number) + price * filled
                )
        self._orders[router_order_id] = order
        if filled:
            self._fills.append(
                (
                    request.account_alias,
                    Fill(
                        fill_id=f"simulated:{router_order_id}:{filled}",
                        router_order_id=router_order_id,
                        provider_fill_id=f"FF{len(self._fills) + 1:010d}",
                        instrument=request.instrument,
                        side=request.side,
                        quantity=filled,
                        price=price,
                        fee=Decimal(0),
                        tax=Decimal(0),
                        filled_at=now,
                    ),
                )
            )
        return order

    async def cancel_order(self, account_number: str, order: Order) -> Order:
        if order.status not in {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}:
            raise ProviderError(
                "ORDER_NOT_CANCELLABLE", "The simulated order is not cancellable."
            )
        updated = order.model_copy(
            update={"status": OrderStatus.CANCELLED, "updated_at": now_kst()}
        )
        self._orders[order.router_order_id] = updated
        return updated

    async def amend_order(
        self,
        account_number: str,
        order: Order,
        *,
        quantity: int | None,
        price: str | None,
    ) -> Order:
        if order.status not in {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}:
            raise ProviderError(
                "ORDER_NOT_AMENDABLE", "The simulated order is not amendable."
            )
        new_quantity = quantity or order.quantity
        if new_quantity < order.filled_quantity:
            raise ProviderError(
                "ORDER_AMEND_QUANTITY_INVALID", "Quantity is below filled quantity."
            )
        updated = order.model_copy(
            update={
                "quantity": new_quantity,
                "price": Decimal(price) if price is not None else order.price,
                "updated_at": now_kst(),
            }
        )
        self._orders[order.router_order_id] = updated
        return updated

    async def rankings(
        self, ranking: str, *, market: str = "ALL", limit: int = 20
    ) -> list[RankingItem]:
        instruments = await self.search_instruments("", limit=limit)
        return [
            RankingItem(
                rank=index,
                instrument=instrument,
                value=self._price(instrument.instrument.code),
            )
            for index, instrument in enumerate(instruments, start=1)
        ]

    async def list_conditions(self) -> list[ConditionDefinition]:
        return [
            ConditionDefinition(id="simulated-rising", name="Simulated rising stocks")
        ]

    async def run_condition(self, condition_id: str) -> list[Instrument]:
        if condition_id != "simulated-rising":
            return []
        return await self.search_instruments("", limit=3)

    async def stream(
        self, subscriptions: list[StreamSubscription]
    ) -> AsyncIterator[StreamEvent]:
        while True:
            for subscription in subscriptions:
                self._sequence += 1
                if subscription.kind == StreamKind.QUOTE and subscription.code:
                    payload = (
                        await self.get_quote(subscription.code, subscription.venue)
                    ).model_dump(mode="json")
                elif subscription.kind == StreamKind.ORDERBOOK and subscription.code:
                    payload = (
                        await self.get_orderbook(subscription.code, subscription.venue)
                    ).model_dump(mode="json")
                elif subscription.kind == StreamKind.TRADE and subscription.code:
                    payload = Trade(
                        instrument=InstrumentId(code=subscription.code),
                        venue=subscription.venue,
                        price=self._price(subscription.code),
                        quantity=1,
                        source=self._source(),
                    ).model_dump(mode="json")
                else:
                    continue
                yield StreamEvent(
                    kind=subscription.kind,
                    subscription_key=f"{subscription.kind.value}:{subscription.venue.value}:{subscription.code or subscription.account_alias}",
                    sequence=self._sequence,
                    event_id=uuid4().hex,
                    occurred_at=now_kst(),
                    payload=payload,
                )
            await asyncio.sleep(1)
