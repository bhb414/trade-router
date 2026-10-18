from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from trade_router import (
    AccessToken,
    Bar,
    Balance,
    ConnectionTarget,
    ConditionDefinition,
    InstrumentId,
    OrderStatus,
    Position,
    Quote,
    ScheduleKind,
    TokenScopeKey,
    Venue,
)
from trade_router_studio.app import (
    _strategy_performance,
    _strategy_trade_history,
    create_app,
)
from trade_router_studio.block_packages import (
    BAR_OBSERVER_TEMPLATE,
    DEFAULT_CONDITION_BOLLINGER_SETTINGS,
    BlockTradeCondition,
    ConditionBollingerBlockSettings,
    BlockStrategySettings,
    block_package_from_source,
    render_block_strategy,
)
from trade_router_studio.connections import _minute_interval
from trade_router_studio.runtime_scheduler import _bar_interval_seconds
from trade_router_studio import paths as paths_module
from trade_router_studio.paths import (
    BLOCK_EXAMPLE_STRATEGY,
    EXAMPLE_STRATEGY,
    LEGACY_EXAMPLE_STRATEGY,
    PREVIOUS_BLOCK_EXAMPLE_STRATEGY,
    PREVIOUS_EXAMPLE_STRATEGY,
    WorkspacePaths,
)
from trade_router_studio.strategies import (
    MAX_LOG_LINES,
    StrategyManager,
    StrategyNameError,
    StrategyProcess,
)


def workspace(root: Path) -> WorkspacePaths:
    return WorkspacePaths(
        root=root,
        strategies=root / "strategies",
        logs=root / "logs",
        data=root / "data",
        config=root / "config",
    )


def test_workspace_replaces_only_the_obsolete_bundled_example(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "workspace")
    paths.strategies.mkdir(parents=True)
    example = paths.strategies / "example_strategy.py"
    example.write_text(LEGACY_EXAMPLE_STRATEGY, encoding="utf-8")

    paths.ensure()

    source = example.read_text(encoding="utf-8")
    assert "class ExampleStrategy(Strategy):" in source
    assert "waiting for a broker connection" not in source


def test_workspace_preserves_a_user_edited_example(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "workspace")
    paths.strategies.mkdir(parents=True)
    example = paths.strategies / "example_strategy.py"
    source = LEGACY_EXAMPLE_STRATEGY + "\n# my change\n"
    example.write_text(source, encoding="utf-8")

    paths.ensure()

    assert example.read_text(encoding="utf-8") == source


def test_workspace_upgrades_the_previous_bundled_example(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "workspace")
    paths.strategies.mkdir(parents=True)
    example = paths.strategies / "example_strategy.py"
    example.write_text(PREVIOUS_EXAMPLE_STRATEGY, encoding="utf-8")

    paths.ensure()

    assert example.read_text(encoding="utf-8") == EXAMPLE_STRATEGY
    assert 'TRADING_ACCOUNT = "등록한 계좌 별칭"' in EXAMPLE_STRATEGY
    assert 'MARKET_DATA_API = "등록한 API 이름"' in EXAMPLE_STRATEGY


def test_workspace_includes_a_managed_block_example(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "workspace")

    paths.ensure()

    block_example = paths.strategies / "블록 예제.py"
    assert block_example.read_text(encoding="utf-8") == BLOCK_EXAMPLE_STRATEGY
    assert '"type": "block"' in block_example.with_suffix(
        ".strategy.json"
    ).read_text(encoding="utf-8")


def test_workspace_upgrades_only_the_untouched_block_example(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "workspace")
    paths.strategies.mkdir(parents=True)
    block_example = paths.strategies / "블록 예제.py"
    block_example.write_text(PREVIOUS_BLOCK_EXAMPLE_STRATEGY, encoding="utf-8")

    paths.ensure()

    source = block_example.read_text(encoding="utf-8")
    assert source == BLOCK_EXAMPLE_STRATEGY
    assert 'BLOCK_TEMPLATE = "condition-bollinger-v1"' in source


def test_workspace_preserves_a_user_edited_block_example(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "workspace")
    paths.strategies.mkdir(parents=True)
    block_example = paths.strategies / "블록 예제.py"
    source = PREVIOUS_BLOCK_EXAMPLE_STRATEGY + "\n# 사용자 수정\n"
    block_example.write_text(source, encoding="utf-8")

    paths.ensure()

    assert block_example.read_text(encoding="utf-8") == source


def test_managed_block_compiles_generic_comparisons() -> None:
    source = render_block_strategy(DEFAULT_CONDITION_BOLLINGER_SETTINGS)
    namespace: dict[str, object] = {}
    exec(compile(source, "<managed-block>", "exec"), namespace)
    strategy = namespace["ManagedBlockStrategy"]()
    bars = [
        SimpleNamespace(
            open=Decimal(str(100 + index)), high=Decimal(str(102 + index)),
            low=Decimal(str(99 + index)), close=Decimal(str(101 + index)),
            volume=Decimal(str(1_000 + index)),
        )
        for index in range(60)
    ]
    bars_by_request = {("3m", True): bars}
    close_condition = BlockTradeCondition(
        left="close", operator="GT", right="value", value=Decimal("150")
    ).model_dump(mode="json")
    assert strategy._condition_matches(
        close_condition, bars_by_request, None,
    )
    profit_condition = BlockTradeCondition(
        left="profit_rate", operator="GTE", right="value", value=Decimal("3")
    ).model_dump(mode="json")
    assert strategy._condition_matches(
        profit_condition, bars_by_request, Decimal("150"), current_price=Decimal("160"),
    )
    cross_condition = BlockTradeCondition(
        left="close", operator="CROSS_UP", right="bollinger_upper"
    ).model_dump(mode="json")
    requests = strategy._condition_requests(cross_condition)
    assert requests[("3m", True)] == 56


def test_managed_block_compares_heikin_ashi_prices_with_a_stable_warmup() -> None:
    source = render_block_strategy(DEFAULT_CONDITION_BOLLINGER_SETTINGS)
    namespace: dict[str, object] = {}
    exec(compile(source, "<managed-block>", "exec"), namespace)
    strategy = namespace["ManagedBlockStrategy"]()
    bars = [
        SimpleNamespace(
            open=Decimal(str(100 + index)),
            high=Decimal(str(104 + index)),
            low=Decimal(str(98 + index)),
            close=Decimal(str(102 + index)),
            volume=Decimal("1000"),
        )
        for index in range(24)
    ]
    condition = BlockTradeCondition(
        left="heikin_close", operator="GT", right="value", value=Decimal("120")
    ).model_dump(mode="json")

    assert strategy._condition_requests(condition)[("3m", True)] == 24
    assert strategy._condition_matches(condition, {("3m", True): bars}, None) is True
    values = strategy._heikin_ashi_values(bars)
    assert values[0][0] == Decimal("101")
    assert values[-1][3] == Decimal("124")


def test_managed_block_routes_quotes_and_bars_through_the_selected_market() -> None:
    settings = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_copy(
        update={"market_venue": "NXT", "route": "NXT"}
    )
    source = render_block_strategy(settings)
    assert 'MARKET_VENUE = "NXT"' in source
    assert 'ORDER_ROUTE = "NXT"' in source
    assert "ctx.market.quote(code, venue=Venue(MARKET_VENUE))" in source
    assert 'BAR_DATA_MODE = "COMPLETE_ONLY"' in source
    assert "complete_only=key[1]" in source
    assert 'condition[f"{side}_bar_interval"]' in source
    assert "route=ORDER_ROUTE" in source

    package = block_package_from_source("nxt-block", source)
    assert package.settings.market_venue == "NXT"
    assert package.settings.route == "NXT"


def test_managed_block_normalizes_hourly_bars_and_rejects_position_only_buy_metrics() -> None:
    payload = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    sell_condition = payload["sell_condition_groups"][0]["conditions"][2]
    sell_condition["left_bar_interval"] = "1h"
    sell_condition["right_bar_interval"] = "1h"
    sell_condition["right_bollinger_period"] = 20
    normalized = ConditionBollingerBlockSettings.model_validate(payload)
    assert normalized.sell_condition_groups[0].conditions[2].left_bar_interval == "60m"
    assert _minute_interval("1h") == 60
    assert _bar_interval_seconds("1h") == 60 * 60

    payload["buy_condition_groups"] = [{
        "id": "buy-invalid", "bar_interval": "3m", "bar_data_mode": "COMPLETE_ONLY",
        "bollinger_period": 20, "bollinger_stddev": "1.04",
        "conditions": [{
            "left": "profit_rate",
            "operator": "GTE",
            "right": "value",
            "value": "1",
        }],
    }]
    try:
        ConditionBollingerBlockSettings.model_validate(payload)
    except ValueError as exc:
        assert "평균 매수가·수익률" in str(exc)
    else:
        raise AssertionError("Position-only metrics must be rejected in buy conditions.")


def test_managed_block_supports_daily_bars_at_the_sell_start_time() -> None:
    for venue in ("KRX", "NXT", "INTEGRATED"):
        payload = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
        payload["market_venue"] = venue
        sell_condition = payload["sell_condition_groups"][0]["conditions"][2]
        sell_condition["left_bar_interval"] = "1d"
        sell_condition["right_bar_interval"] = "1d"
        normalized = ConditionBollingerBlockSettings.model_validate(payload)
        source = render_block_strategy(normalized)
        assert normalized.sell_condition_groups[0].conditions[2].right_bar_interval == "1d"
        assert "ctx.scheduler.daily_at(" in source
        assert 'schedule_id=f"condition-sell-daily-{index}"' in source
        assert "await self.on_bar(ctx, event)" in source


def test_managed_block_can_include_the_current_forming_bar() -> None:
    payload = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    sell_condition = payload["sell_condition_groups"][0]["conditions"][2]
    sell_condition["left_bar_interval"] = "1d"
    sell_condition["right_bar_interval"] = "1d"
    sell_condition["left_bar_data_mode"] = "INCLUDE_CURRENT"
    sell_condition["right_bar_data_mode"] = "INCLUDE_CURRENT"
    settings = ConditionBollingerBlockSettings.model_validate(payload)
    source = render_block_strategy(settings)

    round_trip = block_package_from_source("forming-bar", source)
    assert round_trip.settings.sell_condition_groups[0].conditions[2].right_bar_data_mode == "INCLUDE_CURRENT"
    assert 'schedule_id="condition-sell-live"' in source
    assert "seconds=BUY_SCAN_SECONDS" in source
    assert 'condition[f"{side}_bar_data_mode"]' in source

    package = block_package_from_source("live-daily", source)
    assert package.settings.sell_condition_groups[0].conditions[2].right_bar_data_mode == "INCLUDE_CURRENT"


def test_managed_block_limits_daily_history_to_one_period_request() -> None:
    payload = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    condition = payload["sell_condition_groups"][0]["conditions"][2]
    condition["right_bar_interval"] = "1d"
    condition["right_bollinger_period"] = 401
    try:
        ConditionBollingerBlockSettings.model_validate(payload)
    except ValueError as exc:
        assert "최대 400봉" in str(exc)
    else:
        raise AssertionError("Daily conditions beyond the period API bound must be rejected.")


def test_managed_block_rejects_history_windows_larger_than_the_adapter_bound() -> None:
    payload = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    condition = payload["sell_condition_groups"][0]["conditions"][2]
    condition["right_bar_interval"] = "30m"
    try:
        ConditionBollingerBlockSettings.model_validate(payload)
    except ValueError as exc:
        assert "1회 조회 범위를 초과" in str(exc)
    else:
        raise AssertionError("An unreachable managed history window must be rejected.")


def test_managed_block_keeps_or_conditions_inside_and_groups_across_timeframes() -> None:
    payload = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    payload["buy_condition_groups"] = [
        {
            "id": "buy-fast",
            "conditions": [
                {
                    **BlockTradeCondition(
                        left="close", operator="GT", right="open",
                        left_bar_interval="1m", left_bar_data_mode="INCLUDE_CURRENT",
                        right_bar_interval="3m", right_bar_data_mode="COMPLETE_ONLY",
                    ).model_dump(mode="json"),
                },
                BlockTradeCondition(
                    left="volume", operator="GT", right="value", value=Decimal("1000"),
                    left_bar_interval="1m", left_bar_data_mode="INCLUDE_CURRENT",
                ).model_dump(mode="json"),
            ],
        },
        {
            "id": "buy-slow",
            "conditions": [
                BlockTradeCondition(
                    left="close", operator="CROSS_UP", right="bollinger_upper",
                    left_bar_interval="3m", right_bar_interval="3m",
                ).model_dump(mode="json"),
            ],
        },
    ]
    settings = ConditionBollingerBlockSettings.model_validate(payload)
    source = render_block_strategy(settings)
    package = block_package_from_source("multi-timeframe", source)

    first = package.settings.buy_condition_groups[0].conditions[0]
    assert (first.left_bar_interval, first.right_bar_interval) == ("1m", "3m")
    assert len(package.settings.buy_condition_groups[0].conditions) == 2
    assert "condition, bars_by_request, average_price, current_price," in source
    assert "for group in groups:" in source


def test_managed_block_only_fetches_data_for_reached_and_groups() -> None:
    source = render_block_strategy(DEFAULT_CONDITION_BOLLINGER_SETTINGS)
    namespace: dict[str, object] = {}
    exec(compile(source, "<managed-block>", "exec"), namespace)
    strategy = namespace["ManagedBlockStrategy"]()
    groups = (
        {
            "id": "first",
            "conditions": (
                BlockTradeCondition(
                    left="close", operator="GT", right="value", value=Decimal("999999"),
                    left_bar_interval="1m",
                ).model_dump(mode="json"),
            ),
        },
        {
            "id": "second",
            "conditions": (
                BlockTradeCondition(
                    left="close", operator="GT", right="bollinger_upper",
                    left_bar_interval="1m", right_bar_interval="1m",
                    right_bollinger_period=55,
                ).model_dump(mode="json"),
            ),
        },
    )

    class Market:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def bars(self, code, interval, **kwargs):
            self.calls.append((interval, kwargs["count"]))
            return [SimpleNamespace(
                open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
                close=Decimal("100"), volume=Decimal("1000"),
            )]

    market = Market()
    context = SimpleNamespace(
        market=market,
        log=SimpleNamespace(warning=lambda message: None),
    )
    passed, _ = asyncio.run(
        strategy._condition_groups_result(context, "005930", groups, side="매수")
    )

    assert passed is False
    assert market.calls == [("1m", 1)]


def test_managed_block_stops_after_the_first_matching_or_condition() -> None:
    source = render_block_strategy(DEFAULT_CONDITION_BOLLINGER_SETTINGS)
    namespace: dict[str, object] = {}
    exec(compile(source, "<managed-block>", "exec"), namespace)
    strategy = namespace["ManagedBlockStrategy"]()
    groups = (
        {
            "id": "or-group",
            "conditions": (
                BlockTradeCondition(
                    left="close", operator="GT", right="value", value=Decimal("0"),
                    left_bar_interval="1m",
                ).model_dump(mode="json"),
                BlockTradeCondition(
                    left="close", operator="GT", right="bollinger_upper",
                    left_bar_interval="1m", right_bar_interval="1m",
                    right_bollinger_period=55,
                ).model_dump(mode="json"),
            ),
        },
    )

    class Market:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def bars(self, code, interval, **kwargs):
            self.calls.append((interval, kwargs["count"]))
            return [SimpleNamespace(
                open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
                close=Decimal("100"), volume=Decimal("1000"),
            )]

    market = Market()
    context = SimpleNamespace(
        market=market,
        log=SimpleNamespace(warning=lambda message: None),
    )
    passed, matched = asyncio.run(
        strategy._condition_groups_result(context, "005930", groups, side="매수")
    )

    assert passed is True
    assert len(matched[0]) == 1
    assert market.calls == [("1m", 1)]


def test_managed_block_does_not_require_bars_for_account_only_conditions() -> None:
    payload = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    payload["buy_condition_groups"] = []
    payload["sell_condition_groups"] = [{
        "id": "account-only",
        "conditions": [BlockTradeCondition(
            left="average_price", operator="GT", right="value", value=Decimal("0")
        ).model_dump(mode="json")],
    }]
    settings = ConditionBollingerBlockSettings.model_validate(payload)
    source = render_block_strategy(settings)
    namespace: dict[str, object] = {}
    exec(compile(source, "<managed-block>", "exec"), namespace)

    assert namespace["NEEDS_BARS"] is False
    assert "bars" not in namespace["ManagedBlockStrategy"].config.required_capabilities["primary"]


def test_managed_block_treats_profit_rate_as_live_account_data() -> None:
    payload = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    payload["buy_condition_groups"] = []
    payload["sell_condition_groups"] = [{
        "id": "profit-only",
        "conditions": [BlockTradeCondition(
            left="profit_rate", operator="GTE", right="value", value=Decimal("3")
        ).model_dump(mode="json")],
    }]
    settings = ConditionBollingerBlockSettings.model_validate(payload)
    source = render_block_strategy(settings)
    namespace: dict[str, object] = {}
    exec(compile(source, "<managed-block>", "exec"), namespace)
    strategy = namespace["ManagedBlockStrategy"]()

    assert namespace["NEEDS_BARS"] is False
    assert "bars" not in strategy.config.required_capabilities["primary"]
    condition = settings.sell_condition_groups[0].conditions[0].model_dump(mode="json")
    assert strategy._condition_requests(condition) == {}
    assert strategy._condition_matches(
        condition, {}, Decimal("100"), current_price=Decimal("103"),
    )


def test_live_profit_exit_still_honors_other_and_condition_groups() -> None:
    payload = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_dump(mode="json")
    payload["sell_condition_groups"] = [
        {
            "id": "profit",
            "conditions": [BlockTradeCondition(
                left="profit_rate", operator="GTE", right="value", value=Decimal("3")
            ).model_dump(mode="json")],
        },
        {
            "id": "bar-confirmation",
            "conditions": [BlockTradeCondition(
                left="close", operator="LT", right="value", value=Decimal("100")
            ).model_dump(mode="json")],
        },
    ]
    source = render_block_strategy(ConditionBollingerBlockSettings.model_validate(payload))
    namespace: dict[str, object] = {}
    exec(compile(source, "<managed-block>", "exec"), namespace)
    strategy = namespace["ManagedBlockStrategy"]()

    class Market:
        def __init__(self):
            self.close = Decimal("101")

        async def bars(self, code, interval, **kwargs):
            return [SimpleNamespace(
                open=self.close, high=self.close, low=self.close,
                close=self.close, volume=Decimal("1000"),
            )]

    market = Market()
    context = SimpleNamespace(
        market=market,
        log=SimpleNamespace(warning=lambda message: None),
    )

    async def evaluate():
        return await strategy._condition_groups_result(
            context,
            "005930",
            namespace["SELL_CONDITION_GROUPS"],
            average_price=Decimal("100"),
            current_price=Decimal("104"),
            side="실시간 매도",
        )

    passed, _ = asyncio.run(evaluate())
    assert passed is False
    market.close = Decimal("99")
    passed, matched = asyncio.run(evaluate())
    assert passed is True
    assert len(matched) == 2


def test_legacy_bar_block_package_still_round_trips() -> None:
    settings = BlockStrategySettings(
        strategy_id="legacy-observer",
        trading_account="account",
        market_source="market",
        symbol="005930",
        bar_interval="3m",
        bar_count=55,
        schedule_id="regular-bars",
        start_time="09:00",
        end_time="15:20",
    )
    source = render_block_strategy(settings, template=BAR_OBSERVER_TEMPLATE)

    package = block_package_from_source("legacy", source)

    assert package.template == BAR_OBSERVER_TEMPLATE
    assert package.settings == settings


def test_workspace_removes_only_the_untouched_personal_bundled_example(
    tmp_path: Path, monkeypatch,
) -> None:
    paths = workspace(tmp_path / "workspace")
    paths.strategies.mkdir(parents=True)
    old_sample = paths.strategies / "kis_condition_bollinger.py"
    bundled_source = "# old bundled personal sample\n"
    old_sample.write_text(bundled_source, encoding="utf-8")
    monkeypatch.setattr(
        paths_module,
        "OBSOLETE_KIS_EXAMPLE_SHA256",
        hashlib.sha256(bundled_source.encode("utf-8")).hexdigest(),
    )

    paths.ensure()

    assert not old_sample.exists()


def test_workspace_preserves_an_edited_personal_strategy(
    tmp_path: Path, monkeypatch,
) -> None:
    paths = workspace(tmp_path / "workspace")
    paths.strategies.mkdir(parents=True)
    strategy = paths.strategies / "kis_condition_bollinger.py"
    strategy.write_text("# user-owned strategy\n", encoding="utf-8")
    monkeypatch.setattr(paths_module, "OBSOLETE_KIS_EXAMPLE_SHA256", "0" * 64)

    paths.ensure()

    assert strategy.read_text(encoding="utf-8") == "# user-owned strategy\n"


def test_removing_last_shared_key_alias_removes_its_encrypted_token(
    tmp_path: Path,
) -> None:
    app = create_app(workspace(tmp_path / "workspace"))
    settings = app.state.settings
    first = settings.add_connection(
        provider="kis",
        target="PAPER",
        label="first",
        app_key="shared-key",
        app_secret="shared-secret",
    )
    second = settings.add_connection(
        provider="kis",
        target="PAPER",
        label="second",
        app_key="shared-key",
        app_secret="shared-secret",
    )
    scope = TokenScopeKey(
        "kis",
        settings.rate_limit_identity(first["id"]),
        ConnectionTarget.PAPER,
    )
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    settings.save_token(
        scope,
        AccessToken("encrypted-token", now + timedelta(hours=1), issued_at=now),
    )

    settings.remove_connection(first["id"])
    assert settings.load_token(scope) is not None
    settings.remove_connection(second["id"])
    assert settings.load_token(scope) is None


def test_workspace_api_creates_and_saves_strategy(tmp_path: Path) -> None:
    app = create_app(workspace(tmp_path / "workspace"))
    process_actions: list[str] = []
    app.state.process_control = process_actions.append
    with TestClient(app) as client:
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.headers["cache-control"] == "no-store, max-age=0"
        static_script = client.get("/static/app.js")
        assert static_script.status_code == 200
        assert static_script.headers["cache-control"] == "no-store, max-age=0"
        assert "사용자 Python 로직" not in static_script.text
        assert "blockPackageFields" in static_script.text
        assert "/api/block-options/conditions" in static_script.text
        assert 'button.addEventListener("pointerdown"' in static_script.text
        assert "buy_condition_groups" in static_script.text
        assert "매수 주문 조건" in static_script.text
        assert "createCalculationEditor" not in static_script.text
        assert "left_bar_interval" in static_script.text
        assert "하이킨아시 종가" in static_script.text
        assert "왼쪽 값 설정" in static_script.text
        assert "오른쪽 값 설정" in static_script.text
        assert "실시간 현재가와 계좌 평균 매수가로 자동 계산합니다." in static_script.text
        assert "선택한 봉의 종가와 평균 매수가로 계산" not in static_script.text
        page = client.get("/")
        chart_script = client.get("/static/chart.js")
        assert chart_script.status_code == 200
        assert "HEIKIN_ASHI" in chart_script.text
        assert 'id="dashboard-chart"' in page.text
        assert 'id="chart-modal"' in page.text
        assert 'id="chart-indicator-modal"' in page.text
        assert 'aria-autocomplete="list"' in page.text
        assert 'id="order-permission-button"' in page.text
        assert 'role="switch"' in page.text
        assert 'name="strategy-type" value="script"' in page.text
        assert 'name="strategy-type" value="block"' in page.text
        assert status.json()["instance_id"]
        assert status.json()["instance_started_at"]
        assert status.json()["strategy_count"] == 2
        assert status.json()["database_recovery"] is None
        assert "status.database_recovery" in static_script.text
        assert not (
            app.state.workspace.strategies / "kis_condition_bollinger.py"
        ).exists()
        block_example = client.get(
            "/api/strategies/%EB%B8%94%EB%A1%9D%20%EC%98%88%EC%A0%9C.py"
        )
        assert block_example.status_code == 200
        assert block_example.json()["strategy_type"] == "block"
        exported = client.get(
            "/api/strategies/%EB%B8%94%EB%A1%9D%20%EC%98%88%EC%A0%9C.py/block-package"
        )
        assert exported.status_code == 200
        package = exported.json()
        assert package["kind"] == "trade-router-block-strategy"
        assert package["template"] == "condition-bollinger-v1"
        assert "source" not in package
        assert package["settings"]["condition_id"] == "1"
        assert package["settings"]["bar_interval"] == "3m"
        assert package["settings"]["total_operating_budget"] == "150000"
        assert package["settings"]["use_take_profit"] is True
        assert package["settings"]["target_source"] == "HTS_CONDITION"
        assert package["settings"]["buy_order_type"] == "MARKET"
        assert len(package["settings"]["sell_conditions"]) == 3
        assert package["settings"]["buy_condition_groups"] == []
        assert len(package["settings"]["sell_condition_groups"]) == 1
        assert package["layout"]["viewport"]["zoom"] == 0.75
        package["layout"]["nodes"]["target"] = {"x": 412.5, "y": 93.0}
        package["layout"]["viewport"] = {"x": 18.0, "y": 27.0, "zoom": 0.2}
        package["settings"]["target_source"] = "RANKING"
        package["settings"]["ranking_kind"] = "TRADING_VALUE"
        package["settings"]["buy_order_type"] = "LIMIT"
        package["settings"]["buy_condition_groups"] = [{
            "id": "buy-1", "bar_interval": "1m", "bar_data_mode": "COMPLETE_ONLY",
            "bollinger_period": 20, "bollinger_stddev": "2",
            "conditions": [{
                "left": "close", "operator": "CROSS_UP", "right": "bollinger_upper",
                "value": "0", "left_offset": 0, "right_offset": 0,
            }],
        }]
        saved_package = client.put(
            "/api/strategies/%EB%B8%94%EB%A1%9D%20%EC%98%88%EC%A0%9C.py/block-package",
            json=package,
        )
        assert saved_package.status_code == 200
        compiled_block = client.get(
            "/api/strategies/%EB%B8%94%EB%A1%9D%20%EC%98%88%EC%A0%9C.py"
        ).json()["source"]
        assert 'TARGET_SOURCE = "RANKING"' in compiled_block
        assert 'RANKING_KIND = "TRADING_VALUE"' in compiled_block
        assert 'BUY_ORDER_TYPE = "LIMIT"' in compiled_block
        assert '"CROSS_UP"' in compiled_block
        compiled_package = block_package_from_source("compiled", compiled_block)
        compiled_condition = compiled_package.settings.buy_condition_groups[0].conditions[0]
        assert compiled_condition.left_bar_interval == "1m"
        assert compiled_condition.right_bollinger_period == 20
        compile(compiled_block, "<managed-block>", "exec")
        assert "BLOCK_LAYOUT_JSON" in compiled_block
        saved_layout = client.get(
            "/api/strategies/%EB%B8%94%EB%A1%9D%20%EC%98%88%EC%A0%9C.py/block-package"
        ).json()["layout"]
        assert saved_layout["nodes"]["target"] == {"x": 412.5, "y": 93.0}
        assert saved_layout["viewport"]["zoom"] == 0.2
        package["name"] = "가져온 블록 예제"
        imported = client.post("/api/strategies/import-block", json=package)
        assert imported.status_code == 200
        assert imported.json()["strategy_type"] == "block"
        imported_source = client.get(
            "/api/strategies/%EA%B0%80%EC%A0%B8%EC%98%A8%20%EB%B8%94%EB%A1%9D%20%EC%98%88%EC%A0%9C.py"
        )
        assert imported_source.status_code == 200
        assert "ManagedBlockStrategy" in imported_source.json()["source"]
        assert "ctx.discovery.matches" in imported_source.json()["source"]
        assert "ctx.orders.market_buy" in imported_source.json()["source"]
        assert "ctx.orders.market_sell" in imported_source.json()["source"]
        package["source"] = "print('hidden code')"
        rejected_hidden_source = client.post(
            "/api/strategies/import-block",
            json=package,
        )
        assert rejected_hidden_source.status_code == 422
        assert status.json()["unresolved_orders"] == 0
        doctor = client.get("/api/system/doctor")
        assert doctor.status_code == 200
        assert doctor.json()["order_submission_enabled"] is False

        created = client.put(
            "/api/strategies/my_strategy.py",
            json={"source": "print('ready')\n"},
        )
        assert created.status_code == 200
        loaded = client.get("/api/strategies/my_strategy.py")
        assert loaded.json()["source"] == "print('ready')\n"
        assert loaded.json()["strategy_type"] == "script"

        block_created = client.put(
            "/api/strategies/my_blocks.py",
            json={
                "source": "print('must be removed')\n" + block_example.json()["source"],
                "strategy_type": "block",
            },
        )
        assert block_created.status_code == 200
        assert block_created.json()["strategy_type"] == "block"
        block_loaded = client.get("/api/strategies/my_blocks.py")
        assert block_loaded.json()["strategy_type"] == "block"
        assert "must be removed" not in block_loaded.json()["source"]
        assert "ManagedBlockStrategy" in block_loaded.json()["source"]
        assert (
            app.state.workspace.strategies / "my_blocks.strategy.json"
        ).is_file()
        type_change = client.put(
            "/api/strategies/my_blocks.py",
            json={"source": "print('changed')\n", "strategy_type": "script"},
        )
        assert type_change.status_code == 409
        deleted = client.delete("/api/strategies/my_blocks.py")
        assert deleted.status_code == 204
        assert not (
            app.state.workspace.strategies / "my_blocks.strategy.json"
        ).exists()

        restart = client.post("/api/system/restart")
        shutdown = client.post("/api/system/shutdown")
        assert restart.status_code == 200
        assert shutdown.status_code == 200
        assert process_actions == ["restart", "shutdown"]


def test_managed_block_strategy_recovers_a_late_filled_position() -> None:
    source = render_block_strategy(DEFAULT_CONDITION_BOLLINGER_SETTINGS)
    namespace: dict[str, object] = {}
    exec(compile(source, "<managed-block>", "exec"), namespace)
    strategy = namespace["ManagedBlockStrategy"]()

    values: dict[str, object] = {
        "ownership_initialized": True,
        "strategy_positions": {},
    }

    class State:
        def get(self, key: str, default=None):
            return values.get(key, default)

        def set(self, key: str, value) -> None:
            values[key] = value

    position = SimpleNamespace(
        instrument=SimpleNamespace(code="079650"),
        quantity=Decimal("5"),
        strategy_quantity=Decimal("5"),
    )
    tracked = strategy._sync_tracked_positions(
        SimpleNamespace(state=State()),
        SimpleNamespace(positions=(position,)),
    )

    assert tracked == {"079650": "5"}
    assert values["strategy_positions"] == {"079650": "5"}


def test_managed_block_strategy_uses_realtime_quote_for_take_profit() -> None:
    source = render_block_strategy(DEFAULT_CONDITION_BOLLINGER_SETTINGS)
    namespace: dict[str, object] = {}
    exec(compile(source, "<managed-block>", "exec"), namespace)
    assert namespace["ScheduleKind"] is ScheduleKind
    strategy = namespace["ManagedBlockStrategy"]()

    values: dict[str, object] = {
        "ownership_initialized": True,
        "strategy_positions": {"079650": "5"},
    }

    class State:
        def get(self, key: str, default=None):
            return values.get(key, default)

        def set(self, key: str, value) -> None:
            values[key] = value

    position = SimpleNamespace(
        instrument=SimpleNamespace(code="079650"),
        quantity=Decimal("5"),
        available_quantity=Decimal("5"),
        average_price=Decimal("7590"),
        strategy_quantity=Decimal("5"),
    )
    balance = SimpleNamespace(positions=(position,))

    class Account:
        async def balance(self):
            return balance

        async def open_orders(self):
            return ()

    class Market:
        async def subscribe_quotes(self, code, *, venue):
            return ({"id": f"quote-{code}"},)

        async def unsubscribe(self, subscription_id):
            return None

    submitted: list[dict[str, object]] = []

    class Orders:
        async def market_sell(self, **kwargs):
            submitted.append(kwargs)
            return SimpleNamespace(status=OrderStatus.ACKNOWLEDGED)

    logger = SimpleNamespace(
        info=lambda message: None,
        warning=lambda message: None,
        error=lambda message: None,
    )
    ctx = SimpleNamespace(
        state=State(),
        account=Account(),
        market=Market(),
        orders=Orders(),
        log=logger,
    )

    async def exercise() -> None:
        await strategy._refresh_live_positions(ctx, balance, ())
        await strategy.on_quote(
            ctx,
            SimpleNamespace(
                instrument=SimpleNamespace(code="079650"),
                price=Decimal("8000"),
                delayed=False,
                stale=False,
                received_at=datetime(
                    2026, 10, 16, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")
                ),
            ),
        )

    asyncio.run(exercise())

    assert submitted == [
        {
            "code": "079650",
            "quantity": Decimal("5"),
            "route": "KRX",
            "client_order_id": "live-exit-079650-20261016100000000000-sell",
        }
    ]


def test_managed_block_strategy_logs_condition_matches_by_name() -> None:
    source = render_block_strategy(DEFAULT_CONDITION_BOLLINGER_SETTINGS)
    namespace: dict[str, object] = {}
    exec(compile(source, "<managed-block>", "exec"), namespace)
    strategy = namespace["ManagedBlockStrategy"]()
    values: dict[str, object] = {}
    messages: list[str] = []

    class State:
        def get(self, key: str, default=None):
            return values.get(key, default)

        def set(self, key: str, value) -> None:
            values[key] = value

    class Discovery:
        async def matches(self, user_id: str, condition_id: str):
            return (
                SimpleNamespace(
                    instrument=SimpleNamespace(code="048910"),
                    name="대원미디어",
                ),
            )

    ctx = SimpleNamespace(
        state=State(),
        discovery=Discovery(),
        log=SimpleNamespace(info=messages.append),
    )

    codes = asyncio.run(strategy._candidate_codes(ctx))

    assert codes == ["048910"]
    assert values["instrument_names"] == {"048910": "대원미디어"}
    assert messages == [
        "HTS 조건검색 1 1종목 [대원미디어] · 신규 매수 후보를 확인합니다."
    ]
    assert strategy._instrument_label(ctx, "048910") == "대원미디어"


def test_status_reports_automatic_runtime_database_recovery(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "workspace")
    paths.data.mkdir(parents=True)
    (paths.data / "runtime.sqlite3").write_bytes(b"not a sqlite database")
    app = create_app(paths)

    with TestClient(app) as client:
        response = client.get("/api/status")

    assert response.status_code == 200
    recovery = response.json()["database_recovery"]
    assert recovery["message"].startswith("실행 기록 DB 손상을 자동 복구했습니다.")
    assert Path(recovery["backup_path"], "runtime.sqlite3").is_file()


def test_block_editor_condition_options_use_connection_runtime(tmp_path: Path) -> None:
    app = create_app(workspace(tmp_path / "workspace"))
    calls: list[tuple[str, str]] = []

    async def conditions(source: str, user_id: str, *, source_alias=None):
        calls.append((source, user_id))
        return (
            ConditionDefinition(
                id="3",
                name="bb+macd 상향돌파",
                provider="kis",
                source_alias=source_alias,
            ),
        )

    app.state.connections.condition_definitions = conditions
    with TestClient(app) as client:
        response = client.get(
            "/api/block-options/conditions",
            params={"source": "kis-live", "user_id": "tester"},
        )

    assert response.status_code == 200
    assert response.json()["conditions"] == [
        {
            "id": "3",
            "name": "bb+macd 상향돌파",
            "provider": "kis",
            "source_alias": None,
        }
    ]
    assert calls == [("kis-live", "tester")]


def test_chart_api_uses_an_eligible_connection_and_normalizes_market_data(
    tmp_path: Path,
) -> None:
    app = create_app(workspace(tmp_path / "workspace"))
    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(microsecond=0)
    with TestClient(app) as client:
        connection = client.post(
            "/api/settings/connections",
            json={
                "provider": "kis",
                "target": "PAPER",
                "label": "chart-source",
                "app_key": "chart-key",
                "app_secret": "chart-secret",
            },
        ).json()

        async def bars(source, code, interval, **kwargs):
            assert source == connection["id"]
            assert code == "005930"
            assert interval == "3m"
            assert kwargs["venue"] is Venue.KRX
            return (
                Bar(
                    instrument=InstrumentId(code), interval=interval,
                    open_time=now - timedelta(minutes=3), close_time=now,
                    open=Decimal("70000"), high=Decimal("70500"),
                    low=Decimal("69800"), close=Decimal("70400"),
                    volume=Decimal("12345"), venue=Venue.KRX,
                    provider="kis", correlation_id="bar-1",
                    source_alias=kwargs.get("source_alias"), received_at=now,
                ),
            )

        async def quote(source, code, venue, **kwargs):
            assert source == connection["id"]
            return Quote(
                instrument=InstrumentId(code), price=Decimal("70400"),
                venue=venue, provider="kis", market_time=now,
                received_at=now, correlation_id="quote-1",
                source_alias=kwargs.get("source_alias"),
            )

        async def instrument_name(source, code):
            assert source == connection["id"]
            return "삼성전자" if code == "005930" else None

        app.state.connections.bars = bars
        app.state.connections.quote = quote
        app.state.connections.instrument_name = instrument_name

        class ChartCatalog:
            async def search(self, query, *, limit=20):
                if "삼성" in query:
                    return [
                        {"code": "005930", "name": "삼성전자", "market": "KOSPI"}
                    ][:limit]
                return []

            async def name(self, code):
                return None

        app.state.instrument_catalog = ChartCatalog()

        sources = client.get("/api/chart/sources").json()
        assert sources["automatic"] == connection["id"]
        assert sources["sources"][0]["realtime"] is True

        response = client.get(
            "/api/chart/data",
            params={
                "code": "005930", "interval": "3m", "venue": "KRX",
                "source": "AUTO", "count": 20,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["source"]["id"] == connection["id"]
        assert payload["instrument"] == {"code": "005930", "name": "삼성전자"}
        assert payload["bars"][0]["close"] == "70400"
        assert payload["quote"]["price"] == "70400"

        search = client.get("/api/chart/instruments", params={"q": "삼성"}).json()
        assert search["instruments"][0] == {"code": "005930", "name": "삼성전자"}


def test_dashboard_reports_balances_profit_and_limit_usage(tmp_path: Path) -> None:
    app = create_app(workspace(tmp_path / "workspace"))
    name_lookups: list[tuple[str, str]] = []

    async def balance(_account: str) -> Balance:
        return Balance(
            cash=Decimal("25000"),
            withdrawable_cash=Decimal("25000"),
            total_value=Decimal("125000"),
            positions=(
                Position(
                    instrument=InstrumentId("005930"),
                    quantity=Decimal("10"),
                    available_quantity=Decimal("10"),
                    average_price=Decimal("9000"),
                    market_price=Decimal("10000"),
                ),
            ),
            as_of=datetime.now(ZoneInfo("Asia/Seoul")),
        )

    app.state.connections.balance = balance

    async def instrument_name(connection: str, code: str) -> str | None:
        name_lookups.append((connection, code))
        return "삼성전자" if code == "005930" else None

    app.state.connections.instrument_name = instrument_name
    with TestClient(app) as client:
        connection = client.post(
            "/api/settings/connections",
            json={
                "provider": "kis",
                "target": "PAPER",
                "label": "dashboard-source",
                "app_key": "dashboard-key",
                "app_secret": "dashboard-secret",
            },
        ).json()
        client.post(
            "/api/settings/accounts",
            json={
                "alias": "dashboard-account",
                "connection_id": connection["id"],
                "account_number": "12345678",
                "product_code": "01",
                "max_daily_buy_amount": "100000",
                "max_daily_buy_count": 4,
                "max_exposure": "200000",
                "max_open_orders": 3,
            },
        )
        buy, _ = app.state.runtime_store.reserve_order(
            strategy_id="dashboard-strategy",
            account_alias="dashboard-account",
            client_order_id="dashboard-buy",
            request={
                "code": "005930",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": "1",
            },
        )
        app.state.runtime_store.update_order(
            buy["id"], status=OrderStatus.ACKNOWLEDGED
        )
        app.state.runtime_store.update_order(
            buy["id"],
            status=OrderStatus.FILLED,
            filled_quantity="1",
            average_fill_price="9000",
        )
        sell, _ = app.state.runtime_store.reserve_order(
            strategy_id="dashboard-strategy",
            account_alias="dashboard-account",
            client_order_id="dashboard-sell",
            request={
                "code": "005930",
                "side": "SELL",
                "order_type": "MARKET",
                "quantity": "1",
            },
        )
        app.state.runtime_store.update_order(
            sell["id"], status=OrderStatus.ACKNOWLEDGED
        )
        app.state.runtime_store.update_order(
            sell["id"],
            status=OrderStatus.FILLED,
            filled_quantity="1",
            average_fill_price="10000",
        )

        payload = client.get("/api/dashboard?refresh=true").json()

    assert payload["summary"]["total_assets"] == "125000"
    assert payload["summary"]["cash"] == "25000"
    assert payload["summary"]["unrealized_profit"] == "10000"
    assert payload["summary"]["live_assets"] == "0"
    assert payload["summary"]["paper_assets"] == "125000"
    assert payload["summary"]["paper_unrealized_profit"] == "10000"
    assert payload["summary"]["account_count"] == 1
    assert payload["summary"]["valued_accounts"] == 1
    assert payload["summary"]["unresolved_orders"] == 0
    account = payload["accounts"][0]
    assert account["state"] == "ok"
    assert account["position_count"] == 1
    assert account["positions"][0]["available_quantity"] == "10"
    assert account["positions"][0]["name"] == "삼성전자"
    assert account["positions"][0]["market_value"] == "100000"
    assert account["positions"][0]["unrealized_profit"] == "10000"
    assert account["positions"][0]["return_percent"] == "11.11"
    assert account["trade_history"][0]["name"] == "삼성전자"
    assert account["trade_history"][0]["realized_profit"] == "1000"
    assert account["trade_performance"]["realized_profit"] == "1000"
    assert name_lookups == [(connection["id"], "005930")]
    assert app.state.runtime_store.instrument_names()["005930"] == "삼성전자"
    assert account["limits"]["max_daily_buy_count"]["used"] == "1"
    assert account["limits"]["max_daily_buy_count"]["limit"] == "4"
    assert account["limits"]["max_exposure"]["used"] == "100000"
    assert account["limits"]["max_exposure"]["percent"] == 50.0
    assert payload["services"][0]["state"] == "connected"
    results = {item["id"]: item for item in payload["service_results"]}
    assert results["account"]["snapshot_success"] == 1
    assert results["account"]["total"] == 1
    assert results["account"]["success"] == 1
    assert results["router"]["state"] == "ok"
    assert "key-" not in str(payload)


def test_strategy_performance_uses_router_owned_fills_and_current_price() -> None:
    rows = [
        {
            "created_at": "2026-10-15T09:00:00+09:00",
            "request": {"code": "005930", "side": "BUY"},
            "filled_quantity": "2",
            "average_fill_price": "100",
        },
        {
            "created_at": "2026-10-15T10:00:00+09:00",
            "request": {"code": "005930", "side": "SELL"},
            "filled_quantity": "1",
            "average_fill_price": "120",
        },
    ]
    account = {
        "positions": [
            {"code": "005930", "market_price": "150", "average_price": "100"}
        ]
    }

    result = _strategy_performance(rows, account)

    assert result["realized_profit"] == "20"
    assert result["unrealized_profit"] == "50"
    assert result["estimated_profit"] == "70"
    assert result["estimated_return_percent"] == "35.00"
    assert result["matched_sales"] == 1
    assert result["winning_sales"] == 1
    assert result["positions"] == [
        {
            "code": "005930",
            "name": None,
            "quantity": "1",
            "available_quantity": "1",
            "average_price": "100",
            "market_price": "150",
            "cost_basis": "100",
            "market_value": "150",
            "unrealized_profit": "50",
            "return_percent": "50.00",
        }
    ]
    assert result["includes_fees"] is False


def test_strategy_trade_history_includes_names_and_matched_sale_profit() -> None:
    rows = [
        {
            "id": "sell",
            "created_at": "2026-10-15T10:00:00+09:00",
            "updated_at": "2026-10-15T10:00:01+09:00",
            "request": {
                "code": "005930",
                "side": "SELL",
                "order_type": "MARKET",
                "quantity": "1",
            },
            "filled_quantity": "1",
            "average_fill_price": "120",
            "commission": "1",
            "tax": "2",
            "costs_complete": True,
            "status": "FILLED",
        },
        {
            "id": "buy",
            "created_at": "2026-10-15T09:00:00+09:00",
            "updated_at": "2026-10-15T09:00:01+09:00",
            "request": {
                "code": "005930",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": "1",
            },
            "filled_quantity": "1",
            "average_fill_price": "100",
            "commission": "1",
            "tax": "0",
            "costs_complete": True,
            "status": "FILLED",
        },
    ]

    history = _strategy_trade_history(
        rows,
        instrument_names={"005930": "삼성전자"},
    )

    assert history[0]["name"] == "삼성전자"
    assert history[0]["realized_profit"] == "16"
    assert history[0]["return_percent"] == "15.84"
    assert history[0]["profit_costs_complete"] is True
    assert history[1]["realized_profit"] is None


def test_strategy_performance_subtracts_commission_and_tax() -> None:
    rows = [
        {
            "created_at": "2026-10-15T09:00:00+09:00",
            "request": {"code": "005930", "side": "BUY"},
            "filled_quantity": "1",
            "average_fill_price": "100",
            "commission": "1",
            "tax": "0",
            "costs_complete": True,
        },
        {
            "created_at": "2026-10-15T10:00:00+09:00",
            "request": {"code": "005930", "side": "SELL"},
            "filled_quantity": "1",
            "average_fill_price": "120",
            "commission": "1",
            "tax": "2",
            "costs_complete": True,
        },
    ]
    result = _strategy_performance(rows, {"positions": []})
    assert result["realized_profit"] == "16"
    assert result["commission"] == "2"
    assert result["tax"] == "2"
    assert result["includes_fees"] is True


def test_stopped_strategy_summary_reads_config_without_executing_it(
    tmp_path: Path,
) -> None:
    app = create_app(workspace(tmp_path / "workspace"))

    async def balance(_account: str) -> Balance:
        return Balance(
            cash=Decimal("500000"),
            withdrawable_cash=Decimal("500000"),
            total_value=Decimal("500000"),
            positions=(),
            as_of=datetime.now(ZoneInfo("Asia/Seoul")),
        )

    app.state.connections.balance = balance
    with TestClient(app) as client:
        connection = client.post(
            "/api/settings/connections",
            json={
                "provider": "kis",
                "target": "PAPER",
                "label": "summary-source",
                "app_key": "summary-key",
                "app_secret": "summary-secret",
            },
        ).json()
        client.post(
            "/api/settings/accounts",
            json={
                "alias": "summary-account",
                "connection_id": connection["id"],
                "account_number": "12345678",
                "product_code": "01",
            },
        )
        source = '''from decimal import Decimal
from trade_router import RiskLimits, Strategy, StrategyConfig

raise RuntimeError("must not execute")

ACCOUNT = "summary-account"
SOURCE = "summary-source"
MAX_ORDER = Decimal("50000")

class SummaryStrategy(Strategy):
    config = StrategyConfig(
        id="summary-strategy",
        trading_account=ACCOUNT,
        market_sources={"primary": SOURCE},
        limits=RiskLimits(
            max_order_amount=MAX_ORDER,
            max_daily_buy_amount=Decimal("150000"),
            max_open_orders=2,
        ),
    )
'''
        client.put("/api/strategies/summary.py", json={"source": source})

        payload = client.get("/api/strategies/summary.py/summary").json()

    assert payload["strategy_id"] == "summary-strategy"
    assert payload["trading_account"] == "summary-account"
    assert payload["market_sources"] == {"primary": "summary-source"}
    assert payload["running"] is False
    assert payload["limits"]["max_order_amount"]["limit"] == "50000"
    assert payload["limits"]["max_daily_buy_amount"]["limit"] == "150000"
    assert payload["limits"]["max_open_orders"]["limit"] == "2"
    assert payload["performance"] == {
        "estimated_profit": "0",
        "estimated_return_percent": None,
        "realized_profit": "0",
        "unrealized_profit": "0",
        "filled_buy_amount": "0",
        "filled_sell_amount": "0",
        "matched_sales": 0,
        "winning_sales": 0,
        "win_rate": None,
        "unpriced_codes": [],
        "positions": [],
        "includes_fees": False,
        "commission": "0",
        "tax": "0",
        "total_costs": "0",
        "costs_complete": False,
    }
    assert [item["label"] for item in payload["service_results"]] == [
        "시세 데이터",
        "스케줄러",
        "계좌 조회",
        "주문 처리",
    ]


def test_settings_api_keeps_secrets_out_of_responses(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "workspace")
    app = create_app(paths)
    with TestClient(app) as client:
        connection = client.post(
            "/api/settings/connections",
            json={
                "provider": "kis",
                "target": "PAPER",
                "label": "KIS main",
                "app_key": "test-app-key",
                "app_secret": "test-app-secret",
            },
        )
        assert connection.status_code == 201
        connection_payload = connection.json()
        assert connection_payload["target"] == "PAPER"
        assert connection_payload["rest_rate_limit"]["requests_per_second"] == 1
        assert connection_payload["rest_rate_limit"]["minimum_interval_ms"] == 1000
        assert (
            connection_payload["websocket_rate_limit"][
                "max_subscriptions_per_session"
            ]
            == 41
        )
        assert (
            connection_payload["websocket_rate_limit"][
                "control_messages_per_second"
            ]
            == 1
        )
        assert "app_key" not in connection_payload
        assert "app_secret" not in connection_payload

        account = client.post(
            "/api/settings/accounts",
            json={
                "alias": "main account",
                "connection_id": connection_payload["id"],
                "account_number": "12345678",
                "product_code": "01",
                "max_order_amount": "500000",
                "max_daily_buy_amount": "1000000",
                "max_daily_buy_count": 7,
                "max_open_orders": 3,
            },
        )
        assert account.status_code == 201
        assert account.json()["masked_account"] == "****5678"
        assert account.json()["risk_limits"]["max_order_amount"] == "500000"
        assert account.json()["risk_limits"]["max_daily_buy_count"] == 7
        assert account.json()["risk_limits"]["max_open_orders"] == 3
        assert "account_number" not in account.json()

        stored_text = (paths.config / "settings.json").read_text(encoding="utf-8")
        stored_secrets = (paths.config / "secrets.dpapi").read_bytes()
        assert "test-app-secret" not in stored_text
        assert b"test-app-secret" not in stored_secrets

        status = client.get("/api/status").json()
        assert status["registered_apis"] == 1
        assert status["registered_accounts"] == 1

        providers = client.get("/api/settings/providers").json()
        kis = next(item for item in providers if item["id"] == "kis")
        live = next(item for item in kis["targets"] if item["id"] == "LIVE")
        assert live["rest_rate_limit"]["requests_per_second"] == 18
        assert live["websocket_rate_limit"]["control_messages_per_second"] == 1
        namuh = next(item for item in providers if item["id"] == "namuh")
        assert namuh["targets"][0]["rest_rate_limit"]["requests_per_second"] == 4
        assert (
            namuh["targets"][0]["websocket_rate_limit"][
                "operating_max_sessions_per_key"
            ]
            == 2
        )


def test_connection_registration_can_check_authentication_immediately(
    tmp_path: Path,
) -> None:
    paths = workspace(tmp_path / "workspace")
    app = create_app(paths)

    async def connected(connection_id: str) -> dict[str, object]:
        return {
            "connection_id": connection_id,
            "state": "connected",
            "expires_at": "2026-10-16T09:00:00+09:00",
            "message": "접근 토큰을 확인했습니다.",
        }

    app.state.connections.check = connected
    with TestClient(app) as client:
        response = client.post(
            "/api/settings/connections?check=true",
            json={
                "provider": "kis",
                "target": "PAPER",
                "label": "checked immediately",
                "app_key": "test-app-key",
                "app_secret": "test-app-secret",
            },
        )

    assert response.status_code == 201
    assert response.json()["connection_status"]["state"] == "connected"


def test_connection_edit_never_returns_or_replaces_api_secrets(
    tmp_path: Path,
) -> None:
    app = create_app(workspace(tmp_path / "workspace"))
    with TestClient(app) as client:
        connection = client.post(
            "/api/settings/connections",
            json={
                "provider": "kis",
                "target": "PAPER",
                "label": "before",
                "app_key": "private-app-key",
                "app_secret": "private-app-secret",
                "realtime_user_id": "old-hts",
            },
        ).json()
        edited = client.put(
            f"/api/settings/connections/{connection['id']}",
            json={
                "provider": "namuh",
                "target": "LIVE",
                "label": "after",
                "realtime_user_id": "new-hts",
            },
        )
        listed = client.get("/api/settings/connections").json()

    assert edited.status_code == 200
    assert edited.json()["label"] == "after"
    assert edited.json()["provider"] == "namuh"
    assert edited.json()["target"] == "LIVE"
    assert edited.json()["has_realtime_user_id"] is True
    assert edited.json()["realtime_user_id"] == "new-hts"
    assert "app_key" not in edited.json()
    assert "app_secret" not in edited.json()
    assert "app_key" not in listed[0]
    assert "app_secret" not in listed[0]
    assert app.state.settings.get_credentials(connection["id"]) == {
        "app_key": "private-app-key",
        "app_secret": "private-app-secret",
        "realtime_user_id": "new-hts",
    }


def test_kis_account_accepts_grouped_number_and_edit_preserves_identity(
    tmp_path: Path,
) -> None:
    app = create_app(workspace(tmp_path / "workspace"))
    with TestClient(app) as client:
        connection = client.post(
            "/api/settings/connections",
            json={
                "provider": "kis",
                "target": "PAPER",
                "label": "account-edit-source",
                "app_key": "key",
                "app_secret": "secret",
            },
        ).json()
        replacement = client.post(
            "/api/settings/connections",
            json={
                "provider": "kis",
                "target": "LIVE",
                "label": "account-edit-replacement",
                "app_key": "replacement-key",
                "app_secret": "replacement-secret",
            },
        ).json()
        created = client.post(
            "/api/settings/accounts",
            json={
                "alias": "paper-main",
                "connection_id": connection["id"],
                "account_number": "12345678-01",
            },
        )
        edited = client.put(
            f"/api/settings/accounts/{created.json()['id']}",
            json={
                "alias": "live-main",
                "connection_id": replacement["id"],
                "max_exposure": "150000",
                "sell_tax_rate": "0.0015",
            },
        )

    assert created.status_code == 201
    assert created.json()["product_code"] == "01"
    assert app.state.settings.get_account_number(created.json()["id"]) == "12345678"
    assert edited.status_code == 200
    assert edited.json()["alias"] == "live-main"
    assert edited.json()["connection_id"] == replacement["id"]
    assert app.state.settings.get_account("paper-main")["alias"] == "live-main"
    assert edited.json()["risk_limits"]["max_exposure"] == "150000"
    assert edited.json()["cost_rates"]["sell_tax_rate"] == "0.0015"
    assert app.state.settings.get_account_number(created.json()["id"]) == "12345678"


def test_namuh_account_accepts_three_two_six_grouped_number(tmp_path: Path) -> None:
    app = create_app(workspace(tmp_path / "workspace"))
    with TestClient(app) as client:
        connection = client.post(
            "/api/settings/connections",
            json={
                "provider": "namuh",
                "target": "LIVE",
                "label": "namuh-account-source",
                "app_key": "key",
                "app_secret": "secret",
            },
        ).json()
        created = client.post(
            "/api/settings/accounts",
            json={
                "alias": "namuh-main",
                "connection_id": connection["id"],
                "account_number": "000-00-000000",
            },
        )

    assert created.status_code == 201
    assert created.json()["formatted_masked_account"] == "00*-**-**0000"
    assert app.state.settings.get_account_number(created.json()["id"]) == "00000000000"


def test_strategy_order_toggle_is_available_while_stopped_and_persists(
    tmp_path: Path,
) -> None:
    paths = workspace(tmp_path / "workspace")
    app = create_app(paths)
    with TestClient(app) as client:
        initial = client.get(
            "/api/strategies/example_strategy.py/order-permission"
        ).json()
        assert initial == {
            "available": True,
            "armed": False,
            "strategy_id": None,
            "running": False,
        }

        armed = client.post(
            "/api/strategies/example_strategy.py/arm-orders"
        )
        assert armed.status_code == 200
        assert armed.json()["armed"] is True
        assert client.get(
            "/api/strategies/example_strategy.py/order-permission"
        ).json()["armed"] is True

        connection = client.post(
            "/api/settings/connections",
            json={
                "provider": "kis",
                "target": "PAPER",
                "label": "toggle-source",
                "app_key": "toggle-key",
                "app_secret": "toggle-secret",
            },
        ).json()
        client.post(
            "/api/settings/accounts",
            json={
                "alias": "toggle-account",
                "connection_id": connection["id"],
                "account_number": "12345678",
                "product_code": "01",
            },
        )
        token = app.state.sessions.issue("example_strategy.py")
        registered = client.post(
            "/api/v1/strategy/manifest",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "strategy_id": "stopped-toggle-strategy",
                "trading_account": "toggle-account",
                "market_sources": {"primary": "toggle-source"},
            },
        )
        assert registered.status_code == 200
        runtime_permissions = client.get("/api/runtime/order-permissions").json()
        actual = next(
            item
            for item in runtime_permissions
            if item["scope_type"] == "strategy"
            and item["scope_id"] == "stopped-toggle-strategy"
        )
        assert actual["armed"] is True

    restarted = create_app(paths)
    with TestClient(restarted) as client:
        restored = client.get(
            "/api/strategies/example_strategy.py/order-permission"
        ).json()
        assert restored["available"] is True
        assert restored["armed"] is True
        assert restored["running"] is False
        disarmed = client.post(
            "/api/strategies/example_strategy.py/disarm-orders"
        )
        assert disarmed.status_code == 200
        assert disarmed.json()["armed"] is False


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
        assert any(
            entry["message"] == "hello from strategy"
            and entry["level"] == "output"
            and entry["timestamp"]
            for entry in logs["entries"]
        )
        assert logs["exit_code"] == 0

    asyncio.run(run())


def test_strategy_process_decodes_korean_output(tmp_path: Path) -> None:
    async def run() -> None:
        paths = workspace(tmp_path / "workspace")
        paths.ensure()
        manager = StrategyManager(paths)
        await manager.save("korean.py", "print('전략 연결 확인', flush=True)\n")
        await manager.start("korean.py")
        for _ in range(40):
            if not manager.details("korean.py")["running"]:
                break
            await asyncio.sleep(0.025)
        logs = manager.logs("korean.py")
        assert "전략 연결 확인" in logs["lines"]

    asyncio.run(run())


def test_strategy_log_cursor_remains_monotonic_after_ring_buffer_rollover(
    tmp_path: Path,
) -> None:
    paths = workspace(tmp_path / "workspace")
    paths.ensure()
    manager = StrategyManager(paths)
    state = StrategyProcess(name="example_strategy.py")
    manager._states[state.name] = state

    for index in range(MAX_LOG_LINES):
        manager._append_log(state, "output", f"old-{index}")
    cursor = manager.logs(state.name)["next"]
    manager._append_log(state, "output", "new-after-clear")

    result = manager.logs(state.name, cursor)
    assert result["lines"] == ["new-after-clear"]
    assert result["next"] == cursor + 1


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
            "사용자 요청으로 전략을 중지했습니다." in line
            for line in manager.logs("running.py")["lines"]
        )

    asyncio.run(run())


def test_requested_restart_restores_only_running_strategies(tmp_path: Path) -> None:
    async def run() -> None:
        paths = workspace(tmp_path / "workspace")
        paths.ensure()
        manager = StrategyManager(paths)
        source = "import time\nprint('ready', flush=True)\ntime.sleep(30)\n"
        await manager.save("running.py", source)
        await manager.save("stopped.py", source)
        await manager.start("running.py")

        assert manager.checkpoint_running_for_restart() == ["running.py"]
        await manager.shutdown()

        restarted = StrategyManager(paths)
        names = restarted.consume_restart_checkpoint()
        assert names == ["running.py"]
        assert restarted.consume_restart_checkpoint() == []
        for name in names:
            await restarted.start(name)
        assert restarted.details("running.py")["running"] is True
        assert restarted.details("stopped.py")["running"] is False
        await restarted.shutdown()

    asyncio.run(run())


def test_app_restart_restores_the_previous_running_process(tmp_path: Path) -> None:
    paths = workspace(tmp_path / "workspace")
    app = create_app(paths)
    app.state.process_control = lambda _action: None
    with TestClient(app) as client:
        created = client.put(
            "/api/strategies/restart-me.py",
            json={"source": "import time\ntime.sleep(30)\n"},
        )
        assert created.status_code == 200
        assert client.post("/api/strategies/restart-me.py/start").status_code == 200
        assert client.post("/api/system/restart").status_code == 200

    restarted = create_app(paths)
    with TestClient(restarted) as client:
        for _ in range(30):
            details = client.get("/api/strategies/restart-me.py").json()
            if details["running"]:
                break
            time.sleep(0.05)
        assert details["running"] is True
        assert not (paths.data / "restart-strategies.json").exists()


def test_strategy_runtime_api_requires_session_and_schedules_events(tmp_path: Path) -> None:
    app = create_app(workspace(tmp_path / "workspace"))
    with TestClient(app) as client:
        unauthorized = client.get(
            "/api/v1/market/quote",
            params={"source": "missing", "code": "005930"},
        )
        assert unauthorized.status_code == 401

        token = app.state.sessions.issue("central-scheduler.py")
        headers = {"Authorization": f"Bearer {token}"}
        connection = client.post(
            "/api/settings/connections",
            json={
                "provider": "kis",
                "target": "PAPER",
                "label": "schedule-source",
                "app_key": "schedule-key",
                "app_secret": "schedule-secret",
            },
        ).json()
        client.post(
            "/api/settings/accounts",
            json={
                "alias": "schedule-account",
                "connection_id": connection["id"],
                "account_number": "12345678",
                "product_code": "01",
            },
        )
        manifest = client.post(
            "/api/v1/strategy/manifest",
            headers=headers,
            json={
                "strategy_id": "central-scheduler",
                "trading_account": "schedule-account",
                "market_sources": {"primary": "schedule-source"},
            },
        )
        assert manifest.status_code == 200
        registered = client.post(
            "/api/v1/strategy/schedules",
            headers=headers,
            json={
                "schedules": [
                    {
                        "id": "one-second",
                        "kind": "INTERVAL",
                        "interval_seconds": 1,
                        "aligned": True,
                        "trading_days_only": False,
                    }
                ]
            },
        )
        assert registered.status_code == 200
        event = client.get(
            "/api/v1/strategy/events/next",
            headers=headers,
                params={"timeout": 5},
        )
        assert event.status_code == 200
        assert event.json()["event"]["schedule_id"] == "one-second"
        today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
        service_results = app.state.runtime_store.service_results(
            today, source="central-scheduler.py"
        )
        assert service_results["scheduler"]["success"] >= 2
        assert service_results["scheduler"]["last_operation"] == "deliver"
