from __future__ import annotations

import httpx

from trade_router.server.app import create_app


async def test_api_requires_installation_token(runtime):
    app = create_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/system/health")).status_code == 401
        response = await client.get(
            "/v1/system/health",
            headers={"authorization": f"Bearer {runtime.api_token}"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ready"


async def test_fake_quote_and_capabilities(runtime):
    app = create_app(runtime)
    headers = {"authorization": f"Bearer {runtime.api_token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        quote = await client.get(
            "/v1/market/quote",
            params={"profile": "fake-main", "code": "005930", "venue": "INTEGRATED"},
            headers=headers,
        )
        assert quote.status_code == 200
        assert quote.json()["source"]["provider"] == "fake"
        caps = await client.get("/v1/system/capabilities", headers=headers)
        assert {item["provider"] for item in caps.json()} == {"fake", "kis", "namuh"}
