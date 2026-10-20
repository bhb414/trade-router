from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from trade_router import (
    AccountOrder, AsyncTradeRouter, Order, OrderRequest, OrderRoute, OrderSide,
    OrderStatus, OrderType, RouterError, RouterErrorCode, StrategyConfig,
)
from trade_router.client import SyncOrderClient
from trade_router_studio.app import create_app
from trade_router_studio.orders import OrderService
from trade_router_studio.runtime_store import RuntimeStore
from test_connections import workspace
from test_order_service import SimulationGateway, SEOUL, manifest, market_buy


@pytest.mark.parametrize("cancel_quantity", (None, Decimal("2")))
def test_cancel_receipt_child_keeps_original_lookup_and_reconciles(
    tmp_path: Path, cancel_quantity: Decimal | None,
) -> None:
    class CancelChildGateway(SimulationGateway):
        async def cancel_order(self, *args, **kwargs):
            acknowledgement = await super().cancel_order(*args, **kwargs)
            return replace(acknowledgement, provider_order_id="cancel-receipt-child")

    async def run() -> None:
        store = RuntimeStore(tmp_path / "cancel-child.sqlite3")
        gateway = CancelChildGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("cancel-child"), quantity=Decimal("10"), order_type=OrderType.BEST_LIMIT),
            )
            gateway.orders = (broker_order("broker-1", filled="3"),)
            await service.refresh_unresolved_orders("broker-account")
            cancelled = await service.cancel(
                strategy_name="strategy.py", manifest=manifest(), order_id=submitted.id,
                quantity=cancel_quantity, route=None,
            )
            assert cancelled.provider_order_id == "broker-1"
            assert store.order(submitted.id)["provider_metadata"]["cancel_provider_order_id"] == "cancel-receipt-child"
            assert await service.open_orders(manifest=manifest()) == []
            final_snapshot = broker_order(
                "broker-1", filled="3", remaining="0" if cancel_quantity is None else "5",
            )
            if cancel_quantity is None:
                final_snapshot = replace(final_snapshot, status=OrderStatus.CANCELLED)
            gateway.orders = (final_snapshot,)
            await service.refresh_unresolved_orders("broker-account")
            row = store.order(submitted.id)
            assert row["status"] == ("CANCELLED" if cancel_quantity is None else "PARTIALLY_FILLED")
            assert row["filled_quantity"] == "3"
            effective = row["provider_metadata"]["effective_request"]
            assert effective["quantity"] == ("3" if cancel_quantity is None else "8")
            assert effective["order_type"] == "BEST_LIMIT"
            assert effective["price"] is None
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ("amend", "cancel"))
def test_uncertain_modification_does_not_reopen_on_stale_original_snapshot(
    tmp_path: Path, operation: str,
) -> None:
    class UncertainGateway(SimulationGateway):
        attempts = 0
        async def amend_order(self, *args, **kwargs):
            self.attempts += 1
            raise RouterError(RouterErrorCode.ORDER_STATUS_UNKNOWN, "ambiguous mocked response")
        async def cancel_order(self, *args, **kwargs):
            self.attempts += 1
            raise RouterError(RouterErrorCode.ORDER_STATUS_UNKNOWN, "ambiguous mocked response")

    async def run() -> None:
        store = RuntimeStore(tmp_path / "uncertain.sqlite3")
        gateway = UncertainGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("uncertain"), quantity=Decimal("10")),
            )
            gateway.orders = (broker_order("broker-1", filled="3"),)
            await service.refresh_unresolved_orders("broker-account")
            kwargs = dict(strategy_name="strategy.py", manifest=manifest(), order_id=submitted.id, quantity=None, route=None)
            if operation == "amend":
                kwargs.update(order_type=OrderType.BEST_LIMIT, price=None)
            result = await getattr(service, operation)(**kwargs)
            assert result.status is OrderStatus.UNKNOWN_RECONCILING
            assert await service.open_orders(manifest=manifest()) == []
            assert store.order(submitted.id)["status"] == "UNKNOWN_RECONCILING"
            with pytest.raises(RouterError):
                await getattr(service, operation)(**kwargs)
            assert gateway.attempts == 1
            with pytest.raises(RouterError) as unresolved:
                await service.reconcile_account("broker-account")
            assert unresolved.value.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
            gateway.orders = (replace(
                broker_order("broker-1", filled="10", remaining="0"), status=OrderStatus.FILLED,
            ),)
            await service.refresh_unresolved_orders("broker-account")
            assert store.order(submitted.id)["status"] == "FILLED"
        finally:
            store.close()
    asyncio.run(run())



@pytest.mark.parametrize("rest_first", (False, True))
@pytest.mark.parametrize("cumulative_snapshot", (False, True))
def test_amended_rest_and_realtime_fills_do_not_double_count(
    tmp_path: Path, rest_first: bool, cumulative_snapshot: bool,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "notice-amend.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("amend-notice"), quantity=Decimal("10")),
            )
            gateway.orders = (broker_order("broker-1", filled="3"),)
            amended = await service.amend(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                order_id=submitted.id, quantity=Decimal("7"), order_type=OrderType.BEST_LIMIT,
                price=None, route=None,
            )
            child_id = amended.provider_order_id or ""
            def snapshot(full: bool = False) -> AccountOrder:
                child = broker_order(
                    child_id,
                    quantity="10" if cumulative_snapshot else "7",
                    filled=("10" if full else "5") if cumulative_snapshot else ("7" if full else "2"),
                    remaining="0" if full else "5",
                    average=("104000" if full else "98000") if cumulative_snapshot else "110000",
                )
                return replace(child, status=OrderStatus.FILLED) if full else child
            notice = {
                "provider": "kis", "provider_order_id": child_id,
                "original_order_id": "broker-1", "code": "005930",
                "quantity": "2", "price": "110000", "time": "160100",
                "rejected": "0", "execution_code": "2", "ordered_quantity": "7",
            }
            gateway.orders = (broker_order("broker-1", filled="3"), snapshot())
            if rest_first:
                await service.refresh_unresolved_orders("broker-account")
            await service.apply_realtime_notice("broker-account", notice)
            await service.refresh_unresolved_orders("broker-account")
            assert store.order(submitted.id)["filled_quantity"] == "5"
            assert store.order(submitted.id)["average_fill_price"] == "98000"
            assert sum(Decimal(fill["quantity"]) for fill in store.fills(order_id=submitted.id)) == 5
            gateway.orders = (broker_order("broker-1", filled="3"), snapshot(full=True))
            if rest_first:
                await service.refresh_unresolved_orders("broker-account")
            await service.apply_realtime_notice("broker-account", {
                **notice, "quantity": "5", "time": "160110",
            })
            await service.refresh_unresolved_orders("broker-account")
            current = store.order(submitted.id)
            assert current["status"] == "FILLED"
            assert current["filled_quantity"] == "10"
            assert current["average_fill_price"] == "104000"
            assert sum(Decimal(fill["quantity"]) for fill in store.fills(order_id=submitted.id)) == 10
            assert await service.open_orders(manifest=manifest()) == []
        finally:
            store.close()
    asyncio.run(run())



def broker_order(provider_id: str, *, filled: str = "0", quantity: str = "10",
                 remaining: str | None = None, average: str = "90000") -> AccountOrder:
    quantity_value = Decimal(quantity)
    filled_value = Decimal(filled)
    return AccountOrder(
        provider_order_id=provider_id, account_alias="broker-account",
        instrument=market_buy().instrument, side=OrderSide.BUY,
        order_type=OrderType.BEST_LIMIT,
        status=OrderStatus.PARTIALLY_FILLED if filled_value else OrderStatus.ACKNOWLEDGED,
        requested_quantity=quantity_value, filled_quantity=filled_value,
        remaining_quantity=Decimal(remaining) if remaining is not None else quantity_value - filled_value,
        ordered_at=datetime.now(SEOUL), provider="simulation",
        average_fill_price=Decimal(average) if filled_value else None,
        route=OrderRoute.KRX,
    )


def test_open_orders_api_reconciles_and_only_exposes_owned_actionable_orders(tmp_path: Path) -> None:
    app = create_app(workspace(tmp_path / "api"))
    with TestClient(app) as client:
        gateway = SimulationGateway()
        app.state.order_service._gateway = gateway
        store = app.state.runtime_store
        owner = manifest()
        token = app.state.sessions.issue("owned.py")
        app.state.sessions.register_manifest("owned.py", owner)

        def seed(key: str, strategy: str = owner.strategy_id, account: str = owner.trading_account,
                 status: OrderStatus = OrderStatus.ACKNOWLEDGED, provider: str | None = None) -> str:
            row, _ = store.reserve_order(
                strategy_id=strategy, account_alias=account, client_order_id=key,
                request={
                    "code": "005930", "side": "BUY", "order_type": "BEST_LIMIT",
                    "quantity": "10", "price": None, "route": "KRX", "reserved_amount": "1000000",
                },
            )
            store.update_order(row["id"], status=OrderStatus.ACKNOWLEDGED, provider_order_id=provider)
            if status is not OrderStatus.ACKNOWLEDGED:
                store.update_order(row["id"], status=status)
            return row["id"]

        owned_id = seed("owned", provider="owned-broker")
        seed("other-strategy", strategy="someone-else", provider="other-broker")
        seed("other-account", account="another-account", provider="other-account-broker")
        seed("cancel", status=OrderStatus.CANCEL_PENDING, provider="cancel-broker")
        seed("unknown", status=OrderStatus.UNKNOWN_RECONCILING)
        seed("not-confirmed", provider="missing-broker")
        gateway.orders = (
            broker_order("owned-broker", filled="3"),
            broker_order("other-broker"),
            broker_order("cancel-broker"),
            broker_order("manual-broker"),
        )
        assert client.get("/api/v1/orders/open").status_code == 401
        result = client.get("/api/v1/orders/open", headers={"Authorization": f"Bearer {token}"})
        assert result.status_code == 200
        orders = result.json()["orders"]
        assert [item["id"] for item in orders] == [owned_id]
        assert orders[0]["filled_quantity"] == "3"
        assert orders[0]["request"]["order_type"] == "BEST_LIMIT"
        assert orders[0]["status"] == "PARTIALLY_FILLED"
        cancel = store.order_by_client_id(owner.strategy_id, "cancel")
        assert cancel["status"] == "CANCEL_PENDING"


@pytest.mark.parametrize("synchronous", (False, True))
def test_open_orders_sdk_returns_router_orders(synchronous: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/orders/open"
        assert request.headers["x-trade-router-strategy"] == "execution-test"
        return httpx.Response(200, json={"orders": [{
            "id": "internal-id", "client_order_id": "original-client-id",
            "account_alias": "broker-account", "status": "PARTIALLY_FILLED",
            "submitted_at": "2026-10-21T16:00:00+09:00", "provider_order_id": "amended-id",
            "filled_quantity": "5", "request": {
                "instrument": {"code": "005930"}, "side": "BUY", "order_type": "BEST_LIMIT",
                "quantity": "10", "price": None, "route": "NXT",
            },
        }]})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://router.test")
    router = AsyncTradeRouter(
        strategy=StrategyConfig(id="execution-test", trading_account="broker-account"), http_client=http,
    )
    try:
        orders = (
            SyncOrderClient(SimpleNamespace(_async=router, _call=asyncio.run)).open_orders()
            if synchronous else asyncio.run(router.orders.open_orders())
        )
        assert isinstance(orders[0], Order)
        assert orders[0].id == "internal-id"
        assert orders[0].request.quantity - orders[0].filled_quantity == 5
        assert orders[0].submitted_at.hour == 16
    finally:
        asyncio.run(http.aclose())


def test_amended_child_fills_remain_cumulative_and_keep_original_time(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "amendments.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("amend-with-fills"), quantity=Decimal("10")),
            )
            gateway.orders = (broker_order("broker-1", filled="3"),)
            await service.refresh_unresolved_orders("broker-account")
            amended = await service.amend(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                order_id=submitted.id, quantity=Decimal("7"), order_type=OrderType.BEST_LIMIT,
                price=None, route=None,
            )
            assert amended.request.quantity == 10
            assert amended.filled_quantity == 3
            assert amended.submitted_at == submitted.submitted_at
            child_id = amended.provider_order_id or ""
            gateway.orders = (
                broker_order("broker-1", filled="3"),
                broker_order(child_id, quantity="7", filled="2", remaining="5", average="110000"),
            )
            for _ in range(2):
                await service.refresh_unresolved_orders("broker-account")
            current = (await service.open_orders(manifest=manifest()))[0]
            assert current.filled_quantity == 5
            assert current.request.quantity == 10
            assert current.request.quantity - current.filled_quantity == 5
            assert current.average_fill_price == Decimal("98000")
            assert sum(Decimal(fill["quantity"]) for fill in store.fills(order_id=submitted.id)) == 5
            assert current.submitted_at == submitted.submitted_at
        finally:
            store.close()
    asyncio.run(run())


def test_buy_amendment_rechecks_live_price_and_never_expands_reserved_budget(tmp_path: Path) -> None:
    class RepricedGateway(SimulationGateway):
        calculation_price = Decimal("100000")
        async def buying_power(self, *args, **kwargs):
            power = await super().buying_power(*args, **kwargs)
            return replace(power, calculation_price=self.calculation_price)

    async def run() -> None:
        store = RuntimeStore(tmp_path / "risk.sqlite3")
        gateway = RepricedGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("budget-amend"), quantity=Decimal("10")),
            )
            gateway.orders = (broker_order("broker-1", filled="3"),)
            gateway.calculation_price = Decimal("120000")
            with pytest.raises(RouterError) as blocked:
                await service.amend(
                    strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                    order_id=submitted.id, quantity=None, order_type=OrderType.BEST_LIMIT,
                    price=None, route=None,
                )
            assert blocked.value.code is RouterErrorCode.RISK_LIMIT_EXCEEDED
            assert gateway.changes == []
            with pytest.raises(RouterError) as partial:
                await service.amend(
                    strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                    order_id=submitted.id, quantity=Decimal("5"), order_type=OrderType.BEST_LIMIT,
                    price=None, route=None,
                )
            assert partial.value.code is RouterErrorCode.UNSUPPORTED_CAPABILITY
            assert gateway.changes == []
            gateway.calculation_price = Decimal("90000")
            amended = await service.amend(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                order_id=submitted.id, quantity=None, order_type=OrderType.BEST_LIMIT,
                price=None, route=None,
            )
            assert amended.request.quantity == 10
            assert amended.filled_quantity == 3
            assert gateway.changes == [("amend", "broker-1", Decimal("7"))]
            assert store.order(submitted.id)["request"]["reserved_amount"] == "1000000"
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("rest_first", (False, True))
@pytest.mark.parametrize("reversed_notices", (False, True))
def test_original_rest_and_realtime_streams_never_double_count(
    tmp_path: Path, rest_first: bool, reversed_notices: bool,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "original-overlap.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("original-overlap"), quantity=Decimal("10")),
            )
            base = {
                "provider": "kis", "provider_order_id": "broker-1",
                "original_order_id": "", "code": "005930",
                "rejected": "0", "execution_code": "2",
            }
            notices = [
                {**base, "quantity": "1", "price": "90000", "time": "160100"},
                {**base, "quantity": "2", "price": "105000", "time": "160102"},
            ]
            if reversed_notices:
                notices.reverse()
            gateway.orders = (broker_order("broker-1", filled="3", average="100000"),)
            if rest_first:
                await service.refresh_unresolved_orders("broker-account")
            await service.apply_realtime_notice("broker-account", notices[0])
            await service.refresh_unresolved_orders("broker-account")
            # Persisted stream totals also survive a service restart.
            service = OrderService(store, gateway)
            await service.apply_realtime_notice("broker-account", notices[1])
            await service.apply_realtime_notice("broker-account", notices[0])
            await service.refresh_unresolved_orders("broker-account")
            row = store.order(submitted.id)
            assert Decimal(row["filled_quantity"]) == 3
            assert Decimal(row["average_fill_price"]) == 100000
            # The stream was behind REST, so even a subsequent notice is not
            # assumed to be an incremental fill. The next snapshot confirms it.
            await service.apply_realtime_notice("broker-account", {
                **base, "quantity": "2", "price": "120000", "time": "160104",
            })
            await service.refresh_unresolved_orders("broker-account")  # stale REST=3
            assert Decimal(store.order(submitted.id)["filled_quantity"]) == 3
            gateway.orders = (broker_order("broker-1", filled="5", average="108000"),)
            await service.refresh_unresolved_orders("broker-account")
            row = store.order(submitted.id)
            assert Decimal(row["filled_quantity"]) == 5
            assert Decimal(row["average_fill_price"]) == 108000
            assert sum(Decimal(fill["quantity"]) for fill in store.fills(order_id=submitted.id)) == 5
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ("amend", "cancel"))
def test_rejected_modification_notice_does_not_reject_uncertain_original(
    tmp_path: Path, operation: str,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "child-rejection.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("child-rejection"), quantity=Decimal("10")),
            )
            store.update_order(
                submitted.id, status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={"uncertain_modification": operation},
            )
            await service.apply_realtime_notice("broker-account", {
                "provider": "kis", "provider_order_id": "rejected-child",
                "original_order_id": "broker-1", "execution_code": "1",
                "rejected": "1", "time": "160100",
            })
            assert store.order(submitted.id)["status"] == "UNKNOWN_RECONCILING"
            gateway.orders = (broker_order("broker-1"),)
            assert await service.open_orders(manifest=manifest()) == []
            assert store.order(submitted.id)["status"] == "UNKNOWN_RECONCILING"
            gateway.orders = (replace(
                broker_order("broker-1", filled="10", remaining="0"), status=OrderStatus.FILLED,
            ),)
            await service.refresh_unresolved_orders("broker-account")
            assert store.order(submitted.id)["status"] == "FILLED"
        finally:
            store.close()
    asyncio.run(run())


def test_late_execution_cannot_mutate_terminal_cancelled_order(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "late-cancelled.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("late-cancelled"), quantity=Decimal("10")),
            )
            gateway.orders = (broker_order("broker-1", filled="3"),)
            await service.refresh_unresolved_orders("broker-account")
            store.update_order(submitted.id, status=OrderStatus.CANCELLED)
            await service.apply_realtime_notice("broker-account", {
                "provider": "kis", "provider_order_id": "broker-1", "quantity": "4",
                "price": "90000", "time": "160100", "rejected": "0", "execution_code": "2",
            })
            row = store.order(submitted.id)
            assert row["status"] == "CANCELLED"
            assert Decimal(row["filled_quantity"]) == 3
            assert sum(Decimal(fill["quantity"]) for fill in store.fills(order_id=submitted.id)) == 3
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("amended", (False, True))
def test_missing_realtime_segment_waits_for_rest_without_fabricating_fill_price(
    tmp_path: Path, amended: bool,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "missing-notice.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("missing-notice"), quantity=Decimal("10")),
            )
            provider_id = "broker-1"
            if amended:
                gateway.orders = (broker_order("broker-1", filled="3"),)
                changed = await service.amend(
                    strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                    order_id=submitted.id, quantity=None, order_type=OrderType.BEST_LIMIT,
                    price=None, route=None,
                )
                provider_id = changed.provider_order_id or ""
                gateway.orders += (broker_order(provider_id, filled="2", quantity="7"),)
            else:
                gateway.orders = (broker_order(provider_id, filled="3"),)
            await service.refresh_unresolved_orders("broker-account")
            before = store.order(submitted.id)
            before_fills = store.fills(order_id=submitted.id)
            notice = {
                "provider": "kis", "provider_order_id": provider_id,
                "original_order_id": "broker-1" if amended else "",
                "quantity": "4", "price": "110000", "time": "160100",
                "rejected": "0", "execution_code": "2",
            }
            await service.apply_realtime_notice("broker-account", notice)
            service = OrderService(store, gateway)
            await service.apply_realtime_notice("broker-account", {
                **notice, "quantity": "1", "price": "120000", "time": "160101",
            })
            current = store.order(submitted.id)
            assert current["filled_quantity"] == before["filled_quantity"]
            assert current["average_fill_price"] == before["average_fill_price"]
            assert store.fills(order_id=submitted.id) == before_fills
            assert current["provider_metadata"]["realtime_fill_totals"][provider_id]["rest_only"] is True
            if amended:
                child = replace(
                    broker_order(provider_id, filled="7", quantity="7", average=str(Decimal("740000") / 7)),
                    status=OrderStatus.FILLED,
                )
                gateway.orders = (broker_order("broker-1", filled="3"), child)
            else:
                gateway.orders = (broker_order(provider_id, filled="8", average="103750"),)
            await service.refresh_unresolved_orders("broker-account")
            final = store.order(submitted.id)
            assert Decimal(final["filled_quantity"]) == (10 if amended else 8)
            assert abs(Decimal(final["average_fill_price"]) - (101000 if amended else 103750)) < Decimal("0.000001")
        finally:
            store.close()
    asyncio.run(run())


def test_uninterrupted_realtime_stream_keeps_advancing_without_waiting_for_rest(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "continuous-stream.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("continuous-stream"), quantity=Decimal("10")),
            )
            notice = {
                "provider": "kis", "provider_order_id": "broker-1", "quantity": "3",
                "price": "90000", "time": "160100", "rejected": "0", "execution_code": "2",
            }
            await service.apply_realtime_notice("broker-account", notice)
            gateway.orders = (broker_order("broker-1", filled="3"),)
            await service.refresh_unresolved_orders("broker-account")
            await service.apply_realtime_notice("broker-account", {
                **notice, "quantity": "4", "price": "110000", "time": "160101",
            })
            assert Decimal(store.order(submitted.id)["filled_quantity"]) == 7
            assert sum(Decimal(fill["quantity"]) for fill in store.fills(order_id=submitted.id)) == 7
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("original_id", ("", "0", "0000000000"))
def test_initial_order_rejection_with_empty_original_id_is_still_terminal(
    tmp_path: Path, original_id: str,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "initial-rejection.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("initial-rejection"), quantity=Decimal("10")),
            )
            store.update_order(submitted.id, status=OrderStatus.UNKNOWN_RECONCILING)
            await service.apply_realtime_notice("broker-account", {
                "provider": "kis", "provider_order_id": "broker-1", "original_order_id": original_id,
                "execution_code": "1", "rejected": "1", "time": "160100",
            })
            assert store.order(submitted.id)["status"] == "REJECTED"
        finally:
            store.close()
    asyncio.run(run())

