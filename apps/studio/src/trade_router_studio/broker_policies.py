from __future__ import annotations

from typing import Any


# Fixed, provider-owned REST pacing. These values are deliberately not user
# configurable: every strategy, account and Studio interaction sharing an API
# key must pass through the same central bucket.
_REST_RATE_LIMITS: dict[tuple[str, str], dict[str, Any]] = {
    ("kis", "PAPER"): {
        "requests_per_second": 1.0,
        "provider_max_requests_per_second": 1.0,
        "minimum_interval_ms": 1000,
        "basis": "KIS 공식 유량 공지(2026-04-20)의 모의투자 초당 1건",
    },
    ("kis", "LIVE"): {
        "requests_per_second": 18.0,
        "provider_max_requests_per_second": 18.0,
        "minimum_interval_ms": 56,
        "basis": "KIS 공식 유량 공지(2026-04-20)의 실전투자 초당 18건",
    },
    ("namuh", "PAPER"): {
        "requests_per_second": 4.0,
        "provider_max_requests_per_second": 5.0,
        "minimum_interval_ms": 250,
        "basis": "NAMUH 공식 SDK 기본 4회/초, 서버 상한 5회/초",
    },
    ("namuh", "LIVE"): {
        "requests_per_second": 4.0,
        "provider_max_requests_per_second": 5.0,
        "minimum_interval_ms": 250,
        "basis": "NAMUH 공식 SDK 기본 4회/초, 서버 상한 5회/초",
    },
}


# WebSocket control traffic has a separate provider limit from REST. Incoming
# market events do not consume this allowance. KIS does not publish a separate
# control-frame rate in its notice, so Router uses a conservative one-message
# per second operational pace in addition to the published hard ceilings.
_WEBSOCKET_RATE_LIMITS: dict[tuple[str, str], dict[str, Any]] = {
    ("kis", "PAPER"): {
        "operating_max_sessions_per_key": 1,
        "provider_max_sessions_per_key": 1,
        "max_subscriptions_per_session": 41,
        "control_messages_per_second": 1.0,
        "provider_max_control_messages_per_second": None,
        "minimum_control_interval_ms": 1000,
        "basis": "KIS 공식 유량 공지(2026-04-20)의 키당 1세션·합산 41구독",
    },
    ("kis", "LIVE"): {
        "operating_max_sessions_per_key": 1,
        "provider_max_sessions_per_key": 1,
        "max_subscriptions_per_session": 41,
        "control_messages_per_second": 1.0,
        "provider_max_control_messages_per_second": None,
        "minimum_control_interval_ms": 1000,
        "basis": "KIS 공식 유량 공지(2026-04-20)의 키당 1세션·합산 41구독",
    },
    ("namuh", "PAPER"): {
        "operating_max_sessions_per_key": 2,
        "provider_max_sessions_per_key": 2,
        "max_subscriptions_per_session": 30,
        "control_messages_per_second": 10.0,
        "provider_max_control_messages_per_second": 10.0,
        "minimum_control_interval_ms": 100,
        "basis": "NAMUH 공식 SDK의 키당 2세션·세션당 30구독·초당 10제어",
    },
    ("namuh", "LIVE"): {
        "operating_max_sessions_per_key": 2,
        "provider_max_sessions_per_key": 2,
        "max_subscriptions_per_session": 30,
        "control_messages_per_second": 10.0,
        "provider_max_control_messages_per_second": 10.0,
        "minimum_control_interval_ms": 100,
        "basis": "NAMUH 공식 SDK의 키당 2세션·세션당 30구독·초당 10제어",
    },
}


def rest_rate_limit_profile(provider: str, target: str) -> dict[str, Any]:
    """Return the immutable operational rate for one broker environment."""

    profile = _REST_RATE_LIMITS.get((provider.strip().lower(), target.strip().upper()))
    if profile is None:
        # Unknown adapters remain deliberately slow until their contract
        # declares an official limit.
        profile = {
            "requests_per_second": 1.0,
            "provider_max_requests_per_second": None,
            "minimum_interval_ms": 1000,
            "basis": "공식 호출 한도 확인 전 안전 기본값",
        }
    return {
        **profile,
        "scope": "API_KEY",
        "scope_label": "API 키별 공용",
    }


def websocket_rate_limit_profile(provider: str, target: str) -> dict[str, Any]:
    """Return immutable WebSocket session and subscription limits."""

    profile = _WEBSOCKET_RATE_LIMITS.get(
        (provider.strip().lower(), target.strip().upper())
    )
    if profile is None:
        profile = {
            "operating_max_sessions_per_key": 1,
            "provider_max_sessions_per_key": None,
            "max_subscriptions_per_session": 1,
            "control_messages_per_second": 1.0,
            "provider_max_control_messages_per_second": None,
            "minimum_control_interval_ms": 1000,
            "basis": "공식 실시간 한도 확인 전 안전 기본값",
        }
    return {
        **profile,
        "scope": "API_KEY",
        "scope_label": "API 키별 공용",
    }
