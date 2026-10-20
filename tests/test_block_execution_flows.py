"""Schema, source round-trip and scheduler integration for execution trees."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from trade_router_studio.block_packages import block_package_from_source, render_block_strategy
from trade_router_studio.execution_models import BlockExecutionFlow, ExecutionCondition
from test_block_buy_flows import Harness, flow, settings_for


def execution_flow(**changes):
    result = {
        "id": "unfilled", "name": "미체결 관리",
        "execution_start_time": "08:00", "execution_end_time": "20:00",
        "execution_scan_seconds": 5,
        "execution_rule_tree": {
            "kind": "logic", "id": "pending-root", "operator": "ALL",
            "children": [
                {"kind": "condition", "id": "buy-only", "condition": {
                    "metric": "order_side", "operator": "==", "value": "BUY"}},
                {"kind": "condition", "id": "timeout", "condition": {
                    "metric": "elapsed_seconds", "operator": ">=", "value": 30}},
            ],
            "action": {"kind": "execution", "id": "cancel", "action_type": "CANCEL_REMAINDER"},
        },
    }
    result.update(changes)
    return result


def test_execution_flow_round_trip_preserves_tree_and_old_strategies_default_empty():
    settings = settings_for([flow()], execution_flows=[execution_flow()])
    source = render_block_strategy(settings)
    restored = block_package_from_source("체결 구성", source)
    assert restored.settings.model_dump(mode="json") == settings.model_dump(mode="json")
    old_source = "\n".join(line for line in source.splitlines()
                           if not line.startswith("EXECUTION_FLOWS_JSON ="))
    assert block_package_from_source("구버전", old_source).settings.execution_flows == []


@pytest.mark.parametrize("value", ["garbage", "null", "{}", "[{}]"])
def test_explicit_invalid_execution_metadata_does_not_silently_disable_it(value):
    source = render_block_strategy(settings_for([flow()]))
    source = source.replace('EXECUTION_FLOWS_JSON = "[]"', f"EXECUTION_FLOWS_JSON = {value!r}")
    with pytest.raises(ValueError):
        block_package_from_source("잘못된 설정", source)


@pytest.mark.parametrize("literal", ["None", "False", "[]", "{}", "str([])"])
def test_invalid_execution_metadata_python_literals_are_not_legacy(literal):
    source = render_block_strategy(settings_for([flow()]))
    source = source.replace('EXECUTION_FLOWS_JSON = "[]"', f"EXECUTION_FLOWS_JSON = {literal}")
    with pytest.raises(ValueError):
        block_package_from_source("잘못된 설정", source)


@pytest.mark.parametrize("condition", [
    {"metric": "order_side", "operator": ">", "value": "BUY"},
    {"metric": "order_type", "operator": "==", "value": "ANYTHING"},
    {"metric": "elapsed_seconds", "operator": ">=", "value": "NaN"},
    {"metric": "elapsed_seconds", "operator": ">=", "value": "Infinity"},
    {"metric": "remaining_quantity", "operator": ">=", "value": -1},
    {"metric": "remaining_quantity", "operator": ">=", "value": True},
    {"metric": "price", "operator": ">=", "value": 1},
])
def test_invalid_execution_conditions_fail_closed(condition):
    with pytest.raises(ValidationError):
        ExecutionCondition.model_validate(condition)


@pytest.mark.parametrize("changes", [
    {"execution_scan_seconds": 1},
    {"execution_start_time": "20:00", "execution_end_time": "08:00"},
    {"execution_start_time": "25:00"},
])
def test_execution_schedule_validation(changes):
    with pytest.raises(ValidationError):
        BlockExecutionFlow.model_validate(execution_flow(**changes))


def test_empty_execution_editor_allowed_but_unconditional_action_rejected():
    data = execution_flow()
    tree = data["execution_rule_tree"]
    tree["children"] = []
    with pytest.raises(ValidationError):
        BlockExecutionFlow.model_validate(data)
    tree["action"] = None
    assert BlockExecutionFlow.model_validate(data).execution_rule_tree.children == []


def test_duplicate_ids_and_ambiguous_and_actions_rejected():
    data = execution_flow()
    data["execution_rule_tree"]["children"][1]["id"] = "buy-only"
    with pytest.raises(ValidationError):
        BlockExecutionFlow.model_validate(data)
    data = execution_flow()
    for index, child in enumerate(data["execution_rule_tree"]["children"]):
        child["action"] = {"kind": "execution", "id": f"cancel-{index}",
                           "action_type": "CANCEL_REMAINDER"}
    with pytest.raises(ValidationError):
        BlockExecutionFlow.model_validate(data)
    data["execution_rule_tree"]["operator"] = "ANY"
    BlockExecutionFlow.model_validate(data)
    with pytest.raises(ValidationError):
        settings_for([flow()], execution_flows=[data, deepcopy(data)])


def test_generated_strategy_dispatches_execution_separately_from_buy_and_sell():
    harness = Harness(settings_for([flow()], execution_flows=[execution_flow()]))

    async def exercise():
        await harness.strategy.on_start(harness.ctx)
        expected = [(kind, details) for kind, _, details in harness.ctx.scheduler.calls
                    if details["schedule_id"].startswith("execution-")]
        assert expected == [("every", {
            "seconds": 5, "schedule_id": "execution-manage-unfilled",
            "between": ("08:00", "20:00"), "aligned": True,
        })]
        harness.strategy._execution_rule_manager.tick = AsyncMock()
        await harness.strategy.on_timer(harness.ctx, SimpleNamespace(
            schedule_id="execution-manage-unfilled", scheduled_at=harness.now,
        ))
        harness.strategy._execution_rule_manager.tick.assert_awaited_once_with(harness.ctx, "unfilled")

    asyncio.run(exercise())
    assert not harness.order_calls and not harness.sell_calls


def test_legacy_strategy_registers_no_execution_schedule():
    harness = Harness(settings_for([flow()]))
    asyncio.run(harness.strategy.on_start(harness.ctx))
    assert not any(details["schedule_id"].startswith("execution-")
                   for _, _, details in harness.ctx.scheduler.calls)
