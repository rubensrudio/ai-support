"""`POST /chat` endpoint (TASK-020, CHAT-01, CHAT-90, CHAT-91, CHAT-92)."""

from collections.abc import AsyncIterator

import httpx
import pytest

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
async def client(fake: FakeLLMClient) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    service = ChatService(fake, Settings(llm_provider="fake", max_message_chars=MAX_CHARS))
    app.dependency_overrides[get_chat_service] = lambda: service
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def test_module_app_exposes_chat_route() -> None:
    assert "post" in module_app.openapi()["paths"]["/chat"]


async def test_chat_returns_scripted_answer(client: httpx.AsyncClient, fake: FakeLLMClient) -> None:
    fake.enqueue(_response("Hello from Atlas."))

    response = await client.post("/chat", json={"message": "Hi there"})

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "Hello from Atlas."
    assert body["status"] == "completed"
    assert body["language"] == "en"
    assert body["citations"] == []
    assert body["conversation_id"] is None
    assert body["message_id"] is None
    assert body["confirmation"] is None
    assert body["trace_id"] == response.headers["x-trace-id"]
    assert len(fake.requests) == 1


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
