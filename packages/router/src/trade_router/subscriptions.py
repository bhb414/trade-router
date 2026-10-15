from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .errors import RouterError, RouterErrorCode
from .rate_limits import RateLimitManager, RateLimitPolicy, RateLimitScope


@dataclass(frozen=True, slots=True)
class WebSocketLimitPolicy:
    """Fixed provider limits for one API-key/environment scope."""

    max_sessions: int
    max_subscriptions_per_session: int
    control_messages_per_second: float

    def __post_init__(self) -> None:
        if self.max_sessions < 1 or self.max_subscriptions_per_session < 1:
            raise ValueError("WebSocket session and subscription limits must be positive.")
        if self.control_messages_per_second <= 0:
            raise ValueError("WebSocket control rate must be positive.")


@dataclass(frozen=True, slots=True)
class SubscriptionLease:
    scope: str
    session_id: str
    subscription_key: str
    created: bool


class SubscriptionQuotaManager:
    """Shares subscriptions and enforces provider WebSocket limits centrally.

    The manager owns logical session allocation and reference counts. A broker
    adapter sends a subscribe/unsubscribe frame only when ``created`` is true,
    after this manager has paced the control message.
    """

    def __init__(self) -> None:
        self._policies: dict[str, WebSocketLimitPolicy] = {}
        self._sessions: dict[str, dict[str, set[str]]] = {}
        self._consumers: dict[str, dict[str, set[str]]] = {}
        self._subscription_sessions: dict[str, dict[str, str]] = {}
        self._limits = RateLimitManager()
        self._lock = asyncio.Lock()

    def configure(self, scope: str, policy: WebSocketLimitPolicy) -> None:
        self._policies[scope] = policy
        self._sessions.setdefault(scope, {})
        self._consumers.setdefault(scope, {})
        self._subscription_sessions.setdefault(scope, {})
        self._limits.configure(
            f"{scope}:websocket-control",
            RateLimitPolicy(
                capacity=1,
                refill_per_second=policy.control_messages_per_second,
            ),
        )

    async def subscribe(
        self,
        scope: str,
        *,
        consumer_id: str,
        subscription_key: str,
    ) -> SubscriptionLease:
        if not consumer_id or not subscription_key:
            raise ValueError("Consumer and subscription keys are required.")
        async with self._lock:
            policy = self._policy(scope)
            consumers = self._consumers[scope]
            existing_session = self._subscription_sessions[scope].get(subscription_key)
            if existing_session is not None:
                consumers[subscription_key].add(consumer_id)
                return SubscriptionLease(
                    scope, existing_session, subscription_key, created=False
                )

            session_id = self._available_session(scope, policy)
            self._sessions[scope][session_id].add(subscription_key)
            self._subscription_sessions[scope][subscription_key] = session_id
            consumers[subscription_key] = {consumer_id}

        await self._limits.acquire(
            (RateLimitScope(f"{scope}:websocket-control"),)
        )
        return SubscriptionLease(scope, session_id, subscription_key, created=True)

    async def unsubscribe(
        self,
        lease: SubscriptionLease,
        *,
        consumer_id: str,
    ) -> bool:
        """Release a consumer; return true only when a broker frame is needed."""

        async with self._lock:
            consumers = self._consumers.get(lease.scope, {}).get(
                lease.subscription_key
            )
            if not consumers or consumer_id not in consumers:
                return False
            consumers.remove(consumer_id)
            if consumers:
                return False
            del self._consumers[lease.scope][lease.subscription_key]
            session_id = self._subscription_sessions[lease.scope].pop(
                lease.subscription_key
            )
            session_subscriptions = self._sessions[lease.scope][session_id]
            session_subscriptions.discard(lease.subscription_key)
            if not session_subscriptions:
                del self._sessions[lease.scope][session_id]

        await self._limits.acquire(
            (RateLimitScope(f"{lease.scope}:websocket-control"),)
        )
        return True

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            result = []
            for scope, policy in sorted(self._policies.items()):
                sessions = self._sessions[scope]
                result.append(
                    {
                        "scope": scope,
                        "sessions": len(sessions),
                        "max_sessions": policy.max_sessions,
                        "subscriptions": sum(len(items) for items in sessions.values()),
                        "max_subscriptions_per_session": (
                            policy.max_subscriptions_per_session
                        ),
                        "control_messages_per_second": (
                            policy.control_messages_per_second
                        ),
                    }
                )
            return result

    def _policy(self, scope: str) -> WebSocketLimitPolicy:
        try:
            return self._policies[scope]
        except KeyError as exc:
            raise KeyError(f"WebSocket limit scope '{scope}' is not configured.") from exc

    def _available_session(
        self,
        scope: str,
        policy: WebSocketLimitPolicy,
    ) -> str:
        sessions = self._sessions[scope]
        for session_id, subscriptions in sessions.items():
            if len(subscriptions) < policy.max_subscriptions_per_session:
                return session_id
        if len(sessions) >= policy.max_sessions:
            raise RouterError(
                RouterErrorCode.RATE_LIMITED,
                "이 API 키의 웹소켓 구독 한도에 도달했습니다.",
                details={
                    "scope": scope,
                    "max_sessions": policy.max_sessions,
                    "max_subscriptions_per_session": (
                        policy.max_subscriptions_per_session
                    ),
                },
            )
        session_id = f"session-{len(sessions) + 1}"
        sessions[session_id] = set()
        return session_id
