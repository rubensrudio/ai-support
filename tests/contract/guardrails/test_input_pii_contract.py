"""Personal data in the question is masked, never blocked (GRD-07, OBS-91, LAC-14, CT-53, CT-54).

A question with a card number (valid Luhn), a CPF, an e-mail and a phone number still gets an
answer, but the database, every request sent to the LLM and every log line carry only the
typed markers (`[CARD]`, `[ID]`, `[EMAIL]`, `[PHONE]`), never the original values.
"""

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.chat.schemas import ChatRequest
from api.chat.service import ChatService

from .conftest import LoginFn, Persona
from .guard_helpers import (
    CARD,
    CPF,
    PHONE_BR,
    PHONE_US,
    RAW_PII,
    RAW_PII_PT,
    VISITOR_EMAIL,
    RoutingFakeLLM,
    conversation_messages,
    customer_messages,
    parse_sse,
    request_dump,
    streamed_tokens,
    terminal,
    text_response,
)

pytestmark = pytest.mark.integration

EN_QUESTION = (
    f"My card {CARD} was charged twice this month. My CPF is {CPF}, my e-mail is "
    f"{VISITOR_EMAIL} and my phone is {PHONE_US}. Can you look into the duplicate charge?"
)
EN_ANSWER = "I can help with the duplicate charge. Please tell me the order number."
PT_QUESTION = (
    f"Meu cartão {CARD} foi cobrado duas vezes, meu CPF é {CPF}, meu e-mail é "
    f"{VISITOR_EMAIL} e meu telefone é {PHONE_BR}. Pode verificar a cobrança duplicada?"
)
PT_ANSWER = "Posso ajudar com a cobrança duplicada. Qual é o número do pedido?"


def _assert_masked(text: str, raw_values: tuple[str, ...], where: str) -> None:
    for raw in raw_values:
        assert raw not in text, f"raw personal data {raw!r} leaked into {where}"


async def test_pii_is_masked_in_db_llm_and_logs_and_still_answered_json(
    client: httpx.AsyncClient,
    login: LoginFn,
    personas: dict[str, Persona],
    llm: RoutingFakeLLM,
    db_session: AsyncSession,
    atlas_logs: list[str],
) -> None:
    llm.script(text_response(EN_ANSWER))
    headers = await login("alex")

    response = await client.post("/chat", json={"message": EN_QUESTION}, headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed", "masking must not block the message"
    assert body["content"] == EN_ANSWER
    assert len(llm.chat.requests) == 1

    stored = await conversation_messages(db_session, uuid.UUID(body["conversation_id"]))
    user_turn = next(m for m in stored if m.role == "user")
    for marker in ("[CARD]", "[ID]", "[EMAIL]", "[PHONE]"):
        assert marker in user_turn.content, f"{marker} missing from the stored message"
    _assert_masked("\n".join(m.content for m in stored), RAW_PII, "messages")

    sent = request_dump(llm.all_requests)
    assert "[CARD]" in sent and "[EMAIL]" in sent
    _assert_masked(sent, RAW_PII, "the LLM requests")

    assert atlas_logs, "the request produced no log line"
    _assert_masked("\n".join(atlas_logs), RAW_PII, "the logs")


async def test_pii_is_masked_on_the_stream_route_pt(
    client: httpx.AsyncClient,
    login: LoginFn,
    personas: dict[str, Persona],
    llm: RoutingFakeLLM,
    db_session: AsyncSession,
    atlas_logs: list[str],
) -> None:
    llm.script(text_response(PT_ANSWER))
    headers = await login("bia")

    response = await client.post("/chat/stream", json={"message": PT_QUESTION}, headers=headers)

    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    name, done = terminal(events)
    assert name == "done"
    assert done["status"] == "completed"
    assert done["language"] == "pt"
    assert " ".join(streamed_tokens(events).split()) == PT_ANSWER

    stored = await customer_messages(db_session, personas["bia"].id)
    user_turn = next(m for m in stored if m.role == "user")
    for marker in ("[CARD]", "[ID]", "[EMAIL]", "[PHONE]"):
        assert marker in user_turn.content, f"{marker} missing from the stored message"
    _assert_masked("\n".join(m.content for m in stored), RAW_PII_PT, "messages")
    _assert_masked(request_dump(llm.all_requests), RAW_PII_PT, "the LLM requests")
    _assert_masked("\n".join(atlas_logs), RAW_PII_PT, "the logs")


async def test_pii_is_masked_by_chat_service(
    service: ChatService,
    personas: dict[str, Persona],
    llm: RoutingFakeLLM,
    db_session: AsyncSession,
) -> None:
    alex = personas["alex"]
    llm.script(text_response(EN_ANSWER))

    result = await service.collect(
        service.handle_message(ChatRequest(message=EN_QUESTION), alex.principal)
    )

    assert result.status == "completed"
    assert result.content == EN_ANSWER
    stored = await customer_messages(db_session, alex.id)
    assert any("[CARD]" in m.content for m in stored if m.role == "user")
    _assert_masked("\n".join(m.content for m in stored), RAW_PII, "messages")
    _assert_masked(request_dump(llm.all_requests), RAW_PII, "the LLM requests")
