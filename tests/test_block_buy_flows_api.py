from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from trade_router_studio.app import create_app
from trade_router_studio.paths import WorkspacePaths


def _workspace(root: Path) -> WorkspacePaths:
    return WorkspacePaths(
        root=root, strategies=root / "strategies", logs=root / "logs",
        data=root / "data", config=root / "config",
    )


def _flows(settings: dict) -> list[dict]:
    source_keys = (
        "target_source", "hts_user_id", "condition_id", "expected_condition_name",
        "ranking_kind", "ranking_venue", "ranking_market", "ranking_limit", "symbols",
        "buy_scan_seconds", "buy_start_time", "buy_end_time", "buy_condition_groups",
    )
    base = {key: deepcopy(settings[key]) for key in source_keys}
    result = []
    for flow_id, name, start, end, condition_id, interval in (
        ("morning", "장초반", "09:00", "10:00", "3", "1m"),
        ("daytime", "장중", "10:00", "15:00", "7", "3m"),
    ):
        flow = deepcopy(base)
        flow.update(
            id=flow_id, name=name, condition_id=condition_id,
            buy_start_time=start, buy_end_time=end,
            buy_condition_groups=[{
                "id": f"{flow_id}-buy",
                "conditions": [{
                    "left": "close", "operator": "GT", "right": "value", "value": "1000",
                    "left_bar_interval": interval,
                }],
            }],
        )
        result.append(flow)
    return result


def test_flow_settings_survive_save_export_and_copy_without_resetting_shared_settings(tmp_path: Path) -> None:
    app = create_app(_workspace(tmp_path / "workspace"))
    endpoint = f"/api/strategies/{quote('블록 예제.py')}/block-package"
    with TestClient(app) as client:
        package = client.get(endpoint).json()
        package["settings"]["buy_flows"] = _flows(package["settings"])
        shared = {
            key: deepcopy(package["settings"][key])
            for key in ("strategy_id", "trading_account", "total_operating_budget",
                        "buy_budget_per_symbol", "sell_condition_groups", "sell_start_time", "close_time")
        }
        saved = client.put(endpoint, json=package)
        assert saved.status_code == 200, saved.text
        exported = client.get(endpoint).json()
        flows = exported["settings"]["buy_flows"]
        assert [flow["condition_id"] for flow in flows] == ["3", "7"]
        assert [flow["buy_start_time"] for flow in flows] == ["09:00", "10:00"]
        assert [flow["buy_condition_groups"][0]["conditions"][0]["left_bar_interval"] for flow in flows] == ["1m", "3m"]
        assert {key: exported["settings"][key] for key in shared} == shared

        exported["name"] = "시간별 복사"
        copied = client.post("/api/strategies/import-block", json=exported)
        assert copied.status_code == 200, copied.text
        new_name = copied.json()["name"]
        duplicate = client.get(f"/api/strategies/{quote(new_name)}/block-package").json()
        assert duplicate["settings"]["buy_flows"] == flows
        assert duplicate["settings"]["strategy_id"] != shared["strategy_id"]
        assert duplicate["settings"]["total_operating_budget"] == shared["total_operating_budget"]
        assert client.get("/api/status").json()["running"] == 0


def test_invalid_flow_window_does_not_replace_saved_strategy(tmp_path: Path) -> None:
    paths = _workspace(tmp_path / "workspace")
    with TestClient(create_app(paths)) as client:
        endpoint = f"/api/strategies/{quote('블록 예제.py')}/block-package"
        package = client.get(endpoint).json()
        source_path = paths.strategies / "블록 예제.py"
        original = source_path.read_text(encoding="utf-8")
        package["settings"]["buy_flows"] = _flows(package["settings"])
        package["settings"]["buy_flows"][1]["buy_end_time"] = "09:30"
        rejected = client.put(endpoint, json=package)
        assert rejected.status_code == 422
        assert source_path.read_text(encoding="utf-8") == original
