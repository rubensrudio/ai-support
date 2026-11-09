"""Authenticated `POST /chat` with conversation ownership (TASK-032, CHAT-94, TOOL-95)."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from chat_helpers import CustomerFactory
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import SESSION_COOKIE
from api.auth.service import create_session
from api.chat.service import ChatService, get_chat_service
from api.config import Settings
from api.db.engine import get_db_session
from api.llm.client import LLMResponse, Usage
from api.llm.fake import FakeLLMClient
from api.main import create_app

pytestmark = pytest.mark.integration


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


@pytest.fixture
def fake() -> FakeLLMClient:
    return FakeLLMClient([])


@pytest.fixture
async def client(fake: FakeLLMClient, db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """The real `require_customer`; every dependency shares the test transaction."""
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    service = ChatService(fake, Settings(llm_provider="fake"), db_session)
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_chat_service] = lambda: service
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _login(
    client: httpx.AsyncClient, db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    token = await create_session(db_session, customer.id)
    client.cookies.set(SESSION_COOKIE, token)


async def test_without_cookie_is_401_before_any_llm_call(
    client: httpx.AsyncClient, fake: FakeLLMClient
) -> None:
    fake.enqueue(_response("unused"))

    response = await client.post("/chat", json={"message": "Hi there"})

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "AUTH_REQUIRED"
    assert error["trace_id"] == response.headers["x-trace-id"]
    assert fake.requests == []


async def test_unknown_cookie_is_401(client: httpx.AsyncClient, fake: FakeLLMClient) -> None:
    client.cookies.set(SESSION_COOKIE, "not-a-session-token")

    response = await client.post("/chat", json={"message": "Hi there"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert fake.requests == []


async def test_logged_in_customer_gets_answer_in_new_conversation(
    client: httpx.AsyncClient,
    fake: FakeLLMClient,
    db_session: AsyncSession,
    make_customer: CustomerFactory,
) -> None:
    await _login(client, db_session, make_customer)
    fake.enqueue(_response("Hello from Atlas."), _response("Still here."))

    first = await client.post("/chat", json={"message": "Hi there"})
    conversation_id = first.json()["conversation_id"]
    second = await client.post(
        "/chat", json={"message": "Are you there?", "conversation_id": conversation_id}
    )

    assert first.status_code == 200
    assert first.json()["content"] == "Hello from Atlas."
    assert conversation_id is not None
    assert isinstance(first.json()["message_id"], int)
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id
    assert [m.content for m in fake.requests[1].messages] == [
        "Hi there",
        "Hello from Atlas.",
        "Are you there?",
    ]


async def test_missing_and_foreign_conversation_get_identical_404(
    client: httpx.AsyncClient,
    fake: FakeLLMClient,
    db_session: AsyncSession,
    make_customer: CustomerFactory,
) -> None:
    await _login(client, db_session, make_customer)
    fake.enqueue(_response("Hi, owner."))
    owned = await client.post("/chat", json={"message": "Hi"})
    foreign_id = owned.json()["conversation_id"]

    await _login(client, db_session, make_customer)  # another persona, same client
    foreign = await client.post("/chat", json={"message": "Hi", "conversation_id": foreign_id})
    missing = await client.post(
        "/chat", json={"message": "Hi", "conversation_id": str(uuid.uuid4())}
    )

    for response in (foreign, missing):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    assert len(fake.requests) == 1


async def test_customer_id_in_body_is_rejected(
    client: httpx.AsyncClient,
    fake: FakeLLMClient,
    db_session: AsyncSession,
    make_customer: CustomerFactory,
) -> None:
    """The identity comes only from the cookie (TOOL-95): extra body fields are refused."""
    await _login(client, db_session, make_customer)

    response = await client.post("/chat", json={"message": "Hi", "customer_id": str(uuid.uuid4())})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fake.requests == []
