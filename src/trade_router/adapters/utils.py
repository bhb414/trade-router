from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from ..core.time import ensure_kst, now_kst
from ..models import SourceMetadata


def decimal_of(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return default


def int_of(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(Decimal(str(value).replace(",", "").strip()))
    except (InvalidOperation, ValueError):
        return default


def first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def source_metadata(
    *,
    provider: str,
    profile: str,
    generation: int,
    timestamp: datetime | None = None,
    correlation_id: str | None = None,
    delayed: bool = False,
    stale: bool = False,
) -> SourceMetadata:
    received = now_kst()
    return SourceMetadata(
        provider=provider,
        profile=profile,
        source_timestamp=ensure_kst(timestamp or received),
        received_at=received,
        connection_generation=generation,
        delayed=delayed,
        stale=stale,
        correlation_id=correlation_id or uuid4().hex,
    )


def redact_text(message: Any, *sensitive_values: Any) -> str:
    text = str(message or "")
    for value in sensitive_values:
        if isinstance(value, dict):
            candidates = value.values()
        elif isinstance(value, (list, tuple, set)):
            candidates = value
        else:
            candidates = (value,)
        for candidate in candidates:
            rendered = str(candidate or "")
            if len(rendered) >= 4:
                text = text.replace(rendered, "<redacted>")
    return text
