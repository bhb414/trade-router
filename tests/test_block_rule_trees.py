"""Compatibility and runtime contracts for recursive block rule trees."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trade_router import ScheduleKind
from trade_router_studio.block_packages import (
    BlockTradeCondition,
    block_package_from_source,
    render_block_strategy,
)

from test_block_buy_flows import Harness, condition_group, flow, settings_for
from test_block_sell_flows import (
    SellHarness,
    sell_flow,
    settings_for as sell_settings_for,
)


def _condition(
    identifier: str,
    *,
    operator: str = "GT",
    value: str = "100",
    action: dict | None = None,
) -> dict:
    return {
        "kind": "condition",
        "id": identifier,
        "condition": BlockTradeCondition(
            left="close",
            operator=operator,
            right="value",
            value=Decimal(value),
        ).model_dump(mode="json"),
        "action": action,
    }


def _action(identifier: str, side: str, order_type: str) -> dict:
    return {
        "kind": "order",
        "id": identifier,
        "side": side,
        "order_type": order_type,
    }


def _logic(
    identifier: str,
    operator: str,
    *children: dict,
    action: dict | None = None,
) -> dict:
    return {
        "kind": "logic",
        "id": identifier,
        "operator": operator,
        "children": list(children),
        "action": action,
    }


def test_legacy_groups_become_all_of_any_with_the_global_order_style() -> None:
    """The migration must preserve the old parentheses exactly.

    Former rows inside a block were OR, while consecutive blocks were AND.
    The global order type becomes the root action so merely opening an older
    strategy cannot change either its signal or its order style.
    """

    legacy_flow = flow(
        buy_condition_groups=[
            {
                "id": "first",
                "conditions": [
                    BlockTradeCondition(
                        left="close", operator="GT", right="value", value=Decimal("100")
                    ).model_dump(mode="json"),
                    BlockTradeCondition(
                        left="close", operator="LT", right="value", value=Decimal("300")
                    ).model_dump(mode="json"),
                ],
            },
            condition_group("second", value="150"),
        ],
    )
    namespace: dict[str, object] = {}
    exec(
        compile(
            render_block_strategy(
                settings_for([legacy_flow], buy_order_type="LIMIT")
            ),
            "<legacy-rule-tree>",
            "exec",
        ),
        namespace,
    )

    tree = namespace["BUY_FLOWS"][0]["buy_rule_tree"]
    assert tree["kind"] == "logic"
    assert tree["id"] == "legacy-buy-root"
    assert tree["operator"] == "ALL"
    assert tree["action"] == _action("legacy-buy-order", "BUY", "LIMIT")
    assert [child["operator"] for child in tree["children"]] == ["ANY", "ANY"]
    assert [child["id"] for child in tree["children"]] == [
        "legacy-buy-group-1",
        "legacy-buy-group-2",
    ]
    assert [leaf["id"] for leaf in tree["children"][0]["children"]] == [
        "legacy-buy-condition-1-1",
        "legacy-buy-condition-1-2",
    ]
    assert all(
        leaf["action"] is None
        for group in tree["children"]
        for leaf in group["children"]
    )


def test_explicit_recursive_tree_round_trips_through_generated_source() -> None:
    tree = _logic(
        "root",
        "ALL",
        _condition("trend", value="100"),
        _logic(
            "entry-choice",
            "ANY",
            _condition(
                "market-entry",
                operator="LT",
                value="150",
                action=_action("market-buy", "BUY", "MARKET"),
            ),
            _condition(
                "limit-entry",
                value="150",
                action=_action("limit-buy", "BUY", "LIMIT"),
            ),
        ),
    )
    settings = settings_for([
        flow(buy_condition_groups=[], buy_rule_tree=deepcopy(tree)),
    ])

    restored = block_package_from_source(
        "재귀 조건",
        render_block_strategy(settings),
    )

    assert restored.settings.buy_flows[0].buy_rule_tree.model_dump(mode="json") == tree


def test_explicit_tree_remains_authoritative_even_with_legacy_ids() -> None:
    tree = _logic(
        "legacy-buy-root",
        "ALL",
        _condition("legacy-buy-condition-1-1", value="999"),
        action=_action("legacy-buy-order", "BUY", "LIMIT"),
    )
    settings = settings_for([
        flow(
            buy_condition_groups=[condition_group("old", value="100")],
            buy_rule_tree=tree,
        ),
    ])

    restored = settings.buy_flows[0].buy_rule_tree
    assert restored is not None
    assert restored.action is not None
    assert restored.action.order_type == "LIMIT"
    assert restored.children[0].condition.value == Decimal("999")


def test_empty_sell_action_tree_and_nested_empty_group_are_rejected() -> None:
    empty_sell = _logic(
        "sell-root",
        "ALL",
        action=_action("sell-all", "SELL", "MARKET"),
    )
    with pytest.raises(ValidationError):
        sell_settings_for([
            sell_flow(sell_condition_groups=[], sell_rule_tree=empty_sell),
        ])

    nested_empty = _logic(
        "buy-root",
        "ALL",
        _logic(
            "empty-branch",
            "ALL",
            action=_action("buy-empty", "BUY", "MARKET"),
        ),
    )
    with pytest.raises(ValidationError):
        settings_for([
            flow(buy_condition_groups=[], buy_rule_tree=nested_empty),
        ])


def test_all_group_rejects_actions_in_multiple_child_subtrees() -> None:
    ambiguous = _logic(
        "buy-root",
        "ALL",
        _condition(
            "first",
            value="100",
            action=_action("first-order", "BUY", "MARKET"),
        ),
        _logic(
            "second-branch",
            "ANY",
            _condition(
                "second",
                value="100",
                action=_action("second-order", "BUY", "LIMIT"),
            ),
        ),
        action=_action("fallback-order", "BUY", "MARKET"),
    )

    with pytest.raises(ValidationError, match="AND"):
        settings_for([
            flow(buy_condition_groups=[], buy_rule_tree=ambiguous),
        ])

    one_actionable_subtree = _logic(
        "buy-root",
        "ALL",
        _condition("guard", value="100"),
        _condition(
            "actionable",
            value="100",
            action=_action("child-order", "BUY", "LIMIT"),
        ),
        action=_action("fallback-order", "BUY", "MARKET"),
    )
    settings = settings_for([
        flow(buy_condition_groups=[], buy_rule_tree=one_actionable_subtree),
    ])
    assert settings.buy_flows[0].buy_rule_tree is not None


def test_source_without_rule_tree_constants_remains_readable_as_legacy() -> None:
    source = render_block_strategy(settings_for([flow()]))
    legacy_source = "\n".join(
        line
        for line in source.splitlines()
        if not line.startswith(("BUY_RULE_TREE_JSON = ", "SELL_RULE_TREE_JSON = "))
    )

    restored = block_package_from_source("구형 조건 블록", legacy_source)

    assert restored.settings.buy_rule_tree.id == "legacy-buy-root"
    assert restored.settings.buy_rule_tree.action.side == "BUY"
    assert restored.settings.sell_rule_tree.id == "legacy-sell-root"
    assert restored.settings.sell_rule_tree.action.side == "SELL"
    assert restored.settings.buy_flows[0].buy_rule_tree.id == "legacy-buy-root"
    assert restored.settings.sell_flows == []


def test_malformed_explicit_rule_tree_metadata_fails_closed() -> None:
    source = render_block_strategy(settings_for([flow()]))
    malformed = "\n".join(
        "BUY_RULE_TREE_JSON = 123"
        if line.startswith("BUY_RULE_TREE_JSON = ")
        else line
        for line in source.splitlines()
    )

    try:
        block_package_from_source("손상 조건 계층", malformed)
    except ValueError as exc:
        assert "조건 계층" in str(exc)
    else:  # pragma: no cover - a silent fallback could place unintended orders
        raise AssertionError("malformed rule-tree metadata must not use legacy fallback")


@pytest.mark.parametrize(
    "tree",
    [
        _logic(
            "wrong-side-root",
            "ALL",
            _condition("passes", value="100"),
            action=_action("wrong-side", "SELL", "MARKET"),
        ),
        _logic(
            "duplicate-action-root",
            "ALL",
            _condition(
                "first",
                value="100",
                action=_action("same-action", "BUY", "MARKET"),
            ),
            _condition(
                "second",
                value="100",
                action=_action("same-action", "BUY", "LIMIT"),
            ),
        ),
    ],
)
def test_invalid_or_ambiguous_buy_actions_are_rejected(tree: dict) -> None:
    with pytest.raises(ValidationError):
        settings_for([
            flow(buy_condition_groups=[], buy_rule_tree=tree),
        ])


def test_nested_all_any_selects_the_matching_branch_action() -> None:
    tree = _logic(
        "root",
        "ALL",
        _condition("required", value="100"),
        _logic(
            "choice",
            "ANY",
            _condition(
                "false-market",
                operator="LT",
                value="100",
                action=_action("market-action", "BUY", "MARKET"),
            ),
            _condition(
                "true-limit",
                value="150",
                action=_action("limit-action", "BUY", "LIMIT"),
            ),
        ),
    )
    harness = Harness(settings_for([
        flow(buy_condition_groups=[], buy_rule_tree=tree),
    ]))
    submitted: list[str] = []

    async def market_buy(**kwargs):
        submitted.append("MARKET")
        return await harness.buy(**kwargs)

    async def limit_buy(**kwargs):
        submitted.append("LIMIT")
        return await harness.buy(**kwargs)

    harness.ctx.orders.market_buy = market_buy
    harness.ctx.orders.limit_buy = limit_buy

    asyncio.run(harness.tick())

    assert submitted == ["LIMIT"]
    assert len(harness.order_calls) == 1
    assert harness.order_calls[0]["price"] == Decimal("25000")


def test_descendant_action_does_not_run_when_an_all_ancestor_fails() -> None:
    tree = _logic(
        "root",
        "ALL",
        _condition("outer-failure", value="999"),
        _logic(
            "nested",
            "ANY",
            _condition(
                "locally-true",
                value="100",
                action=_action("must-not-run", "BUY", "MARKET"),
            ),
        ),
    )
    harness = Harness(settings_for([
        flow(buy_condition_groups=[], buy_rule_tree=tree),
    ]))

    asyncio.run(harness.tick())

    assert harness.order_calls == []


def test_passing_tree_without_an_action_never_places_an_order() -> None:
    tree = _logic(
        "root",
        "ALL",
        _condition("passes", value="100"),
    )
    harness = Harness(settings_for([
        flow(buy_condition_groups=[], buy_rule_tree=tree),
    ]))

    asyncio.run(harness.tick())

    assert harness.order_calls == []
    assert any("주문 행동이 없습니다" in message for message in harness.logs)


def test_sell_branch_can_choose_its_own_limit_order_style() -> None:
    tree = _logic(
        "sell-root",
        "ALL",
        _condition(
            "sell-now",
            value="100",
            action=_action("limit-sell", "SELL", "LIMIT"),
        ),
    )
    harness = SellHarness(sell_settings_for([
        sell_flow(sell_condition_groups=[], sell_rule_tree=tree),
    ]))
    submitted: list[str] = []

    async def market_sell(**kwargs):
        submitted.append("MARKET")
        return await harness.sell(**kwargs)

    async def limit_sell(**kwargs):
        submitted.append("LIMIT")
        return await harness.sell(**kwargs)

    harness.ctx.orders.market_sell = market_sell
    harness.ctx.orders.limit_sell = limit_sell
    runtime_flow = harness.namespace["SELL_FLOWS"][0]

    asyncio.run(harness.strategy._on_bar_locked(
        harness.ctx,
        type("Event", (), {
            "schedule_id": "condition-sell-early-bars-1",
            "scheduled_at": harness.now,
            "kind": ScheduleKind.BAR_CLOSE,
        })(),
        runtime_flow,
    ))

    assert submitted == ["LIMIT"]
    assert harness.orders[0]["price"] == Decimal("104")


def test_startup_does_not_register_or_run_an_implicit_market_close() -> None:
    harness = Harness(settings_for([flow()]), now="15:21:00")
    implicit_liquidations: list[object] = []

    async def unexpected_liquidation(*args, **kwargs):
        implicit_liquidations.append((args, kwargs))

    harness.strategy._liquidate_owned_positions = unexpected_liquidation
    asyncio.run(harness.strategy.on_start(harness.ctx))

    schedule_ids = {
        call[2].get("schedule_id")
        for call in harness.ctx.scheduler.calls
    }
    assert {"close-all", "close-retry", "close-confirm"}.isdisjoint(schedule_ids)
    assert implicit_liquidations == []


def test_block_ui_uses_rule_tree_markers_and_removes_the_fixed_close_ui() -> None:
    script = (
        Path(__file__).parents[1]
        / "apps/studio/src/trade_router_studio/static/app.js"
    ).read_text(encoding="utf-8")

    for marker in (
        "buy_rule_tree",
        "sell_rule_tree",
        "ruleNode",
        "ruleActionOwner",
        "조건식",
        "데이터 추가",
        "setBlockRuleConnection",
        "boundaryIndex",
        "하위 계층 추가",
        "전체 조건 설정",
        "통과하면",
        "order_type",
    ):
        assert marker in script
    for removed in (
        "closeSchedule",
        "장 마감 청산",
        "내부 OR",
        "이전 블록과 AND",
    ):
        assert removed not in script
