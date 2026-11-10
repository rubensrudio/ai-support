"""Conversational chat service with persisted history (TASK-031, CHAT-03/04/06/10/93/95)."""

import uuid
from collections.abc import AsyncIterator

import pytest
from chat_helpers import CustomerFactory, create_customer, principal_of
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.events import DoneEvent, StartEvent, TokenEvent
from api.auth.models import Customer
from api.chat.context import summary_block
from api.chat.prompt_builder import grounded_system_prompt, language_instruction
from api.chat.repository import ConversationRepository
from api.chat.schemas import ChatRequest
from api.chat.service import ChatService
from api.config import Settings
from api.db.engine import get_sessionmaker
from api.errors import AtlasError, ErrorCode
from api.llm.client import (
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMTimeoutError,
    Usage,
)
from api.llm.fake import FakeLLMClient
from api.llm.tokens import count_messages
from api.messages import Lang, MessageKey, user_message
from api.observability.logging import bind_trace_id, reset_trace_id
from api.tools.catalog import TOOL_NAMES

pytestmark = pytest.mark.integration

GERMAN = "Ich möchte meine Bestellung von gestern stornieren, bitte helfen Sie mir dabei."


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model",
        provider="fake",
        stop_reason="end_turn",
    )


def _system(language: Lang) -> list[str]:
    """Phase 11 (TASK-110): stable block (cache breakpoint), then the reply-language block."""
    return [grounded_system_prompt(), language_instruction(language)]


def _service(fake: FakeLLMClient, session: AsyncSession, **settings: object) -> ChatService:
    return ChatService(fake, Settings(llm_provider="fake", **settings), session)  # type: ignore[arg-type]


async def test_second_message_sends_history_with_name(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    principal = principal_of(await make_customer())
    fake = FakeLLMClient([_response("Nice to meet you, Rubens."), _response("You are Rubens.")])
    service = _service(fake, db_session)

    first = await service.collect(
        service.handle_message(ChatRequest(message="Hi, my name is Rubens."), principal)
    )
    second = await service.collect(
        service.handle_message(
            ChatRequest(message="What is my name?", conversation_id=first.conversation_id),
            principal,
        )
    )

    assert second.conversation_id == first.conversation_id
    assert len(fake.requests) == 2
    sent = [(m.role, m.content) for m in fake.requests[1].messages]
    assert sent == [
        ("user", "Hi, my name is Rubens."),
        ("assistant", "Nice to meet you, Rubens."),
        ("user", "What is my name?"),
    ]
    assert any("Rubens" in content for _, content in sent[:-1])
    assert [block.text for block in fake.requests[1].system] == _system("en")
    assert [block.cache_breakpoint for block in fake.requests[1].system] == [True, False]
    assert fake.requests[1].purpose == "chat"
    # Phase 06 (TASK-077): with agent tools enabled (default) the chat request carries them.
    assert [spec.name for spec in fake.requests[1].tools] == list(TOOL_NAMES)


async def test_turn_persists_user_then_assistant(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    fake = FakeLLMClient([_response("Your plan renews monthly.")])
    service = _service(fake, db_session)
    token = bind_trace_id("trace-xyz")
    try:
        events = [
            event
            async for event in service.handle_message(
                ChatRequest(message="When does my subscription plan renew?"),
                principal_of(customer),
            )
            # Phase 09: `start` and `token` events are covered by test_streaming_cancel.py.
            if not isinstance(event, TokenEvent | StartEvent)
        ]
    finally:
        reset_trace_id(token)

    assert len(events) == 1
    done = events[0]
    assert isinstance(done, DoneEvent)
    assert done.status == "completed"
    assert done.content == "Your plan renews monthly."
    assert done.trace_id == "trace-xyz"
    assert done.language == "en"
    assert done.conversation_id is not None

    repo = ConversationRepository(db_session)
    conversation = await repo.get_owned(done.conversation_id, customer.id)
    messages = await repo.list_messages(conversation.id)
    assert [(m.role, m.content, m.status) for m in messages] == [
        ("user", "When does my subscription plan renew?", "complete"),
        ("assistant", "Your plan renews monthly.", "complete"),
    ]
    assert done.message_id == messages[1].id
    assert {m.trace_id for m in messages} == {"trace-xyz"}
    assert [m.language for m in messages] == ["en", "en"]


async def test_language_detected_from_message_overrides_ui(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    fake = FakeLLMClient([_response("Claro, posso ajudar.")])
    service = _service(fake, db_session)

    response = await service.collect(
        service.handle_message(
            ChatRequest(message="Quero cancelar meu pedido de ontem", ui_language="en"),
            principal_of(await make_customer()),
        )
    )

    assert response.language == "pt"


async def test_short_message_uses_ui_language_then_customer_language(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    principal = principal_of(await make_customer(language="pt"))
    fake = FakeLLMClient([_response("ok"), _response("ok")])
    service = _service(fake, db_session)

    with_ui = await service.collect(
        service.handle_message(ChatRequest(message="ok", ui_language="en"), principal)
    )
    without_ui = await service.collect(service.handle_message(ChatRequest(message="ok"), principal))

    assert with_ui.language == "en"
    assert without_ui.language == "pt"


async def test_german_message_gets_fixed_english_text_without_llm(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer(language="pt")
    fake = FakeLLMClient([_response("unused")])
    service = _service(fake, db_session)

    response = await service.collect(
        service.handle_message(ChatRequest(message=GERMAN), principal_of(customer))
    )

    assert fake.requests == []
    assert response.content == user_message(MessageKey.UNSUPPORTED_LANGUAGE, "en")
    assert response.language == "en"
    assert response.status == "completed"
    assert response.conversation_id is not None
    messages = await ConversationRepository(db_session).list_messages(response.conversation_id)
    assert [(m.role, m.language) for m in messages] == [("user", "other"), ("assistant", "en")]
    assert response.message_id == messages[1].id


@pytest.mark.parametrize(
    "error",
    [
        LLMError("provider exploded: secret detail", recoverable=False, status=500),
        LLMTimeoutError(),
    ],
)
async def test_llm_error_keeps_only_user_message(
    db_session: AsyncSession, make_customer: CustomerFactory, error: LLMError
) -> None:
    customer = await make_customer()
    fake = FakeLLMClient([_response("Hello there."), error])
    service = _service(fake, db_session)
    principal = principal_of(customer)
    first = await service.collect(
        service.handle_message(ChatRequest(message="Hello, I need help."), principal)
    )
    assert first.conversation_id is not None

    with pytest.raises(AtlasError) as info:
        await service.collect(
            service.handle_message(
                ChatRequest(message="Are you there?", conversation_id=first.conversation_id),
                principal,
            )
        )

    assert info.value.code is ErrorCode.LLM_UNAVAILABLE
    assert info.value.http_status == 502
    assert info.value.params == {}
    assert "secret detail" not in str(info.value)
    # The user message was committed before the LLM call, so a rollback keeps it.
    await db_session.rollback()
    messages = await ConversationRepository(db_session).list_messages(first.conversation_id)
    assert [(m.role, m.content) for m in messages] == [
        ("user", "Hello, I need help."),
        ("assistant", "Hello there."),
        ("user", "Are you there?"),
    ]


async def test_foreign_conversation_is_not_found(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    owner = principal_of(await make_customer())
    intruder = principal_of(await make_customer())
    fake = FakeLLMClient([_response("Hi.")])
    service = _service(fake, db_session)
    first = await service.collect(service.handle_message(ChatRequest(message="Hi"), owner))

    for conversation_id in (first.conversation_id, uuid.uuid4()):
        with pytest.raises(AtlasError) as info:
            await service.collect(
                service.handle_message(
                    ChatRequest(message="Hi", conversation_id=conversation_id), intruder
                )
            )
        assert info.value.code is ErrorCode.CONVERSATION_NOT_FOUND
        assert info.value.http_status == 404
    assert len(fake.requests) == 1


class PurposeFake(FakeLLMClient):
    """Answers summarisation calls with a numbered summary and chat calls with a fixed text."""

    def __init__(self) -> None:
        super().__init__([])
        self.summaries = 0

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        # Phase 09: answers like a provider without streaming, so `complete` serves every call.
        raise NotImplementedError

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.purpose == "summarize":
            self.summaries += 1
            return _response(f"Summary number {self.summaries}.")
        return _response("Noted.")


LONG_TEXT = "Please remember the detail number {n} about my subscription plan. " * 6


async def test_long_conversation_is_summarized_and_summary_saved(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    customer = await make_customer()
    principal = principal_of(customer)
    fake = PurposeFake()
    service = _service(fake, db_session, context_budget_tokens=300)

    conversation_id = None
    for n in range(6):
        response = await service.collect(
            service.handle_message(
                ChatRequest(message=LONG_TEXT.format(n=n), conversation_id=conversation_id),
                principal,
            )
        )
        conversation_id = response.conversation_id
    assert conversation_id is not None

    assert fake.summaries >= 1
    chat_requests = [request for request in fake.requests if request.purpose == "chat"]
    assert len(chat_requests) == 6
    last = chat_requests[-1]
    assert last.system[0].text == grounded_system_prompt()
    assert len(last.system) == 2
    # The summary shares the variable block with the reply language (DA-23).
    summary = summary_block(f"Summary number {fake.summaries}.")
    assert last.system[1].text == f"{language_instruction('en')}\n\n{summary.text}"
    assert last.messages[-1].content == LONG_TEXT.format(n=5)
    # The history budget covers the summary and the messages, not the fixed instructions.
    assert count_messages([summary], last.messages) <= 300

    conversation = await ConversationRepository(db_session).get_owned(conversation_id, customer.id)
    await db_session.refresh(conversation)
    assert conversation.summary == f"Summary number {fake.summaries}."
    assert conversation.summary_upto_message_id is not None
    # Folded messages are not sent again: the window starts after `summary_upto_message_id`.
    folded = [
        m.content
        for m in await ConversationRepository(db_session).list_messages(conversation_id)
        if m.id <= conversation.summary_upto_message_id and m.role == "user"
    ]
    assert folded
    assert not any(m.content in folded for m in last.messages if m.role == "user")


async def test_full_history_mode_never_summarizes(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    principal = principal_of(await make_customer())
    fake = FakeLLMClient([])
    service = _service(fake, db_session, context_budget_tokens=300, history_mode="full")

    conversation_id = None
    for n in range(4):
        fake.enqueue(_response(f"Noted detail {n}."))
        response = await service.collect(
            service.handle_message(
                ChatRequest(message=LONG_TEXT.format(n=n), conversation_id=conversation_id),
                principal,
            )
        )
        conversation_id = response.conversation_id

    assert [request.purpose for request in fake.requests] == ["chat"] * 4
    assert len(fake.requests[-1].messages) == 7
    # No summary: the variable block carries only the reply language (TASK-110).
    assert [block.text for block in fake.requests[-1].system] == _system("en")


@pytest.mark.parametrize(
    ("message", "language", "language_name"),
    [
        ("I want to cancel my order from yesterday", "en", "English"),
        ("Quero cancelar meu pedido de ontem", "pt", "Brazilian Portuguese"),
    ],
)
async def test_system_prompt_names_the_reply_language(
    db_session: AsyncSession,
    make_customer: CustomerFactory,
    message: str,
    language: Lang,
    language_name: str,
) -> None:
    fake = FakeLLMClient([_response("ok")])
    service = _service(fake, db_session)

    await service.collect(
        service.handle_message(ChatRequest(message=message), principal_of(await make_customer()))
    )

    stable, variable = (block.text for block in fake.requests[0].system)
    # Phase 11 (TASK-110, DA-23): the language is outside the stable (cached) block.
    assert language_name not in stable
    assert "{language}" not in stable + variable
    assert f"Always reply in {language_name}," in variable
    assert [stable, variable] == _system(language)


async def test_naive_layout_puts_date_time_and_language_on_top(
    db_session: AsyncSession, make_customer: CustomerFactory
) -> None:
    fake = FakeLLMClient([_response("ok")])
    service = _service(fake, db_session, prompt_layout="naive")

    await service.collect(
        service.handle_message(
            ChatRequest(message="Quero cancelar meu pedido de ontem"),
            principal_of(await make_customer()),
        )
    )

    (block,) = fake.requests[0].system
    assert block.cache_breakpoint is False
    assert block.text.startswith("Current date and time (UTC): ")
    assert block.text.index("Always reply in Brazilian Portuguese,") < block.text.index(
        grounded_system_prompt()
    )


async def test_assistant_reply_is_committed_before_done_event(migrated_database: str) -> None:
    """A fresh session sees the reply as soon as `collect` returns, before the request ends."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as setup:
        customer = await create_customer(setup)
        await setup.commit()
        customer_id = customer.id
        principal = principal_of(customer)

    fake = PurposeFake()
    try:
        async with sessionmaker() as request_session:
            service = _service(fake, request_session, context_budget_tokens=300)
            conversation_id = None
            for n in range(6):
                response = await service.collect(
                    service.handle_message(
                        ChatRequest(message=LONG_TEXT.format(n=n), conversation_id=conversation_id),
                        principal,
                    )
                )
                conversation_id = response.conversation_id
                assert conversation_id is not None
                async with sessionmaker() as fresh:
                    repo = ConversationRepository(fresh)
                    messages = await repo.list_messages(conversation_id)
                    conversation = await repo.get_owned(conversation_id, customer_id)
                assert messages[-1].role == "assistant"
                assert messages[-1].id == response.message_id
                if fake.summaries:
                    assert conversation.summary == f"Summary number {fake.summaries}."
            assert fake.summaries >= 1
            # Nothing commits this session on exit: the service already did.
    finally:
        async with sessionmaker() as cleanup:
            await cleanup.execute(delete(Customer).where(Customer.id == customer_id))
            await cleanup.commit()
