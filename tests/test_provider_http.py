from __future__ import annotations

import json

import httpx
import respx

from trade_router.adapters.base import AdapterContext
from trade_router.adapters.kis import KisAdapter
from trade_router.adapters.namuh import NamuhAdapter
from trade_router.core.time import now_kst
from trade_router.models import (
    CredentialScope,
    MarketDataVenue,
    ProfilePublic,
    ProviderEnvironment,
)


def context(
    provider: str, environment: ProviderEnvironment, saved: list[dict]
) -> AdapterContext:
    now = now_kst()
    return AdapterContext(
        profile=ProfilePublic(
            id=f"{provider}-test",
            provider=provider,
            environment=environment,
            created_at=now,
            updated_at=now,
        ),
        secrets={"app_key": "test-key", "app_secret": "test-secret"},
        persist_secrets=lambda value: saved.append(dict(value)),
    )


@respx.mock
async def test_kis_reuses_token_and_normalizes_quote():
    saved: list[dict] = []
    token = respx.post("https://openapi.koreainvestment.com:9443/oauth2/tokenP").mock(
        return_value=httpx.Response(
            200, json={"access_token": "access", "expires_in": 86400}
        )
    )
    quote = respx.get(
        "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-price"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output": {
                    "stck_prpr": "70000",
                    "stck_oprc": "69000",
                    "stck_hgpr": "71000",
                    "stck_lwpr": "68000",
                    "stck_mxpr": "91000",
                    "acml_vol": "123",
                },
            },
        )
    )
    adapter = KisAdapter(context("kis", ProviderEnvironment.LIVE, saved))
    try:
        first = await adapter.get_quote("005930", MarketDataVenue.KRX)
        second = await adapter.get_quote("005930", MarketDataVenue.KRX)
    finally:
        await adapter.close()
    assert first.last == second.last == 70000
    assert first.source.provider == "kis"
    assert token.call_count == 1
    assert quote.call_count == 2
    assert saved and saved[-1]["access_token"] == "access"


@respx.mock
async def test_namuh_paper_uses_live_oauth_and_mock_rest():
    saved: list[dict] = []
    token = respx.post("https://api.nhplug.com:8443/oauth2/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "access", "expires_in": 86400}
        )
    )
    quote = respx.post(
        "https://moapi.nhplug.com:8443/krstock/quote/v1/currentPrice"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "Output_0": {
                    "stck_prpr": 70000,
                    "iem_nm": "삼성전자",
                    "askp": 70100,
                    "bidp": 69900,
                    "acml_vol": 10,
                }
            },
        )
    )
    adapter = NamuhAdapter(context("namuh", ProviderEnvironment.PAPER, saved))
    try:
        result = await adapter.get_quote("005930", MarketDataVenue.INTEGRATED)
    finally:
        await adapter.close()
    assert result.last == 70000
    assert token.call_count == 1
    assert quote.call_count == 1
    request = quote.calls[0].request
    assert request.headers["x-client-id"] == "test-key"
    assert json.loads(request.content) == {
        "Input_0": {"iem_cd": "005930", "market_cd": "UNT"}
    }
    capabilities = NamuhAdapter.capabilities()
    assert capabilities.environments == {
        ProviderEnvironment.LIVE,
        ProviderEnvironment.PAPER,
    }
    assert capabilities.credential_scope == CredentialScope.PROVIDER
