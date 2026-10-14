from __future__ import annotations

from pathlib import Path

import pytest

from trade_router.core.config import RouterPaths
from trade_router.core.runtime import TradingRuntime
from trade_router.core.secrets import MemorySecretStore
from trade_router.models import ProviderEnvironment, RiskLimits, StrategyConfig


@pytest.fixture
def paths(tmp_path: Path) -> RouterPaths:
    return RouterPaths(
        home=tmp_path,
        database=tmp_path / "router.db",
        secrets=tmp_path / "secrets.dpapi",
        pid=tmp_path / "daemon.pid",
        daemon_log=tmp_path / "daemon.log",
    )


@pytest.fixture
async def runtime(paths: RouterPaths):
    secrets = MemorySecretStore()
    active = TradingRuntime(paths=paths, secret_store=secrets)
    await active.start()
    await active.add_profile(
        profile_id="simulated-main",
        provider="simulated",
        environment=ProviderEnvironment.PAPER,
        credentials={},
        settings={"cash": 10_000_000},
    )
    await active.add_account(
        alias="paper-main",
        profile_id="simulated-main",
        account_number="PAPER-001",
        risk=RiskLimits(
            max_order_krw=20_000_000, max_daily_buy_krw=50_000_000, max_open_orders=5
        ),
    )
    await active.add_strategy(
        StrategyConfig(
            id="sample",
            trading_account="paper-main",
            market_data="simulated-main",
            risk=RiskLimits(
                max_order_krw=500_000, max_daily_buy_krw=1_000_000, max_open_orders=3
            ),
        )
    )
    await active.set_account_armed("paper-main", True)
    await active.set_strategy_armed("sample", True)
    try:
        yield active
    finally:
        await active.close()
