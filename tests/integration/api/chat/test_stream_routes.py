"""SSE chat endpoints (TASK-095, CT-76, STR-01, STR-04, STR-05, STR-90, STR-91).

`require_customer` is overridden with the acting Customer, except in the 401 test, which keeps
the real cookie dependency.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from chat_helpers import CustomerFactory, principal_of
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import CurrentCustomer, require_customer
from api.auth.models import Customer
from api.chat.models import Message
from api.chat.schemas import ChatRequest
from api.chat.service import ChatService, get_chat_service
from api.chat.stream_routes import sse_stream
from api.config import Settings
from api.db.engine import get_db_session
from api.llm.client import LLMError, LLMRequest, LLMResponse, LLMStreamEvent, ToolCall, Usage
from api.llm.fake import FakeLLMClient
from api.main import create_app
from api.tools.models import EmailOutbox, Order, OrderStatus

pytestmark = pytest.mark.integration

SSE_HEADERS = {"cache-control": "no-cache", "x-accel-buffering": "no"}


def _response(text: str = "", calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls or []),
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="tool_use" if calls else "end_turn",
    )


class BrokenStreamLLM(FakeLLMClient):
    """Scripted, but a stream with an empty script fails with an unexpected exception."""

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        if self.remaining:
            return super().stream(request)
        raise RuntimeError("secret provider detail")


@pytest.fixture
def fake() -> BrokenStreamLLM:
    return BrokenStreamLLM([])


@pytest.fixture
async def owner(make_customer: CustomerFactory) -> Customer:
    return await make_customer()


@pytest.fixture
def service(fake: FakeLLMClient, db_session: AsyncSession) -> ChatService:
    return ChatService(fake, Settings(llm_provider="fake"), db_session)


@pytest.fixture
def app(service: ChatService, owner: Customer) -> FastAPI:
    app = create_app()
    app.dependency_overrides[require_customer] = lambda: principal_of(owner)
    app.dependency_overrides[get_chat_service] = lambda: service
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def _events(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body into `(event, data)` pairs."""
    parsed: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        parsed.append((lines["event"], json.loads(lines["data"])))
    return parsed


def _types(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events]


async def _messages(session: AsyncSession, customer_id: uuid.UUID) -> list[Message]:
    from api.chat.models import Conversation

    statement = (
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.customer_id == customer_id)
        .order_by(Message.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(statement)).scalars())


async def test_chat_stream_is_event_stream_with_a_single_final_done(
    client: httpx.AsyncClient, fake: FakeLLMClient
) -> None:
    fake.enqueue(_response("Your plan renews monthly."))

    response = await client.post("/chat/stream", json={"message": "When does my plan renew?"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    for name, value in SSE_HEADERS.items():
        assert response.headers[name] == value
    events = _events(response.text)
    assert _types(events) == ["start"] + ["token"] * 4 + ["done"]
    start = events[0][1]
    assert start["trace_id"] == response.headers["x-trace-id"]
    assert start["conversation_id"] == events[-1][1]["conversation_id"] is not None
    assert "".join(data["text"] for name, data in events if name == "token") == (
        "Your plan renews monthly."
    )
    done = events[-1][1]
    assert done["status"] == "completed"
    assert done["content"] == "Your plan renews monthly."
    assert isinstance(done["message_id"], int)
    assert done["trace_id"] == response.headers["x-trace-id"]


async def test_llm_failure_after_a_tool_is_one_error_event_and_rolls_back(
    client: httpx.AsyncClient,
    fake: FakeLLMClient,
    db_session: AsyncSession,
    owner: Customer,
) -> None:
    email = ToolCall(
        id="call-email", name="send_email", arguments={"subject": "Hi", "body": "Details."}
    )
    fake.enqueue(_response(calls=[email]), LLMError("provider down", recoverable=False))
    owner_id = owner.id  # the route's rollback expires the instances of this session

    response = await client.post("/chat/stream", json={"message": "Email me the details"})

    assert response.status_code == 200
    events = _events(response.text)
    assert _types(events) == ["start", "tool_start", "tool_end", "error"]
    error = events[-1][1]
    assert error["code"] == "LLM_UNAVAILABLE"
    assert error["message"]
    assert error["trace_id"] == response.headers["x-trace-id"]
    assert "provider down" not in response.text
    # CHAT-92: only the user message stays; the tool effect is rolled back with the turn.
    stored = await _messages(db_session, owner_id)
    assert [(m.role, m.status) for m in stored] == [("user", "complete")]
    outbox = select(EmailOutbox).where(EmailOutbox.customer_id == owner_id)
    assert list((await db_session.execute(outbox)).scalars()) == []


async def test_unexpected_exception_is_internal_error_without_done(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/chat/stream", json={"message": "Hello there"})

    assert response.status_code == 200
    events = _events(response.text)
    assert _types(events) == ["start", "error"]
    assert events[1][1]["code"] == "INTERNAL_ERROR"
    assert "secret provider detail" not in response.text
    assert "done" not in _types(events)


async def test_blank_message_is_422_before_the_stream(
    client: httpx.AsyncClient, fake: FakeLLMClient
) -> None:
    response = await client.post("/chat/stream", json={"message": "   "})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fake.requests == []


async def test_missing_body_field_is_422_before_the_stream(client: httpx.AsyncClient) -> None:
    response = await client.post("/chat/stream", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_without_session_is_401_before_opening_the_stream(
    fake: FakeLLMClient, service: ChatService, db_session: AsyncSession
) -> None:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_chat_service] = lambda: service
    fake.enqueue(_response("unused"))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        chat = await http.post("/chat/stream", json={"message": "Hi there"})
        confirm = await http.post(
            "/chat/confirmations/00000000-0000-0000-0000-000000000001/stream",
            json={"decision": "confirm"},
        )

    for response in (chat, confirm):
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/json")
        error = response.json()["error"]
        assert error["code"] == "AUTH_REQUIRED"
        assert error["trace_id"] == response.headers["x-trace-id"]
    assert fake.requests == []


async def test_confirmation_stream_runs_the_tool_and_ends_with_done(
    client: httpx.AsyncClient, fake: FakeLLMClient, db_session: AsyncSession, owner: Customer
) -> None:
    order = Order(
        customer_id=owner.id,
        status=OrderStatus.PAID.value,
        total_cents=4990,
        currency="USD",
        items=[{"sku": "box-coffee", "name": "Coffee box", "qty": 1}],
    )
    db_session.add(order)
    await db_session.flush()
    cancel = ToolCall(id="c1", name="cancel_order", arguments={"order_id": order.id})
    fake.enqueue(_response(calls=[cancel]))

    asked = await client.post("/chat/stream", json={"message": f"Cancel my order #{order.id}"})

    waiting = _events(asked.text)
    assert _types(waiting) == ["start", "done"]
    assert waiting[1][1]["status"] == "awaiting_confirmation"
    confirmation_id = waiting[1][1]["confirmation"]["id"]
    conversation_id = waiting[0][1]["conversation_id"]

    fake.enqueue(_response("Your order is cancelled."))
    response = await client.post(
        f"/chat/confirmations/{confirmation_id}/stream", json={"decision": "confirm"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    assert _types(events) == ["start", "tool_start", "tool_end"] + ["token"] * 4 + ["done"]
    # The start of a confirmation stream carries the confirmation's conversation.
    assert events[0][1]["conversation_id"] == conversation_id
    assert events[0][1]["trace_id"] == response.headers["x-trace-id"]
    assert events[1][1]["label_key"] == "STEP_CANCEL_ORDER"
    assert events[2][1]["ok"] is True
    assert events[-1][1]["status"] == "completed"

    again = await client.post(
        f"/chat/confirmations/{confirmation_id}/stream", json={"decision": "confirm"}
    )
    replay = _events(again.text)
    assert _types(replay) == ["error"]  # resolution fails before the stream's `start`
    assert replay[0][1]["code"] == "CONFIRMATION_NOT_PENDING"


async def test_confirmation_stream_rejects_an_unknown_decision_with_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/chat/confirmations/00000000-0000-0000-0000-000000000001/stream",
        json={"decision": "maybe"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class Disconnecting:
    """`Request` stand-in: connected for the first `after` checks, then disconnected."""

    def __init__(self, after: int) -> None:
        self.after = after
        self.checks = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks > self.after


async def test_disconnect_stops_the_stream_and_stores_the_partial_as_cancelled(
    fake: FakeLLMClient, service: ChatService, db_session: AsyncSession, owner: Customer
) -> None:
    fake.enqueue(_response("One two three four five six."))
    owner_id = owner.id
    principal: CurrentCustomer = principal_of(owner)
    events = service.handle_message(ChatRequest(message="Count to six"), principal)

    chunks = [
        chunk
        async for chunk in sse_stream(
            Disconnecting(after=4), events, service=service, language="en"
        )
    ]

    assert _types(_events(b"".join(chunks).decode())) == ["start"] + ["token"] * 3
    await db_session.rollback()
    stored = await _messages(db_session, owner_id)
    assert [(m.role, m.status, m.content) for m in stored] == [
        ("user", "complete", "Count to six"),
        ("assistant", "cancelled", "One two three "),
    ]
