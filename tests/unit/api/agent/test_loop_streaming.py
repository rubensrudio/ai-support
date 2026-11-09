"""Streaming agent loop (TASK-094, STR-01, STR-02, STR-05, STR-91).

The loop calls the model through `stream_or_complete`: one `token` per `text_delta`, the
`tool_start`/`tool_end` pair per tool, and `done` last, with no event after it.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
    ToolEndEvent,
    ToolStartEvent,
)
from api.agent.loop import AgentLoop, AgentState, AgentTurn
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
from api.llm.fake import FakeLLMClient
from api.tools.registry import ToolContext, ToolDefinition, ToolRegistry, ToolResult

CONVERSATION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c9")


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


async def _get_order(args: Any, ctx: ToolContext) -> ToolResult:
    return ToolResult(ok=True, content={"id": args.order_id, "status": "shipped"})


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="get_order",
            description="get_order description",
            args_model=OrderArgs,
            destructive=False,
            handler=_get_order,
            label_key="STEP_GET_ORDER",
        )
    )
    return registry


async def _suspend(call: ToolCall, state: AgentState) -> ConfirmationPrompt:
    raise AssertionError("no destructive tool in these tests")


def _ctx() -> ToolContext:
    return ToolContext(
        customer_id=uuid.uuid4(),
        customer_email="alex@example.com",
        trace_id="trace-stream",
        session=cast(AsyncSession, FakeSession()),
        language="en",
        now=datetime(2026, 11, 9, tzinfo=UTC),
    )


def _turn(registry: ToolRegistry) -> AgentTurn:
    return AgentTurn(
        request=LLMRequest(
            system=[SystemBlock(text="You are Atlas.")],
            messages=[ChatMessage(role="user", content="where is my order 1001?")],
            tools=registry.specs(),
        ),
        ctx=_ctx(),
        suspend=_suspend,
        conversation_id=CONVERSATION_ID,
    )


def _response(
    text: str = "",
    calls: list[ToolCall] | None = None,
    *,
    model: str = "fake-model",
    provider: str = "fake",
) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls or []),
        usage=Usage(input_tokens=10, output_tokens=5),
        model=model,
        provider=provider,
        stop_reason="tool_use" if calls else "end_turn",
    )


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None).model_copy(update=overrides)


async def _collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in stream]


def _types(events: list[AgentEvent]) -> list[str]:
    return [event.type for event in events]


class CompleteOnlyLLM(FakeLLMClient):
    """A provider without streaming (the subscription CLI adapters): `stream()` raises."""

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError


class IdentifiedLLM(FakeLLMClient):
    """Streams like the fake and names its model and provider, as `InstrumentedLLMClient`."""

    def __init__(self, script: list[LLMResponse], *, model: str, provider: str) -> None:
        super().__init__(list(script))
        self.model = model
        self.provider = provider


async def test_final_answer_streams_one_token_per_delta_then_done() -> None:
    llm = FakeLLMClient([_response("Your order is on its way.")])
    loop = AgentLoop(llm, _registry(), _settings())

    events = await _collect(loop.run(_turn(_registry())))

    tokens = [event for event in events if isinstance(event, TokenEvent)]
    assert [token.text for token in tokens] == ["Your ", "order ", "is ", "on ", "its ", "way."]
    assert _types(events) == ["token"] * 6 + ["done"]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "completed"
    assert done.content == "Your order is on its way."
    assert "".join(token.text for token in tokens) == done.content


async def test_tool_turn_orders_tool_events_then_tokens_and_done_last() -> None:
    call = ToolCall(id="call-1", name="get_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([_response("Let me check.", [call]), _response("Order 1001 was shipped.")])
    loop = AgentLoop(llm, _registry(), _settings())

    events = await _collect(loop.run(_turn(_registry())))

    # The text that precedes a tool call is streamed as it arrives (it cannot be held back
    # without knowing whether a tool call follows); the `done` is always the last event.
    assert _types(events) == ["token"] * 3 + ["tool_start", "tool_end"] + ["token"] * 4 + ["done"]
    start, end = events[3], events[4]
    assert isinstance(start, ToolStartEvent)
    assert start.label_key == "STEP_GET_ORDER"
    assert isinstance(end, ToolEndEvent)
    assert end.ok is True
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.content == "Order 1001 was shipped."
    # The streamed tool call keeps the phase-06 semantics: the result goes back to the model.
    assert [m.role for m in llm.requests[1].messages][-2:] == ["assistant", "tool"]
    assert llm.requests[1].messages[-2].tool_calls == [call]
    assert llm.requests[1].messages[-2].content == "Let me check."


async def test_provider_without_streaming_falls_back_to_one_token_and_its_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(loop_module, "log_event", lambda event, **f: logged.append((event, f)))
    llm = CompleteOnlyLLM([_response("Done.", model="sonnet-unpriced", provider="claude_cli")])
    loop = AgentLoop(llm, _registry(), _settings())

    events = await _collect(loop.run(_turn(_registry())))

    assert events[0] == TokenEvent(text="Done.")
    assert _types(events) == ["token", "done"]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "completed"
    # The fallback response names the subscription provider: unknown cost, not a limit.
    assert ("agent_cost_unknown", {"provider": "claude_cli", "model": "sonnet-unpriced"}) in logged


async def test_streamed_call_is_charged_with_the_client_model() -> None:
    llm = IdentifiedLLM(
        [_response("Hello.", model="ignored")], model="model-without-price", provider="anthropic"
    )
    loop = AgentLoop(llm, _registry(), _settings())

    events = await _collect(loop.run(_turn(_registry())))

    done = events[-1]
    assert isinstance(done, DoneEvent)
    # An unpriced model reaches the cost limit (AS-2), so the streamed identity was used.
    assert done.status == "limit_reached"


class StalledLLM(FakeLLMClient):
    """Streams the first deltas, then waits forever (a slow provider)."""

    def __init__(self) -> None:
        super().__init__([])
        self.waiting = asyncio.Event()

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stalled()

    async def _stalled(self) -> AsyncIterator[LLMStreamEvent]:
        for text in ("One ", "two ", "three "):
            yield LLMStreamEvent(kind="text_delta", text=text)
        self.waiting.set()
        await asyncio.Event().wait()
        yield LLMStreamEvent(kind="stop")  # pragma: no cover - never reached


async def test_cancellation_mid_stream_propagates_without_done() -> None:
    llm = StalledLLM()
    loop = AgentLoop(llm, _registry(), _settings())
    seen: list[AgentEvent] = []

    async def consume() -> None:
        async for event in loop.run(_turn(_registry())):
            seen.append(event)

    task = asyncio.create_task(consume())
    waiting = asyncio.create_task(llm.waiting.wait())
    await asyncio.wait({task, waiting}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    waiting.cancel()
    assert llm.waiting.is_set(), "the stream never reached its third delta"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _types(seen) == ["token", "token", "token"]


async def test_partial_text_is_only_the_call_in_progress_not_an_earlier_tool_preamble() -> None:
    """Round 1: a call that ends with tool calls clears the partial; the next call starts it."""
    call = ToolCall(id="call-1", name="get_order", arguments={"order_id": 1001})
    llm = FakeLLMClient([_response("Let me check.", [call]), _response("Shipped today.")])
    loop = AgentLoop(llm, _registry(), _settings())
    partials: list[tuple[str, str]] = []

    async for event in loop.run(_turn(_registry())):
        partials.append((event.type, loop.partial_text))

    assert partials[:3] == [("token", "Let "), ("token", "Let me "), ("token", "Let me check.")]
    assert partials[3:5] == [("tool_start", ""), ("tool_end", "")]
    assert partials[5:7] == [("token", "Shipped "), ("token", "Shipped today.")]
    assert partials[-1] == ("done", "Shipped today.")


async def test_cost_limit_clears_the_partial_of_a_discarded_answer() -> None:
    """Round 1: the tokens of an answer discarded by the cost limit were already sent; the
    `done` replaces them with the limit message and they are not a partial any more."""
    llm = IdentifiedLLM(
        [_response("Discarded answer.")], model="model-without-price", provider="anthropic"
    )
    loop = AgentLoop(llm, _registry(), _settings())
    partials: list[tuple[str, str]] = []
    events: list[AgentEvent] = []

    async for event in loop.run(_turn(_registry())):
        events.append(event)
        partials.append((event.type, loop.partial_text))

    assert [kind for kind, _ in partials] == ["token", "token", "done"]
    assert partials[1] == ("token", "Discarded answer.")
    assert partials[-1] == ("done", "")
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "limit_reached"
    assert "Discarded answer." not in done.content
