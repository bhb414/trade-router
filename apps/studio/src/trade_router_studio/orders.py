from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta
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
UNKNOWN_ORDER_EARLY_TOLERANCE = timedelta(seconds=10)
UNKNOWN_ORDER_LATE_TOLERANCE = timedelta(seconds=10)
UNKNOWN_NOT_ACCEPTED_CONFIRMATION = "HTS_NOT_ACCEPTED"
UNKNOWN_LINK_CONFIRMATION = "HTS_LINK_CONFIRMED"


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

    async def balance(
        self, account_reference: str, *, fresh: bool = False
    ) -> Balance: ...

    async def account_orders(
        self,
        account_reference: str,
        *,
        state: str = "all",
        business_date: object | None = None,
        fresh: bool = False,
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

    async def reconcile_unknown_order(self, order_id: str) -> dict[str, object]:
        """Try to attach one unambiguous broker order without resubmitting it."""

        row = self._require_unknown_order(order_id)
        account_alias = str(row["account_alias"])
        async with self._account_lock(account_alias):
            # Reload after waiting for the account lock. A background tracker or
            # realtime notice may have resolved the row in the meantime.
            row = self._require_unknown_order(order_id)
            broker_orders, open_provider_ids = await self._broker_orders_for_row(
                row, fresh=True
            )
            by_provider_id = _orders_by_provider_id(broker_orders)
            provider_order_id = str(row.get("provider_order_id") or "")
            if provider_order_id:
                metadata = dict(row.get("provider_metadata") or {})
                if not metadata.get("uncertain_modification"):
                    raise RouterError(
                        RouterErrorCode.INVALID_REQUEST,
                        "증권사 주문번호가 있는 미확정 주문에는 정정·취소 조정 정보가 필요합니다.",
                    )
                working_row, broker_order, candidates, metadata_updates = (
                    self._uncertain_modification_snapshot(
                        row,
                        broker_orders,
                        by_provider_id=by_provider_id,
                        open_provider_ids=open_provider_ids,
                    )
                )
                unresolved_message = _uncertain_modification_message(
                    metadata_updates
                )
            else:
                candidates = self._unknown_order_candidates(row, broker_orders)
                if len(candidates) == 1:
                    working_row = row
                    broker_order = candidates[0]
                    metadata_updates = {
                        **_unknown_reconcile_metadata(
                            row, candidate_count=1, match_result="linked"
                        ),
                        "resolution": "auto_linked",
                    }
                else:
                    manual_time_candidates = (
                        self._unknown_manual_time_candidates(row, broker_orders)
                        if not candidates
                        else ()
                    )
                    if manual_time_candidates:
                        candidates = manual_time_candidates
                    working_row = row
                    broker_order = None
                    metadata_updates = _unknown_reconcile_metadata(
                        row,
                        candidate_count=len(candidates),
                        match_result=(
                            "manual_required_no_order_time"
                            if manual_time_candidates
                            else "none" if not candidates else "ambiguous"
                        ),
                    )
                unresolved_message = (
                    "증권사 주문시각이 없어 자동 연결할 수 없습니다. HTS 주문번호를 확인해 수동 연결하세요."
                    if metadata_updates.get("match_result")
                    == "manual_required_no_order_time"
                    else _unknown_candidate_message(len(candidates))
                )

            if broker_order is None:
                changed = self._store.update_order(
                    order_id,
                    status=OrderStatus.UNKNOWN_RECONCILING,
                    provider_metadata=metadata_updates,
                    message=unresolved_message,
                )
                self._reconciled_accounts.discard(account_alias)
                await self._publish_order(changed)
                return _unknown_reconcile_result(changed, candidates)

            changed, fill = self._apply_broker_snapshot(
                working_row,
                broker_order,
                by_provider_id=by_provider_id,
                open_provider_ids=open_provider_ids,
                provider_metadata_updates=metadata_updates,
            )
            if changed["status"] != OrderStatus.UNKNOWN_RECONCILING.value:
                self._store.record(
                    source=str(row["strategy_id"]),
                    event_type="order.reconciled",
                    message=(
                        "증권사 주문내역에서 정정·취소 상태를 안전하게 조정했습니다."
                        if provider_order_id
                        else "증권사 주문내역의 유일한 일치 주문을 자동 연결했습니다."
                    ),
                    details={
                        "order_id": order_id,
                        "provider_order_id": str(
                            getattr(broker_order, "provider_order_id", "")
                        ),
                        "resolution": str(
                            dict(changed.get("provider_metadata") or {}).get("resolution")
                            or dict(changed.get("provider_metadata") or {}).get(
                                "modification_resolution"
                            )
                            or "reconciled"
                        ),
                    },
                )
            await self._publish_order(changed)
            if fill is not None and self._event_publisher is not None:
                await self._event_publisher(
                    str(row["strategy_id"]),
                    {"event_type": "fill", "fill": fill},
                    None,
                )
            return _unknown_reconcile_result(changed, candidates)

    async def resolve_unknown_order(
        self,
        order_id: str,
        *,
        action: str,
        provider_order_id: str | None = None,
        confirmation: str = "",
    ) -> dict[str, object]:
        """Resolve an unknown submit through a verified link or explicit non-acceptance.

        ``LINK`` never trusts the supplied id alone: the broker order must be
        present in that order's business-day history and its immutable request
        fields must match. ``NOT_ACCEPTED`` is terminal only after an exact
        confirmation token and a fresh history query with no unlinked match.
        """

        normalized_action = action.strip().upper()
        if normalized_action not in {"LINK", "NOT_ACCEPTED"}:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "미확정 주문 해결 방식은 LINK 또는 NOT_ACCEPTED여야 합니다.",
            )
        row = self._require_unknown_order(order_id)
        account_alias = str(row["account_alias"])
        async with self._account_lock(account_alias):
            row = self._require_unknown_order(order_id)
            # A manual resolution must be based on a broker round trip.  A
            # short-lived cached snapshot can otherwise miss an order accepted
            # immediately after the previous reconciliation request.
            broker_orders, open_provider_ids = await self._broker_orders_for_row(
                row, fresh=True
            )
            by_provider_id = _orders_by_provider_id(broker_orders)

            if row.get("provider_order_id"):
                metadata = dict(row.get("provider_metadata") or {})
                modification_kind = str(
                    metadata.get("uncertain_modification") or ""
                )
                pending = metadata.get("pending_modification")
                if not modification_kind:
                    raise RouterError(
                        RouterErrorCode.INVALID_REQUEST,
                        "정정·취소 미확정 정보가 없는 주문입니다.",
                    )
                legacy_modification = not isinstance(pending, dict)
                if normalized_action == "LINK":
                    if modification_kind != "amend":
                        raise RouterError(
                            RouterErrorCode.INVALID_REQUEST,
                            "증권사 주문번호 연결은 정정 주문에만 사용할 수 있습니다.",
                        )
                    if confirmation != UNKNOWN_LINK_CONFIRMATION:
                        raise RouterError(
                            RouterErrorCode.INVALID_REQUEST,
                            f"정정 주문 연결에는 {UNKNOWN_LINK_CONFIRMATION} 확인 문구가 필요합니다.",
                        )
                    clean_provider_id = str(provider_order_id or "").strip()
                    original_provider_id = str(row["provider_order_id"])
                    if not clean_provider_id or clean_provider_id == original_provider_id:
                        raise RouterError(
                            RouterErrorCode.INVALID_REQUEST,
                            "연결할 정정 주문의 새 증권사 주문번호가 필요합니다.",
                        )
                    broker_order = by_provider_id.get(clean_provider_id)
                    if broker_order is None:
                        raise RouterError(
                            RouterErrorCode.INVALID_REQUEST,
                            "해당 영업일의 증권사 주문내역에서 정정 주문번호를 찾지 못했습니다.",
                        )
                    linked = self._store.order_by_provider_id(
                        account_alias,
                        clean_provider_id,
                        business_date=_order_business_date(row).isoformat(),
                    )
                    if linked is not None and str(linked["id"]) != order_id:
                        raise RouterError(
                            RouterErrorCode.IDEMPOTENCY_CONFLICT,
                            "이 증권사 주문번호는 이미 다른 주문에 연결되어 있습니다.",
                        )
                    broker_parent = str(
                        getattr(
                            broker_order, "original_provider_order_id", ""
                        )
                        or ""
                    ).strip()
                    if (
                        legacy_modification
                        and broker_parent != original_provider_id
                    ):
                        raise RouterError(
                            RouterErrorCode.INVALID_REQUEST,
                            "구버전 정정 주문은 증권사 원주문 번호가 현재 주문과 명시적으로 일치해야 연결할 수 있습니다.",
                        )
                    if (
                        not legacy_modification
                        and broker_parent
                        and broker_parent != original_provider_id
                    ):
                        raise RouterError(
                            RouterErrorCode.INVALID_REQUEST,
                            "선택한 정정 주문의 원주문 번호가 현재 주문과 일치하지 않습니다.",
                        )
                    if (
                        getattr(broker_order, "status", None)
                        is OrderStatus.REJECTED
                        or (
                            not legacy_modification
                            and not _broker_order_matches_pending_modification(
                                row,
                                broker_order,
                                pending,
                                enforce_time_window=False,
                            )
                        )
                    ):
                        raise RouterError(
                            RouterErrorCode.INVALID_REQUEST,
                            "증권사 정정 주문의 종목·방향·수량·주문유형 또는 가격이 요청과 일치하지 않습니다.",
                        )
                    if (
                        legacy_modification
                        and not _legacy_amendment_fill_semantics_are_safe(
                            row,
                            by_provider_id.get(original_provider_id),
                        )
                    ):
                        raise RouterError(
                            RouterErrorCode.ORDER_STATUS_UNKNOWN,
                            "구버전 정정 주문의 체결수량이 누적값인지 정정 이후 값인지 구분할 수 없어 안전하게 연결할 수 없습니다.",
                        )
                    working = (
                        _row_for_linked_legacy_amendment(row, broker_order)
                        if legacy_modification
                        else _row_for_linked_amendment(row, broker_order)
                    )
                    resolved_metadata = dict(
                        working.get("provider_metadata") or {}
                    )
                    resolved_metadata.update(
                        {
                            "match_result": "manual_linked_modification",
                            "resolution": "manual_amendment_linked",
                            "modification_resolution": "manual_amendment_linked",
                        }
                    )
                    working["provider_metadata"] = resolved_metadata
                    changed, fill = self._apply_broker_snapshot(
                        working,
                        broker_order,
                        by_provider_id=by_provider_id,
                        open_provider_ids=open_provider_ids,
                        provider_metadata_updates=resolved_metadata,
                    )
                    self._store.record(
                        source=str(row["strategy_id"]),
                        event_type="order.manually_resolved",
                        message="사용자가 검증된 정정 주문번호를 원주문에 연결했습니다.",
                        details={
                            "order_id": order_id,
                            "provider_order_id": clean_provider_id,
                            "resolution": "manual_amendment_linked",
                        },
                    )
                    await self._publish_order(changed)
                    if fill is not None and self._event_publisher is not None:
                        await self._event_publisher(
                            str(row["strategy_id"]),
                            {"event_type": "fill", "fill": fill},
                            None,
                        )
                    return _unknown_reconcile_result(
                        changed, (broker_order,)
                    )

                if confirmation != UNKNOWN_NOT_ACCEPTED_CONFIRMATION:
                    raise RouterError(
                        RouterErrorCode.INVALID_REQUEST,
                        f"정정·취소 미접수 확정에는 {UNKNOWN_NOT_ACCEPTED_CONFIRMATION} 확인 문구가 필요합니다.",
                    )
                candidates = (
                    self._legacy_uncertain_amendment_candidates(
                        row, broker_orders
                    )
                    if legacy_modification and modification_kind == "amend"
                    else self._uncertain_modification_candidates(
                        row,
                        broker_orders,
                        require_parent_relation=False,
                        include_missing_time=True,
                    )
                )
                blocking_candidates = tuple(
                    candidate
                    for candidate in candidates
                    if getattr(candidate, "status", None)
                    is not OrderStatus.REJECTED
                )
                if blocking_candidates:
                    raise RouterError(
                        RouterErrorCode.ORDER_STATUS_UNKNOWN,
                        "응답 시간대에 일치하는 정정 주문 후보가 있어 미접수 확정을 중단했습니다.",
                        details={"candidate_count": len(blocking_candidates)},
                    )
                if modification_kind == "cancel" or legacy_modification:
                    (
                        auto_working,
                        auto_broker_order,
                        _,
                        auto_metadata,
                    ) = self._uncertain_modification_snapshot(
                        row,
                        broker_orders,
                        by_provider_id=by_provider_id,
                        open_provider_ids=open_provider_ids,
                    )
                    # A fresh snapshot that already reflects the requested
                    # reduction is affirmative evidence of acceptance. Never
                    # let an operator's stale HTS observation restore the
                    # pre-cancel effective quantity in that case.
                    auto_resolution = str(
                        auto_metadata.get("modification_resolution") or ""
                    )
                    accepted_auto_resolutions = {"auto_cancel_reconciled"}
                    if modification_kind == "cancel":
                        accepted_auto_resolutions.add("broker_terminal")
                    if legacy_modification:
                        accepted_auto_resolutions.update(
                            {"broker_terminal", "broker_amendment_rejected"}
                        )
                    if (
                        auto_broker_order is not None
                        and auto_resolution in accepted_auto_resolutions
                    ):
                        changed, fill = self._apply_broker_snapshot(
                            auto_working,
                            auto_broker_order,
                            by_provider_id=by_provider_id,
                            open_provider_ids=open_provider_ids,
                            provider_metadata_updates=auto_metadata,
                        )
                        self._store.record(
                            source=str(row["strategy_id"]),
                            event_type="order.reconciled",
                            message="증권사 주문내역에 반영된 취소 수량을 확인해 자동 조정했습니다.",
                            details={
                                "order_id": order_id,
                                "provider_order_id": str(
                                    getattr(
                                        auto_broker_order,
                                        "provider_order_id",
                                        "",
                                    )
                                ),
                                "resolution": str(
                                    auto_metadata.get(
                                        "modification_resolution"
                                    )
                                    or "broker_terminal"
                                ),
                            },
                        )
                        await self._publish_order(changed)
                        if fill is not None and self._event_publisher is not None:
                            await self._event_publisher(
                                str(row["strategy_id"]),
                                {"event_type": "fill", "fill": fill},
                                None,
                            )
                        return _unknown_reconcile_result(changed, ())
                original = by_provider_id.get(str(row["provider_order_id"]))
                if original is None:
                    raise RouterError(
                        RouterErrorCode.ORDER_STATUS_UNKNOWN,
                        "원주문을 증권사 주문내역에서 확인하지 못해 미접수 확정을 중단했습니다.",
                    )
                original_status = _reconciled_order_status(
                    original, open_provider_ids=open_provider_ids
                )
                if original_status not in {
                    OrderStatus.ACKNOWLEDGED,
                    OrderStatus.PARTIALLY_FILLED,
                }:
                    raise RouterError(
                        RouterErrorCode.ORDER_STATUS_UNKNOWN,
                        "원주문이 이미 종료 상태여서 정정·취소 미접수 확인과 모순됩니다.",
                        details={"broker_status": original_status.value},
                    )
                resolved_metadata = _resolved_modification_metadata(
                    row, resolution="manual_modification_not_accepted"
                )
                working = dict(row)
                working["provider_metadata"] = resolved_metadata
                changed, fill = self._apply_broker_snapshot(
                    working,
                    original,
                    by_provider_id=by_provider_id,
                    open_provider_ids=open_provider_ids,
                    provider_metadata_updates=resolved_metadata,
                )
                self._store.record(
                    source=str(row["strategy_id"]),
                    event_type="order.manually_resolved",
                    message="사용자가 HTS에서 정정·취소 미접수를 확인하여 원주문 상태를 복원했습니다.",
                    details={
                        "order_id": order_id,
                        "resolution": "manual_modification_not_accepted",
                    },
                )
                await self._publish_order(changed)
                if fill is not None and self._event_publisher is not None:
                    await self._event_publisher(
                        str(row["strategy_id"]),
                        {"event_type": "fill", "fill": fill},
                        None,
                    )
                return _unknown_reconcile_result(changed, ())

            if normalized_action == "LINK":
                if confirmation != UNKNOWN_LINK_CONFIRMATION:
                    raise RouterError(
                        RouterErrorCode.INVALID_REQUEST,
                        f"주문 연결에는 {UNKNOWN_LINK_CONFIRMATION} 확인 문구가 필요합니다.",
                    )
                clean_provider_id = str(provider_order_id or "").strip()
                if not clean_provider_id:
                    raise RouterError(
                        RouterErrorCode.INVALID_REQUEST,
                        "연결할 증권사 주문번호가 필요합니다.",
                    )
                broker_order = by_provider_id.get(clean_provider_id)
                if broker_order is None:
                    raise RouterError(
                        RouterErrorCode.INVALID_REQUEST,
                        "해당 영업일의 증권사 주문내역에서 주문번호를 찾지 못했습니다.",
                    )
                linked = self._store.order_by_provider_id(
                    account_alias,
                    clean_provider_id,
                    business_date=_order_business_date(row).isoformat(),
                )
                if linked is not None and str(linked["id"]) != order_id:
                    raise RouterError(
                        RouterErrorCode.IDEMPOTENCY_CONFLICT,
                        "이 증권사 주문번호는 이미 다른 주문에 연결되어 있습니다.",
                    )
                if not _broker_order_matches_unknown(
                    row, broker_order, enforce_time_window=False
                ):
                    raise RouterError(
                        RouterErrorCode.INVALID_REQUEST,
                        "증권사 주문의 종목·방향·수량·주문유형 또는 가격이 원주문과 일치하지 않습니다.",
                    )
                changed, fill = self._apply_broker_snapshot(
                    row,
                    broker_order,
                    by_provider_id=by_provider_id,
                    open_provider_ids=open_provider_ids,
                    provider_metadata_updates={
                        **_unknown_reconcile_metadata(
                            row, candidate_count=1, match_result="linked"
                        ),
                        "resolution": "manual_linked",
                    },
                )
                self._store.record(
                    source=str(row["strategy_id"]),
                    event_type="order.manually_resolved",
                    message="사용자가 검증된 증권사 주문번호를 연결했습니다.",
                    details={
                        "order_id": order_id,
                        "provider_order_id": clean_provider_id,
                        "resolution": "manual_linked",
                    },
                )
                await self._publish_order(changed)
                if fill is not None and self._event_publisher is not None:
                    await self._event_publisher(
                        str(row["strategy_id"]),
                        {"event_type": "fill", "fill": fill},
                        None,
                    )
                return _unknown_reconcile_result(changed, (broker_order,))

            if confirmation != UNKNOWN_NOT_ACCEPTED_CONFIRMATION:
                raise RouterError(
                    RouterErrorCode.INVALID_REQUEST,
                    f"미접수 확정에는 {UNKNOWN_NOT_ACCEPTED_CONFIRMATION} 확인 문구가 필요합니다.",
                )
            # A same-day order placed independently hours later must not veto
            # an explicit HTS non-acceptance confirmation. Only candidates in
            # the same narrow submit/response window are safety-relevant.
            candidates = self._unknown_order_candidates(row, broker_orders)
            if not candidates:
                candidates = self._unknown_manual_time_candidates(row, broker_orders)
            if candidates:
                manual_time_only = all(
                    isinstance(getattr(item, "ordered_at", None), datetime)
                    and not any(
                        (
                            getattr(item, "ordered_at").hour,
                            getattr(item, "ordered_at").minute,
                            getattr(item, "ordered_at").second,
                            getattr(item, "ordered_at").microsecond,
                        )
                    )
                    for item in candidates
                )
                changed = self._store.update_order(
                    order_id,
                    status=OrderStatus.UNKNOWN_RECONCILING,
                    provider_metadata=_unknown_reconcile_metadata(
                        row,
                        candidate_count=len(candidates),
                        match_result=(
                            "manual_required_no_order_time"
                            if manual_time_only
                            else "unique" if len(candidates) == 1 else "ambiguous"
                        ),
                    ),
                    message="일치하는 증권사 주문 후보가 있어 미접수로 확정할 수 없습니다.",
                )
                await self._publish_order(changed)
                raise RouterError(
                    RouterErrorCode.ORDER_STATUS_UNKNOWN,
                    "일치하는 증권사 주문 후보가 있어 미접수 확정을 중단했습니다.",
                    details={"candidate_count": len(candidates)},
                )
            metadata = _unknown_reconcile_metadata(
                row, candidate_count=0, match_result="not_accepted"
            )
            metadata["resolution"] = "manual_not_accepted"
            changed = self._store.update_order(
                order_id,
                status=OrderStatus.REJECTED,
                provider_metadata=metadata,
                message="사용자가 HTS 주문내역을 확인하여 미접수로 확정했습니다.",
            )
            self._store.record(
                source=str(row["strategy_id"]),
                event_type="order.manually_resolved",
                message="사용자가 증권사 미접수를 명시적으로 확인했습니다.",
                details={
                    "order_id": order_id,
                    "resolution": "manual_not_accepted",
                },
            )
            await self._publish_order(changed)
            return _unknown_reconcile_result(changed, ())

    def _require_unknown_order(self, order_id: str) -> dict[str, Any]:
        row = self._store.order(order_id)
        if row is None:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "주문을 찾지 못했습니다.",
            )
        if row["status"] != OrderStatus.UNKNOWN_RECONCILING.value:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "UNKNOWN_RECONCILING 상태의 주문만 수동으로 해결할 수 있습니다.",
            )
        return row

    async def _broker_orders_for_row(
        self, row: dict[str, Any], *, fresh: bool = False
    ) -> tuple[tuple[object, ...], set[str]]:
        account_alias = str(row["account_alias"])
        business_date = _order_business_date(row)
        snapshots, open_provider_ids = await self._broker_order_snapshots(
            account_alias, (row,), fresh=fresh
        )
        return snapshots[business_date], open_provider_ids

    async def _broker_order_snapshots(
        self,
        account_alias: str,
        rows: tuple[dict[str, Any], ...] | list[dict[str, Any]],
        *,
        include_today: bool = False,
        fresh: bool = False,
    ) -> tuple[dict[date, tuple[object, ...]], set[str]]:
        today = datetime.now(SEOUL).date()
        business_dates = {_order_business_date(row) for row in rows}
        if include_today or not business_dates:
            business_dates.add(today)
        snapshots: dict[date, tuple[object, ...]] = {}
        for business_date in sorted(business_dates):
            snapshots[business_date] = tuple(
                await self._gateway.account_orders(
                    account_alias,
                    state="all",
                    business_date=business_date,
                    fresh=fresh,
                )
            )
        # Reconciliation status comes exclusively from explicit history rows;
        # absence from an open-order snapshot is never used to infer terminal
        # state. Avoid a redundant broker call on every tracker cycle.
        return snapshots, set()

    def _unknown_order_candidates(
        self,
        row: dict[str, Any],
        broker_orders: tuple[object, ...] | list[object],
        *,
        enforce_time_window: bool = True,
    ) -> tuple[object, ...]:
        candidates: list[object] = []
        seen: set[str] = set()
        for broker_order in broker_orders:
            provider_order_id = str(
                getattr(broker_order, "provider_order_id", "") or ""
            ).strip()
            if not provider_order_id or provider_order_id in seen:
                continue
            linked = self._store.order_by_provider_id(
                str(row["account_alias"]),
                provider_order_id,
                business_date=_order_business_date(row).isoformat(),
            )
            if linked is not None and str(linked["id"]) != str(row["id"]):
                continue
            if not _broker_order_matches_unknown(
                row,
                broker_order,
                enforce_time_window=enforce_time_window,
            ):
                continue
            seen.add(provider_order_id)
            candidates.append(broker_order)
        return tuple(candidates)

    def _unknown_manual_time_candidates(
        self,
        row: dict[str, Any],
        broker_orders: tuple[object, ...] | list[object],
    ) -> tuple[object, ...]:
        """Broker rows without order time require an explicit broker-id link."""

        candidates: list[object] = []
        seen: set[str] = set()
        for broker_order in broker_orders:
            ordered_at = getattr(broker_order, "ordered_at", None)
            if not isinstance(ordered_at, datetime) or any(
                (ordered_at.hour, ordered_at.minute, ordered_at.second, ordered_at.microsecond)
            ):
                continue
            provider_order_id = str(
                getattr(broker_order, "provider_order_id", "") or ""
            ).strip()
            if not provider_order_id or provider_order_id in seen:
                continue
            linked = self._store.order_by_provider_id(
                str(row["account_alias"]),
                provider_order_id,
                business_date=_order_business_date(row).isoformat(),
            )
            if linked is not None and str(linked["id"]) != str(row["id"]):
                continue
            if not _broker_order_matches_unknown(
                row, broker_order, enforce_time_window=False
            ):
                continue
            seen.add(provider_order_id)
            candidates.append(broker_order)
        return tuple(candidates)

    def _uncertain_modification_candidates(
        self,
        row: dict[str, Any],
        broker_orders: tuple[object, ...] | list[object],
        *,
        require_parent_relation: bool = True,
        include_missing_time: bool = False,
    ) -> tuple[object, ...]:
        """Return unlinked amendment children in the response-time window."""

        metadata = dict(row.get("provider_metadata") or {})
        pending = metadata.get("pending_modification")
        if metadata.get("uncertain_modification") != "amend" or not isinstance(
            pending, dict
        ):
            return ()
        original_provider_id = str(row.get("provider_order_id") or "")
        candidates: list[object] = []
        seen: set[str] = set()
        for broker_order in broker_orders:
            provider_order_id = str(
                getattr(broker_order, "provider_order_id", "") or ""
            ).strip()
            if (
                not provider_order_id
                or provider_order_id == original_provider_id
                or provider_order_id in seen
            ):
                continue
            # Never infer an amendment relationship from matching symbol,
            # quantity and time alone. A manual order can legitimately have
            # all of those fields. Automatic recovery is allowed only when
            # the broker history explicitly relates the child to this parent.
            broker_parent = str(
                getattr(broker_order, "original_provider_order_id", "") or ""
            ).strip()
            if require_parent_relation:
                if broker_parent != original_provider_id:
                    continue
            elif broker_parent and broker_parent != original_provider_id:
                continue
            linked = self._store.order_by_provider_id(
                str(row["account_alias"]),
                provider_order_id,
                business_date=_order_business_date(row).isoformat(),
            )
            if linked is not None and str(linked["id"]) != str(row["id"]):
                continue
            matches = _broker_order_matches_pending_modification(
                row, broker_order, pending
            )
            ordered_at = getattr(broker_order, "ordered_at", None)
            missing_order_time = isinstance(ordered_at, datetime) and not any(
                (
                    ordered_at.hour,
                    ordered_at.minute,
                    ordered_at.second,
                    ordered_at.microsecond,
                )
            )
            if (
                not matches
                and include_missing_time
                and missing_order_time
            ):
                matches = _broker_order_matches_pending_modification(
                    row,
                    broker_order,
                    pending,
                    enforce_time_window=False,
                )
            if not matches:
                continue
            seen.add(provider_order_id)
            candidates.append(broker_order)
        return tuple(candidates)

    def _legacy_uncertain_amendment_candidates(
        self,
        row: dict[str, Any],
        broker_orders: tuple[object, ...] | list[object],
    ) -> tuple[object, ...]:
        """Return relation-verified children for pre-pending-metadata rows.

        Releases before pending modification details were persisted retained
        only ``uncertain_modification=amend``.  Their requested quantity,
        order type and response window cannot be reconstructed safely, so a
        related child is evidence for an operator-assisted link only.  Never
        infer a relation from otherwise identical order fields.
        """

        metadata = dict(row.get("provider_metadata") or {})
        if (
            metadata.get("uncertain_modification") != "amend"
            or isinstance(metadata.get("pending_modification"), dict)
        ):
            return ()
        original_provider_id = str(row.get("provider_order_id") or "").strip()
        candidates: list[object] = []
        seen: set[str] = set()
        for broker_order in broker_orders:
            provider_order_id = str(
                getattr(broker_order, "provider_order_id", "") or ""
            ).strip()
            broker_parent = str(
                getattr(broker_order, "original_provider_order_id", "") or ""
            ).strip()
            if (
                not provider_order_id
                or provider_order_id == original_provider_id
                or provider_order_id in seen
                or broker_parent != original_provider_id
            ):
                continue
            linked = self._store.order_by_provider_id(
                str(row["account_alias"]),
                provider_order_id,
                business_date=_order_business_date(row).isoformat(),
            )
            if linked is not None and str(linked["id"]) != str(row["id"]):
                continue
            seen.add(provider_order_id)
            candidates.append(broker_order)
        return tuple(candidates)

    def _uncertain_modification_snapshot(
        self,
        row: dict[str, Any],
        broker_orders: tuple[object, ...] | list[object],
        *,
        by_provider_id: dict[str, object],
        open_provider_ids: set[str],
    ) -> tuple[
        dict[str, Any],
        object | None,
        tuple[object, ...],
        dict[str, object],
    ]:
        """Choose a safe snapshot for an uncertain amend/cancel request."""

        metadata = dict(row.get("provider_metadata") or {})
        kind = str(metadata.get("uncertain_modification") or "")
        pending = metadata.get("pending_modification")
        if kind == "amend" and not isinstance(pending, dict):
            # v0.1.74 did not persist the amendment request.  Related children
            # therefore cannot be field/time matched automatically, but they
            # must block both terminal-parent assumptions and NOT_ACCEPTED.
            legacy_candidates = self._legacy_uncertain_amendment_candidates(
                row, broker_orders
            )
            live_candidates = tuple(
                candidate
                for candidate in legacy_candidates
                if getattr(candidate, "status", None) is not OrderStatus.REJECTED
            )
            rejected_candidates = tuple(
                candidate
                for candidate in legacy_candidates
                if getattr(candidate, "status", None) is OrderStatus.REJECTED
            )
            original = by_provider_id.get(
                str(row.get("provider_order_id") or "")
            )
            if live_candidates:
                updates = _unknown_reconcile_metadata(
                    row,
                    candidate_count=len(live_candidates),
                    match_result="manual_required_legacy_modification",
                )
                return row, None, live_candidates, updates
            if rejected_candidates:
                if original is None:
                    updates = _unknown_reconcile_metadata(
                        row,
                        candidate_count=len(rejected_candidates),
                        match_result="amendment_rejected_parent_missing",
                    )
                    return row, None, rejected_candidates, updates
                updates = _resolved_modification_metadata(
                    row, resolution="broker_amendment_rejected"
                )
                updates.update(
                    _unknown_reconcile_metadata(
                        {**row, "provider_metadata": updates},
                        candidate_count=len(rejected_candidates),
                        match_result="broker_amendment_rejected",
                    )
                )
                working = dict(row)
                working["provider_metadata"] = updates
                return working, original, rejected_candidates, updates
            updates = _unknown_reconcile_metadata(
                row,
                candidate_count=0,
                match_result=(
                    "legacy_modification_pending"
                    if original is not None
                    else "none"
                ),
            )
            if original is None:
                return row, None, (), updates
            target_status = _reconciled_order_status(
                original, open_provider_ids=open_provider_ids
            )
            if target_status in {
                OrderStatus.CANCELLED,
                OrderStatus.FILLED,
                OrderStatus.REJECTED,
            }:
                updates = _resolved_modification_metadata(
                    row, resolution="broker_terminal"
                )
                working = dict(row)
                working["provider_metadata"] = updates
                return working, original, (), updates
            return row, original, (), updates

        candidates = self._uncertain_modification_candidates(row, broker_orders)
        if (
            kind == "amend"
            and len(candidates) == 1
            and getattr(candidates[0], "status", None)
            is OrderStatus.REJECTED
        ):
            original = by_provider_id.get(str(row.get("provider_order_id") or ""))
            if original is not None:
                updates = _resolved_modification_metadata(
                    row, resolution="broker_amendment_rejected"
                )
                updates.update(
                    _unknown_reconcile_metadata(
                        {**row, "provider_metadata": updates},
                        candidate_count=1,
                        match_result="broker_amendment_rejected",
                    )
                )
                working = dict(row)
                working["provider_metadata"] = updates
                return working, original, candidates, updates
            updates = _unknown_reconcile_metadata(
                row,
                candidate_count=1,
                match_result="amendment_rejected_parent_missing",
            )
            return row, None, candidates, updates
        if kind == "amend" and len(candidates) == 1:
            working = _row_for_linked_amendment(row, candidates[0])
            return (
                working,
                candidates[0],
                candidates,
                dict(working.get("provider_metadata") or {}),
            )
        if kind == "amend" and len(candidates) > 1:
            updates = _unknown_reconcile_metadata(
                row,
                candidate_count=len(candidates),
                match_result="ambiguous_modification",
            )
            return row, None, candidates, updates

        if kind == "amend" and not candidates:
            possible_candidates = self._uncertain_modification_candidates(
                row,
                broker_orders,
                require_parent_relation=False,
                include_missing_time=True,
            )
            if possible_candidates:
                missing_order_time = any(
                    isinstance(getattr(item, "ordered_at", None), datetime)
                    and not any(
                        (
                            getattr(item, "ordered_at").hour,
                            getattr(item, "ordered_at").minute,
                            getattr(item, "ordered_at").second,
                            getattr(item, "ordered_at").microsecond,
                        )
                    )
                    for item in possible_candidates
                )
                updates = _unknown_reconcile_metadata(
                    row,
                    candidate_count=len(possible_candidates),
                    match_result=(
                        "manual_required_no_order_time"
                        if missing_order_time
                        else "manual_required_missing_parent_relation"
                    ),
                )
                return row, None, possible_candidates, updates

        original = by_provider_id.get(str(row.get("provider_order_id") or ""))
        updates = _unknown_reconcile_metadata(
            row,
            candidate_count=0,
            match_result="modification_pending" if original is not None else "none",
        )
        if original is None:
            return row, None, candidates, updates
        target_status = _reconciled_order_status(
            original, open_provider_ids=open_provider_ids
        )
        if kind == "cancel" and target_status in {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIALLY_FILLED,
        }:
            pending = metadata.get("pending_modification")
            try:
                # An amended child reports fills for only its own segment.
                # Compare the post-cancel total against cumulative fills from
                # the original chain plus the current child remainder.
                filled, _ = _broker_cumulative_fill(
                    row, original, by_provider_id
                )
                remaining = Decimal(
                    str(getattr(original, "remaining_quantity", "0"))
                )
            except (ArithmeticError, TypeError, ValueError):
                filled = remaining = Decimal("0")
            try:
                expected_total = Decimal(
                    str(
                        pending.get("expected_total_after_cancel")
                        if isinstance(pending, dict)
                        else ""
                    )
                )
            except (ArithmeticError, TypeError, ValueError):
                expected_total = Decimal("-1")
            if not isinstance(pending, dict):
                # v0.1.74 retained no requested cancel quantity.  A fresh
                # parent snapshot whose effective total is smaller than the
                # pre-cancel ledger total is nevertheless affirmative broker
                # evidence that a cancel was accepted.  Prefer that evidence
                # over a stale operator NOT_ACCEPTED confirmation.
                try:
                    previous_total = Decimal(
                        str(_effective_request_payload(row).get("quantity") or "0")
                    )
                except (ArithmeticError, TypeError, ValueError):
                    previous_total = Decimal("0")
                observed_total = filled + remaining
                if (
                    previous_total > 0
                    and observed_total >= filled
                    and observed_total < previous_total
                ):
                    updates = _resolved_modification_metadata(
                        row, resolution="auto_cancel_reconciled"
                    )
                    current = _effective_request_payload(row)
                    updates["effective_request"] = {
                        "quantity": str(observed_total),
                        "order_type": str(current.get("order_type")),
                        "price": current.get("price"),
                        "route": str(
                            current.get("route") or OrderRoute.AUTO.value
                        ),
                    }
                    if remaining == 0:
                        updates["cancel_reconciled_full"] = True
                    working = dict(row)
                    working["provider_metadata"] = updates
                    return working, original, candidates, updates
            if expected_total >= 0 and filled + remaining == expected_total:
                updates = _resolved_modification_metadata(
                    row, resolution="auto_cancel_reconciled"
                )
                current = _effective_request_payload(row)
                updates["effective_request"] = {
                    "quantity": str(expected_total),
                    "order_type": str(current.get("order_type")),
                    "price": current.get("price"),
                    "route": str(current.get("route") or OrderRoute.AUTO.value),
                }
                if remaining == 0:
                    updates["cancel_reconciled_full"] = True
                working = dict(row)
                working["provider_metadata"] = updates
                return working, original, candidates, updates
        if kind == "amend" and target_status in {
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }:
            # Successful amendments commonly cancel/replace the original.
            # Without a relation-verified successor, resolving the parent
            # would orphan a potentially live child order.
            updates["match_result"] = "amendment_successor_missing"
            return row, None, candidates, updates
        if target_status in {
            OrderStatus.CANCELLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
        }:
            updates = _resolved_modification_metadata(
                row, resolution="broker_terminal"
            )
            working = dict(row)
            working["provider_metadata"] = updates
            return working, original, candidates, updates
        return row, original, candidates, updates

    def _apply_broker_snapshot(
        self,
        row: dict[str, Any],
        broker_order: object,
        *,
        by_provider_id: dict[str, object],
        open_provider_ids: set[str],
        provider_metadata_updates: dict[str, object] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        target_status = _reconciled_order_status(
            broker_order, open_provider_ids=open_provider_ids
        )
        filled_quantity, average_fill_price = _broker_cumulative_fill(
            row, broker_order, by_provider_id
        )
        previous_quantity = Decimal(str(row.get("filled_quantity", "0")))
        if filled_quantity < previous_quantity:
            raise ValueError("Broker fill quantity moved backwards.")
        target_status = _preserve_pending_status(
            row,
            target_status,
            filled_quantity,
            Decimal(str(getattr(broker_order, "remaining_quantity", "0"))),
        )
        fill = self._record_fill_delta(
            row,
            previous_quantity=previous_quantity,
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
        if provider_metadata_updates:
            provider_metadata.update(provider_metadata_updates)
        changed = self._store.update_order(
            str(row["id"]),
            status=target_status,
            provider_order_id=str(getattr(broker_order, "provider_order_id")),
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
        return changed, fill

    async def _reconcile_account(self, account_alias: str) -> dict[str, object]:
        """Refresh broker state and resolve ledger rows before opening orders."""

        capabilities = self._gateway.capabilities(account_alias)
        if not capabilities.accounts or not capabilities.order_history:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 거래 계좌는 잔고와 주문 조정 기능을 지원하지 않습니다.",
            )
        await self._gateway.balance(account_alias, fresh=True)
        # Scope in SQL instead of filtering the globally limited result.  A
        # busy installation can have more than 1,000 unresolved rows across
        # accounts; reconciliation for this account must not silently miss
        # its older orders.
        unresolved = self._store.unresolved_orders(account_alias=account_alias)
        snapshots, open_provider_ids = await self._broker_order_snapshots(
            account_alias, unresolved, include_today=True, fresh=True
        )
        still_unknown: list[str] = []
        for row in unresolved:
            started_unknown = row["status"] in {
                OrderStatus.SUBMITTING.value,
                OrderStatus.UNKNOWN_RECONCILING.value,
            }
            business_date = _order_business_date(row)
            broker_orders = snapshots.get(business_date, ())
            by_provider_id = _orders_by_provider_id(broker_orders)
            provider_order_id = row.get("provider_order_id")
            if not provider_order_id:
                if row["status"] == OrderStatus.SUBMITTING.value:
                    row = self._store.update_order(
                        row["id"],
                        status=OrderStatus.UNKNOWN_RECONCILING,
                        provider_metadata=_unknown_submit_metadata(
                            row,
                            origin="restart_submitting",
                            failure_phase="reconcile",
                        ),
                        message="재시작 전에 제출 중이던 주문의 접수 여부를 확인해야 합니다.",
                    )
                candidates = self._unknown_order_candidates(row, broker_orders)
                if len(candidates) != 1:
                    manual_time_candidates = (
                        self._unknown_manual_time_candidates(row, broker_orders)
                        if not candidates
                        else ()
                    )
                    if manual_time_candidates:
                        candidates = manual_time_candidates
                    self._store.update_order(
                        row["id"],
                        status=OrderStatus.UNKNOWN_RECONCILING,
                        provider_metadata=_unknown_reconcile_metadata(
                            row,
                            candidate_count=len(candidates),
                            match_result=(
                                "manual_required_no_order_time"
                                if manual_time_candidates
                                else "none" if not candidates else "ambiguous"
                            ),
                        ),
                        message=(
                            "증권사 주문시각이 없어 자동 연결할 수 없습니다. HTS 주문번호를 확인해 수동 연결하세요."
                            if manual_time_candidates
                            else _unknown_candidate_message(len(candidates))
                        ),
                    )
                    still_unknown.append(str(row["id"]))
                    continue
                broker_order = candidates[0]
                provider_order_id = str(
                    getattr(broker_order, "provider_order_id", "")
                )
                metadata_updates = {
                    **_unknown_reconcile_metadata(
                        row, candidate_count=1, match_result="linked"
                    ),
                    "resolution": "auto_linked",
                }
            else:
                metadata = dict(row.get("provider_metadata") or {})
                if metadata.get("uncertain_modification"):
                    row, broker_order, _, metadata_updates = (
                        self._uncertain_modification_snapshot(
                            row,
                            broker_orders,
                            by_provider_id=by_provider_id,
                            open_provider_ids=open_provider_ids,
                        )
                    )
                    if broker_order is None:
                        self._store.update_order(
                            str(row["id"]),
                            status=OrderStatus.UNKNOWN_RECONCILING,
                            provider_metadata=metadata_updates,
                            message="정정·취소 접수 여부를 증권사 주문내역에서 확정하지 못했습니다.",
                        )
                        still_unknown.append(str(row["id"]))
                        continue
                else:
                    broker_order = by_provider_id.get(str(provider_order_id))
                    metadata_updates = None
            if broker_order is None:
                still_unknown.append(str(row["id"]))
                continue
            try:
                changed, fill = self._apply_broker_snapshot(
                    row,
                    broker_order,
                    by_provider_id=by_provider_id,
                    open_provider_ids=open_provider_ids,
                    provider_metadata_updates=metadata_updates,
                )
            except ValueError:
                still_unknown.append(str(row["id"]))
                continue
            if changed["status"] == OrderStatus.UNKNOWN_RECONCILING.value:
                still_unknown.append(str(row["id"]))
                continue
            if started_unknown:
                self._store.record(
                    source=str(row["strategy_id"]),
                    event_type="order.reconciled",
                    message="증권사 주문내역에서 미확정 주문 상태를 안전하게 조정했습니다.",
                    details={
                        "order_id": str(row["id"]),
                        "provider_order_id": str(
                            getattr(broker_order, "provider_order_id", "")
                        ),
                        "resolution": str(
                            dict(changed.get("provider_metadata") or {}).get(
                                "resolution"
                            )
                            or dict(changed.get("provider_metadata") or {}).get(
                                "modification_resolution"
                            )
                            or "startup_reconciled"
                        ),
                    },
                )
            # Publish the reconciled snapshot (and any newly persisted fill)
            # before the account gate is opened so the UI/strategy process
            # cannot observe an armed account with stale order ownership.
            await self._publish_order(changed)
            if fill is not None and self._event_publisher is not None:
                await self._event_publisher(
                    str(row["strategy_id"]),
                    {"event_type": "fill", "fill": fill},
                    None,
                )

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
            "broker_order_count": sum(len(items) for items in snapshots.values()),
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
            polled_rows = [
                row
                for row in unresolved
                if (
                    _background_unknown_reconcile_due(row)
                    if row["status"] == OrderStatus.UNKNOWN_RECONCILING.value
                    else bool(row.get("provider_order_id"))
                    or row["status"] == OrderStatus.SUBMITTING.value
                )
            ]
            # UNKNOWN rows use bounded backoff, including uncertain amendments
            # and cancellations that retain an original provider id. Do not perform
            # account-history requests, writes or websocket publications while
            # every such row is still inside its retry delay. Confirmed
            # ACK/PARTIAL/CANCEL_PENDING rows remain on the normal poll cadence.
            if not polled_rows:
                return []
            capabilities = self._gateway.capabilities(account_alias)
            if not capabilities.order_history:
                raise RouterError(
                    RouterErrorCode.UNSUPPORTED_CAPABILITY,
                    "이 거래 계좌는 주문·체결 조회를 지원하지 않습니다.",
                )
            snapshots, open_provider_ids = await self._broker_order_snapshots(
                account_alias, polled_rows
            )
            updated: list[dict[str, object]] = []
            for row in polled_rows:
                broker_orders = snapshots.get(_order_business_date(row), ())
                by_provider_id = _orders_by_provider_id(broker_orders)
                provider_order_id = row.get("provider_order_id")
                if not provider_order_id:
                    if row["status"] == OrderStatus.SUBMITTING.value:
                        row = self._store.update_order(
                            str(row["id"]),
                            status=OrderStatus.UNKNOWN_RECONCILING,
                            provider_metadata=_unknown_submit_metadata(
                                row,
                                origin="restart_submitting",
                                failure_phase="reconcile",
                            ),
                            message="재시작 전에 제출 중이던 주문의 접수 여부를 확인해야 합니다.",
                        )
                    candidates = self._unknown_order_candidates(row, broker_orders)
                    if len(candidates) != 1:
                        manual_time_candidates = (
                            self._unknown_manual_time_candidates(row, broker_orders)
                            if not candidates
                            else ()
                        )
                        if manual_time_candidates:
                            candidates = manual_time_candidates
                        changed = self._store.update_order(
                            str(row["id"]),
                            status=OrderStatus.UNKNOWN_RECONCILING,
                            provider_metadata=_unknown_reconcile_metadata(
                                row,
                                candidate_count=len(candidates),
                                match_result=(
                                    "manual_required_no_order_time"
                                    if manual_time_candidates
                                    else "none" if not candidates else "ambiguous"
                                ),
                            ),
                            message=(
                                "증권사 주문시각이 없어 자동 연결할 수 없습니다. HTS 주문번호를 확인해 수동 연결하세요."
                                if manual_time_candidates
                                else _unknown_candidate_message(len(candidates))
                            ),
                        )
                        self._reconciled_accounts.discard(account_alias)
                        await self._publish_order(changed)
                        updated.append(changed)
                        continue
                    broker_order = candidates[0]
                    provider_order_id = str(
                        getattr(broker_order, "provider_order_id", "")
                    )
                    metadata_updates = {
                        **_unknown_reconcile_metadata(
                            row, candidate_count=1, match_result="linked"
                        ),
                        "resolution": "auto_linked",
                    }
                else:
                    metadata = dict(row.get("provider_metadata") or {})
                    if metadata.get("uncertain_modification"):
                        row, broker_order, _, metadata_updates = (
                            self._uncertain_modification_snapshot(
                                row,
                                broker_orders,
                                by_provider_id=by_provider_id,
                                open_provider_ids=open_provider_ids,
                            )
                        )
                        if broker_order is None:
                            changed = self._store.update_order(
                                str(row["id"]),
                                status=OrderStatus.UNKNOWN_RECONCILING,
                                provider_metadata=metadata_updates,
                                message=_uncertain_modification_message(
                                    metadata_updates
                                ),
                            )
                            self._reconciled_accounts.discard(account_alias)
                            await self._publish_order(changed)
                            updated.append(changed)
                            continue
                    else:
                        broker_order = by_provider_id.get(str(provider_order_id))
                        metadata_updates = None
                if broker_order is None:
                    continue
                provider_order_id = str(
                    getattr(broker_order, "provider_order_id", provider_order_id)
                )
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
                if metadata_updates:
                    provider_metadata.update(metadata_updates)
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
                        provider_order_id=str(provider_order_id),
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

    def order_by_client_id(
        self,
        *,
        manifest: StrategyManifest,
        client_order_id: str,
    ) -> Order | None:
        """Read one exact central-ledger row without broker reconciliation.

        This intentionally includes terminal and ``UNKNOWN_RECONCILING`` rows.
        Managed strategies use it only to distinguish a legacy local reservation
        that never reached the central ledger from a real unresolved order.
        """

        row = self._store.order_by_client_id(
            manifest.strategy_id,
            str(client_order_id).strip(),
        )
        if row is None or row.get("account_alias") != manifest.trading_account:
            return None
        return _order_from_row(row)

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
        business_date = datetime.now(SEOUL).date().isoformat()
        signature = _provider_notice_event_key(notice, business_date)
        if not self._store.claim_provider_event(
            provider=str(notice.get("provider") or "unknown"),
            account_alias=account_alias,
            event_key=signature,
        ):
            return None
        row = self._store.order_by_provider_id(
            account_alias,
            provider_order_id,
            business_date=business_date,
        )
        if row is None:
            original = str(notice.get("original_order_id") or "").strip()
            if original:
                row = self._store.order_by_provider_id(
                    account_alias,
                    original,
                    business_date=business_date,
                )
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
                    restored_metadata = (
                        _resolved_modification_metadata(
                            row, resolution="realtime_rejected"
                        )
                        if modification_notice
                        else dict(row.get("provider_metadata") or {})
                    )
                    row = self._store.update_order(
                        str(row["id"]),
                        status=(
                            _modification_previous_status(row)
                            if modification_notice else OrderStatus.REJECTED
                        ),
                        provider_metadata=restored_metadata,
                        message=(
                            "정정·취소 요청 거절 통보를 받아 원주문 상태를 복원했습니다."
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
            protective_sell_ignored_order_ids: frozenset[str] = frozenset()
            if account_alias not in self._reconciled_accounts:
                try:
                    await self._reconcile_account(account_alias)
                except RouterError as exc:
                    if not self._can_bypass_unknown_gate_for_protective_sell(
                        account_alias, request, exc
                    ):
                        raise
                    protective_sell_ignored_order_ids = frozenset(
                        str(order_id)
                        for order_id in exc.details.get("order_ids") or ()
                    )
                    self._store.record(
                        source=strategy_name,
                        event_type="order.protective_sell_allowed",
                        message=(
                            "다른 종목의 미확정 매수가 있지만 보유 위험을 줄이는 "
                            "매도를 계속합니다."
                        ),
                        details={
                            "code": request.instrument.code,
                            "blocking_order_ids": sorted(
                                protective_sell_ignored_order_ids
                            ),
                        },
                    )

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
                    fresh=bool(protective_sell_ignored_order_ids),
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

            reserved_amount = await self._preflight(
                manifest,
                request,
                protective_sell_ignored_order_ids=(
                    protective_sell_ignored_order_ids
                ),
            )
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
                provider_metadata = None
                if status is OrderStatus.UNKNOWN_RECONCILING:
                    provider_metadata = _unknown_submit_metadata(
                        row,
                        origin=str(
                            exc.details.get("unknown_origin")
                            or "unexpected_adapter_error"
                        ),
                        failure_phase="submit",
                        exception_type=(
                            _safe_metadata_text(exc.details.get("exception_type"))
                            or type(exc).__name__
                        ),
                        winerror=_safe_winerror(exc.details.get("winerror")),
                        http_status=_safe_http_status(exc.details.get("http_status")),
                    )
                row = self._store.update_order(
                    row["id"],
                    status=status,
                    provider_metadata=provider_metadata,
                    message=exc.message,
                )
                await self._publish_order(
                    row, strategy_name=strategy_name
                )
                if status is OrderStatus.UNKNOWN_RECONCILING:
                    self._reconciled_accounts.discard(account_alias)
                self._store.record(
                    source=strategy_name,
                    event_type="order.unknown" if status is OrderStatus.UNKNOWN_RECONCILING else "order.rejected",
                    message=(
                        "주문 응답을 확정하지 못해 증권사 주문내역 조정이 필요합니다."
                        if status is OrderStatus.UNKNOWN_RECONCILING
                        else exc.message
                    ),
                    details=(
                        {
                            **_unknown_event_details(row),
                            "code": exc.code.value,
                        }
                        if status is OrderStatus.UNKNOWN_RECONCILING
                        else {"order_id": row["id"], "code": exc.code.value}
                    ),
                )
                if status is OrderStatus.UNKNOWN_RECONCILING:
                    return _order_from_row(row)
                # The adapter was called and returned a terminal error.  Keep
                # that boundary explicit so API clients do not mistake a
                # broker-side rejection for a local pre-submit validation.
                exc.details.setdefault("submission_started", True)
                raise
            except Exception as exc:
                # The adapter crossed the submission boundary. Never retry an
                # unclassified failure because the broker may have accepted it.
                row = self._store.update_order(
                    row["id"],
                    status=OrderStatus.UNKNOWN_RECONCILING,
                    provider_metadata=_unknown_submit_metadata(
                        row,
                        origin="unexpected_adapter_error",
                        failure_phase="submit",
                        exception_type=type(exc).__name__,
                        winerror=_exception_winerror(exc),
                    ),
                    message="주문 응답을 확인하지 못해 증권사 주문내역 조정이 필요합니다.",
                )
                self._reconciled_accounts.discard(account_alias)
                self._store.record(
                    source=strategy_name,
                    event_type="order.unknown",
                    message="주문 응답을 확인하지 못해 증권사 주문내역 조정이 필요합니다.",
                    details=_unknown_event_details(row),
                )
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
        modification_requested_at = datetime.now(SEOUL).isoformat()
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
            unknown_metadata = _unknown_submit_metadata(
                row,
                origin=str(
                    exc.details.get("unknown_origin") or "unexpected_adapter_error"
                ),
                failure_phase="modify",
                exception_type=(
                    _safe_metadata_text(exc.details.get("exception_type"))
                    or type(exc).__name__
                ),
                winerror=_safe_winerror(exc.details.get("winerror")),
                http_status=_safe_http_status(exc.details.get("http_status")),
            )
            unknown_metadata["uncertain_modification"] = "amend"
            unknown_metadata["pending_modification"] = {
                "kind": "amend",
                "previous_status": str(row["status"]),
                "requested_at": modification_requested_at,
                "quantity": str(amended_quantity),
                "order_type": order_type.value,
                "price": str(price) if price is not None else None,
                "route": normalized_route.value,
            }
            changed = self._store.update_order(
                order_id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata=unknown_metadata,
                message=exc.message,
            )
            unknown_details = _unknown_event_details(changed)
            unknown_details["code"] = exc.code.value
            self._store.record(
                source=strategy_name,
                event_type="order.unknown",
                message="정정 응답을 확인하지 못해 증권사 주문내역 조정이 필요합니다.",
                details=unknown_details,
            )
            self._reconciled_accounts.discard(manifest.trading_account)
            await self._publish_order(changed, strategy_name=strategy_name)
            return _order_from_row(changed)
        except Exception as exc:
            unknown_metadata = _unknown_submit_metadata(
                row,
                origin="unexpected_adapter_error",
                failure_phase="modify",
                exception_type=type(exc).__name__,
                winerror=_exception_winerror(exc),
            )
            unknown_metadata["uncertain_modification"] = "amend"
            unknown_metadata["pending_modification"] = {
                "kind": "amend",
                "previous_status": str(row["status"]),
                "requested_at": modification_requested_at,
                "quantity": str(amended_quantity),
                "order_type": order_type.value,
                "price": str(price) if price is not None else None,
                "route": normalized_route.value,
            }
            changed = self._store.update_order(
                order_id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata=unknown_metadata,
                message="정정 응답을 확인하지 못해 주문내역 조정이 필요합니다.",
            )
            self._store.record(
                source=strategy_name,
                event_type="order.unknown",
                message="정정 응답을 확인하지 못해 증권사 주문내역 조정이 필요합니다.",
                details=_unknown_event_details(changed),
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
        previous_effective_quantity = Decimal(str(effective_request["quantity"]))
        cancellation_quantity = quantity if quantity is not None else remaining
        expected_total_after_cancel = (
            previous_effective_quantity - cancellation_quantity
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
        modification_requested_at = datetime.now(SEOUL).isoformat()
        try:
            acknowledgement = await self._gateway.cancel_order(
                manifest.trading_account,
                str(row["provider_order_id"]),
                quantity=quantity,
                route=normalized_route,
            )
        except RouterError as exc:
            if exc.code is RouterErrorCode.ORDER_STATUS_UNKNOWN:
                unknown_metadata = _unknown_submit_metadata(
                    row,
                    origin=str(
                        exc.details.get("unknown_origin")
                        or "unexpected_adapter_error"
                    ),
                    failure_phase="modify",
                    exception_type=(
                        _safe_metadata_text(exc.details.get("exception_type"))
                        or type(exc).__name__
                    ),
                    winerror=_safe_winerror(exc.details.get("winerror")),
                    http_status=_safe_http_status(exc.details.get("http_status")),
                )
                unknown_metadata["uncertain_modification"] = "cancel"
                unknown_metadata["pending_modification"] = {
                    "kind": "cancel",
                    "previous_status": prior_status.value,
                    "requested_at": modification_requested_at,
                    "quantity": str(cancellation_quantity),
                    "previous_effective_quantity": str(previous_effective_quantity),
                    "expected_total_after_cancel": str(expected_total_after_cancel),
                    "route": normalized_route.value,
                }
                unknown = self._store.update_order(
                    order_id,
                    status=OrderStatus.UNKNOWN_RECONCILING,
                    provider_metadata=unknown_metadata,
                    message=exc.message,
                )
                unknown_details = _unknown_event_details(unknown)
                unknown_details["code"] = exc.code.value
                self._store.record(
                    source=strategy_name,
                    event_type="order.unknown",
                    message="취소 응답을 확인하지 못해 증권사 주문내역 조정이 필요합니다.",
                    details=unknown_details,
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
        except Exception as exc:
            unknown_metadata = _unknown_submit_metadata(
                row,
                origin="unexpected_adapter_error",
                failure_phase="modify",
                exception_type=type(exc).__name__,
                winerror=_exception_winerror(exc),
            )
            unknown_metadata["uncertain_modification"] = "cancel"
            unknown_metadata["pending_modification"] = {
                "kind": "cancel",
                "previous_status": prior_status.value,
                "requested_at": modification_requested_at,
                "quantity": str(cancellation_quantity),
                "previous_effective_quantity": str(previous_effective_quantity),
                "expected_total_after_cancel": str(expected_total_after_cancel),
                "route": normalized_route.value,
            }
            unknown = self._store.update_order(
                order_id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata=unknown_metadata,
                message="취소 응답을 확인하지 못해 주문내역 조정이 필요합니다.",
            )
            self._store.record(
                source=strategy_name,
                event_type="order.unknown",
                message="취소 응답을 확인하지 못해 증권사 주문내역 조정이 필요합니다.",
                details=_unknown_event_details(unknown),
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
            expected_total_after_cancel
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

    def _can_bypass_unknown_gate_for_protective_sell(
        self,
        account_alias: str,
        request: OrderRequest,
        error: RouterError,
    ) -> bool:
        """Allow only an unrelated position-reducing sell past a strict gate.

        The strict reconciliation error supplies the complete set of rows it
        could not resolve while the account submission lock is held. A sell
        may continue only when every such row is a BUY for another symbol.
        Same-symbol uncertainty, any uncertain SELL, and every new BUY remain
        blocked. The caller still performs a fresh broker open-order check and
        a fresh sellable-balance check before submitting the order.
        """

        if (
            request.side is not OrderSide.SELL
            or error.code is not RouterErrorCode.ORDER_STATUS_UNKNOWN
        ):
            return False
        blocking_order_ids = {
            str(order_id).strip()
            for order_id in error.details.get("order_ids", ())
            if str(order_id).strip()
        }
        if not blocking_order_ids:
            return False
        rows_by_id = {
            str(row["id"]): row
            for row in self._store.unresolved_orders(account_alias=account_alias)
        }
        blocking_rows = [
            rows_by_id[order_id]
            for order_id in blocking_order_ids
            if order_id in rows_by_id
        ]
        if len(blocking_rows) != len(blocking_order_ids):
            return False
        return all(
            str(row["request"].get("side") or "") == OrderSide.BUY.value
            and bool(str(row["request"].get("code") or "").strip())
            and str(row["request"].get("code") or "").strip()
            != request.instrument.code
            for row in blocking_rows
        )

    async def _preflight(
        self,
        manifest: StrategyManifest,
        request: OrderRequest,
        *,
        protective_sell_ignored_order_ids: frozenset[str] = frozenset(),
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
        strategy_open_count = sum(
            1
            for row in self._store.unresolved_orders(
                strategy_id=manifest.strategy_id
            )
            if str(row["id"]) not in protective_sell_ignored_order_ids
        )
        if max_open_orders is not None and strategy_open_count >= max_open_orders:
            raise RouterError(
                RouterErrorCode.RISK_LIMIT_EXCEEDED,
                "전략의 최대 미체결 주문 수를 초과합니다.",
            )
        account_max_open = _optional_int(account_limits.get("max_open_orders"))
        account_open_count = sum(
            1
            for row in self._store.unresolved_orders(
                account_alias=manifest.trading_account
            )
            if str(row["id"]) not in protective_sell_ignored_order_ids
        )
        if account_max_open is not None and account_open_count >= account_max_open:
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
            balance = await self._gateway.balance(
                account_alias,
                fresh=bool(protective_sell_ignored_order_ids),
            )
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
            if request.sell_buy_flow_ids or request.sell_buy_node_refs:
                origin_lots = self._store.strategy_buy_origin_lots(
                    manifest.strategy_id, account_alias=account_alias,
                    actual_quantities={item.instrument.code: item.quantity for item in balance.positions},
                    as_of=balance.as_of,
                ).get(request.instrument.code, [])
                linked_quantity = sum((lot["quantity"] for lot in origin_lots if (
                    lot["flow_id"] in request.sell_buy_flow_ids
                    or any(ref["flow_id"] == lot["flow_id"] and ref["node_id"] in lot["node_ids"] for ref in request.sell_buy_node_refs)
                )), Decimal("0"))
                if request.quantity > linked_quantity:
                    raise RouterError(
                        RouterErrorCode.RISK_LIMIT_EXCEEDED,
                        "연결한 매수 조건으로 체결된 보유 수량보다 매도 요청 수량이 큽니다.",
                        details={"linked_quantity": str(linked_quantity)},
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
        "sell_position_scope": request.sell_position_scope,
        "buy_flow_id": request.buy_flow_id,
        "sell_buy_flow_ids": list(request.sell_buy_flow_ids),
        "buy_rule_node_ids": list(request.buy_rule_node_ids),
        "sell_buy_node_refs": list(request.sell_buy_node_refs),
        "reserved_amount": str(reserved_amount),
    }


def _same_request(stored: dict[str, object], request: OrderRequest) -> bool:
    comparison = _request_payload(request, reserved_amount=Decimal("0"))
    comparison.pop("reserved_amount")
    normalized = {"sell_position_scope": "STRATEGY", "buy_flow_id": None, "sell_buy_flow_ids": [], "buy_rule_node_ids": [], "sell_buy_node_refs": [], **stored}
    return all(normalized.get(key) == value for key, value in comparison.items())


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


def _order_business_date(row: dict[str, object]) -> date:
    created_at = datetime.fromisoformat(str(row["created_at"]))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=SEOUL)
    return created_at.astimezone(SEOUL).date()


def _orders_by_provider_id(
    broker_orders: tuple[object, ...] | list[object],
) -> dict[str, object]:
    return {
        str(getattr(item, "provider_order_id")): item
        for item in broker_orders
        if getattr(item, "provider_order_id", None)
    }


def _enum_text(value: object) -> str:
    enum_value = getattr(value, "value", value)
    return str(enum_value or "").strip().upper()


def _provider_notice_event_key(
    notice: dict[str, object], business_date: str
) -> str:
    """Scope broker-notice de-duplication to its Seoul receiving date."""

    payload = {"business_date": business_date, "notice": notice}
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _broker_order_matches_unknown(
    row: dict[str, object],
    broker_order: object,
    *,
    enforce_time_window: bool,
) -> bool:
    """Match only immutable request fields; uniqueness is checked separately."""

    request = _effective_request_payload(row)
    required = ("code", "side", "quantity", "order_type")
    if any(request.get(key) in {None, ""} for key in required):
        return False
    instrument = getattr(broker_order, "instrument", None)
    if str(getattr(instrument, "code", "")) != str(request["code"]):
        return False
    if _enum_text(getattr(broker_order, "side", "")) != _enum_text(request["side"]):
        return False
    try:
        requested_quantity = Decimal(str(request["quantity"]))
        broker_quantity = Decimal(str(getattr(broker_order, "requested_quantity")))
    except (ArithmeticError, TypeError, ValueError):
        return False
    if requested_quantity <= 0 or requested_quantity != broker_quantity:
        return False
    order_type = _enum_text(request["order_type"])
    if order_type != _enum_text(getattr(broker_order, "order_type", "")):
        return False
    if order_type == OrderType.LIMIT.value:
        if request.get("price") in {None, ""}:
            return False
        try:
            if Decimal(str(request["price"])) != Decimal(
                str(getattr(broker_order, "price"))
            ):
                return False
        except (ArithmeticError, TypeError, ValueError):
            return False
    requested_route = _enum_text(request.get("route") or OrderRoute.AUTO.value)
    broker_route = _enum_text(getattr(broker_order, "route", OrderRoute.AUTO))
    if requested_route == OrderRoute.SOR.value:
        if broker_route not in {
            OrderRoute.SOR.value,
            OrderRoute.KRX.value,
            OrderRoute.NXT.value,
        }:
            return False
    elif requested_route != OrderRoute.AUTO.value and requested_route != broker_route:
        return False

    try:
        submitted_at = datetime.fromisoformat(str(row["created_at"]))
        ordered_at = getattr(broker_order, "ordered_at")
        if not isinstance(ordered_at, datetime):
            return False
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=SEOUL)
        if ordered_at.tzinfo is None:
            ordered_at = ordered_at.replace(tzinfo=SEOUL)
        submitted_at = submitted_at.astimezone(SEOUL)
        ordered_at = ordered_at.astimezone(SEOUL)
    except (TypeError, ValueError):
        return False
    if submitted_at.date() != ordered_at.date():
        return False
    if enforce_time_window:
        unknown_at = submitted_at
        metadata = row.get("provider_metadata")
        unknown_value = (
            metadata.get("first_unknown_at")
            if isinstance(metadata, dict) and metadata.get("first_unknown_at")
            else None
        )
        if unknown_value:
            try:
                parsed_unknown_at = datetime.fromisoformat(
                    str(unknown_value)
                )
                if parsed_unknown_at.tzinfo is None:
                    parsed_unknown_at = parsed_unknown_at.replace(tzinfo=SEOUL)
                parsed_unknown_at = parsed_unknown_at.astimezone(SEOUL)
                if parsed_unknown_at.date() == submitted_at.date():
                    # A legacy row can be updated long after submission; do
                    # not turn that later write into an all-day match window.
                    unknown_at = min(
                        max(submitted_at, parsed_unknown_at),
                        submitted_at + timedelta(minutes=2),
                    )
            except (TypeError, ValueError):
                pass
        if ordered_at < submitted_at - UNKNOWN_ORDER_EARLY_TOLERANCE:
            return False
        if ordered_at > unknown_at + UNKNOWN_ORDER_LATE_TOLERANCE:
            return False
    return True


def _broker_order_matches_pending_modification(
    row: dict[str, object],
    broker_order: object,
    pending: dict[str, object],
    *,
    enforce_time_window: bool = True,
) -> bool:
    requested_at = pending.get("requested_at")
    if not requested_at:
        return False
    request = _effective_request_payload(row)
    request.update(
        {
            "quantity": pending.get("quantity"),
            "order_type": pending.get("order_type"),
            "price": pending.get("price"),
            "route": pending.get("route") or request.get("route"),
        }
    )
    synthetic = dict(row)
    synthetic["request"] = request
    synthetic["created_at"] = str(requested_at)
    synthetic["provider_metadata"] = {
        "first_unknown_at": dict(row.get("provider_metadata") or {}).get(
            "first_unknown_at"
        )
        or requested_at
    }
    if _broker_order_matches_unknown(
        synthetic, broker_order, enforce_time_window=enforce_time_window
    ):
        return True
    # Some histories report an amendment child as a cumulative order: prior
    # fills plus the replacement remainder.  Accept that alternate quantity
    # only when the broker explicitly relates the child to this parent.
    broker_parent = str(
        getattr(broker_order, "original_provider_order_id", "") or ""
    ).strip()
    if broker_parent != str(row.get("provider_order_id") or "").strip():
        return False
    try:
        previous_filled = Decimal(str(row.get("filled_quantity") or "0"))
        pending_quantity = Decimal(str(pending.get("quantity") or "0"))
        cumulative_quantity = previous_filled + pending_quantity
        broker_quantity = Decimal(
            str(getattr(broker_order, "requested_quantity", "0"))
        )
    except (ArithmeticError, TypeError, ValueError):
        return False
    if previous_filled <= 0 or broker_quantity != cumulative_quantity:
        return False
    cumulative_request = dict(request)
    cumulative_request["quantity"] = str(cumulative_quantity)
    cumulative_synthetic = dict(synthetic)
    cumulative_synthetic["request"] = cumulative_request
    return _broker_order_matches_unknown(
        cumulative_synthetic,
        broker_order,
        enforce_time_window=enforce_time_window,
    )


def _resolved_modification_metadata(
    row: dict[str, object], *, resolution: str
) -> dict[str, object]:
    metadata = dict(row.get("provider_metadata") or {})
    pending = metadata.get("pending_modification")
    if (
        resolution in {"manual_modification_not_accepted", "realtime_rejected"}
        and isinstance(pending, dict)
        and pending.get("kind") == "cancel"
        and pending.get("previous_effective_quantity") not in {None, ""}
    ):
        effective = dict(metadata.get("effective_request") or {})
        effective["quantity"] = str(pending["previous_effective_quantity"])
        metadata["effective_request"] = effective
    metadata.pop("uncertain_modification", None)
    metadata.pop("pending_modification", None)
    metadata["modification_resolution"] = resolution
    metadata["match_result"] = resolution
    metadata["last_reconcile_at"] = datetime.now(SEOUL).isoformat()
    return metadata


def _modification_previous_status(row: dict[str, object]) -> OrderStatus:
    metadata = dict(row.get("provider_metadata") or {})
    pending = metadata.get("pending_modification")
    previous = pending.get("previous_status") if isinstance(pending, dict) else None
    try:
        status = OrderStatus(str(previous))
    except (TypeError, ValueError):
        status = (
            OrderStatus.PARTIALLY_FILLED
            if Decimal(str(row.get("filled_quantity") or "0")) > 0
            else OrderStatus.ACKNOWLEDGED
        )
    if status not in {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}:
        return (
            OrderStatus.PARTIALLY_FILLED
            if Decimal(str(row.get("filled_quantity") or "0")) > 0
            else OrderStatus.ACKNOWLEDGED
        )
    return status


def _row_for_linked_amendment(
    row: dict[str, object], broker_order: object
) -> dict[str, object]:
    metadata = dict(row.get("provider_metadata") or {})
    pending = metadata.get("pending_modification")
    if not isinstance(pending, dict):
        return dict(row)
    child_provider_id = str(getattr(broker_order, "provider_order_id", ""))
    previous_filled = Decimal(str(row.get("filled_quantity") or "0"))
    previous_average = Decimal(str(row.get("average_fill_price") or "0"))
    offsets = dict(metadata.get("amendment_fill_offsets") or {})
    offsets[child_provider_id] = {
        "quantity": str(previous_filled),
        "amount": str(previous_filled * previous_average),
        "requested_quantity": str(pending.get("quantity") or "0"),
        "previous_provider_order_id": str(row.get("provider_order_id") or ""),
    }
    metadata["amendment_fill_offsets"] = offsets
    metadata["effective_request"] = {
        "quantity": str(
            previous_filled + Decimal(str(pending.get("quantity") or "0"))
        ),
        "order_type": str(pending.get("order_type") or OrderType.MARKET.value),
        "price": pending.get("price"),
        "route": str(pending.get("route") or OrderRoute.AUTO.value),
    }
    metadata.pop("uncertain_modification", None)
    metadata.pop("pending_modification", None)
    metadata.update(
        _unknown_reconcile_metadata(
            {**row, "provider_metadata": metadata},
            candidate_count=1,
            match_result="linked_modification",
        )
    )
    metadata["resolution"] = "auto_amendment_linked"
    metadata["modification_resolution"] = "auto_amendment_linked"
    working = dict(row)
    working["provider_metadata"] = metadata
    return working


def _row_for_linked_legacy_amendment(
    row: dict[str, object], broker_order: object
) -> dict[str, object]:
    """Build fill offsets from a relation-verified legacy amendment child."""

    metadata = dict(row.get("provider_metadata") or {})
    child_provider_id = str(getattr(broker_order, "provider_order_id", ""))
    child_quantity = Decimal(
        str(getattr(broker_order, "requested_quantity", "0"))
    )
    previous_filled = Decimal(str(row.get("filled_quantity") or "0"))
    previous_average = Decimal(str(row.get("average_fill_price") or "0"))
    offsets = dict(metadata.get("amendment_fill_offsets") or {})
    offsets[child_provider_id] = {
        "quantity": str(previous_filled),
        "amount": str(previous_filled * previous_average),
        "requested_quantity": str(child_quantity),
        "previous_provider_order_id": str(row.get("provider_order_id") or ""),
    }
    metadata["amendment_fill_offsets"] = offsets
    broker_order_type = getattr(broker_order, "order_type", OrderType.MARKET)
    broker_route = getattr(broker_order, "route", OrderRoute.AUTO)
    broker_price = getattr(broker_order, "price", None)
    metadata["effective_request"] = {
        "quantity": str(previous_filled + child_quantity),
        "order_type": _enum_text(broker_order_type),
        "price": (
            str(broker_price)
            if broker_price is not None and Decimal(str(broker_price)) > 0
            else None
        ),
        "route": _enum_text(broker_route) or OrderRoute.AUTO.value,
    }
    metadata.pop("uncertain_modification", None)
    metadata.pop("pending_modification", None)
    metadata.update(
        _unknown_reconcile_metadata(
            {**row, "provider_metadata": metadata},
            candidate_count=1,
            match_result="manual_linked_modification",
        )
    )
    metadata["resolution"] = "manual_amendment_linked"
    metadata["modification_resolution"] = "manual_amendment_linked"
    working = dict(row)
    working["provider_metadata"] = metadata
    return working


def _legacy_amendment_fill_semantics_are_safe(
    row: dict[str, object], original_broker_order: object | None
) -> bool:
    """Reject legacy links whose cumulative/segment fill meaning is unknown.

    With no persisted pending request, a child showing five fills after the
    parent had three could mean either five cumulative fills or five new
    fills.  Even a child showing zero fills can encode either a new segment
    quantity or a new cumulative target.  Linking it would silently under- or
    over-count inventory, so only a parent with no prior fills is unambiguous.
    Both the ledger and the fresh broker parent snapshot must confirm zero;
    the broker may have fills that arrived after the last local update.
    """

    try:
        ledger_filled = Decimal(str(row.get("filled_quantity") or "0"))
        if original_broker_order is None:
            return False
        broker_filled = Decimal(
            str(getattr(original_broker_order, "filled_quantity", "0"))
        )
    except (ArithmeticError, TypeError, ValueError):
        return False
    return ledger_filled <= 0 and broker_filled <= 0


def _safe_metadata_text(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or len(text) > 80:
        return None
    if not all(character.isalnum() or character in {"_", ".", "-"} for character in text):
        return None
    return text


def _safe_winerror(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if -1_000_000 <= parsed <= 1_000_000 else None


def _safe_http_status(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if 100 <= parsed <= 599 else None


def _safe_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _exception_winerror(exc: BaseException) -> int | None:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        winerror = _safe_winerror(getattr(current, "winerror", None))
        if winerror is not None:
            return winerror
        current = current.__cause__ or current.__context__
    return None


def _unknown_submit_metadata(
    row: dict[str, object],
    *,
    origin: str,
    failure_phase: str,
    exception_type: str | None = None,
    winerror: int | None = None,
    http_status: int | None = None,
) -> dict[str, object]:
    metadata = dict(row.get("provider_metadata") or {})
    now = datetime.now(SEOUL).isoformat()
    safe_origin = _safe_metadata_text(origin) or "unexpected_adapter_error"
    safe_phase = _safe_metadata_text(failure_phase) or "submit"
    first_unknown_at = metadata.get("first_unknown_at") or now
    if safe_origin == "restart_submitting":
        # A restart can occur hours after the process died. The response-time
        # window for a formerly SUBMITTING row starts and ends at submission,
        # not at the later restart time.
        first_unknown_at = str(row.get("created_at") or now)
    metadata.update(
        {
            "unknown_origin": safe_origin,
            "failure_phase": safe_phase,
            "first_unknown_at": first_unknown_at,
            "reconcile_attempt_count": _safe_nonnegative_int(
                metadata.get("reconcile_attempt_count")
            ),
            "last_reconcile_at": metadata.get("last_reconcile_at"),
            "candidate_count": _safe_nonnegative_int(
                metadata.get("candidate_count")
            ),
            "match_result": metadata.get("match_result") or "not_checked",
        }
    )
    safe_exception_type = _safe_metadata_text(exception_type)
    if safe_exception_type is not None:
        metadata["exception_type"] = safe_exception_type
    if winerror is not None:
        metadata["winerror"] = winerror
    if http_status is not None:
        metadata["http_status"] = http_status
    return metadata


def _unknown_reconcile_metadata(
    row: dict[str, object],
    *,
    candidate_count: int,
    match_result: str,
) -> dict[str, object]:
    metadata = dict(row.get("provider_metadata") or {})
    metadata.setdefault("unknown_origin", "legacy_unknown")
    metadata.setdefault("failure_phase", "reconcile")
    # A legacy row may have been updated hours after submission.  Using that
    # mutable timestamp would widen a later automatic-match window and could
    # attach an unrelated manual order.  Missing onset data stays pinned to
    # the immutable submission time.
    metadata.setdefault("first_unknown_at", str(row["created_at"]))
    metadata["reconcile_attempt_count"] = _safe_nonnegative_int(
        metadata.get("reconcile_attempt_count")
    ) + 1
    metadata["last_reconcile_at"] = datetime.now(SEOUL).isoformat()
    metadata["candidate_count"] = max(0, int(candidate_count))
    metadata["match_result"] = match_result
    return metadata


def _background_unknown_reconcile_due(row: dict[str, object]) -> bool:
    metadata = row.get("provider_metadata")
    if not isinstance(metadata, dict):
        return True
    last_value = metadata.get("last_reconcile_at")
    if not last_value:
        return True
    try:
        last_reconcile_at = datetime.fromisoformat(str(last_value))
        if last_reconcile_at.tzinfo is None:
            last_reconcile_at = last_reconcile_at.replace(tzinfo=SEOUL)
        last_reconcile_at = last_reconcile_at.astimezone(SEOUL)
    except (TypeError, ValueError):
        return True
    attempts = _safe_nonnegative_int(metadata.get("reconcile_attempt_count"))
    delay_seconds = min(60, 5 * (2 ** min(max(0, attempts - 1), 4)))
    return datetime.now(SEOUL) >= last_reconcile_at + timedelta(
        seconds=delay_seconds
    )


def _unknown_event_details(row: dict[str, object]) -> dict[str, object]:
    metadata = dict(row.get("provider_metadata") or {})
    details: dict[str, object] = {
        "order_id": str(row["id"]),
        "unknown_origin": str(
            metadata.get("unknown_origin") or "unexpected_adapter_error"
        ),
        "failure_phase": str(metadata.get("failure_phase") or "submit"),
        "provider_id_present": bool(row.get("provider_order_id")),
    }
    exception_type = _safe_metadata_text(metadata.get("exception_type"))
    if exception_type is not None:
        details["exception_type"] = exception_type
    winerror = _safe_winerror(metadata.get("winerror"))
    if winerror is not None:
        details["winerror"] = winerror
    http_status = _safe_http_status(metadata.get("http_status"))
    if http_status is not None:
        details["http_status"] = http_status
    return details


def _unknown_candidate_message(candidate_count: int) -> str:
    if candidate_count <= 0:
        return "증권사 주문내역에서 일치하는 주문을 찾지 못해 확인이 필요합니다."
    return f"증권사 주문내역에 일치 후보가 {candidate_count}건 있어 자동 연결하지 않았습니다."


def _uncertain_modification_message(metadata: dict[str, object]) -> str:
    match_result = str(metadata.get("match_result") or "")
    if match_result == "manual_required_no_order_time":
        return "증권사 정정 주문시각이 없어 자동 연결할 수 없습니다. HTS의 새 주문번호를 확인하세요."
    if match_result == "manual_required_missing_parent_relation":
        return "증권사 주문내역에 원주문 연결정보가 없어 자동 연결하지 않았습니다. HTS의 새 주문번호를 확인하세요."
    if match_result == "manual_required_legacy_modification":
        return "구버전 미확정 정정은 요청값을 복원할 수 없어 자동 연결하지 않았습니다. HTS의 새 주문번호를 확인하세요."
    if match_result == "amendment_rejected_parent_missing":
        return "거절된 정정 주문은 확인했지만 원주문 상태를 찾지 못해 확인이 필요합니다."
    return "정정·취소 접수 여부를 증권사 주문내역에서 확정하지 못했습니다."


def _broker_order_summary(broker_order: object) -> dict[str, object]:
    price = getattr(broker_order, "price", None)
    return {
        "provider_order_id": str(getattr(broker_order, "provider_order_id", "")),
        "original_provider_order_id": (
            str(getattr(broker_order, "original_provider_order_id", "") or "")
            or None
        ),
        "code": str(getattr(getattr(broker_order, "instrument", None), "code", "")),
        "side": _enum_text(getattr(broker_order, "side", "")),
        "order_type": _enum_text(getattr(broker_order, "order_type", "")),
        "requested_quantity": str(getattr(broker_order, "requested_quantity", "0")),
        "filled_quantity": str(getattr(broker_order, "filled_quantity", "0")),
        "price": str(price) if price is not None else None,
        "route": _enum_text(getattr(broker_order, "route", "")),
        "ordered_at": getattr(broker_order, "ordered_at").isoformat(),
        "status": _enum_text(getattr(broker_order, "status", "")),
    }


def _unknown_reconcile_result(
    row: dict[str, Any], candidates: tuple[object, ...] | list[object]
) -> dict[str, object]:
    metadata = dict(row.get("provider_metadata") or {})
    return {
        "order": row,
        "resolved": row["status"] != OrderStatus.UNKNOWN_RECONCILING.value,
        "match_result": str(metadata.get("match_result") or "not_checked"),
        "candidate_count": _safe_nonnegative_int(
            metadata.get("candidate_count")
            if metadata.get("candidate_count") is not None
            else len(candidates)
        ),
        "candidates": [_broker_order_summary(item) for item in candidates],
    }


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
        if metadata.get("cancel_reconciled_full") and remaining_quantity == 0:
            return OrderStatus.CANCELLED
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
    # Never let an intermediate broker snapshot overwrite the pre-modification
    # baseline while amend/cancel acceptance is still uncertain. The verified
    # resolution path supplies the effective request explicitly.
    if metadata.get("uncertain_modification"):
        return metadata
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
        sell_position_scope=str(payload.get("sell_position_scope") or "STRATEGY"),
        buy_flow_id=payload.get("buy_flow_id"),
        sell_buy_flow_ids=tuple(payload.get("sell_buy_flow_ids") or ()),
        buy_rule_node_ids=tuple(payload.get("buy_rule_node_ids") or ()),
        sell_buy_node_refs=tuple(payload.get("sell_buy_node_refs") or ()),
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
            "sell_position_scope": order.request.sell_position_scope,
            "buy_flow_id": order.request.buy_flow_id,
            "sell_buy_flow_ids": list(order.request.sell_buy_flow_ids),
            "buy_rule_node_ids": list(order.request.buy_rule_node_ids),
            "sell_buy_node_refs": list(order.request.sell_buy_node_refs),
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
