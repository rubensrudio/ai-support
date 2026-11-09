"""Customer isolation on `POST /chat` (CHAT-94, TOOL-95, CT-14, CT-16, CT-20, TASK-032).

A conversation belongs to the customer of the session cookie. Another customer's id and an id
that does not exist get the same 404, and an anonymous request never reaches the LLM.
"""

import uuid

import httpx
import pytest
from auth_contract_helpers import LoginFn, cookie_header, fake_answer, without_trace_id
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.chat.models import Conversation, Message
from api.llm.fake import FakeLLMClient

pytestmark = pytest.mark.integration

ALEX_MESSAGE = "Hello, I would like to know the status of my latest order, please."
ALEX_FOLLOW_UP = "Thanks. Could you also tell me when it will be delivered to my home?"
BIA_MESSAGE = "Olá, gostaria de saber o status do meu último pedido, por favor."


async def _message_count(db_session: AsyncSession, conversation_id: uuid.UUID) -> int:
    count = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
    )
    return int(count or 0)


async def test_chat_without_session_is_auth_required_before_the_llm(
    client: httpx.AsyncClient, fake_llm: FakeLLMClient, personas: dict[str, uuid.UUID]
) -> None:
    fake_llm.enqueue(fake_answer("This must never be returned."))

    response = await client.post("/chat", json={"message": ALEX_MESSAGE})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert fake_llm.requests == []


async def test_chat_with_forged_session_is_auth_required_before_the_llm(
    client: httpx.AsyncClient, fake_llm: FakeLLMClient, personas: dict[str, uuid.UUID]
) -> None:
    fake_llm.enqueue(fake_answer("This must never be returned."))

    response = await client.post(
        "/chat", json={"message": ALEX_MESSAGE}, headers=cookie_header("forged-token")
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert fake_llm.requests == []


async def test_other_customers_conversation_is_indistinguishable_from_a_missing_one(
    client: httpx.AsyncClient,
    login: LoginFn,
    fake_llm: FakeLLMClient,
    db_session: AsyncSession,
    personas: dict[str, uuid.UUID],
) -> None:
    alex = await login("alex")
    bia = await login("bia")
    fake_llm.enqueue(fake_answer("Your latest order is on its way."))

    created = await client.post(
        "/chat", json={"message": ALEX_MESSAGE}, headers=cookie_header(alex)
    )
    assert created.status_code == 200, created.text
    alex_conversation = uuid.UUID(created.json()["conversation_id"])
    conversation = await db_session.get(Conversation, alex_conversation)
    assert conversation is not None
    assert conversation.customer_id == personas["alex"]
    alex_messages = await _message_count(db_session, alex_conversation)
    llm_calls = len(fake_llm.requests)

    foreign = await client.post(
        "/chat",
        json={"message": BIA_MESSAGE, "conversation_id": str(alex_conversation)},
        headers=cookie_header(bia),
    )
    missing = await client.post(
        "/chat",
        json={"message": BIA_MESSAGE, "conversation_id": str(uuid.uuid4())},
        headers=cookie_header(bia),
    )

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"
    assert without_trace_id(foreign.json()) == without_trace_id(missing.json())
    assert str(alex_conversation) not in foreign.text
    assert len(fake_llm.requests) == llm_calls
    assert await _message_count(db_session, alex_conversation) == alex_messages


async def test_owner_can_continue_its_own_conversation(
    client: httpx.AsyncClient, login: LoginFn, fake_llm: FakeLLMClient
) -> None:
    alex = await login("alex")
    fake_llm.enqueue(
        fake_answer("Your latest order is on its way."),
        fake_answer("It should arrive tomorrow."),
    )

    first = await client.post("/chat", json={"message": ALEX_MESSAGE}, headers=cookie_header(alex))
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]

    second = await client.post(
        "/chat",
        json={"message": ALEX_FOLLOW_UP, "conversation_id": conversation_id},
        headers=cookie_header(alex),
    )

    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == conversation_id
    assert second.json()["content"] == "It should arrive tomorrow."


async def test_customer_identity_cannot_come_from_the_request_body(
    client: httpx.AsyncClient,
    login: LoginFn,
    fake_llm: FakeLLMClient,
    personas: dict[str, uuid.UUID],
) -> None:
    bia = await login("bia")

    response = await client.post(
        "/chat",
        json={"message": BIA_MESSAGE, "customer_id": str(personas["alex"])},
        headers=cookie_header(bia),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fake_llm.requests == []
