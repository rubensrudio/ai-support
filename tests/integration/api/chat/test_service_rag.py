"""Retrieval-augmented chat service (TASK-049, RAG-03, RAG-04, RAG-90, RAG-94).

The retriever is a fake (`retrieve` scripted per test), so these tests do not depend on the
full-text search; the database holds the conversation and its messages.
"""

import pytest
from chat_helpers import CustomerFactory, principal_of
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.events import CitationEvent, DoneEvent
from api.chat.models import Conversation
from api.chat.prompt_builder import NO_EXCERPTS_INSTRUCTION, system_prompt
from api.chat.repository import ConversationRepository
from api.chat.schemas import ChatRequest, Citation
from api.chat.service import ChatService
from api.config import RetrievalMode, Settings
from api.errors import AtlasError, ErrorCode
from api.llm.client import LLMError, LLMRequest, LLMResponse, Usage
from api.llm.fake import FakeLLMClient
from api.messages import Lang, MessageKey, user_message
from api.rag.retriever import RetrievalUnavailable, RetrievedChunk, Retriever

pytestmark = pytest.mark.integration

QUESTION = "How do I cancel my subscription plan?"
GERMAN = "Ich möchte meine Bestellung von gestern stornieren, bitte helfen Sie mir dabei."


def _chunk(n: int, language: Lang = "en") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=n,
        document_title=f"Help article {n}",
        doc_key=f"help-{n}",
        section=f"Section {n}",
        page=n if n % 2 else None,
        language=language,
        content=f"Excerpt number {n} about subscriptions.",
        score=1.0 / n,
    )


CHUNKS = [_chunk(1), _chunk(2), _chunk(3)]


class FakeRetriever(Retriever):
    """Returns `chunks` (or raises `error`) and records every call."""

    def __init__(
        self, chunks: list[RetrievedChunk] | None = None, error: Exception | None = None
    ) -> None:
        self.chunks = chunks or []
        self.error = error
        self.calls: list[tuple[str, Lang]] = []

    async def retrieve(
        self,
        query: str,
        language: Lang,
        k: int | None = None,
        mode: RetrievalMode | None = None,
    ) -> list[RetrievedChunk]:
        self.calls.append((query, language))
        if self.error is not None:
            raise self.error
        return self.chunks


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


def _service(
    fake: FakeLLMClient,
    session: AsyncSession,
    retriever: Retriever | None,
    **settings: object,
) -> ChatService:
    config = Settings(llm_provider="fake", **settings)  # type: ignore[arg-type]
    return ChatService(fake, config, session, retriever=retriever)


def _sent_text(request: LLMRequest) -> str:
    return "\n".join(
        [block.text for block in request.system[1:]] + [m.content for m in request.messages]
    )


async def test_response_citations_match_markers_of_the_answer(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    fake = FakeLLMClient([_response("Open Settings > Plan [2], then confirm [3][2]. Not [7].")])
    retriever = FakeRetriever(CHUNKS)
    service = _service(fake, db_session, retriever)

    response = await service.collect(
        service.handle_message(ChatRequest(message=QUESTION), principal_of(await make_customer()))
    )

    assert retriever.calls == [(QUESTION, "en")]
    assert response.status == "completed"
    assert response.content == "Open Settings > Plan [2], then confirm [3][2]. Not [7]."
    assert response.citations == [
        Citation(
            index=2, document="Help article 2", doc_key="help-2", section="Section 2", page=None
        ),
        Citation(index=3, document="Help article 3", doc_key="help-3", section="Section 3", page=3),
    ]
    assert len(fake.requests) == 1
    sent = _sent_text(fake.requests[0])
    for n, chunk in enumerate(CHUNKS, start=1):
        assert f'<excerpt index="{n}" title="{chunk.document_title}"' in sent
        assert chunk.content in sent
    assert fake.requests[0].system[0].text == system_prompt("en")
    assert (
        fake.requests[0]
        .messages[-1]
        .content.endswith(f"<customer_question>\n{QUESTION}\n</customer_question>")
    )
    assert fake.requests[0].tools == []


async def test_citation_events_come_before_the_done_event(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    fake = FakeLLMClient([_response("See [1].")])
    service = _service(fake, db_session, FakeRetriever(CHUNKS))

    events = [
        event
        async for event in service.handle_message(
            ChatRequest(message=QUESTION), principal_of(await make_customer())
        )
    ]

    assert [type(event) for event in events] == [CitationEvent, DoneEvent]
    citation = events[0]
    assert isinstance(citation, CitationEvent)
    assert (
        citation.index,
        citation.document,
        citation.doc_key,
        citation.section,
        citation.page,
    ) == (1, "Help article 1", "help-1", "Section 1", 1)


async def test_retriever_failure_answers_knowledge_unavailable_without_llm(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    fake = FakeLLMClient([_response("unused")])
    retriever = FakeRetriever(error=RetrievalUnavailable("search failed"))
    service = _service(fake, db_session, retriever)

    response = await service.collect(
        service.handle_message(
            ChatRequest(message="Quero cancelar a minha assinatura"),
            principal_of(await make_customer()),
        )
    )

    assert len(fake.requests) == 0
    assert retriever.calls == [("Quero cancelar a minha assinatura", "pt")]
    assert response.content == user_message(MessageKey.KNOWLEDGE_UNAVAILABLE, "pt")
    assert response.status == "completed"
    assert response.language == "pt"
    assert response.citations == []
    assert response.conversation_id is not None
    messages = await ConversationRepository(db_session).list_messages(response.conversation_id)
    assert [(m.role, m.content, m.status) for m in messages] == [
        ("user", "Quero cancelar a minha assinatura", "complete"),
        ("assistant", user_message(MessageKey.KNOWLEDGE_UNAVAILABLE, "pt"), "complete"),
    ]
    assert response.message_id == messages[1].id


async def test_empty_index_calls_llm_with_do_not_know_instruction(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    answer = "I don't know: I could not find this in the Assinaflow knowledge base."
    fake = FakeLLMClient([_response(answer)])
    retriever = FakeRetriever([])
    service = _service(fake, db_session, retriever)

    response = await service.collect(
        service.handle_message(ChatRequest(message=QUESTION), principal_of(await make_customer()))
    )

    assert retriever.calls == [(QUESTION, "en")]
    assert len(fake.requests) == 1
    assert NO_EXCERPTS_INSTRUCTION in _sent_text(fake.requests[0])
    assert response.status == "completed"
    assert response.content == answer
    assert response.citations == []


async def test_second_turn_sends_history_once_and_the_question_last(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    principal = principal_of(await make_customer())
    fake = FakeLLMClient([_response("Hello, Rubens [1]."), _response("Cancel in Settings [1].")])
    retriever = FakeRetriever(CHUNKS)
    service = _service(fake, db_session, retriever)

    first = await service.collect(
        service.handle_message(ChatRequest(message="Hi, my name is Rubens."), principal)
    )
    await service.collect(
        service.handle_message(
            ChatRequest(message=QUESTION, conversation_id=first.conversation_id), principal
        )
    )

    messages = fake.requests[1].messages
    assert [(m.role, m.content) for m in messages[:-1]] == [
        ("user", "Hi, my name is Rubens."),
        ("assistant", "Hello, Rubens [1]."),
    ]
    assert messages[-1].role == "user"
    assert messages[-1].content.endswith(f"<customer_question>\n{QUESTION}\n</customer_question>")
    assert sum(QUESTION in m.content for m in messages) == 1
    assert retriever.calls[1] == (QUESTION, "en")


@pytest.mark.parametrize("layout", ["naive", "cache_ordered"])
async def test_prompt_layout_comes_from_settings(
    db_session: AsyncSession, make_customer: CustomerFactory, layout: str
) -> None:
    fake = FakeLLMClient([_response("ok [1]")])
    service = _service(fake, db_session, FakeRetriever(CHUNKS), prompt_layout=layout)

    await service.collect(
        service.handle_message(ChatRequest(message=QUESTION), principal_of(await make_customer()))
    )

    request = fake.requests[0]
    assert request.system[0].cache_breakpoint is (layout == "cache_ordered")
    assert len(request.system) == 1
    assert '<excerpt index="1" title="Help article 1"' in request.messages[-1].content


async def test_retrieval_mode_none_keeps_phase_two_request(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    fake = FakeLLMClient([_response("Your plan renews monthly [1].")])
    retriever = FakeRetriever(CHUNKS)
    service = _service(fake, db_session, retriever, retrieval_mode="none")

    response = await service.collect(
        service.handle_message(ChatRequest(message=QUESTION), principal_of(await make_customer()))
    )

    assert retriever.calls == []
    request = fake.requests[0]
    assert [block.text for block in request.system] == [system_prompt("en")]
    assert [(m.role, m.content) for m in request.messages] == [("user", QUESTION)]
    assert response.citations == []


async def test_without_retriever_there_is_no_retrieval_and_no_citation(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    fake = FakeLLMClient([_response("Your plan renews monthly [1].")])
    service = _service(fake, db_session, None)

    response = await service.collect(
        service.handle_message(ChatRequest(message=QUESTION), principal_of(await make_customer()))
    )

    assert [block.text for block in fake.requests[0].system] == [system_prompt("en")]
    assert response.citations == []


async def test_unsupported_language_skips_retrieval(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    fake = FakeLLMClient([])
    retriever = FakeRetriever(CHUNKS)
    service = _service(fake, db_session, retriever)

    response = await service.collect(
        service.handle_message(ChatRequest(message=GERMAN), principal_of(await make_customer()))
    )

    assert retriever.calls == []
    assert fake.requests == []
    assert response.content == user_message(MessageKey.UNSUPPORTED_LANGUAGE, "en")


async def test_llm_failure_with_retrieval_keeps_only_the_user_message(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    fake = FakeLLMClient([LLMError("provider exploded: secret", recoverable=False, status=500)])
    service = _service(fake, db_session, FakeRetriever(CHUNKS))
    customer = await make_customer()
    customer_id = customer.id

    with pytest.raises(AtlasError) as info:
        await service.collect(
            service.handle_message(ChatRequest(message=QUESTION), principal_of(customer))
        )

    assert info.value.code is ErrorCode.LLM_UNAVAILABLE
    assert info.value.http_status == 502
    await db_session.rollback()
    conversation_ids = (
        await db_session.scalars(
            select(Conversation.id).where(Conversation.customer_id == customer_id)
        )
    ).all()
    assert len(conversation_ids) == 1
    messages = await ConversationRepository(db_session).list_messages(conversation_ids[0])
    assert [(m.role, m.content) for m in messages] == [("user", QUESTION)]
