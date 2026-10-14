from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(tz=KST)


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def ensure_kst(value: datetime | None = None) -> datetime:
    value = value or now_kst()
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)
