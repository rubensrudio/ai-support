"""Agent loop (TASK-076, AGT-01, AGT-04, AGT-90, AGT-91, TOOL-05, TOOL-12..14)."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.events import (
    AgentEvent,
    ConfirmationPrompt,
    DoneEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from api.agent.loop import AgentLoop, AgentState, AgentTurn
from api.config import Settings
from api.llm.client import (
    ChatMessage,
    LLMError,
    LLMRequest,
    LLMResponse,
    SystemBlock,
    ToolCall,
    Usage,
)
from api.llm.fake import FakeLLMClient
from api.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolRegistry,
    ToolResult,
)

CONVERSATION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


class OrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(ge=1)


class ListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = 10


class FakeSavepoint:
    def __init__(self, session: "FakeSession") -> None:
        self.session = session

    async def __aenter__(self) -> "FakeSavepoint":
        self.session.events.append("begin")
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        self.session.events.append("rollback" if exc_type is not None else "release")
        return False


class FakeSession:
    """Records savepoints: `begin`, then `release` (no error) or `rollback` (exception)."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def begin_nested(self) -> FakeSavepoint:
        return FakeSavepoint(self)


def _session_events(ctx: ToolContext) -> list[str]:
    return cast(FakeSession, ctx.session).events


class SpyHandler:
    def __init__(self, result: ToolResult | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, ToolContext]] = []
        self.session_events: list[list[str]] = []
        self.result = result or ToolResult(ok=True, content={"done": True})
        self.error = error

    async def __call__(self, args: Any, ctx: ToolContext) -> ToolResult:
        self.calls.append((args, ctx))
        self.session_events.append(list(_session_events(ctx)))
        if self.error is not None:
            raise self.error
        return self.result


class SuspendSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[ToolCall, AgentState]] = []
        self.prompt_id = uuid.uuid4()

    async def __call__(self, call: ToolCall, state: AgentState) -> ConfirmationPrompt:
        self.calls.append((call, state))
        return ConfirmationPrompt(id=self.prompt_id, tool=call.name, target=f"#{call.arguments}")


class Fixture:
    def __init__(self) -> None:
        self.list_orders = SpyHandler(ToolResult(ok=True, content={"orders": [{"id": 1001}]}))
        self.get_order = SpyHandler(ToolResult(ok=True, content={"id": 1001, "status": "paid"}))
        self.cancel_order = SpyHandler(ToolResult(ok=True, content={"status": "cancelled"}))
        self.cancel_precheck = SpyHandler(ToolResult(ok=True))
        self.issue_refund = SpyHandler(ToolResult(ok=True, content={"refund_id": 1}))
        self.refund_precheck = SpyHandler(ToolResult(ok=True))
        self.registry = ToolRegistry()
        self.registry.register(_tool("list_orders", ListArgs, self.list_orders))
        self.registry.register(_tool("get_order", OrderArgs, self.get_order))
        self.registry.register(
            _tool(
                "cancel_order",
                OrderArgs,
                self.cancel_order,
                destructive=True,
                precheck=self.cancel_precheck,
            )
        )
        self.registry.register(
            _tool(
                "issue_refund",
                OrderArgs,
                self.issue_refund,
                destructive=True,
                precheck=self.refund_precheck,
            )
        )
        self.suspend = SuspendSpy()


def _tool(
    name: str,
    args_model: type[BaseModel],
    handler: SpyHandler,
    *,
    destructive: bool = False,
    precheck: SpyHandler | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} description",
        args_model=args_model,
        destructive=destructive,
        handler=handler,
        label_key=f"STEP_{name.upper()}",
        precheck=precheck,
    )


def _settings() -> Settings:
    return Settings(_env_file=None)


def _ctx(customer_id: uuid.UUID | None = None, trace_id: str = "trace-1") -> ToolContext:
    return ToolContext(
        customer_id=customer_id or uuid.uuid4(),
        customer_email="alex@example.com",
        trace_id=trace_id,
        session=cast(AsyncSession, FakeSession()),
        language="en",
        now=datetime(2026, 11, 9, tzinfo=UTC),
    )


def _text(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


def _calls(*calls: ToolCall, text: str = "") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls),
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="tool_use",
    )


def _request(registry: ToolRegistry) -> LLMRequest:
    return LLMRequest(
        system=[SystemBlock(text="You are Atlas.", cache_breakpoint=True)],
        messages=[ChatMessage(role="user", content="cancel my order 1001")],
        tools=registry.specs(),
    )


def _turn(fx: Fixture, ctx: ToolContext | None = None) -> AgentTurn:
    return AgentTurn(
        request=_request(fx.registry),
        ctx=ctx or _ctx(),
        suspend=fx.suspend,
        conversation_id=CONVERSATION_ID,
    )


async def _collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    # Phase 09: the loop streams `token` events (covered by test_loop_streaming.py); these
    # phase-06 tests check the tool and terminal events, so the tokens are left out.
    return [event async for event in stream if not isinstance(event, TokenEvent)]


def _tool_messages(request: LLMRequest) -> list[ChatMessage]:
    return [message for message in request.messages if message.role == "tool"]


def _payload(message: ChatMessage) -> dict[str, Any]:
    payload = json.loads(message.content)
    assert isinstance(payload, dict)
    return payload


def _done(events: list[AgentEvent]) -> DoneEvent:
    last = events[-1]
    assert isinstance(last, DoneEvent)
    return last


# --- AGT-90 / AGT-01 -------------------------------------------------------------------------


async def test_answer_without_tools_ends_in_one_step() -> None:
    fx = Fixture()
    llm = FakeLLMClient([_text("Hello!")])
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx, _ctx(trace_id="trace-a"))))

    assert len(llm.requests) == 1
    assert events == [
        DoneEvent(
            status="completed",
            content="Hello!",
            conversation_id=CONVERSATION_ID,
            trace_id="trace-a",
            language="en",
        )
    ]
    assert llm.requests[0].tools == fx.registry.specs()


async def test_non_destructive_tool_emits_start_end_and_feeds_the_result_back() -> None:
    fx = Fixture()
    call = ToolCall(id="call-1", name="get_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([_calls(call), _text("Order 1001 is paid.")])
    loop = AgentLoop(llm, fx.registry, _settings())
    ctx = _ctx()

    events = await _collect(loop.run(_turn(fx, ctx)))

    assert events[:2] == [
        ToolStartEvent(call_id="call-1", tool="get_order", label_key="STEP_GET_ORDER"),
        ToolEndEvent(call_id="call-1", tool="get_order", ok=True),
    ]
    done = _done(events)
    assert (done.status, done.content) == ("completed", "Order 1001 is paid.")
    assert len(fx.get_order.calls) == 1
    args, used_ctx = fx.get_order.calls[0]
    assert args == OrderArgs(order_id=1001)
    assert used_ctx is ctx

    second = llm.requests[1]
    assistant = second.messages[-2]
    assert assistant.role == "assistant"
    assert assistant.tool_calls == [call]
    (tool_message,) = _tool_messages(second)
    assert tool_message.tool_call_id == "call-1"
    assert _payload(tool_message) == {"ok": True, "content": {"id": 1001, "status": "paid"}}


async def test_several_calls_in_one_step_run_in_order() -> None:
    fx = Fixture()
    first = ToolCall(id="c1", name="list_orders", arguments={})
    second = ToolCall(id="c2", name="get_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([_calls(first, second), _text("done")])
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx)))

    assert [(type(e).__name__, getattr(e, "call_id", None)) for e in events[:-1]] == [
        ("ToolStartEvent", "c1"),
        ("ToolEndEvent", "c1"),
        ("ToolStartEvent", "c2"),
        ("ToolEndEvent", "c2"),
    ]
    assert [m.tool_call_id for m in _tool_messages(llm.requests[1])] == ["c1", "c2"]


# --- AGT-04 / AGT-91 -------------------------------------------------------------------------


async def test_handler_exception_becomes_tool_failed_without_internal_details() -> None:
    fx = Fixture()
    fx.get_order.error = RuntimeError("connection to 10.0.0.5 refused: password=secret")
    call = ToolCall(id="call-1", name="get_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([_calls(call), _text("Sorry, something went wrong.")])
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx)))

    assert events[1] == ToolEndEvent(call_id="call-1", tool="get_order", ok=False)
    assert _done(events).status == "completed"
    (tool_message,) = _tool_messages(llm.requests[1])
    assert _payload(tool_message) == {"ok": False, "error": ToolErrorCode.TOOL_FAILED}
    assert "secret" not in tool_message.content
    assert "10.0.0.5" not in tool_message.content


async def test_failed_tool_result_goes_back_to_the_model() -> None:
    fx = Fixture()
    fx.get_order.result = ToolResult(
        ok=False, error_code=ToolErrorCode.ORDER_NOT_FOUND, content={"order_id": 9}
    )
    call = ToolCall(id="call-1", name="get_order", arguments={"order_id": 9})
    llm = FakeLLMClient([_calls(call), _text("Not found.")])
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx)))

    assert events[1] == ToolEndEvent(call_id="call-1", tool="get_order", ok=False)
    (tool_message,) = _tool_messages(llm.requests[1])
    assert _payload(tool_message) == {
        "ok": False,
        "error": ToolErrorCode.ORDER_NOT_FOUND,
        "content": {"order_id": 9},
    }


async def test_unknown_tool_returns_an_error_result_without_executing_anything() -> None:
    fx = Fixture()
    call = ToolCall(id="call-1", name="delete_account", arguments={})
    llm = FakeLLMClient([_calls(call), _text("I can't do that.")])
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx)))

    assert events[:2] == [
        ToolStartEvent(call_id="call-1", tool="delete_account", label_key="STEP_GENERIC"),
        ToolEndEvent(call_id="call-1", tool="delete_account", ok=False),
    ]
    (tool_message,) = _tool_messages(llm.requests[1])
    assert _payload(tool_message)["error"] == ToolErrorCode.UNKNOWN_TOOL
    assert fx.suspend.calls == []


# --- TOOL-05 / TOOL-12 -----------------------------------------------------------------------


async def test_destructive_tool_suspends_without_calling_the_handler() -> None:
    fx = Fixture()
    call = ToolCall(id="call-1", name="cancel_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([_calls(call)])
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx, _ctx(trace_id="trace-s"))))

    assert fx.cancel_order.calls == []
    assert len(llm.requests) == 1
    assert len(fx.suspend.calls) == 1
    suspended_call, state = fx.suspend.calls[0]
    assert suspended_call == call
    assert events == [
        DoneEvent(
            status="awaiting_confirmation",
            conversation_id=CONVERSATION_ID,
            trace_id="trace-s",
            language="en",
            confirmation=ConfirmationPrompt(
                id=fx.suspend.prompt_id, tool="cancel_order", target="#{'order_id': 1001}"
            ),
        )
    ]
    data = state.to_dict()
    assert data["pending_call"] == {
        "id": "call-1",
        "name": "cancel_order",
        "arguments": {"order_id": 1001},
    }
    assert data["steps"] == 1


async def test_calls_before_the_destructive_one_run_and_later_ones_wait() -> None:
    fx = Fixture()
    before = ToolCall(id="c1", name="get_order", arguments={"order_id": 1001})
    destructive = ToolCall(id="c2", name="cancel_order", arguments={"order_id": 1001})
    after = ToolCall(id="c3", name="list_orders", arguments={})
    llm = FakeLLMClient([_calls(before, destructive, after)])
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx)))

    assert [type(e).__name__ for e in events] == ["ToolStartEvent", "ToolEndEvent", "DoneEvent"]
    assert len(fx.get_order.calls) == 1
    assert fx.list_orders.calls == []
    data = fx.suspend.calls[0][1].to_dict()
    assert [c["id"] for c in data["remaining_calls"]] == ["c3"]


# --- resume (TOOL-05, TOOL-13, TOOL-14) --------------------------------------------------------


async def _suspended(
    fx: Fixture, llm: FakeLLMClient, *calls: ToolCall
) -> tuple[AgentLoop, AgentState]:
    llm.enqueue(_calls(*calls))
    loop = AgentLoop(llm, fx.registry, _settings())
    await _collect(loop.run(_turn(fx)))
    state = fx.suspend.calls[-1][1]
    # The chat service stores the state as JSONB and reads it back on the next request.
    return loop, AgentState.from_dict(json.loads(json.dumps(state.to_dict())))


async def test_resume_confirm_executes_with_the_current_session_and_completes() -> None:
    fx = Fixture()
    call = ToolCall(id="call-1", name="cancel_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([])
    loop, state = await _suspended(fx, llm, call)
    llm.enqueue(_text("Order 1001 was cancelled."))
    confirm_ctx = _ctx(trace_id="trace-confirm")

    events = await _collect(
        loop.resume(
            state, "confirm", confirm_ctx, suspend=fx.suspend, conversation_id=CONVERSATION_ID
        )
    )

    assert events == [
        ToolStartEvent(call_id="call-1", tool="cancel_order", label_key="STEP_CANCEL_ORDER"),
        ToolEndEvent(call_id="call-1", tool="cancel_order", ok=True),
        DoneEvent(
            status="completed",
            content="Order 1001 was cancelled.",
            conversation_id=CONVERSATION_ID,
            trace_id="trace-confirm",
            language="en",
        ),
    ]
    assert len(fx.cancel_order.calls) == 1
    args, used_ctx = fx.cancel_order.calls[0]
    assert args == OrderArgs(order_id=1001)
    assert used_ctx is confirm_ctx
    resumed_request = llm.requests[-1]
    assert resumed_request.messages[0] == ChatMessage(role="user", content="cancel my order 1001")
    assert resumed_request.system == [SystemBlock(text="You are Atlas.", cache_breakpoint=True)]
    assert resumed_request.tools == fx.registry.specs()
    (tool_message,) = _tool_messages(resumed_request)
    assert _payload(tool_message) == {"ok": True, "content": {"status": "cancelled"}}


async def test_resume_refuse_sends_customer_refused_and_repeat_is_already_refused() -> None:
    fx = Fixture()
    call = ToolCall(id="call-1", name="cancel_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([])
    loop, state = await _suspended(fx, llm, call)
    repeat = ToolCall(id="call-2", name="cancel_order", arguments={"order_id": 1001})
    llm.enqueue(_calls(repeat), _text("Okay, I won't cancel it."))

    events = await _collect(
        loop.resume(state, "refuse", _ctx(), suspend=fx.suspend, conversation_id=CONVERSATION_ID)
    )

    assert fx.cancel_order.calls == []
    assert len(fx.suspend.calls) == 1  # only the original suspension, no new confirmation
    assert len(fx.cancel_precheck.calls) == 1  # TOOL-14 answers before any precheck
    assert events == [
        DoneEvent(
            status="completed",
            content="Okay, I won't cancel it.",
            conversation_id=CONVERSATION_ID,
            trace_id="trace-1",
            language="en",
        )
    ]
    after_refusal = _tool_messages(llm.requests[1])
    assert after_refusal[-1].tool_call_id == "call-1"
    assert _payload(after_refusal[-1]) == {"ok": False, "error": "customer_refused"}
    after_repeat = _tool_messages(llm.requests[2])
    assert after_repeat[-1].tool_call_id == "call-2"
    assert _payload(after_repeat[-1]) == {
        "ok": False,
        "error": ToolErrorCode.ACTION_ALREADY_REFUSED,
    }


async def test_refused_call_with_other_arguments_still_asks_for_confirmation() -> None:
    fx = Fixture()
    call = ToolCall(id="call-1", name="cancel_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([])
    loop, state = await _suspended(fx, llm, call)
    other = ToolCall(id="call-2", name="cancel_order", arguments={"order_id": 1002})
    llm.enqueue(_calls(other))

    events = await _collect(loop.resume(state, "refuse", _ctx(), suspend=fx.suspend))

    assert _done(events).status == "awaiting_confirmation"
    assert len(fx.suspend.calls) == 2
    data = fx.suspend.calls[-1][1].to_dict()
    assert data["refused_calls"] == [["cancel_order", {"order_id": 1001}]]


async def test_resume_processes_the_remaining_calls_of_the_suspended_step() -> None:
    fx = Fixture()
    destructive = ToolCall(id="c1", name="cancel_order", arguments={"order_id": 1001})
    after = ToolCall(id="c2", name="list_orders", arguments={})
    llm = FakeLLMClient([])
    loop, state = await _suspended(fx, llm, destructive, after)
    llm.enqueue(_text("All set."))

    events = await _collect(loop.resume(state, "confirm", _ctx(), suspend=fx.suspend))

    assert [getattr(e, "call_id", None) for e in events] == ["c1", "c1", "c2", "c2", None]
    assert len(fx.list_orders.calls) == 1
    assert [m.tool_call_id for m in _tool_messages(llm.requests[-1])] == ["c1", "c2"]


async def test_resume_without_a_pending_call_is_rejected() -> None:
    fx = Fixture()
    loop = AgentLoop(FakeLLMClient([]), fx.registry, _settings())
    state = AgentState.from_dict(_minimal_state())

    with pytest.raises(ValueError, match="pending"):
        await _collect(loop.resume(state, "confirm", _ctx(), suspend=fx.suspend))


def _minimal_state() -> dict[str, Any]:
    return {
        "version": 1,
        "system": [],
        "tools": [],
        "max_tokens": 1024,
        "purpose": "chat",
        "response_schema": None,
        "messages": [],
        "steps": 0,
        "usage": {},
        "refused_calls": [],
        "pending_call": None,
        "remaining_calls": [],
        "language": "en",
        "trace_id": "t",
        "conversation_id": None,
    }


# --- AgentState ------------------------------------------------------------------------------


async def test_agent_state_round_trips_through_json() -> None:
    fx = Fixture()
    calls = (
        ToolCall(id="c1", name="get_order", arguments={"order_id": 1001}),
        ToolCall(id="c2", name="cancel_order", arguments={"order_id": 1001}),
        ToolCall(id="c3", name="list_orders", arguments={"limit": 3}),
    )
    llm = FakeLLMClient([_calls(*calls, text="Let me check.")])
    loop = AgentLoop(llm, fx.registry, _settings())
    await _collect(loop.run(_turn(fx, _ctx(trace_id="trace-j"))))
    state = fx.suspend.calls[0][1]

    data = state.to_dict()
    restored = AgentState.from_dict(json.loads(json.dumps(data)))

    assert restored.to_dict() == data
    assert restored == state
    assert data["steps"] == 1
    assert data["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert data["language"] == "en"
    assert data["trace_id"] == "trace-j"
    assert data["conversation_id"] == str(CONVERSATION_ID)
    assert data["system"] == [{"text": "You are Atlas.", "cache_breakpoint": True}]
    assert [tool["name"] for tool in data["tools"]] == fx.registry.names()
    assert [m["role"] for m in data["messages"]] == ["user", "assistant", "tool"]
    assert data["messages"][1]["content"] == "Let me check."
    assert [c["id"] for c in data["messages"][1]["tool_calls"]] == ["c1", "c2", "c3"]
    assert data["messages"][2]["tool_call_id"] == "c1"
    assert data["refused_calls"] == []
    assert data["pending_call"]["id"] == "c2"
    assert [c["id"] for c in data["remaining_calls"]] == ["c3"]


def test_agent_state_to_dict_is_a_copy() -> None:
    state = AgentState.from_dict(_minimal_state())

    state.to_dict()["messages"].append({"role": "user"})

    assert state.to_dict()["messages"] == []


# --- safety cap ------------------------------------------------------------------------------


async def test_loop_stops_after_max_steps_model_calls() -> None:
    fx = Fixture()
    max_steps = _settings().agent_max_steps
    script: list[LLMResponse | LLMError] = [
        _calls(ToolCall(id=f"c{i}", name="list_orders", arguments={"limit": i + 1}))
        for i in range(max_steps + 2)
    ]
    llm = FakeLLMClient(script)
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx)))

    assert max_steps == 8
    assert len(llm.requests) == max_steps
    # Phase 07 (AGT-02): the tools asked by the call that used the last step never run.
    assert len(fx.list_orders.calls) == max_steps - 1
    done = _done(events)
    assert done.status == "limit_reached"
    assert done.content
    assert done.conversation_id == CONVERSATION_ID


async def test_steps_count_across_suspension_and_resume() -> None:
    fx = Fixture()
    max_steps = _settings().agent_max_steps
    llm = FakeLLMClient([])
    for i in range(max_steps - 2):
        llm.enqueue(_calls(ToolCall(id=f"l{i}", name="list_orders", arguments={"limit": i + 1})))
    loop, state = await _suspended(
        fx, llm, ToolCall(id="d", name="cancel_order", arguments={"order_id": 1001})
    )
    llm.enqueue(
        _calls(ToolCall(id="x", name="list_orders", arguments={})),
        _calls(ToolCall(id="y", name="list_orders", arguments={"limit": 50})),
    )

    events = await _collect(loop.resume(state, "confirm", _ctx(), suspend=fx.suspend))

    assert state.to_dict()["steps"] == max_steps - 1
    assert len(fx.cancel_order.calls) == 1
    # The resumed call uses the last step: its tool never runs and no further call is made.
    assert len(fx.list_orders.calls) == max_steps - 2
    assert _done(events).status == "limit_reached"
    assert llm.remaining == 1


# --- precheck before the confirmation card (round 1) -----------------------------------------


@pytest.mark.parametrize(
    ("tool", "error_code"),
    [
        ("cancel_order", ToolErrorCode.ORDER_NOT_FOUND),  # another customer's order
        ("cancel_order", ToolErrorCode.ORDER_NOT_CANCELLABLE),
        ("issue_refund", ToolErrorCode.REFUND_NOT_ELIGIBLE),
    ],
)
async def test_failed_precheck_goes_back_to_the_model_without_a_card(
    tool: str, error_code: str
) -> None:
    fx = Fixture()
    precheck = fx.cancel_precheck if tool == "cancel_order" else fx.refund_precheck
    handler = fx.cancel_order if tool == "cancel_order" else fx.issue_refund
    precheck.result = ToolResult(ok=False, error_code=error_code, content={"order_id": 1001})
    call = ToolCall(id="call-1", name=tool, arguments={"order_id": 1001})
    llm = FakeLLMClient([_calls(call), _text("That can't be done.")])
    loop = AgentLoop(llm, fx.registry, _settings())

    events = await _collect(loop.run(_turn(fx)))

    assert events[:2] == [
        ToolStartEvent(call_id="call-1", tool=tool, label_key=f"STEP_{tool.upper()}"),
        ToolEndEvent(call_id="call-1", tool=tool, ok=False),
    ]
    done = _done(events)
    assert (done.status, done.confirmation) == ("completed", None)
    assert fx.suspend.calls == []
    assert handler.calls == []
    assert len(precheck.calls) == 1
    (tool_message,) = _tool_messages(llm.requests[1])
    assert tool_message.tool_call_id == "call-1"
    assert _payload(tool_message) == {
        "ok": False,
        "error": error_code,
        "content": {"order_id": 1001},
    }


async def test_passing_precheck_suspends_and_runs_inside_a_savepoint() -> None:
    fx = Fixture()
    call = ToolCall(id="call-1", name="issue_refund", arguments={"order_id": 1001})
    llm = FakeLLMClient([_calls(call)])
    loop = AgentLoop(llm, fx.registry, _settings())
    ctx = _ctx()

    events = await _collect(loop.run(_turn(fx, ctx)))

    assert [type(e).__name__ for e in events] == ["DoneEvent"]
    assert _done(events).status == "awaiting_confirmation"
    assert len(fx.suspend.calls) == 1
    assert fx.issue_refund.calls == []
    assert fx.refund_precheck.session_events == [["begin"]]
    assert _session_events(ctx) == ["begin", "release"]


async def test_precheck_exception_becomes_tool_failed_without_a_card() -> None:
    fx = Fixture()
    fx.cancel_precheck.error = RuntimeError("db exploded: password=secret")
    call = ToolCall(id="call-1", name="cancel_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([_calls(call), _text("Something went wrong.")])
    loop = AgentLoop(llm, fx.registry, _settings())
    ctx = _ctx()

    events = await _collect(loop.run(_turn(fx, ctx)))

    assert events[:2] == [
        ToolStartEvent(call_id="call-1", tool="cancel_order", label_key="STEP_CANCEL_ORDER"),
        ToolEndEvent(call_id="call-1", tool="cancel_order", ok=False),
    ]
    assert _done(events).status == "completed"
    assert fx.suspend.calls == []
    assert fx.cancel_order.calls == []
    assert _session_events(ctx) == ["begin", "rollback"]
    (tool_message,) = _tool_messages(llm.requests[1])
    assert _payload(tool_message) == {"ok": False, "error": ToolErrorCode.TOOL_FAILED}
    assert "secret" not in tool_message.content


async def test_handler_exception_rolls_back_its_savepoint_and_the_loop_goes_on() -> None:
    fx = Fixture()
    fx.get_order.error = RuntimeError("boom")
    failing = ToolCall(id="c1", name="get_order", arguments={"order_id": 1001})
    working = ToolCall(id="c2", name="list_orders", arguments={})
    llm = FakeLLMClient([_calls(failing, working), _text("Here is what I found.")])
    loop = AgentLoop(llm, fx.registry, _settings())
    ctx = _ctx()

    events = await _collect(loop.run(_turn(fx, ctx)))

    assert fx.get_order.session_events == [["begin"]]
    assert fx.list_orders.session_events == [["begin", "rollback", "begin"]]
    assert _session_events(ctx) == ["begin", "rollback", "begin", "release"]
    assert [e.ok for e in events if isinstance(e, ToolEndEvent)] == [False, True]
    assert _done(events).content == "Here is what I found."


async def test_resume_confirm_runs_the_handler_in_a_savepoint_without_a_new_precheck() -> None:
    fx = Fixture()
    call = ToolCall(id="call-1", name="cancel_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([])
    loop, state = await _suspended(fx, llm, call)
    llm.enqueue(_text("Cancelled."))
    confirm_ctx = _ctx()

    events = await _collect(loop.resume(state, "confirm", confirm_ctx, suspend=fx.suspend))

    assert _done(events).status == "completed"
    assert len(fx.cancel_precheck.calls) == 1  # only before the card
    assert fx.cancel_order.session_events == [["begin"]]
    assert _session_events(confirm_ctx) == ["begin", "release"]
