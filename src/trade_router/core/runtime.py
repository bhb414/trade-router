from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

from ..adapters import AdapterContext, AdapterRegistry, ProviderAdapterV1
from ..adapters.utils import redact_text
from ..errors import (
    OrderUncertainError,
    ProviderError,
    RouterError,
    UnsupportedCapabilityError,
)
from ..models import (
    AccountPublic,
    Balance,
    Bar,
    BrokerCapabilities,
    Capability,
    ConditionDefinition,
    CredentialScope,
    CredentialUsage,
    Fill,
    HealthStatus,
    Instrument,
    MarketDataVenue,
    Order,
    OrderBook,
    OrderPreview,
    OrderRequest,
    OrderStatus,
    OrderSubmission,
    Position,
    ProfilePublic,
    ProviderEnvironment,
    Quote,
    RankingItem,
    RiskLimits,
    StrategyConfig,
    StrategyPublic,
    StreamSubscription,
)
from ..version import __version__
from .cache import AsyncSingleFlightCache
from .config import RouterPaths
from .limiter import RateLimiterPool
from .risk import RiskEngine
from .secrets import DpapiSecretStore, SecretStore
from .storage import SQLiteStore
from .subscriptions import SubscriptionClient, SubscriptionHub
from .time import now_kst


def mask_account(number: str) -> str:
    compact = number.strip()
    if len(compact) <= 4:
        return "*" * len(compact)
    return "*" * (len(compact) - 4) + compact[-4:]


def provider_order_key(value: str | None) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        return ""
    return rendered.lstrip("0") or "0"


def reject_secret_settings(value: Any, *, path: str = "settings") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(
                part in normalized
                for part in ("secret", "token", "password", "app_key", "appkey")
            ):
                raise RouterError(
                    "SECRET_IN_PLAINTEXT_SETTINGS",
                    f"Secret-like field '{path}.{key}' must be stored as profile credentials.",
                )
            reject_secret_settings(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_secret_settings(item, path=f"{path}[{index}]")


class TradingRuntime:
    def __init__(
        self,
        *,
        paths: RouterPaths | None = None,
        secret_store: SecretStore | None = None,
        store: SQLiteStore | None = None,
        registry: AdapterRegistry | None = None,
    ) -> None:
        self.paths = paths or RouterPaths.discover()
        self.paths.ensure()
        self.secrets = secret_store or DpapiSecretStore(self.paths.secrets)
        self.store = store or SQLiteStore(self.paths.database)
        self.registry = registry or AdapterRegistry()
        self.limiters = RateLimiterPool()
        self.subscriptions = SubscriptionHub()
        self.read_cache: AsyncSingleFlightCache[Any] = AsyncSingleFlightCache()
        self.risk = RiskEngine(self.store)
        self.adapters: dict[tuple[str, ProviderEnvironment], ProviderAdapterV1] = {}
        self.adapter_locks: dict[tuple[str, ProviderEnvironment], asyncio.Lock] = {}
        self.credential_locks: dict[str, asyncio.Lock] = {}
        self.profile_secrets: dict[str, dict[str, Any]] = {}
        self.started_at = now_kst()
        self.database_ready = False
        self.accepting_orders = False
        self.stopping = False
        self.api_token: str | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._reconciliation_ready = False
        self._reconcile_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.store.open()
        self.database_ready = True
        if isinstance(self.secrets, DpapiSecretStore):
            self.api_token = self.secrets.installation_token()
        else:
            existing = self.secrets.get("installation.api_token")
            if not isinstance(existing, str) or not existing:
                existing = uuid4().hex + uuid4().hex
                self.secrets.set("installation.api_token", existing)
            self.api_token = existing
        self.accepting_orders = False
        await self.reconcile_startup()
        self._maintenance_task = asyncio.create_task(self._maintain_orders())

    async def close(self) -> None:
        self.stopping = True
        self.accepting_orders = False
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._maintenance_task
            self._maintenance_task = None
        await self.subscriptions.close()
        await asyncio.gather(
            *(adapter.close() for adapter in self.adapters.values()),
            return_exceptions=True,
        )
        self.adapters.clear()
        self.profile_secrets.clear()
        await self.limiters.close()
        await self.store.close()
        self.database_ready = False

    async def health(self) -> HealthStatus:
        unresolved = await self.store.unresolved_count() if self.database_ready else 0
        if self.stopping:
            status = "stopping"
        elif not self.database_ready:
            status = "starting"
        elif unresolved or not self.accepting_orders:
            status = "degraded"
        else:
            status = "ready"
        return HealthStatus(
            status=status,
            version=__version__,
            database_ready=self.database_ready,
            unresolved_orders=unresolved,
            accepting_orders=self.accepting_orders,
            started_at=self.started_at,
        )

    async def reconcile_unresolved(self) -> dict[str, int]:
        records = await self.store.unresolved_records()
        reconciled = 0
        checked = 0
        by_account: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            by_account.setdefault(str(record["account_alias"]), []).append(record)
        for account_alias, account_records in by_account.items():
            try:
                provider_orders = await asyncio.wait_for(
                    self.account_orders(account_alias, open_only=False), timeout=12
                )
            except Exception:
                continue
            checked += len(account_records)
            lookup = {
                provider_order_key(item.provider_order_id): item
                for item in provider_orders
                if item.provider_order_id
            }
            for record in account_records:
                if not record.get("order_json"):
                    continue
                old = Order.model_validate_json(record["order_json"])
                match_key = provider_order_key(old.provider_order_id)
                if not match_key or match_key not in lookup:
                    continue
                provider_order = lookup[match_key]
                merged = provider_order.model_copy(
                    update={
                        "router_order_id": old.router_order_id,
                        "client_order_id": old.client_order_id,
                        "strategy_id": old.strategy_id,
                        "account_alias": old.account_alias,
                        "correlation_id": old.correlation_id,
                    }
                )
                await self.store.save_order(merged)
                await self._record_fill_delta(old, merged)
                reconciled += 1
        remaining = await self.store.unresolved_count()
        return {
            "found": len(records),
            "checked": checked,
            "reconciled": reconciled,
            "remaining": remaining,
        }

    async def reconcile_startup(self) -> dict[str, int]:
        async with self._reconcile_lock:
            unresolved = await self.reconcile_unresolved()
            checked_accounts = 0
            failed_accounts = 0
            for account in await self.store.list_accounts():
                if not account.armed:
                    continue
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            self.balance(account.alias),
                            self.positions(account.alias),
                            self.account_orders(account.alias, open_only=False),
                        ),
                        timeout=20,
                    )
                    checked_accounts += 1
                except Exception:
                    failed_accounts += 1
            remaining = await self.store.unresolved_count()
            self._reconciliation_ready = remaining == 0 and failed_accounts == 0
            self.accepting_orders = self._reconciliation_ready and not self.stopping
            return {
                **unresolved,
                "checked_accounts": checked_accounts,
                "failed_accounts": failed_accounts,
                "remaining": remaining,
            }

    async def _maintain_orders(self) -> None:
        while not self.stopping:
            try:
                if not self._reconciliation_ready:
                    await self.reconcile_startup()
                    await asyncio.sleep(5)
                    continue
                accounts = await self.store.list_accounts()
                for account in accounts:
                    local = await self.store.list_orders(
                        account_alias=account.alias, limit=1000
                    )
                    active = [
                        item
                        for item in local
                        if item.status
                        in {
                            OrderStatus.ACKNOWLEDGED,
                            OrderStatus.PARTIALLY_FILLED,
                            OrderStatus.CANCEL_PENDING,
                            OrderStatus.UNKNOWN_RECONCILING,
                        }
                        and item.provider_order_id
                    ]
                    if not active:
                        continue
                    try:
                        provider_orders = await self.account_orders(
                            account.alias, open_only=False
                        )
                    except Exception:
                        continue
                    provider_lookup = {
                        provider_order_key(item.provider_order_id): item
                        for item in provider_orders
                        if item.provider_order_id
                    }
                    for existing in active:
                        provider_order = provider_lookup.get(
                            provider_order_key(existing.provider_order_id)
                        )
                        if provider_order is None:
                            continue
                        merged = provider_order.model_copy(
                            update={
                                "router_order_id": existing.router_order_id,
                                "client_order_id": existing.client_order_id,
                                "strategy_id": existing.strategy_id,
                                "account_alias": existing.account_alias,
                                "correlation_id": existing.correlation_id,
                            }
                        )
                        await self.store.save_order(merged)
                        await self._record_fill_delta(existing, merged)
                if await self.store.unresolved_count() != 0:
                    self._reconciliation_ready = False
                    self.accepting_orders = False
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(5)

    async def capabilities(
        self, provider: str | None = None
    ) -> list[BrokerCapabilities]:
        adapters = self.registry.all()
        if provider:
            adapters = {provider: self.registry.get(provider)}
        return [adapter.capabilities() for _, adapter in sorted(adapters.items())]

    async def connection_states(self) -> list[dict[str, Any]]:
        states: list[dict[str, Any]] = []
        for profile in await self.list_profiles():
            loaded = {
                environment.value: adapter
                for (profile_id, environment), adapter in self.adapters.items()
                if profile_id == profile.id
            }
            states.append(
                {
                    "profile": profile.id,
                    "provider": profile.provider,
                    "usage": profile.usage.value,
                    "loaded_environments": sorted(loaded),
                    "adapter_loaded": bool(loaded),
                    "connection_generation": max(
                        (item.context.connection_generation for item in loaded.values()),
                        default=0,
                    ),
                }
            )
        return states

    async def add_profile(
        self,
        *,
        profile_id: str,
        provider: str,
        usage: CredentialUsage,
        credentials: dict[str, Any],
        display_name: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> ProfilePublic:
        reject_secret_settings(settings or {})
        adapter_type = self.registry.get(provider)
        capabilities = adapter_type.capabilities()
        requested_environments = (
            {ProviderEnvironment.LIVE, ProviderEnvironment.PAPER}
            if usage in {CredentialUsage.AUTO, CredentialUsage.SHARED}
            else {ProviderEnvironment(usage.value)}
        )
        if not requested_environments.issubset(capabilities.environments):
            raise RouterError(
                "ENVIRONMENT_UNSUPPORTED",
                f"Provider '{provider}' does not support credential usage '{usage.value}'.",
            )
        if usage == CredentialUsage.SHARED and capabilities.credential_scope != CredentialScope.PROVIDER:
            raise RouterError(
                "SHARED_CREDENTIAL_UNSUPPORTED",
                f"Provider '{provider}' does not issue credentials shared by live and paper environments.",
            )
        if provider != "simulated" and not credentials.get("app_key"):
            raise RouterError("CREDENTIAL_REQUIRED", "app_key is required.")
        if provider != "simulated" and not credentials.get("app_secret"):
            raise RouterError("CREDENTIAL_REQUIRED", "app_secret is required.")
        profile = await self.store.add_profile(
            profile_id=profile_id,
            provider=provider,
            usage=usage,
            display_name=display_name,
            settings=settings or {},
        )
        self.secrets.set(f"profile:{profile_id}", credentials)
        self.profile_secrets[profile_id] = credentials
        return profile.model_copy(update={"secret_configured": True})

    async def list_profiles(self) -> list[ProfilePublic]:
        profiles = await self.store.list_profiles()
        return [
            profile.model_copy(
                update={
                    "secret_configured": self.secrets.contains(f"profile:{profile.id}")
                }
            )
            for profile in profiles
        ]

    async def get_profile(self, profile_id: str) -> ProfilePublic:
        profile = await self.store.get_profile(profile_id)
        return profile.model_copy(
            update={"secret_configured": self.secrets.contains(f"profile:{profile.id}")}
        )

    async def remove_profile(self, profile_id: str) -> None:
        adapters = [
            self.adapters.pop(key)
            for key in list(self.adapters)
            if key[0] == profile_id
        ]
        for key in [key for key in self.adapter_locks if key[0] == profile_id]:
            self.adapter_locks.pop(key, None)
        self.credential_locks.pop(profile_id, None)
        self.profile_secrets.pop(profile_id, None)
        await asyncio.gather(*(adapter.close() for adapter in adapters), return_exceptions=True)
        await self.store.remove_profile(profile_id)
        self.secrets.delete(f"profile:{profile_id}")

    async def add_account(
        self,
        *,
        alias: str,
        profile_id: str,
        environment: ProviderEnvironment | None,
        account_number: str,
        risk: RiskLimits,
    ) -> AccountPublic:
        profile = await self.get_profile(profile_id)
        resolved_environment = self._resolve_environment(profile, environment)
        account = await self.store.add_account(
            alias=alias,
            profile_id=profile_id,
            environment=resolved_environment,
            masked_account=mask_account(account_number),
            risk=risk,
        )
        self.secrets.set(f"account:{alias}", {"account_number": account_number})
        return account

    async def account_number(self, alias: str) -> str:
        secret = self.secrets.get(f"account:{alias}")
        if not isinstance(secret, dict) or not secret.get("account_number"):
            raise RouterError(
                "ACCOUNT_SECRET_MISSING", f"Account secret for '{alias}' is missing."
            )
        return str(secret["account_number"])

    async def add_strategy(self, config: StrategyConfig) -> StrategyPublic:
        profile = await self.get_profile(config.market_data)
        market_environment = self._resolve_environment(
            profile, config.market_environment
        )
        return await self.store.add_strategy(
            strategy_id=config.id,
            trading_account=config.trading_account,
            market_data=config.market_data,
            market_environment=market_environment,
            risk=config.risk,
        )

    async def set_account_armed(self, alias: str, armed: bool) -> AccountPublic:
        account = await self.store.set_account_armed(alias, armed)
        if armed:
            self._reconciliation_ready = False
            self.accepting_orders = False
            await self.reconcile_startup()
        return account

    async def set_strategy_armed(self, strategy_id: str, armed: bool) -> StrategyPublic:
        strategy = await self.store.set_strategy_armed(strategy_id, armed)
        if armed:
            self._reconciliation_ready = False
            self.accepting_orders = False
            await self.reconcile_startup()
        return strategy

    async def check_strategy_config(self, config: StrategyConfig) -> StrategyPublic:
        saved = await self.store.get_strategy(config.id)
        if (
            saved.trading_account != config.trading_account
            or saved.market_data != config.market_data
            or (
                config.market_environment is not None
                and saved.market_environment != config.market_environment
            )
        ):
            raise RouterError(
                "STRATEGY_CONFIG_MISMATCH",
                "The script metadata does not match the centrally registered strategy.",
                status_code=409,
            )
        return saved

    @staticmethod
    def _resolve_environment(
        profile: ProfilePublic, requested: ProviderEnvironment | None
    ) -> ProviderEnvironment:
        if profile.usage in {CredentialUsage.AUTO, CredentialUsage.SHARED}:
            if requested is None:
                raise RouterError(
                    "ENVIRONMENT_REQUIRED",
                    "This broker connection requires a live or paper account type.",
                    status_code=422,
                )
            return requested
        allowed = ProviderEnvironment(profile.usage.value)
        if requested is not None and requested != allowed:
            raise RouterError(
                "PROFILE_USAGE_MISMATCH",
                f"Profile '{profile.id}' is for '{profile.usage.value}' credentials.",
                status_code=422,
            )
        return allowed

    async def resolve_environment(
        self, profile_id: str, requested: ProviderEnvironment | None = None
    ) -> ProviderEnvironment:
        return self._resolve_environment(await self.get_profile(profile_id), requested)

    async def get_adapter(
        self, profile_id: str, environment: ProviderEnvironment | None = None
    ) -> ProviderAdapterV1:
        profile = await self.get_profile(profile_id)
        resolved_environment = self._resolve_environment(profile, environment)
        key = (profile_id, resolved_environment)
        existing = self.adapters.get(key)
        if existing:
            return existing
        lock = self.adapter_locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self.adapters.get(key)
            if existing:
                return existing
            secret = self.profile_secrets.get(profile_id)
            if secret is None:
                stored_secret = self.secrets.get(f"profile:{profile_id}")
                if isinstance(stored_secret, dict):
                    secret = stored_secret
                    self.profile_secrets[profile_id] = secret
            if profile.provider != "simulated" and not isinstance(secret, dict):
                raise RouterError(
                    "PROFILE_SECRET_MISSING",
                    f"Credentials for '{profile_id}' are missing.",
                )
            adapter_type = self.registry.get(profile.provider)
            adapter = adapter_type(
                AdapterContext(
                    profile=profile,
                    environment=resolved_environment,
                    secrets=secret if isinstance(secret, dict) else {},
                    connection_generation=1,
                    settings=profile.settings,
                    persist_secrets=lambda value, key=f"profile:{profile_id}": (
                        self.secrets.set(key, value)
                    ),
                    limiters=self.limiters,
                    credential_lock=self.credential_locks.setdefault(
                        profile_id, asyncio.Lock()
                    ),
                )
            )
            self.adapters[key] = adapter
            return adapter

    async def check_profile(
        self, profile_id: str, environment: ProviderEnvironment | None = None
    ) -> dict[str, Any]:
        profile = await self.get_profile(profile_id)
        if profile.usage == CredentialUsage.AUTO and environment is None:
            raise RouterError(
                "ENVIRONMENT_REQUIRED",
                "Register an account type before checking this broker connection.",
                status_code=422,
            )
        if profile.usage == CredentialUsage.SHARED and environment is None:
            results = {}
            for item in (ProviderEnvironment.LIVE, ProviderEnvironment.PAPER):
                results[item.value] = await (
                    await self.get_adapter(profile_id, item)
                ).health_check()
            return {"profile_id": profile_id, "environments": results, "ok": True}
        return await (
            await self.get_adapter(profile_id, environment)
        ).health_check()

    async def search_instruments(
        self,
        *,
        profile_id: str,
        query: str,
        limit: int = 20,
        environment: ProviderEnvironment | None = None,
    ) -> list[Instrument]:
        adapter = await self.get_adapter(profile_id, environment)
        key = f"instrument:{profile_id}:{adapter.context.environment.value}:{query}:{limit}"
        return await self.read_cache.get_or_create(
            key, lambda: adapter.search_instruments(query, limit=limit), ttl=30
        )

    async def quote(
        self,
        *,
        profile_id: str,
        code: str,
        venue: MarketDataVenue,
        environment: ProviderEnvironment | None = None,
    ) -> Quote:
        adapter = await self.get_adapter(profile_id, environment)
        key = f"quote:{profile_id}:{adapter.context.environment.value}:{venue.value}:{code}"
        return await self.read_cache.get_or_create(
            key, lambda: adapter.get_quote(code, venue), ttl=0.3
        )

    async def orderbook(
        self,
        *,
        profile_id: str,
        code: str,
        venue: MarketDataVenue,
        environment: ProviderEnvironment | None = None,
    ) -> OrderBook:
        adapter = await self.get_adapter(profile_id, environment)
        key = f"orderbook:{profile_id}:{adapter.context.environment.value}:{venue.value}:{code}"
        return await self.read_cache.get_or_create(
            key, lambda: adapter.get_orderbook(code, venue), ttl=0.3
        )

    async def bars(
        self,
        *,
        profile_id: str,
        code: str,
        interval: str,
        count: int,
        venue: MarketDataVenue,
        until: date | None,
        environment: ProviderEnvironment | None = None,
    ) -> list[Bar]:
        adapter = await self.get_adapter(profile_id, environment)
        if interval not in adapter.capabilities().bar_intervals:
            raise UnsupportedCapabilityError(
                adapter.provider_id, f"market.bars.{interval}"
            )
        key = f"bars:{profile_id}:{adapter.context.environment.value}:{venue.value}:{code}:{interval}:{count}:{until}"
        return await self.read_cache.get_or_create(
            key,
            lambda: adapter.get_bars(
                code, interval=interval, count=count, venue=venue, until=until
            ),
            ttl=3,
        )

    async def account_adapter(
        self, account_alias: str
    ) -> tuple[AccountPublic, ProviderAdapterV1, str]:
        account = await self.store.get_account(account_alias)
        adapter = await self.get_adapter(account.profile_id, account.environment)
        number = await self.account_number(account_alias)
        return account, adapter, number

    async def balance(self, account_alias: str) -> Balance:
        _, adapter, number = await self.account_adapter(account_alias)
        return await adapter.get_balance(account_alias, number)

    async def positions(self, account_alias: str) -> list[Position]:
        _, adapter, number = await self.account_adapter(account_alias)
        return await adapter.get_positions(account_alias, number)

    async def account_orders(
        self, account_alias: str, *, open_only: bool = False
    ) -> list[Order]:
        _, adapter, number = await self.account_adapter(account_alias)
        return await adapter.get_orders(account_alias, number, open_only=open_only)

    async def router_orders(
        self,
        *,
        account_alias: str | None = None,
        strategy_id: str | None = None,
        limit: int = 200,
    ) -> list[Order]:
        return await self.store.list_orders(
            account_alias=account_alias,
            strategy_id=strategy_id,
            limit=min(max(limit, 1), 1000),
        )

    async def fills(
        self,
        *,
        account_alias: str | None = None,
        strategy_id: str | None = None,
        limit: int = 500,
    ) -> list[Fill]:
        return await self.store.list_fills(
            account_alias=account_alias,
            strategy_id=strategy_id,
            limit=min(max(limit, 1), 1000),
        )

    async def preview_order(self, request: OrderRequest) -> OrderPreview:
        strategy = await self.store.get_strategy(request.strategy_id)
        if strategy.trading_account != request.account_alias:
            raise RouterError(
                "ACCOUNT_BINDING_MISMATCH", "Strategy is not bound to this account."
            )
        _account, adapter, number = await self.account_adapter(request.account_alias)
        self._validate_adapter_order(adapter, request)
        return await adapter.preview_order(
            number, self._provider_request(adapter, request)
        )

    async def submit_order(self, request: OrderRequest) -> OrderSubmission:
        strategy = await self.store.get_strategy(request.strategy_id)
        if strategy.trading_account != request.account_alias:
            raise RouterError(
                "ACCOUNT_BINDING_MISMATCH", "Strategy is not bound to this account."
            )
        existing_record = await self.store.get_order_record_by_client(
            request.strategy_id, request.client_order_id
        )
        if existing_record:
            existing = await self.store.get_order_by_client(
                request.strategy_id, request.client_order_id
            )
            if existing:
                return OrderSubmission(
                    accepted=existing.status != OrderStatus.REJECTED,
                    order=existing,
                    replayed=True,
                )
            raise RouterError(
                "ORDER_ALREADY_SUBMITTING",
                "This client_order_id is already reserved and requires reconciliation.",
                status_code=409,
            )
        if not self.accepting_orders:
            raise RouterError(
                "ORDER_GATE_CLOSED",
                "New orders are disabled until unresolved orders are reconciled.",
                status_code=409,
            )
        account, adapter, number = await self.account_adapter(request.account_alias)
        if account.environment == ProviderEnvironment.LIVE and (
            not account.armed or not strategy.armed
        ):
            raise RouterError(
                "LIVE_NOT_ARMED",
                "Live orders require both the account and strategy to be armed.",
            )
        # Paper and simulated environments also honor explicit arming so test behavior matches live behavior.
        if not account.armed or not strategy.armed:
            raise RouterError(
                "ORDER_NOT_ARMED", "Both the account and strategy must be armed."
            )
        self._validate_adapter_order(adapter, request)
        if await self.store.has_symbol_lock(
            request.account_alias, request.instrument.code
        ):
            raise RouterError(
                "SYMBOL_ORDER_LOCKED",
                "An unresolved or open order already exists for this account and instrument.",
                status_code=409,
            )
        provider_request = self._provider_request(adapter, request)
        preview = await adapter.preview_order(number, provider_request)
        positions = await adapter.get_positions(request.account_alias, number)
        if account.risk.max_exposure_krw is not None and any(
            position.market_value is None for position in positions
        ):
            raise RouterError(
                "ACCOUNT_EXPOSURE_UNKNOWN",
                "Account exposure cannot be verified because a position has no market value.",
            )
        positions_value = sum(
            (position.market_value or Decimal(0) for position in positions), Decimal(0)
        )
        strategy_exposure = await self.store.strategy_exposure(request.strategy_id)
        reserved = await self.risk.validate(
            request=request,
            preview=preview,
            account_risk=account.risk,
            strategy_risk=strategy.risk,
            account_positions_value=positions_value,
            strategy_positions_value=strategy_exposure,
        )
        router_order_id = uuid4().hex
        if not await self.store.reserve_order(
            router_order_id=router_order_id,
            request=request,
            reserved_notional=reserved,
        ):
            existing = await self.store.get_order_by_client(
                request.strategy_id, request.client_order_id
            )
            if existing:
                return OrderSubmission(accepted=True, order=existing, replayed=True)
            raise RouterError(
                "ORDER_IDEMPOTENCY_CONFLICT",
                "Order key was reserved concurrently.",
                status_code=409,
            )
        try:
            order = await adapter.submit_order(
                number, provider_request, router_order_id=router_order_id
            )
        except OrderUncertainError as exc:
            now = now_kst()
            order = Order(
                router_order_id=router_order_id,
                client_order_id=request.client_order_id,
                strategy_id=request.strategy_id,
                account_alias=request.account_alias,
                provider=adapter.provider_id,
                instrument=request.instrument,
                side=request.side,
                order_type=request.order_type,
                route=request.route,
                time_in_force=request.time_in_force,
                quantity=request.quantity,
                price=request.price,
                status=OrderStatus.UNKNOWN_RECONCILING,
                submitted_at=now,
                updated_at=now,
                message=exc.message,
                correlation_id=exc.correlation_id or uuid4().hex,
            )
            await self.store.save_order(order)
            self.accepting_orders = False
            return OrderSubmission(
                accepted=False,
                order=order,
                block_code="ORDER_SUBMISSION_UNCERTAIN",
                block_reason=exc.message,
            )
        except Exception as exc:
            now = now_kst()
            safe_reason = redact_text(str(exc), number, adapter.context.secrets)
            order = Order(
                router_order_id=router_order_id,
                client_order_id=request.client_order_id,
                strategy_id=request.strategy_id,
                account_alias=request.account_alias,
                provider=adapter.provider_id,
                instrument=request.instrument,
                side=request.side,
                order_type=request.order_type,
                route=request.route,
                time_in_force=request.time_in_force,
                quantity=request.quantity,
                price=request.price,
                status=OrderStatus.REJECTED,
                submitted_at=now,
                updated_at=now,
                message=safe_reason,
                correlation_id=getattr(exc, "correlation_id", None) or uuid4().hex,
            )
            await self.store.save_order(order, reserved_notional=Decimal(0))
            if isinstance(exc, RouterError):
                raise
            raise ProviderError(
                "ORDER_REJECTED",
                "The provider rejected the order before acknowledgement.",
                provider=adapter.provider_id,
                details={"reason": safe_reason[:300]},
            ) from exc
        await self.store.save_order(order)
        await self._record_fill_delta(None, order)
        return OrderSubmission(
            accepted=order.status != OrderStatus.REJECTED, order=order
        )

    async def _record_fill_delta(self, previous: Order | None, current: Order) -> None:
        previous_quantity = previous.filled_quantity if previous else 0
        delta = current.filled_quantity - previous_quantity
        if delta <= 0 or current.average_fill_price is None:
            return
        fill = Fill(
            fill_id=f"{current.router_order_id}:{current.filled_quantity}",
            router_order_id=current.router_order_id,
            provider_fill_id=None,
            instrument=current.instrument,
            side=current.side,
            quantity=delta,
            price=current.average_fill_price,
            filled_at=current.updated_at,
        )
        await self.store.save_fill(fill)

    async def cancel_order(self, router_order_id: str) -> Order:
        order = await self.store.get_order(router_order_id)
        account, adapter, number = await self.account_adapter(order.account_alias)
        adapter.require(Capability.ORDER_CANCEL)
        strategy = await self.store.get_strategy(order.strategy_id)
        if not account.armed or not strategy.armed:
            raise RouterError(
                "ORDER_NOT_ARMED", "Both the account and strategy must be armed."
            )
        pending = order.model_copy(
            update={"status": OrderStatus.CANCEL_PENDING, "updated_at": now_kst()}
        )
        await self.store.save_order(pending)
        try:
            updated = await adapter.cancel_order(number, order)
        except OrderUncertainError as exc:
            uncertain = pending.model_copy(
                update={
                    "status": OrderStatus.UNKNOWN_RECONCILING,
                    "message": exc.message,
                    "updated_at": now_kst(),
                }
            )
            await self.store.save_order(uncertain)
            self.accepting_orders = False
            return uncertain
        except Exception:
            await self.store.save_order(
                order.model_copy(update={"updated_at": now_kst()})
            )
            raise
        await self.store.save_order(updated)
        return updated

    async def amend_order(
        self,
        router_order_id: str,
        *,
        quantity: int | None = None,
        price: Decimal | None = None,
    ) -> Order:
        order = await self.store.get_order(router_order_id)
        account, adapter, number = await self.account_adapter(order.account_alias)
        adapter.require(Capability.ORDER_AMEND)
        strategy = await self.store.get_strategy(order.strategy_id)
        if not account.armed or not strategy.armed:
            raise RouterError(
                "ORDER_NOT_ARMED", "Both the account and strategy must be armed."
            )
        if quantity is None and price is None:
            raise RouterError(
                "AMENDMENT_EMPTY", "An amended quantity or price is required."
            )
        if quantity is not None and quantity <= 0:
            raise RouterError("AMENDMENT_INVALID", "Amended quantity must be positive.")
        try:
            updated = await adapter.amend_order(
                number,
                order,
                quantity=quantity,
                price=str(price) if price is not None else None,
            )
        except OrderUncertainError as exc:
            uncertain = order.model_copy(
                update={
                    "status": OrderStatus.UNKNOWN_RECONCILING,
                    "message": exc.message,
                    "updated_at": now_kst(),
                }
            )
            await self.store.save_order(uncertain)
            self.accepting_orders = False
            return uncertain
        await self.store.save_order(updated)
        return updated

    def _validate_adapter_order(
        self, adapter: ProviderAdapterV1, request: OrderRequest
    ) -> None:
        capabilities = adapter.capabilities()
        feature = (
            Capability.ORDER_MARKET
            if request.order_type.value == "MARKET"
            else Capability.ORDER_LIMIT
        )
        if not capabilities.supports(feature):
            raise UnsupportedCapabilityError(adapter.provider_id, feature.value)
        if request.route not in capabilities.order_routes:
            raise UnsupportedCapabilityError(
                adapter.provider_id, f"order.route.{request.route.value}"
            )
        options = request.provider_options
        if options and set(options) - {adapter.provider_id}:
            raise RouterError(
                "PROVIDER_OPTIONS_MISMATCH",
                f"Only the '{adapter.provider_id}' provider option namespace is allowed.",
            )
        values = options.get(adapter.provider_id, {}) if options else {}
        if values:
            if not isinstance(values, dict):
                raise RouterError(
                    "PROVIDER_OPTIONS_INVALID", "Provider options must be an object."
                )
            schema = capabilities.provider_option_schema
            unknown = set(values) - set(schema)
            if unknown:
                raise RouterError(
                    "PROVIDER_OPTIONS_INVALID",
                    f"Unknown provider options: {', '.join(sorted(unknown))}.",
                )
            for key, value in values.items():
                rule = schema.get(key, {})
                expected = rule.get("type")
                if expected == "string" and not isinstance(value, str):
                    raise RouterError(
                        "PROVIDER_OPTIONS_INVALID",
                        f"Provider option '{key}' must be a string.",
                    )
                if expected == "boolean" and not isinstance(value, bool):
                    raise RouterError(
                        "PROVIDER_OPTIONS_INVALID",
                        f"Provider option '{key}' must be a boolean.",
                    )
                if "enum" in rule and value not in rule["enum"]:
                    raise RouterError(
                        "PROVIDER_OPTIONS_INVALID",
                        f"Provider option '{key}' has an unsupported value.",
                    )

    @staticmethod
    def _provider_request(
        adapter: ProviderAdapterV1, request: OrderRequest
    ) -> OrderRequest:
        namespaced = (
            request.provider_options.get(adapter.provider_id, {})
            if request.provider_options
            else {}
        )
        if namespaced and not isinstance(namespaced, dict):
            raise RouterError(
                "PROVIDER_OPTIONS_INVALID", "Provider options must be an object."
            )
        return request.model_copy(update={"provider_options": namespaced})

    async def subscribe(
        self,
        profile_id: str,
        subscriptions: list[StreamSubscription],
        environment: ProviderEnvironment | None = None,
    ) -> SubscriptionClient:
        adapter = await self.get_adapter(profile_id, environment)
        key = f"{profile_id}:{adapter.context.environment.value}"
        return self.subscriptions.subscribe(key, adapter, subscriptions)

    def unsubscribe(
        self,
        profile_id: str,
        client_id: str,
        environment: ProviderEnvironment,
    ) -> None:
        self.subscriptions.unsubscribe(f"{profile_id}:{environment.value}", client_id)

    async def rankings(
        self,
        *,
        profile_id: str,
        ranking: str,
        market: str = "ALL",
        limit: int = 20,
        environment: ProviderEnvironment | None = None,
    ) -> list[RankingItem]:
        adapter = await self.get_adapter(profile_id, environment)
        adapter.require(Capability.DISCOVERY_RANKINGS)
        return await adapter.rankings(ranking, market=market, limit=limit)

    async def conditions(
        self, *, profile_id: str, environment: ProviderEnvironment | None = None
    ) -> list[ConditionDefinition]:
        adapter = await self.get_adapter(profile_id, environment)
        adapter.require(Capability.DISCOVERY_CONDITIONS)
        return await adapter.list_conditions()

    async def run_condition(
        self,
        *,
        profile_id: str,
        condition_id: str,
        environment: ProviderEnvironment | None = None,
    ) -> list[Instrument]:
        adapter = await self.get_adapter(profile_id, environment)
        adapter.require(Capability.DISCOVERY_CONDITIONS)
        return await adapter.run_condition(condition_id)
