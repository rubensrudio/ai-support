"""Daily spend cap contract (CT-68, CT-70, DEMO-03, DEMO-08, DEMO-09, LAC-09, LAC-35, plan 8.1).

When the LLM calls recorded today (UTC) add up to `ATLAS_DAILY_SPEND_CAP_USD`, every route that
would call the LLM answers 429 `DAILY_CAP_REACHED` with `retry_at` = the next 00:00 UTC: `/chat`,
`/chat/stream`, `/triage` (persona and `X-Operator-Key`) and both confirmation routes. Yesterday's
spend never blocks, authentication still comes first (401), and no refused request reaches the
LLM.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from limits_contract_helpers import (
    DAILY_CAP_USD,
    CountFn,
    FakeLLMs,
    Persona,
    SpendFn,
    start_of_utc_day,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("unlimited_rate")]

EN_MESSAGE = "Hi, where is my order?"
PT_MESSAGE = "Oi, onde está o meu pedido?"
TICKET = {"text": "My package arrived broken and I want my money back."}
EN_CAP_MESSAGE = "The demo has reached its daily limit and will be back at 00:00 UTC."
PT_CAP_MESSAGE = "A demo atingiu o limite diário e volta às 00:00 UTC."

ReachFn = Callable[[], Awaitable[None]]


def _assert_cap_reached(response: httpx.Response, now: datetime, message: str) -> None:
    assert response.status_code == 429, response.text
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert error["code"] == "DAILY_CAP_REACHED"
    assert error["trace_id"] == response.headers["x-trace-id"]
    assert error["message"] == message
    retry_at = datetime.fromisoformat(error["retry_at"])
    assert retry_at.tzinfo is not None, "retry_at must carry its UTC offset"
    assert retry_at == start_of_utc_day(now) + timedelta(days=1)


async def test_chat_is_refused_when_todays_spend_reaches_the_cap(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    cap_reached: ReachFn,
    fixed_now: datetime,
    fake_llms: FakeLLMs,
) -> None:
    await cap_reached()

    response = await client.post(
        "/chat", json={"message": EN_MESSAGE}, headers=personas["alex"].headers
    )

    _assert_cap_reached(response, fixed_now, EN_CAP_MESSAGE)
    assert fake_llms.calls == 0


async def test_cap_message_is_in_portuguese_for_bia(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    cap_reached: ReachFn,
    fixed_now: datetime,
) -> None:
    await cap_reached()

    response = await client.post(
        "/chat", json={"message": PT_MESSAGE}, headers=personas["bia"].headers
    )

    _assert_cap_reached(response, fixed_now, PT_CAP_MESSAGE)


async def test_chat_stream_is_refused_before_streaming(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    cap_reached: ReachFn,
    fixed_now: datetime,
    fake_llms: FakeLLMs,
) -> None:
    await cap_reached()

    response = await client.post(
        "/chat/stream", json={"message": EN_MESSAGE}, headers=personas["alex"].headers
    )

    _assert_cap_reached(response, fixed_now, EN_CAP_MESSAGE)
    assert fake_llms.calls == 0


async def test_persona_triage_is_refused(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    cap_reached: ReachFn,
    fixed_now: datetime,
    fake_llms: FakeLLMs,
) -> None:
    await cap_reached()

    response = await client.post("/triage", json=TICKET, headers=personas["alex"].headers)

    _assert_cap_reached(response, fixed_now, EN_CAP_MESSAGE)
    assert fake_llms.calls == 0


async def test_operator_triage_is_refused(
    client: httpx.AsyncClient,
    cap_reached: ReachFn,
    fixed_now: datetime,
    operator_headers: dict[str, str],
    fake_llms: FakeLLMs,
) -> None:
    await cap_reached()

    response = await client.post("/triage", json=TICKET, headers=operator_headers)

    _assert_cap_reached(response, fixed_now, EN_CAP_MESSAGE)
    assert fake_llms.calls == 0


@pytest.mark.parametrize("suffix", ["", "/stream"], ids=["json", "stream"])
async def test_confirmation_is_refused(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    cap_reached: ReachFn,
    fixed_now: datetime,
    fake_llms: FakeLLMs,
    suffix: str,
) -> None:
    await cap_reached()

    response = await client.post(
        f"/chat/confirmations/{uuid.uuid4()}{suffix}",
        json={"decision": "confirm"},
        headers=personas["alex"].headers,
    )

    _assert_cap_reached(response, fixed_now, EN_CAP_MESSAGE)
    assert fake_llms.calls == 0


async def test_spend_below_the_cap_is_allowed(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    spend: SpendFn,
    fixed_now: datetime,
    operator_headers: dict[str, str],
) -> None:
    await spend(DAILY_CAP_USD - Decimal("0.000001"), fixed_now - timedelta(hours=1))

    chat = await client.post(
        "/chat", json={"message": EN_MESSAGE}, headers=personas["alex"].headers
    )
    triage = await client.post("/triage", json=TICKET, headers=operator_headers)

    assert chat.status_code == 200, chat.text
    assert triage.status_code == 200, triage.text


async def test_yesterdays_spend_does_not_block(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    spend: SpendFn,
    fixed_now: datetime,
    operator_headers: dict[str, str],
) -> None:
    just_before_midnight = start_of_utc_day(fixed_now) - timedelta(seconds=1)
    await spend(DAILY_CAP_USD * 5, just_before_midnight)

    chat = await client.post(
        "/chat", json={"message": EN_MESSAGE}, headers=personas["alex"].headers
    )
    triage = await client.post("/triage", json=TICKET, headers=operator_headers)

    assert chat.status_code == 200, chat.text
    assert triage.status_code == 200, triage.text


async def test_without_a_session_the_answer_is_401_not_429(
    client: httpx.AsyncClient,
    cap_reached: ReachFn,
    fake_llms: FakeLLMs,
) -> None:
    await cap_reached()

    requests = [
        client.post("/chat", json={"message": EN_MESSAGE}),
        client.post("/chat/stream", json={"message": EN_MESSAGE}),
        client.post("/triage", json=TICKET),
        client.post(f"/chat/confirmations/{uuid.uuid4()}", json={"decision": "confirm"}),
    ]
    for pending in requests:
        response = await pending
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert fake_llms.calls == 0


async def test_rate_limit_is_checked_before_the_cap(
    client: httpx.AsyncClient,
    personas: dict[str, Persona],
    cap_reached: ReachFn,
    count_rate_events: CountFn,
) -> None:
    """A message refused by the cap was accepted by the rate limit, so it counts there."""
    alex = personas["alex"]
    await cap_reached()

    response = await client.post("/chat", json={"message": EN_MESSAGE}, headers=alex.headers)

    assert response.status_code == 429, response.text
    assert await count_rate_events(alex.id) == 1
