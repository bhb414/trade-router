from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from ..models import ProviderEnvironment, RiskLimits, StrategyConfig


class ProfileCreate(BaseModel):
    id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    provider: str
    environment: ProviderEnvironment
    app_key: str | None = None
    app_secret: str | None = None
    display_name: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class AccountCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=128)
    profile_id: str
    account_number: str
    risk: RiskLimits = Field(default_factory=RiskLimits)


class StrategyCreate(BaseModel):
    config: StrategyConfig


class AmendRequest(BaseModel):
    quantity: int | None = Field(default=None, gt=0)
    price: Decimal | None = Field(default=None, gt=0)


class StreamRequest(BaseModel):
    profile_id: str
    subscriptions: list[dict[str, Any]]
