"""Chat service driven by the agent loop with confirmations (TASK-077).

TOOL-05 (destructive tools wait for confirmation), TOOL-11 (cancel end to end with audit),
TOOL-13 (refusal goes back to the model), TOOL-96 (a confirmation runs at most once, never
after it expires), TOOL-97 (a new message refuses the pending confirmation).
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from chat_helpers import CustomerFactory, principal_of
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.events import AgentEvent, DoneEvent, ToolEndEvent, ToolStartEvent
from api.agent.models import PendingConfirmation
from api.auth.models import Customer
from api.chat.models import Message
from api.chat.schemas import ChatRequest, ChatResponse
from api.chat.service import ChatService
from api.config import Settings
from api.errors import AtlasError, ErrorCode
from api.llm.client import LLMResponse, ToolCall, Usage
from api.llm.fake import FakeLLMClient
from api.tools.catalog import TOOL_NAMES
from api.tools.models import AuditLog, Order, OrderStatus

pytestmark = pytest.mark.integration


def _response(text: str = "", calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls or []),
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="tool_use" if calls else "end_turn",
    )


def _service(fake: FakeLLMClient, session: AsyncSession, **settings: object) -> ChatService:
    return ChatService(fake, Settings(llm_provider="fake", **settings), session)  # type: ignore[arg-type]


async def _order(
    session: AsyncSession, customer: Customer, status: OrderStatus, **overrides: object
) -> Order:
    values: dict[str, object] = {
        "customer_id": customer.id,
        "status": status.value,
        "total_cents": 4990,
        "currency": "USD",
        "items": [{"sku": "box-coffee", "name": "Coffee box", "qty": 1}],
    }
    values.update(overrides)
    order = Order(**values)
    session.add(order)
    await session.flush()
    return order


async def _order_status(session: AsyncSession, order_id: int) -> str:
    statement = (
        select(Order.status).where(Order.id == order_id).execution_options(populate_existing=True)
    )
    return str((await session.execute(statement)).scalar_one())


async def _confirmation(session: AsyncSession, confirmation_id: uuid.UUID) -> PendingConfirmation:
    statement = (
        select(PendingConfirmation)
        .where(PendingConfirmation.id == confirmation_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one()


async def _audit(session: AsyncSession, customer: Customer) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.customer_id == customer.id)
    return list((await session.execute(statement)).scalars())


async def _messages(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    statement = (
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
    )
    return list((await session.execute(statement)).scalars())


def _cancel_call(order_id: int, call_id: str = "call-cancel") -> ToolCall:
    return ToolCall(id=call_id, name="cancel_order", arguments={"order_id": order_id})


async def _ask_cancel(
    service: ChatService, customer: Customer, order: Order, message: str | None = None
) -> ChatResponse:
    return await service.collect(
        service.handle_message(
            ChatRequest(message=message or f"Please cancel my order #{order.id}"),
            principal_of(customer),
        )
    )


async def test_cancel_waits_for_confirmation_then_cancels_with_audit(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    order = await _order(db_session, customer, OrderStatus.PAID)
    fake = FakeLLMClient(
        [_response(calls=[_cancel_call(order.id)]), _response(f"Order #{order.id} is cancelled.")]
    )
    service = _service(fake, db_session)

    waiting = await _ask_cancel(service, customer, order)

    assert waiting.status == "awaiting_confirmation"
    assert waiting.confirmation is not None
    assert waiting.confirmation.tool == "cancel_order"
    assert waiting.confirmation.target == f"#{order.id}"
    assert waiting.confirmation.amount_cents is None
    assert isinstance(waiting.message_id, int)
    assert waiting.content
    assert await _order_status(db_session, order.id) == "paid"
    assert await _audit(db_session, customer) == []
    pending = await _confirmation(db_session, waiting.confirmation.id)
    assert pending.status == "pending"
    assert pending.customer_id == customer.id
    assert pending.conversation_id == waiting.conversation_id
    assert pending.arguments == {"order_id": order.id}
    assert {spec.name for spec in fake.requests[0].tools} == set(TOOL_NAMES)

    done = await service.collect(
        service.resolve_confirmation(waiting.confirmation.id, "confirm", principal_of(customer))
    )

    assert done.status == "completed"
    assert done.content == f"Order #{order.id} is cancelled."
    assert done.conversation_id == waiting.conversation_id
    assert isinstance(done.message_id, int)
    assert done.message_id > waiting.message_id
    assert await _order_status(db_session, order.id) == "cancelled"
    audit = await _audit(db_session, customer)
    assert [(row.tool, row.outcome, row.arguments) for row in audit] == [
        ("cancel_order", "success", {"order_id": order.id})
    ]
    assert (await _confirmation(db_session, waiting.confirmation.id)).status == "confirmed"
    tool_messages = [m for m in fake.requests[1].messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["call-cancel"]
    assert json.loads(tool_messages[0].content)["ok"] is True
    assert done.conversation_id is not None
    stored = await _messages(db_session, done.conversation_id)
    assert [(m.role, m.id) for m in stored][-1] == ("assistant", done.message_id)


async def test_resume_emits_tool_events_before_done(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    order = await _order(db_session, customer, OrderStatus.PENDING)
    fake = FakeLLMClient([_response(calls=[_cancel_call(order.id)]), _response("Done.")])
    service = _service(fake, db_session)
    waiting = await _ask_cancel(service, customer, order)
    assert waiting.confirmation is not None

    events: list[AgentEvent] = [
        event
        async for event in service.resolve_confirmation(
            waiting.confirmation.id, "confirm", principal_of(customer)
        )
    ]

    assert [type(event) for event in events] == [ToolStartEvent, ToolEndEvent, DoneEvent]
    start, end, done = events
    assert isinstance(start, ToolStartEvent) and start.tool == "cancel_order"
    assert start.label_key == "STEP_CANCEL_ORDER"
    assert isinstance(end, ToolEndEvent) and end.ok is True
    assert isinstance(done, DoneEvent) and isinstance(done.message_id, int)


async def test_read_only_tool_runs_without_confirmation(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    order = await _order(db_session, customer, OrderStatus.SHIPPED)
    call = ToolCall(id="call-get", name="get_order", arguments={"order_id": order.id})
    fake = FakeLLMClient([_response(calls=[call]), _response("It has shipped.")])
    service = _service(fake, db_session)

    events = [
        event
        async for event in service.handle_message(
            ChatRequest(message=f"Where is my order #{order.id}?"), principal_of(customer)
        )
    ]

    assert [type(event) for event in events] == [ToolStartEvent, ToolEndEvent, DoneEvent]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "completed"
    assert done.content == "It has shipped."
    assert isinstance(done.message_id, int)
    result = json.loads(fake.requests[1].messages[-1].content)
    assert result["content"]["status"] == "shipped"


async def test_new_message_refuses_the_pending_confirmation(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    order = await _order(db_session, customer, OrderStatus.PAID)
    fake = FakeLLMClient(
        [_response(calls=[_cancel_call(order.id)]), _response("Sure, what else can I do?")]
    )
    service = _service(fake, db_session)
    waiting = await _ask_cancel(service, customer, order)
    assert waiting.confirmation is not None

    answer = await service.collect(
        service.handle_message(
            ChatRequest(
                message="Actually, what plans do you have?",
                conversation_id=waiting.conversation_id,
            ),
            principal_of(customer),
        )
    )

    assert answer.status == "completed"
    assert (await _confirmation(db_session, waiting.confirmation.id)).status == "refused"
    assert await _order_status(db_session, order.id) == "paid"
    assert await _audit(db_session, customer) == []
    # TOOL-97: the refusal is part of the history the model sees, before the new message.
    sent = fake.requests[1].messages
    assert sent[-1].role == "user"
    assert "Actually, what plans do you have?" in sent[-1].content
    assert any(
        m.role == "assistant" and "cancel_order" in m.content and "refused" in m.content
        for m in sent[:-1]
    )
    with pytest.raises(AtlasError) as error:
        await service.collect(
            service.resolve_confirmation(waiting.confirmation.id, "confirm", principal_of(customer))
        )
    assert error.value.code is ErrorCode.CONFIRMATION_NOT_PENDING
    assert await _order_status(db_session, order.id) == "paid"


async def test_refuse_sends_customer_refused_to_the_model(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    order = await _order(db_session, customer, OrderStatus.PAID)
    fake = FakeLLMClient(
        [_response(calls=[_cancel_call(order.id)]), _response("Ok, I kept your order.")]
    )
    service = _service(fake, db_session)
    waiting = await _ask_cancel(service, customer, order)
    assert waiting.confirmation is not None

    done = await service.collect(
        service.resolve_confirmation(waiting.confirmation.id, "refuse", principal_of(customer))
    )

    assert done.status == "completed"
    assert done.content == "Ok, I kept your order."
    assert await _order_status(db_session, order.id) == "paid"
    assert await _audit(db_session, customer) == []
    assert (await _confirmation(db_session, waiting.confirmation.id)).status == "refused"
    tool_message = fake.requests[1].messages[-1]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "call-cancel"
    assert "customer_refused" in tool_message.content


async def test_refund_prompt_shows_the_amount_of_the_customer_order(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    delivered = datetime.now(UTC) - timedelta(days=2)
    order = await _order(
        db_session,
        customer,
        OrderStatus.DELIVERED,
        total_cents=1250,
        shipped_at=delivered - timedelta(days=1),
        delivered_at=delivered,
    )
    call = ToolCall(id="call-refund", name="issue_refund", arguments={"order_id": order.id})
    fake = FakeLLMClient([_response(calls=[call])])
    service = _service(fake, db_session)

    waiting = await _ask_cancel(service, customer, order, f"Refund my order #{order.id}")

    assert waiting.status == "awaiting_confirmation"
    assert waiting.confirmation is not None
    assert waiting.confirmation.tool == "issue_refund"
    assert waiting.confirmation.target == f"#{order.id}"
    assert waiting.confirmation.amount_cents == 1250
    assert waiting.confirmation.currency == "USD"


async def test_refund_of_another_customer_order_asks_no_confirmation(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    other = await make_customer()
    foreign = await _order(db_session, other, OrderStatus.DELIVERED, total_cents=9999)
    call = ToolCall(id="call-refund", name="issue_refund", arguments={"order_id": foreign.id})
    fake = FakeLLMClient([_response(calls=[call]), _response("That order cannot be refunded.")])
    service = _service(fake, db_session)

    waiting = await _ask_cancel(service, customer, foreign, f"Refund order #{foreign.id}")

    assert waiting.status == "completed"
    assert waiting.confirmation is None
    tool_result = fake.requests[1].messages[-1]
    assert tool_result.role == "tool"
    assert "ORDER_NOT_FOUND" in tool_result.content


async def test_another_customer_cannot_resolve_the_confirmation(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    owner = await make_customer()
    intruder = await make_customer()
    order = await _order(db_session, owner, OrderStatus.PAID)
    fake = FakeLLMClient([_response(calls=[_cancel_call(order.id)])])
    service = _service(fake, db_session)
    waiting = await _ask_cancel(service, owner, order)
    assert waiting.confirmation is not None

    with pytest.raises(AtlasError) as error:
        await service.collect(
            service.resolve_confirmation(waiting.confirmation.id, "confirm", principal_of(intruder))
        )

    assert error.value.code is ErrorCode.CONFIRMATION_NOT_FOUND
    assert (await _confirmation(db_session, waiting.confirmation.id)).status == "pending"
    assert await _order_status(db_session, order.id) == "paid"
    assert fake.remaining == 0
    assert len(fake.requests) == 1


async def test_expired_confirmation_is_marked_expired_and_never_runs(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    order = await _order(db_session, customer, OrderStatus.PAID)
    fake = FakeLLMClient([_response(calls=[_cancel_call(order.id)])])
    service = _service(fake, db_session, confirmation_ttl_minutes=15)
    waiting = await _ask_cancel(service, customer, order)
    assert waiting.confirmation is not None
    await db_session.execute(
        update(PendingConfirmation)
        .where(PendingConfirmation.id == waiting.confirmation.id)
        .values(created_at=datetime.now(UTC) - timedelta(minutes=16))
    )

    with pytest.raises(AtlasError) as error:
        await service.collect(
            service.resolve_confirmation(waiting.confirmation.id, "confirm", principal_of(customer))
        )

    assert error.value.code is ErrorCode.CONFIRMATION_NOT_PENDING
    await db_session.rollback()  # what the request session does after the error
    assert (await _confirmation(db_session, waiting.confirmation.id)).status == "expired"
    assert await _order_status(db_session, order.id) == "paid"


async def test_tools_disabled_keeps_the_plain_chat(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    fake = FakeLLMClient([_response("Hello.")])
    service = _service(fake, db_session, agent_tools_enabled=False)

    response = await service.collect(
        service.handle_message(ChatRequest(message="Hi there"), principal_of(customer))
    )

    assert response.status == "completed"
    assert response.content == "Hello."
    assert fake.requests[0].tools == []


async def test_provider_without_tool_support_uses_the_plain_chat(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    fake = FakeLLMClient([_response("Hello without tools.")])
    service = ChatService(
        fake, Settings(llm_provider="codex_cli", agent_tools_enabled=True), db_session
    )

    response = await service.collect(
        service.handle_message(ChatRequest(message="Hi there"), principal_of(customer))
    )

    assert response.status == "completed"
    assert response.content == "Hello without tools."
    assert fake.requests[0].tools == []
    assert service.tools_enabled is False


async def test_refund_of_an_ineligible_order_asks_no_confirmation(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    delivered = datetime.now(UTC) - timedelta(days=30)
    order = await _order(
        db_session,
        customer,
        OrderStatus.DELIVERED,
        total_cents=1250,
        shipped_at=delivered - timedelta(days=1),
        delivered_at=delivered,
    )
    call = ToolCall(id="call-refund", name="issue_refund", arguments={"order_id": order.id})
    fake = FakeLLMClient([_response(calls=[call]), _response("That order cannot be refunded.")])
    service = _service(fake, db_session)

    waiting = await _ask_cancel(service, customer, order, f"Refund my order #{order.id}")

    assert waiting.status == "completed"
    assert waiting.confirmation is None
    tool_result = fake.requests[1].messages[-1]
    assert tool_result.role == "tool"
    assert "REFUND_NOT_ELIGIBLE" in tool_result.content
