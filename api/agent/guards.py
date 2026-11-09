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

import json
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
        if self.used >= self.max_steps:
            return False
        self.used += 1
        return True


class CostBudget:
    def __init__(
        self, max_usd: Decimal, spent: Decimal = Decimal("0"), unknown_calls: int = 0
    ) -> None:
        self.max_usd = max_usd
        self.spent = spent
        self.unknown_calls = unknown_calls

    def add(self, cost: Decimal | None) -> bool:
        if cost is None:
            self.unknown_calls += 1
        else:
            self.spent += cost
        return self.spent < self.max_usd


class RepeatDetector:
    def __init__(self, last_key: str | None = None, streak: int = 0) -> None:
        self.last_key = last_key
        self.streak = streak

    def check(self, call: ToolCall) -> RepeatVerdict:
        key = _call_key(call)
        if key == self.last_key:
            self.streak += 1
        else:
            self.last_key = key
            self.streak = 1
        if self.streak == 1:
            return "ok"
        if self.streak == 2:
            return "repeat_error"
        return "terminate"

    def to_dict(self) -> dict[str, Any]:
        return {"last_key": self.last_key, "streak": self.streak}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepeatDetector":
        last_key = data.get("last_key")
        return cls(
            last_key=str(last_key) if last_key is not None else None,
            streak=int(data.get("streak", 0)),
        )


def _call_key(call: ToolCall) -> str:
    """Tool name plus canonical JSON arguments: key order never matters."""
    return call.name + json.dumps(
        call.arguments, sort_keys=True, separators=(",", ":"), default=str
    )
