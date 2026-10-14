from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from trade_router import (
    BrokerCapabilities,
    InstrumentId,
    Quote,
    RouterError,
    RouterErrorCode,
    TradeRouter,
    Venue,
)


class SimulatedProvider:
    name = "simulated"

    async def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider=self.name,
            quotes=True,
            venues=(Venue.KRX,),
        )

    async def quote(self, instrument: InstrumentId, venue: Venue) -> Quote:
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        return Quote(
            instrument=instrument,
            price=Decimal("73100"),
            venue=venue,
            provider=self.name,
            market_time=now,
            received_at=now,
            correlation_id="test-quote-1",
        )

    async def close(self) -> None:
        return None


def test_router_normalizes_quote() -> None:
    async def run() -> None:
        router = TradeRouter([SimulatedProvider()])
        quote = await router.quote(
            "simulated", InstrumentId("005930"), venue=Venue.KRX
        )
        assert quote.price == Decimal("73100")
        assert quote.instrument.code == "005930"
        assert quote.market_time.tzinfo is not None

    asyncio.run(run())


def test_router_rejects_unsupported_venue_before_provider_call() -> None:
    async def run() -> None:
        router = TradeRouter([SimulatedProvider()])
        with pytest.raises(RouterError) as caught:
            await router.quote(
                "simulated", InstrumentId("005930"), venue=Venue.NXT
            )
        assert caught.value.code is RouterErrorCode.UNSUPPORTED_CAPABILITY

    asyncio.run(run())
