"""Validated condition/action trees for managing existing strategy orders."""

from __future__ import annotations

from datetime import time
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExecutionCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: Literal[
        "order_side", "order_type", "elapsed_seconds", "remaining_quantity", "filled_quantity"
    ]
    operator: Literal["==", "!=", ">", ">=", "<", "<="] = ">="
    value: str | Decimal

    @field_validator("value", mode="before")
    @classmethod
    def reject_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("체결 조건의 값에는 숫자나 주문 구분을 입력하세요.")
        return value

    @model_validator(mode="after")
    def validate_comparison(self):
        choices = {
            "order_side": {"BUY", "SELL"},
            "order_type": {"MARKET", "LIMIT", "BEST_LIMIT"},
        }
        if self.metric in choices:
            if self.operator not in {"==", "!="} or self.value not in choices[self.metric]:
                raise ValueError("주문 구분은 지정된 값과 같음·다름으로 비교하세요.")
        else:
            try:
                number = Decimal(str(self.value))
            except InvalidOperation as exc:
                raise ValueError("체결 조건의 값에는 유한한 0 이상의 숫자가 필요합니다.") from exc
            if not number.is_finite() or not 0 <= number <= Decimal("1000000000000"):
                raise ValueError("체결 조건의 숫자 범위가 올바르지 않습니다.")
            self.value = number
        return self


class ExecutionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["execution"] = "execution"
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)
    action_type: Literal["CANCEL_REMAINDER", "AMEND_BEST_LIMIT"]


class ExecutionConditionNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["condition"] = "condition"
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)
    condition: ExecutionCondition
    action: ExecutionAction | None = None


class ExecutionLogicNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["logic"] = "logic"
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)
    operator: Literal["ALL", "ANY"] = "ALL"
    children: list["ExecutionNode"] = Field(default_factory=list, max_length=20)
    action: ExecutionAction | None = None


ExecutionNode = Annotated[ExecutionConditionNode | ExecutionLogicNode, Field(discriminator="kind")]
ExecutionLogicNode.model_rebuild(_types_namespace={"ExecutionNode": ExecutionNode})


class BlockExecutionFlow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=64)
    name: str = Field(default="체결 흐름", min_length=1, max_length=80)
    execution_start_time: str = "08:00"
    execution_end_time: str = "20:00"
    execution_scan_seconds: int = Field(default=5, ge=5, le=3600)
    execution_rule_tree: ExecutionLogicNode

    @field_validator("execution_start_time", "execution_end_time")
    @classmethod
    def validate_clock(cls, value):
        value = value.strip()
        if len(value) not in {5, 8}:
            raise ValueError("시간은 HH:MM 또는 HH:MM:SS로 입력하세요.")
        parsed = time.fromisoformat(value)
        if parsed.tzinfo is not None:
            raise ValueError("체결 시간은 한국 시각으로 입력하세요.")
        return value

    @model_validator(mode="after")
    def validate_flow(self):
        if time.fromisoformat(self.execution_start_time) >= time.fromisoformat(self.execution_end_time):
            raise ValueError("체결 흐름의 시작 시간은 종료 시간보다 빨라야 합니다.")
        ids: set[str] = set()
        count = 0

        def walk(node, depth=0):
            nonlocal count
            count += 1
            if depth > 8 or count > 100:
                raise ValueError("체결 조건은 최대 8단계·100개 블록까지 사용할 수 있습니다.")
            for identifier in [node.id] + ([node.action.id] if node.action else []):
                if identifier in ids:
                    raise ValueError("같은 체결 흐름의 블록 ID는 서로 달라야 합니다.")
                ids.add(identifier)
            if isinstance(node, ExecutionLogicNode):
                if not node.children and (depth or node.action):
                    raise ValueError("빈 체결 조건 묶음에는 행동을 연결할 수 없습니다.")
                action_branches = sum(walk(child, depth + 1) for child in node.children)
                if node.operator == "ALL" and action_branches > 1:
                    raise ValueError("AND 조건의 행동은 하나의 하위 분기나 묶음에 연결하세요.")
                return bool(node.action or action_branches)
            return bool(node.action)

        walk(self.execution_rule_tree)
        return self
