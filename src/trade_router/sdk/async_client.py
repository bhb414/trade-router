from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any, TypeVar

import httpx
import websockets
from pydantic import TypeAdapter

from ..core.config import RouterPaths, daemon_base_url
from ..core.secrets import DpapiSecretStore
from ..errors import ErrorBody, RouterError
from ..models import (
    Balance,
    Bar,
    BrokerCapabilities,
    Fill,
    Instrument,
    InstrumentId,
    MarketDataVenue,
    Order,
    OrderBook,
    OrderRequest,
    OrderRoute,
    OrderSide,
    OrderSubmission,
    OrderType,
    Position,
    ProviderEnvironment,
    Quote,
    StrategyConfig,
    StrategyPublic,
    StreamEvent,
    StreamSubscription,
    TimeInForce,
)

T = TypeVar("T")


def _default_token() -> str:
    configured = os.environ.get("TRADE_ROUTER_TOKEN", "").strip()
    if configured:
        return configured
    try:
        value = DpapiSecretStore(RouterPaths.discover().secrets).get(
            "installation.api_token"
        )
    except Exception:
        value = None
    if isinstance(value, str) and value:
        return value
    raise RouterError(
        "LOCAL_AUTH_MISSING", "Set TRADE_ROUTER_TOKEN or start the local daemon once."
    )


class _Service:
    def __init__(self, client: "AsyncTradeRouter") -> None:
        self.client = client

    def market_target(
        self,
        profile: str | None,
        environment: ProviderEnvironment | str | None,
    ) -> tuple[str, str | None]:
        chosen = profile or self.client.strategy.market_data
        selected = (
            ProviderEnvironment(environment).value if environment is not None else None
        )
        if selected is None and chosen == self.client.strategy.market_data:
            configured = self.client.strategy.market_environment
            selected = configured.value if configured else None
        return chosen, selected


class SystemService(_Service):
    async def health(self) -> dict[str, Any]:
        return await self.client._request("GET", "/v1/system/health")

    async def capabilities(
        self, provider: str | None = None
    ) -> list[BrokerCapabilities]:
        data = await self.client._request(
            "GET",
            "/v1/system/capabilities",
            params={"provider": provider} if provider else None,
        )
        return TypeAdapter(list[BrokerCapabilities]).validate_python(data)

    async def connections(self) -> list[dict[str, Any]]:
        return await self.client._request("GET", "/v1/system/connections")

    async def rate_limits(self) -> list[dict[str, Any]]:
        return await self.client._request("GET", "/v1/system/rate-limits")


class InstrumentService(_Service):
    async def search(
        self,
        query: str,
        *,
        profile: str | None = None,
        environment: ProviderEnvironment | str | None = None,
        limit: int = 20,
    ) -> list[Instrument]:
        chosen, selected = self.market_target(profile, environment)
        data = await self.client._request(
            "GET",
            "/v1/instruments/search",
            params={
                "profile": chosen,
                "environment": selected,
                "q": query,
                "limit": limit,
            },
        )
        return TypeAdapter(list[Instrument]).validate_python(data)


class MarketService(_Service):
    async def quote(
        self,
        *,
        code: str,
        venue: MarketDataVenue | str = MarketDataVenue.INTEGRATED,
        profile: str | None = None,
        environment: ProviderEnvironment | str | None = None,
    ) -> Quote:
        chosen, selected = self.market_target(profile, environment)
        data = await self.client._request(
            "GET",
            "/v1/market/quote",
            params={
                "profile": chosen,
                "environment": selected,
                "code": code,
                "venue": MarketDataVenue(venue).value,
            },
        )
        return Quote.model_validate(data)

    async def orderbook(
        self,
        *,
        code: str,
        venue: MarketDataVenue | str = MarketDataVenue.INTEGRATED,
        profile: str | None = None,
        environment: ProviderEnvironment | str | None = None,
    ) -> OrderBook:
        chosen, selected = self.market_target(profile, environment)
        data = await self.client._request(
            "GET",
            "/v1/market/orderbook",
            params={
                "profile": chosen,
                "environment": selected,
                "code": code,
                "venue": MarketDataVenue(venue).value,
            },
        )
        return OrderBook.model_validate(data)

    async def bars(
        self,
        *,
        code: str,
        interval: str,
        count: int = 100,
        venue: MarketDataVenue | str = MarketDataVenue.INTEGRATED,
        profile: str | None = None,
        environment: ProviderEnvironment | str | None = None,
        until: date | None = None,
    ) -> list[Bar]:
        chosen, selected = self.market_target(profile, environment)
        params: dict[str, Any] = {
            "profile": chosen,
            "environment": selected,
            "code": code,
            "interval": interval,
            "count": count,
            "venue": MarketDataVenue(venue).value,
        }
        if until:
            params["until"] = until.isoformat()
        data = await self.client._request("GET", "/v1/market/bars", params=params)
        return TypeAdapter(list[Bar]).validate_python(data)

    async def stream(
        self,
        subscriptions: list[StreamSubscription],
        *,
        profile: str | None = None,
        environment: ProviderEnvironment | str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        chosen, selected = self.market_target(profile, environment)
        ws_url = (
            self.client.base_url.replace("http://", "ws://").replace(
                "https://", "wss://"
            )
            + "/v1/stream"
        )
        async with websockets.connect(
            ws_url, additional_headers={"authorization": f"Bearer {self.client.token}"}
        ) as socket:
            await socket.send(
                TypeAdapter(dict[str, Any])
                .dump_json(
                    {
                        "profile_id": chosen,
                        "environment": selected,
                        "subscriptions": [
                            item.model_dump(mode="json") for item in subscriptions
                        ],
                    }
                )
                .decode()
            )
            async for raw in socket:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                yield StreamEvent.model_validate_json(raw)


class AccountService(_Service):
    async def balance(self) -> Balance:
        data = await self.client._request(
            "GET", f"/v1/accounts/{self.client.strategy.trading_account}/balance"
        )
        return Balance.model_validate(data)

    async def positions(self) -> list[Position]:
        data = await self.client._request(
            "GET", f"/v1/accounts/{self.client.strategy.trading_account}/positions"
        )
        return TypeAdapter(list[Position]).validate_python(data)

    async def orders(
        self, *, source: str = "router", open_only: bool = False
    ) -> list[Order]:
        data = await self.client._request(
            "GET",
            f"/v1/accounts/{self.client.strategy.trading_account}/orders",
            params={"source": source, "open_only": str(open_only).lower()},
        )
        return TypeAdapter(list[Order]).validate_python(data)

    async def fills(self, *, limit: int = 500) -> list[Fill]:
        data = await self.client._request(
            "GET",
            f"/v1/accounts/{self.client.strategy.trading_account}/fills",
            params={"limit": limit},
        )
        return TypeAdapter(list[Fill]).validate_python(data)


class OrderService(_Service):
    def _request_model(
        self,
        *,
        code: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType,
        price: Decimal | int | str | None,
        route: OrderRoute | str,
        time_in_force: TimeInForce | str,
        client_order_id: str,
        provider_options: dict[str, Any] | None,
    ) -> OrderRequest:
        return OrderRequest(
            strategy_id=self.client.strategy.id,
            account_alias=self.client.strategy.trading_account,
            instrument=InstrumentId(code=code),
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=Decimal(str(price)) if price is not None else None,
            route=OrderRoute(route),
            time_in_force=TimeInForce(time_in_force),
            client_order_id=client_order_id,
            provider_options=provider_options or {},
        )

    async def submit(self, request: OrderRequest) -> OrderSubmission:
        if (
            request.strategy_id != self.client.strategy.id
            or request.account_alias != self.client.strategy.trading_account
        ):
            raise RouterError(
                "SDK_ORDER_BINDING_MISMATCH",
                "Order request is not bound to this SDK session.",
            )
        data = await self.client._request(
            "POST", "/v1/orders", json=request.model_dump(mode="json")
        )
        return OrderSubmission.model_validate(data)

    async def market_buy(
        self,
        *,
        code: str,
        quantity: int,
        client_order_id: str,
        route: OrderRoute | str = OrderRoute.AUTO,
        time_in_force: TimeInForce | str = TimeInForce.DAY,
        provider_options: dict[str, Any] | None = None,
    ) -> OrderSubmission:
        return await self.submit(
            self._request_model(
                code=code,
                side=OrderSide.BUY,
                quantity=quantity,
                order_type=OrderType.MARKET,
                price=None,
                route=route,
                time_in_force=time_in_force,
                client_order_id=client_order_id,
                provider_options=provider_options,
            )
        )

    async def market_sell(
        self,
        *,
        code: str,
        quantity: int,
        client_order_id: str,
        route: OrderRoute | str = OrderRoute.AUTO,
        time_in_force: TimeInForce | str = TimeInForce.DAY,
        provider_options: dict[str, Any] | None = None,
    ) -> OrderSubmission:
        return await self.submit(
            self._request_model(
                code=code,
                side=OrderSide.SELL,
                quantity=quantity,
                order_type=OrderType.MARKET,
                price=None,
                route=route,
                time_in_force=time_in_force,
                client_order_id=client_order_id,
                provider_options=provider_options,
            )
        )

    async def limit_buy(
        self,
        *,
        code: str,
        quantity: int,
        price: Decimal | int | str,
        client_order_id: str,
        route: OrderRoute | str = OrderRoute.AUTO,
        time_in_force: TimeInForce | str = TimeInForce.DAY,
        provider_options: dict[str, Any] | None = None,
    ) -> OrderSubmission:
        return await self.submit(
            self._request_model(
                code=code,
                side=OrderSide.BUY,
                quantity=quantity,
                order_type=OrderType.LIMIT,
                price=price,
                route=route,
                time_in_force=time_in_force,
                client_order_id=client_order_id,
                provider_options=provider_options,
            )
        )

    async def limit_sell(
        self,
        *,
        code: str,
        quantity: int,
        price: Decimal | int | str,
        client_order_id: str,
        route: OrderRoute | str = OrderRoute.AUTO,
        time_in_force: TimeInForce | str = TimeInForce.DAY,
        provider_options: dict[str, Any] | None = None,
    ) -> OrderSubmission:
        return await self.submit(
            self._request_model(
                code=code,
                side=OrderSide.SELL,
                quantity=quantity,
                order_type=OrderType.LIMIT,
                price=price,
                route=route,
                time_in_force=time_in_force,
                client_order_id=client_order_id,
                provider_options=provider_options,
            )
        )

    async def cancel(self, router_order_id: str) -> Order:
        return Order.model_validate(
            await self.client._request("POST", f"/v1/orders/{router_order_id}/cancel")
        )

    async def amend(
        self,
        router_order_id: str,
        *,
        quantity: int | None = None,
        price: Decimal | int | str | None = None,
    ) -> Order:
        body = {
            "quantity": quantity,
            "price": str(price) if price is not None else None,
        }
        return Order.model_validate(
            await self.client._request(
                "POST", f"/v1/orders/{router_order_id}/amend", json=body
            )
        )


class DiscoveryService(_Service):
    async def rankings(
        self,
        ranking: str,
        *,
        market: str = "ALL",
        limit: int = 20,
        profile: str | None = None,
        environment: ProviderEnvironment | str | None = None,
    ) -> list[dict[str, Any]]:
        chosen, selected = self.market_target(profile, environment)
        return await self.client._request(
            "GET",
            "/v1/discovery/rankings",
            params={
                "profile": chosen,
                "environment": selected,
                "ranking": ranking,
                "market": market,
                "limit": limit,
            },
        )

    async def conditions(
        self,
        *,
        profile: str | None = None,
        environment: ProviderEnvironment | str | None = None,
    ) -> list[dict[str, Any]]:
        chosen, selected = self.market_target(profile, environment)
        return await self.client._request(
            "GET",
            "/v1/discovery/conditions",
            params={"profile": chosen, "environment": selected},
        )

    async def run_condition(
        self,
        condition_id: str,
        *,
        profile: str | None = None,
        environment: ProviderEnvironment | str | None = None,
    ) -> list[Instrument]:
        chosen, selected = self.market_target(profile, environment)
        data = await self.client._request(
            "POST",
            f"/v1/discovery/conditions/{condition_id}/run",
            params={"profile": chosen, "environment": selected},
        )
        return TypeAdapter(list[Instrument]).validate_python(data)


class AsyncTradeRouter:
    def __init__(
        self,
        *,
        strategy: StrategyConfig,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 15,
    ) -> None:
        self.strategy = strategy
        self.base_url = (base_url or daemon_base_url()).rstrip("/")
        self.token = token or _default_token()
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"authorization": f"Bearer {self.token}"},
        )
        self.system = SystemService(self)
        self.instruments = InstrumentService(self)
        self.market = MarketService(self)
        self.accounts = AccountService(self)
        self.orders = OrderService(self)
        self.discovery = DiscoveryService(self)

    async def __aenter__(self) -> "AsyncTradeRouter":
        data = await self._request("GET", f"/v1/strategies/{self.strategy.id}")
        saved = StrategyPublic.model_validate(data)
        if (
            saved.trading_account != self.strategy.trading_account
            or saved.market_data != self.strategy.market_data
            or (
                self.strategy.market_environment is not None
                and saved.market_environment != self.strategy.market_environment
            )
        ):
            raise RouterError(
                "STRATEGY_CONFIG_MISMATCH",
                "The SDK strategy metadata differs from central registration.",
                status_code=409,
            )
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        if params is not None:
            params = {key: value for key, value in params.items() if value is not None}
        try:
            response = await self._http.request(method, path, params=params, json=json)
        except httpx.RequestError as exc:
            raise RouterError(
                "DAEMON_UNAVAILABLE",
                "Trade Router daemon is unavailable.",
                retryable=True,
                status_code=503,
            ) from exc
        if response.status_code >= 400:
            try:
                body = ErrorBody.model_validate(response.json())
                raise RouterError(
                    body.code,
                    body.message,
                    retryable=body.retryable,
                    status_code=response.status_code,
                    provider=body.provider,
                    provider_code=body.provider_code,
                    correlation_id=body.correlation_id,
                    details=body.details,
                )
            except RouterError:
                raise
            except Exception as exc:
                raise RouterError(
                    "DAEMON_HTTP_ERROR",
                    f"Trade Router returned HTTP {response.status_code}.",
                    status_code=response.status_code,
                ) from exc
        if response.status_code == 204:
            return None
        return response.json()
