from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    Header,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse

from ..core.runtime import TradingRuntime
from ..errors import RouterError
from ..models import MarketDataVenue, OrderRequest, StreamSubscription
from ..version import __version__
from .schemas import (
    AccountCreate,
    AmendRequest,
    ProfileCreate,
    StrategyCreate,
    StreamRequest,
)


def create_app(runtime: TradingRuntime | None = None) -> FastAPI:
    owned_runtime = runtime is None
    active = runtime or TradingRuntime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not active.database_ready:
            await active.start()
        app.state.runtime = active
        try:
            yield
        finally:
            if owned_runtime:
                await active.close()

    app = FastAPI(title="Trade Router", version=__version__, lifespan=lifespan)

    @app.exception_handler(RouterError)
    async def router_error_handler(_: Request, exc: RouterError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content=exc.as_body().model_dump(mode="json")
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        # Never reflect arbitrary exception text; it may contain provider material.
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "The router encountered an internal error.",
                "retryable": False,
                "details": {},
            },
        )

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = active.api_token or ""
        supplied = (
            authorization.removeprefix("Bearer ").strip() if authorization else ""
        )
        if not expected or not secrets.compare_digest(supplied, expected):
            raise RouterError(
                "LOCAL_AUTH_REQUIRED",
                "A valid installation token is required.",
                status_code=401,
            )

    secured = [Depends(require_token)]

    @app.get("/v1/system/health", dependencies=secured)
    async def health():
        return await active.health()

    @app.get("/v1/system/capabilities", dependencies=secured)
    async def capabilities(provider: str | None = None):
        return await active.capabilities(provider)

    @app.get("/v1/system/connections", dependencies=secured)
    async def connections():
        return await active.connection_states()

    @app.get("/v1/system/rate-limits", dependencies=secured)
    async def rate_limits():
        return active.limiters.snapshot()

    @app.post("/v1/system/shutdown", dependencies=secured)
    async def shutdown(request: Request):
        server = getattr(request.app.state, "server", None)
        if server is not None:
            asyncio.get_running_loop().call_soon(setattr, server, "should_exit", True)
        return {"stopping": True}

    @app.post("/v1/system/reconcile", dependencies=secured)
    async def reconcile():
        return await active.reconcile_startup()

    @app.get("/v1/profiles", dependencies=secured)
    async def list_profiles():
        return await active.list_profiles()

    @app.post("/v1/profiles", dependencies=secured)
    async def add_profile(body: ProfileCreate):
        credentials = {}
        if body.app_key is not None:
            credentials["app_key"] = body.app_key
        if body.app_secret is not None:
            credentials["app_secret"] = body.app_secret
        return await active.add_profile(
            profile_id=body.id,
            provider=body.provider,
            environment=body.environment,
            credentials=credentials,
            display_name=body.display_name,
            settings=body.settings,
        )

    @app.delete("/v1/profiles/{profile_id}", status_code=204, dependencies=secured)
    async def remove_profile(profile_id: str):
        await active.remove_profile(profile_id)
        return None

    @app.post("/v1/profiles/{profile_id}/check", dependencies=secured)
    async def check_profile(profile_id: str):
        return await active.check_profile(profile_id)

    @app.get("/v1/accounts", dependencies=secured)
    async def list_accounts():
        return await active.store.list_accounts()

    @app.post("/v1/accounts", dependencies=secured)
    async def add_account(body: AccountCreate):
        return await active.add_account(
            alias=body.alias,
            profile_id=body.profile_id,
            account_number=body.account_number,
            risk=body.risk,
        )

    @app.post("/v1/accounts/{alias}/arm", dependencies=secured)
    async def arm_account(alias: str):
        return await active.set_account_armed(alias, True)

    @app.post("/v1/accounts/{alias}/disarm", dependencies=secured)
    async def disarm_account(alias: str):
        return await active.set_account_armed(alias, False)

    @app.get("/v1/accounts/{alias}/balance", dependencies=secured)
    async def balance(alias: str):
        return await active.balance(alias)

    @app.get("/v1/accounts/{alias}/positions", dependencies=secured)
    async def positions(alias: str):
        return await active.positions(alias)

    @app.get("/v1/accounts/{alias}/orders", dependencies=secured)
    async def account_orders(
        alias: str, source: str = "router", open_only: bool = False
    ):
        if source == "provider":
            return await active.account_orders(alias, open_only=open_only)
        return await active.router_orders(account_alias=alias)

    @app.get("/v1/accounts/{alias}/fills", dependencies=secured)
    async def account_fills(alias: str, limit: int = Query(500, ge=1, le=1000)):
        return await active.fills(account_alias=alias, limit=limit)

    @app.get("/v1/strategies", dependencies=secured)
    async def list_strategies():
        return await active.store.list_strategies()

    @app.post("/v1/strategies", dependencies=secured)
    async def add_strategy(body: StrategyCreate):
        return await active.add_strategy(body.config)

    @app.get("/v1/strategies/{strategy_id}", dependencies=secured)
    async def get_strategy(strategy_id: str):
        return await active.store.get_strategy(strategy_id)

    @app.post("/v1/strategies/{strategy_id}/arm", dependencies=secured)
    async def arm_strategy(strategy_id: str):
        return await active.set_strategy_armed(strategy_id, True)

    @app.post("/v1/strategies/{strategy_id}/disarm", dependencies=secured)
    async def disarm_strategy(strategy_id: str):
        return await active.set_strategy_armed(strategy_id, False)

    @app.get("/v1/instruments/search", dependencies=secured)
    async def search_instruments(
        profile: str, q: str, limit: int = Query(20, ge=1, le=100)
    ):
        return await active.search_instruments(profile_id=profile, query=q, limit=limit)

    @app.get("/v1/market/quote", dependencies=secured)
    async def quote(
        profile: str, code: str, venue: MarketDataVenue = MarketDataVenue.INTEGRATED
    ):
        return await active.quote(profile_id=profile, code=code, venue=venue)

    @app.get("/v1/market/orderbook", dependencies=secured)
    async def orderbook(
        profile: str, code: str, venue: MarketDataVenue = MarketDataVenue.INTEGRATED
    ):
        return await active.orderbook(profile_id=profile, code=code, venue=venue)

    @app.get("/v1/market/bars", dependencies=secured)
    async def bars(
        profile: str,
        code: str,
        interval: str,
        count: int = Query(100, ge=1, le=1000),
        venue: MarketDataVenue = MarketDataVenue.INTEGRATED,
        until: date | None = None,
    ):
        return await active.bars(
            profile_id=profile,
            code=code,
            interval=interval,
            count=count,
            venue=venue,
            until=until,
        )

    @app.post("/v1/orders/preview", dependencies=secured)
    async def preview_order(body: OrderRequest):
        return await active.preview_order(body)

    @app.post("/v1/orders", dependencies=secured)
    async def submit_order(body: OrderRequest):
        return await active.submit_order(body)

    @app.post("/v1/orders/{router_order_id}/cancel", dependencies=secured)
    async def cancel_order(router_order_id: str):
        return await active.cancel_order(router_order_id)

    @app.post("/v1/orders/{router_order_id}/amend", dependencies=secured)
    async def amend_order(router_order_id: str, body: AmendRequest):
        return await active.amend_order(
            router_order_id, quantity=body.quantity, price=body.price
        )

    @app.get("/v1/discovery/rankings", dependencies=secured)
    async def rankings(
        profile: str,
        ranking: str,
        market: str = "ALL",
        limit: int = Query(20, ge=1, le=100),
    ):
        return await active.rankings(
            profile_id=profile, ranking=ranking, market=market, limit=limit
        )

    @app.get("/v1/discovery/conditions", dependencies=secured)
    async def conditions(profile: str):
        return await active.conditions(profile_id=profile)

    @app.post("/v1/discovery/conditions/{condition_id}/run", dependencies=secured)
    async def run_condition(condition_id: str, profile: str):
        return await active.run_condition(profile_id=profile, condition_id=condition_id)

    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket, token: str | None = None):
        authorization = websocket.headers.get("authorization")
        supplied = token or (
            authorization.removeprefix("Bearer ").strip() if authorization else ""
        )
        if not active.api_token or not secrets.compare_digest(
            supplied, active.api_token
        ):
            await websocket.close(code=4401, reason="installation token required")
            return
        await websocket.accept()
        client = None
        profile_id = ""
        try:
            body = StreamRequest.model_validate(await websocket.receive_json())
            subscriptions = [
                StreamSubscription.model_validate(item) for item in body.subscriptions
            ]
            profile_id = body.profile_id
            client = await active.subscribe(profile_id, subscriptions)
            while True:
                event = await client.queue.get()
                await websocket.send_json(event.model_dump(mode="json"))
        except WebSocketDisconnect:
            pass
        except RouterError as exc:
            await websocket.send_json(exc.as_body().model_dump(mode="json"))
            await websocket.close(code=4400)
        finally:
            if client is not None:
                active.unsubscribe(profile_id, client.id)

    return app
