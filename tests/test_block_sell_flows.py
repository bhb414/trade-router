"""Offline contracts for time-scoped sell flows in one managed strategy."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote as urlquote
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trade_router import OrderStatus, ScheduleKind
from trade_router_studio.block_packages import (
    DEFAULT_CONDITION_BOLLINGER_SETTINGS,
    BlockTradeCondition,
    ConditionBollingerBlockSettings,
    block_package_from_source,
    render_block_strategy,
)
from trade_router_studio.app import create_app
from trade_router_studio.paths import WorkspacePaths


SEOUL = ZoneInfo("Asia/Seoul")


def at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-20T{clock}").replace(tzinfo=SEOUL)


def condition_group(identifier: str, *conditions: BlockTradeCondition) -> dict:
    return {
        "id": identifier,
        "conditions": [item.model_dump(mode="json") for item in conditions],
    }


def profit_condition(value: str = "3", *, operator: str = "GTE") -> BlockTradeCondition:
    return BlockTradeCondition(
        left="profit_rate", operator=operator, right="value", value=Decimal(value)
    )


def close_condition(value: str, *, interval: str = "3m") -> BlockTradeCondition:
    return BlockTradeCondition(
        left="close", operator="GT", right="value", value=Decimal(value),
        left_bar_interval=interval, left_bar_data_mode="COMPLETE_ONLY",
    )


def sell_flow(identifier: str = "early", **changes) -> dict:
    result = {
        "id": identifier,
        "name": f"{identifier} 매도",
        "sell_scan_seconds": 20 if identifier == "early" else 45,
        "sell_start_time": "09:00" if identifier == "early" else "10:00",
        "sell_end_time": "10:00" if identifier == "early" else "15:20",
        "sell_condition_groups": [
            condition_group(f"{identifier}-profit", profit_condition())
        ],
    }
    result.update(changes)
    return result


def settings_for(flows: list[dict], **changes) -> ConditionBollingerBlockSettings:
    values = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    # Most tests in this module exercise legacy condition-group compatibility.
    # Clear the bundled example's already-migrated tree so validation performs
    # the same group-to-tree conversion as an older exported strategy.
    values["sell_rule_tree"] = None
    values.update(
        strategy_id="sell-flows",
        trading_account="test-account",
        condition_source="test-api",
        hts_user_id="test-hts",
        sell_flows=flows,
    )
    values.update(changes)
    return ConditionBollingerBlockSettings.model_validate(values)


class MemoryState:
    def __init__(self) -> None:
        self.values = {
            "ownership_initialized": True,
            "strategy_positions": {"005930": "5"},
        }

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value) -> None:
        self.values[key] = value


class RecordingScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def every(self, *args, **kwargs) -> None:
        self.calls.append(("every", args, kwargs))

    def daily_at(self, *args, **kwargs) -> None:
        self.calls.append(("daily_at", args, kwargs))

    def on_bar_close(self, *args, **kwargs) -> None:
        self.calls.append(("on_bar_close", args, kwargs))


class SellHarness:
    def __init__(self, settings: ConditionBollingerBlockSettings, now: str = "09:30:00") -> None:
        self.now = at(now)
        self.orders: list[dict] = []
        self.bar_calls: list[tuple[str, str, int]] = []
        self.logs: list[str] = []
        self.advance_on_bars_to: datetime | None = None
        self.advance_on_balance_to: datetime | None = None
        self.scheduler = RecordingScheduler()
        self.position = SimpleNamespace(
            instrument=SimpleNamespace(code="005930"),
            quantity=Decimal("5"), available_quantity=Decimal("5"),
            average_price=Decimal("100"), strategy_quantity=Decimal("5"),
        )
        namespace: dict[str, object] = {}
        exec(compile(render_block_strategy(settings), "<sell-flows>", "exec"), namespace)
        harness = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return harness.now.astimezone(tz) if tz is not None else harness.now

        namespace["datetime"] = Clock
        self.namespace = namespace
        self.strategy = namespace["ManagedBlockStrategy"]()
        self.ctx = SimpleNamespace(
            state=MemoryState(), scheduler=self.scheduler,
            discovery=SimpleNamespace(conditions=self.conditions),
            account=SimpleNamespace(balance=self.balance, open_orders=self.open_orders),
            market=SimpleNamespace(
                quote=self.market_quote, bars=self.bars,
                subscribe_quotes=self.subscribe_quotes, unsubscribe=self.unsubscribe,
            ),
            orders=SimpleNamespace(
                market_sell=self.sell, limit_sell=self.sell,
            ),
            log=SimpleNamespace(
                info=self.logs.append, warning=self.logs.append, error=self.logs.append,
            ),
        )

    async def conditions(self, user_id):
        return [SimpleNamespace(id="1", name="테스트 조건식")]

    async def balance(self):
        if self.advance_on_balance_to is not None:
            self.now = self.advance_on_balance_to
            self.advance_on_balance_to = None
        return SimpleNamespace(positions=(self.position,), buy_commission_rate=Decimal("0"),
                               sell_commission_rate=Decimal("0"), sell_tax_rate=Decimal("0"))

    async def open_orders(self):
        return ()

    async def market_quote(self, code, **kwargs):
        return SimpleNamespace(price=Decimal("104"), delayed=False, stale=False)

    async def bars(self, code, interval, **kwargs):
        self.bar_calls.append((code, interval, kwargs["count"]))
        if self.advance_on_bars_to is not None:
            self.now = self.advance_on_bars_to
            self.advance_on_bars_to = None
        return [SimpleNamespace(
            open=Decimal("190"), high=Decimal("210"), low=Decimal("180"),
            close=Decimal("200"), volume=Decimal("1000"),
        ) for _ in range(kwargs["count"])]

    async def subscribe_quotes(self, code, **kwargs):
        return ({"id": f"quote-{code}"},)

    async def unsubscribe(self, subscription_id):
        return None

    async def sell(self, **kwargs):
        self.orders.append(kwargs)
        return SimpleNamespace(status=OrderStatus.ACKNOWLEDGED, message="")

    def quote(self, price: str = "104"):
        return SimpleNamespace(
            instrument=SimpleNamespace(code="005930"), price=Decimal(price),
            delayed=False, stale=False, received_at=self.now,
        )

    async def prepare_live_position(self) -> None:
        await self.strategy._refresh_live_positions(
            self.ctx, await self.balance(), await self.open_orders()
        )


def test_sell_flows_round_trip_with_independent_windows_scans_and_conditions() -> None:
    settings = settings_for([
        sell_flow(),
        sell_flow(
            "late", sell_scan_seconds=60,
            sell_condition_groups=[condition_group("late-bars", close_condition("150", interval="5m"))],
        ),
    ])
    restored = block_package_from_source("시간별 매도", render_block_strategy(settings))

    assert restored.settings.model_dump(mode="json") == settings.model_dump(mode="json")
    assert restored.settings.sell_flows[0].sell_scan_seconds == 20
    condition = restored.settings.sell_flows[1].sell_condition_groups[0].conditions[0]
    assert condition.left_bar_interval == "5m"


@pytest.mark.parametrize("change", [
    {"sell_start_time": "10:00", "sell_end_time": "10:00"},
    {"sell_start_time": "15:00", "sell_end_time": "09:00"},
    {"sell_start_time": "25:00"},
    {"sell_scan_seconds": 0},
    {"id": "two words"},
    {"sell_condition_groups": []},
    {"sell_condition_groups": [
        condition_group("same", profit_condition()),
        condition_group("same", profit_condition("-3", operator="LTE")),
    ]},
])
def test_invalid_sell_flow_is_rejected(change: dict) -> None:
    with pytest.raises(ValidationError):
        settings_for([sell_flow(**change)])


def test_sell_flow_ids_are_unique_and_at_most_eight_are_allowed() -> None:
    with pytest.raises(ValidationError):
        settings_for([sell_flow(), sell_flow()])
    with pytest.raises(ValidationError):
        settings_for([sell_flow(f"flow-{index}") for index in range(9)])
    assert len(settings_for([sell_flow(f"flow-{index}") for index in range(8)]).sell_flows) == 8


def test_old_source_without_sell_flows_uses_the_legacy_window_and_scan_interval() -> None:
    settings = settings_for([], sell_condition_groups=[
        condition_group("legacy-profit", profit_condition())
    ])
    source = render_block_strategy(settings)
    source = "\n".join(
        line for line in source.splitlines()
        if not line.startswith("SELL_FLOWS_JSON = ")
    )
    restored = block_package_from_source("기존 전략", source)
    assert restored.settings.sell_flows == []

    harness = SellHarness(settings)
    asyncio.run(harness.strategy.on_start(harness.ctx))
    sell_calls = [
        entry for entry in harness.scheduler.calls
        if entry[2]["schedule_id"].startswith("condition-sell")
    ]
    assert sell_calls == [("every", (), {
        "seconds": settings.buy_scan_seconds,
        "schedule_id": "condition-sell-live",
        "between": (settings.sell_start_time, settings.sell_end_time),
        "aligned": True,
    })]


def test_malformed_explicit_sell_flow_metadata_fails_closed() -> None:
    source = render_block_strategy(settings_for([sell_flow()]))
    source = "\n".join(
        "SELL_FLOWS_JSON = 123"
        if line.startswith("SELL_FLOWS_JSON = ") else line
        for line in source.splitlines()
    )
    with pytest.raises(ValueError, match="매도 흐름"):
        block_package_from_source("손상 전략", source)


def test_each_sell_flow_registers_its_own_schedule_kind_and_window() -> None:
    harness = SellHarness(settings_for([
        sell_flow(),
        sell_flow(
            "late", sell_start_time="10:00", sell_end_time="15:20",
            sell_condition_groups=[condition_group("late-bar", close_condition("150", interval="5m"))],
        ),
    ]))
    asyncio.run(harness.strategy.on_start(harness.ctx))
    by_id = {entry[2]["schedule_id"]: entry for entry in harness.scheduler.calls}

    early = by_id["condition-sell-early-live"]
    assert early[0] == "every"
    assert early[2]["seconds"] == 20
    assert early[2]["between"] == ("09:00", "10:00")
    late = by_id["condition-sell-late-bars-1"]
    assert late[0] == "on_bar_close"
    assert late[1] == ("5m",)
    assert late[2]["between"] == ("10:00", "15:20")


@pytest.mark.parametrize(("clock", "expected"), [
    ("08:59:59", 0),
    ("09:00:00", 1),
    ("09:59:59", 1),
    ("10:00:00", 0),
])
def test_realtime_sell_window_includes_start_and_excludes_end(clock: str, expected: int) -> None:
    harness = SellHarness(settings_for([sell_flow()]), now=clock)

    async def exercise() -> None:
        await harness.prepare_live_position()
        await harness.strategy.on_quote(harness.ctx, harness.quote())

    asyncio.run(exercise())
    assert len(harness.orders) == expected


def test_realtime_sell_rechecks_actual_time_after_account_refresh() -> None:
    harness = SellHarness(settings_for([sell_flow()]), now="09:59:59")

    async def exercise() -> None:
        await harness.prepare_live_position()
        quote = harness.quote()
        harness.advance_on_balance_to = at("10:00:00")
        await harness.strategy.on_quote(harness.ctx, quote)

    asyncio.run(exercise())
    assert harness.orders == []


def test_realtime_sell_uses_actual_session_after_account_refresh() -> None:
    flow = sell_flow(sell_start_time="09:00", sell_end_time="16:00")
    harness = SellHarness(
        settings_for([flow], route="SOR", sell_order_type="MARKET"),
        now="15:29:59",
    )

    async def exercise() -> None:
        await harness.prepare_live_position()
        quote = harness.quote()
        harness.advance_on_balance_to = at("15:30:01")
        await harness.strategy.on_quote(harness.ctx, quote)

    asyncio.run(exercise())
    assert harness.orders == []
    assert any("애프터마켓" in message for message in harness.logs)


def test_active_overlapping_sell_flows_are_or_but_submit_only_one_order() -> None:
    harness = SellHarness(settings_for([
        sell_flow(
            sell_end_time="11:00",
            sell_condition_groups=[condition_group("too-high", profit_condition("10"))],
        ),
        sell_flow(
            "second", sell_start_time="09:00", sell_end_time="11:00",
            sell_condition_groups=[condition_group("passes", profit_condition("3"))],
        ),
        sell_flow(
            "third", sell_start_time="09:00", sell_end_time="11:00",
            sell_condition_groups=[condition_group("also-passes", profit_condition("2"))],
        ),
    ]), now="09:30:00")

    async def exercise() -> None:
        await harness.prepare_live_position()
        await harness.strategy.on_quote(harness.ctx, harness.quote())

    asyncio.run(exercise())
    assert len(harness.orders) == 1
    assert any("second 매도" in message for message in harness.logs)


def test_each_sell_flow_keeps_or_rows_and_and_groups() -> None:
    pass_flow = sell_flow(sell_condition_groups=[
        condition_group(
            "profit-or", profit_condition("10"), profit_condition("-3", operator="LTE")
        ),
        condition_group(
            "average-required",
            BlockTradeCondition(
                left="average_price", operator="GT", right="value", value=Decimal("0")
            ),
        ),
    ])
    fail_flow = sell_flow(sell_condition_groups=[
        condition_group(
            "profit-or", profit_condition("3"), profit_condition("-3", operator="LTE")
        ),
        condition_group(
            "average-required",
            BlockTradeCondition(
                left="average_price", operator="LT", right="value", value=Decimal("0")
            ),
        ),
    ])

    failing = SellHarness(settings_for([fail_flow]))
    passing = SellHarness(settings_for([pass_flow]))

    async def exercise() -> None:
        await failing.prepare_live_position()
        await failing.strategy.on_quote(failing.ctx, failing.quote(price="96"))
        await passing.prepare_live_position()
        await passing.strategy.on_quote(passing.ctx, passing.quote(price="96"))

    asyncio.run(exercise())
    assert failing.orders == []
    assert len(passing.orders) == 1


def test_scheduled_sell_event_uses_only_its_mapped_flow_conditions() -> None:
    harness = SellHarness(settings_for([
        sell_flow(
            sell_end_time="11:00",
            sell_condition_groups=[condition_group("passes", close_condition("100"))],
        ),
        sell_flow(
            "second", sell_start_time="09:00", sell_end_time="11:00",
            sell_condition_groups=[condition_group("fails", close_condition("300"))],
        ),
    ]), now="09:03:00")

    async def exercise() -> None:
        await harness.strategy.on_start(harness.ctx)
        await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
            schedule_id="condition-sell-second-bars-1",
            scheduled_at=harness.now, kind=ScheduleKind.BAR_CLOSE,
        ))
        assert harness.orders == []
        await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
            schedule_id="condition-sell-early-bars-1",
            scheduled_at=harness.now, kind=ScheduleKind.BAR_CLOSE,
        ))

    asyncio.run(exercise())
    assert len(harness.orders) == 1
    assert any(call[1] == "3m" for call in harness.bar_calls)


def test_delayed_sell_timer_does_not_submit_after_its_flow_ends() -> None:
    harness = SellHarness(settings_for([
        sell_flow(sell_condition_groups=[condition_group("passes", close_condition("100"))]),
    ]), now="09:59:59")

    async def exercise() -> None:
        await harness.strategy.on_start(harness.ctx)
        harness.now = at("10:00:00")
        await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
            schedule_id="condition-sell-early-bars-1",
            scheduled_at=at("09:59:59"), kind=ScheduleKind.BAR_CLOSE,
        ))

    asyncio.run(exercise())
    assert harness.orders == []


def test_overlapping_scheduled_sell_flows_cannot_race_duplicate_orders() -> None:
    harness = SellHarness(settings_for([
        sell_flow(
            sell_end_time="11:00",
            sell_condition_groups=[condition_group("first", close_condition("100"))],
        ),
        sell_flow(
            "second", sell_start_time="09:00", sell_end_time="11:00",
            sell_condition_groups=[condition_group("second", close_condition("100"))],
        ),
    ]), now="09:03:00")

    async def exercise() -> None:
        await harness.strategy.on_start(harness.ctx)
        events = [SimpleNamespace(
            schedule_id=f"condition-sell-{identifier}-bars-1",
            scheduled_at=harness.now, kind=ScheduleKind.BAR_CLOSE,
        ) for identifier in ("early", "second")]
        await asyncio.gather(*(harness.strategy.on_bar(harness.ctx, event) for event in events))

    asyncio.run(exercise())
    assert len(harness.orders) == 1


def test_timer_dispatch_still_routes_interval_and_daily_sell_flows() -> None:
    live = SellHarness(settings_for([
        sell_flow(sell_condition_groups=[
            condition_group("live-profit", profit_condition())
        ]),
    ]), now="09:30:00")
    daily = SellHarness(settings_for([
        sell_flow(
            sell_end_time="11:00",
            sell_condition_groups=[
                condition_group("daily-close", close_condition("100", interval="1d"))
            ],
        ),
    ]), now="09:00:00")

    async def exercise() -> None:
        await live.strategy.on_start(live.ctx)
        await live.strategy.on_timer(live.ctx, SimpleNamespace(
            schedule_id="condition-sell-early-live",
            scheduled_at=live.now, kind=ScheduleKind.INTERVAL,
        ))
        await daily.strategy.on_start(daily.ctx)
        await daily.strategy.on_timer(daily.ctx, SimpleNamespace(
            schedule_id="condition-sell-early-daily-1",
            scheduled_at=daily.now, kind=ScheduleKind.DAILY,
        ))

    asyncio.run(exercise())
    assert len(live.orders) == 1
    assert len(daily.orders) == 1
    assert any(call[1] == "1d" for call in daily.bar_calls)


def test_bar_sell_rechecks_actual_time_after_condition_evaluation() -> None:
    harness = SellHarness(settings_for([
        sell_flow(sell_condition_groups=[
            condition_group("bar-pass", close_condition("100"))
        ]),
    ]), now="09:59:59")
    harness.advance_on_bars_to = at("10:00:00")

    async def exercise() -> None:
        await harness.strategy.on_start(harness.ctx)
        await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
            schedule_id="condition-sell-early-bars-1",
            scheduled_at=at("09:59:59"), kind=ScheduleKind.BAR_CLOSE,
        ))

    asyncio.run(exercise())
    assert harness.orders == []


def test_bar_sell_uses_actual_session_instead_of_stale_schedule_time() -> None:
    flow = sell_flow(
        sell_start_time="09:00", sell_end_time="16:00",
        sell_condition_groups=[condition_group("bar-pass", close_condition("100"))],
    )
    harness = SellHarness(
        settings_for([flow], route="SOR", sell_order_type="MARKET"),
        now="15:30:01",
    )

    async def exercise() -> None:
        await harness.strategy.on_start(harness.ctx)
        await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
            schedule_id="condition-sell-early-bars-1",
            scheduled_at=at("15:29:59"), kind=ScheduleKind.BAR_CLOSE,
        ))

    asyncio.run(exercise())
    assert harness.orders == []
    assert any("애프터마켓" in message for message in harness.logs)



def test_buy_only_market_quote_rejection_does_not_suppress_later_sells() -> None:
    harness = SellHarness(settings_for([sell_flow()]), now="09:30:00")
    strategy = harness.strategy
    rejection = "시장가호가불가로 주문이 불가합니다. [APBK1943]"

    assert strategy._is_non_retryable_buy_rejection(rejection)
    assert not strategy._is_non_retryable_sell_rejection(rejection)



def test_sell_flows_survive_api_save_export_and_import(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    paths = WorkspacePaths(
        root=root, strategies=root / "strategies", logs=root / "logs",
        data=root / "data", config=root / "config",
    )
    with TestClient(create_app(paths)) as client:
        endpoint = f"/api/strategies/{urlquote('블록 예제.py')}/block-package"
        package = client.get(endpoint).json()
        flows = [sell_flow(), sell_flow(
            "late", sell_scan_seconds=60,
            sell_condition_groups=[condition_group("late-bar", close_condition("150", interval="5m"))],
        )]
        package["settings"]["sell_flows"] = deepcopy(flows)

        saved = client.put(endpoint, json=package)
        assert saved.status_code == 200, saved.text
        exported = client.get(endpoint).json()
        exported_flows = exported["settings"]["sell_flows"]
        for original, restored_flow in zip(flows, exported_flows, strict=True):
            assert {
                key: restored_flow[key]
                for key in original
            } == original
            assert restored_flow["sell_rule_tree"]["id"] == "legacy-sell-root"
            assert restored_flow["sell_rule_tree"]["action"]["side"] == "SELL"

        exported["name"] = "시간별 매도 복사"
        imported = client.post("/api/strategies/import-block", json=exported)
        assert imported.status_code == 200, imported.text
        copied = client.get(
            f"/api/strategies/{urlquote(imported.json()['name'])}/block-package"
        ).json()
        assert copied["settings"]["sell_flows"] == exported_flows
        assert copied["settings"]["strategy_id"] != package["settings"]["strategy_id"]

