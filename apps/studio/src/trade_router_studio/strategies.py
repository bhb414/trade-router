from __future__ import annotations

import asyncio
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import WorkspacePaths


MAX_SOURCE_BYTES = 512 * 1024
MAX_LOG_LINES = 1_000
_SAFE_NAME = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]+\.py$", re.IGNORECASE)


class StrategyNameError(ValueError):
    pass


@dataclass(slots=True)
class StrategyProcess:
    name: str
    process: asyncio.subprocess.Process | None = None
    reader_task: asyncio.Task[None] | None = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    exit_code: int | None = None
    stop_requested: bool = False

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None


class StrategyManager:
    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths
        self._states: dict[str, StrategyProcess] = {}
        self._lock = asyncio.Lock()

    def resolve(self, name: str) -> Path:
        clean = name.strip()
        if (
            not clean
            or clean in {".", ".."}
            or not _SAFE_NAME.fullmatch(clean)
            or Path(clean).name != clean
        ):
            raise StrategyNameError(
                "Strategy name must be a single valid file name ending in .py."
            )
        path = self.paths.strategies / clean
        strategy_root = os.path.normcase(os.path.abspath(self.paths.strategies))
        path_parent = os.path.normcase(os.path.abspath(path.parent))
        if path_parent != strategy_root:
            raise StrategyNameError("Strategy path is outside the workspace.")
        return path

    def list(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.paths.strategies.glob("*.py"), key=lambda p: p.name.casefold()):
            state = self._states.get(path.name)
            result.append(self._describe(path.name, state))
        return result

    def read(self, name: str) -> str:
        path = self.resolve(name)
        if not path.is_file():
            raise FileNotFoundError(name)
        return path.read_text(encoding="utf-8")

    async def save(self, name: str, source: str) -> dict[str, Any]:
        encoded = source.encode("utf-8")
        if len(encoded) > MAX_SOURCE_BYTES:
            raise ValueError("Strategy source exceeds 512 KiB.")
        path = self.resolve(name)
        state = self._states.get(name)
        if state and state.running:
            raise RuntimeError("Stop the strategy before saving it.")
        temporary = path.with_suffix(".py.tmp")
        temporary.write_bytes(encoded)
        temporary.replace(path)
        return self._describe(name, state)

    async def delete(self, name: str) -> None:
        path = self.resolve(name)
        state = self._states.get(name)
        if state and state.running:
            raise RuntimeError("Stop the strategy before deleting it.")
        path.unlink(missing_ok=True)
        self._states.pop(name, None)

    async def start(self, name: str) -> dict[str, Any]:
        path = self.resolve(name)
        if not path.is_file():
            raise FileNotFoundError(name)
        async with self._lock:
            state = self._states.setdefault(name, StrategyProcess(name=name))
            if state.running:
                return self._describe(name, state)
            state.logs.append("--- starting strategy ---")
            state.started_at = datetime.now().astimezone()
            state.stopped_at = None
            state.exit_code = None
            state.stop_requested = False
            environment = os.environ.copy()
            environment["PYTHONUNBUFFERED"] = "1"
            environment["TRADE_ROUTER_HOME"] = str(self.paths.root)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                str(path),
                cwd=str(self.paths.strategies),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            state.process = process
            state.reader_task = asyncio.create_task(self._read_output(state))
            return self._describe(name, state)

    async def stop(self, name: str) -> dict[str, Any]:
        self.resolve(name)
        async with self._lock:
            state = self._states.get(name)
            if state is None or not state.running:
                return self._describe(name, state)
            assert state.process is not None
            state.logs.append("--- stopping strategy ---")
            state.stop_requested = True
            state.process.terminate()
            try:
                await asyncio.wait_for(state.process.wait(), timeout=5)
            except TimeoutError:
                state.logs.append("--- forced stop after timeout ---")
                state.process.kill()
                await state.process.wait()
            if state.reader_task:
                await state.reader_task
            return self._describe(name, state)

    async def shutdown(self) -> None:
        running = [name for name, state in self._states.items() if state.running]
        for name in running:
            await self.stop(name)

    def details(self, name: str) -> dict[str, Any]:
        self.resolve(name)
        return self._describe(name, self._states.get(name))

    def logs(self, name: str, after: int = 0) -> dict[str, Any]:
        self.resolve(name)
        state = self._states.get(name)
        lines = list(state.logs) if state else []
        safe_after = min(max(after, 0), len(lines))
        return {
            "lines": lines[safe_after:],
            "next": len(lines),
            "running": bool(state and state.running),
            "exit_code": state.exit_code if state else None,
        }

    async def _read_output(self, state: StrategyProcess) -> None:
        assert state.process is not None
        assert state.process.stdout is not None
        while line := await state.process.stdout.readline():
            state.logs.append(line.decode("utf-8", errors="replace").rstrip())
        state.exit_code = await state.process.wait()
        state.stopped_at = datetime.now().astimezone()
        if state.stop_requested:
            state.logs.append("--- strategy stopped by user ---")
        else:
            state.logs.append(f"--- strategy exited ({state.exit_code}) ---")

    @staticmethod
    def _describe(
        name: str, state: StrategyProcess | None
    ) -> dict[str, Any]:
        return {
            "name": name,
            "running": bool(state and state.running),
            "started_at": state.started_at.isoformat() if state and state.started_at else None,
            "stopped_at": state.stopped_at.isoformat() if state and state.stopped_at else None,
            "exit_code": state.exit_code if state else None,
            "stop_requested": bool(state and state.stop_requested),
        }
