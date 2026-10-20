from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from trade_router import AsyncTradeRouter, OrderRoute, OrderSide, OrderType, StrategyConfig
from trade_router.client import SyncOrderClient


@pytest.mark.parametrize("side", ("BUY", "SELL"))
@pytest.mark.parametrize("synchronous", (False, True))
def test_best_limit_sdk_submits_unpriced_order_without_losing_route_or_identity(
    side: str, synchronous: bool
) -> None:
    requests: list[dict] = []
    client_order_id = f"best-limit-{side.lower()}"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/orders"
        assert request.headers["x-trade-router-strategy"] == "best-limit-sdk"
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload == {
            "code": "005930",
            "side": side,
            "order_type": "BEST_LIMIT",
            "quantity": "2",
            "price": None,
            "client_order_id": client_order_id,
            "route": "NXT",
            "provider_options": {"memo": "sdk-test"},
        }
        return httpx.Response(
            200,
            json={
                "id": "accepted-order",
                "client_order_id": client_order_id,
                "account_alias": "broker-account",
                "status": "ACKNOWLEDGED",
                "submitted_at": "2026-10-21T16:30:00+09:00",
                "provider_order_id": "broker-order",
                "request": {
                    **payload,
                    "instrument": {"code": payload["code"]},
                },
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://router.test"
    )
    router = AsyncTradeRouter(
        strategy=StrategyConfig(
            id="best-limit-sdk", trading_account="broker-account"
        ),
        http_client=http,
    )
    # Exercise the same sync facade used by TradeRouterClient, with an isolated
    # event loop and mock transport instead of creating a real runtime connection.
    orders = (
        SyncOrderClient(SimpleNamespace(_async=router, _call=asyncio.run))
        if synchronous
        else router.orders
    )
    try:
        result = getattr(orders, f"best_limit_{side.lower()}")(
            "005930",
            Decimal("2"),
            client_order_id=client_order_id,
            route=OrderRoute.NXT,
            provider_options={"memo": "sdk-test"},
        )
        order = result if synchronous else asyncio.run(result)
        assert len(requests) == 1
        assert order.request.order_type is OrderType.BEST_LIMIT
        assert order.request.side is OrderSide(side)
        assert order.request.price is None
        assert order.request.route is OrderRoute.NXT
        assert order.client_order_id == client_order_id
        assert order.provider_order_id == "broker-order"
        assert order.filled_quantity == 0
    finally:
        asyncio.run(http.aclose())
