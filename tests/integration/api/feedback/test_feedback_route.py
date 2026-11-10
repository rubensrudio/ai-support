"""`POST /messages/{id}/feedback` with ownership check (TASK-136, OBS-02, OBS-92, AS-1)."""

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import SESSION_COOKIE
from api.auth.models import Customer
from api.auth.service import create_session
from api.chat.models import Message
from api.db.engine import get_db_session
from api.feedback.models import Feedback
from api.feedback.routes import get_langfuse_api
from api.main import create_app

CustomerFactory = Callable[[], Awaitable[Customer]]
MessageFactory = Callable[..., Awaitable[Message]]

pytestmark = pytest.mark.integration


class FakeLangfuseApi:
    """Records `push_score` calls; `fail=True` makes every call raise."""

    def __init__(self) -> None:
        self.scores: list[tuple[str, int, str | None]] = []
        self.fail = False

    async def push_score(
        self,
        trace_id: str,
        value: int,
        comment: str | None = None,
        score_id: str | None = None,
    ) -> bool:
        if self.fail:
            raise RuntimeError("langfuse down")
        self.scores.append((trace_id, value, score_id))
        return True


@pytest.fixture
def fake_langfuse() -> FakeLangfuseApi:
    return FakeLangfuseApi()


@pytest.fixture
async def client(
    db_session: AsyncSession, fake_langfuse: FakeLangfuseApi
) -> AsyncIterator[httpx.AsyncClient]:
    """The real `require_customer`; every dependency shares the test transaction."""
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_langfuse_api] = lambda: fake_langfuse
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _login(client: httpx.AsyncClient, db_session: AsyncSession, customer: Customer) -> None:
    token = await create_session(db_session, customer.id)
    client.cookies.set(SESSION_COOKIE, token)


async def _feedback_rows(db_session: AsyncSession) -> int:
    count = await db_session.scalar(select(func.count()).select_from(Feedback))
    return int(count or 0)


async def test_owner_feedback_is_204_and_stores_the_message_trace_id(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_customer: CustomerFactory,
    make_message: MessageFactory,
) -> None:
    customer = await make_customer()
    message = await make_message(customer)
    await _login(client, db_session, customer)

    response = await client.post(f"/messages/{message.id}/feedback", json={"rating": "up"})

    assert response.status_code == 204
    assert response.content == b""
    stored = (
        await db_session.execute(select(Feedback).where(Feedback.message_id == message.id))
    ).scalar_one()
    assert stored.customer_id == customer.id
    assert stored.rating == "up"
    assert stored.trace_id == message.trace_id


async def test_message_of_another_customer_is_404_like_unknown_and_writes_nothing(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_customer: CustomerFactory,
    make_message: MessageFactory,
    fake_langfuse: FakeLangfuseApi,
) -> None:
    owner = await make_customer()
    intruder = await make_customer()
    message = await make_message(owner)
    await _login(client, db_session, intruder)
    before = await _feedback_rows(db_session)

    foreign = await client.post(f"/messages/{message.id}/feedback", json={"rating": "down"})
    unknown = await client.post(
        f"/messages/{9_000_000_000_000_000}/feedback", json={"rating": "down"}
    )

    assert foreign.status_code == 404
    assert unknown.status_code == 404
    foreign_error = foreign.json()["error"]
    unknown_error = unknown.json()["error"]
    assert foreign_error["code"] == "MESSAGE_NOT_FOUND"
    assert foreign_error["code"] == unknown_error["code"]
    assert foreign_error["message"] == unknown_error["message"]
    assert set(foreign.json()) == set(unknown.json()) == {"error"}
    assert await _feedback_rows(db_session) == before
    assert fake_langfuse.scores == []


async def test_user_message_is_404_and_writes_nothing(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_customer: CustomerFactory,
    make_message: MessageFactory,
) -> None:
    customer = await make_customer()
    message = await make_message(customer, role="user")
    await _login(client, db_session, customer)
    before = await _feedback_rows(db_session)

    response = await client.post(f"/messages/{message.id}/feedback", json={"rating": "up"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MESSAGE_NOT_FOUND"
    assert await _feedback_rows(db_session) == before


async def test_second_feedback_replaces_the_rating(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_customer: CustomerFactory,
    make_message: MessageFactory,
) -> None:
    customer = await make_customer()
    message = await make_message(customer)
    await _login(client, db_session, customer)

    first = await client.post(f"/messages/{message.id}/feedback", json={"rating": "up"})
    second = await client.post(f"/messages/{message.id}/feedback", json={"rating": "down"})

    assert first.status_code == second.status_code == 204
    rows = (
        (await db_session.execute(select(Feedback).where(Feedback.message_id == message.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    await db_session.refresh(rows[0])
    assert rows[0].rating == "down"
    assert rows[0].trace_id == message.trace_id


async def test_without_session_is_401(
    client: httpx.AsyncClient, make_customer: CustomerFactory, make_message: MessageFactory
) -> None:
    message = await make_message(await make_customer())

    response = await client.post(f"/messages/{message.id}/feedback", json={"rating": "up"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize(
    "body",
    [{"rating": "meh"}, {}, {"rating": "up", "customer_id": "someone-else"}],
)
async def test_invalid_body_is_422_and_writes_nothing(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_customer: CustomerFactory,
    make_message: MessageFactory,
    body: dict[str, str],
) -> None:
    customer = await make_customer()
    message = await make_message(customer)
    await _login(client, db_session, customer)
    before = await _feedback_rows(db_session)

    response = await client.post(f"/messages/{message.id}/feedback", json=body)

    assert response.status_code == 422
    assert await _feedback_rows(db_session) == before


async def test_scores_are_pushed_with_the_trace_id(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_customer: CustomerFactory,
    make_message: MessageFactory,
    fake_langfuse: FakeLangfuseApi,
) -> None:
    customer = await make_customer()
    liked = await make_message(customer)
    disliked = await make_message(customer)
    await _login(client, db_session, customer)

    await client.post(f"/messages/{liked.id}/feedback", json={"rating": "up"})
    await client.post(f"/messages/{disliked.id}/feedback", json={"rating": "down"})

    assert fake_langfuse.scores == [
        (liked.trace_id, 1, f"feedback-{liked.trace_id}-{liked.id}"),
        (disliked.trace_id, -1, f"feedback-{disliked.trace_id}-{disliked.id}"),
    ]


async def test_changing_the_rating_reuses_the_same_score_id(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_customer: CustomerFactory,
    make_message: MessageFactory,
    fake_langfuse: FakeLangfuseApi,
) -> None:
    customer = await make_customer()
    message = await make_message(customer)
    await _login(client, db_session, customer)

    await client.post(f"/messages/{message.id}/feedback", json={"rating": "up"})
    await client.post(f"/messages/{message.id}/feedback", json={"rating": "down"})

    score_id = f"feedback-{message.trace_id}-{message.id}"
    assert fake_langfuse.scores == [
        (message.trace_id, 1, score_id),
        (message.trace_id, -1, score_id),
    ]


async def test_failing_langfuse_does_not_change_the_204(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_customer: CustomerFactory,
    make_message: MessageFactory,
    fake_langfuse: FakeLangfuseApi,
) -> None:
    fake_langfuse.fail = True
    customer = await make_customer()
    message = await make_message(customer)
    await _login(client, db_session, customer)

    response = await client.post(f"/messages/{message.id}/feedback", json={"rating": "up"})

    assert response.status_code == 204
    stored = await db_session.scalar(select(Feedback).where(Feedback.message_id == message.id))
    assert stored is not None
