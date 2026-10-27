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
    AccountOrder, AsyncTradeRouter, InstrumentId, Order, OrderRequest, OrderRoute, OrderSide,
    OrderStatus, OrderType, RouterError, RouterErrorCode, StrategyConfig,
)
from trade_router.client import SyncOrderClient
from trade_router_studio.app import create_app
from trade_router_studio.connections import (
    _normalize_kis_account_orders,
    _normalize_namuh_account_orders,
)
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
            pending = store.order(submitted.id)["provider_metadata"]["pending_modification"]
            assert pending["kind"] == operation
            assert pending["previous_status"] == "PARTIALLY_FILLED"
            assert pending["requested_at"]
            unknown_event = store.events(event_types=("order.unknown",), limit=1)[0]
            assert unknown_event["details"]["failure_phase"] == "modify"
            assert unknown_event["details"]["provider_id_present"] is True
            assert "ambiguous mocked response" not in str(unknown_event)
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
            await service.reconcile_unknown_order(submitted.id)
            assert store.order(submitted.id)["status"] == "FILLED"
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ("amend", "cancel"))
def test_unexpected_modification_exception_records_safe_unknown_event(
    tmp_path: Path, operation: str,
) -> None:
    class FaultGateway(SimulationGateway):
        async def amend_order(self, *args, **kwargs):
            raise RuntimeError("secret broker response")

        async def cancel_order(self, *args, **kwargs):
            raise RuntimeError("secret broker response")

    async def run() -> None:
        store = RuntimeStore(tmp_path / f"modify-secret-{operation}.sqlite3")
        gateway = FaultGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy(f"modify-secret-{operation}"), quantity=Decimal("10")),
            )
            kwargs = dict(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=None,
                route=None,
            )
            if operation == "amend":
                kwargs.update(order_type=OrderType.BEST_LIMIT, price=None)
            result = await getattr(service, operation)(**kwargs)
            assert result.status is OrderStatus.UNKNOWN_RECONCILING
            event = store.events(event_types=("order.unknown",), limit=1)[0]
            assert event["details"]["failure_phase"] == "modify"
            assert event["details"]["exception_type"] == "RuntimeError"
            assert event["details"]["provider_id_present"] is True
            assert "secret" not in str(event)
        finally:
            store.close()

    asyncio.run(run())


def test_uncertain_amendment_auto_links_unique_broker_child(tmp_path: Path) -> None:
    class UncertainAmendGateway(SimulationGateway):
        async def amend_order(self, *args, **kwargs):
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN, "ambiguous mocked response"
            )

    async def run() -> None:
        store = RuntimeStore(tmp_path / "uncertain-amend-child.sqlite3")
        gateway = UncertainAmendGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("uncertain-child"), quantity=Decimal("10")),
            )
            gateway.orders = (broker_order("broker-1"),)
            uncertain = await service.amend(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=None,
                order_type=OrderType.BEST_LIMIT,
                price=None,
                route=None,
            )
            assert uncertain.status is OrderStatus.UNKNOWN_RECONCILING
            requested_at = datetime.fromisoformat(
                store.order(submitted.id)["provider_metadata"]["pending_modification"]["requested_at"]
            )
            gateway.orders = (
                broker_order("broker-1"),
                replace(
                    broker_order("independent-manual"),
                    ordered_at=requested_at,
                ),
                replace(
                    broker_order(
                        "broker-child",
                        original_provider_id="broker-1",
                    ),
                    ordered_at=requested_at,
                ),
            )
            assert [
                item.provider_order_id
                for item in service._uncertain_modification_candidates(
                    store.order(submitted.id), gateway.orders
                )
            ] == ["broker-child"]

            await service.refresh_unresolved_orders("broker-account")

            stored = store.order(submitted.id)
            assert stored["status"] == "ACKNOWLEDGED"
            assert stored["provider_order_id"] == "broker-child"
            assert "uncertain_modification" not in stored["provider_metadata"]
            assert stored["provider_metadata"]["modification_resolution"] == "auto_amendment_linked"
        finally:
            store.close()

    asyncio.run(run())


def test_uncertain_amendment_without_parent_relation_requires_manual_id_link(
    tmp_path: Path,
) -> None:
    class UncertainAmendGateway(SimulationGateway):
        async def amend_order(self, *args, **kwargs):
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN, "ambiguous mocked response"
            )

    async def run() -> None:
        store = RuntimeStore(tmp_path / "uncertain-amend-manual-link.sqlite3")
        gateway = UncertainAmendGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("uncertain-manual-child"),
                    quantity=Decimal("10"),
                ),
            )
            gateway.orders = (broker_order("broker-1"),)
            await service.amend(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=None,
                order_type=OrderType.BEST_LIMIT,
                price=None,
                route=None,
            )
            requested_at = datetime.fromisoformat(
                store.order(submitted.id)["provider_metadata"]
                ["pending_modification"]["requested_at"]
            )
            relationless = replace(
                broker_order("manual-child"), ordered_at=requested_at
            )
            gateway.orders = (broker_order("broker-1"), relationless)

            result = await service.reconcile_unknown_order(submitted.id)
            assert result["resolved"] is False
            assert result["match_result"] == (
                "manual_required_missing_parent_relation"
            )
            assert store.order(submitted.id)["status"] == "UNKNOWN_RECONCILING"

            gateway.orders = (
                broker_order("broker-1"),
                replace(
                    broker_order(
                        "manual-child", original_provider_id="another-parent"
                    ),
                    ordered_at=requested_at,
                ),
            )
            with pytest.raises(RouterError) as wrong_parent:
                await service.resolve_unknown_order(
                    submitted.id,
                    action="LINK",
                    provider_order_id="manual-child",
                    confirmation="HTS_LINK_CONFIRMED",
                )
            assert wrong_parent.value.code is RouterErrorCode.INVALID_REQUEST

            gateway.orders = (broker_order("broker-1"), relationless)
            linked = await service.resolve_unknown_order(
                submitted.id,
                action="LINK",
                provider_order_id="manual-child",
                confirmation="HTS_LINK_CONFIRMED",
            )
            assert linked["resolved"] is True
            stored = store.order(submitted.id)
            assert stored["provider_order_id"] == "manual-child"
            assert stored["provider_metadata"]["modification_resolution"] == (
                "manual_amendment_linked"
            )
        finally:
            store.close()

    asyncio.run(run())


def test_uncertain_amendment_missing_broker_time_vetoes_not_accepted(
    tmp_path: Path,
) -> None:
    class UncertainAmendGateway(SimulationGateway):
        async def amend_order(self, *args, **kwargs):
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN, "ambiguous mocked response"
            )

    async def run() -> None:
        store = RuntimeStore(tmp_path / "uncertain-amend-no-time.sqlite3")
        gateway = UncertainAmendGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("uncertain-no-time-child"),
                    quantity=Decimal("10"),
                ),
            )
            gateway.orders = (broker_order("broker-1"),)
            await service.amend(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=None,
                order_type=OrderType.BEST_LIMIT,
                price=None,
                route=None,
            )
            requested_at = datetime.fromisoformat(
                store.order(submitted.id)["provider_metadata"]
                ["pending_modification"]["requested_at"]
            )
            missing_time_child = replace(
                broker_order(
                    "no-time-child", original_provider_id="broker-1"
                ),
                ordered_at=requested_at.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ),
            )
            gateway.orders = (broker_order("broker-1"), missing_time_child)

            result = await service.reconcile_unknown_order(submitted.id)
            assert result["resolved"] is False
            assert result["match_result"] == "manual_required_no_order_time"

            with pytest.raises(RouterError) as vetoed:
                await service.resolve_unknown_order(
                    submitted.id,
                    action="NOT_ACCEPTED",
                    confirmation="HTS_NOT_ACCEPTED",
                )
            assert vetoed.value.code is RouterErrorCode.ORDER_STATUS_UNKNOWN

            linked = await service.resolve_unknown_order(
                submitted.id,
                action="LINK",
                provider_order_id="no-time-child",
                confirmation="HTS_LINK_CONFIRMED",
            )
            assert linked["resolved"] is True
            assert store.order(submitted.id)["provider_order_id"] == (
                "no-time-child"
            )
        finally:
            store.close()

    asyncio.run(run())


def test_uncertain_amendment_rejected_child_restores_original_order(
    tmp_path: Path,
) -> None:
    class UncertainAmendGateway(SimulationGateway):
        async def amend_order(self, *args, **kwargs):
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN, "ambiguous mocked response"
            )

    async def run() -> None:
        store = RuntimeStore(tmp_path / "uncertain-amend-rejected.sqlite3")
        gateway = UncertainAmendGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("uncertain-rejected-child"),
                    quantity=Decimal("10"),
                ),
            )
            gateway.orders = (broker_order("broker-1"),)
            await service.amend(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=None,
                order_type=OrderType.BEST_LIMIT,
                price=None,
                route=None,
            )
            requested_at = datetime.fromisoformat(
                store.order(submitted.id)["provider_metadata"]
                ["pending_modification"]["requested_at"]
            )
            rejected_child = replace(
                broker_order(
                    "rejected-child", original_provider_id="broker-1"
                ),
                status=OrderStatus.REJECTED,
                remaining_quantity=Decimal("0"),
                ordered_at=requested_at,
            )
            gateway.orders = (broker_order("broker-1"), rejected_child)

            await service.refresh_unresolved_orders("broker-account")

            stored = store.order(submitted.id)
            assert stored["status"] == "ACKNOWLEDGED"
            assert stored["provider_order_id"] == "broker-1"
            assert "uncertain_modification" not in stored["provider_metadata"]
            assert stored["provider_metadata"]["modification_resolution"] == (
                "broker_amendment_rejected"
            )
        finally:
            store.close()

    asyncio.run(run())


def test_uncertain_amendment_rejected_child_without_parent_stays_unknown(
    tmp_path: Path,
) -> None:
    class UncertainAmendGateway(SimulationGateway):
        async def amend_order(self, *args, **kwargs):
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN, "ambiguous mocked response"
            )

    async def run() -> None:
        store = RuntimeStore(tmp_path / "rejected-child-parent-missing.sqlite3")
        gateway = UncertainAmendGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("rejected-child-parent-missing"),
                    quantity=Decimal("10"),
                ),
            )
            await service.amend(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=None,
                order_type=OrderType.BEST_LIMIT,
                price=None,
                route=None,
            )
            requested_at = datetime.fromisoformat(
                store.order(submitted.id)["provider_metadata"]
                ["pending_modification"]["requested_at"]
            )
            gateway.orders = (
                replace(
                    broker_order(
                        "rejected-child", original_provider_id="broker-1"
                    ),
                    status=OrderStatus.REJECTED,
                    remaining_quantity=Decimal("0"),
                    ordered_at=requested_at,
                ),
            )

            result = await service.reconcile_unknown_order(submitted.id)

            assert result["resolved"] is False
            assert result["match_result"] == "amendment_rejected_parent_missing"
            stored = store.order(submitted.id)
            assert stored["status"] == "UNKNOWN_RECONCILING"
            assert stored["provider_order_id"] == "broker-1"
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("manual_link", (False, True))
def test_response_lost_amendment_accepts_relation_verified_cumulative_quantity(
    tmp_path: Path, manual_link: bool,
) -> None:
    class UncertainAmendGateway(SimulationGateway):
        async def amend_order(self, *args, **kwargs):
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN, "ambiguous mocked response"
            )

    async def run() -> None:
        store = RuntimeStore(
            tmp_path / f"cumulative-amend-{int(manual_link)}.sqlite3"
        )
        gateway = UncertainAmendGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("cumulative-amend"),
                    quantity=Decimal("10"),
                    order_type=OrderType.BEST_LIMIT,
                ),
            )
            gateway.orders = (broker_order("broker-1", filled="3"),)
            await service.refresh_unresolved_orders("broker-account")
            await service.amend(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=None,
                order_type=OrderType.BEST_LIMIT,
                price=None,
                route=None,
            )
            pending = store.order(submitted.id)["provider_metadata"]
            pending = pending["pending_modification"]
            assert pending["quantity"] == "7"
            child = replace(
                broker_order(
                    "cumulative-child",
                    quantity="10",
                    filled="3",
                    remaining="7",
                    original_provider_id="broker-1",
                ),
                ordered_at=datetime.fromisoformat(pending["requested_at"]),
            )
            gateway.orders = (broker_order("broker-1", filled="3"), child)

            if manual_link:
                result = await service.resolve_unknown_order(
                    submitted.id,
                    action="LINK",
                    provider_order_id="cumulative-child",
                    confirmation="HTS_LINK_CONFIRMED",
                )
            else:
                result = await service.reconcile_unknown_order(submitted.id)

            assert result["resolved"] is True
            stored = store.order(submitted.id)
            assert stored["provider_order_id"] == "cumulative-child"
            assert stored["filled_quantity"] == "3"
            assert stored["provider_metadata"]["effective_request"]["quantity"] == (
                "10"
            )
            assert sum(
                Decimal(fill["quantity"])
                for fill in store.fills(order_id=submitted.id)
            ) == 3
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ("amend", "cancel"))
def test_legacy_uncertain_modification_can_restore_live_parent(
    tmp_path: Path, operation: str,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / f"legacy-{operation}-restore.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy(f"legacy-{operation}-restore"),
                    quantity=Decimal("10"),
                ),
            )
            # Exact persisted shape produced by v0.1.74: the operation marker
            # exists but pending_modification was not recorded.
            store.update_order(
                submitted.id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={"uncertain_modification": operation},
            )
            gateway.orders = (broker_order("broker-1"),)

            result = await service.resolve_unknown_order(
                submitted.id,
                action="NOT_ACCEPTED",
                confirmation="HTS_NOT_ACCEPTED",
            )

            assert result["resolved"] is True
            stored = store.order(submitted.id)
            assert stored["status"] == "ACKNOWLEDGED"
            assert "uncertain_modification" not in stored["provider_metadata"]
            assert stored["provider_metadata"]["modification_resolution"] == (
                "manual_modification_not_accepted"
            )
        finally:
            store.close()

    asyncio.run(run())


def test_legacy_uncertain_cancel_prefers_fresh_reduced_parent_total(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "legacy-cancel-reduced.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("legacy-cancel-reduced"),
                    quantity=Decimal("10"),
                    order_type=OrderType.BEST_LIMIT,
                ),
            )
            store.update_order(
                submitted.id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                filled_quantity=Decimal("3"),
                average_fill_price=Decimal("90000"),
                provider_metadata={"uncertain_modification": "cancel"},
            )
            gateway.orders = (
                broker_order("broker-1", filled="3", remaining="5"),
            )

            resolved = await service.resolve_unknown_order(
                submitted.id,
                action="NOT_ACCEPTED",
                confirmation="HTS_NOT_ACCEPTED",
            )

            assert resolved["resolved"] is True
            stored = store.order(submitted.id)
            assert stored["status"] == "PARTIALLY_FILLED"
            assert stored["provider_metadata"]["effective_request"]["quantity"] == (
                "8"
            )
            assert stored["provider_metadata"]["modification_resolution"] == (
                "auto_cancel_reconciled"
            )
        finally:
            store.close()

    asyncio.run(run())


def test_legacy_uncertain_amendment_terminal_parent_reconciles(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "legacy-terminal-parent.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("legacy-terminal-parent"),
                    quantity=Decimal("10"),
                ),
            )
            store.update_order(
                submitted.id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={"uncertain_modification": "amend"},
            )
            gateway.orders = (
                replace(
                    broker_order("broker-1", filled="10", remaining="0"),
                    status=OrderStatus.FILLED,
                ),
            )

            result = await service.reconcile_unknown_order(submitted.id)

            assert result["resolved"] is True
            stored = store.order(submitted.id)
            assert stored["status"] == "FILLED"
            assert stored["provider_metadata"]["modification_resolution"] == (
                "broker_terminal"
            )
        finally:
            store.close()

    asyncio.run(run())


def test_legacy_uncertain_amendment_requires_relation_verified_manual_link(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "legacy-amend-manual.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("legacy-amend-manual"), quantity=Decimal("10")
                ),
            )
            store.update_order(
                submitted.id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={"uncertain_modification": "amend"},
            )
            parent = broker_order("broker-1")
            child = broker_order(
                "legacy-child", quantity="6", original_provider_id="broker-1"
            )
            gateway.orders = (parent, child)

            pending = await service.reconcile_unknown_order(submitted.id)
            assert pending["resolved"] is False
            assert pending["match_result"] == (
                "manual_required_legacy_modification"
            )
            with pytest.raises(RouterError):
                await service.resolve_unknown_order(
                    submitted.id,
                    action="NOT_ACCEPTED",
                    confirmation="HTS_NOT_ACCEPTED",
                )

            for invalid_child in (
                replace(child, original_provider_order_id=None),
                replace(child, original_provider_order_id="other-parent"),
                replace(
                    child,
                    status=OrderStatus.REJECTED,
                    remaining_quantity=Decimal("0"),
                ),
            ):
                gateway.orders = (parent, invalid_child)
                with pytest.raises(RouterError) as invalid:
                    await service.resolve_unknown_order(
                        submitted.id,
                        action="LINK",
                        provider_order_id="legacy-child",
                        confirmation="HTS_LINK_CONFIRMED",
                    )
                assert invalid.value.code is RouterErrorCode.INVALID_REQUEST

            gateway.orders = (parent, child)
            linked = await service.resolve_unknown_order(
                submitted.id,
                action="LINK",
                provider_order_id="legacy-child",
                confirmation="HTS_LINK_CONFIRMED",
            )
            assert linked["resolved"] is True
            stored = store.order(submitted.id)
            assert stored["provider_order_id"] == "legacy-child"
            assert stored["provider_metadata"]["effective_request"]["quantity"] == (
                "6"
            )
            assert stored["provider_metadata"]["modification_resolution"] == (
                "manual_amendment_linked"
            )
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("child_quantity", "child_filled", "child_remaining"),
    (("10", "5", "5"), ("5", "0", "5")),
)
def test_legacy_cumulative_amendment_snapshot_is_not_double_counted(
    tmp_path: Path,
    child_quantity: str,
    child_filled: str,
    child_remaining: str,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "legacy-cumulative-amend.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("legacy-cumulative-amend"),
                    quantity=Decimal("10"),
                ),
            )
            store.update_order(
                submitted.id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                filled_quantity=Decimal("3"),
                average_fill_price=Decimal("90000"),
                provider_metadata={"uncertain_modification": "amend"},
            )
            parent = broker_order("broker-1", filled="3")
            # With no pending request, both a cumulative fill snapshot and a
            # zero-fill replacement can encode either cumulative or segment
            # quantities. Do not guess after the parent already filled.
            cumulative_or_segment = broker_order(
                "legacy-cumulative-child",
                quantity=child_quantity,
                filled=child_filled,
                remaining=child_remaining,
                original_provider_id="broker-1",
            )
            gateway.orders = (parent, cumulative_or_segment)

            with pytest.raises(RouterError) as ambiguous:
                await service.resolve_unknown_order(
                    submitted.id,
                    action="LINK",
                    provider_order_id="legacy-cumulative-child",
                    confirmation="HTS_LINK_CONFIRMED",
                )

            assert ambiguous.value.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
            stored = store.order(submitted.id)
            assert stored["status"] == "UNKNOWN_RECONCILING"
            assert stored["provider_order_id"] == "broker-1"
            assert stored["filled_quantity"] == "3"
            assert sum(
                Decimal(fill["quantity"])
                for fill in store.fills(order_id=submitted.id)
            ) == 0
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("child_quantity", "child_filled", "child_remaining"),
    (("10", "3", "7"), ("5", "0", "5")),
)
def test_legacy_amendment_link_checks_fresh_parent_fills(
    tmp_path: Path,
    child_quantity: str,
    child_filled: str,
    child_remaining: str,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / f"legacy-fresh-parent-{child_quantity}.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("legacy-fresh-parent"), quantity=Decimal("10")
                ),
            )
            # The legacy ledger has not received the parent's three fills yet.
            store.update_order(
                submitted.id,
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={"uncertain_modification": "amend"},
            )
            parent = broker_order("broker-1", filled="3")
            child = broker_order(
                "legacy-fresh-child",
                quantity=child_quantity,
                filled=child_filled,
                remaining=child_remaining,
                original_provider_id="broker-1",
            )
            gateway.orders = (parent, child)

            with pytest.raises(RouterError) as ambiguous:
                await service.resolve_unknown_order(
                    submitted.id,
                    action="LINK",
                    provider_order_id="legacy-fresh-child",
                    confirmation="HTS_LINK_CONFIRMED",
                )

            assert ambiguous.value.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
            stored = store.order(submitted.id)
            assert stored["status"] == "UNKNOWN_RECONCILING"
            assert stored["provider_order_id"] == "broker-1"
            assert stored["filled_quantity"] == "0"
        finally:
            store.close()

    asyncio.run(run())


def test_uncertain_amendment_does_not_close_on_cancelled_parent_without_successor(
    tmp_path: Path,
) -> None:
    class UncertainAmendGateway(SimulationGateway):
        async def amend_order(self, *args, **kwargs):
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN, "ambiguous mocked response"
            )

    async def run() -> None:
        store = RuntimeStore(tmp_path / "orphan-amend-child.sqlite3")
        gateway = UncertainAmendGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("orphan-child"), quantity=Decimal("10")),
            )
            await service.amend(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=None,
                order_type=OrderType.BEST_LIMIT,
                price=None,
                route=None,
            )
            pending = store.order(submitted.id)["provider_metadata"]["pending_modification"]
            requested_at = datetime.fromisoformat(pending["requested_at"])
            gateway.orders = (
                replace(
                    broker_order("broker-1"),
                    status=OrderStatus.CANCELLED,
                    remaining_quantity=Decimal("0"),
                ),
                # It looks like the desired child but has no credible temporal
                # relation to the request and therefore cannot be auto-linked.
                replace(
                    broker_order("late-child"),
                    ordered_at=requested_at.replace(hour=0, minute=0, second=0),
                ),
            )

            await service.refresh_unresolved_orders("broker-account")

            stored = store.order(submitted.id)
            assert stored["status"] == "UNKNOWN_RECONCILING"
            assert stored["provider_order_id"] == "broker-1"
            assert stored["provider_metadata"]["match_result"] == "amendment_successor_missing"
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ("amend", "cancel"))
def test_uncertain_modification_can_be_manually_restored_after_hts_check(
    tmp_path: Path, operation: str,
) -> None:
    class UncertainGateway(SimulationGateway):
        async def amend_order(self, *args, **kwargs):
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN, "ambiguous mocked response"
            )

        async def cancel_order(self, *args, **kwargs):
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN, "ambiguous mocked response"
            )

    async def run() -> None:
        store = RuntimeStore(tmp_path / f"uncertain-manual-{operation}.sqlite3")
        gateway = UncertainGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy(f"manual-{operation}"), quantity=Decimal("10")),
            )
            gateway.orders = (broker_order("broker-1"),)
            kwargs = dict(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=None,
                route=None,
            )
            if operation == "amend":
                kwargs.update(order_type=OrderType.BEST_LIMIT, price=None)
            await getattr(service, operation)(**kwargs)
            with pytest.raises(RouterError) as missing_confirmation:
                await service.resolve_unknown_order(
                    submitted.id, action="NOT_ACCEPTED"
                )
            assert missing_confirmation.value.code is RouterErrorCode.INVALID_REQUEST

            result = await service.resolve_unknown_order(
                submitted.id,
                action="NOT_ACCEPTED",
                confirmation="HTS_NOT_ACCEPTED",
            )

            assert result["resolved"] is True
            stored = store.order(submitted.id)
            assert stored["status"] == "ACKNOWLEDGED"
            assert "uncertain_modification" not in stored["provider_metadata"]
            assert stored["provider_metadata"]["modification_resolution"] == "manual_modification_not_accepted"
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ("amend", "cancel"))
def test_manual_modification_not_accepted_rejects_terminal_parent_snapshot(
    tmp_path: Path, operation: str,
) -> None:
    class UncertainGateway(SimulationGateway):
        async def amend_order(self, *args, **kwargs):
            raise RouterError(RouterErrorCode.ORDER_STATUS_UNKNOWN, "uncertain")

        async def cancel_order(self, *args, **kwargs):
            raise RouterError(RouterErrorCode.ORDER_STATUS_UNKNOWN, "uncertain")

    async def run() -> None:
        store = RuntimeStore(tmp_path / f"terminal-manual-{operation}.sqlite3")
        gateway = UncertainGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy(f"terminal-{operation}"), quantity=Decimal("10")),
            )
            kwargs = dict(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=None,
                route=None,
            )
            if operation == "amend":
                kwargs.update(order_type=OrderType.BEST_LIMIT, price=None)
            await getattr(service, operation)(**kwargs)
            gateway.orders = (
                replace(
                    broker_order("broker-1"),
                    status=OrderStatus.CANCELLED,
                    remaining_quantity=Decimal("0"),
                ),
            )

            if operation == "cancel":
                resolved = await service.resolve_unknown_order(
                    submitted.id,
                    action="NOT_ACCEPTED",
                    confirmation="HTS_NOT_ACCEPTED",
                )
                assert resolved["resolved"] is True
                stored = store.order(submitted.id)
                assert stored["status"] == "CANCELLED"
                assert stored["provider_metadata"]["modification_resolution"] == (
                    "broker_terminal"
                )
            else:
                with pytest.raises(RouterError) as contradiction:
                    await service.resolve_unknown_order(
                        submitted.id,
                        action="NOT_ACCEPTED",
                        confirmation="HTS_NOT_ACCEPTED",
                    )
                assert contradiction.value.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
                assert store.order(submitted.id)["status"] == "UNKNOWN_RECONCILING"
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("cancel_quantity", "remaining_after", "expected_status", "expected_total"),
    (
        (None, "0", "CANCELLED", "3"),
        (Decimal("2"), "5", "PARTIALLY_FILLED", "8"),
    ),
)
def test_response_lost_cancel_reconciles_reduced_remaining_quantity(
    tmp_path: Path,
    cancel_quantity: Decimal | None,
    remaining_after: str,
    expected_status: str,
    expected_total: str,
) -> None:
    class LostCancelGateway(SimulationGateway):
        async def cancel_order(self, *args, **kwargs):
            raise RouterError(RouterErrorCode.ORDER_STATUS_UNKNOWN, "response lost")

    async def run() -> None:
        store = RuntimeStore(tmp_path / f"lost-cancel-{expected_total}.sqlite3")
        gateway = LostCancelGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy(f"lost-cancel-{expected_total}"), quantity=Decimal("10")),
            )
            gateway.orders = (broker_order("broker-1", filled="3"),)
            await service.refresh_unresolved_orders("broker-account")
            result = await service.cancel(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=cancel_quantity,
                route=None,
            )
            assert result.status is OrderStatus.UNKNOWN_RECONCILING
            pending = store.order(submitted.id)["provider_metadata"]["pending_modification"]
            assert pending["previous_effective_quantity"] == "10"
            assert pending["expected_total_after_cancel"] == expected_total
            gateway.orders = (
                broker_order(
                    "broker-1", filled="3", remaining=remaining_after
                ),
            )

            await service.refresh_unresolved_orders("broker-account")

            stored = store.order(submitted.id)
            assert stored["status"] == expected_status
            assert stored["provider_metadata"]["effective_request"]["quantity"] == expected_total
            assert "uncertain_modification" not in stored["provider_metadata"]
            assert stored["provider_metadata"]["modification_resolution"] == "auto_cancel_reconciled"
        finally:
            store.close()

    asyncio.run(run())


def test_manual_not_accepted_cannot_restore_an_already_applied_partial_cancel(
    tmp_path: Path,
) -> None:
    class LostCancelGateway(SimulationGateway):
        async def cancel_order(self, *args, **kwargs):
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN, "response lost"
            )

    async def run() -> None:
        store = RuntimeStore(tmp_path / "partial-cancel-not-accepted.sqlite3")
        gateway = LostCancelGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("partial-cancel-not-accepted"),
                    quantity=Decimal("10"),
                    order_type=OrderType.BEST_LIMIT,
                ),
            )
            gateway.orders = (broker_order("broker-1", filled="3"),)
            await service.refresh_unresolved_orders("broker-account")
            await service.cancel(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=Decimal("2"),
                route=None,
            )
            gateway.orders = (
                broker_order("broker-1", filled="3", remaining="5"),
            )

            resolved = await service.resolve_unknown_order(
                submitted.id,
                action="NOT_ACCEPTED",
                confirmation="HTS_NOT_ACCEPTED",
            )

            assert resolved["resolved"] is True
            stored = store.order(submitted.id)
            assert stored["status"] == "PARTIALLY_FILLED"
            assert stored["provider_metadata"]["effective_request"]["quantity"] == (
                "8"
            )
            assert stored["provider_metadata"]["modification_resolution"] == (
                "auto_cancel_reconciled"
            )
        finally:
            store.close()

    asyncio.run(run())


def test_uncertain_cancel_poll_preserves_baseline_for_manual_not_accepted(
    tmp_path: Path,
) -> None:
    class LostCancelGateway(SimulationGateway):
        async def cancel_order(self, *args, **kwargs):
            raise RouterError(RouterErrorCode.ORDER_STATUS_UNKNOWN, "response lost")

    async def run() -> None:
        store = RuntimeStore(tmp_path / "lost-cancel-baseline.sqlite3")
        gateway = LostCancelGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(market_buy("lost-cancel-baseline"), quantity=Decimal("10")),
            )
            gateway.orders = (broker_order("broker-1", filled="3"),)
            await service.refresh_unresolved_orders("broker-account")
            await service.cancel(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=Decimal("2"),
                route=None,
            )
            # An intermediate total of 9 is neither the previous 10 nor the
            # expected post-cancel 8. It must not overwrite the baseline.
            gateway.orders = (broker_order("broker-1", filled="3", remaining="6"),)
            await service.refresh_unresolved_orders("broker-account")
            pending = store.order(submitted.id)
            assert pending["status"] == "UNKNOWN_RECONCILING"
            assert pending["request"]["quantity"] == "10"
            assert "effective_request" not in pending["provider_metadata"]

            gateway.orders = (broker_order("broker-1", filled="3", remaining="7"),)
            resolved = await service.resolve_unknown_order(
                submitted.id,
                action="NOT_ACCEPTED",
                confirmation="HTS_NOT_ACCEPTED",
            )
            assert resolved["resolved"] is True
            stored = store.order(submitted.id)
            assert stored["status"] == "PARTIALLY_FILLED"
            assert stored["provider_metadata"]["effective_request"]["quantity"] == "10"
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("cancel_quantity", "child_remaining", "expected_status", "expected_total"),
    (
        (None, "0", "CANCELLED", "3"),
        (Decimal("2"), "5", "PARTIALLY_FILLED", "8"),
    ),
)
def test_response_lost_cancel_after_amendment_uses_cumulative_fill_offsets(
    tmp_path: Path,
    cancel_quantity: Decimal | None,
    child_remaining: str,
    expected_status: str,
    expected_total: str,
) -> None:
    class LostCancelGateway(SimulationGateway):
        async def cancel_order(self, *args, **kwargs):
            raise RouterError(RouterErrorCode.ORDER_STATUS_UNKNOWN, "response lost")

    async def run() -> None:
        store = RuntimeStore(tmp_path / f"amended-lost-cancel-{expected_total}.sqlite3")
        gateway = LostCancelGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy(f"amended-lost-cancel-{expected_total}"),
                    quantity=Decimal("10"),
                    order_type=OrderType.BEST_LIMIT,
                ),
            )
            original = broker_order("broker-1", filled="2")
            gateway.orders = (original,)
            await service.refresh_unresolved_orders("broker-account")
            amended = await service.amend(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                order_id=submitted.id,
                quantity=Decimal("8"),
                order_type=OrderType.BEST_LIMIT,
                price=None,
                route=None,
            )
            assert amended.provider_order_id == "broker-1-amended"
            child = broker_order(
                "broker-1-amended", quantity="8", filled="1", remaining="7"
            )
            gateway.orders = (original, child)
            await service.refresh_unresolved_orders("broker-account")
            assert store.order(submitted.id)["filled_quantity"] == "3"

            unknown = await service.cancel(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=cancel_quantity,
                route=None,
            )
            assert unknown.status is OrderStatus.UNKNOWN_RECONCILING
            gateway.orders = (
                original,
                broker_order(
                    "broker-1-amended",
                    quantity="8",
                    filled="1",
                    remaining=child_remaining,
                ),
            )

            await service.refresh_unresolved_orders("broker-account")

            stored = store.order(submitted.id)
            assert stored["status"] == expected_status
            assert stored["filled_quantity"] == "3"
            assert stored["provider_metadata"]["effective_request"]["quantity"] == (
                expected_total
            )
            assert stored["provider_metadata"]["modification_resolution"] == (
                "auto_cancel_reconciled"
            )
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("response_lost", (False, True))
@pytest.mark.parametrize(
    ("cancel_quantity", "expected_status", "expected_total"),
    (
        (None, "CANCELLED", "3"),
        (Decimal("2"), "PARTIALLY_FILLED", "8"),
    ),
)
def test_namuh_cancel_child_snapshot_reconciles_service_order(
    tmp_path: Path,
    response_lost: bool,
    cancel_quantity: Decimal | None,
    expected_status: str,
    expected_total: str,
) -> None:
    class NamuhCancelGateway(SimulationGateway):
        async def submit_order(self, *args, **kwargs):
            return replace(
                await super().submit_order(*args, **kwargs),
                provider_order_id="30",
            )

        async def cancel_order(self, *args, **kwargs):
            acknowledgement = replace(
                await super().cancel_order(*args, **kwargs),
                provider_order_id="31",
            )
            if response_lost:
                raise RouterError(
                    RouterErrorCode.ORDER_STATUS_UNKNOWN, "response lost"
                )
            return acknowledgement

    def snapshots(cancelled: Decimal = Decimal("0")) -> tuple[AccountOrder, ...]:
        day = datetime.now(SEOUL).date()
        rows = [
            {
                "itg_orr_no": "30",
                "orr_dt": day.strftime("%Y%m%d"),
                "orr_tm": "101500",
                "iem_cd": "005930",
                "sby_dit_cd_nm": "매수",
                "nmn_pr_tp_cd_nm": "보통가",
                "orr_qty": "10",
                "orr_pr": "73000",
                "tot_cns_qty": "3",
                "cns_avg_uit_pr": "72900",
                "ny_cns_qty": "7",
                "can_qty": "0",
                "orr_rjt_rsn_cd_nm": "",
                "rmt_mkt_cd": "KRX",
            }
        ]
        if cancelled > 0:
            rows.append(
                {
                    "itg_orr_no": "31",
                    "org_mkt_orr_no": "30",
                    "orr_dt": day.strftime("%Y%m%d"),
                    "orr_tm": "101600",
                    "iem_cd": "005930",
                    "sby_dit_cd_nm": "매수",
                    "nmn_pr_tp_cd_nm": "취소",
                    "orr_qty": str(cancelled),
                    "orr_pr": "0",
                    "tot_cns_qty": "0",
                    "ny_cns_qty": "0",
                    "can_qty": str(cancelled),
                    "orr_rjt_rsn_cd_nm": "",
                    "rmt_mkt_cd": "KRX",
                }
            )
        return _normalize_namuh_account_orders(rows, "broker-account", day)

    async def run() -> None:
        store = RuntimeStore(
            tmp_path
            / f"namuh-cancel-{int(response_lost)}-{expected_total}.sqlite3"
        )
        gateway = NamuhCancelGateway(provider="namuh")
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="1000000"),
                request=replace(
                    market_buy("namuh-cancel-service"),
                    quantity=Decimal("10"),
                    order_type=OrderType.BEST_LIMIT,
                ),
            )
            gateway.orders = snapshots()
            await service.refresh_unresolved_orders("broker-account")
            result = await service.cancel(
                strategy_name="strategy.py",
                manifest=manifest(),
                order_id=submitted.id,
                quantity=cancel_quantity,
                route=None,
            )
            assert result.status is (
                OrderStatus.UNKNOWN_RECONCILING
                if response_lost
                else OrderStatus.CANCEL_PENDING
            )
            confirmed_cancel = Decimal("7") if cancel_quantity is None else cancel_quantity
            gateway.orders = snapshots(confirmed_cancel)

            await service.refresh_unresolved_orders("broker-account")

            stored = store.order(submitted.id)
            assert stored["status"] == expected_status
            assert stored["filled_quantity"] == "3"
            assert stored["provider_metadata"]["effective_request"]["quantity"] == (
                expected_total
            )
        finally:
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("provider", ("kis", "namuh"))
def test_cancel_child_fold_preserves_submit_time_for_unknown_auto_link(
    tmp_path: Path, provider: str,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / f"{provider}-cancel-fold-window.sqlite3")
        gateway = SimulationGateway(provider=provider)
        service = OrderService(store, gateway)
        try:
            day = datetime.now(SEOUL).date()
            submitted_at = datetime.combine(
                day, datetime.min.time(), tzinfo=SEOUL
            ).replace(hour=9)
            row, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id=f"{provider}-cancel-fold-window",
                request={
                    "code": "005930",
                    "side": "BUY",
                    "order_type": "LIMIT",
                    "quantity": "10",
                    "price": "73000",
                    "route": "KRX",
                    "reserved_amount": "730000",
                },
            )
            with store._connection:
                store._connection.execute(
                    "UPDATE order_ledger SET created_at = ?, updated_at = ? WHERE id = ?",
                    (submitted_at.isoformat(), submitted_at.isoformat(), row["id"]),
                )
            store.update_order(
                str(row["id"]),
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={"first_unknown_at": submitted_at.isoformat()},
            )
            if provider == "kis":
                raw_rows = [
                    {
                        "odno": "original",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd": "02",
                        "ord_dvsn_name": "지정가",
                        "ord_qty": "10",
                        "tot_ccld_qty": "1",
                        "rmn_qty": "9",
                        "ord_unpr": "73000",
                        "avg_prvs": "72900",
                        "ord_dt": day.strftime("%Y%m%d"),
                        "ord_tmd": "090000",
                        "excg_id_dvsn_cd": "KRX",
                    },
                    {
                        "odno": "cancel-child",
                        "orgn_odno": "original",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd": "02",
                        "rvse_cncl_dvsn_cd": "02",
                        "cncl_yn": "Y",
                        "ord_dvsn_name": "취소",
                        "ord_qty": "2",
                        "cncl_qty": "2",
                        "tot_ccld_qty": "0",
                        "rmn_qty": "0",
                        "ord_dt": day.strftime("%Y%m%d"),
                        "ord_tmd": "090500",
                        "excg_id_dvsn_cd": "KRX",
                    },
                ]
                gateway.orders = _normalize_kis_account_orders(
                    raw_rows, "broker-account", day
                )
            else:
                raw_rows = [
                    {
                        "itg_orr_no": "30",
                        "orr_dt": day.strftime("%Y%m%d"),
                        "orr_tm": "090000",
                        "iem_cd": "005930",
                        "sby_dit_cd_nm": "매수",
                        "nmn_pr_tp_cd_nm": "보통가",
                        "orr_qty": "10",
                        "orr_pr": "73000",
                        "tot_cns_qty": "1",
                        "cns_avg_uit_pr": "72900",
                        "ny_cns_qty": "9",
                        "can_qty": "0",
                        "orr_rjt_rsn_cd_nm": "",
                        "rmt_mkt_cd": "KRX",
                    },
                    {
                        "itg_orr_no": "31",
                        "org_mkt_orr_no": "30",
                        "orr_dt": day.strftime("%Y%m%d"),
                        "orr_tm": "090500",
                        "iem_cd": "005930",
                        "sby_dit_cd_nm": "매수",
                        "nmn_pr_tp_cd_nm": "취소",
                        "orr_qty": "2",
                        "orr_pr": "0",
                        "tot_cns_qty": "0",
                        "ny_cns_qty": "0",
                        "can_qty": "2",
                        "orr_rjt_rsn_cd_nm": "",
                        "rmt_mkt_cd": "KRX",
                    },
                ]
                gateway.orders = _normalize_namuh_account_orders(
                    raw_rows, "broker-account", day
                )

            result = await service.reconcile_unknown_order(str(row["id"]))

            assert result["resolved"] is True
            stored = store.order(str(row["id"]))
            assert stored["provider_order_id"] == (
                "original" if provider == "kis" else "30"
            )
            assert stored["status"] == "PARTIALLY_FILLED"
            assert stored["filled_quantity"] == "1"
            assert stored["provider_metadata"]["effective_request"]["quantity"] == (
                "8"
            )
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
                 remaining: str | None = None, average: str = "90000",
                 original_provider_id: str | None = None) -> AccountOrder:
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
        original_provider_order_id=original_provider_id,
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
            # An unrelated external order must not look identical to the
            # provider-id-less router order now that unique matching is active.
            replace(
                broker_order("manual-broker"),
                instrument=InstrumentId("000660"),
            ),
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
def test_rejected_modification_notice_restores_uncertain_original(
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
            restored = store.order(submitted.id)
            assert restored["status"] == "ACKNOWLEDGED"
            assert "uncertain_modification" not in restored["provider_metadata"]
            assert restored["provider_metadata"]["modification_resolution"] == "realtime_rejected"
            gateway.orders = (broker_order("broker-1"),)
            assert [order.id for order in await service.open_orders(manifest=manifest())] == [submitted.id]
            assert store.order(submitted.id)["status"] == "ACKNOWLEDGED"
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

