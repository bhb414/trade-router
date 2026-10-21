from __future__ import annotations

import ast
import hashlib
import json
from datetime import time
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .execution_models import BlockExecutionFlow


BLOCK_PACKAGE_KIND = "trade-router-block-strategy"
BAR_OBSERVER_TEMPLATE = "bar-observer-v1"
CONDITION_BOLLINGER_TEMPLATE = "condition-bollinger-v1"
BLOCK_TEMPLATE = CONDITION_BOLLINGER_TEMPLATE


def _validate_clock(value: str) -> str:
    candidate = value.strip()
    if len(candidate) not in {5, 8}:
        raise ValueError("Time must use HH:MM or HH:MM:SS.")
    try:
        time.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("Time must use HH:MM or HH:MM:SS.") from exc
    return candidate


class BlockStrategySettings(BaseModel):
    """Settings for the legacy read-only bar observer block."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = Field(min_length=1, max_length=128)
    trading_account: str = Field(min_length=1, max_length=128)
    market_source: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    bar_interval: str = Field(pattern=r"^[1-9]\d*[smhdw]$", max_length=16)
    bar_count: int = Field(ge=1, le=2_000)
    schedule_id: str = Field(min_length=1, max_length=128)
    start_time: str
    end_time: str

    _validate_times = field_validator("start_time", "end_time")(_validate_clock)


def _normalize_managed_bar_interval(value: str) -> str:
    candidate = value.strip().lower()
    if candidate in {"1d", "d", "day"}:
        return "1d"
    if candidate == "1h":
        candidate = "60m"
    if not candidate.endswith("m") or not candidate[:-1].isdigit():
        raise ValueError("관리형 블록 전략은 1분봉부터 120분봉과 일봉을 지원합니다.")
    minutes = int(candidate[:-1])
    if not 1 <= minutes <= 120:
        raise ValueError("관리형 블록 전략은 1분봉부터 120분봉과 일봉을 지원합니다.")
    return f"{minutes}m"


BlockMetricName = Literal[
    "open", "high", "low", "close", "volume",
    "heikin_open", "heikin_high", "heikin_low", "heikin_close",
    "bollinger_upper", "bollinger_middle", "bollinger_lower",
    "obv", "atr",
    "average_price", "profit_rate",
]

_BLOCK_BAR_METRICS = frozenset({
    "open", "high", "low", "close", "volume",
    "heikin_open", "heikin_high", "heikin_low", "heikin_close",
    "bollinger_upper", "bollinger_middle", "bollinger_lower",
    "obv", "atr",
})


class BlockMetricExpression(BaseModel):
    """A typed metric leaf in a condition expression.

    ATR uses Wilder's true range smoothing.  ``atr_period`` is the Wilder
    period; the runtime seeds the series with a simple average of the first
    period true ranges and applies Wilder's recursive average thereafter.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["metric"] = "metric"
    metric: BlockMetricName = "close"
    offset: int = Field(default=0, ge=0, le=200)
    bar_interval: str = Field(default="3m", max_length=16)
    bar_data_mode: Literal["COMPLETE_ONLY", "INCLUDE_CURRENT"] = "COMPLETE_ONLY"
    bollinger_period: int = Field(default=55, ge=2, le=2_000)
    bollinger_stddev: Decimal = Field(
        default=Decimal("1.04"), gt=0, le=Decimal("20"), allow_inf_nan=False,
    )
    atr_period: int = Field(default=14, ge=1, le=400)

    _validate_interval = field_validator("bar_interval")(_normalize_managed_bar_interval)


class BlockConstantExpression(BaseModel):
    """A finite numeric constant in a condition expression."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["constant"] = "constant"
    value: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("-1000000000000000000"),
        le=Decimal("1000000000000000000"),
        allow_inf_nan=False,
    )


class BlockBinaryExpression(BaseModel):
    """A safe arithmetic node.  It is interpreted without ``eval``."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["binary"] = "binary"
    operator: Literal["ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"] = "ADD"
    left: "BlockConditionExpression"
    right: "BlockConditionExpression"


BlockConditionExpression = Annotated[
    BlockMetricExpression | BlockConstantExpression | BlockBinaryExpression,
    Field(discriminator="kind"),
]
BlockBinaryExpression.model_rebuild(
    _types_namespace={"BlockConditionExpression": BlockConditionExpression},
)


def _expression_nodes(
    expression: BlockConditionExpression,
    *,
    depth: int = 0,
) -> tuple[int, int, list[BlockMetricExpression]]:
    """Return node count, maximum depth, and metric leaves."""

    if not isinstance(expression, BlockBinaryExpression):
        metrics = [expression] if isinstance(expression, BlockMetricExpression) else []
        return 1, depth, metrics
    left_count, left_depth, left_metrics = _expression_nodes(expression.left, depth=depth + 1)
    right_count, right_depth, right_metrics = _expression_nodes(expression.right, depth=depth + 1)
    return (
        1 + left_count + right_count,
        max(depth, left_depth, right_depth),
        [*left_metrics, *right_metrics],
    )


def _condition_metric_leaves(condition: "BlockTradeCondition") -> list[BlockMetricExpression]:
    """Return effective metric leaves, adapting legacy side fields."""

    metrics: list[BlockMetricExpression] = []
    for side in ("left", "right"):
        expression = getattr(condition, f"{side}_expression")
        if expression is not None:
            metrics.extend(_expression_nodes(expression)[2])
            continue
        metric = getattr(condition, side)
        if metric == "value":
            continue
        metrics.append(BlockMetricExpression(
            metric=metric,
            offset=getattr(condition, f"{side}_offset"),
            bar_interval=getattr(condition, f"{side}_bar_interval"),
            bar_data_mode=getattr(condition, f"{side}_bar_data_mode"),
            bollinger_period=getattr(condition, f"{side}_bollinger_period"),
            bollinger_stddev=getattr(condition, f"{side}_bollinger_stddev"),
            atr_period=getattr(condition, f"{side}_atr_period"),
        ))
    return metrics


class BlockTradeCondition(BaseModel):
    """One broker-neutral comparison used by a managed block strategy."""

    model_config = ConfigDict(extra="forbid")
    left: Literal[
        "open", "high", "low", "close", "volume",
        "heikin_open", "heikin_high", "heikin_low", "heikin_close",
        "bollinger_upper", "bollinger_middle", "bollinger_lower",
        "obv", "atr",
        "average_price", "profit_rate",
    ] = "close"
    operator: Literal["LT", "LTE", "GT", "GTE", "EQ", "CROSS_UP", "CROSS_DOWN"] = "LT"
    right: Literal[
        "open", "high", "low", "close", "volume",
        "heikin_open", "heikin_high", "heikin_low", "heikin_close",
        "bollinger_upper", "bollinger_middle", "bollinger_lower",
        "obv", "atr",
        "average_price", "profit_rate", "value",
    ] = "bollinger_upper"
    value: Decimal = Decimal("0")
    left_offset: int = Field(default=0, ge=0, le=200)
    right_offset: int = Field(default=0, ge=0, le=200)
    left_bar_interval: str = Field(default="3m", max_length=16)
    left_bar_data_mode: Literal["COMPLETE_ONLY", "INCLUDE_CURRENT"] = "COMPLETE_ONLY"
    left_bollinger_period: int = Field(default=55, ge=2, le=2_000)
    left_bollinger_stddev: Decimal = Field(default=Decimal("1.04"), gt=0, le=Decimal("20"))
    right_bar_interval: str = Field(default="3m", max_length=16)
    right_bar_data_mode: Literal["COMPLETE_ONLY", "INCLUDE_CURRENT"] = "COMPLETE_ONLY"
    right_bollinger_period: int = Field(default=55, ge=2, le=2_000)
    right_bollinger_stddev: Decimal = Field(default=Decimal("1.04"), gt=0, le=Decimal("20"))
    left_atr_period: int = Field(default=14, ge=1, le=400)
    right_atr_period: int = Field(default=14, ge=1, le=400)
    left_expression: BlockConditionExpression | None = None
    right_expression: BlockConditionExpression | None = None

    _validate_intervals = field_validator(
        "left_bar_interval", "right_bar_interval",
    )(_normalize_managed_bar_interval)

    @model_validator(mode="after")
    def validate_expressions(self) -> "BlockTradeCondition":
        expressions = [
            expression
            for expression in (self.left_expression, self.right_expression)
            if expression is not None
        ]
        for expression in expressions:
            node_count, depth, _ = _expression_nodes(expression)
            if depth > 4:
                raise ValueError("조건 수식은 최대 4단계까지 중첩할 수 있습니다.")
            if node_count > 31:
                raise ValueError("조건 수식은 최대 31개 항목까지 사용할 수 있습니다.")

        # OBV is anchored at zero at the beginning of a bounded history
        # window.  Only a direct same-series, different-offset comparison has
        # an anchor-independent meaning, so preserve that safety rule for the
        # expression schema as well as the legacy fields.
        metrics = _condition_metric_leaves(self)
        if self.operator in {"CROSS_UP", "CROSS_DOWN"}:
            if any(metric.metric == "profit_rate" for metric in metrics):
                raise ValueError("교차 조건에서는 실시간 수익률을 사용할 수 없습니다.")
            bar_metrics = [
                metric for metric in metrics if metric.metric in _BLOCK_BAR_METRICS
            ]
            if not bar_metrics:
                raise ValueError("교차 조건에는 봉 지표가 하나 이상 필요합니다.")
            bar_series = {
                (metric.bar_interval, metric.bar_data_mode)
                for metric in bar_metrics
            }
            if len(bar_series) != 1:
                raise ValueError(
                    "교차 조건의 모든 봉 지표는 같은 봉 주기와 봉 상태를 사용해야 합니다."
                )
        if not any(metric.metric == "obv" for metric in metrics):
            return self

        def obv_source(side: str) -> BlockMetricExpression | None:
            expression = getattr(self, f"{side}_expression")
            if expression is not None:
                return (
                    expression
                    if isinstance(expression, BlockMetricExpression) and expression.metric == "obv"
                    else None
                )
            if getattr(self, side) != "obv":
                return None
            return BlockMetricExpression(
                metric="obv",
                offset=getattr(self, f"{side}_offset"),
                bar_interval=getattr(self, f"{side}_bar_interval"),
                bar_data_mode=getattr(self, f"{side}_bar_data_mode"),
                bollinger_period=getattr(self, f"{side}_bollinger_period"),
                bollinger_stddev=getattr(self, f"{side}_bollinger_stddev"),
                atr_period=getattr(self, f"{side}_atr_period"),
            )

        left_source = obv_source("left")
        right_source = obv_source("right")
        if left_source is None or right_source is None:
            raise ValueError("OBV는 기준점과 무관한 직접 시점 비교에만 사용할 수 있습니다.")
        if (
            left_source.bar_interval != right_source.bar_interval
            or left_source.bar_data_mode != right_source.bar_data_mode
        ):
            raise ValueError("OBV 비교의 양쪽은 같은 봉 주기와 봉 상태를 사용해야 합니다.")
        if left_source.offset == right_source.offset:
            raise ValueError("OBV 비교의 양쪽은 서로 다른 봉 위치를 사용해야 합니다.")
        return self


DEFAULT_SELL_CONDITIONS = (
    BlockTradeCondition(left="profit_rate", operator="GTE", right="value", value=Decimal("3")),
    BlockTradeCondition(left="profit_rate", operator="LTE", right="value", value=Decimal("-3")),
    BlockTradeCondition(left="close", operator="LT", right="bollinger_upper"),
)


class BlockConditionGroup(BaseModel):
    """One OR group. Multiple groups are combined with AND by the compiler."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)
    conditions: list[BlockTradeCondition] = Field(min_length=1, max_length=20)


class BlockOrderAction(BaseModel):
    """One order selected after a condition branch matches.

    Quantity and exposure remain strategy-level safety settings.  A branch only
    chooses the side and order style so different branches can use market and
    limit orders without duplicating the portfolio/risk configuration.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["order"] = "order"
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT", "BEST_LIMIT"] = "MARKET"


class BlockConditionNode(BaseModel):
    """A comparison leaf in a recursive condition/action tree."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["condition"] = "condition"
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)
    condition: BlockTradeCondition
    action: BlockOrderAction | None = None


class BlockLogicNode(BaseModel):
    """A parenthesized ALL/ANY group with an optional fallback action."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["logic"] = "logic"
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)
    operator: Literal["ALL", "ANY"] = "ALL"
    children: list["BlockRuleNode"] = Field(default_factory=list, max_length=20)
    action: BlockOrderAction | None = None


BlockRuleNode = Annotated[
    BlockConditionNode | BlockLogicNode,
    Field(discriminator="kind"),
]
BlockLogicNode.model_rebuild(_types_namespace={"BlockRuleNode": BlockRuleNode})


def _rule_tree_nodes(
    node: BlockRuleNode,
    *,
    depth: int = 0,
) -> tuple[list[BlockRuleNode], list[BlockTradeCondition], list[BlockOrderAction], int]:
    """Flatten a rule tree for validation and capability discovery."""

    nodes: list[BlockRuleNode] = [node]
    conditions: list[BlockTradeCondition] = []
    actions: list[BlockOrderAction] = []
    if node.action is not None:
        actions.append(node.action)
    if isinstance(node, BlockConditionNode):
        conditions.append(node.condition)
        return nodes, conditions, actions, depth
    maximum_depth = depth
    for child in node.children:
        child_nodes, child_conditions, child_actions, child_depth = _rule_tree_nodes(
            child, depth=depth + 1,
        )
        nodes.extend(child_nodes)
        conditions.extend(child_conditions)
        actions.extend(child_actions)
        maximum_depth = max(maximum_depth, child_depth)
    return nodes, conditions, actions, maximum_depth


def _validate_rule_tree(
    tree: BlockLogicNode | None,
    *,
    side: Literal["BUY", "SELL"],
) -> None:
    if tree is None:
        return
    nodes, conditions, actions, depth = _rule_tree_nodes(tree)
    if depth > 8:
        raise ValueError("조건 계층은 최대 8단계까지 중첩할 수 있습니다.")
    if len(nodes) > 100:
        raise ValueError("조건 계층은 최대 100개 블록까지 사용할 수 있습니다.")
    node_ids = [node.id for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("같은 조건 계층의 블록 ID는 서로 달라야 합니다.")
    action_ids = [action.id for action in actions]
    if len(action_ids) != len(set(action_ids)):
        raise ValueError("같은 조건 계층의 주문 행동 ID는 서로 달라야 합니다.")
    if any(action.side != side for action in actions):
        label = "매수" if side == "BUY" else "매도"
        raise ValueError(f"{label} 흐름에는 {label} 주문 행동만 넣을 수 있습니다.")
    if any(
        isinstance(node, BlockLogicNode) and not node.children and node is not tree
        for node in nodes
    ):
        raise ValueError("조건 계층 안의 그룹은 비워 둘 수 없습니다.")
    for node in nodes:
        if not isinstance(node, BlockLogicNode) or node.operator != "ALL":
            continue
        actionable_children = sum(
            bool(_rule_tree_nodes(child)[2]) for child in node.children
        )
        if actionable_children > 1:
            raise ValueError(
                "AND 그룹에서는 하나의 하위 분기에만 주문 행동을 둘 수 있습니다. "
                "여러 주문 행동은 OR 분기로 나누거나 AND 그룹 자체의 행동으로 설정하세요."
            )
    if side == "SELL" and not conditions:
        raise ValueError("매도 조건 계층에는 하나 이상의 조건이 필요합니다.")


def _legacy_condition_groups(
    conditions: list[object] | tuple[object, ...],
    logic: object,
    *,
    prefix: str,
    bar_interval: object,
    bar_data_mode: object,
    bollinger_period: object,
    bollinger_stddev: object,
) -> list[dict[str, object]]:
    rows = list(conditions or [])
    if not rows:
        return []
    groups = [rows] if str(logic or "ANY") == "ANY" else [[row] for row in rows]
    return [
        {
            "id": f"{prefix}-{index}",
            "bar_interval": bar_interval or "3m",
            "bar_data_mode": bar_data_mode or "COMPLETE_ONLY",
            "bollinger_period": bollinger_period or 55,
            "bollinger_stddev": bollinger_stddev or Decimal("1.04"),
            "conditions": group,
        }
        for index, group in enumerate(groups, start=1)
    ]


def _condition_mapping(value: object) -> dict[str, object]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, dict) else {}


def _embed_condition_calculations(
    groups: object,
    *,
    prefix: str,
    common: dict[str, object],
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for group_index, raw_group in enumerate(list(groups or []), start=1):
        group = _condition_mapping(raw_group)
        calculation = {
            "bar_interval": group.get("bar_interval", common["bar_interval"]),
            "bar_data_mode": group.get("bar_data_mode", common["bar_data_mode"]),
            "bollinger_period": group.get("bollinger_period", common["bollinger_period"]),
            "bollinger_stddev": group.get("bollinger_stddev", common["bollinger_stddev"]),
        }
        conditions: list[dict[str, object]] = []
        for raw_condition in list(group.get("conditions") or []):
            condition = _condition_mapping(raw_condition)
            for side in ("left", "right"):
                condition.setdefault(f"{side}_bar_interval", calculation["bar_interval"])
                condition.setdefault(f"{side}_bar_data_mode", calculation["bar_data_mode"])
                condition.setdefault(f"{side}_bollinger_period", calculation["bollinger_period"])
                condition.setdefault(f"{side}_bollinger_stddev", calculation["bollinger_stddev"])
            conditions.append(condition)
        normalized.append(
            {
                "id": group.get("id") or f"{prefix}-{group_index}",
                "conditions": conditions,
            }
        )
    return normalized


def _legacy_rule_tree_payload(
    groups: object,
    *,
    side: Literal["BUY", "SELL"],
    order_type: Literal["MARKET", "LIMIT", "BEST_LIMIT"],
) -> dict[str, object]:
    """Convert the former outer-AND/inner-OR representation to a rule tree."""

    side_key = side.lower()
    children: list[dict[str, object]] = []
    for group_index, raw_group in enumerate(list(groups or []), start=1):
        group = _condition_mapping(raw_group)
        children.append({
            "kind": "logic",
            "id": f"legacy-{side_key}-group-{group_index}",
            "operator": "ANY",
            "children": [
                {
                    "kind": "condition",
                    "id": f"legacy-{side_key}-condition-{group_index}-{condition_index}",
                    "condition": _condition_mapping(condition),
                    "action": None,
                }
                for condition_index, condition in enumerate(
                    list(group.get("conditions") or []), start=1,
                )
            ],
            "action": None,
        })
    return {
        "kind": "logic",
        "id": f"legacy-{side_key}-root",
        "operator": "ALL",
        "children": children,
        "action": {
            "kind": "order",
            "id": f"legacy-{side_key}-order",
            "side": side,
            "order_type": order_type,
        },
    }


class BlockBuyFlow(BaseModel):
    """A timed discovery and condition branch; ownership and risk stay shared."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)
    name: str = Field(default="매수 흐름", min_length=1, max_length=80)
    target_source: Literal["HTS_CONDITION", "RANKING", "SYMBOLS"] = "HTS_CONDITION"
    hts_user_id: str = Field(default="", max_length=128)
    condition_id: str = Field(default="", max_length=64)
    expected_condition_name: str = Field(default="", max_length=200)
    ranking_kind: Literal["HTS_VIEW", "VOLUME", "TRADING_VALUE", "RISE"] = "RISE"
    ranking_venue: Literal["KRX", "NXT", "INTEGRATED"] = "KRX"
    ranking_market: Literal["ALL", "KOSPI", "KOSDAQ"] = "ALL"
    ranking_limit: int = Field(default=20, ge=1, le=100)
    symbols: list[str] = Field(default_factory=list, max_length=200)
    buy_scan_seconds: int = Field(default=30, ge=1, le=86_400)
    buy_start_time: str = "09:00"
    buy_end_time: str = "15:00"
    buy_condition_groups: list[BlockConditionGroup] = Field(default_factory=list, max_length=8)
    buy_rule_tree: BlockLogicNode | None = None

    _validate_times = field_validator("buy_start_time", "buy_end_time")(_validate_clock)

    @model_validator(mode="after")
    def validate_flow(self) -> "BlockBuyFlow":
        if time.fromisoformat(self.buy_start_time) >= time.fromisoformat(self.buy_end_time):
            raise ValueError("매수 흐름의 시작 시간은 종료 시간보다 빨라야 합니다.")
        self.symbols = list(dict.fromkeys(code.strip().upper() for code in self.symbols if code.strip()))
        if self.target_source == "SYMBOLS" and not self.symbols:
            raise ValueError("지정 종목 매수 흐름에는 종목이 하나 이상 필요합니다.")
        if self.target_source == "HTS_CONDITION" and (not self.hts_user_id.strip() or not self.condition_id.strip()):
            raise ValueError("조건검색 매수 흐름에는 HTS 사용자와 조건식이 필요합니다.")
        identifiers = [group.id for group in self.buy_condition_groups]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("같은 매수 흐름의 조건 블록 ID는 서로 달라야 합니다.")
        _validate_rule_tree(self.buy_rule_tree, side="BUY")
        return self


class BlockSellFlow(BaseModel):
    """A timed exit branch; positions, orders, and close handling stay shared."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)
    name: str = Field(default="매도 흐름", min_length=1, max_length=80)
    sell_scan_seconds: int = Field(default=30, ge=1, le=86_400)
    sell_start_time: str = "09:00"
    sell_end_time: str = "15:20"
    sell_condition_groups: list[BlockConditionGroup] = Field(default_factory=list, max_length=8)
    sell_rule_tree: BlockLogicNode | None = None

    _validate_times = field_validator("sell_start_time", "sell_end_time")(
        _validate_clock
    )

    @model_validator(mode="after")
    def validate_flow(self) -> "BlockSellFlow":
        if time.fromisoformat(self.sell_start_time) >= time.fromisoformat(
            self.sell_end_time
        ):
            raise ValueError("매도 흐름의 시작 시간은 종료 시간보다 빨라야 합니다.")
        if self.sell_rule_tree is None and not self.sell_condition_groups:
            raise ValueError("매도 흐름에는 조건 계층이나 기존 조건 블록이 필요합니다.")
        identifiers = [group.id for group in self.sell_condition_groups]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("같은 매도 흐름의 조건 블록 ID는 서로 달라야 합니다.")
        _validate_rule_tree(self.sell_rule_tree, side="SELL")
        return self


class ConditionBollingerBlockSettings(BaseModel):
    """Editable values for the router-owned condition/Bollinger workflow."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = Field(min_length=1, max_length=128)
    trading_account: str = Field(min_length=1, max_length=128)
    condition_source: str = Field(min_length=1, max_length=128)
    market_venue: Literal["KRX", "NXT", "INTEGRATED"] = "KRX"
    target_source: Literal["HTS_CONDITION", "RANKING", "SYMBOLS"] = "HTS_CONDITION"
    hts_user_id: str = Field(min_length=1, max_length=128)
    condition_id: str = Field(min_length=1, max_length=64)
    expected_condition_name: str = Field(default="", max_length=200)
    ranking_kind: Literal["HTS_VIEW", "VOLUME", "TRADING_VALUE", "RISE"] = "RISE"
    ranking_venue: Literal["KRX", "NXT", "INTEGRATED"] = "KRX"
    ranking_market: Literal["ALL", "KOSPI", "KOSDAQ"] = "ALL"
    ranking_limit: int = Field(default=20, ge=1, le=100)
    symbols: list[str] = Field(default_factory=list, max_length=200)
    buy_budget_per_symbol: Decimal = Field(gt=0)
    total_operating_budget: Decimal = Field(gt=0)
    max_new_buys_per_scan: int = Field(ge=1, le=100)
    bollinger_period: int = Field(ge=2, le=2_000)
    bollinger_stddev: Decimal = Field(gt=0, le=Decimal("20"))
    take_profit_multiplier: Decimal = Field(gt=1)
    stop_loss_multiplier: Decimal = Field(gt=0, lt=1)
    use_bollinger_exit: bool = True
    use_take_profit: bool = True
    use_stop_loss: bool = True
    buy_condition_logic: Literal["ALL", "ANY"] = "ALL"
    buy_conditions: list[BlockTradeCondition] = Field(default_factory=list, max_length=20)
    sell_condition_logic: Literal["ALL", "ANY"] = "ANY"
    sell_conditions: list[BlockTradeCondition] = Field(
        default_factory=lambda: [item.model_copy() for item in DEFAULT_SELL_CONDITIONS],
        max_length=20,
    )
    buy_condition_groups: list[BlockConditionGroup] = Field(default_factory=list, max_length=8)
    buy_rule_tree: BlockLogicNode | None = None
    buy_enabled: bool = Field(default=True, strict=True)
    buy_flows: list[BlockBuyFlow] = Field(default_factory=list, max_length=8)
    sell_condition_groups: list[BlockConditionGroup] = Field(default_factory=list, max_length=8)
    sell_rule_tree: BlockLogicNode | None = None
    sell_enabled: bool = Field(default=True, strict=True)
    sell_position_scope: Literal["STRATEGY", "ACCOUNT"] = "STRATEGY"
    sell_flows: list[BlockSellFlow] = Field(default_factory=list, max_length=8)
    execution_flows: list[BlockExecutionFlow] = Field(default_factory=list, max_length=8)
    buy_order_type: Literal["MARKET", "LIMIT", "BEST_LIMIT"] = "MARKET"
    sell_order_type: Literal["MARKET", "LIMIT", "BEST_LIMIT"] = "MARKET"
    buy_scan_seconds: int = Field(ge=1, le=86_400)
    buy_start_time: str
    buy_end_time: str
    bar_interval: str = Field(max_length=16)
    bar_data_mode: Literal["COMPLETE_ONLY", "INCLUDE_CURRENT"] = "COMPLETE_ONLY"
    sell_start_time: str
    sell_end_time: str
    close_time: str
    close_retry_start: str
    close_retry_end: str
    close_confirm_time: str
    route: Literal["KRX", "NXT", "SOR", "AUTO"] = "KRX"

    _validate_times = field_validator(
        "buy_start_time", "buy_end_time", "sell_start_time", "sell_end_time",
        "close_time", "close_retry_start", "close_retry_end", "close_confirm_time",
    )(_validate_clock)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_condition_lists(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        common = {
            "bar_interval": values.get("bar_interval", "3m"),
            "bar_data_mode": values.get("bar_data_mode", "COMPLETE_ONLY"),
            "bollinger_period": values.get("bollinger_period", 55),
            "bollinger_stddev": values.get("bollinger_stddev", Decimal("1.04")),
        }
        if "buy_condition_groups" not in values:
            values["buy_condition_groups"] = _legacy_condition_groups(
                values.get("buy_conditions", []),
                values.get("buy_condition_logic", "ALL"),
                prefix="buy",
                **common,
            )
        if "sell_condition_groups" not in values:
            sell_conditions = values.get("sell_conditions") or [
                item.model_dump(mode="json") for item in DEFAULT_SELL_CONDITIONS
            ]
            values["sell_condition_groups"] = _legacy_condition_groups(
                sell_conditions,
                values.get("sell_condition_logic", "ANY"),
                prefix="sell",
                **common,
            )
        values["buy_condition_groups"] = _embed_condition_calculations(
            values.get("buy_condition_groups"), prefix="buy", common=common
        )
        values["sell_condition_groups"] = _embed_condition_calculations(
            values.get("sell_condition_groups"), prefix="sell", common=common
        )
        buy_order_type = str(values.get("buy_order_type") or "MARKET")
        sell_order_type = str(values.get("sell_order_type") or "MARKET")
        # Presence of a tree makes it authoritative, even when it originated
        # from an earlier migration and still has ``legacy-*`` IDs.  This is
        # essential for round-tripping edits: compatibility groups must never
        # silently overwrite an explicitly supplied tree.
        if values.get("buy_rule_tree") is None:
            values["buy_rule_tree"] = _legacy_rule_tree_payload(
                values.get("buy_condition_groups"),
                side="BUY",
                order_type=buy_order_type,
            )
        if values.get("sell_condition_groups") and values.get("sell_rule_tree") is None:
            values["sell_rule_tree"] = _legacy_rule_tree_payload(
                values.get("sell_condition_groups"),
                side="SELL",
                order_type=sell_order_type,
            )

        migrated_buy_flows: list[dict[str, object]] = []
        for raw_flow in list(values.get("buy_flows") or []):
            flow = _condition_mapping(raw_flow)
            flow["buy_condition_groups"] = _embed_condition_calculations(
                flow.get("buy_condition_groups"), prefix="buy", common=common
            )
            if flow.get("buy_rule_tree") is None:
                flow["buy_rule_tree"] = _legacy_rule_tree_payload(
                    flow.get("buy_condition_groups"),
                    side="BUY",
                    order_type=buy_order_type,
                )
            migrated_buy_flows.append(flow)
        values["buy_flows"] = migrated_buy_flows

        migrated_sell_flows: list[dict[str, object]] = []
        for raw_flow in list(values.get("sell_flows") or []):
            flow = _condition_mapping(raw_flow)
            flow["sell_condition_groups"] = _embed_condition_calculations(
                flow.get("sell_condition_groups"), prefix="sell", common=common
            )
            if flow.get("sell_condition_groups") and flow.get("sell_rule_tree") is None:
                flow["sell_rule_tree"] = _legacy_rule_tree_payload(
                    flow.get("sell_condition_groups"),
                    side="SELL",
                    order_type=sell_order_type,
                )
            migrated_sell_flows.append(flow)
        values["sell_flows"] = migrated_sell_flows
        return values

    @field_validator("bar_interval")
    @classmethod
    def validate_bar_interval(cls, value: str) -> str:
        return _normalize_managed_bar_interval(value)

    @model_validator(mode="after")
    def validate_limits(self) -> "ConditionBollingerBlockSettings":
        if self.buy_enabled and self.buy_budget_per_symbol > self.total_operating_budget:
            raise ValueError("Per-symbol budget cannot exceed total operating budget.")
        if self.buy_enabled and time.fromisoformat(self.buy_start_time) >= time.fromisoformat(self.buy_end_time):
            raise ValueError("Buy start time must be earlier than buy end time.")
        if self.sell_enabled and time.fromisoformat(self.sell_start_time) >= time.fromisoformat(self.sell_end_time):
            raise ValueError("Sell start time must be earlier than sell end time.")
        close_times = [
            time.fromisoformat(self.close_time),
            time.fromisoformat(self.close_retry_start),
            time.fromisoformat(self.close_retry_end),
            time.fromisoformat(self.close_confirm_time),
        ]
        if close_times != sorted(close_times):
            raise ValueError("Close, retry, and confirm times must be chronological.")
        self.symbols = list(dict.fromkeys(code.strip().upper() for code in self.symbols if code.strip()))
        if self.buy_enabled and not self.buy_flows and self.target_source == "SYMBOLS" and not self.symbols:
            raise ValueError("At least one symbol is required for a symbol-list source.")
        position_only_metrics = {"average_price", "profit_rate"}
        flow_identifiers = [flow.id for flow in self.buy_flows]
        if len(flow_identifiers) != len(set(flow_identifiers)):
            raise ValueError("매수 흐름 ID는 서로 달라야 합니다.")
        sell_flow_identifiers = [flow.id for flow in self.sell_flows]
        if len(sell_flow_identifiers) != len(set(sell_flow_identifiers)):
            raise ValueError("매도 흐름 ID는 서로 달라야 합니다.")
        execution_ids = [flow.id for flow in self.execution_flows]
        if len(execution_ids) != len(set(execution_ids)):
            raise ValueError("체결 흐름 ID는 서로 달라야 합니다.")
        if self.sell_enabled and not self.sell_flows and self.sell_rule_tree is None:
            raise ValueError("매도 조건 계층이나 매도 흐름이 필요합니다.")
        if self.buy_enabled:
            _validate_rule_tree(self.buy_rule_tree, side="BUY")
        if self.sell_enabled:
            _validate_rule_tree(self.sell_rule_tree, side="SELL")
        all_buy_groups = [
            *(self.buy_condition_groups if self.buy_rule_tree is None else ()),
            *(
                group
                for flow in self.buy_flows
                if flow.buy_rule_tree is None
                for group in flow.buy_condition_groups
            ),
        ]
        all_buy_trees = [tree for tree in (
            self.buy_rule_tree,
            *(flow.buy_rule_tree for flow in self.buy_flows),
        ) if tree is not None]
        effective_sell_groups = (
            [
                group
                for flow in self.sell_flows
                if flow.sell_rule_tree is None
                for group in flow.sell_condition_groups
            ]
            if self.sell_flows
            else list(self.sell_condition_groups if self.sell_rule_tree is None else ())
        )
        effective_sell_trees = [tree for tree in (
            self.sell_rule_tree,
            *(flow.sell_rule_tree for flow in self.sell_flows),
        ) if tree is not None]
        if not self.buy_enabled:
            all_buy_groups = []
            all_buy_trees = []
        if not self.sell_enabled:
            effective_sell_groups = []
            effective_sell_trees = []
        for group in all_buy_groups:
            for condition in group.conditions:
                if any(
                    metric.metric in position_only_metrics
                    for metric in _condition_metric_leaves(condition)
                ):
                    raise ValueError(
                        "매수 조건에서는 보유 후에만 생기는 평균 매수가·수익률을 사용할 수 없습니다."
                    )
        for tree in all_buy_trees:
            for condition in _rule_tree_nodes(tree)[1]:
                if any(
                    metric.metric in position_only_metrics
                    for metric in _condition_metric_leaves(condition)
                ):
                    raise ValueError(
                        "매수 조건에서는 보유 후에만 생기는 평균 매수가·수익률을 사용할 수 없습니다."
                    )
        identifiers = [group.id for group in (*self.buy_condition_groups, *self.sell_condition_groups)]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("매수·매도 조건 블록 ID는 서로 달라야 합니다.")
        effective_conditions = [
            *(condition for group in (*all_buy_groups, *effective_sell_groups) for condition in group.conditions),
            *(condition for tree in (*all_buy_trees, *effective_sell_trees) for condition in _rule_tree_nodes(tree)[1]),
        ]
        for condition in effective_conditions:
            for source in _condition_metric_leaves(condition):
                    metric = source.metric
                    if metric in {"average_price", "profit_rate"}:
                        continue
                    offset = source.offset
                    if condition.operator in {"CROSS_UP", "CROSS_DOWN"}:
                        offset += 1
                    if metric.startswith("bollinger_"):
                        period = source.bollinger_period
                    elif metric == "atr":
                        # One primer bar supplies the previous close for the
                        # first true range. The rolling warm-up window is
                        # independent of the target offset, matching the chart.
                        period = source.atr_period + 1
                    elif metric.startswith("heikin_"):
                        period = 24
                    else:
                        period = 1
                    interval = source.bar_interval
                    max_history = 400 if interval == "1d" else 1_440 // int(interval[:-1])
                    if metric == "atr" and period <= max_history:
                        period += min(100, max_history - period)
                    required_bars = period + offset
                    if interval == "1d":
                        if required_bars > 400:
                            raise ValueError(
                                "일봉 조건에 필요한 이력은 한 번에 최대 400봉까지 지원합니다."
                            )
                        continue
                    interval_minutes = int(interval[:-1])
                    # The KIS minute-history adapter deliberately performs at most twelve
                    # 120-row calls per evaluation so one strategy cannot monopolize the
                    # shared API queue. Reject settings that can never fit that bound.
                    if required_bars * interval_minutes > 1_440:
                        raise ValueError(
                            "선택한 봉 주기·지표 기간·N봉 전 값에 필요한 이력이 관리형 1회 조회 범위를 초과합니다."
                        )
        return self


class BlockNodePosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(ge=-20_000, le=20_000)
    y: float = Field(ge=-20_000, le=20_000)


class BlockViewport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(default=24, ge=-20_000, le=20_000)
    y: float = Field(default=24, ge=-20_000, le=20_000)
    zoom: float = Field(default=0.75, ge=0.2, le=2)


class BlockEditorLayout(BaseModel):
    """Visual-only node positions. The compiler intentionally ignores them."""

    model_config = ConfigDict(extra="forbid")
    nodes: dict[str, BlockNodePosition] = Field(default_factory=dict)
    viewport: BlockViewport = Field(default_factory=BlockViewport)


class BlockStrategyPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal[1] = 1
    kind: Literal[BLOCK_PACKAGE_KIND] = BLOCK_PACKAGE_KIND
    template: Literal[BAR_OBSERVER_TEMPLATE, CONDITION_BOLLINGER_TEMPLATE]
    name: str = Field(min_length=1, max_length=120)
    settings: BlockStrategySettings | ConditionBollingerBlockSettings
    layout: BlockEditorLayout = Field(default_factory=BlockEditorLayout)
    # A settings-only optimistic concurrency token.  It intentionally excludes
    # the generated Python source and visual layout: server runtime upgrades may
    # recompile the same settings, while canvas position is local UI state.
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_template_settings(self) -> "BlockStrategyPackage":
        expected = ConditionBollingerBlockSettings if self.template == CONDITION_BOLLINGER_TEMPLATE else BlockStrategySettings
        if not isinstance(self.settings, expected):
            raise ValueError("Block template and settings do not match.")
        return self


DEFAULT_CONDITION_BOLLINGER_SETTINGS = ConditionBollingerBlockSettings(
    strategy_id="block-condition-bollinger",
    trading_account="등록한 계좌 별칭",
    condition_source="등록한 KIS 실전 API 이름",
    hts_user_id="등록한 HTS ID",
    condition_id="1",
    expected_condition_name="",
    buy_budget_per_symbol=Decimal("50000"),
    total_operating_budget=Decimal("150000"),
    max_new_buys_per_scan=5,
    bollinger_period=55,
    bollinger_stddev=Decimal("1.04"),
    take_profit_multiplier=Decimal("1.03"),
    stop_loss_multiplier=Decimal("0.97"),
    use_bollinger_exit=True,
    use_take_profit=True,
    use_stop_loss=True,
    buy_scan_seconds=30,
    buy_start_time="09:00",
    buy_end_time="15:00",
    bar_interval="3m",
    bar_data_mode="COMPLETE_ONLY",
    sell_start_time="09:00",
    sell_end_time="15:20",
    close_time="15:20",
    close_retry_start="15:20:30",
    close_retry_end="15:30",
    close_confirm_time="15:30:05",
    market_venue="KRX",
    route="KRX",
)


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _layout_literal(layout: BlockEditorLayout | None) -> str:
    payload = (layout or BlockEditorLayout()).model_dump(mode="json")
    return _literal(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _json_literal(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif isinstance(value, (list, tuple)):
        value = [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value]
    return _literal(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _layout_from_constants(constants: dict[str, object]) -> BlockEditorLayout:
    raw = constants.get("BLOCK_LAYOUT_JSON")
    if not isinstance(raw, str) or not raw:
        return BlockEditorLayout()
    try:
        return BlockEditorLayout.model_validate(json.loads(raw))
    except (ValueError, TypeError, json.JSONDecodeError):
        return BlockEditorLayout()


def _render_bar_observer(
    settings: BlockStrategySettings,
    layout: BlockEditorLayout | None = None,
) -> str:
    return f'''"""Trade Router가 관리하는 블록 전략 실행 파일."""

from trade_router import Strategy, StrategyConfig

BLOCK_TEMPLATE = "{BAR_OBSERVER_TEMPLATE}"
BLOCK_LAYOUT_JSON = {_layout_literal(layout)}
STRATEGY_ID = {_literal(settings.strategy_id)}
TRADING_ACCOUNT = {_literal(settings.trading_account)}
MARKET_SOURCE = {_literal(settings.market_source)}
SYMBOL = {_literal(settings.symbol)}
BAR_INTERVAL = {_literal(settings.bar_interval)}
BAR_COUNT = {settings.bar_count}

class ManagedBlockStrategy(Strategy):
    config = StrategyConfig(
        id=STRATEGY_ID,
        trading_account=TRADING_ACCOUNT,
        market_sources={{"primary": MARKET_SOURCE}},
        required_capabilities={{"primary": ("quotes", "bars")}},
    )

    async def on_start(self, ctx):
        ctx.scheduler.on_bar_close(
            BAR_INTERVAL,
            schedule_id={_literal(settings.schedule_id)},
            between=({_literal(settings.start_time)}, {_literal(settings.end_time)}),
        )
        ctx.log.info("블록 전략을 시작했습니다.")

    async def on_bar(self, ctx, event):
        bars = await ctx.market.bars(SYMBOL, BAR_INTERVAL, count=BAR_COUNT)
        if bars:
            ctx.log.info(f"{{event.schedule_id}}: 완성 봉 {{len(bars)}}개 · 최근 종가 {{bars[-1].close}}")

    async def on_stop(self, ctx):
        ctx.log.info("블록 전략을 종료합니다.")
'''


_CONDITION_BOLLINGER_SOURCE = '''"""Trade Router가 관리하는 종목 데이터·계산·조건 비교 블록 전략."""

import asyncio
import json
from datetime import datetime, time, timedelta
from decimal import Decimal, DecimalException
from zoneinfo import ZoneInfo

from trade_router import OrderStatus, RankingKind, RiskLimits, RouterError, ScheduleKind, Strategy, StrategyConfig, Venue
from trade_router_studio.execution_rules import ExecutionRuleManager

BLOCK_TEMPLATE = __BLOCK_TEMPLATE__
BLOCK_LAYOUT_JSON = __BLOCK_LAYOUT_JSON__
STRATEGY_ID = __STRATEGY_ID__
TRADING_ACCOUNT = __TRADING_ACCOUNT__
CONDITION_SOURCE = __CONDITION_SOURCE__
MARKET_VENUE = __MARKET_VENUE__
TARGET_SOURCE = __TARGET_SOURCE__
HTS_USER_ID = __HTS_USER_ID__
CONDITION_ID = __CONDITION_ID__
EXPECTED_CONDITION_NAME = __EXPECTED_CONDITION_NAME__
RANKING_KIND = __RANKING_KIND__
RANKING_VENUE = __RANKING_VENUE__
RANKING_MARKET = __RANKING_MARKET__
RANKING_LIMIT = __RANKING_LIMIT__
SYMBOLS_JSON = __SYMBOLS_JSON__
BUY_BUDGET_PER_SYMBOL = Decimal(__BUY_BUDGET_PER_SYMBOL__)
TOTAL_OPERATING_BUDGET = Decimal(__TOTAL_OPERATING_BUDGET__)
MAX_NEW_BUYS_PER_SCAN = __MAX_NEW_BUYS_PER_SCAN__
BOLLINGER_PERIOD = __BOLLINGER_PERIOD__
BOLLINGER_STDDEV = Decimal(__BOLLINGER_STDDEV__)
TAKE_PROFIT_MULTIPLIER = Decimal(__TAKE_PROFIT_MULTIPLIER__)
STOP_LOSS_MULTIPLIER = Decimal(__STOP_LOSS_MULTIPLIER__)
USE_BOLLINGER_EXIT = __USE_BOLLINGER_EXIT__
USE_TAKE_PROFIT = __USE_TAKE_PROFIT__
USE_STOP_LOSS = __USE_STOP_LOSS__
BUY_CONDITION_LOGIC = __BUY_CONDITION_LOGIC__
BUY_CONDITIONS_JSON = __BUY_CONDITIONS_JSON__
SELL_CONDITION_LOGIC = __SELL_CONDITION_LOGIC__
SELL_CONDITIONS_JSON = __SELL_CONDITIONS_JSON__
BUY_CONDITION_GROUPS_JSON = __BUY_CONDITION_GROUPS_JSON__
BUY_RULE_TREE_JSON = __BUY_RULE_TREE_JSON__
BUY_ENABLED = __BUY_ENABLED__
BUY_FLOWS_JSON = __BUY_FLOWS_JSON__
SELL_CONDITION_GROUPS_JSON = __SELL_CONDITION_GROUPS_JSON__
SELL_RULE_TREE_JSON = __SELL_RULE_TREE_JSON__
SELL_ENABLED = __SELL_ENABLED__
SELL_POSITION_SCOPE = __SELL_POSITION_SCOPE__
SELL_FLOWS_JSON = __SELL_FLOWS_JSON__
EXECUTION_FLOWS_JSON = __EXECUTION_FLOWS_JSON__
BUY_ORDER_TYPE = __BUY_ORDER_TYPE__
SELL_ORDER_TYPE = __SELL_ORDER_TYPE__
BUY_SCAN_SECONDS = __BUY_SCAN_SECONDS__
BUY_START_TIME = __BUY_START_TIME__
BUY_END_TIME = __BUY_END_TIME__
BAR_INTERVAL = __BAR_INTERVAL__
BAR_DATA_MODE = __BAR_DATA_MODE__
SELL_START_TIME = __SELL_START_TIME__
SELL_END_TIME = __SELL_END_TIME__
CLOSE_TIME = __CLOSE_TIME__
CLOSE_RETRY_START = __CLOSE_RETRY_START__
CLOSE_RETRY_END = __CLOSE_RETRY_END__
CLOSE_CONFIRM_TIME = __CLOSE_CONFIRM_TIME__
ORDER_ROUTE = __ORDER_ROUTE__
SEOUL = ZoneInfo("Asia/Seoul")

SYMBOLS = tuple(json.loads(SYMBOLS_JSON))
BUY_CONDITIONS = tuple(json.loads(BUY_CONDITIONS_JSON))
SELL_CONDITIONS = tuple(json.loads(SELL_CONDITIONS_JSON))
BUY_CONDITION_GROUPS = tuple(json.loads(BUY_CONDITION_GROUPS_JSON))
SELL_CONDITION_GROUPS = tuple(json.loads(SELL_CONDITION_GROUPS_JSON))
BUY_RULE_TREE = json.loads(BUY_RULE_TREE_JSON)
SELL_RULE_TREE = json.loads(SELL_RULE_TREE_JSON)
CONFIGURED_BUY_FLOWS = tuple(json.loads(BUY_FLOWS_JSON))
RAW_BUY_FLOWS = () if not BUY_ENABLED else CONFIGURED_BUY_FLOWS or ({
    "id": "default", "name": "매수 흐름", "target_source": TARGET_SOURCE,
    "hts_user_id": HTS_USER_ID, "condition_id": CONDITION_ID,
    "expected_condition_name": EXPECTED_CONDITION_NAME,
    "ranking_kind": RANKING_KIND, "ranking_venue": RANKING_VENUE,
    "ranking_market": RANKING_MARKET, "ranking_limit": RANKING_LIMIT,
    "symbols": SYMBOLS, "buy_scan_seconds": BUY_SCAN_SECONDS,
    "buy_start_time": BUY_START_TIME, "buy_end_time": BUY_END_TIME,
    "buy_condition_groups": BUY_CONDITION_GROUPS, "buy_rule_tree": BUY_RULE_TREE,
},)
CONFIGURED_SELL_FLOWS = tuple(json.loads(SELL_FLOWS_JSON))
EXECUTION_FLOWS = tuple(json.loads(EXECUTION_FLOWS_JSON))
RAW_SELL_FLOWS = () if not SELL_ENABLED else CONFIGURED_SELL_FLOWS or ({
    "id": "default", "name": "매도 흐름",
    "sell_scan_seconds": BUY_SCAN_SECONDS,
    "sell_start_time": SELL_START_TIME, "sell_end_time": SELL_END_TIME,
    "sell_condition_groups": SELL_CONDITION_GROUPS, "sell_rule_tree": SELL_RULE_TREE,
},)


def _legacy_rule_tree(groups, side, order_type):
    """Adapt the former outer-AND/inner-OR layout without changing exports."""
    side_key = side.lower()
    children = []
    for group_index, group in enumerate(groups, start=1):
        children.append({
            "kind": "logic",
            "id": f"legacy-{side_key}-group-{group_index}",
            "operator": "ANY",
            "children": [
                {
                    "kind": "condition",
                    "id": f"legacy-{side_key}-condition-{group_index}-{condition_index}",
                    "condition": condition,
                    "action": None,
                }
                for condition_index, condition in enumerate(group["conditions"], start=1)
            ],
            "action": None,
        })
    return {
        "kind": "logic",
        "id": f"legacy-{side_key}-root",
        "operator": "ALL",
        "children": children,
        "action": {
            "kind": "order",
            "id": f"legacy-{side_key}-order",
            "side": side,
            "order_type": order_type,
        },
    }


def _effective_rule_flow(flow, side, order_type):
    key = f"{side.lower()}_rule_tree"
    groups_key = f"{side.lower()}_condition_groups"
    tree = flow.get(key) or _legacy_rule_tree(flow.get(groups_key, ()), side, order_type)
    return {**flow, key: tree}


BUY_FLOWS = tuple(
    _effective_rule_flow(flow, "BUY", BUY_ORDER_TYPE) for flow in RAW_BUY_FLOWS
)
SELL_FLOWS = tuple(
    _effective_rule_flow(flow, "SELL", SELL_ORDER_TYPE) for flow in RAW_SELL_FLOWS
)
BUY_FLOW_SCHEDULES = {
    (f"condition-scan-{flow['id']}" if CONFIGURED_BUY_FLOWS else "condition-scan"): flow
    for flow in BUY_FLOWS
}
BAR_METRICS = {
    "open", "high", "low", "close", "volume",
    "heikin_open", "heikin_high", "heikin_low", "heikin_close",
    "bollinger_upper", "bollinger_middle", "bollinger_lower",
    "obv", "atr",
}


def _expression_metric_leaves(expression):
    if not expression:
        return ()
    if expression.get("kind") == "metric":
        return (expression,)
    if expression.get("kind") != "binary":
        return ()
    return (
        *_expression_metric_leaves(expression.get("left")),
        *_expression_metric_leaves(expression.get("right")),
    )


def _condition_metric_names(condition):
    names = []
    for side in ("left", "right"):
        expression = condition.get(f"{side}_expression")
        if expression:
            names.extend(item["metric"] for item in _expression_metric_leaves(expression))
        else:
            names.append(condition.get(side))
    return tuple(name for name in names if name)


def _rule_conditions(node):
    if not node:
        return ()
    if node.get("kind") == "condition":
        condition = node.get("condition")
        return (condition,) if condition else ()
    return tuple(
        condition
        for child in node.get("children", ())
        for condition in _rule_conditions(child)
    )


NEEDS_BARS = any(
    metric in BAR_METRICS
    for tree in (
        *[flow["buy_rule_tree"] for flow in BUY_FLOWS],
        *[flow["sell_rule_tree"] for flow in SELL_FLOWS],
    )
    for condition in _rule_conditions(tree)
    for metric in _condition_metric_names(condition)
)
DISCOVERY_CAPABILITIES = tuple(dict.fromkeys(
    {"HTS_CONDITION": "conditions", "RANKING": "rankings"}.get(flow["target_source"])
    for flow in BUY_FLOWS
))
REQUIRED_CAPABILITIES = tuple(
    item for item in (*DISCOVERY_CAPABILITIES, "quotes", "bars" if NEEDS_BARS else None) if item
)


class ManagedBlockStrategy(Strategy):
    config = StrategyConfig(
        id=STRATEGY_ID,
        trading_account=TRADING_ACCOUNT,
        market_sources={"primary": CONDITION_SOURCE},
        required_capabilities={"primary": REQUIRED_CAPABILITIES},
        limits=RiskLimits(max_exposure=TOTAL_OPERATING_BUDGET),
    )

    @staticmethod
    def _instrument_label(ctx, code):
        names = dict(ctx.state.get("instrument_names", {}))
        return names.get(code) or code

    @staticmethod
    def _buy_exclusions_for_day(ctx, at):
        local_at = at.astimezone(SEOUL) if at.tzinfo is not None else at
        business_date = local_at.date().isoformat()
        stored = ctx.state.get("daily_buy_exclusions", {})
        if not isinstance(stored, dict) or stored.get("date") != business_date:
            stored = {"date": business_date, "symbols": {}}
            ctx.state.set("daily_buy_exclusions", stored)
        else:
            stored = {
                "date": business_date,
                "symbols": dict(stored.get("symbols", {})),
            }
        return stored

    @staticmethod
    def _is_deterministic_aftermarket_rejection(message):
        text = str(message or "")
        compact = "".join(text.split())
        return (
            "APBK3061" in text
            or "애프터지정가/최유리/최우선주문만가능" in compact
        )

    @classmethod
    def _is_non_retryable_buy_rejection(cls, message):
        text = str(message or "")
        compact = "".join(text.split())
        return (
            "APBK1943" in text
            or "시장가호가불가" in compact
            or cls._is_deterministic_aftermarket_rejection(message)
        )

    @classmethod
    def _is_non_retryable_sell_rejection(cls, message):
        # APBK1943 is a transient/price-state market-order rejection for sells.
        # The user's no-retry choice applies to rejected buys only.  A sell is
        # suppressed only when the broker deterministically rejects the order
        # type for the after-market session.
        return cls._is_deterministic_aftermarket_rejection(message)

    @classmethod
    def _exclude_buy_for_day(cls, ctx, at, code, message):
        exclusions = cls._buy_exclusions_for_day(ctx, at)
        exclusions["symbols"][code] = {
            "reason": str(message or "시장가 주문 불가"),
            "excluded_at": at.isoformat(),
        }
        ctx.state.set("daily_buy_exclusions", exclusions)

    @staticmethod
    def _order_session(at):
        local_at = at.astimezone(SEOUL) if at.tzinfo is not None else at
        local_time = local_at.time().replace(tzinfo=None)
        if time(8, 0) <= local_time < time(8, 50):
            return "NXT_PRE"
        if time(9, 0) <= local_time < time(15, 30):
            return "REGULAR"
        if time(15, 30) <= local_time < time(20, 0):
            return "NXT_AFTER"
        return "CLOSED"

    @staticmethod
    def _order_type_session_is_open(at, route, order_type):
        # BEST_LIMIT is a continuous-trading order. A temporary auction/pause
        # must not create a daily rejection or suppress the next valid session.
        if order_type != "BEST_LIMIT":
            return True
        local_at = at.astimezone(SEOUL) if at.tzinfo is not None else at
        local_time = local_at.time().replace(tzinfo=None)
        regular_open = time(9, 0, 30) if route == "NXT" else time(9, 0)
        if regular_open <= local_time < time(15, 20):
            return True
        # AUTO is resolved to SOR by live KIS connections. Paper connections
        # retain their broker capability restrictions when the order is sent.
        return route in {"NXT", "SOR", "AUTO"} and (
            time(8, 0) <= local_time < time(8, 50)
            or time(15, 40) <= local_time < time(20, 0)
        )

    @classmethod
    def _order_session_block_reason(cls, at, route, order_type):
        if cls._order_session(at) != "NXT_AFTER":
            return None
        if route == "KRX":
            if order_type == "MARKET":
                return (
                    "애프터마켓에서는 현재 결정된 KRX 경로와 시장가 주문을 사용할 수 없습니다. "
                    "NXT 거래 가능 여부를 확인하고 NXT 또는 SOR 경로의 지정가로 설정해야 합니다."
                )
            return (
                "애프터마켓에서는 현재 결정된 KRX 주문 경로를 사용할 수 없습니다. "
                "NXT 거래 가능 여부를 확인하고 NXT 또는 SOR 경로로 설정해야 합니다."
            )
        if order_type == "MARKET":
            return (
                f"애프터마켓에서는 {route} 시장가 주문을 제출할 수 없습니다. "
                "주문 방식을 지정가로 설정하거나 최유리지정가를 선택해야 합니다."
            )
        return None

    @staticmethod
    def _order_suppressions_for_day(ctx, at):
        local_at = at.astimezone(SEOUL) if at.tzinfo is not None else at
        business_date = local_at.date().isoformat()
        stored = ctx.state.get("daily_order_suppressions", {})
        if not isinstance(stored, dict) or stored.get("date") != business_date:
            stored = {"date": business_date, "orders": {}}
            ctx.state.set("daily_order_suppressions", stored)
        else:
            stored = {
                "date": business_date,
                "orders": dict(stored.get("orders", {})),
            }
        return stored

    @classmethod
    def _order_suppression_key(cls, at, code, side, order_type, purpose="CONDITION"):
        return f"{cls._order_session(at)}:{purpose}:{side}:{order_type}:{code}"

    @classmethod
    def _order_attempt_is_suppressed(
        cls, ctx, at, code, side, order_type, purpose="CONDITION"
    ):
        stored = cls._order_suppressions_for_day(ctx, at)
        key = cls._order_suppression_key(at, code, side, order_type, purpose)
        return key in stored["orders"]

    @classmethod
    def _suppress_order_for_day(
        cls, ctx, at, code, side, order_type, message, purpose="CONDITION"
    ):
        stored = cls._order_suppressions_for_day(ctx, at)
        key = cls._order_suppression_key(at, code, side, order_type, purpose)
        is_new = key not in stored["orders"]
        stored["orders"][key] = {
            "reason": str(message or "주문을 다시 제출할 수 없는 상태"),
            "suppressed_at": at.isoformat(),
        }
        ctx.state.set("daily_order_suppressions", stored)
        return is_new

    @staticmethod
    def _order_routes_for_day(ctx, at):
        local_at = at.astimezone(SEOUL) if at.tzinfo is not None else at
        business_date = local_at.date().isoformat()
        stored = ctx.state.get("daily_order_routes", {})
        if not isinstance(stored, dict) or stored.get("date") != business_date:
            stored = {"date": business_date, "routes": {}}
            ctx.state.set("daily_order_routes", stored)
        else:
            stored = {
                "date": business_date,
                "routes": dict(stored.get("routes", {})),
            }
        return stored

    @classmethod
    def _order_route_cache_key(cls, code, at):
        # NXT availability changes between the pre-market, regular market, and
        # after-market sessions.  A KRX fallback observed in one session must
        # not be reused for the rest of the day.
        return f"{cls._order_session(at)}:{code}"

    @classmethod
    async def _effective_order_route(cls, ctx, code, at, *, current_price=None):
        if ORDER_ROUTE != "SOR":
            return ORDER_ROUTE
        route_state = cls._order_routes_for_day(ctx, at)
        cache_key = cls._order_route_cache_key(code, at)
        cached = route_state["routes"].get(cache_key)
        if cached in {"SOR", "KRX"}:
            return cached

        nxt_usable = False
        try:
            nxt_quote = await ctx.market.quote(code, venue=Venue.NXT)
            nxt_usable = (
                nxt_quote.price > 0
                and not nxt_quote.delayed
                and not nxt_quote.stale
            )
        except RouterError:
            pass

        effective = "SOR"
        if not nxt_usable:
            krx_usable = current_price is not None and current_price > 0
            if not krx_usable:
                try:
                    krx_quote = await ctx.market.quote(code, venue=Venue.KRX)
                    krx_usable = (
                        krx_quote.price > 0
                        and not krx_quote.delayed
                        and not krx_quote.stale
                    )
                except RouterError:
                    krx_usable = False
            if krx_usable:
                effective = "KRX"

        route_state["routes"][cache_key] = effective
        ctx.state.set("daily_order_routes", route_state)
        if effective == "KRX":
            ctx.log.info(
                f"{cls._instrument_label(ctx, code)} 주문 경로: NXT 거래 시세가 없어 KRX로 전송합니다."
            )
        return effective

    async def _candidate_codes(self, ctx, flow=None):
        if not BUY_FLOWS:
            return []
        flow = flow or BUY_FLOWS[0]
        if flow["target_source"] == "HTS_CONDITION":
            rows = await ctx.discovery.matches(flow["hts_user_id"], flow["condition_id"])
            instruments = [(item.instrument.code, item.name) for item in rows]
            label = f"HTS 조건검색 {flow['condition_id']}"
        elif flow["target_source"] == "RANKING":
            rows = await ctx.discovery.ranking(
                RankingKind(flow["ranking_kind"]),
                venue=Venue(flow["ranking_venue"]),
                market=flow["ranking_market"],
                limit=flow["ranking_limit"],
            )
            instruments = [(item.instrument.code, item.name) for item in rows]
            label = f"{flow['ranking_kind']} 순위"
        else:
            instruments = [(code, None) for code in flow["symbols"]]
            label = "지정 종목"
        codes = [code for code, _ in instruments]
        unique = list(dict.fromkeys(code.strip().upper() for code in codes if code.strip()))
        names = dict(ctx.state.get("instrument_names", {}))
        for raw_code, name in instruments:
            code = raw_code.strip().upper()
            clean_name = str(name or "").strip()
            if code and clean_name:
                names[code] = clean_name
        ctx.state.set("instrument_names", names)
        name_text = ", ".join(names.get(code) or code for code in unique) if unique else "없음"
        flow_text = f"{flow['name']} · " if CONFIGURED_BUY_FLOWS else ""
        ctx.log.info(
            f"{flow_text}{label} {len(unique)}종목 [{name_text}] · 신규 매수 후보를 확인합니다."
        )
        return unique

    @staticmethod
    def _condition_source(condition, side):
        return {
            "metric": condition[side],
            "bar_interval": condition[f"{side}_bar_interval"],
            "bar_data_mode": condition[f"{side}_bar_data_mode"],
            "bollinger_period": int(condition[f"{side}_bollinger_period"]),
            "bollinger_stddev": Decimal(str(condition[f"{side}_bollinger_stddev"])),
            "atr_period": int(condition.get(f"{side}_atr_period", 14)),
            "offset": int(condition.get(f"{side}_offset", 0)),
        }

    @staticmethod
    def _expression_source(expression):
        return {
            "metric": expression["metric"],
            "bar_interval": expression.get("bar_interval", "3m"),
            "bar_data_mode": expression.get("bar_data_mode", "COMPLETE_ONLY"),
            "bollinger_period": int(expression.get("bollinger_period", 55)),
            "bollinger_stddev": Decimal(str(expression.get("bollinger_stddev", "1.04"))),
            "atr_period": int(expression.get("atr_period", 14)),
            "offset": int(expression.get("offset", 0)),
        }

    @classmethod
    def _condition_sources(cls, condition):
        sources = []
        for side in ("left", "right"):
            expression = condition.get(f"{side}_expression")
            if expression:
                sources.extend(
                    cls._expression_source(item)
                    for item in _expression_metric_leaves(expression)
                )
            elif not (side == "right" and condition.get("right") == "value"):
                sources.append(cls._condition_source(condition, side))
        return sources

    @staticmethod
    def _source_uses_bars(source):
        return source["metric"] not in {"value", "average_price", "profit_rate"}

    @staticmethod
    def _source_key(source):
        return source["bar_interval"], source["bar_data_mode"] == "COMPLETE_ONLY"

    @staticmethod
    def _max_source_history(source):
        if source["bar_interval"] == "1d":
            return 400
        return 1440 // int(source["bar_interval"][:-1])

    @classmethod
    def _required_source_bars(cls, source, *, crossing=False):
        if not cls._source_uses_bars(source):
            return 0
        offset = source["offset"] + (1 if crossing else 0)
        if source["metric"].startswith("bollinger_"):
            period = source["bollinger_period"]
        elif source["metric"] == "atr":
            # A primer bar supplies the previous close for the first true
            # range. The fixed rolling window does not shrink when evaluating
            # an older offset or the previous point of a crossing condition.
            base = source["atr_period"] + 1
            maximum = cls._max_source_history(source)
            window = base + min(100, maximum - base) if base <= maximum else base
            return window + offset
        elif source["metric"].startswith("heikin_"):
            # The recursive Heikin-Ashi open converges quickly.  A fixed warm-up
            # keeps comparisons stable without forcing a full-session download.
            period = 24
        else:
            period = 1
        return offset + period

    @classmethod
    def _condition_requests(cls, condition):
        requests = {}
        crossing = condition["operator"] in {"CROSS_UP", "CROSS_DOWN"}
        for source in cls._condition_sources(condition):
            required = cls._required_source_bars(source, crossing=crossing)
            if required:
                key = cls._source_key(source)
                requests[key] = max(requests.get(key, 0), required)
        return requests

    @staticmethod
    def _bollinger_values(bars, offset, source):
        period = int(source["bollinger_period"])
        stddev = Decimal(str(source["bollinger_stddev"]))
        end = len(bars) - offset
        start = end - period
        if start < 0 or end <= 0:
            return None
        closes = [bar.close for bar in bars[start:end]]
        mean = sum(closes, Decimal("0")) / Decimal(period)
        variance = sum(((value - mean) ** 2 for value in closes), Decimal("0")) / Decimal(period)
        deviation = stddev * variance.sqrt()
        return mean + deviation, mean, mean - deviation

    @staticmethod
    def _heikin_ashi_values(bars):
        values = []
        previous_open = None
        previous_close = None
        for bar in bars:
            close = (bar.open + bar.high + bar.low + bar.close) / Decimal("4")
            open_value = (
                (bar.open + bar.close) / Decimal("2")
                if previous_open is None or previous_close is None
                else (previous_open + previous_close) / Decimal("2")
            )
            high = max(bar.high, open_value, close)
            low = min(bar.low, open_value, close)
            values.append((open_value, high, low, close))
            previous_open, previous_close = open_value, close
        return values

    @staticmethod
    def _obv_values(bars):
        """Return an OBV series anchored at zero for the fetched window.

        OBV levels depend on the arbitrary beginning of a data set, while the
        difference between two points does not.  Starting the first point at
        zero therefore preserves comparisons such as the latest completed OBV
        versus the value ten completed bars earlier without requesting the
        instrument's complete trading history.
        """
        values = []
        current = Decimal("0")
        previous_close = None
        for bar in bars:
            if previous_close is not None:
                if bar.close > previous_close:
                    current += bar.volume
                elif bar.close < previous_close:
                    current -= bar.volume
            values.append(current)
            previous_close = bar.close
        return values

    @staticmethod
    def _atr_values(bars, period):
        """Return Wilder ATR values aligned to ``bars``.

        The first bar primes the previous close. Each later true range is the
        maximum of ``high-low``, ``abs(high-previous close)``, and
        ``abs(low-previous close)``. The first ATR is the SMA of the first
        ``period`` true ranges; subsequent values use Wilder's recursive
        ``(previous ATR * (period - 1) + TR) / period`` smoothing.
        """
        period = int(period)
        if period < 1:
            return [None for _ in bars]
        true_ranges = [None]
        for index in range(1, len(bars)):
            bar = bars[index]
            previous_close = bars[index - 1].close
            true_ranges.append(max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            ))
        values = [None for _ in bars]
        if len(bars) < period + 1:
            return values
        current = sum(true_ranges[1:period + 1], Decimal("0")) / Decimal(period)
        values[period] = current
        for index in range(period + 1, len(true_ranges)):
            current = (
                current * Decimal(period - 1) + true_ranges[index]
            ) / Decimal(period)
            values[index] = current
        return values

    def _indicator_value(self, metric, bars, offset, average_price, source, current_price=None):
        if metric == "average_price":
            return average_price if average_price and average_price > 0 else None
        if metric == "profit_rate":
            rates = getattr(self, "_profit_cost_rates", None)
            if rates is None or not average_price or average_price <= 0 or current_price is None or current_price <= 0:
                return None
            buy_fee, sell_fee, sell_tax = rates
            purchase_cost = average_price * (Decimal("1") + buy_fee)
            net_sale = current_price * (Decimal("1") - sell_fee - sell_tax)
            return (net_sale / purchase_cost - Decimal("1")) * Decimal("100")
        index = len(bars) - 1 - offset
        if index < 0:
            return None
        bar = bars[index]
        if metric in {"open", "high", "low", "close", "volume"}:
            return getattr(bar, metric)
        if metric in {"heikin_open", "heikin_high", "heikin_low", "heikin_close"}:
            values = self._heikin_ashi_values(bars)
            if index >= len(values):
                return None
            return values[index][{
                "heikin_open": 0,
                "heikin_high": 1,
                "heikin_low": 2,
                "heikin_close": 3,
            }[metric]]
        if metric == "obv":
            values = self._obv_values(bars)
            return values[index] if index < len(values) else None
        if metric == "atr":
            values = self._atr_values(bars, source["atr_period"])
            return values[index] if index < len(values) else None
        if metric in {"bollinger_upper", "bollinger_middle", "bollinger_lower"}:
            values = self._bollinger_values(bars, offset, source)
            if values is None:
                return None
            return values[{"bollinger_upper": 0, "bollinger_middle": 1, "bollinger_lower": 2}[metric]]
        return None

    def _condition_operand_value(
        self, condition, side, bars_by_request, average_price, extra_offset=0,
        current_price=None, crossing=False,
    ):
        if side == "right" and condition["right"] == "value":
            return Decimal(str(condition.get("value", "0")))
        source = self._condition_source(condition, side)
        bars = ()
        if self._source_uses_bars(source):
            bars = bars_by_request.get(self._source_key(source), ())
            if source["metric"] in {"atr"} or source["metric"].startswith("heikin_"):
                value_source = {**source, "offset": source["offset"] + extra_offset}
                required = self._required_source_bars(value_source)
                bars = bars[-required:]
        return self._indicator_value(
            source["metric"],
            bars,
            source["offset"] + extra_offset,
            average_price,
            source,
            current_price,
        )

    def _expression_value(
        self, expression, bars_by_request, average_price, extra_offset=0,
        current_price=None, crossing=False,
    ):
        kind = expression.get("kind")
        if kind == "constant":
            value = Decimal(str(expression.get("value", "0")))
            return value if value.is_finite() else None
        if kind == "metric":
            source = self._expression_source(expression)
            bars = ()
            if self._source_uses_bars(source):
                bars = bars_by_request.get(self._source_key(source), ())
                if source["metric"] in {"atr"} or source["metric"].startswith("heikin_"):
                    value_source = {**source, "offset": source["offset"] + extra_offset}
                    required = self._required_source_bars(value_source)
                    bars = bars[-required:]
            return self._indicator_value(
                source["metric"],
                bars,
                source["offset"] + extra_offset,
                average_price,
                source,
                current_price,
            )
        if kind != "binary":
            return None
        left = self._expression_value(
            expression["left"], bars_by_request, average_price,
            extra_offset, current_price, crossing,
        )
        right = self._expression_value(
            expression["right"], bars_by_request, average_price,
            extra_offset, current_price, crossing,
        )
        if left is None or right is None:
            return None
        try:
            operator = expression["operator"]
            if operator == "ADD":
                result = left + right
            elif operator == "SUBTRACT":
                result = left - right
            elif operator == "MULTIPLY":
                result = left * right
            elif operator == "DIVIDE":
                if right == 0:
                    return None
                result = left / right
            else:
                return None
        except (DecimalException, ArithmeticError):
            return None
        return result if result.is_finite() else None

    def _condition_values(
        self, condition, bars_by_request, average_price, extra_offset=0,
        current_price=None, crossing=False,
    ):
        values = []
        for side in ("left", "right"):
            expression = condition.get(f"{side}_expression")
            if expression:
                values.append(self._expression_value(
                    expression, bars_by_request, average_price, extra_offset, current_price, crossing,
                ))
            else:
                values.append(self._condition_operand_value(
                    condition, side, bars_by_request, average_price, extra_offset, current_price, crossing,
                ))
        return tuple(values)

    def _condition_matches(self, condition, bars_by_request, average_price, current_price=None):
        crossing = condition["operator"] in {"CROSS_UP", "CROSS_DOWN"}
        left, right = self._condition_values(
            condition, bars_by_request, average_price, current_price=current_price,
            crossing=crossing,
        )
        if left is None or right is None:
            return False
        operator = condition["operator"]
        if operator == "LT":
            return left < right
        if operator == "LTE":
            return left <= right
        if operator == "GT":
            return left > right
        if operator == "GTE":
            return left >= right
        if operator == "EQ":
            return left == right
        previous_left, previous_right = self._condition_values(
            condition, bars_by_request, average_price, 1, current_price, crossing,
        )
        if previous_left is None or previous_right is None:
            return False
        if operator == "CROSS_UP":
            return previous_left <= previous_right and left > right
        if operator == "CROSS_DOWN":
            return previous_left >= previous_right and left < right
        return False

    @staticmethod
    async def _condition_history(ctx, code, key, required, existing=()):
        """Fetch one condition series without exceeding the router's page limit."""
        interval, complete_only = key
        seed = tuple(
            bar for bar in existing
            if not complete_only or getattr(bar, "complete", True)
        )
        if any(not hasattr(bar, "open_time") for bar in seed):
            # Production Bar objects always have open_time. A custom adapter
            # without it cannot expose a stable cursor for another page.
            return seed[-required:]

        collected = {
            bar.open_time: bar
            for bar in seed
        }
        if len(collected) >= required:
            return tuple(
                bar for _, bar in sorted(collected.items(), key=lambda item: item[0])
            )[-required:]
        end = (
            min(bar.open_time for bar in seed) - timedelta(microseconds=1)
            if seed else None
        )
        while len(collected) < required:
            page_count = min(400, required - len(collected))
            page = tuple(await ctx.market.bars(
                code,
                interval,
                count=page_count,
                venue=Venue(MARKET_VENUE),
                complete_only=complete_only,
                end=end,
            ))
            if not page:
                break
            eligible = tuple(
                bar for bar in page
                if not complete_only or getattr(bar, "complete", True)
            )
            if any(not hasattr(bar, "open_time") for bar in page):
                # Keep simple test/custom adapters compatible, but do not try
                # to paginate without a stable time cursor.
                return tuple((*collected.values(), *eligible))[-required:]
            for bar in eligible:
                collected.setdefault(bar.open_time, bar)
            next_end = min(bar.open_time for bar in page) - timedelta(microseconds=1)
            if end is not None and next_end >= end:
                break
            end = next_end
        return tuple(
            bar for _, bar in sorted(collected.items(), key=lambda item: item[0])
        )[-required:]

    async def _condition_groups_result(
        self, ctx, code, groups, *, average_price=None, current_price=None, side,
    ):
        bars_by_request = {}
        fetched_counts = {}
        matched_groups = []
        needs_current_price = any(
            "profit_rate" in _condition_metric_names(condition)
            for group in groups
            for condition in group["conditions"]
        )
        if needs_current_price and current_price is None:
            try:
                current_price = await self._current_price(ctx, code)
            except RouterError as exc:
                ctx.log.warning(
                    f"{self._instrument_label(ctx, code)} {side} 실시간 수익률 조회 실패 "
                    f"[{exc.code.value}]: {exc.message}"
                )
        for group in groups:
            matched = []
            ordered_conditions = sorted(
                group["conditions"],
                key=lambda item: not self._is_live_profit_condition(item),
            )
            for condition in ordered_conditions:
                for key, required in self._condition_requests(condition).items():
                    if fetched_counts.get(key, 0) >= required:
                        continue
                    try:
                        bars_by_request[key] = await self._condition_history(
                            ctx, code, key, required,
                            existing=bars_by_request.get(key, ()),
                        )
                        fetched_counts[key] = required
                    except RouterError as exc:
                        bars_by_request[key] = ()
                        fetched_counts[key] = required
                        ctx.log.warning(f"{self._instrument_label(ctx, code)} {side} 조건 자료 조회 실패 [{exc.code.value}]: {exc.message}")
                if self._condition_matches(
                    condition, bars_by_request, average_price, current_price,
                ):
                    matched.append(condition)
                    break
            if not matched:
                return False, matched_groups
            matched_groups.append(matched)
        return True, matched_groups

    async def _rule_tree_result(
        self, ctx, code, tree, *, average_price=None, current_price=None, side,
    ):
        """Evaluate a parenthesized rule tree and select one branch action.

        ANY is ordered like an if/elif chain: the first true child wins.  ALL
        requires every child and keeps the first child action, falling back to
        the group's own action.  Evaluation is side-effect free; callers submit
        at most one selected order after account and duplicate-order checks.
        """
        if not tree:
            return False, [], None
        bars_by_request = {}
        fetched_counts = {}
        conditions = _rule_conditions(tree)
        needs_current_price = any(
            "profit_rate" in _condition_metric_names(condition)
            for condition in conditions
        )
        if needs_current_price and current_price is None:
            try:
                current_price = await self._current_price(ctx, code)
            except RouterError as exc:
                ctx.log.warning(
                    f"{self._instrument_label(ctx, code)} {side} 실시간 수익률 조회 실패 "
                    f"[{exc.code.value}]: {exc.message}"
                )

        async def evaluate(node, depth=0):
            if node.get("kind") == "condition":
                condition = node["condition"]
                for key, required in self._condition_requests(condition).items():
                    if fetched_counts.get(key, 0) >= required:
                        continue
                    try:
                        bars_by_request[key] = await self._condition_history(
                            ctx, code, key, required,
                            existing=bars_by_request.get(key, ()),
                        )
                        fetched_counts[key] = required
                    except RouterError as exc:
                        bars_by_request[key] = ()
                        fetched_counts[key] = required
                        ctx.log.warning(
                            f"{self._instrument_label(ctx, code)} {side} 조건 자료 조회 실패 "
                            f"[{exc.code.value}]: {exc.message}"
                        )
                matched = self._condition_matches(
                    condition, bars_by_request, average_price, current_price,
                )
                return (
                    matched,
                    [condition] if matched else [],
                    node.get("action") if matched else None,
                )

            children = node.get("children", ())
            if not children:
                # An empty ALL would be vacuously true.  That is useful only
                # for the legacy/direct-buy form (condition-search results are
                # bought without an extra indicator filter).  In every other
                # position, especially SELL, fail closed so deleting the last
                # condition can never become an unconditional order.
                action = node.get("action")
                if depth == 0 and action and action.get("side") == "BUY":
                    return True, [], action
                return False, [], None

            operator = node.get("operator", "ALL")
            if operator == "ANY":
                for child in children:
                    matched, branch_conditions, branch_action = await evaluate(
                        child, depth + 1,
                    )
                    if matched:
                        return (
                            True,
                            branch_conditions,
                            branch_action or node.get("action"),
                        )
                return False, [], None

            matched_conditions = []
            selected_action = None
            for child in children:
                matched, branch_conditions, branch_action = await evaluate(
                    child, depth + 1,
                )
                if not matched:
                    return False, [], None
                matched_conditions.extend(branch_conditions)
                if selected_action is None and branch_action is not None:
                    selected_action = branch_action
            return True, matched_conditions, selected_action or node.get("action")

        return await evaluate(tree)

    @classmethod
    def _expression_summary(cls, expression):
        if expression.get("kind") == "constant":
            return str(expression.get("value", "0"))
        if expression.get("kind") == "metric":
            metric = expression.get("metric", "?")
            if metric == "atr":
                return f"ATR({expression.get('atr_period', 14)})"
            return str(metric)
        operators = {"ADD": "+", "SUBTRACT": "-", "MULTIPLY": "×", "DIVIDE": "÷"}
        left = cls._expression_summary(expression.get("left", {}))
        right = cls._expression_summary(expression.get("right", {}))
        return f"({left} {operators.get(expression.get('operator'), '?')} {right})"

    @classmethod
    def _condition_summary(cls, condition):
        operators = {
            "LT": "<", "LTE": "≤", "GT": ">", "GTE": "≥", "EQ": "=",
            "CROSS_UP": "상향돌파", "CROSS_DOWN": "하향돌파",
        }
        left = (
            cls._expression_summary(condition["left_expression"])
            if condition.get("left_expression")
            else condition["left"]
        )
        right = (
            cls._expression_summary(condition["right_expression"])
            if condition.get("right_expression")
            else condition["value"] if condition["right"] == "value" else condition["right"]
        )
        return f"{left} {operators.get(condition['operator'], condition['operator'])} {right}"

    @staticmethod
    async def _current_price(ctx, code):
        quote = await ctx.market.quote(code, venue=Venue(MARKET_VENUE))
        return quote.price

    def _ensure_live_state(self):
        if not hasattr(self, "_live_quote_subscriptions"):
            self._live_quote_subscriptions = {}
            self._live_positions = {}
            self._live_sell_pending = set()
        if not hasattr(self, "_last_live_quote_at"):
            self._last_live_quote_at = {}
        if not hasattr(self, "_empty_live_reconcile_at"):
            self._empty_live_reconcile_at = None
        if not hasattr(self, "_account_sell_reconcile_at"):
            self._account_sell_reconcile_at = None
        if not hasattr(self, "_profit_costs_refreshed_at"):
            self._profit_costs_refreshed_at = None
        if not hasattr(self, "_live_sell_recheck_at"):
            self._live_sell_recheck_at = {}
        if not hasattr(self, "_buy_submit_pending"):
            self._buy_submit_pending = set()

    def _load_buy_submit_pending(self, ctx):
        """Load restart-safe buy reservations and refresh the in-memory index."""
        self._ensure_live_state()
        stored = ctx.state.get("pending_buy_submissions", {})
        records = {
            str(code): dict(record)
            for code, record in stored.items()
            if isinstance(code, str) and isinstance(record, dict)
        } if isinstance(stored, dict) else {}
        for code, record in tuple(records.items()):
            if not str(record.get("client_order_id") or ""):
                records.pop(code, None)
        self._buy_submit_pending = set(records)
        return records

    def _save_buy_submit_pending(self, ctx, records):
        self._buy_submit_pending = set(records)
        ctx.state.set("pending_buy_submissions", records)

    def _mark_buy_submit_pending(
        self, ctx, code, *, client_order_id=None, status=None, at=None,
        quantity=None, order_type=None, price=None, route=None,
        provider_order_id=None, replace=True,
    ):
        """Reserve a symbol while an accepted buy reaches broker snapshots."""
        records = self._load_buy_submit_pending(ctx)
        current = records.get(code)
        current_id = str((current or {}).get("client_order_id") or "")
        event_id = str(client_order_id or "")
        if current is not None and current_id and event_id and current_id != event_id:
            if not replace:
                return False
            current = None
            current_id = ""
        if not event_id and not current_id:
            return False
        now = at or datetime.now(SEOUL)
        base = dict(current or {})
        if order_type is not None:
            stored_price = str(price) if price is not None else None
        else:
            stored_price = base.get("price")
        records[code] = {
            "client_order_id": event_id or current_id,
            "status": str(status or base.get("status") or OrderStatus.SUBMITTING.value),
            "quantity": str(quantity if quantity is not None else base.get("quantity") or ""),
            "order_type": str(order_type or base.get("order_type") or ""),
            "price": stored_price,
            "route": str(route or base.get("route") or ""),
            "submitted_at": str(base.get("submitted_at") or now.isoformat()),
            "provider_order_id": str(
                provider_order_id or base.get("provider_order_id") or ""
            ),
        }
        self._save_buy_submit_pending(ctx, records)
        return True

    def _clear_buy_submit_pending(self, ctx, code, *, client_order_id=None):
        records = self._load_buy_submit_pending(ctx)
        current = records.get(code)
        if current is None:
            return False
        current_id = str(current.get("client_order_id") or "")
        event_id = str(client_order_id or "")
        if not current_id or not event_id or current_id != event_id:
            return False
        records.pop(code, None)
        self._save_buy_submit_pending(ctx, records)
        return True

    def _reconcile_buy_submit_pending(self, ctx, open_orders, positions):
        """Reconcile restart-safe buy reservations with fresh broker snapshots."""
        records = self._load_buy_submit_pending(ctx)
        held_codes = {
            code for code, position in positions.items() if position.quantity > 0
        }
        broker_orders = {
            item.instrument.code: item
            for item in open_orders
            if item.remaining_quantity > 0 and item.side.value == "BUY"
        }
        for code in held_codes:
            records.pop(code, None)
        for code, item in broker_orders.items():
            current = records.get(code)
            if current is None:
                continue
            status = getattr(getattr(item, "status", None), "value", None)
            records[code] = {
                **current,
                "status": str(status or current.get("status") or OrderStatus.ACKNOWLEDGED.value),
                "provider_order_id": str(
                    getattr(item, "provider_order_id", None)
                    or current.get("provider_order_id")
                    or ""
                ),
            }
        # Absence from an open-order snapshot is not terminal evidence. Broker
        # snapshots can lag both acknowledgement and balance updates, so keep
        # the reservation until a held position or matching terminal event is
        # observed.
        self._save_buy_submit_pending(ctx, records)

    def _mark_live_sell_pending(self, code, *, at=None):
        """Reserve a symbol while a submitted sell reaches the broker ledger."""
        self._ensure_live_state()
        now = at or datetime.now(SEOUL)
        self._live_sell_pending.add(code)
        # A newly acknowledged order can take a few seconds to appear in the
        # broker's open-order snapshot.  Keep the local reservation long enough
        # to cover that propagation window so a quote burst cannot duplicate it.
        self._live_sell_recheck_at[code] = now + timedelta(seconds=15)

    def _clear_live_sell_pending(self, code):
        self._ensure_live_state()
        self._live_sell_pending.discard(code)
        self._live_sell_recheck_at.pop(code, None)

    def _live_sell_recheck_due(self, code, *, at=None):
        self._ensure_live_state()
        if code not in self._live_sell_pending:
            return False
        now = at or datetime.now(SEOUL)
        return now >= self._live_sell_recheck_at.get(code, now)

    def _reconcile_live_sell_pending(self, open_orders, *, desired, at=None):
        """Reconcile the local duplicate guard with the broker's open orders.

        Broker-confirmed sells are checked again after a short interval.  A
        newly submitted local sell gets a longer grace period because the
        broker snapshot may briefly lag the acknowledgement.  Once an order is
        absent after its grace period, an external cancel can no longer leave
        the symbol blocked forever.
        """
        self._ensure_live_state()
        now = at or datetime.now(SEOUL)
        broker_pending = {
            item.instrument.code
            for item in open_orders
            if item.remaining_quantity > 0 and item.side.value == "SELL"
        }
        reconciled = set(broker_pending)
        for code in self._live_sell_pending - broker_pending:
            if code in desired and now < self._live_sell_recheck_at.get(code, now):
                reconciled.add(code)
        self._live_sell_pending = reconciled
        for code in broker_pending:
            # Broker-confirmed orders need only a short polling interval.  If a
            # user cancels one elsewhere, the next due quote refresh releases it.
            self._live_sell_recheck_at[code] = now + timedelta(seconds=3)
        for code in tuple(self._live_sell_recheck_at):
            if code not in reconciled:
                self._live_sell_recheck_at.pop(code, None)

    @staticmethod
    def _is_live_profit_condition(condition):
        metrics = set(_condition_metric_names(condition))
        return (
            condition.get("operator") not in {"CROSS_UP", "CROSS_DOWN"}
            and "profit_rate" in metrics
            and metrics <= {"profit_rate", "value"}
        )

    def _live_profit_match(self, condition, current_price, average_price):
        if not average_price or average_price <= 0:
            return False
        return self._condition_matches(
            condition, {}, average_price, current_price=current_price,
        )

    def _matched_live_profit_condition(self, tree, current_price, average_price):
        for condition in _rule_conditions(tree):
            if self._is_live_profit_condition(condition) and self._live_profit_match(
                condition, current_price, average_price
            ):
                return condition
        return None

    @staticmethod
    def _live_quote_venues():
        configured = Venue(MARKET_VENUE)
        if configured is Venue.INTEGRATED:
            # KIS' integrated H0UNCNT0 stream may accept a subscription without
            # publishing quotes.  Subscribe to the two underlying live feeds so
            # realtime exits work during both the KRX and NXT sessions.
            return (Venue.KRX, Venue.NXT)
        return (configured,)

    async def _subscribe_live_quote(self, ctx, code):
        if not SELL_FLOWS:
            return
        self._ensure_live_state()
        active = self._live_quote_subscriptions.setdefault(code, {})
        for venue in self._live_quote_venues():
            if venue.value in active:
                continue
            try:
                subscriptions = await ctx.market.subscribe_quotes(code, venue=venue)
            except RouterError as exc:
                ctx.log.warning(
                    f"{self._instrument_label(ctx, code)} {venue.value} 실시간 익절·손절 구독 보류 "
                    f"[{exc.code.value}]: {exc.message}"
                )
                continue
            subscription_ids = tuple(
                str(item["id"])
                for item in subscriptions
                if item.get("id") is not None
            )
            if subscription_ids:
                active[venue.value] = subscription_ids
        if not active:
            self._live_quote_subscriptions.pop(code, None)

    async def _unsubscribe_live_quote(self, ctx, code):
        self._ensure_live_state()
        self._last_live_quote_at.pop(code, None)
        subscriptions = self._live_quote_subscriptions.pop(code, None)
        if not subscriptions:
            return
        for subscription_ids in subscriptions.values():
            for subscription_id in subscription_ids:
                try:
                    await ctx.market.unsubscribe(subscription_id)
                except RouterError:
                    pass

    def _update_profit_cost_rates(self, ctx, balance):
        rates = []
        for key in ("buy_commission_rate", "sell_commission_rate", "sell_tax_rate"):
            raw = getattr(balance, key, None)
            try:
                rate = Decimal(str(raw)) if raw is not None else None
            except (DecimalException, ValueError, TypeError):
                rate = None
            if rate is None or not rate.is_finite() or rate < 0 or rate >= 1:
                rates = []
                break
            rates.append(rate)
        if rates and rates[1] + rates[2] >= 1:
            rates = []
        self._profit_cost_rates = tuple(rates) if rates else None
        self._profit_costs_refreshed_at = datetime.now(SEOUL)
        uses_profit = any(
            "profit_rate" in _condition_metric_names(condition)
            for flow in SELL_FLOWS
            for condition in _rule_conditions(flow["sell_rule_tree"])
        )
        if not uses_profit:
            return
        if not rates:
            if not getattr(self, "_profit_cost_warning", False):
                ctx.log.warning(
                    "순수익률 조건 보류: 계좌의 매수·매도 수수료율과 매도 세율을 모두 설정해야 합니다. "
                    "면제 비용은 0으로 설정하세요. 다른 매도 조건은 계속 판단합니다."
                )
            self._profit_cost_warning = True
        elif getattr(self, "_profit_cost_warning", False):
            ctx.log.info("계좌 비용 설정 확인 · 순수익률 조건 판단을 재개합니다.")
            self._profit_cost_warning = False

    async def _refresh_live_positions(self, ctx, balance, open_orders):
        self._ensure_live_state()
        self._update_profit_cost_rates(ctx, balance)
        positions = {item.instrument.code: item for item in balance.positions}
        names = dict(ctx.state.get("instrument_names", {}))
        names.update({
            item.instrument.code: item.name
            for item in balance.positions if getattr(item, "name", None)
        })
        ctx.state.set("instrument_names", names)
        tracked = self._sync_tracked_positions(ctx, balance)
        self._reconcile_buy_submit_pending(ctx, open_orders, positions)
        sell_targets = self._sell_target_quantities(balance, tracked)
        desired = {
            code
            for code in sell_targets
            if code in positions and positions[code].quantity > 0
        }
        self._live_positions = {code: positions[code] for code in desired}
        self._reconcile_live_sell_pending(open_orders, desired=desired)
        for code in sorted(desired):
            await self._subscribe_live_quote(ctx, code)
        for code in tuple(self._live_quote_subscriptions):
            if code not in desired:
                await self._unsubscribe_live_quote(ctx, code)
        return tracked, positions, {item.instrument.code for item in open_orders if item.remaining_quantity > 0}

    @staticmethod
    def _sell_target_quantities(balance, tracked):
        # Selecting account holdings for exits must not adopt them into the
        # strategy's buy ownership/budget tracker.
        if SELL_POSITION_SCOPE == "ACCOUNT":
            return {
                position.instrument.code: position.quantity
                for position in balance.positions if position.quantity > 0
            }
        return {code: Decimal(str(quantity)) for code, quantity in tracked.items()}

    @staticmethod
    def _sell_available_quantity(position, tracked):
        if SELL_POSITION_SCOPE == "ACCOUNT":
            owned = position.quantity
        else:
            owned = (
                position.strategy_quantity if position.strategy_quantity is not None
                else Decimal(str(tracked.get(position.instrument.code, "0")))
            )
        return max(Decimal("0"), min(owned, position.quantity, position.available_quantity))

    @staticmethod
    def _sell_scope_options():
        return {"sell_position_scope": "ACCOUNT"} if SELL_POSITION_SCOPE == "ACCOUNT" else {}

    @staticmethod
    def _sell_flow_is_active(flow, at):
        local_time = at.astimezone(SEOUL).time().replace(tzinfo=None)
        return (
            time.fromisoformat(flow["sell_start_time"])
            <= local_time
            < time.fromisoformat(flow["sell_end_time"])
        )

    @classmethod
    def _active_sell_flows(cls, at):
        return tuple(flow for flow in SELL_FLOWS if cls._sell_flow_is_active(flow, at))

    @classmethod
    def _sell_flow_event_is_active(cls, flow, scheduled_at, at=None):
        now = at or datetime.now(SEOUL)
        if now.tzinfo is None or scheduled_at.tzinfo is None:
            return False
        now = now.astimezone(SEOUL)
        scheduled_at = scheduled_at.astimezone(SEOUL)
        return (
            now.date() == scheduled_at.date()
            and now >= scheduled_at
            and cls._sell_flow_is_active(flow, scheduled_at)
            and cls._sell_flow_is_active(flow, now)
        )

    @classmethod
    def _buy_order_session_is_open(cls, at, order_type=None):
        order_type = order_type or BUY_ORDER_TYPE
        if not cls._order_type_session_is_open(at, ORDER_ROUTE, order_type):
            return False
        if ORDER_ROUTE != "NXT":
            return True
        local_time = at.astimezone(SEOUL).time().replace(tzinfo=None)
        if time(8, 0) <= local_time < time(8, 50):
            return True
        if time(9, 0, 30) <= local_time < time(15, 20):
            return True
        after_open = time(15, 30) if order_type == "LIMIT" else time(15, 40)
        return after_open <= local_time < time(20, 0)

    @staticmethod
    def _buy_flow_is_active(flow, scheduled_at, at=None):
        now = at or datetime.now(SEOUL)
        if now.tzinfo is None or scheduled_at.tzinfo is None:
            return False
        now = now.astimezone(SEOUL)
        scheduled_at = scheduled_at.astimezone(SEOUL)
        start = time.fromisoformat(flow["buy_start_time"])
        end = time.fromisoformat(flow["buy_end_time"])
        return (
            now.date() == scheduled_at.date()
            and now >= scheduled_at
            and start <= scheduled_at.time().replace(tzinfo=None) < end
            and start <= now.time().replace(tzinfo=None) < end
        )

    @staticmethod
    def _shared_buy_scan(ctx, scheduled_at):
        # Branches fired at the same scheduler tick share the submission count
        # and attempted symbols, even while REST snapshots are catching up.
        key = scheduled_at.astimezone(SEOUL).isoformat()
        scan = ctx.state.get("buy_scan_state", {})
        if not isinstance(scan, dict) or scan.get("tick") != key:
            scan = {"tick": key, "submitted": 0, "attempted": []}
        else:
            scan = {**scan, "attempted": list(scan.get("attempted", []))}
        return scan

    async def on_start(self, ctx):
        self._ensure_live_state()
        self._execution_rule_manager = ExecutionRuleManager(
            EXECUTION_FLOWS,
            session_is_open=lambda order, now: self._order_type_session_is_open(
                now, order.request.route.value, "BEST_LIMIT"
            ),
        )
        self._execution_flow_schedules = {}
        for flow in EXECUTION_FLOWS:
            schedule_id = f"execution-manage-{flow['id']}"
            self._execution_flow_schedules[schedule_id] = flow["id"]
            ctx.scheduler.every(
                seconds=flow["execution_scan_seconds"],
                schedule_id=schedule_id,
                between=(flow["execution_start_time"], flow["execution_end_time"]),
                aligned=True,
            )
            ctx.log.info(
                f"체결 흐름 {flow['name']}: {flow['execution_start_time']} 이상 · "
                f"{flow['execution_end_time']} 미만 / {flow['execution_scan_seconds']}초마다 · "
                "전략 소유 미체결 주문만 관리 · 행동별 원주문당 1회"
            )
        buy_sources = [
            source
            for flow in BUY_FLOWS
            for condition in _rule_conditions(flow["buy_rule_tree"])
            for source in self._condition_sources(condition)
        ]
        sell_sources = [
            source
            for flow in SELL_FLOWS
            for condition in _rule_conditions(flow["sell_rule_tree"])
            for source in self._condition_sources(condition)
        ]
        buy_source_text = ", ".join(dict.fromkeys(
            f"{item['bar_interval']}·{'완성봉' if item['bar_data_mode'] == 'COMPLETE_ONLY' else '현재봉 포함'}"
            for item in buy_sources if self._source_uses_bars(item)
        )) or "봉 계산 없음"
        sell_source_text = ", ".join(dict.fromkeys(
            f"{item['bar_interval']}·{'완성봉' if item['bar_data_mode'] == 'COMPLETE_ONLY' else '현재봉 포함'}"
            for item in sell_sources
            if self._source_uses_bars(item)
        )) or "봉 계산 없음"
        if BUY_FLOWS:
            ctx.log.info(f"매수 조건 계층: {buy_source_text} · 괄호 계층의 ALL/ANY 순서로 판단")
        else:
            ctx.log.info("매수 흐름 없음 · 자동 매수를 실행하지 않습니다.")
        if SELL_FLOWS:
            ctx.log.info(f"매도 조건 계층: {sell_source_text} · 괄호 계층의 ALL/ANY 순서로 판단")
            scope_text = "계좌 전체 보유종목 (수동·다른 전략 보유분 포함)" if SELL_POSITION_SCOPE == "ACCOUNT" else "이 전략 보유종목"
            ctx.log.info(f"매도 대상: {scope_text} · 매도 가능 수량 이내")
            if any(source["metric"] == "profit_rate" for source in sell_sources):
                ctx.log.info("수익률 조건: 계좌 평균 매수가에 매수수수료를 더한 원가 대비, 매도수수료·세금을 뺀 예상 순수익률")
        else:
            ctx.log.info("매도 흐름 없음 · 자동 매도를 실행하지 않습니다.")
        for schedule_id, flow in BUY_FLOW_SCHEDULES.items():
            ctx.scheduler.every(
                seconds=flow["buy_scan_seconds"],
                schedule_id=schedule_id,
                between=(flow["buy_start_time"], flow["buy_end_time"]),
                aligned=True,
            )
            ctx.log.info(
                f"매수 흐름 {flow['name']}: {flow['buy_start_time']} 이상 · "
                f"{flow['buy_end_time']} 미만 / {flow['buy_scan_seconds']}초마다 조회 · 예산·보유종목 공유"
            )
        self._sell_flow_schedules = {}
        for flow in SELL_FLOWS:
            flow_sources = [
                source
                for condition in _rule_conditions(flow["sell_rule_tree"])
                for source in self._condition_sources(condition)
            ]
            bar_sell_sources = [
                item for item in flow_sources if self._source_uses_bars(item)
            ]
            schedule_prefix = (
                f"condition-sell-{flow['id']}"
                if CONFIGURED_SELL_FLOWS
                else "condition-sell"
            )
            if not bar_sell_sources or any(
                item["bar_data_mode"] == "INCLUDE_CURRENT"
                for item in bar_sell_sources
            ):
                schedule_id = f"{schedule_prefix}-live"
                self._sell_flow_schedules[schedule_id] = flow
                ctx.scheduler.every(
                    seconds=flow["sell_scan_seconds"],
                    schedule_id=schedule_id,
                    between=(flow["sell_start_time"], flow["sell_end_time"]),
                    aligned=True,
                )
            else:
                intervals = list(dict.fromkeys(
                    item["bar_interval"] for item in bar_sell_sources
                ))
                for index, interval in enumerate(intervals, start=1):
                    suffix = "daily" if interval == "1d" else "bars"
                    schedule_id = f"{schedule_prefix}-{suffix}-{index}"
                    self._sell_flow_schedules[schedule_id] = flow
                    if interval == "1d":
                        ctx.scheduler.daily_at(
                            flow["sell_start_time"], schedule_id=schedule_id,
                        )
                    else:
                        ctx.scheduler.on_bar_close(
                            interval,
                            schedule_id=schedule_id,
                            between=(flow["sell_start_time"], flow["sell_end_time"]),
                        )
            ctx.log.info(
                f"매도 흐름 {flow['name']}: {flow['sell_start_time']} 이상 · "
                f"{flow['sell_end_time']} 미만 / 조건 계층 · 보유종목·주문 공유"
            )

        live_profit_flows = tuple(
            flow for flow in SELL_FLOWS
            if any(self._is_live_profit_condition(condition)
                   for condition in _rule_conditions(flow["sell_rule_tree"]))
        )
        if live_profit_flows:
            # Completed-candle schedules remain unchanged. This independent
            # watchdog only supplies a fresh quote to the existing profit exit
            # path when the websocket has stopped delivering usable ticks.
            ctx.scheduler.every(
                seconds=5, schedule_id="live-profit-watchdog", aligned=True,
                between=(
                    min(flow["sell_start_time"] for flow in live_profit_flows),
                    max(flow["sell_end_time"] for flow in live_profit_flows),
                ),
            )
            ctx.log.info("익절·손절: 실시간 현재가 감시 · 10초간 시세 미수신 시 5초마다 현재가를 보완 조회합니다.")
            try:
                balance = await ctx.account.balance()
                open_orders = await ctx.account.open_orders()
                await self._refresh_live_positions(ctx, balance, open_orders)
            except RouterError as exc:
                ctx.log.warning(
                    f"실시간 익절·손절 초기화 보류 [{exc.code.value}]: {exc.message} · 다음 조회에서 재시도합니다."
                )

        conditions_by_user = {}
        for flow in BUY_FLOWS:
            if flow["target_source"] == "HTS_CONDITION":
                user_id = flow["hts_user_id"]
                if user_id not in conditions_by_user:
                    conditions_by_user[user_id] = await ctx.discovery.conditions(user_id)
                conditions = conditions_by_user[user_id]
                condition_id = flow["condition_id"]
                selected = next((item for item in conditions if item.id == condition_id), None)
                if selected is None:
                    available = ", ".join(f"{item.id}:{item.name}" for item in conditions)
                    raise RuntimeError(f"{flow['name']} 조건식 {condition_id}을 찾지 못했습니다. 사용 가능: {available or '없음'}")
                expected = flow["expected_condition_name"]
                if expected and " ".join(selected.name.split()) != " ".join(expected.split()):
                    raise RuntimeError(f"{flow['name']} 조건식 {condition_id} 이름이 예상과 다릅니다: {selected.name}")
                ctx.log.info(f"{flow['name']} 종목 데이터 연결: HTS 조건식 {selected.id} · {selected.name}")
            elif flow["target_source"] == "RANKING":
                ctx.log.info(f"{flow['name']} 종목 데이터 연결: {flow['ranking_kind']} · {flow['ranking_market']} · 상위 {flow['ranking_limit']}종목")
            else:
                ctx.log.info(f"{flow['name']} 종목 데이터 연결: 지정 종목 {len(flow['symbols'])}개")

    def _sync_tracked_positions(self, ctx, balance):
        positions = {item.instrument.code: item for item in balance.positions}
        tracked = dict(ctx.state.get("strategy_positions", {}))
        for position in balance.positions:
            if position.strategy_quantity is not None and position.strategy_quantity > 0:
                tracked[position.instrument.code] = str(min(position.strategy_quantity, position.quantity))
        if not ctx.state.get("ownership_initialized", False):
            ctx.state.set("ownership_initialized", True)
        for code in list(tracked):
            position = positions.get(code)
            if position is None or position.quantity <= 0:
                tracked.pop(code, None)
                continue
            quantity = min(Decimal(str(tracked[code])), position.quantity)
            if position.strategy_quantity is not None:
                quantity = min(quantity, position.strategy_quantity)
            if quantity <= 0:
                tracked.pop(code, None)
            else:
                tracked[code] = str(quantity)
        ctx.state.set("strategy_positions", tracked)
        return tracked

    async def on_timer(self, ctx, event):
        if event.schedule_id == "live-profit-watchdog":
            await self._check_live_profit_fallback(ctx)
            return
        execution_flow_id = getattr(self, "_execution_flow_schedules", {}).get(event.schedule_id)
        if execution_flow_id is not None:
            await self._execution_rule_manager.tick(ctx, execution_flow_id)
            return
        sell_flow = getattr(self, "_sell_flow_schedules", {}).get(event.schedule_id)
        if sell_flow is not None:
            await self.on_bar(ctx, event)
            return
        flow = BUY_FLOW_SCHEDULES.get(event.schedule_id)
        if flow is None:
            return
        if not hasattr(self, "_buy_flow_lock"):
            self._buy_flow_lock = asyncio.Lock()
        async with self._buy_flow_lock:
            await self._scan_buy_flow(ctx, event, flow)

    async def _scan_buy_flow(self, ctx, event, flow):
        if not BUY_FLOWS:
            return
        def active():
            now = datetime.now(SEOUL)
            # The branch action selects an order type after its conditions
            # match. Keep the scan alive for the union of the order windows,
            # then apply the selected action's exact window below.  Using the
            # legacy global BUY_ORDER_TYPE here would incorrectly skip a LIMIT
            # branch during NXT's 15:30-15:40 limit-only window.
            return self._buy_flow_is_active(
                flow, event.scheduled_at, now
            ) and self._buy_order_session_is_open(now, "LIMIT")

        if not active():
            return
        scan = self._shared_buy_scan(ctx, event.scheduled_at)
        if scan["submitted"] >= MAX_NEW_BUYS_PER_SCAN:
            return
        try:
            candidate_codes = await self._candidate_codes(ctx, flow)
        except RouterError as exc:
            ctx.log.warning(f"종목 데이터 조회 보류 [{exc.code.value}]: {exc.message} · 다음 조회에서 재시도합니다.")
            return
        if not candidate_codes or not active():
            return
        try:
            balance = await ctx.account.balance()
            if not active():
                return
            open_orders = await ctx.account.open_orders()
        except RouterError as exc:
            ctx.log.warning(f"매수 판단 보류 [{exc.code.value}]: {exc.message} · 다음 조회에서 재시도합니다.")
            return
        if not active():
            return
        held_codes = {item.instrument.code for item in balance.positions if item.quantity > 0}
        tracked, _, pending_codes = await self._refresh_live_positions(ctx, balance, open_orders)
        buy_exclusions = self._buy_exclusions_for_day(ctx, event.scheduled_at)
        excluded_codes = set(buy_exclusions["symbols"])
        for code in candidate_codes:
            if not active() or scan["submitted"] >= MAX_NEW_BUYS_PER_SCAN:
                break
            if code in scan["attempted"]:
                continue
            if code in excluded_codes:
                continue
            if code in pending_codes:
                ctx.log.info(f"{self._instrument_label(ctx, code)} 매수 제외: 계좌에 미체결 주문이 있습니다.")
                continue
            if code in held_codes:
                ctx.log.info(f"{self._instrument_label(ctx, code)} 매수 제외: 현재 계좌에 보유 중입니다.")
                continue
            if code in tracked:
                ctx.log.info(f"{self._instrument_label(ctx, code)} 매수 제외: 전략 소유 종목으로 추적 중입니다.")
                continue
            if code in self._buy_submit_pending:
                ctx.log.info(f"{self._instrument_label(ctx, code)} 매수 제외: 제출한 매수 주문의 계좌 반영을 기다리고 있습니다.")
                continue
            passed, matched_conditions, action = await self._rule_tree_result(
                ctx, code, flow["buy_rule_tree"], side=f"{flow['name']} 매수"
            )
            if not active():
                return
            if not passed:
                ctx.log.info(f"{self._instrument_label(ctx, code)} 매수 제외: 매수 조건 계층을 통과하지 못했습니다.")
                continue
            if action is None:
                ctx.log.info(f"{self._instrument_label(ctx, code)} 매수 보류: 통과한 조건 계층에 주문 행동이 없습니다.")
                continue
            order_type = action["order_type"]
            now = datetime.now(SEOUL)
            if not self._buy_order_session_is_open(now, order_type):
                continue
            limit_price = None
            try:
                market_price = await self._current_price(ctx, code)
                if not active():
                    return
                if market_price <= 0:
                    raise RuntimeError(f"{MARKET_VENUE} 현재가가 0 이하입니다.")
                order_at = datetime.now(SEOUL)
                order_route = await self._effective_order_route(
                    ctx, code, order_at, current_price=market_price
                )
                if not active():
                    return
                order_at = datetime.now(SEOUL)
                if not self._order_type_session_is_open(order_at, order_route, order_type):
                    continue
                blocked_reason = self._order_session_block_reason(
                    order_at, order_route, order_type
                )
                if blocked_reason:
                    self._exclude_buy_for_day(ctx, order_at, code, blocked_reason)
                    excluded_codes.add(code)
                    ctx.log.warning(
                        f"{self._instrument_label(ctx, code)} 매수 건너뜀: "
                        f"{blocked_reason} 오늘은 다시 시도하지 않습니다."
                    )
                    continue
                if order_type == "LIMIT":
                    limit_price = market_price
                buying_power = await ctx.account.buying_power(
                    code,
                    order_type=order_type,
                    price=limit_price,
                    route=order_route,
                )
            except RouterError as exc:
                ctx.log.warning(f"{self._instrument_label(ctx, code)} {MARKET_VENUE} 주문 전 조회 실패 [{exc.code.value}]: {exc.message}")
                continue
            except RuntimeError as exc:
                ctx.log.warning(f"{self._instrument_label(ctx, code)} {MARKET_VENUE} 주문 전 조회 실패: {exc}")
                continue
            calculation_price = max(buying_power.calculation_price, market_price)
            if calculation_price <= 0:
                ctx.log.warning(f"{self._instrument_label(ctx, code)} 주문 전 조회 실패: 보수적 계산단가를 확인할 수 없습니다.")
                continue
            budget_quantity = int(BUY_BUDGET_PER_SYMBOL // calculation_price)
            quantity = min(int(buying_power.maximum_quantity), budget_quantity)
            if quantity < 1:
                ctx.log.info(f"{self._instrument_label(ctx, code)} 건너뜀: {MARKET_VENUE} 보수적 계산단가 {calculation_price}원 · 종목당 예산 {BUY_BUDGET_PER_SYMBOL}원")
                continue
            if not active():
                ctx.log.info(f"{flow['name']} 매수 보류: 적용 시간이 종료되었습니다.")
                return
            if not self._order_type_session_is_open(datetime.now(SEOUL), order_route, order_type):
                continue
            # Reserve the symbol before awaiting submission. An ambiguous response
            # must never let another branch or later scan retry it while the
            # broker's balance/open-order snapshots are still catching up.
            scan["attempted"].append(code)
            ctx.state.set("buy_scan_state", scan)
            order_id = f"target-{code}-{event.scheduled_at:%Y%m%d%H%M%S}-buy"
            self._mark_buy_submit_pending(
                ctx, code, client_order_id=order_id,
                status=OrderStatus.SUBMITTING.value, at=datetime.now(SEOUL),
                quantity=quantity, order_type=order_type, price=limit_price,
                route=order_route,
            )
            try:
                if order_type == "LIMIT":
                    order = await ctx.orders.limit_buy(
                        code=code, quantity=quantity, price=limit_price, route=order_route,
                        client_order_id=order_id,
                    )
                elif order_type == "BEST_LIMIT":
                    order = await ctx.orders.best_limit_buy(
                        code=code, quantity=quantity, route=order_route, client_order_id=order_id,
                    )
                else:
                    order = await ctx.orders.market_buy(
                        code=code, quantity=quantity, route=order_route, client_order_id=order_id,
                    )
            except RouterError as exc:
                if exc.code.value == "ORDER_REJECTED":
                    # An explicit broker rejection is terminal. Other errors
                    # can be ambiguous transport failures and must remain
                    # reserved until broker reconciliation proves otherwise.
                    self._clear_buy_submit_pending(
                        ctx, code, client_order_id=order_id,
                    )
                else:
                    self._mark_buy_submit_pending(
                        ctx, code, client_order_id=order_id,
                        status=OrderStatus.UNKNOWN_RECONCILING.value,
                        at=datetime.now(SEOUL),
                    )
                if self._is_non_retryable_buy_rejection(exc.message):
                    self._exclude_buy_for_day(ctx, event.scheduled_at, code, exc.message)
                    excluded_codes.add(code)
                    ctx.log.warning(
                        f"{self._instrument_label(ctx, code)} 매수 제외: 주문 불가 상태로 오늘은 다시 시도하지 않습니다. ({exc.message})"
                    )
                else:
                    ctx.log.error(f"{self._instrument_label(ctx, code)} 매수 요청 실패 [{exc.code.value}]: {exc.message}")
                continue
            if order.status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}:
                self._clear_buy_submit_pending(
                    ctx, code, client_order_id=order_id,
                )
                message = str(order.message or "")
                if (
                    order.status is OrderStatus.REJECTED
                    and self._is_non_retryable_buy_rejection(message)
                ):
                    self._exclude_buy_for_day(ctx, event.scheduled_at, code, message)
                    excluded_codes.add(code)
                    ctx.log.warning(
                        f"{self._instrument_label(ctx, code)} 매수 제외: 주문 불가 상태로 오늘은 다시 시도하지 않습니다. ({message})"
                    )
                continue
            self._mark_buy_submit_pending(
                ctx, code, client_order_id=order_id,
                status=order.status.value, at=datetime.now(SEOUL),
                provider_order_id=getattr(order, "provider_order_id", None),
            )
            tracked[code] = str(quantity)
            ctx.state.set("strategy_positions", tracked)
            scan["submitted"] += 1
            ctx.state.set("buy_scan_state", scan)
            await self._subscribe_live_quote(ctx, code)
            price_text = f" {limit_price}원" if limit_price is not None else ""
            reason = " 그리고 ".join(
                self._condition_summary(item) for item in matched_conditions
            ) or "시간·대상 조건"
            ctx.log.info(
                f"{self._instrument_label(ctx, code)} {reason} · "
                f"{order_type}{price_text} {quantity}주 매수 요청: {order.status.value}"
            )
            if scan["submitted"] >= MAX_NEW_BUYS_PER_SCAN:
                break

    async def on_bar(self, ctx, event):
        if not SELL_FLOWS:
            return
        flow = getattr(self, "_sell_flow_schedules", {}).get(
            getattr(event, "schedule_id", "")
        ) or SELL_FLOWS[0]
        if not hasattr(self, "_sell_flow_lock"):
            self._sell_flow_lock = asyncio.Lock()
        async with self._sell_flow_lock:
            await self._on_bar_locked(ctx, event, flow)

    async def _on_bar_locked(self, ctx, event, flow):
        if not SELL_FLOWS:
            return
        if not self._sell_flow_event_is_active(
            flow, event.scheduled_at, datetime.now(SEOUL)
        ):
            return
        try:
            balance = await ctx.account.balance()
            open_orders = await ctx.account.open_orders()
        except RouterError as exc:
            ctx.log.warning(f"매도 판단 보류 [{exc.code.value}]: {exc.message} · 다음 완성 봉에서 재시도합니다.")
            return
        tracked, positions, pending_codes = await self._refresh_live_positions(ctx, balance, open_orders)
        sell_targets = self._sell_target_quantities(balance, tracked)
        for code in sell_targets:
            position = positions.get(code)
            if position is None or position.quantity <= 0:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)
                continue
            if SELL_POSITION_SCOPE == "STRATEGY" and position.strategy_quantity is not None and position.strategy_quantity <= 0:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)
                continue
            if code in pending_codes or code in self._live_sell_pending:
                continue
            average_price = position.average_price
            should_sell, matched_conditions, action = await self._rule_tree_result(
                ctx,
                code,
                flow["sell_rule_tree"],
                average_price=average_price,
                side=f"{flow['name']} 매도",
            )
            if not should_sell:
                continue
            if action is None:
                ctx.log.info(f"{self._instrument_label(ctx, code)} 매도 보류: 통과한 조건 계층에 주문 행동이 없습니다.")
                continue
            order_type = action["order_type"]
            sell_reason = " 그리고 ".join(
                self._condition_summary(item) for item in matched_conditions
            ) or "시간·보유 조건"
            quantity = self._sell_available_quantity(position, tracked)
            if quantity <= 0:
                continue
            limit_price = None
            try:
                if order_type == "LIMIT":
                    limit_price = await self._current_price(ctx, code)
            except RouterError as exc:
                ctx.log.error(
                    f"{self._instrument_label(ctx, code)} 매도 주문 전 현재가 조회 실패 "
                    f"[{exc.code.value}]: {exc.message}"
                )
                continue
            order_at = datetime.now(SEOUL)
            if not self._sell_flow_event_is_active(flow, event.scheduled_at, order_at):
                return
            if self._order_attempt_is_suppressed(
                ctx, order_at, code, "SELL", order_type
            ):
                continue
            try:
                route_at = order_at
                order_route = await self._effective_order_route(
                    ctx, code, route_at, current_price=limit_price
                )
                order_at = datetime.now(SEOUL)
                if not self._sell_flow_event_is_active(
                    flow, event.scheduled_at, order_at
                ):
                    return
                if self._order_session(order_at) != self._order_session(route_at):
                    order_route = await self._effective_order_route(
                        ctx, code, order_at, current_price=limit_price
                    )
                if not self._order_type_session_is_open(order_at, order_route, order_type):
                    continue
                blocked_reason = self._order_session_block_reason(
                    order_at, order_route, order_type
                )
                if blocked_reason:
                    if self._suppress_order_for_day(
                        ctx, order_at, code, "SELL", order_type,
                        blocked_reason,
                    ):
                        ctx.log.warning(
                            f"{self._instrument_label(ctx, code)} 매도 건너뜀: {blocked_reason}"
                        )
                    continue
                order_id = f"exit-{code}-{order_at:%Y%m%d%H%M%S}-sell"
                if order_type == "LIMIT":
                    order = await ctx.orders.limit_sell(
                        code=code, quantity=quantity, price=limit_price, route=order_route,
                        client_order_id=order_id,
                        **self._sell_scope_options(),
                    )
                elif order_type == "BEST_LIMIT":
                    order = await ctx.orders.best_limit_sell(
                        code=code, quantity=quantity, route=order_route, client_order_id=order_id,
                        **self._sell_scope_options(),
                    )
                else:
                    order = await ctx.orders.market_sell(
                        code=code, quantity=quantity, route=order_route, client_order_id=order_id,
                        **self._sell_scope_options(),
                    )
            except RouterError as exc:
                failure_at = datetime.now(SEOUL)
                if self._is_non_retryable_sell_rejection(exc.message):
                    if self._suppress_order_for_day(
                        ctx, failure_at, code, "SELL", order_type,
                        exc.message,
                    ):
                        ctx.log.error(
                            f"{self._instrument_label(ctx, code)} 매도 요청 중단 "
                            f"[{exc.code.value}]: {exc.message} · 오늘 같은 주문은 다시 시도하지 않습니다."
                        )
                else:
                    ctx.log.error(f"{self._instrument_label(ctx, code)} 매도 요청 실패 [{exc.code.value}]: {exc.message}")
                continue
            if order.status is OrderStatus.REJECTED:
                message = str(order.message or "")
                if self._is_non_retryable_sell_rejection(message):
                    if self._suppress_order_for_day(
                        ctx, datetime.now(SEOUL), code, "SELL", order_type,
                        message,
                    ):
                        ctx.log.error(
                            f"{self._instrument_label(ctx, code)} 매도 요청 중단: {message} · "
                            "오늘 같은 주문은 다시 시도하지 않습니다."
                        )
                continue
            if order.status is not OrderStatus.REJECTED:
                self._mark_live_sell_pending(code)
            price_text = f" {limit_price}원" if limit_price is not None else ""
            decision_time = (
                f"완성봉 마감 {event.scheduled_at:%H:%M:%S}"
                if event.kind is ScheduleKind.BAR_CLOSE
                else f"판단 시각 {event.scheduled_at:%H:%M:%S}"
            )
            ctx.log.info(
                f"{self._instrument_label(ctx, code)} {flow['name']} · "
                f"{decision_time} · {sell_reason} · "
                f"{order_type}{price_text} {quantity}주 매도 요청: {order.status.value}"
            )
            if order.status is OrderStatus.FILLED:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)

    @staticmethod
    def _live_quote_is_usable(quote, *, websocket=False):
        if quote.delayed or quote.stale or not quote.price.is_finite() or quote.price <= 0:
            return False
        # Reject buffered ticks after a slow condition lookup. A REST quote may
        # legitimately have an older last-trade time for an illiquid stock, so
        # freshness here refers to when the provider response was received.
        try:
            age = (datetime.now(SEOUL) - quote.received_at).total_seconds()
        except (AttributeError, TypeError):
            return False
        if age < -1 or age > 10:
            return False
        # A newly received websocket packet can still replay an old trade.
        # REST last-trade timestamps, in contrast, can be old on a quiet stock.
        market_time = getattr(quote, "market_time", None)
        if websocket and market_time is not None:
            try:
                market_age = (datetime.now(SEOUL) - market_time).total_seconds()
            except TypeError:
                return False
            if market_age < -1 or market_age > 10:
                return False
        source_alias = getattr(quote, "source_alias", None)
        if source_alias is not None and source_alias not in {"primary", CONDITION_SOURCE}:
            return False
        venue = getattr(quote, "venue", None)
        venue = getattr(venue, "value", venue)
        allowed_venues = {MARKET_VENUE}
        if MARKET_VENUE == "INTEGRATED":
            allowed_venues.update(("KRX", "NXT"))
        return venue is None or venue in allowed_venues

    async def _check_live_profit_fallback(self, ctx):
        self._ensure_live_state()
        now = datetime.now(SEOUL)
        if not any(
            self._is_live_profit_condition(condition)
            for flow in self._active_sell_flows(now)
            for condition in _rule_conditions(flow["sell_rule_tree"])
        ):
            return
        if not hasattr(self, "_sell_flow_lock"):
            self._sell_flow_lock = asyncio.Lock()
        async with self._sell_flow_lock:
            def needs_quote(code):
                last_tick = self._last_live_quote_at.get(code)
                age = (datetime.now(SEOUL) - last_tick).total_seconds() if last_tick else None
                return age is None or age < 0 or age >= 10

            # Failed startup subscriptions can leave the local position cache
            # empty. Refresh the selected sell targets while keeping the buy
            # ownership tracker limited to this strategy's holdings.
            tracked = ctx.state.get("strategy_positions", {})
            known_codes = set(tracked) | set(self._live_positions)
            account_refresh_due = SELL_POSITION_SCOPE == "ACCOUNT" and (
                self._account_sell_reconcile_at is None or now >= self._account_sell_reconcile_at
            )
            cost_refresh_due = self._profit_costs_refreshed_at is None or not (
                0 <= (now - self._profit_costs_refreshed_at).total_seconds() < 30
            )
            if known_codes:
                if not account_refresh_due and not cost_refresh_due and not any(needs_quote(code) for code in known_codes):
                    return
            else:
                # Startup balance failures can leave both local caches empty,
                # while the central ledger still owns shares. Recover them at
                # a bounded rate; do not poll an empty account every five seconds.
                now = datetime.now(SEOUL)
                if not account_refresh_due and self._empty_live_reconcile_at is not None and now < self._empty_live_reconcile_at:
                    return
                self._empty_live_reconcile_at = now + timedelta(seconds=30)
            if SELL_POSITION_SCOPE == "ACCOUNT":
                self._account_sell_reconcile_at = datetime.now(SEOUL) + timedelta(seconds=30)
            try:
                balance = await ctx.account.balance()
                open_orders = await ctx.account.open_orders()
                await self._refresh_live_positions(ctx, balance, open_orders)
            except RouterError as exc:
                ctx.log.warning(
                    f"실시간 익절·손절 보완 조회 보류 [{exc.code.value}]: {exc.message}"
                )
                return
            for code in tuple(self._live_positions):
                if not needs_quote(code):
                    continue
                try:
                    quote = await ctx.market.quote(code, venue=Venue(MARKET_VENUE))
                except RouterError as exc:
                    ctx.log.warning(
                        f"{self._instrument_label(ctx, code)} 실시간 익절·손절 보완 시세 조회 실패 "
                        f"[{exc.code.value}]: {exc.message}"
                    )
                    continue
                if quote.instrument.code != code:
                    continue
                # Do not count a REST response as a healthy websocket tick.
                # The shared exit path retains all condition/action, ownership,
                # pending-order and trading-session guards.
                await self._on_quote_locked(ctx, quote)

    async def on_quote(self, ctx, quote):
        if not SELL_FLOWS:
            return
        self._ensure_live_state()
        if not hasattr(self, "_sell_flow_lock"):
            self._sell_flow_lock = asyncio.Lock()
        async with self._sell_flow_lock:
            if not self._live_quote_is_usable(quote, websocket=True):
                return
            if quote.instrument.code in self._live_positions:
                market_time = getattr(quote, "market_time", None) or quote.received_at
                self._last_live_quote_at[quote.instrument.code] = min(quote.received_at, market_time)
            await self._on_quote_locked(ctx, quote, websocket=True)

    async def _on_quote_locked(self, ctx, quote, *, websocket=False):
        if not SELL_FLOWS:
            return
        self._ensure_live_state()
        if not self._live_quote_is_usable(quote, websocket=websocket):
            return
        active_flows = self._active_sell_flows(quote.received_at)
        if not active_flows:
            return
        code = quote.instrument.code
        position = self._live_positions.get(code)
        if position is None:
            return
        if code in self._live_sell_pending and not self._live_sell_recheck_due(code):
            return
        candidate_flows = tuple(
            flow
            for flow in active_flows
            if self._matched_live_profit_condition(
                flow["sell_rule_tree"], quote.price, position.average_price
            ) is not None
        )
        if not candidate_flows:
            return
        try:
            balance = await ctx.account.balance()
            open_orders = await ctx.account.open_orders()
            tracked, positions, pending_codes = await self._refresh_live_positions(
                ctx, balance, open_orders
            )
        except RouterError as exc:
            ctx.log.warning(
                f"{self._instrument_label(ctx, code)} 실시간 익절·손절 확인 보류 [{exc.code.value}]: {exc.message}"
            )
            return
        position = positions.get(code)
        if (
            position is None
            or code in pending_codes
            or code in self._live_sell_pending
            or code not in self._live_positions
        ):
            return
        matched_flow = None
        matched_conditions = None
        matched_action = None
        for flow in candidate_flows:
            should_sell, flow_matches, flow_action = await self._rule_tree_result(
                ctx,
                code,
                flow["sell_rule_tree"],
                average_price=position.average_price,
                current_price=quote.price,
                side=f"{flow['name']} 실시간 매도",
            )
            if should_sell and flow_action is not None and any(
                self._is_live_profit_condition(condition)
                for condition in flow_matches
            ):
                matched_flow = flow
                matched_conditions = flow_matches
                matched_action = flow_action
                break
        if matched_flow is None:
            return
        order_type = matched_action["order_type"]
        quantity = self._sell_available_quantity(position, tracked)
        if quantity <= 0:
            return
        order_at = datetime.now(SEOUL)
        if not self._sell_flow_event_is_active(
            matched_flow, quote.received_at, order_at
        ):
            return
        if self._order_attempt_is_suppressed(
            ctx, order_at, code, "SELL", order_type
        ):
            return
        limit_price = None
        try:
            if order_type == "LIMIT":
                limit_price = quote.price
            route_at = order_at
            order_route = await self._effective_order_route(
                ctx, code, route_at, current_price=quote.price
            )
            order_at = datetime.now(SEOUL)
            if not self._sell_flow_event_is_active(
                matched_flow, quote.received_at, order_at
            ):
                return
            if self._order_session(order_at) != self._order_session(route_at):
                order_route = await self._effective_order_route(
                    ctx, code, order_at, current_price=quote.price
                )
            if not self._order_type_session_is_open(order_at, order_route, order_type):
                return
            blocked_reason = self._order_session_block_reason(
                order_at, order_route, order_type
            )
            if blocked_reason:
                if self._suppress_order_for_day(
                    ctx, order_at, code, "SELL", order_type,
                    blocked_reason,
                ):
                    ctx.log.warning(
                        f"{self._instrument_label(ctx, code)} 실시간 익절·손절 매도 건너뜀: "
                        f"{blocked_reason}"
                    )
                return
            # Account reconciliation, mixed candle conditions or route lookup
            # may have taken time after this tick was received.
            if not self._live_quote_is_usable(quote, websocket=websocket):
                return
            self._mark_live_sell_pending(code)
            order_id = f"live-exit-{code}-{order_at:%Y%m%d%H%M%S%f}-sell"
            if order_type == "LIMIT":
                order = await ctx.orders.limit_sell(
                    code=code, quantity=quantity, price=limit_price,
                    route=order_route, client_order_id=order_id,
                    **self._sell_scope_options(),
                )
            elif order_type == "BEST_LIMIT":
                order = await ctx.orders.best_limit_sell(
                    code=code, quantity=quantity, route=order_route,
                    client_order_id=order_id,
                    **self._sell_scope_options(),
                )
            else:
                order = await ctx.orders.market_sell(
                    code=code, quantity=quantity, route=order_route,
                    client_order_id=order_id,
                    **self._sell_scope_options(),
                )
        except RouterError as exc:
            self._clear_live_sell_pending(code)
            failure_at = datetime.now(SEOUL)
            if self._is_non_retryable_sell_rejection(exc.message):
                if self._suppress_order_for_day(
                    ctx, failure_at, code, "SELL", order_type,
                    exc.message,
                ):
                    ctx.log.error(
                        f"{self._instrument_label(ctx, code)} 실시간 익절·손절 매도 중단 "
                        f"[{exc.code.value}]: {exc.message} · 오늘 같은 주문은 다시 시도하지 않습니다."
                    )
            else:
                ctx.log.error(f"{self._instrument_label(ctx, code)} 실시간 익절·손절 매도 실패 [{exc.code.value}]: {exc.message}")
            return
        if order.status is OrderStatus.REJECTED:
            self._clear_live_sell_pending(code)
            message = str(order.message or "")
            if self._is_non_retryable_sell_rejection(message):
                if self._suppress_order_for_day(
                    ctx, datetime.now(SEOUL), code, "SELL", order_type,
                    message,
                ):
                    ctx.log.error(
                        f"{self._instrument_label(ctx, code)} 실시간 익절·손절 매도 중단: "
                        f"{message} · 오늘 같은 주문은 다시 시도하지 않습니다."
                    )
            return
        reason = " 그리고 ".join(
            self._condition_summary(item) for item in matched_conditions
        )
        price_text = f" {limit_price}원" if limit_price is not None else ""
        ctx.log.info(
            f"{self._instrument_label(ctx, code)} {matched_flow['name']} · "
            f"실시간 현재가 {quote.price}원 ({reason}) · "
            f"{order_type}{price_text} {quantity}주 매도 요청: {order.status.value}"
        )

    async def on_order(self, ctx, order):
        self._ensure_live_state()
        code = order.request.instrument.code
        if order.request.side.value == "BUY":
            client_order_id = str(
                getattr(order, "client_order_id", None)
                or getattr(order.request, "client_order_id", None)
                or ""
            )
            if order.status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}:
                self._clear_buy_submit_pending(
                    ctx, code, client_order_id=client_order_id,
                )
                return
            self._mark_buy_submit_pending(
                ctx, code, client_order_id=client_order_id,
                status=order.status.value,
                at=getattr(order, "submitted_at", None) or datetime.now(SEOUL),
                quantity=getattr(order.request, "quantity", None),
                order_type=getattr(getattr(order.request, "order_type", None), "value", None),
                price=getattr(order.request, "price", None),
                route=getattr(getattr(order.request, "route", None), "value", None),
                provider_order_id=getattr(order, "provider_order_id", None),
                replace=False,
            )
            if order.status is not OrderStatus.FILLED:
                return
            try:
                balance = await ctx.account.balance()
                open_orders = await ctx.account.open_orders()
                await self._refresh_live_positions(ctx, balance, open_orders)
            except RouterError as exc:
                ctx.log.warning(
                    f"{self._instrument_label(ctx, code)} 체결 후 실시간 감시 연결 보류 [{exc.code.value}]: {exc.message}"
                )
            return
        if order.request.side.value != "SELL":
            return
        if order.status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}:
            self._clear_live_sell_pending(code)
            return
        if order.status is OrderStatus.FILLED:
            self._clear_live_sell_pending(code)
            tracked = dict(ctx.state.get("strategy_positions", {}))
            tracked.pop(code, None)
            ctx.state.set("strategy_positions", tracked)
            self._live_positions.pop(code, None)
            await self._unsubscribe_live_quote(ctx, code)

    async def on_stop(self, ctx):
        ctx.log.info("관리형 조건 블록 전략을 종료합니다.")
'''


def _render_condition_bollinger(
    settings: ConditionBollingerBlockSettings,
    layout: BlockEditorLayout | None = None,
) -> str:
    replacements = {
        "__BLOCK_TEMPLATE__": _literal(CONDITION_BOLLINGER_TEMPLATE),
        "__BLOCK_LAYOUT_JSON__": _layout_literal(layout),
        "__STRATEGY_ID__": _literal(settings.strategy_id),
        "__TRADING_ACCOUNT__": _literal(settings.trading_account),
        "__CONDITION_SOURCE__": _literal(settings.condition_source),
        "__MARKET_VENUE__": _literal(settings.market_venue),
        "__TARGET_SOURCE__": _literal(settings.target_source),
        "__HTS_USER_ID__": _literal(settings.hts_user_id),
        "__CONDITION_ID__": _literal(settings.condition_id),
        "__EXPECTED_CONDITION_NAME__": _literal(settings.expected_condition_name),
        "__RANKING_KIND__": _literal(settings.ranking_kind),
        "__RANKING_VENUE__": _literal(settings.ranking_venue),
        "__RANKING_MARKET__": _literal(settings.ranking_market),
        "__RANKING_LIMIT__": str(settings.ranking_limit),
        "__SYMBOLS_JSON__": _json_literal(settings.symbols),
        "__BUY_BUDGET_PER_SYMBOL__": _literal(str(settings.buy_budget_per_symbol)),
        "__TOTAL_OPERATING_BUDGET__": _literal(str(settings.total_operating_budget)),
        "__MAX_NEW_BUYS_PER_SCAN__": str(settings.max_new_buys_per_scan),
        "__BOLLINGER_PERIOD__": str(settings.bollinger_period),
        "__BOLLINGER_STDDEV__": _literal(str(settings.bollinger_stddev)),
        "__TAKE_PROFIT_MULTIPLIER__": _literal(str(settings.take_profit_multiplier)),
        "__STOP_LOSS_MULTIPLIER__": _literal(str(settings.stop_loss_multiplier)),
        "__USE_BOLLINGER_EXIT__": str(settings.use_bollinger_exit),
        "__USE_TAKE_PROFIT__": str(settings.use_take_profit),
        "__USE_STOP_LOSS__": str(settings.use_stop_loss),
        "__BUY_CONDITION_LOGIC__": _literal(settings.buy_condition_logic),
        "__BUY_CONDITIONS_JSON__": _json_literal(settings.buy_conditions),
        "__SELL_CONDITION_LOGIC__": _literal(settings.sell_condition_logic),
        "__SELL_CONDITIONS_JSON__": _json_literal(settings.sell_conditions),
        "__BUY_CONDITION_GROUPS_JSON__": _json_literal(settings.buy_condition_groups),
        "__BUY_RULE_TREE_JSON__": _json_literal(settings.buy_rule_tree),
        "__BUY_ENABLED__": str(settings.buy_enabled),
        "__BUY_FLOWS_JSON__": _json_literal(settings.buy_flows),
        "__SELL_CONDITION_GROUPS_JSON__": _json_literal(settings.sell_condition_groups),
        "__SELL_RULE_TREE_JSON__": _json_literal(settings.sell_rule_tree),
        "__SELL_ENABLED__": str(settings.sell_enabled),
        "__SELL_POSITION_SCOPE__": _literal(settings.sell_position_scope),
        "__SELL_FLOWS_JSON__": _json_literal(settings.sell_flows),
        "__EXECUTION_FLOWS_JSON__": _json_literal(settings.execution_flows),
        "__BUY_ORDER_TYPE__": _literal(settings.buy_order_type),
        "__SELL_ORDER_TYPE__": _literal(settings.sell_order_type),
        "__BUY_SCAN_SECONDS__": str(settings.buy_scan_seconds),
        "__BUY_START_TIME__": _literal(settings.buy_start_time),
        "__BUY_END_TIME__": _literal(settings.buy_end_time),
        "__BAR_INTERVAL__": _literal(settings.bar_interval),
        "__BAR_DATA_MODE__": _literal(settings.bar_data_mode),
        "__SELL_START_TIME__": _literal(settings.sell_start_time),
        "__SELL_END_TIME__": _literal(settings.sell_end_time),
        "__CLOSE_TIME__": _literal(settings.close_time),
        "__CLOSE_RETRY_START__": _literal(settings.close_retry_start),
        "__CLOSE_RETRY_END__": _literal(settings.close_retry_end),
        "__CLOSE_CONFIRM_TIME__": _literal(settings.close_confirm_time),
        "__ORDER_ROUTE__": _literal(settings.route),
    }
    source = _CONDITION_BOLLINGER_SOURCE
    for token, value in replacements.items():
        source = source.replace(token, value)
    return source


def compile_block_strategy(
    settings: BlockStrategySettings | ConditionBollingerBlockSettings,
    *,
    template: str | None = None,
    layout: BlockEditorLayout | None = None,
) -> str:
    if isinstance(settings, ConditionBollingerBlockSettings):
        if template not in {None, CONDITION_BOLLINGER_TEMPLATE}:
            raise ValueError("Block template and settings do not match.")
        return _render_condition_bollinger(settings, layout)
    if template not in {None, BAR_OBSERVER_TEMPLATE}:
        raise ValueError("Block template and settings do not match.")
    return _render_bar_observer(settings, layout)


# Compatibility name for older extensions. New code should use the explicit
# compiler name because a block package is the source of truth.
render_block_strategy = compile_block_strategy


def _literal_value(node: ast.AST | None, constants: dict[str, object]) -> object | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_literal_value(item, constants) for item in node.elts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Decimal" and len(node.args) == 1:
        value = _literal_value(node.args[0], constants)
        return Decimal(str(value)) if value is not None else None
    return None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _constants_from_source(source: str) -> tuple[ast.Module, dict[str, object]]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("Block strategy source is not valid Python.") from exc
    constants: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) and node.targets else node.target
        if not isinstance(target, ast.Name):
            continue
        value = _literal_value(node.value, constants)
        if value is not None or target.id in {"EXECUTION_FLOWS_JSON", "BUY_ENABLED", "SELL_ENABLED", "SELL_POSITION_SCOPE"}:
            constants[target.id] = value
    return tree, constants


def _condition_package(name: str, constants: dict[str, object]) -> BlockStrategyPackage:
    mapping = {
        "strategy_id": "STRATEGY_ID", "trading_account": "TRADING_ACCOUNT",
        "condition_source": "CONDITION_SOURCE", "market_venue": "MARKET_VENUE", "hts_user_id": "HTS_USER_ID",
        "condition_id": "CONDITION_ID", "expected_condition_name": "EXPECTED_CONDITION_NAME",
        "target_source": "TARGET_SOURCE", "ranking_kind": "RANKING_KIND",
        "ranking_venue": "RANKING_VENUE", "ranking_market": "RANKING_MARKET",
        "ranking_limit": "RANKING_LIMIT",
        "buy_budget_per_symbol": "BUY_BUDGET_PER_SYMBOL", "total_operating_budget": "TOTAL_OPERATING_BUDGET",
        "max_new_buys_per_scan": "MAX_NEW_BUYS_PER_SCAN", "bollinger_period": "BOLLINGER_PERIOD",
        "bollinger_stddev": "BOLLINGER_STDDEV", "take_profit_multiplier": "TAKE_PROFIT_MULTIPLIER",
        "stop_loss_multiplier": "STOP_LOSS_MULTIPLIER",
        "use_bollinger_exit": "USE_BOLLINGER_EXIT", "use_take_profit": "USE_TAKE_PROFIT",
        "use_stop_loss": "USE_STOP_LOSS", "buy_scan_seconds": "BUY_SCAN_SECONDS",
        "buy_condition_logic": "BUY_CONDITION_LOGIC", "sell_condition_logic": "SELL_CONDITION_LOGIC",
        "buy_order_type": "BUY_ORDER_TYPE", "sell_order_type": "SELL_ORDER_TYPE",
        "buy_start_time": "BUY_START_TIME", "buy_end_time": "BUY_END_TIME",
        "bar_interval": "BAR_INTERVAL", "bar_data_mode": "BAR_DATA_MODE",
        "sell_start_time": "SELL_START_TIME",
        "sell_end_time": "SELL_END_TIME", "close_time": "CLOSE_TIME",
        "close_retry_start": "CLOSE_RETRY_START", "close_retry_end": "CLOSE_RETRY_END",
        "close_confirm_time": "CLOSE_CONFIRM_TIME", "route": "ORDER_ROUTE",
    }
    required = {field: constants.get(source_name) for field, source_name in mapping.items()}
    # Empty flow lists meant the legacy single flow in older exports. Preserve
    # that behavior unless the user explicitly removed this side's last flow.
    for side in ("buy", "sell"):
        required[f"{side}_enabled"] = constants.get(f"{side.upper()}_ENABLED", True)
    required["sell_position_scope"] = constants.get("SELL_POSITION_SCOPE", "STRATEGY")
    # v0.1.13 packages always enabled the three built-in exit conditions.
    for field in ("use_bollinger_exit", "use_take_profit", "use_stop_loss"):
        if required[field] is None:
            required[field] = True
    defaults = {
        "target_source": "HTS_CONDITION", "market_venue": "KRX", "bar_data_mode": "COMPLETE_ONLY",
        "ranking_kind": "RISE", "ranking_venue": "KRX",
        "ranking_market": "ALL", "ranking_limit": 20,
        "buy_condition_logic": "ALL", "sell_condition_logic": "ANY",
        "buy_order_type": "MARKET", "sell_order_type": "MARKET",
    }
    for field, value in defaults.items():
        if required[field] is None:
            required[field] = value
    symbols_json = constants.get("SYMBOLS_JSON")
    try:
        required["symbols"] = json.loads(symbols_json) if isinstance(symbols_json, str) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        required["symbols"] = []
    buy_json = constants.get("BUY_CONDITIONS_JSON")
    sell_json = constants.get("SELL_CONDITIONS_JSON")
    buy_groups_json = constants.get("BUY_CONDITION_GROUPS_JSON")
    sell_groups_json = constants.get("SELL_CONDITION_GROUPS_JSON")
    buy_rule_tree_json = constants.get("BUY_RULE_TREE_JSON")
    sell_rule_tree_json = constants.get("SELL_RULE_TREE_JSON")
    buy_flows_json = constants.get("BUY_FLOWS_JSON")
    sell_flows_json = constants.get("SELL_FLOWS_JSON")
    execution_flows_json = constants.get("EXECUTION_FLOWS_JSON")
    if "EXECUTION_FLOWS_JSON" not in constants:
        required["execution_flows"] = []
    else:
        if not isinstance(execution_flows_json, str):
            raise ValueError("체결 흐름 설정을 읽을 수 없습니다.")
        try:
            required["execution_flows"] = json.loads(execution_flows_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("체결 흐름 설정을 읽을 수 없습니다.") from exc
    # Missing means a pre-flow strategy; malformed explicit flow data must not
    # silently fall back to the legacy discovery source and trade differently.
    if buy_flows_json is None:
        required["buy_flows"] = []
    else:
        if not isinstance(buy_flows_json, str):
            raise ValueError("매수 흐름 설정을 읽을 수 없습니다.")
        try:
            required["buy_flows"] = json.loads(buy_flows_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("매수 흐름 설정을 읽을 수 없습니다.") from exc
    # As with buy flows, absence means a legacy strategy and explicit malformed
    # metadata must fail closed instead of silently changing live behavior.
    if sell_flows_json is None:
        required["sell_flows"] = []
    else:
        if not isinstance(sell_flows_json, str):
            raise ValueError("매도 흐름 설정을 읽을 수 없습니다.")
        try:
            required["sell_flows"] = json.loads(sell_flows_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("매도 흐름 설정을 읽을 수 없습니다.") from exc
    try:
        required["buy_conditions"] = json.loads(buy_json) if isinstance(buy_json, str) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        required["buy_conditions"] = []
    try:
        required["sell_conditions"] = json.loads(sell_json) if isinstance(sell_json, str) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        required["sell_conditions"] = []
    try:
        required["buy_condition_groups"] = (
            json.loads(buy_groups_json) if isinstance(buy_groups_json, str) else None
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        required["buy_condition_groups"] = None
    try:
        required["sell_condition_groups"] = (
            json.loads(sell_groups_json) if isinstance(sell_groups_json, str) else None
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        required["sell_condition_groups"] = None
    for field, raw in (
        ("buy_rule_tree", buy_rule_tree_json),
        ("sell_rule_tree", sell_rule_tree_json),
    ):
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise ValueError("조건 계층 설정을 읽을 수 없습니다.")
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("조건 계층 설정을 읽을 수 없습니다.") from exc
        if decoded is not None:
            required[field] = decoded
    if not required["sell_conditions"]:
        legacy_conditions: list[dict[str, object]] = []
        if required["use_take_profit"]:
            multiplier = Decimal(str(required["take_profit_multiplier"]))
            legacy_conditions.append({"left": "profit_rate", "operator": "GTE", "right": "value", "value": (multiplier - 1) * 100})
        if required["use_stop_loss"]:
            multiplier = Decimal(str(required["stop_loss_multiplier"]))
            legacy_conditions.append({"left": "profit_rate", "operator": "LTE", "right": "value", "value": (multiplier - 1) * 100})
        if required["use_bollinger_exit"]:
            legacy_conditions.append({"left": "close", "operator": "LT", "right": "bollinger_upper", "value": 0})
        required["sell_conditions"] = legacy_conditions or [item.model_dump() for item in DEFAULT_SELL_CONDITIONS]
    if required["buy_condition_groups"] is None:
        required.pop("buy_condition_groups")
    if required["sell_condition_groups"] is None:
        required.pop("sell_condition_groups")
    if any(value is None for value in required.values()):
        raise ValueError("This condition/Bollinger block has unsupported fields.")
    settings = ConditionBollingerBlockSettings.model_validate(required)
    return BlockStrategyPackage(
        name=name,
        template=CONDITION_BOLLINGER_TEMPLATE,
        settings=settings,
        layout=_layout_from_constants(constants),
    )


def _bar_observer_package(name: str, tree: ast.Module, constants: dict[str, object]) -> BlockStrategyPackage:
    schedule_id = start_time = end_time = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "on_bar_close":
            continue
        keywords = {item.arg: item.value for item in node.keywords if item.arg}
        schedule_id = _literal_value(keywords.get("schedule_id"), constants)
        between = _literal_value(keywords.get("between"), constants)
        if isinstance(between, tuple) and len(between) == 2:
            start_time, end_time = between
        break
    required = {
        "strategy_id": constants.get("STRATEGY_ID"), "trading_account": constants.get("TRADING_ACCOUNT"),
        "market_source": constants.get("MARKET_SOURCE"), "symbol": constants.get("SYMBOL"),
        "bar_interval": constants.get("BAR_INTERVAL"), "bar_count": constants.get("BAR_COUNT"),
        "schedule_id": schedule_id, "start_time": start_time, "end_time": end_time,
    }
    if any(value is None for value in required.values()):
        raise ValueError("This block strategy uses unsupported blocks.")
    settings = BlockStrategySettings.model_validate(required)
    return BlockStrategyPackage(
        name=name,
        template=BAR_OBSERVER_TEMPLATE,
        settings=settings,
        layout=_layout_from_constants(constants),
    )


def block_package_from_source(name: str, source: str) -> BlockStrategyPackage:
    tree, constants = _constants_from_source(source)
    template = constants.get("BLOCK_TEMPLATE")
    if template == CONDITION_BOLLINGER_TEMPLATE:
        package = _condition_package(name, constants)
    else:
        if template not in {None, BAR_OBSERVER_TEMPLATE}:
            raise ValueError(f"Unsupported block template: {template}")
        package = _bar_observer_package(name, tree, constants)
    revision_payload = {
        "template": package.template,
        "settings": package.settings.model_dump(mode="json"),
    }
    revision = hashlib.sha256(
        json.dumps(
            revision_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return package.model_copy(update={"revision": revision})
