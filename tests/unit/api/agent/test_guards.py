"""Agent guards (TASK-083, CT-47, AGT-02, AGT-03, AGT-05, AGT-09, AGT-10, LAC-21)."""

import json
from decimal import Decimal

from api.agent.guards import CostBudget, RepeatDetector, StepBudget
from api.llm.client import ToolCall


def _call(name: str = "get_order", call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=dict(arguments))


# --- StepBudget (AGT-02) ---------------------------------------------------------------------


def test_step_budget_allows_eight_consumes_and_denies_the_ninth() -> None:
    budget = StepBudget(8)

    allowed = [budget.consume() for _ in range(8)]

    assert allowed == [True] * 8
    assert budget.consume() is False
    assert budget.consume() is False
    assert budget.used == 8


def test_step_budget_resumes_from_used_steps() -> None:
    budget = StepBudget(3, used=2)

    assert budget.consume() is True
    assert budget.consume() is False


# --- CostBudget (AGT-03) ---------------------------------------------------------------------


def test_cost_budget_denies_when_the_total_reaches_the_limit() -> None:
    budget = CostBudget(Decimal("0.25"))

    assert budget.add(Decimal("0.10")) is True
    assert budget.add(Decimal("0.10")) is True
    assert budget.add(Decimal("0.05")) is False  # exactly 0.25: reached
    assert budget.spent == Decimal("0.25")
    assert budget.add(Decimal("0")) is False


def test_cost_budget_denies_when_one_call_overshoots() -> None:
    budget = CostBudget(Decimal("0.25"), spent=Decimal("0.20"))

    assert budget.add(Decimal("0.30")) is False
    assert budget.spent == Decimal("0.50")


def test_unknown_cost_is_not_added_and_is_counted() -> None:
    budget = CostBudget(Decimal("0.25"), spent=Decimal("0.10"))

    assert budget.add(None) is True
    assert budget.add(None) is True
    assert budget.spent == Decimal("0.10")
    assert budget.unknown_calls == 2


def test_unknown_cost_after_the_limit_keeps_denying() -> None:
    budget = CostBudget(Decimal("0.25"), spent=Decimal("0.25"))

    assert budget.add(None) is False
    assert budget.unknown_calls == 1


# --- RepeatDetector (AGT-09, AGT-10, LAC-21) ---------------------------------------------------


def test_three_identical_calls_in_a_row_are_ok_repeat_error_terminate() -> None:
    detector = RepeatDetector()

    verdicts = [detector.check(_call(call_id=f"c{i}", order_id=1001)) for i in range(3)]

    assert verdicts == ["ok", "repeat_error", "terminate"]


def test_argument_key_order_does_not_matter() -> None:
    detector = RepeatDetector()

    first = detector.check(ToolCall(id="a", name="t", arguments={"a": 1, "b": 2}))
    second = detector.check(ToolCall(id="b", name="t", arguments={"b": 2, "a": 1}))

    assert (first, second) == ("ok", "repeat_error")


def test_changing_arguments_or_tool_resets_the_streak() -> None:
    detector = RepeatDetector()

    assert detector.check(_call(order_id=1001)) == "ok"
    assert detector.check(_call(order_id=1001)) == "repeat_error"
    assert detector.check(_call(order_id=1002)) == "ok"
    assert detector.check(_call(order_id=1002)) == "repeat_error"
    assert detector.check(_call("list_orders", order_id=1002)) == "ok"
    assert detector.check(_call(order_id=1002)) == "ok"
    assert detector.streak == 1


def test_detector_state_round_trips_through_json() -> None:
    detector = RepeatDetector()
    detector.check(_call(order_id=1001))
    detector.check(_call(order_id=1001))

    data = json.loads(json.dumps(detector.to_dict()))
    restored = RepeatDetector.from_dict(data)

    assert restored.last_key == detector.last_key
    assert restored.streak == 2
    assert restored.check(_call(order_id=1001)) == "terminate"


def test_detector_from_empty_dict_starts_fresh() -> None:
    restored = RepeatDetector.from_dict({})

    assert (restored.last_key, restored.streak) == (None, 0)
    assert restored.check(_call(order_id=1)) == "ok"


def test_detector_key_uses_tool_and_canonical_arguments() -> None:
    detector = RepeatDetector()
    detector.check(ToolCall(id="a", name="get_order", arguments={"b": 2, "a": 1}))

    assert detector.last_key == 'get_order{"a":1,"b":2}'
