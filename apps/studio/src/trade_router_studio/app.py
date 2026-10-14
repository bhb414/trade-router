from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .paths import WorkspacePaths
from .strategies import StrategyManager, StrategyNameError
from .updates import check_for_update


class StrategySource(BaseModel):
    source: str


def create_app(paths: WorkspacePaths | None = None) -> FastAPI:
    workspace = paths or WorkspacePaths.discover()
    manager = StrategyManager(workspace)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        workspace.ensure()
        yield
        await manager.shutdown()

    app = FastAPI(title="Trade Router", version=__version__, lifespan=lifespan)
    app.state.workspace = workspace
    app.state.strategies = manager

    static_root = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_root), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_root / "index.html")

    @app.get("/api/status")
    async def status() -> dict[str, object]:
        strategies = manager.list()
        return {
            "version": __version__,
            "workspace": str(workspace.root),
            "server_time": datetime.now().astimezone().isoformat(),
            "running": sum(1 for item in strategies if item["running"]),
            "strategy_count": len(strategies),
            "broker_connections": 0,
        }

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
