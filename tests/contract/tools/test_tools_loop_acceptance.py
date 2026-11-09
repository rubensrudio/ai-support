"""The agent loop never executes a destructive tool without confirmation (TOOL-05, TOOL-12,
TOOL-13, TOOL-14, AGT-01, AGT-04, CT-45).

The loop runs with a scripted `FakeLLMClient` and the real default registry, so the tools touch
the (rolled-back) database for real.
"""

import json
from typing import Any

import pytest
from conftest import ContextFactory, OrderFactory
from sqlalchemy.ext.asyncio import AsyncSession
from tools_acceptance_helpers import (
    RecordingSuspend,
    audit_count,
    order_state,
    refund_count,
    text_response,
    tool_call_response,
)

from api.agent.events import AgentEvent, DoneEvent, ToolEndEvent
from api.agent.loop import AgentLoop, AgentState, AgentTurn
from api.config import get_settings
from api.llm.client import ChatMessage, LLMRequest, SystemBlock, ToolCall
from api.llm.fake import FakeLLMClient
from api.tools.models import OrderStatus
from api.tools.registry import ToolContext, ToolErrorCode, ToolRegistry

pytestmark = pytest.mark.integration


def _request(registry: ToolRegistry, text: str) -> LLMRequest:
    return LLMRequest(
        system=[SystemBlock(text="You are Atlas, a support agent.")],
        messages=[ChatMessage(role="user", content=text)],
        tools=registry.specs(),
    )


async def _collect(events: Any) -> list[AgentEvent]:
    return [event async for event in events]


def _done(events: list[AgentEvent]) -> DoneEvent:
    assert events, "the loop emitted no event"
    last = events[-1]
    assert isinstance(last, DoneEvent), events
    return last


def _tool_results_sent(llm: FakeLLMClient) -> dict[str, str]:
    """`tool_call_id -> content` of every tool result the model received."""
    results: dict[str, str] = {}
    for request in llm.requests:
        for message in request.messages:
            if message.role == "tool" and message.tool_call_id is not None:
                results[message.tool_call_id] = message.content
    return results


def _roundtrip(state: AgentState) -> AgentState:
    """The state is stored as JSON in `pending_confirmations.agent_state`."""
    return AgentState.from_dict(json.loads(json.dumps(state.to_dict())))


async def _run(
    registry: ToolRegistry, llm: FakeLLMClient, ctx: ToolContext, suspend: RecordingSuspend
) -> list[AgentEvent]:
    loop = AgentLoop(llm, registry, get_settings())
    turn = AgentTurn(request=_request(registry, "please help"), ctx=ctx, suspend=suspend)
    return await _collect(loop.run(turn))


@pytest.mark.parametrize(
    ("tool", "status"),
    [("cancel_order", OrderStatus.PAID), ("issue_refund", OrderStatus.DELIVERED)],
)
async def test_run_never_executes_a_destructive_tool(
    tool: str,
    status: OrderStatus,
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    db_session: AsyncSession,
) -> None:
    order = await make_order("alex", status)
    before = await order_state(db_session, order.id)
    ctx = ctx_for("alex")
    call = ToolCall(id="call-1", name=tool, arguments={"order_id": order.id})
    llm = FakeLLMClient([tool_call_response(call), text_response("should not be reached")])
    suspend = RecordingSuspend()

    events = await _run(registry, llm, ctx, suspend)

    done = _done(events)
    assert done.status == "awaiting_confirmation"
    assert done.confirmation == suspend.prompts[0]
    assert [(c.name, c.arguments) for c, _ in suspend.calls] == [(tool, {"order_id": order.id})]
    assert await order_state(db_session, order.id) == before
    assert await refund_count(db_session, order.id) == 0
    assert await audit_count(db_session, ctx.trace_id, "success") == 0
    assert not any(isinstance(e, ToolEndEvent) and e.ok for e in events)


@pytest.mark.parametrize("tool", ["get_order", "list_orders"])
async def test_read_tools_run_without_confirmation(
    tool: str,
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
) -> None:
    order = await make_order("alex", OrderStatus.PAID)
    arguments: dict[str, Any] = {"order_id": order.id} if tool == "get_order" else {}
    call = ToolCall(id="call-read", name=tool, arguments=arguments)
    llm = FakeLLMClient([tool_call_response(call), text_response("Here is your order.")])
    suspend = RecordingSuspend()

    events = await _run(registry, llm, ctx_for("alex"), suspend)

    assert suspend.calls == []
    assert _done(events).status == "completed"
    ends = [e for e in events if isinstance(e, ToolEndEvent) and e.call_id == "call-read"]
    assert [(e.tool, e.ok) for e in ends] == [(tool, True)]
    assert "call-read" in _tool_results_sent(llm)


async def test_confirmed_cancellation_executes_once_after_resume(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    db_session: AsyncSession,
) -> None:
    order = await make_order("alex", OrderStatus.PAID)
    ctx = ctx_for("alex")
    call = ToolCall(id="call-1", name="cancel_order", arguments={"order_id": order.id})
    llm = FakeLLMClient([tool_call_response(call), text_response("Your order is cancelled.")])
    suspend = RecordingSuspend()
    await _run(registry, llm, ctx, suspend)
    state = _roundtrip(suspend.calls[0][1])

    loop = AgentLoop(llm, registry, get_settings())
    events = await _collect(loop.resume(state, "confirm", ctx, suspend=suspend))

    assert _done(events).status == "completed"
    assert (await order_state(db_session, order.id))[0] == OrderStatus.CANCELLED
    assert await audit_count(db_session, ctx.trace_id, "success") == 1
    assert len(suspend.calls) == 1


async def test_refusal_is_returned_to_the_model_and_a_repeat_is_not_asked_again(
    registry: ToolRegistry,
    ctx_for: ContextFactory,
    make_order: OrderFactory,
    db_session: AsyncSession,
) -> None:
    order = await make_order("alex", OrderStatus.PAID)
    before = await order_state(db_session, order.id)
    ctx = ctx_for("alex")
    first = ToolCall(id="call-1", name="cancel_order", arguments={"order_id": order.id})
    repeat = ToolCall(id="call-2", name="cancel_order", arguments={"order_id": order.id})
    llm = FakeLLMClient(
        [
            tool_call_response(first),  # run -> suspended
            tool_call_response(repeat),  # after the refusal the model insists
            text_response("Okay, I did not cancel it."),
        ]
    )
    suspend = RecordingSuspend()
    await _run(registry, llm, ctx, suspend)
    assert len(suspend.calls) == 1
    state = _roundtrip(suspend.calls[0][1])

    loop = AgentLoop(llm, registry, get_settings())
    events = await _collect(loop.resume(state, "refuse", ctx, suspend=suspend))

    assert _done(events).status == "completed"
    assert len(suspend.calls) == 1  # TOOL-14: no new confirmation for the same call
    sent = _tool_results_sent(llm)
    assert "customer_refused" in sent["call-1"]  # TOOL-13
    assert ToolErrorCode.ACTION_ALREADY_REFUSED in sent["call-2"]  # TOOL-14
    assert await order_state(db_session, order.id) == before
    assert await audit_count(db_session, ctx.trace_id, "success") == 0
    assert llm.remaining == 0
