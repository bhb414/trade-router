"""Evaluate explicitly configured rules for this strategy's unfilled orders.

All mutations use the central order service. A durable attempt is recorded
before a request is sent, so an interrupted or ambiguous response never turns
into an automatic second cancel/amend request after a restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo

from trade_router import Order, OrderStatus, OrderType, RouterError


SEOUL = ZoneInfo("Asia/Seoul")
ATTEMPTS_STATE_KEY = "execution_rule_attempts"
_ACTIONABLE = {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}
_ACTIONS = {"CANCEL_REMAINDER", "AMEND_BEST_LIMIT"}


class ExecutionRuleManager:
    """Run one configured flow against fresh, strategy-owned ledger orders."""

    def __init__(
        self,
        flows: Sequence[Mapping[str, Any]],
        *,
        session_is_open: Callable[[Order, datetime], bool] | None = None,
    ) -> None:
        self._flows = {str(flow["id"]): deepcopy(dict(flow)) for flow in flows}
        self._session_is_open = session_is_open or _best_limit_session_is_open
        self._lock = asyncio.Lock()
        # A single manager is shared by all flow timers. The persistent
        # timestamp also prevents overlapping timer events after a restart.
        self._action_interval = min(
            (max(5, int(flow.get("execution_scan_seconds", 5))) for flow in flows),
            default=5,
        )

    async def tick(
        self, ctx: Any, flow_id: str, *, now: datetime | None = None,
    ) -> int:
        flow = self._flows.get(str(flow_id))
        if flow is None:
            return 0
        at = now or datetime.now(SEOUL)
        if at.tzinfo is None:
            raise ValueError("Execution rule evaluation requires an aware timestamp.")
        if not _flow_is_active(flow, at):
            return 0
        tree = flow.get("execution_rule_tree")
        if not isinstance(tree, Mapping) or not _has_action(tree):
            return 0

        async with self._lock:
            at = now or datetime.now(SEOUL)
            if not _flow_is_active(flow, at):
                return 0
            try:
                # Unlike account.open_orders(), this SDK method returns only
                # router-owned orders authorized for the current strategy.
                orders = await ctx.orders.open_orders()
            except RouterError as exc:
                ctx.log.warning(f"체결 구성 조회 보류 [{exc.code.value}]: {exc.message}")
                return 0
            at = now or datetime.now(SEOUL)
            if not _flow_is_active(flow, at):
                return 0
            attempts = _load_attempts(ctx)
            sent = 0
            for order in orders:
                at = now or datetime.now(SEOUL)
                if not _flow_is_active(flow, at):
                    break
                if (
                    order.status not in _ACTIONABLE
                    or not order.provider_order_id
                    or _remaining(order) <= 0
                    or _recent_attempt(attempts, order.id, at, self._action_interval)
                ):
                    continue
                matched, candidates = _evaluate(tree, order, at)
                if not matched:
                    continue
                for action in candidates:
                    at = now or datetime.now(SEOUL)
                    if not _flow_is_active(flow, at):
                        return sent
                    action_id = str(action.get("id") or "")
                    action_type = action.get("action_type")
                    if not action_id or action_type not in _ACTIONS:
                        continue
                    key = _attempt_key(order.id, str(flow_id), action_id)
                    if key in attempts:
                        continue
                    if (
                        action_type == "AMEND_BEST_LIMIT"
                        and not self._session_is_open(order, at)
                    ):
                        # A market pause does not consume the action. A later
                        # matching cancellation remains eligible in this tick.
                        continue
                    record = {
                        "order_id": order.id,
                        "flow_id": str(flow_id),
                        "action_id": action_id,
                        "action_type": action_type,
                        "attempted_at": at.isoformat(),
                        "status": "sending",
                        "rule_digest": _rule_digest(tree),
                    }
                    attempts[key] = record
                    ctx.state.set(ATTEMPTS_STATE_KEY, attempts)
                    label = _order_label(ctx, order)
                    verb = "잔량 취소" if action_type == "CANCEL_REMAINDER" else "최유리지정가 정정"
                    try:
                        if action_type == "CANCEL_REMAINDER":
                            changed = await ctx.orders.cancel(order.id)
                        else:
                            changed = await ctx.orders.amend(
                                order.id, order_type=OrderType.BEST_LIMIT,
                            )
                    except RouterError as exc:
                        record.update(status="failed", error_code=exc.code.value)
                        ctx.log.warning(
                            f"{label} 체결 구성 {verb} 보류 [{exc.code.value}]: "
                            f"{exc.message} · 이 동작은 자동 재시도하지 않습니다."
                        )
                    except Exception:
                        # A transport exception can occur after the broker
                        # accepted the mutation. Never infer cancellation or
                        # clear the strategy's pending-order guards here.
                        record.update(status="unknown")
                        ctx.log.warning(
                            f"{label} 체결 구성 {verb} 응답 확인 필요 · "
                            "주문 상태를 추적하며 이 동작은 자동 재시도하지 않습니다."
                        )
                    else:
                        status = changed.status.value
                        record.update(status="submitted", order_status=status)
                        ctx.log.info(
                            f"{label} {flow.get('name') or '체결 구성'} · "
                            f"미체결 {_remaining(order)}주 {verb} 요청: {status}"
                        )
                    finally:
                        # CancelledError also retains the durable sending
                        # record, intentionally preventing blind retransmit.
                        ctx.state.set(ATTEMPTS_STATE_KEY, attempts)
                    sent += 1
                    break
            return sent


def _flow_is_active(flow: Mapping[str, Any], at: datetime) -> bool:
    try:
        start = time.fromisoformat(str(flow.get("execution_start_time", "08:00")))
        end = time.fromisoformat(str(flow.get("execution_end_time", "20:00")))
    except ValueError:
        return False
    local = at.astimezone(SEOUL).time().replace(tzinfo=None)
    return start <= local < end


def _remaining(order: Order) -> Decimal:
    return max(Decimal("0"), order.request.quantity - order.filled_quantity)


def _metric(order: Order, at: datetime, name: str) -> Decimal | str | None:
    if name == "order_side":
        return order.request.side.value
    if name == "order_type":
        return order.request.order_type.value
    if name == "elapsed_seconds":
        return max(Decimal("0"), Decimal(str((at - order.submitted_at).total_seconds())))
    if name == "remaining_quantity":
        return _remaining(order)
    if name == "filled_quantity":
        return order.filled_quantity
    return None


def _condition_matches(condition: Mapping[str, Any], order: Order, at: datetime) -> bool:
    actual = _metric(order, at, str(condition.get("metric") or ""))
    expected = condition.get("value")
    operator = condition.get("operator")
    if actual is None:
        return False
    if isinstance(actual, str):
        if operator not in {"==", "!="} or not isinstance(expected, str):
            return False
    else:
        try:
            expected = Decimal(str(expected))
        except (InvalidOperation, ValueError):
            return False
        if not expected.is_finite():
            return False
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    if operator == ">":
        return actual > expected
    if operator == ">=":
        return actual >= expected
    if operator == "<":
        return actual < expected
    if operator == "<=":
        return actual <= expected
    return False


def _evaluate(
    node: Mapping[str, Any], order: Order, at: datetime,
) -> tuple[bool, list[Mapping[str, Any]]]:
    """Keep true branches ordered; the caller skips already consumed actions."""
    own = node.get("action")
    own_actions = [own] if isinstance(own, Mapping) else []
    if node.get("kind") == "condition":
        condition = node.get("condition")
        matched = isinstance(condition, Mapping) and _condition_matches(condition, order, at)
        return matched, own_actions if matched else []
    if node.get("kind") != "logic" or node.get("operator") not in {"ALL", "ANY"}:
        return False, []
    children = node.get("children", [])
    if not isinstance(children, (list, tuple)) or any(not isinstance(child, Mapping) for child in children):
        return False, []
    outcomes = [_evaluate(child, order, at) for child in children]
    if node["operator"] == "ALL":
        if not all(matched for matched, _ in outcomes):
            return False, []
        candidates = [action for _, actions in outcomes for action in actions]
        # A group action is a fallback, not an additional next-tick action.
        # Keep consumed child actions in the candidate list so skipping one
        # cannot unexpectedly activate the parent's fallback after restart.
        return True, candidates or own_actions
    matched_outcomes = [(matched, actions) for matched, actions in outcomes if matched]
    if not matched_outcomes:
        return False, []
    candidates = []
    for _, actions in matched_outcomes:
        candidates.extend(actions or own_actions)
    return True, candidates


def _has_action(node: Mapping[str, Any]) -> bool:
    action = node.get("action")
    if isinstance(action, Mapping) and action.get("action_type") in _ACTIONS:
        return True
    return any(_has_action(child) for child in node.get("children", []) if isinstance(child, Mapping))


def _load_attempts(ctx: Any) -> dict[str, dict[str, Any]]:
    stored = ctx.state.get(ATTEMPTS_STATE_KEY, {})
    return {
        str(key): dict(value) for key, value in stored.items()
        if isinstance(value, Mapping)
    } if isinstance(stored, Mapping) else {}


def _attempt_key(order_id: str, flow_id: str, action_id: str) -> str:
    return json.dumps([order_id, flow_id, action_id], ensure_ascii=True, separators=(",", ":"))


def _recent_attempt(attempts: Mapping[str, Mapping[str, Any]], order_id: str, at: datetime, seconds: int) -> bool:
    for record in attempts.values():
        if record.get("order_id") != order_id:
            continue
        try:
            previous = datetime.fromisoformat(str(record.get("attempted_at") or ""))
            if previous.tzinfo is not None and (at - previous).total_seconds() < seconds:
                return True
        except ValueError:
            continue
    return False


def _rule_digest(tree: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(tree, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _order_label(ctx: Any, order: Order) -> str:
    names = ctx.state.get("instrument_names", {})
    return str(names.get(order.request.instrument.code) or order.request.instrument.code) if isinstance(names, Mapping) else order.request.instrument.code


def _best_limit_session_is_open(order: Order, at: datetime) -> bool:
    local = at.astimezone(SEOUL).time().replace(tzinfo=None)
    route = order.request.route.value
    regular_start = time(9, 0, 30) if route == "NXT" else time(9)
    return regular_start <= local < time(15, 20) or (
        route in {"NXT", "SOR", "AUTO"}
        and (time(8) <= local < time(8, 50) or time(15, 40) <= local < time(20))
    )
