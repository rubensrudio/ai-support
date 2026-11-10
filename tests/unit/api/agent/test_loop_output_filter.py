"""Agent loop with the streaming output filter (TASK-107, DA-22, GRD-04, GRD-08).

With an `output_filter`, the deltas of every model call go through `feed`, so only approved
sentences become `token` events; at the end of each model call `finish()` releases (or blocks)
the trailing text. A block ends the turn with `done{status:'blocked'}` and the standard message,
and no tool of that response runs.
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.events import AgentEvent, ConfirmationPrompt, DoneEvent, TokenEvent
from api.agent.loop import AgentLoop, AgentState, AgentTurn
from api.config import Settings
from api.guardrails.output_guard import OutputContext, OutputGuard
from api.guardrails.stream_filter import StreamOutputFilter
from api.llm.client import ChatMessage, LLMRequest, LLMResponse, SystemBlock, ToolCall, Usage
from api.llm.fake import FakeLLMClient
from api.messages import MessageKey, user_message
from api.tools.registry import ToolContext, ToolDefinition, ToolRegistry, ToolResult

CUSTOMER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OWN_ORDER = 1001
OTHER_ORDER = 2002


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


class Handler:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def __call__(self, args: Any, ctx: ToolContext) -> ToolResult:
        self.calls.append(args.order_id)
        return ToolResult(ok=True, content={"id": args.order_id, "status": "shipped"})


def _registry(handler: Handler) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="get_order",
            description="get_order description",
            args_model=OrderArgs,
            destructive=False,
            handler=handler,
            label_key="STEP_GET_ORDER",
        )
    )
    return registry


async def _suspend(call: ToolCall, state: AgentState) -> ConfirmationPrompt:
    raise AssertionError("no destructive tool in these tests")


def _ctx() -> ToolContext:
    return ToolContext(
        customer_id=CUSTOMER_ID,
        customer_email="alex@example.com",
        trace_id="trace-filter",
        session=cast(AsyncSession, FakeSession()),
        language="en",
        now=datetime(2026, 11, 9, tzinfo=UTC),
    )


def _turn(registry: ToolRegistry) -> AgentTurn:
    return AgentTurn(
        request=LLMRequest(
            system=[SystemBlock(text="You are Atlas.")],
            messages=[ChatMessage(role="user", content="where is my order?")],
            tools=registry.specs(),
        ),
        ctx=_ctx(),
        suspend=_suspend,
    )


def _response(text: str = "", calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls or []),
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="tool_use" if calls else "end_turn",
    )


def _topic(on_topic: bool = True) -> LLMResponse:
    return _response(json.dumps({"on_topic": on_topic, "reason": "support"}))


def _settings() -> Settings:
    return Settings(_env_file=None).model_copy(update={"llm_provider": "fake"})


def _filter(guard_llm: FakeLLMClient) -> StreamOutputFilter:
    ctx = OutputContext(
        customer_id=CUSTOMER_ID,
        own_order_ids={OWN_ORDER},
        own_email="alex@example.com",
        successful_tools=set(),
        question="where is my order?",
        language="en",
    )
    return StreamOutputFilter(OutputGuard(guard_llm, _settings()), ctx)


async def _collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in stream]


def _tokens(events: list[AgentEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, TokenEvent))


async def test_only_approved_sentences_become_tokens_and_done_carries_the_released_text() -> None:
    first = f"Your order #{OWN_ORDER} left our warehouse and is on its way. "
    answer = first + "It should arrive soon."
    llm = FakeLLMClient([_response(answer)])
    guard_llm = FakeLLMClient([_topic()])
    loop = AgentLoop(llm, _registry(Handler()), _settings(), output_filter=_filter(guard_llm))

    events = await _collect(loop.run(_turn(_registry(Handler()))))

    tokens = [event.text for event in events if isinstance(event, TokenEvent)]
    assert tokens == [first, "It should arrive soon."]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "completed"
    assert done.content == answer
    assert loop.partial_text == answer
    assert len(guard_llm.requests) == 1  # topic checked once, on the first sentence


async def test_blocked_sentence_never_streams_and_done_is_blocked_with_standard_message() -> None:
    opening = "Sure, here is what I found about the orders you asked for. "
    answer = opening + f"Order #{OTHER_ORDER} belongs to Bruno. More details follow."
    llm = FakeLLMClient([_response(answer)])
    loop = AgentLoop(
        llm, _registry(Handler()), _settings(), output_filter=_filter(FakeLLMClient([_topic()]))
    )

    events = await _collect(loop.run(_turn(_registry(Handler()))))

    assert _tokens(events) == opening
    assert str(OTHER_ORDER) not in _tokens(events)
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "blocked"
    assert done.content == user_message(MessageKey.GUARDRAIL_BLOCKED, "en")
    assert str(OTHER_ORDER) not in loop.partial_text


async def test_blocked_preamble_ends_the_turn_before_its_tools_run() -> None:
    handler = Handler()
    registry = _registry(handler)
    call = ToolCall(id="call-1", name="get_order", arguments={"order_id": OTHER_ORDER})
    llm = FakeLLMClient(
        [_response(f"Checking order #{OTHER_ORDER}", [call]), _response("Never asked.")]
    )
    loop = AgentLoop(llm, registry, _settings(), output_filter=_filter(FakeLLMClient([_topic()])))

    events = await _collect(loop.run(_turn(registry)))

    assert [event.type for event in events] == ["done"]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "blocked"
    assert handler.calls == []
    assert len(llm.requests) == 1


async def test_preamble_tail_is_released_at_the_tool_boundary() -> None:
    handler = Handler()
    registry = _registry(handler)
    call = ToolCall(id="call-1", name="get_order", arguments={"order_id": OWN_ORDER})
    llm = FakeLLMClient([_response("Let me check", [call]), _response("It was shipped.")])
    loop = AgentLoop(llm, registry, _settings(), output_filter=_filter(FakeLLMClient([_topic()])))

    events = await _collect(loop.run(_turn(registry)))

    assert [event.type for event in events] == [
        "token",
        "tool_start",
        "tool_end",
        "token",
        "done",
    ]
    assert _tokens(events) == "Let me checkIt was shipped."
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "completed"
    assert done.content == "It was shipped."
    assert handler.calls == [OWN_ORDER]


async def test_off_topic_first_sentence_blocks_the_answer() -> None:
    llm = FakeLLMClient([_response("Here is a poem about the sea. Waves roll in.")])
    loop = AgentLoop(
        llm,
        _registry(Handler()),
        _settings(),
        output_filter=_filter(FakeLLMClient([_topic(on_topic=False)])),
    )

    events = await _collect(loop.run(_turn(_registry(Handler()))))

    assert _tokens(events) == ""
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "blocked"


async def test_without_filter_the_loop_streams_every_delta() -> None:
    llm = FakeLLMClient([_response(f"Order #{OTHER_ORDER} is yours.")])
    loop = AgentLoop(llm, _registry(Handler()), _settings())

    events = await _collect(loop.run(_turn(_registry(Handler()))))

    assert _tokens(events) == f"Order #{OTHER_ORDER} is yours."
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "completed"
