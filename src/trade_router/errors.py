from __future__ import annotations

from typing import Any

from pydantic import Field

from .models import RouterModel


class ErrorBody(RouterModel):
    code: str
    message: str
    retryable: bool = False
    provider: str | None = None
    provider_code: str | None = None
    correlation_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RouterError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: int = 400,
        provider: str | None = None,
        provider_code: str | None = None,
        correlation_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.provider = provider
        self.provider_code = provider_code
        self.correlation_id = correlation_id
        self.details = details or {}

    def as_body(self) -> ErrorBody:
        return ErrorBody(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            provider=self.provider,
            provider_code=self.provider_code,
            correlation_id=self.correlation_id,
            details=self.details,
        )


class UnsupportedCapabilityError(RouterError):
    def __init__(self, provider: str, capability: str) -> None:
        super().__init__(
            "UNSUPPORTED_CAPABILITY",
            f"Provider '{provider}' does not support '{capability}'.",
            provider=provider,
            details={"capability": capability},
        )


class ProviderError(RouterError):
    pass


class OrderUncertainError(ProviderError):
    def __init__(self, provider: str, message: str, *, correlation_id: str) -> None:
        super().__init__(
            "ORDER_SUBMISSION_UNCERTAIN",
            message,
            retryable=False,
            status_code=502,
            provider=provider,
            correlation_id=correlation_id,
        )
