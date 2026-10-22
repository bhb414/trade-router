"""KIS admission keeps live-exit work ahead of candle/search backlogs."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from trade_router import RequestPriority
from trade_router_studio.connections import ConnectionRuntime, _FairPriorityGate
from trade_router_studio.settings import SettingsStore
from test_connections import workspace


def test_gate_is_serial_prioritizes_waiters_and_bounds_starvation():
    async def check():
        gate = _FairPriorityGate(max_overtakes=3)
        release, started = asyncio.Event(), asyncio.Event()
        order = []
        active = peak = 0

        async def work(name, priority, *, block=False):
            nonlocal active, peak
            async with gate.enter(priority):
                active += 1
                peak = max(peak, active)
                order.append(name)
                if block:
                    started.set()
                    await release.wait()
                await asyncio.sleep(0)
                active -= 1

        first = asyncio.create_task(work("active", 40, block=True))
        await started.wait()
        tasks = [asyncio.create_task(work("old-candle", 40))]
        tasks += [asyncio.create_task(work(f"urgent-{i}", 10)) for i in range(6)]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, *tasks)
        assert order[:5] == ["active", "urgent-0", "urgent-1", "urgent-2", "old-candle"]
        assert peak == 1
        assert not gate._waiters and gate._owner is None

    asyncio.run(check())


def test_cancelling_a_waiter_does_not_leak_or_steal_gate():
    async def check():
        gate = _FairPriorityGate()
        async with gate.enter(40):
            async def wait():
                async with gate.enter(10):
                    pytest.fail("Timed-out request must not run")

            with pytest.raises(TimeoutError):
                await asyncio.wait_for(wait(), 0.01)
            assert not gate._waiters
        async with gate.enter(10):
            assert gate._owner is not None
        assert gate._owner is None

    asyncio.run(check())


def test_cancel_after_slot_grant_releases_it_for_next_waiter():
    async def check():
        gate = _FairPriorityGate()
        context = gate.enter(40)
        await context.__aenter__()

        async def wait():
            async with gate.enter(10):
                pytest.fail("Cancelled waiter must not execute")

        waiting = asyncio.create_task(wait())
        await asyncio.sleep(0)
        await context.__aexit__(None, None, None)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        async with gate.enter(40):
            assert gate._owner is not None
        assert gate._owner is None

    asyncio.run(check())


def test_gate_close_releases_waiters_and_rejects_new_work():
    async def check():
        gate = _FairPriorityGate()
        async with gate.enter(40):
            async def wait():
                async with gate.enter(10):
                    pytest.fail("Closed gate must not start queued work")

            waiting = asyncio.create_task(wait())
            await asyncio.sleep(0)
            gate.close()
            with pytest.raises(RuntimeError, match="closed"):
                await waiting
        with pytest.raises(RuntimeError, match="closed"):
            async with gate.enter(10):
                pytest.fail("Closed gate must not reopen")

    asyncio.run(check())


def test_active_request_cancellation_keeps_key_reserved_until_http_finishes():
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200))) as client:
            runtime = ConnectionRuntime(SimpleNamespace(), http_client=client)
            started, release = asyncio.Event(), asyncio.Event()
            calls = []

            async def active():
                calls.append("active")
                started.set()
                await release.wait()
                return "done"

            async def following():
                calls.append("following")

            first = asyncio.create_task(runtime._kis_guarded_request("key", active))
            await started.wait()
            first.cancel()
            second = asyncio.create_task(runtime._kis_guarded_request("key", following))
            for _ in range(5):
                await asyncio.sleep(0)
            assert calls == ["active"]
            assert not first.done()
            # Repeated cancellation must not release a still-active HTTP slot.
            first.cancel()
            await asyncio.sleep(0)
            assert calls == ["active"]
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            assert isinstance(results[0], asyncio.CancelledError)
            assert calls == ["active", "following"]
            await runtime.close()

    asyncio.run(check())


def test_failed_http_releases_slot_for_next_request():
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200))) as client:
            runtime = ConnectionRuntime(SimpleNamespace(), http_client=client)

            async def failed():
                raise httpx.ReadTimeout("offline timeout")

            async def succeeds():
                return "ok"

            with pytest.raises(httpx.ReadTimeout):
                await runtime._kis_guarded_request("key", failed)
            assert await runtime._kis_guarded_request("key", succeeds) == "ok"
            await runtime.close()

    asyncio.run(check())


def test_order_still_observes_quiet_window_after_read():
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200))) as client:
            runtime = ConnectionRuntime(SimpleNamespace(), http_client=client)
            recorded = []

            async def mark():
                recorded.append(asyncio.get_running_loop().time())

            await runtime._kis_guarded_request("key", mark)
            await runtime._kis_guarded_request("key", mark, order_submission=True, priority=RequestPriority.ORDER)
            assert recorded[1] - recorded[0] >= 1.09
            await runtime.close()

    asyncio.run(check())


def test_actual_kis_backlog_cannot_hide_urgent_work_inside_request_workers(tmp_path: Path):
    async def check():
        settings = SettingsStore(workspace(tmp_path / "priority"))
        connection = settings.add_connection(provider="kis", target="LIVE", label="offline",
                                             app_key="test-key", app_secret="test-secret")
        started, release = asyncio.Event(), asyncio.Event()
        paths, completion_times = [], []
        active = peak = 0

        async def handler(request):
            nonlocal active, peak
            path = request.url.path
            paths.append(path)
            active += 1
            peak = max(peak, active)
            try:
                if path == "/offline/first-bars":
                    started.set()
                    await release.wait()
                completion_times.append((path, asyncio.get_running_loop().time()))
                return httpx.Response(200, json={"rt_cd": "0", "output": {}})
            finally:
                active -= 1

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ConnectionRuntime(settings, http_client=client)

            async def token(_):
                return SimpleNamespace(token_type="Bearer", value="offline-token")

            runtime._access_token = token

            async def read(name, operation, priority):
                return await runtime._kis_get(connection, path=f"/offline/{name}", tr_id="TEST",
                    params={}, operation=operation, priority=priority, group="market",
                    dedupe_key=name, cache_ttl=0)

            first = asyncio.create_task(read("first-bars", "market.bars.minute", RequestPriority.STRATEGY_DATA))
            await started.wait()
            tasks = [asyncio.create_task(read("queued-bars", "market.bars.minute", RequestPriority.STRATEGY_DATA)),
                     asyncio.create_task(read("account", "account.balance", RequestPriority.ACCOUNT)),
                     asyncio.create_task(read("quote", "market.quote", RequestPriority.STRATEGY_DATA)),
                     asyncio.create_task(runtime._kis_order_post(connection, tr_id="ORDER", body={}, path="/offline/order"))]
            # All callers must be visible to the gate before its current HTTP ends.
            for _ in range(20):
                if sum(len(gate._waiters) for gate in runtime._kis_request_gates.values()) == 4:
                    break
                await asyncio.sleep(0)
            assert sum(len(gate._waiters) for gate in runtime._kis_request_gates.values()) == 4
            release.set()
            try:
                await asyncio.wait_for(asyncio.gather(first, *tasks), 8)
                assert paths == ["/offline/first-bars", "/offline/order", "/offline/account", "/offline/quote", "/offline/queued-bars"]
                assert completion_times[1][1] - completion_times[0][1] >= 1.09
                assert peak == 1
            finally:
                await runtime.close()

    asyncio.run(check())


def test_cached_read_does_not_extend_order_quiet_period(tmp_path: Path):
    async def check():
        settings = SettingsStore(workspace(tmp_path / "cached-priority"))
        connection = settings.add_connection(provider="kis", target="LIVE", label="offline",
                                             app_key="test-key", app_secret="test-secret")
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"rt_cd": "0", "output": {}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ConnectionRuntime(settings, http_client=client)

            async def token(_):
                return SimpleNamespace(token_type="Bearer", value="offline-token")

            runtime._access_token = token
            kwargs = dict(path="/offline/account", tr_id="TEST", params={}, operation="account.balance",
                          priority=RequestPriority.ACCOUNT, group="account", dedupe_key="cached-balance", cache_ttl=30)
            await runtime._kis_get(connection, **kwargs)
            completed = dict(runtime._kis_last_request_completed)
            await runtime._kis_get(connection, **kwargs)
            assert calls == ["/offline/account"]
            assert runtime._kis_last_request_completed == completed
            await runtime.close()

    asyncio.run(check())
