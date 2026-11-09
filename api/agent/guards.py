"""Per-request agent guards (CT-47, AGT-02, AGT-03, AGT-05, AGT-09, AGT-10, LAC-21).

- `StepBudget(max_steps)`: `consume()` returns True while a model call is allowed (8 by default,
  `ATLAS_AGENT_MAX_STEPS`); the (max_steps + 1)-th `consume()` returns False.
- `CostBudget(max_usd)`: `add(cost)` accumulates the USD cost of a model call and returns False
  once the total reaches the limit (`>= max_usd`, `ATLAS_AGENT_MAX_COST_USD`, default 0.25,
  premise P-3). `cost=None` means unknown (subscription CLI providers, LAC-43; user decision):
  it is not added, `unknown_calls` counts it, and `add` returns the current state.
- `RepeatDetector.check(call)`: compares `(tool, canonical JSON arguments)` with the immediately
  previous call: 1st -> `ok`; 2nd identical in a row -> `repeat_error` (goes back to the model as
  an error); 3rd identical in a row -> `terminate`. Different arguments reset the streak.
- `LimitReason`: `step_limit | cost_limit | repeat_limit`.
State is plain data so it can live inside the JSON `AgentState` (`to_dict` / `from_dict`).
"""

from decimal import Decimal
from typing import Any, Literal

from api.llm.client import ToolCall

LimitReason = Literal["step_limit", "cost_limit", "repeat_limit"]
RepeatVerdict = Literal["ok", "repeat_error", "terminate"]


class StepBudget:
    def __init__(self, max_steps: int, used: int = 0) -> None:
        self.max_steps = max_steps
        self.used = used

    def consume(self) -> bool:
        raise NotImplementedError("TASK-083")


class CostBudget:
    def __init__(
        self, max_usd: Decimal, spent: Decimal = Decimal("0"), unknown_calls: int = 0
    ) -> None:
        self.max_usd = max_usd
        self.spent = spent
        self.unknown_calls = unknown_calls

    def add(self, cost: Decimal | None) -> bool:
        raise NotImplementedError("TASK-083")


class RepeatDetector:
    def __init__(self, last_key: str | None = None, streak: int = 0) -> None:
        self.last_key = last_key
        self.streak = streak

    def check(self, call: ToolCall) -> RepeatVerdict:
        raise NotImplementedError("TASK-083")

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError("TASK-083")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepeatDetector":
        raise NotImplementedError("TASK-083")
