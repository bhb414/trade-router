from __future__ import annotations

from enum import StrEnum
from typing import Any


class RouterErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_STATUS_UNKNOWN = "ORDER_STATUS_UNKNOWN"
    ORDER_PERMISSION_REQUIRED = "ORDER_PERMISSION_REQUIRED"
    RISK_LIMIT_EXCEEDED = "RISK_LIMIT_EXCEEDED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


class RouterError(Exception):
    def __init__(
        self,
        code: RouterErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }
