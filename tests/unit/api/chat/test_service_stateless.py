"""Chat service checks that need no database (TASK-019, TASK-031, CHAT-90, CHAT-91).

Validation and authentication happen eagerly in `handle_message`, before any LLM call and
before any database access, so an unbound session is enough here. The LLM path (answer, trace
id, 502 without provider text) is covered with a real database in
`tests/integration/api/chat/test_service_history.py` and `test_chat_route.py`.
"""

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.events import AgentEvent, CitationEvent, DoneEvent, ErrorEvent
from api.auth.deps import CurrentCustomer
from api.chat import service as service_module
from api.chat.schemas import ChatRequest, Citation
from api.chat.service import ChatService, get_chat_service
from api.config import Settings, get_settings
from api.db.engine import get_sessionmaker
from api.errors import AtlasError, ErrorCode
from api.llm.fake import FakeLLMClient
from api.rag.retriever import Retriever

PRINCIPAL = CurrentCustomer(
    id=uuid.uuid4(), email="ana@example.test", display_name="Ana", language="en"
)


def _service(fake: FakeLLMClient, max_chars: int = 4000) -> ChatService:
    return ChatService(
        fake, Settings(llm_provider="fake", max_message_chars=max_chars), AsyncSession()
    )


async def _events(*events: AgentEvent) -> AsyncIterator[AgentEvent]:
    for event in events:
        yield event


@pytest.mark.parametrize("message", ["", "   ", "\n\t  "])
def test_blank_message_is_rejected_without_llm_call(message: str) -> None:
    fake = FakeLLMClient([])
    service = _service(fake)

    with pytest.raises(AtlasError) as info:
        service.handle_message(ChatRequest(message=message), PRINCIPAL)

    assert info.value.code is ErrorCode.VALIDATION_ERROR
    assert info.value.http_status == 422
    assert fake.requests == []


def test_message_over_max_chars_is_rejected_without_llm_call() -> None:
    fake = FakeLLMClient([])
    service = _service(fake, max_chars=10)

    with pytest.raises(AtlasError) as info:
        service.handle_message(ChatRequest(message="x" * 11), PRINCIPAL)

    assert info.value.code is ErrorCode.MESSAGE_TOO_LONG
    assert info.value.http_status == 422
    assert info.value.params == {"max_chars": 10}
    assert fake.requests == []


def test_message_at_max_chars_passes_validation() -> None:
    service = _service(FakeLLMClient([]), max_chars=10)

    events = service.handle_message(ChatRequest(message="x" * 10), PRINCIPAL)

    assert hasattr(events, "__anext__")


def test_default_max_chars_is_4000() -> None:
    fake = FakeLLMClient([])
    service = ChatService(fake, Settings(llm_provider="fake"), AsyncSession())

    with pytest.raises(AtlasError) as info:
        service.handle_message(ChatRequest(message="x" * 4001), PRINCIPAL)

    assert info.value.code is ErrorCode.MESSAGE_TOO_LONG
    assert info.value.params == {"max_chars": 4000}


def test_missing_principal_is_auth_required_without_llm_call() -> None:
    fake = FakeLLMClient([])
    service = _service(fake)

    with pytest.raises(AtlasError) as info:
        service.handle_message(ChatRequest(message="hi"), None)

    assert info.value.code is ErrorCode.AUTH_REQUIRED
    assert info.value.http_status == 401
    assert fake.requests == []


def test_validation_runs_before_authentication() -> None:
    service = _service(FakeLLMClient([]))

    with pytest.raises(AtlasError) as info:
        service.handle_message(ChatRequest(message="  "), None)

    assert info.value.code is ErrorCode.VALIDATION_ERROR


async def test_collect_folds_done_event() -> None:
    service = _service(FakeLLMClient([]))
    conversation_id = uuid.uuid4()
    events = _events(
        DoneEvent(
            status="completed",
            content="ok",
            language="pt",
            trace_id="t-1",
            conversation_id=conversation_id,
            message_id=7,
        )
    )

    response = await service.collect(events)

    assert response.content == "ok"
    assert response.language == "pt"
    assert response.conversation_id == conversation_id
    assert response.message_id == 7
    assert response.trace_id == "t-1"
    assert response.citations == []


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
    first_session, second_session = AsyncSession(), AsyncSession()
    first = get_chat_service(first_session)
    second = get_chat_service(second_session)

    assert isinstance(first, ChatService)
    assert first.llm is second.llm
    assert first.settings is get_settings()
    assert first.session is first_session
    assert second.session is second_session


async def test_collect_folds_citation_events_into_citations() -> None:
    service = _service(FakeLLMClient([]))
    events = _events(
        CitationEvent(index=2, document="Help", doc_key="help", section="Billing", page=3),
        CitationEvent(index=1, document="FAQ"),
        DoneEvent(status="completed", content="See [2][1].", language="en", trace_id="t-1"),
    )

    response = await service.collect(events)

    assert response.citations == [
        Citation(index=2, document="Help", doc_key="help", section="Billing", page=3),
        Citation(index=1, document="FAQ", doc_key=None, section=None, page=None),
    ]


@pytest.mark.usefixtures("fake_provider_env")
def test_get_chat_service_builds_a_retriever_from_the_settings() -> None:
    service = get_chat_service(AsyncSession())

    assert isinstance(service.retriever, Retriever)
    assert service.retriever.settings is get_settings()
    assert service.retriever.embedder is None
    assert service.retriever.session_factory is get_sessionmaker()


def test_retriever_defaults_to_none() -> None:
    assert _service(FakeLLMClient([])).retriever is None
