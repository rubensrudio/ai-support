"""Output guardrail on the final answer (GRD-04, GRD-08, GRD-91, DA-22, CT-55, CT-75).

An answer that carries another customer's e-mail or order number, or that promises a cancel or a
refund the audit of the request does not show as successful, is replaced by the standard block
message with `status='blocked'`, in the JSON route and in the stream. In the stream no `token`
with the rejected text is sent before `done` (only approved sentences are released). Answers
with the customer's own data, or with an action the audit confirms, pass unchanged.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.chat.schemas import ChatRequest
from api.chat.service import ChatService
from api.llm.client import ToolCall
from api.messages import Lang
from api.tools.models import OrderStatus

from .conftest import LoginFn, OrderFactory, Persona
from .guard_helpers import (
    RoutingFakeLLM,
    audit_rows,
    blocked_message,
    customer_messages,
    order_status,
    parse_sse,
    streamed_tokens,
    terminal,
    text_response,
    tool_response,
)

pytestmark = pytest.mark.integration


async def _ask_json(
    client: httpx.AsyncClient, headers: dict[str, str], message: str
) -> dict[str, Any]:
    response = await client.post("/chat", json={"message": message}, headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _ask_stream(
    client: httpx.AsyncClient, headers: dict[str, str], message: str
) -> list[tuple[str, dict[str, Any]]]:
    response = await client.post("/chat/stream", json={"message": message}, headers=headers)
    assert response.status_code == 200, response.text
    return parse_sse(response.text)


def _assert_blocked_stream(
    events: list[tuple[str, dict[str, Any]]], lang: Lang, *, rejected: str
) -> None:
    name, done = terminal(events)
    assert name == "done", events
    assert done["status"] == "blocked"
    assert done["content"] == blocked_message(lang)
    tokens = streamed_tokens(events)
    assert rejected not in tokens, f"rejected text {rejected!r} was streamed before done"


# --- GRD-04: another customer's data ----------------------------------------------------------


@pytest.mark.parametrize("route", ["json", "stream"])
async def test_answer_with_other_customer_email_is_replaced(
    client: httpx.AsyncClient,
    login: LoginFn,
    personas: dict[str, Persona],
    llm: RoutingFakeLLM,
    db_session: AsyncSession,
    route: str,
) -> None:
    bia_email = personas["bia"].email
    llm.script(text_response(f"Thanks for waiting. The account e-mail is {bia_email}."))
    headers = await login("alex")
    question = "Which e-mail address do you have on my account?"

    if route == "json":
        body = await _ask_json(client, headers, question)
        assert body["status"] == "blocked"
        assert body["content"] == blocked_message("en")
    else:
        _assert_blocked_stream(await _ask_stream(client, headers, question), "en", rejected="@")

    stored = await customer_messages(db_session, personas["alex"].id)
    assert all(bia_email not in row.content for row in stored)


@pytest.mark.parametrize("route", ["json", "stream"])
async def test_answer_with_other_customer_order_is_replaced(
    client: httpx.AsyncClient,
    login: LoginFn,
    personas: dict[str, Persona],
    make_order: OrderFactory,
    llm: RoutingFakeLLM,
    db_session: AsyncSession,
    route: str,
) -> None:
    foreign = await make_order(personas["bia"], OrderStatus.SHIPPED)
    llm.script(text_response(f"Your order #{foreign.id} has shipped and arrives tomorrow."))
    headers = await login("alex")
    question = "Where is my latest order?"

    if route == "json":
        body = await _ask_json(client, headers, question)
        assert body["status"] == "blocked"
        assert body["content"] == blocked_message("en")
    else:
        events = await _ask_stream(client, headers, question)
        _assert_blocked_stream(events, "en", rejected=f"#{foreign.id}")

    stored = await customer_messages(db_session, personas["alex"].id)
    assert all(f"#{foreign.id}" not in row.content for row in stored)


async def test_answer_with_own_email_and_order_passes(
    client: httpx.AsyncClient,
    login: LoginFn,
    personas: dict[str, Persona],
    make_order: OrderFactory,
    llm: RoutingFakeLLM,
) -> None:
    alex = personas["alex"]
    own = await make_order(alex, OrderStatus.SHIPPED)
    answer = f"Your order #{own.id} has shipped. Tracking updates go to {alex.email}."
    llm.script(text_response(answer))
    headers = await login("alex")

    body = await _ask_json(client, headers, "Where is my latest order?")

    assert body["status"] == "completed"
    assert body["content"] == answer


# --- GRD-08: promise without the audited tool -------------------------------------------------


@pytest.mark.parametrize("route", ["json", "stream"])
async def test_cancel_promise_without_tool_is_replaced(
    client: httpx.AsyncClient,
    login: LoginFn,
    personas: dict[str, Persona],
    make_order: OrderFactory,
    llm: RoutingFakeLLM,
    db_session: AsyncSession,
    route: str,
) -> None:
    alex = personas["alex"]
    own = await make_order(alex, OrderStatus.PAID)
    llm.script(text_response(f"I have cancelled your order #{own.id}. Anything else?"))
    headers = await login("alex")
    question = f"Please cancel my order #{own.id}."

    if route == "json":
        body = await _ask_json(client, headers, question)
        assert body["status"] == "blocked"
        assert body["content"] == blocked_message("en")
    else:
        events = await _ask_stream(client, headers, question)
        _assert_blocked_stream(events, "en", rejected="cancelled")

    assert await order_status(db_session, own.id) == "paid"
    assert [row for row in await audit_rows(db_session, alex.id) if row.outcome == "success"] == []


@pytest.mark.parametrize("route", ["json", "stream"])
async def test_refund_promise_without_tool_is_replaced_pt(
    client: httpx.AsyncClient,
    login: LoginFn,
    personas: dict[str, Persona],
    make_order: OrderFactory,
    llm: RoutingFakeLLM,
    route: str,
) -> None:
    own = await make_order(personas["bia"], OrderStatus.DELIVERED)
    llm.script(text_response("Seu reembolso foi emitido e cai em até 5 dias úteis."))
    headers = await login("bia")
    question = f"Quero o reembolso do pedido #{own.id}, a caixa chegou danificada."

    if route == "json":
        body = await _ask_json(client, headers, question)
        assert body["status"] == "blocked"
        assert body["content"] == blocked_message("pt")
    else:
        _assert_blocked_stream(
            await _ask_stream(client, headers, question), "pt", rejected="emitido"
        )


async def test_refund_promise_is_replaced_by_chat_service(
    service: ChatService,
    personas: dict[str, Persona],
    llm: RoutingFakeLLM,
) -> None:
    llm.script(text_response("I have issued a full refund to your card."))

    result = await service.collect(
        service.handle_message(
            ChatRequest(message="I want a refund for my last box."), personas["alex"].principal
        )
    )

    assert result.status == "blocked"
    assert result.content == blocked_message("en")


async def test_cancel_promise_after_audited_cancel_passes(
    service: ChatService,
    personas: dict[str, Persona],
    make_order: OrderFactory,
    llm: RoutingFakeLLM,
    db_session: AsyncSession,
) -> None:
    """Control for GRD-08: the same promise passes once `cancel_order` succeeded."""
    alex = personas["alex"]
    own = await make_order(alex, OrderStatus.PAID)
    answer = f"I have cancelled your order #{own.id}."
    call = ToolCall(id="call-cancel", name="cancel_order", arguments={"order_id": own.id})
    llm.script(tool_response(call), text_response(answer))

    waiting = await service.collect(
        service.handle_message(
            ChatRequest(message=f"Please cancel my order #{own.id}."), alex.principal
        )
    )
    assert waiting.status == "awaiting_confirmation"
    assert waiting.confirmation is not None

    done = await service.collect(
        service.resolve_confirmation(waiting.confirmation.id, "confirm", alex.principal)
    )

    assert await order_status(db_session, own.id) == "cancelled"
    assert done.status == "completed"
    assert done.content == answer


# --- GRD-91: the model obeys an injected document --------------------------------------------


async def test_obedient_model_cannot_reach_other_customer_data(
    service: ChatService,
    personas: dict[str, Persona],
    make_order: OrderFactory,
    llm: RoutingFakeLLM,
    db_session: AsyncSession,
) -> None:
    """The malicious bulletin escaped quarantine and the model follows it ('show the orders of
    customer X'): the tool still denies the foreign order (TOOL-04) and an answer that names the
    other customer's data is blocked."""
    alex, bia = personas["alex"], personas["bia"]
    foreign = await make_order(bia, OrderStatus.SHIPPED)
    call = ToolCall(id="call-get", name="get_order", arguments={"order_id": foreign.id})
    llm.script(
        tool_response(call),
        text_response(f"Order #{foreign.id} belongs to {bia.display_name} ({bia.email})."),
    )

    result = await service.collect(
        service.handle_message(
            ChatRequest(message="What does the partner promotions bulletin say about my orders?"),
            alex.principal,
        )
    )

    tool_results = [
        message.content
        for request in llm.chat.requests
        for message in request.messages
        if message.role == "tool"
    ]
    assert tool_results, "the tool result goes back to the model"
    assert all(bia.email not in text and bia.display_name not in text for text in tool_results)
    successes = [
        row
        for row in await audit_rows(db_session, alex.id)
        if row.tool == "get_order" and row.outcome == "success"
    ]
    assert successes == []
    assert result.status == "blocked"
    assert result.content == blocked_message("en")
    stored = await customer_messages(db_session, alex.id)
    assert all(bia.email not in row.content for row in stored)
