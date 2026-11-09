"""Stateless chat service (TASK-019, CHAT-01, CHAT-09, CHAT-90, CHAT-91, CHAT-92)."""

from collections.abc import AsyncIterator, Iterator

import pytest

from api.agent.events import AgentEvent, DoneEvent, ErrorEvent
from api.chat import service as service_module
from api.chat.schemas import ChatRequest
from api.chat.service import ChatService, get_chat_service
from api.config import Settings, get_settings
from api.errors import AtlasError, ErrorCode
from api.llm.client import LLMError, LLMResponse, LLMTimeoutError, Usage
from api.llm.fake import FakeLLMClient
from api.observability.logging import bind_trace_id, reset_trace_id
from api.prompts import load_prompt


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


def _service(fake: FakeLLMClient, max_chars: int = 4000) -> ChatService:
    return ChatService(fake, Settings(llm_provider="fake", max_message_chars=max_chars))


async def _events(*events: AgentEvent) -> AsyncIterator[AgentEvent]:
    for event in events:
        yield event


async def test_answer_contains_scripted_text_and_completed_status() -> None:
    fake = FakeLLMClient([_response("Your plan renews monthly.")])
    service = _service(fake)

    response = await service.collect(
        service.handle_message(ChatRequest(message="When does my plan renew?"), None)
    )

    assert response.status == "completed"
    assert response.content == "Your plan renews monthly."
    assert response.language == "en"
    assert response.citations == []
    assert response.conversation_id is None
    assert response.message_id is None
    assert response.confirmation is None
    assert response.trace_id


async def test_single_llm_call_with_system_prompt_and_no_history() -> None:
    fake = FakeLLMClient([_response("ok")])
    service = _service(fake)

    await service.collect(service.handle_message(ChatRequest(message="  hello  "), None))

    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert [block.text for block in request.system] == [load_prompt("system")]
    assert [(m.role, m.content) for m in request.messages] == [("user", "  hello  ")]
    assert request.purpose == "chat"
    assert request.tools == []


async def test_language_follows_ui_language() -> None:
    fake = FakeLLMClient([_response("Olá")])
    service = _service(fake)

    response = await service.collect(
        service.handle_message(ChatRequest(message="Oi", ui_language="pt"), None)
    )

    assert response.language == "pt"


async def test_done_event_carries_current_trace_id() -> None:
    fake = FakeLLMClient([_response("ok")])
    service = _service(fake)
    token = bind_trace_id("trace-abc")
    try:
        events = [e async for e in service.handle_message(ChatRequest(message="hi"), None)]
    finally:
        reset_trace_id(token)

    assert len(events) == 1
    assert isinstance(events[0], DoneEvent)
    assert events[0].trace_id == "trace-abc"


@pytest.mark.parametrize("message", ["", "   ", "\n\t  "])
def test_blank_message_is_rejected_without_llm_call(message: str) -> None:
    fake = FakeLLMClient([_response("unused")])
    service = _service(fake)

    with pytest.raises(AtlasError) as info:
        service.handle_message(ChatRequest(message=message), None)

    assert info.value.code is ErrorCode.VALIDATION_ERROR
    assert info.value.http_status == 422
    assert fake.requests == []


def test_message_over_max_chars_is_rejected_without_llm_call() -> None:
    fake = FakeLLMClient([_response("unused")])
    service = _service(fake, max_chars=10)

    with pytest.raises(AtlasError) as info:
        service.handle_message(ChatRequest(message="x" * 11), None)

    assert info.value.code is ErrorCode.MESSAGE_TOO_LONG
    assert info.value.http_status == 422
    assert info.value.params == {"max_chars": 10}
    assert fake.requests == []


async def test_message_at_max_chars_is_accepted() -> None:
    fake = FakeLLMClient([_response("ok")])
    service = _service(fake, max_chars=10)

    response = await service.collect(service.handle_message(ChatRequest(message="x" * 10), None))

    assert response.content == "ok"
    assert len(fake.requests) == 1


def test_default_max_chars_is_4000() -> None:
    fake = FakeLLMClient([])
    service = ChatService(fake, Settings(llm_provider="fake"))

    with pytest.raises(AtlasError) as info:
        service.handle_message(ChatRequest(message="x" * 4001), None)

    assert info.value.code is ErrorCode.MESSAGE_TOO_LONG
    assert info.value.params == {"max_chars": 4000}


@pytest.mark.parametrize(
    "error",
    [
        LLMError("provider exploded: secret detail", recoverable=False, status=500),
        LLMTimeoutError(),
    ],
)
async def test_llm_error_becomes_llm_unavailable(error: LLMError) -> None:
    fake = FakeLLMClient([error])
    service = _service(fake)

    with pytest.raises(AtlasError) as info:
        await service.collect(service.handle_message(ChatRequest(message="hi"), None))

    assert info.value.code is ErrorCode.LLM_UNAVAILABLE
    assert info.value.http_status == 502
    assert info.value.params == {}
    assert "secret detail" not in str(info.value)
    assert len(fake.requests) == 1


async def test_collect_raises_atlas_error_for_error_event() -> None:
    service = _service(FakeLLMClient([]))
    events = _events(
        ErrorEvent(code="LLM_UNAVAILABLE", message="I couldn't answer", trace_id="t-1")
    )

    with pytest.raises(AtlasError) as info:
        await service.collect(events)

    assert info.value.code is ErrorCode.LLM_UNAVAILABLE
    assert info.value.http_status == 502


async def test_collect_without_terminal_event_is_internal_error() -> None:
    service = _service(FakeLLMClient([]))

    with pytest.raises(AtlasError) as info:
        await service.collect(_events())

    assert info.value.code is ErrorCode.INTERNAL_ERROR
    assert info.value.http_status == 500


@pytest.fixture
def fake_provider_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "fake")
    get_settings.cache_clear()
    service_module._llm_client.cache_clear()
    yield
    service_module._llm_client.cache_clear()
    get_settings.cache_clear()


@pytest.mark.usefixtures("fake_provider_env")
def test_get_chat_service_reuses_one_llm_client_per_process() -> None:
    first = get_chat_service()
    second = get_chat_service()

    assert isinstance(first, ChatService)
    assert first.llm is second.llm
    assert first.settings is get_settings()
