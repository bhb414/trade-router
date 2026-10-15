from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")


class ScheduleKind(StrEnum):
    INTERVAL = "INTERVAL"
    BAR_CLOSE = "BAR_CLOSE"
    DAILY = "DAILY"
    MARKET_OPEN = "MARKET_OPEN"
    MARKET_CLOSE = "MARKET_CLOSE"


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: time
    end: time

    @classmethod
    def parse(cls, start: str, end: str) -> "TimeWindow":
        return cls(_parse_time(start), _parse_time(end))

    def contains(self, moment: datetime) -> bool:
        local = moment.astimezone(SEOUL).timetz().replace(tzinfo=None)
        if self.start <= self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    id: str
    kind: ScheduleKind
    interval_seconds: int | None = None
    bar_interval: str | None = None
    at: time | None = None
    window: TimeWindow | None = None
    aligned: bool = True
    settle_seconds: int = 0
    trading_days_only: bool = True


class SchedulerPlan:
    def __init__(self) -> None:
        self._items: list[ScheduleSpec] = []

    @property
    def items(self) -> tuple[ScheduleSpec, ...]:
        return tuple(self._items)

    def every(
        self,
        *,
        seconds: int,
        schedule_id: str,
        between: tuple[str, str] | None = None,
        aligned: bool = True,
        trading_days_only: bool = True,
    ) -> ScheduleSpec:
        if seconds <= 0:
            raise ValueError("Schedule interval must be positive.")
        return self._add(
            ScheduleSpec(
                id=_validate_id(schedule_id),
                kind=ScheduleKind.INTERVAL,
                interval_seconds=seconds,
                window=_window(between),
                aligned=aligned,
                trading_days_only=trading_days_only,
            )
        )

    def on_bar_close(
        self,
        interval: str,
        *,
        schedule_id: str,
        between: tuple[str, str] | None = None,
        settle_seconds: int = 2,
        trading_days_only: bool = True,
    ) -> ScheduleSpec:
        interval = interval.strip().lower()
        if not interval:
            raise ValueError("Bar interval is required.")
        if settle_seconds < 0 or settle_seconds > 30:
            raise ValueError("Bar settlement delay must be between 0 and 30 seconds.")
        return self._add(
            ScheduleSpec(
                id=_validate_id(schedule_id),
                kind=ScheduleKind.BAR_CLOSE,
                bar_interval=interval,
                window=_window(between),
                settle_seconds=settle_seconds,
                trading_days_only=trading_days_only,
            )
        )

    def daily_at(
        self,
        value: str,
        *,
        schedule_id: str,
        trading_days_only: bool = True,
    ) -> ScheduleSpec:
        return self._add(
            ScheduleSpec(
                id=_validate_id(schedule_id),
                kind=ScheduleKind.DAILY,
                at=_parse_time(value),
                trading_days_only=trading_days_only,
            )
        )

    def next_interval(self, spec: ScheduleSpec, after: datetime) -> datetime:
        if spec.kind is not ScheduleKind.INTERVAL or spec.interval_seconds is None:
            raise ValueError("Schedule is not an interval schedule.")
        local = after.astimezone(SEOUL)
        if not spec.aligned:
            return local + timedelta(seconds=spec.interval_seconds)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = (local - midnight).total_seconds()
        next_slot = (int(elapsed) // spec.interval_seconds + 1) * spec.interval_seconds
        return midnight + timedelta(seconds=next_slot)

    def _add(self, spec: ScheduleSpec) -> ScheduleSpec:
        if any(item.id == spec.id for item in self._items):
            raise ValueError(f"Schedule id '{spec.id}' is already registered.")
        self._items.append(spec)
        return spec


def _parse_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid schedule time '{value}'.") from exc
    if parsed.tzinfo is not None:
        raise ValueError("Schedule time must not include a timezone.")
    return parsed


def _window(value: tuple[str, str] | None) -> TimeWindow | None:
    return TimeWindow.parse(*value) if value is not None else None


def _validate_id(value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError("Schedule id is required.")
    return clean
