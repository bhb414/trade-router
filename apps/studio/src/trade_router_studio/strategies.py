from __future__ import annotations

import asyncio
import ast
import json
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from .paths import WorkspacePaths
from .sessions import StrategySessionRegistry


MAX_SOURCE_BYTES = 512 * 1024
MAX_LOG_LINES = 1_000
STRATEGY_TYPES = {"script", "block"}
_SAFE_NAME = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]+\.py$", re.IGNORECASE)


class StrategyNameError(ValueError):
    pass


@dataclass(slots=True)
class StrategyLogEntry:
    sequence: int
    timestamp: datetime
    level: str
    message: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp.isoformat(),
            "level": self.level,
            "message": self.message,
        }


@dataclass(slots=True)
class StrategyProcess:
    name: str
    process: asyncio.subprocess.Process | None = None
    reader_task: asyncio.Task[None] | None = None
    logs: deque[StrategyLogEntry] = field(
        default_factory=lambda: deque(maxlen=MAX_LOG_LINES)
    )
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    exit_code: int | None = None
    stop_requested: bool = False
    runtime_managed: bool = False
    next_log_sequence: int = 0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None


class StrategyManager:
    def __init__(
        self,
        paths: WorkspacePaths,
        sessions: StrategySessionRegistry | None = None,
        *,
        router_url: str = "http://127.0.0.1:8890",
        on_exit: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.paths = paths
        self._sessions = sessions
        self._router_url = router_url
        self._on_exit = on_exit
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

    def strategy_type(self, name: str) -> str:
        path = self.resolve(name)
        metadata_path = path.with_suffix(".strategy.json")
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return "script"
        strategy_type = payload.get("type") if isinstance(payload, dict) else None
        return strategy_type if strategy_type in STRATEGY_TYPES else "script"

    async def save(
        self,
        name: str,
        source: str,
        *,
        strategy_type: str | None = None,
    ) -> dict[str, Any]:
        encoded = source.encode("utf-8")
        if len(encoded) > MAX_SOURCE_BYTES:
            raise ValueError("Strategy source exceeds 512 KiB.")
        path = self.resolve(name)
        state = self._states.get(name)
        if state and state.running:
            raise RuntimeError("Stop the strategy before saving it.")
        existing_type = self.strategy_type(name)
        requested_type = strategy_type or existing_type
        if requested_type not in STRATEGY_TYPES:
            raise ValueError("Strategy type must be script or block.")
        if path.exists() and strategy_type is not None and requested_type != existing_type:
            raise RuntimeError("An existing strategy type cannot be changed.")
        temporary = path.with_suffix(".py.tmp")
        temporary.write_bytes(encoded)
        temporary.replace(path)
        if strategy_type is not None:
            metadata_path = path.with_suffix(".strategy.json")
            metadata_temporary = metadata_path.with_suffix(".json.tmp")
            metadata_temporary.write_text(
                json.dumps(
                    {"format": 1, "type": requested_type},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            metadata_temporary.replace(metadata_path)
        return self._describe(name, state)

    async def delete(self, name: str) -> None:
        path = self.resolve(name)
        state = self._states.get(name)
        if state and state.running:
            raise RuntimeError("Stop the strategy before deleting it.")
        path.unlink(missing_ok=True)
        path.with_suffix(".strategy.json").unlink(missing_ok=True)
        self._states.pop(name, None)

    async def start(self, name: str) -> dict[str, Any]:
        path = self.resolve(name)
        if not path.is_file():
            raise FileNotFoundError(name)
        async with self._lock:
            state = self._states.setdefault(name, StrategyProcess(name=name))
            if state.running:
                return self._describe(name, state)
            self._append_log(state, "system", "전략을 시작했습니다.")
            state.started_at = datetime.now().astimezone()
            state.stopped_at = None
            state.exit_code = None
            state.stop_requested = False
            source = path.read_text(encoding="utf-8")
            state.runtime_managed = _uses_strategy_runtime(source)
            environment = os.environ.copy()
            environment["PYTHONUNBUFFERED"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUTF8"] = "1"
            environment["TRADE_ROUTER_HOME"] = str(self.paths.root)
            environment["TRADE_ROUTER_URL"] = self._router_url
            environment["TRADE_ROUTER_STRATEGY_NAME"] = name
            if self._sessions is not None:
                environment["TRADE_ROUTER_SESSION_TOKEN"] = self._sessions.issue(name)
            command = (
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "trade_router_studio.strategy_worker",
                    str(path),
                ]
                if state.runtime_managed
                else [sys.executable, "-u", str(path)]
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.paths.strategies),
                env=environment,
                stdin=asyncio.subprocess.PIPE,
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
            self._append_log(state, "system", "전략 중지를 요청했습니다.")
            state.stop_requested = True
            if state.runtime_managed and state.process.stdin is not None:
                state.process.stdin.write(b"stop\n")
                await state.process.stdin.drain()
            else:
                state.process.terminate()
            try:
                await asyncio.wait_for(state.process.wait(), timeout=5)
            except TimeoutError:
                self._append_log(
                    state,
                    "warning",
                    "정상 종료 대기 시간이 지나 프로세스를 종료합니다.",
                )
                state.process.terminate()
                try:
                    await asyncio.wait_for(state.process.wait(), timeout=2)
                except TimeoutError:
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
        visible = [entry for entry in lines if entry.sequence > after]
        return {
            "lines": [entry.message for entry in visible],
            "entries": [entry.as_dict() for entry in visible],
            "next": state.next_log_sequence if state else 0,
            "running": bool(state and state.running),
            "exit_code": state.exit_code if state else None,
        }

    async def _read_output(self, state: StrategyProcess) -> None:
        assert state.process is not None
        assert state.process.stdout is not None
        while line := await state.process.stdout.readline():
            decoded = self._decode_output(line).rstrip()
            level, message = self._classify_output(decoded)
            self._append_log(state, level, message)
        state.exit_code = await state.process.wait()
        if self._sessions is not None:
            self._sessions.revoke_strategy(state.name)
        if self._on_exit is not None:
            await self._on_exit(state.name)
        state.stopped_at = datetime.now().astimezone()
        if state.stop_requested:
            self._append_log(state, "success", "사용자 요청으로 전략을 중지했습니다.")
        else:
            level = "success" if state.exit_code == 0 else "error"
            self._append_log(
                state,
                level,
                f"전략 프로세스가 종료되었습니다. 종료 코드 {state.exit_code}",
            )

    @staticmethod
    def _append_log(state: StrategyProcess, level: str, message: str) -> None:
        if not message:
            return
        state.next_log_sequence += 1
        state.logs.append(
            StrategyLogEntry(
                sequence=state.next_log_sequence,
                timestamp=datetime.now().astimezone(),
                level=level,
                message=message,
            )
        )

    @staticmethod
    def _decode_output(value: bytes) -> str:
        for encoding in ("utf-8", "cp949"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", errors="replace")

    @staticmethod
    def _classify_output(line: str) -> tuple[str, str]:
        if line.startswith("INFO "):
            return "info", line[5:]
        if line.startswith("WARNING "):
            return "warning", line[8:]
        if line.startswith("ERROR "):
            return "error", line[6:]
        ready = re.fullmatch(
            r"--- runtime strategy ready \((\d+) schedules\) ---", line
        )
        if ready:
            return (
                "success",
                f"스케줄 {ready.group(1)}개를 등록하고 실행 준비를 마쳤습니다.",
            )
        if line.startswith("Traceback ") or line.startswith("  File "):
            return "error", line
        return "output", line

    def _describe(
        self,
        name: str, state: StrategyProcess | None
    ) -> dict[str, Any]:
        return {
            "name": name,
            "strategy_type": self.strategy_type(name),
            "running": bool(state and state.running),
            "started_at": state.started_at.isoformat() if state and state.started_at else None,
            "stopped_at": state.stopped_at.isoformat() if state and state.stopped_at else None,
            "exit_code": state.exit_code if state else None,
            "stop_requested": bool(state and state.stop_requested),
            "runtime_managed": bool(state and state.runtime_managed),
        }


def _uses_strategy_runtime(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "Strategy":
                return True
            if isinstance(base, ast.Attribute) and base.attr == "Strategy":
                return True
    return False
