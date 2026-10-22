from __future__ import annotations

import ast
import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from decimal import Decimal
import json
import logging
import os
import re
from pathlib import Path
import time
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from trade_router import (
    InstrumentId,
    Order,
    OrderRequest,
    OrderRoute,
    OrderSide,
    OrderStatus,
    OrderType,
    RouterError,
    RouterErrorCode,
    RankingKind,
    Venue,
)

from . import __version__
from .block_packages import (
    BlockStrategyPackage,
    block_package_from_source,
    compile_block_strategy,
)
from .connections import ConnectionRuntime
from .diagnostics import build_diagnostics, snapshot_manager
from .event_bus import StrategyEventBus
from .file_io import read_shared_text
from .instrument_catalog import InstrumentCatalog
from .paths import WorkspacePaths
from .runtime_scheduler import RuntimeScheduler, schedule_from_dict
from .runtime_store import RuntimeStore, RuntimeStoreRestartRequired
from .orders import OrderService
from .order_reconciler import OrderReconciler
from .realtime import RealtimeHub
from .settings import SettingsError, SettingsStore
from .sessions import StrategyManifest, StrategySession, StrategySessionRegistry
from .strategies import StrategyManager, StrategyNameError
from .updates import check_for_update, prepare_update


SEOUL = ZoneInfo("Asia/Seoul")


class StrategySource(BaseModel):
    source: str
    strategy_type: Literal["script", "block"] | None = None


class ConnectionRegistration(BaseModel):
    provider: str
    target: str
    label: str
    app_key: str
    app_secret: str
    realtime_user_id: str = ""


class ConnectionUpdate(BaseModel):
    provider: str
    target: str
    label: str
    realtime_user_id: str | None = None


class AccountRegistration(BaseModel):
    alias: str
    connection_id: str
    account_number: str
    product_code: str = ""
    max_order_amount: Decimal | None = Field(default=None, gt=0)
    max_daily_buy_amount: Decimal | None = Field(default=None, gt=0)
    max_daily_buy_count: int | None = Field(default=None, ge=1)
    max_exposure: Decimal | None = Field(default=None, gt=0)
    max_open_orders: int | None = Field(default=None, ge=1)
    buy_commission_rate: Decimal | None = Field(default=None, ge=0)
    sell_commission_rate: Decimal | None = Field(default=None, ge=0)
    sell_tax_rate: Decimal | None = Field(default=None, ge=0)


class AccountUpdate(BaseModel):
    alias: str
    connection_id: str
    account_number: str | None = None
    product_code: str | None = None
    max_order_amount: Decimal | None = Field(default=None, gt=0)
    max_daily_buy_amount: Decimal | None = Field(default=None, gt=0)
    max_daily_buy_count: int | None = Field(default=None, ge=1)
    max_exposure: Decimal | None = Field(default=None, gt=0)
    max_open_orders: int | None = Field(default=None, ge=1)
    buy_commission_rate: Decimal | None = Field(default=None, ge=0)
    sell_commission_rate: Decimal | None = Field(default=None, ge=0)
    sell_tax_rate: Decimal | None = Field(default=None, ge=0)


class ScheduleRegistration(BaseModel):
    schedules: list[dict[str, object]]


class StrategyManifestRegistration(BaseModel):
    strategy_id: str
    trading_account: str
    market_sources: dict[str, str]
    required_capabilities: dict[str, list[str]] = Field(default_factory=dict)
    risk_limits: "StrategyRiskLimitsRegistration" = Field(
        default_factory=lambda: StrategyRiskLimitsRegistration()
    )


class StrategyRiskLimitsRegistration(BaseModel):
    max_order_amount: Decimal | None = Field(default=None, gt=0)
    max_daily_buy_amount: Decimal | None = Field(default=None, gt=0)
    max_daily_buy_count: int | None = Field(default=None, ge=1)
    max_exposure: Decimal | None = Field(default=None, gt=0)
    max_open_orders: int | None = Field(default=None, ge=1)


class OrderSubmissionRegistration(BaseModel):
    code: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal = Field(gt=0)
    client_order_id: str = Field(min_length=1, max_length=128)
    route: OrderRoute = OrderRoute.AUTO
    price: Decimal | None = Field(default=None, gt=0)
    provider_options: dict[str, object] = Field(default_factory=dict)
    sell_position_scope: Literal["STRATEGY", "ACCOUNT"] = "STRATEGY"
    buy_flow_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    sell_buy_flow_ids: list[str] = Field(default_factory=list, max_length=64)
    buy_rule_node_ids: list[str] = Field(default_factory=list, max_length=256)
    sell_buy_node_refs: list[dict[str, str]] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_sell_scope(self) -> "OrderSubmissionRegistration":
        if self.side is not OrderSide.SELL and self.sell_position_scope != "STRATEGY":
            raise ValueError("계좌 잔고 대상은 매도 주문에만 지정할 수 있습니다.")
        if self.buy_flow_id is not None and self.side is not OrderSide.BUY:
            raise ValueError("매수 출처는 매수 주문에만 지정할 수 있습니다.")
        if self.sell_buy_flow_ids and (self.side is not OrderSide.SELL or self.sell_position_scope != "STRATEGY"):
            raise ValueError("연결한 매수 조건은 이 전략의 매도 주문에만 지정할 수 있습니다.")
        if len(set(self.sell_buy_flow_ids)) != len(self.sell_buy_flow_ids) or any(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None for value in self.sell_buy_flow_ids):
            raise ValueError("연결한 매수 조건 ID가 올바르지 않습니다.")
        # Keep HTTP and SDK validation identical, including node references.
        OrderRequest(
            instrument=InstrumentId(self.code), side=self.side, order_type=self.order_type,
            quantity=self.quantity, client_order_id=self.client_order_id,
            route=self.route, price=self.price, sell_position_scope=self.sell_position_scope,
            buy_flow_id=self.buy_flow_id, sell_buy_flow_ids=tuple(self.sell_buy_flow_ids),
            buy_rule_node_ids=tuple(self.buy_rule_node_ids), sell_buy_node_refs=tuple(self.sell_buy_node_refs),
        )
        return self


class RealtimeSubscriptionRegistration(BaseModel):
    source: str
    source_alias: str | None = None
    kind: str
    code: str
    venue: Venue = Venue.INTEGRATED


class OrderAmendRegistration(BaseModel):
    quantity: Decimal | None = Field(default=None, gt=0)
    price: Decimal | None = Field(default=None, gt=0)
    order_type: OrderType = OrderType.LIMIT
    route: OrderRoute | None = None


class OrderCancelRegistration(BaseModel):
    quantity: Decimal | None = Field(default=None, gt=0)
    route: OrderRoute | None = None


class OrderPermissionRegistration(BaseModel):
    confirmation: str = ""


def _ast_value(
    node: ast.AST | None,
    bindings: dict[str, ast.AST] | None = None,
    resolving: frozenset[str] = frozenset(),
) -> object | None:
    """Read the small literal subset used by StrategyConfig declarations."""

    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and bindings and node.id in bindings:
        if node.id in resolving:
            return None
        return _ast_value(
            bindings[node.id],
            bindings,
            resolving | {node.id},
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _ast_value(node.operand, bindings, resolving)
        if isinstance(value, (int, float)):
            return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.Dict):
        result: dict[str, object] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            parsed_key = _ast_value(key, bindings, resolving)
            parsed_value = _ast_value(value, bindings, resolving)
            if isinstance(parsed_key, str) and parsed_value is not None:
                result[parsed_key] = parsed_value
        return result
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_ast_value(item, bindings, resolving) for item in node.elts]
    if isinstance(node, ast.Call) and _call_name(node.func) == "Decimal" and node.args:
        value = _ast_value(node.args[0], bindings, resolving)
        return str(value) if value is not None else None
    return None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _strategy_config_from_source(source: str) -> dict[str, object]:
    """Extract display-only metadata without importing or executing strategy code."""

    empty = {
        "strategy_id": None,
        "trading_account": None,
        "market_sources": {},
        "risk_limits": {
            "max_order_amount": None,
            "max_daily_buy_amount": None,
            "max_daily_buy_count": None,
            "max_exposure": None,
            "max_open_orders": None,
        },
    }
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return empty
    bindings: dict[str, ast.AST] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                bindings[target.id] = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            if statement.value is not None:
                bindings[statement.target.id] = statement.value
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "StrategyConfig":
            continue
        keywords = {item.arg: item.value for item in node.keywords if item.arg}
        result = dict(empty)
        result["strategy_id"] = _ast_value(keywords.get("id"), bindings)
        result["trading_account"] = _ast_value(
            keywords.get("trading_account"), bindings
        )
        sources = _ast_value(keywords.get("market_sources"), bindings)
        result["market_sources"] = sources if isinstance(sources, dict) else {}
        limits = dict(empty["risk_limits"])
        limit_node = keywords.get("limits")
        if isinstance(limit_node, ast.Call) and _call_name(limit_node.func) == "RiskLimits":
            for item in limit_node.keywords:
                if item.arg in limits:
                    value = _ast_value(item.value, bindings)
                    limits[item.arg] = str(value) if value is not None else None
        result["risk_limits"] = limits
        return result
    return empty


def _usage_value(used: Decimal | int | None, limit: object | None) -> dict[str, object]:
    normalized_limit: Decimal | None
    try:
        normalized_limit = Decimal(str(limit)) if limit not in {None, ""} else None
    except Exception:
        normalized_limit = None
    normalized_used = Decimal(str(used)) if used is not None else None
    percent = None
    if normalized_used is not None and normalized_limit is not None and normalized_limit > 0:
        percent = float((normalized_used / normalized_limit) * Decimal("100"))
    return {
        "used": str(normalized_used) if normalized_used is not None else None,
        "limit": str(normalized_limit) if normalized_limit is not None else None,
        "percent": percent,
    }


_SERVICE_LABELS = {
    "market": "시세 데이터",
    "scheduler": "스케줄러",
    "account": "계좌 조회",
    "order": "주문 처리",
}


def _service_result_rows(
    activity: dict[str, dict[str, object]],
    events: dict[str, list[dict[str, object]]] | None = None,
) -> list[dict[str, object]]:
    event_map = events or {}
    rows: list[dict[str, object]] = []
    for service_id, label in _SERVICE_LABELS.items():
        item = activity.get(service_id)
        if item is None:
            rows.append(
                {
                    "id": service_id,
                    "label": label,
                    "state": "idle",
                    "total": 0,
                    "success": 0,
                    "error": 0,
                    "average_duration_ms": None,
                    "last_at": None,
                    "last_operation": None,
                    "last_outcome": None,
                    "last_message": "오늘 처리한 요청이 없습니다.",
                    "last_details": {},
                    "events": list(event_map.get(service_id, [])),
                }
            )
        else:
            rows.append(
                {
                    "id": service_id,
                    "label": label,
                    **item,
                    "events": list(event_map.get(service_id, [])),
                }
            )
    return rows


def _workspace_instrument_names(
    workspace: WorkspacePaths,
    strategy_name: str | None = None,
) -> dict[str, str]:
    """Read display names persisted by managed strategies without executing code."""

    state_root = workspace.data / "strategy-state"
    if strategy_name:
        safe_name = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in strategy_name
        )
        paths = [state_root / f"{safe_name}.json"]
    else:
        paths = list(state_root.glob("*.json")) if state_root.is_dir() else []
    names: dict[str, str] = {}
    for path in paths:
        try:
            payload = json.loads(read_shared_text(path))
        except (OSError, ValueError, TypeError):
            continue
        values = payload.get("instrument_names", {}) if isinstance(payload, dict) else {}
        if not isinstance(values, dict):
            continue
        names.update(
            {
                str(code).strip().upper(): str(name).strip()
                for code, name in values.items()
                if str(code).strip() and str(name).strip()
            }
        )
    return names


def _accounting_order_key(row: dict[str, object]) -> tuple[float, int]:
    value = row.get("accounting_at") or row.get("created_at")
    timestamp = datetime.fromisoformat(str(value)) if value else None
    if timestamp is not None and timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=SEOUL)
    return (timestamp.timestamp() if timestamp else 0, int(row.get("accounting_sequence", 0)))


def _matched_sale_results(
    orders: list[dict[str, object]],
) -> dict[int, dict[str, object]]:
    """Match filled sells to weighted strategy cost at each execution."""

    positions: dict[str, dict[str, Decimal | bool]] = {}
    results: dict[int, dict[str, object]] = {}
    for row in sorted(orders, key=_accounting_order_key):
        request = dict(row.get("request") or {})
        code = str(request.get("code") or "")
        side = str(request.get("side") or "")
        quantity = Decimal(str(row.get("filled_quantity") or "0"))
        price_value = row.get("average_fill_price")
        if not code or quantity <= 0 or price_value in {None, ""}:
            continue
        price = Decimal(str(price_value))
        commission = Decimal(str(row.get("commission") or "0"))
        tax = Decimal(str(row.get("tax") or "0"))
        position = positions.setdefault(
            code,
            {
                "quantity": Decimal("0"),
                "cost": Decimal("0"),
                "costs_complete": True,
            },
        )
        if side == "BUY":
            position["quantity"] = Decimal(str(position["quantity"])) + quantity
            position["cost"] = (
                Decimal(str(position["cost"])) + quantity * price + commission
            )
            position["costs_complete"] = bool(position["costs_complete"]) and bool(
                row.get("costs_complete", False)
            )
            continue
        if side != "SELL":
            continue
        held_quantity = Decimal(str(position["quantity"]))
        matched = min(quantity, held_quantity)
        if matched <= 0:
            continue
        average_cost = Decimal(str(position["cost"])) / held_quantity
        matched_ratio = matched / quantity
        net_proceeds = (quantity * price - commission - tax) * matched_ratio
        cost_basis = average_cost * matched
        profit = net_proceeds - cost_basis
        return_percent = (
            profit / cost_basis * Decimal("100") if cost_basis > 0 else None
        )
        results[id(row)] = {
            "matched_quantity": str(matched),
            "cost_basis": str(cost_basis),
            "net_proceeds": str(net_proceeds),
            "realized_profit": str(profit),
            "return_percent": (
                str(return_percent.quantize(Decimal("0.01")))
                if return_percent is not None
                else None
            ),
            "costs_complete": bool(position["costs_complete"])
            and bool(row.get("costs_complete", False)),
        }
        position["quantity"] = held_quantity - matched
        position["cost"] = Decimal(str(position["cost"])) - cost_basis
        if Decimal(str(position["quantity"])) <= 0:
            position["costs_complete"] = True
    return results


def _strategy_performance(
    orders: list[dict[str, object]],
    account: dict[str, object] | None,
    instrument_names: dict[str, str] | None = None,
) -> dict[str, object]:
    """Calculate strategy P/L from router-owned fills and normalized costs."""

    names = instrument_names or {}
    positions: dict[str, dict[str, Decimal]] = {}
    realized = Decimal("0")
    bought = Decimal("0")
    sold = Decimal("0")
    sale_profits: dict[str, Decimal] = {}
    total_commission = Decimal("0")
    total_tax = Decimal("0")
    cost_flags: list[bool] = []
    for row in sorted(orders, key=_accounting_order_key):
        request = dict(row.get("request") or {})
        code = str(request.get("code") or "")
        side = str(request.get("side") or "")
        quantity = Decimal(str(row.get("filled_quantity") or "0"))
        price_value = row.get("average_fill_price")
        if not code or quantity <= 0 or price_value in {None, ""}:
            continue
        price = Decimal(str(price_value))
        commission = Decimal(str(row.get("commission") or "0"))
        tax = Decimal(str(row.get("tax") or "0"))
        total_commission += commission
        total_tax += tax
        cost_flags.append(bool(row.get("costs_complete", False)))
        position = positions.setdefault(
            code, {"quantity": Decimal("0"), "cost": Decimal("0")}
        )
        if side == "BUY":
            amount = quantity * price
            position["quantity"] += quantity
            position["cost"] += amount + commission
            bought += amount + commission
            continue
        if side != "SELL":
            continue
        net_sale = quantity * price - commission - tax
        sold += net_sale
        matched = min(quantity, position["quantity"])
        if matched <= 0:
            continue
        average_cost = position["cost"] / position["quantity"]
        matched_ratio = matched / quantity
        profit = net_sale * matched_ratio - average_cost * matched
        realized += profit
        sale_id = str(row.get("id") or id(row))
        sale_profits[sale_id] = sale_profits.get(sale_id, Decimal("0")) + profit
        position["quantity"] -= matched
        position["cost"] -= average_cost * matched

    account_positions: dict[str, dict[str, Decimal]] = {}
    for item in (account or {}).get("positions", []) or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        if not code:
            continue
        account_quantity = Decimal(
            str(item.get("quantity") or positions.get(code, {}).get("quantity") or "0")
        )
        account_positions[code] = {
            "quantity": account_quantity,
            "available_quantity": Decimal(
                str(item.get("available_quantity") or account_quantity)
            ),
            "average_price": Decimal(str(item.get("average_price") or "0")),
            "market_price": Decimal(
                str(item.get("market_price") or item.get("average_price") or "0")
            ),
        }
    unrealized = Decimal("0")
    unpriced_codes: list[str] = []
    position_rows: list[dict[str, object]] = []
    account_balance_available = account is not None and account.get("state") in {
        None,
        "ok",
    }
    for code, position in positions.items():
        if position["quantity"] <= 0:
            continue
        account_position = account_positions.get(code)
        quantity = position["quantity"]
        if account_balance_available:
            quantity = min(
                quantity,
                account_position["quantity"] if account_position else Decimal("0"),
            )
        if quantity <= 0:
            continue
        average_cost = position["cost"] / position["quantity"]
        market_price = (
            account_position["market_price"] if account_position else Decimal("0")
        )
        if market_price <= 0:
            unpriced_codes.append(code)
            market_price_value: str | None = None
            market_value: Decimal | None = None
            position_profit: Decimal | None = None
            position_return: Decimal | None = None
        else:
            market_price_value = str(market_price)
            market_value = market_price * quantity
            position_cost = average_cost * quantity
            position_profit = market_value - position_cost
            unrealized += position_profit
            position_return = (
                position_profit / position_cost * Decimal("100")
                if position_cost > 0
                else None
            )
        available = (
            min(quantity, account_position["available_quantity"])
            if account_position
            else quantity
        )
        position_rows.append(
            {
                "code": code,
                "name": names.get(code),
                "quantity": str(quantity),
                "available_quantity": str(available),
                "average_price": str(average_cost),
                "market_price": market_price_value,
                "cost_basis": str(average_cost * quantity),
                "market_value": str(market_value) if market_value is not None else None,
                "unrealized_profit": (
                    str(position_profit) if position_profit is not None else None
                ),
                "return_percent": (
                    str(position_return.quantize(Decimal("0.01")))
                    if position_return is not None
                    else None
                ),
            }
        )

    matched_sales = len(sale_profits)
    winning_sales = sum(profit > 0 for profit in sale_profits.values())
    total_profit = realized + unrealized
    return_rate = (
        total_profit / bought * Decimal("100") if bought > 0 else None
    )
    return {
        "estimated_profit": str(total_profit),
        "estimated_return_percent": (
            str(return_rate.quantize(Decimal("0.01")))
            if return_rate is not None
            else None
        ),
        "realized_profit": str(realized),
        "unrealized_profit": str(unrealized),
        "filled_buy_amount": str(bought),
        "filled_sell_amount": str(sold),
        "commission": str(total_commission),
        "tax": str(total_tax),
        "total_costs": str(total_commission + total_tax),
        "matched_sales": matched_sales,
        "winning_sales": winning_sales,
        "win_rate": (
            round(winning_sales / matched_sales * 100, 1)
            if matched_sales
            else None
        ),
        "unpriced_codes": unpriced_codes,
        "positions": position_rows,
        "includes_fees": bool(cost_flags) and all(cost_flags),
        "costs_complete": bool(cost_flags) and all(cost_flags),
    }


def _strategy_trade_history(
    orders: list[dict[str, object]],
    *,
    limit: int = 200,
    instrument_names: dict[str, str] | None = None,
    accounting_orders: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Return presentation-safe strategy order history without provider secrets."""

    names = instrument_names or {}
    cost_orders = orders if accounting_orders is None else accounting_orders
    sale_results = _matched_sale_results(cost_orders)
    sales_by_order: dict[str, dict[str, object]] = {}
    for row in cost_orders:
        sale = sale_results.get(id(row))
        if sale is None:
            continue
        order_id = str(row.get("id") or id(row))
        aggregate = sales_by_order.setdefault(order_id, {
            "matched_quantity": "0", "cost_basis": "0", "net_proceeds": "0",
            "realized_profit": "0", "costs_complete": True,
        })
        for key in ("matched_quantity", "cost_basis", "net_proceeds", "realized_profit"):
            aggregate[key] = str(Decimal(str(aggregate[key])) + Decimal(str(sale[key])))
        aggregate["costs_complete"] = bool(aggregate["costs_complete"]) and bool(sale["costs_complete"])
        cost = Decimal(str(aggregate["cost_basis"]))
        aggregate["return_percent"] = (
            str((Decimal(str(aggregate["realized_profit"])) / cost * 100).quantize(Decimal("0.01")))
            if cost > 0 else None
        )
    history_orders = list(orders)
    if accounting_orders is not None:
        own_ids = {row.get("id") for row in orders}
        external_sales: dict[str, dict[str, object]] = {}
        for row in sorted(accounting_orders, key=_accounting_order_key):
            if (row.get("id") in own_ids or not row.get("execution_strategy_id")
                or row.get("execution_strategy_id") == row.get("attributed_strategy_id")):
                continue
            order_id = str(row["id"])
            combined = external_sales.get(order_id)
            if combined is None:
                external_sales[order_id] = {**row, "request": dict(row["request"])}
                continue
            previous = Decimal(str(combined["filled_quantity"]))
            quantity = Decimal(str(row["filled_quantity"]))
            total = previous + quantity
            combined["average_fill_price"] = str((
                previous * Decimal(str(combined.get("average_fill_price") or "0"))
                + quantity * Decimal(str(row.get("average_fill_price") or "0"))
            ) / total)
            combined["filled_quantity"] = str(total)
            combined["request"]["quantity"] = str(total)
            for key in ("commission", "tax"):
                combined[key] = str(Decimal(str(combined.get(key) or "0")) + Decimal(str(row.get(key) or "0")))
            combined["accounting_sequence"] = row.get("accounting_sequence", 0)
        for row in external_sales.values():
            row["message"] = (
                f"계좌 잔고 대상 전략({row['execution_strategy_id']})의 매도로 "
                f"이 전략 보유분 {row['filled_quantity']}주를 정리했습니다."
            )
        history_orders.extend(external_sales.values())
        sequence_by_order: dict[object, int] = {}
        for row in accounting_orders:
            order_id = row.get("id")
            sequence_by_order[order_id] = max(
                sequence_by_order.get(order_id, -1),
                int(row.get("accounting_sequence", 0)),
            )
        history_orders.sort(
            key=lambda row: (
                str(row.get("created_at") or ""),
                sequence_by_order.get(row.get("id"), -1),
            ), reverse=True,
        )
    rows: list[dict[str, object]] = []
    for row in history_orders[:limit]:
        request = dict(row.get("request") or {})
        code = str(request.get("code") or "")
        sale = sales_by_order.get(str(row.get("id") or id(row)), {})
        rows.append(
            {
                "id": row.get("id"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "code": code,
                "name": names.get(code),
                "side": request.get("side"),
                "order_type": request.get("order_type"),
                "sell_position_scope": request.get("sell_position_scope", "STRATEGY"),
                "execution_strategy_id": row.get("execution_strategy_id", row.get("strategy_id")),
                "quantity": request.get("quantity"),
                "filled_quantity": row.get("filled_quantity"),
                "average_fill_price": row.get("average_fill_price"),
                "status": row.get("status"),
                "commission": row.get("commission"),
                "tax": row.get("tax"),
                "matched_quantity": sale.get("matched_quantity"),
                "cost_basis": sale.get("cost_basis"),
                "net_proceeds": sale.get("net_proceeds"),
                "realized_profit": sale.get("realized_profit"),
                "return_percent": sale.get("return_percent"),
                "profit_costs_complete": sale.get("costs_complete"),
                "message": row.get("message"),
            }
        )
    return rows


def _unfilled_buy_reservations(rows: list[dict[str, object]]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        request = dict(row.get("request") or {})
        if str(request.get("side") or "").upper() != "BUY":
            continue
        requested = Decimal(str(request.get("quantity") or "0"))
        if requested <= 0:
            continue
        filled = Decimal(str(row.get("filled_quantity") or "0"))
        remaining = max(Decimal("0"), requested - filled)
        reserved = Decimal(str(request.get("reserved_amount") or "0"))
        total += reserved * remaining / requested
    return total


def create_app(paths: WorkspacePaths | None = None) -> FastAPI:
    workspace = paths or WorkspacePaths.discover()
    instance_id = uuid4().hex
    instance_started_at = datetime.now(SEOUL)
    sessions = StrategySessionRegistry()
    scheduler = RuntimeScheduler()
    strategy_events = StrategyEventBus()
    pending_strategy_events: dict[str, deque[object]] = defaultdict(deque)
    runtime_store: RuntimeStore | None = None
    order_service: OrderService | None = None
    order_reconciler: OrderReconciler | None = None
    dashboard_cache: dict[str, object] | None = None
    dashboard_cache_until = 0.0
    dashboard_lock = asyncio.Lock()

    settings = SettingsStore(workspace)
    connection_runtime = ConnectionRuntime(settings)
    realtime = RealtimeHub(connection_runtime, strategy_events)
    instrument_catalog = InstrumentCatalog(workspace.data / "instrument-catalog.json")

    async def handle_strategy_exit(strategy_name: str) -> None:
        # Keep a bounded, allowlisted failure summary after the in-memory logs vanish.
        if runtime_store is not None:
            try:
                await asyncio.to_thread(
                    runtime_store.record,
                    source=strategy_name,
                    event_type="strategy.process_exit",
                    message="전략 프로세스가 종료되었습니다.",
                    details=manager.exit_diagnostics(strategy_name),
                )
            except Exception:
                # Diagnostics are best effort; failure must not prevent cleanup.
                logging.getLogger(__name__).warning("전략 종료 진단 기록을 저장하지 못했습니다.")
        await scheduler.unregister(strategy_name)
        await realtime.unsubscribe_strategy(strategy_name)
        await strategy_events.unregister(strategy_name)
        pending_strategy_events.pop(strategy_name, None)

    manager = StrategyManager(
        workspace,
        sessions,
        router_url=os.environ.get("TRADE_ROUTER_URL", "http://127.0.0.1:8890"),
        on_exit=handle_strategy_exit,
    )

    async def refresh_block_runtime(name: str) -> None:
        """Compile saved block settings with the current server-owned runtime."""

        if manager.strategy_type(name) != "block":
            return
        source = manager.read(name)
        display_name = name[:-3] if name.lower().endswith(".py") else name
        package = block_package_from_source(display_name, source)
        compiled = compile_block_strategy(
            package.settings,
            template=package.template,
            layout=package.layout,
        )
        if compiled != source:
            await manager.save(name, compiled)

    async def publish_order_event(
        strategy_id: str,
        event: dict[str, object],
        strategy_name: str | None = None,
    ) -> None:
        names = [strategy_name] if strategy_name else sessions.strategy_names(strategy_id)
        await strategy_events.publish_many(names, dict(event))

    async def handle_account_notice(
        account_alias: str, notice: dict[str, object]
    ) -> None:
        if order_service is not None:
            await order_service.apply_realtime_notice(account_alias, dict(notice))

    realtime.set_account_event_handler(handle_account_notice)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        nonlocal runtime_store, order_service, order_reconciler
        restore_names = manager.consume_restart_checkpoint()
        restore_task: asyncio.Task[None] | None = None
        workspace.ensure()
        runtime_store = RuntimeStore(workspace.data / "runtime.sqlite3")
        order_service = OrderService(
            runtime_store,
            connection_runtime,
            event_publisher=publish_order_event,
        )
        order_reconciler = OrderReconciler(runtime_store, order_service)
        application.state.runtime_store = runtime_store
        application.state.order_service = order_service
        application.state.order_reconciler = order_reconciler
        await connection_runtime.restore_cached_statuses()
        await scheduler.start()
        await order_reconciler.start()
        if restore_names:
            async def restore_running_strategies() -> None:
                # The worker registers its manifest through this HTTP server,
                # so wait until FastAPI has completed application startup.
                await asyncio.sleep(0.5)
                restored: list[str] = []
                failed: dict[str, str] = {}
                for name in restore_names:
                    try:
                        await refresh_block_runtime(name)
                        await manager.start(name)
                        restored.append(name)
                    except Exception as exc:
                        failed[name] = f"{type(exc).__name__}: {exc}"
                if restored:
                    journal().record(
                        source="system",
                        event_type="strategy.restart_restore",
                        message=f"재시작 전 실행 중이던 전략 {len(restored)}개를 복구했습니다.",
                        details={"strategies": restored},
                    )
                if failed:
                    journal().record(
                        source="system",
                        event_type="strategy.restart_restore_error",
                        message="재시작 전 전략 일부를 복구하지 못했습니다.",
                        details={"failures": failed},
                    )

            restore_task = asyncio.create_task(
                restore_running_strategies(),
                name="trade-router-strategy-restart-restore",
            )
        yield
        order_service.close_submission_gate()
        if restore_task is not None:
            if not restore_task.done():
                restore_task.cancel()
            with suppress(asyncio.CancelledError):
                await restore_task
        await manager.shutdown()
        await order_reconciler.close()
        await realtime.close()
        await scheduler.close()
        await connection_runtime.close()
        runtime_store.close()
        runtime_store = None
        order_service = None
        order_reconciler = None

    app = FastAPI(title="Trade Router", version=__version__, lifespan=lifespan)
    app.state.workspace = workspace
    app.state.strategies = manager
    app.state.settings = settings
    app.state.connections = connection_runtime
    app.state.sessions = sessions
    app.state.scheduler = scheduler
    app.state.strategy_events = strategy_events
    app.state.realtime = realtime
    app.state.instrument_catalog = instrument_catalog
    app.state.runtime_store = None
    app.state.order_service = None
    app.state.order_reconciler = None

    @app.exception_handler(RuntimeStoreRestartRequired)
    async def restart_after_runtime_store_failure(
        _request: Request,
        exc: RuntimeStoreRestartRequired,
    ) -> JSONResponse:
        control = getattr(app.state, "process_control", None)
        if callable(control):
            asyncio.get_running_loop().call_later(0.25, control, "restart")
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    def journal() -> RuntimeStore:
        if runtime_store is None:
            raise RuntimeError("Trade Router Runtime이 아직 시작되지 않았습니다.")
        return runtime_store

    def order_engine() -> OrderService:
        if order_service is None:
            raise RuntimeError("Trade Router 주문 엔진이 아직 시작되지 않았습니다.")
        return order_service

    def record_service_result(
        session: StrategySession,
        *,
        service: str,
        operation: str,
        outcome: str,
        message: str,
        started_at: float,
        details: dict[str, object] | None = None,
    ) -> None:
        safe_details = dict(details or {})
        safe_details["duration_ms"] = round(
            (time.perf_counter() - started_at) * 1_000, 1
        )
        journal().record(
            source=session.strategy_name,
            event_type=f"service.{service}.{operation}.{outcome}",
            message=message,
            details=safe_details,
        )

    async def dashboard_snapshot(*, force: bool = False) -> dict[str, object]:
        """Build one shared operational snapshot for the Studio dashboard."""

        nonlocal dashboard_cache, dashboard_cache_until
        now_monotonic = time.monotonic()
        if not force and dashboard_cache is not None and now_monotonic < dashboard_cache_until:
            return dashboard_cache

        async with dashboard_lock:
            now_monotonic = time.monotonic()
            if not force and dashboard_cache is not None and now_monotonic < dashboard_cache_until:
                return dashboard_cache

            account_settings = settings.list_accounts()
            connections = settings.list_connections()
            permissions = {
                (item["scope_type"], item["scope_id"]): item
                for item in order_engine().permission_snapshot()
            }
            request_state = await connection_runtime.request_status()
            business_date = datetime.now(SEOUL).date().isoformat()

            # Multiple aliases may refer to the same real broker account. Fetch
            # and total that account once while keeping every alias visible.
            groups: dict[
                tuple[str, str, str, str, str], list[dict[str, object]]
            ] = {}
            for account in account_settings:
                try:
                    account_number = settings.get_account_number(str(account["id"]))
                except SettingsError:
                    account_number = str(account["id"])
                key = (
                    str(account.get("connection_id") or ""),
                    str(account.get("provider") or ""),
                    str(account.get("target") or ""),
                    account_number,
                    str(account.get("product_code") or ""),
                )
                groups.setdefault(key, []).append(account)

            async def fetch_group(
                members: list[dict[str, object]],
            ) -> tuple[object | None, Exception | None]:
                first = members[0]
                try:
                    capabilities = connection_runtime.capabilities(str(first["alias"]))
                    if not capabilities.accounts:
                        raise RouterError(
                            RouterErrorCode.UNSUPPORTED_CAPABILITY,
                            "이 증권사의 잔고 조회는 아직 지원하지 않습니다.",
                        )
                    return await connection_runtime.balance(str(first["alias"])), None
                except Exception as exc:  # surfaced per account; other accounts continue
                    return None, exc

            group_items = list(groups.items())
            group_results = await asyncio.gather(
                *(fetch_group(members) for _, members in group_items)
            )
            result_by_group = {
                key: result for (key, _), result in zip(group_items, group_results, strict=True)
            }

            unresolved_rows = journal().unresolved_orders()
            known_names = journal().instrument_names()
            known_names.update(_workspace_instrument_names(workspace))
            orders_by_account = {
                str(account["alias"]): journal().orders(
                    account_alias=str(account["alias"]),
                    limit=1_000,
                )
                for account in account_settings
            }

            # Older releases stored normalized order codes but not their names.
            # Resolve only the rows that can be shown in the account/strategy
            # history, then cache the result so subsequent snapshots are local.
            lookup_connection = next(
                (
                    str(connection["id"])
                    for connection in connections
                    if connection.get("provider") == "kis"
                    and connection.get("target") == "LIVE"
                ),
                next(
                    (
                        str(connection["id"])
                        for connection in connections
                        if connection.get("provider") == "kis"
                    ),
                    None,
                ),
            )
            missing_name_codes = sorted(
                {
                    str(dict(order.get("request") or {}).get("code") or "")
                    for rows in orders_by_account.values()
                    for order in rows[:200]
                    if str(dict(order.get("request") or {}).get("code") or "")
                    not in known_names
                }
                - {""}
            )
            if lookup_connection and missing_name_codes:
                resolved_values = await asyncio.gather(
                    *(
                        connection_runtime.instrument_name(lookup_connection, code)
                        for code in missing_name_codes
                    ),
                    return_exceptions=True,
                )
                resolved_names = {
                    code: value
                    for code, value in zip(
                        missing_name_codes,
                        resolved_values,
                        strict=True,
                    )
                    if isinstance(value, str) and value.strip()
                }
                if resolved_names:
                    known_names.update(resolved_names)
                    journal().remember_instrument_names(
                        resolved_names,
                        source="kis:stock-info",
                    )
            account_rows: list[dict[str, object]] = []
            total_assets = Decimal("0")
            total_cash = Decimal("0")
            total_profit = Decimal("0")
            environment_totals = {
                "LIVE": {"assets": Decimal("0"), "cash": Decimal("0"), "profit": Decimal("0")},
                "PAPER": {"assets": Decimal("0"), "cash": Decimal("0"), "profit": Decimal("0")},
            }
            successful_groups = 0
            successful_connections: set[str] = set()
            valued_account_keys: set[tuple[str, str, str, str]] = set()

            for key, members in group_items:
                balance, error = result_by_group[key]
                market_value = Decimal("0")
                cost_basis = Decimal("0")
                if balance is not None:
                    broker_names = {
                        position.instrument.code: position.name
                        for position in balance.positions
                        if position.name
                    }
                    if broker_names:
                        known_names.update(broker_names)
                        journal().remember_instrument_names(
                            broker_names,
                            source=str(key[1]),
                        )
                    for position in balance.positions:
                        price = position.market_price
                        if price is None or price <= 0:
                            price = position.average_price
                        market_value += position.quantity * price
                        cost_basis += position.quantity * position.average_price
                    profit = market_value - cost_basis
                    account_key = (key[1], key[2], key[3], key[4])
                    if account_key not in valued_account_keys:
                        valued_account_keys.add(account_key)
                        total_assets += balance.total_value
                        total_cash += balance.cash
                        total_profit += profit
                        target_totals = environment_totals.get(key[2])
                        if target_totals is not None:
                            target_totals["assets"] += balance.total_value
                            target_totals["cash"] += balance.cash
                            target_totals["profit"] += profit
                        successful_groups += 1
                    successful_connections.update(
                        str(member["connection_id"]) for member in members
                    )
                else:
                    profit = Decimal("0")

                for account in members:
                    alias = str(account["alias"])
                    open_count = sum(
                        1 for row in unresolved_rows if row["account_alias"] == alias
                    )
                    permission = permissions.get(("account", alias), {})
                    limits = dict(account.get("risk_limits") or {})
                    if error is None:
                        state = "ok"
                        message = "잔고를 확인했습니다."
                    elif isinstance(error, RouterError) and error.code is RouterErrorCode.UNSUPPORTED_CAPABILITY:
                        state = "unsupported"
                        message = error.message
                    else:
                        state = "error"
                        message = (
                            error.message if isinstance(error, RouterError) else str(error)
                        ) or "잔고를 조회하지 못했습니다."
                    position_rows = (
                        [
                            {
                                "code": position.instrument.code,
                                "name": position.name
                                or known_names.get(position.instrument.code),
                                "quantity": str(position.quantity),
                                "available_quantity": str(position.available_quantity),
                                "average_price": str(position.average_price),
                                "market_price": (
                                    str(position.market_price)
                                    if position.market_price is not None
                                    else None
                                ),
                                "cost_basis": str(
                                    position.quantity * position.average_price
                                ),
                                "market_value": str(
                                    position.quantity
                                    * (position.market_price or position.average_price)
                                ),
                                "unrealized_profit": str(
                                    position.quantity
                                    * (
                                        (position.market_price or position.average_price)
                                        - position.average_price
                                    )
                                ),
                                "return_percent": (
                                    str(
                                        (
                                            (
                                                (position.market_price or position.average_price)
                                                - position.average_price
                                            )
                                            / position.average_price
                                            * Decimal("100")
                                        ).quantize(Decimal("0.01"))
                                    )
                                    if position.average_price > 0
                                    else None
                                ),
                            }
                            for position in balance.positions
                        ]
                        if balance is not None
                        else []
                    )
                    account_orders = orders_by_account.get(alias, [])
                    account_snapshot = {
                        "state": state,
                        "positions": position_rows,
                    }
                    account_rows.append(
                        {
                            "alias": alias,
                            "connection_id": account.get("connection_id"),
                            "provider": account.get("provider"),
                            "target": account.get("target"),
                            "connection_label": account.get("connection_label"),
                            "masked_account": account.get("masked_account"),
                            "state": state,
                            "message": message,
                            "total_value": str(balance.total_value) if balance is not None else None,
                            "cash": str(balance.cash) if balance is not None else None,
                            "market_value": str(market_value) if balance is not None else None,
                            "cost_basis": str(cost_basis) if balance is not None else None,
                            "unrealized_profit": str(profit) if balance is not None else None,
                            "position_count": len(balance.positions) if balance is not None else None,
                            "positions": position_rows,
                            "trade_performance": _strategy_performance(
                                account_orders,
                                account_snapshot,
                                known_names,
                            ),
                            "trade_history": _strategy_trade_history(
                                account_orders,
                                instrument_names=known_names,
                            ),
                            "as_of": (
                                balance.as_of.isoformat()
                                if balance is not None and balance.as_of is not None
                                else None
                            ),
                            "order_permission": {
                                "armed": bool(permission.get("armed")),
                                "reconciled": bool(permission.get("reconciled")),
                            },
                            "limits": {
                                "max_order_amount": _usage_value(None, limits.get("max_order_amount")),
                                "max_daily_buy_amount": _usage_value(
                                    journal().account_daily_buy_reserved(alias, business_date),
                                    limits.get("max_daily_buy_amount"),
                                ),
                                "max_daily_buy_count": _usage_value(
                                    journal().account_daily_buy_count(alias, business_date),
                                    limits.get("max_daily_buy_count"),
                                ),
                                "max_exposure": _usage_value(
                                    market_value if balance is not None else None,
                                    limits.get("max_exposure"),
                                ),
                                "max_open_orders": _usage_value(
                                    open_count, limits.get("max_open_orders")
                                ),
                            },
                        }
                    )

            rate_limits = {
                str(item["key"]): item for item in request_state.get("limits", [])
            }
            websocket_limits = {
                str(item["scope"]): item
                for item in request_state.get("websocket", [])
            }
            service_rows: list[dict[str, object]] = []
            for connection in connections:
                status_value = connection_runtime.status(str(connection["id"]))
                rest_policy = dict(connection.get("rest_rate_limit") or {})
                websocket_policy = dict(connection.get("websocket_rate_limit") or {})
                try:
                    scope = settings.rate_limit_identity(str(connection["id"]))
                except SettingsError as exc:
                    scope = ""
                    status_value = {
                        **status_value,
                        "state": "error",
                        "message": str(exc),
                    }
                rest_bucket = rate_limits.get(f"{scope}:rest-global") if scope else None
                websocket_bucket = websocket_limits.get(scope) if scope else None
                max_sessions = int(websocket_policy.get("operating_max_sessions_per_key") or 0)
                per_session = int(websocket_policy.get("max_subscriptions_per_session") or 0)
                state = str(status_value.get("state") or "unchecked")
                message = str(status_value.get("message") or "연결 상태를 확인하지 않았습니다.")
                if connection["id"] in successful_connections:
                    state = "connected"
                    message = "계좌 조회와 인증이 정상입니다."
                service_rows.append(
                    {
                        "id": connection["id"],
                        "label": connection["label"],
                        "provider": connection["provider"],
                        "target": connection["target"],
                        "state": state,
                        "message": message,
                        "checked_at": status_value.get("checked_at"),
                        "expires_at": status_value.get("expires_at"),
                        "rest": {
                            "requests_per_second": rest_policy.get("requests_per_second"),
                            "minimum_interval_ms": rest_policy.get("minimum_interval_ms"),
                            "available": (
                                rest_bucket.get("available") if rest_bucket is not None else 1
                            ),
                            "capacity": (
                                rest_bucket.get("capacity") if rest_bucket is not None else 1
                            ),
                        },
                        "websocket": {
                            "sessions": (
                                websocket_bucket.get("sessions") if websocket_bucket else 0
                            ),
                            "max_sessions": max_sessions,
                            "subscriptions": (
                                websocket_bucket.get("subscriptions") if websocket_bucket else 0
                            ),
                            "max_subscriptions": max_sessions * per_session,
                            "control_messages_per_second": websocket_policy.get(
                                "control_messages_per_second"
                            ),
                        },
                    }
                )

            activity = journal().service_results(business_date)
            service_events = journal().service_events(business_date)
            result_rows = _service_result_rows(activity, service_events)
            result_map = {item["id"]: item for item in result_rows}

            account_result = result_map["account"]
            account_success = sum(1 for item in account_rows if item["state"] == "ok")
            account_errors = sum(1 for item in account_rows if item["state"] == "error")
            account_unsupported = sum(
                1 for item in account_rows if item["state"] == "unsupported"
            )
            account_result.update(
                {
                    "state": "error" if account_errors else "ok" if account_success else "idle",
                    "total": len(account_rows),
                    "success": account_success,
                    "error": account_errors,
                    "snapshot_success": account_success,
                    "snapshot_error": account_errors,
                    "snapshot_unsupported": account_unsupported,
                    "last_message": (
                        f"잔고 정상 {account_success} · 오류 {account_errors} · 미지원 {account_unsupported}"
                    ),
                }
            )
            account_result["events"] = [
                {
                    "id": f"account-{index}",
                    "occurred_at": item.get("as_of") or datetime.now(SEOUL).isoformat(),
                    "source": item.get("alias"),
                    "operation": "balance",
                    "outcome": "error" if item["state"] == "error" else "success",
                    "message": item.get("message") or "계좌 상태를 확인했습니다.",
                    "duration_ms": None,
                    "details": {
                        "account": item.get("alias"),
                        "state": item.get("state"),
                        "total_value": item.get("total_value"),
                    },
                }
                for index, item in enumerate(account_rows)
            ]

            schedule_snapshot = await scheduler.snapshot()
            schedule_count = sum(
                len(item.get("schedules", [])) for item in schedule_snapshot
            )
            queued_schedule_events = sum(
                int(item.get("queued_events", 0)) for item in schedule_snapshot
            )
            result_map["scheduler"].update(
                {
                    "active_schedules": schedule_count,
                    "queued_events": queued_schedule_events,
                }
            )

            all_orders = journal().orders(limit=1_000)
            today_ledger = [
                row
                for row in all_orders
                if str(row.get("created_at", "")).startswith(business_date)
            ]
            accepted_statuses = {
                OrderStatus.ACKNOWLEDGED.value,
                OrderStatus.PARTIALLY_FILLED.value,
                OrderStatus.FILLED.value,
                OrderStatus.CANCEL_PENDING.value,
                OrderStatus.CANCELLED.value,
            }
            failed_statuses = {
                OrderStatus.REJECTED.value,
                OrderStatus.UNKNOWN_RECONCILING.value,
            }
            order_result = result_map["order"]
            if today_ledger and not activity.get("order"):
                latest_order = max(today_ledger, key=lambda item: item["updated_at"])
                order_result.update(
                    {
                        "state": (
                            "error"
                            if latest_order["status"] in failed_statuses
                            else "ok"
                        ),
                        "total": len(today_ledger),
                        "success": sum(
                            1 for item in today_ledger if item["status"] in accepted_statuses
                        ),
                        "error": sum(
                            1 for item in today_ledger if item["status"] in failed_statuses
                        ),
                        "last_at": latest_order["updated_at"],
                        "last_outcome": (
                            "error"
                            if latest_order["status"] in failed_statuses
                            else "success"
                        ),
                        "last_message": latest_order.get("message")
                        or f"최근 주문 상태 {latest_order['status']}",
                    }
                )
            if today_ledger and not order_result.get("events"):
                order_result["events"] = [
                    {
                        "id": item["id"],
                        "occurred_at": item["updated_at"],
                        "source": item["strategy_id"],
                        "operation": "submit",
                        "outcome": (
                            "error" if item["status"] in failed_statuses else "success"
                        ),
                        "message": item.get("message")
                        or f"{item['request'].get('code', '')} {item['status']}",
                        "duration_ms": None,
                        "details": {
                            "code": item["request"].get("code"),
                            "side": item["request"].get("side"),
                            "status": item["status"],
                        },
                    }
                    for item in today_ledger[:20]
                ]

            queue = dict(request_state.get("queue", {}))
            result_rows.append(
                {
                    "id": "router",
                    "label": "요청 라우터",
                    "state": (
                        "busy"
                        if int(queue.get("queued", 0)) or int(queue.get("inflight", 0))
                        else "ok"
                    ),
                    "total": int(queue.get("completed", 0)),
                    "success": int(queue.get("completed", 0)),
                    "error": 0,
                    "average_duration_ms": None,
                    "last_at": None,
                    "last_operation": "queue",
                    "last_outcome": "success",
                    "last_message": (
                        f"대기 {queue.get('queued', 0)} · 처리 {queue.get('inflight', 0)} · "
                        f"중복 합치기 {queue.get('deduplicated', 0)} · 캐시 {queue.get('cache_hits', 0)}"
                    ),
                    "last_details": queue,
                }
            )

            generated_at = datetime.now(SEOUL).isoformat()
            dashboard_cache = {
                "generated_at": generated_at,
                "summary": {
                    "total_assets": str(total_assets),
                    "cash": str(total_cash),
                    "unrealized_profit": str(total_profit),
                    "live_assets": str(environment_totals["LIVE"]["assets"]),
                    "live_cash": str(environment_totals["LIVE"]["cash"]),
                    "live_unrealized_profit": str(environment_totals["LIVE"]["profit"]),
                    "paper_assets": str(environment_totals["PAPER"]["assets"]),
                    "paper_cash": str(environment_totals["PAPER"]["cash"]),
                    "paper_unrealized_profit": str(environment_totals["PAPER"]["profit"]),
                    "account_count": len(account_settings),
                    "valued_accounts": successful_groups,
                    "unresolved_orders": len(unresolved_rows),
                },
                "accounts": account_rows,
                "services": service_rows,
                "service_results": result_rows,
                "queue": queue,
            }
            dashboard_cache_until = time.monotonic() + 15.0
            return dashboard_cache

    async def chart_connection_rows() -> list[dict[str, object]]:
        """Return chart-capable connections ordered by live capacity.

        Chart reads stay inside the central request router.  The ordering only
        chooses a connection; token reuse, request pacing and WebSocket quotas
        are still enforced by ConnectionRuntime and RealtimeHub.
        """

        request_state = await connection_runtime.request_status()
        buckets = {str(item["key"]): item for item in request_state.get("limits", [])}
        rows: list[dict[str, object]] = []
        for connection in settings.list_connections():
            connection_id = str(connection["id"])
            capabilities = connection_runtime.capabilities(connection_id)
            if not capabilities.quotes or not capabilities.bars:
                continue
            try:
                scope = settings.rate_limit_identity(connection_id)
            except SettingsError:
                scope = ""
            bucket = buckets.get(f"{scope}:rest-global") if scope else None
            capacity = float(bucket.get("capacity") or 1) if bucket else 1.0
            available = float(bucket.get("available") or capacity) if bucket else capacity
            connection_status = connection_runtime.status(connection_id)
            rows.append(
                {
                    "id": connection_id,
                    "label": connection.get("label") or connection_id,
                    "provider": connection.get("provider"),
                    "target": connection.get("target"),
                    "state": connection_status.get("state"),
                    "message": connection_status.get("message"),
                    "realtime": capabilities.realtime,
                    "venues": [venue.value for venue in capabilities.venues],
                    "available": available,
                    "capacity": capacity,
                    "load_ratio": max(0.0, min(1.0, available / capacity)),
                }
            )
        rows.sort(
            key=lambda item: (
                str(item["state"]) == "connected",
                bool(item["realtime"]),
                float(item["load_ratio"]),
                str(item["target"]) == "LIVE",
            ),
            reverse=True,
        )
        return rows

    async def chart_connection(
        reference: str | None,
        venue: Venue | None = None,
    ) -> dict[str, object]:
        rows = await chart_connection_rows()
        if venue is not None:
            rows = [item for item in rows if venue.value in item["venues"]]
        if not rows:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "현재가와 봉 조회를 모두 지원하는 API 연결이 없습니다.",
            )
        normalized = (reference or "AUTO").strip()
        if normalized.upper() == "AUTO":
            return rows[0]
        selected = next(
            (
                item
                for item in rows
                if normalized in {str(item["id"]), str(item["label"])}
            ),
            None,
        )
        if selected is None:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "선택한 API 연결에서는 차트 조회를 지원하지 않습니다.",
            )
        return selected

    def chart_http_error(exc: Exception) -> HTTPException:
        if isinstance(exc, ValueError):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, SettingsError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, RouterError):
            status_code = (
                422
                if exc.code in {
                    RouterErrorCode.INVALID_REQUEST,
                    RouterErrorCode.UNSUPPORTED_CAPABILITY,
                }
                else 429 if exc.code is RouterErrorCode.RATE_LIMITED else 502
            )
            return HTTPException(status_code=status_code, detail=exc.as_dict())
        return HTTPException(status_code=502, detail=str(exc))

    def quote_payload(quote: object) -> dict[str, object]:
        return {
            "instrument": {
                "code": quote.instrument.code,
                "country": quote.instrument.country,
                "asset": quote.instrument.asset.value,
            },
            "price": str(quote.price),
            "venue": quote.venue.value,
            "provider": quote.provider,
            "source_alias": quote.source_alias,
            "market_time": quote.market_time.isoformat(),
            "received_at": quote.received_at.isoformat(),
            "correlation_id": quote.correlation_id,
            "delayed": quote.delayed,
            "stale": quote.stale,
            "connection_generation": quote.connection_generation,
        }

    def bar_payload(bar: object) -> dict[str, object]:
        return {
            "instrument": {
                "code": bar.instrument.code,
                "country": bar.instrument.country,
                "asset": bar.instrument.asset.value,
            },
            "interval": bar.interval,
            "open_time": bar.open_time.isoformat(),
            "close_time": bar.close_time.isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
            "venue": bar.venue.value,
            "provider": bar.provider,
            "source_alias": bar.source_alias,
            "correlation_id": bar.correlation_id,
            "complete": bar.complete,
            "received_at": bar.received_at.isoformat() if bar.received_at else None,
            "connection_generation": bar.connection_generation,
            "stale": bar.stale,
        }

    async def require_strategy_session(
        authorization: str | None = Header(default=None),
    ) -> StrategySession:
        scheme, _, token = (authorization or "").partition(" ")
        session = sessions.authenticate(token) if scheme.casefold() == "bearer" else None
        if session is None:
            raise HTTPException(status_code=401, detail="유효한 전략 세션이 필요합니다.")
        return session

    @app.middleware("http")
    async def prevent_api_caching(request: Request, call_next):
        response = await call_next(request)
        if (
            request.url.path == "/"
            or request.url.path.startswith("/api/")
            or request.url.path.startswith("/static/")
        ):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

    static_root = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_root), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_root / "index.html")

    @app.get("/api/status")
    async def status() -> dict[str, object]:
        strategies = manager.list()
        registered_connections = settings.list_connections()
        accounts = settings.list_accounts()
        return {
            "version": __version__,
            "instance_id": instance_id,
            "instance_started_at": instance_started_at.isoformat(),
            "workspace": str(workspace.root),
            "server_time": datetime.now(SEOUL).isoformat(),
            "running": sum(1 for item in strategies if item["running"]),
            "strategy_count": len(strategies),
            "broker_connections": connection_runtime.connected_count(),
            "registered_apis": len(registered_connections),
            "registered_accounts": len(accounts),
            "unresolved_orders": len(journal().unresolved_orders()),
            "database_recovery": journal().recovery,
        }

    @app.get("/api/dashboard")
    async def dashboard(
        refresh: bool = Query(default=False),
    ) -> dict[str, object]:
        return await dashboard_snapshot(force=refresh)

    @app.get("/api/chart/sources")
    async def chart_sources() -> dict[str, object]:
        rows = await chart_connection_rows()
        return {
            "sources": rows,
            "automatic": rows[0]["id"] if rows else None,
        }

    @app.get("/api/chart/instruments")
    async def chart_instruments(
        q: str = Query(default="", max_length=80),
        source: str = Query(default="AUTO", max_length=160),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> dict[str, object]:
        query = q.strip()
        names = journal().instrument_names()
        names.update(_workspace_instrument_names(workspace))
        normalized = query.casefold()
        rows = [
            {"code": code, "name": name}
            for code, name in names.items()
            if not query
            or normalized in code.casefold()
            or normalized in str(name).casefold()
        ]
        rows.sort(
            key=lambda item: (
                0 if str(item["code"]).casefold().startswith(normalized) else 1,
                0 if str(item["name"]).casefold().startswith(normalized) else 1,
                str(item["name"]),
            )
        )
        catalog = app.state.instrument_catalog
        catalog_rows = await catalog.search(query, limit=limit)
        if catalog_rows:
            catalog_names = {
                str(item["code"]): str(item["name"])
                for item in catalog_rows
            }
            names.update(catalog_names)
            journal().remember_instrument_names(
                catalog_names,
                source="instrument-master",
            )
            rows.extend(
                {"code": item["code"], "name": item["name"]}
                for item in catalog_rows
            )
        code_query = query.upper()
        if (
            code_query
            and code_query.isascii()
            and code_query.isalnum()
            and len(code_query) <= 24
            and code_query not in names
        ):
            try:
                selected = await chart_connection(source)
                resolved = await connection_runtime.instrument_name(
                    str(selected["id"]), code_query
                )
                if resolved:
                    journal().remember_instrument_names({code_query: resolved})
                    rows.insert(0, {"code": code_query, "name": resolved})
                elif code_query.isdigit() and len(code_query) == 6:
                    rows.insert(0, {"code": code_query, "name": code_query})
            except (SettingsError, RouterError):
                if code_query.isdigit() and len(code_query) == 6:
                    rows.insert(0, {"code": code_query, "name": code_query})
        unique: list[dict[str, object]] = []
        seen: set[str] = set()
        for row in rows:
            code = str(row["code"])
            if code in seen:
                continue
            seen.add(code)
            unique.append(row)
        return {"instruments": unique[:limit]}

    @app.get("/api/chart/data")
    async def chart_data(
        code: str = Query(min_length=1, max_length=24),
        interval: str = Query(default="3m", max_length=16),
        venue: str = Query(default="INTEGRATED", max_length=16),
        source: str = Query(default="AUTO", max_length=160),
        count: int = Query(default=180, ge=20, le=400),
    ) -> dict[str, object]:
        try:
            instrument = InstrumentId(code)
            market_venue = Venue(venue.upper())
            selected = await chart_connection(source, market_venue)
            selected_id = str(selected["id"])
            bars, quote = await asyncio.gather(
                connection_runtime.bars(
                    selected_id,
                    instrument.code,
                    interval,
                    count=count,
                    venue=market_venue,
                    source_alias=str(selected["label"]),
                ),
                connection_runtime.quote(
                    selected_id,
                    instrument.code,
                    market_venue,
                    source_alias=str(selected["label"]),
                ),
            )
            known_name = journal().instrument_names().get(instrument.code)
            if not known_name:
                known_name = await app.state.instrument_catalog.name(instrument.code)
            if not known_name:
                known_name = await connection_runtime.instrument_name(
                    selected_id, instrument.code
                )
                if known_name:
                    journal().remember_instrument_names({instrument.code: known_name})
            return {
                "source": selected,
                "instrument": {"code": instrument.code, "name": known_name},
                "interval": interval,
                "venue": market_venue.value,
                "quote": quote_payload(quote),
                "bars": [bar_payload(bar) for bar in bars[-count:]],
            }
        except (ValueError, SettingsError, RouterError) as exc:
            raise chart_http_error(exc) from exc

    @app.get("/api/chart/bars")
    async def chart_bars(
        code: str = Query(min_length=1, max_length=24),
        interval: str = Query(default="3m", max_length=16),
        venue: str = Query(default="INTEGRATED", max_length=16),
        source: str = Query(default="AUTO", max_length=160),
        count: int = Query(default=180, ge=20, le=400),
        end: datetime | None = None,
    ) -> dict[str, object]:
        try:
            instrument = InstrumentId(code)
            market_venue = Venue(venue.upper())
            selected = await chart_connection(source, market_venue)
            bars = await connection_runtime.bars(
                str(selected["id"]),
                instrument.code,
                interval,
                count=count,
                venue=market_venue,
                source_alias=str(selected["label"]),
                end=end,
            )
            return {
                "source": selected,
                "instrument": {"code": instrument.code},
                "interval": interval,
                "venue": market_venue.value,
                "bars": [bar_payload(bar) for bar in bars[-count:]],
            }
        except (ValueError, SettingsError, RouterError) as exc:
            raise chart_http_error(exc) from exc

    @app.get("/api/chart/quote")
    async def chart_quote(
        code: str = Query(min_length=1, max_length=24),
        venue: str = Query(default="INTEGRATED", max_length=16),
        source: str = Query(default="AUTO", max_length=160),
    ) -> dict[str, object]:
        try:
            instrument = InstrumentId(code)
            market_venue = Venue(venue.upper())
            selected = await chart_connection(source, market_venue)
            quote = await connection_runtime.quote(
                str(selected["id"]),
                instrument.code,
                market_venue,
                source_alias=str(selected["label"]),
            )
            return {"source": selected, "quote": quote_payload(quote)}
        except (ValueError, SettingsError, RouterError) as exc:
            raise chart_http_error(exc) from exc

    @app.websocket("/api/chart/stream")
    async def chart_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        consumer = f"__chart__:{uuid4().hex}"
        subscription_id: str | None = None
        try:
            code = InstrumentId(websocket.query_params.get("code") or "").code
            venue = Venue(
                str(websocket.query_params.get("venue") or "INTEGRATED").upper()
            )
            selected = await chart_connection(
                websocket.query_params.get("source"), venue
            )
            if not selected.get("realtime"):
                raise RouterError(
                    RouterErrorCode.UNSUPPORTED_CAPABILITY,
                    "선택한 API 연결은 실시간 시세를 지원하지 않습니다.",
                )
            subscription = await realtime.subscribe(
                strategy_name=consumer,
                source=str(selected["id"]),
                source_alias=str(selected["label"]),
                kind="QUOTE",
                code=code,
                venue=venue,
            )
            subscription_id = str(subscription["id"])
            await websocket.send_json(
                {
                    "event_type": "subscribed",
                    "source": selected,
                    "subscription": subscription,
                }
            )
            while True:
                event = await strategy_events.next_event(consumer, timeout=20)
                await websocket.send_json(event or {"event_type": "keepalive"})
        except WebSocketDisconnect:
            pass
        except (ValueError, SettingsError, RouterError) as exc:
            detail = exc.as_dict() if isinstance(exc, RouterError) else str(exc)
            with suppress(Exception):
                await websocket.send_json({"event_type": "error", "detail": detail})
        finally:
            if subscription_id is not None:
                await realtime.unsubscribe(subscription_id, strategy_name=consumer)
            await realtime.unsubscribe_strategy(consumer)
            await strategy_events.unregister(consumer)

    @app.get("/api/settings/providers")
    async def provider_schemas() -> list[dict[str, object]]:
        return settings.providers()

    @app.get("/api/settings/connections")
    async def list_connections() -> list[dict[str, object]]:
        return settings.list_connections()

    @app.post("/api/settings/connections", status_code=201)
    async def register_connection(
        body: ConnectionRegistration,
        check: bool = Query(default=False),
    ) -> dict[str, object]:
        try:
            connection = settings.add_connection(
                provider=body.provider,
                target=body.target,
                label=body.label,
                app_key=body.app_key,
                app_secret=body.app_secret,
                realtime_user_id=body.realtime_user_id,
            )
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not check:
            return connection
        try:
            connection_status = await connection_runtime.check(connection["id"])
        except (SettingsError, RouterError):
            # Registration and authentication are separate outcomes. Keep the
            # encrypted credentials so the user can see and remove the failed
            # registration instead of losing the submitted form silently.
            connection_status = connection_runtime.status(connection["id"])
        return {**connection, "connection_status": connection_status}

    @app.delete("/api/settings/connections/{connection_id}", status_code=204)
    async def delete_connection(connection_id: str) -> None:
        try:
            settings.remove_connection(connection_id)
        except SettingsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/api/settings/connections/{connection_id}")
    async def update_connection(
        connection_id: str,
        body: ConnectionUpdate,
    ) -> dict[str, object]:
        try:
            existing = settings.get_connection(connection_id)
            identity_changed = (
                str(existing["provider"]) != body.provider.strip().lower()
                or str(existing["target"]) != body.target.strip().upper()
            )
            if identity_changed:
                active = []
                for strategy_name, manifest in sessions.manifests().items():
                    references = list(manifest.market_sources.values())
                    try:
                        account = settings.get_account(manifest.trading_account)
                    except SettingsError:
                        account = None
                    uses_account = account is not None and account["connection_id"] == connection_id
                    uses_market = False
                    for reference in references:
                        try:
                            resolved = settings.resolve_connection(reference)
                        except SettingsError:
                            continue
                        if resolved["id"] == connection_id:
                            uses_market = True
                            break
                    if uses_account or uses_market:
                        active.append(strategy_name)
                if active:
                    raise SettingsError(
                        "이 API를 사용하는 실행 중 전략을 먼저 중지하세요: "
                        + ", ".join(sorted(active))
                    )
                await connection_runtime.invalidate(connection_id)
            return settings.update_connection(
                connection_id,
                provider=body.provider,
                target=body.target,
                label=body.label,
                realtime_user_id=body.realtime_user_id,
            )
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/settings/connections/{connection_id}/check")
    async def check_connection(connection_id: str) -> dict[str, object]:
        try:
            return await connection_runtime.check(connection_id)
        except SettingsError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RouterError as exc:
            status_code = (
                501
                if exc.code is RouterErrorCode.UNSUPPORTED_CAPABILITY
                else 502
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from exc

    @app.get("/api/settings/connections/{connection_id}/status")
    async def connection_status(connection_id: str) -> dict[str, object]:
        try:
            settings.get_connection(connection_id)
        except SettingsError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return connection_runtime.status(connection_id)

    @app.get("/api/runtime/requests")
    async def runtime_requests() -> dict[str, object]:
        return {
            **await connection_runtime.request_status(),
            "realtime_connections": await realtime.snapshot(),
        }

    @app.get("/api/system/capabilities")
    async def system_capabilities() -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for connection in settings.list_connections():
            capabilities = connection_runtime.capabilities(connection["id"])
            result.append(
                {
                    "connection_id": connection["id"],
                    "connection_label": connection["label"],
                    "provider": capabilities.provider,
                    "target": connection["target"],
                    "quotes": capabilities.quotes,
                    "bars": capabilities.bars,
                    "orderbook": capabilities.orderbook,
                    "realtime": capabilities.realtime,
                    "accounts": capabilities.accounts,
                    "order_history": capabilities.order_history,
                    "cash_orders": capabilities.cash_orders,
                    "amend_orders": capabilities.amend_orders,
                    "cancel_orders": capabilities.cancel_orders,
                    "conditions": capabilities.conditions,
                    "rankings": capabilities.rankings,
                    "venues": [venue.value for venue in capabilities.venues],
                    "order_routes": [route.value for route in capabilities.order_routes],
                    "extensions": [
                        {
                            "id": extension.id,
                            "version": extension.version,
                            "targets": [target.value for target in extension.targets],
                        }
                        for extension in capabilities.extensions
                    ],
                }
            )
        return result

    @app.get("/api/block-options/conditions")
    async def block_condition_options(
        source: str,
        user_id: str,
    ) -> dict[str, object]:
        """Return saved broker conditions for the block editor.

        This endpoint deliberately has no strategy session dependency: it is a
        read-only editor lookup.  Authentication, token reuse, caching and rate
        limiting still go through ConnectionRuntime.
        """

        try:
            conditions = await connection_runtime.condition_definitions(
                source,
                user_id,
            )
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RouterError as exc:
            status_code = (
                422
                if exc.code is RouterErrorCode.UNSUPPORTED_CAPABILITY
                else 502
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from exc
        return {
            "conditions": [
                {
                    "id": item.id,
                    "name": item.name,
                    "provider": item.provider,
                    "source_alias": item.source_alias,
                }
                for item in conditions
            ]
        }

    @app.get("/api/system/diagnostics/export")
    async def export_diagnostics(
        request: Request, hours: int = Query(default=72, ge=1, le=168),
    ) -> Response:
        # This local download never contacts a broker or uploads the archive.
        # Refuse cross-site browser requests as well as relying on normal CORS.
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(status_code=403, detail="앱 설정에서 진단 자료를 저장해 주세요.")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="앱 설정에서 진단 자료를 저장해 주세요.")
        realtime_states = await realtime.snapshot()
        connection_states = [
            {"id": item["id"], "state": connection_runtime.status(item["id"]).get("state")}
            for item in settings.list_connections()
        ]
        content, filename = await asyncio.to_thread(
            build_diagnostics,
            version=__version__, manager=snapshot_manager(manager), store=journal(),
            permissions=order_engine().permission_snapshot(), accounts=settings.list_accounts(),
            connection_states=connection_states, realtime_states=realtime_states,
            manifests=sessions.manifests(), hours=hours,
        )
        return Response(content, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        })

    @app.get("/api/system/doctor")
    async def system_doctor() -> dict[str, object]:
        checks: list[dict[str, object]] = []
        connections = settings.list_connections()
        accounts = settings.list_accounts()
        unresolved = journal().unresolved_orders()
        request_state = await connection_runtime.request_status()

        checks.append(
            {
                "id": "connections",
                "level": "ok" if connections else "warning",
                "message": (
                    f"등록된 API 연결 {len(connections)}개"
                    if connections
                    else "등록된 증권사 API 연결이 없습니다."
                ),
            }
        )
        checks.append(
            {
                "id": "accounts",
                "level": "ok" if accounts else "warning",
                "message": (
                    f"등록된 거래 계좌 {len(accounts)}개"
                    if accounts
                    else "등록된 거래 계좌가 없습니다."
                ),
            }
        )
        for connection in connections:
            connection_status = connection_runtime.status(connection["id"])
            checks.append(
                {
                    "id": f"connection:{connection['id']}",
                    "level": (
                        "ok"
                        if connection_status["state"] == "connected"
                        else "warning"
                    ),
                    "message": (
                        f"{connection['label']}: {connection_status['message']}"
                    ),
                }
            )
        checks.append(
            {
                "id": "order-ledger",
                "level": "error" if unresolved else "ok",
                "message": (
                    f"확인이 필요한 주문 {len(unresolved)}건"
                    if unresolved
                    else "확인이 필요한 주문이 없습니다."
                ),
            }
        )
        permissions = order_engine().permission_snapshot()
        order_submission_enabled = (
            any(
                item["scope_type"] == "account"
                and item["armed"]
                and item.get("reconciled")
                for item in permissions
            )
            and any(item["scope_type"] == "strategy" and item["armed"] for item in permissions)
        )
        return {
            "healthy": not any(item["level"] == "error" for item in checks),
            "order_submission_enabled": order_submission_enabled,
            "order_engine_available": True,
            "checks": checks,
            "requests": request_state,
        }

    @app.get("/api/runtime/schedules")
    async def runtime_schedules() -> list[dict[str, object]]:
        return await scheduler.snapshot()

    @app.get("/api/runtime/events")
    async def runtime_events(
        limit: int = Query(default=200, ge=1, le=1_000),
        source: str | None = None,
    ) -> list[dict[str, object]]:
        return journal().events(limit=limit, source=source)

    @app.get("/api/runtime/orders")
    async def runtime_orders(
        limit: int = Query(default=200, ge=1, le=1_000),
        strategy_id: str | None = None,
        unresolved_only: bool = False,
    ) -> list[dict[str, object]]:
        if unresolved_only:
            rows = journal().unresolved_orders(strategy_id=strategy_id)
            return rows[:limit]
        return journal().orders(strategy_id=strategy_id, limit=limit)

    @app.get("/api/runtime/order-permissions")
    async def runtime_order_permissions() -> list[dict[str, object]]:
        return order_engine().permission_snapshot()

    @app.post("/api/runtime/accounts/{account_alias}/arm")
    async def arm_account_orders(
        account_alias: str,
        body: OrderPermissionRegistration,
    ) -> dict[str, object]:
        try:
            account = settings.get_account(account_alias)
            if account["target"] == "LIVE" and body.confirmation != "LIVE":
                raise HTTPException(
                    status_code=409,
                    detail="실전 계좌 주문 허용에는 confirmation 값을 LIVE로 보내야 합니다.",
                )
            return await order_engine().arm_account(account["alias"])
        except SettingsError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RouterError as exc:
            raise HTTPException(status_code=409, detail=exc.as_dict()) from exc

    @app.post("/api/runtime/accounts/{account_alias}/disarm")
    async def disarm_account_orders(account_alias: str) -> dict[str, object]:
        try:
            account = settings.get_account(account_alias)
        except SettingsError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return order_engine().disarm_account(account["alias"])

    @app.post("/api/runtime/accounts/{account_alias}/reconcile")
    async def reconcile_account_orders(account_alias: str) -> dict[str, object]:
        try:
            account = settings.get_account(account_alias)
            return await order_engine().reconcile_account(account["alias"])
        except SettingsError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RouterError as exc:
            raise HTTPException(status_code=409, detail=exc.as_dict()) from exc
        except Exception as exc:
            journal().record(
                source=account_alias,
                event_type="order.reconcile_error",
                message="계좌 주문 조정 중 내부 오류가 발생했습니다.",
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "RECONCILIATION_FAILED",
                    "message": "계좌 주문 조정 중 내부 오류가 발생했습니다.",
                    "error_type": type(exc).__name__,
                },
            ) from exc

    @app.post("/api/runtime/strategies/{strategy_id}/arm")
    async def arm_strategy_orders(strategy_id: str) -> dict[str, object]:
        return order_engine().arm_strategy(strategy_id)

    @app.post("/api/runtime/strategies/{strategy_id}/disarm")
    async def disarm_strategy_orders(strategy_id: str) -> dict[str, object]:
        return order_engine().disarm_strategy(strategy_id)

    @app.get("/api/strategies/{name}/order-permission")
    async def strategy_order_permission(name: str) -> dict[str, object]:
        try:
            manager.read(name)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Strategy not found.") from exc
        manifest = sessions.manifest(name)
        if not journal().has_order_permission("strategy_file", name):
            # One-time migration for strategies whose permission predates the
            # file-level switch. A running manifest lets us preserve its state.
            previous = bool(
                manifest
                and journal().order_permission("strategy", manifest.strategy_id)
            )
            journal().set_order_permission("strategy_file", name, armed=previous)
        return {
            "available": True,
            "armed": journal().order_permission("strategy_file", name),
            "strategy_id": manifest.strategy_id if manifest is not None else None,
            "running": manifest is not None,
        }

    @app.post("/api/strategies/{name}/arm-orders")
    async def arm_strategy_by_name(name: str) -> dict[str, object]:
        try:
            manager.read(name)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Strategy not found.") from exc
        result = journal().set_order_permission("strategy_file", name, armed=True)
        manifest = sessions.manifest(name)
        if manifest is not None:
            order_engine().arm_strategy(manifest.strategy_id)
        return {**result, "available": True, "strategy_id": manifest.strategy_id if manifest else None}

    @app.post("/api/strategies/{name}/disarm-orders")
    async def disarm_strategy_by_name(name: str) -> dict[str, object]:
        try:
            manager.read(name)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Strategy not found.") from exc
        result = journal().set_order_permission("strategy_file", name, armed=False)
        manifest = sessions.manifest(name)
        if manifest is not None:
            order_engine().disarm_strategy(manifest.strategy_id)
        return {**result, "available": True, "strategy_id": manifest.strategy_id if manifest else None}

    @app.post("/api/v1/strategy/manifest")
    async def register_strategy_manifest(
        body: StrategyManifestRegistration,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        try:
            account = settings.get_account(body.trading_account)
            connection_runtime.require_capabilities(
                account["connection_id"],
                ("accounts",),
            )
            for alias, connection in body.market_sources.items():
                settings.resolve_connection(connection)
                connection_runtime.require_capabilities(
                    connection,
                    body.required_capabilities.get(alias, []),
                )
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RouterError as exc:
            raise HTTPException(status_code=400, detail=exc.as_dict()) from exc
        if not body.market_sources:
            raise HTTPException(
                status_code=400,
                detail="전략에는 하나 이상의 데이터 연결이 필요합니다.",
            )
        unknown_aliases = set(body.required_capabilities) - set(body.market_sources)
        if unknown_aliases:
            raise HTTPException(
                status_code=400,
                detail=(
                    "기능 요구사항에 등록되지 않은 데이터 별칭이 있습니다: "
                    + ", ".join(sorted(unknown_aliases))
                ),
            )
        manifest = StrategyManifest(
            strategy_id=body.strategy_id,
            trading_account=str(account["alias"]),
            market_sources=dict(body.market_sources),
            required_capabilities={
                alias: tuple(values)
                for alias, values in body.required_capabilities.items()
            },
            risk_limits={
                key: (str(value) if isinstance(value, Decimal) else value)
                for key, value in body.risk_limits.model_dump().items()
            },
        )
        try:
            sessions.register_manifest(session.strategy_name, manifest)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not journal().has_order_permission("strategy_file", session.strategy_name):
            previous = journal().order_permission("strategy", manifest.strategy_id)
            journal().set_order_permission(
                "strategy_file", session.strategy_name, armed=previous
            )
        if journal().order_permission("strategy_file", session.strategy_name):
            order_engine().arm_strategy(manifest.strategy_id)
        else:
            order_engine().disarm_strategy(manifest.strategy_id)
        journal().record(
            source=session.strategy_name,
            event_type="strategy.manifest",
            message="전략 실행 설정을 등록했습니다.",
            details={
                "strategy_id": body.strategy_id,
                "trading_account": account["alias"],
                "market_source_aliases": sorted(body.market_sources),
                "risk_limits": {
                    key: (str(value) if isinstance(value, Decimal) else value)
                    for key, value in body.risk_limits.model_dump().items()
                },
            },
        )
        account_stream = await realtime.subscribe_account(
            strategy_name=session.strategy_name,
            account_reference=manifest.trading_account,
        )
        return {
            "registered": True,
            "account_realtime": account_stream is not None,
        }

    @app.post("/api/v1/strategy/schedules")
    async def register_strategy_schedules(
        body: ScheduleRegistration,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        started_at = time.perf_counter()
        if sessions.manifest(session.strategy_name) is None:
            record_service_result(
                session,
                service="scheduler",
                operation="register",
                outcome="error",
                message="전략 실행 설정이 없어 스케줄을 등록하지 못했습니다.",
                started_at=started_at,
            )
            raise HTTPException(
                status_code=409,
                detail="전략 실행 설정을 먼저 등록하세요.",
            )
        try:
            specs = [schedule_from_dict(item) for item in body.schedules]
            await scheduler.register(session.strategy_name, specs)
        except (KeyError, TypeError, ValueError) as exc:
            record_service_result(
                session,
                service="scheduler",
                operation="register",
                outcome="error",
                message=str(exc),
                started_at=started_at,
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        journal().record(
            source=session.strategy_name,
            event_type="strategy.schedules",
            message="중앙 스케줄을 등록했습니다.",
            details={"schedule_ids": [spec.id for spec in specs]},
        )
        record_service_result(
            session,
            service="scheduler",
            operation="register",
            outcome="success",
            message=f"중앙 스케줄 {len(specs)}개를 등록했습니다.",
            started_at=started_at,
            details={"schedule_ids": [spec.id for spec in specs]},
        )
        return {"registered": len(specs)}

    @app.post("/api/v1/orders")
    async def submit_strategy_order(
        body: OrderSubmissionRegistration,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        started_at = time.perf_counter()
        manifest = sessions.manifest(session.strategy_name)
        if manifest is None:
            record_service_result(
                session,
                service="order",
                operation="submit",
                outcome="error",
                message="전략 실행 설정이 없어 주문을 제출하지 못했습니다.",
                started_at=started_at,
                details={"code": body.code, "side": body.side.value},
            )
            raise HTTPException(
                status_code=409,
                detail="전략 실행 설정을 먼저 등록하세요.",
            )
        try:
            request = OrderRequest(
                instrument=InstrumentId(body.code),
                side=body.side,
                order_type=body.order_type,
                quantity=body.quantity,
                client_order_id=body.client_order_id,
                route=body.route,
                price=body.price,
                provider_options=dict(body.provider_options),
                sell_position_scope=body.sell_position_scope,
                buy_flow_id=body.buy_flow_id,
                sell_buy_flow_ids=tuple(body.sell_buy_flow_ids),
                buy_rule_node_ids=tuple(body.buy_rule_node_ids),
                sell_buy_node_refs=tuple(body.sell_buy_node_refs),
            )
            order = await order_engine().submit(
                strategy_name=session.strategy_name,
                manifest=manifest,
                request=request,
            )
        except (TypeError, ValueError) as exc:
            record_service_result(
                session,
                service="order",
                operation="submit",
                outcome="error",
                message=str(exc),
                started_at=started_at,
                details={"code": body.code, "side": body.side.value},
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RouterError as exc:
            record_service_result(
                session,
                service="order",
                operation="submit",
                outcome="error",
                message=exc.message,
                started_at=started_at,
                details={
                    "code": body.code,
                    "side": body.side.value,
                    "error_code": exc.code.value,
                },
            )
            status_code = {
                RouterErrorCode.ORDER_PERMISSION_REQUIRED: 403,
                RouterErrorCode.IDEMPOTENCY_CONFLICT: 409,
                RouterErrorCode.ORDER_STATUS_UNKNOWN: 409,
                RouterErrorCode.RISK_LIMIT_EXCEEDED: 422,
                RouterErrorCode.UNSUPPORTED_CAPABILITY: 422,
                RouterErrorCode.ORDER_REJECTED: 422,
            }.get(exc.code, 502)
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from exc
        record_service_result(
            session,
            service="order",
            operation="submit",
            outcome="success",
            message=f"{body.code} {body.side.value} 주문 요청 결과 {order.status.value}",
            started_at=started_at,
            details={
                "code": body.code,
                "side": body.side.value,
                "status": order.status.value,
                "order_id": order.id,
            },
        )
        return _order_payload(order)

    @app.get("/api/v1/orders/open")
    async def open_strategy_orders(
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        manifest = sessions.manifest(session.strategy_name)
        if manifest is None:
            raise HTTPException(status_code=409, detail="전략 실행 설정을 먼저 등록하세요.")
        try:
            orders = await order_engine().open_orders(manifest=manifest)
        except RouterError as exc:
            raise HTTPException(status_code=502, detail=exc.as_dict()) from exc
        return {"orders": [_order_payload(order) for order in orders]}

    @app.post("/api/v1/orders/{order_id}/amend")
    async def amend_strategy_order(
        order_id: str,
        body: OrderAmendRegistration,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        manifest = sessions.manifest(session.strategy_name)
        if manifest is None:
            raise HTTPException(status_code=409, detail="전략 실행 설정을 먼저 등록하세요.")
        try:
            order = await order_engine().amend(
                strategy_name=session.strategy_name,
                manifest=manifest,
                order_id=order_id,
                quantity=body.quantity,
                order_type=body.order_type,
                price=body.price,
                route=body.route,
            )
        except RouterError as exc:
            status_code = 403 if exc.code is RouterErrorCode.ORDER_PERMISSION_REQUIRED else 422 if exc.code in {RouterErrorCode.INVALID_REQUEST, RouterErrorCode.UNSUPPORTED_CAPABILITY, RouterErrorCode.ORDER_REJECTED, RouterErrorCode.RISK_LIMIT_EXCEEDED} else 502
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from exc
        return _order_payload(order)

    @app.post("/api/v1/orders/{order_id}/cancel")
    async def cancel_strategy_order(
        order_id: str,
        body: OrderCancelRegistration,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        manifest = sessions.manifest(session.strategy_name)
        if manifest is None:
            raise HTTPException(status_code=409, detail="전략 실행 설정을 먼저 등록하세요.")
        try:
            order = await order_engine().cancel(
                strategy_name=session.strategy_name,
                manifest=manifest,
                order_id=order_id,
                quantity=body.quantity,
                route=body.route,
            )
        except RouterError as exc:
            status_code = 403 if exc.code is RouterErrorCode.ORDER_PERMISSION_REQUIRED else 422 if exc.code in {RouterErrorCode.INVALID_REQUEST, RouterErrorCode.UNSUPPORTED_CAPABILITY, RouterErrorCode.ORDER_REJECTED} else 502
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from exc
        return _order_payload(order)

    @app.get("/api/v1/strategy/events/next")
    async def next_strategy_event(
        timeout: float = Query(default=25.0, ge=0.1, le=30.0),
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        started_at = time.perf_counter()
        queued = pending_strategy_events[session.strategy_name]
        if queued:
            selected = queued.popleft()
        else:
            schedule_task = asyncio.create_task(
                scheduler.next_event(session.strategy_name, timeout=timeout)
            )
            runtime_task = asyncio.create_task(
                strategy_events.next_event(session.strategy_name, timeout=timeout)
            )
            done, pending = await asyncio.wait(
                {schedule_task, runtime_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            completed = [task.result() for task in done if task.result() is not None]
            # Order/fill/market events take precedence, but if a scheduler event
            # completes in the same loop turn it is retained for the next poll.
            completed.sort(key=lambda value: 0 if isinstance(value, dict) else 1)
            selected = completed[0] if completed else None
            queued.extend(completed[1:])
        if selected is not None and hasattr(selected, "as_dict"):
            event = {"event_type": "schedule", **selected.as_dict()}
            record_service_result(
                session,
                service="scheduler",
                operation="deliver",
                outcome="success",
                message=f"{selected.schedule_id} 일정을 전략에 전달했습니다.",
                started_at=started_at,
                details={
                    "schedule_id": selected.schedule_id,
                    "kind": selected.kind.value,
                    "scheduled_at": selected.scheduled_at.isoformat(),
                },
            )
        else:
            event = selected
        return {"event": event}

    @app.post("/api/v1/market/subscriptions", status_code=201)
    async def subscribe_strategy_market(
        body: RealtimeSubscriptionRegistration,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        if not sessions.authorize_source(
            session.strategy_name,
            source=body.source,
            source_alias=body.source_alias,
        ):
            raise HTTPException(
                status_code=403,
                detail="전략에 허용되지 않은 데이터 연결입니다.",
            )
        try:
            return await realtime.subscribe(
                strategy_name=session.strategy_name,
                source=body.source,
                source_alias=body.source_alias,
                kind=body.kind,
                code=body.code,
                venue=body.venue,
            )
        except SettingsError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RouterError as exc:
            status_code = 422 if exc.code is RouterErrorCode.UNSUPPORTED_CAPABILITY else 429 if exc.code is RouterErrorCode.RATE_LIMITED else 400
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from exc

    @app.delete("/api/v1/market/subscriptions/{subscription_id}", status_code=204)
    async def unsubscribe_strategy_market(
        subscription_id: str,
        session: StrategySession = Depends(require_strategy_session),
    ) -> None:
        removed = await realtime.unsubscribe(
            subscription_id, strategy_name=session.strategy_name
        )
        if not removed:
            raise HTTPException(status_code=404, detail="실시간 구독을 찾을 수 없습니다.")

    @app.get("/api/v1/market/quote")
    async def strategy_quote(
        source: str,
        code: str,
        venue: str = "INTEGRATED",
        source_alias: str | None = None,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        started_at = time.perf_counter()
        if not sessions.authorize_source(
            session.strategy_name,
            source=source,
            source_alias=source_alias,
        ):
            record_service_result(
                session,
                service="market",
                operation="quote",
                outcome="error",
                message="전략에 허용되지 않은 데이터 연결입니다.",
                started_at=started_at,
                details={"code": code, "source_alias": source_alias},
            )
            raise HTTPException(
                status_code=403,
                detail="전략에 허용되지 않은 데이터 연결입니다.",
            )
        try:
            quote = await connection_runtime.quote(
                source,
                code,
                Venue(venue),
                source_alias=source_alias,
            )
        except ValueError as exc:
            record_service_result(
                session,
                service="market",
                operation="quote",
                outcome="error",
                message="지원하지 않는 시장입니다.",
                started_at=started_at,
                details={"code": code, "venue": venue},
            )
            raise HTTPException(status_code=400, detail="지원하지 않는 시장입니다.") from exc
        except SettingsError as exc:
            record_service_result(
                session,
                service="market",
                operation="quote",
                outcome="error",
                message=str(exc),
                started_at=started_at,
                details={"code": code, "source_alias": source_alias},
            )
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RouterError as exc:
            record_service_result(
                session,
                service="market",
                operation="quote",
                outcome="error",
                message=exc.message,
                started_at=started_at,
                details={"code": code, "error_code": exc.code.value},
            )
            raise HTTPException(status_code=502, detail=exc.as_dict()) from exc
        payload = {
            "instrument": {
                "code": quote.instrument.code,
                "country": quote.instrument.country,
                "asset": quote.instrument.asset.value,
            },
            "price": str(quote.price),
            "venue": quote.venue.value,
            "provider": quote.provider,
            "source_alias": quote.source_alias,
            "market_time": quote.market_time.isoformat(),
            "received_at": quote.received_at.isoformat(),
            "correlation_id": quote.correlation_id,
            "delayed": quote.delayed,
            "stale": quote.stale,
            "connection_generation": quote.connection_generation,
        }
        record_service_result(
            session,
            service="market",
            operation="quote",
            outcome="success",
            message=f"{quote.instrument.code} 현재가 {quote.price}원을 받았습니다.",
            started_at=started_at,
            details={
                "code": quote.instrument.code,
                "price": str(quote.price),
                "venue": quote.venue.value,
                "provider": quote.provider,
                "source_alias": quote.source_alias,
            },
        )
        return payload

    @app.get("/api/v1/market/bars")
    async def strategy_bars(
        source: str,
        code: str,
        interval: str,
        count: int = Query(default=100, ge=1, le=400),
        venue: str = "INTEGRATED",
        source_alias: str | None = None,
        end: datetime | None = None,
        complete_only: bool = True,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        started_at = time.perf_counter()
        if not sessions.authorize_source(
            session.strategy_name,
            source=source,
            source_alias=source_alias,
        ):
            record_service_result(
                session,
                service="market",
                operation="bars",
                outcome="error",
                message="전략에 허용되지 않은 데이터 연결입니다.",
                started_at=started_at,
                details={"code": code, "interval": interval},
            )
            raise HTTPException(
                status_code=403,
                detail="전략에 허용되지 않은 데이터 연결입니다.",
            )
        try:
            requested_count = min(400, count + 1) if complete_only else count
            bars = await connection_runtime.bars(
                source,
                code,
                interval,
                count=requested_count,
                venue=Venue(venue),
                source_alias=source_alias,
                end=end,
            )
        except ValueError as exc:
            record_service_result(
                session,
                service="market",
                operation="bars",
                outcome="error",
                message="지원하지 않는 시장입니다.",
                started_at=started_at,
                details={"code": code, "interval": interval, "venue": venue},
            )
            raise HTTPException(status_code=400, detail="지원하지 않는 시장입니다.") from exc
        except SettingsError as exc:
            record_service_result(
                session,
                service="market",
                operation="bars",
                outcome="error",
                message=str(exc),
                started_at=started_at,
                details={"code": code, "interval": interval},
            )
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RouterError as exc:
            record_service_result(
                session,
                service="market",
                operation="bars",
                outcome="error",
                message=exc.message,
                started_at=started_at,
                details={
                    "code": code,
                    "interval": interval,
                    "error_code": exc.code.value,
                },
            )
            raise HTTPException(status_code=502, detail=exc.as_dict()) from exc
        if complete_only:
            bars = tuple(bar for bar in bars if bar.complete)
        bars = bars[-count:]
        payload = {
            "bars": [
                {
                    "instrument": {
                        "code": bar.instrument.code,
                        "country": bar.instrument.country,
                        "asset": bar.instrument.asset.value,
                    },
                    "interval": bar.interval,
                    "open_time": bar.open_time.isoformat(),
                    "close_time": bar.close_time.isoformat(),
                    "open": str(bar.open),
                    "high": str(bar.high),
                    "low": str(bar.low),
                    "close": str(bar.close),
                    "volume": str(bar.volume),
                    "venue": bar.venue.value,
                    "provider": bar.provider,
                    "source_alias": bar.source_alias,
                    "correlation_id": bar.correlation_id,
                    "complete": bar.complete,
                    "received_at": (
                        bar.received_at.isoformat() if bar.received_at else None
                    ),
                    "connection_generation": bar.connection_generation,
                    "stale": bar.stale,
                }
                for bar in bars
            ]
        }
        record_service_result(
            session,
            service="market",
            operation="bars",
            outcome="success",
            message=f"{code} {interval} 봉 {len(bars)}개를 받았습니다.",
            started_at=started_at,
            details={
                "code": code,
                "interval": interval,
                "count": len(bars),
                "venue": venue,
                "source_alias": source_alias,
            },
        )
        return payload

    @app.get("/api/v1/discovery/conditions")
    async def strategy_conditions(
        source: str,
        user_id: str,
        source_alias: str | None = None,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        started_at = time.perf_counter()
        if not sessions.authorize_source(
            session.strategy_name,
            source=source,
            source_alias=source_alias,
        ):
            record_service_result(
                session,
                service="market",
                operation="conditions",
                outcome="error",
                message="전략에 허용되지 않은 조건검색 연결입니다.",
                started_at=started_at,
            )
            raise HTTPException(
                status_code=403,
                detail="전략에 허용되지 않은 조건검색 연결입니다.",
            )
        try:
            conditions = await connection_runtime.condition_definitions(
                source,
                user_id,
                source_alias=source_alias,
            )
        except (SettingsError, RouterError) as exc:
            message = exc.message if isinstance(exc, RouterError) else str(exc)
            details = (
                {"error_code": exc.code.value}
                if isinstance(exc, RouterError)
                else {}
            )
            record_service_result(
                session,
                service="market",
                operation="conditions",
                outcome="error",
                message=message,
                started_at=started_at,
                details=details,
            )
            status_code = 422 if isinstance(exc, RouterError) and exc.code is RouterErrorCode.UNSUPPORTED_CAPABILITY else 502
            raise HTTPException(
                status_code=status_code,
                detail=exc.as_dict() if isinstance(exc, RouterError) else message,
            ) from exc
        record_service_result(
            session,
            service="market",
            operation="conditions",
            outcome="success",
            message=f"저장된 조건식 {len(conditions)}개를 받았습니다.",
            started_at=started_at,
            details={"count": len(conditions), "source_alias": source_alias},
        )
        return {
            "conditions": [
                {
                    "id": item.id,
                    "name": item.name,
                    "provider": item.provider,
                    "source_alias": item.source_alias,
                }
                for item in conditions
            ]
        }

    @app.get("/api/v1/discovery/condition-matches")
    async def strategy_condition_matches(
        source: str,
        user_id: str,
        condition_id: str,
        source_alias: str | None = None,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        started_at = time.perf_counter()
        if not sessions.authorize_source(
            session.strategy_name,
            source=source,
            source_alias=source_alias,
        ):
            record_service_result(
                session,
                service="market",
                operation="condition_matches",
                outcome="error",
                message="전략에 허용되지 않은 조건검색 연결입니다.",
                started_at=started_at,
            )
            raise HTTPException(
                status_code=403,
                detail="전략에 허용되지 않은 조건검색 연결입니다.",
            )
        try:
            matches = await connection_runtime.condition_matches(
                source,
                user_id,
                condition_id,
                source_alias=source_alias,
            )
        except (SettingsError, RouterError) as exc:
            message = exc.message if isinstance(exc, RouterError) else str(exc)
            details = (
                {"error_code": exc.code.value, "condition_id": condition_id}
                if isinstance(exc, RouterError)
                else {"condition_id": condition_id}
            )
            record_service_result(
                session,
                service="market",
                operation="condition_matches",
                outcome="error",
                message=message,
                started_at=started_at,
                details=details,
            )
            status_code = 422 if isinstance(exc, RouterError) and exc.code is RouterErrorCode.UNSUPPORTED_CAPABILITY else 502
            raise HTTPException(
                status_code=status_code,
                detail=exc.as_dict() if isinstance(exc, RouterError) else message,
            ) from exc
        record_service_result(
            session,
            service="market",
            operation="condition_matches",
            outcome="success",
            message=f"조건식 {condition_id}에서 {len(matches)}종목을 받았습니다.",
            started_at=started_at,
            details={
                "condition_id": condition_id,
                "count": len(matches),
                "source_alias": source_alias,
                "matches": [
                    {
                        "name": item.name or item.instrument.code,
                        "code": item.instrument.code,
                    }
                    for item in matches
                ],
            },
        )
        journal().remember_instrument_names(
            {
                item.instrument.code: item.name
                for item in matches
                if item.name
            },
            source=source_alias or source,
        )
        return {
            "matches": [
                {
                    "condition_id": item.condition_id,
                    "instrument": {
                        "code": item.instrument.code,
                        "country": item.instrument.country,
                        "asset": item.instrument.asset.value,
                    },
                    "name": item.name,
                    "price": str(item.price) if item.price is not None else None,
                    "change_rate": (
                        str(item.change_rate)
                        if item.change_rate is not None
                        else None
                    ),
                    "provider": item.provider,
                    "source_alias": item.source_alias,
                }
                for item in matches
            ]
        }

    @app.get("/api/v1/discovery/ranking")
    async def strategy_ranking(
        source: str,
        kind: RankingKind,
        venue: Venue = Venue.KRX,
        market: str = "ALL",
        limit: int = Query(default=20, ge=1, le=100),
        min_price: Decimal | None = Query(default=None, ge=0),
        max_price: Decimal | None = Query(default=None, ge=0),
        min_volume: Decimal | None = Query(default=None, ge=0),
        source_alias: str | None = None,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        started_at = time.perf_counter()
        if not sessions.authorize_source(
            session.strategy_name,
            source=source,
            source_alias=source_alias,
        ):
            record_service_result(
                session,
                service="market",
                operation="ranking",
                outcome="error",
                message="전략에 허용되지 않은 순위 조회 연결입니다.",
                started_at=started_at,
                details={"kind": kind.value},
            )
            raise HTTPException(
                status_code=403,
                detail="전략에 허용되지 않은 순위 조회 연결입니다.",
            )
        try:
            entries = await connection_runtime.ranking(
                source,
                kind,
                venue=venue,
                market=market,
                limit=limit,
                min_price=min_price,
                max_price=max_price,
                min_volume=min_volume,
                source_alias=source_alias,
            )
        except (SettingsError, RouterError) as exc:
            message = exc.message if isinstance(exc, RouterError) else str(exc)
            details = {
                "kind": kind.value,
                "venue": venue.value,
                "market": market,
                **(
                    {"error_code": exc.code.value}
                    if isinstance(exc, RouterError)
                    else {}
                ),
            }
            record_service_result(
                session,
                service="market",
                operation="ranking",
                outcome="error",
                message=message,
                started_at=started_at,
                details=details,
            )
            status_code = (
                422
                if isinstance(exc, RouterError)
                and exc.code
                in {
                    RouterErrorCode.UNSUPPORTED_CAPABILITY,
                    RouterErrorCode.INVALID_REQUEST,
                }
                else 502
            )
            raise HTTPException(
                status_code=status_code,
                detail=exc.as_dict() if isinstance(exc, RouterError) else message,
            ) from exc
        record_service_result(
            session,
            service="market",
            operation="ranking",
            outcome="success",
            message=f"{kind.value} 순위에서 {len(entries)}종목을 받았습니다.",
            started_at=started_at,
            details={
                "kind": kind.value,
                "venue": venue.value,
                "market": market,
                "count": len(entries),
                "source_alias": source_alias,
                "entries": [
                    {
                        "rank": item.rank,
                        "name": item.name or item.instrument.code,
                        "code": item.instrument.code,
                    }
                    for item in entries
                ],
            },
        )
        journal().remember_instrument_names(
            {
                item.instrument.code: item.name
                for item in entries
                if item.name
            },
            source=source_alias or source,
        )
        return {
            "entries": [
                {
                    "rank": item.rank,
                    "kind": item.kind.value,
                    "instrument": {
                        "code": item.instrument.code,
                        "country": item.instrument.country,
                        "asset": item.instrument.asset.value,
                    },
                    "name": item.name,
                    "price": str(item.price) if item.price is not None else None,
                    "change_rate": (
                        str(item.change_rate)
                        if item.change_rate is not None
                        else None
                    ),
                    "volume": str(item.volume) if item.volume is not None else None,
                    "trading_value": (
                        str(item.trading_value)
                        if item.trading_value is not None
                        else None
                    ),
                    "provider": item.provider,
                    "source_alias": item.source_alias,
                }
                for item in entries
            ]
        }

    @app.get("/api/v1/account/balance")
    async def strategy_balance(
        account: str,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        started_at = time.perf_counter()
        if not sessions.authorize_account(session.strategy_name, account):
            record_service_result(
                session,
                service="account",
                operation="balance",
                outcome="error",
                message="전략에 허용되지 않은 거래 계좌입니다.",
                started_at=started_at,
            )
            raise HTTPException(
                status_code=403,
                detail="전략에 허용되지 않은 거래 계좌입니다.",
            )
        try:
            balance = await connection_runtime.balance(account)
            cost_rates = connection_runtime.account_cost_rates(account)
        except SettingsError as exc:
            record_service_result(
                session,
                service="account",
                operation="balance",
                outcome="error",
                message=str(exc),
                started_at=started_at,
            )
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RouterError as exc:
            record_service_result(
                session,
                service="account",
                operation="balance",
                outcome="error",
                message=exc.message,
                started_at=started_at,
                details={"error_code": exc.code.value},
            )
            raise HTTPException(status_code=502, detail=exc.as_dict()) from exc
        manifest = sessions.manifest(session.strategy_name)
        strategy_quantities = (
            journal().strategy_position_quantities(
                manifest.strategy_id,
                account_alias=account,
            )
            if manifest is not None
            else {}
        )
        buy_origin_lots = (
            journal().strategy_buy_origin_lots(
                manifest.strategy_id, account_alias=account,
                actual_quantities={position.instrument.code: position.quantity for position in balance.positions},
                as_of=balance.as_of,
            )
            if manifest is not None else {}
        )
        buy_flow_quantities = {}
        for code, lots in buy_origin_lots.items():
            quantities = buy_flow_quantities.setdefault(code, {})
            for lot in lots:
                quantities[lot["flow_id"]] = quantities.get(lot["flow_id"], Decimal("0")) + lot["quantity"]
        journal().remember_instrument_names(
            {
                position.instrument.code: position.name
                for position in balance.positions
                if position.name
            },
            source=account,
        )
        payload = {
            "cash": str(balance.cash),
            "withdrawable_cash": str(balance.withdrawable_cash),
            "total_value": str(balance.total_value),
            "as_of": balance.as_of.isoformat() if balance.as_of else None,
            **{
                key: str(cost_rates[key]) if cost_rates.get(key) not in {None, ""} else None
                for key in ("buy_commission_rate", "sell_commission_rate", "sell_tax_rate")
            },
            "positions": [
                {
                    "instrument": {
                        "code": position.instrument.code,
                        "country": position.instrument.country,
                        "asset": position.instrument.asset.value,
                    },
                    "quantity": str(position.quantity),
                    "available_quantity": str(position.available_quantity),
                    "average_price": str(position.average_price),
                    "market_price": (
                        str(position.market_price)
                        if position.market_price is not None
                        else None
                    ),
                    "strategy_quantity": (
                        str(
                            min(
                                strategy_quantities.get(
                                    position.instrument.code,
                                    Decimal("0"),
                                ),
                                position.quantity,
                            )
                        )
                    ),
                    "name": position.name,
                    "buy_origin_lots": [
                        {"flow_id": lot["flow_id"], "node_ids": list(lot["node_ids"]), "quantity": str(lot["quantity"])}
                        for lot in buy_origin_lots.get(position.instrument.code, [])
                    ],
                    "buy_flow_quantities": {
                        flow_id: str(quantity)
                        for flow_id, quantity in buy_flow_quantities.get(position.instrument.code, {}).items()
                    },
                }
                for position in balance.positions
            ],
        }
        record_service_result(
            session,
            service="account",
            operation="balance",
            outcome="success",
            message=f"잔고 {len(balance.positions)}종목과 총자산을 확인했습니다.",
            started_at=started_at,
            details={
                "position_count": len(balance.positions),
                "total_value": str(balance.total_value),
            },
        )
        return payload

    @app.get("/api/v1/account/orders")
    async def strategy_account_orders(
        account: str,
        state: str = "all",
        business_date: date | None = None,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        started_at = time.perf_counter()
        if not sessions.authorize_account(session.strategy_name, account):
            record_service_result(
                session,
                service="account",
                operation="orders",
                outcome="error",
                message="전략에 허용되지 않은 거래 계좌입니다.",
                started_at=started_at,
            )
            raise HTTPException(
                status_code=403,
                detail="전략에 허용되지 않은 거래 계좌입니다.",
            )
        try:
            orders = await connection_runtime.account_orders(
                account,
                state=state,
                business_date=business_date,
            )
        except SettingsError as exc:
            record_service_result(
                session,
                service="account",
                operation="orders",
                outcome="error",
                message=str(exc),
                started_at=started_at,
            )
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RouterError as exc:
            record_service_result(
                session,
                service="account",
                operation="orders",
                outcome="error",
                message=exc.message,
                started_at=started_at,
                details={"error_code": exc.code.value, "state": state},
            )
            status_code = (
                400 if exc.code is RouterErrorCode.INVALID_REQUEST else 502
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from exc
        payload = {
            "orders": [
                {
                    "provider_order_id": order.provider_order_id,
                    "account_alias": order.account_alias,
                    "instrument": {
                        "code": order.instrument.code,
                        "country": order.instrument.country,
                        "asset": order.instrument.asset.value,
                    },
                    "side": order.side.value,
                    "order_type": order.order_type.value,
                    "status": order.status.value,
                    "requested_quantity": str(order.requested_quantity),
                    "filled_quantity": str(order.filled_quantity),
                    "remaining_quantity": str(order.remaining_quantity),
                    "price": str(order.price) if order.price is not None else None,
                    "average_fill_price": (
                        str(order.average_fill_price)
                        if order.average_fill_price is not None
                        else None
                    ),
                    "route": order.route.value,
                    "ordered_at": order.ordered_at.isoformat(),
                    "provider": order.provider,
                }
                for order in orders
            ]
        }
        record_service_result(
            session,
            service="account",
            operation="orders",
            outcome="success",
            message=f"계좌 주문·체결 {len(orders)}건을 확인했습니다.",
            started_at=started_at,
            details={"count": len(orders), "state": state},
        )
        return payload

    @app.get("/api/v1/account/buying-power")
    async def strategy_buying_power(
        account: str,
        code: str,
        order_type: str = "MARKET",
        price: Decimal | None = None,
        route: OrderRoute = OrderRoute.AUTO,
        session: StrategySession = Depends(require_strategy_session),
    ) -> dict[str, object]:
        started_at = time.perf_counter()
        if not sessions.authorize_account(session.strategy_name, account):
            record_service_result(
                session,
                service="account",
                operation="buying_power",
                outcome="error",
                message="전략에 허용되지 않은 거래 계좌입니다.",
                started_at=started_at,
                details={"code": code, "route": route.value},
            )
            raise HTTPException(
                status_code=403,
                detail="전략에 허용되지 않은 거래 계좌입니다.",
            )
        try:
            buying_power = await connection_runtime.buying_power(
                account,
                code,
                order_type=OrderType(order_type.upper()),
                price=price,
                route=route,
            )
        except ValueError as exc:
            record_service_result(
                session,
                service="account",
                operation="buying_power",
                outcome="error",
                message="주문 유형은 MARKET 또는 LIMIT이어야 합니다.",
                started_at=started_at,
                details={
                    "code": code,
                    "order_type": order_type,
                    "route": route.value,
                },
            )
            raise HTTPException(
                status_code=400,
                detail="주문 유형은 MARKET 또는 LIMIT이어야 합니다.",
            ) from exc
        except SettingsError as exc:
            record_service_result(
                session,
                service="account",
                operation="buying_power",
                outcome="error",
                message=str(exc),
                started_at=started_at,
                details={"code": code, "route": route.value},
            )
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RouterError as exc:
            record_service_result(
                session,
                service="account",
                operation="buying_power",
                outcome="error",
                message=exc.message,
                started_at=started_at,
                details={
                    "code": code,
                    "route": route.value,
                    "error_code": exc.code.value,
                },
            )
            status_code = (
                400 if exc.code is RouterErrorCode.INVALID_REQUEST else 502
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from exc
        payload = {
            "instrument": {
                "code": buying_power.instrument.code,
                "country": buying_power.instrument.country,
                "asset": buying_power.instrument.asset.value,
            },
            "account_alias": buying_power.account_alias,
            "order_type": buying_power.order_type.value,
            "requested_price": str(buying_power.requested_price),
            "calculation_price": str(buying_power.calculation_price),
            "cash_available": str(buying_power.cash_available),
            "maximum_quantity": str(buying_power.maximum_quantity),
            "provider": buying_power.provider,
            "as_of": buying_power.as_of.isoformat(),
            "route": buying_power.route.value,
        }
        record_service_result(
            session,
            service="account",
            operation="buying_power",
            outcome="success",
            message=f"{code} 주문 가능 수량 {buying_power.maximum_quantity}주를 확인했습니다.",
            started_at=started_at,
            details={
                "code": code,
                "route": buying_power.route.value,
                "maximum_quantity": str(buying_power.maximum_quantity),
                "calculation_price": str(buying_power.calculation_price),
                "cash_available": str(buying_power.cash_available),
            },
        )
        return payload

    @app.get("/api/settings/accounts")
    async def list_accounts() -> list[dict[str, object]]:
        return settings.list_accounts()

    @app.post("/api/settings/accounts", status_code=201)
    async def register_account(body: AccountRegistration) -> dict[str, object]:
        try:
            return settings.add_account(
                alias=body.alias,
                connection_id=body.connection_id,
                account_number=body.account_number,
                product_code=body.product_code,
                risk_limits={
                    "max_order_amount": (
                        str(body.max_order_amount)
                        if body.max_order_amount is not None
                        else None
                    ),
                    "max_daily_buy_amount": (
                        str(body.max_daily_buy_amount)
                        if body.max_daily_buy_amount is not None
                        else None
                    ),
                    "max_daily_buy_count": body.max_daily_buy_count,
                    "max_exposure": (
                        str(body.max_exposure)
                        if body.max_exposure is not None
                        else None
                    ),
                    "max_open_orders": body.max_open_orders,
                },
                cost_rates={
                    "buy_commission_rate": (
                        str(body.buy_commission_rate)
                        if body.buy_commission_rate is not None
                        else None
                    ),
                    "sell_commission_rate": (
                        str(body.sell_commission_rate)
                        if body.sell_commission_rate is not None
                        else None
                    ),
                    "sell_tax_rate": (
                        str(body.sell_tax_rate)
                        if body.sell_tax_rate is not None
                        else None
                    ),
                },
            )
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/settings/accounts/{account_id}")
    async def update_account(
        account_id: str,
        body: AccountUpdate,
    ) -> dict[str, object]:
        try:
            existing = settings.get_account(account_id)
            identity_changed = (
                str(existing["alias"]).casefold() != body.alias.strip().casefold()
                or str(existing["connection_id"]) != body.connection_id
                or bool(body.account_number and body.account_number.strip())
                or body.product_code is not None
            )
            if identity_changed:
                aliases = {
                    str(existing["alias"]).casefold(),
                    *{
                        str(value).casefold()
                        for value in existing.get("previous_aliases", [])
                    },
                }
                active = [
                    name
                    for name, manifest in sessions.manifests().items()
                    if manifest.trading_account.casefold() in aliases
                ]
                if active:
                    raise SettingsError(
                        "이 계좌를 사용하는 실행 중 전략을 먼저 중지하세요: "
                        + ", ".join(sorted(active))
                    )
            updated = settings.update_account(
                account_id,
                alias=body.alias,
                connection_id=body.connection_id,
                account_number=body.account_number,
                product_code=body.product_code,
                risk_limits={
                    "max_order_amount": (
                        str(body.max_order_amount)
                        if body.max_order_amount is not None
                        else None
                    ),
                    "max_daily_buy_amount": (
                        str(body.max_daily_buy_amount)
                        if body.max_daily_buy_amount is not None
                        else None
                    ),
                    "max_daily_buy_count": body.max_daily_buy_count,
                    "max_exposure": (
                        str(body.max_exposure)
                        if body.max_exposure is not None
                        else None
                    ),
                    "max_open_orders": body.max_open_orders,
                },
                cost_rates={
                    "buy_commission_rate": (
                        str(body.buy_commission_rate)
                        if body.buy_commission_rate is not None
                        else None
                    ),
                    "sell_commission_rate": (
                        str(body.sell_commission_rate)
                        if body.sell_commission_rate is not None
                        else None
                    ),
                    "sell_tax_rate": (
                        str(body.sell_tax_rate)
                        if body.sell_tax_rate is not None
                        else None
                    ),
                },
            )
            old_alias = str(existing["alias"])
            new_alias = str(updated["alias"])
            if old_alias != new_alias:
                journal().rename_account_alias(old_alias, new_alias)
            if identity_changed:
                order_engine().disarm_account(new_alias)
            return updated
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/settings/accounts/{account_id}", status_code=204)
    async def delete_account(account_id: str) -> None:
        try:
            account = settings.get_account(account_id)
            order_engine().disarm_account(account["alias"])
            settings.remove_account(account_id)
            journal().delete_order_permission("account", account["alias"])
        except SettingsError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/strategies")
    async def list_strategies() -> list[dict[str, object]]:
        return manager.list()

    @app.get("/api/strategies/{name}")
    async def read_strategy(name: str) -> dict[str, object]:
        try:
            return {"name": name, "source": manager.read(name), **manager.details(name)}
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Strategy not found.") from exc

    @app.get("/api/strategies/{name}/block-package")
    async def export_block_strategy(name: str) -> BlockStrategyPackage:
        try:
            source = manager.read(name)
            if manager.strategy_type(name) != "block":
                raise HTTPException(
                    status_code=409,
                    detail="Only block strategies can be exported as a block file.",
                )
            display_name = name[:-3] if name.lower().endswith(".py") else name
            return block_package_from_source(display_name, source)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Strategy not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/strategies/import-block")
    async def import_block_strategy(
        package: BlockStrategyPackage,
    ) -> dict[str, object]:
        requested_display_name = package.name.strip()
        if requested_display_name.lower().endswith(".py"):
            requested_display_name = requested_display_name[:-3].rstrip()
        try:
            display_name = requested_display_name
            copy_number = 1
            while manager.resolve(f"{display_name}.py").exists():
                copy_number += 1
                suffix = f" ({copy_number})"
                display_name = f"{requested_display_name[:120 - len(suffix)]}{suffix}"
            name = f"{display_name}.py"

            existing_strategy_ids: set[str] = set()
            for item in manager.list():
                existing_name = str(item.get("name") or "")
                if not existing_name:
                    continue
                try:
                    parsed = _strategy_config_from_source(manager.read(existing_name))
                except (FileNotFoundError, StrategyNameError, SyntaxError, ValueError):
                    continue
                existing_id = parsed.get("strategy_id")
                if existing_id:
                    existing_strategy_ids.add(str(existing_id).casefold())

            requested_strategy_id = package.settings.strategy_id.strip()
            strategy_id = requested_strategy_id
            id_copy_number = 1
            while strategy_id.casefold() in existing_strategy_ids:
                id_copy_number += 1
                suffix = f"-{id_copy_number}"
                strategy_id = f"{requested_strategy_id[:128 - len(suffix)]}{suffix}"
            settings = package.settings.model_copy(
                update={"strategy_id": strategy_id},
            )
            result = await manager.save(
                name,
                compile_block_strategy(
                    settings,
                    template=package.template,
                    layout=package.layout,
                ),
                strategy_type="block",
            )
            return {
                **result,
                "strategy_id": strategy_id,
                "copied": (
                    display_name != requested_display_name
                    or strategy_id != requested_strategy_id
                ),
            }
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/strategies/{name}/summary")
    async def strategy_summary(name: str) -> dict[str, object]:
        try:
            source = manager.read(name)
            process = manager.details(name)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Strategy not found.") from exc

        parsed = _strategy_config_from_source(source)
        manifest = sessions.manifest(name)
        strategy_id = (
            manifest.strategy_id if manifest is not None else parsed["strategy_id"]
        )
        trading_account = (
            manifest.trading_account
            if manifest is not None
            else parsed["trading_account"]
        )
        market_sources = (
            manifest.market_sources if manifest is not None else parsed["market_sources"]
        )
        risk_limits = dict(
            manifest.risk_limits if manifest is not None else parsed["risk_limits"]
        )
        business_date = datetime.now(SEOUL).date().isoformat()
        orders = (
            journal().orders(strategy_id=str(strategy_id), limit=1_000)
            if strategy_id
            else []
        )
        today_orders = [
            row for row in orders if str(row.get("created_at", "")).startswith(business_date)
        ]
        unresolved = [
            row
            for row in orders
            if row["status"]
            in {
                OrderStatus.SUBMITTING.value,
                OrderStatus.ACKNOWLEDGED.value,
                OrderStatus.PARTIALLY_FILLED.value,
                OrderStatus.CANCEL_PENDING.value,
                OrderStatus.UNKNOWN_RECONCILING.value,
            }
        ]
        filled_count = sum(
            1
            for row in today_orders
            if Decimal(str(row.get("filled_quantity") or "0")) > 0
        )
        rejected_count = sum(
            1 for row in today_orders if row["status"] == OrderStatus.REJECTED.value
        )
        daily_buy = (
            journal().daily_buy_reserved(str(strategy_id), business_date)
            if strategy_id
            else Decimal("0")
        )
        largest_order = max(
            (
                Decimal(str(row.get("request", {}).get("reserved_amount") or "0"))
                for row in today_orders
                if row["status"] != OrderStatus.REJECTED.value
            ),
            default=Decimal("0"),
        )

        snapshot = await dashboard_snapshot()
        account = next(
            (
                item
                for item in snapshot["accounts"]
                if item["alias"] == trading_account
            ),
            None,
        )
        instrument_names = journal().instrument_names()
        instrument_names.update(_workspace_instrument_names(workspace, name))
        if account is not None:
            instrument_names.update(
                {
                    str(position.get("code") or ""): str(position.get("name") or "")
                    for position in account.get("positions", []) or []
                    if position.get("code") and position.get("name")
                }
            )
        strategy_exposure = None
        if account is not None and strategy_id:
            owned = journal().strategy_position_quantities(
                str(strategy_id),
                account_alias=str(trading_account),
            )
            strategy_exposure = _unfilled_buy_reservations(unresolved)
            for position in account.get("positions", []) or []:
                code = str(position.get("code") or "")
                quantity = min(
                    owned.get(code, Decimal("0")),
                    Decimal(str(position.get("quantity") or "0")),
                )
                price = Decimal(
                    str(
                        position.get("market_price")
                        or position.get("average_price")
                        or "0"
                    )
                )
                strategy_exposure += quantity * price
        accounting_orders = (
            journal().strategy_accounting_orders(
                str(strategy_id), account_alias=str(trading_account),
            )
            if strategy_id and trading_account else orders
        )
        schedules = await scheduler.snapshot()
        schedule = next(
            (item for item in schedules if item["strategy"] == name),
            {"strategy": name, "queued_events": 0, "schedules": []},
        )
        activity = journal().service_results(business_date, source=name)
        service_events = journal().service_events(business_date, source=name)
        service_results = _service_result_rows(activity, service_events)
        service_map = {item["id"]: item for item in service_results}
        service_map["scheduler"].update(
            {
                "active_schedules": len(schedule.get("schedules", [])),
                "queued_events": int(schedule.get("queued_events", 0)),
            }
        )
        if today_orders and not activity.get("order"):
            latest_order = max(today_orders, key=lambda item: item["updated_at"])
            failed_statuses = {
                OrderStatus.REJECTED.value,
                OrderStatus.UNKNOWN_RECONCILING.value,
            }
            failed = latest_order["status"] in failed_statuses
            failed_count = sum(
                1 for item in today_orders if item["status"] in failed_statuses
            )
            service_map["order"].update(
                {
                    "state": "error" if failed else "ok",
                    "total": len(today_orders),
                    "success": len(today_orders) - failed_count,
                    "error": failed_count,
                    "last_at": latest_order["updated_at"],
                    "last_outcome": "error" if failed else "success",
                    "last_message": latest_order.get("message")
                    or f"최근 주문 상태 {latest_order['status']}",
                }
            )
        if today_orders and not service_map["order"].get("events"):
            service_map["order"]["events"] = [
                {
                    "id": item["id"],
                    "occurred_at": item["updated_at"],
                    "source": name,
                    "operation": "submit",
                    "outcome": (
                        "error" if item["status"] in failed_statuses else "success"
                    ),
                    "message": item.get("message")
                    or f"{item['request'].get('code', '')} {item['status']}",
                    "duration_ms": None,
                    "details": {
                        "code": item["request"].get("code"),
                        "side": item["request"].get("side"),
                        "status": item["status"],
                    },
                }
                for item in today_orders[:20]
            ]
        return {
            "name": name,
            "generated_at": datetime.now(SEOUL).isoformat(),
            "running": bool(process["running"]),
            "started_at": process.get("started_at"),
            "runtime_managed": bool(process.get("runtime_managed")),
            "order_permission": {
                "armed": journal().order_permission("strategy_file", name),
            },
            "strategy_id": strategy_id,
            "trading_account": trading_account,
            "market_sources": market_sources,
            "account": account,
            "orders": {
                "today": len(today_orders),
                "filled": filled_count,
                "rejected": rejected_count,
                "unresolved": len(unresolved),
            },
            "performance": _strategy_performance(
                accounting_orders,
                account,
                instrument_names,
            ),
            "trade_history": _strategy_trade_history(
                orders,
                instrument_names=instrument_names,
                accounting_orders=accounting_orders,
            ),
            "limits": {
                "max_order_amount": _usage_value(
                    largest_order, risk_limits.get("max_order_amount")
                ),
                "max_daily_buy_amount": _usage_value(
                    daily_buy, risk_limits.get("max_daily_buy_amount")
                ),
                "max_daily_buy_count": _usage_value(
                    journal().daily_buy_count(str(strategy_id), business_date)
                    if strategy_id
                    else 0,
                    risk_limits.get("max_daily_buy_count"),
                ),
                "max_exposure": _usage_value(
                    strategy_exposure, risk_limits.get("max_exposure")
                ),
                "max_open_orders": _usage_value(
                    len(unresolved), risk_limits.get("max_open_orders")
                ),
            },
            "schedule": schedule,
            "service_results": service_results,
        }

    @app.put("/api/strategies/{name}")
    async def save_strategy(name: str, body: StrategySource) -> dict[str, object]:
        source = body.source
        try:
            existing_type = manager.strategy_type(name)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        effective_type = body.strategy_type or existing_type
        if effective_type == "block":
            display_name = name[:-3] if name.lower().endswith(".py") else name
            try:
                package = block_package_from_source(display_name, source)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "블록 전략은 제공된 블록과 필드로만 저장할 수 있습니다. "
                        f"{exc}"
                    ),
                ) from exc
            # Block strategies always run the server-owned template.  The browser
            # only supplies validated settings extracted from its block canvas.
            source = compile_block_strategy(
                package.settings,
                template=package.template,
                layout=package.layout,
            )
        try:
            return await manager.save(
                name,
                source,
                strategy_type=body.strategy_type,
            )
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc

    @app.put("/api/strategies/{name}/block-package")
    async def save_block_strategy_package(
        name: str,
        package: BlockStrategyPackage,
    ) -> dict[str, object]:
        """Validate a block definition and compile it to the managed runtime."""

        try:
            if manager.strategy_type(name) != "block":
                raise HTTPException(
                    status_code=409,
                    detail="Only block strategies accept block definitions.",
                )
            display_name = name[:-3] if name.lower().endswith(".py") else name
            current = block_package_from_source(display_name, manager.read(name))
            if package.revision is None or package.revision != current.revision:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "다른 창이나 API에서 블록 전략이 변경되었습니다. "
                        "전략을 다시 열어 최신 설정을 불러온 뒤 변경해 주세요."
                    ),
                )
            normalized = package.model_copy(update={"name": display_name})
            source = compile_block_strategy(
                normalized.settings,
                template=normalized.template,
                layout=normalized.layout,
            )
            return await manager.save(name, source)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Strategy not found.") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/strategies/{name}", status_code=204)
    async def delete_strategy(name: str) -> None:
        try:
            await manager.delete(name)
            journal().delete_order_permission("strategy_file", name)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/strategies/{name}/start")
    async def start_strategy(name: str) -> dict[str, object]:
        try:
            await refresh_block_runtime(name)
            result = await manager.start(name)
            journal().record(
                source=name,
                event_type="strategy.start",
                message="전략 프로세스를 시작했습니다.",
                details={"runtime_managed": result["runtime_managed"]},
            )
            return result
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Strategy not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/strategies/{name}/stop")
    async def stop_strategy(name: str) -> dict[str, object]:
        try:
            result = await manager.stop(name)
            journal().record(
                source=name,
                event_type="strategy.stop",
                message="전략 프로세스를 중지했습니다.",
                details={"exit_code": result["exit_code"]},
            )
            return result
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/strategies/{name}/logs")
    async def strategy_logs(
        name: str, after: int = Query(default=0, ge=0)
    ) -> dict[str, object]:
        try:
            return manager.logs(name, after)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/updates/check")
    async def updates() -> dict[str, object]:
        try:
            return await check_for_update(__version__)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    def request_process_action(action: str) -> dict[str, str]:
        control = getattr(app.state, "process_control", None)
        if not callable(control):
            raise HTTPException(
                status_code=503,
                detail="현재 실행 방식에서는 프로그램 제어를 사용할 수 없습니다.",
            )
        if action in {"restart", "update"}:
            manager.checkpoint_running_for_restart()
        else:
            manager.clear_restart_checkpoint()
        control(action)
        return {"action": action, "message": f"프로그램 {action} 요청을 받았습니다."}

    @app.post("/api/updates/apply")
    async def apply_update() -> dict[str, object]:
        try:
            result = await prepare_update(
                __version__,
                workspace_root=workspace.root,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        request_process_action("update")
        return result

    @app.post("/api/system/restart")
    async def restart_system() -> dict[str, str]:
        return request_process_action("restart")

    @app.post("/api/system/shutdown")
    async def shutdown_system() -> dict[str, str]:
        return request_process_action("shutdown")

    return app


def _order_payload(order: Order) -> dict[str, object]:
    return {
        "id": order.id,
        "client_order_id": order.client_order_id,
        "account_alias": order.account_alias,
        "status": order.status.value,
        "submitted_at": order.submitted_at.isoformat(),
        "provider_order_id": order.provider_order_id,
        "filled_quantity": str(order.filled_quantity),
        "average_fill_price": (
            str(order.average_fill_price)
            if order.average_fill_price is not None
            else None
        ),
        "commission": str(order.commission),
        "tax": str(order.tax),
        "costs_complete": order.costs_complete,
        "message": order.message,
        "request": {
            "instrument": {
                "code": order.request.instrument.code,
                "country": order.request.instrument.country,
                "asset": order.request.instrument.asset.value,
            },
            "side": order.request.side.value,
            "order_type": order.request.order_type.value,
            "quantity": str(order.request.quantity),
            "price": (
                str(order.request.price)
                if order.request.price is not None
                else None
            ),
            "route": order.request.route.value,
            "provider_options": order.request.provider_options,
            "sell_position_scope": order.request.sell_position_scope,
            "buy_flow_id": order.request.buy_flow_id,
            "sell_buy_flow_ids": list(order.request.sell_buy_flow_ids),
            "buy_rule_node_ids": list(order.request.buy_rule_node_ids),
            "sell_buy_node_refs": list(order.request.sell_buy_node_refs),
        },
    }


app = create_app()
