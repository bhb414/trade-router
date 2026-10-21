from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from trade_router import AsyncTradeRouter, InstrumentId, OrderRequest, OrderSide, OrderStatus, OrderType, StrategyConfig
from trade_router_studio.app import OrderSubmissionRegistration, _strategy_performance, _strategy_trade_history
from trade_router_studio.orders import _request_payload, _same_request, _order_from_row
from trade_router_studio.runtime_store import RuntimeStore


def fill(store, owner, side, quantity, price="100", *, account="account", scope=None, commission="0", tax="0", partial=False, requested_quantity=None):
    request = {"code": "005930", "side": side, "quantity": str(requested_quantity or quantity), "order_type": "MARKET"}
    if scope is not None:
        request["sell_position_scope"] = scope
    row, _ = store.reserve_order(
        strategy_id=owner, account_alias=account,
        client_order_id=f"{owner}-{side}-{len(store.orders(limit=1000))}", request=request,
    )
    store.update_order(row["id"], status=OrderStatus.ACKNOWLEDGED)
    return store.update_order(
        row["id"], status=OrderStatus.PARTIALLY_FILLED if partial else OrderStatus.FILLED,
        filled_quantity=str(quantity), average_fill_price=str(price),
        commission=commission, tax=tax, costs_complete=True,
    )


def test_account_sell_debits_executor_then_other_strategy_fifo_and_keeps_accounts_separate(tmp_path):
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        fill(store, "first", "BUY", 4)
        fill(store, "second", "BUY", 6)
        fill(store, "seller", "BUY", 2)
        fill(store, "first", "BUY", 9, account="elsewhere")
        sell = fill(store, "seller", "SELL", 7, scope="ACCOUNT")
        assert store.strategy_position_quantities("seller", account_alias="account") == {}
        assert store.strategy_position_quantities("first", account_alias="account") == {}
        assert store.strategy_position_quantities("second", account_alias="account") == {"005930": Decimal("5")}
        assert store.strategy_position_quantities("first", account_alias="elsewhere") == {"005930": Decimal("9")}
        assert store.order(sell["id"])["strategy_id"] == "seller"
        assert store.order(sell["id"])["filled_quantity"] == "7"
    finally:
        store.close()


def test_default_strategy_sell_cannot_consume_other_ledger_owner(tmp_path):
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        fill(store, "owner", "BUY", 4)
        fill(store, "seller", "SELL", 3)
        assert store.strategy_position_quantities("owner") == {"005930": Decimal("4")}
        assert store.strategy_position_quantities("seller") == {}
    finally:
        store.close()


def test_account_sell_replay_after_restart_does_not_resurrect_previous_ownership(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    store = RuntimeStore(path)
    fill(store, "owner", "BUY", 5)
    fill(store, "seller", "SELL", 5, scope="ACCOUNT")
    store.close()
    store = RuntimeStore(path)
    try:
        assert store.strategy_position_quantities("owner") == {}
        fill(store, "new-owner", "BUY", 5)
        assert store.strategy_position_quantities("owner") == {}
        assert store.strategy_position_quantities("new-owner") == {"005930": Decimal("5")}
    finally:
        store.close()


def test_progressive_partial_fill_is_replayed_once(tmp_path):
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        fill(store, "owner", "BUY", 5)
        row = fill(store, "seller", "SELL", 2, scope="ACCOUNT", partial=True, requested_quantity=5)
        assert store.strategy_position_quantities("owner") == {"005930": Decimal("3")}
        assert store.strategy_position_quantities("owner") == {"005930": Decimal("3")}
        store.update_order(row["id"], status=OrderStatus.FILLED, filled_quantity="5")
        assert store.strategy_position_quantities("owner") == {}
    finally:
        store.close()


def test_other_strategy_sale_keeps_profit_and_cost_with_original_owner(tmp_path):
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        fill(store, "seller", "BUY", 2, "100")
        fill(store, "owner", "BUY", 4, "200")
        sale = fill(store, "seller", "SELL", 8, "250", scope="ACCOUNT", commission="8", tax="16")
        seller_cost_orders = store.strategy_accounting_orders("seller", account_alias="account")
        owner_cost_orders = store.strategy_accounting_orders("owner", account_alias="account")
        seller = _strategy_performance(seller_cost_orders, {"positions": []})
        owner = _strategy_performance(owner_cost_orders, {"positions": []})
        assert seller["realized_profit"] == "294.00"
        assert owner["realized_profit"] == "188.0"
        assert seller["positions"] == owner["positions"] == []
        assert Decimal(seller["filled_sell_amount"]) == Decimal("494")
        assert Decimal(owner["filled_sell_amount"]) == Decimal("988")
        seller_history = _strategy_trade_history(store.orders(strategy_id="seller"), accounting_orders=seller_cost_orders)
        owner_history = _strategy_trade_history(store.orders(strategy_id="owner"), accounting_orders=owner_cost_orders)
        assert seller_history[0]["filled_quantity"] == "8"
        assert seller_history[0]["matched_quantity"] == "2"
        assert Decimal(seller_history[0]["realized_profit"]) == 294
        assert owner_history[0]["id"] == sale["id"]
        assert owner_history[0]["matched_quantity"] == "4"
        assert owner_history[0]["execution_strategy_id"] == "seller"
        assert "계좌 잔고 대상" in owner_history[0]["message"]
    finally:
        store.close()


def test_manual_only_account_sell_does_not_invent_strategy_profit(tmp_path):
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        fill(store, "seller", "SELL", 5, "250", scope="ACCOUNT")
        accounting = store.strategy_accounting_orders("seller")
        assert accounting == []
        result = _strategy_performance(accounting, {"positions": []})
        assert result["realized_profit"] == "0"
        history = _strategy_trade_history(store.orders(strategy_id="seller"), accounting_orders=accounting)
        assert history[0]["filled_quantity"] == "5"
        assert history[0]["realized_profit"] is None
    finally:
        store.close()


def test_depleted_owner_reentry_goes_to_end_of_fifo(tmp_path):
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        fill(store, "first", "BUY", 2)
        fill(store, "first", "SELL", 2)
        fill(store, "second", "BUY", 2)
        fill(store, "first", "BUY", 2)
        fill(store, "seller", "SELL", 2, scope="ACCOUNT")
        assert store.strategy_position_quantities("second") == {}
        assert store.strategy_position_quantities("first") == {"005930": Decimal("2")}
    finally:
        store.close()


def test_scope_validation_and_legacy_idempotency():
    kwargs = dict(instrument=InstrumentId("005930"), side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=Decimal("1"), client_order_id="scope")
    default = OrderRequest(**kwargs)
    stored = _request_payload(default, reserved_amount=Decimal("100"))
    stored.pop("sell_position_scope")
    assert _same_request(stored, default)
    assert not _same_request(stored, OrderRequest(**kwargs, sell_position_scope="ACCOUNT"))
    with pytest.raises(ValueError):
        OrderRequest(**kwargs, sell_position_scope="ALL")
    with pytest.raises(ValueError):
        OrderRequest(**{**kwargs, "side": OrderSide.BUY}, sell_position_scope="ACCOUNT")
    with pytest.raises(ValueError):
        OrderSubmissionRegistration(code="005930", side="BUY", order_type="MARKET", quantity=1, client_order_id="bad", sell_position_scope="ACCOUNT")


@pytest.mark.parametrize("method", ["market_sell", "best_limit_sell", "limit_sell"])
def test_sell_sdk_scope_survives_request_and_response(method):
    async def run():
        sent = []
        def handler(request):
            body = json.loads(request.content)
            sent.append(body)
            return httpx.Response(200, json={
                "id": "scope-order", "client_order_id": body["client_order_id"], "account_alias": "account",
                "status": "ACKNOWLEDGED", "submitted_at": "2026-10-21T09:00:00+09:00",
                "request": {**body, "instrument": {"code": body["code"]}},
            })
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as http:
            router = AsyncTradeRouter(strategy=StrategyConfig(id="seller", trading_account="account"), http_client=http)
            args = ["005930", 1] + ([100] if method == "limit_sell" else [])
            result = await getattr(router.orders, method)(*args, client_order_id="scope", sell_position_scope="ACCOUNT")
            assert sent[0]["sell_position_scope"] == "ACCOUNT"
            assert result.request.sell_position_scope == "ACCOUNT"
    asyncio.run(run())


def test_order_service_persists_scope_and_rejects_idempotency_scope_change(tmp_path):
    from test_order_service import SimulationGateway, manifest
    from trade_router import RouterError, RouterErrorCode
    from trade_router_studio.orders import OrderService
    async def run():
        store = RuntimeStore(tmp_path / "service.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            kwargs = dict(instrument=InstrumentId("005930"), side=OrderSide.SELL,
                          order_type=OrderType.MARKET, quantity=Decimal("2"), client_order_id="scope")
            request = OrderRequest(**kwargs, sell_position_scope="ACCOUNT")
            result = await service.submit(strategy_name="strategy.py", manifest=manifest(), request=request)
            assert result.request.sell_position_scope == "ACCOUNT"
            assert store.order(result.id)["request"]["sell_position_scope"] == "ACCOUNT"
            assert _order_from_row(store.order(result.id)).request.sell_position_scope == "ACCOUNT"
            with pytest.raises(RouterError) as caught:
                await service.submit(strategy_name="strategy.py", manifest=manifest(), request=OrderRequest(**kwargs))
            assert caught.value.code is RouterErrorCode.IDEMPOTENCY_CONFLICT
            assert gateway.submissions == 1
        finally:
            store.close()
    asyncio.run(run())


def test_account_scope_still_checks_broker_available_quantity(tmp_path):
    from test_order_service import SimulationGateway, manifest
    from trade_router import RouterError, RouterErrorCode
    from trade_router_studio.orders import OrderService
    async def run():
        store = RuntimeStore(tmp_path / "service.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            request = OrderRequest(instrument=InstrumentId("005930"), side=OrderSide.SELL,
                                   order_type=OrderType.MARKET, quantity=Decimal("3"),
                                   client_order_id="too-many", sell_position_scope="ACCOUNT")
            with pytest.raises(RouterError) as caught:
                await service.submit(strategy_name="strategy.py", manifest=manifest(), request=request)
            assert caught.value.code is RouterErrorCode.RISK_LIMIT_EXCEEDED
            assert gateway.submissions == 0
        finally:
            store.close()
    asyncio.run(run())
