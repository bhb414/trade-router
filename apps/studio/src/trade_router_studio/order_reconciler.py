from __future__ import annotations

import asyncio

from trade_router import RouterError

from .orders import OrderService
from .process_diagnostics import EXCEPTION_TYPES
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
        self._last_errors: dict[
            str, tuple[str | None, str | None, int | None]
        ] = {}

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
        accounts = self._store.unresolved_order_account_aliases()
        for account_alias in accounts:
            try:
                await self._service.refresh_unresolved_orders(account_alias)
            except RouterError as exc:
                details = exc.details if isinstance(exc.details, dict) else {}
                wrapped_type = details.get("exception_type")
                self._record_error_once(
                    account_alias,
                    "주문 상태 자동 확인 중 증권사 조회 오류가 발생했습니다.",
                    error_code=exc.code.value,
                    exception_type=(
                        wrapped_type
                        if isinstance(wrapped_type, str)
                        and wrapped_type in EXCEPTION_TYPES
                        else "RouterError"
                    ),
                    winerror=_safe_winerror(details.get("winerror")),
                )
            except Exception as exc:
                exception_type = type(exc).__name__
                self._record_error_once(
                    account_alias,
                    "주문 상태 자동 확인 중 예상하지 못한 오류가 발생했습니다.",
                    exception_type=(
                        exception_type if exception_type in EXCEPTION_TYPES else None
                    ),
                    winerror=_safe_winerror(getattr(exc, "winerror", None)),
                )
            else:
                self._last_errors.pop(account_alias, None)

    async def _run(self) -> None:
        while True:
            await self.refresh_once()
            await asyncio.sleep(self._interval_seconds)

    def _record_error_once(
        self,
        account_alias: str,
        message: str,
        *,
        error_code: str | None = None,
        exception_type: str | None = None,
        winerror: int | None = None,
    ) -> None:
        signature = (error_code, exception_type, winerror)
        if self._last_errors.get(account_alias) == signature:
            return
        self._last_errors[account_alias] = signature
        details: dict[str, object] = {
            "source_kind": "account",
            "failure_phase": "reconcile",
        }
        if error_code is not None:
            details["error_code"] = error_code
        if exception_type is not None:
            details["exception_type"] = exception_type
        if winerror is not None:
            details["winerror"] = winerror
        self._store.record(
            source=account_alias,
            event_type="order.reconcile_error",
            message=message,
            details=details,
        )


def _safe_winerror(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 65535 else None
