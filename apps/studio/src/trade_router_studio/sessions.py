from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class StrategySession:
    strategy_name: str
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class StrategyManifest:
    strategy_id: str
    trading_account: str
    market_sources: dict[str, str]
    required_capabilities: dict[str, tuple[str, ...]]
    risk_limits: dict[str, str | int | None]


class StrategySessionRegistry:
    """Issues process-local credentials without exposing broker secrets."""

    def __init__(self) -> None:
        self._sessions: dict[str, StrategySession] = {}
        self._strategy_tokens: dict[str, set[str]] = {}
        self._manifests: dict[str, StrategyManifest] = {}
        self._lock = threading.RLock()

    def issue(self, strategy_name: str) -> str:
        token = secrets.token_urlsafe(32)
        digest = _digest(token)
        with self._lock:
            self.revoke_strategy(strategy_name)
            self._sessions[digest] = StrategySession(
                strategy_name=strategy_name,
                issued_at=datetime.now().astimezone(),
            )
            self._strategy_tokens.setdefault(strategy_name, set()).add(digest)
        return token

    def authenticate(self, token: str) -> StrategySession | None:
        if not token:
            return None
        digest = _digest(token)
        with self._lock:
            session = self._sessions.get(digest)
            if session is None:
                return None
            if not hmac.compare_digest(digest, _digest(token)):
                return None
            return session

    def revoke_strategy(self, strategy_name: str) -> None:
        with self._lock:
            for digest in self._strategy_tokens.pop(strategy_name, set()):
                self._sessions.pop(digest, None)
            self._manifests.pop(strategy_name, None)

    def register_manifest(
        self,
        strategy_name: str,
        manifest: StrategyManifest,
    ) -> None:
        with self._lock:
            duplicate = next(
                (
                    name
                    for name, existing in self._manifests.items()
                    if name != strategy_name
                    and existing.strategy_id.casefold() == manifest.strategy_id.casefold()
                ),
                None,
            )
            if duplicate is not None:
                raise ValueError(
                    f"Strategy id '{manifest.strategy_id}' is already used by {duplicate}."
                )
            self._manifests[strategy_name] = manifest

    def manifest(self, strategy_name: str) -> StrategyManifest | None:
        with self._lock:
            return self._manifests.get(strategy_name)

    def strategy_names(self, strategy_id: str) -> list[str]:
        normalized = strategy_id.casefold()
        with self._lock:
            return [
                name
                for name, manifest in self._manifests.items()
                if manifest.strategy_id.casefold() == normalized
            ]

    def authorize_source(
        self,
        strategy_name: str,
        *,
        source: str,
        source_alias: str | None,
    ) -> bool:
        manifest = self.manifest(strategy_name)
        if manifest is None:
            return False
        if source_alias is not None:
            return manifest.market_sources.get(source_alias) == source
        return source in manifest.market_sources.values()

    def authorize_account(self, strategy_name: str, account: str) -> bool:
        manifest = self.manifest(strategy_name)
        return manifest is not None and manifest.trading_account == account


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
