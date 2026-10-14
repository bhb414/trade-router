from __future__ import annotations

import asyncio
from typing import Any

from ..errors import RouterError
from ..models import StrategyConfig
from .async_client import AsyncTradeRouter


class _SyncService:
    def __init__(self, owner: "TradeRouter", async_service: Any) -> None:
        self._owner = owner
        self._service = async_service

    def __getattr__(self, name: str):
        target = getattr(self._service, name)
        if not callable(target):
            return target

        def call(*args: Any, **kwargs: Any):
            result = target(*args, **kwargs)
            if hasattr(result, "__aiter__"):
                raise RouterError(
                    "SYNC_STREAM_UNSUPPORTED",
                    "Use AsyncTradeRouter for real-time streams.",
                )
            return self._owner._run(result)

        return call


class TradeRouter:
    """Synchronous facade backed by one persistent asyncio Runner."""

    def __init__(
        self,
        *,
        strategy: StrategyConfig,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 15,
    ) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RouterError(
                "SYNC_CLIENT_IN_ASYNC_LOOP",
                "Use AsyncTradeRouter inside an asyncio event loop.",
            )
        self._runner = asyncio.Runner()
        self._client = AsyncTradeRouter(
            strategy=strategy, base_url=base_url, token=token, timeout=timeout
        )
        self.strategy = strategy
        self.system = _SyncService(self, self._client.system)
        self.instruments = _SyncService(self, self._client.instruments)
        self.market = _SyncService(self, self._client.market)
        self.accounts = _SyncService(self, self._client.accounts)
        self.orders = _SyncService(self, self._client.orders)
        self.discovery = _SyncService(self, self._client.discovery)
        self._closed = False

    def _run(self, awaitable):
        if self._closed:
            raise RuntimeError("TradeRouter is closed")
        return self._runner.run(awaitable)

    def __enter__(self) -> "TradeRouter":
        self._run(self._client.__aenter__())
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._runner.run(self._client.close())
            self._runner.close()
            self._closed = True
