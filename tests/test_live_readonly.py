from __future__ import annotations

import os

import pytest

from trade_router.adapters.base import AdapterContext
from trade_router.adapters.kis import KisAdapter
from trade_router.adapters.namuh import NamuhAdapter
from trade_router.core.time import now_kst
from trade_router.models import MarketDataVenue, ProfilePublic, ProviderEnvironment


def _live_context(provider: str) -> AdapterContext:
    prefix = "KIS" if provider == "kis" else "NAMUH"
    key = os.environ.get(f"TRADE_ROUTER_TEST_{prefix}_APP_KEY")
    secret = os.environ.get(f"TRADE_ROUTER_TEST_{prefix}_APP_SECRET")
    if not key or not secret:
        pytest.skip(f"{prefix} read-only test credentials are not configured")
    now = now_kst()
    profile = ProfilePublic(
        id=f"{provider}-readonly",
        provider=provider,
        environment=ProviderEnvironment.LIVE,
        created_at=now,
        updated_at=now,
    )
    return AdapterContext(
        profile=profile, secrets={"app_key": key, "app_secret": secret}
    )


@pytest.mark.live
async def test_kis_live_readonly_quote_orderbook_and_bars():
    adapter = KisAdapter(_live_context("kis"))
    try:
        assert (await adapter.health_check())["ok"] is True
        assert (await adapter.get_quote("005930", MarketDataVenue.KRX)).last > 0
        assert await adapter.get_orderbook("005930", MarketDataVenue.KRX)
        assert await adapter.get_bars(
            "005930", interval="1d", count=2, venue=MarketDataVenue.KRX
        )
    finally:
        await adapter.close()


@pytest.mark.live
async def test_namuh_live_readonly_quote_orderbook_and_bars():
    adapter = NamuhAdapter(_live_context("namuh"))
    try:
        assert (await adapter.health_check())["ok"] is True
        assert (await adapter.get_quote("005930", MarketDataVenue.INTEGRATED)).last > 0
        assert await adapter.get_orderbook("005930", MarketDataVenue.INTEGRATED)
        assert await adapter.get_bars(
            "005930", interval="1d", count=2, venue=MarketDataVenue.INTEGRATED
        )
    finally:
        await adapter.close()
