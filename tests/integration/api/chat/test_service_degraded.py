"""Degraded answer with a ticket when every provider fails (TASK-121).

RES-06 (both providers fail -> degraded answer + one ticket with the trace), RES-91 (a tool that
already ran in the turn is not run again), RES-92 (every circuit open -> no provider is called),
RES-93 (a failure after the first token ends the stream with an error, no fallback, no ticket).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from chat_helpers import CustomerFactory, principal_of
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.events import AgentEvent, DoneEvent, StartEvent, TokenEvent
from api.auth.models import Customer
from api.chat.models import Message
from api.chat.schemas import ChatRequest
from api.chat.service import ChatService, OfflineTopicClient
from api.config import Settings
from api.errors import AtlasError, ErrorCode
from api.llm.circuit_breaker import BreakerLLMClient, CircuitBreaker
from api.llm.client import (
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    ToolCall,
    Usage,
)
from api.llm.fake import FakeLLMClient
from api.llm.fallback import FallbackLLMClient
from api.messages import MessageKey, user_message
from api.tickets.models import Ticket
from api.tools.models import AuditLog, Order, OrderStatus

pytestmark = pytest.mark.integration

DEGRADED_EN = user_message(MessageKey.DEGRADED_RESPONSE, "en")


def _overloaded() -> LLMError:
    return LLMError("overloaded", recoverable=True, status=529)


def _response(text: str = "", calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls or []),
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="tool_use" if calls else "end_turn",
    )


def _breaker(name: str) -> CircuitBreaker:
    return CircuitBreaker(name, failure_threshold=5, reset_seconds=60.0)


def _chain(
    primary: LLMClient, secondary: LLMClient
) -> tuple[FallbackLLMClient, CircuitBreaker, CircuitBreaker]:
    first, second = _breaker("primary-fake"), _breaker("secondary-fake")
    chain = FallbackLLMClient(
        [
            ("primary", BreakerLLMClient(primary, first), first),
            ("secondary", BreakerLLMClient(secondary, second), second),
        ]
    )
    return chain, first, second


def _service(llm: LLMClient, session: AsyncSession, **settings: object) -> ChatService:
    return ChatService(
        llm,
        Settings(llm_provider="fake", **settings),  # type: ignore[arg-type]
        session,
        guardrail_llm=OfflineTopicClient(),
    )


async def _tickets(session: AsyncSession, customer: Customer) -> list[Ticket]:
    statement = select(Ticket).where(Ticket.customer_id == customer.id)
    return list((await session.execute(statement)).scalars())


async def _messages(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    statement = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(statement)).scalars())


async def _audit(session: AsyncSession, customer: Customer) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.customer_id == customer.id)
    return list((await session.execute(statement)).scalars())


async def _order(session: AsyncSession, customer: Customer) -> Order:
    order = Order(
        customer_id=customer.id,
        status=OrderStatus.PAID.value,
        total_cents=4990,
        currency="USD",
        items=[{"sku": "box-coffee", "name": "Coffee box", "qty": 1}],
    )
    session.add(order)
    await session.flush()
    return order


async def _order_status(session: AsyncSession, order_id: int) -> str:
    statement = (
        select(Order.status).where(Order.id == order_id).execution_options(populate_existing=True)
    )
    return str((await session.execute(statement)).scalar_one())


@pytest.mark.parametrize("tools", [True, False], ids=["agent", "plain"])
async def test_both_providers_failing_answer_degraded_with_one_ticket(
    db_session: AsyncSession, make_customer: CustomerFactory, tools: bool
) -> None:
    customer = await make_customer()
    primary = FakeLLMClient([_overloaded()])
    secondary = FakeLLMClient([_overloaded()])
    llm, _, _ = _chain(primary, secondary)
    service = _service(llm, db_session, agent_tools_enabled=tools)

    response = await service.collect(
        service.handle_message(ChatRequest(message="Where is my order?"), principal_of(customer))
    )

    assert response.status == "degraded"
    assert response.content == DEGRADED_EN
    assert len(primary.requests) == 1
    assert len(secondary.requests) == 1
    tickets = await _tickets(db_session, customer)
    assert len(tickets) == 1
    assert tickets[0].trace_id == response.trace_id
    assert tickets[0].conversation_id == response.conversation_id
    assert tickets[0].reason == "all_providers_failed"
    messages = await _messages(db_session, response.conversation_id)
    assert [(m.role, m.status) for m in messages] == [
        ("user", "complete"),
        ("assistant", "degraded"),
    ]
    assert messages[-1].id == response.message_id
    assert messages[-1].content == DEGRADED_EN


async def test_degraded_answer_follows_the_conversation_language(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer("pt")
    llm, _, _ = _chain(FakeLLMClient([_overloaded()]), FakeLLMClient([_overloaded()]))
    service = _service(llm, db_session)

    response = await service.collect(
        service.handle_message(
            ChatRequest(message="Onde está o meu pedido? Ainda não chegou."),
            principal_of(customer),
        )
    )

    assert response.status == "degraded"
    assert response.content == user_message(MessageKey.DEGRADED_RESPONSE, "pt")


async def test_stream_ends_with_a_degraded_done(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    llm, _, _ = _chain(FakeLLMClient([_overloaded()]), FakeLLMClient([_overloaded()]))
    service = _service(llm, db_session)

    events = [
        event
        async for event in service.handle_message(
            ChatRequest(message="Where is my order?"), principal_of(customer)
        )
    ]

    assert isinstance(events[0], StartEvent)
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "degraded"
    assert done.content == DEGRADED_EN
    assert isinstance(done.message_id, int)
    assert len(await _tickets(db_session, customer)) == 1


async def test_every_circuit_open_answers_degraded_without_calling_a_provider(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    primary, secondary = FakeLLMClient([]), FakeLLMClient([])
    llm, first, second = _chain(primary, secondary)
    for breaker in (first, second):
        for _ in range(breaker.failure_threshold):
            breaker.record_failure()
    assert first.state == "open" and second.state == "open"
    service = _service(llm, db_session)

    response = await service.collect(
        service.handle_message(ChatRequest(message="Where is my order?"), principal_of(customer))
    )

    assert response.status == "degraded"
    assert primary.requests == [] and secondary.requests == []
    assert len(await _tickets(db_session, customer)) == 1


async def test_failure_after_a_confirmed_cancel_does_not_run_the_tool_again(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    order = await _order(db_session, customer)
    call = ToolCall(id="call-cancel", name="cancel_order", arguments={"order_id": order.id})
    primary = FakeLLMClient([_response(calls=[call]), _overloaded()])
    secondary = FakeLLMClient([_overloaded()])
    llm, _, _ = _chain(primary, secondary)
    service = _service(llm, db_session)
    principal = principal_of(customer)

    waiting = await service.collect(
        service.handle_message(ChatRequest(message=f"Cancel my order #{order.id}"), principal)
    )
    assert waiting.status == "awaiting_confirmation"
    assert waiting.confirmation is not None

    done = await service.collect(
        service.resolve_confirmation(waiting.confirmation.id, "confirm", principal)
    )

    assert done.status == "degraded"
    assert done.content == DEGRADED_EN
    assert await _order_status(db_session, order.id) == "cancelled"
    audit = await _audit(db_session, customer)
    assert [(row.tool, row.outcome) for row in audit] == [("cancel_order", "success")]
    tickets = await _tickets(db_session, customer)
    assert len(tickets) == 1
    assert tickets[0].trace_id == done.trace_id
    messages = await _messages(db_session, done.conversation_id)
    assert messages[-1].status == "degraded"
    assert messages[-1].id == done.message_id
    # The tool ran once: no model call after the failure, nothing left to replay.
    assert len(primary.requests) == 2
    assert len(secondary.requests) == 1


class _FailsAfterFirstToken:
    """Streams one token, then fails recoverably (a provider that dies mid-answer)."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError("the chat streams")

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.calls += 1
        yield LLMStreamEvent(kind="text_delta", text="Your order ")
        raise _overloaded()


async def test_failure_after_the_first_token_ends_with_an_error_without_fallback(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    primary = _FailsAfterFirstToken()
    secondary = FakeLLMClient([_response("never used")])
    llm, _, _ = _chain(primary, secondary)
    service = _service(llm, db_session, guardrails_enabled=False)
    events: list[AgentEvent] = []

    with pytest.raises(AtlasError) as info:
        async for event in service.handle_message(
            ChatRequest(message="Where is my order?"), principal_of(customer)
        ):
            events.append(event)

    assert info.value.code is ErrorCode.LLM_UNAVAILABLE
    assert any(isinstance(event, TokenEvent) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert primary.calls == 1
    assert secondary.requests == []
    assert await _tickets(db_session, customer) == []
