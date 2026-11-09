"""Streaming chat turn and cancellation persistence (TASK-094, STR-01, STR-03, STR-04, STR-05,
STR-07, STR-91, STR-92, LAC-16).

The service stream emits `tool_start, tool_end, token+, citation, done`. When the stream is
cancelled (the task is cancelled while the model streams, or the consumer closes the stream),
the partial answer is stored as a `cancelled` assistant message that stays in the history, and
the tools that already ran keep their effects and audit rows: both are committed.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from chat_helpers import CustomerFactory, principal_of
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import api.chat.service as service_module
from api.agent.events import AgentEvent, CitationEvent, DoneEvent, StartEvent, TokenEvent
from api.agent.models import PendingConfirmation
from api.auth.models import Customer
from api.chat.models import Message
from api.chat.schemas import ChatRequest
from api.chat.service import ChatService
from api.chat.stream_routes import sse_stream
from api.config import RetrievalMode, Settings
from api.llm.client import LLMRequest, LLMResponse, LLMStreamEvent, ToolCall, Usage
from api.llm.fake import FakeLLMClient
from api.messages import Lang
from api.rag.retriever import RetrievedChunk, Retriever
from api.tools.models import AuditLog, EmailOutbox, Order, OrderStatus

pytestmark = pytest.mark.integration

EMAIL_CALL = ToolCall(
    id="call-email",
    name="send_email",
    arguments={"subject": "Your order", "body": "Here are the details of your order."},
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


CHUNK = RetrievedChunk(
    chunk_id=1,
    document_title="Shipping policy",
    doc_key="shipping-policy",
    section="Delivery times",
    page=2,
    language="en",
    content="Orders ship within two business days.",
    score=1.0,
)


class OneChunkRetriever(Retriever):
    def __init__(self) -> None:
        pass

    async def retrieve(
        self,
        query: str,
        language: Lang,
        k: int | None = None,
        mode: RetrievalMode | None = None,
    ) -> list[RetrievedChunk]:
        return [CHUNK]


class StallingLLM(FakeLLMClient):
    """Replays the script; the call after it streams `deltas`, then waits forever."""

    def __init__(self, script: list[LLMResponse], deltas: list[str]) -> None:
        super().__init__(list(script))
        self.deltas = deltas
        self.stalled = asyncio.Event()

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        if self.remaining:
            return super().stream(request)
        self.requests.append(request)
        return self._stall()

    async def _stall(self) -> AsyncIterator[LLMStreamEvent]:
        for text in self.deltas:
            yield LLMStreamEvent(kind="text_delta", text=text)
        self.stalled.set()
        await asyncio.Event().wait()
        yield LLMStreamEvent(kind="stop")  # pragma: no cover - never reached


def _service(
    llm: FakeLLMClient, session: AsyncSession, retriever: Retriever | None = None
) -> ChatService:
    return ChatService(llm, Settings(llm_provider="fake"), session, retriever=retriever)


async def _messages(session: AsyncSession, customer: Customer) -> list[Message]:
    from api.chat.models import Conversation

    statement = (
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.customer_id == customer.id)
        .order_by(Message.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(statement)).scalars())


async def _audit(session: AsyncSession, customer: Customer) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.customer_id == customer.id)
    return list((await session.execute(statement)).scalars())


async def _outbox(session: AsyncSession, customer: Customer) -> list[EmailOutbox]:
    statement = select(EmailOutbox).where(EmailOutbox.customer_id == customer.id)
    return list((await session.execute(statement)).scalars())


async def _cancel_when_stalled(stream: AsyncIterator[AgentEvent], llm: StallingLLM) -> list[str]:
    """Consume `stream` in a task and cancel it once the model stalls mid-answer."""
    seen: list[str] = []

    async def consume() -> None:
        async for event in stream:
            seen.append(event.type)

    task = asyncio.create_task(consume())
    stalled = asyncio.create_task(llm.stalled.wait())
    await asyncio.wait({task, stalled}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    stalled.cancel()
    assert llm.stalled.is_set(), f"the model never stalled; events: {seen}"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return seen


async def test_tool_turn_streams_tool_events_tokens_citation_then_done(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    llm = FakeLLMClient(
        [_response(calls=[EMAIL_CALL]), _response("I emailed you the details [1].")]
    )
    service = _service(llm, db_session, OneChunkRetriever())

    events = [
        event
        async for event in service.handle_message(
            ChatRequest(message="Please email me my order details"), principal_of(customer)
        )
    ]

    types = [event.type for event in events]
    assert types[:3] == ["start", "tool_start", "tool_end"]
    assert types[3:-2] == ["token"] * (len(types) - 5)
    assert len(types) - 5 >= 2
    assert types[-2:] == ["citation", "done"]
    tokens = "".join(event.text for event in events if isinstance(event, TokenEvent))
    assert tokens == "I emailed you the details [1]."
    citation = events[-2]
    assert isinstance(citation, CitationEvent)
    assert (citation.index, citation.document, citation.page) == (1, "Shipping policy", 2)
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.status == "completed"
    assert done.content == tokens
    start = events[0]
    assert isinstance(start, StartEvent)
    assert start.conversation_id == done.conversation_id is not None
    assert start.trace_id == done.trace_id
    stored = await _messages(db_session, customer)
    assert [(m.role, m.status) for m in stored] == [
        ("user", "complete"),
        ("assistant", "complete"),
    ]
    assert stored[-1].id == done.message_id


async def test_cancel_after_three_tokens_keeps_partial_answer_and_tool_audit(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    llm = StallingLLM([_response(calls=[EMAIL_CALL])], deltas=["Your ", "email ", "was "])
    service = _service(llm, db_session)

    seen = await _cancel_when_stalled(
        service.handle_message(
            ChatRequest(message="Please email me my order details"), principal_of(customer)
        ),
        llm,
    )

    assert seen == ["start", "tool_start", "tool_end", "token", "token", "token"]
    # Everything below was committed: rolling the session back does not undo it.
    await db_session.rollback()
    stored = await _messages(db_session, customer)
    assert [(m.role, m.status, m.content) for m in stored] == [
        ("user", "complete", "Please email me my order details"),
        ("assistant", "cancelled", "Your email was "),
    ]
    audit = await _audit(db_session, customer)
    assert [(row.tool, row.outcome) for row in audit] == [("send_email", "success")]
    assert len(await _outbox(db_session, customer)) == 1
    assert stored[-1].trace_id == audit[0].trace_id


async def test_closing_the_stream_after_three_tokens_stores_the_partial(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    """A consumer that stops reading (the SSE route on disconnect) closes the stream."""
    customer = await make_customer()
    llm = FakeLLMClient([_response("One two three four five six.")])
    service = _service(llm, db_session)
    stream = service.handle_message(ChatRequest(message="Count to six"), principal_of(customer))

    tokens: list[str] = []
    async for event in stream:
        if isinstance(event, StartEvent):
            continue
        assert isinstance(event, TokenEvent)
        tokens.append(event.text)
        if len(tokens) == 3:
            break
    await stream.aclose()  # type: ignore[attr-defined]

    await db_session.rollback()
    stored = await _messages(db_session, customer)
    assert [(m.role, m.status, m.content) for m in stored][-1] == (
        "assistant",
        "cancelled",
        "One two three ",
    )


async def test_cancel_before_any_token_keeps_tools_and_stores_no_empty_message(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    llm = StallingLLM([_response(calls=[EMAIL_CALL])], deltas=[])
    service = _service(llm, db_session)

    seen = await _cancel_when_stalled(
        service.handle_message(
            ChatRequest(message="Please email me my order details"), principal_of(customer)
        ),
        llm,
    )

    assert seen == ["start", "tool_start", "tool_end"]
    await db_session.rollback()
    stored = await _messages(db_session, customer)
    assert [(m.role, m.status) for m in stored] == [("user", "complete")]
    assert [row.tool for row in await _audit(db_session, customer)] == ["send_email"]


async def test_closing_after_done_changes_nothing(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    """STR-92: cancelling after `done` leaves the completed answer as it is."""
    customer = await make_customer()
    llm = FakeLLMClient([_response("All done.")])
    service = _service(llm, db_session)
    stream = service.handle_message(ChatRequest(message="Hello there"), principal_of(customer))

    async for event in stream:
        if isinstance(event, DoneEvent):
            break
    await stream.aclose()  # type: ignore[attr-defined]

    stored = await _messages(db_session, customer)
    assert [(m.role, m.status, m.content) for m in stored] == [
        ("user", "complete", "Hello there"),
        ("assistant", "complete", "All done."),
    ]


async def test_cancelled_partial_is_part_of_the_next_turn_history(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    llm = FakeLLMClient([_response("One two three four five six.")])
    service = _service(llm, db_session)
    stream = service.handle_message(ChatRequest(message="Count to six"), principal_of(customer))
    conversation_id: uuid.UUID | None = None
    count = 0
    async for event in stream:
        count += isinstance(event, TokenEvent)
        if count == 3:
            break
    await stream.aclose()  # type: ignore[attr-defined]
    stored = await _messages(db_session, customer)
    conversation_id = stored[0].conversation_id

    llm.enqueue(_response("Four five six."))
    events = [
        event
        async for event in service.handle_message(
            ChatRequest(message="Go on", conversation_id=conversation_id),
            principal_of(customer),
        )
    ]

    assert isinstance(events[-1], DoneEvent)
    history = [(m.role, m.content) for m in llm.requests[-1].messages]
    assert ("assistant", "One two three ") in history


async def test_start_is_the_first_event_once_the_user_message_is_committed(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    """Round 1: `start{conversation_id, trace_id}` comes first, after the user message commit."""
    customer = await make_customer()
    llm = FakeLLMClient([_response("Hello!")])
    service = _service(llm, db_session)
    stream = service.handle_message(ChatRequest(message="Hi there"), principal_of(customer))

    first = await anext(stream)

    assert isinstance(first, StartEvent)
    assert llm.requests == []  # before any model call
    await db_session.rollback()  # committed: a rollback keeps it
    stored = await _messages(db_session, customer)
    assert [(m.role, m.content) for m in stored] == [("user", "Hi there")]
    assert first.conversation_id == stored[0].conversation_id
    rest = [event async for event in stream]
    done = rest[-1]
    assert isinstance(done, DoneEvent)
    assert done.trace_id == first.trace_id
    assert [event.type for event in rest].count("start") == 0


async def test_cancel_stores_only_the_text_of_the_call_in_progress(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    """Round 1: the preamble of an earlier call that asked for tools is not glued to it."""
    customer = await make_customer()
    llm = StallingLLM(
        [_response("Let me send it.", [EMAIL_CALL])], deltas=["Your ", "email ", "is "]
    )
    service = _service(llm, db_session)

    seen = await _cancel_when_stalled(
        service.handle_message(
            ChatRequest(message="Please email me my order details"), principal_of(customer)
        ),
        llm,
    )

    assert seen == ["start"] + ["token"] * 4 + ["tool_start", "tool_end"] + ["token"] * 3
    await db_session.rollback()
    stored = await _messages(db_session, customer)
    assert [(m.role, m.status, m.content) for m in stored][-1] == (
        "assistant",
        "cancelled",
        "Your email is ",
    )


class PricedOutLLM(FakeLLMClient):
    """Streams like the fake, charged as a model without a price: every call hits the limit."""

    model = "model-without-price"
    provider = "anthropic"


async def test_cancel_after_a_cost_limit_does_not_store_the_discarded_answer(
    db_session: AsyncSession, make_customer: CustomerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 1: the cost limit discards a final answer already streamed; a cancellation before
    the limit message is stored must not save the discarded text as the partial."""
    reading = asyncio.Event()

    async def stalled_audit(session: AsyncSession, trace_id: str) -> list[AuditLog]:
        reading.set()
        await asyncio.Event().wait()
        return []  # pragma: no cover - never reached

    monkeypatch.setattr(service_module, "list_successful_actions", stalled_audit)
    customer = await make_customer()
    llm = PricedOutLLM([_response("This answer is discarded.")])
    service = _service(llm, db_session)
    seen: list[str] = []

    async def consume() -> None:
        async for event in service.handle_message(
            ChatRequest(message="Hello there"), principal_of(customer)
        ):
            seen.append(event.type)

    task = asyncio.create_task(consume())
    waiting = asyncio.create_task(reading.wait())
    await asyncio.wait({task, waiting}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    waiting.cancel()
    assert reading.is_set(), f"the limit was never reached; events: {seen}"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen == ["start"] + ["token"] * 4  # the discarded answer was streamed
    await db_session.rollback()
    stored = await _messages(db_session, customer)
    assert [(m.role, m.status) for m in stored] == [("user", "complete")]


# --- round 3: cancelling a confirmation stream ------------------------------------------------


class PendingCancel:
    """A paid order and a pending `cancel_order` confirmation for it (committed)."""

    def __init__(self, customer_id: uuid.UUID, order_id: int, confirmation_id: uuid.UUID) -> None:
        self.customer_id = customer_id
        self.order_id = order_id
        self.confirmation_id = confirmation_id


async def _pending_cancel(
    service: ChatService, llm: FakeLLMClient, session: AsyncSession, customer: Customer
) -> PendingCancel:
    order = Order(
        customer_id=customer.id,
        status=OrderStatus.PAID.value,
        total_cents=4990,
        currency="USD",
        items=[{"sku": "box-coffee", "name": "Coffee box", "qty": 1}],
    )
    session.add(order)
    await session.flush()
    llm.enqueue(
        _response(calls=[ToolCall(id="c1", name="cancel_order", arguments={"order_id": order.id})])
    )
    waiting = await service.collect(
        service.handle_message(
            ChatRequest(message=f"Cancel my order #{order.id}"), principal_of(customer)
        )
    )
    assert waiting.confirmation is not None
    return PendingCancel(customer.id, order.id, waiting.confirmation.id)


async def _confirmation_state(session: AsyncSession, case: PendingCancel) -> tuple[str, str, int]:
    """(confirmation status, order status, audit rows) once the request session ends.

    `session_scope` commits at teardown when the endpoint did not raise (a disconnect does not
    raise out of it), so whatever the stream left uncommitted is committed here, as it would be.
    """
    await session.commit()
    confirmation = (
        await session.execute(
            select(PendingConfirmation.status)
            .where(PendingConfirmation.id == case.confirmation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    order = (
        await session.execute(
            select(Order.status)
            .where(Order.id == case.order_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    audit = (
        await session.execute(select(AuditLog).where(AuditLog.customer_id == case.customer_id))
    ).scalars()
    return str(confirmation), str(order), len(list(audit))


async def _confirm_until(
    service: ChatService, customer: Customer, case: PendingCancel, last: str
) -> list[str]:
    """Read the confirm stream until the event `last`, then close it (a disconnect)."""
    stream = service.resolve_confirmation(case.confirmation_id, "confirm", principal_of(customer))
    seen: list[str] = []
    async for event in stream:
        seen.append(event.type)
        if event.type == last:
            break
    await stream.aclose()  # type: ignore[attr-defined]
    return seen


async def test_closing_the_confirm_stream_right_after_start_leaves_it_pending(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    llm = FakeLLMClient([])
    service = _service(llm, db_session)
    case = await _pending_cancel(service, llm, db_session, customer)

    seen = await _confirm_until(service, customer, case, "start")

    assert seen == ["start"]
    assert await _confirmation_state(db_session, case) == ("pending", "paid", 0)


async def test_closing_the_confirm_stream_at_tool_start_leaves_it_pending(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    llm = FakeLLMClient([])
    service = _service(llm, db_session)
    case = await _pending_cancel(service, llm, db_session, customer)

    seen = await _confirm_until(service, customer, case, "tool_start")

    assert seen == ["start", "tool_start"]
    assert await _confirmation_state(db_session, case) == ("pending", "paid", 0)


async def test_disconnect_right_after_start_of_the_confirm_route_leaves_it_pending(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    """The SSE route's disconnect check closes the stream after `start`."""

    class DisconnectsAfterFirstEvent:
        def __init__(self) -> None:
            self.checks = 0

        async def is_disconnected(self) -> bool:
            self.checks += 1
            return self.checks > 1

    customer = await make_customer()
    llm = FakeLLMClient([])
    service = _service(llm, db_session)
    case = await _pending_cancel(service, llm, db_session, customer)
    events = service.resolve_confirmation(case.confirmation_id, "confirm", principal_of(customer))

    chunks = [
        chunk
        async for chunk in sse_stream(
            DisconnectsAfterFirstEvent(), events, service=service, language="en"
        )
    ]

    assert len(chunks) == 1 and chunks[0].startswith(b"event: start")
    assert await _confirmation_state(db_session, case) == ("pending", "paid", 0)


async def test_cancel_after_the_confirmed_tool_ran_keeps_it_confirmed_with_audit(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    llm = StallingLLM([], deltas=["Your ", "order "])
    service = _service(llm, db_session)
    case = await _pending_cancel(service, llm, db_session, customer)

    seen = await _cancel_when_stalled(
        service.resolve_confirmation(case.confirmation_id, "confirm", principal_of(customer)),
        llm,
    )

    assert seen == ["start", "tool_start", "tool_end", "token", "token"]
    assert await _confirmation_state(db_session, case) == ("confirmed", "cancelled", 1)


async def test_closing_at_tool_end_keeps_the_executed_tool(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    llm = FakeLLMClient([])
    service = _service(llm, db_session)
    case = await _pending_cancel(service, llm, db_session, customer)

    seen = await _confirm_until(service, customer, case, "tool_end")

    assert seen == ["start", "tool_start", "tool_end"]
    assert await _confirmation_state(db_session, case) == ("confirmed", "cancelled", 1)
