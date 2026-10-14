from __future__ import annotations

import pytest

from trade_router.errors import RouterError
from trade_router.models import (
    InstrumentId,
    OrderRequest,
    OrderRoute,
    OrderSide,
    OrderStatus,
    OrderType,
)


def order(
    *, key: str = "one", behavior: str = "fill", quantity: int = 1
) -> OrderRequest:
    return OrderRequest(
        strategy_id="sample",
        account_alias="paper-main",
        instrument=InstrumentId(code="005930"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=quantity,
        route=OrderRoute.AUTO,
        client_order_id=key,
        provider_options={"fake": {"behavior": behavior}},
    )


async def test_order_is_idempotent_and_updates_fake_position(runtime):
    first = await runtime.submit_order(order())
    second = await runtime.submit_order(order())
    assert first.accepted is True
    assert first.order is not None and first.order.status == OrderStatus.FILLED
    assert second.replayed is True
    assert second.order.router_order_id == first.order.router_order_id
    positions = await runtime.positions("paper-main")
    assert positions[0].quantity == 1
    fills = await runtime.fills(account_alias="paper-main")
    assert len(fills) == 1
    assert fills[0].quantity == 1


async def test_uncertain_order_closes_global_gate(runtime):
    result = await runtime.submit_order(order(key="lost", behavior="uncertain"))
    assert result.accepted is False
    assert result.order.status == OrderStatus.UNKNOWN_RECONCILING
    assert runtime.accepting_orders is False
    with pytest.raises(RouterError, match="disabled"):
        await runtime.submit_order(order(key="next"))


async def test_partial_fill_locks_symbol(runtime):
    result = await runtime.submit_order(
        order(key="partial", behavior="partial", quantity=2)
    )
    assert result.order.status == OrderStatus.PARTIALLY_FILLED
    with pytest.raises(RouterError) as captured:
        await runtime.submit_order(order(key="conflict"))
    assert captured.value.code == "SYMBOL_ORDER_LOCKED"


async def test_explicit_rejection_is_durable_and_replayed(runtime):
    first = await runtime.submit_order(order(key="reject", behavior="reject"))
    second = await runtime.submit_order(order(key="reject", behavior="fill"))
    assert first.accepted is False
    assert first.order.status == OrderStatus.REJECTED
    assert second.accepted is False and second.replayed is True


async def test_strategy_limit_is_lower_than_account_limit(runtime):
    request = order(key="too-large", quantity=100)
    with pytest.raises(RouterError) as captured:
        await runtime.submit_order(request)
    assert captured.value.code == "STRATEGY_MAX_ORDER_EXCEEDED"


async def test_provider_options_are_namespaced_and_validated(runtime):
    bad = order(key="bad")
    bad.provider_options = {"kis": {"anything": True}}
    with pytest.raises(RouterError) as captured:
        await runtime.submit_order(bad)
    assert captured.value.code == "PROVIDER_OPTIONS_MISMATCH"


async def test_zero_quantity_is_rejected_by_model():
    with pytest.raises(Exception):
        order(quantity=0)
