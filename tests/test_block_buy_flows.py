"""Offline contracts for time-scoped buy flows in one managed strategy."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from trade_router import (
    Balance, BrokerCapabilities, BuyingPower, InstrumentId, OrderRequest,
    OrderRoute, OrderSide, OrderStatus, OrderType, ProviderOrderAck, RouterError,
    RouterErrorCode, ScheduleKind,
)
from trade_router_studio.block_packages import (
    DEFAULT_CONDITION_BOLLINGER_SETTINGS,
    BlockTradeCondition,
    ConditionBollingerBlockSettings,
    block_package_from_source,
    render_block_strategy,
)
from trade_router_studio.orders import OrderService
from trade_router_studio.runtime_store import RuntimeStore
from trade_router_studio.sessions import StrategyManifest


SEOUL = ZoneInfo("Asia/Seoul")


def at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-20T{clock}").replace(tzinfo=SEOUL)


def condition_group(identifier="price", *, value="100", interval="3m", operator="GT"):
    return {
        "id": identifier,
        "conditions": [BlockTradeCondition(
            left="close", operator=operator, right="value", value=Decimal(value),
            left_bar_interval=interval,
        ).model_dump(mode="json")],
    }


def flow(identifier="early", **changes):
    values = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    result = {key: values[key] for key in (
        "target_source", "hts_user_id", "condition_id", "expected_condition_name",
        "ranking_kind", "ranking_venue", "ranking_market", "ranking_limit",
        "symbols", "buy_scan_seconds", "buy_start_time", "buy_end_time",
        "buy_condition_groups",
    )}
    result.update(
        id=identifier, name=f"{identifier} 매수",
        hts_user_id="test-hts", condition_id="1" if identifier == "early" else "2",
        buy_start_time="09:00" if identifier == "early" else "10:00",
        buy_end_time="10:00" if identifier == "early" else "15:00",
    )
    result.update(changes)
    return result


def settings_for(flows, **changes):
    values = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    values.update(
        strategy_id="shared-flows", trading_account="test-account",
        condition_source="test-api", hts_user_id="test-hts",
        buy_flows=flows,
    )
    values.update(changes)
    return ConditionBollingerBlockSettings.model_validate(values)


class MemoryState:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class RecordingScheduler:
    def __init__(self):
        self.calls = []

    def every(self, *args, **kwargs):
        self.calls.append(("every", args, kwargs))

    def daily_at(self, *args, **kwargs):
        self.calls.append(("daily_at", args, kwargs))

    def on_bar_close(self, *args, **kwargs):
        self.calls.append(("on_bar_close", args, kwargs))


class Harness:
    """A broker-free generated strategy with a controllable wall clock."""

    def __init__(self, settings, now="09:30:00", *, pending=False):
        self.settings = settings
        self.now = at(now)
        self.advance_on = None
        self.pending = pending
        self.discovery_calls = []
        self.bar_calls = []
        self.order_calls = []
        self.sell_calls = []
        self.positions = {}
        self.pending_orders = []
        self.matches_by_id = {"1": ["005930"], "2": ["000660"]}
        self.logs = []
        namespace = {}
        source = render_block_strategy(settings)
        exec(compile(source, "<time-scoped-block>", "exec"), namespace)
        harness = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return harness.now.astimezone(tz) if tz is not None else harness.now

        namespace["datetime"] = Clock
        self.namespace = namespace
        self.strategy = namespace["ManagedBlockStrategy"]()
        self.ctx = SimpleNamespace(
            state=MemoryState(), scheduler=RecordingScheduler(),
            discovery=SimpleNamespace(
                matches=self.matches, conditions=self.conditions, ranking=self.ranking,
            ),
            account=SimpleNamespace(
                balance=self.balance, open_orders=self.open_orders, buying_power=self.buying_power,
            ),
            market=SimpleNamespace(
                quote=self.quote, bars=self.bars,
                subscribe_quotes=self.subscribe_quotes, unsubscribe=self.unsubscribe,
            ),
            orders=SimpleNamespace(market_buy=self.buy, limit_buy=self.buy, market_sell=self.sell),
            log=SimpleNamespace(info=self.logs.append, warning=self.logs.append, error=self.logs.append),
        )

    def advance(self, stage):
        if self.advance_on == stage:
            self.now = at("10:00:00")

    async def matches(self, user, condition_id):
        self.discovery_calls.append((user, condition_id))
        await asyncio.sleep(0)
        self.advance("discovery")
        return [SimpleNamespace(instrument=SimpleNamespace(code=code), name=f"종목 {code}")
                for code in self.matches_by_id.get(condition_id, [])]

    async def conditions(self, user):
        return [SimpleNamespace(id="1", name="장초반"), SimpleNamespace(id="2", name="장중")]

    async def ranking(self, kind, **kwargs):
        self.discovery_calls.append((str(kind), kwargs))
        return [SimpleNamespace(instrument=SimpleNamespace(code="035420"), name="순위 종목")]

    async def balance(self):
        return SimpleNamespace(positions=list(self.positions.values()))

    async def open_orders(self):
        return list(self.pending_orders)

    async def buying_power(self, code, **kwargs):
        self.advance("buying_power")
        return SimpleNamespace(calculation_price=Decimal("25000"), maximum_quantity=Decimal("100"))

    async def quote(self, code, **kwargs):
        await asyncio.sleep(0)
        self.advance("quote")
        return SimpleNamespace(price=Decimal("25000"), stale=False, delayed=False)

    async def bars(self, code, interval, **kwargs):
        self.bar_calls.append((code, interval))
        self.advance("bars")
        return [SimpleNamespace(
            open=Decimal("190"), high=Decimal("210"), low=Decimal("180"),
            close=Decimal("200"), volume=Decimal("1000"),
        ) for _ in range(kwargs["count"])]

    async def subscribe_quotes(self, code, **kwargs):
        return [{"id": code}]

    async def unsubscribe(self, subscription_id):
        return None

    async def buy(self, **kwargs):
        self.order_calls.append(kwargs)
        code = kwargs["code"]
        quantity = Decimal(kwargs["quantity"])
        if self.pending:
            self.pending_orders.append(SimpleNamespace(
                instrument=SimpleNamespace(code=code), remaining_quantity=quantity,
                side=SimpleNamespace(value="BUY"),
            ))
            return SimpleNamespace(status=OrderStatus.ACKNOWLEDGED, message="")
        self.positions[code] = SimpleNamespace(
            instrument=SimpleNamespace(code=code), quantity=quantity,
            strategy_quantity=quantity, available_quantity=quantity,
            average_price=Decimal("25000"),
        )
        return SimpleNamespace(status=OrderStatus.FILLED, message="")

    async def sell(self, **kwargs):
        self.sell_calls.append(kwargs)
        self.positions.pop(kwargs["code"], None)
        return SimpleNamespace(status=OrderStatus.FILLED, message="")

    async def tick(self, identifier="early", scheduled_at=None):
        await self.strategy.on_timer(self.ctx, SimpleNamespace(
            schedule_id=f"condition-scan-{identifier}",
            scheduled_at=scheduled_at or self.now,
        ))


def test_buy_flows_round_trip_keeps_independent_sources_and_operand_settings():
    early = flow(buy_condition_groups=[condition_group(interval="1m")])
    later = flow("midday", condition_id="7", buy_scan_seconds=60,
                 buy_condition_groups=[condition_group("slow", interval="3m", value="150")])
    settings = settings_for([early, later])
    restored = block_package_from_source("시간별 매수", render_block_strategy(settings))
    assert restored.settings.model_dump(mode="json") == settings.model_dump(mode="json")
    assert restored.settings.buy_flows[0].buy_condition_groups[0].conditions[0].left_bar_interval == "1m"
    assert restored.settings.buy_flows[1].condition_id == "7"
    assert restored.settings.total_operating_budget == Decimal("150000")
    assert restored.settings.trading_account == "test-account"


def test_flow_can_use_hts_rankings_or_normalized_explicit_symbols():
    harness = Harness(settings_for([
        flow(),
        flow("rank", target_source="RANKING", ranking_kind="VOLUME", ranking_venue="NXT",
             ranking_market="KOSDAQ", ranking_limit=5),
        flow("symbols", target_source="SYMBOLS", symbols=[" 005930 ", "000660", "005930"]),
    ]))
    effective_flows = harness.namespace["BUY_FLOWS"]

    async def exercise():
        # Omitting a flow preserves the existing discovery helper's public shape.
        assert await harness.strategy._candidate_codes(harness.ctx) == ["005930"]
        assert await harness.strategy._candidate_codes(harness.ctx, effective_flows[1]) == ["035420"]
        assert await harness.strategy._candidate_codes(harness.ctx, effective_flows[2]) == ["005930", "000660"]

    asyncio.run(exercise())
    assert harness.discovery_calls[1][1] == {"venue": "NXT", "market": "KOSDAQ", "limit": 5}
    assert {"conditions", "rankings", "quotes"} <= set(
        harness.strategy.config.required_capabilities["primary"]
    )


def test_non_hts_flows_do_not_inherit_unused_top_level_discovery_requirements():
    harness = Harness(settings_for([
        flow(target_source="SYMBOLS", symbols=["005930"]),
        flow("midday", target_source="SYMBOLS", symbols=["000660"]),
    ]))
    assert "conditions" not in harness.strategy.config.required_capabilities["primary"]
    assert "rankings" not in harness.strategy.config.required_capabilities["primary"]


def test_every_hts_flow_validates_its_selected_condition_at_startup():
    harness = Harness(settings_for([
        flow(expected_condition_name="장초반"),
        flow("midday", expected_condition_name="잘못된 이름"),
    ]))
    with pytest.raises(RuntimeError, match="이름"):
        asyncio.run(harness.strategy.on_start(harness.ctx))


@pytest.mark.parametrize("change", [
    {"buy_start_time": "10:00", "buy_end_time": "10:00"},
    {"buy_start_time": "15:00", "buy_end_time": "09:00"},
    {"buy_start_time": "25:00"},
    {"id": "two words"},
    {"buy_scan_seconds": 0},
    {"target_source": "SYMBOLS", "symbols": []},
])
def test_invalid_buy_flow_is_rejected(change):
    with pytest.raises(ValidationError):
        settings_for([flow(**change)])


def test_flow_ids_are_unique_and_at_most_eight_flows_are_allowed():
    with pytest.raises(ValidationError):
        settings_for([flow(), flow()])
    with pytest.raises(ValidationError):
        settings_for([flow(f"flow-{index}") for index in range(9)])
    assert len(settings_for([flow(f"flow-{index}") for index in range(8)]).buy_flows) == 8


def test_flow_cannot_override_the_shared_account_or_budget():
    for key, value in (("trading_account", "other"), ("total_operating_budget", "999999")):
        with pytest.raises(ValidationError):
            settings_for([flow(**{key: value})])


@pytest.mark.parametrize("condition", [
    BlockTradeCondition(left="profit_rate", right="value"),
    BlockTradeCondition(left="close", right="average_price"),
    BlockTradeCondition(left="close", right="bollinger_upper", right_bar_interval="120m",
                        right_bollinger_period=55),
])
def test_flow_conditions_obey_existing_buy_metric_and_history_limits(condition):
    with pytest.raises(ValidationError):
        settings_for([flow(buy_condition_groups=[{
            "id": "invalid", "conditions": [condition.model_dump(mode="json")],
        }])])


def test_old_source_without_buy_flows_round_trips_and_keeps_legacy_scan():
    legacy = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    legacy.pop("buy_flows", None)
    settings = ConditionBollingerBlockSettings.model_validate(legacy)
    assert settings.buy_flows == []
    source = render_block_strategy(settings)
    # Old generated sources do not contain the new optional metadata constant.
    source = "\n".join(line for line in source.splitlines()
                       if not line.startswith("BUY_FLOWS_JSON = "))
    restored = block_package_from_source("기존 전략", source)
    assert restored.settings.buy_flows == []
    harness = Harness(settings, now="09:30:00")
    asyncio.run(harness.strategy.on_start(harness.ctx))
    scans = [entry[2] for entry in harness.ctx.scheduler.calls
             if entry[2]["schedule_id"].startswith("condition-scan")]
    assert scans == [{"seconds": 30, "schedule_id": "condition-scan",
                      "between": ("09:00", "15:00"), "aligned": True}]


def test_flow_schedules_are_independent_but_sell_and_close_schedules_are_shared():
    baseline = Harness(settings_for([]))
    harness = Harness(settings_for([flow(), flow("midday", buy_scan_seconds=60)]))
    asyncio.run(baseline.strategy.on_start(baseline.ctx))
    asyncio.run(harness.strategy.on_start(harness.ctx))
    by_id = {entry[2]["schedule_id"]: entry for entry in harness.ctx.scheduler.calls}
    assert by_id["condition-scan-early"][2]["between"] == ("09:00", "10:00")
    assert by_id["condition-scan-midday"][2]["between"] == ("10:00", "15:00")
    assert by_id["condition-scan-midday"][2]["seconds"] == 60
    sell_schedules = lambda h: [entry for entry in h.ctx.scheduler.calls
                               if not entry[2]["schedule_id"].startswith("condition-scan")]
    assert sell_schedules(harness) == sell_schedules(baseline)


@pytest.mark.parametrize("clock,identifier,expected", [
    ("08:59:59", "early", []),
    ("09:00:00", "early", ["1"]),
    ("09:59:59", "early", ["1"]),
    ("10:00:00", "early", []),
    ("09:59:59", "midday", []),
    ("10:00:00", "midday", ["2"]),
    ("15:00:00", "midday", []),
])
def test_flow_windows_include_start_and_exclude_end(clock, identifier, expected):
    harness = Harness(settings_for([flow(), flow("midday")]), now=clock)
    asyncio.run(harness.tick(identifier))
    assert [item[1] for item in harness.discovery_calls] == expected
    assert len(harness.order_calls) == len(expected)


@pytest.mark.parametrize("stage", ["discovery", "bars", "quote", "buying_power"])
def test_expired_flow_never_submits_after_awaited_work(stage):
    harness = Harness(settings_for([flow(buy_condition_groups=[condition_group()])]), now="09:59:59")
    harness.advance_on = stage
    asyncio.run(harness.tick())
    assert harness.now == at("10:00:00")
    assert harness.order_calls == []


def test_delayed_previous_session_tick_does_not_execute_in_new_session():
    harness = Harness(settings_for([flow(), flow("midday")]), now="10:00:01")
    asyncio.run(harness.tick("early", scheduled_at=at("09:59:30")))
    assert harness.discovery_calls == []
    assert harness.order_calls == []


def test_each_flow_uses_only_its_own_condition_groups():
    harness = Harness(settings_for([
        flow(buy_condition_groups=[condition_group("early-rule", value="999", interval="1m")]),
        flow("midday", buy_condition_groups=[condition_group("late-rule", value="100", interval="3m")]),
    ]))

    async def exercise():
        await harness.tick()
        assert not harness.order_calls
        harness.now = at("10:00:00")
        await harness.tick("midday")

    asyncio.run(exercise())
    assert harness.bar_calls == [("005930", "1m"), ("000660", "3m")]
    assert [order["code"] for order in harness.order_calls] == ["000660"]


@pytest.mark.parametrize("pending", [False, True])
def test_existing_holdings_or_pending_orders_are_shared_between_flows(pending):
    harness = Harness(settings_for([flow(), flow("midday")]), pending=pending)
    harness.matches_by_id["2"] = ["005930"]

    async def exercise():
        await harness.tick()
        harness.now = at("10:00:00")
        await harness.tick("midday")

    asyncio.run(exercise())
    assert len(harness.order_calls) == 1
    assert harness.strategy.config.trading_account == "test-account"
    assert harness.strategy.config.limits.max_exposure == Decimal("150000")


def test_overlapping_flows_share_the_same_tick_buy_limit():
    harness = Harness(settings_for([
        flow(buy_end_time="15:00"), flow("midday", buy_start_time="09:00"),
    ], max_new_buys_per_scan=1))

    async def exercise():
        await asyncio.gather(harness.tick("early"), harness.tick("midday"))

    asyncio.run(exercise())
    assert len(harness.order_calls) == 1


def test_one_portfolio_and_sell_flow_manage_buys_from_both_time_windows():
    sell_group = {
        "id": "sell-owned",
        "conditions": [BlockTradeCondition(
            left="average_price", operator="GT", right="value", value=Decimal("0"),
        ).model_dump(mode="json")],
    }
    harness = Harness(settings_for([flow(), flow("midday")], sell_condition_groups=[sell_group]))

    async def exercise():
        await harness.tick()
        harness.now = at("10:00:00")
        await harness.tick("midday")
        assert set(harness.ctx.state.get("strategy_positions")) == {"005930", "000660"}
        await harness.strategy.on_bar(harness.ctx, SimpleNamespace(
            schedule_id="condition-sell-live", scheduled_at=harness.now, kind=ScheduleKind.INTERVAL,
        ))

    asyncio.run(exercise())
    assert {order["code"] for order in harness.sell_calls} == {"005930", "000660"}
    assert harness.ctx.state.get("strategy_positions") == {}


def test_rejected_submission_is_not_retried_by_an_overlapping_flow_in_same_tick():
    harness = Harness(settings_for([
        flow(buy_end_time="15:00"), flow("midday", buy_start_time="09:00"),
    ]))
    harness.matches_by_id["2"] = ["005930"]
    attempts = []

    async def rejected_buy(**kwargs):
        attempts.append(kwargs)
        raise RouterError(RouterErrorCode.ORDER_REJECTED, "Test rejection")

    harness.ctx.orders.market_buy = rejected_buy

    async def exercise():
        await asyncio.gather(harness.tick("early"), harness.tick("midday"))

    asyncio.run(exercise())
    assert len(attempts) == 1


def test_aftermarket_market_buy_is_skipped_and_excluded_before_submission():
    harness = Harness(
        settings_for(
            [flow("after", buy_start_time="15:30", buy_end_time="19:00")],
            route="SOR",
            buy_order_type="MARKET",
        ),
        now="16:00:00",
    )

    async def exercise():
        await harness.tick("after")
        harness.now = at("16:00:30")
        await harness.tick("after")

    asyncio.run(exercise())

    assert harness.order_calls == []
    assert set(
        harness.ctx.state.get("daily_buy_exclusions", {})["symbols"]
    ) == {"000660"}
    warnings = [message for message in harness.logs if "매수 건너뜀" in message]
    assert len(warnings) == 1
    assert "애프터마켓" in warnings[0]
    assert "지정가로 설정" in warnings[0]


def test_buy_flows_share_actual_central_exposure_reservations(tmp_path):
    """Use the real order service/SQLite limit, not a simulated risk counter."""
    harness = Harness(settings_for([flow(), flow("midday")]), pending=True)
    harness.matches_by_id = {"1": ["005930", "000660"], "2": ["035420", "035720"]}

    class Gateway:
        def __init__(self):
            self.submitted = []

        def capabilities(self, account):
            return BrokerCapabilities(
                provider="offline-test", accounts=True, order_history=True,
                cash_orders=True, order_routes=(OrderRoute.KRX,),
            )

        def account_risk_limits(self, account):
            return {}

        def account_cost_rates(self, account):
            return {}

        async def balance(self, account):
            return Balance(cash=Decimal("1000000"), withdrawable_cash=Decimal("1000000"),
                           total_value=Decimal("1000000"), positions=(), as_of=harness.now)

        async def account_orders(self, account, **kwargs):
            return ()

        async def buying_power(self, account, code, *, order_type, price, route):
            return BuyingPower(
                instrument=InstrumentId(code), account_alias=account, order_type=order_type,
                requested_price=price or Decimal("0"), calculation_price=Decimal("25000"),
                cash_available=Decimal("1000000"), maximum_quantity=Decimal("100"),
                provider="offline-test", as_of=harness.now, route=route,
            )

        async def submit_order(self, account, request):
            self.submitted.append(request)
            return ProviderOrderAck(
                provider_order_id=f"offline-{len(self.submitted)}", provider="offline-test",
                acknowledged_at=harness.now, correlation_id="offline",
            )

    async def exercise():
        gateway = Gateway()
        store = RuntimeStore(tmp_path / "flow-exposure.sqlite3")
        service = OrderService(store, gateway)
        config = harness.strategy.config
        manifest = StrategyManifest(
            strategy_id=config.id, trading_account=config.trading_account,
            market_sources=config.market_sources, required_capabilities=config.required_capabilities,
            risk_limits={"max_exposure": str(config.limits.max_exposure)},
        )

        async def submit_through_central_service(**kwargs):
            request = OrderRequest(
                instrument=InstrumentId(kwargs["code"]), side=OrderSide.BUY,
                order_type=OrderType.MARKET, quantity=Decimal(kwargs["quantity"]),
                client_order_id=kwargs["client_order_id"], route=OrderRoute(kwargs["route"]),
            )
            order = await service.submit(strategy_name="shared.py", manifest=manifest, request=request)
            harness.pending_orders.append(SimpleNamespace(
                instrument=request.instrument, remaining_quantity=request.quantity,
                side=SimpleNamespace(value="BUY"),
            ))
            return order

        harness.ctx.orders.market_buy = submit_through_central_service
        try:
            await service.arm_account(config.trading_account)
            service.arm_strategy(config.id)
            await harness.tick("early")
            assert len(gateway.submitted) == 2  # 100,000 reserved in the morning.
            harness.now = at("10:00:00")
            await harness.tick("midday")
            assert len(gateway.submitted) == 3  # Only 50,000 remains for the second flow.
            rows = store.unresolved_orders(strategy_id=config.id)
            assert len(rows) == 3
            assert {row["strategy_id"] for row in rows} == {"shared-flows"}
            assert sum(Decimal(row["request"]["reserved_amount"]) for row in rows) == Decimal("150000")
            assert any("RISK_LIMIT_EXCEEDED" in message for message in harness.logs)
        finally:
            store.close()

    asyncio.run(exercise())
