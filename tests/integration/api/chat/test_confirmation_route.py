"""`POST /chat/confirmations/{confirmation_id}` (TASK-078, CT-46, TOOL-05, TOOL-13).

`require_customer` is overridden per test with the acting Customer; the 401 test keeps the real
cookie dependency.
"""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from chat_helpers import CustomerFactory, principal_of
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import CurrentCustomer, require_customer
from api.auth.models import Customer
from api.chat.service import ChatService, get_chat_service
from api.config import Settings
from api.db.engine import get_db_session
from api.llm.client import LLMResponse, ToolCall, Usage
from api.llm.fake import FakeLLMClient
from api.main import create_app
from api.tools.models import Order, OrderStatus

pytestmark = pytest.mark.integration


def _response(text: str = "", calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls or []),
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="tool_use" if calls else "end_turn",
    )


class Acting:
    """The Customer `require_customer` resolves to; tests switch it between requests."""

    def __init__(self, principal: CurrentCustomer) -> None:
        self.principal = principal


@pytest.fixture
def fake() -> FakeLLMClient:
    return FakeLLMClient([])


@pytest.fixture
async def owner(make_customer: CustomerFactory) -> Customer:
    return await make_customer()


@pytest.fixture
async def acting(owner: Customer) -> Acting:
    return Acting(principal_of(owner))


@pytest.fixture
def app(fake: FakeLLMClient, db_session: AsyncSession, acting: Acting) -> FastAPI:
    app = create_app()
    service = ChatService(fake, Settings(llm_provider="fake"), db_session)
    app.dependency_overrides[require_customer] = lambda: acting.principal
    app.dependency_overrides[get_chat_service] = lambda: service
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _paid_order(session: AsyncSession, customer: Customer) -> Order:
    order = Order(
        customer_id=customer.id,
        status=OrderStatus.PAID.value,
        total_cents=4990,
        currency="USD",
        items=[{"sku": "box-coffee", "name": "Coffee box", "qty": 1}],
    )
    session.add(order)
    await session.flush()
    return order


async def _order_status(session: AsyncSession, order_id: int) -> str:
    statement = (
        select(Order.status).where(Order.id == order_id).execution_options(populate_existing=True)
    )
    return str((await session.execute(statement)).scalar_one())


async def _awaiting(
    client: httpx.AsyncClient, fake: FakeLLMClient, order: Order
) -> dict[str, object]:
    fake.enqueue(
        _response(calls=[ToolCall(id="c1", name="cancel_order", arguments={"order_id": order.id})])
    )
    response = await client.post("/chat", json={"message": f"Cancel my order #{order.id}"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_confirmation"
    assert body["confirmation"]["target"] == f"#{order.id}"
    confirmation: dict[str, object] = body["confirmation"]
    return confirmation


async def test_confirm_returns_completed_and_second_call_is_409(
    client: httpx.AsyncClient, fake: FakeLLMClient, db_session: AsyncSession, owner: Customer
) -> None:
    order = await _paid_order(db_session, owner)
    confirmation = await _awaiting(client, fake, order)
    fake.enqueue(_response("Your order is cancelled."))

    first = await client.post(
        f"/chat/confirmations/{confirmation['id']}", json={"decision": "confirm"}
    )

    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "completed"
    assert body["content"] == "Your order is cancelled."
    assert isinstance(body["message_id"], int)
    assert body["confirmation"] is None
    assert body["trace_id"] == first.headers["x-trace-id"]
    assert await _order_status(db_session, order.id) == "cancelled"

    second = await client.post(
        f"/chat/confirmations/{confirmation['id']}", json={"decision": "confirm"}
    )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFIRMATION_NOT_PENDING"
    assert len(fake.requests) == 2


async def test_refuse_returns_the_model_answer_without_cancelling(
    client: httpx.AsyncClient, fake: FakeLLMClient, db_session: AsyncSession, owner: Customer
) -> None:
    order = await _paid_order(db_session, owner)
    confirmation = await _awaiting(client, fake, order)
    fake.enqueue(_response("Alright, nothing was changed."))

    response = await client.post(
        f"/chat/confirmations/{confirmation['id']}", json={"decision": "refuse"}
    )

    assert response.status_code == 200
    assert response.json()["content"] == "Alright, nothing was changed."
    assert await _order_status(db_session, order.id) == "paid"


async def test_another_customer_confirmation_is_404(
    client: httpx.AsyncClient,
    fake: FakeLLMClient,
    db_session: AsyncSession,
    owner: Customer,
    acting: Acting,
    make_customer: CustomerFactory,
) -> None:
    order = await _paid_order(db_session, owner)
    confirmation = await _awaiting(client, fake, order)
    acting.principal = principal_of(await make_customer())

    response = await client.post(
        f"/chat/confirmations/{confirmation['id']}", json={"decision": "confirm"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONFIRMATION_NOT_FOUND"
    assert await _order_status(db_session, order.id) == "paid"
    assert len(fake.requests) == 1


async def test_unknown_confirmation_is_404(client: httpx.AsyncClient, fake: FakeLLMClient) -> None:
    response = await client.post(
        f"/chat/confirmations/{uuid.uuid4()}", json={"decision": "confirm"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONFIRMATION_NOT_FOUND"
    assert fake.requests == []


@pytest.mark.parametrize(
    "payload",
    [{}, {"decision": "maybe"}, {"decision": "confirm", "customer_id": "x"}],
)
async def test_invalid_body_is_validation_error(
    client: httpx.AsyncClient, fake: FakeLLMClient, payload: dict[str, object]
) -> None:
    response = await client.post(f"/chat/confirmations/{uuid.uuid4()}", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fake.requests == []


async def test_without_session_is_401_without_llm(
    fake: FakeLLMClient, db_session: AsyncSession
) -> None:
    app = create_app()
    service = ChatService(fake, Settings(llm_provider="fake"), db_session)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_chat_service] = lambda: service
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            f"/chat/confirmations/{uuid.uuid4()}", json={"decision": "confirm"}
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert fake.requests == []
