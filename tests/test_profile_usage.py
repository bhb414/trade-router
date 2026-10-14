from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest
import respx

from trade_router.core.config import RouterPaths
from trade_router.core.runtime import TradingRuntime
from trade_router.core.secrets import MemorySecretStore
from trade_router.core.storage import SQLiteStore
from trade_router.errors import RouterError
from trade_router.models import CredentialUsage, ProviderEnvironment, RiskLimits


@respx.mock
async def test_shared_credentials_are_one_profile_with_explicit_request_environment(
    paths: RouterPaths,
):
    runtime = TradingRuntime(paths=paths, secret_store=MemorySecretStore())
    await runtime.start()
    try:
        profile = await runtime.add_profile(
            profile_id="nh-main",
            provider="namuh",
            usage=CredentialUsage.SHARED,
            credentials={"app_key": "key", "app_secret": "secret"},
        )
        assert profile.usage == CredentialUsage.SHARED

        with pytest.raises(RouterError) as missing:
            await runtime.get_adapter("nh-main")
        assert missing.value.code == "ENVIRONMENT_REQUIRED"

        account = await runtime.add_account(
            alias="nh-paper",
            profile_id="nh-main",
            environment=ProviderEnvironment.PAPER,
            account_number="03-0001",
            risk=RiskLimits(),
        )
        assert account.environment == ProviderEnvironment.PAPER

        adapter = await runtime.get_adapter("nh-main", ProviderEnvironment.PAPER)
        assert adapter.base_url == "https://moapi.nhplug.com:8443"
        assert adapter.auth_url == "https://api.nhplug.com:8443"

        token = respx.post("https://api.nhplug.com:8443/oauth2/token").mock(
            return_value=httpx.Response(
                200, json={"access_token": "access", "expires_in": 86400}
            )
        )
        live_adapter = await runtime.get_adapter(
            "nh-main", ProviderEnvironment.LIVE
        )
        await asyncio.gather(adapter.health_check(), live_adapter.health_check())
        assert token.call_count == 1
    finally:
        await runtime.close()


async def test_v1_environment_columns_migrate_to_usage_and_request_environment(
    paths: RouterPaths,
):
    timestamp = "2026-10-14T12:00:00+09:00"
    with sqlite3.connect(paths.database) as connection:
        connection.executescript(
            """
            CREATE TABLE profiles (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, environment TEXT NOT NULL,
                display_name TEXT, settings_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE accounts (
                alias TEXT PRIMARY KEY, profile_id TEXT NOT NULL, masked_account TEXT NOT NULL,
                armed INTEGER NOT NULL, risk_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE strategies (
                id TEXT PRIMARY KEY, trading_account TEXT NOT NULL, market_data TEXT NOT NULL,
                armed INTEGER NOT NULL, risk_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO profiles VALUES(?,?,?,?,?,?,?)",
            ("kis-live", "kis", "live", None, "{}", timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO accounts VALUES(?,?,?,?,?,?,?)",
            ("main", "kis-live", "******-01", 0, "{}", timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO strategies VALUES(?,?,?,?,?,?,?)",
            ("sample", "main", "kis-live", 0, "{}", timestamp, timestamp),
        )

    store = SQLiteStore(paths.database)
    await store.open()
    try:
        assert (await store.get_profile("kis-live")).usage == CredentialUsage.LIVE
        assert (await store.get_account("main")).environment == ProviderEnvironment.LIVE
        assert (
            await store.get_strategy("sample")
        ).market_environment == ProviderEnvironment.LIVE
    finally:
        await store.close()

