from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from trade_router import AsyncTradeRouter, Balance, StrategyConfig
from trade_router_studio.app import create_app
from trade_router_studio.connections import ConnectionRuntime
from trade_router_studio.orders import OrderService
from trade_router_studio.sessions import StrategyManifest
from trade_router_studio.settings import SettingsError
from test_studio import workspace


RATE_KEYS = ("buy_commission_rate", "sell_commission_rate", "sell_tax_rate")


def configured_cost_reader(rates):
    runtime = SimpleNamespace(_settings=SimpleNamespace(
        get_account=lambda account: {"cost_rates": rates},
    ))
    return lambda account: ConnectionRuntime.account_cost_rates(runtime, account)


@pytest.mark.parametrize("rates,expected", [
    ({}, ("0", "0", "0")),
    (None, ("0", "0", "0")),
    ({"buy_commission_rate": None, "sell_commission_rate": "", "sell_tax_rate": " \t"}, ("0", "0", "0")),
    ({"buy_commission_rate": "0.00003", "sell_tax_rate": "0.002"}, ("0.00003", "0", "0.002")),
    ({"buy_commission_rate": "0", "sell_commission_rate": "0.00015", "sell_tax_rate": "0.0015"}, ("0", "0.00015", "0.0015")),
])
def test_account_cost_reader_defaults_only_blank_items_to_zero(rates, expected):
    before = dict(rates) if rates is not None else None
    result = configured_cost_reader(rates)("test-account")
    assert result == dict(zip(RATE_KEYS, expected))
    assert rates == before


@pytest.mark.parametrize("invalid", ["invalid", "NaN", "Infinity", "-0.01", "1.2"])
def test_account_cost_reader_does_not_mask_invalid_explicit_rates(invalid):
    result = configured_cost_reader({"sell_tax_rate": invalid})("test-account")
    assert result["sell_tax_rate"] == invalid
    assert result["buy_commission_rate"] == "0"


def test_unregistered_account_still_raises_instead_of_receiving_zero_rates():
    def missing_account(account):
        raise SettingsError("거래 계좌를 찾을 수 없습니다.")
    runtime = SimpleNamespace(_settings=SimpleNamespace(get_account=missing_account))
    with pytest.raises(SettingsError, match="거래 계좌를 찾을 수 없습니다"):
        ConnectionRuntime.account_cost_rates(runtime, "unregistered-account")


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
    {"buy_commission_rate": " \t", "sell_commission_rate": "0.00003", "sell_tax_rate": "0.002"},
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
        return configured_cost_reader({**rates, "internal_setting": "not-exposed"})(account)
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
            assert payload[key] == (str(rates[key]) if rates.get(key) is not None and str(rates[key]).strip() else "0")
        assert "internal_setting" not in payload
        assert "not-exposed" not in response.text
        assert reads == ["test-account"]
        denied = client.get("/api/v1/account/balance", params={"account": "other-account"},
                            headers={"Authorization": f"Bearer {token}"})
        assert denied.status_code == 403
        assert reads == ["test-account"]


@pytest.mark.parametrize("side,rates,expected_fee,expected_tax", [
    ("BUY", {}, "0", "0"),
    ("SELL", {"buy_commission_rate": None, "sell_commission_rate": " ", "sell_tax_rate": ""}, "0", "0"),
    ("SELL", {"sell_tax_rate": "0.002"}, "0", "20"),
    ("SELL", {"sell_commission_rate": "0.00003", "sell_tax_rate": "0.002"}, "0.3", "20"),
])
def test_fill_cost_estimate_uses_same_account_blank_zero_policy(side, rates, expected_fee, expected_tax):
    recorded = []
    def record_fill(**values):
        recorded.append(values)
        return values
    service = SimpleNamespace(
        _gateway=SimpleNamespace(account_cost_rates=configured_cost_reader(rates)),
        _store=SimpleNamespace(record_fill=record_fill),
    )
    OrderService._record_fill_delta(
        service, {"id": "test-fill", "account_alias": "test-account", "request": {"side": side}},
        previous_quantity=Decimal("0"), previous_average=Decimal("0"),
        cumulative_quantity=Decimal("10"), cumulative_average=Decimal("1000"),
    )
    assert recorded[0]["commission"] == Decimal(expected_fee)
    assert recorded[0]["tax"] == Decimal(expected_tax)
    assert recorded[0]["costs_complete"] is True


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
