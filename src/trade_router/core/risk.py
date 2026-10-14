from __future__ import annotations

from decimal import Decimal

from ..errors import RouterError
from ..models import OrderPreview, OrderRequest, OrderSide, RiskLimits
from .storage import SQLiteStore


class RiskEngine:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    async def validate(
        self,
        *,
        request: OrderRequest,
        preview: OrderPreview,
        account_risk: RiskLimits,
        strategy_risk: RiskLimits,
        account_positions_value: Decimal,
        strategy_positions_value: Decimal,
    ) -> Decimal:
        if not preview.allowed:
            raise RouterError(
                preview.block_code or "BROKER_PREVIEW_REJECTED",
                preview.block_reason or "Broker order preview rejected the request.",
            )
        if request.order_type not in account_risk.allowed_order_types:
            raise RouterError(
                "ACCOUNT_ORDER_TYPE_BLOCKED", "Order type is blocked by account policy."
            )
        if request.order_type not in strategy_risk.allowed_order_types:
            raise RouterError(
                "STRATEGY_ORDER_TYPE_BLOCKED",
                "Order type is blocked by strategy policy.",
            )
        if request.route not in account_risk.allowed_routes:
            raise RouterError(
                "ACCOUNT_ROUTE_BLOCKED", "Order route is blocked by account policy."
            )
        if request.route not in strategy_risk.allowed_routes:
            raise RouterError(
                "STRATEGY_ROUTE_BLOCKED", "Order route is blocked by strategy policy."
            )

        notional = preview.max_notional or preview.estimated_notional
        if notional is None or notional <= 0:
            raise RouterError(
                "ORDER_NOTIONAL_UNKNOWN",
                "A conservative order value could not be calculated.",
            )
        await self._check_limits(
            request=request,
            risk=account_risk,
            notional=notional,
            positions_value=account_positions_value,
            strategy_id=None,
            prefix="ACCOUNT",
        )
        await self._check_limits(
            request=request,
            risk=strategy_risk,
            notional=notional,
            positions_value=strategy_positions_value,
            strategy_id=request.strategy_id,
            prefix="STRATEGY",
        )
        return notional

    async def _check_limits(
        self,
        *,
        request: OrderRequest,
        risk: RiskLimits,
        notional: Decimal,
        positions_value: Decimal,
        strategy_id: str | None,
        prefix: str,
    ) -> None:
        if risk.max_order_krw is not None and notional > risk.max_order_krw:
            raise RouterError(
                f"{prefix}_MAX_ORDER_EXCEEDED",
                f"Conservative order value {notional} exceeds {risk.max_order_krw}.",
            )
        if risk.max_open_orders is not None:
            count = await self.store.open_order_count(
                account_alias=request.account_alias, strategy_id=strategy_id
            )
            if count >= risk.max_open_orders:
                raise RouterError(
                    f"{prefix}_MAX_OPEN_ORDERS_EXCEEDED",
                    f"Open order count {count} reached the configured maximum.",
                )
        if request.side == OrderSide.BUY and risk.max_daily_buy_krw is not None:
            used = await self.store.daily_buy_reserved(
                account_alias=request.account_alias, strategy_id=strategy_id
            )
            if used + notional > risk.max_daily_buy_krw:
                raise RouterError(
                    f"{prefix}_DAILY_BUY_EXCEEDED",
                    f"Daily reserved buys {used} plus {notional} exceed {risk.max_daily_buy_krw}.",
                )
        if (
            request.side == OrderSide.BUY
            and risk.max_exposure_krw is not None
            and positions_value + notional > risk.max_exposure_krw
        ):
            raise RouterError(
                f"{prefix}_EXPOSURE_EXCEEDED",
                f"Exposure {positions_value} plus {notional} exceeds {risk.max_exposure_krw}.",
            )
