from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from trade_router import (
    AccessToken,
    AsyncTradeRouter,
    BrokerCapabilities,
    BrokerRequestIntent,
    ConnectionTarget,
    ExtensionCapability,
    InMemoryTokenStore,
    RateLimitManager,
    RateLimitPolicy,
    RateLimitScope,
    RequestManager,
    RequestPriority,
    OrderRoute,
    RankingKind,
    SchedulerPlan,
    TimeWindow,
    TradeRouterClient,
    StrategyConfig,
    SubscriptionQuotaManager,
    TokenManager,
    TokenScopeKey,
    WebSocketLimitPolicy,
)
from trade_router.errors import RouterError, RouterErrorCode


SEOUL = ZoneInfo("Asia/Seoul")


def test_token_manager_issues_once_for_concurrent_strategies() -> None:
    async def run() -> None:
        now = datetime(2026, 10, 15, 9, 0, tzinfo=SEOUL)
        scope = TokenScopeKey("kis", "kis-paper", ConnectionTarget.PAPER)
        manager = TokenManager(InMemoryTokenStore(), clock=lambda: now)
        calls = 0

        async def issue(_: TokenScopeKey) -> AccessToken:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return AccessToken("secret-token", now + timedelta(hours=24), issued_at=now)

        tokens = await asyncio.gather(*(manager.get(scope, issue) for _ in range(8)))
        assert calls == 1
        assert all(token.value == "secret-token" for token in tokens)
        status = await manager.status(scope)
        assert status["usable"] is True

    asyncio.run(run())


def test_request_manager_deduplicates_and_caches_reads() -> None:
    async def run() -> None:
        limits = RateLimitManager()
        limits.configure("kis:market", RateLimitPolicy(10, 10))
        manager = RequestManager(limits, worker_count=2)
        release = asyncio.Event()
        calls = 0

        async def operation() -> dict[str, int]:
            nonlocal calls
            calls += 1
            await release.wait()
            return {"price": 100}

        intent = BrokerRequestIntent(
            connection_id="kis-paper",
            operation="market.quote",
            priority=RequestPriority.STRATEGY_DATA,
            rate_limit_scopes=(RateLimitScope("kis:market"),),
            dedupe_key="kis-paper:quote:005930:KRX",
            cache_ttl=5,
        )
        first = asyncio.create_task(manager.execute(intent, operation))
        await asyncio.sleep(0)
        second = asyncio.create_task(manager.execute(intent, operation))
        await asyncio.sleep(0)
        release.set()

        assert await first == {"price": 100}
        assert await second == {"price": 100}
        assert await manager.execute(intent, operation) == {"price": 100}
        assert calls == 1
        snapshot = await manager.snapshot()
        assert snapshot["deduplicated"] == 1
        assert snapshot["cache_hits"] == 1
        await manager.close()

    asyncio.run(run())


def test_request_manager_prunes_expired_cache_for_long_running_processes() -> None:
    async def run() -> None:
        now = 0.0
        limits = RateLimitManager(clock=lambda: now)
        limits.configure("market", RateLimitPolicy(10, 10))
        manager = RequestManager(limits, clock=lambda: now)

        async def operation() -> int:
            return 1

        def intent(key: str) -> BrokerRequestIntent:
            return BrokerRequestIntent(
                connection_id="connection",
                operation="market.quote",
                priority=RequestPriority.STRATEGY_DATA,
                rate_limit_scopes=(RateLimitScope("market"),),
                dedupe_key=key,
                cache_ttl=1,
            )

        await manager.execute(intent("expired"), operation)
        assert (await manager.snapshot())["cached"] == 1
        now = 61.0
        await manager.execute(intent("current"), operation)
        assert (await manager.snapshot())["cached"] == 1
        await manager.close()

    asyncio.run(run())


def test_rate_limit_manager_consumes_all_hierarchical_scopes() -> None:
    async def run() -> None:
        manager = RateLimitManager()
        manager.configure("connection", RateLimitPolicy(5, 1))
        manager.configure("market", RateLimitPolicy(2, 1))
        await manager.acquire(
            (RateLimitScope("connection"), RateLimitScope("market"))
        )
        snapshot = {item["key"]: item for item in await manager.snapshot()}
        assert snapshot["connection"]["available"] < 5
        assert snapshot["market"]["available"] < 2

    asyncio.run(run())


def test_websocket_quota_deduplicates_and_enforces_session_capacity() -> None:
    async def run() -> None:
        manager = SubscriptionQuotaManager()
        manager.configure(
            "kis:paper:key-test",
            WebSocketLimitPolicy(
                max_sessions=1,
                max_subscriptions_per_session=2,
                control_messages_per_second=10_000,
            ),
        )

        first = await manager.subscribe(
            "kis:paper:key-test",
            consumer_id="strategy-a",
            subscription_key="quote:005930",
        )
        shared = await manager.subscribe(
            "kis:paper:key-test",
            consumer_id="strategy-b",
            subscription_key="quote:005930",
        )
        second = await manager.subscribe(
            "kis:paper:key-test",
            consumer_id="studio",
            subscription_key="orderbook:005930",
        )

        assert first.created is True
        assert shared.created is False
        assert shared.session_id == first.session_id == second.session_id
        try:
            await manager.subscribe(
                "kis:paper:key-test",
                consumer_id="strategy-c",
                subscription_key="quote:000660",
            )
        except RouterError as exc:
            assert exc.code is RouterErrorCode.RATE_LIMITED
        else:
            raise AssertionError("WebSocket subscription ceiling was not enforced.")

        assert await manager.unsubscribe(first, consumer_id="strategy-a") is False
        assert await manager.unsubscribe(shared, consumer_id="strategy-b") is True
        snapshot = (await manager.snapshot())[0]
        assert snapshot["sessions"] == 1
        assert snapshot["subscriptions"] == 1

    asyncio.run(run())


def test_scheduler_uses_half_open_windows_and_clock_alignment() -> None:
    plan = SchedulerPlan()
    morning = plan.every(
        seconds=30,
        schedule_id="morning",
        between=("09:00", "10:00"),
        aligned=True,
    )
    assert morning.window == TimeWindow.parse("09:00", "10:00")
    assert morning.window.contains(datetime(2026, 10, 15, 9, 59, 59, tzinfo=SEOUL))
    assert not morning.window.contains(datetime(2026, 10, 15, 10, 0, tzinfo=SEOUL))
    after = datetime(2026, 10, 15, 9, 3, 17, tzinfo=SEOUL)
    assert plan.next_interval(morning, after) == datetime(
        2026, 10, 15, 9, 3, 30, tzinfo=SEOUL
    )


def test_provider_extension_capability_checks_connection_target() -> None:
    capabilities = BrokerCapabilities(
        provider="kis",
        extensions=(
            ExtensionCapability(
                id="kis.hts_conditions.v1",
                targets=(ConnectionTarget.LIVE,),
            ),
        ),
    )
    assert capabilities.supports_extension(
        "kis.hts_conditions.v1", target=ConnectionTarget.LIVE
    )
    assert not capabilities.supports_extension(
        "kis.hts_conditions.v1", target=ConnectionTarget.PAPER
    )


def test_sync_client_owns_and_closes_private_event_loop() -> None:
    from trade_router import StrategyConfig

    client = TradeRouterClient(
        strategy=StrategyConfig(
            id="sync-test",
            trading_account="account",
            market_sources={"primary": "source"},
        ),
        base_url="http://127.0.0.1:9",
        session_token="test-session",
    )
    assert client.market is not None
    client.close()
    client.close()


def test_async_client_read_timeout_covers_scheduler_long_poll() -> None:
    client = AsyncTradeRouter(
        strategy=StrategyConfig(
            id="timeout-test",
            trading_account="account",
            market_sources={"primary": "source"},
        ),
        base_url="http://127.0.0.1:9",
        session_token="test-session",
    )
    try:
        assert client._http.timeout.read > 25  # noqa: SLF001
    finally:
        asyncio.run(client.close())


def test_async_order_sdk_uses_common_order_endpoint() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/orders"
            assert request.headers["x-trade-router-strategy"] == "sdk-order-test"
            assert b'"client_order_id":"005930-sdk-buy"' in request.content
            assert b'"route":"AUTO"' in request.content
            return httpx.Response(
                200,
                json={
                    "id": "order-1",
                    "client_order_id": "005930-sdk-buy",
                    "account_alias": "broker-account",
                    "status": "ACKNOWLEDGED",
                    "submitted_at": "2026-10-15T09:30:00+09:00",
                    "provider_order_id": "provider-1",
                    "filled_quantity": "0",
                    "average_fill_price": None,
                    "message": "accepted",
                    "request": {
                        "instrument": {
                            "code": "005930",
                            "country": "KR",
                            "asset": "EQUITY",
                        },
                        "side": "BUY",
                        "order_type": "MARKET",
                        "quantity": "1",
                        "price": None,
                        "route": "AUTO",
                        "provider_options": {},
                    },
                },
            )

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://router.test",
        )
        router = AsyncTradeRouter(
            strategy=StrategyConfig(
                id="sdk-order-test",
                trading_account="broker-account",
                market_sources={"primary": "market-source"},
            ),
            http_client=http,
        )
        try:
            order = await router.orders.market_buy(
                "005930",
                Decimal("1"),
                client_order_id="005930-sdk-buy",
            )
            assert order.id == "order-1"
            assert order.provider_order_id == "provider-1"
            assert order.request.instrument.code == "005930"
        finally:
            await http.aclose()

    asyncio.run(run())


def test_async_discovery_sdk_normalizes_ranking_entries() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/discovery/ranking"
            assert request.url.params["source"] == "live-source"
            assert request.url.params["source_alias"] == "primary"
            assert request.url.params["kind"] == "RISE"
            assert request.url.params["market"] == "KOSPI"
            assert request.url.params["limit"] == "10"
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            "rank": 1,
                            "kind": "RISE",
                            "instrument": {
                                "code": "005930",
                                "country": "KR",
                                "asset": "EQUITY",
                            },
                            "provider": "kis",
                            "source_alias": "primary",
                            "name": "삼성전자",
                            "price": "73000",
                            "change_rate": "3.20",
                            "volume": "123456",
                            "trading_value": "9000000",
                        }
                    ]
                },
            )

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://router.test",
        )
        router = AsyncTradeRouter(
            strategy=StrategyConfig(
                id="ranking-test",
                trading_account="broker-account",
                market_sources={"primary": "live-source"},
            ),
            http_client=http,
        )
        try:
            entries = await router.discovery.ranking(
                RankingKind.RISE,
                market="KOSPI",
                limit=10,
            )
            assert entries[0].instrument.code == "005930"
            assert entries[0].price == Decimal("73000")
            assert entries[0].change_rate == Decimal("3.20")
        finally:
            await http.aclose()

    asyncio.run(run())


def test_async_account_sdk_sends_order_route_to_buying_power_preflight() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/account/buying-power"
            assert request.url.params["route"] == "NXT"
            return httpx.Response(
                200,
                json={
                    "instrument": {"code": "005930", "country": "KR"},
                    "account_alias": "broker-account",
                    "order_type": "MARKET",
                    "requested_price": "0",
                    "calculation_price": "75000",
                    "cash_available": "150000",
                    "maximum_quantity": "2",
                    "provider": "kis",
                    "as_of": "2026-10-16T09:00:00+09:00",
                    "route": "NXT",
                },
            )

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://router.test",
        )
        router = AsyncTradeRouter(
            strategy=StrategyConfig(
                id="buying-power-route-test",
                trading_account="broker-account",
                market_sources={"primary": "live-source"},
            ),
            http_client=http,
        )
        try:
            power = await router.account.buying_power("005930", route=OrderRoute.NXT)
            assert power.route is OrderRoute.NXT
            assert power.maximum_quantity == 2
        finally:
            await http.aclose()

    asyncio.run(run())


def test_async_market_subscription_and_order_change_use_common_endpoints() -> None:
    async def run() -> None:
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.url.path == "/api/v1/market/subscriptions":
                return httpx.Response(
                    201,
                    json={
                        "id": "stream-1",
                        "kind": "QUOTE",
                        "code": "005930",
                        "venue": "INTEGRATED",
                    },
                )
            if request.url.path.endswith("/amend"):
                return httpx.Response(
                    200,
                    json={
                        "id": "order-1",
                        "client_order_id": "original",
                        "account_alias": "account",
                        "status": "ACKNOWLEDGED",
                        "submitted_at": "2026-10-15T09:00:00+09:00",
                        "provider_order_id": "broker-2",
                        "filled_quantity": "0",
                        "average_fill_price": None,
                        "commission": "0",
                        "tax": "0",
                        "costs_complete": False,
                        "message": "amended",
                        "request": {
                            "instrument": {"code": "005930", "country": "KR", "asset": "EQUITY"},
                            "side": "BUY",
                            "order_type": "LIMIT",
                            "quantity": "1",
                            "price": "70000",
                            "route": "AUTO",
                            "provider_options": {},
                        },
                    },
                )
            if request.method == "DELETE":
                return httpx.Response(204)
            raise AssertionError(request.url)

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://router.test"
        )
        router = AsyncTradeRouter(
            strategy=StrategyConfig(
                id="event-sdk",
                trading_account="account",
                market_sources={"primary": "source"},
            ),
            http_client=http,
        )
        try:
            streams = await router.market.subscribe_quotes("005930")
            assert streams[0]["id"] == "stream-1"
            amended = await router.orders.amend(
                "order-1", quantity=1, price=70000
            )
            assert amended.provider_order_id == "broker-2"
            await router.close()
            assert ("DELETE", "/api/v1/market/subscriptions/stream-1") in requests
        finally:
            await http.aclose()

    asyncio.run(run())
