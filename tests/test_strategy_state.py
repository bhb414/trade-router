from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from trade_router import Strategy
from trade_router_studio import strategy_worker
from trade_router_studio.strategy_worker import StrategyState, _run_events
from trade_router_studio.file_io import snapshot_backup_path


class Log:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, message):
        self.warnings.append(str(message))

    def error(self, message):
        self.errors.append(str(message))


def make_state(tmp_path, monkeypatch, name="test.py"):
    monkeypatch.setenv("TRADE_ROUTER_HOME", str(tmp_path))
    monkeypatch.setattr(StrategyState, "_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(StrategyState, "_CLOSE_TIMEOUT", 0.08)
    return StrategyState(name, Log())


def payload(path):
    return json.loads(path.read_text(encoding="utf-8"))


def deny_main_replace(monkeypatch, state):
    real_replace = strategy_worker.replace_snapshot
    locked = threading.Event()
    locked.set()

    def replace(path, target):
        if Path(target) == state._path and locked.is_set():
            raise PermissionError("held Windows read handle")
        return real_replace(path, target)

    monkeypatch.setattr(strategy_worker, "replace_snapshot", replace)
    return locked


def await_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("state writer did not finish")
        time.sleep(0.005)


def test_normal_save_is_durable_and_legacy_state_loads(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state._path.write_text('{"pending_buy_submissions": {"a": 3}}', encoding="utf-8")
    state.close()
    loaded = make_state(tmp_path, monkeypatch)
    calls = []
    real_fsync = os.fsync

    def fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    try:
        assert loaded.get("pending_buy_submissions") == {"a": 3}
        loaded.set("strategy_positions", {"005930": "2"})
        assert payload(loaded._path)["strategy_positions"] == {"005930": "2"}
        assert calls
        assert not loaded._pending_path.exists()
        assert not list(loaded._path.parent.glob("*.tmp"))
    finally:
        loaded.close()


def test_locked_main_commits_journal_then_promotes_latest_revision(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state.set("pending_buy_submissions", {})
    locked = deny_main_replace(monkeypatch, state)
    try:
        state.set("pending_buy_submissions", {"first": 1})
        state.set("pending_buy_submissions", {"first": 1, "second": 2})
        assert payload(state._path)["pending_buy_submissions"] == {}
        assert payload(state._pending_path)["pending_buy_submissions"] == {"first": 1, "second": 2}
        assert len(state._log.warnings) == 1
        locked.clear()
        await_until(lambda: not state._pending_path.exists())
        assert payload(state._path)["pending_buy_submissions"] == {"first": 1, "second": 2}
    finally:
        state.close()


def test_close_under_persistent_lock_keeps_durable_recoverable_state(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state.set("strategy_positions", {"005930": "4"})
    locked = deny_main_replace(monkeypatch, state)
    state.set("strategy_positions", {"005930": "1"})
    started = time.monotonic()
    state.close()
    assert time.monotonic() - started < 0.5
    assert not state._writer.is_alive()
    assert state._pending_path.exists()
    locked.clear()
    recovered = make_state(tmp_path, monkeypatch)
    try:
        assert recovered.get("strategy_positions") == {"005930": "1"}
        assert not recovered._pending_path.exists()
        assert payload(recovered._path)["strategy_positions"] == {"005930": "1"}
    finally:
        recovered.close()


def test_newer_main_wins_if_old_journal_cleanup_failed_before_crash(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state.set("value", 1)
    locked = deny_main_replace(monkeypatch, state)
    state.set("value", 2)
    old_journal = state._pending_path.read_bytes()
    locked.clear()
    state.set("value", 3)
    state.close()
    # Emulate a crash before the stale journal could be removed after main save.
    state._pending_path.write_bytes(old_journal)
    recovered = make_state(tmp_path, monkeypatch)
    try:
        assert recovered.get("value") == 3
        assert not recovered._pending_path.exists()
    finally:
        recovered.close()


def test_equal_revision_journal_cannot_overwrite_already_promoted_snapshot(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state.set("value", 4)
    snapshot = state._path.read_bytes()
    state.close()
    state._pending_path.write_bytes(snapshot)
    recovered = make_state(tmp_path, monkeypatch)
    try:
        assert recovered.get("value") == 4
        assert not recovered._pending_path.exists()
    finally:
        recovered.close()


def test_journal_commit_failure_is_fatal_and_does_not_acknowledge_update(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state.set("pending_buy_submissions", {})
    real_replace = strategy_worker.replace_snapshot

    def replace(path, target):
        if Path(target) in {state._path, state._pending_path}:
            raise PermissionError("directory is not writable")
        return real_replace(path, target)

    monkeypatch.setattr(strategy_worker, "replace_snapshot", replace)
    try:
        with pytest.raises(PermissionError):
            state.set("pending_buy_submissions", {"uncommitted": 1})
        assert state.get("pending_buy_submissions") == {}
        assert payload(state._path)["pending_buy_submissions"] == {}
        assert not state._pending_path.exists()
    finally:
        state.close()


def test_serialization_failure_keeps_committed_memory_and_disk(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    try:
        state.set("value", 5)
        with pytest.raises(TypeError):
            state.set("value", object())
        assert state.get("value") == 5
        assert payload(state._path)["value"] == 5
    finally:
        state.close()


def test_mutated_value_obtained_from_get_is_persisted(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    try:
        state.set("positions", {})
        positions = state.get("positions")
        positions["005930"] = 2
        state.set("positions", positions)
        assert payload(state._path)["positions"] == {"005930": 2}
    finally:
        state.close()


def test_corrupt_recovery_journal_is_preserved_and_never_silently_ignored(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state.set("value", 5)
    state.close()
    state._pending_path.write_text("broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="복구 기록"):
        make_state(tmp_path, monkeypatch)
    assert state._pending_path.read_text(encoding="utf-8") == "broken"


@pytest.mark.skipif(os.name != "nt", reason="Windows ordinary read handles deny atomic replacement")
def test_actual_windows_reader_lock_does_not_crash_state_save(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state.set("value", 1)
    try:
        with state._path.open("r", encoding="utf-8") as reader:
            state.set("value", 2)
            assert state.get("value") == 2
            assert payload(state._pending_path)["value"] == 2
            assert json.load(reader)["value"] == 1
        await_until(lambda: not state._pending_path.exists())
        assert payload(state._path)["value"] == 2
    finally:
        state.close()


def test_locked_state_promotion_does_not_delay_live_event_lane(tmp_path, monkeypatch):
    async def check():
        state = make_state(tmp_path, monkeypatch)
        state.set("value", 1)
        locked = deny_main_replace(monkeypatch, state)
        buy_seen, quote_seen = asyncio.Event(), asyncio.Event()
        events = asyncio.Queue()

        class Managed(Strategy):
            def runtime_lane(self, event):
                return "live" if event["event_type"] == "quote" else "buy"

            async def on_timer(self, ctx, event):
                ctx.state.set("value", 2)
                buy_seen.set()

            async def on_quote(self, ctx, event):
                assert ctx.state.get("value") == 2
                quote_seen.set()

        ctx = SimpleNamespace(state=state, log=state._log)
        stop = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(_run_events(Managed(), ctx, SimpleNamespace(next_event=events.get), stop))
        try:
            from test_strategy_event_dispatch import quote, schedule
            events.put_nowait(schedule("search"))
            await asyncio.wait_for(buy_seen.wait(), 1)
            events.put_nowait(quote())
            await asyncio.wait_for(quote_seen.wait(), 0.2)
            assert state._pending_path.exists()
        finally:
            stop.set_result(None)
            await task
            locked.clear()
            await asyncio.to_thread(state.close)

    asyncio.run(check())


def test_background_integrity_failure_stops_event_processing(tmp_path, monkeypatch):
    async def check():
        state = make_state(tmp_path, monkeypatch)
        state.set("value", 1)
        real_replace = strategy_worker.replace_snapshot
        failed = threading.Event()

        def replace(path, target):
            if Path(target) == state._path:
                if path == state._pending_path:
                    failed.set()
                    raise OSError("unexpected journal filesystem failure")
                raise PermissionError("locked")
            return real_replace(path, target)

        monkeypatch.setattr(strategy_worker, "replace_snapshot", replace)
        state.set("value", 2)
        stop = asyncio.get_running_loop().create_future()
        runtime = SimpleNamespace(next_event=asyncio.Queue().get)
        try:
            with pytest.raises(OSError, match="unexpected journal"):
                await asyncio.wait_for(_run_events(Strategy(), SimpleNamespace(state=state, log=state._log), runtime, stop), 1)
            assert failed.is_set()
            assert payload(state._pending_path)["value"] == 2
        finally:
            stop.cancel()
            with pytest.raises(RuntimeError, match="저장에 실패"):
                state.close()

    asyncio.run(check())


@pytest.mark.parametrize("unreadable", ["corrupt", "permission"])
def test_unreadable_newer_main_never_promotes_older_journal(tmp_path, monkeypatch, unreadable):
    state = make_state(tmp_path, monkeypatch)
    state.set("pending_buy_submissions", {"old": 1})
    older = state._path.read_bytes()
    state.set("pending_buy_submissions", {"new": 2})
    state.close()
    state._pending_path.write_bytes(older)
    if unreadable == "corrupt":
        state._path.write_text("corrupt current snapshot", encoding="utf-8")
    else:
        real_read = strategy_worker.read_shared_text

        def read(path):
            if path == state._path:
                raise PermissionError("temporarily unreadable main snapshot")
            return real_read(path)

        monkeypatch.setattr(strategy_worker, "read_shared_text", read)
    current = state._path.read_bytes()
    with pytest.raises(RuntimeError, match="기존 전략 상태 파일"):
        make_state(tmp_path, monkeypatch)
    assert state._path.read_bytes() == current
    assert state._pending_path.read_bytes() == older


def test_snapshot_fsync_failure_preserves_previous_state(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state.set("pending_buy_submissions", {"saved": 1})

    def fail_fsync(fd):
        raise OSError("disk cannot flush")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    try:
        with pytest.raises(OSError, match="disk cannot flush"):
            state.set("pending_buy_submissions", {"unsaved": 2})
        assert state.get("pending_buy_submissions") == {"saved": 1}
        assert payload(state._path)["pending_buy_submissions"] == {"saved": 1}
        assert not list(state._path.parent.glob("*.tmp"))
    finally:
        state.close()


def test_worker_flushes_state_even_when_startup_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_ROUTER_HOME", str(tmp_path))
    closed = []
    original_close = StrategyState.close

    def close(state):
        closed.append(state)
        original_close(state)

    async def fail_start(strategy, config, logger, state):
        state.set("pending_buy_submissions", {"on_start": 1})
        raise ValueError("startup failed")

    monkeypatch.setattr(StrategyState, "close", close)
    monkeypatch.setattr(strategy_worker, "_load_strategy", lambda path: SimpleNamespace(config=object()))
    monkeypatch.setattr(strategy_worker, "_run_with_state", fail_start)
    with pytest.raises(ValueError, match="startup failed"):
        asyncio.run(strategy_worker.run(Path("test.py")))
    assert len(closed) == 1
    assert closed[0]._closed
    assert payload(closed[0]._path)["pending_buy_submissions"] == {"on_start": 1}


@pytest.mark.parametrize("error", [1176, 1177])
def test_partial_main_replacement_recovers_acknowledged_state_not_stale_journal(tmp_path, monkeypatch, error):
    state = make_state(tmp_path, monkeypatch)
    state.set("strategy_positions", {"sold-stock": 10})
    stale_journal = state._path.read_bytes()
    state.set("strategy_positions", {})
    acknowledged = state._path.read_bytes()
    state._pending_path.write_bytes(stale_journal)
    real_replace = strategy_worker.replace_snapshot

    def partial_replace(source, target):
        if target == state._path:
            if error == 1177:
                os.replace(target, snapshot_backup_path(target))
            raise OSError(f"native partial replacement {error}")
        return real_replace(source, target)

    monkeypatch.setattr(strategy_worker, "replace_snapshot", partial_replace)
    with pytest.raises(OSError, match="native partial replacement"):
        state.set("strategy_positions", {"unacknowledged": 1})
    assert state.get("strategy_positions") == {}
    assert list(state._path.parent.glob("*.tmp")), "failed replacement must preserve its attempted snapshot"
    state.close()
    survivor = state._path if state._path.exists() else snapshot_backup_path(state._path)
    assert survivor.read_bytes() == acknowledged
    monkeypatch.setattr(strategy_worker, "replace_snapshot", real_replace)
    recovered = make_state(tmp_path, monkeypatch)
    try:
        assert recovered.get("strategy_positions") == {}
        assert payload(recovered._path)["strategy_positions"] == {}
        assert not recovered._pending_path.exists()
    finally:
        recovered.close()


@pytest.mark.parametrize("error", [1176, 1177])
def test_partial_pending_replacement_recovers_latest_acknowledged_journal(tmp_path, monkeypatch, error):
    state = make_state(tmp_path, monkeypatch)
    state.set("pending_buy_submissions", {})
    real_replace = strategy_worker.replace_snapshot
    # Keep this failure injection deterministic; background recovery has its
    # own crash/race and live-lane tests below and above.
    monkeypatch.setattr(state, "_start_writer_locked", lambda: None)
    locked = deny_main_replace(monkeypatch, state)
    state.set("pending_buy_submissions", {"already-submitted": 1})
    acknowledged = state._pending_path.read_bytes()

    def partial_replace(source, target):
        if target == state._path:
            raise PermissionError("locked main")
        if target == state._pending_path:
            if error == 1177:
                os.replace(target, snapshot_backup_path(target))
            raise OSError(f"native partial replacement {error}")
        return real_replace(source, target)

    monkeypatch.setattr(strategy_worker, "replace_snapshot", partial_replace)
    with pytest.raises(OSError, match="native partial replacement"):
        state.set("pending_buy_submissions", {"unacknowledged": 2})
    assert state.get("pending_buy_submissions") == {"already-submitted": 1}
    state.close()
    survivor = state._pending_path if state._pending_path.exists() else snapshot_backup_path(state._pending_path)
    assert survivor.read_bytes() == acknowledged
    locked.clear()
    monkeypatch.setattr(strategy_worker, "replace_snapshot", real_replace)
    recovered = make_state(tmp_path, monkeypatch)
    try:
        assert recovered.get("pending_buy_submissions") == {"already-submitted": 1}
        assert payload(recovered._path)["pending_buy_submissions"] == {"already-submitted": 1}
    finally:
        recovered.close()


def test_crash_during_promotion_recovers_pending_after_main_renamed(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state.set("value", 1)
    locked = deny_main_replace(monkeypatch, state)
    state.set("value", 2)
    state.close()
    os.replace(state._path, snapshot_backup_path(state._path))
    locked.clear()
    recovered = make_state(tmp_path, monkeypatch)
    try:
        assert recovered.get("value") == 2
        assert payload(recovered._path)["value"] == 2
    finally:
        recovered.close()


def test_missing_legacy_main_with_backup_is_never_empty_new_state(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state.close()
    snapshot_backup_path(state._path).write_text('{"pending_buy_submissions":{"legacy":1}}', encoding="utf-8")
    recovered = make_state(tmp_path, monkeypatch)
    try:
        assert recovered.get("pending_buy_submissions") == {"legacy": 1}
        assert payload(recovered._path)["pending_buy_submissions"] == {"legacy": 1}
        assert recovered._revision == 1
    finally:
        recovered.close()


def test_unreadable_backup_fails_closed_instead_of_loading_older_pending(tmp_path, monkeypatch):
    state = make_state(tmp_path, monkeypatch)
    state.set("value", 1)
    state._pending_path.write_bytes(state._path.read_bytes())
    state.close()
    state._path.unlink()
    backup = snapshot_backup_path(state._path)
    backup.write_text("broken latest acknowledged backup", encoding="utf-8")
    with pytest.raises(RuntimeError, match="복구 기록"):
        make_state(tmp_path, monkeypatch)
    assert not state._path.exists()
    assert backup.read_text(encoding="utf-8") == "broken latest acknowledged backup"
