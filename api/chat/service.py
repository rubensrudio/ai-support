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
   `Conversation.summary_upto_message_id`, calls the LLM with the system blocks, the summary
   and the window, stores the reply (`status="complete"`) and the new summary when one was
   produced (CHAT-04, CHAT-06). Since phase 11 (TASK-110) the system blocks come from
   `api.chat.prompt_builder` in the layout of `settings.prompt_layout`: with `cache_ordered` a
   stable block with the cache breakpoint, then a variable block with the reply language and
   the summary (DA-23);
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
request is the phase-02 one (`build_base_request`): the same system blocks and stable prefix as
the RAG request, then the history window, no citations.

A provider failure (chat or summarisation) becomes `AtlasError(LLM_UNAVAILABLE, 502)` without
the provider text (CHAT-92), and no assistant message is stored.

Phase 12 (TASK-121, RES-06, RES-91..93): the chat client is the resilient chain of
`api.llm.factory` (retry, breaker and fallback to the secondary provider). When it raises
`AllProvidersFailed` (every provider failed, or every circuit was open and none was called) on
any model call of the turn (chat, summarisation, the answer after a confirmation; JSON and
stream alike), the turn ends with `user_message(DEGRADED_RESPONSE, language)` stored with
`status='degraded'` and a `done{status: 'degraded'}`; `open_ticket(reason='all_providers_failed',
trace_id)` runs in the same transaction, committed with the answer, and `chat_degraded` is
logged. Tools that already ran in the turn keep their effects and audit rows and are never run
again: the chain only wraps model calls (RES-91). A failure after the first streamed event is not
an `AllProvidersFailed` (the chain does not switch provider then, RES-93): it is still the
`LLMError` above, i.e. an `error` event on the stream, and the customer may resend.

Phase 06 (TASK-077) runs the agent loop (CT-45) when `settings.agent_tools_enabled` and the
provider takes tools (`codex_cli` does not: it runs the plain chat and logs
`chat_tools_unsupported_by_provider` once per process):

- the request of step 5 carries `registry.specs()` as tools and goes to `AgentLoop.run` with a
  `ToolContext` built **only from the session principal** (customer id and e-mail, trace id,
  request session, reply language, now). `tool_start`/`tool_end` are forwarded as they come;
  the loop's final `done` is replaced, after the commit, by one with the stored `message_id`;
- a destructive call suspends the loop (TOOL-05): `suspend` stores a `PendingConfirmation`
  with the serialised `AgentState` and returns the `ConfirmationPrompt` (`target='#<order_id>'`;
  for `issue_refund` the amount comes from `check_refund_policy` on the customer's own order,
  only when eligible, never from the model). The assistant turn is stored with the confirmation
  question (`messages.status` stays `complete`: the column has no `awaiting_confirmation` value, the
  `done` carries that status);
- a new message in a conversation with a pending confirmation refuses it first
  (`refuse_pending`, TOOL-97) and stores, before the user message, an assistant note saying the
  action was refused and not executed, so the model sees the refusal in the history;
- `resolve_confirmation` resolves the confirmation (`ConfirmationStore.resolve`: 404 missing or
  another customer's, 409 not pending or expired; an expired row is committed as `expired`
  before the error), resumes the loop from the stored state with the current session's
  `ToolContext` and stores the answer like `handle_message`. The resolution, the tool effects
  and the answer are committed together, before the `done`: if the model fails after the
  tool ran, everything rolls back and the confirmation is still pending.

Phase 09 (TASK-094, STR-01, STR-03, STR-04, STR-07, STR-91, STR-92, LAC-16): the agent loop
streams, so the service forwards its `token` events as they come: a turn with tools emits
`tool_start, tool_end, token+, citation, done` (citations after the commit, before `done`, as
before). The plain chat path (tools off or a provider without tools) still makes one
`complete()` call and emits no `token` (its `done.content` carries the answer).

Round 1: every stream of `handle_message` and `resolve_confirmation` starts with a
`StartEvent{conversation_id, trace_id}`, emitted once the user message is committed and the
conversation exists (for a confirmation, once it is resolved, with its conversation id), before
any token or tool event; `collect` ignores it.

Cancellation: when the stream is cancelled while the agent loop runs (`asyncio.CancelledError`
raised inside the generator, e.g. the task is cancelled while the model streams) or the consumer
closes it early (`GeneratorExit` from `aclose()`, which the SSE route does on disconnect), the
service, before re-raising:
- stores the partial answer, the text streamed by the model call in progress
  (`AgentLoop.partial_text`: not the preamble of earlier calls that asked for tools, nor a
  text discarded by a limit), as an assistant message with `status='cancelled'` (it is part of
  the history of the next turns); nothing is stored when that text is empty, so the history
  never gets an empty assistant turn;
- commits, so the tools that already ran keep their effects and audit rows, together with the
  partial (on the confirmation path the resolution is committed as well);
- emits nothing (no `done`).
On the confirmation path (round 3) the resolution is committed only once the confirmed tool
has run (`AgentLoop.resumed_call_settled`): a cancellation before that (right after `start`, or
at the confirmed call's `tool_start`) rolls back under the same shield, so the confirmation
goes back to pending, the order is untouched and nothing is audited; it is never committed at
teardown as `confirmed` without its tool. A refusal settles at once (no tool runs).
The persistence runs inside `anyio.CancelScope(shield=True)`: Starlette cancels a streaming
response through an anyio cancel scope, which re-delivers the cancellation at every `await`; the
shield lets the commit finish on the request session (no second session is needed and no task
outlives the request). A failure while persisting is logged (`chat_cancel_persist_failed`) and
the cancellation still propagates. Once the answer is committed (the first event of `_finish`),
a cancellation changes nothing (STR-92).

Phase 10 guardrails (TASK-107, GRD-01, GRD-04, GRD-06, GRD-07, GRD-08, GRD-91, GRD-93, OBS-91),
only when `settings.guardrails_enabled` (without it everything above is unchanged):

- input: `InputGuard.check` runs on the message before anything is persisted and before any LLM
  call. From then on only its `masked_text` is used: the stored user message, the language
  detection, the history, the retrieval query, the prompt and the logs (`chat_message` carries
  the masked text, never the original). A `block` (an injection, or a guard failure) stores the
  masked user message and the standard reply (`GUARDRAIL_BLOCKED` in the conversation language),
  both with `status='blocked'`, logs `guardrail_blocked{stage: 'input', reasons}` (never the
  text) and ends with `start` and `done{status: 'blocked'}`, with no LLM call. Blocked messages
  are left out of the history window of later turns, so an attack never reaches the model later;
- output: the agent loop gets a `StreamOutputFilter(OutputGuard(guardrail_llm), OutputContext)`
  (see `api/agent/loop.py`): only approved sentences become `token` events, and a block ends the
  turn with `done{status: 'blocked'}` and the standard message, stored with `status='blocked'`
  in place of the whole answer. The `OutputContext` holds the customer's own order ids (read from
  the database), which of them are cancelled or have a refund (round 1, so stating that state is
  not a promise), the order numbers cited in the masked question (`question_order_ids`, which the
  answer may repeat), the session e-mail, the masked question, the reply language and the tools
  with a `success` audit row in the request's trace ids (`run` and, after a confirmation, the
  earlier requests of the turn); the orders and the tools are read again after every
  `tool_end`. The plain chat path (no tools) puts its whole answer through the same filter. The
  confirmation path (`resolve_confirmation`) is guarded the same way, with the last user message
  of the conversation as the question. A cancelled stream stores as partial only the text the
  filter already released;
- `guardrail_llm` runs the topic check. `get_chat_service` passes a client built for
  `ATLAS_GUARDRAIL_MODEL` when it is set (one per process); otherwise the chat client is used,
  except when the chat client is the offline fake provider (a `FakeLLMClient`, or a client whose
  `provider` is `fake`, possibly behind `inner` wrappers: tests and `--provider fake` demos): its
  script holds only chat answers, so the topic check answers "on topic" offline
  (`OfflineTopicClient`). A test that exercises the classifier injects `guardrail_llm`, or hands
  a chat client of its own (no `fake` provider) that also answers the classifier calls.
"""

import asyncio
import contextlib
import contextvars
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cache, lru_cache
from typing import Annotated, Any, cast

import anyio
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.confirmations import ConfirmationStore
from api.agent.events import (
    AgentEvent,
    ChatStatus,
    CitationEvent,
    ConfirmationPrompt,
    DoneEvent,
    ErrorEvent,
    StartEvent,
    ToolEndEvent,
)
from api.agent.loop import (
    AgentLoop,
    AgentState,
    AgentTurn,
    Decision,
    SuspendFn,
    aclose_stream,
    closing_stream,
    state_trace_ids,
)
from api.agent.models import PendingConfirmation
from api.auth.deps import CurrentCustomer
from api.chat.context import HistoryWindow, LLMSummarizer, build_history
from api.chat.language import DetectedLanguage, detect_language
from api.chat.models import Conversation
from api.chat.prompt_builder import (
    LANGUAGE_NAMES,
    build_base_request,
    build_request,
)
from api.chat.repository import ConversationRepository
from api.chat.schemas import ChatRequest, ChatResponse, Citation
from api.config import Settings, get_settings
from api.db.engine import get_db_session, get_sessionmaker
from api.errors import AtlasError, ErrorCode
from api.guardrails.input_guard import InputGuard, InputVerdict
from api.guardrails.output_guard import OutputContext, OutputGuard
from api.guardrails.stream_filter import StreamOutputFilter
from api.llm.client import (
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
)
from api.llm.factory import build_llm_client
from api.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient
from api.llm.fallback import AllProvidersFailed
from api.messages import Lang, MessageKey, user_message
from api.observability.logging import get_trace_id, log_event
from api.observability.tracing import bind_session_id, reset_session_id
from api.rag.citations import extract_citations
from api.rag.embeddings import EmbeddingClient, OpenAIEmbeddingClient, build_embedder
from api.rag.retriever import RetrievalUnavailable, RetrievedChunk, Retriever, is_vector_mode
from api.tickets.service import open_ticket
from api.tools.audit import list_successful_actions
from api.tools.catalog import build_default_registry
from api.tools.models import CURRENCY, Order, OrderStatus, Refund
from api.tools.refunds import CheckRefundArgs, check_refund_policy
from api.tools.registry import ToolContext, ToolRegistry

__all__ = [
    "LANGUAGE_NAMES",
    "UNSUPPORTED_LANGUAGE_REPLY_LANG",
    "ChatService",
    "get_chat_service",
]

UNSUPPORTED_LANGUAGE_REPLY_LANG: Lang = "en"

# `Ticket.reason` of a turn answered with the degraded message (RES-06, RES-92).
ALL_PROVIDERS_FAILED = "all_providers_failed"

# Providers whose adapter rejects tool definitions (`LLMError`, not recoverable): the chat runs
# without tools for them, as with `agent_tools_enabled=False`.
PROVIDERS_WITHOUT_TOOLS: frozenset[str] = frozenset({"codex_cli"})


@cache
def _log_tools_unsupported(provider: str) -> None:
    """Logged once per process and provider."""
    log_event("chat_tools_unsupported_by_provider", provider=provider)


# Action names of the confirmation question stored as the assistant turn (`CONFIRM_ACTION`).
_ACTION_NAMES: dict[str, dict[Lang, str]] = {
    "cancel_order": {"pt": "cancelar o pedido", "en": "cancel order"},
    "issue_refund": {"pt": "reembolsar o pedido", "en": "refund order"},
}

# TOOL-97: history note stored when a new message refuses a pending confirmation. It is for the
# model (the conversation language does not matter), so it stays in English.
REFUSED_BY_NEW_MESSAGE_NOTE = (
    "[The customer sent a new message instead of confirming {tool} {target}: the action was "
    "refused and was not executed.]"
)


class ChatService:
    def __init__(
        self,
        llm: LLMClient,
        settings: Settings,
        session: AsyncSession,
        retriever: Retriever | None = None,
        registry: ToolRegistry | None = None,
        guardrail_llm: LLMClient | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.session = session
        self.retriever = retriever
        self.registry = registry if registry is not None else build_default_registry()
        self.repository = ConversationRepository(session)
        self.confirmations = ConfirmationStore(session, settings)
        self.guardrail_llm = (
            guardrail_llm if guardrail_llm is not None else _default_guardrail_llm(llm)
        )

    @property
    def tools_enabled(self) -> bool:
        """Agent tools on, unless the provider cannot take tools (then the plain chat runs)."""
        if not self.settings.agent_tools_enabled:
            return False
        if self.settings.llm_provider in PROVIDERS_WITHOUT_TOOLS:
            _log_tools_unsupported(self.settings.llm_provider)
            return False
        return True

    def handle_message(
        self, request: ChatRequest, principal: CurrentCustomer | None
    ) -> AsyncIterator[AgentEvent]:
        """Validate `request` now (raises `AtlasError`), then return its event stream."""
        self._validate(request.message)
        if principal is None:
            raise AtlasError(ErrorCode.AUTH_REQUIRED, 401)
        return self._answer(request, principal)

    def resolve_confirmation(
        self, confirmation_id: uuid.UUID, decision: Decision, customer: CurrentCustomer
    ) -> AsyncIterator[AgentEvent]:
        """Confirm or refuse a pending destructive call and resume the agent loop (TOOL-05,
        TOOL-13). Raises `AtlasError` 404/409 from `ConfirmationStore.resolve` when iterated."""
        return self._resume(confirmation_id, decision, customer)

    async def collect(self, events: AsyncIterator[AgentEvent]) -> ChatResponse:
        """Fold an event stream into the JSON response of `POST /chat`."""
        citations: list[Citation] = []
        async with closing_stream(events) as stream:
            return await self._fold(stream, citations)

    async def _fold(
        self, events: AsyncIterator[AgentEvent], citations: list[Citation]
    ) -> ChatResponse:
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
        # GRD-01/GRD-07: checked before anything is stored or sent; from here on only the
        # masked text is used (`message`), never `request.message`.
        verdict = self._input_verdict(request.message)
        message = verdict.masked_text if verdict is not None else request.message
        # `get_owned` already checked that the conversation is the principal's, so refusing its
        # pending confirmations cannot touch another customer's (`refuse_pending` takes no owner).
        conversation = await self._conversation(request, principal)
        session_token = bind_session_id(str(conversation.id))
        try:
            if request.conversation_id is not None:
                await self._refuse_pending(conversation, trace_id)
            fallback: Lang = request.ui_language or principal.language
            detected = detect_language(message, fallback=fallback)
            if verdict is not None and verdict.action == "block":
                async for event in self._block_input(
                    conversation.id, message, detected, fallback, verdict, trace_id
                ):
                    yield event
                return
            await self.repository.append_message(
                conversation.id, "user", message, "complete", detected, trace_id
            )
            await self.session.commit()
            self._log_message(conversation.id, message, detected)
            # Round 1: the first event, once the user message is committed and the conversation
            # exists, before any token or tool (the JSON `collect` ignores it).
            yield StartEvent(conversation_id=conversation.id, trace_id=trace_id)

            if detected == "other":
                content = user_message(
                    MessageKey.UNSUPPORTED_LANGUAGE, UNSUPPORTED_LANGUAGE_REPLY_LANG
                )
                log_event("chat_unsupported_language", conversation_id=str(conversation.id))
                async for event in self._finish(
                    conversation.id, content, UNSUPPORTED_LANGUAGE_REPLY_LANG, trace_id
                ):
                    yield event
                return

            reply_language: Lang = detected
            try:
                chunks = await self._retrieve(message, reply_language)
            except RetrievalUnavailable as exc:
                log_event(
                    "chat_knowledge_unavailable",
                    conversation_id=str(conversation.id),
                    exc_type=type(exc).__name__,
                )
                content = user_message(MessageKey.KNOWLEDGE_UNAVAILABLE, reply_language)
                async for event in self._finish(conversation.id, content, reply_language, trace_id):
                    yield event
                return

            guard = await self._output_guardrail(principal, message, reply_language, [trace_id])
            if not self.tools_enabled:
                answer = self._plain_answer(
                    conversation, message, reply_language, chunks, guard, trace_id
                )
            else:
                answer = self._agent_answer(
                    conversation, principal, message, reply_language, chunks, guard, trace_id
                )
            answer = self._degrade_on_failure(
                answer, conversation.id, principal.id, reply_language, trace_id
            )
            async with closing_stream(answer) as stream:
                async for event in stream:
                    yield event
        finally:
            _reset_session_id(session_token)

    async def _plain_answer(
        self,
        conversation: Conversation,
        message: str,
        language: Lang,
        chunks: list[RetrievedChunk] | None,
        guard: "_OutputGuardrail | None",
        trace_id: str,
    ) -> AsyncIterator[AgentEvent]:
        """One `complete()` call (tools off or a provider without tools), its whole answer put
        through the output filter when the guardrails are on."""
        content = await self._llm_reply(conversation, language, message, chunks)
        status: ChatStatus = "completed"
        if guard is not None:
            released = await guard.check_text(content)
            if released is None:
                content, status, chunks = _blocked_text(language), "blocked", None
            else:
                content = released
        async for event in self._finish(
            conversation.id, content, language, trace_id, chunks=chunks, status=status
        ):
            yield event

    async def _agent_answer(
        self,
        conversation: Conversation,
        principal: CurrentCustomer,
        message: str,
        language: Lang,
        chunks: list[RetrievedChunk] | None,
        guard: "_OutputGuardrail | None",
        trace_id: str,
    ) -> AsyncIterator[AgentEvent]:
        """The agent loop (CT-45) with the tools, driven and stored by `_drive`."""
        window = await self._window(conversation)
        llm_request = self._request(window, language, message, chunks, self.registry.specs())
        ctx = self._tool_context(principal, trace_id, language)
        turn = AgentTurn(
            request=llm_request,
            ctx=ctx,
            suspend=self._suspend_fn(conversation.id, ctx),
            conversation_id=conversation.id,
        )
        loop = self._loop(guard)
        driven = self._drive(
            loop.run(turn),
            conversation.id,
            chunks,
            partial=lambda: loop.partial_text,
            language=language,
            trace_id=trace_id,
            guard=guard,
        )
        async with closing_stream(driven) as stream:
            async for event in stream:
                yield event

    async def _resume(
        self, confirmation_id: uuid.UUID, decision: Decision, customer: CurrentCustomer
    ) -> AsyncIterator[AgentEvent]:
        trace_id = get_trace_id()
        driving = False
        session_token: contextvars.Token[str | None] | None = None
        try:
            pending = await self._resolve(confirmation_id, decision, customer)
            session_token = bind_session_id(str(pending.conversation_id))
            yield StartEvent(conversation_id=pending.conversation_id, trace_id=trace_id)
            state = AgentState.from_dict(pending.agent_state)
            language = _state_language(pending.agent_state, customer.language)
            # Identity, session and clock come from the current request, never from the state.
            ctx = self._tool_context(customer, trace_id, language)
            guard = await self._output_guardrail(
                customer,
                await self._last_question(pending.conversation_id),
                language,
                [*state_trace_ids(pending.agent_state), trace_id],
            )
            loop = self._loop(guard)
            events = loop.resume(
                state,
                decision,
                ctx,
                suspend=self._suspend_fn(pending.conversation_id, ctx),
                conversation_id=pending.conversation_id,
            )
            driven = self._degrade_on_failure(
                self._drive(
                    events,
                    pending.conversation_id,
                    None,
                    partial=lambda: loop.partial_text,
                    is_settled=lambda: loop.resumed_call_settled,
                    language=language,
                    trace_id=trace_id,
                    guard=guard,
                ),
                pending.conversation_id,
                customer.id,
                language,
                trace_id,
            )
            driving = True  # from here `_drive` handles a cancellation
            async with closing_stream(driven) as stream:
                async for event in stream:
                    yield event
        except (asyncio.CancelledError, GeneratorExit):
            if not driving:
                # Cancelled between the resolution and the loop (e.g. right after `start`):
                # the confirmation goes back to pending instead of being committed at teardown
                # as confirmed without its tool.
                await self._rollback_cancelled("before_resume")
            raise
        finally:
            if session_token is not None:
                _reset_session_id(session_token)

    async def _resolve(
        self, confirmation_id: uuid.UUID, decision: Decision, customer: CurrentCustomer
    ) -> PendingConfirmation:
        try:
            pending = await self.confirmations.resolve(confirmation_id, customer.id, decision)
        except AtlasError as exc:
            if exc.code is ErrorCode.CONFIRMATION_NOT_PENDING:
                # Keep an `expired` mark: the request session rolls back after the error.
                await self.session.commit()
            raise
        log_event(
            "chat_confirmation_resolved",
            confirmation_id=str(pending.id),
            tool=pending.tool,
            decision=decision,
        )
        return pending

    async def _drive(
        self,
        events: AsyncIterator[AgentEvent],
        conversation_id: uuid.UUID,
        chunks: list[RetrievedChunk] | None,
        *,
        partial: Callable[[], str],
        language: Lang,
        trace_id: str,
        is_settled: Callable[[], bool] = lambda: True,
        guard: "_OutputGuardrail | None" = None,
    ) -> AsyncIterator[AgentEvent]:
        """Forward the loop's events, then store the answer and emit the final `done`.

        A cancellation before the answer is committed stores `partial()` (the loop's
        `partial_text`: the text of the model call in progress) as a `cancelled` message and
        commits the tools that already ran (see the module docstring). While `is_settled()` is
        False (a resume whose confirmed tool has not run yet) it rolls back instead.
        """
        progress = _Progress()
        settle = self._settle(events, conversation_id, chunks, progress, guard)
        try:
            async with closing_stream(settle) as settled:
                async for event in settled:
                    yield event
        except (asyncio.CancelledError, GeneratorExit):
            if progress.stored:
                raise
            if is_settled():
                await self._persist_cancelled(
                    [settle, events], conversation_id, partial(), language, trace_id
                )
            else:
                await self._rollback_cancelled("before_confirmed_tool", [settle, events])
            raise

    async def _degrade_on_failure(
        self,
        events: AsyncIterator[AgentEvent],
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        language: Lang,
        trace_id: str,
    ) -> AsyncIterator[AgentEvent]:
        """Forward `events`; when every provider fails (`AllProvidersFailed`, RES-06, RES-92)
        end the turn with the degraded answer and a ticket instead of an error."""
        try:
            async with closing_stream(events) as stream:
                async for event in stream:
                    yield event
        except AllProvidersFailed as exc:
            async for event in self._degraded(
                conversation_id, customer_id, language, trace_id, exc
            ):
                yield event

    async def _degraded(
        self,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        language: Lang,
        trace_id: str,
        exc: AllProvidersFailed,
    ) -> AsyncIterator[AgentEvent]:
        """Open the ticket and store the degraded answer in one commit (RES-06). What already
        ran in the turn (tools and their audit rows, a confirmation's resolution) is committed
        with it and never run again (RES-91)."""
        ticket = await open_ticket(
            self.session,
            conversation_id=conversation_id,
            customer_id=customer_id,
            reason=ALL_PROVIDERS_FAILED,
            trace_id=trace_id,
        )
        cause = exc.__cause__
        log_event(
            "chat_degraded",
            conversation_id=str(conversation_id),
            ticket_id=str(ticket.id),
            reason=ALL_PROVIDERS_FAILED,
            error_type=type(cause).__name__ if cause is not None else "CircuitOpen",
        )
        content = user_message(MessageKey.DEGRADED_RESPONSE, language)
        async for event in self._finish(
            conversation_id, content, language, trace_id, status="degraded"
        ):
            yield event

    async def _persist_cancelled(
        self,
        streams: list[AsyncIterator[AgentEvent]],
        conversation_id: uuid.UUID,
        partial: str,
        language: Lang,
        trace_id: str,
    ) -> None:
        """Store the partial answer as `cancelled` and commit what already ran (STR-07)."""
        with anyio.CancelScope(shield=True):
            try:
                for stream in streams:
                    await aclose_stream(stream)
                message_id: int | None = None
                if partial:
                    message = await self.repository.append_message(
                        conversation_id, "assistant", partial, "cancelled", language, trace_id
                    )
                    message_id = message.id
                await self.session.commit()
            except Exception as exc:
                log_event("chat_cancel_persist_failed", exc_type=type(exc).__name__)
                return
        log_event(
            "chat_stream_cancelled",
            conversation_id=str(conversation_id),
            message_id=message_id,
            partial_chars=len(partial),
        )

    async def _rollback_cancelled(
        self, stage: str, streams: list[AsyncIterator[AgentEvent]] | None = None
    ) -> None:
        """Undo the request's uncommitted work (the confirmation resolution) on a cancellation,
        shielded like `_persist_cancelled`; the confirmation stays pending."""
        with anyio.CancelScope(shield=True):
            try:
                for stream in streams or []:
                    await aclose_stream(stream)
                await self.session.rollback()
            except Exception as exc:
                log_event("chat_cancel_rollback_failed", exc_type=type(exc).__name__)
                return
        log_event("chat_stream_cancelled_unsettled", stage=stage)

    async def rollback(self) -> None:
        """Undo what the request did since its last commit (the SSE route calls it when the
        stream fails, since the request session would otherwise commit at teardown)."""
        await self.session.rollback()

    async def _settle(
        self,
        events: AsyncIterator[AgentEvent],
        conversation_id: uuid.UUID,
        chunks: list[RetrievedChunk] | None,
        progress: "_Progress",
        guard: "_OutputGuardrail | None" = None,
    ) -> AsyncIterator[AgentEvent]:
        """`_drive` without the cancellation handling; records in `progress` when the answer
        is committed."""
        final: DoneEvent | None = None
        try:
            async with closing_stream(events) as loop_events:
                async for event in loop_events:
                    if isinstance(event, DoneEvent):
                        final = event
                        break
                    if guard is not None and isinstance(event, ToolEndEvent):
                        # GRD-08: the next model call is checked against what really ran.
                        await guard.refresh(self.session)
                    yield event
        except LLMError as exc:
            raise _llm_unavailable(exc) from exc
        if final is None:
            log_event("chat_agent_without_terminal_event")
            raise AtlasError(ErrorCode.INTERNAL_ERROR, 500)

        if final.status == "awaiting_confirmation" and final.confirmation is not None:
            content = _confirmation_text(final.confirmation, final.language)
            async for event in self._finish(
                conversation_id,
                content,
                final.language,
                final.trace_id,
                status="awaiting_confirmation",
                confirmation=final.confirmation,
            ):
                progress.stored = True  # `_finish` yields only after its commit
                yield event
            return

        status = final.status if final.status != "awaiting_confirmation" else "completed"
        async for event in self._finish(
            conversation_id,
            final.content,
            final.language,
            final.trace_id,
            chunks=chunks if status != "blocked" else None,
            status=status,
        ):
            progress.stored = True  # `_finish` yields only after its commit
            yield event

    async def _finish(
        self,
        conversation_id: uuid.UUID,
        content: str,
        language: Lang,
        trace_id: str,
        *,
        chunks: list[RetrievedChunk] | None = None,
        status: ChatStatus = "completed",
        confirmation: ConfirmationPrompt | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Store the assistant reply, commit, then emit its citations and the `done` event."""
        citations = extract_citations(content, chunks) if chunks is not None else []
        message_status = status if status in ("blocked", "degraded") else "complete"
        reply = await self.repository.append_message(
            conversation_id, "assistant", content, message_status, language, trace_id
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
            status=status,
            content=content,
            language=language,
            trace_id=trace_id,
            conversation_id=conversation_id,
            message_id=reply.id,
            confirmation=confirmation,
        )

    async def _refuse_pending(self, conversation: Conversation, trace_id: str) -> None:
        """TOOL-97: a new message refuses the pending confirmations of the conversation."""
        refused = await self.confirmations.refuse_pending(conversation.id)
        for row in refused:
            note = REFUSED_BY_NEW_MESSAGE_NOTE.format(tool=row.tool, target=_target(row.arguments))
            await self.repository.append_message(
                conversation.id, "assistant", note, "complete", None, trace_id
            )
            log_event(
                "chat_confirmation_refused_by_message",
                confirmation_id=str(row.id),
                tool=row.tool,
            )

    def _loop(self, guard: "_OutputGuardrail | None" = None) -> AgentLoop:
        async def audit_reader(trace_id: str) -> list[Any]:
            return list(await list_successful_actions(self.session, trace_id))

        output_filter = guard.filter if guard is not None else None
        return AgentLoop(
            self.llm, self.registry, self.settings, audit_reader, output_filter=output_filter
        )

    def _input_verdict(self, message: str) -> InputVerdict | None:
        """`InputGuard` verdict, or None when the guardrails are off."""
        if not self.settings.guardrails_enabled:
            return None
        return InputGuard(self.settings).check(message)

    async def _block_input(
        self,
        conversation_id: uuid.UUID,
        masked: str,
        detected: DetectedLanguage,
        fallback: Lang,
        verdict: InputVerdict,
        trace_id: str,
    ) -> AsyncIterator[AgentEvent]:
        """GRD-06: store the masked message and the standard reply, both `blocked`, without
        any LLM call."""
        language: Lang = detected if detected in ("pt", "en") else fallback
        await self.repository.append_message(
            conversation_id, "user", masked, "blocked", detected, trace_id
        )
        await self.session.commit()
        log_event(
            "guardrail_blocked",
            stage="input",
            reasons=list(verdict.reasons),
            conversation_id=str(conversation_id),
        )
        yield StartEvent(conversation_id=conversation_id, trace_id=trace_id)
        async for event in self._finish(
            conversation_id, _blocked_text(language), language, trace_id, status="blocked"
        ):
            yield event

    def _log_message(self, conversation_id: uuid.UUID, message: str, language: str) -> None:
        """OBS-91: the logged content is the masked text; without guardrails it is not logged."""
        fields: dict[str, object] = {"conversation_id": str(conversation_id)}
        if self.settings.guardrails_enabled:
            fields["content"] = message
        log_event("chat_message", language=language, chars=len(message), **fields)

    async def _output_guardrail(
        self, principal: CurrentCustomer, question: str, language: Lang, trace_ids: list[str]
    ) -> "_OutputGuardrail | None":
        """The output filter of one request, or None when the guardrails are off."""
        if not self.settings.guardrails_enabled:
            return None
        ctx = OutputContext(
            customer_id=principal.id,
            own_order_ids=set(),
            own_email=principal.email,
            successful_tools=set(),
            question=question,
            language=language,
            question_order_ids=question_order_ids(question),
        )
        guard = _OutputGuardrail(
            filter=StreamOutputFilter(OutputGuard(self.guardrail_llm, self.settings), ctx),
            ctx=ctx,
            trace_ids=list(dict.fromkeys(trace_ids)),
        )
        await guard.refresh(self.session)
        return guard

    async def _last_question(self, conversation_id: uuid.UUID) -> str:
        """The last user message of the conversation (stored masked), for the topic check."""
        messages = await self.repository.list_messages(conversation_id)
        return next((m.content for m in reversed(messages) if m.role == "user"), "")

    def _tool_context(
        self, principal: CurrentCustomer, trace_id: str, language: Lang
    ) -> ToolContext:
        return ToolContext(
            customer_id=principal.id,
            customer_email=principal.email,
            trace_id=trace_id,
            session=self.session,
            language=language,
            now=datetime.now(UTC),
        )

    def _suspend_fn(self, conversation_id: uuid.UUID, ctx: ToolContext) -> SuspendFn:
        async def suspend(call: ToolCall, state: AgentState) -> ConfirmationPrompt:
            row = await self.confirmations.create(
                conversation_id=conversation_id,
                customer_id=ctx.customer_id,
                tool=call.name,
                arguments=dict(call.arguments),
                agent_state=state.to_dict(),
                trace_id=ctx.trace_id,
            )
            amount_cents = await self._refund_amount(call, ctx)
            log_event("chat_confirmation_requested", confirmation_id=str(row.id), tool=call.name)
            return ConfirmationPrompt(
                id=row.id,
                tool=call.name,
                target=_target(call.arguments),
                amount_cents=amount_cents,
                currency=CURRENCY if amount_cents is not None else None,
            )

        return suspend

    async def _refund_amount(self, call: ToolCall, ctx: ToolContext) -> int | None:
        """Amount shown for `issue_refund`: the total of the session customer's own order,
        through `check_refund_policy` (owner-filtered), never a value from the model, and only
        when the policy says the order is eligible."""
        if call.name != "issue_refund":
            return None
        order_id = _order_id(call.arguments)
        if order_id is None:
            return None
        result = await check_refund_policy(CheckRefundArgs(order_id=order_id), ctx)
        if not result.ok or result.content.get("eligible") is not True:
            return None
        amount = result.content.get("amount_cents")
        return amount if isinstance(amount, int) else None

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
        tools: list[ToolSpec] | None = None,
    ) -> LLMRequest:
        if chunks is None:
            return build_base_request(
                language=language,
                history=window,
                tools=list(tools or []),
                layout=self.settings.prompt_layout,
            )
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
            tools=list(tools or []),
            layout=self.settings.prompt_layout,
        )

    async def _window(self, conversation: Conversation) -> HistoryWindow:
        """History window of `conversation` (CT-22); stores a new summary when one is made."""
        upto = conversation.summary_upto_message_id
        # Blocked turns (an attack and its standard reply) never go back to the model.
        pending = [
            message
            for message in await self.repository.list_messages(conversation.id)
            if (upto is None or message.id > upto) and message.status != "blocked"
        ]
        try:
            window = await build_history(
                pending,
                conversation.summary,
                self.settings.context_budget_tokens,
                LLMSummarizer(self.llm),
                mode=self.settings.history_mode,
            )
        except LLMError as exc:
            raise _llm_unavailable(exc) from exc
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
        return window

    async def _llm_reply(
        self,
        conversation: Conversation,
        language: Lang,
        message: str,
        chunks: list[RetrievedChunk] | None,
    ) -> str:
        window = await self._window(conversation)
        try:
            response = await self.llm.complete(self._request(window, language, message, chunks))
        except LLMError as exc:
            raise _llm_unavailable(exc) from exc
        return response.text


@dataclass
class _Progress:
    """Whether the answer of a streamed turn is already committed."""

    stored: bool = False


@dataclass
class _OutputGuardrail:
    """The output filter of one request and the context it checks against."""

    filter: StreamOutputFilter
    ctx: OutputContext
    trace_ids: list[str]

    async def refresh(self, session: AsyncSession) -> None:
        """Read again what the answer may state: the customer's orders, which of them are
        cancelled or refunded (one query), and the tools with a `success` audit row in the
        request's traces (GRD-08). Called once at the start and after every `tool_end`, so a
        cancellation or refund done in this request counts."""
        statement = (
            select(Order.id, Order.status, Refund.id)
            .outerjoin(Refund, Refund.order_id == Order.id)
            .where(Order.customer_id == self.ctx.customer_id)
        )
        for order_id, status, refund_id in (await session.execute(statement)).all():
            self.ctx.own_order_ids.add(order_id)
            if status == OrderStatus.CANCELLED:
                self.ctx.cancelled_order_ids.add(order_id)
            if refund_id is not None:
                self.ctx.refunded_order_ids.add(order_id)
        for trace_id in self.trace_ids:
            rows = await list_successful_actions(session, trace_id)
            self.ctx.successful_tools.update(row.tool for row in rows)

    async def check_text(self, text: str) -> str | None:
        """Whole-answer check (the plain chat path): the released text, or None if blocked."""
        released = "".join(await self.filter.feed(text))
        outcome = await self.filter.finish()
        if outcome.blocked:
            log_event("guardrail_blocked", stage="output", reason=outcome.reason)
            return None
        return released + outcome.released


class OfflineTopicClient:
    """Topic classifier of the offline `fake` provider: every answer is on topic.

    The fake chat client replays a script, so it cannot also answer the topic check; tests that
    exercise the classifier inject their own `guardrail_llm`. Never used with a real provider.
    """

    VERDICT = '{"on_topic": true, "reason": "offline fake provider"}'

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text=self.VERDICT,
            tool_calls=[],
            usage=Usage(input_tokens=0, output_tokens=0),
            model=FAKE_MODEL,
            provider=FAKE_PROVIDER,
            stop_reason="end_turn",
        )

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        response = await self.complete(request)
        yield LLMStreamEvent(kind="text_delta", text=response.text)
        yield LLMStreamEvent(kind="usage", usage=response.usage)
        yield LLMStreamEvent(kind="stop", stop_reason=response.stop_reason)


# How many `inner` wrappers (instrumentation, recording, pacing) `_is_fake_script` looks through.
_MAX_WRAPPERS = 5


def _is_fake_script(llm: object) -> bool:
    """The chat client is the offline fake provider: a `FakeLLMClient`, or a client that names
    `fake` as its provider, possibly behind `inner` wrappers. Its script holds chat answers only.
    """
    for _ in range(_MAX_WRAPPERS):
        if isinstance(llm, FakeLLMClient) or getattr(llm, "provider", None) == FAKE_PROVIDER:
            return True
        llm = getattr(llm, "inner", None)
        if llm is None:
            return False
    return False


def _reset_session_id(token: contextvars.Token[str | None]) -> None:
    """Unbind the conversation id (OBS-01). A stream closed from another context (e.g. by the
    garbage collector) cannot reset a token of the request context; the binding then simply
    dies with that context."""
    with contextlib.suppress(ValueError):
        reset_session_id(token)


def _default_guardrail_llm(llm: LLMClient) -> LLMClient:
    """The topic classifier when none is injected: the chat client, or the offline one when the
    chat client is the fake provider's script (`_is_fake_script`)."""
    return OfflineTopicClient() if _is_fake_script(llm) else llm


# Order numbers the customer wrote (`#13`, "order 13", "pedido 13"), as the output guard reads
# them in the answer (CT-55).
_QUESTION_ORDER = re.compile(
    r"(?<![\w&#])#\s?(?P<hash>\d+)\b"
    r"|\b(?:orders?|pedidos?|encomendas?)\s+(?:de\s+)?"
    r"(?:(?:number|numero|num\.?|no\.?|n\.)\s*)?#?\s*(?P<word>\d+)\b",
    re.IGNORECASE,
)


def question_order_ids(question: str) -> set[int]:
    """Order numbers cited in the (masked) question, which the answer may repeat even when they
    are not the customer's ("I couldn't find order #13")."""
    text = unicodedata.normalize("NFKD", question)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in {"Mn", "Cf"})
    return {
        int(match.group("hash") or match.group("word")) for match in _QUESTION_ORDER.finditer(text)
    }


def _blocked_text(language: Lang) -> str:
    return user_message(MessageKey.GUARDRAIL_BLOCKED, language)


def _llm_unavailable(exc: LLMError) -> AtlasError:
    log_event(
        "chat_llm_unavailable",
        exc_type=type(exc).__name__,
        recoverable=exc.recoverable,
        status=exc.status,
    )
    return AtlasError(ErrorCode.LLM_UNAVAILABLE, 502)


def _order_id(arguments: dict[str, Any]) -> int | None:
    value = arguments.get("order_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 1:
        return value
    if isinstance(value, str) and value.strip().lstrip("#").isdigit():
        number = int(value.strip().lstrip("#"))
        return number if number >= 1 else None
    return None


def _target(arguments: dict[str, Any]) -> str:
    """`#<order_id>` as the web card shows it."""
    order_id = _order_id(arguments)
    if order_id is not None:
        return f"#{order_id}"
    return f"#{arguments.get('order_id', '?')}"


def _confirmation_text(prompt: ConfirmationPrompt, language: Lang) -> str:
    action = _ACTION_NAMES.get(prompt.tool, {}).get(language, prompt.tool)
    return user_message(MessageKey.CONFIRM_ACTION, language, action=action, target=prompt.target)


def _state_language(state: dict[str, Any], fallback: Lang) -> Lang:
    language = state.get("language")
    return cast(Lang, language) if language in ("pt", "en") else fallback


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


@lru_cache(maxsize=1)
def _guardrail_llm_client() -> LLMClient | None:
    """One client per process for `ATLAS_GUARDRAIL_MODEL`; None without it (the chat client
    is used, see `_default_guardrail_llm`)."""
    settings = get_settings()
    if not settings.guardrail_model:
        return None
    return build_llm_client(settings.model_copy(update={"primary_model": settings.guardrail_model}))


@lru_cache(maxsize=1)
def _embedder() -> EmbeddingClient:
    """One embedding client per process, built from the settings on first use."""
    return build_embedder(get_settings())


async def close_embedder() -> None:
    """Close the cached embedder's HTTP client, if one was built (app shutdown)."""
    if _embedder.cache_info().currsize:
        embedder = _embedder()
        _embedder.cache_clear()
        if isinstance(embedder, OpenAIEmbeddingClient):
            await embedder.aclose()


@lru_cache(maxsize=1)
def _registry() -> ToolRegistry:
    """The default tool registry, built once per process (it holds no state per request)."""
    return build_default_registry()


def get_chat_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ChatService:
    """FastAPI dependency: the request session, the cached LLM client and tool registry, and
    a retriever.

    The retriever opens its own sessions (`get_sessionmaker`). The embedder is built only when
    the configured retrieval mode is a vector mode, so `fulltext` needs no embedding key.
    Tests override it with `app.dependency_overrides`.
    """
    settings = get_settings()
    embedder = _embedder() if is_vector_mode(settings.retrieval_mode) else None
    retriever = Retriever(get_sessionmaker(), embedder, settings)
    return ChatService(
        _llm_client(),
        settings,
        session,
        retriever=retriever,
        registry=_registry(),
        guardrail_llm=_guardrail_llm_client(),
    )
