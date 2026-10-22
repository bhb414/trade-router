from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest

from trade_router import AsyncTradeRouter, InstrumentId, OrderRequest, OrderSide, OrderStatus, OrderType, StrategyConfig
from trade_router_studio.app import OrderSubmissionRegistration
from trade_router_studio.orders import OrderService, _request_payload, _same_request
from trade_router_studio.runtime_store import RuntimeStore
from test_order_service import SimulationGateway, manifest

BASE = datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Seoul"))


def reserve(store, *, owner="neutral-strategy", side="BUY", quantity=5, flow=None, nodes=(), selected=(), refs=(), scope="STRATEGY", account="broker-account"):
    row, _ = store.reserve_order(
        strategy_id=owner, account_alias=account,
        client_order_id=f"test-{len(store.orders(limit=10000))}",
        request={"code": "005930", "side": side, "quantity": str(quantity), "order_type": "MARKET", "buy_flow_id": flow,
                 "buy_rule_node_ids": list(nodes), "sell_buy_flow_ids": list(selected), "sell_buy_node_refs": list(refs), "sell_position_scope": scope},
    )
    store.update_order(row["id"], status=OrderStatus.ACKNOWLEDGED)
    return row


def fill(store, row, quantity, second, *, cumulative=None, complete=True):
    cumulative = quantity if cumulative is None else cumulative
    store.record_fill(order_id=row["id"], cumulative_quantity=Decimal(cumulative), quantity=Decimal(quantity), price=Decimal(100),
                      commission=Decimal(0), tax=Decimal(0), costs_complete=True, filled_at=BASE+timedelta(seconds=second))
    store.update_order(row["id"], status=OrderStatus.FILLED if complete else OrderStatus.PARTIALLY_FILLED,
                       filled_quantity=str(cumulative), average_fill_price="100")


def quantities(store, **kwargs):
    return store.strategy_buy_flow_quantities("neutral-strategy", account_alias="broker-account", **kwargs).get("005930", {})


def test_origins_require_fills_and_survive_partial_cancel_and_restart(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    store = RuntimeStore(path)
    row = reserve(store, flow="morning", nodes=("root", "if-1"))
    assert quantities(store) == {}
    fill(store, row, 2, 1, complete=False)
    assert quantities(store) == {"morning": Decimal(2)}
    store.update_order(row["id"], status=OrderStatus.CANCELLED)
    store.close()
    store = RuntimeStore(path)
    try:
        assert quantities(store) == {"morning": Decimal(2)}
        assert store.strategy_buy_origin_lots("neutral-strategy", account_alias="broker-account")["005930"] == [
            {"flow_id": "morning", "node_ids": ("root", "if-1"), "quantity": Decimal(2)}]
    finally:
        store.close()


def test_linked_node_sale_consumes_only_matching_lots_union_once(tmp_path):
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        fill(store, reserve(store, flow="morning", nodes=("root", "if-1")), 3, 1)
        fill(store, reserve(store, flow="morning", nodes=("root", "if-2")), 4, 2)
        fill(store, reserve(store, flow="afternoon", nodes=("root", "if-1")), 5, 3)
        # No node selected in the other flow; a shared node name is not enough.
        refs = ({"flow_id": "morning", "node_id": "if-2"}, {"flow_id": "morning", "node_id": "root"})
        sale = reserve(store, side="SELL", selected=("morning",), refs=refs)
        fill(store, sale, 5, 4)
        assert quantities(store) == {"morning": Decimal(2), "afternoon": Decimal(5)}
        assert store.strategy_position_quantities("neutral-strategy", account_alias="broker-account") == {"005930": Decimal(7)}
        sale = reserve(store, side="SELL", refs=({"flow_id": "afternoon", "node_id": "absent"},))
        fill(store, sale, 1, 5)
        assert quantities(store) == {"morning": Decimal(2), "afternoon": Decimal(5)}
    finally:
        store.close()


def test_legacy_unattributed_lots_are_not_assigned_and_accounts_are_isolated(tmp_path):
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        fill(store, reserve(store), 3, 1)
        fill(store, reserve(store, flow="elsewhere", account="other-account"), 5, 2)
        fill(store, reserve(store, flow="known"), 2, 3)
        assert quantities(store) == {"known": Decimal(2)}
        # Generic strategy sells retain FIFO including legacy lots.
        fill(store, reserve(store, side="SELL"), 4, 4)
        assert quantities(store) == {"known": Decimal(1)}
    finally:
        store.close()


def test_account_sale_partial_fills_follow_execution_order(tmp_path):
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        early = reserve(store, flow="early")
        delayed = reserve(store, flow="late")
        fill(store, early, 3, 1)
        sell = reserve(store, owner="external-strategy", side="SELL", scope="ACCOUNT", quantity=5)
        fill(store, sell, 2, 2, complete=False)
        fill(store, delayed, 4, 3)
        fill(store, sell, 3, 4, cumulative=5)
        assert quantities(store) == {"late": Decimal(2)}
    finally:
        store.close()


def test_manual_reduction_is_persistent_and_later_manual_buy_never_restores_origin(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    store = RuntimeStore(path)
    fill(store, reserve(store, flow="early"), 3, 1)
    fill(store, reserve(store, flow="late"), 4, 2)
    actual = {"005930": Decimal(2)}
    assert quantities(store, actual_quantities=actual, as_of=BASE+timedelta(seconds=10)) == {"late": Decimal(2)}
    assert quantities(store, actual_quantities=actual, as_of=BASE+timedelta(seconds=13)) == {"late": Decimal(2)}
    store.close()
    store = RuntimeStore(path)
    try:
        assert quantities(store, actual_quantities={"005930": Decimal(20)}, as_of=BASE+timedelta(seconds=20)) == {"late": Decimal(2)}
        fill(store, reserve(store, flow="new"), 2, 30)
        assert quantities(store) == {"late": Decimal(2), "new": Decimal(2)}
    finally:
        store.close()


def test_stale_or_pending_balance_caps_without_erasing_fill_origins(tmp_path):
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        row = reserve(store, flow="early")
        fill(store, row, 3, 20, complete=False)
        assert quantities(store, actual_quantities={}, as_of=BASE+timedelta(seconds=30)) == {}
        assert quantities(store) == {"early": Decimal(3)}
        store.update_order(row["id"], status=OrderStatus.CANCELLED)
        assert quantities(store, actual_quantities={}, as_of=BASE+timedelta(seconds=10)) == {}
        assert quantities(store) == {"early": Decimal(3)}
    finally:
        store.close()


def test_cached_balance_after_recent_fill_does_not_permanently_erase_origin(tmp_path):
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        row = reserve(store, flow="early")
        fill(store, row, 3, 20)
        for second in (21, 22, 26):
            assert quantities(store, actual_quantities={}, as_of=BASE+timedelta(seconds=second)) == {}
        assert quantities(store, actual_quantities={"005930": Decimal(3)}, as_of=BASE+timedelta(seconds=27)) == {"early": Decimal(3)}
        assert quantities(store) == {"early": Decimal(3)}
    finally:
        store.close()


@pytest.mark.parametrize("method", ["market_buy", "limit_buy", "best_limit_buy", "market_sell", "limit_sell", "best_limit_sell"])
def test_sdk_origin_fields_round_trip(method):
    async def run():
        sent = []
        def handle(request):
            body = json.loads(request.content)
            sent.append(body)
            return httpx.Response(200, json={"id": "test-order", "client_order_id": body["client_order_id"], "account_alias": "account",
                "status": "ACKNOWLEDGED", "submitted_at": "2026-10-21T09:00:00+09:00", "request": {**body, "instrument": {"code": body["code"]}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://test") as http:
            router = AsyncTradeRouter(strategy=StrategyConfig(id="test", trading_account="account"), http_client=http)
            args = ["005930", 1] + ([100] if method.startswith("limit_") else [])
            kwargs = {"buy_flow_id": "morning", "buy_rule_node_ids": ["if-1"]} if method.endswith("buy") else {"sell_buy_flow_ids": ["afternoon"], "sell_buy_node_refs": [{"flow_id": "morning", "node_id": "if-1"}]}
            result = await getattr(router.orders, method)(*args, client_order_id="origin", **kwargs)
            for key, value in kwargs.items():
                assert sent[0][key] == value
                assert getattr(result.request, key) == (tuple(value) if isinstance(value, list) else value)
    asyncio.run(run())


def test_linked_service_cap_and_unresolved_duplicate_guard(tmp_path):
    from trade_router import RouterError, RouterErrorCode
    async def run():
        store = RuntimeStore(tmp_path / "ledger.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            fill(store, reserve(store, flow="morning", nodes=("if-1",)), 1, 1)
            fill(store, reserve(store, flow="afternoon", nodes=("if-2",)), 1, 2)
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            kwargs = dict(instrument=InstrumentId("005930"), side=OrderSide.SELL, order_type=OrderType.MARKET,
                          sell_buy_flow_ids=("morning",), sell_buy_node_refs=({"flow_id": "morning", "node_id": "if-1"},))
            with pytest.raises(RouterError) as caught:
                await service.submit(strategy_name="strategy.py", manifest=manifest(), request=OrderRequest(**kwargs, quantity=Decimal(2), client_order_id="too-much"))
            assert caught.value.code is RouterErrorCode.RISK_LIMIT_EXCEEDED
            assert gateway.submissions == 0
            order = await service.submit(strategy_name="strategy.py", manifest=manifest(), request=OrderRequest(**kwargs, quantity=Decimal(1), client_order_id="allowed"))
            assert order.request.sell_buy_node_refs == kwargs["sell_buy_node_refs"]
            with pytest.raises(RouterError) as caught:
                await service.submit(strategy_name="strategy.py", manifest=manifest(), request=OrderRequest(**kwargs, quantity=Decimal(1), client_order_id="duplicate"))
            assert caught.value.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
            assert gateway.submissions == 1
        finally:
            store.close()
    asyncio.run(run())


def test_origin_validation_and_legacy_idempotency():
    base = dict(instrument=InstrumentId("005930"), side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=Decimal(1), client_order_id="test")
    request = OrderRequest(**base)
    old = _request_payload(request, reserved_amount=Decimal(100))
    for key in ("buy_flow_id", "buy_rule_node_ids", "sell_buy_flow_ids", "sell_buy_node_refs"):
        old.pop(key)
    assert _same_request(old, request)
    assert not _same_request(old, OrderRequest(**base, buy_flow_id="morning"))
    for kwargs in ({"buy_flow_id": "bad/path"}, {"buy_rule_node_ids": ("if-1",)}, {"sell_buy_flow_ids": ("morning",)}, {"buy_flow_id": "morning", "buy_rule_node_ids": ("if-1", "if-1")}):
        with pytest.raises(ValueError):
            OrderRequest(**base, **kwargs)
    with pytest.raises(ValueError):
        OrderSubmissionRegistration(code="005930", side="SELL", order_type="MARKET", quantity=1, client_order_id="test", sell_position_scope="ACCOUNT", sell_buy_node_refs=[{"flow_id": "morning", "node_id": "if-1"}])


def test_balance_api_and_sdk_expose_only_calling_strategy_origins(tmp_path):
    from fastapi.testclient import TestClient
    from trade_router import Balance, Position
    from trade_router_studio.app import create_app
    from trade_router_studio.sessions import StrategyManifest
    from test_studio import workspace

    app = create_app(workspace(tmp_path / "workspace"))
    async def balance(account):
        return Balance(cash=Decimal(1000), withdrawable_cash=Decimal(1000), total_value=Decimal(1000),
            positions=(Position(instrument=InstrumentId("005930"), quantity=Decimal(10), available_quantity=Decimal(10), average_price=Decimal(100)),),
            as_of=BASE+timedelta(seconds=10))
    app.state.connections.balance = balance
    app.state.connections.account_cost_rates = lambda account: {}
    with TestClient(app) as client:
        store = app.state.runtime_store
        fill(store, reserve(store, flow="morning", nodes=("root", "if-1")), 3, 1)
        fill(store, reserve(store, owner="other", flow="other-flow"), 4, 2)
        fill(store, reserve(store, owner="neutral-strategy", account="other-account", flow="wrong-account"), 4, 3)
        token = app.state.sessions.issue("test.py")
        app.state.sessions.register_manifest("test.py", StrategyManifest(strategy_id="neutral-strategy", trading_account="broker-account", market_sources={}, required_capabilities={}, risk_limits={}))
        response = client.get("/api/v1/account/balance", params={"account": "broker-account"}, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["positions"][0]["buy_flow_quantities"] == {"morning": "3"}
        assert payload["positions"][0]["buy_origin_lots"] == [{"flow_id": "morning", "node_ids": ["root", "if-1"], "quantity": "3"}]

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), base_url="http://test") as http:
            router = AsyncTradeRouter(strategy=StrategyConfig(id="neutral-strategy", trading_account="broker-account"), http_client=http)
            position = (await router.account.balance()).positions[0]
            assert position.buy_flow_quantities == {"morning": Decimal(3)}
            assert position.buy_origin_lots == ({"flow_id": "morning", "node_ids": ("root", "if-1"), "quantity": Decimal(3)},)
    asyncio.run(run())
