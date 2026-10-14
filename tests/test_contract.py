from __future__ import annotations

from trade_router.adapters.kis import KisAdapter
from trade_router.adapters.namuh import NamuhAdapter
from trade_router.adapters.simulated import SimulatedAdapter
from trade_router.models import Capability, MarketDataVenue, OrderRoute


def test_all_builtin_adapters_expose_v1_capabilities():
    for adapter in (SimulatedAdapter, KisAdapter, NamuhAdapter):
        capabilities = adapter.capabilities()
        assert capabilities.protocol_version == "1"
        assert capabilities.provider == adapter.provider_id
        assert Capability.QUOTE in capabilities.features
        assert MarketDataVenue.KRX in capabilities.market_data_venues
        assert OrderRoute.KRX in capabilities.order_routes
