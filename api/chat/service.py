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
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cache, lru_cache
from typing import Annotated, Any, cast

import anyio
from fastapi import Depends
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
)
from api.agent.loop import (
    AgentLoop,
    AgentState,
    AgentTurn,
    Decision,
    SuspendFn,
    aclose_stream,
    closing_stream,
)
from api.agent.models import PendingConfirmation
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
from api.llm.client import LLMClient, LLMError, LLMRequest, SystemBlock, ToolCall, ToolSpec
from api.llm.factory import build_llm_client
from api.messages import Lang, MessageKey, user_message
from api.observability.logging import get_trace_id, log_event
from api.rag.citations import extract_citations
from api.rag.embeddings import EmbeddingClient, OpenAIEmbeddingClient, build_embedder
from api.rag.retriever import RetrievalUnavailable, RetrievedChunk, Retriever, is_vector_mode
from api.tools.audit import list_successful_actions
from api.tools.catalog import build_default_registry
from api.tools.models import CURRENCY
from api.tools.refunds import CheckRefundArgs, check_refund_policy
from api.tools.registry import ToolContext, ToolRegistry

__all__ = [
    "LANGUAGE_NAMES",
    "LANGUAGE_PLACEHOLDER",
    "UNSUPPORTED_LANGUAGE_REPLY_LANG",
    "ChatService",
    "get_chat_service",
    "system_prompt",
]

UNSUPPORTED_LANGUAGE_REPLY_LANG: Lang = "en"

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
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.session = session
        self.retriever = retriever
        self.registry = registry if registry is not None else build_default_registry()
        self.repository = ConversationRepository(session)
        self.confirmations = ConfirmationStore(session, settings)

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
        # `get_owned` already checked that the conversation is the principal's, so refusing its
        # pending confirmations cannot touch another customer's (`refuse_pending` takes no owner).
        conversation = await self._conversation(request, principal)
        if request.conversation_id is not None:
            await self._refuse_pending(conversation, trace_id)
        fallback: Lang = request.ui_language or principal.language
        detected = detect_language(request.message, fallback=fallback)
        await self.repository.append_message(
            conversation.id, "user", request.message, "complete", detected, trace_id
        )
        await self.session.commit()
        # Round 1: the first event, once the user message is committed and the conversation
        # exists, before any token or tool (the JSON `collect` ignores it).
        yield StartEvent(conversation_id=conversation.id, trace_id=trace_id)

        if detected == "other":
            content = user_message(MessageKey.UNSUPPORTED_LANGUAGE, UNSUPPORTED_LANGUAGE_REPLY_LANG)
            log_event("chat_unsupported_language", conversation_id=str(conversation.id))
            async for event in self._finish(
                conversation.id, content, UNSUPPORTED_LANGUAGE_REPLY_LANG, trace_id
            ):
                yield event
            return

        reply_language: Lang = detected
        try:
            chunks = await self._retrieve(request.message, reply_language)
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

        if not self.tools_enabled:
            content = await self._llm_reply(conversation, reply_language, request.message, chunks)
            async for event in self._finish(
                conversation.id, content, reply_language, trace_id, chunks=chunks
            ):
                yield event
            return

        window = await self._window(conversation)
        llm_request = self._request(
            window, reply_language, request.message, chunks, self.registry.specs()
        )
        ctx = self._tool_context(principal, trace_id, reply_language)
        turn = AgentTurn(
            request=llm_request,
            ctx=ctx,
            suspend=self._suspend_fn(conversation.id, ctx),
            conversation_id=conversation.id,
        )
        loop = self._loop()
        driven = self._drive(
            loop.run(turn),
            conversation.id,
            chunks,
            partial=lambda: loop.partial_text,
            language=reply_language,
            trace_id=trace_id,
        )
        async with closing_stream(driven) as stream:
            async for event in stream:
                yield event

    async def _resume(
        self, confirmation_id: uuid.UUID, decision: Decision, customer: CurrentCustomer
    ) -> AsyncIterator[AgentEvent]:
        trace_id = get_trace_id()
        driving = False
        try:
            pending = await self._resolve(confirmation_id, decision, customer)
            yield StartEvent(conversation_id=pending.conversation_id, trace_id=trace_id)
            state = AgentState.from_dict(pending.agent_state)
            language = _state_language(pending.agent_state, customer.language)
            # Identity, session and clock come from the current request, never from the state.
            ctx = self._tool_context(customer, trace_id, language)
            loop = self._loop()
            events = loop.resume(
                state,
                decision,
                ctx,
                suspend=self._suspend_fn(pending.conversation_id, ctx),
                conversation_id=pending.conversation_id,
            )
            driven = self._drive(
                events,
                pending.conversation_id,
                None,
                partial=lambda: loop.partial_text,
                is_settled=lambda: loop.resumed_call_settled,
                language=language,
                trace_id=trace_id,
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
    ) -> AsyncIterator[AgentEvent]:
        """Forward the loop's events, then store the answer and emit the final `done`.

        A cancellation before the answer is committed stores `partial()` (the loop's
        `partial_text`: the text of the model call in progress) as a `cancelled` message and
        commits the tools that already ran (see the module docstring). While `is_settled()` is
        False (a resume whose confirmed tool has not run yet) it rolls back instead.
        """
        progress = _Progress()
        settle = self._settle(events, conversation_id, chunks, progress)
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
            chunks=chunks,
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
        reply = await self.repository.append_message(
            conversation_id, "assistant", content, "complete", language, trace_id
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

    def _loop(self) -> AgentLoop:
        async def audit_reader(trace_id: str) -> list[Any]:
            return list(await list_successful_actions(self.session, trace_id))

        return AgentLoop(self.llm, self.registry, self.settings, audit_reader)

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
            system = [SystemBlock(system_prompt(language))]
            if window.summary:
                system.append(summary_block(window.summary))
            return LLMRequest(
                system=system, messages=window.messages, tools=list(tools or []), purpose="chat"
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
    return ChatService(_llm_client(), settings, session, retriever=retriever, registry=_registry())
