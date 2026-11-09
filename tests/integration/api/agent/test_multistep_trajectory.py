"""Multi-step agent trajectory with confirmations (TASK-084, AGT-06, AGT-01, TOOL-05).

"Cancel my last order, refund it to my card and email me the receipt", with both destructive
steps confirmed through `ConfirmationStore` + `AgentLoop.resume`, runs the exact trajectory
`list_orders -> get_order -> check_refund_policy -> cancel_order -> issue_refund -> send_email`
against the real tools, the database and the commerce seed, inside the configured limits.
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.confirmations import ConfirmationStore
from api.agent.events import AgentEvent, ConfirmationPrompt, DoneEvent, ToolEndEvent, ToolStartEvent
from api.agent.loop import AgentLoop, AgentState, AgentTurn
from api.auth.models import Customer
from api.chat.models import Conversation
from api.config import Settings
from api.llm.client import ChatMessage, LLMRequest, LLMResponse, SystemBlock, ToolCall, Usage
from api.llm.fake import FakeLLMClient
from api.seed.commerce import seed_commerce
from api.seed.personas import seed_personas
from api.tools.audit import list_successful_actions
from api.tools.catalog import build_default_registry
from api.tools.models import Order, OrderStatus, Refund
from api.tools.registry import ToolContext

pytestmark = pytest.mark.integration

PASSWORD = "persona-password-for-tests"
TRACE_ID = "trace-agt-06"
EXPECTED_TRAJECTORY = [
    "list_orders",
    "get_order",
    "check_refund_policy",
    "cancel_order",
    "issue_refund",
    "send_email",
]


def _response(*calls: ToolCall, text: str = "") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls),
        usage=Usage(input_tokens=120, output_tokens=40),
        model="fake-model",
        provider="fake",
        stop_reason="tool_use" if calls else "end_turn",
    )


async def _collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in stream]


def _done(events: list[AgentEvent]) -> DoneEvent:
    last = events[-1]
    assert isinstance(last, DoneEvent)
    return last


class Harness:
    def __init__(self, session: AsyncSession, customer: Customer, conversation: Conversation):
        self.session = session
        self.customer = customer
        self.conversation = conversation
        self.store = ConfirmationStore(session, Settings(_env_file=None))
        self.events: list[AgentEvent] = []

    def ctx(self) -> ToolContext:
        return ToolContext(
            customer_id=self.customer.id,
            customer_email=self.customer.email,
            trace_id=TRACE_ID,
            session=self.session,
            language="en",
            now=datetime.now(UTC),
        )

    async def suspend(self, call: ToolCall, state: AgentState) -> ConfirmationPrompt:
        row = await self.store.create(
            conversation_id=self.conversation.id,
            customer_id=self.customer.id,
            tool=call.name,
            arguments=dict(call.arguments),
            agent_state=state.to_dict(),
            trace_id=TRACE_ID,
        )
        return ConfirmationPrompt(id=row.id, tool=call.name, target=str(call.arguments))

    async def audit_reader(self, trace_id: str) -> list[Any]:
        return list(await list_successful_actions(self.session, trace_id))

    async def confirm(self, loop: AgentLoop, done: DoneEvent) -> DoneEvent:
        """Approve the pending confirmation the way the chat service does."""
        assert done.status == "awaiting_confirmation"
        assert done.confirmation is not None
        pending = await self.store.resolve(done.confirmation.id, self.customer.id, "confirm")
        # The state goes through JSONB and back, like in the chat service.
        state = AgentState.from_dict(json.loads(json.dumps(pending.agent_state)))
        events = await _collect(
            loop.resume(
                state,
                "confirm",
                self.ctx(),
                suspend=self.suspend,
                conversation_id=self.conversation.id,
            )
        )
        self.events.extend(events)
        return _done(events)


async def _alex(session: AsyncSession) -> Customer:
    await seed_personas(session, PASSWORD)
    await seed_commerce(session, datetime.now(UTC))
    statement = select(Customer).where(Customer.username == "alex")
    return (await session.scalars(statement)).one()


async def _last_order(session: AsyncSession, customer: Customer) -> Order:
    statement = (
        select(Order)
        .where(Order.customer_id == customer.id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(1)
    )
    return (await session.scalars(statement)).one()


async def test_cancel_refund_and_email_follow_the_exact_trajectory(
    db_session: AsyncSession,
) -> None:
    customer = await _alex(db_session)
    order = await _last_order(db_session, customer)
    assert order.status in (OrderStatus.PENDING.value, OrderStatus.PAID.value)
    conversation = Conversation(customer_id=customer.id)
    db_session.add(conversation)
    await db_session.flush()
    harness = Harness(db_session, customer, conversation)
    target = {"order_id": order.id}
    llm = FakeLLMClient(
        [
            _response(ToolCall(id="t1", name="list_orders", arguments={"limit": 5})),
            _response(ToolCall(id="t2", name="get_order", arguments=target)),
            _response(ToolCall(id="t3", name="check_refund_policy", arguments=target)),
            _response(ToolCall(id="t4", name="cancel_order", arguments=target)),
            _response(ToolCall(id="t5", name="issue_refund", arguments=target)),
            _response(
                ToolCall(
                    id="t6",
                    name="send_email",
                    arguments={
                        "subject": f"Receipt for order #{order.id}",
                        "body": f"Order #{order.id} was cancelled and refunded to your card.",
                    },
                )
            ),
            _response(text=f"Order #{order.id} is cancelled, refunded and the receipt is sent."),
        ]
    )
    registry = build_default_registry()
    loop = AgentLoop(llm, registry, Settings(_env_file=None), harness.audit_reader)
    turn = AgentTurn(
        request=LLMRequest(
            system=[SystemBlock(text="You are Atlas.", cache_breakpoint=True)],
            messages=[
                ChatMessage(
                    role="user",
                    content="Cancel my last order, refund it to my card and email me the receipt.",
                )
            ],
            tools=registry.specs(),
        ),
        ctx=harness.ctx(),
        suspend=harness.suspend,
        conversation_id=conversation.id,
    )

    first = await _collect(loop.run(turn))
    harness.events.extend(first)
    after_cancel = await harness.confirm(loop, _done(first))
    final = await harness.confirm(loop, after_cancel)

    assert final.status == "completed"
    assert final.content == f"Order #{order.id} is cancelled, refunded and the receipt is sent."
    started = [e.tool for e in harness.events if isinstance(e, ToolStartEvent)]
    assert started == EXPECTED_TRAJECTORY
    assert all(e.ok for e in harness.events if isinstance(e, ToolEndEvent))
    assert len(llm.requests) == 7  # within the default limit of 8 steps
    assert llm.remaining == 0

    audited = [row.tool for row in await list_successful_actions(db_session, TRACE_ID)]
    assert audited == ["cancel_order", "issue_refund", "send_email"]
    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED.value
    refunds = (await db_session.scalars(select(Refund).where(Refund.order_id == order.id))).all()
    assert len(refunds) == 1
    assert refunds[0].amount_cents == order.total_cents


async def test_step_limit_in_a_real_trajectory_lists_only_audited_actions(
    db_session: AsyncSession,
) -> None:
    customer = await _alex(db_session)
    order = await _last_order(db_session, customer)
    conversation = Conversation(customer_id=customer.id)
    db_session.add(conversation)
    await db_session.flush()
    harness = Harness(db_session, customer, conversation)
    target = {"order_id": order.id}
    llm = FakeLLMClient(
        [
            _response(ToolCall(id="t1", name="cancel_order", arguments=target)),
            _response(ToolCall(id="t2", name="get_order", arguments=target)),
            _response(ToolCall(id="t3", name="list_orders", arguments={"limit": 3})),
            _response(text="never"),
        ]
    )
    registry = build_default_registry()
    settings = Settings(_env_file=None).model_copy(update={"agent_max_steps": 2})
    loop = AgentLoop(llm, registry, settings, harness.audit_reader)
    turn = AgentTurn(
        request=LLMRequest(
            system=[SystemBlock(text="You are Atlas.", cache_breakpoint=True)],
            messages=[ChatMessage(role="user", content="Cancel my last order.")],
            tools=registry.specs(),
        ),
        ctx=harness.ctx(),
        suspend=harness.suspend,
        conversation_id=conversation.id,
    )

    first = await _collect(loop.run(turn))
    done = await harness.confirm(loop, _done(first))

    assert done.status == "limit_reached"
    assert len(llm.requests) == 2
    assert done.content == "I couldn't complete your request. Actions already taken: cancel_order."
    assert uuid.UUID(str(done.conversation_id)) == conversation.id
