from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .paths import WorkspacePaths
from .settings import SettingsError, SettingsStore
from .strategies import StrategyManager, StrategyNameError
from .updates import check_for_update


class StrategySource(BaseModel):
    source: str


class ConnectionRegistration(BaseModel):
    provider: str
    label: str
    app_key: str
    app_secret: str


class AccountRegistration(BaseModel):
    alias: str
    connection_id: str
    account_number: str
    product_code: str = ""


def create_app(paths: WorkspacePaths | None = None) -> FastAPI:
    workspace = paths or WorkspacePaths.discover()
    manager = StrategyManager(workspace)
    settings = SettingsStore(workspace)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        workspace.ensure()
        yield
        await manager.shutdown()

    app = FastAPI(title="Trade Router", version=__version__, lifespan=lifespan)
    app.state.workspace = workspace
    app.state.strategies = manager
    app.state.settings = settings

    @app.middleware("http")
    async def prevent_api_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

    static_root = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_root), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_root / "index.html")

    @app.get("/api/status")
    async def status() -> dict[str, object]:
        strategies = manager.list()
        connections = settings.list_connections()
        accounts = settings.list_accounts()
        return {
            "version": __version__,
            "workspace": str(workspace.root),
            "server_time": datetime.now().astimezone().isoformat(),
            "running": sum(1 for item in strategies if item["running"]),
            "strategy_count": len(strategies),
            "broker_connections": 0,
            "registered_apis": len(connections),
            "registered_accounts": len(accounts),
        }

    @app.get("/api/settings/providers")
    async def provider_schemas() -> list[dict[str, object]]:
        return settings.providers()

    @app.get("/api/settings/connections")
    async def list_connections() -> list[dict[str, object]]:
        return settings.list_connections()

    @app.post("/api/settings/connections", status_code=201)
    async def register_connection(
        body: ConnectionRegistration,
    ) -> dict[str, object]:
        try:
            return settings.add_connection(
                provider=body.provider,
                label=body.label,
                app_key=body.app_key,
                app_secret=body.app_secret,
            )
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/settings/connections/{connection_id}", status_code=204)
    async def delete_connection(connection_id: str) -> None:
        try:
            settings.remove_connection(connection_id)
        except SettingsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/settings/accounts")
    async def list_accounts() -> list[dict[str, object]]:
        return settings.list_accounts()

    @app.post("/api/settings/accounts", status_code=201)
    async def register_account(body: AccountRegistration) -> dict[str, object]:
        try:
            return settings.add_account(
                alias=body.alias,
                connection_id=body.connection_id,
                account_number=body.account_number,
                product_code=body.product_code,
            )
        except SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/settings/accounts/{account_id}", status_code=204)
    async def delete_account(account_id: str) -> None:
        try:
            settings.remove_account(account_id)
        except SettingsError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/strategies")
    async def list_strategies() -> list[dict[str, object]]:
        return manager.list()

    @app.get("/api/strategies/{name}")
    async def read_strategy(name: str) -> dict[str, object]:
        try:
            return {"name": name, "source": manager.read(name), **manager.details(name)}
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Strategy not found.") from exc

    @app.put("/api/strategies/{name}")
    async def save_strategy(name: str, body: StrategySource) -> dict[str, object]:
        try:
            return await manager.save(name, body.source)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc

    @app.delete("/api/strategies/{name}", status_code=204)
    async def delete_strategy(name: str) -> None:
        try:
            await manager.delete(name)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/strategies/{name}/start")
    async def start_strategy(name: str) -> dict[str, object]:
        try:
            return await manager.start(name)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Strategy not found.") from exc

    @app.post("/api/strategies/{name}/stop")
    async def stop_strategy(name: str) -> dict[str, object]:
        try:
            return await manager.stop(name)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/strategies/{name}/logs")
    async def strategy_logs(
        name: str, after: int = Query(default=0, ge=0)
    ) -> dict[str, object]:
        try:
            return manager.logs(name, after)
        except StrategyNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/updates/check")
    async def updates() -> dict[str, object]:
        try:
            return await check_for_update(__version__)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


app = create_app()
