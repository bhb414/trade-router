from __future__ import annotations

import asyncio

from trade_router import RouterError

from .orders import OrderService
from .runtime_store import RuntimeStore


class OrderReconciler:
    """Poll broker order history only while router-owned orders are unresolved."""

    def __init__(
        self,
        store: RuntimeStore,
        service: OrderService,
        *,
        interval_seconds: float = 5.0,
    ) -> None:
        self._store = store
        self._service = service
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._last_errors: dict[str, str] = {}

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="trade-router-order-reconciler",
            )

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def refresh_once(self) -> None:
        accounts = sorted(
            {
                str(row["account_alias"])
                for row in self._store.unresolved_orders()
            }
        )
        for account_alias in accounts:
            try:
                await self._service.refresh_unresolved_orders(account_alias)
            except RouterError as exc:
                self._record_error_once(account_alias, exc.message)
            except Exception:
                self._record_error_once(
                    account_alias,
                    "주문 상태 자동 확인 중 예상하지 못한 오류가 발생했습니다.",
                )
            else:
                self._last_errors.pop(account_alias, None)

    async def _run(self) -> None:
        while True:
            await self.refresh_once()
            await asyncio.sleep(self._interval_seconds)

    def _record_error_once(self, account_alias: str, message: str) -> None:
        if self._last_errors.get(account_alias) == message:
            return
        self._last_errors[account_alias] = message
        self._store.record(
            source=account_alias,
            event_type="order.reconcile_error",
            message=message,
        )
