"""Agent loop limits (TASK-084, AGT-02, AGT-03, AGT-05, AGT-08, AGT-09, AGT-10, LAC-21, AS-2)."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

import api.agent.loop as loop_module
from api.agent.events import (
    AgentEvent,
    ConfirmationPrompt,
    DoneEvent,
    TokenEvent,
    ToolStartEvent,
)
from api.agent.loop import REPEATED_CALL, AgentLoop, AgentState, AgentTurn
from api.config import Settings
from api.llm.client import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    SystemBlock,
    ToolCall,
    Usage,
)
from api.llm.fake import FakeLLMClient as ScriptedLLMClient
from api.llm.pricing import UnknownModelPricing
from api.messages import MessageKey, user_message
from api.tools.registry import ToolContext, ToolDefinition, ToolErrorCode, ToolRegistry, ToolResult

PRICED_MODEL = "priced-model"


class FakeLLMClient(ScriptedLLMClient):
    """Phase 09: these tests price each scripted response by its own `model`/`provider`. A
    streamed call does not carry them (the loop then charges the client's identity), so this
    fake answers like the non-streaming providers: `stream()` raises `NotImplementedError` and
    `stream_or_complete` falls back to `complete()`, keeping the response's identity."""

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError


class OrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


class FakeSavepoint:
    async def __aenter__(self) -> "FakeSavepoint":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class FakeSession:
    def begin_nested(self) -> FakeSavepoint:
        return FakeSavepoint()


class Spy:
    def __init__(self, result: ToolResult | None = None) -> None:
        self.calls: list[Any] = []
        self.result = result or ToolResult(ok=True, content={"done": True})

    async def __call__(self, args: Any, ctx: ToolContext) -> ToolResult:
        self.calls.append(args)
        return self.result


class Fixture:
    def __init__(self) -> None:
        self.get_order = Spy(ToolResult(ok=True, content={"id": 1001, "status": "paid"}))
        self.cancel_order = Spy(ToolResult(ok=True, content={"status": "cancelled"}))
        self.cancel_precheck = Spy(ToolResult(ok=True))
        self.registry = ToolRegistry()
        self.registry.register(
            ToolDefinition(
                name="get_order",
                description="get_order description",
                args_model=OrderArgs,
                destructive=False,
                handler=self.get_order,
                label_key="STEP_GET_ORDER",
            )
        )
        self.registry.register(
            ToolDefinition(
                name="cancel_order",
                description="cancel_order description",
                args_model=OrderArgs,
                destructive=True,
                handler=self.cancel_order,
                label_key="STEP_CANCEL_ORDER",
                precheck=self.cancel_precheck,
            )
        )
        self.suspended: list[AgentState] = []
        self.audit_rows: list[Any] = []
        self.audit_by_trace: dict[str, list[Any]] = {}
        self.audit_traces: list[str] = []

    async def suspend(self, call: ToolCall, state: AgentState) -> ConfirmationPrompt:
        self.suspended.append(state)
        return ConfirmationPrompt(id=uuid.uuid4(), tool=call.name, target=str(call.arguments))

    async def audit_reader(self, trace_id: str) -> list[Any]:
        self.audit_traces.append(trace_id)
        return list(self.audit_by_trace.get(trace_id, self.audit_rows))


@pytest.fixture
def events_log(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    recorded: list[tuple[str, dict[str, object]]] = []

    def fake_log_event(event: str, **fields: object) -> None:
        recorded.append((event, fields))

    monkeypatch.setattr(loop_module, "log_event", fake_log_event)
    return recorded


@pytest.fixture
def priced(monkeypatch: pytest.MonkeyPatch) -> dict[str, Decimal]:
    """Per-model cost of one call; models outside the table keep the real pricing."""
    table: dict[str, Decimal] = {}
    real = loop_module.estimate_cost

    def fake_estimate(model: str, usage: Usage) -> Decimal:
        if model in table:
            return table[model]
        return real(model, usage)

    monkeypatch.setattr(loop_module, "estimate_cost", fake_estimate)
    return table


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None).model_copy(update=overrides)


def _ctx(trace_id: str = "trace-limits") -> ToolContext:
    return ToolContext(
        customer_id=uuid.uuid4(),
        customer_email="alex@example.com",
        trace_id=trace_id,
        session=cast(AsyncSession, FakeSession()),
        language="en",
        now=datetime(2026, 11, 9, tzinfo=UTC),
    )


def _response(
    *calls: ToolCall, text: str = "", model: str = "fake-model", provider: str = "fake"
) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls),
        usage=Usage(input_tokens=10, output_tokens=5),
        model=model,
        provider=provider,
        stop_reason="tool_use" if calls else "end_turn",
    )


def _get(call_id: str, order_id: int = 1001) -> ToolCall:
    return ToolCall(id=call_id, name="get_order", arguments={"order_id": order_id})


def _turn(fx: Fixture, ctx: ToolContext | None = None) -> AgentTurn:
    return AgentTurn(
        request=LLMRequest(
            system=[SystemBlock(text="You are Atlas.", cache_breakpoint=True)],
            messages=[ChatMessage(role="user", content="cancel my order 1001")],
            tools=fx.registry.specs(),
        ),
        ctx=ctx or _ctx(),
        suspend=fx.suspend,
    )


async def _collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    # Phase 09: `token` events are covered by test_loop_streaming.py.
    return [event async for event in stream if not isinstance(event, TokenEvent)]


def _done(events: list[AgentEvent]) -> DoneEvent:
    last = events[-1]
    assert isinstance(last, DoneEvent)
    return last


def _tool_payloads(request: LLMRequest) -> list[dict[str, Any]]:
    return [json.loads(m.content) for m in request.messages if m.role == "tool"]


def _named(events_log: list[tuple[str, dict[str, object]]], name: str) -> list[dict[str, object]]:
    return [fields for event, fields in events_log if event == name]


# --- AGT-02 / AGT-08: step limit ---------------------------------------------------------------


async def test_step_limit_makes_exactly_max_steps_calls_and_lists_audited_actions(
    events_log: list[tuple[str, dict[str, object]]],
) -> None:
    fx = Fixture()
    fx.audit_rows = [SimpleNamespace(tool="cancel_order"), SimpleNamespace(tool="send_email")]
    llm = FakeLLMClient([_response(_get(f"c{i}", order_id=i + 1)) for i in range(5)])
    loop = AgentLoop(llm, fx.registry, _settings(agent_max_steps=2), fx.audit_reader)

    events = await _collect(loop.run(_turn(fx, _ctx("trace-steps"))))

    assert len(llm.requests) == 2
    # The tool asked by the call that used the last step never runs (AGT-02).
    assert len(fx.get_order.calls) == 1
    assert not [e for e in events if isinstance(e, ToolStartEvent) and e.call_id == "c1"]
    done = _done(events)
    assert done.status == "limit_reached"
    assert done.content == user_message(
        MessageKey.AGENT_LIMIT_REACHED, "en", actions="cancel_order, send_email"
    )
    assert fx.audit_traces == ["trace-steps"]
    assert _named(events_log, "agent_limit") == [
        {"reason": "step_limit", "steps": 2, "cost_usd": "0", "unknown_cost_calls": 0}
    ]


async def test_step_limit_message_in_portuguese_without_actions() -> None:
    fx = Fixture()
    llm = FakeLLMClient([_response(_get(f"c{i}", order_id=i + 1)) for i in range(3)])
    loop = AgentLoop(llm, fx.registry, _settings(agent_max_steps=1), fx.audit_reader)
    ctx = _ctx()
    ctx_pt = ToolContext(
        customer_id=ctx.customer_id,
        customer_email=ctx.customer_email,
        trace_id=ctx.trace_id,
        session=ctx.session,
        language="pt",
        now=ctx.now,
    )

    events = await _collect(loop.run(_turn(fx, ctx_pt)))

    assert len(llm.requests) == 1
    assert _done(events).content == user_message(MessageKey.AGENT_LIMIT_REACHED, "pt", actions="—")


# --- AGT-03 / AS-2: cost limit -----------------------------------------------------------------


async def test_cost_limit_ends_without_running_tools_or_calling_the_model_again(
    priced: dict[str, Decimal], events_log: list[tuple[str, dict[str, object]]]
) -> None:
    priced[PRICED_MODEL] = Decimal("0.15")
    fx = Fixture()
    llm = FakeLLMClient(
        [
            _response(_get("c1", 1), model=PRICED_MODEL),
            _response(_get("c2", 2), model=PRICED_MODEL),
            _response(_get("c3", 3), model=PRICED_MODEL),
        ]
    )
    loop = AgentLoop(llm, fx.registry, _settings(agent_max_cost_usd=Decimal("0.25")))

    events = await _collect(loop.run(_turn(fx)))

    assert len(llm.requests) == 2  # 0.15 then 0.30 >= 0.25: no third call
    assert len(fx.get_order.calls) == 1  # the tool of the call that reached the limit never ran
    assert not [e for e in events if isinstance(e, ToolStartEvent) and e.call_id == "c2"]
    assert _done(events).status == "limit_reached"
    assert _named(events_log, "agent_limit") == [
        {"reason": "cost_limit", "steps": 2, "cost_usd": "0.3", "unknown_cost_calls": 0}
    ]


async def test_final_answer_that_reaches_the_cost_limit_ends_with_the_limit_message(
    priced: dict[str, Decimal], events_log: list[tuple[str, dict[str, object]]]
) -> None:
    priced[PRICED_MODEL] = Decimal("0.30")
    fx = Fixture()
    fx.audit_rows = [SimpleNamespace(tool="cancel_order")]
    llm = FakeLLMClient([_response(text="Your order is on its way.", model=PRICED_MODEL)])
    loop = AgentLoop(llm, fx.registry, _settings(), fx.audit_reader)

    events = await _collect(loop.run(_turn(fx)))

    done = _done(events)
    assert done.status == "limit_reached"
    assert done.content == user_message(
        MessageKey.AGENT_LIMIT_REACHED, "en", actions="cancel_order"
    )
    assert _named(events_log, "agent_limit") == [
        {"reason": "cost_limit", "steps": 1, "cost_usd": "0.3", "unknown_cost_calls": 0}
    ]


async def test_unpriced_final_answer_ends_with_the_limit_message() -> None:
    fx = Fixture()
    llm = FakeLLMClient([_response(text="Done.", model="model-without-price")])
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx)))

    assert _done(events).status == "limit_reached"


async def test_state_restored_at_the_cost_limit_ends_before_any_model_call(
    priced: dict[str, Decimal], events_log: list[tuple[str, dict[str, object]]]
) -> None:
    priced[PRICED_MODEL] = Decimal("0.10")
    fx = Fixture()
    cancel = ToolCall(id="d1", name="cancel_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([_response(cancel, model=PRICED_MODEL)])
    loop = AgentLoop(llm, fx.registry, _settings(agent_max_cost_usd=Decimal("0.25")))
    await _collect(loop.run(_turn(fx)))
    stored = json.loads(json.dumps(fx.suspended[0].to_dict()))
    stored["cost"]["spent"] = "0.25"
    llm.enqueue(_response(text="never"))

    events = await _collect(
        loop.resume(AgentState.from_dict(stored), "confirm", _ctx(), suspend=fx.suspend)
    )

    assert len(llm.requests) == 1  # only the call before the suspension
    assert llm.remaining == 1
    assert _done(events).status == "limit_reached"
    assert [f["reason"] for f in _named(events_log, "agent_limit")] == ["cost_limit"]


async def test_subscription_provider_cost_is_unknown_and_never_reaches_the_limit(
    events_log: list[tuple[str, dict[str, object]]],
) -> None:
    fx = Fixture()
    llm = FakeLLMClient(
        [
            _response(_get("c1", 1), model="sonnet-unpriced", provider="claude_cli"),
            _response(_get("c2", 2), model="gpt-unpriced", provider="codex_cli"),
            _response(text="Done.", model="sonnet-unpriced", provider="claude_cli"),
        ]
    )
    loop = AgentLoop(llm, fx.registry, _settings(agent_max_cost_usd=Decimal("0.0001")))

    events = await _collect(loop.run(_turn(fx)))

    assert _done(events).status == "completed"
    assert len(llm.requests) == 3
    assert len(_named(events_log, "agent_cost_unknown")) == 3
    assert _named(events_log, "agent_limit") == []


async def test_unknown_model_pricing_is_treated_as_a_cost_limit(
    events_log: list[tuple[str, dict[str, object]]],
) -> None:
    fx = Fixture()
    llm = FakeLLMClient(
        [_response(_get("c1"), model="model-without-price"), _response(text="never")]
    )
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx)))

    assert len(llm.requests) == 1
    assert fx.get_order.calls == []
    assert _done(events).status == "limit_reached"
    assert [f["model"] for f in _named(events_log, "agent_cost_unpriced_model")] == [
        "model-without-price"
    ]
    assert _named(events_log, "agent_limit") == [
        {"reason": "cost_limit", "steps": 1, "cost_usd": "0", "unknown_cost_calls": 0}
    ]


def test_unknown_model_pricing_really_raises_for_the_unpriced_model() -> None:
    with pytest.raises(UnknownModelPricing):
        loop_module.estimate_cost("model-without-price", Usage(input_tokens=1, output_tokens=1))


async def test_cost_accumulates_across_suspension_and_resume(priced: dict[str, Decimal]) -> None:
    priced[PRICED_MODEL] = Decimal("0.15")
    fx = Fixture()
    cancel = ToolCall(id="d1", name="cancel_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([_response(cancel, model=PRICED_MODEL)])
    loop = AgentLoop(llm, fx.registry, _settings())
    await _collect(loop.run(_turn(fx)))
    stored = json.loads(json.dumps(fx.suspended[0].to_dict()))
    assert stored["version"] == 2
    assert stored["cost"] == {"spent": "0.15", "unknown_calls": 0}
    llm.enqueue(_response(_get("c2"), model=PRICED_MODEL), _response(text="never"))

    events = await _collect(
        loop.resume(AgentState.from_dict(stored), "confirm", _ctx(), suspend=fx.suspend)
    )

    assert len(fx.cancel_order.calls) == 1
    assert len(llm.requests) == 2  # 0.15 + 0.15 >= 0.25 after the resumed call
    assert fx.get_order.calls == []
    assert _done(events).status == "limit_reached"


# --- AGT-05 / AGT-09 / AGT-10: repeated calls --------------------------------------------------


async def test_second_identical_call_is_an_error_and_the_third_ends_the_loop(
    events_log: list[tuple[str, dict[str, object]]],
) -> None:
    fx = Fixture()
    fx.audit_rows = [SimpleNamespace(tool="send_email")]
    llm = FakeLLMClient(
        [
            _response(_get("c1")),
            _response(_get("c2")),
            _response(_get("c3")),
            _response(text="never"),
        ]
    )
    loop = AgentLoop(llm, fx.registry, _settings(), fx.audit_reader)

    events = await _collect(loop.run(_turn(fx)))

    assert len(fx.get_order.calls) == 1
    assert len(llm.requests) == 3
    assert _tool_payloads(llm.requests[2])[-1] == {
        "ok": False,
        "error": "repeated call",
        "error_code": REPEATED_CALL,
    }
    assert [e.call_id for e in events if isinstance(e, ToolStartEvent)] == ["c1"]
    done = _done(events)
    assert done.status == "limit_reached"
    assert done.content == user_message(MessageKey.AGENT_LIMIT_REACHED, "en", actions="send_email")
    assert [f["verdict"] for f in _named(events_log, "agent_repeat_detected")] == [
        "repeat_error",
        "terminate",
    ]
    assert [f["reason"] for f in _named(events_log, "agent_limit")] == ["repeat_limit"]


async def test_changing_arguments_resets_the_repeat_streak() -> None:
    fx = Fixture()
    llm = FakeLLMClient(
        [
            _response(_get("c1", 1)),
            _response(_get("c2", 1)),
            _response(_get("c3", 2)),
            _response(_get("c4", 2)),
            _response(text="Done."),
        ]
    )
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx)))

    assert _done(events).status == "completed"
    assert len(fx.get_order.calls) == 2


async def test_repeated_destructive_call_is_refused_before_the_precheck() -> None:
    fx = Fixture()
    fx.cancel_precheck.result = ToolResult(ok=False, error_code=ToolErrorCode.ORDER_NOT_CANCELLABLE)
    cancel = {"name": "cancel_order", "arguments": {"order_id": 1001}}
    llm = FakeLLMClient(
        [
            _response(ToolCall(id="d1", **cancel)),  # type: ignore[arg-type]
            _response(ToolCall(id="d2", **cancel)),  # type: ignore[arg-type]
            _response(text="It can't be cancelled."),
        ]
    )
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx)))

    assert _done(events).status == "completed"
    assert len(fx.cancel_precheck.calls) == 1
    assert fx.suspended == []
    assert _tool_payloads(llm.requests[2])[-1]["error_code"] == REPEATED_CALL


async def test_repeat_streak_survives_suspension_and_resume() -> None:
    fx = Fixture()
    cancel = {"name": "cancel_order", "arguments": {"order_id": 1001}}
    llm = FakeLLMClient([_response(ToolCall(id="d1", **cancel))])  # type: ignore[arg-type]
    loop = AgentLoop(llm, fx.registry, _settings())
    await _collect(loop.run(_turn(fx)))
    stored = json.loads(json.dumps(fx.suspended[0].to_dict()))
    assert stored["repeat"]["streak"] == 1
    llm.enqueue(
        _response(ToolCall(id="d2", **cancel)),  # type: ignore[arg-type]
        _response(ToolCall(id="d3", **cancel)),  # type: ignore[arg-type]
        _response(text="never"),
    )

    events = await _collect(
        loop.resume(AgentState.from_dict(stored), "confirm", _ctx(), suspend=fx.suspend)
    )

    assert len(fx.cancel_order.calls) == 1
    assert len(fx.suspended) == 1  # the repeat never asked for a new confirmation
    assert _done(events).status == "limit_reached"
    assert llm.remaining == 1


# --- AGT-08 across requests ------------------------------------------------------------------


async def test_limit_after_resume_lists_actions_of_the_original_run_trace() -> None:
    fx = Fixture()
    fx.audit_by_trace = {
        "trace-run": [SimpleNamespace(tool="send_email")],
        "trace-resume": [SimpleNamespace(tool="cancel_order")],
    }
    cancel = ToolCall(id="d1", name="cancel_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([_response(cancel)])
    loop = AgentLoop(llm, fx.registry, _settings(agent_max_steps=2), fx.audit_reader)
    await _collect(loop.run(_turn(fx, _ctx("trace-run"))))
    stored = json.loads(json.dumps(fx.suspended[0].to_dict()))
    assert stored["trace_ids"] == ["trace-run"]
    llm.enqueue(_response(_get("c2")), _response(text="never"))

    events = await _collect(
        loop.resume(
            AgentState.from_dict(stored), "confirm", _ctx("trace-resume"), suspend=fx.suspend
        )
    )

    done = _done(events)
    assert done.status == "limit_reached"
    assert done.content == user_message(
        MessageKey.AGENT_LIMIT_REACHED, "en", actions="send_email, cancel_order"
    )
    assert fx.audit_traces == ["trace-run", "trace-resume"]


async def test_trace_ids_are_not_duplicated_across_resumes() -> None:
    fx = Fixture()
    first = ToolCall(id="d1", name="cancel_order", arguments={"order_id": 1001})
    second = ToolCall(id="d2", name="cancel_order", arguments={"order_id": 1002})
    llm = FakeLLMClient([_response(first)])
    loop = AgentLoop(llm, fx.registry, _settings(), fx.audit_reader)
    await _collect(loop.run(_turn(fx, _ctx("trace-a"))))
    llm.enqueue(_response(second))
    state = AgentState.from_dict(json.loads(json.dumps(fx.suspended[-1].to_dict())))
    await _collect(loop.resume(state, "confirm", _ctx("trace-a"), suspend=fx.suspend))
    state = AgentState.from_dict(json.loads(json.dumps(fx.suspended[-1].to_dict())))

    assert state.to_dict()["trace_ids"] == ["trace-a"]


# --- state compatibility -----------------------------------------------------------------------


def _v1_state_with_pending_call() -> dict[str, Any]:
    return {
        "version": 1,
        "system": [{"text": "You are Atlas.", "cache_breakpoint": True}],
        "tools": [],
        "max_tokens": 1024,
        "purpose": "chat",
        "response_schema": None,
        "messages": [
            {"role": "user", "content": "cancel 1001", "tool_calls": [], "tool_call_id": None},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "d1", "name": "cancel_order", "arguments": {"order_id": 1001}}
                ],
                "tool_call_id": None,
            },
        ],
        "steps": 3,
        "usage": {"input_tokens": 30, "output_tokens": 15},
        "refused_calls": [],
        "pending_call": {"id": "d1", "name": "cancel_order", "arguments": {"order_id": 1001}},
        "remaining_calls": [],
        "language": "en",
        "trace_id": "trace-v1",
        "conversation_id": None,
    }


async def test_phase_06_v1_state_still_resumes(
    events_log: list[tuple[str, dict[str, object]]],
) -> None:
    fx = Fixture()
    llm = FakeLLMClient([_response(_get("c1")), _response(text="never")])
    loop = AgentLoop(llm, fx.registry, _settings(agent_max_steps=4), fx.audit_reader)

    events = await _collect(
        loop.resume(
            AgentState.from_dict(_v1_state_with_pending_call()),
            "confirm",
            _ctx(),
            suspend=fx.suspend,
        )
    )

    assert len(fx.cancel_order.calls) == 1
    assert len(llm.requests) == 1  # v1 had used 3 steps: only the 4th is left
    assert fx.get_order.calls == []  # asked by the call that used the last step
    assert _done(events).status == "limit_reached"
    # v1 has no `trace_ids`: its own `trace_id` plus the current request's.
    assert fx.audit_traces == ["trace-v1", "trace-limits"]
    assert _named(events_log, "agent_limit") == [
        {"reason": "step_limit", "steps": 4, "cost_usd": "0", "unknown_cost_calls": 0}
    ]
