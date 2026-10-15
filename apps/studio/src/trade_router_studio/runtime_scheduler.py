from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from trade_router import ScheduleKind, ScheduleSpec, SchedulerPlan


SEOUL = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    schedule_id: str
    kind: ScheduleKind
    scheduled_at: datetime
    bar_interval: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "schedule_id": self.schedule_id,
            "kind": self.kind.value,
            "scheduled_at": self.scheduled_at.isoformat(),
            "bar_interval": self.bar_interval,
        }


@dataclass(slots=True)
class _Registration:
    spec: ScheduleSpec
    next_at: datetime


class RuntimeScheduler:
    """Central wall-clock scheduler. Missed short ticks are skipped, not replayed."""

    def __init__(self) -> None:
        self._registrations: dict[str, list[_Registration]] = {}
        self._queues: dict[str, asyncio.Queue[ScheduledEvent]] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="trade-router-scheduler")

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            self._registrations.clear()
            self._queues.clear()

    async def register(self, strategy_name: str, specs: list[ScheduleSpec]) -> None:
        ids = [spec.id for spec in specs]
        if len(ids) != len(set(ids)):
            raise ValueError("Schedule ids must be unique within a strategy.")
        now = datetime.now(SEOUL)
        registrations = [
            _Registration(spec=spec, next_at=_next_after(spec, now)) for spec in specs
        ]
        async with self._lock:
            self._registrations[strategy_name] = registrations
            self._queues.setdefault(strategy_name, asyncio.Queue(maxsize=100))

    async def unregister(self, strategy_name: str) -> None:
        async with self._lock:
            self._registrations.pop(strategy_name, None)
            self._queues.pop(strategy_name, None)

    async def next_event(
        self,
        strategy_name: str,
        *,
        timeout: float = 25.0,
    ) -> ScheduledEvent | None:
        async with self._lock:
            queue = self._queues.setdefault(strategy_name, asyncio.Queue(maxsize=100))
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            result: list[dict[str, Any]] = []
            for strategy, registrations in sorted(self._registrations.items()):
                queue = self._queues.get(strategy)
                result.append(
                    {
                        "strategy": strategy,
                        "queued_events": queue.qsize() if queue else 0,
                        "schedules": [
                            {
                                "id": item.spec.id,
                                "kind": item.spec.kind.value,
                                "next_at": item.next_at.isoformat(),
                            }
                            for item in registrations
                        ],
                    }
                )
            return result

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            now = datetime.now(SEOUL)
            due: list[tuple[str, ScheduledEvent]] = []
            async with self._lock:
                for strategy, registrations in self._registrations.items():
                    for registration in registrations:
                        if registration.next_at > now:
                            continue
                        scheduled_at = _logical_schedule_time(registration)
                        registration.next_at = _next_after(registration.spec, now)
                        window = registration.spec.window
                        if window is not None and not window.contains(scheduled_at):
                            continue
                        if registration.spec.trading_days_only and scheduled_at.weekday() >= 5:
                            continue
                        due.append(
                            (
                                strategy,
                                ScheduledEvent(
                                    schedule_id=registration.spec.id,
                                    kind=registration.spec.kind,
                                    scheduled_at=scheduled_at,
                                    bar_interval=registration.spec.bar_interval,
                                ),
                            )
                        )
                for strategy, event in due:
                    queue = self._queues.get(strategy)
                    if queue is None:
                        continue
                    if queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    queue.put_nowait(event)


def schedule_from_dict(payload: dict[str, Any]) -> ScheduleSpec:
    plan = SchedulerPlan()
    kind = ScheduleKind(str(payload["kind"]))
    schedule_id = str(payload["id"])
    between_payload = payload.get("between")
    between = (
        (str(between_payload[0]), str(between_payload[1]))
        if isinstance(between_payload, list) and len(between_payload) == 2
        else None
    )
    if kind is ScheduleKind.INTERVAL:
        return plan.every(
            seconds=int(payload["interval_seconds"]),
            schedule_id=schedule_id,
            between=between,
            aligned=bool(payload.get("aligned", True)),
            trading_days_only=bool(payload.get("trading_days_only", True)),
        )
    if kind is ScheduleKind.BAR_CLOSE:
        return plan.on_bar_close(
            str(payload["bar_interval"]),
            schedule_id=schedule_id,
            between=between,
            settle_seconds=int(payload.get("settle_seconds", 2)),
            trading_days_only=bool(payload.get("trading_days_only", True)),
        )
    if kind is ScheduleKind.DAILY:
        return plan.daily_at(
            str(payload["at"]),
            schedule_id=schedule_id,
            trading_days_only=bool(payload.get("trading_days_only", True)),
        )
    raise ValueError(f"Schedule kind {kind.value} is not implemented yet.")


def _next_after(spec: ScheduleSpec, after: datetime) -> datetime:
    local = after.astimezone(SEOUL)
    if spec.kind is ScheduleKind.INTERVAL:
        return SchedulerPlan().next_interval(spec, local)
    if spec.kind is ScheduleKind.BAR_CLOSE:
        seconds = _bar_interval_seconds(spec.bar_interval or "")
        interval_spec = ScheduleSpec(
            id=spec.id,
            kind=ScheduleKind.INTERVAL,
            interval_seconds=seconds,
            aligned=True,
            window=spec.window,
        )
        logical_after = local - timedelta(seconds=spec.settle_seconds)
        return (
            SchedulerPlan().next_interval(interval_spec, logical_after)
            + timedelta(seconds=spec.settle_seconds)
        )
    if spec.kind is ScheduleKind.DAILY and spec.at is not None:
        candidate = local.replace(
            hour=spec.at.hour,
            minute=spec.at.minute,
            second=spec.at.second,
            microsecond=0,
        )
        return candidate if candidate > local else candidate + timedelta(days=1)
    raise ValueError(f"Schedule kind {spec.kind.value} is not implemented yet.")


def _bar_interval_seconds(value: str) -> int:
    clean = value.strip().lower()
    if clean.endswith("m") and clean[:-1].isdigit():
        minutes = int(clean[:-1])
        if minutes > 0:
            return minutes * 60
    raise ValueError("Central scheduler currently supports minute bars such as 1m or 3m.")


def _logical_schedule_time(registration: _Registration) -> datetime:
    if registration.spec.kind is ScheduleKind.BAR_CLOSE:
        return registration.next_at - timedelta(seconds=registration.spec.settle_seconds)
    return registration.next_at
