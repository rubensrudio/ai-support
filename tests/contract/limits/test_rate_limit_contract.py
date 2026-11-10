"""Per-account rate limit contract (CT-69, CT-70, DEMO-02, DEMO-08, DEMO-09, LAC-35, plan 8.1).

With `ATLAS_RATE_LIMIT_PER_HOUR=3`, the 4th message of a persona within one hour is refused with
429 `RATE_LIMITED`; the envelope carries `retry_at` (ISO-8601, in the future, at most one hour
away) and the message is in the persona's language. The refused message never reaches the LLM
and stores neither a message nor a conversation. Each persona has its own budget; the operator
on `/triage` and the confirmation routes never consume it.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from limits_contract_helpers import CountFn, FakeLLMs, Persona

pytestmark = pytest.mark.integration

EN_MESSAGE = "Hi, where is my order?"
PT_MESSAGE = "Oi, onde está o meu pedido?"
TICKET = {"text": "My package arrived broken and I want my money back."}
HHMM_UTC = re.compile(r"\b\d{2}:\d{2} UTC\b")


async def _send(client: httpx.AsyncClient, persona: Persona, message: str) -> httpx.Response:
    return await client.post("/chat", json={"message": message}, headers=persona.headers)


async def _use_budget(
    client: httpx.AsyncClient, persona: Persona, message: str, times: int
) -> None:
    for attempt in range(times):
        response = await _send(client, persona, message)
        assert response.status_code == 200, f"message {attempt + 1}: {response.text}"


def _assert_rate_limited(response: httpx.Response, sent_at: datetime) -> datetime:
    assert response.status_code == 429, response.text
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert error["code"] == "RATE_LIMITED"
    assert error["trace_id"] == response.headers["x-trace-id"]
    retry_at = datetime.fromisoformat(error["retry_at"])
    assert retry_at.tzinfo is not None, "retry_at must carry its UTC offset"
    assert sent_at < retry_at <= sent_at + timedelta(hours=1, seconds=5)
    assert HHMM_UTC.search(error["message"]), error["message"]
    assert retry_at.astimezone(UTC).strftime("%H:%M UTC") in error["message"]
    return retry_at


async def test_fourth_message_in_an_hour_is_rate_limited_in_english(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    rate_limit: int,
    fake_llms: FakeLLMs,
) -> None:
    alex = personas["alex"]
    started = datetime.now(UTC)
    await _use_budget(client, alex, EN_MESSAGE, rate_limit)

    response = await _send(client, alex, EN_MESSAGE)

    _assert_rate_limited(response, started)
    assert "message limit" in response.json()["error"]["message"]


async def test_rate_limited_message_is_in_portuguese_for_bia(
    client: httpx.AsyncClient, personas: dict[str, Persona], rate_limit: int
) -> None:
    bia = personas["bia"]
    started = datetime.now(UTC)
    await _use_budget(client, bia, PT_MESSAGE, rate_limit)

    response = await _send(client, bia, PT_MESSAGE)

    _assert_rate_limited(response, started)
    assert "limite de mensagens" in response.json()["error"]["message"]


async def test_refused_message_never_reaches_the_llm_nor_the_database(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    rate_limit: int,
    fake_llms: FakeLLMs,
    count_conversations: CountFn,
    count_messages: CountFn,
    count_rate_events: CountFn,
) -> None:
    alex = personas["alex"]
    await _use_budget(client, alex, EN_MESSAGE, rate_limit)
    calls = fake_llms.calls
    conversations = await count_conversations(alex.id)
    messages = await count_messages(alex.id)
    events = await count_rate_events(alex.id)
    assert events == rate_limit

    response = await _send(client, alex, EN_MESSAGE)

    assert response.status_code == 429, response.text
    assert fake_llms.calls == calls, "a refused message must not call the LLM"
    assert await count_conversations(alex.id) == conversations
    assert await count_messages(alex.id) == messages
    assert await count_rate_events(alex.id) == events, "a refused message is not counted"


async def test_stream_route_shares_the_rate_limit(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    rate_limit: int,
    fake_llms: FakeLLMs,
) -> None:
    alex = personas["alex"]
    started = datetime.now(UTC)
    await _use_budget(client, alex, EN_MESSAGE, rate_limit)
    calls = fake_llms.calls

    response = await client.post("/chat/stream", json={"message": EN_MESSAGE}, headers=alex.headers)

    _assert_rate_limited(response, started)
    assert fake_llms.calls == calls


async def test_one_persona_does_not_consume_the_other_budget(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    rate_limit: int,
    count_rate_events: CountFn,
) -> None:
    alex, bia = personas["alex"], personas["bia"]
    await _use_budget(client, alex, EN_MESSAGE, rate_limit)
    assert (await _send(client, alex, EN_MESSAGE)).status_code == 429

    for attempt in range(rate_limit):
        response = await _send(client, bia, PT_MESSAGE)
        assert response.status_code == 200, f"bia message {attempt + 1}: {response.text}"
    assert await count_rate_events(bia.id) == rate_limit


async def test_persona_triage_counts_against_the_same_budget(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    rate_limit: int,
    fake_llms: FakeLLMs,
) -> None:
    alex = personas["alex"]
    started = datetime.now(UTC)
    await _use_budget(client, alex, EN_MESSAGE, rate_limit)
    calls = fake_llms.calls

    response = await client.post("/triage", json=TICKET, headers=alex.headers)

    _assert_rate_limited(response, started)
    assert fake_llms.calls == calls


async def test_operator_triage_does_not_consume_the_rate_limit(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    rate_limit: int,
    operator_headers: dict[str, str],
    count_rate_events: CountFn,
) -> None:
    for attempt in range(rate_limit + 2):
        response = await client.post("/triage", json=TICKET, headers=operator_headers)
        assert response.status_code == 200, f"operator triage {attempt + 1}: {response.text}"

    for persona in personas.values():
        assert await count_rate_events(persona.id) == 0
    # The personas still have their whole budget.
    await _use_budget(client, personas["alex"], EN_MESSAGE, rate_limit)


async def test_confirmation_does_not_consume_the_rate_limit(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    rate_limit: int,
    count_rate_events: CountFn,
) -> None:
    alex = personas["alex"]
    await _use_budget(client, alex, EN_MESSAGE, rate_limit)
    unknown = uuid.uuid4()

    # Not refused by the rate limit: the route itself answers (unknown confirmation).
    response = await client.post(
        f"/chat/confirmations/{unknown}", json={"decision": "confirm"}, headers=alex.headers
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "CONFIRMATION_NOT_FOUND"

    response = await client.post(
        f"/chat/confirmations/{unknown}/stream",
        json={"decision": "confirm"},
        headers=alex.headers,
    )
    assert response.status_code == 200, response.text  # SSE: the error is an `error` event
    assert "CONFIRMATION_NOT_FOUND" in response.text
    assert "RATE_LIMITED" not in response.text

    assert await count_rate_events(alex.id) == rate_limit


async def test_invalid_message_does_not_consume_the_rate_limit(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    rate_limit: int,
    count_rate_events: CountFn,
) -> None:
    alex = personas["alex"]

    for attempt in range(rate_limit + 1):
        response = await _send(client, alex, "   ")
        assert response.status_code == 422, f"blank message {attempt + 1}: {response.text}"

    assert await count_rate_events(alex.id) == 0
    await _use_budget(client, alex, EN_MESSAGE, rate_limit)


async def test_without_a_session_the_answer_is_401_not_429(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    rate_limit: int,
) -> None:
    await _use_budget(client, personas["alex"], EN_MESSAGE, rate_limit)

    response = await client.post("/chat", json={"message": EN_MESSAGE})

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
