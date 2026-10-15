from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitScope:
    key: str
    cost: float = 1.0

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("Rate-limit scope key is required.")
        if self.cost <= 0:
            raise ValueError("Rate-limit cost must be positive.")


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    capacity: float
    refill_per_second: float

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.refill_per_second <= 0:
            raise ValueError("Rate-limit capacity and refill rate must be positive.")


@dataclass(slots=True)
class _Bucket:
    policy: RateLimitPolicy
    tokens: float
    updated_at: float


class RateLimitManager:
    """Atomically consumes all hierarchical rate-limit scopes for a request."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    def configure(self, key: str, policy: RateLimitPolicy) -> None:
        now = self._clock()
        existing = self._buckets.get(key)
        tokens = policy.capacity if existing is None else min(existing.tokens, policy.capacity)
        self._buckets[key] = _Bucket(policy=policy, tokens=tokens, updated_at=now)

    async def acquire(self, scopes: Iterable[RateLimitScope]) -> None:
        requested = tuple(scopes)
        if not requested:
            return
        while True:
            async with self._lock:
                now = self._clock()
                wait_for = 0.0
                buckets: list[tuple[RateLimitScope, _Bucket]] = []
                for scope in requested:
                    bucket = self._buckets.get(scope.key)
                    if bucket is None:
                        raise KeyError(f"Rate-limit scope '{scope.key}' is not configured.")
                    elapsed = max(0.0, now - bucket.updated_at)
                    bucket.tokens = min(
                        bucket.policy.capacity,
                        bucket.tokens + elapsed * bucket.policy.refill_per_second,
                    )
                    bucket.updated_at = now
                    if scope.cost > bucket.policy.capacity:
                        raise ValueError(
                            f"Request cost exceeds capacity for scope '{scope.key}'."
                        )
                    shortage = max(0.0, scope.cost - bucket.tokens)
                    if shortage:
                        wait_for = max(
                            wait_for,
                            shortage / bucket.policy.refill_per_second,
                        )
                    buckets.append((scope, bucket))
                if wait_for <= 0:
                    for scope, bucket in buckets:
                        bucket.tokens -= scope.cost
                    return
            await asyncio.sleep(wait_for)

    async def snapshot(self) -> list[dict[str, float | str]]:
        async with self._lock:
            now = self._clock()
            result: list[dict[str, float | str]] = []
            for key, bucket in sorted(self._buckets.items()):
                elapsed = max(0.0, now - bucket.updated_at)
                available = min(
                    bucket.policy.capacity,
                    bucket.tokens + elapsed * bucket.policy.refill_per_second,
                )
                result.append(
                    {
                        "key": key,
                        "available": available,
                        "capacity": bucket.policy.capacity,
                        "refill_per_second": bucket.policy.refill_per_second,
                    }
                )
            return result
