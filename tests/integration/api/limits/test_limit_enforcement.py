"""Demo limits on the chat, confirmation and triage endpoints (TASK-145, CT-70, DEMO-02,
DEMO-03, DEMO-09, LAC-35, AS-2).

The real app with a real persona login (`/auth/login` over the seeded personas). Every
dependency shares the rolled-back test transaction, the LLM is a scripted fake and the triage
service a recording double, so a refused request is proven to reach no LLM. `utc_now` is
pinned to a fixed instant far from the real clock.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import OPERATOR_HEADER
from api.auth.models import Customer
from api.chat.service import ChatService, get_chat_service
from api.config import Settings, get_settings
from api.db.engine import get_db_session
from api.limits import deps
from api.limits.models import LlmCall, RateLimitEvent
from api.llm.client import LLMResponse, Usage
from api.llm.fake import FakeLLMClient
from api.main import create_app
from api.seed.personas import seed_personas
from api.triage.routes import get_triage_service
from api.triage.schemas import TriageResult
from api.triage.service import TriageMode, TriageService

pytestmark = pytest.mark.integration

NOW = datetime(2031, 3, 15, 12, 0, tzinfo=UTC)
LIMIT = 20
PASSWORD = "limits-test-password"  # noqa: S105 - fixture value
OPERATOR_KEY = "operator-key-for-limit-tests"
RESULT = TriageResult(
    categoria="reembolso",
    prioridade="alta",
    sentimento="negativo",
    precisa_humano=True,
    resumo="Customer wants a refund.",
)


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


class RecordingTriageService(TriageService):
    def __init__(self) -> None:
        super().__init__(FakeLLMClient([]), Settings(llm_provider="fake"))
        self.calls: list[str] = []

    async def classify(self, text: str, mode: TriageMode | None = None) -> TriageResult:
        self.calls.append(text)
        return RESULT


@pytest.fixture(autouse=True)
def demo_limits(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_RATE_LIMIT_PER_HOUR", str(LIMIT))
    monkeypatch.setenv("ATLAS_DAILY_SPEND_CAP_USD", "2.00")
    monkeypatch.setenv("ATLAS_OPERATOR_API_KEY", OPERATOR_KEY)
    monkeypatch.setattr(deps, "utc_now", lambda: NOW)
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def fake() -> FakeLLMClient:
    return FakeLLMClient([])


@pytest.fixture
def triage() -> RecordingTriageService:
    return RecordingTriageService()


@pytest.fixture
async def client(
    fake: FakeLLMClient, triage: RecordingTriageService, db_session: AsyncSession
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    service = ChatService(
        fake,
        Settings(llm_provider="fake", guardrails_enabled=False, agent_tools_enabled=False),
        db_session,
    )
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_chat_service] = lambda: service
    app.dependency_overrides[get_triage_service] = lambda: triage
    await seed_personas(db_session, PASSWORD)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _login(client: httpx.AsyncClient, username: str) -> None:
    response = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200


async def _events(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(RateLimitEvent)) or 0)


async def _reach_the_cap(session: AsyncSession) -> None:
    session.add(
        LlmCall(
            trace_id=uuid.uuid4().hex,
            provider="anthropic",
            model="claude-haiku-5-5",
            cost_usd=Decimal("2.000000"),
            created_at=NOW - timedelta(hours=1),
        )
    )
    await session.flush()


def _assert_429(response: httpx.Response, code: str, retry_at: datetime) -> str:
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == code
    assert error["retry_at"] == retry_at.isoformat()
    assert error["trace_id"] == response.headers["x-trace-id"]
    message: str = error["message"]
    return message


# --- rate limit ------------------------------------------------------------------------------


async def test_21st_message_is_rate_limited_without_llm_call(
    client: httpx.AsyncClient, fake: FakeLLMClient
) -> None:
    await _login(client, "bia")
    fake.enqueue(*(_response(f"answer {i}") for i in range(LIMIT + 1)))
    for i in range(LIMIT):
        response = await client.post("/chat", json={"message": f"message {i}"})
        assert response.status_code == 200
    calls_before = len(fake.requests)

    response = await client.post("/chat", json={"message": "one too many"})

    message = _assert_429(response, "RATE_LIMITED", NOW + timedelta(hours=1))
    assert message == "Você atingiu o limite de mensagens. Tente de novo às 13:00 UTC."
    assert len(fake.requests) == calls_before


async def test_stream_counts_against_the_same_limit(
    client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeLLMClient
) -> None:
    await _login(client, "alex")
    alex = await _persona_id(db_session, "alex")
    db_session.add_all(RateLimitEvent(customer_id=alex, created_at=NOW) for _ in range(LIMIT))
    await db_session.flush()

    response = await client.post("/chat/stream", json={"message": "hello"})

    message = _assert_429(response, "RATE_LIMITED", NOW + timedelta(hours=1))
    assert message == "You have reached the message limit. Please try again at 13:00 UTC."
    assert fake.requests == []


async def test_invalid_message_does_not_consume_the_limit(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _login(client, "alex")

    blank = await client.post("/chat", json={"message": "   "})
    bad_body = await client.post("/chat/stream", json={"message": 42})
    bad_triage = await client.post("/triage", json={"text": ""})

    assert [blank.status_code, bad_body.status_code, bad_triage.status_code] == [422, 422, 422]
    assert await _events(db_session) == 0


async def test_without_session_is_401_before_the_limits(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _reach_the_cap(db_session)

    response = await client.post("/chat", json={"message": "hello"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


# --- daily spend cap -------------------------------------------------------------------------


async def test_cap_blocks_chat_stream_triage_and_confirmations(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake: FakeLLMClient,
    triage: RecordingTriageService,
) -> None:
    await _login(client, "bia")
    await _reach_the_cap(db_session)
    midnight = datetime(2031, 3, 16, tzinfo=UTC)
    confirmation = uuid.uuid4()

    responses = [
        await client.post("/chat", json={"message": "hello"}),
        await client.post("/chat/stream", json={"message": "hello"}),
        await client.post("/triage", json={"text": "My order arrived broken"}),
        await client.post(f"/chat/confirmations/{confirmation}", json={"decision": "confirm"}),
        await client.post(
            f"/chat/confirmations/{confirmation}/stream", json={"decision": "confirm"}
        ),
    ]

    for response in responses:
        message = _assert_429(response, "DAILY_CAP_REACHED", midnight)
        assert message == "A demo atingiu o limite diário e volta às 00:00 UTC."
    assert fake.requests == []
    assert triage.calls == []


async def test_cap_blocks_the_operator_triage(
    client: httpx.AsyncClient, db_session: AsyncSession, triage: RecordingTriageService
) -> None:
    await _reach_the_cap(db_session)

    response = await client.post(
        "/triage", json={"text": "Refund please"}, headers={OPERATOR_HEADER: OPERATOR_KEY}
    )

    message = _assert_429(response, "DAILY_CAP_REACHED", datetime(2031, 3, 16, tzinfo=UTC))
    assert message == "The demo has reached its daily limit and will be back at 00:00 UTC."
    assert triage.calls == []


async def test_below_the_cap_is_served(
    client: httpx.AsyncClient, db_session: AsyncSession, fake: FakeLLMClient
) -> None:
    await _login(client, "alex")
    db_session.add(
        LlmCall(
            trace_id=uuid.uuid4().hex,
            provider="anthropic",
            model="claude-haiku-5-5",
            cost_usd=Decimal("1.990000"),
            created_at=NOW - timedelta(minutes=5),
        )
    )
    await db_session.flush()
    fake.enqueue(_response("Hi!"))

    response = await client.post("/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert len(fake.requests) == 1


# --- who consumes the rate limit -------------------------------------------------------------


async def test_confirmation_does_not_consume_the_rate_limit(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _login(client, "alex")

    response = await client.post(
        f"/chat/confirmations/{uuid.uuid4()}", json={"decision": "confirm"}
    )

    assert response.status_code == 404  # the limits let it through to the service
    assert await _events(db_session) == 0


async def test_operator_triage_does_not_consume_the_rate_limit(
    client: httpx.AsyncClient, db_session: AsyncSession, triage: RecordingTriageService
) -> None:
    response = await client.post(
        "/triage", json={"text": "Refund please"}, headers={OPERATOR_HEADER: OPERATOR_KEY}
    )

    assert response.status_code == 200
    assert triage.calls == ["Refund please"]
    assert await _events(db_session) == 0


async def test_persona_triage_consumes_the_rate_limit(
    client: httpx.AsyncClient, db_session: AsyncSession, triage: RecordingTriageService
) -> None:
    await _login(client, "alex")

    response = await client.post("/triage", json={"text": "Refund please"})

    assert response.status_code == 200
    assert await _events(db_session) == 1


async def _persona_id(session: AsyncSession, username: str) -> uuid.UUID:
    customer_id = await session.scalar(select(Customer.id).where(Customer.username == username))
    assert customer_id is not None
    return customer_id
