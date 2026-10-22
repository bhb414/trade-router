from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from trade_router_studio.app import create_app
from trade_router_studio.diagnostics import build_diagnostics
from trade_router_studio.process_diagnostics import failure_line, safe_exit_details
from trade_router_studio.runtime_store import RuntimeStore
from trade_router_studio.strategies import StrategyManager, StrategyProcess
from test_diagnostics import fixture_input, unpack, SECRETS
from test_studio import workspace


def test_exit_projection_has_a_fixed_allowlist_not_arbitrary_exception_or_details():
    assert failure_line("PRIVATE_SECRET_Error: private") is None
    assert failure_line("ordinary message PermissionError: private") is None
    projected = safe_exit_details({"exit_code": 1, "stop_requested": False,
        "exception_type": SECRETS[0], "failure_area": SECRETS[1],
        "winerror": SECRETS[2], "raw_message": " ".join(SECRETS)})
    assert projected == {"exit_code": 1, "stop_requested": False, "exception_type": None,
                         "winerror": None, "failure_area": "UNKNOWN", "exit_reason": "PROCESS_FAILED"}
    assert not any(secret in json.dumps(projected) for secret in SECRETS)
    assert safe_exit_details({"exit_code": 0, "stop_requested": False})["exit_reason"] == "PROCESS_COMPLETED"
    assert safe_exit_details({"exit_code": -15, "stop_requested": True})["exit_reason"] == "USER_STOPPED"


def test_reader_collects_failure_before_exit_callback_and_tolerates_callback_failure(tmp_path):
    async def run():
        paths = workspace(tmp_path / "workspace")
        paths.ensure()
        snapshots = []

        async def on_exit(name):
            snapshots.append(manager.exit_diagnostics(name))
            raise OSError("PRIVATE_DIAGNOSTIC_DISK_ERROR")

        manager = StrategyManager(paths, on_exit=on_exit)
        reader = asyncio.StreamReader()
        reader.feed_data(('Traceback (most recent call last):\n'
            '  File "C:/PRIVATE_PATH/strategy_worker.py", line 1, in set\n'
            'PermissionError: [WinError 5] C:/PRIVATE_PATH/strategy-state/PRIVATE_NAME.json\n').encode())
        reader.feed_eof()

        async def wait():
            return 1

        process = SimpleNamespace(stdout=reader, returncode=1, wait=wait)
        state = StrategyProcess(name="private.py", process=process)
        manager._states[state.name] = state
        await manager._read_output(state)
        assert snapshots == [{"exit_code": 1, "stop_requested": False, "exception_type": "PermissionError",
                              "winerror": 5, "failure_area": "STATE_PERSISTENCE", "exit_reason": "PROCESS_FAILED"}]
        assert state.stopped_at is not None
        assert any("종료 코드 1" in entry.message for entry in state.logs)
        assert "PRIVATE_DIAGNOSTIC_DISK_ERROR" not in json.dumps(snapshots)

    asyncio.run(run())


def test_user_stop_or_success_does_not_reuse_caught_exception_details(tmp_path):
    manager = StrategyManager(workspace(tmp_path))
    state = StrategyProcess(name="test.py", exit_code=1, stop_requested=True,
                            failure_details={"exception_type": "PermissionError", "winerror": 5})
    manager._states[state.name] = state
    assert manager.exit_diagnostics(state.name)["exception_type"] is None
    state.exit_code = 0
    state.stop_requested = False
    assert manager.exit_diagnostics(state.name)["exception_type"] is None


def test_later_exception_does_not_inherit_an_old_windows_failure():
    state = StrategyProcess(name="test.py")
    StrategyManager._append_log(state, "error", "Traceback (most recent call last):")
    StrategyManager._append_log(state, "error", "PermissionError: [WinError 5] strategy-state/private.json")
    StrategyManager._append_log(state, "error", "Traceback (most recent call last):")
    StrategyManager._append_log(state, "error", "KeyError: private")
    assert state.failure_details == {"exception_type": "KeyError", "winerror": None, "failure_area": "UNKNOWN"}


def test_unclassified_final_exception_does_not_reuse_a_previous_caught_traceback(tmp_path):
    manager = StrategyManager(workspace(tmp_path))
    state = StrategyProcess(name="test.py", exit_code=1)
    manager._states[state.name] = state
    StrategyManager._append_log(state, "error", "Traceback (most recent call last):")
    StrategyManager._append_log(state, "error", "PermissionError: [WinError 5] strategy-state/private.json")
    StrategyManager._append_log(state, "error", "Traceback (most recent call last):")
    StrategyManager._append_log(state, "error", "PrivateCustomException: PRIVATE_SECRET")
    assert manager.exit_diagnostics(state.name) == {
        "exit_code": 1, "stop_requested": False, "exception_type": None,
        "winerror": None, "failure_area": "UNKNOWN", "exit_reason": "PROCESS_FAILED",
    }


def test_restart_waits_for_old_exit_cleanup_and_clears_failure_context(tmp_path):
    async def run():
        paths = workspace(tmp_path / "workspace")
        paths.ensure()
        cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
        exits = []

        async def on_exit(name):
            exits.append(manager.exit_diagnostics(name))
            cleanup_started.set()
            await release_cleanup.wait()

        manager = StrategyManager(paths, on_exit=on_exit)
        await manager.save("restarting.py", "raise PermissionError('private')\n")
        await manager.start("restarting.py")
        await asyncio.wait_for(cleanup_started.wait(), 2)
        await manager.save("restarting.py", "print('ready')\n")
        second_start = asyncio.create_task(manager.start("restarting.py"))
        await asyncio.sleep(0)
        assert not second_start.done()
        release_cleanup.set()
        await asyncio.wait_for(second_start, 2)
        await asyncio.wait_for(manager._states["restarting.py"].reader_task, 2)
        assert exits[0]["exception_type"] == "PermissionError"
        assert exits[1]["exception_type"] is None
        assert exits[1]["exit_reason"] == "PROCESS_COMPLETED"

    asyncio.run(run())


def test_process_exit_survives_restart_and_busy_market_history(tmp_path):
    paths = workspace(tmp_path / "workspace")
    app = create_app(paths)
    with TestClient(app) as client:
        # A plain script exercises the actual supervising process without any brokerage call.
        response = client.put("/api/strategies/private-crash.py", json={
            "source": "raise PermissionError(13, 'PRIVATE_API_KEY_SENTINEL')\n"})
        assert response.status_code == 200
        assert client.post("/api/strategies/private-crash.py/start").status_code == 200
        for _ in range(100):
            events = app.state.runtime_store.events(event_types=("strategy.process_exit",))
            if events:
                break
            import time
            time.sleep(0.02)
        assert events
        details = events[0]["details"]
        assert details["exit_code"] == 1
        assert details["exception_type"] == "PermissionError"
        assert details["stop_requested"] is False
        assert SECRETS[0] not in json.dumps(details)
        for _ in range(1005):
            app.state.runtime_store.record(source="system", event_type="service.market.quote.success", message="ok")

    with TestClient(create_app(paths)) as client:
        result = client.get("/api/system/diagnostics/export?hours=24")
        assert result.status_code == 200
        data, text = unpack(result.content)
        assert data["history"]["strategy_logs"] == []
        assert all(item["event"] != "strategy.process_exit" for item in data["history"]["events"])
        assert len(data["history"]["strategy_exits"]) == 1
        summary = data["history"]["strategy_exits"][0]
        assert summary["reason"] == "PROCESS_FAILED"
        assert summary["exception_type"] == "PermissionError"
        assert summary["exit_code"] == 1
        assert "private-crash" not in text and SECRETS[0] not in text


def test_diagnostic_store_failure_does_not_skip_subscription_cleanup(tmp_path, monkeypatch):
    app = create_app(workspace(tmp_path / "workspace"))
    cleaned = []

    def failing_record(**kwargs):
        raise OSError("PRIVATE_DIAGNOSTIC_STORAGE_ERROR")

    def cleanup_for(kind):
        async def cleanup(name):
            cleaned.append((kind, name))
        return cleanup

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.runtime_store, "record", failing_record)
        monkeypatch.setattr(app.state.scheduler, "unregister", cleanup_for("schedule"))
        monkeypatch.setattr(app.state.realtime, "unsubscribe_strategy", cleanup_for("realtime"))
        monkeypatch.setattr(app.state.strategy_events, "unregister", cleanup_for("events"))
        client.portal.call(app.state.strategies._on_exit, "private.py")
        assert cleaned == [("schedule", "private.py"), ("realtime", "private.py"), ("events", "private.py")]


def test_old_exit_event_is_unknown_exit_not_subscription_failure():
    arguments, _, _, events = fixture_input()
    events[:] = [{"source": SECRETS[5], "occurred_at": arguments["now"].isoformat(),
        "event_type": "strategy.process_exit", "details": {},
        "message": "전략 프로세스가 종료되어 일정·실시간 구독·세션을 정리했습니다."}]
    data, _ = unpack(build_diagnostics(**arguments)[0])
    assert data["history"]["events"][0]["reason"] == "PROCESS_EXITED"
    assert data["history"]["strategy_exits"][0]["exit_code"] is None


def test_event_type_filter_combines_with_source_and_uses_sql_parameters(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    try:
        store.record(source="a", event_type="strategy.process_exit", message="")
        store.record(source="b", event_type="strategy.process_exit", message="")
        store.record(source="a", event_type="strategy.start", message="")
        assert [item["source"] for item in store.events(source="a", event_types=("strategy.process_exit",))] == ["a"]
        assert store.events(event_types=()) == []
        assert store.events(event_types=("x') OR 1=1 --",)) == []
    finally:
        store.close()
