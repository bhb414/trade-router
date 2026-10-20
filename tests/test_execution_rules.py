"""Offline contracts for explicit pending-order policies and restart safety."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trade_router import (
    InstrumentId, Order, OrderRequest, OrderRoute, OrderSide, OrderStatus,
    OrderType, RouterError, RouterErrorCode,
)
from trade_router_studio.execution_rules import ATTEMPTS_STATE_KEY, ExecutionRuleManager
from trade_router_studio import execution_rules


SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=SEOUL)


def action(action_id="cancel", action_type="CANCEL_REMAINDER"):
    return {"kind": "execution", "id": action_id, "action_type": action_type}


def condition(metric="elapsed_seconds", operator=">=", value=30, *, selected=None):
    return {
        "kind": "condition", "id": f"condition-{metric}",
        "condition": {"metric": metric, "operator": operator, "value": value},
        "action": selected,
    }


def group(*children, operator="ALL", selected=None):
    return {
        "kind": "logic", "id": "root", "operator": operator,
        "children": list(children), "action": selected,
    }


def flow(tree=None, flow_id="pending", **kwargs):
    return {
        "id": flow_id, "name": "체결 흐름",
        "execution_start_time": "08:00", "execution_end_time": "20:00",
        "execution_scan_seconds": 5,
        "execution_rule_tree": tree or condition(selected=action()),
        **kwargs,
    }


def order(order_id="order-1", **kwargs):
    request = OrderRequest(
        instrument=InstrumentId("005930"), side=kwargs.pop("side", OrderSide.BUY),
        order_type=kwargs.pop("order_type", OrderType.BEST_LIMIT),
        quantity=Decimal("10"), client_order_id=f"client-{order_id}",
        route=OrderRoute.NXT,
    )
    return Order(
        id=order_id, client_order_id=request.client_order_id,
        account_alias="strategy-account", request=request,
        status=kwargs.pop("status", OrderStatus.ACKNOWLEDGED),
        submitted_at=kwargs.pop("submitted_at", NOW - timedelta(seconds=60)),
        provider_order_id=f"broker-{order_id}", **kwargs,
    )


class State:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return deepcopy(self.values.get(key, default))

    def set(self, key, value):
        self.values[key] = deepcopy(value)


class Harness:
    def __init__(self, *orders):
        self.items = list(orders or [order()])
        self.state = State()
        self.fetches = 0
        self.calls = []
        self.logs = []
        self.error = None
        self.cancel_during_send = False
        self.ctx = SimpleNamespace(
            state=self.state,
            log=SimpleNamespace(info=self.logs.append, warning=self.logs.append),
            orders=SimpleNamespace(
                open_orders=self.open_orders, cancel=self.cancel, amend=self.amend,
            ),
        )

    async def open_orders(self):
        self.fetches += 1
        await asyncio.sleep(0)
        return tuple(self.items)

    async def change(self, name, order_id, kwargs):
        # The durability check is made inside the send boundary, before any
        # result can be returned to the caller.
        markers = self.state.get(ATTEMPTS_STATE_KEY)
        assert any(record["order_id"] == order_id and record["status"] == "sending" for record in markers.values())
        self.calls.append((name, order_id, kwargs))
        await asyncio.sleep(0)
        if self.cancel_during_send:
            raise asyncio.CancelledError()
        if self.error:
            raise self.error
        current = next(item for item in self.items if item.id == order_id)
        changed = replace(
            current,
            status=OrderStatus.CANCEL_PENDING if name == "cancel" else current.status,
            provider_order_id=f"changed-{order_id}",
        )
        self.items = [changed if item.id == order_id else item for item in self.items]
        return changed

    async def cancel(self, order_id, **kwargs):
        return await self.change("cancel", order_id, kwargs)

    async def amend(self, order_id, **kwargs):
        return await self.change("amend", order_id, kwargs)


def test_empty_rules_no_actions_or_outside_hours_never_read_or_mutate_orders():
    harness = Harness()

    async def run():
        assert await ExecutionRuleManager([]).tick(harness.ctx, "pending", now=NOW) == 0
        assert await ExecutionRuleManager([flow(condition())]).tick(harness.ctx, "pending", now=NOW) == 0
        manager = ExecutionRuleManager([flow(execution_start_time="10:00", execution_end_time="11:00")])
        assert await manager.tick(harness.ctx, "pending", now=NOW - timedelta(seconds=1)) == 0
        assert await manager.tick(harness.ctx, "pending", now=NOW + timedelta(hours=1)) == 0

    asyncio.run(run())
    assert harness.fetches == 0
    assert harness.calls == []


def test_partial_fill_cancels_only_remaining_via_owned_order_id_and_side_filter():
    partial = order(status=OrderStatus.PARTIALLY_FILLED, filled_quantity=Decimal("3"))
    other_side = order("sell-order", side=OrderSide.SELL)
    harness = Harness(partial, other_side)
    harness.state.set("instrument_names", {"005930": "삼성전자"})
    tree = group(
        condition("order_side", "==", "BUY"),
        condition("remaining_quantity", "==", 7),
        condition("filled_quantity", ">", 0), selected=action(),
    )
    result = asyncio.run(ExecutionRuleManager([flow(tree)]).tick(harness.ctx, "pending", now=NOW))
    assert result == 1
    # quantity omitted requests the central service's authoritative remainder,
    # never the full original ten-share order nor a new sell/buy submission.
    assert harness.calls == [("cancel", partial.id, {})]
    assert harness.items[0].filled_quantity == 3
    assert harness.items[0].status is OrderStatus.CANCEL_PENDING
    assert any("삼성전자" in text and "미체결 7주" in text for text in harness.logs)


@pytest.mark.parametrize("status", [
    OrderStatus.SUBMITTING, OrderStatus.CANCEL_PENDING, OrderStatus.UNKNOWN_RECONCILING,
    OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.FILLED,
])
def test_nonactionable_orders_are_never_changed(status):
    harness = Harness(order(status=status))
    asyncio.run(ExecutionRuleManager([flow()]).tick(harness.ctx, "pending", now=NOW))
    assert harness.calls == []
    assert harness.state.get(ATTEMPTS_STATE_KEY, {}) == {}


def test_all_waits_for_every_condition_before_child_action_and_any_uses_true_branch():
    harness = Harness()
    blocked = group(
        condition(selected=action("premature")),
        condition("order_side", "==", "SELL"),
    )
    alternate = group(
        blocked,
        condition("order_type", "==", "BEST_LIMIT", selected=action("correct")),
        operator="ANY",
    )
    asyncio.run(ExecutionRuleManager([flow(alternate)]).tick(harness.ctx, "pending", now=NOW))
    assert len(harness.calls) == 1
    records = list(harness.state.get(ATTEMPTS_STATE_KEY).values())
    assert records[0]["action_id"] == "correct"


def test_consumed_any_branch_allows_later_timeout_cancel_after_restart():
    harness = Harness()
    tree = group(
        condition(value=30, selected=action("reprice", "AMEND_BEST_LIMIT")),
        condition(value=120, selected=action("timeout")),
        operator="ANY",
    )

    async def run():
        manager = ExecutionRuleManager([flow(tree)])
        assert await manager.tick(harness.ctx, "pending", now=NOW) == 1
        assert await manager.tick(harness.ctx, "pending", now=NOW + timedelta(seconds=10)) == 0
        restarted = ExecutionRuleManager([flow(tree)])
        assert await restarted.tick(harness.ctx, "pending", now=NOW + timedelta(seconds=60)) == 1

    asyncio.run(run())
    assert harness.calls == [
        ("amend", "order-1", {"order_type": OrderType.BEST_LIMIT}),
        ("cancel", "order-1", {}),
    ]
    assert harness.items[0].submitted_at == NOW - timedelta(seconds=60)


def test_modified_rule_does_not_repeat_same_action_id_on_original_order():
    harness = Harness()
    original = condition(value=30, selected=action("same", "AMEND_BEST_LIMIT"))
    changed = condition(value=45, selected=action("same", "CANCEL_REMAINDER"))

    async def run():
        await ExecutionRuleManager([flow(original)]).tick(harness.ctx, "pending", now=NOW)
        assert await ExecutionRuleManager([flow(changed)]).tick(
            harness.ctx, "pending", now=NOW + timedelta(seconds=20),
        ) == 0

    asyncio.run(run())
    assert [name for name, *_ in harness.calls] == ["amend"]


@pytest.mark.parametrize("operator", ["ALL", "ANY"])
def test_consumed_child_action_never_activates_parent_fallback(operator):
    harness = Harness()
    tree = group(
        condition(selected=action("child", "AMEND_BEST_LIMIT")),
        operator=operator, selected=action("parent"),
    )

    async def run():
        await ExecutionRuleManager([flow(tree)]).tick(harness.ctx, "pending", now=NOW)
        assert await ExecutionRuleManager([flow(tree)]).tick(
            harness.ctx, "pending", now=NOW + timedelta(seconds=30),
        ) == 0

    asyncio.run(run())
    assert [name for name, *_ in harness.calls] == ["amend"]
    assert [item["action_id"] for item in harness.state.get(ATTEMPTS_STATE_KEY).values()] == ["child"]


def test_overlapping_concurrent_flow_timers_send_only_one_change_per_order():
    harness = Harness()
    flows = [
        flow(condition(selected=action("reprice", "AMEND_BEST_LIMIT")), "first"),
        flow(flow_id="second"),
    ]

    async def run():
        manager = ExecutionRuleManager(flows)
        results = await asyncio.gather(
            manager.tick(harness.ctx, "first", now=NOW),
            manager.tick(harness.ctx, "second", now=NOW + timedelta(milliseconds=1)),
        )
        assert sum(results) == 1
        # Restart does not remove the cross-flow throttle either.
        restarted = ExecutionRuleManager(flows)
        assert await restarted.tick(harness.ctx, "second", now=NOW + timedelta(seconds=1)) == 0
        assert await restarted.tick(harness.ctx, "second", now=NOW + timedelta(seconds=5)) == 1

    asyncio.run(run())
    assert [name for name, *_ in harness.calls] == ["amend", "cancel"]


@pytest.mark.parametrize("error", [
    RouterError(RouterErrorCode.RISK_LIMIT_EXCEEDED, "운용 예산 초과"),
    RouterError(RouterErrorCode.ORDER_STATUS_UNKNOWN, "접수 응답 불명"),
    RuntimeError("connection closed after send"),
])
def test_send_failure_is_durable_and_not_retried_but_later_cancel_can_run(error):
    harness = Harness()
    harness.error = error
    tree = group(
        condition(value=30, selected=action("reprice", "AMEND_BEST_LIMIT")),
        condition(value=120, selected=action("timeout")), operator="ANY",
    )

    async def run():
        await ExecutionRuleManager([flow(tree)]).tick(harness.ctx, "pending", now=NOW)
        restarted = ExecutionRuleManager([flow(tree)])
        assert await restarted.tick(harness.ctx, "pending", now=NOW + timedelta(seconds=10)) == 0
        # Central reconciliation, represented by this acknowledged snapshot,
        # makes a distinct later action eligible. UNKNOWN would be skipped.
        harness.error = None
        assert await restarted.tick(harness.ctx, "pending", now=NOW + timedelta(seconds=60)) == 1

    asyncio.run(run())
    assert [name for name, *_ in harness.calls] == ["amend", "cancel"]
    assert any("자동 재시도하지 않습니다" in text for text in harness.logs)


def test_interrupted_request_keeps_before_send_marker_across_restart():
    harness = Harness()
    harness.cancel_during_send = True

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await ExecutionRuleManager([flow()]).tick(harness.ctx, "pending", now=NOW)
        harness.cancel_during_send = False
        assert await ExecutionRuleManager([flow()]).tick(
            harness.ctx, "pending", now=NOW + timedelta(minutes=1),
        ) == 0

    asyncio.run(run())
    assert len(harness.calls) == 1
    assert list(harness.state.get(ATTEMPTS_STATE_KEY).values())[0]["status"] == "sending"


def test_session_pause_leaves_amend_unconsumed_then_resumes_when_allowed():
    harness = Harness(order(submitted_at=NOW - timedelta(hours=6)))
    tree = condition(selected=action("best", "AMEND_BEST_LIMIT"))

    async def run():
        manager = ExecutionRuleManager([flow(tree)])
        assert await manager.tick(harness.ctx, "pending", now=NOW.replace(hour=15, minute=35)) == 0
        assert harness.state.get(ATTEMPTS_STATE_KEY, {}) == {}
        assert await manager.tick(harness.ctx, "pending", now=NOW.replace(hour=15, minute=40)) == 1

    asyncio.run(run())
    assert [name for name, *_ in harness.calls] == ["amend"]


def test_session_callback_blocks_amend_but_does_not_block_matching_cancellation():
    harness = Harness()
    tree = group(
        condition(selected=action("best", "AMEND_BEST_LIMIT")),
        condition(selected=action("cancel")), operator="ANY",
    )
    seen = []

    def closed(current, at):
        seen.append((current.id, at))
        return False

    asyncio.run(ExecutionRuleManager([flow(tree)], session_is_open=closed).tick(harness.ctx, "pending", now=NOW))
    assert seen == [("order-1", NOW)]
    assert [name for name, *_ in harness.calls] == ["cancel"]
    assert [item["action_id"] for item in harness.state.get(ATTEMPTS_STATE_KEY).values()] == ["cancel"]


@pytest.mark.parametrize("metric,operator,value,expected", [
    ("elapsed_seconds", ">=", 60, True),
    ("elapsed_seconds", ">", 60, False),
    ("remaining_quantity", "<=", 10, True),
    ("remaining_quantity", "<", 10, False),
    ("filled_quantity", "==", 0, True),
    ("order_type", "!=", "MARKET", True),
    ("order_side", ">", "BUY", False),
    ("remaining_quantity", ">", "NaN", False),
    ("remaining_quantity", ">", "Infinity", False),
    ("unsupported", "==", 0, False),
])
def test_order_condition_boundaries_and_invalid_values_fail_closed(metric, operator, value, expected):
    harness = Harness()
    tree = condition(metric, operator, value, selected=action())
    result = asyncio.run(ExecutionRuleManager([flow(tree)]).tick(harness.ctx, "pending", now=NOW))
    assert bool(result) is expected


def test_distinct_orders_for_same_symbol_have_independent_attempts():
    harness = Harness(order("first"), order("second"))
    result = asyncio.run(ExecutionRuleManager([flow()]).tick(harness.ctx, "pending", now=NOW))
    assert result == 2
    assert {order_id for _, order_id, _ in harness.calls} == {"first", "second"}


def test_lookup_failure_cannot_create_or_consume_an_action():
    harness = Harness()

    async def unavailable():
        raise RouterError(RouterErrorCode.RATE_LIMITED, "조회 대기")

    harness.ctx.orders.open_orders = unavailable
    result = asyncio.run(ExecutionRuleManager([flow()]).tick(harness.ctx, "pending", now=NOW))
    assert result == 0
    assert harness.calls == []
    assert harness.state.get(ATTEMPTS_STATE_KEY, {}) == {}


@pytest.mark.parametrize("start,end,flow_end", [
    (NOW.replace(minute=0, second=59), NOW.replace(minute=1), "10:01"),
    (NOW.replace(hour=15, minute=19, second=59), NOW.replace(hour=15, minute=20), "20:00"),
])
def test_slow_snapshot_crossing_flow_end_or_best_limit_pause_is_rechecked(monkeypatch, start, end, flow_end):
    harness = Harness()

    class Clock(datetime):
        current = start

        @classmethod
        def now(cls, tz=None):
            return cls.current

    async def slow_snapshot():
        Clock.current = end
        return tuple(harness.items)

    monkeypatch.setattr(execution_rules, "datetime", Clock)
    harness.ctx.orders.open_orders = slow_snapshot
    tree = condition(selected=action("best", "AMEND_BEST_LIMIT"))
    configured = flow(tree, execution_end_time=flow_end)
    result = asyncio.run(ExecutionRuleManager([configured]).tick(harness.ctx, "pending"))
    assert result == 0
    assert harness.calls == []
    assert harness.state.get(ATTEMPTS_STATE_KEY, {}) == {}
