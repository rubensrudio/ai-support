"""Chat service (CT-15). Phase 02: conversations with persisted history (TASK-031).

`handle_message` validates the message eagerly, when it is called and before it returns the
event stream, so a blank or too long message (CHAT-90, CHAT-91) raises `AtlasError` before any
LLM call and before an HTTP or SSE response starts. A missing principal raises
`AtlasError(AUTH_REQUIRED, 401)` right after.

The returned async iterator then:

1. opens the Customer's conversation (`get_owned`, 404 for a missing or foreign id, CHAT-94)
   or creates one when the request has no `conversation_id`;
2. detects the language of the message (CT-21), falling back to the UI language and then to
   the Customer's language;
3. persists the user message and **commits** (CHAT-03). The request session
   (`get_db_session`) rolls back when the request raises, so committing here is what keeps the
   user message when the LLM call fails afterwards (CHAT-92: only the user message stays);
4. language `other` → fixed English text (`UNSUPPORTED_LANGUAGE`) stored as the assistant
   reply, with no LLM call and no tools (CHAT-95);
5. otherwise builds the history window (CT-22) from the messages after
   `Conversation.summary_upto_message_id`, calls the LLM with the system prompt, the summary as
   an extra system block and the window, stores the reply (`status="complete"`) and the new
   summary when one was produced (CHAT-04, CHAT-06). The `{language}` placeholder of the
   system prompt is replaced with the name of the reply language (`system_prompt`);
6. **commits** the assistant reply (and the summary) and only then yields one `DoneEvent`
   with the conversation id and the assistant message id. FastAPI closes the request session
   after the response is sent, so without this commit a failed commit at teardown would leave
   the client with a `message_id` that does not exist, and a quick next turn could miss it.

Phase 03 (TASK-049) adds retrieval-augmented answers. With a `retriever` and
`settings.retrieval_mode != "none"`, step 5 first retrieves the top-k chunks for the message in
its detected language (RAG-03):

- `RetrievalUnavailable` → fixed `KNOWLEDGE_UNAVAILABLE` text in the reply language, stored as
  the assistant reply with `status="complete"`, with no LLM call (RAG-94);
- otherwise the request comes from `build_request` (CT-31) with the numbered chunks, in the
  layout of `settings.prompt_layout`. With no chunk (empty index, RAG-90) the LLM is still
  called and the request carries the instruction to say it does not know (RAG-04);
- the `[n]` markers of the answer become citations (CT-32): one `CitationEvent` per citation,
  after the commit and before the `DoneEvent`, folded into `ChatResponse.citations` by
  `collect`.

Without a retriever, or with `retrieval_mode="none"` (the "without RAG" mode of demo_03), the
request is the phase-02 one: system prompt, summary block and history window, no citations.

A provider failure (chat or summarisation) becomes `AtlasError(LLM_UNAVAILABLE, 502)` without
the provider text (CHAT-92), and no assistant message is stored.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.events import AgentEvent, CitationEvent, DoneEvent, ErrorEvent
from api.auth.deps import CurrentCustomer
from api.chat.context import HistoryWindow, LLMSummarizer, build_history, summary_block
from api.chat.language import detect_language
from api.chat.models import Conversation
from api.chat.prompt_builder import (
    LANGUAGE_NAMES,
    LANGUAGE_PLACEHOLDER,
    build_request,
    system_prompt,
)
from api.chat.repository import ConversationRepository
from api.chat.schemas import ChatRequest, ChatResponse, Citation
from api.config import Settings, get_settings
from api.db.engine import get_db_session, get_sessionmaker
from api.errors import AtlasError, ErrorCode
from api.llm.client import LLMClient, LLMError, LLMRequest, SystemBlock
from api.llm.factory import build_llm_client
from api.messages import Lang, MessageKey, user_message
from api.observability.logging import get_trace_id, log_event
from api.rag.citations import extract_citations
from api.rag.retriever import RetrievalUnavailable, RetrievedChunk, Retriever

__all__ = [
    "LANGUAGE_NAMES",
    "LANGUAGE_PLACEHOLDER",
    "UNSUPPORTED_LANGUAGE_REPLY_LANG",
    "ChatService",
    "get_chat_service",
    "system_prompt",
]

UNSUPPORTED_LANGUAGE_REPLY_LANG: Lang = "en"


class ChatService:
    def __init__(
        self,
        llm: LLMClient,
        settings: Settings,
        session: AsyncSession,
        retriever: Retriever | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.session = session
        self.retriever = retriever
        self.repository = ConversationRepository(session)

    def handle_message(
        self, request: ChatRequest, principal: CurrentCustomer | None
    ) -> AsyncIterator[AgentEvent]:
        """Validate `request` now (raises `AtlasError`), then return its event stream."""
        self._validate(request.message)
        if principal is None:
            raise AtlasError(ErrorCode.AUTH_REQUIRED, 401)
        return self._answer(request, principal)

    async def collect(self, events: AsyncIterator[AgentEvent]) -> ChatResponse:
        """Fold an event stream into the JSON response of `POST /chat`."""
        citations: list[Citation] = []
        async for event in events:
            if isinstance(event, ErrorEvent):
                raise _error_from_code(event.code)
            if isinstance(event, CitationEvent):
                citations.append(
                    Citation(
                        index=event.index,
                        document=event.document,
                        doc_key=event.doc_key,
                        section=event.section,
                        page=event.page,
                    )
                )
            if isinstance(event, DoneEvent):
                return ChatResponse(
                    conversation_id=event.conversation_id,
                    message_id=event.message_id,
                    status=event.status,
                    content=event.content,
                    language=event.language,
                    citations=citations,
                    confirmation=event.confirmation,
                    trace_id=event.trace_id,
                )
        log_event("chat_stream_without_terminal_event")
        raise AtlasError(ErrorCode.INTERNAL_ERROR, 500)

    def _validate(self, message: str) -> None:
        if not message.strip():
            raise AtlasError(ErrorCode.VALIDATION_ERROR, 422)
        max_chars = self.settings.max_message_chars
        if len(message) > max_chars:
            raise AtlasError(ErrorCode.MESSAGE_TOO_LONG, 422, params={"max_chars": max_chars})

    async def _conversation(self, request: ChatRequest, principal: CurrentCustomer) -> Conversation:
        if request.conversation_id is None:
            return await self.repository.create(principal.id)
        return await self.repository.get_owned(request.conversation_id, principal.id)

    async def _answer(
        self, request: ChatRequest, principal: CurrentCustomer
    ) -> AsyncIterator[AgentEvent]:
        trace_id = get_trace_id()
        conversation = await self._conversation(request, principal)
        fallback: Lang = request.ui_language or principal.language
        detected = detect_language(request.message, fallback=fallback)
        await self.repository.append_message(
            conversation.id, "user", request.message, "complete", detected, trace_id
        )
        await self.session.commit()

        citations: list[Citation] = []
        if detected == "other":
            content = user_message(MessageKey.UNSUPPORTED_LANGUAGE, UNSUPPORTED_LANGUAGE_REPLY_LANG)
            reply_language: Lang = UNSUPPORTED_LANGUAGE_REPLY_LANG
            log_event("chat_unsupported_language", conversation_id=str(conversation.id))
        else:
            reply_language = detected
            try:
                chunks = await self._retrieve(request.message, reply_language)
            except RetrievalUnavailable as exc:
                log_event(
                    "chat_knowledge_unavailable",
                    conversation_id=str(conversation.id),
                    exc_type=type(exc).__name__,
                )
                content = user_message(MessageKey.KNOWLEDGE_UNAVAILABLE, reply_language)
            else:
                content = await self._llm_reply(
                    conversation, reply_language, request.message, chunks
                )
                if chunks is not None:
                    citations = extract_citations(content, chunks)

        reply = await self.repository.append_message(
            conversation.id, "assistant", content, "complete", reply_language, trace_id
        )
        await self.session.commit()
        for citation in citations:
            yield CitationEvent(
                index=citation.index,
                document=citation.document,
                doc_key=citation.doc_key,
                section=citation.section,
                page=citation.page,
            )
        yield DoneEvent(
            status="completed",
            content=content,
            language=reply_language,
            trace_id=trace_id,
            conversation_id=conversation.id,
            message_id=reply.id,
        )

    async def _retrieve(self, message: str, language: Lang) -> list[RetrievedChunk] | None:
        """Top-k chunks for `message`, or `None` when retrieval is off (no retriever or mode
        `none`). Raises `RetrievalUnavailable` when the search fails (RAG-94)."""
        if self.retriever is None or self.settings.retrieval_mode == "none":
            return None
        chunks = await self.retriever.retrieve(message, language)
        log_event(
            "chat_retrieval",
            mode=self.settings.retrieval_mode,
            language=language,
            chunks=len(chunks),
        )
        return chunks

    def _request(
        self,
        window: HistoryWindow,
        language: Lang,
        message: str,
        chunks: list[RetrievedChunk] | None,
    ) -> LLMRequest:
        if chunks is None:
            system = [SystemBlock(system_prompt(language))]
            if window.summary:
                system.append(summary_block(window.summary))
            return LLMRequest(system=system, messages=window.messages, purpose="chat")
        # The window ends with the user message persisted for this turn; `build_request`
        # appends it itself, after the excerpts, in its `<customer_question>` tag.
        history = window
        last = window.messages[-1] if window.messages else None
        if last is not None and last.role == "user" and last.content == message:
            history = replace(window, messages=window.messages[:-1])
        return build_request(
            language=language,
            history=history,
            chunks=chunks,
            user_message=message,
            tools=[],
            layout=self.settings.prompt_layout,
        )

    async def _llm_reply(
        self,
        conversation: Conversation,
        language: Lang,
        message: str,
        chunks: list[RetrievedChunk] | None,
    ) -> str:
        upto = conversation.summary_upto_message_id
        pending = [
            message
            for message in await self.repository.list_messages(conversation.id)
            if upto is None or message.id > upto
        ]
        try:
            window = await build_history(
                pending,
                conversation.summary,
                self.settings.context_budget_tokens,
                LLMSummarizer(self.llm),
                mode=self.settings.history_mode,
            )
            response = await self.llm.complete(self._request(window, language, message, chunks))
        except LLMError as exc:
            log_event(
                "chat_llm_unavailable",
                exc_type=type(exc).__name__,
                recoverable=exc.recoverable,
                status=exc.status,
            )
            raise AtlasError(ErrorCode.LLM_UNAVAILABLE, 502) from exc
        log_event(
            "chat_context_window",
            conversation_id=str(conversation.id),
            history_tokens=window.tokens,
            window_messages=len(window.messages),
            summarized_upto=window.summarized_upto,
        )
        if window.summarized_upto is not None and window.summary:
            await self.repository.save_summary(
                conversation.id, window.summary, window.summarized_upto
            )
        return response.text


def _error_from_code(code: str) -> AtlasError:
    try:
        error_code = ErrorCode(code)
    except ValueError:
        error_code = ErrorCode.INTERNAL_ERROR
    return AtlasError(error_code, error_code.http_status or 500)


@lru_cache(maxsize=1)
def _llm_client() -> LLMClient:
    """One LLM client per process, built from the settings on first use."""
    return build_llm_client(get_settings())


def get_chat_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ChatService:
    """FastAPI dependency: the request session, the cached LLM client and a retriever.

    The retriever opens its own sessions (`get_sessionmaker`); no embedder in phase 03.
    Tests override it with `app.dependency_overrides`.
    """
    settings = get_settings()
    retriever = Retriever(get_sessionmaker(), None, settings)
    return ChatService(_llm_client(), settings, session, retriever=retriever)
