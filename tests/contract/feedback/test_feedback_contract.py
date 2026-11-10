"""`POST /messages/{message_id}/feedback` contract (CT-64, CT-65, OBS-02, OBS-92, AS-1).

Only the owner of an assistant message can rate it. Every other case (another customer's
message, the customer's own `user` message, a message that does not exist) is the same 404
`MESSAGE_NOT_FOUND` and writes nothing. The Langfuse score follows the accepted feedback only.
"""

import uuid
from typing import Any

import httpx
import pytest
from feedback_contract_helpers import FakeLangfuse, MessageFactory
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import SESSION_COOKIE
from api.feedback.models import Feedback

pytestmark = pytest.mark.integration

MISSING_MESSAGE_ID = 2**62


def _url(message_id: int) -> str:
    return f"/messages/{message_id}/feedback"


async def _rows(session: AsyncSession, message_id: int) -> list[tuple[str, uuid.UUID, str]]:
    result = await session.execute(
        select(Feedback.rating, Feedback.customer_id, Feedback.trace_id).where(
            Feedback.message_id == message_id
        )
    )
    return [(row.rating, row.customer_id, row.trace_id) for row in result]


async def _total_rows(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(Feedback)) or 0)


def _error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "trace_id"}
    assert body["error"]["code"] == code
    assert body["error"]["trace_id"] == response.headers["x-trace-id"]
    error: dict[str, Any] = body["error"]
    return error


# --- owner -------------------------------------------------------------------------------


@pytest.mark.parametrize(("rating", "score"), [("up", 1), ("down", -1)])
async def test_owner_rates_assistant_message(
    client: httpx.AsyncClient,
    cookies: dict[str, dict[str, str]],
    make_message: MessageFactory,
    db_session: AsyncSession,
    fake_langfuse: FakeLangfuse,
    rating: str,
    score: int,
) -> None:
    message = await make_message("alex")

    response = await client.post(_url(message.id), json={"rating": rating}, headers=cookies["alex"])

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert await _rows(db_session, message.id) == [(rating, message.customer_id, message.trace_id)]
    assert fake_langfuse.scores == [(message.trace_id, score)]


async def test_second_rating_replaces_the_first(
    client: httpx.AsyncClient,
    cookies: dict[str, dict[str, str]],
    make_message: MessageFactory,
    db_session: AsyncSession,
    fake_langfuse: FakeLangfuse,
) -> None:
    message = await make_message("alex")

    first = await client.post(_url(message.id), json={"rating": "up"}, headers=cookies["alex"])
    second = await client.post(_url(message.id), json={"rating": "down"}, headers=cookies["alex"])

    assert first.status_code == 204, first.text
    assert second.status_code == 204, second.text
    assert await _rows(db_session, message.id) == [("down", message.customer_id, message.trace_id)]
    assert await _total_rows(db_session) == 1
    assert fake_langfuse.scores == [(message.trace_id, 1), (message.trace_id, -1)]


async def test_score_id_is_stable_per_message_and_distinct_across_messages(
    client: httpx.AsyncClient,
    cookies: dict[str, dict[str, str]],
    make_message: MessageFactory,
    fake_langfuse: FakeLangfuse,
) -> None:
    first = await make_message("alex")
    second = await make_message("alex")

    for message_id, rating in [(first.id, "up"), (first.id, "down"), (second.id, "up")]:
        response = await client.post(
            _url(message_id), json={"rating": rating}, headers=cookies["alex"]
        )
        assert response.status_code == 204, response.text

    assert fake_langfuse.scores == [
        (first.trace_id, 1),
        (first.trace_id, -1),
        (second.trace_id, 1),
    ]
    first_up, first_down, second_up = fake_langfuse.score_ids
    assert first_up is not None
    assert second_up is not None
    assert first_up == first_down
    assert first_up != second_up


async def test_langfuse_failure_does_not_change_the_response(
    client: httpx.AsyncClient,
    cookies: dict[str, dict[str, str]],
    make_message: MessageFactory,
    db_session: AsyncSession,
    fake_langfuse: FakeLangfuse,
) -> None:
    fake_langfuse.fail = True
    message = await make_message("alex")

    response = await client.post(_url(message.id), json={"rating": "up"}, headers=cookies["alex"])

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert await _rows(db_session, message.id) == [("up", message.customer_id, message.trace_id)]


# --- ownership (AS-1, OBS-92) ------------------------------------------------------------


async def test_other_customer_gets_the_same_404_as_a_missing_message(
    client: httpx.AsyncClient,
    cookies: dict[str, dict[str, str]],
    make_message: MessageFactory,
    db_session: AsyncSession,
    fake_langfuse: FakeLangfuse,
) -> None:
    message = await make_message("alex")

    foreign = await client.post(_url(message.id), json={"rating": "down"}, headers=cookies["bia"])
    missing = await client.post(
        _url(MISSING_MESSAGE_ID), json={"rating": "down"}, headers=cookies["bia"]
    )

    foreign_error = _error(foreign, 404, "MESSAGE_NOT_FOUND")
    missing_error = _error(missing, 404, "MESSAGE_NOT_FOUND")
    assert set(foreign.json()) == set(missing.json())
    assert {k: v for k, v in foreign_error.items() if k != "trace_id"} == {
        k: v for k, v in missing_error.items() if k != "trace_id"
    }
    assert await _total_rows(db_session) == 0
    assert fake_langfuse.scores == []


async def test_other_customer_cannot_change_the_owner_rating(
    client: httpx.AsyncClient,
    cookies: dict[str, dict[str, str]],
    make_message: MessageFactory,
    db_session: AsyncSession,
    fake_langfuse: FakeLangfuse,
) -> None:
    message = await make_message("alex")
    owner = await client.post(_url(message.id), json={"rating": "up"}, headers=cookies["alex"])
    assert owner.status_code == 204, owner.text

    intruder = await client.post(_url(message.id), json={"rating": "down"}, headers=cookies["bia"])

    _error(intruder, 404, "MESSAGE_NOT_FOUND")
    assert await _rows(db_session, message.id) == [("up", message.customer_id, message.trace_id)]
    assert fake_langfuse.scores == [(message.trace_id, 1)]


async def test_own_user_message_is_not_found(
    client: httpx.AsyncClient,
    cookies: dict[str, dict[str, str]],
    make_message: MessageFactory,
    db_session: AsyncSession,
    fake_langfuse: FakeLangfuse,
) -> None:
    message = await make_message("alex", role="user")

    response = await client.post(_url(message.id), json={"rating": "up"}, headers=cookies["alex"])

    _error(response, 404, "MESSAGE_NOT_FOUND")
    assert await _total_rows(db_session) == 0
    assert fake_langfuse.scores == []


# --- authentication ----------------------------------------------------------------------


async def test_no_session_is_auth_required(
    client: httpx.AsyncClient,
    make_message: MessageFactory,
    db_session: AsyncSession,
    fake_langfuse: FakeLangfuse,
) -> None:
    message = await make_message("alex")

    response = await client.post(_url(message.id), json={"rating": "up"})

    _error(response, 401, "AUTH_REQUIRED")
    assert await _total_rows(db_session) == 0
    assert fake_langfuse.scores == []


async def test_invalid_cookie_is_auth_required(
    client: httpx.AsyncClient,
    make_message: MessageFactory,
    db_session: AsyncSession,
    fake_langfuse: FakeLangfuse,
) -> None:
    message = await make_message("alex")

    response = await client.post(
        _url(message.id),
        json={"rating": "up"},
        headers={"Cookie": f"{SESSION_COOKIE}=not-a-real-session-token"},
    )

    _error(response, 401, "AUTH_REQUIRED")
    assert await _total_rows(db_session) == 0
    assert fake_langfuse.scores == []


# --- validation --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"rating": "meh"},
        {"rating": "UP"},
        {"rating": 1},
        {"rating": None},
        {},
        {"rating": "up", "comment": "extra field"},
        {"rating": "up", "customer_id": str(uuid.uuid4())},
    ],
)
async def test_invalid_body_is_rejected_without_writing(
    client: httpx.AsyncClient,
    cookies: dict[str, dict[str, str]],
    make_message: MessageFactory,
    db_session: AsyncSession,
    fake_langfuse: FakeLangfuse,
    body: dict[str, Any],
) -> None:
    message = await make_message("alex")

    response = await client.post(_url(message.id), json=body, headers=cookies["alex"])

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await _total_rows(db_session) == 0
    assert fake_langfuse.scores == []
