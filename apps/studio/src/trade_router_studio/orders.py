from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from trade_router import (
    Balance,
    BrokerCapabilities,
    BuyingPower,
    Order,
    OrderRequest,
    OrderRoute,
    OrderSide,
    OrderStatus,
    OrderType,
    ProviderOrderAck,
    RouterError,
    RouterErrorCode,
)

from .runtime_store import RuntimeStore
from .sessions import StrategyManifest


SEOUL = ZoneInfo("Asia/Seoul")


class OrderGateway(Protocol):
    """Narrow broker boundary used by the provider-neutral order service."""

    def capabilities(self, account_reference: str) -> BrokerCapabilities: ...

    async def buying_power(
        self,
        account_reference: str,
        code: str,
        *,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
        route: OrderRoute = OrderRoute.AUTO,
    ) -> BuyingPower: ...

    async def balance(self, account_reference: str) -> Balance: ...

    async def account_orders(
        self,
        account_reference: str,
        *,
        state: str = "all",
        business_date: object | None = None,
    ) -> tuple[object, ...]: ...

    async def submit_order(
        self,
        account_reference: str,
        request: OrderRequest,
    ) -> ProviderOrderAck: ...

    async def amend_order(
        self,
        account_reference: str,
        provider_order_id: str,
        *,
        quantity: Decimal,
        order_type: OrderType,
        price: Decimal | None,
        route: OrderRoute,
    ) -> ProviderOrderAck: ...

    async def cancel_order(
        self,
        account_reference: str,
        provider_order_id: str,
        *,
        quantity: Decimal | None,
        route: OrderRoute,
    ) -> ProviderOrderAck: ...

    def account_risk_limits(
        self,
        account_reference: str,
    ) -> dict[str, str | int | None]: ...

    def account_cost_rates(
        self,
        account_reference: str,
    ) -> dict[str, str | None]: ...


class OrderService:
    """Central permissions, risk, idempotency and broker submission pipeline."""

    def __init__(
        self,
        store: RuntimeStore,
        gateway: OrderGateway,
        *,
        event_publisher: Callable[[str, dict[str, object], str | None], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._reconciled_accounts: set[str] = set()
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._accepting_submissions = True
        self._event_publisher = event_publisher

    def close_submission_gate(self) -> None:
        self._accepting_submissions = False

    def permission_snapshot(self) -> list[dict[str, object]]:
        return [
            {
                **item,
                "reconciled": (
                    item["scope_id"] in self._reconciled_accounts
                    if item["scope_type"] == "account"
                    else None
                ),
            }
            for item in self._store.order_permissions()
        ]

    def disarm_account(self, account_alias: str) -> dict[str, object]:
        self._reconciled_accounts.discard(account_alias)
        return self._store.set_order_permission(
            "account", account_alias, armed=False
        )

    def arm_strategy(self, strategy_id: str) -> dict[str, object]:
        return self._store.set_order_permission(
            "strategy", strategy_id, armed=True
        )

    def disarm_strategy(self, strategy_id: str) -> dict[str, object]:
        return self._store.set_order_permission(
            "strategy", strategy_id, armed=False
        )

    async def arm_account(self, account_alias: str) -> dict[str, object]:
        await self.reconcile_account(account_alias)
        return self._store.set_order_permission(
            "account", account_alias, armed=True
        )

    async def reconcile_account(self, account_alias: str) -> dict[str, object]:
        async with self._account_lock(account_alias):
            return await self._reconcile_account(account_alias)

    async def _reconcile_account(self, account_alias: str) -> dict[str, object]:
        """Refresh broker state and resolve ledger rows before opening orders."""

        capabilities = self._gateway.capabilities(account_alias)
        if not capabilities.accounts or not capabilities.order_history:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 거래 계좌는 잔고와 주문 조정 기능을 지원하지 않습니다.",
            )
        await self._gateway.balance(account_alias)
        broker_orders = await self._gateway.account_orders(account_alias, state="all")
        broker_open_orders = await self._gateway.account_orders(
            account_alias, state="open"
        )
        open_provider_ids = {
            str(getattr(item, "provider_order_id"))
            for item in broker_open_orders
            if getattr(item, "provider_order_id", None)
        }
        by_provider_id = {
            str(getattr(item, "provider_order_id")): item
            for item in broker_orders
            if getattr(item, "provider_order_id", None)
        }
        unresolved = [
            row
            for row in self._store.unresolved_orders()
            if row["account_alias"] == account_alias
        ]
        still_unknown: list[str] = []
        for row in unresolved:
            provider_order_id = row.get("provider_order_id")
            if not provider_order_id:
                if row["status"] == OrderStatus.SUBMITTING.value:
                    self._store.update_order(
                        row["id"],
                        status=OrderStatus.UNKNOWN_RECONCILING,
                        message="재시작 전에 제출 중이던 주문의 접수 여부를 확인해야 합니다.",
                    )
                still_unknown.append(row["id"])
                continue
            broker_order = by_provider_id.get(str(provider_order_id))
            if broker_order is None:
                still_unknown.append(row["id"])
                continue
            target_status = _reconciled_order_status(
                broker_order,
                open_provider_ids=open_provider_ids,
            )
            filled_quantity, average_fill_price = _broker_cumulative_fill(
                row, broker_order, by_provider_id
            )
            if filled_quantity < Decimal(str(row.get("filled_quantity") or "0")):
                still_unknown.append(row["id"])
                continue
            target_status = _preserve_pending_status(
                row, target_status, filled_quantity,
                Decimal(str(getattr(broker_order, "remaining_quantity", "0"))),
            )
            if target_status is OrderStatus.UNKNOWN_RECONCILING:
                still_unknown.append(row["id"])
            try:
                self._record_fill_delta(
                    row,
                    previous_quantity=Decimal(str(row.get("filled_quantity", "0"))),
                    previous_average=Decimal(str(row.get("average_fill_price") or "0")),
                    cumulative_quantity=filled_quantity,
                    cumulative_average=(
                        Decimal(str(average_fill_price))
                        if average_fill_price is not None
                        else Decimal("0")
                    ),
                )
                totals = self._cost_totals(str(row["id"]))
                provider_metadata = _reconciled_provider_metadata(
                    row, broker_order, filled_quantity
                )
                self._store.update_order(
                    row["id"],
                    status=target_status,
                    filled_quantity=filled_quantity,
                    average_fill_price=average_fill_price,
                    commission=totals["commission"],
                    tax=totals["tax"],
                    costs_complete=totals["costs_complete"],
                    provider_metadata=provider_metadata,
                    message=_tracked_order_message(
                        target_status,
                        filled_quantity=filled_quantity,
                        requested_quantity=Decimal(
                            str(getattr(broker_order, "requested_quantity", "0"))
                        ),
                    ),
                )
            except ValueError:
                still_unknown.append(row["id"])

        if still_unknown:
            self._reconciled_accounts.discard(account_alias)
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN,
                "접수 여부를 확정하지 못한 주문이 있어 새 주문을 차단했습니다.",
                details={"order_ids": still_unknown},
            )
        self._reconciled_accounts.add(account_alias)
        return {
            "account_alias": account_alias,
            "reconciled": True,
            "broker_order_count": len(broker_orders),
            "checked_at": datetime.now(SEOUL).isoformat(),
        }

    async def refresh_unresolved_orders(
        self,
        account_alias: str,
    ) -> list[dict[str, object]]:
        """Refresh router-owned open orders from the broker order snapshot.

        A broker snapshot can briefly omit a newly accepted order, so the
        background tracker leaves unmatched rows unchanged. Strict startup
        reconciliation remains responsible for resolving uncertain submits.
        """

        async with self._account_lock(account_alias):
            unresolved = self._store.unresolved_orders(account_alias=account_alias)
            if not unresolved:
                return []
            capabilities = self._gateway.capabilities(account_alias)
            if not capabilities.order_history:
                raise RouterError(
                    RouterErrorCode.UNSUPPORTED_CAPABILITY,
                    "이 거래 계좌는 주문·체결 조회를 지원하지 않습니다.",
                )
            broker_orders = await self._gateway.account_orders(
                account_alias,
                state="all",
            )
            broker_open_orders = await self._gateway.account_orders(
                account_alias,
                state="open",
            )
            open_provider_ids = {
                str(getattr(item, "provider_order_id"))
                for item in broker_open_orders
                if getattr(item, "provider_order_id", None)
            }
            by_provider_id = {
                str(getattr(item, "provider_order_id")): item
                for item in broker_orders
                if getattr(item, "provider_order_id", None)
            }
            updated: list[dict[str, object]] = []
            for row in unresolved:
                provider_order_id = row.get("provider_order_id")
                if not provider_order_id:
                    continue
                broker_order = by_provider_id.get(str(provider_order_id))
                if broker_order is None:
                    continue
                target_status = _reconciled_order_status(
                    broker_order,
                    open_provider_ids=open_provider_ids,
                )
                filled_quantity, average_fill_price = _broker_cumulative_fill(
                    row, broker_order, by_provider_id
                )
                previous_filled = Decimal(str(row.get("filled_quantity", "0")))
                previous_average = Decimal(str(row.get("average_fill_price") or "0"))
                if filled_quantity < previous_filled:
                    continue
                target_status = _preserve_pending_status(
                    row, target_status, filled_quantity,
                    Decimal(str(getattr(broker_order, "remaining_quantity", "0"))),
                )
                provider_metadata = _reconciled_provider_metadata(
                    row, broker_order, filled_quantity
                )
                if (
                    row["status"] == target_status.value
                    and Decimal(str(row.get("filled_quantity", "0")))
                    == filled_quantity
                    and (
                        row.get("average_fill_price") is None
                        and average_fill_price is None
                        or str(row.get("average_fill_price"))
                        == str(average_fill_price)
                    )
                    and provider_metadata == dict(row.get("provider_metadata") or {})
                ):
                    continue
                message = _tracked_order_message(
                    target_status,
                    filled_quantity=filled_quantity,
                    requested_quantity=Decimal(
                        str(getattr(broker_order, "requested_quantity", "0"))
                    ),
                )
                try:
                    fill = self._record_fill_delta(
                        row,
                        previous_quantity=previous_filled,
                        previous_average=previous_average,
                        cumulative_quantity=filled_quantity,
                        cumulative_average=(
                            Decimal(str(average_fill_price))
                            if average_fill_price is not None
                            else Decimal("0")
                        ),
                    )
                    totals = self._cost_totals(str(row["id"]))
                    changed = self._store.update_order(
                        str(row["id"]),
                        status=target_status,
                        filled_quantity=filled_quantity,
                        average_fill_price=average_fill_price,
                        commission=totals["commission"],
                        tax=totals["tax"],
                        costs_complete=totals["costs_complete"],
                        provider_metadata=provider_metadata,
                        message=message,
                    )
                except ValueError:
                    continue
                self._store.record(
                    source=str(row["strategy_id"]),
                    event_type=f"order.{target_status.value.casefold()}",
                    message=message,
                    details={
                        "order_id": row["id"],
                        "provider_order_id": provider_order_id,
                        "filled_quantity": str(filled_quantity),
                    },
                )
                await self._publish_order(changed)
                if fill is not None and self._event_publisher is not None:
                    await self._event_publisher(
                        str(row["strategy_id"]),
                        {"event_type": "fill", "fill": fill},
                        None,
                    )
                updated.append(changed)
            return updated

    async def open_orders(self, *, manifest: StrategyManifest) -> list[Order]:
        """Expose only confirmed actionable orders belonging to this strategy."""
        account_alias = manifest.trading_account
        await self.refresh_unresolved_orders(account_alias)
        async with self._account_lock(account_alias):
            rows = self._store.orders(
                strategy_id=manifest.strategy_id,
                account_alias=account_alias,
                statuses=(OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED),
                limit=1000,
            )
            if not rows:
                return []
            broker_open = await self._gateway.account_orders(account_alias, state="open")
            confirmed_ids = {
                str(getattr(order, "provider_order_id", ""))
                for order in broker_open
                if getattr(order, "remaining_quantity", Decimal("0")) > 0
            }
            return [
                order for row in rows
                if str(row.get("provider_order_id") or "") in confirmed_ids
                for order in (_order_from_row(row),)
                if order.request.quantity > order.filled_quantity
            ]

    async def apply_realtime_notice(
        self, account_alias: str, notice: dict[str, Any]
    ) -> dict[str, Any] | None:
        async with self._account_lock(account_alias):
            return await self._apply_realtime_notice(account_alias, notice)

    async def _apply_realtime_notice(
        self, account_alias: str, notice: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply one de-duplicated KIS execution notice to the central ledger."""

        provider_order_id = str(notice.get("provider_order_id") or "").strip()
        if not provider_order_id:
            return None
        signature = hashlib.sha256(
            json.dumps(notice, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if not self._store.claim_provider_event(
            provider=str(notice.get("provider") or "unknown"),
            account_alias=account_alias,
            event_key=signature,
        ):
            return None
        row = self._store.order_by_provider_id(account_alias, provider_order_id)
        if row is None:
            original = str(notice.get("original_order_id") or "").strip()
            if original:
                row = self._store.order_by_provider_id(account_alias, original)
        if row is None:
            return None
        # Receipt/rejection notifications still become on_order events; the
        # REST reconciler remains the final authority for cancellation states.
        if str(notice.get("execution_code") or "") != "2":
            if str(notice.get("rejected") or "") not in {"", "0", "N"}:
                metadata = dict(row.get("provider_metadata") or {})
                original_id = str(notice.get("original_order_id") or "").strip()
                if original_id and set(original_id) == {"0"}:
                    original_id = ""
                modification_notice = (
                    original_id not in {"", "0", provider_order_id}
                    or provider_order_id != str(row.get("provider_order_id") or "")
                    or provider_order_id in dict(metadata.get("amendment_fill_offsets") or {})
                    or bool(metadata.get("uncertain_modification"))
                    or bool(metadata.get("cancel_provider_order_id"))
                    or row["status"] == OrderStatus.CANCEL_PENDING.value
                )
                try:
                    row = self._store.update_order(
                        str(row["id"]),
                        status=(
                            OrderStatus(str(row["status"]))
                            if modification_notice else OrderStatus.REJECTED
                        ),
                        message=(
                            "정정·취소 요청 거절 통보를 받았습니다. 원주문 상태는 주문내역으로 확인합니다."
                            if modification_notice
                            else "증권사 실시간 통보에서 주문 거절을 확인했습니다."
                        ),
                    )
                except ValueError:
                    pass
            await self._publish_order(row)
            return row
        quantity = _optional_decimal(notice.get("quantity")) or Decimal("0")
        price = _optional_decimal(notice.get("price")) or Decimal("0")
        if quantity <= 0 or price <= 0:
            return None
        previous_quantity = Decimal(str(row.get("filled_quantity") or "0"))
        requested_quantity = Decimal(
            str(_effective_request_payload(row).get("quantity") or "0")
        )
        provider_metadata = dict(row.get("provider_metadata") or {})
        offsets = dict(provider_metadata.get("amendment_fill_offsets") or {})
        previous_average = Decimal(str(row.get("average_fill_price") or "0"))
        if provider_order_id != str(row.get("provider_order_id") or ""):
            # A delayed parent or unidentified child cannot safely be combined
            # with the current segment. REST reconciles all known segments.
            return None
        # Keep an independent cumulative realtime stream for original orders
        # as well as amendment children. REST may already include this notice;
        # adding it to the ledger's total would count the same execution twice.
        realtime_totals = dict(provider_metadata.get("realtime_fill_totals") or {})
        segment = dict(realtime_totals.get(provider_order_id) or {})
        offset = dict(offsets.get(provider_order_id) or {})
        base_quantity = Decimal(str(offset.get("quantity") or "0"))
        base_amount = Decimal(str(offset.get("amount") or "0"))
        observed_quantity = Decimal(str(segment.get("quantity") or "0"))
        observed_amount = Decimal(str(segment.get("amount") or "0"))
        # If REST has advanced beyond the observed stream, we cannot tell a
        # delayed overlapping notice from a new fill after a missing notice.
        # Keep this segment on authoritative snapshots, including after restart;
        # guessing an offset can fabricate quantities and execution prices.
        rest_only = (
            bool(segment.get("rest_only"))
            or base_quantity + observed_quantity != previous_quantity
            or abs(base_amount + observed_amount - previous_quantity * previous_average) > Decimal("0.00000001")
        )
        segment_quantity = observed_quantity + quantity
        segment_amount = observed_amount + quantity * price
        realtime_totals[provider_order_id] = {
            "quantity": str(segment_quantity), "amount": str(segment_amount),
            "rest_only": rest_only,
        }
        provider_metadata["realtime_fill_totals"] = realtime_totals
        cumulative_quantity = base_quantity + segment_quantity
        if (
            rest_only
            or cumulative_quantity > requested_quantity
            or cumulative_quantity <= previous_quantity
            or OrderStatus(str(row["status"])) in {
                OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.FILLED,
            }
        ):
            # A missing/reordered notice can leave the stream behind REST.
            # Retain its independent total, without changing financial totals.
            return self._store.update_order(
                str(row["id"]), status=OrderStatus(str(row["status"])),
                provider_metadata=provider_metadata,
            )
        cumulative_average = (base_amount + segment_amount) / cumulative_quantity
        actual_delta = cumulative_quantity - previous_quantity
        if actual_delta <= 0:
            return None
        fill = self._record_fill_delta(
            row,
            previous_quantity=previous_quantity,
            previous_average=previous_average,
            cumulative_quantity=cumulative_quantity,
            cumulative_average=cumulative_average,
        )
        totals = self._cost_totals(str(row["id"]))
        status = (
            OrderStatus.FILLED
            if cumulative_quantity >= requested_quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        status = _preserve_pending_status(row, status, cumulative_quantity)
        changed = self._store.update_order(
            str(row["id"]),
            status=status,
            filled_quantity=cumulative_quantity,
            average_fill_price=cumulative_average,
            commission=totals["commission"],
            tax=totals["tax"],
            costs_complete=totals["costs_complete"],
            provider_metadata=provider_metadata,
            message=_tracked_order_message(
                status,
                filled_quantity=cumulative_quantity,
                requested_quantity=requested_quantity,
            ),
        )
        await self._publish_order(changed)
        if fill is not None and self._event_publisher is not None:
            await self._event_publisher(
                str(row["strategy_id"]),
                {"event_type": "fill", "fill": fill},
                None,
            )
        return changed

    def _record_fill_delta(
        self,
        row: dict[str, Any],
        *,
        previous_quantity: Decimal,
        previous_average: Decimal,
        cumulative_quantity: Decimal,
        cumulative_average: Decimal,
    ) -> dict[str, Any] | None:
        delta = cumulative_quantity - previous_quantity
        if delta <= 0 or cumulative_average <= 0:
            return None
        cumulative_amount = cumulative_quantity * cumulative_average
        previous_amount = previous_quantity * previous_average
        price = (cumulative_amount - previous_amount) / delta
        request = dict(row.get("request") or {})
        side = str(request.get("side") or "").upper()
        rate_reader = getattr(self._gateway, "account_cost_rates", None)
        rates = (
            rate_reader(str(row["account_alias"]))
            if callable(rate_reader)
            else {}
        )
        fee_key = "buy_commission_rate" if side == "BUY" else "sell_commission_rate"
        fee_rate = _optional_decimal(rates.get(fee_key)) or Decimal("0")
        tax_rate = (
            _optional_decimal(rates.get("sell_tax_rate")) or Decimal("0")
            if side == "SELL"
            else Decimal("0")
        )
        costs_complete = rates.get(fee_key) not in {None, ""} and (
            side != "SELL" or rates.get("sell_tax_rate") not in {None, ""}
        )
        amount = delta * price
        return self._store.record_fill(
            order_id=str(row["id"]),
            cumulative_quantity=cumulative_quantity,
            quantity=delta,
            price=price,
            commission=amount * fee_rate,
            tax=amount * tax_rate,
            costs_complete=costs_complete,
        )

    def _cost_totals(self, order_id: str) -> dict[str, Any]:
        fills = self._store.fills(order_id=order_id)
        return {
            "commission": sum((Decimal(str(fill["commission"])) for fill in fills), Decimal("0")),
            "tax": sum((Decimal(str(fill["tax"])) for fill in fills), Decimal("0")),
            "costs_complete": bool(fills) and all(bool(fill["costs_complete"]) for fill in fills),
        }

    async def submit(
        self,
        *,
        strategy_name: str,
        manifest: StrategyManifest,
        request: OrderRequest,
    ) -> Order:
        account_alias = manifest.trading_account
        if not self._accepting_submissions:
            raise RouterError(
                RouterErrorCode.ORDER_PERMISSION_REQUIRED,
                "Trade Router가 종료 중이어서 새 주문을 받지 않습니다.",
            )
        existing = self._store.order_by_client_id(
            manifest.strategy_id, request.client_order_id
        )
        if existing is not None:
            if not _same_request(existing["request"], request):
                raise RouterError(
                    RouterErrorCode.IDEMPOTENCY_CONFLICT,
                    "client_order_id가 다른 주문 내용에 이미 사용되었습니다.",
                )
            return _order_from_row(existing)

        async with self._account_lock(account_alias):
            if not self._accepting_submissions:
                raise RouterError(
                    RouterErrorCode.ORDER_PERMISSION_REQUIRED,
                    "Trade Router가 종료 중이어서 새 주문을 받지 않습니다.",
                )
            # Recheck after entering the single submission critical section.
            existing = self._store.order_by_client_id(
                manifest.strategy_id, request.client_order_id
            )
            if existing is not None:
                if not _same_request(existing["request"], request):
                    raise RouterError(
                        RouterErrorCode.IDEMPOTENCY_CONFLICT,
                        "client_order_id가 다른 주문 내용에 이미 사용되었습니다.",
                    )
                return _order_from_row(existing)

            self._require_permissions(manifest)
            capabilities = self._gateway.capabilities(account_alias)
            _require_order_capability(capabilities, request.route)
            if account_alias not in self._reconciled_accounts:
                await self._reconcile_account(account_alias)

            conflicts = [
                row
                for row in self._store.unresolved_orders(account_alias=account_alias)
                if row["request"].get("code") == request.instrument.code
            ]
            if conflicts:
                raise RouterError(
                    RouterErrorCode.ORDER_STATUS_UNKNOWN,
                    "같은 계좌·종목의 미해결 주문이 있어 새 주문을 차단했습니다.",
                    details={"order_ids": [row["id"] for row in conflicts]},
                )

            if capabilities.order_history:
                broker_open_orders = await self._gateway.account_orders(
                    account_alias,
                    state="open",
                )
                broker_conflicts = [
                    item
                    for item in broker_open_orders
                    if item.instrument == request.instrument
                    and item.remaining_quantity > 0
                ]
                if broker_conflicts:
                    raise RouterError(
                        RouterErrorCode.ORDER_STATUS_UNKNOWN,
                        "다른 기기를 포함해 같은 계좌·종목에 미체결 주문이 있어 새 주문을 차단했습니다.",
                        details={
                            "provider_order_ids": [
                                item.provider_order_id for item in broker_conflicts
                            ]
                        },
                    )

            reserved_amount = await self._preflight(manifest, request)
            payload = _request_payload(request, reserved_amount=reserved_amount)
            try:
                row, created = self._store.reserve_order(
                    strategy_id=manifest.strategy_id,
                    account_alias=account_alias,
                    client_order_id=request.client_order_id,
                    request=payload,
                )
            except ValueError as exc:
                raise RouterError(
                    RouterErrorCode.IDEMPOTENCY_CONFLICT,
                    str(exc),
                ) from exc
            if not created:
                return _order_from_row(row)

            self._store.record(
                source=strategy_name,
                event_type="order.submitting",
                message="위험 한도와 주문 가능 수량을 확인하고 주문을 제출합니다.",
                details={
                    "order_id": row["id"],
                    "client_order_id": request.client_order_id,
                    "account_alias": account_alias,
                    "code": request.instrument.code,
                    "side": request.side.value,
                },
            )
            try:
                acknowledgement = await self._gateway.submit_order(
                    account_alias, request
                )
            except RouterError as exc:
                status = (
                    OrderStatus.UNKNOWN_RECONCILING
                    if exc.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
                    else OrderStatus.REJECTED
                )
                row = self._store.update_order(
                    row["id"], status=status, message=exc.message
                )
                await self._publish_order(
                    row, strategy_name=strategy_name
                )
                if status is OrderStatus.UNKNOWN_RECONCILING:
                    self._reconciled_accounts.discard(account_alias)
                self._store.record(
                    source=strategy_name,
                    event_type="order.unknown" if status is OrderStatus.UNKNOWN_RECONCILING else "order.rejected",
                    message=exc.message,
                    details={"order_id": row["id"], "code": exc.code.value},
                )
                if status is OrderStatus.UNKNOWN_RECONCILING:
                    return _order_from_row(row)
                raise
            except Exception as exc:
                # The adapter crossed the submission boundary. Never retry an
                # unclassified failure because the broker may have accepted it.
                row = self._store.update_order(
                    row["id"],
                    status=OrderStatus.UNKNOWN_RECONCILING,
                    message="주문 응답을 확인하지 못해 증권사 주문내역 조정이 필요합니다.",
                )
                self._reconciled_accounts.discard(account_alias)
                await self._publish_order(row, strategy_name=strategy_name)
                return _order_from_row(row)

            row = self._store.update_order(
                row["id"],
                status=OrderStatus.ACKNOWLEDGED,
                provider_order_id=acknowledgement.provider_order_id,
                provider_metadata=acknowledgement.provider_options,
                message=acknowledgement.message,
            )
            self._store.record(
                source=strategy_name,
                event_type="order.acknowledged",
                message="증권사가 주문 접수를 확인했습니다.",
                details={
                    "order_id": row["id"],
                    "provider": acknowledgement.provider,
                    "correlation_id": acknowledgement.correlation_id,
                },
            )
            await self._publish_order(row, strategy_name=strategy_name)
            return _order_from_row(row)

    async def amend(
        self,
        *,
        strategy_name: str,
        manifest: StrategyManifest,
        order_id: str,
        quantity: Decimal | None,
        order_type: OrderType,
        price: Decimal | None,
        route: OrderRoute | None,
    ) -> Order:
        await self.refresh_unresolved_orders(manifest.trading_account)
        async with self._account_lock(manifest.trading_account):
            return await self._amend(
                strategy_name=strategy_name,
                manifest=manifest,
                order_id=order_id,
                quantity=quantity,
                order_type=order_type,
                price=price,
                route=route,
            )

    async def _amend(
        self,
        *,
        strategy_name: str,
        manifest: StrategyManifest,
        order_id: str,
        quantity: Decimal | None,
        order_type: OrderType,
        price: Decimal | None,
        route: OrderRoute | None,
    ) -> Order:
        self._require_permissions(manifest)
        row = self._owned_open_order(manifest, order_id)
        capabilities = self._gateway.capabilities(manifest.trading_account)
        if not capabilities.amend_orders:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사 연결은 주문 정정을 지원하지 않습니다.",
            )
        if order_type is OrderType.LIMIT and (price is None or price <= 0):
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "지정가 정정에는 가격이 필요합니다.",
            )
        effective_request = _effective_request_payload(row)
        remaining = Decimal(str(effective_request["quantity"])) - Decimal(
            str(row.get("filled_quantity") or "0")
        )
        amended_quantity = quantity if quantity is not None else remaining
        if (
            amended_quantity <= 0
            or amended_quantity > remaining
            or amended_quantity != amended_quantity.to_integral_value()
        ):
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "정정 수량은 현재 미체결 수량 이하여야 합니다.",
            )
        if amended_quantity != remaining:
            # KIS partial amendments leave the unamended remainder working at
            # its original price. Until split order legs can be tracked, never
            # interpret a partial price amendment as reducing the total order.
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "정정은 미체결 잔량 전체만 지원합니다. 수량을 줄이려면 먼저 일부 취소 후 남은 잔량을 정정하세요.",
            )
        normalized_route = route or OrderRoute(
            str(effective_request.get("route", "AUTO"))
        )
        _require_order_capability(capabilities, normalized_route)
        if str(effective_request.get("side")) == OrderSide.BUY.value:
            await self._preflight_buy_amendment(
                manifest, row, quantity=amended_quantity,
                order_type=order_type, price=price, route=normalized_route,
            )
        try:
            acknowledgement = await self._gateway.amend_order(
                manifest.trading_account,
                str(row["provider_order_id"]),
                quantity=amended_quantity,
                order_type=order_type,
                price=price,
                route=normalized_route,
            )
        except RouterError as exc:
            if exc.code is not RouterErrorCode.ORDER_STATUS_UNKNOWN:
                raise
            changed = self._store.update_order(
                order_id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={**dict(row.get("provider_metadata") or {}), "uncertain_modification": "amend"},
                message=exc.message,
            )
            self._reconciled_accounts.discard(manifest.trading_account)
            await self._publish_order(changed, strategy_name=strategy_name)
            return _order_from_row(changed)
        except Exception as exc:
            changed = self._store.update_order(
                order_id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={**dict(row.get("provider_metadata") or {}), "uncertain_modification": "amend"},
                message="정정 응답을 확인하지 못해 주문내역 조정이 필요합니다.",
            )
            self._reconciled_accounts.discard(manifest.trading_account)
            await self._publish_order(changed, strategy_name=strategy_name)
            return _order_from_row(changed)
        provider_metadata = dict(row.get("provider_metadata") or {})
        provider_metadata.update(acknowledgement.provider_options)
        if acknowledgement.provider_order_id != row.get("provider_order_id"):
            offsets = dict(provider_metadata.get("amendment_fill_offsets") or {})
            previous_filled = Decimal(str(row.get("filled_quantity") or "0"))
            offsets[acknowledgement.provider_order_id] = {
                "quantity": str(previous_filled),
                "amount": str(previous_filled * Decimal(str(row.get("average_fill_price") or "0"))),
                "requested_quantity": str(amended_quantity),
                "previous_provider_order_id": str(row.get("provider_order_id") or ""),
            }
            provider_metadata["amendment_fill_offsets"] = offsets
        provider_metadata["effective_request"] = {
            "quantity": str(
                Decimal(str(row.get("filled_quantity") or "0")) + amended_quantity
            ),
            "order_type": order_type.value,
            "price": str(price) if price is not None else None,
            "route": normalized_route.value,
        }
        changed = self._store.update_order(
            order_id,
            status=OrderStatus(str(row["status"])),
            provider_order_id=acknowledgement.provider_order_id,
            provider_metadata=provider_metadata,
            message=acknowledgement.message or "주문 정정이 접수되었습니다.",
        )
        self._store.record(
            source=strategy_name,
            event_type="order.amended",
            message="증권사가 주문 정정을 접수했습니다.",
            details={"order_id": order_id, "quantity": str(amended_quantity)},
        )
        await self._publish_order(changed, strategy_name=strategy_name)
        return _order_from_row(changed)

    async def _preflight_buy_amendment(
        self, manifest: StrategyManifest, row: dict[str, Any], *,
        quantity: Decimal, order_type: OrderType, price: Decimal | None,
        route: OrderRoute,
    ) -> None:
        original = dict(row["request"])
        effective = _effective_request_payload(row)
        filled = Decimal(str(row.get("filled_quantity") or "0"))
        original_quantity = Decimal(str(original.get("quantity") or "0"))
        reserved = Decimal(str(original.get("reserved_amount") or "0"))
        remaining = Decimal(str(effective["quantity"])) - filled
        filled_amount = filled * Decimal(str(row.get("average_fill_price") or "0"))
        # Keep the original per-share allocation and total order budget. A
        # reprice must reduce quantity when the conservative unit price rises.
        remaining_budget = min(
            max(Decimal("0"), reserved - filled_amount),
            reserved * remaining / original_quantity if original_quantity > 0 else Decimal("0"),
        )
        power = await self._gateway.buying_power(
            manifest.trading_account, str(effective["code"]),
            order_type=order_type, price=price, route=route,
        )
        unit_price = max(power.calculation_price, price or Decimal("0"))
        if not unit_price.is_finite() or unit_price <= 0:
            raise RouterError(RouterErrorCode.INVALID_REQUEST, "정정 주문 계산단가를 확인할 수 없습니다.")
        amount = quantity * unit_price
        if amount > remaining_budget:
            raise RouterError(
                RouterErrorCode.RISK_LIMIT_EXCEEDED,
                "정정 주문 금액이 기존 미체결 주문의 예산을 초과합니다.",
                details={"requested": str(amount), "remaining_budget": str(remaining_budget)},
            )
        account_limits = self._gateway.account_risk_limits(manifest.trading_account)
        for limits in (manifest.risk_limits, account_limits):
            limit = _optional_decimal(limits.get("max_order_amount"))
            if limit is not None and filled_amount + amount > limit:
                raise RouterError(RouterErrorCode.RISK_LIMIT_EXCEEDED, "정정 주문이 1회 주문 금액 한도를 초과합니다.")

    async def cancel(
        self,
        *,
        strategy_name: str,
        manifest: StrategyManifest,
        order_id: str,
        quantity: Decimal | None,
        route: OrderRoute | None,
    ) -> Order:
        async with self._account_lock(manifest.trading_account):
            return await self._cancel(
                strategy_name=strategy_name,
                manifest=manifest,
                order_id=order_id,
                quantity=quantity,
                route=route,
            )

    async def _cancel(
        self,
        *,
        strategy_name: str,
        manifest: StrategyManifest,
        order_id: str,
        quantity: Decimal | None,
        route: OrderRoute | None,
    ) -> Order:
        self._require_permissions(manifest)
        row = self._owned_open_order(manifest, order_id)
        capabilities = self._gateway.capabilities(manifest.trading_account)
        if not capabilities.cancel_orders:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사 연결은 주문 취소를 지원하지 않습니다.",
            )
        effective_request = _effective_request_payload(row)
        remaining = Decimal(str(effective_request["quantity"])) - Decimal(
            str(row.get("filled_quantity") or "0")
        )
        if quantity is not None and (
            quantity <= 0
            or quantity > remaining
            or quantity != quantity.to_integral_value()
        ):
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "취소 수량은 현재 미체결 수량 이하여야 합니다.",
            )
        normalized_route = route or OrderRoute(
            str(effective_request.get("route", "AUTO"))
        )
        _require_order_capability(capabilities, normalized_route)
        prior_status = OrderStatus(str(row["status"]))
        pending = self._store.update_order(
            order_id,
            status=OrderStatus.CANCEL_PENDING,
            message="취소 요청을 증권사에 제출합니다.",
        )
        await self._publish_order(pending, strategy_name=strategy_name)
        try:
            acknowledgement = await self._gateway.cancel_order(
                manifest.trading_account,
                str(row["provider_order_id"]),
                quantity=quantity,
                route=normalized_route,
            )
        except RouterError as exc:
            if exc.code is RouterErrorCode.ORDER_STATUS_UNKNOWN:
                unknown = self._store.update_order(
                    order_id,
                    status=OrderStatus.UNKNOWN_RECONCILING,
                    provider_metadata={**dict(row.get("provider_metadata") or {}), "uncertain_modification": "cancel"},
                    message=exc.message,
                )
                self._reconciled_accounts.discard(manifest.trading_account)
                await self._publish_order(unknown, strategy_name=strategy_name)
                return _order_from_row(unknown)
            restored = self._store.update_order(
                order_id,
                status=prior_status,
                message=exc.message,
            )
            await self._publish_order(restored, strategy_name=strategy_name)
            raise
        except Exception:
            unknown = self._store.update_order(
                order_id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={**dict(row.get("provider_metadata") or {}), "uncertain_modification": "cancel"},
                message="취소 응답을 확인하지 못해 주문내역 조정이 필요합니다.",
            )
            self._reconciled_accounts.discard(manifest.trading_account)
            await self._publish_order(unknown, strategy_name=strategy_name)
            return _order_from_row(unknown)
        provider_metadata = dict(row.get("provider_metadata") or {})
        provider_metadata.update(acknowledgement.provider_options)
        # KIS normalizes cancellation child rows into the cancelled original
        # row. Keep querying that original while retaining the receipt for audit.
        provider_metadata["cancel_provider_order_id"] = acknowledgement.provider_order_id
        provider_metadata["pending_cancel_expected_total"] = str(
            Decimal(str(effective_request["quantity"])) - (quantity if quantity is not None else remaining)
        )
        changed = self._store.update_order(
            order_id,
            status=OrderStatus.CANCEL_PENDING,
            provider_order_id=str(row["provider_order_id"]),
            provider_metadata=provider_metadata,
            message=acknowledgement.message or "주문 취소가 접수되었습니다.",
        )
        self._store.record(
            source=strategy_name,
            event_type="order.cancel_pending",
            message="증권사가 주문 취소를 접수했습니다.",
            details={"order_id": order_id, "quantity": str(quantity or remaining)},
        )
        await self._publish_order(changed, strategy_name=strategy_name)
        return _order_from_row(changed)

    def _owned_open_order(
        self, manifest: StrategyManifest, order_id: str
    ) -> dict[str, Any]:
        row = self._store.order(order_id)
        if row is None or row["strategy_id"] != manifest.strategy_id or row["account_alias"] != manifest.trading_account:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "이 전략이 소유한 주문을 찾을 수 없습니다.",
            )
        if not row.get("provider_order_id") or OrderStatus(str(row["status"])) not in {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIALLY_FILLED,
        }:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "접수되어 미체결 수량이 남은 주문만 정정·취소할 수 있습니다.",
            )
        return row

    def _account_lock(self, account_alias: str) -> asyncio.Lock:
        return self._account_locks.setdefault(account_alias, asyncio.Lock())

    async def _publish_order(
        self, row: dict[str, Any], *, strategy_name: str | None = None
    ) -> None:
        if self._event_publisher is None:
            return
        await self._event_publisher(
            str(row["strategy_id"]),
            {"event_type": "order", "order": _order_event_payload(row)},
            strategy_name,
        )

    def _require_permissions(self, manifest: StrategyManifest) -> None:
        if not self._store.order_permission("account", manifest.trading_account):
            raise RouterError(
                RouterErrorCode.ORDER_PERMISSION_REQUIRED,
                "거래 계좌의 주문 허용 상태를 먼저 켜세요.",
            )
        if not self._store.order_permission("strategy", manifest.strategy_id):
            raise RouterError(
                RouterErrorCode.ORDER_PERMISSION_REQUIRED,
                "전략의 주문 허용 상태를 먼저 켜세요.",
            )

    async def _preflight(
        self,
        manifest: StrategyManifest,
        request: OrderRequest,
    ) -> Decimal:
        if request.quantity != request.quantity.to_integral_value():
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "국내주식 주문 수량은 정수여야 합니다.",
            )
        limits = manifest.risk_limits
        account_limits = self._gateway.account_risk_limits(
            manifest.trading_account
        )
        max_open_orders = _optional_int(limits.get("max_open_orders"))
        if max_open_orders is not None and len(
            self._store.unresolved_orders(strategy_id=manifest.strategy_id)
        ) >= max_open_orders:
            raise RouterError(
                RouterErrorCode.RISK_LIMIT_EXCEEDED,
                "전략의 최대 미체결 주문 수를 초과합니다.",
            )
        account_max_open = _optional_int(account_limits.get("max_open_orders"))
        if account_max_open is not None and len(
            self._store.unresolved_orders(account_alias=manifest.trading_account)
        ) >= account_max_open:
            raise RouterError(
                RouterErrorCode.RISK_LIMIT_EXCEEDED,
                "계좌의 최대 미체결 주문 수를 초과합니다.",
            )

        account_alias = manifest.trading_account
        if request.side is OrderSide.BUY:
            business_date = datetime.now(SEOUL).date().isoformat()
            max_daily_count = _optional_int(limits.get("max_daily_buy_count"))
            if max_daily_count is not None:
                used_count = self._store.daily_buy_count(
                    manifest.strategy_id, business_date
                )
                if used_count >= max_daily_count:
                    raise RouterError(
                        RouterErrorCode.RISK_LIMIT_EXCEEDED,
                        "전략의 일일 누적 매수횟수 한도를 초과합니다.",
                        details={"used": used_count, "limit": max_daily_count},
                    )
            account_max_daily_count = _optional_int(
                account_limits.get("max_daily_buy_count")
            )
            if account_max_daily_count is not None:
                account_used_count = self._store.account_daily_buy_count(
                    account_alias, business_date
                )
                if account_used_count >= account_max_daily_count:
                    raise RouterError(
                        RouterErrorCode.RISK_LIMIT_EXCEEDED,
                        "계좌의 일일 누적 매수횟수 한도를 초과합니다.",
                        details={
                            "used": account_used_count,
                            "limit": account_max_daily_count,
                        },
                    )
            buying_power = await self._gateway.buying_power(
                account_alias,
                request.instrument.code,
                order_type=request.order_type,
                price=request.price,
                route=request.route,
            )
            if buying_power.calculation_price <= 0:
                raise RouterError(
                    RouterErrorCode.INVALID_REQUEST,
                    "보수적 주문 계산단가를 확인할 수 없습니다.",
                )
            if buying_power.maximum_quantity < request.quantity:
                raise RouterError(
                    RouterErrorCode.RISK_LIMIT_EXCEEDED,
                    "증권사 주문 가능 수량보다 요청 수량이 큽니다.",
                    details={"maximum_quantity": str(buying_power.maximum_quantity)},
                )
            unit_price = buying_power.calculation_price
            if request.price is not None:
                unit_price = max(unit_price, request.price)
            amount = unit_price * request.quantity
            if amount > buying_power.cash_available:
                raise RouterError(
                    RouterErrorCode.RISK_LIMIT_EXCEEDED,
                    "주문 계산금액이 주문 가능 현금을 초과합니다.",
                    details={
                        "cash_available": str(buying_power.cash_available),
                        "requested": str(amount),
                    },
                )
            max_daily = _optional_decimal(limits.get("max_daily_buy_amount"))
            if max_daily is not None:
                used = self._store.daily_buy_reserved(
                    manifest.strategy_id, business_date
                )
                if used + amount > max_daily:
                    raise RouterError(
                        RouterErrorCode.RISK_LIMIT_EXCEEDED,
                        "전략의 일일 누적 매수금액 한도를 초과합니다.",
                        details={"used": str(used), "requested": str(amount)},
                    )
            account_max_daily = _optional_decimal(
                account_limits.get("max_daily_buy_amount")
            )
            if account_max_daily is not None:
                account_used = self._store.account_daily_buy_reserved(
                    account_alias, business_date
                )
                if account_used + amount > account_max_daily:
                    raise RouterError(
                        RouterErrorCode.RISK_LIMIT_EXCEEDED,
                        "계좌의 일일 누적 매수금액 한도를 초과합니다.",
                        details={
                            "used": str(account_used),
                            "requested": str(amount),
                        },
                    )
            max_exposure = _optional_decimal(limits.get("max_exposure"))
            account_max_exposure = _optional_decimal(
                account_limits.get("max_exposure")
            )
            if max_exposure is not None or account_max_exposure is not None:
                balance = await self._gateway.balance(account_alias)
                positions_by_code = {
                    position.instrument.code: position
                    for position in balance.positions
                }
                strategy_quantities = self._store.strategy_position_quantities(
                    manifest.strategy_id,
                    account_alias=account_alias,
                )
                strategy_exposure = sum(
                    (
                        min(quantity, positions_by_code[code].quantity)
                        * (
                            positions_by_code[code].market_price
                            or positions_by_code[code].average_price
                        )
                        for code, quantity in strategy_quantities.items()
                        if code in positions_by_code
                    ),
                    Decimal("0"),
                ) + _open_buy_reservations(
                    self._store.unresolved_orders(
                        strategy_id=manifest.strategy_id,
                    )
                )
                account_exposure = sum(
                    (
                        position.quantity
                        * (position.market_price or position.average_price)
                        for position in balance.positions
                    ),
                    Decimal("0"),
                ) + _open_buy_reservations(
                    self._store.unresolved_orders(account_alias=account_alias)
                )
                if (
                    max_exposure is not None
                    and strategy_exposure + amount > max_exposure
                ):
                    raise RouterError(
                        RouterErrorCode.RISK_LIMIT_EXCEEDED,
                        "전략의 최대 노출 한도를 초과합니다.",
                        details={
                            "exposure": str(strategy_exposure),
                            "requested": str(amount),
                        },
                    )
                if (
                    account_max_exposure is not None
                    and account_exposure + amount > account_max_exposure
                ):
                    raise RouterError(
                        RouterErrorCode.RISK_LIMIT_EXCEEDED,
                        "계좌의 최대 노출 한도를 초과합니다.",
                        details={
                            "exposure": str(account_exposure),
                            "requested": str(amount),
                        },
                    )
        else:
            balance = await self._gateway.balance(account_alias)
            position = next(
                (
                    item
                    for item in balance.positions
                    if item.instrument == request.instrument
                ),
                None,
            )
            if position is None or position.available_quantity < request.quantity:
                raise RouterError(
                    RouterErrorCode.RISK_LIMIT_EXCEEDED,
                    "매도 가능한 보유 수량보다 요청 수량이 큽니다.",
                )
            amount = (request.price or position.market_price or position.average_price) * request.quantity

        max_order = _optional_decimal(limits.get("max_order_amount"))
        if max_order is not None and amount > max_order:
            raise RouterError(
                RouterErrorCode.RISK_LIMIT_EXCEEDED,
                "전략의 1회 주문 금액 한도를 초과합니다.",
                details={"requested": str(amount), "limit": str(max_order)},
            )
        account_max_order = _optional_decimal(account_limits.get("max_order_amount"))
        if account_max_order is not None and amount > account_max_order:
            raise RouterError(
                RouterErrorCode.RISK_LIMIT_EXCEEDED,
                "계좌의 1회 주문 금액 한도를 초과합니다.",
                details={"requested": str(amount), "limit": str(account_max_order)},
            )
        return amount


def _require_order_capability(
    capabilities: BrokerCapabilities,
    route: OrderRoute,
) -> None:
    if not capabilities.cash_orders:
        raise RouterError(
            RouterErrorCode.UNSUPPORTED_CAPABILITY,
            "이 증권사 연결은 아직 국내주식 현금 주문을 지원하지 않습니다.",
        )
    if route is OrderRoute.AUTO and not capabilities.order_routes:
        raise RouterError(
            RouterErrorCode.UNSUPPORTED_CAPABILITY,
            "이 증권사 연결에 사용할 수 있는 주문 경로가 없습니다.",
        )
    if route is not OrderRoute.AUTO and route not in capabilities.order_routes:
        raise RouterError(
            RouterErrorCode.UNSUPPORTED_CAPABILITY,
            f"이 증권사 연결은 {route.value} 주문 경로를 지원하지 않습니다.",
        )


def _request_payload(
    request: OrderRequest,
    *,
    reserved_amount: Decimal,
) -> dict[str, object]:
    return {
        "code": request.instrument.code,
        "country": request.instrument.country,
        "asset": request.instrument.asset.value,
        "side": request.side.value,
        "order_type": request.order_type.value,
        "quantity": str(request.quantity),
        "price": str(request.price) if request.price is not None else None,
        "route": request.route.value,
        "provider_options": request.provider_options,
        "reserved_amount": str(reserved_amount),
    }


def _same_request(stored: dict[str, object], request: OrderRequest) -> bool:
    comparison = _request_payload(request, reserved_amount=Decimal("0"))
    comparison.pop("reserved_amount")
    return all(stored.get(key) == value for key, value in comparison.items())


def _effective_request_payload(row: dict[str, object]) -> dict[str, object]:
    """Return current broker terms while preserving the original idempotent request."""

    original = dict(row.get("request") or {})
    metadata = row.get("provider_metadata")
    if not isinstance(metadata, dict):
        return original
    effective = metadata.get("effective_request")
    if not isinstance(effective, dict):
        return original
    for key in ("quantity", "order_type", "price", "route"):
        if key in effective:
            original[key] = effective[key]
    return original


def _broker_cumulative_fill(
    row: dict[str, object], broker_order: object, by_provider_id: dict[str, object],
    visited: frozenset[str] = frozenset(),
) -> tuple[Decimal, Decimal | None]:
    """Convert an amended broker segment's fills to original-order totals."""
    quantity = Decimal(str(getattr(broker_order, "filled_quantity", "0")))
    average = getattr(broker_order, "average_fill_price", None)
    average = Decimal(str(average)) if average is not None else None
    provider_id = str(getattr(broker_order, "provider_order_id", ""))
    metadata = dict(row.get("provider_metadata") or {})
    offsets = dict(metadata.get("amendment_fill_offsets") or {})
    offset = offsets.get(provider_id)
    if not isinstance(offset, dict) or provider_id in visited:
        return quantity, average
    # Some brokers return original-order cumulative snapshots after amendment.
    # Those already include earlier fills and must not receive the offset twice.
    if Decimal(str(getattr(broker_order, "requested_quantity", "0"))) > Decimal(
        str(offset.get("requested_quantity") or "0")
    ):
        return quantity, average
    base_quantity = Decimal(str(offset.get("quantity") or "0"))
    base_amount = Decimal(str(offset.get("amount") or "0"))
    previous_id = str(offset.get("previous_provider_order_id") or "")
    previous = by_provider_id.get(previous_id)
    if previous is not None and previous_id not in visited | {provider_id}:
        prior_quantity, prior_average = _broker_cumulative_fill(
            row, previous, by_provider_id, visited | {provider_id}
        )
        if prior_quantity > base_quantity and prior_average is not None:
            base_quantity = prior_quantity
            base_amount = prior_quantity * prior_average
    total_quantity = base_quantity + quantity
    total_amount = base_amount + quantity * (average or Decimal("0"))
    return total_quantity, total_amount / total_quantity if total_quantity > 0 else average


def _preserve_pending_status(
    row: dict[str, object], status: OrderStatus, filled_quantity: Decimal,
    remaining_quantity: Decimal | None = None,
) -> OrderStatus:
    if status in {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}:
        metadata = dict(row.get("provider_metadata") or {})
        if row["status"] == OrderStatus.UNKNOWN_RECONCILING.value and metadata.get("uncertain_modification"):
            return OrderStatus.UNKNOWN_RECONCILING
        if row["status"] == OrderStatus.CANCEL_PENDING.value:
            expected = metadata.get("pending_cancel_expected_total")
            if remaining_quantity is None or expected is None or filled_quantity + remaining_quantity > Decimal(str(expected)):
                return OrderStatus.CANCEL_PENDING
        if filled_quantity > 0:
            return OrderStatus.PARTIALLY_FILLED
    return status


def _reconciled_provider_metadata(
    row: dict[str, object], broker_order: object, filled_quantity: Decimal
) -> dict[str, object]:
    metadata = dict(row.get("provider_metadata") or {})
    current = _effective_request_payload(row)
    remaining = Decimal(str(getattr(broker_order, "remaining_quantity", "0")))
    effective_total = filled_quantity + remaining
    current_total = Decimal(str(current.get("quantity") or "0"))
    if effective_total > 0 and (current_total <= 0 or effective_total < current_total):
        broker_order_type = getattr(broker_order, "order_type")
        broker_price = getattr(broker_order, "price", None)
        metadata["effective_request"] = {
            "quantity": str(effective_total),
            "order_type": broker_order_type.value,
            "price": (
                None
                if broker_order_type in {OrderType.MARKET, OrderType.BEST_LIMIT}
                else (
                    str(broker_price)
                    if broker_price is not None and Decimal(str(broker_price)) > 0
                    else current.get("price")
                )
            ),
            "route": getattr(broker_order, "route").value,
        }
    return metadata


def _order_from_row(row: dict[str, object]) -> Order:
    from trade_router import AssetClass, InstrumentId

    payload = _effective_request_payload(row)
    request = OrderRequest(
        instrument=InstrumentId(
            str(payload["code"]),
            country=str(payload.get("country", "KR")),
            asset=AssetClass(str(payload.get("asset", "EQUITY"))),
        ),
        side=OrderSide(str(payload["side"])),
        order_type=OrderType(str(payload["order_type"])),
        quantity=Decimal(str(payload["quantity"])),
        client_order_id=str(row["client_order_id"]),
        route=OrderRoute(str(payload.get("route", "AUTO"))),
        price=(Decimal(str(payload["price"])) if payload.get("price") is not None else None),
        provider_options=dict(payload.get("provider_options") or {}),
    )
    return Order(
        id=str(row["id"]),
        client_order_id=str(row["client_order_id"]),
        account_alias=str(row["account_alias"]),
        request=request,
        status=OrderStatus(str(row["status"])),
        submitted_at=datetime.fromisoformat(str(row["created_at"])),
        provider_order_id=(
            str(row["provider_order_id"])
            if row.get("provider_order_id") is not None
            else None
        ),
        filled_quantity=Decimal(str(row.get("filled_quantity", "0"))),
        average_fill_price=(
            Decimal(str(row["average_fill_price"]))
            if row.get("average_fill_price") is not None
            else None
        ),
        commission=Decimal(str(row.get("commission", "0"))),
        tax=Decimal(str(row.get("tax", "0"))),
        costs_complete=bool(row.get("costs_complete", False)),
        message=(str(row["message"]) if row.get("message") else None),
    )


def _order_event_payload(row: dict[str, Any]) -> dict[str, Any]:
    order = _order_from_row(row)
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
            "price": str(order.request.price) if order.request.price is not None else None,
            "route": order.request.route.value,
            "provider_options": order.request.provider_options,
        },
    }


def _tracked_order_message(
    status: OrderStatus,
    *,
    filled_quantity: Decimal,
    requested_quantity: Decimal,
) -> str:
    if status is OrderStatus.PARTIALLY_FILLED:
        return f"부분 체결 {filled_quantity}/{requested_quantity}주"
    if status is OrderStatus.FILLED:
        return f"전량 체결 {filled_quantity}/{requested_quantity}주"
    if status is OrderStatus.CANCELLED:
        return f"주문 취소 · 체결 {filled_quantity}/{requested_quantity}주"
    if status is OrderStatus.REJECTED:
        return "증권사에서 주문을 거절했습니다."
    return f"주문 상태 {status.value} · 체결 {filled_quantity}/{requested_quantity}주"


def _reconciled_order_status(
    broker_order: object,
    *,
    open_provider_ids: set[str],
) -> OrderStatus:
    """Use an explicit broker status as the ledger's reconciliation result.

    Broker open-order snapshots are eventually consistent and may briefly omit
    an accepted or partially filled order.  Absence from that snapshot is not
    proof of cancellation, so only order history or a realtime notice may move
    the central ledger into a terminal state.
    """

    # The caller still supplies this snapshot for reconciliation context, but
    # it must not be used on its own to infer a terminal order state.
    del open_provider_ids
    return OrderStatus(getattr(broker_order, "status"))


def _open_buy_reservations(rows: list[dict[str, object]]) -> Decimal:
    """Return only the unfilled part of live buy-order reservations."""

    total = Decimal("0")
    for row in rows:
        request = dict(row.get("request") or {})
        if str(request.get("side") or "").upper() != OrderSide.BUY.value:
            continue
        requested = Decimal(str(request.get("quantity") or "0"))
        if requested <= 0:
            continue
        filled = Decimal(str(row.get("filled_quantity") or "0"))
        remaining = max(Decimal("0"), requested - filled)
        reserved = Decimal(str(request.get("reserved_amount") or "0"))
        total += reserved * remaining / requested
    return total


def _optional_decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value not in {None, ""} else None


def _optional_int(value: object) -> int | None:
    return int(value) if value not in {None, ""} else None
