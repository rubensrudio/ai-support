"""`POST /chat` endpoint (TASK-020, TASK-032, CHAT-01, CHAT-90, CHAT-91, CHAT-92).

`require_customer` is overridden with a Customer created in the test database; the real
cookie dependency is covered in `test_chat_route_auth.py`.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from chat_helpers import CustomerFactory, principal_of
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.deps import require_customer
from api.chat.prompt_builder import grounded_system_prompt, language_instruction
from api.chat.service import ChatService, get_chat_service
from api.config import Settings
from api.llm.client import LLMError, LLMResponse, Usage
from api.llm.fake import FakeLLMClient
from api.main import app as module_app
from api.main import create_app

pytestmark = pytest.mark.integration

MAX_CHARS = 50


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
async def client(
    fake: FakeLLMClient, db_session: AsyncSession, make_customer: CustomerFactory
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    principal = principal_of(await make_customer())
    service = ChatService(
        fake, Settings(llm_provider="fake", max_message_chars=MAX_CHARS), db_session
    )
    app.dependency_overrides[require_customer] = lambda: principal
    app.dependency_overrides[get_chat_service] = lambda: service
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def test_module_app_exposes_chat_and_auth_routes() -> None:
    paths = module_app.openapi()["paths"]
    assert "post" in paths["/chat"]
    assert "post" in paths["/auth/login"]


async def test_chat_returns_scripted_answer(client: httpx.AsyncClient, fake: FakeLLMClient) -> None:
    fake.enqueue(_response("Hello from Atlas."))

    response = await client.post("/chat", json={"message": "Hi there"})

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "Hello from Atlas."
    assert body["status"] == "completed"
    assert body["language"] == "en"
    assert body["citations"] == []
    assert body["conversation_id"] is not None
    assert isinstance(body["message_id"], int)
    assert body["confirmation"] is None
    assert body["trace_id"] == response.headers["x-trace-id"]
    assert len(fake.requests) == 1
    request = fake.requests[0]
    # Phase 11 (TASK-110): stable system block (cache breakpoint), then the reply language.
    assert [block.text for block in request.system] == [
        grounded_system_prompt(),
        language_instruction("en"),
    ]
    assert [block.cache_breakpoint for block in request.system] == [True, False]
    assert [(m.role, m.content) for m in request.messages] == [("user", "Hi there")]


async def test_chat_answers_in_ui_language(client: httpx.AsyncClient, fake: FakeLLMClient) -> None:
    fake.enqueue(_response("Olá!"))

    response = await client.post("/chat", json={"message": "Oi", "ui_language": "pt"})

    assert response.status_code == 200
    assert response.json()["language"] == "pt"


@pytest.mark.parametrize("message", ["", "    "])
async def test_blank_message_is_validation_error(
    client: httpx.AsyncClient, fake: FakeLLMClient, message: str
) -> None:
    response = await client.post("/chat", json={"message": message})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["trace_id"] == response.headers["x-trace-id"]
    assert fake.requests == []


async def test_long_message_is_message_too_long(
    client: httpx.AsyncClient, fake: FakeLLMClient
) -> None:
    response = await client.post("/chat", json={"message": "x" * (MAX_CHARS + 1)})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "MESSAGE_TOO_LONG"
    assert str(MAX_CHARS) in error["message"]
    assert error["trace_id"] == response.headers["x-trace-id"]
    assert fake.requests == []


@pytest.mark.parametrize(
    "payload",
    [{}, {"message": 42}, {"message": "hi", "extra": True}, {"message": "hi", "ui_language": "fr"}],
)
async def test_invalid_body_uses_error_envelope(
    client: httpx.AsyncClient, fake: FakeLLMClient, payload: dict[str, object]
) -> None:
    response = await client.post("/chat", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fake.requests == []


async def test_malformed_json_uses_error_envelope(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/chat", content=b"{not json", headers={"content-type": "application/json"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_llm_error_is_llm_unavailable_without_traceback(
    client: httpx.AsyncClient, fake: FakeLLMClient
) -> None:
    fake.enqueue(LLMError("upstream 500: internal provider detail", recoverable=True, status=500))

    response = await client.post("/chat", json={"message": "Hi there"})

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "LLM_UNAVAILABLE"
    assert error["trace_id"] == response.headers["x-trace-id"]
    assert "Traceback" not in response.text
    assert "internal provider detail" not in response.text
    assert len(fake.requests) == 1
