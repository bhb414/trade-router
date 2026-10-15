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
            while True:
                event_task = asyncio.create_task(router.runtime.next_event())
                done, _ = await asyncio.wait(
                    {stop_signal, event_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_signal in done:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                    break
                try:
                    event = await event_task
                except RouterError as exc:
                    logger.warning(
                        f"중앙 실행 이벤트 연결이 끊겨 다시 시도합니다: {exc}"
                    )
                    await asyncio.sleep(1)
                    continue
                if event is None:
                    continue
                event_type = str(event.get("event_type") or "schedule")
                if event_type == "quote":
                    payload = dict(event["quote"])
                    payload["source_alias"] = event.get("source_alias")
                    await strategy.on_quote(context, quote_from_event(payload))
                    continue
                if event_type == "orderbook":
                    payload = dict(event["orderbook"])
                    payload["source_alias"] = event.get("source_alias")
                    await strategy.on_orderbook(context, orderbook_from_event(payload))
                    continue
                if event_type == "order":
                    await strategy.on_order(context, _order_from_payload(event["order"]))
                    continue
                if event_type == "fill":
                    await strategy.on_fill(context, fill_from_event(event["fill"]))
                    continue
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
