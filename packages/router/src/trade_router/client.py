from __future__ import annotations

import asyncio
import os
import threading
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from .errors import RouterError, RouterErrorCode
from .models import (
    AccountOrder,
    AssetClass,
    Balance,
    Bar,
    BuyingPower,
    ConditionDefinition,
    ConditionMatch,
    Fill,
    InstrumentId,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderRequest,
    OrderRoute,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    RankingEntry,
    RankingKind,
    Venue,
)
from .scheduler import ScheduleSpec
from .strategy import StrategyConfig


class AsyncTradeRouter:
    """Strategy-facing client for the local Trade Router Runtime."""

    def __init__(
        self,
        *,
        strategy: StrategyConfig,
        base_url: str | None = None,
        session_token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.strategy = strategy
        self._base_url = (
            base_url or os.environ.get("TRADE_ROUTER_URL") or "http://127.0.0.1:8890"
        ).rstrip("/")
        token = session_token or os.environ.get("TRADE_ROUTER_SESSION_TOKEN", "")
        headers = {
            "Authorization": f"Bearer {token}",
            # HTTP header values must be ASCII. Strategy identifiers are user
            # supplied and may contain Korean or other Unicode characters, so
            # keep the diagnostic header reversible without preventing the
            # strategy process from starting.
            "X-Trade-Router-Strategy": quote(strategy.id, safe=""),
        }
        self._headers = headers
        self._http = http_client or httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            # Scheduler event polling waits for up to 25 seconds. Keep the read
            # timeout above that long poll while retaining short connection and
            # write timeouts for ordinary broker operations.
            timeout=httpx.Timeout(15.0, connect=5.0, read=35.0),
        )
        self._owns_http = http_client is None
        self._stream_subscription_ids: set[str] = set()
        self.market = MarketClient(self)
        self.discovery = DiscoveryClient(self)
        self.account = AccountClient(self)
        self.orders = OrderClient(self)
        self.runtime = RuntimeClient(self)

    async def __aenter__(self) -> "AsyncTradeRouter":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        for subscription_id in tuple(self._stream_subscription_ids):
            try:
                await self._delete(f"/api/v1/market/subscriptions/{subscription_id}")
            except RouterError:
                pass
        self._stream_subscription_ids.clear()
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._http.get(path, params=params, headers=self._headers)
        except httpx.HTTPError as exc:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "로컬 Trade Router에 연결하지 못했습니다.",
            ) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(detail, dict):
                code_text = detail.get("code", RouterErrorCode.INVALID_REQUEST.value)
                message = str(detail.get("message") or "Trade Router 요청이 실패했습니다.")
                details = detail.get("details")
            else:
                code_text = RouterErrorCode.INVALID_REQUEST.value
                message = str(detail or "Trade Router 요청이 실패했습니다.")
                details = None
            try:
                code = RouterErrorCode(code_text)
            except ValueError:
                code = RouterErrorCode.INVALID_REQUEST
            raise RouterError(code, message, details=details)
        if not isinstance(payload, dict):
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "Trade Router 응답 형식이 올바르지 않습니다.",
            )
        return payload

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(path, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "로컬 Trade Router에 연결하지 못했습니다.",
            ) from exc
        try:
            result = response.json()
        except ValueError:
            result = {}
        if response.status_code >= 400:
            detail = result.get("detail") if isinstance(result, dict) else None
            if isinstance(detail, dict):
                code_text = detail.get("code", RouterErrorCode.INVALID_REQUEST.value)
                message = str(detail.get("message") or "Trade Router 요청이 실패했습니다.")
                details = detail.get("details")
            else:
                code_text = RouterErrorCode.INVALID_REQUEST.value
                message = str(detail or "Trade Router 요청이 실패했습니다.")
                details = None
            try:
                code = RouterErrorCode(code_text)
            except ValueError:
                code = RouterErrorCode.INVALID_REQUEST
            raise RouterError(code, message, details=details)
        return result if isinstance(result, dict) else {}

    async def _delete(self, path: str) -> dict[str, Any]:
        try:
            response = await self._http.delete(path, headers=self._headers)
        except httpx.HTTPError as exc:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "로컬 Trade Router에 연결하지 못했습니다.",
            ) from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            if isinstance(detail, dict):
                try:
                    code = RouterErrorCode(detail.get("code"))
                except ValueError:
                    code = RouterErrorCode.INVALID_REQUEST
                raise RouterError(code, str(detail.get("message") or "요청이 실패했습니다."))
            raise RouterError(RouterErrorCode.INVALID_REQUEST, str(detail or "요청이 실패했습니다."))
        if response.status_code == 204 or not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}


class TradeRouterClient:
    """Synchronous facade backed by one private event-loop thread.

    Managed ``Strategy`` classes should use the async services injected through
    ``ctx``. This wrapper is for small standalone scripts and notebooks.
    """

    def __init__(
        self,
        *,
        strategy: StrategyConfig,
        base_url: str | None = None,
        session_token: str | None = None,
    ) -> None:
        self.strategy = strategy
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"trade-router-{strategy.id}",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

        async def create() -> AsyncTradeRouter:
            return AsyncTradeRouter(
                strategy=strategy,
                base_url=base_url,
                session_token=session_token,
            )

        self._async = self._call(create())
        self.market = SyncMarketClient(self)
        self.discovery = SyncDiscoveryClient(self)
        self.account = SyncAccountClient(self)
        self.orders = SyncOrderClient(self)
        self.runtime = SyncRuntimeClient(self)
        self._closed = False

    def __enter__(self) -> "TradeRouterClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._call(self._async.close())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _call(self, awaitable: Any) -> Any:
        if not self._thread.is_alive():
            raise RuntimeError("Trade Router 동기 클라이언트가 종료되었습니다.")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        return future.result()


SyncTradeRouter = TradeRouterClient


class SyncMarketClient:
    def __init__(self, router: TradeRouterClient) -> None:
        self._router = router

    def source(self, alias: str) -> "SyncMarketSourceClient":
        return SyncMarketSourceClient(self._router, alias)

    def quote(self, code: str, venue: Venue | str = Venue.INTEGRATED) -> Quote:
        return self._router._call(self._router._async.market.quote(code, venue))

    def bars(
        self,
        code: str,
        interval: str,
        *,
        count: int = 100,
        venue: Venue | str = Venue.INTEGRATED,
        complete_only: bool = True,
        end: datetime | None = None,
    ) -> tuple[Bar, ...]:
        return self._router._call(
            self._router._async.market.bars(
                code,
                interval,
                count=count,
                venue=venue,
                complete_only=complete_only,
                end=end,
            )
        )


class SyncMarketSourceClient:
    def __init__(self, router: TradeRouterClient, alias: str) -> None:
        self._router = router
        self._alias = alias

    def quote(self, code: str, venue: Venue | str = Venue.INTEGRATED) -> Quote:
        return self._router._call(
            self._router._async.market.source(self._alias).quote(code, venue)
        )

    def bars(
        self,
        code: str,
        interval: str,
        *,
        count: int = 100,
        venue: Venue | str = Venue.INTEGRATED,
        complete_only: bool = True,
        end: datetime | None = None,
    ) -> tuple[Bar, ...]:
        return self._router._call(
            self._router._async.market.source(self._alias).bars(
                code,
                interval,
                count=count,
                venue=venue,
                complete_only=complete_only,
                end=end,
            )
        )


class SyncDiscoveryClient:
    def __init__(self, router: TradeRouterClient) -> None:
        self._router = router

    def source(self, alias: str) -> "SyncDiscoverySourceClient":
        return SyncDiscoverySourceClient(self._router, alias)

    def conditions(self, user_id: str) -> tuple[ConditionDefinition, ...]:
        return self._router._call(self._router._async.discovery.conditions(user_id))

    def matches(self, user_id: str, condition_id: str) -> tuple[ConditionMatch, ...]:
        return self._router._call(
            self._router._async.discovery.matches(user_id, condition_id)
        )

    def ranking(
        self,
        kind: RankingKind | str,
        *,
        venue: Venue | str = Venue.KRX,
        market: str = "ALL",
        limit: int = 20,
        min_price: Decimal | None = None,
        max_price: Decimal | None = None,
        min_volume: Decimal | None = None,
    ) -> tuple[RankingEntry, ...]:
        return self._router._call(
            self._router._async.discovery.ranking(
                kind,
                venue=venue,
                market=market,
                limit=limit,
                min_price=min_price,
                max_price=max_price,
                min_volume=min_volume,
            )
        )


class SyncDiscoverySourceClient:
    def __init__(self, router: TradeRouterClient, alias: str) -> None:
        self._router = router
        self._alias = alias

    def conditions(self, user_id: str) -> tuple[ConditionDefinition, ...]:
        return self._router._call(
            self._router._async.discovery.source(self._alias).conditions(user_id)
        )

    def matches(self, user_id: str, condition_id: str) -> tuple[ConditionMatch, ...]:
        return self._router._call(
            self._router._async.discovery.source(self._alias).matches(
                user_id, condition_id
            )
        )

    def ranking(
        self,
        kind: RankingKind | str,
        *,
        venue: Venue | str = Venue.KRX,
        market: str = "ALL",
        limit: int = 20,
        min_price: Decimal | None = None,
        max_price: Decimal | None = None,
        min_volume: Decimal | None = None,
    ) -> tuple[RankingEntry, ...]:
        return self._router._call(
            self._router._async.discovery.source(self._alias).ranking(
                kind,
                venue=venue,
                market=market,
                limit=limit,
                min_price=min_price,
                max_price=max_price,
                min_volume=min_volume,
            )
        )


class SyncAccountClient:
    def __init__(self, router: TradeRouterClient) -> None:
        self._router = router

    def balance(self) -> Balance:
        return self._router._call(self._router._async.account.balance())

    def positions(self) -> tuple[Position, ...]:
        return self._router._call(self._router._async.account.positions())

    def position(self, code: str) -> Position | None:
        return self._router._call(self._router._async.account.position(code))

    def orders(
        self,
        *,
        state: str = "all",
        business_date: date | None = None,
    ) -> tuple[AccountOrder, ...]:
        return self._router._call(
            self._router._async.account.orders(
                state=state,
                business_date=business_date,
            )
        )

    def open_orders(self) -> tuple[AccountOrder, ...]:
        return self._router._call(self._router._async.account.open_orders())

    def filled_orders(self) -> tuple[AccountOrder, ...]:
        return self._router._call(self._router._async.account.filled_orders())

    def buying_power(
        self,
        code: str,
        *,
        order_type: OrderType | str = OrderType.MARKET,
        price: Decimal | None = None,
        route: OrderRoute | str = OrderRoute.AUTO,
    ) -> BuyingPower:
        return self._router._call(
            self._router._async.account.buying_power(
                code,
                order_type=order_type,
                price=price,
                route=route,
            )
        )


class SyncOrderClient:
    def __init__(self, router: TradeRouterClient) -> None:
        self._router = router

    def market_buy(self, *args: object, **kwargs: object) -> Any:
        return self._router._call(
            self._router._async.orders.market_buy(*args, **kwargs)
        )

    def market_sell(self, *args: object, **kwargs: object) -> Any:
        return self._router._call(
            self._router._async.orders.market_sell(*args, **kwargs)
        )

    def best_limit_buy(self, *args: object, **kwargs: object) -> Any:
        return self._router._call(
            self._router._async.orders.best_limit_buy(*args, **kwargs)
        )

    def best_limit_sell(self, *args: object, **kwargs: object) -> Any:
        return self._router._call(
            self._router._async.orders.best_limit_sell(*args, **kwargs)
        )

    def limit_buy(self, *args: object, **kwargs: object) -> Any:
        return self._router._call(
            self._router._async.orders.limit_buy(*args, **kwargs)
        )

    def limit_sell(self, *args: object, **kwargs: object) -> Any:
        return self._router._call(
            self._router._async.orders.limit_sell(*args, **kwargs)
        )

    def amend(self, *args: object, **kwargs: object) -> Any:
        return self._router._call(self._router._async.orders.amend(*args, **kwargs))

    def open_orders(self) -> list[Order]:
        return self._router._call(self._router._async.orders.open_orders())

    def cancel(self, *args: object, **kwargs: object) -> Any:
        return self._router._call(self._router._async.orders.cancel(*args, **kwargs))


class SyncRuntimeClient:
    def __init__(self, router: TradeRouterClient) -> None:
        self._router = router

    def register_manifest(self) -> None:
        self._router._call(self._router._async.runtime.register_manifest())

    def register_schedules(self, schedules: tuple[ScheduleSpec, ...]) -> None:
        self._router._call(self._router._async.runtime.register_schedules(schedules))

    def next_event(self, timeout: float = 25.0) -> dict[str, Any] | None:
        return self._router._call(self._router._async.runtime.next_event(timeout))


class MarketClient:
    def __init__(self, router: AsyncTradeRouter) -> None:
        self._router = router

    def source(self, alias: str) -> "MarketSourceClient":
        connection = self._router.strategy.market_sources.get(alias)
        if connection is None:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                f"전략에 허용된 데이터 연결 별칭이 아닙니다: {alias}",
            )
        return MarketSourceClient(self._router, alias, connection)

    async def quote(
        self,
        code: str,
        venue: Venue | str = Venue.INTEGRATED,
    ) -> Quote:
        sources = self._router.strategy.market_sources
        if "primary" in sources:
            return await self.source("primary").quote(code, venue)
        if len(sources) == 1:
            return await self.source(next(iter(sources))).quote(code, venue)
        raise RouterError(
            RouterErrorCode.INVALID_REQUEST,
            "사용할 데이터 연결 별칭을 source()로 선택하세요.",
        )

    async def bars(
        self,
        code: str,
        interval: str,
        *,
        count: int = 100,
        venue: Venue | str = Venue.INTEGRATED,
        complete_only: bool = True,
        end: datetime | None = None,
    ) -> tuple[Bar, ...]:
        sources = self._router.strategy.market_sources
        if "primary" in sources:
            return await self.source("primary").bars(
                code,
                interval,
                count=count,
                venue=venue,
                complete_only=complete_only,
                end=end,
            )
        if len(sources) == 1:
            return await self.source(next(iter(sources))).bars(
                code,
                interval,
                count=count,
                venue=venue,
                complete_only=complete_only,
                end=end,
            )
        raise RouterError(
            RouterErrorCode.INVALID_REQUEST,
            "사용할 데이터 연결 별칭을 source()로 선택하세요.",
        )

    async def subscribe_quotes(
        self,
        codes: str | list[str] | tuple[str, ...],
        *,
        venue: Venue | str = Venue.INTEGRATED,
    ) -> tuple[dict[str, Any], ...]:
        source = "primary" if "primary" in self._router.strategy.market_sources else next(iter(self._router.strategy.market_sources), "")
        if not source:
            raise RouterError(RouterErrorCode.INVALID_REQUEST, "등록된 시세 연결이 없습니다.")
        return await self.source(source).subscribe_quotes(codes, venue=venue)

    async def subscribe_orderbooks(
        self,
        codes: str | list[str] | tuple[str, ...],
        *,
        venue: Venue | str = Venue.INTEGRATED,
    ) -> tuple[dict[str, Any], ...]:
        source = "primary" if "primary" in self._router.strategy.market_sources else next(iter(self._router.strategy.market_sources), "")
        if not source:
            raise RouterError(RouterErrorCode.INVALID_REQUEST, "등록된 시세 연결이 없습니다.")
        return await self.source(source).subscribe_orderbooks(codes, venue=venue)

    async def unsubscribe(self, subscription_id: str) -> None:
        await self._router._delete(
            f"/api/v1/market/subscriptions/{subscription_id}"
        )
        self._router._stream_subscription_ids.discard(subscription_id)


class MarketSourceClient:
    def __init__(
        self,
        router: AsyncTradeRouter,
        alias: str,
        connection: str,
    ) -> None:
        self._router = router
        self._alias = alias
        self._connection = connection

    async def quote(
        self,
        code: str,
        venue: Venue | str = Venue.INTEGRATED,
    ) -> Quote:
        venue_value = Venue(venue).value
        payload = await self._router._get(
            "/api/v1/market/quote",
            {
                "source": self._connection,
                "source_alias": self._alias,
                "code": code,
                "venue": venue_value,
            },
        )
        instrument = payload["instrument"]
        return Quote(
            instrument=InstrumentId(
                code=instrument["code"],
                country=instrument.get("country", "KR"),
                asset=AssetClass(instrument.get("asset", "EQUITY")),
            ),
            price=Decimal(payload["price"]),
            venue=Venue(payload["venue"]),
            provider=payload["provider"],
            source_alias=payload.get("source_alias"),
            market_time=datetime.fromisoformat(payload["market_time"]),
            received_at=datetime.fromisoformat(payload["received_at"]),
            correlation_id=payload["correlation_id"],
            delayed=bool(payload.get("delayed", False)),
            stale=bool(payload.get("stale", False)),
            connection_generation=int(payload.get("connection_generation", 0)),
        )

    async def bars(
        self,
        code: str,
        interval: str,
        *,
        count: int = 100,
        venue: Venue | str = Venue.INTEGRATED,
        complete_only: bool = True,
        end: datetime | None = None,
    ) -> tuple[Bar, ...]:
        payload = await self._router._get(
            "/api/v1/market/bars",
            {
                "source": self._connection,
                "source_alias": self._alias,
                "code": code,
                "interval": interval,
                "count": str(count),
                "venue": Venue(venue).value,
                "complete_only": str(complete_only).lower(),
                **({"end": end.isoformat()} if end is not None else {}),
            },
        )
        return tuple(_bar_from_payload(row) for row in payload.get("bars", []))

    async def subscribe_quotes(
        self,
        codes: str | list[str] | tuple[str, ...],
        *,
        venue: Venue | str = Venue.INTEGRATED,
    ) -> tuple[dict[str, Any], ...]:
        return await self._subscribe("QUOTE", codes, venue)

    async def subscribe_orderbooks(
        self,
        codes: str | list[str] | tuple[str, ...],
        *,
        venue: Venue | str = Venue.INTEGRATED,
    ) -> tuple[dict[str, Any], ...]:
        return await self._subscribe("ORDERBOOK", codes, venue)

    async def _subscribe(
        self,
        kind: str,
        codes: str | list[str] | tuple[str, ...],
        venue: Venue | str,
    ) -> tuple[dict[str, Any], ...]:
        normalized_codes = [codes] if isinstance(codes, str) else list(codes)
        result = []
        for code in normalized_codes:
            payload = await self._router._post(
                "/api/v1/market/subscriptions",
                {
                    "source": self._connection,
                    "source_alias": self._alias,
                    "kind": kind,
                    "code": str(code),
                    "venue": Venue(venue).value,
                },
            )
            subscription_id = str(payload["id"])
            self._router._stream_subscription_ids.add(subscription_id)
            result.append(payload)
        return tuple(result)


class DiscoveryClient:
    def __init__(self, router: AsyncTradeRouter) -> None:
        self._router = router

    def source(self, alias: str) -> "DiscoverySourceClient":
        connection = self._router.strategy.market_sources.get(alias)
        if connection is None:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                f"전략에 허용된 데이터 연결 별칭이 아닙니다: {alias}",
            )
        return DiscoverySourceClient(self._router, alias, connection)

    def _default(self) -> "DiscoverySourceClient":
        sources = self._router.strategy.market_sources
        if "primary" in sources:
            return self.source("primary")
        if len(sources) == 1:
            return self.source(next(iter(sources)))
        raise RouterError(
            RouterErrorCode.INVALID_REQUEST,
            "조건검색에 사용할 데이터 연결 별칭을 source()로 선택하세요.",
        )

    async def conditions(self, user_id: str) -> tuple[ConditionDefinition, ...]:
        return await self._default().conditions(user_id)

    async def matches(
        self, user_id: str, condition_id: str
    ) -> tuple[ConditionMatch, ...]:
        return await self._default().matches(user_id, condition_id)

    async def ranking(
        self,
        kind: RankingKind | str,
        *,
        venue: Venue | str = Venue.KRX,
        market: str = "ALL",
        limit: int = 20,
        min_price: Decimal | None = None,
        max_price: Decimal | None = None,
        min_volume: Decimal | None = None,
    ) -> tuple[RankingEntry, ...]:
        return await self._default().ranking(
            kind,
            venue=venue,
            market=market,
            limit=limit,
            min_price=min_price,
            max_price=max_price,
            min_volume=min_volume,
        )


class DiscoverySourceClient:
    def __init__(
        self,
        router: AsyncTradeRouter,
        alias: str,
        connection: str,
    ) -> None:
        self._router = router
        self._alias = alias
        self._connection = connection

    async def conditions(self, user_id: str) -> tuple[ConditionDefinition, ...]:
        payload = await self._router._get(
            "/api/v1/discovery/conditions",
            {
                "source": self._connection,
                "source_alias": self._alias,
                "user_id": user_id,
            },
        )
        return tuple(
            ConditionDefinition(
                id=str(row["id"]),
                name=str(row["name"]),
                provider=str(row["provider"]),
                source_alias=row.get("source_alias"),
            )
            for row in payload.get("conditions", [])
        )

    async def matches(
        self, user_id: str, condition_id: str
    ) -> tuple[ConditionMatch, ...]:
        payload = await self._router._get(
            "/api/v1/discovery/condition-matches",
            {
                "source": self._connection,
                "source_alias": self._alias,
                "user_id": user_id,
                "condition_id": condition_id,
            },
        )
        return tuple(
            ConditionMatch(
                condition_id=str(row["condition_id"]),
                instrument=InstrumentId(str(row["instrument"]["code"])),
                provider=str(row["provider"]),
                name=row.get("name"),
                price=(Decimal(str(row["price"])) if row.get("price") is not None else None),
                change_rate=(
                    Decimal(str(row["change_rate"]))
                    if row.get("change_rate") is not None
                    else None
                ),
                source_alias=row.get("source_alias"),
            )
            for row in payload.get("matches", [])
        )

    async def ranking(
        self,
        kind: RankingKind | str,
        *,
        venue: Venue | str = Venue.KRX,
        market: str = "ALL",
        limit: int = 20,
        min_price: Decimal | None = None,
        max_price: Decimal | None = None,
        min_volume: Decimal | None = None,
    ) -> tuple[RankingEntry, ...]:
        payload = await self._router._get(
            "/api/v1/discovery/ranking",
            {
                "source": self._connection,
                "source_alias": self._alias,
                "kind": RankingKind(kind).value,
                "venue": Venue(venue).value,
                "market": market,
                "limit": str(limit),
                **(
                    {"min_price": str(min_price)}
                    if min_price is not None
                    else {}
                ),
                **(
                    {"max_price": str(max_price)}
                    if max_price is not None
                    else {}
                ),
                **(
                    {"min_volume": str(min_volume)}
                    if min_volume is not None
                    else {}
                ),
            },
        )
        return tuple(
            RankingEntry(
                rank=int(row["rank"]),
                kind=RankingKind(row["kind"]),
                instrument=InstrumentId(str(row["instrument"]["code"])),
                provider=str(row["provider"]),
                name=row.get("name"),
                price=(Decimal(str(row["price"])) if row.get("price") is not None else None),
                change_rate=(
                    Decimal(str(row["change_rate"]))
                    if row.get("change_rate") is not None
                    else None
                ),
                volume=(
                    Decimal(str(row["volume"]))
                    if row.get("volume") is not None
                    else None
                ),
                trading_value=(
                    Decimal(str(row["trading_value"]))
                    if row.get("trading_value") is not None
                    else None
                ),
                source_alias=row.get("source_alias"),
            )
            for row in payload.get("entries", [])
        )


class AccountClient:
    def __init__(self, router: AsyncTradeRouter) -> None:
        self._router = router

    async def balance(self) -> Balance:
        payload = await self._router._get(
            "/api/v1/account/balance",
            {"account": self._router.strategy.trading_account},
        )
        positions = tuple(
            Position(
                instrument=InstrumentId(row["instrument"]["code"]),
                quantity=Decimal(row["quantity"]),
                available_quantity=Decimal(row["available_quantity"]),
                average_price=Decimal(row["average_price"]),
                market_price=(
                    Decimal(row["market_price"])
                    if row.get("market_price") is not None
                    else None
                ),
                strategy_quantity=(
                    Decimal(row["strategy_quantity"])
                    if row.get("strategy_quantity") is not None
                    else None
                ),
                name=row.get("name"),
            )
            for row in payload.get("positions", [])
        )
        return Balance(
            cash=Decimal(payload["cash"]),
            withdrawable_cash=Decimal(payload["withdrawable_cash"]),
            total_value=Decimal(payload["total_value"]),
            positions=positions,
            as_of=(
                datetime.fromisoformat(payload["as_of"])
                if payload.get("as_of")
                else None
            ),
        )

    async def positions(self) -> tuple[Position, ...]:
        return (await self.balance()).positions

    async def position(self, code: str) -> Position | None:
        target = InstrumentId(code)
        return next(
            (
                position
                for position in await self.positions()
                if position.instrument == target
            ),
            None,
        )

    async def orders(
        self,
        *,
        state: str = "all",
        business_date: date | None = None,
    ) -> tuple[AccountOrder, ...]:
        payload = await self._router._get(
            "/api/v1/account/orders",
            {
                "account": self._router.strategy.trading_account,
                "state": state,
                **(
                    {"business_date": business_date.isoformat()}
                    if business_date is not None
                    else {}
                ),
            },
        )
        return tuple(_account_order_from_payload(row) for row in payload.get("orders", []))

    async def open_orders(self) -> tuple[AccountOrder, ...]:
        return await self.orders(state="open")

    async def filled_orders(self) -> tuple[AccountOrder, ...]:
        return await self.orders(state="filled")

    async def buying_power(
        self,
        code: str,
        *,
        order_type: OrderType | str = OrderType.MARKET,
        price: Decimal | None = None,
        route: OrderRoute | str = OrderRoute.AUTO,
    ) -> BuyingPower:
        normalized_type = OrderType(order_type)
        normalized_route = OrderRoute(route)
        payload = await self._router._get(
            "/api/v1/account/buying-power",
            {
                "account": self._router.strategy.trading_account,
                "code": code,
                "order_type": normalized_type.value,
                "route": normalized_route.value,
                **({"price": str(price)} if price is not None else {}),
            },
        )
        instrument = payload["instrument"]
        return BuyingPower(
            instrument=InstrumentId(
                code=instrument["code"],
                country=instrument.get("country", "KR"),
            ),
            account_alias=payload["account_alias"],
            order_type=OrderType(payload["order_type"]),
            requested_price=Decimal(payload["requested_price"]),
            calculation_price=Decimal(payload["calculation_price"]),
            cash_available=Decimal(payload["cash_available"]),
            maximum_quantity=Decimal(payload["maximum_quantity"]),
            provider=payload["provider"],
            as_of=datetime.fromisoformat(payload["as_of"]),
            route=OrderRoute(payload.get("route", normalized_route.value)),
        )


class OrderClient:
    def __init__(self, router: AsyncTradeRouter) -> None:
        self._router = router

    async def open_orders(self) -> list[Order]:
        """Reconcile and return actionable orders owned by this strategy/account."""
        payload = await self._router._get("/api/v1/orders/open", {})
        return [_order_from_payload(item) for item in payload.get("orders", [])]

    async def market_buy(
        self,
        code: str,
        quantity: Decimal | int | str,
        *,
        client_order_id: str,
        route: OrderRoute | str = OrderRoute.AUTO,
        provider_options: dict[str, Any] | None = None,
    ) -> Order:
        return await self._submit(
            code,
            quantity,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            client_order_id=client_order_id,
            route=route,
            provider_options=provider_options,
        )

    async def market_sell(
        self,
        code: str,
        quantity: Decimal | int | str,
        *,
        client_order_id: str,
        route: OrderRoute | str = OrderRoute.AUTO,
        provider_options: dict[str, Any] | None = None,
    ) -> Order:
        return await self._submit(
            code,
            quantity,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            client_order_id=client_order_id,
            route=route,
            provider_options=provider_options,
        )

    async def best_limit_buy(
        self,
        code: str,
        quantity: Decimal | int | str,
        *,
        client_order_id: str,
        route: OrderRoute | str = OrderRoute.AUTO,
        provider_options: dict[str, Any] | None = None,
    ) -> Order:
        """Buy at the best ask on receipt; any remainder rests at that price."""
        return await self._submit(
            code,
            quantity,
            side=OrderSide.BUY,
            order_type=OrderType.BEST_LIMIT,
            client_order_id=client_order_id,
            route=route,
            provider_options=provider_options,
        )

    async def best_limit_sell(
        self,
        code: str,
        quantity: Decimal | int | str,
        *,
        client_order_id: str,
        route: OrderRoute | str = OrderRoute.AUTO,
        provider_options: dict[str, Any] | None = None,
    ) -> Order:
        """Sell at the best bid on receipt; any remainder rests at that price."""
        return await self._submit(
            code,
            quantity,
            side=OrderSide.SELL,
            order_type=OrderType.BEST_LIMIT,
            client_order_id=client_order_id,
            route=route,
            provider_options=provider_options,
        )

    async def limit_buy(
        self,
        code: str,
        quantity: Decimal | int | str,
        price: Decimal | int | str,
        *,
        client_order_id: str,
        route: OrderRoute | str = OrderRoute.AUTO,
        provider_options: dict[str, Any] | None = None,
    ) -> Order:
        return await self._submit(
            code,
            quantity,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(price)),
            client_order_id=client_order_id,
            route=route,
            provider_options=provider_options,
        )

    async def limit_sell(
        self,
        code: str,
        quantity: Decimal | int | str,
        price: Decimal | int | str,
        *,
        client_order_id: str,
        route: OrderRoute | str = OrderRoute.AUTO,
        provider_options: dict[str, Any] | None = None,
    ) -> Order:
        return await self._submit(
            code,
            quantity,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            price=Decimal(str(price)),
            client_order_id=client_order_id,
            route=route,
            provider_options=provider_options,
        )

    async def _submit(
        self,
        code: str,
        quantity: Decimal | int | str,
        *,
        side: OrderSide,
        order_type: OrderType,
        client_order_id: str,
        route: OrderRoute | str,
        price: Decimal | None = None,
        provider_options: dict[str, Any] | None = None,
    ) -> Order:
        payload = await self._router._post(
            "/api/v1/orders",
            {
                "code": code,
                "side": side.value,
                "order_type": order_type.value,
                "quantity": str(quantity),
                "price": str(price) if price is not None else None,
                "client_order_id": client_order_id,
                "route": OrderRoute(route).value,
                "provider_options": provider_options or {},
            },
        )
        return _order_from_payload(payload)

    async def amend(
        self,
        order_id: str,
        *,
        quantity: Decimal | int | str | None = None,
        price: Decimal | int | str | None = None,
        order_type: OrderType | str = OrderType.LIMIT,
        route: OrderRoute | str | None = None,
    ) -> Order:
        payload = await self._router._post(
            f"/api/v1/orders/{order_id}/amend",
            {
                "quantity": str(quantity) if quantity is not None else None,
                "price": str(price) if price is not None else None,
                "order_type": OrderType(order_type).value,
                "route": OrderRoute(route).value if route is not None else None,
            },
        )
        return _order_from_payload(payload)

    async def cancel(
        self,
        order_id: str,
        *,
        quantity: Decimal | int | str | None = None,
        route: OrderRoute | str | None = None,
    ) -> Order:
        payload = await self._router._post(
            f"/api/v1/orders/{order_id}/cancel",
            {
                "quantity": str(quantity) if quantity is not None else None,
                "route": OrderRoute(route).value if route is not None else None,
            },
        )
        return _order_from_payload(payload)


class RuntimeClient:
    def __init__(self, router: AsyncTradeRouter) -> None:
        self._router = router

    async def register_manifest(self) -> None:
        config = self._router.strategy
        await self._router._post(
            "/api/v1/strategy/manifest",
            {
                "strategy_id": config.id,
                "trading_account": config.trading_account,
                "market_sources": config.market_sources,
                "required_capabilities": {
                    alias: list(capabilities)
                    for alias, capabilities in config.required_capabilities.items()
                },
                "risk_limits": {
                    "max_order_amount": (
                        str(config.limits.max_order_amount)
                        if config.limits.max_order_amount is not None
                        else None
                    ),
                    "max_daily_buy_amount": (
                        str(config.limits.max_daily_buy_amount)
                        if config.limits.max_daily_buy_amount is not None
                        else None
                    ),
                    "max_daily_buy_count": config.limits.max_daily_buy_count,
                    "max_exposure": (
                        str(config.limits.max_exposure)
                        if config.limits.max_exposure is not None
                        else None
                    ),
                    "max_open_orders": config.limits.max_open_orders,
                },
            },
        )

    async def register_schedules(self, schedules: tuple[ScheduleSpec, ...]) -> None:
        await self._router._post(
            "/api/v1/strategy/schedules",
            {
                "schedules": [_schedule_payload(schedule) for schedule in schedules],
            },
        )

    async def next_event(self, timeout: float = 25.0) -> dict[str, Any] | None:
        payload = await self._router._get(
            "/api/v1/strategy/events/next",
            {"timeout": str(timeout)},
        )
        event = payload.get("event")
        return event if isinstance(event, dict) else None


def _schedule_payload(schedule: ScheduleSpec) -> dict[str, Any]:
    between = None
    if schedule.window is not None:
        between = [
            schedule.window.start.isoformat(timespec="seconds"),
            schedule.window.end.isoformat(timespec="seconds"),
        ]
    return {
        "id": schedule.id,
        "kind": schedule.kind.value,
        "interval_seconds": schedule.interval_seconds,
        "bar_interval": schedule.bar_interval,
        "at": schedule.at.isoformat(timespec="seconds") if schedule.at else None,
        "between": between,
        "aligned": schedule.aligned,
        "settle_seconds": schedule.settle_seconds,
        "trading_days_only": schedule.trading_days_only,
    }


def _bar_from_payload(payload: dict[str, Any]) -> Bar:
    instrument = payload["instrument"]
    received_at = payload.get("received_at")
    return Bar(
        instrument=InstrumentId(
            code=instrument["code"],
            country=instrument.get("country", "KR"),
        ),
        interval=payload["interval"],
        open_time=datetime.fromisoformat(payload["open_time"]),
        close_time=datetime.fromisoformat(payload["close_time"]),
        open=Decimal(payload["open"]),
        high=Decimal(payload["high"]),
        low=Decimal(payload["low"]),
        close=Decimal(payload["close"]),
        volume=Decimal(payload["volume"]),
        venue=Venue(payload["venue"]),
        provider=payload["provider"],
        correlation_id=payload["correlation_id"],
        complete=bool(payload.get("complete", True)),
        source_alias=payload.get("source_alias"),
        received_at=(datetime.fromisoformat(received_at) if received_at else None),
        connection_generation=int(payload.get("connection_generation", 0)),
        stale=bool(payload.get("stale", False)),
    )


def _account_order_from_payload(payload: dict[str, Any]) -> AccountOrder:
    instrument = payload["instrument"]
    return AccountOrder(
        provider_order_id=payload["provider_order_id"],
        account_alias=payload["account_alias"],
        instrument=InstrumentId(
            code=instrument["code"],
            country=instrument.get("country", "KR"),
        ),
        side=OrderSide(payload["side"]),
        order_type=OrderType(payload["order_type"]),
        status=OrderStatus(payload["status"]),
        requested_quantity=Decimal(payload["requested_quantity"]),
        filled_quantity=Decimal(payload["filled_quantity"]),
        remaining_quantity=Decimal(payload["remaining_quantity"]),
        price=(Decimal(payload["price"]) if payload.get("price") is not None else None),
        average_fill_price=(
            Decimal(payload["average_fill_price"])
            if payload.get("average_fill_price") is not None
            else None
        ),
        route=OrderRoute(payload.get("route", "AUTO")),
        ordered_at=datetime.fromisoformat(payload["ordered_at"]),
        provider=payload["provider"],
    )


def _order_from_payload(payload: dict[str, Any]) -> Order:
    request_payload = payload["request"]
    instrument = request_payload["instrument"]
    return Order(
        id=payload["id"],
        client_order_id=payload["client_order_id"],
        account_alias=payload["account_alias"],
        request=OrderRequest(
            instrument=InstrumentId(
                code=instrument["code"],
                country=instrument.get("country", "KR"),
                asset=AssetClass(instrument.get("asset", "EQUITY")),
            ),
            side=OrderSide(request_payload["side"]),
            order_type=OrderType(request_payload["order_type"]),
            quantity=Decimal(request_payload["quantity"]),
            client_order_id=payload["client_order_id"],
            route=OrderRoute(request_payload.get("route", "AUTO")),
            price=(
                Decimal(request_payload["price"])
                if request_payload.get("price") is not None
                else None
            ),
            provider_options=dict(request_payload.get("provider_options") or {}),
        ),
        status=OrderStatus(payload["status"]),
        submitted_at=datetime.fromisoformat(payload["submitted_at"]),
        provider_order_id=payload.get("provider_order_id"),
        filled_quantity=Decimal(payload.get("filled_quantity", "0")),
        average_fill_price=(
            Decimal(payload["average_fill_price"])
            if payload.get("average_fill_price") is not None
            else None
        ),
        commission=Decimal(payload.get("commission", "0")),
        tax=Decimal(payload.get("tax", "0")),
        costs_complete=bool(payload.get("costs_complete", False)),
        message=payload.get("message"),
    )


def quote_from_event(payload: dict[str, Any]) -> Quote:
    instrument = payload["instrument"]
    return Quote(
        instrument=InstrumentId(
            code=instrument["code"],
            country=instrument.get("country", "KR"),
            asset=AssetClass(instrument.get("asset", "EQUITY")),
        ),
        price=Decimal(payload["price"]),
        venue=Venue(payload["venue"]),
        provider=payload["provider"],
        market_time=datetime.fromisoformat(payload["market_time"]),
        received_at=datetime.fromisoformat(payload["received_at"]),
        correlation_id=payload["correlation_id"],
        delayed=bool(payload.get("delayed", False)),
        source_alias=payload.get("source_alias"),
        connection_generation=int(payload.get("connection_generation", 0)),
        stale=bool(payload.get("stale", False)),
    )


def orderbook_from_event(payload: dict[str, Any]) -> OrderBook:
    instrument = payload["instrument"]
    return OrderBook(
        instrument=InstrumentId(
            code=instrument["code"],
            country=instrument.get("country", "KR"),
            asset=AssetClass(instrument.get("asset", "EQUITY")),
        ),
        asks=tuple(OrderBookLevel(Decimal(row["price"]), Decimal(row["quantity"])) for row in payload.get("asks", [])),
        bids=tuple(OrderBookLevel(Decimal(row["price"]), Decimal(row["quantity"])) for row in payload.get("bids", [])),
        venue=Venue(payload["venue"]),
        provider=payload["provider"],
        market_time=datetime.fromisoformat(payload["market_time"]),
        received_at=datetime.fromisoformat(payload["received_at"]),
        correlation_id=payload["correlation_id"],
        source_alias=payload.get("source_alias"),
        connection_generation=int(payload.get("connection_generation", 0)),
        stale=bool(payload.get("stale", False)),
    )


def fill_from_event(payload: dict[str, Any]) -> Fill:
    return Fill(
        id=payload["id"],
        order_id=payload["order_id"],
        quantity=Decimal(payload["quantity"]),
        price=Decimal(payload["price"]),
        filled_at=datetime.fromisoformat(payload["filled_at"]),
        commission=Decimal(payload.get("commission", "0")),
        tax=Decimal(payload.get("tax", "0")),
        costs_complete=bool(payload.get("costs_complete", False)),
    )
