from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from .models import ConnectionTarget


@dataclass(frozen=True, slots=True)
class TokenScopeKey:
    provider: str
    connection_id: str
    target: ConnectionTarget

    @property
    def storage_key(self) -> str:
        return f"{self.provider}:{self.connection_id}:{self.target.value}"


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: str = field(repr=False)
    expires_at: datetime
    token_type: str = "Bearer"
    issued_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("Access token cannot be empty.")
        if self.expires_at.tzinfo is None:
            raise ValueError("Token expiry must include a timezone.")
        if self.issued_at is not None and self.issued_at.tzinfo is None:
            raise ValueError("Token issue time must include a timezone.")

    def is_usable(self, now: datetime, refresh_skew: timedelta) -> bool:
        if now.tzinfo is None:
            raise ValueError("Current time must include a timezone.")
        return now + refresh_skew < self.expires_at


class TokenStore(Protocol):
    async def load(self, scope: TokenScopeKey) -> AccessToken | None: ...

    async def save(self, scope: TokenScopeKey, token: AccessToken) -> None: ...

    async def delete(self, scope: TokenScopeKey) -> None: ...


TokenIssuer = Callable[[TokenScopeKey], Awaitable[AccessToken]]


class InMemoryTokenStore:
    def __init__(self) -> None:
        self._tokens: dict[str, AccessToken] = {}

    async def load(self, scope: TokenScopeKey) -> AccessToken | None:
        return self._tokens.get(scope.storage_key)

    async def save(self, scope: TokenScopeKey, token: AccessToken) -> None:
        self._tokens[scope.storage_key] = token

    async def delete(self, scope: TokenScopeKey) -> None:
        self._tokens.pop(scope.storage_key, None)


class TokenManager:
    """Shares one valid token per adapter-defined token scope."""

    def __init__(
        self,
        store: TokenStore,
        *,
        refresh_skew: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._refresh_skew = refresh_skew
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._locks: dict[TokenScopeKey, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def get(self, scope: TokenScopeKey, issuer: TokenIssuer) -> AccessToken:
        token = await self._store.load(scope)
        now = self._clock()
        if token is not None and token.is_usable(now, self._refresh_skew):
            return token

        lock = await self._lock_for(scope)
        async with lock:
            token = await self._store.load(scope)
            now = self._clock()
            if token is not None and token.is_usable(now, self._refresh_skew):
                return token
            token = await issuer(scope)
            await self._store.save(scope, token)
            return token

    async def invalidate(self, scope: TokenScopeKey) -> None:
        lock = await self._lock_for(scope)
        async with lock:
            await self._store.delete(scope)

    async def status(self, scope: TokenScopeKey) -> dict[str, object]:
        token = await self._store.load(scope)
        if token is None:
            return {"available": False, "expires_at": None, "usable": False}
        now = self._clock()
        return {
            "available": True,
            "expires_at": token.expires_at.isoformat(),
            "usable": token.is_usable(now, self._refresh_skew),
        }

    async def _lock_for(self, scope: TokenScopeKey) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(scope, asyncio.Lock())
