from __future__ import annotations

import asyncio
from pathlib import Path

from trade_router import RouterError, RouterErrorCode
from trade_router_studio.order_reconciler import OrderReconciler
from trade_router_studio.runtime_store import RuntimeStore


class FakeStore:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def unresolved_orders(self):
        return [{"account_alias": "private-account"}]

    def unresolved_order_account_aliases(self):
        return ("private-account",)

    def record(self, **item):
        self.records.append(item)


class ChangingRouterErrorService:
    def __init__(self) -> None:
        self.calls = 0

    async def refresh_unresolved_orders(self, account_alias):
        self.calls += 1
        raise RouterError(
            RouterErrorCode.ORDER_STATUS_UNKNOWN,
            f"dynamic private broker message {self.calls}",
        )


class SecretTimeoutService:
    async def refresh_unresolved_orders(self, account_alias):
        raise TimeoutError("PRIVATE_TOKEN_SENTINEL C:\\Users\\PRIVATE_USER")


class WindowsTimeoutService:
    async def refresh_unresolved_orders(self, account_alias):
        error = TimeoutError("PRIVATE_NETWORK_PATH")
        error.winerror = 10060
        raise error


class WrappedTransportService:
    async def refresh_unresolved_orders(self, account_alias):
        raise RouterError(
            RouterErrorCode.ORDER_STATUS_UNKNOWN,
            "PRIVATE_BROKER_MESSAGE",
            details={
                "unknown_origin": "transport_error",
                "exception_type": "ReadTimeout",
                "winerror": 10060,
                "raw_error": "PRIVATE_TOKEN_SENTINEL",
            },
        )


class RecordingService:
    def __init__(self) -> None:
        self.accounts: list[str] = []

    async def refresh_unresolved_orders(self, account_alias):
        self.accounts.append(account_alias)
        return []


def test_refresh_once_discovers_every_account_beyond_global_row_limit(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "many-unresolved-accounts.sqlite3")
        service = RecordingService()
        try:
            for index in range(1_001):
                store.reserve_order(
                    strategy_id=f"strategy-{index}",
                    account_alias=f"account-{index:04d}",
                    client_order_id=f"order-{index}",
                    request={
                        "code": "005930",
                        "side": "BUY",
                        "quantity": "1",
                    },
                )

            reconciler = OrderReconciler(store, service)
            await reconciler.refresh_once()

            assert len(service.accounts) == 1_001
            assert service.accounts[0] == "account-0000"
            assert service.accounts[-1] == "account-1000"
        finally:
            store.close()

    asyncio.run(run())


def test_reconcile_error_deduplicates_by_safe_signature_not_dynamic_message():
    async def run() -> None:
        store = FakeStore()
        reconciler = OrderReconciler(store, ChangingRouterErrorService())
        await reconciler.refresh_once()
        await reconciler.refresh_once()
        assert len(store.records) == 1
        record = store.records[0]
        assert "dynamic private broker message" not in record["message"]
        assert record["source"] == "private-account"
        assert record["event_type"] == "order.reconcile_error"
        assert record["details"] == {
            "source_kind": "account",
            "failure_phase": "reconcile",
            "error_code": "ORDER_STATUS_UNKNOWN",
            "exception_type": "RouterError",
        }

    asyncio.run(run())


def test_reconcile_error_retains_only_bounded_windows_error_number():
    async def run() -> None:
        store = FakeStore()
        reconciler = OrderReconciler(store, WindowsTimeoutService())
        await reconciler.refresh_once()
        record = store.records[0]
        assert "PRIVATE_NETWORK_PATH" not in str(record)
        assert record["details"] == {
            "source_kind": "account",
            "failure_phase": "reconcile",
            "exception_type": "TimeoutError",
            "winerror": 10060,
        }

    asyncio.run(run())


def test_unexpected_reconcile_error_retains_type_without_secret_message():
    async def run() -> None:
        store = FakeStore()
        reconciler = OrderReconciler(store, SecretTimeoutService())
        await reconciler.refresh_once()
        assert len(store.records) == 1
        record = store.records[0]
        assert record["message"] == "주문 상태 자동 확인 중 예상하지 못한 오류가 발생했습니다."
        assert "PRIVATE_TOKEN_SENTINEL" not in str(record)
        assert "PRIVATE_USER" not in str(record)
        assert record["details"] == {
            "source_kind": "account",
            "failure_phase": "reconcile",
            "exception_type": "TimeoutError",
        }

    asyncio.run(run())


def test_wrapped_transport_error_retains_safe_root_cause_without_secret():
    async def run() -> None:
        store = FakeStore()
        reconciler = OrderReconciler(store, WrappedTransportService())
        await reconciler.refresh_once()
        record = store.records[0]
        assert "PRIVATE_BROKER_MESSAGE" not in str(record)
        assert "PRIVATE_TOKEN_SENTINEL" not in str(record)
        assert record["details"] == {
            "source_kind": "account",
            "failure_phase": "reconcile",
            "error_code": "ORDER_STATUS_UNKNOWN",
            "exception_type": "ReadTimeout",
            "winerror": 10060,
        }

    asyncio.run(run())
