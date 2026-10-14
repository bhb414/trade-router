from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..models import (
    CredentialUsage,
    ProviderEnvironment,
    RiskLimits,
    StrategyConfig,
)


class ProfileCreate(BaseModel):
    id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    provider: str
    usage: CredentialUsage | None = None
    environment: ProviderEnvironment | None = None
    app_key: str | None = None
    app_secret: str | None = None
    display_name: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_usage(self) -> ProfileCreate:
        legacy_usage = CredentialUsage(self.environment.value) if self.environment else None
        if self.usage is not None and legacy_usage is not None and self.usage != legacy_usage:
            raise ValueError("usage and legacy environment do not match")
        if self.usage is None:
            self.usage = legacy_usage or CredentialUsage.AUTO
        return self


class AccountCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=128)
    profile_id: str
    environment: ProviderEnvironment | None = None
    account_number: str
    risk: RiskLimits = Field(default_factory=RiskLimits)


class StrategyCreate(BaseModel):
    config: StrategyConfig


class AmendRequest(BaseModel):
    quantity: int | None = Field(default=None, gt=0)
    price: Decimal | None = Field(default=None, gt=0)


class StreamRequest(BaseModel):
    profile_id: str
    environment: ProviderEnvironment | None = None
    subscriptions: list[dict[str, Any]]
