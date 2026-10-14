from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from trade_router_studio.app import create_app
from trade_router_studio.paths import WorkspacePaths
from trade_router_studio.strategies import StrategyManager, StrategyNameError


def workspace(root: Path) -> WorkspacePaths:
    return WorkspacePaths(
        root=root,
        strategies=root / "strategies",
        logs=root / "logs",
        data=root / "data",
        config=root / "config",
    )


def test_workspace_api_creates_and_saves_strategy(tmp_path: Path) -> None:
    app = create_app(workspace(tmp_path / "workspace"))
    with TestClient(app) as client:
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.headers["cache-control"] == "no-store, max-age=0"
        assert status.json()["strategy_count"] == 1

        created = client.put(
            "/api/strategies/my_strategy.py",
            json={"source": "print('ready')\n"},
        )
        assert created.status_code == 200
        loaded = client.get("/api/strategies/my_strategy.py")
        assert loaded.json()["source"] == "print('ready')\n"


def test_settings_api_keeps_secrets_out_of_responses(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "workspace")
    app = create_app(paths)
    with TestClient(app) as client:
        connection = client.post(
            "/api/settings/connections",
            json={
                "provider": "kis",
                "label": "KIS main",
                "app_key": "test-app-key",
                "app_secret": "test-app-secret",
            },
        )
        assert connection.status_code == 201
        connection_payload = connection.json()
        assert "app_key" not in connection_payload
        assert "app_secret" not in connection_payload

        account = client.post(
            "/api/settings/accounts",
            json={
                "alias": "main account",
                "connection_id": connection_payload["id"],
                "account_number": "12345678",
                "product_code": "01",
            },
        )
        assert account.status_code == 201
        assert account.json()["masked_account"] == "****5678"
        assert "account_number" not in account.json()

        stored_text = (paths.config / "settings.json").read_text(encoding="utf-8")
        stored_secrets = (paths.config / "secrets.dpapi").read_bytes()
        assert "test-app-secret" not in stored_text
        assert b"test-app-secret" not in stored_secrets

        status = client.get("/api/status").json()
        assert status["registered_apis"] == 1
        assert status["registered_accounts"] == 1


def test_strategy_name_cannot_escape_workspace(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "workspace")
    paths.ensure()
    manager = StrategyManager(paths)
    for name in ("../outside.py", "folder/inside.py", "bad.txt"):
        try:
            manager.resolve(name)
        except StrategyNameError:
            pass
        else:
            raise AssertionError(f"unsafe name accepted: {name}")


def test_strategy_process_captures_output(tmp_path: Path) -> None:
    async def run() -> None:
        paths = workspace(tmp_path / "workspace")
        paths.ensure()
        manager = StrategyManager(paths)
        await manager.save("once.py", "print('hello from strategy', flush=True)\n")
        await manager.start("once.py")
        for _ in range(40):
            if not manager.details("once.py")["running"]:
                break
            await asyncio.sleep(0.025)
        logs = manager.logs("once.py")
        assert any("hello from strategy" in line for line in logs["lines"])
        assert logs["exit_code"] == 0

    asyncio.run(run())


def test_user_stop_is_not_reported_as_strategy_failure(tmp_path: Path) -> None:
    async def run() -> None:
        paths = workspace(tmp_path / "workspace")
        paths.ensure()
        manager = StrategyManager(paths)
        await manager.save("running.py", "import time\ntime.sleep(30)\n")
        await manager.start("running.py")
        result = await manager.stop("running.py")
        assert result["running"] is False
        assert result["stop_requested"] is True
        assert any(
            "stopped by user" in line
            for line in manager.logs("running.py")["lines"]
        )

    asyncio.run(run())
