from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from trade_router import AsyncTradeRouter, Balance, StrategyConfig
from trade_router_studio.app import create_app
from trade_router_studio.sessions import StrategyManifest
from test_studio import workspace


RATE_KEYS = ("buy_commission_rate", "sell_commission_rate", "sell_tax_rate")


@pytest.mark.parametrize("rates", [
    {},
    {"buy_commission_rate": None, "sell_commission_rate": None, "sell_tax_rate": None},
    {"buy_commission_rate": "0", "sell_commission_rate": "0.00015", "sell_tax_rate": "0.0015"},
])
def test_balance_sdk_preserves_fractional_cost_rates_missing_and_explicit_zero(rates):
    async def run():
        def handler(request):
            assert request.url.path == "/api/v1/account/balance"
            assert request.url.params["account"] == "test-account"
            return httpx.Response(200, json={
                "cash": "1000", "withdrawable_cash": "900", "total_value": "1100",
                "as_of": "2026-10-21T12:00:00+09:00", "positions": [], **rates,
            })
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as http:
            router = AsyncTradeRouter(strategy=StrategyConfig(id="cost-test", trading_account="test-account"), http_client=http)
            balance = await router.account.balance()
            for key in RATE_KEYS:
                expected = Decimal(rates[key]) if rates.get(key) is not None else None
                assert getattr(balance, key) == expected
            assert balance.cash == 1000
    asyncio.run(run())


@pytest.mark.parametrize("rates", [
    {},
    {"buy_commission_rate": "", "sell_commission_rate": None, "sell_tax_rate": "0"},
    {"buy_commission_rate": "0.00015", "sell_commission_rate": "0.00015", "sell_tax_rate": "0.0015"},
])
def test_balance_api_reads_only_authorized_account_settings_rates(tmp_path, rates):
    app = create_app(workspace(tmp_path / "workspace"))
    reads = []
    async def balance(account):
        assert account == "test-account"
        return Balance(cash=Decimal("1000"), withdrawable_cash=Decimal("1000"), total_value=Decimal("1000"))
    def cost_rates(account):
        reads.append(account)
        return {**rates, "internal_setting": "not-exposed"}
    app.state.connections.balance = balance
    app.state.connections.account_cost_rates = cost_rates
    with TestClient(app) as client:
        token = app.state.sessions.issue("cost-test.py")
        app.state.sessions.register_manifest("cost-test.py", StrategyManifest(
            strategy_id="cost-test", trading_account="test-account", market_sources={},
            required_capabilities={}, risk_limits={},
        ))
        response = client.get("/api/v1/account/balance", params={"account": "test-account"},
                              headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        payload = response.json()
        for key in RATE_KEYS:
            assert payload[key] == (str(rates[key]) if rates.get(key) not in {None, ""} else None)
        assert "internal_setting" not in payload
        assert "not-exposed" not in response.text
        assert reads == ["test-account"]
        denied = client.get("/api/v1/account/balance", params={"account": "other-account"},
                            headers={"Authorization": f"Bearer {token}"})
        assert denied.status_code == 403
        assert reads == ["test-account"]


@pytest.mark.parametrize("rate", [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")])
def test_balance_rejects_invalid_configured_rates(rate):
    with pytest.raises(ValueError):
        Balance(cash=Decimal("0"), withdrawable_cash=Decimal("0"), total_value=Decimal("0"), sell_tax_rate=rate)


def test_balance_rate_defaults_do_not_claim_zero_costs():
    balance = Balance(cash=Decimal("0"), withdrawable_cash=Decimal("0"), total_value=Decimal("0"))
    assert all(getattr(balance, key) is None for key in RATE_KEYS)
    explicit_zero = Balance(cash=Decimal("0"), withdrawable_cash=Decimal("0"), total_value=Decimal("0"),
                            buy_commission_rate=Decimal("0"), sell_commission_rate=Decimal("0"), sell_tax_rate=Decimal("0"))
    assert all(getattr(explicit_zero, key) == Decimal("0") for key in RATE_KEYS)
