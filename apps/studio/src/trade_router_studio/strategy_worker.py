from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
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
from trade_router_studio.file_io import read_shared_text, replace_snapshot, snapshot_backup_path


class StrategyLogger:
    def info(self, message: object) -> None:
        print(f"INFO {message}", flush=True)

    def warning(self, message: object) -> None:
        print(f"WARNING {message}", flush=True)

    def error(self, message: object) -> None:
        print(f"ERROR {message}", flush=True)


class StrategyState:
    """Durable snapshots, with a recovery journal for Windows sharing locks.

    set() never acknowledges an in-memory-only update. Normally it atomically
    replaces the main snapshot; when that path is held open, the same fsynced
    snapshot is committed to a journal before returning. Only its promotion is
    retried off the event loop. Revisions prevent an obsolete journal from
    reviving state after a newer main snapshot was committed.
    """

    _REVISION_KEY = "__trade_router_state_revision__"
    _RETRY_SECONDS = 0.1
    _CLOSE_TIMEOUT = 1.5
    _WARNING_SECONDS = 30.0

    def __init__(self, strategy_name: str, logger: StrategyLogger | None = None) -> None:
        root = Path(os.environ["TRADE_ROUTER_HOME"])
        safe_name = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in strategy_name
        )
        self._path = root / "data" / "strategy-state" / f"{safe_name}.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._pending_path = self._path.with_suffix(".json.pending")
        self._log = logger or StrategyLogger()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._writer: threading.Thread | None = None
        self._closed = False
        self._close_deadline: float | None = None
        self._error: BaseException | None = None
        self._last_warning = 0.0
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self.failure: asyncio.Future[BaseException] | None = (
            self._loop.create_future() if self._loop is not None else None
        )
        self._values, self._revision = self._read_snapshot(self._path)
        self._persisted_revision = self._revision if self._path.exists() else -1
        self._pending_revision: int | None = None
        snapshots = [(self._path, self._values, self._revision)] if self._persisted_revision >= 0 else []
        if self._pending_path.exists():
            pending, revision = self._read_snapshot(self._pending_path, required=True)
            self._pending_revision = revision
            snapshots.append((self._pending_path, pending, revision))
        # ReplaceFileW error 1177 can leave either acknowledged snapshot under
        # its backup name. Read every surviving record before choosing a
        # revision; otherwise an older pending journal can revive sold holdings.
        for original in (self._path, self._pending_path):
            backup = snapshot_backup_path(original)
            if backup.exists():
                values, revision = self._read_snapshot(
                    backup, required=True, allow_legacy=original == self._path,
                )
                snapshots.append((backup, values, revision))
        source, values, revision = max(
            snapshots, key=lambda item: item[2], default=(self._path, self._values, self._revision),
        )
        if source != self._path and revision > self._persisted_revision:
            revision = max(1, revision)  # A recovered legacy backup becomes a versioned journal.
            self._values, self._revision = values, revision
            if source != self._pending_path:
                # Copy rather than move: a second partial replacement must not
                # consume the only surviving acknowledged recovery snapshot.
                serialized = json.dumps(
                    {**values, self._REVISION_KEY: revision}, ensure_ascii=False, indent=2,
                )
                temporary = self._write_snapshot(serialized)
                replace_snapshot(temporary, self._pending_path)
                self._pending_revision = revision
            self._log.warning("이전 실행의 전략 상태 저장 기록을 복구했습니다.")
        if self._pending_revision is not None:
            with self._lock:
                if not self._promote_pending_locked():
                    self._start_writer_locked()

    def get(self, key: str, default: Any = None) -> Any:
        self._raise_failure()
        return self._values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if key == self._REVISION_KEY:
            raise ValueError("Reserved strategy state key.")
        with self._lock:
            self._raise_failure()
            if self._closed:
                raise RuntimeError("Strategy state is closed.")
            values = {**self._values, key: value}
            revision = self._revision + 1
            # Validate/serialize before touching the committed in-memory state.
            serialized = json.dumps(
                {**values, self._REVISION_KEY: revision}, ensure_ascii=False, indent=2,
            )
            temporary = self._write_snapshot(serialized)
            committed = False
            try:
                try:
                    replace_snapshot(temporary, self._path)
                except PermissionError:
                    # Unlike the displayed/main snapshot, nobody keeps this
                    # journal open. Failure here must propagate to the worker.
                    replace_snapshot(temporary, self._pending_path)
                    self._pending_revision = revision
                    self._warn_pending_locked()
                    self._start_writer_locked()
                else:
                    self._persisted_revision = revision
                    if self._pending_revision is not None:
                        if not self._promote_pending_locked():
                            self._start_writer_locked()
                self._values, self._revision = values, revision
                committed = True
            finally:
                # Native replacement may fail after renaming the old target.
                # Keep the attempted snapshot for diagnosis/recovery instead of
                # deleting the sole remaining new file on that error path.
                if committed:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _write_snapshot(self, serialized: str) -> Path:
        fd, name = tempfile.mkstemp(prefix=self._path.name + ".", suffix=".tmp", dir=self._path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return temporary

    def _read_snapshot(self, path: Path, *, required: bool = False,
                       allow_legacy: bool = False) -> tuple[dict[str, Any], int]:
        try:
            payload = json.loads(read_shared_text(path))
            if not isinstance(payload, dict):
                raise ValueError("Strategy state must be an object.")
            revision = payload.pop(self._REVISION_KEY, 0)
            if (not isinstance(revision, int) or isinstance(revision, bool)
                    or revision < (1 if required and not allow_legacy else 0)):
                raise ValueError("Invalid strategy state revision.")
            return payload, revision
        except FileNotFoundError as exc:
            if not required:
                return {}, 0
            raise RuntimeError("전략 상태 복구 기록을 읽지 못했습니다. 기존 파일을 보존합니다.") from exc
        except (OSError, ValueError) as exc:
            # An unreadable main file is not revision zero: promoting an older
            # leftover journal would resurrect obsolete holdings/order state.
            description = "전략 상태 복구 기록" if required else "기존 전략 상태 파일"
            raise RuntimeError(f"{description}을 읽지 못했습니다. 기존 파일을 보존합니다.") from exc

    def _promote_pending_locked(self) -> bool:
        if self._pending_revision is None:
            return True
        try:
            if self._pending_revision <= self._persisted_revision:
                # A newer normal save won. An old journal is cleanup-only and
                # must never replace it, including after a crash/restart.
                self._pending_path.unlink(missing_ok=True)
            else:
                replace_snapshot(self._pending_path, self._path)
                self._persisted_revision = self._pending_revision
        except PermissionError:
            self._warn_pending_locked()
            return False
        self._pending_revision = None
        return True

    def _warn_pending_locked(self) -> None:
        now = time.monotonic()
        if self._last_warning == 0.0 or now - self._last_warning >= self._WARNING_SECONDS:
            self._last_warning = now
            self._log.warning(
                "전략 상태 파일이 다른 작업에 사용 중입니다. 변경 내용은 복구 기록에 저장했으며 "
                "매도 감시를 유지하면서 파일 반영을 재시도합니다."
            )

    def _start_writer_locked(self) -> None:
        if self._writer is None:
            self._writer = threading.Thread(
                target=self._retry_pending, name="trade-router-state-writer", daemon=True,
            )
            self._writer.start()
        self._wake.set()

    def _retry_pending(self) -> None:
        try:
            while True:
                self._wake.wait(self._RETRY_SECONDS)
                self._wake.clear()
                with self._lock:
                    clean = self._promote_pending_locked()
                    if self._closed:
                        if clean or time.monotonic() >= (self._close_deadline or 0.0):
                            return
                if clean:
                    # Sleep until the next locked save or close, with no polling.
                    self._wake.wait()
        except BaseException as exc:
            with self._lock:
                self._error = exc
            self._log.error(f"전략 상태 저장 기록을 반영하지 못했습니다: {exc}")
            if self._loop is not None and self.failure is not None:
                def report(error: BaseException = exc) -> None:
                    if self.failure is not None and not self.failure.done():
                        self.failure.set_result(error)
                try:
                    self._loop.call_soon_threadsafe(report)
                except RuntimeError:
                    pass

    def _raise_failure(self) -> None:
        if self._error is not None:
            raise RuntimeError("전략 상태 저장에 실패했습니다. 복구 기록을 확인하세요.") from self._error

    def close(self) -> None:
        """Flush promotion briefly; a locked but durable journal survives exit."""
        with self._lock:
            self._closed = True
            self._close_deadline = time.monotonic() + self._CLOSE_TIMEOUT
            self._wake.set()
            writer = self._writer
        if writer is not None:
            writer.join(self._CLOSE_TIMEOUT + self._RETRY_SECONDS * 2)
        if self._pending_revision is not None:
            self._log.warning("전략 상태 복구 기록을 보존했습니다. 다음 실행에서 이어서 복구합니다.")
        self._raise_failure()


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
    state_failure = getattr(getattr(context, "state", None), "failure", None)
    waiters = {stop_signal, receiver, dispatcher.failure}
    if state_failure is not None:
        waiters.add(state_failure)
    try:
        done, _ = await asyncio.wait(
            waiters, return_when=asyncio.FIRST_COMPLETED,
        )
        if state_failure is not None and state_failure in done:
            raise state_failure.result()
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
    state = StrategyState(path.name, logger)
    try:
        await _run_with_state(strategy, config, logger, state)
    finally:
        await asyncio.to_thread(state.close)


async def _run_with_state(strategy: Strategy, config: Any, logger: StrategyLogger, state: StrategyState) -> None:
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
