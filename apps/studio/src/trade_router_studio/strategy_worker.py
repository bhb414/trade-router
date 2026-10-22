from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from trade_router import (
    AsyncTradeRouter,
    RouterError,
    ScheduleKind,
    Strategy,
    StrategyContext,
)
from trade_router.client import fill_from_event, orderbook_from_event, quote_from_event, _order_from_payload

from trade_router_studio.event_bus import RuntimeEventBuffer


class StrategyLogger:
    def info(self, message: object) -> None:
        print(f"INFO {message}", flush=True)

    def warning(self, message: object) -> None:
        print(f"WARNING {message}", flush=True)

    def error(self, message: object) -> None:
        print(f"ERROR {message}", flush=True)


class StrategyState:
    def __init__(self, strategy_name: str) -> None:
        root = Path(os.environ["TRADE_ROUTER_HOME"])
        safe_name = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in strategy_name
        )
        self._path = root / "data" / "strategy-state" / f"{safe_name}.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._values = self._load()

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._values[key] = value
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._values, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._path)

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}


async def _dispatch_event(strategy: Strategy, context: StrategyContext, event: dict[str, Any]) -> None:
    event_type = str(event.get("event_type") or "schedule")
    if event_type == "quote":
        payload = dict(event["quote"])
        payload["source_alias"] = event.get("source_alias")
        await strategy.on_quote(context, quote_from_event(payload))
    elif event_type == "orderbook":
        payload = dict(event["orderbook"])
        payload["source_alias"] = event.get("source_alias")
        await strategy.on_orderbook(context, orderbook_from_event(payload))
    elif event_type == "order":
        await strategy.on_order(context, _order_from_payload(event["order"]))
    elif event_type == "fill":
        await strategy.on_fill(context, fill_from_event(event["fill"]))
    else:
        value = SimpleNamespace(
            schedule_id=event["schedule_id"],
            kind=ScheduleKind(event["kind"]),
            scheduled_at=datetime.fromisoformat(event["scheduled_at"]),
            bar_interval=event.get("bar_interval"),
        )
        if value.kind is ScheduleKind.BAR_CLOSE:
            await strategy.on_bar(context, value)
        else:
            await strategy.on_timer(context, value)


class StrategyEventDispatcher:
    """Five bounded, serial lanes for explicitly concurrency-safe strategies.

    Hand-written Strategy subclasses remain serial unless they opt in with
    runtime_lane(event). Generated managed strategies provide that contract and
    protect shared portfolio/order mutations separately from market I/O.
    """

    LANES = ("buy", "sell", "live", "execution", "control")

    def __init__(self, strategy: Strategy, context: StrategyContext, *, capacity: int = 256) -> None:
        self.strategy = strategy
        self.context = context
        self.classify = getattr(strategy, "runtime_lane", None)
        self.failure: asyncio.Future[BaseException] = asyncio.get_running_loop().create_future()
        self.closed = False
        self.queues: dict[str, RuntimeEventBuffer] = {}
        self.tasks: list[asyncio.Task[None]] = []
        self._reported_coalescing: dict[str, int] = {}
        if callable(self.classify):
            self.queues = {name: RuntimeEventBuffer(capacity, coalesce_schedules=True) for name in self.LANES}
            self.tasks = [asyncio.create_task(self._consume(name), name=f"strategy-{name}") for name in self.LANES]

    async def submit(self, event: dict[str, Any]) -> None:
        if self.closed:
            raise RuntimeError("Strategy dispatcher is closed.")
        if not self.queues:
            await _dispatch_event(self.strategy, self.context, event)
            return
        name = self.classify(event)
        if name not in self.queues:
            raise ValueError(f"Unknown strategy runtime lane: {name!r}")
        queue = self.queues[name]
        await queue.put(event)
        previous = self._reported_coalescing.get(name, 0)
        if queue.coalesced and (previous == 0 or queue.coalesced - previous >= 100):
            self._reported_coalescing[name] = queue.coalesced
            self.context.log.warning(
                f"실행 이벤트 대기 병합: {name}, {queue.coalesced}건. "
                "일정은 최신 요청, 시세는 최저·최고·최신 가격을 보존합니다."
            )

    async def _consume(self, name: str) -> None:
        try:
            while True:
                event = await self.queues[name].get()
                await _dispatch_event(self.strategy, self.context, event)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if not self.failure.done():
                self.failure.set_result(exc)

    async def close(self) -> None:
        self.closed = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for queue in self.queues.values():
            await queue.close()
        if not self.failure.done():
            self.failure.cancel()


async def _run_events(strategy: Strategy, context: StrategyContext, runtime: Any,
                      stop_signal: asyncio.Future[None]) -> None:
    dispatcher = StrategyEventDispatcher(strategy, context)

    async def receive() -> None:
        while True:
            try:
                event = await runtime.next_event()
            except RouterError as exc:
                context.log.warning(f"중앙 실행 이벤트 연결이 끊겨 다시 시도합니다: {exc}")
                await asyncio.sleep(1)
                continue
            if event is not None:
                await dispatcher.submit(event)

    receiver = asyncio.create_task(receive(), name="strategy-events")
    try:
        done, _ = await asyncio.wait(
            {stop_signal, receiver, dispatcher.failure}, return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_signal not in done:
            if dispatcher.failure in done:
                raise dispatcher.failure.result()
            await receiver
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        # No callbacks remain alive when the caller invokes strategy.on_stop.
        await dispatcher.close()


async def run(path: Path) -> None:
    strategy = _load_strategy(path)
    config = getattr(strategy, "config", None)
    if config is None:
        raise RuntimeError("Strategy class must define a StrategyConfig named 'config'.")

    logger = StrategyLogger()
    state = StrategyState(path.name)
    async with AsyncTradeRouter(strategy=config) as router:
        context = StrategyContext(
            config=config,
            market=router.market,
            account=router.account,
            orders=router.orders,
            discovery=router.discovery,
            state=state,
            logger=logger,
        )
        await router.runtime.register_manifest()
        await strategy.on_start(context)
        await router.runtime.register_schedules(context.scheduler.items)
        print(
            f"--- runtime strategy ready ({len(context.scheduler.items)} schedules) ---",
            flush=True,
        )
        stop_signal = _stdin_stop_signal()
        try:
            await _run_events(strategy, context, router.runtime, stop_signal)
        finally:
            await strategy.on_stop(context)
            if not stop_signal.done():
                stop_signal.cancel()


def _stdin_stop_signal() -> asyncio.Future[None]:
    """Wait for the parent's stop command without blocking asyncio shutdown.

    ``asyncio.to_thread(sys.stdin.readline)`` uses the loop's shared executor.
    Cancelling its task cannot cancel the blocking Windows read, so a strategy
    that otherwise exited remained visible as running. A daemon reader thread
    can be abandoned safely when the worker exits.
    """

    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()

    def finish() -> None:
        if not future.done():
            future.set_result(None)

    def read() -> None:
        sys.stdin.readline()
        try:
            loop.call_soon_threadsafe(finish)
        except RuntimeError:
            pass

    threading.Thread(
        target=read,
        name="trade-router-stop-reader",
        daemon=True,
    ).start()
    return future


def _load_strategy(path: Path) -> Strategy:
    module_name = f"trade_router_user_{path.stem}_{os.getpid()}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load strategy module.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    classes = [
        value
        for value in vars(module).values()
        if isinstance(value, type) and issubclass(value, Strategy) and value is not Strategy
    ]
    if len(classes) != 1:
        raise RuntimeError("Strategy file must define exactly one Strategy subclass.")
    return classes[0]()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    arguments = parser.parse_args()
    asyncio.run(run(arguments.path))


if __name__ == "__main__":
    main()
